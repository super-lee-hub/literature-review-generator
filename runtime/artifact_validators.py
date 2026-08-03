"""Type-specific validators for the current Outline v3 artifact family."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from outline.v3_models import compute_v3_hash


class ArtifactSchemaError(ValueError):
    """Raised when a current artifact is structurally or hash invalid."""


# These are the current Outline/validation artifacts that must never be
# accepted merely because they happen to be JSON objects.  The registry and
# every runtime reconciliation gate use the same dispatch table.
OUTLINE_V3_ARTIFACT_TYPES = frozenset(
    {
        "outline_artifact",
        "outline_evidence_views",
        "global_corpus_ledger",
        "multi_view_matrix",
        "review_intent",
        "coverage_contract",
        "global_relation_map",
        "organizing_axes_and_candidate_plans",
        "relation_adjudication_result",
        "confirmed_global_relation_map",
        "outline_candidate",
        "structure_critique",
        "coverage_critique",
        "evidence_critique",
        "arbitration_decision",
        "selected_outline_candidate",
        "section_evidence_packet",
        "section_evidence_packet_set",
        "final_outline",
        "coverage_audit",
        "stability_audit",
        "provider_receipt_closure",
        "outline_stage_health",
        "adopted_outline",
        "stage1_canonical_summaries",
        "outline_v3_node_dag",
    }
)

_ENVELOPE_TYPES = frozenset(
    {
        "outline_artifact",
        "relation_adjudication_result",
        "confirmed_global_relation_map",
        "outline_candidate",
        "structure_critique",
        "coverage_critique",
        "evidence_critique",
        "arbitration_decision",
        "selected_outline_candidate",
        "section_evidence_packet",
        "section_evidence_packet_set",
        "final_outline",
        "coverage_audit",
        "stability_audit",
        "provider_receipt_closure",
        "outline_stage_health",
        "adopted_outline",
    }
)

_REQUIRED_PAYLOAD_FIELDS: dict[str, tuple[str, ...]] = {
    "relation_adjudication_result": ("confirmed_relation_ids", "rejected_relations"),
    "confirmed_global_relation_map": ("relations", "paper_keys"),
    "outline_candidate": ("candidate_id",),
    "structure_critique": ("passed", "blocking_diagnostics"),
    "coverage_critique": ("passed", "blocking_diagnostics"),
    "evidence_critique": ("passed", "blocking_diagnostics"),
    "arbitration_decision": ("selected_candidate_id",),
    "selected_outline_candidate": ("candidate_id", "candidate_hash"),
    "section_evidence_packet": ("section_id", "paper_keys", "planned_claims"),
    "section_evidence_packet_set": ("packets", "coverage_ledger"),
    "final_outline": ("title", "sections", "paper_keys", "source_hashes"),
    "coverage_audit": (
        "passed",
        "quality_gate",
        "quality_gate_hash",
        "paper_coverage",
        "claim_coverage",
        "section_coverage",
        "research_streams",
        "quality_checks",
    ),
    "stability_audit": (
        "status",
        "variant_definitions",
        "variant_input_hashes",
        "variant_output_hashes",
        "comparisons",
        "checks",
        "failed_checks",
    ),
    "provider_receipt_closure": (
        "expected_call_ids",
        "observed_call_ids",
        "missing_call_ids",
        "hash_mismatches",
        "complete",
    ),
    "outline_stage_health": (
        "status",
        "adoption_eligible",
        "quality_gate",
        "quality_gate_hash",
        "diagnostics",
    ),
    "adopted_outline": (
        "status",
        "adoption_id",
        "adoption_identity",
        "current_pointer_artifact_id",
        "current_pointer_role",
        "actor",
        "reason",
        "expected_hash",
        "final_outline_hash",
        "coverage_audit_hash",
        "stability_audit_hash",
        "stage_health_hash",
        "provider_receipt_closure_hash",
        "final_outline",
    ),
}

_MODEL_REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    "outline_evidence_views": ("views", "source_summary_hashes", "blocking_diagnostics"),
    "global_corpus_ledger": ("entries", "source_summary_hashes", "blocking_diagnostics"),
    "multi_view_matrix": ("dimensions", "rows", "source_summary_hashes", "blocking_diagnostics"),
    "review_intent": ("review_question", "scope", "must_cover", "must_not_do"),
    "coverage_contract": ("corpus_paper_keys", "must_use_paper_keys", "required_dimensions"),
    "global_relation_map": ("relations", "paper_keys", "source_artifact_hashes"),
    "organizing_axes_and_candidate_plans": ("axes", "candidates"),
}


def _read_object(path: str | Path) -> dict[str, Any]:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ArtifactSchemaError(f"artifact is not valid JSON: {path}") from exc
    if not isinstance(raw, Mapping):
        raise ArtifactSchemaError(f"artifact root must be a JSON object: {path}")
    return dict(raw)


def _require_fields(payload: Mapping[str, Any], fields: tuple[str, ...], label: str) -> None:
    missing = [field for field in fields if field not in payload]
    if missing:
        raise ArtifactSchemaError(f"{label} is missing fields: {sorted(missing)}")


def _require_nonempty_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or not value:
        raise ArtifactSchemaError(f"{label} must be a non-empty JSON object")
    return value


def _validate_envelope(record: Any, root: Mapping[str, Any]) -> None:
    artifact_type = str(getattr(record, "artifact_type", "") or root.get("artifact_type") or "")
    if str(root.get("artifact_type") or "") != artifact_type:
        raise ArtifactSchemaError(f"artifact type mismatch: {artifact_type!r}")
    if str(root.get("artifact_version") or "") != "v3":
        raise ArtifactSchemaError(f"{artifact_type} must use artifact_version v3")
    if str(getattr(record, "artifact_version", "v3") or "") != "v3":
        raise ArtifactSchemaError(f"Registry version does not match {artifact_type}")
    job_id = str(root.get("job_id") or "")
    owner_job_id = str(getattr(record, "job_id", "") or "")
    if not job_id or (owner_job_id and job_id != owner_job_id):
        raise ArtifactSchemaError(f"{artifact_type} has an invalid job_id")
    dependency_hashes = root.get("dependency_hashes")
    if not isinstance(dependency_hashes, Mapping):
        raise ArtifactSchemaError(f"{artifact_type}.dependency_hashes must be an object")
    payload = _require_nonempty_mapping(root.get("payload"), f"{artifact_type}.payload")
    blocking = root.get("blocking_diagnostics")
    if not isinstance(blocking, list):
        raise ArtifactSchemaError(f"{artifact_type}.blocking_diagnostics must be an array")
    status = str(root.get("status") or "")
    if status not in {"ready", "blocked"}:
        raise ArtifactSchemaError(f"{artifact_type}.status is invalid")
    _require_fields(payload, _REQUIRED_PAYLOAD_FIELDS.get(artifact_type, ()), f"{artifact_type}.payload")
    canonical = {
        "artifact_type": artifact_type,
        "artifact_version": "v3",
        "job_id": job_id,
        "dependency_hashes": {str(key): str(value) for key, value in sorted(dependency_hashes.items())},
        "payload": payload,
        "blocking_diagnostics": [dict(item) for item in blocking if isinstance(item, Mapping)],
    }
    expected_hash = compute_v3_hash(canonical)
    if str(root.get("content_hash") or "") != expected_hash:
        raise ArtifactSchemaError(f"{artifact_type}.content_hash does not match canonical payload")
    if status == "ready" and blocking:
        raise ArtifactSchemaError(f"ready {artifact_type} cannot carry blocking diagnostics")


def _validate_model(record: Any, root: Mapping[str, Any]) -> None:
    artifact_type = str(getattr(record, "artifact_type", "") or root.get("artifact_type") or "")
    if str(root.get("artifact_type") or "") != artifact_type:
        raise ArtifactSchemaError(f"artifact type mismatch: {artifact_type!r}")
    if str(root.get("artifact_version") or "") != "v3":
        raise ArtifactSchemaError(f"{artifact_type} must use artifact_version v3")
    _require_fields(root, _MODEL_REQUIRED_FIELDS.get(artifact_type, ()), artifact_type)
    if artifact_type in {"outline_evidence_views", "global_corpus_ledger", "multi_view_matrix"}:
        if not isinstance(root.get("blocking_diagnostics"), list):
            raise ArtifactSchemaError(f"{artifact_type}.blocking_diagnostics must be an array")


def validate_current_outline_artifact(record: Any, path: str | Path) -> None:
    """Validate one current Outline artifact at every reconciliation gate."""

    artifact_type = str(getattr(record, "artifact_type", "") or "")
    if artifact_type not in OUTLINE_V3_ARTIFACT_TYPES:
        raise ArtifactSchemaError(f"unsupported current Outline artifact type: {artifact_type!r}")
    root = _read_object(path)
    if artifact_type in _ENVELOPE_TYPES:
        _validate_envelope(record, root)
    elif artifact_type == "stage1_canonical_summaries":
        _require_fields(root, ("job_id", "summaries"), artifact_type)
        if not isinstance(root.get("summaries"), list) or not root.get("summaries"):
            raise ArtifactSchemaError("stage1_canonical_summaries.summaries must be a non-empty array")
        if str(getattr(record, "job_id", "") or "") != str(root.get("job_id") or ""):
            raise ArtifactSchemaError("stage1_canonical_summaries job_id mismatch")
    elif artifact_type == "outline_v3_node_dag":
        _require_fields(root, ("job_id", "dag_version", "nodes", "content_hash"), artifact_type)
        if str(root.get("job_id") or "") != str(getattr(record, "job_id", "") or ""):
            raise ArtifactSchemaError("outline_v3_node_dag job_id mismatch")
        if not isinstance(root.get("nodes"), list) or not root.get("nodes"):
            raise ArtifactSchemaError("outline_v3_node_dag.nodes must be a non-empty array")
    else:
        _validate_model(record, root)


def make_outline_schema_validators() -> dict[str, Any]:
    return {artifact_type: validate_current_outline_artifact for artifact_type in OUTLINE_V3_ARTIFACT_TYPES}


__all__ = [
    "ArtifactSchemaError",
    "OUTLINE_V3_ARTIFACT_TYPES",
    "make_outline_schema_validators",
    "validate_current_outline_artifact",
]
