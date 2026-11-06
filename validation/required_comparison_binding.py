"""Resolve durable required comparisons to their adopted Writer task scope.

The resolver starts from the Outline and adoption identities recorded on the
review draft.  It follows those exact Registry dependencies to the section
packet and confirmed relation map.  It never selects a latest candidate scope
or adoption pointer.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable, Mapping, TypeAlias

from outline.v3_artifacts import (
    AdoptedOutline,
    ConfirmedGlobalRelationMap,
    FinalOutline,
    SectionEvidencePacketSet,
)
from outline.v3_models import ReviewIntent
from runtime.job_spec import load_runtime_job_spec
from services.artifact_registry import (
    ArtifactClosureBatchError,
    ArtifactRecord,
    ArtifactRegistry,
    RegistryError,
)
from services.review_draft import iter_review_text_blocks


RequiredComparisonKey: TypeAlias = tuple[tuple[str, ...], str, str]
RequiredComparisonBindings: TypeAlias = dict[RequiredComparisonKey, dict[str, Any]]

_SPEC_ROLES = frozenset({"runtime_spec", "runtime_job_spec"})
_OUTLINE_ROLE = "outline_v3_node_output"
_ADOPTION_ROLE = "outline_v3_adoption"


def required_comparison_key(value: Mapping[str, Any]) -> RequiredComparisonKey:
    """Return the normalized durable key for one ReviewIntent comparison."""

    intent = ReviewIntent.from_dict({"required_comparisons": [dict(value)]})
    comparison = intent.required_comparisons[0]
    return (
        tuple(str(item) for item in comparison["paper_keys"]),
        str(comparison["question"]),
        str(comparison.get("relation_type") or ""),
    )


def _issue(code: str, *, pair: tuple[str, ...] = ()) -> str:
    suffix = "|".join(pair)
    return f"{code}:{suffix}" if suffix else code


def _normalized_text(value: Any) -> str:
    return " ".join(str(value or "").split()).casefold()


def _durable_intent(
    registry: ArtifactRegistry,
    *, external_registry_resolver: Callable[[str], ArtifactRegistry | None] | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    record = registry.get("runtime_job_spec")
    if record is None:
        current_run = (
            registry.get("provider_run_envelope") is not None
            or registry.get("current-artifact-set:pointer") is not None
        )
        if current_run:
            return [], ["required_comparison_intent_untrusted:current runtime spec is missing"]
        return [], []  # Legacy workspaces without a durable explicit intent.
    if (
        record.job_id != registry.job_id
        or record.artifact_id != "runtime_job_spec"
        or record.artifact_type != "runtime_job_spec"
        or record.artifact_version != "v1"
        or record.artifact_role not in _SPEC_ROLES
        or record.status != "ready"
    ):
        return [], ["required_comparison_intent_untrusted:runtime spec Registry identity mismatch"]
    try:
        verified = registry.verify_ready_artifact_closure(
            record, external_registry_resolver=external_registry_resolver,
        )
        if (
            verified.job_id != registry.job_id
            or verified.artifact_id != record.artifact_id
            or verified.content_hash != record.content_hash
            or verified.path != record.path
        ):
            raise ValueError("runtime spec Registry closure identity changed")
        spec = load_runtime_job_spec(verified.path)
        if spec.job_id != registry.job_id:
            raise ValueError("runtime spec belongs to another job")
        raw_intent = spec.metadata.get("review_intent")
        if not raw_intent:
            return [], []
        intent = ReviewIntent.from_dict(raw_intent)
    except (OSError, UnicodeError, json.JSONDecodeError, RegistryError, TypeError, ValueError) as exc:
        return [], [f"required_comparison_intent_untrusted:{exc}"]
    return [dict(item) for item in intent.required_comparisons], []


def _verified_dependency(
    registry: ArtifactRegistry,
    owner: ArtifactRecord,
    *,
    artifact_type: str,
) -> ArtifactRecord:
    refs = [
        ref for ref in owner.depends_on
        if ref.artifact_type == artifact_type and ref.dependency_kind == "local_job"
    ]
    if len(refs) != 1:
        raise ValueError(
            f"{owner.artifact_id} must bind exactly one {artifact_type} dependency"
        )
    ref = refs[0]
    if ref.job_id != registry.job_id:
        raise ValueError(f"{artifact_type} dependency is foreign to this Registry")
    record = registry.get(ref.artifact_id)
    if (
        record is None
        or record.status != "ready"
        or record.job_id != registry.job_id
        or record.artifact_type != ref.artifact_type
        or record.path != ref.path
        or record.content_hash != ref.content_hash
    ):
        raise ValueError(f"{artifact_type} dependency no longer matches its Registry reference")
    return record


def _read_outline_payload(
    registry: ArtifactRegistry,
    record: ArtifactRecord,
    *,
    artifact_class: type[Any],
    artifact_type: str,
    artifact_version: str,
    artifact_role: str,
) -> dict[str, Any]:
    if (
        record.job_id != registry.job_id
        or record.status != "ready"
        or record.artifact_type != artifact_type
        or record.artifact_version != artifact_version
        or record.artifact_role != artifact_role
    ):
        raise ValueError(f"{record.artifact_id} has the wrong job or artifact identity")
    raw = json.loads(Path(record.path).read_text(encoding="utf-8"))
    if (
        not isinstance(raw, Mapping)
        or raw.get("job_id") != registry.job_id
        or raw.get("artifact_type") != artifact_type
        or raw.get("artifact_version") != artifact_version
        or raw.get("status") != "ready"
    ):
        raise ValueError(f"{record.artifact_id} envelope identity is invalid")
    artifact = artifact_class.from_dict(raw)
    if (
        artifact.job_id != registry.job_id
        or artifact.content_hash != str(raw.get("content_hash") or "")
        or (
            str(record.metadata.get("content_hash") or "")
            and artifact.content_hash != str(record.metadata.get("content_hash") or "")
        )
    ):
        raise ValueError(f"{record.artifact_id} content envelope is invalid")
    return dict(artifact.payload)


def _support_signature(row: Mapping[str, Any]) -> tuple[Any, ...]:
    """Capture the source-side identity and closure used by a comparison slot."""

    def ids(name: str) -> tuple[str, ...]:
        raw = row.get(name) or ()
        if isinstance(raw, str):
            raw = [raw]
        return tuple(sorted({str(item) for item in raw if str(item)}))

    return (
        str(row.get("claim_slot_id") or ""),
        str(row.get("task_id") or ""),
        str(row.get("paper_key") or ""),
        str(row.get("primary_claim_id") or ""),
        ids("source_claim_ids"),
        ids("evidence_ids"),
        ids("source_field_ids"),
        ids("primary_evidence_ids"),
        ids("qualifier_source_claim_ids"),
        ids("qualifier_evidence_ids"),
        ids("qualifier_source_field_ids"),
        str(row.get("study_id") or ""),
    )


def _rows_for_task(value: Any, task_id: str) -> list[Mapping[str, Any]]:
    if not isinstance(value, list):
        return []
    return [
        row for row in value
        if isinstance(row, Mapping) and str(row.get("task_id") or "") == task_id
    ]


def _support_rows_match(
    expected: list[Mapping[str, Any]],
    observed: list[Mapping[str, Any]],
    *,
    task_id: str,
    paper_keys: tuple[str, ...],
) -> bool:
    if not expected or len(expected) != len(observed):
        return False
    expected_signatures = [_support_signature(row) for row in expected]
    observed_signatures = [_support_signature(row) for row in observed]
    if sorted(expected_signatures) != sorted(observed_signatures):
        return False
    if {str(row.get("paper_key") or "") for row in expected} != set(paper_keys):
        return False
    return all(
        signature[1] == task_id
        and bool(signature[0])
        and bool(signature[3])
        and bool(signature[4])
        and bool(signature[5])
        and bool(signature[6])
        for signature in expected_signatures
    )


def resolve_required_comparison_task_bindings(
    registry: ArtifactRegistry,
    draft: Mapping[str, Any],
    *, external_registry_resolver: Callable[[str], ArtifactRegistry | None] | None = None,
) -> tuple[RequiredComparisonBindings, list[str]]:
    """Map each durable required-comparison tuple to a verified Writer task.

    The lookup follows the draft's named final Outline and adoption, then the
    Outline's section-packet and relation-map dependency edges.  It does not
    inspect a mutable current-adoption pointer or a latest/static scope record.
    """

    if external_registry_resolver is None and registry.get("source_revision_plan") is not None:
        try:
            from runtime.runner import AgentRuntimeRunner

            workspace, _ = AgentRuntimeRunner._open_workspace(Path(registry.registry_path).parent)
            external_registry_resolver = AgentRuntimeRunner._external_registry_resolver(workspace)
        except (OSError, RegistryError, TypeError, ValueError) as exc:
            return {}, [f"required_comparison_outline_binding_untrusted:{exc}"]
    intents, issues = _durable_intent(registry, external_registry_resolver=external_registry_resolver)
    if issues or not intents:
        return {}, issues
    if (
        not isinstance(draft, Mapping)
        or draft.get("artifact_type") != "review_draft"
        or draft.get("artifact_version") != "v3"
    ):
        return {}, ["required_comparison_outline_binding_untrusted:review draft schema mismatch"]
    generation = draft.get("generation_context")
    if not isinstance(generation, Mapping):
        return {}, ["required_comparison_outline_binding_untrusted:review draft has no generation context"]
    outline_id = str(generation.get("outline_artifact_id") or "").strip()
    outline_hash = str(generation.get("outline_artifact_hash") or "").strip()
    adoption_id = str(generation.get("adoption_artifact_id") or "").strip()
    adoption_hash = str(generation.get("adoption_artifact_hash") or "").strip()
    if not outline_id or not outline_hash or not adoption_id or not adoption_hash:
        return {}, ["required_comparison_outline_binding_untrusted:review draft Outline/adoption binding is missing"]

    outline_record = registry.get(outline_id)
    adoption_record = registry.get(adoption_id)
    if (
        outline_record is None
        or outline_record.status != "ready"
        or outline_record.job_id != registry.job_id
        or outline_record.artifact_type != "final_outline"
        or outline_record.artifact_version != "v3"
        or outline_record.artifact_role != "outline_v3_node_output"
        or outline_record.content_hash != outline_hash
    ):
        return {}, ["required_comparison_outline_binding_untrusted:named final Outline is missing or stale"]
    if (
        adoption_record is None
        or adoption_record.status != "ready"
        or adoption_record.job_id != registry.job_id
        or adoption_record.artifact_type != "adopted_outline"
        or adoption_record.artifact_version != "v3"
        or adoption_record.artifact_role != "outline_v3_adoption"
        or adoption_record.content_hash != adoption_hash
    ):
        return {}, ["required_comparison_outline_binding_untrusted:named adoption is missing or stale"]
    scope_candidate_error: Exception | None = None
    packet_record: ArtifactRecord | None = None
    relation_record: ArtifactRecord | None = None
    try:
        packet_record = _verified_dependency(
            registry, outline_record, artifact_type="section_evidence_packet_set",
        )
        relation_record = _verified_dependency(
            registry, packet_record, artifact_type="confirmed_global_relation_map",
        )
    except (OSError, UnicodeError, json.JSONDecodeError, RegistryError, TypeError, ValueError) as exc:
        scope_candidate_error = exc

    if scope_candidate_error is None:
        assert packet_record is not None and relation_record is not None
        batch_roots = (outline_record, adoption_record, packet_record, relation_record)
    else:
        batch_roots = (outline_record, adoption_record)
    try:
        verified_roots = registry.verify_ready_artifact_closures(
            batch_roots, external_registry_resolver=external_registry_resolver,
        )
    except ArtifactClosureBatchError as exc:
        prefix = "required_comparison_outline_binding_untrusted"
        if scope_candidate_error is None and exc.root_index in {2, 3}:
            prefix = "required_comparison_scope_untrusted"
        return {}, [f"{prefix}:{exc.original_error}"]
    except (OSError, UnicodeError, json.JSONDecodeError, RegistryError, TypeError, ValueError) as exc:
        return {}, [f"required_comparison_outline_binding_untrusted:{exc}"]
    if scope_candidate_error is None:
        (
            verified_outline_record,
            verified_adoption_record,
            verified_packet_record,
            verified_relation_record,
        ) = verified_roots
    else:
        verified_outline_record, verified_adoption_record = verified_roots
        verified_packet_record = None
        verified_relation_record = None

    try:
        outline_payload = _read_outline_payload(
            registry,
            verified_outline_record,
            artifact_class=FinalOutline,
            artifact_type="final_outline",
            artifact_version="v3",
            artifact_role="outline_v3_node_output",
        )
        adoption_payload = _read_outline_payload(
            registry,
            verified_adoption_record,
            artifact_class=AdoptedOutline,
            artifact_type="adopted_outline",
            artifact_version="v3",
            artifact_role="outline_v3_adoption",
        )
    except (OSError, UnicodeError, json.JSONDecodeError, RegistryError, TypeError, ValueError) as exc:
        return {}, [f"required_comparison_outline_binding_untrusted:{exc}"]
    if (
        str(adoption_payload.get("adoption_id") or "") != adoption_id
        or str(adoption_payload.get("adoption_identity") or "") != adoption_id
        or str(adoption_payload.get("final_outline_hash") or "") != outline_hash
        or adoption_payload.get("final_outline") != outline_payload
        or not any(
            ref.artifact_id == outline_id
            and ref.job_id == registry.job_id
            and ref.artifact_type == "final_outline"
            and ref.path == verified_outline_record.path
            and ref.content_hash == verified_outline_record.content_hash
            for ref in verified_adoption_record.depends_on
        )
    ):
        return {}, ["required_comparison_outline_binding_untrusted:adoption does not bind the named final Outline"]

    if scope_candidate_error is not None:
        return {}, [f"required_comparison_scope_untrusted:{scope_candidate_error}"]

    assert verified_packet_record is not None and verified_relation_record is not None

    try:
        packet_payload = _read_outline_payload(
            registry,
            verified_packet_record,
            artifact_class=SectionEvidencePacketSet,
            artifact_type="section_evidence_packet_set",
            artifact_version="v3",
            artifact_role="outline_v3_node_output",
        )
        relation_payload = _read_outline_payload(
            registry,
            verified_relation_record,
            artifact_class=ConfirmedGlobalRelationMap,
            artifact_type="confirmed_global_relation_map",
            artifact_version="v3",
            artifact_role="outline_v3_node_output",
        )
    except (OSError, UnicodeError, json.JSONDecodeError, RegistryError, TypeError, ValueError) as exc:
        return {}, [f"required_comparison_scope_untrusted:{exc}"]

    outline_sections = [
        row for row in outline_payload.get("sections") or () if isinstance(row, Mapping)
    ]
    packets = [
        row for row in packet_payload.get("packets") or () if isinstance(row, Mapping)
    ]
    relations = [
        row for row in relation_payload.get("relations") or () if isinstance(row, Mapping)
    ]
    content = draft.get("content")
    writer_sections = [
        row for row in content.get("sections") or () if isinstance(row, Mapping)
    ] if isinstance(content, Mapping) else []

    bindings: RequiredComparisonBindings = {}
    for raw_intent in intents:
        try:
            key = required_comparison_key(raw_intent)
        except (TypeError, ValueError) as exc:
            issues.append(f"required_comparison_intent_untrusted:{exc}")
            continue
        if key in bindings:
            continue
        paper_keys, question, relation_type = key
        paper_set = set(paper_keys)
        matching_relations = [
            row for row in relations
            if set(str(item) for item in row.get("paper_keys") or () if str(item)) == paper_set
            and str(row.get("relation_type") or "").strip() == relation_type
            and "required_user_comparison" in {
                str(item) for item in row.get("diagnostics") or () if str(item)
            }
        ]
        if len(matching_relations) != 1:
            issues.append(_issue("required_comparison_task_binding_missing", pair=paper_keys))
            continue
        relation_id = str(matching_relations[0].get("relation_id") or "").strip()
        if not relation_id:
            issues.append(_issue("required_comparison_task_binding_missing", pair=paper_keys))
            continue
        task_id = f"relation_comparison:{relation_id}"
        matching_packets = [
            packet for packet in packets
            if relation_id in {str(item) for item in packet.get("relation_ids") or () if str(item)}
            and set(str(item) for item in packet.get("paper_keys") or () if str(item)) == paper_set
            and _normalized_text(packet.get("section_goal")) == _normalized_text(question)
        ]
        if len(matching_packets) != 1:
            issues.append(_issue("required_comparison_task_binding_missing", pair=paper_keys))
            continue
        packet = matching_packets[0]
        section_id = str(packet.get("section_id") or "").strip()
        matching_outline_sections = [
            section for section in outline_sections
            if str(section.get("section_id") or "").strip() == section_id
            and _normalized_text(section.get("goal")) == _normalized_text(question)
            and set(str(item) for item in section.get("paper_keys") or () if str(item)) == paper_set
            and relation_id in {str(item) for item in section.get("relation_ids") or () if str(item)}
            and task_id in {str(item) for item in section.get("task_ids") or () if str(item)}
        ]
        if len(matching_outline_sections) != 1:
            issues.append(_issue("required_comparison_task_binding_missing", pair=paper_keys))
            continue
        outline_section = matching_outline_sections[0]
        packet_support = _rows_for_task(packet.get("claim_support"), task_id)
        outline_support = _rows_for_task(outline_section.get("claim_support"), task_id)
        if (
            not _support_rows_match(packet_support, outline_support, task_id=task_id, paper_keys=paper_keys)
            or {str(row.get("paper_key") or "") for row in packet_support} != paper_set
        ):
            issues.append(_issue("required_comparison_task_support_untrusted", pair=paper_keys))
            continue

        expected_claims = {
            str(row.get("claim") or "").strip()
            for row in packet_support if str(row.get("claim") or "").strip()
        }
        if (
            len(expected_claims) != 1
            or not expected_claims.issubset({
                str(item).strip() for item in outline_section.get("claims") or () if str(item).strip()
            })
            or not expected_claims.issubset({
                str(item).strip() for item in packet.get("planned_claims") or () if str(item).strip()
            })
        ):
            issues.append(_issue("required_comparison_task_claim_untrusted", pair=paper_keys))
            continue

        matching_writer_scopes: list[tuple[Mapping[str, Any], Mapping[str, Any], Mapping[str, Any]]] = []
        for writer_section in writer_sections:
            scope = writer_section.get("writer_task_scope")
            if (
                not isinstance(scope, Mapping)
                or str(scope.get("section_id") or "").strip() != section_id
            ):
                continue
            matching_tasks = [
                task for task in scope.get("tasks") or ()
                if isinstance(task, Mapping)
                and str(task.get("planned_claim") or "").strip() in expected_claims
                and set(str(item) for item in task.get("paper_keys") or () if str(item)) == paper_set
                and _support_rows_match(
                    packet_support,
                    _rows_for_task(task.get("support_rows"), task_id),
                    task_id=task_id,
                    paper_keys=paper_keys,
                )
            ]
            if len(matching_tasks) == 1:
                matching_writer_scopes.append((writer_section, scope, matching_tasks[0]))
        if len(matching_writer_scopes) != 1:
            issues.append(_issue("required_comparison_writer_scope_missing", pair=paper_keys))
            continue
        writer_section, writer_scope, writer_task = matching_writer_scopes[0]
        if (
            writer_scope.get("scope_status") != "ready"
            or writer_scope.get("usable_for_provider_admission") is not True
            or writer_task.get("status") != "ready"
            or writer_task.get("task_kind") != "planned_claim"
        ):
            issues.append(_issue("required_comparison_writer_scope_untrusted", pair=paper_keys))
            continue
        basis = str(writer_scope.get("writer_task_basis_hash") or "")
        writer_task_id = str(writer_task.get("writer_task_id") or "")
        required_unit_ids = {
            str(unit.get("writer_output_unit_id") or "")
            for unit in writer_task.get("output_units") or ()
            if isinstance(unit, Mapping)
            and unit.get("required") is True
            and unit.get("unit_kind") == "planned_claim"
            and str(unit.get("writer_output_unit_id") or "")
        }
        dispositions = [
            row for row in writer_section.get("writer_task_dispositions") or ()
            if isinstance(row, Mapping)
            and str(row.get("writer_task_id") or "") == writer_task_id
        ]
        try:
            text_blocks = list(iter_review_text_blocks(writer_section))
        except (AttributeError, KeyError, TypeError, ValueError):
            text_blocks = []
        bound_blocks = [
            block for block in text_blocks
            if str(block.get("writer_task_id") or "") == writer_task_id
            and str(block.get("writer_output_unit_id") or "") in required_unit_ids
            and str(block.get("writer_task_basis_hash") or "") == basis
        ]
        if (
            not re.fullmatch(r"[0-9a-f]{64}", basis)
            or len(required_unit_ids) != 1
            or len(dispositions) != 1
            or dispositions[0].get("disposition") != "covered"
            or str(dispositions[0].get("writer_task_basis_hash") or "") != basis
            or len(bound_blocks) != 1
        ):
            issues.append(_issue("required_comparison_writer_output_unbound", pair=paper_keys))
            continue

        bindings[key] = {
            "paper_keys": list(paper_keys),
            "question": question,
            "relation_type": relation_type,
            "relation_id": relation_id,
            "task_id": task_id,
            "section_id": section_id,
            "claim_slot_ids": sorted({
                str(row.get("claim_slot_id") or "")
                for row in packet_support if str(row.get("claim_slot_id") or "")
            }),
            "writer_task_id": writer_task_id,
            "writer_output_unit_id": next(iter(required_unit_ids)),
            "writer_task_basis_hash": basis,
            "outline_artifact_id": outline_id,
            "outline_artifact_hash": outline_hash,
            "adoption_artifact_id": adoption_id,
            "adoption_artifact_hash": adoption_hash,
        }
    return bindings, issues


__all__ = [
    "RequiredComparisonBindings",
    "RequiredComparisonKey",
    "required_comparison_key",
    "resolve_required_comparison_task_bindings",
]
