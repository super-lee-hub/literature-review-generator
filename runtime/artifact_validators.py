"""Type-specific validators for the current Outline v3 artifact family."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping
import zipfile

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
        "outline_provider_call_plan",
    }
)

# Non-outline artifacts which are consumed by the current validation,
# promotion, export, and forensic gates.  Legacy v1/v2 projections remain
# readable by their owning service; only the current version is admitted to
# the unified ready-artifact dispatch below.
CURRENT_PRODUCTION_ARTIFACT_TYPES = frozenset(
    {
        "review_draft",
        "review_draft_repaired",
        "citation_manifest",
        "citation_manifest_repaired",
        "citation_ref_catalog",
        "review_replay_ledger",
        "review_docx",
        "review_docx_repaired",
        "validation_run_result",
        "validation_run_result_repaired",
        "provider_receipt_closure",
        "provider_expected_call_graph",
        "stage1_summary_reuse_record",
        "stage1_reusable_summary_manifest",
        "validation_disposition",
        "lease_publication_manifest",
        "repair_plan",
        "repair_apply_result",
        "repair_report",
        "repair_transaction",
        "repair_promotion_transaction",
        "repair_lineage",
        "current_artifact_set",
        "current_artifact_set_pointer",
        "current_artifact_pointer",
        "outline_adoption_pointer",
        "export_bundle",
        "forensic_attestation",
        "provider_receipt_ledger",
        "review_section",
        "stage1_portable_summary_source",
        "stage1_portable_summary_manifest",
        "stage1_portable_provider_closure",
        "stage1_portable_provider_ledger",
    }
)

# These versions are historical projections still written by compatibility
# helpers.  They are not current production contracts; allowing them here
# keeps the Registry able to read/migrate old records without weakening any
# current-version validator below.
LEGACY_COMPATIBLE_ARTIFACT_VERSIONS: dict[str, frozenset[str]] = {
    "review_draft": frozenset({"v1", "v2"}),
    "citation_manifest": frozenset({"v1", "v2"}),
}

CURRENT_ARTIFACT_TYPES = OUTLINE_V3_ARTIFACT_TYPES | CURRENT_PRODUCTION_ARTIFACT_TYPES

# Outline artifacts are versioned individually.  The DAG snapshot and the
# immutable Stage 1 input predate the v3 envelope and are intentionally kept
# as explicit v1 contracts; every other current Outline artifact uses v3.
CURRENT_OUTLINE_ARTIFACT_VERSIONS: dict[str, frozenset[str]] = {
    artifact_type: frozenset({"v1", "v3"})
    if artifact_type == "provider_receipt_closure"
    else frozenset({"v1"})
    if artifact_type in {"outline_v3_node_dag", "stage1_canonical_summaries", "outline_provider_call_plan"}
    else frozenset({"v3"})
    for artifact_type in OUTLINE_V3_ARTIFACT_TYPES
}

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


def _validate_review_replay_ledger(record: Any, path: str | Path) -> None:
    """Validate the append-only JSONL ledger used to replay review sections."""

    if str(getattr(record, "artifact_version", "") or "") != "v1":
        raise ArtifactSchemaError("review_replay_ledger must use artifact_version v1")
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise ArtifactSchemaError(f"review_replay_ledger cannot be read: {path}") from exc
    required = (
        "replay_version",
        "section_id",
        "binding_hash",
        "artifact_id",
        "artifact_path",
        "artifact_content_hash",
        "registry_file_hash",
        "receipt_id",
        "normalized_output_hash",
    )
    records = 0
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ArtifactSchemaError(
                f"review_replay_ledger line {line_number} is not valid JSON"
            ) from exc
        if not isinstance(payload, Mapping):
            raise ArtifactSchemaError(
                f"review_replay_ledger line {line_number} must be an object"
            )
        missing = [field for field in required if not str(payload.get(field) or "")]
        if missing:
            raise ArtifactSchemaError(
                f"review_replay_ledger line {line_number} is missing fields: {sorted(missing)}"
            )
        if str(payload.get("replay_version") or "") != "review-section-replay-v1":
            raise ArtifactSchemaError(
                f"review_replay_ledger line {line_number} has an invalid replay_version"
            )
        records += 1
    if records == 0:
        raise ArtifactSchemaError("review_replay_ledger must contain at least one record")


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


def _require_owner(root: Mapping[str, Any], record: Any, artifact_type: str) -> None:
    owner = str(
        root.get("job_id")
        or root.get("created_from_job_id")
        or root.get("created_by_job_id")
        or root.get("owner_job_id")
        or ""
    )
    record_owner = str(getattr(record, "job_id", "") or "")
    if not owner or (record_owner and owner != record_owner):
        raise ArtifactSchemaError(f"{artifact_type} has an invalid owner job identity")


def _validate_production_identity(
    record: Any,
    root: Mapping[str, Any],
    *,
    expected_types: tuple[str, ...] | None = None,
    expected_version: str,
) -> None:
    artifact_type = str(getattr(record, "artifact_type", "") or "")
    root_type = str(root.get("artifact_type") or "")
    if expected_types is None:
        expected_types = (artifact_type,)
    if root_type not in expected_types:
        raise ArtifactSchemaError(
            f"{artifact_type} payload artifact_type {root_type!r} is not one of {expected_types!r}"
        )
    if str(root.get("artifact_version") or "") not in {expected_version, "v3" if expected_version == "v1" and artifact_type.endswith("_repaired") else expected_version}:
        raise ArtifactSchemaError(f"{artifact_type}.artifact_version is invalid")
    if str(getattr(record, "artifact_version", "") or "") != expected_version:
        raise ArtifactSchemaError(f"{artifact_type} registry version is not {expected_version}")
    _require_owner(root, record, artifact_type)


def _validate_review_json(record: Any, _path: str | Path, root: Mapping[str, Any]) -> None:
    artifact_type = str(getattr(record, "artifact_type", "") or "")
    expected_types = ("review_draft",) if artifact_type == "review_draft" else ("review_draft", "review_draft_repaired")
    _validate_production_identity(
        record,
        root,
        expected_types=expected_types,
        expected_version="v1" if artifact_type.endswith("_repaired") else "v3",
    )
    _require_fields(root, ("created_at", "draft_identity", "generation_context", "content", "projections"), artifact_type)
    if not isinstance(root.get("content"), Mapping) or not isinstance(root.get("draft_identity"), Mapping):
        raise ArtifactSchemaError(f"{artifact_type} content and draft_identity must be objects")


def _validate_citation_json(record: Any, _path: str | Path, root: Mapping[str, Any]) -> None:
    artifact_type = str(getattr(record, "artifact_type", "") or "")
    expected_types = ("citation_manifest",) if artifact_type == "citation_manifest" else ("citation_manifest", "citation_manifest_repaired")
    _validate_production_identity(
        record,
        root,
        expected_types=expected_types,
        expected_version="v1" if artifact_type.endswith("_repaired") else "v3",
    )
    _require_fields(
        root,
        ("created_at", "manifest_identity", "review_reference", "occurrences", "citation_sets", "bibliography"),
        artifact_type,
    )
    if not isinstance(root.get("occurrences"), list) or not isinstance(root.get("bibliography"), list):
        raise ArtifactSchemaError(f"{artifact_type} occurrences and bibliography must be arrays")


def _validate_citation_ref_catalog(record: Any, _path: str | Path, root: Mapping[str, Any]) -> None:
    """Validate the canonical R### catalog with its owning service contract."""

    _validate_production_identity(
        record,
        root,
        expected_types=("citation_ref_catalog",),
        expected_version="v1",
    )
    from services.citation_ref_catalog import validate_document_ref_catalog

    try:
        validate_document_ref_catalog(root)
    except (TypeError, ValueError, KeyError) as exc:
        raise ArtifactSchemaError(f"citation_ref_catalog failed canonical validation: {exc}") from exc


def _validate_repaired_json(record: Any, path: str | Path, root: Mapping[str, Any]) -> None:
    artifact_type = str(getattr(record, "artifact_type", "") or "")
    if artifact_type == "review_draft_repaired":
        _validate_review_json(record, path, root)
    elif artifact_type == "citation_manifest_repaired":
        _validate_citation_json(record, path, root)
    else:
        raise ArtifactSchemaError(f"unsupported repaired artifact type: {artifact_type}")


def _validate_docx(record: Any, path: str | Path, _root: Mapping[str, Any] | None = None) -> None:
    artifact_type = str(getattr(record, "artifact_type", "") or "")
    if str(getattr(record, "artifact_version", "") or "") != "v1":
        raise ArtifactSchemaError(f"{artifact_type} must use artifact_version v1")
    file_path = Path(path)
    if file_path.stat().st_size <= 0:
        raise ArtifactSchemaError(f"{artifact_type} is empty")
    try:
        with zipfile.ZipFile(file_path) as archive:
            names = set(archive.namelist())
            if "[Content_Types].xml" not in names or "word/document.xml" not in names:
                raise ArtifactSchemaError(f"{artifact_type} is not a valid OOXML document")
    except zipfile.BadZipFile as exc:
        raise ArtifactSchemaError(f"{artifact_type} is not a valid OOXML document") from exc


def _validate_validation_result(record: Any, _path: str | Path, root: Mapping[str, Any]) -> None:
    from validation.run_result import ValidationRunResultV1

    artifact_type = str(getattr(record, "artifact_type", "") or "")
    if artifact_type == "validation_run_result_repaired":
        allowed = ("validation_run_result", "validation_run_result_repaired")
    else:
        allowed = ("validation_run_result",)
    _validate_production_identity(record, root, expected_types=allowed, expected_version="v1")
    try:
        ValidationRunResultV1.from_dict(root)
    except (TypeError, ValueError, KeyError) as exc:
        raise ArtifactSchemaError(f"{artifact_type} failed ValidationRunResultV1 validation: {exc}") from exc


def _validate_receipt_closure(record: Any, _path: str | Path, root: Mapping[str, Any]) -> None:
    _validate_production_identity(record, root, expected_types=("provider_receipt_closure",), expected_version="v1")
    payload_value = root.get("payload")
    payload: Mapping[str, Any] = payload_value if isinstance(payload_value, Mapping) else root
    _require_fields(payload, ("closure_epoch_id", "expected_call_ids", "observed_call_ids", "missing_call_ids", "hash_mismatches", "complete", "closure_hash"), "provider_receipt_closure.payload")
    if not isinstance(payload.get("expected_call_ids"), list) or not isinstance(payload.get("hash_mismatches"), Mapping):
        raise ArtifactSchemaError("provider_receipt_closure payload has invalid call graph fields")
    if not isinstance(payload.get("complete"), bool) or not str(payload.get("closure_hash") or ""):
        raise ArtifactSchemaError("provider_receipt_closure payload has invalid completion identity")


def _validate_provider_expected_call_graph(record: Any, _path: str | Path, root: Mapping[str, Any]) -> None:
    _validate_production_identity(
        record,
        root,
        expected_types=("provider_expected_call_graph",),
        expected_version="v1",
    )
    _require_fields(
        root,
        (
            "job_id",
            "stage_name",
            "attempt_id",
            "closure_epoch_id",
            "expected_call_graph_hash",
            "source_bundle_hash",
            "runtime_spec_hash",
            "expected_calls",
        ),
        "provider_expected_call_graph",
    )
    expected_calls = root.get("expected_calls")
    if not isinstance(expected_calls, list):
        raise ArtifactSchemaError("provider_expected_call_graph.expected_calls must be an array")
    for index, item in enumerate(expected_calls):
        if not isinstance(item, Mapping):
            raise ArtifactSchemaError(f"provider_expected_call_graph.expected_calls[{index}] is not an object")
        _require_fields(
            item,
            (
                "call_id",
                "job_id",
                "attempt_id",
                "stage_name",
                "node_id",
                "prompt_hash",
                "input_hash",
                "config_hash",
                "schema_hash",
                "closure_epoch_id",
                "expected_call_graph_hash",
            ),
            f"provider_expected_call_graph.expected_calls[{index}]",
        )
        if str(item.get("job_id") or "") != str(root.get("job_id") or ""):
            raise ArtifactSchemaError("provider_expected_call_graph call job_id mismatch")
        if str(item.get("closure_epoch_id") or "") != str(root.get("closure_epoch_id") or ""):
            raise ArtifactSchemaError("provider_expected_call_graph call epoch mismatch")
        if str(item.get("expected_call_graph_hash") or "") != str(root.get("expected_call_graph_hash") or ""):
            raise ArtifactSchemaError("provider_expected_call_graph call graph hash mismatch")
    for label in ("expected_call_graph_hash", "source_bundle_hash", "runtime_spec_hash"):
        value = str(root.get(label) or "")
        if len(value) != 64:
            raise ArtifactSchemaError(f"provider_expected_call_graph.{label} must be a SHA-256 hex digest")


def _validate_stage1_summary_reuse_record(record: Any, _path: str | Path, root: Mapping[str, Any]) -> None:
    _validate_production_identity(
        record,
        root,
        expected_types=("stage1_summary_reuse_record",),
        expected_version="v1",
    )
    _require_fields(
        root,
        (
            "stage_name",
            "attempt_id",
            "reused_summary_artifact_id",
            "reused_summary_artifact_hash",
            "summary_payload_hash",
            "registered_source_artifact_id",
            "registered_source_artifact_hash",
            "registry_file_hash",
            "source_summary_manifest_id",
            "source_summary_manifest_hash",
            "source_bundle_paper_identity",
            "input_manifest_hashes",
            "original_provider_receipt_ids",
            "current_runtime_spec_id",
            "current_runtime_spec_hash",
            "current_evidence_manifest_id",
            "current_evidence_manifest_hash",
            "reuse_policy",
            "reuse_decision_reason",
            "created_at",
            "content_hash",
        ),
        "stage1_summary_reuse_record",
    )
    identity = root.get("source_bundle_paper_identity")
    manifests = root.get("input_manifest_hashes")
    receipt_ids = root.get("original_provider_receipt_ids")
    if not isinstance(identity, Mapping) or not str(identity.get("canonical_paper_key") or ""):
        raise ArtifactSchemaError("stage1_summary_reuse_record paper identity is invalid")
    if not isinstance(manifests, Mapping) or not manifests:
        raise ArtifactSchemaError("stage1_summary_reuse_record input manifests are invalid")
    if not isinstance(receipt_ids, list):
        raise ArtifactSchemaError("stage1_summary_reuse_record original receipt IDs must be an array")
    for label in (
        "reused_summary_artifact_hash",
        "summary_payload_hash",
        "registry_file_hash",
        "source_summary_manifest_hash",
        "current_runtime_spec_hash",
        "current_evidence_manifest_hash",
        "content_hash",
    ):
        value = str(root.get(label) or "")
        if len(value) != 64 or any(char not in "0123456789abcdef" for char in value.lower()):
            raise ArtifactSchemaError(f"stage1_summary_reuse_record.{label} is not a SHA-256 hex digest")
    registered_id = str(root.get("registered_source_artifact_id") or "")
    registered_hash = str(root.get("registered_source_artifact_hash") or "")
    reused_id = str(root.get("reused_summary_artifact_id") or "")
    reused_hash = str(root.get("reused_summary_artifact_hash") or "")
    if bool(registered_id) != bool(registered_hash):
        raise ArtifactSchemaError(
            "stage1_summary_reuse_record registered source identity must be paired"
        )
    if registered_id and (registered_id != reused_id or registered_hash != reused_hash):
        raise ArtifactSchemaError(
            "stage1_summary_reuse_record registered source is not bound to reused summary"
        )
    # New records distinguish the immutable parent authority from the current
    # run's derived snapshot.  The legacy registered_source fields are kept in
    # the schema for compatibility, but may intentionally be empty.
    authority_id = str(root.get("source_authority_artifact_id") or "")
    authority_hash = str(root.get("source_authority_artifact_hash") or "")
    if bool(authority_id) != bool(authority_hash):
        raise ArtifactSchemaError(
            "stage1_summary_reuse_record source authority identity must be paired"
        )
    if authority_id or authority_hash:
        if authority_id != reused_id or authority_hash != reused_hash:
            raise ArtifactSchemaError(
                "stage1_summary_reuse_record source authority is not bound to reused summary"
            )
    elif not registered_id:
        raise ArtifactSchemaError(
            "stage1_summary_reuse_record has no registered source authority"
        )
    if str(root.get("reuse_policy") or "") != "exact_summary_reuse_v1":
        raise ArtifactSchemaError("stage1_summary_reuse_record reuse policy is invalid")
    from runtime.provider_runtime import hash_json

    canonical = dict(root)
    expected_hash = str(canonical.pop("content_hash") or "")
    if hash_json(canonical) != expected_hash:
        raise ArtifactSchemaError("stage1_summary_reuse_record.content_hash does not match content")


def _validate_stage1_reusable_summary_manifest(
    record: Any,
    _path: str | Path,
    root: Mapping[str, Any],
) -> None:
    _validate_production_identity(
        record,
        root,
        expected_types=("stage1_reusable_summary_manifest",),
        expected_version="v1",
    )
    _require_fields(
        root,
        (
            "stage_name",
            "canonical_paper_key",
            "source_paper_id",
            "source_summary_artifact_id",
            "source_summary_artifact_hash",
            "summary_payload_hash",
            "binding_hash",
            "runtime_spec_id",
            "runtime_spec_hash",
            "evidence_manifest_id",
            "evidence_manifest_hash",
            "source_bundle_id",
            "source_bundle_hash",
            "created_at",
            "producer",
        ),
        "stage1_reusable_summary_manifest",
    )
    for label in (
        "source_summary_artifact_hash",
        "summary_payload_hash",
        "binding_hash",
        "runtime_spec_hash",
        "evidence_manifest_hash",
        "source_bundle_hash",
    ):
        value = str(root.get(label) or "")
        if len(value) != 64 or any(char not in "0123456789abcdef" for char in value.lower()):
            raise ArtifactSchemaError(
                f"stage1_reusable_summary_manifest.{label} is not a SHA-256 hex digest"
            )
    for id_label, hash_label in (
        ("provider_receipt_closure_id", "provider_receipt_closure_hash"),
        ("provider_receipt_ledger_id", "provider_receipt_ledger_hash"),
    ):
        identifier = str(root.get(id_label) or "")
        digest = str(root.get(hash_label) or "")
        if bool(identifier) != bool(digest):
            raise ArtifactSchemaError(
                f"stage1_reusable_summary_manifest {id_label}/{hash_label} must be paired"
            )
        if digest and (
            len(digest) != 64
            or any(char not in "0123456789abcdef" for char in digest.lower())
        ):
            raise ArtifactSchemaError(
                f"stage1_reusable_summary_manifest.{hash_label} is not a SHA-256 hex digest"
            )


def _validate_stage1_portable_metadata(record: Any, *, expected_type: str) -> Mapping[str, Any]:
    if (
        str(getattr(record, "artifact_type", "") or "") != expected_type
        or str(getattr(record, "artifact_version", "") or "") != "v1"
    ):
        raise ArtifactSchemaError(f"{expected_type} Registry identity is invalid")
    metadata_value = getattr(record, "metadata", None)
    metadata = metadata_value if isinstance(metadata_value, Mapping) else {}
    _require_fields(
        metadata,
        (
            "authority_kind",
            "stage_name",
            "source_authority_job_id",
            "original_artifact_id",
            "original_artifact_hash",
            "typed_manifest_artifact_id",
            "typed_manifest_artifact_hash",
        ),
        f"{expected_type}.metadata",
    )
    if (
        metadata.get("authority_kind") != "typed_manifest"
        or str(metadata.get("stage_name") or "") != "stage1_analyze"
        or not str(metadata.get("source_authority_job_id") or "")
        or not str(metadata.get("original_artifact_id") or "")
        or not str(metadata.get("typed_manifest_artifact_id") or "")
    ):
        raise ArtifactSchemaError(f"{expected_type} portable authority metadata is invalid")
    for label in (
        "original_artifact_hash",
        "typed_manifest_artifact_hash",
    ):
        digest = str(metadata.get(label) or "")
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest.lower()):
            raise ArtifactSchemaError(f"{expected_type}.metadata.{label} is not SHA-256")
    if str(metadata.get("original_artifact_hash") or "") != str(
        getattr(record, "content_hash", "") or ""
    ):
        raise ArtifactSchemaError(f"{expected_type} original byte hash is inconsistent")
    if expected_type == "stage1_portable_summary_manifest" and (
        str(metadata.get("typed_manifest_artifact_id") or "")
        != str(metadata.get("original_artifact_id") or "")
        or str(metadata.get("typed_manifest_artifact_hash") or "")
        != str(getattr(record, "content_hash", "") or "")
    ):
        raise ArtifactSchemaError(
            "stage1 portable summary manifest metadata is not self-bound"
        )
    return metadata


def _validate_stage1_portable_summary_source(
    record: Any,
    path: str | Path,
    _root: Mapping[str, Any] | None = None,
) -> None:
    _validate_stage1_portable_metadata(
        record,
        expected_type="stage1_portable_summary_source",
    )
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ArtifactSchemaError("stage1 portable summary source is not valid JSON") from exc
    items = raw if isinstance(raw, list) else [raw] if isinstance(raw, Mapping) else []
    if not items:
        raise ArtifactSchemaError("stage1 portable summary source is empty")
    for index, item in enumerate(items):
        if (
            not isinstance(item, Mapping)
            or not isinstance(item.get("ai_summary"), Mapping)
            or not isinstance(item.get("paper_info"), Mapping)
            or not str(item["paper_info"].get("canonical_paper_key") or "")
        ):
            raise ArtifactSchemaError(
                f"stage1 portable summary source item {index} is not canonical"
            )


def _validate_stage1_portable_summary_manifest(
    record: Any,
    _path: str | Path,
    root: Mapping[str, Any],
) -> None:
    metadata = _validate_stage1_portable_metadata(
        record,
        expected_type="stage1_portable_summary_manifest",
    )
    _require_fields(
        root,
        (
            "artifact_type",
            "artifact_version",
            "job_id",
            "stage_name",
            "canonical_paper_key",
            "source_summary_artifact_id",
            "source_summary_artifact_hash",
            "summary_payload",
            "summary_payload_hash",
            "normalized_summary_payload_hash",
            "binding",
            "binding_hash",
            "manifest_content_hash",
            "provider_receipt_closure_id",
            "provider_receipt_closure_hash",
            "provider_receipt_ledger_id",
            "provider_receipt_ledger_hash",
        ),
        "stage1_portable_summary_manifest",
    )
    if (
        root.get("artifact_type") != "stage1_reusable_summary_manifest"
        or root.get("artifact_version") != "v1"
        or str(root.get("job_id") or "")
        != str(metadata.get("source_authority_job_id") or "")
        or str(root.get("stage_name") or "") != "stage1_analyze"
        or not isinstance(root.get("summary_payload"), Mapping)
        or not isinstance(root.get("binding"), Mapping)
    ):
        raise ArtifactSchemaError("stage1 portable summary manifest identity is invalid")
    from runtime.provider_runtime import hash_json

    manifest_content_hash = str(root.get("manifest_content_hash") or "")
    normalized_manifest = dict(root)
    normalized_manifest["manifest_content_hash"] = ""
    if hash_json(normalized_manifest) != manifest_content_hash:
        raise ArtifactSchemaError("stage1 portable summary manifest self hash is invalid")
    summary_hash = hash_json(root["summary_payload"])
    if any(
        str(root.get(label) or "") != summary_hash
        for label in ("summary_payload_hash", "normalized_summary_payload_hash")
    ):
        raise ArtifactSchemaError("stage1 portable summary manifest summary hash is invalid")
    normalized_binding = dict(root["binding"])
    normalized_binding.pop("source_summary_manifest_id", None)
    normalized_binding.pop("source_summary_manifest_hash", None)
    if hash_json(normalized_binding) != str(root.get("binding_hash") or ""):
        raise ArtifactSchemaError("stage1 portable summary manifest binding hash is invalid")
    for label in (
        "source_summary_artifact_hash",
        "binding_hash",
        "manifest_content_hash",
        "provider_receipt_closure_hash",
        "provider_receipt_ledger_hash",
    ):
        digest = str(root.get(label) or "")
        if digest and (
            len(digest) != 64
            or any(char not in "0123456789abcdef" for char in digest.lower())
        ):
            raise ArtifactSchemaError(f"stage1 portable summary manifest {label} is invalid")


def _validate_stage1_portable_provider_closure(
    record: Any,
    _path: str | Path,
    root: Mapping[str, Any],
) -> None:
    metadata = _validate_stage1_portable_metadata(
        record,
        expected_type="stage1_portable_provider_closure",
    )
    if (
        root.get("artifact_type") != "provider_receipt_closure"
        or root.get("artifact_version") != "v1"
        or str(root.get("job_id") or "")
        != str(metadata.get("source_authority_job_id") or "")
        or str(root.get("stage_name") or "") != "stage1_analyze"
        or not isinstance(root.get("expected_calls"), list)
    ):
        raise ArtifactSchemaError("stage1 portable provider closure identity is invalid")
    payload_value = root.get("payload")
    if not isinstance(payload_value, Mapping):
        raise ArtifactSchemaError("stage1 portable provider closure payload is invalid")
    _require_fields(
        payload_value,
        (
            "closure_epoch_id",
            "expected_call_ids",
            "observed_call_ids",
            "missing_call_ids",
            "hash_mismatches",
            "complete",
            "closure_hash",
        ),
        "stage1_portable_provider_closure.payload",
    )
    if payload_value.get("complete") is not True:
        raise ArtifactSchemaError("stage1 portable provider closure is incomplete")


def _validate_stage1_portable_provider_ledger(
    record: Any,
    path: str | Path,
    _root: Mapping[str, Any] | None = None,
) -> None:
    _validate_stage1_portable_metadata(
        record,
        expected_type="stage1_portable_provider_ledger",
    )
    _validate_receipt_ledger(record, path)


def _validate_outline_provider_call_plan(record: Any, _path: str | Path, root: Mapping[str, Any]) -> None:
    _validate_production_identity(
        record,
        root,
        expected_types=("outline_provider_call_plan",),
        expected_version="v1",
    )
    _require_fields(
        root,
        (
            "job_id",
            "stage_name",
            "closure_epoch_id",
            "provider_call_plan_hash",
            "provider_call_plans",
            "pricing_policy",
        ),
        "outline_provider_call_plan",
    )
    if str(root.get("stage_name") or "") != "outline_v3":
        raise ArtifactSchemaError("outline_provider_call_plan stage_name is invalid")
    if len(str(root.get("provider_call_plan_hash") or "")) != 64:
        raise ArtifactSchemaError("outline_provider_call_plan hash is invalid")
    root_cost_status = str(root.get("cost_status") or "")
    if root_cost_status not in {"estimate", "unknown"}:
        raise ArtifactSchemaError("outline_provider_call_plan cost_status is invalid")
    if root_cost_status == "unknown" and root.get("estimated_cost") is not None:
        raise ArtifactSchemaError(
            "outline_provider_call_plan cannot claim an estimated cost when pricing is unknown"
        )
    plans = root.get("provider_call_plans")
    if not isinstance(plans, list) or not plans:
        raise ArtifactSchemaError("outline_provider_call_plan must contain plan rows")
    required = (
        "variant_name",
        "node_id",
        "call_id",
        "provider",
        "model",
        "estimated_input_tokens",
        "estimated_output_tokens",
        "estimated_reasoning_tokens",
        "estimated_cached_input_tokens",
        "estimated_cache_write_tokens",
        "estimated_total_tokens",
        "estimated_cost",
        "pricing_source",
        "pricing_policy",
        "cost_status",
        "assumptions",
        "confidence",
        "upper_bound",
        "transport_expected",
    )
    for index, item in enumerate(plans):
        if not isinstance(item, Mapping):
            raise ArtifactSchemaError(f"outline_provider_call_plan row {index} is not an object")
        _require_fields(item, required, f"outline_provider_call_plan[{index}]")
        for field_name in (
            "estimated_input_tokens",
            "estimated_output_tokens",
            "estimated_reasoning_tokens",
            "estimated_cached_input_tokens",
            "estimated_cache_write_tokens",
            "estimated_total_tokens",
        ):
            raw_value: Any = item.get(field_name)
            try:
                value = int(raw_value)
            except (TypeError, ValueError) as exc:
                raise ArtifactSchemaError(
                    f"outline_provider_call_plan[{index}].{field_name} must be an integer"
                ) from exc
            if value < 0:
                raise ArtifactSchemaError(
                    f"outline_provider_call_plan[{index}].{field_name} cannot be negative"
                )
        estimated_cost = item.get("estimated_cost")
        cost_status = str(item.get("cost_status") or "")
        if cost_status not in {"estimate", "unknown"}:
            raise ArtifactSchemaError(
                f"outline_provider_call_plan[{index}].cost_status is invalid"
            )
        if cost_status == "unknown" and estimated_cost is not None:
            raise ArtifactSchemaError(
                f"outline_provider_call_plan[{index}] cannot include estimated_cost when pricing is unknown"
            )
        if estimated_cost is not None:
            try:
                if float(estimated_cost) < 0:
                    raise ArtifactSchemaError(
                        f"outline_provider_call_plan[{index}].estimated_cost cannot be negative"
                    )
            except (TypeError, ValueError) as exc:
                raise ArtifactSchemaError(
                    f"outline_provider_call_plan[{index}].estimated_cost is invalid"
                ) from exc
        if str(item.get("confidence") or "") not in {"low", "medium", "high"}:
            raise ArtifactSchemaError(f"outline_provider_call_plan[{index}].confidence is invalid")
        if not isinstance(item.get("assumptions"), list):
            raise ArtifactSchemaError(f"outline_provider_call_plan[{index}].assumptions must be an array")
        if not isinstance(item.get("upper_bound"), bool) or not isinstance(item.get("transport_expected"), bool):
            raise ArtifactSchemaError(f"outline_provider_call_plan[{index}] has invalid bound flags")


def _validate_validation_disposition(record: Any, _path: str | Path, root: Mapping[str, Any]) -> None:
    _validate_production_identity(
        record,
        root,
        expected_types=("validation_disposition",),
        expected_version="v1",
    )
    try:
        from validation.disposition import ValidationDispositionV1

        ValidationDispositionV1.from_dict(root)
    except (TypeError, ValueError, KeyError) as exc:
        raise ArtifactSchemaError(f"validation_disposition is invalid: {exc}") from exc


def _validate_lease_publication_manifest(record: Any, _path: str | Path, root: Mapping[str, Any]) -> None:
    _validate_production_identity(
        record,
        root,
        expected_types=("lease_publication_manifest",),
        expected_version="v1",
    )
    _require_fields(
        root,
        (
            "job_id",
            "lease_id",
            "lease_generation",
            "fence_token",
            "target_path",
            "final_path",
            "content_hash",
            "size_bytes",
            "registered_artifact_id",
            "registered_artifact_type",
            "registered_artifact_version",
            "registered_artifact_hash",
        ),
        "lease_publication_manifest",
    )
    raw_lease_generation: Any = root.get("lease_generation")
    raw_size_bytes: Any = root.get("size_bytes")
    if not isinstance(raw_lease_generation, int) or int(raw_lease_generation) < 0:
        raise ArtifactSchemaError("lease_publication_manifest.lease_generation is invalid")
    if not isinstance(raw_size_bytes, int) or int(raw_size_bytes) < 0:
        raise ArtifactSchemaError("lease_publication_manifest.size_bytes is invalid")
    if len(str(root.get("content_hash") or "")) != 64:
        raise ArtifactSchemaError("lease_publication_manifest.content_hash is invalid")
    if len(str(root.get("registered_artifact_hash") or "")) != 64:
        raise ArtifactSchemaError("lease_publication_manifest.registered_artifact_hash is invalid")
    if not str(root.get("registered_artifact_type") or ""):
        raise ArtifactSchemaError("lease_publication_manifest.registered_artifact_type is invalid")
    if not str(root.get("registered_artifact_version") or ""):
        raise ArtifactSchemaError("lease_publication_manifest.registered_artifact_version is invalid")
    if str(root.get("content_hash") or "") != str(root.get("registered_artifact_hash") or ""):
        raise ArtifactSchemaError("lease_publication_manifest target hash is inconsistent")
    dependencies = list(getattr(record, "depends_on", ()) or ())
    if len(dependencies) != 1:
        raise ArtifactSchemaError("lease_publication_manifest must have exactly one target dependency")
    dependency = dependencies[0]
    if (
        str(getattr(dependency, "dependency_kind", "") or "") != "local_job"
        or str(getattr(dependency, "job_id", "") or "") != str(root.get("job_id") or "")
        or str(getattr(dependency, "artifact_id", "") or "")
        != str(root.get("registered_artifact_id") or "")
        or str(getattr(dependency, "artifact_type", "") or "")
        != str(root.get("registered_artifact_type") or "")
        or os.path.normcase(os.path.abspath(os.fspath(getattr(dependency, "path", "") or "")))
        != os.path.normcase(os.path.abspath(os.fspath(root.get("final_path") or "")))
        or str(getattr(dependency, "content_hash", "") or "")
        != str(root.get("registered_artifact_hash") or "")
    ):
        raise ArtifactSchemaError("lease_publication_manifest target dependency is inconsistent")
    metadata = getattr(record, "metadata", {}) or {}
    if (
        not isinstance(metadata, Mapping)
        or str(metadata.get("target_artifact_id") or "")
        != str(root.get("registered_artifact_id") or "")
        or str(metadata.get("target_artifact_type") or "")
        != str(root.get("registered_artifact_type") or "")
        or str(metadata.get("target_artifact_version") or "")
        != str(root.get("registered_artifact_version") or "")
        or str(metadata.get("target_artifact_hash") or "")
        != str(root.get("registered_artifact_hash") or "")
        or metadata.get("immutable") is not True
    ):
        raise ArtifactSchemaError("lease_publication_manifest Registry metadata is inconsistent")


def _validate_receipt_ledger(record: Any, path: str | Path, _root: Mapping[str, Any] | None = None) -> None:
    if str(getattr(record, "artifact_version", "") or "") != "v1":
        raise ArtifactSchemaError("provider_receipt_ledger must use artifact_version v1")
    records = 0
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise ArtifactSchemaError("provider_receipt_ledger cannot be read") from exc
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ArtifactSchemaError(f"provider_receipt_ledger line {line_number} is invalid JSON") from exc
        if not isinstance(payload, Mapping):
            raise ArtifactSchemaError(f"provider_receipt_ledger line {line_number} is not an object")
        _require_fields(payload, ("artifact_type", "artifact_version", "receipt_id", "call_id", "job_id", "stage_name", "status"), "provider_receipt_ledger entry")
        if str(payload.get("artifact_type") or "") != "provider_call_receipt":
            raise ArtifactSchemaError("provider receipt ledger entry has an invalid artifact_type")
        if str(payload.get("artifact_version") or "") != "v2":
            raise ArtifactSchemaError("provider receipt ledger entry is not provider_call_receipt v2")
        try:
            from runtime.provider_runtime import ProviderCallReceiptV1

            ProviderCallReceiptV1.from_dict(payload)
        except (TypeError, ValueError, KeyError) as exc:
            raise ArtifactSchemaError(
                f"provider receipt ledger entry failed ProviderCallReceiptV1 validation: {exc}"
            ) from exc
        records += 1
    if records == 0:
        raise ArtifactSchemaError("provider_receipt_ledger must contain at least one receipt")


def _validate_current_set(record: Any, _path: str | Path, root: Mapping[str, Any]) -> None:
    from services.artifact_registry import CurrentArtifactSetV1

    _validate_production_identity(record, root, expected_types=("current_artifact_set",), expected_version="v1")
    try:
        current_set = CurrentArtifactSetV1.from_dict(root)
    except (TypeError, ValueError) as exc:
        raise ArtifactSchemaError("current_artifact_set is not a valid CurrentArtifactSetV1") from exc
    if current_set.job_id != str(getattr(record, "job_id", "") or ""):
        raise ArtifactSchemaError("current_artifact_set job_id mismatch")
    if len(current_set.promotion_transaction_hash) != 64:
        raise ArtifactSchemaError("current_artifact_set promotion_transaction_hash is invalid")
    if current_set.validation_status == "not_requested":
        if not current_set.validation_disposition_artifact_id or len(current_set.validation_disposition_artifact_hash) != 64:
            raise ArtifactSchemaError("not_requested current_artifact_set requires validation disposition")
    elif not current_set.validation_run_result_artifact_id or len(current_set.validation_run_result_artifact_hash) != 64:
        raise ArtifactSchemaError("validated current_artifact_set requires validation run result")


def _validate_current_set_pointer(record: Any, path: str | Path, root: Mapping[str, Any]) -> None:
    _validate_current_set(record, path, root)
    current_set = root
    metadata = getattr(record, "metadata", {}) or {}
    if str(metadata.get("current_set_id") or "") != str(current_set.get("set_id") or ""):
        raise ArtifactSchemaError("current_artifact_set_pointer current_set_id mismatch")
    if str(metadata.get("current_set_hash") or "") != str(getattr(record, "content_hash", "") or ""):
        raise ArtifactSchemaError("current_artifact_set_pointer current_set_hash mismatch")


def _validate_current_pointer(record: Any, _path: str | Path, root: Mapping[str, Any]) -> None:
    _validate_production_identity(record, root, expected_types=("current_artifact_pointer",), expected_version="v1")
    _require_fields(
        root,
        ("pointer_kind", "pointer_role", "target_artifact_id", "target_content_hash", "target_path", "promotion_transaction_id", "updated_at"),
        "current_artifact_pointer",
    )
    if str(root.get("pointer_role") or "") != "current" or len(str(root.get("target_content_hash") or "")) != 64:
        raise ArtifactSchemaError("current_artifact_pointer target identity is invalid")


def _validate_outline_adoption_pointer(record: Any, _path: str | Path, root: Mapping[str, Any]) -> None:
    _validate_production_identity(record, root, expected_types=("outline_adoption_pointer", "current_outline_adoption_pointer"), expected_version="v1")
    if str(getattr(record, "artifact_id", "") or "") == "outline-v3:adoption:current":
        _require_fields(
            root,
            (
                "role",
                "current_adoption_artifact_id",
                "current_adoption_hash",
                "adoption_identity",
                "updated_at",
            ),
            "outline_adoption_pointer",
        )
        if str(root.get("role") or "") != "current" or len(str(root.get("current_adoption_hash") or "")) != 64:
            raise ArtifactSchemaError("current outline adoption pointer identity is invalid")
    else:
        _require_fields(root, ("status", "adoption_id", "expected_hash"), "outline_adoption_pointer")
        if str(root.get("expected_hash") or "") == "":
            raise ArtifactSchemaError("outline_adoption_pointer.expected_hash cannot be empty")


def _validate_repair_plan(record: Any, _path: str | Path, root: Mapping[str, Any]) -> None:
    _validate_production_identity(record, root, expected_types=("repair_plan",), expected_version="v1")
    _require_fields(root, ("plan_id", "created_at", "created_from_job_id", "validation_report_id", "policy", "proposals", "issues", "manual_review_actions"), "repair_plan")


def _validate_repair_apply(record: Any, _path: str | Path, root: Mapping[str, Any]) -> None:
    _validate_production_identity(record, root, expected_types=("repair_apply_result",), expected_version="v1")
    _require_fields(root, ("plan_id", "applied_count", "rejected_count"), "repair_apply_result")


def _validate_repair_report(record: Any, _path: str | Path, root: Mapping[str, Any]) -> None:
    _validate_production_identity(record, root, expected_types=("repair_report",), expected_version="v1")
    _require_fields(root, ("report_id", "created_at", "plan_id", "summary", "proposals_detail"), "repair_report")


def _validate_repair_transaction(record: Any, _path: str | Path, root: Mapping[str, Any]) -> None:
    version = str(getattr(record, "artifact_version", "") or "")
    if version not in {"v1", "v3"}:
        raise ArtifactSchemaError("repair_transaction must use artifact_version v1 or v3")
    _validate_production_identity(record, root, expected_types=("repair_transaction",), expected_version=version)
    _require_fields(root, ("transaction_id", "job_id", "status", "policy", "plan_id", "validation_artifact_id", "previous_artifact_ids", "previous_artifact_hashes", "created_at"), "repair_transaction")


def _validate_promotion_transaction(record: Any, _path: str | Path, root: Mapping[str, Any]) -> None:
    _validate_production_identity(record, root, expected_types=("repair_promotion_transaction",), expected_version="v1")
    _require_fields(
        root,
        ("transaction_id", "job_id", "source_transaction_id", "status", "actor", "reason", "canonical_version", "review_draft_artifact_id", "citation_manifest_artifact_id", "review_docx_artifact_id", "audit_artifact_id", "lineage_artifact_id", "canonical_input_hashes", "output_hashes", "created_at", "validation_run_result_artifact_id"),
        "repair_promotion_transaction",
    )
    if str(root.get("status") or "") not in {"prepared", "promoted"}:
        raise ArtifactSchemaError("repair_promotion_transaction.status is invalid")
    if not isinstance(root.get("canonical_input_hashes"), Mapping) or not isinstance(root.get("output_hashes"), Mapping) or not root.get("output_hashes"):
        raise ArtifactSchemaError("repair_promotion_transaction hash maps are invalid")
    if str(root.get("canonical_version") or "") != "runtime-validation" and not str(root.get("audit_artifact_id") or ""):
        raise ArtifactSchemaError("repair_promotion_transaction.audit_artifact_id cannot be empty")
    if str(root.get("canonical_version") or "") != "runtime-validation" and not str(root.get("lineage_artifact_id") or ""):
        raise ArtifactSchemaError("repair_promotion_transaction.lineage_artifact_id cannot be empty")
    if not str(root.get("validation_run_result_artifact_id") or "") and not str(
        root.get("validation_disposition_artifact_id") or ""
    ):
        raise ArtifactSchemaError(
            "repair_promotion_transaction requires validation run result or validation disposition"
        )


def _validate_repair_lineage(record: Any, _path: str | Path, root: Mapping[str, Any]) -> None:
    _validate_production_identity(record, root, expected_types=("repair_lineage",), expected_version="v1")
    _require_fields(root, ("lineage_id", "source_transaction_id", "canonical_inputs", "derived_repair_inputs", "versioned_outputs", "structural_closure", "canonical_replacement"), "repair_lineage")
    if not isinstance(root.get("canonical_inputs"), Mapping) or not isinstance(root.get("versioned_outputs"), Mapping):
        raise ArtifactSchemaError("repair_lineage hash maps are invalid")


def _validate_review_section(record: Any, _path: str | Path, root: Mapping[str, Any]) -> None:
    _validate_production_identity(record, root, expected_types=("review_section",), expected_version="v3")
    _require_fields(root, ("status", "section_id", "binding_hash", "binding", "content_hash", "section"), "review_section")


def _validate_export_bundle(record: Any, path: str | Path, _root: Mapping[str, Any] | None = None) -> None:
    if str(getattr(record, "artifact_version", "") or "") != "v1":
        raise ArtifactSchemaError("export_bundle must use artifact_version v1")
    try:
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
            if not {"provenance_manifest.json", "checksums.json", "EXPORT_STATUS.txt"}.issubset(names):
                raise ArtifactSchemaError("export_bundle is missing provenance files")
            manifest = json.loads(archive.read("provenance_manifest.json").decode("utf-8"))
    except (OSError, UnicodeError, zipfile.BadZipFile, KeyError, json.JSONDecodeError) as exc:
        raise ArtifactSchemaError("export_bundle cannot be read as a verified ZIP") from exc
    if not isinstance(manifest, Mapping):
        raise ArtifactSchemaError("export_bundle provenance manifest must be an object")
    _require_fields(
        manifest,
        (
            "artifact_type",
            "artifact_version",
            "bundle_id",
            "job_id",
            "status",
            "spec",
            "records",
            "completion_manifest",
            "validation_closure",
            "provider_receipt_closure",
            "current_stage_closure_map",
            "requested_stages",
            "spec_hash",
            "issues",
            "validation_status",
            "validation_required",
            "validation_enabled",
            "allow_unvalidated",
            "validation_disposition_artifact_id",
            "validation_disposition_artifact_hash",
            "stage_plan_hash",
            "runtime_spec_hash",
            "validation_warning",
        ),
        "export_bundle.provenance_manifest",
    )
    if str(manifest.get("artifact_type") or "") != "export_bundle" or str(manifest.get("artifact_version") or "") != "v1":
        raise ArtifactSchemaError("export_bundle provenance identity is invalid")
    if str(manifest.get("job_id") or "") != str(getattr(record, "job_id", "") or ""):
        raise ArtifactSchemaError("export_bundle job_id mismatch")
    if (
        not isinstance(manifest.get("records"), list)
        or not isinstance(manifest.get("issues"), list)
        or not isinstance(manifest.get("current_stage_closure_map"), Mapping)
        or not isinstance(manifest.get("requested_stages"), list)
        or not isinstance(manifest.get("spec_hash"), str)
    ):
        raise ArtifactSchemaError("export_bundle records/issues must be arrays")
    status = str(manifest.get("status") or "")
    validation_status = str(manifest.get("validation_status") or "")
    if status not in {"canonical_verified", "canonical_unvalidated", "forensic_untrusted", "manual_repaired", "untrusted"}:
        raise ArtifactSchemaError("export_bundle status is invalid")
    if validation_status not in {"clean", "findings", "not_requested", "unknown"}:
        raise ArtifactSchemaError("export_bundle validation_status is invalid")
    if status == "canonical_unvalidated":
        if (
            validation_status != "not_requested"
            or manifest.get("validation_required") is not False
            or manifest.get("validation_enabled") is not False
            or manifest.get("allow_unvalidated") is not True
            or not str(manifest.get("validation_disposition_artifact_id") or "")
            or len(str(manifest.get("validation_disposition_artifact_hash") or "")) != 64
            or len(str(manifest.get("stage_plan_hash") or "")) != 64
            or len(str(manifest.get("runtime_spec_hash") or "")) != 64
            or "semantic validation was not performed" not in str(manifest.get("validation_warning") or "").lower()
        ):
            raise ArtifactSchemaError("canonical_unvalidated export does not carry its optional-validation attestation")


def _validate_forensic_attestation(record: Any, _path: str | Path, root: Mapping[str, Any]) -> None:
    _validate_production_identity(record, root, expected_types=("forensic_attestation",), expected_version="v1")
    _require_fields(root, ("checked_at", "evidence_hash", "status", "graph", "verified", "manual", "issues", "completion", "closure", "receipt_closure", "adoption"), "forensic_attestation")


def _validate_current_production_artifact(record: Any, path: str | Path, root: Mapping[str, Any] | None) -> None:
    artifact_type = str(getattr(record, "artifact_type", "") or "")
    version = str(getattr(record, "artifact_version", "") or "")
    validators = {
        ("review_draft", "v3"): _validate_review_json,
        ("review_draft_repaired", "v1"): _validate_repaired_json,
        ("citation_manifest", "v3"): _validate_citation_json,
        ("citation_manifest_repaired", "v1"): _validate_repaired_json,
        ("citation_ref_catalog", "v1"): _validate_citation_ref_catalog,
        ("review_replay_ledger", "v1"): lambda r, p, x: _validate_review_replay_ledger(r, p),
        ("review_docx", "v1"): _validate_docx,
        ("review_docx_repaired", "v1"): _validate_docx,
        ("validation_run_result", "v1"): _validate_validation_result,
        ("validation_run_result_repaired", "v1"): _validate_validation_result,
        ("provider_receipt_closure", "v1"): _validate_receipt_closure,
        ("provider_expected_call_graph", "v1"): _validate_provider_expected_call_graph,
        ("stage1_summary_reuse_record", "v1"): _validate_stage1_summary_reuse_record,
        ("stage1_reusable_summary_manifest", "v1"): _validate_stage1_reusable_summary_manifest,
        ("stage1_portable_summary_source", "v1"): _validate_stage1_portable_summary_source,
        ("stage1_portable_summary_manifest", "v1"): _validate_stage1_portable_summary_manifest,
        ("stage1_portable_provider_closure", "v1"): _validate_stage1_portable_provider_closure,
        ("stage1_portable_provider_ledger", "v1"): _validate_stage1_portable_provider_ledger,
        ("outline_provider_call_plan", "v1"): _validate_outline_provider_call_plan,
        ("validation_disposition", "v1"): _validate_validation_disposition,
        ("lease_publication_manifest", "v1"): _validate_lease_publication_manifest,
        ("provider_receipt_ledger", "v1"): _validate_receipt_ledger,
        ("repair_plan", "v1"): _validate_repair_plan,
        ("repair_apply_result", "v1"): _validate_repair_apply,
        ("repair_report", "v1"): _validate_repair_report,
        ("repair_transaction", "v1"): _validate_repair_transaction,
        ("repair_transaction", "v3"): _validate_repair_transaction,
        ("repair_promotion_transaction", "v1"): _validate_promotion_transaction,
        ("repair_lineage", "v1"): _validate_repair_lineage,
        ("current_artifact_set", "v1"): _validate_current_set,
        ("current_artifact_set_pointer", "v1"): _validate_current_set_pointer,
        ("current_artifact_pointer", "v1"): _validate_current_pointer,
        ("outline_adoption_pointer", "v1"): _validate_outline_adoption_pointer,
        ("export_bundle", "v1"): _validate_export_bundle,
        ("forensic_attestation", "v1"): _validate_forensic_attestation,
        ("review_section", "v3"): _validate_review_section,
    }
    validator = validators.get((artifact_type, version))
    if validator is None:
        raise ArtifactSchemaError(
            f"no version-aware current validator is registered for {(artifact_type, version)!r}"
        )
    validator(record, path, root)


def validate_registered_artifact(record: Any, path: str | Path) -> None:
    """Unified ready-artifact validator used by registry and runtime gates."""

    artifact_type = str(getattr(record, "artifact_type", "") or "")
    version = str(getattr(record, "artifact_version", "") or "")
    if artifact_type in OUTLINE_V3_ARTIFACT_TYPES:
        artifact_version = str(getattr(record, "artifact_version", "") or "")
        if artifact_version not in CURRENT_OUTLINE_ARTIFACT_VERSIONS[artifact_type]:
            raise ArtifactSchemaError(
                f"no version-aware current validator is registered for "
                f"{(artifact_type, artifact_version)!r}"
            )
        if artifact_type == "provider_receipt_closure" and artifact_version == "v1":
            root = _read_object(path)
            _validate_current_production_artifact(record, path, root)
            return
        validate_current_outline_artifact(record, path)
        return
    if artifact_type in CURRENT_PRODUCTION_ARTIFACT_TYPES:
        if version in LEGACY_COMPATIBLE_ARTIFACT_VERSIONS.get(artifact_type, frozenset()):
            return
        if artifact_type in {
            "review_docx",
            "review_docx_repaired",
            "export_bundle",
            "provider_receipt_ledger",
            "review_replay_ledger",
            "stage1_portable_summary_source",
            "stage1_portable_provider_ledger",
        }:
            root = None
        else:
            root = _read_object(path)
        _validate_current_production_artifact(record, path, root)
        return


def validate_current_outline_artifact(record: Any, path: str | Path) -> None:
    """Validate one current Outline artifact at every reconciliation gate."""

    artifact_type = str(getattr(record, "artifact_type", "") or "")
    if artifact_type not in OUTLINE_V3_ARTIFACT_TYPES:
        raise ArtifactSchemaError(f"unsupported current Outline artifact type: {artifact_type!r}")
    artifact_version = str(getattr(record, "artifact_version", "") or "")
    if artifact_version not in CURRENT_OUTLINE_ARTIFACT_VERSIONS[artifact_type]:
        raise ArtifactSchemaError(
            f"no version-aware current validator is registered for "
            f"{(artifact_type, artifact_version)!r}"
        )
    root = _read_object(path)
    if artifact_type == "provider_receipt_closure" and artifact_version == "v1":
        _validate_current_production_artifact(record, path, root)
        return
    if artifact_type in _ENVELOPE_TYPES:
        _validate_envelope(record, root)
    elif artifact_type == "stage1_canonical_summaries":
        _require_fields(root, ("artifact_type", "artifact_version", "job_id", "summaries"), artifact_type)
        if str(root.get("artifact_type") or "") != artifact_type or str(root.get("artifact_version") or "") != artifact_version:
            raise ArtifactSchemaError("stage1_canonical_summaries identity mismatch")
        if not isinstance(root.get("summaries"), list) or not root.get("summaries"):
            raise ArtifactSchemaError("stage1_canonical_summaries.summaries must be a non-empty array")
        if str(getattr(record, "job_id", "") or "") != str(root.get("job_id") or ""):
            raise ArtifactSchemaError("stage1_canonical_summaries job_id mismatch")
    elif artifact_type == "outline_v3_node_dag":
        _require_fields(root, ("job_id", "dag_version", "nodes", "content_hash"), artifact_type)
        if "artifact_type" in root and str(root.get("artifact_type") or "") != artifact_type:
            raise ArtifactSchemaError("outline_v3_node_dag artifact_type identity mismatch")
        if "artifact_version" in root and str(root.get("artifact_version") or "") != artifact_version:
            raise ArtifactSchemaError("outline_v3_node_dag identity mismatch")
        if str(root.get("job_id") or "") != str(getattr(record, "job_id", "") or ""):
            raise ArtifactSchemaError("outline_v3_node_dag job_id mismatch")
        if not isinstance(root.get("nodes"), list) or not root.get("nodes"):
            raise ArtifactSchemaError("outline_v3_node_dag.nodes must be a non-empty array")
    elif artifact_type == "outline_provider_call_plan":
        _validate_outline_provider_call_plan(record, path, root)
    else:
        _validate_model(record, root)


def make_outline_schema_validators() -> dict[str, Any]:
    return {artifact_type: validate_current_outline_artifact for artifact_type in OUTLINE_V3_ARTIFACT_TYPES}


__all__ = [
    "ArtifactSchemaError",
    "CURRENT_ARTIFACT_TYPES",
    "CURRENT_PRODUCTION_ARTIFACT_TYPES",
    "CURRENT_OUTLINE_ARTIFACT_VERSIONS",
    "LEGACY_COMPATIBLE_ARTIFACT_VERSIONS",
    "OUTLINE_V3_ARTIFACT_TYPES",
    "make_outline_schema_validators",
    "validate_registered_artifact",
    "validate_current_outline_artifact",
]
