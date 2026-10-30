"""Paper-scoped source fields must survive into topic interpretation context."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping
import hashlib

from outline.semantic_chunking import (
    build_paper_content_layers,
    build_semantic_chunk_plan,
    build_topic_synthesis_plan,
)
from outline.v3_evidence import build_outline_evidence_views
from outline.v3_executor import OutlineV3Executor
from tests.test_current_runtime_full_e2e import _outline_provider_response
from tests.test_outline_v3_semantic_execution import (
    _configured_test_provider,
    _executor,
    _summary,
)

PAPER = "paper-level-source-fields"
PRIMARY = "The intervention improves preference."
QUALIFIER = "Only consumers with high prior knowledge showed the effect."
EXECUTOR_QUALIFIER = "The result is bounded by the tested context."


def _candidate_slot_fixture_response(request: Mapping[str, Any]) -> Mapping[str, Any]:
    """Author compact claims only for the exact source groups in this request."""
    scope = request.get("candidate_output_scope")
    semantic = request.get("shared_semantic_context")
    tables = semantic.get("interpretation_source_tables") if isinstance(semantic, Mapping) else None
    if not isinstance(scope, Mapping) or not isinstance(tables, Mapping):
        raise AssertionError("candidate fixture needs its exact scoped source context")

    source_claims = {
        str(row.get("claim_id") or ""): row
        for row in tables.get("source_claims") or ()
        if isinstance(row, Mapping) and str(row.get("claim_id") or "")
    }
    source_fields = {
        str(row.get("source_field_id") or ""): row
        for row in tables.get("source_fields") or ()
        if isinstance(row, Mapping) and str(row.get("source_field_id") or "")
    }
    topic_routes = {
        str(row.get("logical_node_id") or ""): row
        for row in semantic.get("topic_routes") or ()
        if isinstance(row, Mapping) and str(row.get("logical_node_id") or "")
    }
    claim_group_ids = [str(value) for value in scope.get("claim_group_ids") or ()]
    slots = [row for row in scope.get("claim_slots") or () if isinstance(row, Mapping)]
    required_indices = {
        value for value in scope.get("required_claim_group_indices") or ()
        if type(value) is int
    }
    if not claim_group_ids or not slots or not required_indices.issubset(range(len(claim_group_ids))):
        raise AssertionError("candidate fixture received an empty or malformed assigned scope")

    sections = []
    for group_index, group_id in enumerate(claim_group_ids):
        group_slots = [row for row in slots if row.get("claim_group_index") == group_index]
        if not group_slots:
            raise AssertionError(f"candidate scope group has no source slots: {group_id}")
        statements: list[str] = []
        paper_roles: dict[str, list[str]] = {}
        for slot in group_slots:
            paper_key = str(slot.get("paper_key") or "")
            source_ids = [str(value) for value in slot.get("source_claim_ids") or () if str(value)]
            primary_id = str(slot.get("primary_claim_id") or "")
            ordered_claim_ids = [primary_id, *(value for value in source_ids if value != primary_id)]
            for claim_id in ordered_claim_ids:
                row = source_claims.get(claim_id)
                if not isinstance(row, Mapping) or str(row.get("paper_key") or "") != paper_key:
                    raise AssertionError("candidate fixture source claim is missing or foreign")
                text = str(row.get("text") or "").strip()
                if text and text not in statements:
                    statements.append(text)
                task_id = str(slot.get("task_id") or "")
                topic = topic_routes.get(task_id)
                if isinstance(topic, Mapping):
                    question = str(topic.get("question") or "").strip()
                    role = f"Contribute this source's finding to the topic question: {question}; source claim: {text}"
                else:
                    role = (
                        f"Contribute this source's recorded finding to "
                        f"{task_id.replace('_', ' ')}: {text}"
                    )
                paper_roles.setdefault(paper_key, []).append(role)
            for field_id in slot.get("source_field_ids") or ():
                field = source_fields.get(str(field_id))
                if not isinstance(field, Mapping) or str(field.get("paper_key") or "") != paper_key:
                    raise AssertionError("candidate fixture qualifier field is missing or foreign")
                value = str(field.get("source_value") or "").strip()
                if value and value not in statements:
                    statements.append(f"Recorded source qualification: {value}")
        if not statements:
            raise AssertionError(f"candidate fixture group has no source-authored text: {group_id}")
        section_suffix = hashlib.sha256(group_id.encode("utf-8")).hexdigest()[:12]
        sections.append({
            "section_id": f"{request.get('candidate_id')}_group_{section_suffix}",
            "title": "Source-backed synthesis",
            "goal": "Present the source finding with its recorded qualifiers.",
            "rationale": "Use the exact source-supported claim group assigned to this request.",
            "paper_roles": {
                paper: "; ".join(dict.fromkeys(roles))
                for paper, roles in paper_roles.items()
            },
            "relation_ids": sorted({
                str(value) for row in group_slots for value in row.get("relation_ids") or () if str(value)
            }),
            "claims": [{
                "text": "; ".join(statements),
                "claim_group_indices": [group_index],
            }],
        })

    return {
        "status": "success",
        "content": {
            "candidate_id": str(request.get("candidate_id") or ""),
            "organizing_logic": str(request.get("organizing_logic") or "evidence"),
            "candidate_wire_version": "candidate-slot-output/v1",
            "sections": sections,
        },
    }


def test_paper_level_source_fields_reach_topic_interpretation_context(
    tmp_path: Path,
) -> None:
    summary = _summary(PAPER, "Paper-level source fields", PRIMARY)
    summary["core_analysis"]["findings"] = PRIMARY
    summary["core_analysis"]["limitations"] = QUALIFIER

    views = build_outline_evidence_views([summary], "paper-level-context")
    layers = build_paper_content_layers(
        [summary], views, job_id="paper-level-context"
    )
    dossier = layers.dossier_by_paper[PAPER]
    unit = dossier.research_units[0]
    primary_claim = next(
        claim
        for claim in unit.claims
        if claim.claim_type == "empirical_finding" and claim.text == PRIMARY
    )
    dependency = next(
        dependency
        for dependency in unit.interpretation_dependencies
        if dependency.primary_claim_id == primary_claim.claim_id
    )
    ledger_by_id = {
        entry.source_field_id: entry for entry in dossier.source_field_ledger
    }
    primary_field_ids = {
        entry.source_field_id
        for entry in dossier.source_field_ledger
        if entry.canonical_field == "findings" and entry.source_value == PRIMARY
    }

    plan = build_semantic_chunk_plan(
        layers,
        candidate_count=1,
        physical_call_limit=24,
        retry_fallback_reserve=0,
    )
    route = next(item for item in plan.topics if "method" in item.dimensions)
    topic = next(
        item
        for item in build_topic_synthesis_plan(plan)
        if item.topic_id == route.topic_id
    )
    executor: OutlineV3Executor = _executor(tmp_path, stability_mode="off")
    request = executor._build_topic_provider_request(
        [topic],
        topic_routes={route.topic_id: route},
        evidence_model=views,
        content_layers_model=layers,
        batch_index=1,
    )
    topic_row = request["topics"][0]
    context = executor._interpretation_context_for_units(
        request["evidence_units"], topic_row["planned_evidence_unit_ids"]
    )
    context_dependencies = context["dependencies"]
    context_dependency = next(
        item
        for item in context_dependencies
        if item["primary_claim_id"] == primary_claim.claim_id
    )
    context_fields = {
        item["source_field_id"]: item for item in context["fields"]
    }
    primary_request_claim = next(
        claim
        for evidence_unit in request["evidence_units"]
        for study in evidence_unit.get("study_units") or ()
        if evidence_unit["paper_key"] == PAPER
        for claim in study.get("claims") or ()
        if claim["claim_id"] == primary_claim.claim_id
    )
    qualifier_field_ids = {
        field_id
        for field_id in dependency.required_source_field_ids
        if ledger_by_id[field_id].source_value == QUALIFIER
    }

    assert unit.study_id.endswith(":paper_level")
    assert unit.source_study_id == ""
    assert primary_field_ids
    assert dependency.required_source_field_ids
    assert unit.source_field_ids == []
    assert set(dependency.required_source_field_ids).issubset(context_fields)
    assert qualifier_field_ids
    assert all(
        context_fields[field_id]["paper_key"] == PAPER
        and context_fields[field_id]["owner_study_id"] == ""
        for field_id in dependency.required_source_field_ids
    )
    assert all(
        context_fields[field_id]["source_value"] == QUALIFIER
        for field_id in qualifier_field_ids
    )
    assert context_dependency["owner_study_id"] == ""
    assert context_dependency["scope"] == "paper"
    assert context_dependency["required_source_field_ids"] == (
        dependency.required_source_field_ids
    )
    assert primary_request_claim["text"] == PRIMARY
    assert primary_request_claim["evidence_ids"] == primary_claim.evidence_ids
    assert context_dependency["primary_evidence_ids"] == primary_claim.evidence_ids


def test_executor_candidate_receives_paper_qualifiers_after_topic_synthesis(
    tmp_path: Path,
) -> None:
    candidate_requests: list[Mapping[str, Any]] = []

    def provider(node_id: str, request: Mapping[str, Any]) -> Mapping[str, Any]:
        if node_id.startswith("topic_synthesis_provider"):
            return _outline_provider_response(node_id, request)
        if (
            node_id.endswith("_provider_generation")
            or "_provider_generation:local:" in node_id
        ):
            candidate_requests.append(request)
            return _candidate_slot_fixture_response(request)
        return _configured_test_provider(node_id, request)

    executor = _executor(tmp_path, provider=provider, stability_mode="off")
    executor.semantic_provider_synthesis_enabled = True
    result = executor.run()
    assert result.ok, result
    assert candidate_requests
    canonical_scope = executor._candidate_output_scope
    canonical_tables = executor._candidate_interpretation_tables
    assert canonical_scope is not None
    canonical_fields = {
        str(row["source_field_id"]): dict(row)
        for row in canonical_tables["source_fields"]
    }
    canonical_dependencies = [dict(row) for row in canonical_tables["dependencies"]]
    canonical_claim_rows = list(canonical_tables["source_claims"])
    canonical_claim_ids = [str(row.get("claim_id") or "") for row in canonical_claim_rows]
    assert all(canonical_claim_ids)
    assert len(canonical_claim_ids) == len(set(canonical_claim_ids))
    canonical_claims = {
        str(row["claim_id"]): dict(row)
        for row in canonical_claim_rows
    }
    required_qualifier_field_ids = {
        field_id
        for slot in canonical_scope.claim_slots
        for field_id in slot.source_field_ids
        if canonical_fields[field_id]["source_value"] == EXECUTOR_QUALIFIER
    }
    assert required_qualifier_field_ids
    assert {
        canonical_fields[field_id]["paper_key"]
        for field_id in required_qualifier_field_ids
    } == {"paper-a", "paper-b"}
    projected_fields: set[str] = set()
    projected_qualifier_fields: set[str] = set()
    for request in candidate_requests:
        assigned_group_ids = request["hierarchy"]["claim_group_ids"]
        local_scope = canonical_scope.for_groups(assigned_group_ids)
        assigned_papers = set(local_scope.paper_keys)
        assigned_primaries = {slot.primary_claim_id for slot in local_scope.claim_slots}
        expected_field_ids = {
            field_id for slot in local_scope.claim_slots for field_id in slot.source_field_ids
        }
        expected_dependencies = [
            row for row in canonical_dependencies
            if row.get("paper_key") in assigned_papers
            and row.get("primary_claim_id") in assigned_primaries
        ]
        expected_source_claim_ids = {
            source_claim_id
            for slot in local_scope.claim_slots for source_claim_id in slot.source_claim_ids
        }
        context = request["shared_semantic_context"]
        tables = context["interpretation_source_tables"]
        fields = {row["source_field_id"]: row for row in tables["source_fields"]}
        assert set(fields) == expected_field_ids
        assert all(dict(fields[field_id]) == canonical_fields[field_id] for field_id in expected_field_ids)
        dependencies = tables["dependencies"]
        dependency_fields = (
            "dependency_id",
            "paper_key",
            "primary_claim_id",
            "owner_study_id",
            "scope",
            "required_source_claim_ids",
            "required_evidence_ids",
            "required_source_field_ids",
            "primary_evidence_ids",
        )
        def dependency_closures(rows: list[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
            result: dict[str, dict[str, Any]] = {}
            sequence_fields = {
                "required_source_claim_ids",
                "required_evidence_ids",
                "required_source_field_ids",
            }
            for row in rows:
                dependency_id = str(row.get("dependency_id") or "")
                projected = {}
                for key in dependency_fields:
                    if key not in row:
                        continue
                    value = row[key]
                    projected[key] = (
                        tuple(str(item) for item in value or ())
                        if key in sequence_fields else value
                    )
                result[dependency_id] = projected
            return result

        actual_dependencies = dependency_closures(dependencies)
        expected_dependency_map = dependency_closures(expected_dependencies)
        dependency_differences = {
            dependency_id: {
                "actual": actual_dependencies.get(dependency_id),
                "expected": expected_dependency_map.get(dependency_id),
            }
            for dependency_id in set(actual_dependencies) | set(expected_dependency_map)
            if actual_dependencies.get(dependency_id) != expected_dependency_map.get(dependency_id)
        }
        assert not dependency_differences, dependency_differences
        assert all(row["owner_study_id"] == "" for row in dependencies)
        for dependency in dependencies:
            assert set(dependency["required_source_field_ids"]).issubset(fields)
            assert all(
                fields[field_id]["paper_key"] == dependency["paper_key"]
                for field_id in dependency["required_source_field_ids"]
            )
        source_claim_rows = list(tables["source_claims"])
        source_claim_ids = [str(row.get("claim_id") or "") for row in source_claim_rows]
        assert all(source_claim_ids)
        assert len(source_claim_ids) == len(set(source_claim_ids))
        source_claims = {str(row["claim_id"]): dict(row) for row in source_claim_rows}
        assert set(source_claims) == expected_source_claim_ids
        for claim_id in expected_source_claim_ids:
            actual_claim = source_claims[claim_id]
            expected_claim = canonical_claims[claim_id]
            for key in (
                "paper_key",
                "claim_type",
                "source_locator",
                "text",
                "source_summary_hash",
            ):
                assert actual_claim.get(key, "") == expected_claim.get(key, "")
            owner_study_ids = tuple(actual_claim.get("owner_study_ids") or ())
            assert owner_study_ids == tuple(
                expected_claim.get("owner_study_ids") or ()
            )
            assert not any(str(value).strip() for value in owner_study_ids)
            for key in ("evidence_ids", "source_field_ids"):
                assert tuple(actual_claim.get(key) or ()) == tuple(expected_claim.get(key) or ())
        projected_fields.update(fields)
        projected_qualifier_fields.update(
            field_id for field_id, row in fields.items()
            if row.get("source_value") == EXECUTOR_QUALIFIER
        )
        dependency_ids = {row["dependency_id"] for row in tables["dependencies"]}
        for topic in context["topic_routes"]:
            for fragment in topic["fragments"]:
                for row in fragment["provider_results"]:
                    bindings = row.get("interpretation_context", {})
                    assert set(bindings.get("source_field_ids", ())).issubset(fields)
                    assert set(bindings.get("dependency_ids", ())).issubset(dependency_ids)
    assert projected_fields == {field_id for slot in canonical_scope.claim_slots for field_id in slot.source_field_ids}
    assert projected_qualifier_fields == required_qualifier_field_ids
