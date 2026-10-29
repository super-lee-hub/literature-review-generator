from __future__ import annotations

import configparser
import hashlib
import json
import re
import threading
import traceback
from collections.abc import Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import fitz  # type: ignore

from runtime.control_plane import ReviewControlPlane
from runtime.job_spec import RuntimeJobSpec, RuntimeSourceSpec
from runtime.runner import AgentRuntimeRunner
from services.artifact_registry import file_sha256
from runtime.provider_runtime import (
    ProviderAggregateBudgetV2,
    canonical_provider_request_payload,
    hash_json,
)


from services.artifact_registry import ArtifactDependencyRefV2, ArtifactRegistry
from services.job_workspace import JobWorkspace, publish_json_artifact
from services.review_generation_service import ReviewGenerationService
from services.settings import ApplicationSettings
from validation.execution_service import ValidationExecutionService
from tests.test_current_runtime_full_e2e import (
    _outline_provider_response,
    _reader_summary,
)


_CURRENT82_FIXTURE_PAPERS = frozenset({
    "studya_unknown_author",
    "studyb_unknown_author",
    "studyc_unknown_author",
    "studyd_unknown_author",
})
_CURRENT82_SOURCE_REPLAY_PATH = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "run_first_current82_full_scope_replay.json"
)
_CURRENT82_SOURCE_REPLAY = json.loads(_CURRENT82_SOURCE_REPLAY_PATH.read_text(encoding="utf-8"))
_CURRENT86_ALIAS_MAP_PATH = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "run_first_current86_evidence_alias_map.json"
)
_CURRENT86_ALIAS_MAP_SHA256 = "73a8fd0bcce11e9802579c99845c2ca88d966b571711fa6dfe4392de0d123db2"


def _current86_snapshot_projection(request: Mapping[str, Any]) -> dict[str, Any] | None:
    """Canonicalize only the verified current86 paper/relation aliases for snapshot comparison."""
    try:
        alias_bytes = _CURRENT86_ALIAS_MAP_PATH.read_bytes()
        if hashlib.sha256(alias_bytes).hexdigest() != _CURRENT86_ALIAS_MAP_SHA256:
            return None
        alias_map = json.loads(alias_bytes)
        from outline.evidence_alias import build_alias_map, canonicalize_structural

        if not isinstance(alias_map, Mapping) or alias_map.get("artifact_type") != "outline_evidence_alias_map":
            return None
        if alias_map.get("artifact_version") != "v1":
            return None
        if build_alias_map(
            list((alias_map.get("papers") or {}).keys()),
            list((alias_map.get("relations") or {}).keys()),
        ) != alias_map:
            return None
        canonical_papers = set((alias_map.get("papers") or {}).keys())
        if canonical_papers != _CURRENT82_FIXTURE_PAPERS:
            return None

        projection = canonicalize_structural(dict(request), alias_map)
        paper_values = {
            str(value) for value in projection.get("paper_keys") or () if str(value)
        }
        if not paper_values or not paper_values.issubset(_CURRENT82_FIXTURE_PAPERS):
            return None

        raw_scope = request.get("candidate_output_scope")
        projected_scope = projection.get("candidate_output_scope")
        if not isinstance(raw_scope, Mapping) or not isinstance(projected_scope, Mapping):
            return None
        raw_slots = [row for row in raw_scope.get("claim_slots") or () if isinstance(row, Mapping)]
        projected_slots = [
            row for row in projected_scope.get("claim_slots") or () if isinstance(row, Mapping)
        ]
        if len(raw_slots) != len(projected_slots):
            return None
        slot_ids_by_alias: dict[str, set[str]] = {}
        canonical_slot_ids_by_alias: dict[str, set[str]] = {}
        for raw_slot, projected_slot in zip(raw_slots, projected_slots, strict=True):
            raw_slot_id = str(raw_slot.get("claim_slot_id") or "")
            projected_slot_id = str(projected_slot.get("claim_slot_id") or "")
            projected_relations = {
                str(value) for value in projected_slot.get("relation_ids") or () if str(value)
            }
            for relation_alias in (
                str(value) for value in raw_slot.get("relation_ids") or () if str(value)
            ):
                canonical_relation = (alias_map.get("relations_reverse") or {}).get(relation_alias)
                if canonical_relation is None:
                    continue
                slot_ids_by_alias.setdefault(relation_alias, set()).add(raw_slot_id)
                if str(canonical_relation) not in projected_relations:
                    return None
                canonical_slot_ids_by_alias.setdefault(relation_alias, set()).add(projected_slot_id)

        raw_tasks = [
            row for row in raw_scope.get("organization_tasks") or () if isinstance(row, Mapping)
        ]
        projected_tasks = [
            row for row in projected_scope.get("organization_tasks") or () if isinstance(row, Mapping)
        ]
        projected_tasks_by_id = {
            str(row.get("task_id") or ""): row for row in projected_tasks
        }
        tasks_by_alias: dict[str, list[Mapping[str, Any]]] = {}
        for task in raw_tasks:
            contract = task.get("task_contract")
            if not isinstance(contract, Mapping):
                continue
            relation_alias = str(contract.get("relation_id") or "")
            if relation_alias in (alias_map.get("relations_reverse") or {}):
                tasks_by_alias.setdefault(relation_alias, []).append(task)
        if set(tasks_by_alias) != set(slot_ids_by_alias):
            return None
        for relation_alias, raw_slot_ids in slot_ids_by_alias.items():
            canonical_relation = str((alias_map.get("relations_reverse") or {}).get(relation_alias) or "")
            expected_task_id = f"relation_comparison:{canonical_relation}"
            matching_tasks = [
                row for row in tasks_by_alias.get(relation_alias, ())
                if str(row.get("task_id") or "") == expected_task_id
            ]
            if len(matching_tasks) != 1:
                return None
            task = matching_tasks[0]
            contract = task.get("task_contract")
            if (
                not isinstance(contract, Mapping)
                or contract.get("task_id") != expected_task_id
                or contract.get("relation_id") != relation_alias
                or contract.get("schema_version") != "relation-comparison-task/v1"
                or contract.get("relation_id_is_not_a_conclusion") is not True
            ):
                return None
            support_slot_ids = {
                str(value) for value in task.get("support_slot_ids") or () if str(value)
            }
            if support_slot_ids != raw_slot_ids or len(support_slot_ids) != len(
                list(task.get("support_slot_ids") or ())
            ):
                return None
            projected_task = projected_tasks_by_id.get(expected_task_id)
            if not isinstance(projected_task, Mapping):
                return None
            projected_task_papers = {
                str(value) for value in projected_task.get("paper_keys") or () if str(value)
            }
            assigned_slot_papers = {
                str(slot.get("paper_key") or "")
                for slot in projected_slots
                if str(slot.get("claim_slot_id") or "")
                in canonical_slot_ids_by_alias.get(relation_alias, set())
            }
            if projected_task_papers != assigned_slot_papers:
                return None
        return projection
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        return None


def _write_source_pdf(path: Path, *, title: str, finding: str) -> None:
    document = fitz.open()
    page = document.new_page()
    page.insert_text(
        (72, 72),
        f"Title: {title}\n"
        "Methodology: A controlled study of 24 participants.\n"
        "Analysis: Descriptive comparison of observed group means.\n"
        f"Results: {finding}\n"
        "Conclusion: Findings apply only to the observed sample.",
    )
    document.save(path)
    document.close()


def _source_bound_four_paper_reader_response(user_text: str) -> dict[str, Any]:
    source_rows = (
        ("study-a", "Study A", "Treatment improved the measured outcome by four points."),
        ("study-b", "Study B", "Treatment improved the measured outcome by two points."),
        ("study-c", "Study C", "Treatment improved the measured outcome by one point."),
        ("study-d", "Study D", "Treatment did not change the measured outcome."),
    )
    matches = [row for row in source_rows
               if re.search(rf"\bTitle\s*:\s*{re.escape(row[1])}\b", user_text, re.IGNORECASE)
               and row[2] in user_text]
    if len(matches) != 1:
        raise AssertionError("Reader fixture requires exactly one matching printed source title and finding")
    return _fixture_reader_summary(*matches[0])


def _fixture_reader_summary(paper_key: str, title: str, finding: str) -> dict[str, Any]:
    """Return a compact source-grounded Stage 1 result for the loopback fixture."""

    summary = _reader_summary(paper_key, title, finding)
    core = summary.get("core_analysis")
    if isinstance(core, dict):
        core.update(
            {
                "summary": f"{title}: {finding}",
                "key_points": [finding],
                "methodology": "Controlled sample (n=24).",
                "findings": finding,
                "conclusions": finding,
                "relevance": None,
                "limitations": "Findings apply only to this sample.",
                "research_gap": None,
                "theoretical_framework": None,
                "future_research_directions": [],
            }
        )
    summary["specialized_details"] = {
        "empirical": {
            "research_questions_or_hypotheses": [],
            "data_source_and_size": "Observed controlled sample of 24 participants.",
            "analysis_technique": "Descriptive comparison of observed group means.",
            "core_variables": {"independent": ["treatment"], "dependent": ["measured outcome"]},
            "sample_characteristics_or_context": "Observed controlled sample.",
        },
        "review": None,
        "conceptual": None,
    }
    return summary


def _grounded_fixture_candidate(
    node_id: str, request: Mapping[str, Any],
    fixture_trace: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if request.get("schema_version") == "candidate-comparison-fragments-request/v1":
        from tests.run_first_candidate_fragments_fixture import _comparison_response
        from outline.candidate_relation_fragments import validate_candidate_comparison_fragments_output_v1

        response = _comparison_response(node_id, request)
        scope = request["comparison_scope"]
        # These are labels of the four controlled PDFs, resolved through each
        # request's actual source claims rather than an opaque alias position.
        titles_by_finding = {
            "Treatment improved the measured outcome by four points.": "Study A",
            "Treatment improved the measured outcome by two points.": "Study B",
            "Treatment improved the measured outcome by one point.": "Study C",
            "Treatment did not change the measured outcome.": "Study D",
        }
        titles = {
            row["paper_key"]: titles_by_finding[row["text"]]
            for row in request["shared_semantic_context"]["interpretation_source_tables"]["source_claims"]
            if row.get("claim_type") == "empirical_finding" and row.get("text") in titles_by_finding
        }
        for index, group in enumerate(scope["claim_groups"]):
            if {titles.get(key) for key in group["paper_keys"]} != {"Study A", "Study B"}:
                continue
            group_id = group["claim_group_id"]
            slots = [dict(row, claim_group_index=0) for row in scope["claim_slots"]
                     if row["claim_group_id"] == group_id]
            adapted = {
                "candidate_id": "shared_comparison",
                "paper_keys": list(group["paper_keys"]),
                "candidate_output_scope": {
                    "claim_group_ids": [group_id], "claim_slots": slots,
                    "required_claim_group_indices": [0],
                    "organization_tasks": [dict(group, claim_group_index=0)],
                },
                "hierarchy": {"claim_group_ids": [group_id]},
                "evidence": [{"paper_key": key, "title": titles[key]} for key in group["paper_keys"]],
                "shared_semantic_context": request["shared_semantic_context"],
            }
            grounded = _grounded_fixture_candidate("shared_provider_generation", adapted, fixture_trace)
            claims = [text for section in grounded["sections"] for text in section["claims"]]
            if len(claims) != 1:
                raise AssertionError("shared A/B fixture requires one complete validated comparison")
            response["fragments"][index]["text"] = claims[0]
        validate_candidate_comparison_fragments_output_v1(response, request)
        return response
    if request.get("schema_version") == "candidate-organization-request/v1":
        from tests.run_first_candidate_fragments_fixture import _organization_response

        preferred = []
        for finding in ("Treatment improved the measured outcome by one point.",
                        "Treatment did not change the measured outcome."):
            candidates = [row for row in request["fragment_cards"]
                          if row[3] == "materialized" and len(row[2]) == 1
                          and finding.rstrip(".") in request["text_pool"][row[1]]]
            if not candidates:
                raise AssertionError("shared bank omitted an original C/D source finding")
            preferred.append(min(candidates, key=lambda row: (
                not request["task_refs"][row[4]].startswith("topic_synthesis:"), row[0],
            ))[0])
        return _organization_response(request, preferred_indices=preferred)
    if node_id.startswith("cross_group_comparison_provider"):
        return _outline_provider_response(node_id, dict(request), fixture_trace)["content"]
    if node_id.startswith("global_synthesis_provider"):
        return _outline_provider_response(node_id, dict(request), fixture_trace)["content"]
    base_node_id = node_id.split(":local:", 1)[0]
    if not base_node_id.endswith("_provider_generation"):
        return _outline_provider_response(node_id, dict(request), fixture_trace)["content"]

    scope = request.get("candidate_output_scope")
    hierarchy = request.get("hierarchy")
    if not isinstance(scope, Mapping) or not isinstance(hierarchy, Mapping):
        raise AssertionError("candidate fixture needs the assigned finite output scope")
    group_ids = [str(value) for value in scope.get("claim_group_ids") or () if str(value)]
    assigned_group_ids = [
        str(value) for value in hierarchy.get("claim_group_ids") or () if str(value)
    ] or group_ids
    if len(group_ids) != len(set(group_ids)) or set(group_ids) != set(assigned_group_ids):
        raise AssertionError(
            "candidate scope group ordering and durable shard assignment differ: "
            f"wire={group_ids}, assigned={assigned_group_ids}"
        )
    raw_required_indices = scope.get("required_claim_group_indices") or ()
    if not isinstance(raw_required_indices, (list, tuple)):
        raise AssertionError("candidate fixture required group indices must be an array")
    required_group_indices: set[int] = set()
    for value in raw_required_indices:
        if isinstance(value, bool):
            raise AssertionError("candidate fixture required group indices must be integers")
        try:
            index = int(value)
        except (TypeError, ValueError) as exc:
            raise AssertionError("candidate fixture required group indices must be integers") from exc
        if index < 0 or index >= len(group_ids):
            raise AssertionError("candidate fixture required group index is outside its assigned scope")
        required_group_indices.add(index)
    required_group_ids = {group_ids[index] for index in required_group_indices}

    task_rows = [
        dict(row) for row in scope.get("organization_tasks") or ()
        if isinstance(row, Mapping)
        and str(row.get("claim_group_id") or "") in set(assigned_group_ids)
        and str(row.get("task_id") or "").startswith("relation_comparison:")
    ]
    evidence = [dict(row) for row in request.get("evidence") or () if isinstance(row, Mapping)]
    paper_a = next((
        str(row.get("paper_key") or "") for row in evidence
        if str(row.get("title") or "").lower().replace(" ", "-") == "study-a"
    ), "")
    paper_b = next((
        str(row.get("paper_key") or "") for row in evidence
        if str(row.get("title") or "").lower().replace(" ", "-") == "study-b"
    ), "")
    ab_tasks = [
        row for row in task_rows
        if paper_a and paper_b
        and set(str(value) for value in row.get("paper_keys") or () if str(value)) == {paper_a, paper_b}
    ]
    relation_groups: list[tuple[dict[str, Any], int, list[dict[str, Any]]]] = []
    for task in ab_tasks:
        contract = task.get("task_contract")
        if (
            task.get("claim_seed") is not True
            or task.get("claim_kind") != "model_authored_relation_comparison_slot"
            or not isinstance(contract, Mapping)
            or contract.get("schema_version") != "relation-comparison-task/v1"
            or contract.get("task_id") != task.get("task_id")
            or contract.get("relation_id_is_not_a_conclusion") is not True
        ):
            raise AssertionError(f"relation task is missing its explicit comparison contract: {task}")

        group_id = str(task.get("claim_group_id") or "")
        group_index = group_ids.index(group_id)
        slot_rows = [
            dict(row) for row in scope.get("claim_slots") or ()
            if isinstance(row, Mapping) and int(row.get("claim_group_index", -1)) == group_index
        ]
        expected_slot_ids = {str(value) for value in task.get("support_slot_ids") or () if str(value)}
        if {str(row.get("claim_slot_id") or "") for row in slot_rows} != expected_slot_ids:
            raise AssertionError("assigned relation task wire omitted one or more exact source support slots")
        expected_papers = {str(value) for value in task.get("paper_keys") or () if str(value)}
        if len(expected_papers) != 2 or {str(row.get("paper_key") or "") for row in slot_rows} != expected_papers:
            raise AssertionError("assigned relation task must retain complete support from both papers")
        relation_groups.append((task, group_index, slot_rows))
    ab_groups = relation_groups

    shared = request.get("shared_semantic_context")
    tables = shared.get("interpretation_source_tables") if isinstance(shared, Mapping) else None
    if not isinstance(tables, Mapping):
        raise AssertionError("A/B task omitted projected source dependencies")
    dependencies = [row for row in tables.get("dependencies") or () if isinstance(row, Mapping)]
    source_fields = {
        str(row.get("source_field_id") or ""): row
        for row in tables.get("source_fields") or ()
        if isinstance(row, Mapping) and str(row.get("source_field_id") or "")
    }
    source_claims = {
        str(row.get("claim_id") or ""): row
        for row in tables.get("source_claims") or ()
        if isinstance(row, Mapping) and str(row.get("claim_id") or "")
    }
    snapshot_projection = _current86_snapshot_projection(request)
    def canonical_study_owners(row: Mapping[str, Any]) -> tuple[str, ...]:
        raw_owners = row.get("owner_study_ids") if "owner_study_ids" in row else ()
        if not isinstance(raw_owners, (tuple, list)):
            return ("<invalid-owner-shape>",)
        owners = {str(value).strip() for value in raw_owners if str(value).strip()}
        for key in ("study_id", "source_study_id"):
            value = str(row.get(key) or "").strip()
            if value:
                owners.add(value)
        return tuple(sorted(owners))

    def field_signature(row: Mapping[str, Any]) -> tuple[Any, ...]:
        return (
            str(row.get("paper_key") or ""),
            str(row.get("source_path") or ""),
            str(row.get("canonical_field") or ""),
            str(row.get("source_value") or "").strip(),
            str(row.get("derived_value") or "").strip(),
            str(row.get("scope") or ""),
            canonical_study_owners(row),
        )

    def snapshot_evidence_texts() -> dict[str, set[str]]:
        """Use exact saved evidence text only when the 4-paper projection matches."""

        if not isinstance(snapshot_projection, Mapping):
            return {}
        request_papers = {
            str(value) for value in snapshot_projection.get("paper_keys") or () if str(value)
        }
        if not request_papers or not request_papers.issubset(_CURRENT82_FIXTURE_PAPERS):
            return {}
        snapshot_request = _CURRENT82_SOURCE_REPLAY.get("request")
        if not isinstance(snapshot_request, Mapping):
            return {}
        snapshot_tables = (
            snapshot_request.get("shared_semantic_context", {}).get("interpretation_source_tables", {})
        )
        if not isinstance(snapshot_tables, Mapping):
            return {}
        projected_shared = snapshot_projection.get("shared_semantic_context")
        projected_tables = (
            projected_shared.get("interpretation_source_tables")
            if isinstance(projected_shared, Mapping)
            else None
        )
        projected_scope = snapshot_projection.get("candidate_output_scope")
        if not isinstance(projected_tables, Mapping) or not isinstance(projected_scope, Mapping):
            return {}
        projected_dependencies = [
            row for row in projected_tables.get("dependencies") or () if isinstance(row, Mapping)
        ]
        projected_source_claims = {
            str(row.get("claim_id") or ""): row
            for row in projected_tables.get("source_claims") or ()
            if isinstance(row, Mapping) and str(row.get("claim_id") or "")
        }
        projected_source_fields = {
            str(row.get("source_field_id") or ""): row
            for row in projected_tables.get("source_fields") or ()
            if isinstance(row, Mapping) and str(row.get("source_field_id") or "")
        }

        expected_claims = {
            (str(row.get("paper_key") or ""), str(row.get("claim_id") or "")): row
            for row in snapshot_tables.get("source_claims") or () if isinstance(row, Mapping)
        }
        actual_claims = {
            (str(row.get("paper_key") or ""), str(claim_id)): row
            for claim_id, row in projected_source_claims.items()
        }
        expected_fields = {
            field_signature(row)
            for row in snapshot_tables.get("source_fields") or () if isinstance(row, Mapping)
        }
        expected_field_by_id = {
            str(row.get("source_field_id") or ""): field_signature(row)
            for row in snapshot_tables.get("source_fields") or () if isinstance(row, Mapping)
        }
        actual_field_by_id = {
            field_id: field_signature(row) for field_id, row in projected_source_fields.items()
        }

        def dependency_signature(row: Mapping[str, Any], field_by_id: Mapping[str, tuple[Any, ...]]) -> tuple[Any, ...]:
            required_fields = tuple(sorted(
                field_by_id.get(str(field_id), ("missing", str(field_id)))
                for field_id in row.get("required_source_field_ids") or ()
            ))
            return (
                str(row.get("paper_key") or ""),
                str(row.get("primary_claim_id") or ""),
                tuple(sorted(str(value) for value in row.get("primary_evidence_ids") or () if str(value))),
                tuple(sorted(str(value) for value in row.get("required_source_claim_ids") or () if str(value))),
                tuple(sorted(str(value) for value in row.get("required_evidence_ids") or () if str(value))),
                required_fields,
                str(row.get("scope") or ""),
                str(row.get("study_id") or ""),
                str(row.get("reason") or ""),
            )

        expected_dependencies = {
            dependency_signature(row, expected_field_by_id)
            for row in snapshot_tables.get("dependencies") or () if isinstance(row, Mapping)
        }

        reachable_claims = {
            (str(row.get("paper_key") or ""), str(row.get("primary_claim_id") or ""))
            for row in projected_scope.get("claim_slots") or () if isinstance(row, Mapping)
        }
        used_field_ids = {
            str(value) for row in projected_scope.get("claim_slots") or () if isinstance(row, Mapping)
            for value in row.get("source_field_ids") or () if str(value)
        }
        used_evidence_ids = {
            str(value) for row in projected_scope.get("claim_slots") or () if isinstance(row, Mapping)
            for value in row.get("evidence_ids") or () if str(value)
        }
        reachable_dependency_indexes: set[int] = set()
        while True:
            before = len(reachable_claims)
            for paper_key, claim_id in tuple(reachable_claims):
                claim = actual_claims.get((paper_key, claim_id))
                expected_claim = expected_claims.get((paper_key, claim_id))
                if claim is None or expected_claim is None or any(
                    claim.get(key) != expected_claim.get(key)
                    for key in ("text", "evidence_ids", "claim_type", "source_locator")
                    if key in claim or key in expected_claim
                ) or canonical_study_owners(claim) != canonical_study_owners(expected_claim):
                    return {}
                used_evidence_ids.update(
                    str(value) for value in claim.get("evidence_ids") or () if str(value)
                )
                for index, dependency in enumerate(projected_dependencies):
                    if (
                        str(dependency.get("paper_key") or "") != paper_key
                        or str(dependency.get("primary_claim_id") or "") != claim_id
                    ):
                        continue
                    reachable_dependency_indexes.add(index)
                    used_evidence_ids.update(
                        str(value) for value in dependency.get("primary_evidence_ids") or () if str(value)
                    )
                    used_evidence_ids.update(
                        str(value) for value in dependency.get("required_evidence_ids") or () if str(value)
                    )
                    used_field_ids.update(
                        str(value) for value in dependency.get("required_source_field_ids") or () if str(value)
                    )
                    reachable_claims.update(
                        (paper_key, str(value))
                        for value in dependency.get("required_source_claim_ids") or () if str(value)
                    )
            if len(reachable_claims) == before:
                break

        if any(
            index in reachable_dependency_indexes
            and dependency_signature(row, actual_field_by_id) not in expected_dependencies
            for index, row in enumerate(projected_dependencies)
        ):
            return {}
        if any(
            (row := projected_source_fields.get(field_id)) is None
            or field_signature(row) not in expected_fields
            for field_id in used_field_ids
        ):
            return {}

        expected_slots = set()
        for row in snapshot_request.get("candidate_output_scope", {}).get("claim_slots") or ():
            if not isinstance(row, Mapping):
                continue
            expected_slots.add((
                str(row.get("paper_key") or ""),
                str(row.get("primary_claim_id") or ""),
                str(row.get("study_id") or ""),
                tuple(sorted(str(value) for value in row.get("source_claim_ids") or () if str(value))),
                tuple(sorted(str(value) for value in row.get("evidence_ids") or () if str(value))),
                tuple(sorted(str(value) for value in row.get("relation_ids") or () if str(value))),
                tuple(sorted(expected_field_by_id.get(str(field_id), ("missing", str(field_id)))
                             for field_id in row.get("source_field_ids") or ()
                             )),
            ))
        for row in projected_scope.get("claim_slots") or ():
            if not isinstance(row, Mapping):
                continue
            actual_slot = (
                str(row.get("paper_key") or ""),
                str(row.get("primary_claim_id") or ""),
                str(row.get("study_id") or ""),
                tuple(sorted(str(value) for value in row.get("source_claim_ids") or () if str(value))),
                tuple(sorted(str(value) for value in row.get("evidence_ids") or () if str(value))),
                tuple(sorted(str(value) for value in row.get("relation_ids") or () if str(value))),
                tuple(sorted(actual_field_by_id.get(str(field_id), ("missing", str(field_id)))
                             for field_id in row.get("source_field_ids") or ())),
            )
            if actual_slot not in expected_slots:
                return {}

        canonical_evidence_texts = snapshot_tables.get("evidence_text_by_id")
        if not isinstance(canonical_evidence_texts, Mapping):
            return {}
        if not used_evidence_ids.issubset({str(key) for key in canonical_evidence_texts}):
            return {}
        return {
            evidence_id: {str(canonical_evidence_texts[evidence_id]).strip()}
            for evidence_id in used_evidence_ids
            if str(canonical_evidence_texts[evidence_id]).strip()
        }

    raw_evidence_texts = tables.get("evidence_text_by_id")
    direct_fields_present = False
    for slot in scope.get("claim_slots") or ():
        if not isinstance(slot, Mapping):
            continue
        qualifier_fields = {
            str(value) for row in dependencies
            if str(row.get("paper_key") or "") == str(slot.get("paper_key") or "")
            and str(row.get("primary_claim_id") or "") == str(slot.get("primary_claim_id") or "")
            for value in row.get("required_source_field_ids") or () if str(value)
        }
        if {
            str(value) for value in slot.get("source_field_ids") or () if str(value)
        } - qualifier_fields:
            direct_fields_present = True
            break
    paper_identity_request = (
        snapshot_projection if isinstance(snapshot_projection, Mapping) else request
    )
    requested_papers = {
        str(value) for value in paper_identity_request.get("paper_keys") or () if str(value)
    }
    evidence_texts_by_id: dict[str, set[str]] = {}
    uses_known_paper_ids = bool(requested_papers) and requested_papers.issubset(_CURRENT82_FIXTURE_PAPERS)
    if uses_known_paper_ids and (
        direct_fields_present or isinstance(raw_evidence_texts, Mapping)
    ):
        # For this synthetic four-PDF fixture, evidence text comes from the
        # independent saved projection. Request claim text is never used to
        # manufacture or authorize an evidence lookup.
        evidence_texts_by_id = snapshot_evidence_texts()
        used_evidence_ids = {
            str(value) for row in scope.get("claim_slots") or () if isinstance(row, Mapping)
            for value in row.get("evidence_ids") or () if str(value)
        }
        if not evidence_texts_by_id or not used_evidence_ids.issubset(evidence_texts_by_id):
            raise AssertionError("four-paper source projection differs from the saved evidence snapshot")
        if isinstance(raw_evidence_texts, Mapping) and any(
            str(raw_evidence_texts.get(evidence_id) or "").strip()
            != next(iter(evidence_texts_by_id[evidence_id]))
            for evidence_id in used_evidence_ids
        ):
            raise AssertionError("four-paper evidence text map differs from the saved evidence snapshot")
    elif isinstance(raw_evidence_texts, Mapping):
        for evidence_id, raw_text in raw_evidence_texts.items():
            if str(evidence_id) and str(raw_text or "").strip():
                evidence_texts_by_id.setdefault(str(evidence_id), set()).add(str(raw_text).strip())
    claim = (
        "In the observed controlled samples of 24 participants, Study B reported a two-point "
        "improvement whereas Study A reported a four-point improvement"
    )

    def support(
        slot: Mapping[str, Any],
        claim_text: str,
        *,
        allow_primary_only: bool = False,
        require_n24_boundary: bool = False,
    ) -> dict[str, Any]:
        paper_key = str(slot.get("paper_key") or "")
        primary_claim_id = str(slot.get("primary_claim_id") or "")
        source_claim_ids = {str(value) for value in slot.get("source_claim_ids") or () if str(value)}
        evidence_ids = {str(value) for value in slot.get("evidence_ids") or () if str(value)}
        source_field_ids = {str(value) for value in slot.get("source_field_ids") or () if str(value)}
        primary_claim = source_claims.get(primary_claim_id)
        if (
            not paper_key
            or not primary_claim_id
            or primary_claim_id not in source_claim_ids
            or primary_claim is None
            or str(primary_claim.get("paper_key") or "") != paper_key
            or not str(primary_claim.get("text") or "").strip()
        ):
            raise AssertionError(f"source slot has no same-paper primary claim text: {slot}")
        primary_evidence_ids = {
            str(value) for value in primary_claim.get("evidence_ids") or () if str(value)
        }
        if not primary_evidence_ids:
            raise AssertionError(f"source primary claim has no linked evidence: {slot}")

        declared_dependencies = [
            row for row in dependencies
            if str(row.get("primary_claim_id") or "") == primary_claim_id
        ]
        if any(str(row.get("paper_key") or "") != paper_key for row in declared_dependencies):
            raise AssertionError(f"source slot dependency belongs to a different paper: {slot}")
        if not declared_dependencies:
            if not allow_primary_only or slot.get("relation_ids"):
                raise AssertionError(f"source slot is missing its declared interpretation dependency: {slot}")
            if (
                source_claim_ids != {primary_claim_id}
                or evidence_ids != primary_evidence_ids
            ):
                raise AssertionError(f"primary-only slot contains unbound source material: {slot}")
            qualifier_claim_ids: set[str] = set()
            qualifier_evidence_ids: set[str] = set()
            qualifier_field_ids: set[str] = set()
        else:
            paper_dependencies = declared_dependencies
            dependency_primary_evidence_ids = {
                str(value) for row in paper_dependencies
                for value in row.get("primary_evidence_ids") or () if str(value)
            }
            if dependency_primary_evidence_ids and dependency_primary_evidence_ids != primary_evidence_ids:
                raise AssertionError(f"source dependency primary evidence differs from its claim: {slot}")
        qualifier_claim_ids = {
            str(value) for row in declared_dependencies
            for value in row.get("required_source_claim_ids") or () if str(value)
        }
        qualifier_evidence_ids = {
            str(value) for row in declared_dependencies
            for value in row.get("required_evidence_ids") or () if str(value)
        }
        qualifier_field_ids = {
            str(value) for row in declared_dependencies
            for value in row.get("required_source_field_ids") or () if str(value)
        }
        expected_source_claim_ids = {primary_claim_id, *qualifier_claim_ids}
        expected_evidence_ids = primary_evidence_ids | qualifier_evidence_ids
        if (
            source_claim_ids != expected_source_claim_ids
            or evidence_ids != expected_evidence_ids
            or not qualifier_field_ids.issubset(source_field_ids)
        ):
            raise AssertionError(f"source slot is not the exact primary-plus-qualifier closure: {slot}")
        if any(
            (row := source_claims.get(claim_id)) is None
            or str(row.get("paper_key") or "") != paper_key
            or not str(row.get("text") or "").strip()
            or not (claim_evidence_ids := {
                str(value) for value in row.get("evidence_ids") or () if str(value)
            })
            or not claim_evidence_ids.issubset(expected_evidence_ids)
            for claim_id in expected_source_claim_ids
        ):
            raise AssertionError(f"source slot contains missing or foreign source claim text: {slot}")
        slot_study_id = str(slot.get("study_id") or "")
        for field_id in source_field_ids:
            field = source_fields.get(field_id)
            if (
                field is None
                or str(field.get("paper_key") or "") != paper_key
                or not (
                    str(field.get("source_value") or "").strip()
                    or str(field.get("derived_value") or "").strip()
                )
                or not {
                    str(value) for value in field.get("evidence_ids") or () if str(value)
                }.issubset(expected_evidence_ids)
            ):
                raise AssertionError(f"source slot contains a missing or foreign source field: {slot}")
            field_owners = {
                str(value) for value in field.get("owner_study_ids") or () if str(value)
            }
            field_study_id = str(field.get("study_id") or field.get("source_study_id") or "")
            if field_study_id:
                field_owners.add(field_study_id)
            if field_owners and (not slot_study_id or not field_owners.issubset({slot_study_id})):
                raise AssertionError(f"source slot field crosses its verified study owner: {slot}")
            if str(field.get("scope") or "") == "explicit_study" and (
                not slot_study_id or slot_study_id not in field_owners
            ):
                raise AssertionError(f"source slot field has an incompatible explicit study: {slot}")
        direct_field_ids = source_field_ids - qualifier_field_ids
        for field_id in direct_field_ids:
            field = source_fields[field_id]
            source_values = {
                str(field.get("source_value") or "").strip().casefold(),
                str(field.get("derived_value") or "").strip().casefold(),
            } - {""}
            direct_evidence_ids = {
                evidence_id for evidence_id in expected_evidence_ids
                if source_values.intersection(
                    text.strip().casefold()
                    for text in evidence_texts_by_id.get(evidence_id, set())
                    if text.strip()
                )
            }
            if not direct_evidence_ids:
                raise AssertionError(
                    f"direct source field lacks an exact same-closure evidence text match: {field_id}"
                )
        if require_n24_boundary and not any(
            re.search(r"\b24\b", str(source_fields[value].get("source_value") or ""))
            for value in source_field_ids
        ):
            raise AssertionError("the four-source comparison requires its actual n=24 boundary")
        result: dict[str, Any] = {
            "claim": claim_text,
            "claim_slot_id": str(slot["claim_slot_id"]),
            "task_id": str(slot["task_id"]),
            "paper_key": paper_key,
            "primary_claim_id": primary_claim_id,
            "source_claim_ids": sorted(source_claim_ids),
            "evidence_ids": sorted(evidence_ids),
            "source_field_ids": sorted(source_field_ids),
            "primary_evidence_ids": sorted(primary_evidence_ids),
            "qualifier_source_claim_ids": sorted(qualifier_claim_ids),
            "qualifier_evidence_ids": sorted(qualifier_evidence_ids),
            "qualifier_source_field_ids": sorted(qualifier_field_ids),
        }
        study_id = str(slot.get("study_id") or "")
        if study_id:
            result["study_id"] = study_id
        return result

    # Equivalent relation IDs may describe the same two-sided source closure.
    # Emit one comparison section for each distinct closure/question pair and
    # bind every assigned task's complete slots to that statement.
    group_buckets: dict[tuple[Any, ...], dict[str, Any]] = {}
    assigned_ab_group_ids = {str(task.get("claim_group_id") or "") for task in ab_tasks}
    for task, _group_index, slot_rows in ab_groups:
        contract = task["task_contract"]
        closure_key = tuple(sorted(
            (
                str(slot.get("paper_key") or ""),
                str(slot.get("primary_claim_id") or ""),
                str(slot.get("study_id") or ""),
                tuple(sorted(str(value) for value in slot.get("source_claim_ids") or () if str(value))),
                tuple(sorted(str(value) for value in slot.get("evidence_ids") or () if str(value))),
                tuple(sorted(str(value) for value in slot.get("source_field_ids") or () if str(value))),
            )
            for slot in slot_rows
        ))
        bucket_key = closure_key
        bucket = group_buckets.setdefault(bucket_key, {
            "tasks": [], "slots": [], "relation_ids": set(), "questions": set(),
        })
        bucket["tasks"].append(task)
        bucket["slots"].extend(slot_rows)
        bucket["questions"].add(str(contract.get("comparison_question") or ""))
        bucket["relation_ids"].update(
            str(value) for slot in slot_rows for value in slot.get("relation_ids") or () if str(value)
        )

    relation_group_ids = {str(task.get("claim_group_id") or "") for task in task_rows}
    scope_slots = [row for row in scope.get("claim_slots") or () if isinstance(row, Mapping)]
    slots_by_group_index: dict[int, list[dict[str, Any]]] = {}
    for row in scope_slots:
        slots_by_group_index.setdefault(int(row.get("claim_group_index", -1)), []).append(dict(row))

    def closure_slot_identity(slot: Mapping[str, Any]) -> tuple[Any, ...]:
        return (
            str(slot.get("paper_key") or ""),
            str(slot.get("primary_claim_id") or ""),
            str(slot.get("study_id") or ""),
            tuple(sorted(str(value) for value in slot.get("source_claim_ids") or () if str(value))),
            tuple(sorted(str(value) for value in slot.get("evidence_ids") or () if str(value))),
            tuple(sorted(str(value) for value in slot.get("source_field_ids") or () if str(value))),
        )

    # The user-required A/B statement subsumes the matching single-paper
    # organization tasks for A and B. Keep them in the same one-claim section
    # only when their complete source/qualifier closures match the relation
    # witness; this preserves three Writer/Validator tasks (A/B, C, D).
    for group_index, slot_rows in slots_by_group_index.items():
        group_id = group_ids[group_index]
        if group_id in assigned_ab_group_ids or group_id in relation_group_ids:
            continue
        papers = {str(row.get("paper_key") or "") for row in slot_rows}
        if papers not in ({paper_a}, {paper_b}):
            continue
        task_ids = {str(row.get("task_id") or "") for row in slot_rows if str(row.get("task_id") or "")}
        if not task_ids or not all(task_id.startswith("topic_synthesis:") for task_id in task_ids):
            continue
        paper_key = next(iter(papers))
        signature = {closure_slot_identity(row) for row in slot_rows}
        bucket = next((
            item for item in group_buckets.values()
            if signature == {
                closure_slot_identity(row) for row in item["slots"]
                if str(row.get("paper_key") or "") == paper_key
            }
        ), None)
        if bucket is None:
            continue
        bucket["slots"].extend(slot_rows)
        bucket["tasks"].append({"task_id": next(iter(task_ids))})
        bucket["relation_ids"].update(
            str(value) for row in slot_rows for value in row.get("relation_ids") or () if str(value)
        )
        assigned_ab_group_ids.add(group_id)

    def source_text_for_slot(slot: Mapping[str, Any]) -> str:
        primary_id = str(slot.get("primary_claim_id") or "")
        primary = str(source_claims.get(primary_id, {}).get("text") or "").strip()
        if not primary:
            raise AssertionError(f"source slot has no primary claim text: {slot}")
        text = primary
        for claim_id in sorted(str(value) for value in slot.get("source_claim_ids") or () if str(value)):
            if claim_id == primary_id:
                continue
            extra = str(source_claims.get(claim_id, {}).get("text") or "").strip()
            if extra and extra.casefold() not in text.casefold():
                text += f"; {extra.rstrip('.')}"
        for field_id in sorted(str(value) for value in slot.get("source_field_ids") or () if str(value)):
            field = source_fields.get(field_id, {})
            value = str(field.get("source_value") or "").strip()
            if value and value.casefold() not in text.casefold():
                label = str(field.get("canonical_field") or field.get("source_path") or "source boundary")
                text += f"; {label.replace('_', ' ')}: {value.rstrip('.')}"
        return text.rstrip(" ;") + "."

    def source_role_for_slot(slot: Mapping[str, Any]) -> str:
        primary_id = str(slot.get("primary_claim_id") or "")
        primary = source_claims.get(primary_id)
        if not isinstance(primary, Mapping):
            raise AssertionError(f"paper role has no typed primary source claim: {slot}")
        source_locator = str(primary.get("source_locator") or "").strip()
        family = source_locator.rsplit(".", 1)[-1].casefold() if source_locator else ""
        if not family:
            claim_parts = primary_id.split(":")
            family = claim_parts[2].casefold() if len(claim_parts) > 3 and claim_parts[0] == "claim" else ""
        task_id = str(slot.get("task_id") or "")
        task_parts = task_id.split(":")
        task_dimension = task_parts[1] if len(task_parts) > 2 and task_parts[0] == "topic_synthesis" else ""
        claim_type = str(primary.get("claim_type") or "").casefold()
        if family == "findings" and claim_type == "empirical_finding":
            return "Report this source's empirical outcome finding within its recorded study context."
        if family == "operationalizations" and task_dimension == "construct":
            return "Describe how this source operationalizes its focal outcome construct."
        if family == "operationalizations" and task_dimension == "context":
            return "Describe this source's outcome operationalization within its study context."
        if family == "concept_definitions" and task_dimension == "construct":
            return "Define this source's focal construct from its conceptual framing."
        if family == "concept_definitions" and task_dimension == "context":
            return "Place this source's defined concept within its study context."
        raise AssertionError(
            "fixture cannot assign a source role from an unsupported primary family/task dimension: "
            f"family={family!r}, dimension={task_dimension!r}, claim_type={claim_type!r}"
        )

    candidate_id = str(request.get("candidate_id") or "candidate")
    title_by_paper = {
        str(row.get("paper_key") or ""): str(row.get("title") or row.get("paper_key") or "")
        for row in evidence
    }
    single_sections: list[dict[str, Any]] = []
    emitted_single_group_ids: set[str] = set()
    for paper_key in sorted({
        str(row.get("paper_key") or "") for row in scope_slots
        if str(row.get("paper_key") or "") not in {paper_a, paper_b}
    }):
        candidates: list[tuple[int, str, list[dict[str, Any]]]] = []
        for index, rows in slots_by_group_index.items():
            group_id = group_ids[index]
            if group_id in relation_group_ids or group_id in assigned_ab_group_ids:
                continue
            papers = {str(row.get("paper_key") or "") for row in rows}
            if papers != {paper_key}:
                continue
            task_ids = {str(row.get("task_id") or "") for row in rows if str(row.get("task_id") or "")}
            task_id = sorted(task_ids)[0] if task_ids else ""
            if task_id.startswith("topic_synthesis:"):
                candidates.append((index, task_id, rows))
        if not candidates:
            continue
        required_candidates = [
            item for item in candidates if item[0] in required_group_indices
        ]
        selected_candidates = sorted(
            required_candidates or candidates,
            key=lambda item: (item[1], item[0]),
        )
        if not required_candidates:
            selected_candidates = selected_candidates[:1]
        for group_index, task_id, slot_rows in selected_candidates:
            claim_texts = list(dict.fromkeys(source_text_for_slot(slot) for slot in slot_rows))
            claim_text = "; ".join(text.rstrip(".") for text in claim_texts) + "."
            single_sections.append({
                "section_id": f"{candidate_id}_paper_{group_index + 1}",
                "title": f"{title_by_paper.get(paper_key, paper_key)}: source finding",
                "goal": "State the individual finding with its recorded source qualifiers.",
                "rationale": "Use the assigned source claim and its complete qualifier closure.",
                "task_ids": [task_id],
                "paper_keys": [paper_key],
                "paper_roles": {
                    paper_key: "; ".join(sorted({source_role_for_slot(slot) for slot in slot_rows}))
                },
                "relation_ids": sorted({
                    str(value) for slot in slot_rows for value in slot.get("relation_ids") or () if str(value)
                }),
                "claims": [claim_text],
                "claim_support": [support(slot, claim_text, allow_primary_only=True) for slot in slot_rows],
            })
            emitted_single_group_ids.add(group_ids[group_index])
    base_content: dict[str, Any] = {
        "candidate_id": candidate_id,
        "organizing_logic": request.get("organizing_logic") or "evidence",
        "sections": [],
        "claims": [],
    }
    relation_sections: list[dict[str, Any]] = []
    for index, bucket in enumerate(group_buckets.values(), start=1):
        tasks = bucket["tasks"]
        question = "; ".join(sorted(value for value in bucket["questions"] if value))
        relation_papers = sorted({
            str(slot.get("paper_key") or "") for slot in bucket["slots"]
            if str(slot.get("paper_key") or "")
        })
        if len(relation_papers) != 2:
            raise AssertionError("comparison roles require two complete source paper sides")
        paper_roles: dict[str, str] = {}
        for paper_key in relation_papers:
            paper_slots = [
                slot for slot in bucket["slots"]
                if str(slot.get("paper_key") or "") == paper_key
            ]
            if not paper_slots:
                raise AssertionError("comparison paper role has no source support slot")
            counterpart = next(value for value in relation_papers if value != paper_key)
            paper_roles[paper_key] = (
                "Contribute this source's empirical outcome finding and recorded sample boundary "
                f"for comparison with {title_by_paper.get(counterpart, counterpart)}."
            )
        relation_sections.append({
            "section_id": f"{request.get('candidate_id')}_relation_{index}",
            "title": "Comparison of the observed treatment changes",
            "goal": question or "Compare both source findings and their limits.",
            "rationale": "Use both complete source-side closures and preserve their qualifiers.",
            "task_ids": sorted({str(task["task_id"]) for task in tasks}),
            "paper_keys": sorted((paper_a, paper_b)),
            "relation_ids": sorted(bucket["relation_ids"]),
            "paper_roles": paper_roles,
            "claims": [claim],
            "claim_support": [support(slot, claim, require_n24_boundary=True) for slot in sorted(
                bucket["slots"],
                key=lambda row: (str(row.get("paper_key") or ""), str(row.get("claim_slot_id") or "")),
            )],
        })
    sections = [*relation_sections, *single_sections]
    base_content["sections"] = sections
    base_content["claims"] = [
        str(value) for section in sections for value in section.get("claims") or () if str(value)
    ]
    emitted_slot_ids = {
        str(row.get("claim_slot_id") or "")
        for section in sections
        for row in section.get("claim_support") or ()
        if isinstance(row, Mapping) and str(row.get("claim_slot_id") or "")
    }
    emitted_group_ids = set(emitted_single_group_ids)
    emitted_group_ids.update(
        group_ids[index]
        for index, rows in slots_by_group_index.items()
        if any(str(row.get("claim_slot_id") or "") in emitted_slot_ids for row in rows)
    )
    missing_required_groups = required_group_ids - emitted_group_ids
    if missing_required_groups:
        raise AssertionError(
            "candidate fixture omitted required assigned claim groups: "
            + ", ".join(sorted(missing_required_groups))
        )
    return base_content


def _four_source_writer_content(
    writer_request: Mapping[str, Any],
    fixture_trace: list[dict[str, Any]],
    *,
    inject_defect: bool,
) -> tuple[dict[str, Any], bool]:
    from tests.test_current_runtime_full_e2e import _source_bound_writer_response

    # The four PDFs use "Study A" headings; their filename-derived catalog
    # titles are "study-a".  Resolve only that exact known source alias for
    # clause placement, leaving the actual catalog/ref identities unchanged.
    source_alias_request = dict(writer_request)
    source_alias_request["citation_ref_catalog"] = [
        {**row, "title": str(row.get("title") or "").replace("-", " ").title()}
        if str(row.get("canonical_paper_key") or "") in {
            "studya_unknown_author", "studyb_unknown_author",
        } and str(row.get("title") or "").casefold() in {"study-a", "study-b"}
        else row
        for row in writer_request["citation_ref_catalog"]
    ]
    source_alias_request["writer_task_scope"] = dict(writer_request["writer_task_scope"])
    source_alias_request["writer_task_scope"]["tasks"] = []
    catalog_by_key = {
        str(row.get("canonical_paper_key") or ""): row
        for row in source_alias_request["citation_ref_catalog"]
    }
    for task in writer_request["writer_task_scope"]["tasks"]:
        task = dict(task)
        keys = task.get("paper_keys") or []
        if len(keys) == 1:
            title = str(catalog_by_key[keys[0]]["title"])
            if title.casefold() not in str(task["planned_claim"]).casefold():
                task["planned_claim"] = f"{title} reports {task['planned_claim']}"
        source_alias_request["writer_task_scope"]["tasks"].append(task)
    response = _source_bound_writer_response(source_alias_request, fixture_trace)
    if response is None:
        raise AssertionError("four-source Writer omitted its production source scope")
    content = response["content"]
    tasks = writer_request["writer_task_scope"]["tasks"]
    catalog = writer_request["citation_ref_catalog"]
    active_refs = {
        str(row["canonical_paper_key"]): str(row["ref_id"])
        for row in catalog if row.get("status") == "active"
    }
    ab_keys = {"studya_unknown_author", "studyb_unknown_author"}
    ab_task_ids = {
        str(task["writer_task_id"])
        for task in tasks if ab_keys.issubset(set(task.get("paper_keys") or ()))
    }
    injected = False
    for block in content["blocks"]:
        if str(block["writer_task_id"]) not in ab_task_ids:
            continue
        text = str(block["text"])
        a_token = f"[[cite_ref:{active_refs['studya_unknown_author']}]]"
        b_token = f"[[cite_ref:{active_refs['studyb_unknown_author']}]]"
        if any(token not in text for token in (a_token, b_token)):
            raise AssertionError("required comparison must cite both source-specific clauses")
        if not all(value in text for value in ("Study A", "Study B", "four-point", "two-point", "24")):
            raise AssertionError("required comparison lost a source finding or sample boundary")
        if inject_defect and not injected:
            # Keep the complete comparison and change only B's citation.
            block["text"] = text.replace(b_token, a_token, 1)
            injected = True
    return content, injected


def _four_source_validator_observation(user_text: str) -> dict[str, Any]:
    decoder = json.JSONDecoder()
    packet = None
    for match in re.finditer(r"\{", user_text):
        try:
            candidate, _end = decoder.raw_decode(user_text[match.start():])
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict) and {"citation_set_key", "paper_ids", "claim_units"}.issubset(candidate):
            packet = candidate
            break
    if packet is None:
        raise AssertionError("Validator fixture requires the actual structured adjudication packet")
    paper_ids = {str(value) for value in packet["paper_ids"]}
    text = str(packet.get("claim_text") or "")
    claim_about_b = "Study B" in text and "two-point" in text
    cites_a = "studya_unknown_author" in paper_ids
    cites_b = "studyb_unknown_author" in paper_ids
    if cites_a and cites_b:
        source_packets = packet.get("per_paper_evidence_packets") or {}
        for key, finding in (("studya_unknown_author", "four points"), ("studyb_unknown_author", "two points")):
            if finding not in json.dumps(source_packets.get(key), ensure_ascii=False):
                raise AssertionError(f"comparison adjudication omitted {key}'s actual source evidence")
    return {
        "claim_about_study_b": claim_about_b,
        "cites_study_a": cites_a,
        "cites_study_b": cites_b,
        "paper_ids": sorted(paper_ids),
        "is_defective": claim_about_b and cites_a and not cites_b,
    }


class _LocalModelFixture(BaseHTTPRequestHandler):
    calls: list[dict[str, Any]]
    reader_calls: int
    review_writer_calls: int
    validator_calls: int
    validator_observations: list[dict[str, Any]]

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    def do_POST(self) -> None:  # noqa: N802
        try:
            request = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
            model = str(request.get("model") or "")
            messages = request.get("messages") or []
            user_text = "\n".join(
                str(message.get("content") or "")
                for message in messages
                if isinstance(message, Mapping) and message.get("role") == "user"
            )
            self.server.calls.append({  # type: ignore[attr-defined]
                "path": self.path,
                "model": model,
                "user_text": user_text,
            })

            if model == "reader-local":
                self.server.reader_calls += 1  # type: ignore[attr-defined]
                content: Any = _source_bound_four_paper_reader_response(user_text)
            elif model in {"outline-local", "relation-local", "writer-local"}:
                try:
                    envelope = json.loads(user_text)
                except json.JSONDecodeError:
                    envelope = None
                node_id = str(envelope.get("node_id") or "") if isinstance(envelope, Mapping) else ""
                request_payload = envelope.get("request") if isinstance(envelope, Mapping) else None
                is_outline_request = isinstance(request_payload, Mapping) and (
                    node_id.startswith((
                        "topic_synthesis_provider",
                        "cross_group_comparison_provider",
                        "global_synthesis_provider",
                        "candidate_comparison_fragments_provider:",
                        "candidate_organization_provider:",
                    ))
                    or node_id == "relation_adjudication"
                    or node_id.endswith(("_provider_generation", "_critique"))
                    or "_provider_generation:local:" in node_id
                    or any(role in node_id for role in ("structure_critique", "coverage_critique", "evidence_critique"))
                    or node_id in {"arbitration", "structure_critique", "coverage_critique", "evidence_critique"}
                )
                if is_outline_request:
                    content = _grounded_fixture_candidate(
                        node_id,
                        dict(request_payload),
                        self.server.fixture_trace,  # type: ignore[attr-defined]
                    )
                elif model == "writer-local":
                    writer_request = json.loads(user_text)
                    self.server.review_writer_calls += 1  # type: ignore[attr-defined]
                    content, injected = _four_source_writer_content(
                        writer_request,
                        self.server.fixture_trace,  # type: ignore[attr-defined]
                        inject_defect=not self.server.defect_sent,  # type: ignore[attr-defined]
                    )
                    self.server.defect_sent = self.server.defect_sent or injected  # type: ignore[attr-defined]
                else:
                    raise ValueError(f"outline fixture request is not a node envelope for {model}")
            elif model == "validator-local":
                self.server.validator_calls += 1  # type: ignore[attr-defined]
                observation = _four_source_validator_observation(user_text)
                is_defective = bool(observation["is_defective"])
                self.server.validator_observations.append(observation)  # type: ignore[attr-defined]
                content = {
                    "status": "wrong_source" if is_defective else "supported",
                    "confidence": 0.99,
                    "repair_scope": "citation_mapping" if is_defective else "none",
                    "disposition": "manual_review" if is_defective else "keep_as_is",
                    "low_confidence": False,
                    "reasoning": (
                        "The cited source is Study A, while the two-point finding belongs to Study B."
                        if is_defective
                        else "The corrected citation and bounded statement match the selected source."
                    ),
                    "repair_hint": (
                        "Use Study B's citation for the two-point finding."
                        if is_defective
                        else ""
                    ),
                    "summary_paper_ids": [],
                    "manual_review_reason": "" if not is_defective else "Claim exceeds source evidence.",
                    "claim_type": "result",
                    "claim_type_confidence": 1.0,
                    "claim_type_rationale": "The statement is an empirical result.",
                    "adjudication_status": "wrong_source" if is_defective else "supported",
                }
            else:
                raise ValueError(f"unexpected fixture model: {model}")

            response = {
                "id": f"chatcmpl-local-{len(self.server.calls)}",  # type: ignore[attr-defined]
                "object": "chat.completion",
                "created": 1,
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": json.dumps(content, ensure_ascii=False),
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 512,
                    "completion_tokens": 128,
                    "total_tokens": 640,
                },
            }
            body = json.dumps(response, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception:  # make fixture faults visible as provider failures
            body = json.dumps({"error": {"message": traceback.format_exc()}}).encode("utf-8")
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)


def _start_local_model_fixture() -> tuple[ThreadingHTTPServer, threading.Thread, str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _LocalModelFixture)
    server.calls = []  # type: ignore[attr-defined]
    server.fixture_trace = []  # type: ignore[attr-defined]
    server.reader_calls = 0  # type: ignore[attr-defined]
    server.review_writer_calls = 0  # type: ignore[attr-defined]
    server.defect_sent = False  # type: ignore[attr-defined]
    server.validator_calls = 0  # type: ignore[attr-defined]
    server.validator_observations = []  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, f"http://127.0.0.1:{server.server_address[1]}/v1"


def _runtime_config(tmp_path: Path, api_base: str) -> Path:
    template = Path(__file__).resolve().parents[1] / "config.ini.example"
    config_path = tmp_path / "local-provider.ini"
    parser = configparser.ConfigParser()
    parser.read(template, encoding="utf-8")
    parser["Paths"]["output_path"] = str(tmp_path / "output")
    parser["Paths"]["library_path"] = ""
    parser["Paths"]["zotero_report"] = ""
    parser["Preprocess"]["enabled"] = "true"
    parser["Preprocess"]["cache_dir"] = str(tmp_path / "preprocess-cache")
    parser["Preprocess"]["primary_parser"] = "local"
    parser["Preprocess"]["fallback_parser"] = "local"
    parser["Stage1_Input"]["send_extracted_text"] = "true"
    parser["Stage1_Input"]["send_selected_visuals"] = "false"
    parser["Stage1_Input"]["send_original_pdf"] = "never"
    parser["Stage1_Visual"]["enabled"] = "false"
    for section, model in (
        ("Primary_Reader_API", "reader-local"),
        ("Backup_Reader_API", "reader-local"),
        ("Outline_API", "outline-local"),
        ("Writer_API", "writer-local"),
        ("Free_Mode_API", "relation-local"),
        ("Validator_API", "validator-local"),
    ):
        parser[section]["api_key"] = "loopback-fixture-key"
        parser[section]["model"] = model
        parser[section]["api_base"] = api_base
        parser[section]["endpoint_type"] = "chat_completions"
        parser[section]["provider_family"] = "generic"
        parser[section]["thinking"] = ""
        parser[section]["reasoning_effort"] = ""
        parser[section]["reasoning_display"] = ""
        parser[section]["transport_retries"] = "0"
        parser[section]["max_context_tokens"] = "128000"
    parser["Outline"]["candidate_count"] = "2"
    parser["Writer_API"]["max_output_tokens"] = "4096"
    parser["Outline"]["require_explicit_adoption"] = "true"
    parser["OutlineStability"]["mode"] = "off"
    parser["Validation"]["review_enabled"] = "true"
    parser["Validation"]["repair_policy"] = "report_only"
    with config_path.open("w", encoding="utf-8") as handle:
        parser.write(handle)
    return config_path


def test_writer_request_binding_matches_transport_identity() -> None:
    service = object.__new__(ReviewGenerationService)
    service.settings = SimpleNamespace(
        section=lambda _name: {"max_output_tokens": "2048"}
    )
    prompt = '{"section":"bounded outcome"}'

    bound_payload = service._writer_request_payload(prompt)
    transport_payload = canonical_provider_request_payload(
        prompt=prompt,
        system_prompt=service._system_prompt(),
        user_content=None,
        response_format="json",
        max_output_tokens=service._max_output_tokens(),
        temperature=0.2,
    )

    assert hash_json(bound_payload) == hash_json(transport_payload)


def test_validator_output_artifacts_are_immutable_across_attempts(tmp_path: Path) -> None:
    workspace = JobWorkspace.create(
        str(tmp_path / "output"),
        "validation",
        job_id="validator-output-immutability",
    )
    registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    api_config = {
        "api_key": "loopback-only-test-key",
        "model": "validator-local",
        "api_base": "http://127.0.0.1:1/v1",
        "endpoint_type": "chat_completions",
    }
    settings = ApplicationSettings.from_config(
        {
            "Validator_API": dict(api_config),
            "Runtime": {"validation_retry_limit": "0"},
        }
    )

    def service_for_attempt(attempt_id: str) -> ValidationExecutionService:
        service = ValidationExecutionService(
            job_id=workspace.job_id,
            attempt_id=attempt_id,
            workspace=workspace,
            artifact_registry=registry,
            settings=settings,
            summaries=[],
            review_draft_record=None,
            citation_manifest_record=None,
            paper_artifact_records=[],
            visual_artifact_records=[],
            provider_factory=None,
            cancellation_checker=None,
            logger=None,
            runtime_config={"Validator_API": dict(api_config)},
        )
        service.new_provider_runtime(
            stage_name="stage4_validate",
            route="Validator_API",
            node_id="validation_node",
            call_id="validation:primary:stable-packet",
            api_config=api_config,
            schema_hash="validator-schema-v1",
        )
        service.bind_provider_call(
            call_id="validation:primary:stable-packet",
            prompt="adjudicate current claim",
            input_payload={"claim": "stable claim"},
            api_config=api_config,
            schema_hash="validator-schema-v1",
        )
        return service

    first_service = service_for_attempt("attempt-before-repair")
    call_id = "validation:primary:stable-packet"
    first_service.bind_provider_output(
        call_id=call_id,
        content={"status": "wrong_source", "confidence": 0.99},
    )
    first_expected = first_service._expected_provider_calls[call_id]
    first_record = next(
        record
        for record in registry.list_records()
        if record.artifact_id.startswith("validation-provider-output:")
    )
    first_record_hash = first_record.content_hash
    first_path = Path(first_record.path)

    second_service = service_for_attempt("attempt-after-repair")
    second_service.bind_provider_output(
        call_id=call_id,
        content={"status": "supported", "confidence": 0.99},
    )
    second_expected = second_service._expected_provider_calls[call_id]

    assert first_expected.closure_epoch_id != second_expected.closure_epoch_id
    assert first_expected.artifact_path == str(first_path)
    current_first_record = registry.get(first_record.artifact_id)
    assert current_first_record is not None
    assert first_record_hash == current_first_record.content_hash
    assert json.loads(first_path.read_text(encoding="utf-8"))["payload"] == {
        "status": "wrong_source",
        "confidence": 0.99,
    }
    assert second_expected.artifact_path != first_expected.artifact_path
    second_record = next(
        (
            record
            for record in registry.list_records()
            if Path(record.path).resolve() == Path(second_expected.artifact_path).resolve()
        ),
        None,
    )
    assert second_record is not None and second_record.status == "ready"
    assert second_record.artifact_id != first_record.artifact_id

    source_record = publish_json_artifact(
        first_service.publication_context,
        registry,
        workspace.artifact_path("test_immutable_source.json"),
        {"source": "fixture"},
        artifact_role="test_source",
        artifact_type="test_source",
        artifact_version="v1",
        producer="tests.test_pr25_repair_v2",
        artifact_id="test:immutable-source",
    )
    dependency = ArtifactDependencyRefV2.from_record(source_record)
    reuse_payload = {"reuse": "fixture"}
    reuse_record = publish_json_artifact(
        first_service.publication_context,
        registry,
        workspace.artifact_path("test_immutable_reuse.json"),
        reuse_payload,
        artifact_role="test_reuse",
        artifact_type="test_reuse",
        artifact_version="v1",
        producer="tests.test_pr25_repair_v2",
        artifact_id="test:immutable-reuse",
        depends_on=[dependency],
    )
    assert first_service._existing_immutable_json_record(
        artifact_id=reuse_record.artifact_id,
        payload=reuse_payload,
        dependencies=[dependency],
    ) == reuse_record
    Path(source_record.path).write_text('{"source":"changed"}', encoding="utf-8")
    with pytest.raises(RuntimeError, match="immutable validation reuse record is untrusted"):
        first_service._existing_immutable_json_record(
            artifact_id=reuse_record.artifact_id,
            payload=reuse_payload,
            dependencies=[dependency],
        )


def test_current_text_finding_can_be_explicitly_repaired_and_exported(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise a real local HTTP validation finding through repair and export.

    The model server is the only fixture boundary. Runner, adoption, current
    validation, repair planning/application/promotion, Registry, resume,
    CurrentArtifactSet, DOCX generation, and export remain production paths.
    """

    server, thread, api_base = _start_local_model_fixture()
    try:
        pdf_dir = tmp_path / "papers"
        pdf_dir.mkdir()
        source_rows = (
            ("study-a", "Study A", "Treatment improved the measured outcome by four points."),
            ("study-b", "Study B", "Treatment improved the measured outcome by two points."),
            ("study-c", "Study C", "Treatment improved the measured outcome by one point."),
            ("study-d", "Study D", "Treatment did not change the measured outcome."),
        )
        for key, title, finding in source_rows:
            _write_source_pdf(pdf_dir / f"{key}.pdf", title=title, finding=finding)
        spec = RuntimeJobSpec(
            project_name="pr25-repair-v2",
            source=RuntimeSourceSpec(mode="direct", pdf_folder=str(pdf_dir)),
            job_id="pr25-repair-v2-job",
            config=str(_runtime_config(tmp_path, api_base)),
            action="run_all",
            queue_file=str(tmp_path / "queue.json"),
            metadata={
                "provider_budget": ProviderAggregateBudgetV2(
                    max_provider_calls_total=200,
                    max_output_tokens_total=2_000_000,
                    max_retry_attempts_total=0,
                    max_wall_seconds=3600,
                ).to_dict(),
                "review_intent": {
                    "required_comparisons": [{
                        "paper_keys": ["studya_unknown_author", "studyb_unknown_author"],
                        "question": "Compare actual source findings within their recorded sample boundary.",
                        "relation_type": "reported_magnitude_contrast",
                    }],
                },
            },
        )

        first = AgentRuntimeRunner(spec).run()
        assert first.job_status == "completed", first.message
        assert first.job_disposition == "needs_review", first
        assert first.completed_stages == ("source_intake", "analyze", "outline"), first

        control = ReviewControlPlane(repo_root=Path(__file__).resolve().parents[1])
        inspection = control.inspect(workspace=first.workspace_path)
        final_outline = next(
            item
            for item in inspection["artifacts"]
            if item["artifact_id"] == "outline-v3:final_outline"
        )
        adoption = control.adopt(
            workspace=first.workspace_path,
            artifact_id="outline-v3:final_outline",
            actor="tests.pr25_repair_v2",
            reason="approve the local fixture outline before review generation",
            expected_hash=str(final_outline["content_hash"]),
        )
        assert adoption["status"] == "succeeded", adoption

        first_resume = control.resume(workspace=first.workspace_path)
        assert first_resume["job_status"] == "completed", first_resume
        assert first_resume["completion_status"] == "blocked", first_resume
        assert first_resume["completed_stages"] == (
            "source_intake",
            "analyze",
            "outline",
            "review",
            "validate",
        ), first_resume
        status = control.validation_status(workspace=first.workspace_path)
        assert status["closure"]["semantic"]["claim_verdict_counts"]["wrong_source"] == 1, status

        workspace, registry = AgentRuntimeRunner._open_workspace(first.workspace_path)
        initial_set = registry.resolve_current_artifact_set()
        assert initial_set is not None
        initial_draft = registry.get(initial_set.review_draft_artifact_id)
        initial_manifest = registry.get(initial_set.citation_manifest_artifact_id)
        assert initial_draft is not None and initial_manifest is not None
        draft_payload = json.loads(Path(initial_draft.path).read_text(encoding="utf-8"))
        block = next(
            block
            for section in draft_payload["content"]["sections"]
            for block in section["blocks"]
            if "Study B" in str(block.get("text") or "")
            and "Study A" in str(block.get("text") or "")
        )
        original_text = str(block["text"])
        block_id = str(block["block_id"])
        expected_anchor_hash = hashlib.sha256(original_text.encode("utf-8")).hexdigest()
        assert "Study B" in original_text and "Study A" in original_text
        assert "two-point" in original_text and "four-point" in original_text
        assert "24" in original_text
        assert server.reader_calls == 4  # type: ignore[attr-defined]
        assert server.validator_calls == 3  # type: ignore[attr-defined]
        initial_validation_calls = server.validator_calls  # type: ignore[attr-defined]
        assert any(
            item["claim_about_study_b"] and item["cites_study_a"]
            for item in server.validator_observations  # type: ignore[attr-defined]
        )

        manifest_payload = json.loads(Path(initial_manifest.path).read_text(encoding="utf-8"))
        occurrence = next(
            item
            for item in manifest_payload["occurrences"]
            if str(item.get("block_id") or "") == block_id
            and "Study B" in str(item.get("context_before") or "")
            and "two-point" in str(item.get("context_before") or "")
            and str(item.get("context_before") or "").rfind("Study B")
            > str(item.get("context_before") or "").rfind("Study A")
        )
        catalog_record = registry.get("citation_ref_catalog")
        assert catalog_record is not None
        catalog_payload = json.loads(Path(catalog_record.path).read_text(encoding="utf-8"))
        target_source = next(
            item
            for item in catalog_payload["entries"]
            if str(item.get("canonical_paper_key") or "") == "studyb_unknown_author"
            and item.get("status") == "active"
        )

        report = control.repair_plan(workspace=first.workspace_path)
        assert report["status"] == "available", report
        plan_id = str(report["plan_id"])
        approved_correction = {
            "block_id": block_id,
            "expected_anchor_hash": expected_anchor_hash,
            "replacement_text": (
                original_text[:int(occurrence["spans"][0]["start_offset"])]
                + f"[[cite_ref:{target_source['ref_id']}]]"
                + original_text[int(occurrence["spans"][0]["end_offset"]):]
            ),
            "source_evidence_ids": ["studya_unknown_author", "studyb_unknown_author"],
            "citation_mapping": {
                "occurrence_id": str(occurrence["occurrence_id"]),
                "expected_ref_id": str(occurrence["ref_id"]),
                "expected_paper_id": str(occurrence["paper_id"]),
                "replacement_ref_id": str(target_source["ref_id"]),
                "replacement_paper_id": str(target_source["canonical_paper_key"]),
            },
        }
        assert original_text[
            int(occurrence["spans"][0]["start_offset"]):int(occurrence["spans"][0]["end_offset"])
        ] == occurrence["citation_token"]
        assert "Study A" in approved_correction["replacement_text"]
        assert "Study B" in approved_correction["replacement_text"]
        assert "four-point" in approved_correction["replacement_text"]
        assert "two-point" in approved_correction["replacement_text"]
        assert "24" in approved_correction["replacement_text"]

        stale_anchor_result = control.repair_apply(
            workspace=first.workspace_path,
            plan_id=plan_id,
            manual_proposal={**approved_correction, "expected_anchor_hash": "0" * 64},
            actor="tests.pr25_repair_v2.reviewer",
            reason="reject a stale correction target before any mutation",
        )
        assert stale_anchor_result["status"] == "blocked", stale_anchor_result
        assert stale_anchor_result["mutation_performed"] is False
        registry.reload()
        unchanged_set = registry.resolve_current_artifact_set()
        assert unchanged_set is not None
        assert unchanged_set.review_draft_artifact_hash == initial_draft.content_hash
        assert unchanged_set.citation_manifest_artifact_hash == initial_manifest.content_hash

        unchanged_revision = registry.revision
        stale_source_result = control.repair_apply(
            workspace=first.workspace_path,
            plan_id=plan_id,
            manual_proposal={
                **approved_correction,
                "citation_mapping": {
                    **approved_correction["citation_mapping"],
                    "replacement_ref_id": occurrence["ref_id"],
                },
            },
            actor="tests.pr25_repair_v2.reviewer",
            reason="reject a citation key that does not resolve to the approved source paper",
        )
        assert stale_source_result["status"] == "blocked", stale_source_result
        assert stale_source_result["mutation_performed"] is False
        registry.reload()
        assert registry.revision == unchanged_revision
        unchanged_set = registry.resolve_current_artifact_set()
        assert unchanged_set is not None
        assert unchanged_set.review_draft_artifact_hash == initial_draft.content_hash
        assert unchanged_set.citation_manifest_artifact_hash == initial_manifest.content_hash

        applied = control.repair_apply(
            workspace=first.workspace_path,
            plan_id=plan_id,
            manual_proposal=approved_correction,
            actor="tests.pr25_repair_v2.reviewer",
            reason="replace the contradicted universal claim with the bounded result shown in the source PDF",
        )
        assert applied["status"] == "quarantined", applied
        assert applied["mutation_performed"] is True
        assert applied["canonical_replacement"] is False
        registry.reload()
        manual_plan_record = registry.get(str(applied["manual_plan_artifact_id"]))
        assert manual_plan_record is not None and manual_plan_record.status == "ready"
        manual_plan_payload = json.loads(Path(manual_plan_record.path).read_text(encoding="utf-8"))
        approval = manual_plan_payload["manual_approval"]
        assert approval["actor"] == "tests.pr25_repair_v2.reviewer"
        initial_validation_record = registry.get(initial_set.validation_run_result_artifact_id)
        assert initial_validation_record is not None
        assert approval["canonical_input_hashes"] == {
            "review_draft": initial_draft.content_hash,
            "citation_manifest": initial_manifest.content_hash,
            "validation": initial_validation_record.content_hash,
        }
        assert approval["citation_mapping"]["occurrence_id"] == occurrence["occurrence_id"]
        assert approval["citation_mapping"]["replacement_ref_id"] == target_source["ref_id"]
        transaction_id = str(applied["transaction_id"])
        applied_ids = set(applied["applied_artifact_ids"])
        derived_draft = next(
            registry.get(artifact_id)
            for artifact_id in applied_ids
            if registry.get(artifact_id) is not None
            and registry.get(artifact_id).artifact_type == "review_draft_repaired"
        )
        derived_manifest = next(
            registry.get(artifact_id)
            for artifact_id in applied_ids
            if registry.get(artifact_id) is not None
            and registry.get(artifact_id).artifact_type == "citation_manifest_repaired"
        )
        assert derived_draft.content_hash != initial_draft.content_hash
        assert derived_manifest.content_hash != initial_manifest.content_hash
        assert file_sha256(derived_draft.path) == derived_draft.content_hash
        assert file_sha256(derived_manifest.path) == derived_manifest.content_hash
        repaired = json.loads(Path(derived_draft.path).read_text(encoding="utf-8"))
        repaired_block = next(
            block
            for section in repaired["content"]["sections"]
            for block in section["blocks"]
            if str(block.get("block_id") or "") == block_id
        )
        assert repaired_block["text"] == approved_correction["replacement_text"]

        budget_state_path = Path(workspace.log_path("acceptance_budget_state_v1.json"))
        budget_before = json.loads(budget_state_path.read_text(encoding="utf-8"))
        assert budget_before["acceptance_run_id"] == f"run:{spec.job_id}"
        assert budget_before["calls_used"] == len(server.calls)  # type: ignore[attr-defined]
        promoted = control.repair_promote(
            workspace=first.workspace_path,
            transaction_id=transaction_id,
            actor="tests.pr25_repair_v2.reviewer",
            reason="the production validator now confirms the complete source-grounded A/B comparison",
        )
        assert promoted["status"] == "promoted", promoted
        assert promoted["revalidation_execution_status"] == "succeeded", promoted
        assert promoted["revalidation_disposition"] == "clean", promoted
        assert server.validator_calls == 2 * initial_validation_calls  # type: ignore[attr-defined]
        calls_after_promotion = server.validator_calls  # type: ignore[attr-defined]
        all_calls_after_promotion = len(server.calls)  # type: ignore[attr-defined]
        budget_after = json.loads(budget_state_path.read_text(encoding="utf-8"))
        assert budget_after["acceptance_run_id"] == budget_before["acceptance_run_id"]
        assert budget_after["budget"] == budget_before["budget"]
        assert budget_after["absolute_deadline_epoch"] == budget_before["absolute_deadline_epoch"]
        assert budget_after["calls_used"] == len(server.calls)  # type: ignore[attr-defined]
        assert budget_after["calls_used"] - budget_before["calls_used"] == initial_validation_calls
        assert budget_after["retry_attempts_used"] == budget_before["retry_attempts_used"] == 0
        assert any(
            item["claim_about_study_b"] and item["cites_study_b"]
            for item in server.validator_observations  # type: ignore[attr-defined]
        )

        registry.reload()
        promoted_set = registry.resolve_current_artifact_set()
        assert promoted_set is not None
        assert promoted_set.review_draft_artifact_id.startswith("review_draft:v3:repair:")
        assert promoted_set.citation_manifest_artifact_id.startswith("citation_manifest:v3:repair:")
        assert promoted_set.review_docx_artifact_id.startswith("review_docx:v1:repair:")
        promoted_draft = registry.get(promoted_set.review_draft_artifact_id)
        promoted_manifest = registry.get(promoted_set.citation_manifest_artifact_id)
        promoted_docx = registry.get(promoted_set.review_docx_artifact_id)
        assert promoted_draft is not None and promoted_manifest is not None and promoted_docx is not None
        promoted_payload = json.loads(Path(promoted_draft.path).read_text(encoding="utf-8"))
        assert approved_correction["replacement_text"] in json.dumps(promoted_payload, ensure_ascii=False)
        assert "[[cite_ref:R001]]" in approved_correction["replacement_text"]
        from docx import Document

        promoted_docx_text = "\n".join(
            paragraph.text for paragraph in Document(promoted_docx.path).paragraphs
        )
        assert all(value in promoted_docx_text for value in ("Study A", "Study B", "four-point", "two-point", "24"))
        export_before_resume = control.export(workspace=first.workspace_path)
        assert export_before_resume["status"] == "untrusted", export_before_resume
        assert export_before_resume["bundle_path"] == ""
        assert "runtime_completion_not_canonical" in export_before_resume["issues"]

        resumed = control.resume(workspace=first.workspace_path)
        assert resumed["job_status"] == "completed", resumed
        assert resumed["completion_status"] == "complete", resumed
        assert resumed["canonical_ready"] is True, resumed
        assert server.validator_calls == calls_after_promotion  # type: ignore[attr-defined]
        assert len(server.calls) == all_calls_after_promotion  # type: ignore[attr-defined]
        _resumed_workspace, current_registry = AgentRuntimeRunner._open_workspace(first.workspace_path)
        current_set = current_registry.resolve_current_artifact_set()
        assert current_set is not None
        assert current_set.validation_status == "clean"
        assert current_set.review_draft_artifact_id == promoted_set.review_draft_artifact_id
        assert current_set.review_draft_artifact_hash == promoted_set.review_draft_artifact_hash
        assert current_set.citation_manifest_artifact_id == promoted_set.citation_manifest_artifact_id
        assert current_set.citation_manifest_artifact_hash == promoted_set.citation_manifest_artifact_hash
        assert current_set.review_docx_artifact_id == promoted_set.review_docx_artifact_id
        assert current_set.review_docx_artifact_hash == promoted_set.review_docx_artifact_hash
        current_draft = current_registry.get(current_set.review_draft_artifact_id)
        current_manifest = current_registry.get(current_set.citation_manifest_artifact_id)
        current_docx = current_registry.get(current_set.review_docx_artifact_id)
        assert current_draft is not None and current_manifest is not None and current_docx is not None
        current_payload = json.loads(Path(current_draft.path).read_text(encoding="utf-8"))
        assert approved_correction["replacement_text"] in json.dumps(current_payload, ensure_ascii=False)
        assert "Study A" in json.dumps(current_payload, ensure_ascii=False)
        assert "Study B" in json.dumps(current_payload, ensure_ascii=False)
        assert f"[[cite_ref:{target_source['ref_id']}]]" in json.dumps(current_payload, ensure_ascii=False)
        current_manifest_payload = json.loads(Path(current_manifest.path).read_text(encoding="utf-8"))
        repaired_occurrence = next(
            item
            for item in current_manifest_payload["occurrences"]
            if str(item.get("occurrence_id") or "") == str(occurrence["occurrence_id"])
        )
        assert repaired_occurrence["ref_id"] == target_source["ref_id"]
        assert repaired_occurrence["paper_id"] == target_source["canonical_paper_key"]
        current_docx_document = Document(current_docx.path)
        docx_paragraphs = [paragraph.text for paragraph in current_docx_document.paragraphs]
        docx_paragraphs.extend(
            cell.text
            for table in current_docx_document.tables
            for row in table.rows
            for cell in row.cells
        )
        docx_text = "\n".join(docx_paragraphs)
        assert all(value in docx_text for value in ("Study A", "Study B", "four-point", "two-point", "24"))
        corrected_claim_paragraph = next(
            paragraph for paragraph in docx_paragraphs if "Study A" in paragraph and "Study B" in paragraph
        )
        assert "(Author, 2025b)" in corrected_claim_paragraph
        assert "Author, E. (2025b). study-b." in docx_text
        assert "https://doi.org/10.1000/study-b" in docx_text
        assert "[[cite_ref:" not in docx_text

        export = control.export(workspace=first.workspace_path)
        assert export["status"] == "canonical_verified", export
        assert Path(export["bundle_path"]).is_file()
        assert export["artifact_id"].startswith("export_bundle:")
        import zipfile

        runtime_spec_record = current_registry.get("runtime_job_spec")
        assert runtime_spec_record is not None
        assert runtime_spec_record.artifact_type == "runtime_job_spec"
        assert runtime_spec_record.artifact_role == "runtime_spec"
        assert file_sha256(runtime_spec_record.path) == runtime_spec_record.content_hash
        runtime_spec_payload = json.loads(Path(runtime_spec_record.path).read_text(encoding="utf-8"))
        stage_plan = runtime_spec_payload["metadata"]["stage_plan"]
        assert stage_plan["validation_enabled"] is True
        assert stage_plan["validation_required"] is True
        stage_plan_hash = hash_json(stage_plan)
        with zipfile.ZipFile(export["bundle_path"]) as archive:
            export_manifest = json.loads(archive.read("provenance_manifest.json"))
            export_status = dict(
                line.split("=", 1)
                for line in archive.read("EXPORT_STATUS.txt").decode("utf-8").splitlines()
                if "=" in line
            )
        assert export_manifest["validation_enabled"] is True
        assert export_manifest["validation_required"] is True
        assert export_manifest["stage_plan_hash"] == stage_plan_hash
        assert export_manifest["runtime_spec_hash"] == runtime_spec_record.content_hash
        assert export_status["validation_enabled"] == "true"
        assert export_status["validation_required"] == "true"
        assert export_status["stage_plan_hash"] == stage_plan_hash
        assert export_status["runtime_spec_hash"] == runtime_spec_record.content_hash
        export_hash = file_sha256(export["bundle_path"])
        repeated_export = control.export(workspace=first.workspace_path)
        assert repeated_export["status"] == "canonical_verified", repeated_export
        assert repeated_export["artifact_id"] == export["artifact_id"]
        assert file_sha256(repeated_export["bundle_path"]) == export_hash
        assert len(server.calls) == all_calls_after_promotion  # type: ignore[attr-defined]
        budget_after_exports = json.loads(budget_state_path.read_text(encoding="utf-8"))
        for key in (
            "acceptance_run_id", "budget", "first_started_epoch", "absolute_deadline_epoch",
            "calls_used", "output_tokens_used", "retry_attempts_used",
            "calls_reserved", "output_tokens_reserved", "retry_attempts_reserved",
        ):
            assert budget_after_exports[key] == budget_after[key], key

        original_draft_bytes = Path(current_draft.path).read_bytes()
        try:
            Path(current_draft.path).write_bytes(original_draft_bytes + b"\n")
            tampered_export = control.export(workspace=first.workspace_path)
            assert tampered_export["status"] == "untrusted", tampered_export
            assert tampered_export["bundle_path"] == ""
        finally:
            Path(current_draft.path).write_bytes(original_draft_bytes)
        assert file_sha256(current_draft.path) == current_draft.content_hash
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()
