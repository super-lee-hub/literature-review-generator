from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import stat
from typing import Any, Callable, Iterable, Mapping, Sequence
import zipfile

from runtime.stage_contracts import StageArtifactRef, StageResult
from runtime.stage_terminal import (
    STAGE_TERMINAL_ARTIFACT_TYPE,
    STAGE_TERMINAL_ARTIFACT_VERSION,
    STAGE_TERMINAL_ROLE,
    StageTerminalStore,
    TerminalStageRecordV1,
)
from services.artifact_registry import (
    ArtifactDependencyRefV2,
    ArtifactRecord,
    ArtifactRegistry,
    file_sha256,
)
from services.job_outcome import JobOutcomeV1
from runtime.artifact_validators import (
    ArtifactSchemaError,
    OUTLINE_V3_ARTIFACT_TYPES as CURRENT_OUTLINE_V3_ARTIFACT_TYPES,
    validate_current_outline_artifact,
)
from summary_schema import (
    ROUTE_CONFIDENCE_VALUES,
    is_canonical_ai_summary,
    normalize_ai_summary,
)


SchemaValidator = Callable[[ArtifactRecord, Path], None]
ExternalRegistryResolver = Callable[[str], ArtifactRegistry | None]


class ReconcileValidationError(ValueError):
    pass


@dataclass(frozen=True)
class ReconcileIssue:
    code: str
    message: str
    artifact_id: str = ""
    stage_name: str = ""


@dataclass(frozen=True)
class ProvenStageRecovery:
    """Inputs sufficient to reconstruct a missing terminal record without generation."""

    stage_name: str
    attempt_id: str
    output_artifact_refs: tuple[ArtifactDependencyRefV2, ...]
    input_artifact_refs: tuple[ArtifactDependencyRefV2, ...] = ()
    model_call_count: int = 0


@dataclass(frozen=True)
class ReconcileResult:
    job_id: str
    completed_stages: tuple[str, ...]
    repaired_artifact_ids: tuple[str, ...]
    reconstructed_stage_records: tuple[str, ...]
    outcome_repaired: bool
    pointer_repaired: bool
    issues: tuple[ReconcileIssue, ...]

    @property
    def clean(self) -> bool:
        return not self.issues


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReconcileValidationError(f"invalid JSON {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ReconcileValidationError(f"JSON artifact must be an object: {path}")
    return payload


def _read_json_array(path: Path) -> list[Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReconcileValidationError(f"invalid JSON {path}: {exc}") from exc
    if not isinstance(payload, list):
        raise ReconcileValidationError(f"JSON artifact must be an array: {path}")
    return payload


def _valid_fingerprint_bundle(value: Any) -> bool:
    if not isinstance(value, Mapping):
        return False
    fields = ("config_hash", "source_hash", "request_hash", "combined_hash")
    normalized = {field: str(value.get(field) or "") for field in fields}
    if any(
        len(item) != 64 or any(char not in "0123456789abcdef" for char in item)
        for item in normalized.values()
    ):
        return False
    expected_combined = hashlib.sha256(
        json.dumps(
            {
                "config_hash": normalized["config_hash"],
                "source_hash": normalized["source_hash"],
                "request_hash": normalized["request_hash"],
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return normalized["combined_hash"] == expected_combined


def _validate_job_outcome(record: ArtifactRecord, path: Path) -> None:
    outcome = JobOutcomeV1.from_dict(_read_json_object(path))
    if outcome.job_id != record.job_id:
        raise ReconcileValidationError("job outcome job_id does not match its Registry owner")


def _validate_stage_terminal(record: ArtifactRecord, path: Path) -> None:
    terminal = TerminalStageRecordV1.from_dict(_read_json_object(path))
    if terminal.job_id != record.job_id:
        raise ReconcileValidationError("stage terminal job_id does not match its Registry owner")
    if terminal.record_id != record.artifact_id:
        raise ReconcileValidationError("stage terminal record_id does not match its Registry identity")


def _validate_source_bundle(_record: ArtifactRecord, path: Path) -> None:
    payload = _read_json_object(path)
    required = {"source_mode", "project_name", "paper_work_items", "source_snapshot"}
    if not required.issubset(payload):
        raise ReconcileValidationError(f"source bundle is missing fields: {sorted(required - payload.keys())}")
    if not isinstance(payload["paper_work_items"], list) or not isinstance(payload["source_snapshot"], dict):
        raise ReconcileValidationError("source bundle collections have invalid types")


def validate_canonical_ai_summary(payload: Any, *, label: str) -> Mapping[str, Any]:
    if not isinstance(payload, Mapping):
        raise ReconcileValidationError(f"{label} must be a JSON object")
    normalized = normalize_ai_summary(payload)
    payload_dict = dict(payload)
    if (
        not is_canonical_ai_summary(payload)
        or set(payload_dict) != set(normalized)
        or any(
            payload_dict[field] != normalized[field]
            for field in normalized
            if field != "quality_audit"
        )
    ):
        raise ReconcileValidationError(
            f"{label} does not match the canonical summary_schema"
        )
    quality_audit = payload_dict.get("quality_audit")
    normalized_quality = normalized["quality_audit"]
    list_fields = ("missing_critical_fields", "conflict_flags", "inferred_fields")
    if (
        not isinstance(quality_audit, Mapping)
        or set(quality_audit) != set(normalized_quality)
        or quality_audit.get("extraction_confidence") not in ROUTE_CONFIDENCE_VALUES
        or quality_audit.get("extraction_confidence")
        != normalized_quality["extraction_confidence"]
        or isinstance(quality_audit.get("completeness_score"), bool)
        or not isinstance(quality_audit.get("completeness_score"), (int, float))
        or not 0.0 <= float(quality_audit["completeness_score"]) <= 1.0
        or float(quality_audit["completeness_score"])
        != float(normalized_quality["completeness_score"])
        or not isinstance(quality_audit.get("needs_manual_review"), bool)
        or (
            bool(normalized_quality["needs_manual_review"])
            and not bool(quality_audit["needs_manual_review"])
        )
        or any(
            not isinstance(quality_audit.get(field), list)
            or any(not isinstance(item, str) for item in quality_audit[field])
            or not set(normalized_quality[field]).issubset(quality_audit[field])
            for field in list_fields
        )
    ):
        raise ReconcileValidationError(
            f"{label} quality_audit does not match the canonical summary_schema"
        )
    return payload_dict


def _validate_summary_file(_record: ArtifactRecord, path: Path) -> None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReconcileValidationError(f"invalid summary JSON {path}: {exc}") from exc
    if not isinstance(payload, list):
        raise ReconcileValidationError("summary_file must contain a JSON array")
    if not payload:
        raise ReconcileValidationError("summary_file must contain at least one summary")
    paper_keys: set[str] = set()
    for index, item in enumerate(payload):
        if not isinstance(item, dict):
            raise ReconcileValidationError("summary_file entries must be JSON objects")
        if str(item.get("status") or "").strip().lower() != "success":
            raise ReconcileValidationError(f"summary_file entry {index} is not successful")
        paper_info = item.get("paper_info")
        if not isinstance(paper_info, Mapping):
            raise ReconcileValidationError(f"summary_file entry {index} has no paper_info")
        paper_key = _require_nonempty_string(
            paper_info.get("canonical_paper_key"),
            label=f"summary_file entry {index} canonical_paper_key",
        )
        if paper_key in paper_keys:
            raise ReconcileValidationError(f"summary_file has duplicate paper identity: {paper_key}")
        validate_canonical_ai_summary(
            item.get("ai_summary"),
            label=f"summary_file entry {index} ai_summary",
        )
        paper_keys.add(paper_key)


def _validate_summary_source_manifest(record: ArtifactRecord, path: Path) -> None:
    payload = _read_json_object(path)
    _require_contract_header(
        record,
        payload,
        artifact_type="summary_source_manifest",
        versions=("v2",),
    )
    _require_fields(
        payload,
        (
            "created_at",
            "project_name",
            "source_kind",
            "source_path",
            "source_items",
            "rejected_candidates",
            "materialized_summary_file",
            "summary_count",
        ),
        label="summary_source_manifest",
    )
    _require_nonempty_string(payload.get("created_at"), label="summary manifest created_at")
    _require_nonempty_string(payload.get("project_name"), label="summary manifest project_name")
    _require_nonempty_string(payload.get("source_kind"), label="summary manifest source_kind")
    source_items = _require_list(payload.get("source_items"), label="summary manifest source_items")
    rejected = _require_list(
        payload.get("rejected_candidates"), label="summary manifest rejected_candidates"
    )
    if any(not isinstance(item, Mapping) for item in (*source_items, *rejected)):
        raise ReconcileValidationError("summary manifest candidates must be JSON objects")
    summary_path = Path(
        _require_nonempty_string(
            payload.get("materialized_summary_file"),
            label="summary manifest materialized_summary_file",
        )
    ).expanduser()
    if not summary_path.is_absolute():
        summary_path = path.parent / summary_path
    summary_path = summary_path.resolve()
    try:
        summaries = json.loads(summary_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReconcileValidationError(f"summary manifest target is unavailable: {exc}") from exc
    if not isinstance(summaries, list):
        raise ReconcileValidationError("summary manifest target must be a JSON array")
    summary_count = payload.get("summary_count")
    if isinstance(summary_count, bool) or not isinstance(summary_count, int) or summary_count < 0:
        raise ReconcileValidationError("summary manifest summary_count must be a non-negative integer")
    if summary_count != len(summaries):
        raise ReconcileValidationError("summary manifest summary_count is inconsistent")


def _validate_nonempty_text(_record: ArtifactRecord, path: Path) -> None:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ReconcileValidationError(f"invalid UTF-8 text artifact {path}: {exc}") from exc
    if not text.strip():
        raise ReconcileValidationError(f"text artifact is empty: {path}")


def _validate_docx(_record: ArtifactRecord, path: Path) -> None:
    if not zipfile.is_zipfile(path):
        raise ReconcileValidationError(f"DOCX artifact is not a ZIP package: {path}")
    with zipfile.ZipFile(path) as package:
        names = set(package.namelist())
        if "[Content_Types].xml" not in names or "word/document.xml" not in names:
            raise ReconcileValidationError(f"DOCX package is missing required parts: {path}")


def _validate_pdf(_record: ArtifactRecord, path: Path) -> None:
    try:
        if path.read_bytes()[:5] != b"%PDF-":
            raise ReconcileValidationError(f"PDF artifact has no PDF header: {path}")
    except OSError as exc:
        raise ReconcileValidationError(f"cannot read PDF artifact {path}: {exc}") from exc


def _validate_validation_run_result(record: ArtifactRecord, path: Path) -> None:
    from validation.input_dependencies import (
        ValidationInputDependencyError,
        validate_validation_dependency_contract,
    )
    from validation.run_result import ValidationRunResultV1

    result = ValidationRunResultV1.from_dict(_read_json_object(path))
    if result.job_id != record.job_id:
        raise ReconcileValidationError(
            "validation run result job_id does not match its Registry owner"
        )
    if not result.contract_satisfied:
        raise ReconcileValidationError(
            "validation run result does not satisfy the canonical completion contract"
        )
    try:
        validate_validation_dependency_contract(record, result.input_artifacts)
    except ValidationInputDependencyError as exc:
        raise ReconcileValidationError(str(exc)) from exc


def _require_contract_header(
    record: ArtifactRecord,
    payload: Mapping[str, Any],
    *,
    artifact_type: str,
    versions: Iterable[str],
) -> str:
    allowed_versions = tuple(versions)
    payload_type = str(payload.get("artifact_type") or "")
    payload_version = str(payload.get("artifact_version") or "")
    if payload_type != artifact_type or record.artifact_type != artifact_type:
        raise ReconcileValidationError(
            f"artifact type mismatch for {record.artifact_id}: {payload_type!r}"
        )
    if payload_version not in allowed_versions:
        raise ReconcileValidationError(
            f"unsupported {artifact_type} version: {payload_version!r}"
        )
    if record.artifact_version != payload_version:
        raise ReconcileValidationError(
            f"Registry version does not match {artifact_type} payload: {record.artifact_id}"
        )
    return payload_version


def _require_fields(payload: Mapping[str, Any], fields: Iterable[str], *, label: str) -> None:
    missing = [field for field in fields if field not in payload]
    if missing:
        raise ReconcileValidationError(f"{label} is missing fields: {sorted(missing)}")


def _require_owned_job(
    record: ArtifactRecord,
    payload: Mapping[str, Any],
    *,
    field: str,
) -> str:
    job_id = str(payload.get(field) or "")
    if not job_id:
        raise ReconcileValidationError(f"{record.artifact_type} has no {field}")
    if job_id != record.job_id:
        raise ReconcileValidationError(
            f"{record.artifact_type} {field} does not match its Registry owner"
        )
    return job_id


def _require_nonempty_string(value: Any, *, label: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ReconcileValidationError(f"{label} must be a non-empty string")
    return normalized


def _require_mapping(value: Any, *, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ReconcileValidationError(f"{label} must be a JSON object")
    return value


def _require_list(value: Any, *, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise ReconcileValidationError(f"{label} must be a JSON array")
    return value


_OUTLINE_V3_ARTIFACT_TYPES = frozenset(
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


def _validate_outline_v3_artifact(record: ArtifactRecord, path: Path) -> None:
    if record.artifact_version != "v3":
        _validate_provider_receipt_closure(record, path)
        return
    try:
        validate_current_outline_artifact(record, path)
    except ArtifactSchemaError as exc:
        raise ReconcileValidationError(str(exc)) from exc


def _validate_final_outline(record: ArtifactRecord, path: Path) -> None:
    _validate_outline_v3_artifact(record, path)


def _validate_outline_stage_health(record: ArtifactRecord, path: Path) -> None:
    _validate_outline_v3_artifact(record, path)


def _validate_provider_receipt_closure(record: ArtifactRecord, path: Path) -> None:
    payload = _read_json_object(path)
    if not str(payload.get("job_id") or "") or str(payload.get("job_id")) != record.job_id:
        raise ReconcileValidationError("provider receipt closure job_id mismatch")
    closure = payload.get("payload")
    if not isinstance(closure, Mapping):
        raise ReconcileValidationError("provider receipt closure payload must be an object")
    _require_fields(closure, ("expected_call_ids", "observed_call_ids", "complete"), label="provider receipt closure")
    if not isinstance(closure.get("expected_call_ids"), list) or not isinstance(closure.get("observed_call_ids"), list):
        raise ReconcileValidationError("provider receipt closure call ids must be arrays")
    if not isinstance(closure.get("complete"), bool):
        raise ReconcileValidationError("provider receipt closure complete must be boolean")


def _validate_outline_adoption_pointer(record: ArtifactRecord, path: Path) -> None:
    payload = _read_json_object(path)
    _require_fields(
        payload,
        (
            "artifact_type",
            "artifact_version",
            "job_id",
            "role",
            "current_adoption_artifact_id",
            "current_adoption_hash",
            "adoption_identity",
        ),
        label="outline adoption pointer",
    )
    if payload.get("artifact_type") != "outline_adoption_pointer" or payload.get("artifact_version") != "v1":
        raise ReconcileValidationError("outline adoption pointer contract header is invalid")
    _require_owned_job(record, payload, field="job_id")
    if payload.get("role") != "current":
        raise ReconcileValidationError("outline adoption pointer role must be current")
    target_id = _require_nonempty_string(
        payload.get("current_adoption_artifact_id"),
        label="outline adoption pointer target",
    )
    target_hash = _require_nonempty_string(
        payload.get("current_adoption_hash"),
        label="outline adoption pointer target hash",
    )
    if not target_id.startswith("outline-v3:adoption:"):
        raise ReconcileValidationError("outline adoption pointer target must be versioned")
    if len(target_hash) != 64 or any(char not in "0123456789abcdef" for char in target_hash.lower()):
        raise ReconcileValidationError("outline adoption pointer target hash must be SHA-256")
    if payload.get("adoption_identity") != target_id:
        raise ReconcileValidationError("outline adoption pointer identity does not match target")


def _validate_evidence_manifest(record: ArtifactRecord, path: Path) -> None:
    from services.evidence_manifest import EvidenceManifestV1, verified_evidence_paths

    payload = _read_json_object(path)
    _require_contract_header(
        record,
        payload,
        artifact_type="evidence_manifest",
        versions=("v1",),
    )
    manifest = EvidenceManifestV1.from_dict(payload)
    _require_owned_job(record, payload, field="job_id")
    _require_nonempty_string(manifest.canonical_paper_key, label="canonical_paper_key")
    _require_nonempty_string(manifest.created_at, label="evidence_manifest created_at")
    for item in manifest.artifacts:
        _require_nonempty_string(item.path, label=f"{item.artifact_type} evidence path")
        _require_nonempty_string(item.content_hash, label=f"{item.artifact_type} evidence hash")
    verified_evidence_paths(manifest)


def _validate_paper_artifact(record: ArtifactRecord, path: Path) -> None:
    from services.paper_artifact import PaperArtifactV1

    payload = _read_json_object(path)
    _require_contract_header(
        record,
        payload,
        artifact_type="paper_artifact",
        versions=("v1",),
    )
    _require_fields(
        payload,
        (
            "created_from_job_id",
            "created_at",
            "paper_identity",
            "source",
            "paper_info",
            "analysis",
            "stage1_inputs",
        ),
        label="paper_artifact",
    )
    _require_owned_job(record, payload, field="created_from_job_id")
    artifact = PaperArtifactV1(
        artifact_type=str(payload["artifact_type"]),
        artifact_version=str(payload["artifact_version"]),
        created_from_job_id=str(payload["created_from_job_id"]),
        created_at=str(payload["created_at"]),
        paper_identity=dict(_require_mapping(payload["paper_identity"], label="paper_identity")),
        source=dict(_require_mapping(payload["source"], label="paper source")),
        paper_info=dict(_require_mapping(payload["paper_info"], label="paper_info")),
        analysis=dict(_require_mapping(payload["analysis"], label="paper analysis")),
        stage1_inputs=dict(_require_mapping(payload["stage1_inputs"], label="stage1_inputs")),
    )
    _require_nonempty_string(artifact.created_at, label="paper_artifact created_at")
    _require_nonempty_string(
        artifact.paper_identity.get("source_paper_id"), label="paper source_paper_id"
    )
    _require_nonempty_string(
        artifact.paper_identity.get("canonical_paper_key"), label="paper canonical_paper_key"
    )
    analysis_status = _require_nonempty_string(
        artifact.analysis.get("status"), label="paper analysis status"
    )
    if analysis_status.casefold() == "success":
        validate_canonical_ai_summary(
            artifact.analysis.get("ai_summary"),
            label="paper artifact ai_summary",
        )


def _validate_review_draft(record: ArtifactRecord, path: Path) -> None:
    from services.review_draft import ReviewDraft

    payload = _read_json_object(path)
    _require_contract_header(
        record,
        payload,
        artifact_type="review_draft",
        versions=("v3",),
    )
    _require_fields(
        payload,
        (
            "created_from_job_id",
            "created_at",
            "draft_identity",
            "generation_context",
            "content",
            "projections",
        ),
        label="review_draft",
    )
    _require_owned_job(record, payload, field="created_from_job_id")
    draft = ReviewDraft(
        artifact_type=str(payload["artifact_type"]),
        artifact_version=str(payload["artifact_version"]),
        created_from_job_id=str(payload["created_from_job_id"]),
        created_at=str(payload["created_at"]),
        draft_identity=dict(_require_mapping(payload["draft_identity"], label="draft_identity")),
        generation_context=dict(
            _require_mapping(payload["generation_context"], label="review generation_context")
        ),
        content=dict(_require_mapping(payload["content"], label="review content")),
        projections=dict(_require_mapping(payload["projections"], label="review projections")),
    )
    _require_nonempty_string(draft.created_at, label="review_draft created_at")
    _require_nonempty_string(draft.draft_identity.get("draft_id"), label="review draft_id")
    _require_nonempty_string(draft.draft_identity.get("project_name"), label="review project_name")
    sections = _require_list(draft.content.get("sections"), label="review sections")
    _require_list(draft.content.get("references"), label="review references")
    if not sections:
        raise ReconcileValidationError("review_draft has no sections")
    expected_count = draft.generation_context.get("section_count")
    if expected_count is not None and int(expected_count) != len(sections):
        raise ReconcileValidationError("review_draft section_count projection is inconsistent")
    for section in sections:
        section_data = _require_mapping(section, label="review section")
        if int(section_data.get("section_number") or 0) <= 0:
            raise ReconcileValidationError("review section_number must be positive")
        _require_nonempty_string(section_data.get("section_title"), label="review section title")
        blocks = _require_list(section_data.get("blocks"), label="review section blocks")
        if not blocks:
            raise ReconcileValidationError("review_draft section has no blocks")
        for block in blocks:
            block_data = _require_mapping(block, label="review block")
            _require_nonempty_string(block_data.get("block_id"), label="review block_id")
            _require_nonempty_string(block_data.get("text"), label="review block text")


def _validate_citation_manifest(record: ArtifactRecord, path: Path) -> None:
    from services.citation_manifest import CitationManifestV3

    payload = _read_json_object(path)
    _require_contract_header(
        record,
        payload,
        artifact_type="citation_manifest",
        versions=("v3",),
    )
    common_fields = (
        "created_from_job_id",
        "created_at",
        "manifest_identity",
        "review_reference",
    )
    _require_fields(payload, common_fields, label="citation_manifest")
    _require_owned_job(record, payload, field="created_from_job_id")
    _require_nonempty_string(payload.get("created_at"), label="citation_manifest created_at")
    identity = _require_mapping(payload.get("manifest_identity"), label="manifest_identity")
    _require_nonempty_string(identity.get("manifest_id"), label="citation manifest_id")
    _require_nonempty_string(identity.get("project_name"), label="citation project_name")
    review_reference = _require_mapping(
        payload.get("review_reference"), label="citation review_reference"
    )
    _require_nonempty_string(
        review_reference.get("review_draft_path"), label="citation review_draft_path"
    )
    _require_nonempty_string(
        review_reference.get("review_word_path"), label="citation review_word_path"
    )
    manifest = CitationManifestV3.from_dict(dict(payload))
    for field_name in (
        "paper_entries",
        "occurrences",
        "clusters",
        "citation_sets",
        "bibliography",
    ):
        _require_list(payload.get(field_name), label=f"citation_manifest {field_name}")
    if manifest.review_draft_version != "v3":
        raise ReconcileValidationError("citation_manifest v3 requires review_draft v3")
    _require_mapping(payload.get("dependencies"), label="citation dependencies")


def _validate_citation_ref_catalog(record: ArtifactRecord, path: Path) -> None:
    from services.citation_ref_catalog import validate_document_ref_catalog

    payload = _read_json_object(path)
    _require_contract_header(
        record,
        payload,
        artifact_type="citation_ref_catalog",
        versions=("v1",),
    )
    _require_owned_job(record, payload, field="created_from_job_id")
    validate_document_ref_catalog(payload)


def _validate_json_object(_record: ArtifactRecord, path: Path) -> None:
    _read_json_object(path)


def _validate_provider_receipt_ledger(record: ArtifactRecord, path: Path) -> None:
    from runtime.provider_runtime import ProviderRuntimeContractError, ProviderRuntimeLedger

    try:
        receipts = ProviderRuntimeLedger(path).list_receipts()
    except ProviderRuntimeContractError as exc:
        raise ReconcileValidationError(f"provider receipt ledger is invalid: {exc}") from exc
    if not receipts:
        raise ReconcileValidationError("provider receipt ledger must contain at least one receipt")
    for receipt in receipts:
        if receipt.job_id != record.job_id:
            raise ReconcileValidationError(
                "provider receipt job_id does not match its Registry owner"
            )
        if not receipt.receipt_id or not receipt.stage_name or not receipt.route:
            raise ReconcileValidationError("provider receipt is missing its durable routing identity")


def _validate_json_array(_record: ArtifactRecord, path: Path) -> None:
    _read_json_array(path)


def _validate_audit_record(record: ArtifactRecord, path: Path) -> None:
    from services.audit_record import AuditRecordV1

    payload = _read_json_object(path)
    if not str(payload.get("record_hash") or "").strip():
        raise ReconcileValidationError("audit_record is missing its immutable record_hash")
    audit = AuditRecordV1.from_dict(payload)
    if record.artifact_version != "v1":
        raise ReconcileValidationError("audit_record Registry version must be v1")
    if audit.job_id != record.job_id:
        raise ReconcileValidationError("audit_record job_id does not match its Registry owner")
    if audit.audit_id != record.artifact_id:
        raise ReconcileValidationError("audit_record audit_id does not match its Registry identity")
    metadata_hash = str(record.metadata.get("record_hash") or "").strip()
    if metadata_hash and metadata_hash != audit.record_hash:
        raise ReconcileValidationError("audit_record metadata hash does not match immutable content")

    if audit.audit_type != "dependency_force_delete":
        audit_ref_identities = {
            (
                ref.job_id or audit.job_id,
                ref.artifact_id,
                ref.artifact_type,
                ref.content_hash,
            )
            for ref in (
                *audit.target_artifacts,
                *audit.input_artifact_refs,
                *audit.output_artifact_refs,
            )
        }
        dependency_identities = {
            (
                dependency.job_id or record.job_id,
                dependency.artifact_id,
                dependency.artifact_type,
                dependency.content_hash,
            )
            for dependency in record.depends_on
        }
        detached_audit_types = {
            "identity_override",
            "artifact_quarantine_release",
        }
        dependencies_match = audit_ref_identities == dependency_identities
        detached_audit = not record.depends_on and audit.audit_type in detached_audit_types
        if not dependencies_match and not detached_audit:
            raise ReconcileValidationError(
                "audit_record Registry dependencies do not match its live artifact references"
            )


def _validate_summary_selection(record: ArtifactRecord, path: Path) -> None:
    from services.review_batch import SELECTION_SCHEMA_VERSION, SummarySelectionSpecV1

    payload = _read_json_object(path)
    _require_contract_header(
        record,
        payload,
        artifact_type="summary_selection",
        versions=("v1",),
    )
    _require_fields(
        payload,
        (
            "schema_version",
            "project_name",
            "child_job_id",
            "created_at",
            "selection",
            "selected_paper_keys",
            "selected_count",
            "stage1_model_calls",
        ),
        label="summary_selection",
    )
    if str(payload.get("schema_version") or "") != SELECTION_SCHEMA_VERSION:
        raise ReconcileValidationError("summary_selection schema_version is invalid")
    _require_owned_job(record, payload, field="child_job_id")
    _require_nonempty_string(payload.get("project_name"), label="summary_selection project_name")
    _require_nonempty_string(payload.get("created_at"), label="summary_selection created_at")
    selection_payload = _require_mapping(payload.get("selection"), label="summary selection spec")
    selection = SummarySelectionSpecV1.from_dict(selection_payload, origin_dir=path.parent)
    selected_keys = tuple(
        _require_nonempty_string(item, label="selected canonical paper key")
        for item in _require_list(payload.get("selected_paper_keys"), label="selected_paper_keys")
    )
    if not selected_keys:
        raise ReconcileValidationError("summary_selection has no selected papers")
    selected_count = payload.get("selected_count")
    if isinstance(selected_count, bool) or not isinstance(selected_count, int):
        raise ReconcileValidationError("summary_selection selected_count must be an integer")
    if selected_count != len(selected_keys):
        raise ReconcileValidationError("summary_selection selected_count is inconsistent")
    if len(selected_keys) != selection.expected_count:
        raise ReconcileValidationError("summary_selection does not satisfy expected_count")
    if len(set(selected_keys)) != len(selected_keys):
        raise ReconcileValidationError("summary_selection contains duplicate paper keys")
    if selection.ordered_paper_keys and selected_keys != selection.ordered_paper_keys:
        raise ReconcileValidationError("summary_selection paper order differs from its selection spec")
    stage1_model_calls = payload.get("stage1_model_calls")
    if isinstance(stage1_model_calls, bool) or not isinstance(stage1_model_calls, int):
        raise ReconcileValidationError("summary_selection stage1_model_calls must be an integer")
    if stage1_model_calls != 0:
        raise ReconcileValidationError("summary_selection must not contain Stage 1 provider calls")
    metadata_hash = str(record.metadata.get("selection_hash") or "")
    if metadata_hash and metadata_hash != selection.selection_hash:
        raise ReconcileValidationError("summary_selection metadata hash is inconsistent")


def validate_review_batch_manifest_location(
    record: ArtifactRecord,
    path: Path,
    registry: ArtifactRegistry,
) -> Mapping[str, Any]:
    """Bind a batch manifest to its real Registry workspace before trusting links."""

    if record.job_id != registry.job_id:
        raise ReconcileValidationError(
            "review_batch_manifest does not belong to the active Registry owner"
        )
    registry_path = Path(os.path.abspath(os.path.expanduser(registry.registry_path)))
    if registry_path.name != "artifact_registry.json":
        raise ReconcileValidationError(
            "review_batch_manifest active Registry is outside a canonical workspace"
        )
    coordinator_workspace = registry_path.parent
    artifacts_dir = coordinator_workspace / "artifacts"
    lexical_path = Path(os.path.abspath(os.path.expanduser(str(path))))
    try:
        lexical_path.relative_to(coordinator_workspace)
    except ValueError as exc:
        raise ReconcileValidationError(
            "review_batch_manifest is outside the active Registry workspace"
        ) from exc

    relative_parts = lexical_path.relative_to(coordinator_workspace).parts
    current = coordinator_workspace
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    for part in ("", *relative_parts):
        if part:
            current = current / part
        try:
            info = os.lstat(current)
        except OSError as exc:
            raise ReconcileValidationError(
                "review_batch_manifest path cannot be inspected"
            ) from exc
        if stat.S_ISLNK(info.st_mode) or (
            reparse_flag
            and int(getattr(info, "st_file_attributes", 0)) & reparse_flag
        ):
            raise ReconcileValidationError(
                "review_batch_manifest path contains a symlink or reparse point"
            )

    physical_workspace = coordinator_workspace.resolve()
    physical_path = lexical_path.resolve()
    try:
        physical_path.relative_to(physical_workspace)
    except ValueError as exc:
        raise ReconcileValidationError(
            "review_batch_manifest resolves outside the active Registry workspace"
        ) from exc

    payload = _read_json_object(lexical_path)
    coordinator_job_id = _require_nonempty_string(
        payload.get("coordinator_job_id"),
        label="review_batch_manifest coordinator_job_id",
    )
    if coordinator_job_id != registry.job_id:
        raise ReconcileValidationError(
            "review_batch_manifest coordinator does not match the active Registry"
        )
    batch_id = _require_nonempty_string(
        payload.get("batch_id"),
        label="review_batch_manifest batch_id",
    )
    derivation_id = str(payload.get("derivation_id") or "").strip()
    if derivation_id:
        if len(derivation_id) != 24 or any(
            char not in "0123456789abcdef" for char in derivation_id
        ):
            raise ReconcileValidationError(
                "review_batch_manifest derivation_id must be a 24-character hex digest"
            )
        if record.artifact_id != f"{batch_id}:{derivation_id}":
            raise ReconcileValidationError(
                "review_batch_manifest identity does not match its Registry identity"
            )
        projection_path = artifacts_dir / "review_batch_manifest.json"
        if os.path.normcase(str(lexical_path)) == os.path.normcase(str(projection_path)):
            raise ReconcileValidationError(
                "review_batch_manifest derivation must use its immutable derivation path"
            )
        expected_path = (
            artifacts_dir / "review_batch_manifests" / f"{derivation_id}.json"
        )
    else:
        if record.artifact_id != batch_id:
            raise ReconcileValidationError(
                "review_batch_manifest identity does not match its Registry identity"
            )
        expected_path = artifacts_dir / "review_batch_manifest.json"
    if os.path.normcase(str(lexical_path)) != os.path.normcase(str(expected_path)):
        raise ReconcileValidationError(
            "review_batch_manifest is outside the active Registry workspace"
        )
    return payload


def _absolute_lexical_path(path: str | Path) -> Path:
    return Path(os.path.abspath(os.path.expanduser(str(path))))


def _validate_child_owned_path(
    child_workspace: Path,
    target: Path,
    *,
    label: str,
) -> Path:
    child_lexical = _absolute_lexical_path(child_workspace)
    target_lexical = _absolute_lexical_path(target)
    try:
        relative = target_lexical.relative_to(child_lexical)
    except ValueError as exc:
        raise ReconcileValidationError(f"{label} is outside its child workspace") from exc
    current = child_lexical
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    for part in ("", *relative.parts):
        if part:
            current = current / part
        try:
            info = os.lstat(current)
        except OSError as exc:
            raise ReconcileValidationError(f"{label} cannot be inspected") from exc
        if stat.S_ISLNK(info.st_mode) or (
            reparse_flag
            and int(getattr(info, "st_file_attributes", 0)) & reparse_flag
        ):
            raise ReconcileValidationError(
                f"{label} contains a symlink or reparse point"
            )
    physical_child = child_lexical.resolve()
    physical_target = target_lexical.resolve()
    try:
        physical_target.relative_to(physical_child)
    except ValueError as exc:
        raise ReconcileValidationError(
            f"{label} resolves outside its child workspace"
        ) from exc
    return target_lexical


def _validate_review_batch_manifest(record: ArtifactRecord, path: Path) -> None:
    from services.review_batch import (
        BATCH_SCHEMA_VERSION,
        PROJECTION_GENERATION_SCHEMA_VERSION,
    )

    payload = _read_json_object(path)
    _require_contract_header(
        record,
        payload,
        artifact_type="review_batch_manifest",
        versions=("v1",),
    )
    _require_fields(
        payload,
        (
            "schema_version",
            "batch_id",
            "projection_generation",
            "project_name",
            "coordinator_job_id",
            "created_at",
            "parent",
            "variants",
            "completed_variant_count",
            "failed_variant_count",
            "stage1_model_calls",
            "status",
        ),
        label="review_batch_manifest",
    )
    if str(payload.get("schema_version") or "") != BATCH_SCHEMA_VERSION:
        raise ReconcileValidationError("review_batch_manifest schema_version is invalid")
    _require_owned_job(record, payload, field="coordinator_job_id")
    if path.parent.name == "review_batch_manifests":
        coordinator_artifacts_dir = path.parent.parent
    elif path.parent.name == "artifacts":
        coordinator_artifacts_dir = path.parent
    else:
        raise ReconcileValidationError(
            "review_batch_manifest is outside the coordinator artifacts directory"
        )
    coordinator_workspace_lexical = _absolute_lexical_path(
        coordinator_artifacts_dir.parent
    )
    coordinator_workspace_path = coordinator_workspace_lexical.resolve()
    if (
        coordinator_artifacts_dir.name != "artifacts"
        or "__" not in coordinator_workspace_path.name
        or coordinator_workspace_path.name.rsplit("__", 1)[1] != record.job_id
    ):
        raise ReconcileValidationError(
            "review_batch_manifest path does not match its coordinator workspace"
        )
    coordinator_output_root = coordinator_workspace_path.parent
    coordinator_output_root_lexical = coordinator_workspace_lexical.parent
    batch_id = _require_nonempty_string(
        payload.get("batch_id"),
        label="review_batch_manifest batch_id",
    )
    derivation_id = str(payload.get("derivation_id") or "").strip()
    if derivation_id and (
        len(derivation_id) != 24
        or any(char not in "0123456789abcdef" for char in derivation_id)
    ):
        raise ReconcileValidationError(
            "review_batch_manifest derivation_id must be a 24-character hex digest"
        )
    projection_generation = payload.get("projection_generation")
    if (
        isinstance(projection_generation, bool)
        or not isinstance(projection_generation, int)
        or projection_generation <= 0
    ):
        raise ReconcileValidationError(
            "review_batch_manifest projection_generation must be a positive integer"
        )
    expected_artifact_id = (
        f"{batch_id}:{derivation_id}" if derivation_id else batch_id
    )
    if expected_artifact_id != record.artifact_id:
        raise ReconcileValidationError(
            "review_batch_manifest identity does not match its Registry identity"
        )
    _require_nonempty_string(
        payload.get("project_name"),
        label="review_batch_manifest project_name",
    )
    _require_nonempty_string(
        payload.get("created_at"),
        label="review_batch_manifest created_at",
    )
    parent = _require_mapping(payload.get("parent"), label="review_batch_manifest parent")
    parent_registry_path = Path(
        _require_nonempty_string(
            parent.get("registry_path"),
            label="review batch parent registry_path",
        )
    ).resolve()
    parent_identity = (
        _require_nonempty_string(parent.get("job_id"), label="review batch parent job_id"),
        _require_nonempty_string(
            parent.get("artifact_id"), label="review batch parent artifact_id"
        ),
        "summary_file",
        _require_nonempty_string(
            parent.get("summary_path"), label="review batch parent summary_path"
        ),
        _require_nonempty_string(
            parent.get("content_hash"), label="review batch parent content_hash"
        ),
    )
    if len(parent_identity[4]) != 64:
        raise ReconcileValidationError(
            "review_batch_manifest parent content_hash must be a SHA-256 digest"
        )
    if not parent_registry_path.is_file():
        raise ReconcileValidationError("review_batch_manifest parent Registry is missing")
    try:
        parent_registry = ArtifactRegistry(parent_registry_path, parent_identity[0])
    except Exception as exc:
        raise ReconcileValidationError(
            f"review_batch_manifest parent Registry is invalid: {exc}"
        ) from exc
    parent_record = parent_registry.get(parent_identity[1])
    if (
        parent_record is None
        or parent_record.status != "ready"
        or parent_record.artifact_type != parent_identity[2]
        or Path(parent_record.path).resolve() != Path(parent_identity[3]).resolve()
        or parent_record.content_hash != parent_identity[4]
    ):
        raise ReconcileValidationError(
            "review_batch_manifest parent Registry identity is invalid"
        )
    if (
        not Path(parent_identity[3]).is_file()
        or file_sha256(parent_identity[3]) != parent_identity[4]
    ):
        raise ReconcileValidationError(
            "review_batch_manifest parent content hash changed"
        )

    variants = _require_list(payload.get("variants"), label="review_batch_manifest variants")
    if not variants:
        raise ReconcileValidationError("review_batch_manifest has no variants")
    completed_count = payload.get("completed_variant_count")
    failed_count = payload.get("failed_variant_count")
    for label, value in (
        ("completed_variant_count", completed_count),
        ("failed_variant_count", failed_count),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ReconcileValidationError(
                f"review_batch_manifest {label} must be a non-negative integer"
            )
    if payload.get("stage1_model_calls") != 0:
        raise ReconcileValidationError(
            "review_batch_manifest must not contain Stage 1 provider calls"
        )

    variant_ids: set[str] = set()
    project_names: set[str] = set()
    child_job_ids: set[str] = set()
    completed_seen = 0
    failed_seen = 0
    output_identities: set[tuple[str, str, str, str, str]] = set()
    for index, raw_variant in enumerate(variants):
        variant = _require_mapping(raw_variant, label=f"review batch variant[{index}]")
        variant_id = _require_nonempty_string(
            variant.get("variant_id"), label=f"review batch variant[{index}] variant_id"
        )
        project_name = _require_nonempty_string(
            variant.get("project_name"),
            label=f"review batch variant[{index}] project_name",
        )
        child_job_id = _require_nonempty_string(
            variant.get("child_job_id"),
            label=f"review batch variant[{index}] child_job_id",
        )
        child_workspace_path = _absolute_lexical_path(
            _require_nonempty_string(
                variant.get("child_workspace_path"),
                label=f"review batch variant[{index}] child_workspace_path",
            )
        )
        child_registry_path = _absolute_lexical_path(
            _require_nonempty_string(
                variant.get("child_registry_path"),
                label=f"review batch variant[{index}] child_registry_path",
            )
        )
        expected_registry_path = child_workspace_path / "artifact_registry.json"
        if os.path.normcase(str(child_registry_path)) != os.path.normcase(
            str(expected_registry_path)
        ):
            raise ReconcileValidationError(
                f"review batch variant[{index}] Registry is outside its child workspace"
            )
        if os.path.normcase(str(child_workspace_path.parent)) != os.path.normcase(
            str(coordinator_output_root_lexical)
        ):
            raise ReconcileValidationError(
                f"review batch variant[{index}] workspace is outside the coordinator output root"
            )
        _validate_child_owned_path(
            child_workspace_path,
            child_workspace_path,
            label=f"review batch variant[{index}] workspace",
        )
        if child_workspace_path.resolve().parent != coordinator_output_root:
            raise ReconcileValidationError(
                f"review batch variant[{index}] workspace resolves outside the coordinator output root"
            )
        child_artifacts_path = _validate_child_owned_path(
            child_workspace_path,
            child_workspace_path / "artifacts",
            label=f"review batch variant[{index}] artifacts directory",
        )
        _validate_child_owned_path(
            child_workspace_path,
            child_registry_path,
            label=f"review batch variant[{index}] Registry",
        )
        expected_child_workspace_name = f"{project_name}__{child_job_id}"
        if child_workspace_path.name != expected_child_workspace_name:
            raise ReconcileValidationError(
                f"review batch variant[{index}] workspace identity is inconsistent"
            )
        selection_hash = _require_nonempty_string(
            variant.get("selection_hash"),
            label=f"review batch variant[{index}] selection_hash",
        )
        if len(selection_hash) != 64:
            raise ReconcileValidationError(
                f"review batch variant[{index}] selection_hash must be a SHA-256 digest"
            )
        if variant_id in variant_ids or project_name in project_names or child_job_id in child_job_ids:
            raise ReconcileValidationError(
                "review_batch_manifest variant identities must be unique"
            )
        variant_ids.add(variant_id)
        project_names.add(project_name)
        child_job_ids.add(child_job_id)
        if variant.get("stage1_model_calls") != 0:
            raise ReconcileValidationError(
                f"review batch variant[{index}] contains Stage 1 provider calls"
            )
        selected_count = variant.get("selected_count")
        if isinstance(selected_count, bool) or not isinstance(selected_count, int) or selected_count < 0:
            raise ReconcileValidationError(
                f"review batch variant[{index}] selected_count must be a non-negative integer"
            )

        status = str(variant.get("status") or "")
        outputs = _require_mapping(
            variant.get("output_artifacts"),
            label=f"review batch variant[{index}] output_artifacts",
        )
        failure_reason = str(variant.get("failure_reason") or "")
        if status == "failed":
            failed_seen += 1
            if outputs or not failure_reason or selected_count != 0:
                raise ReconcileValidationError(
                    f"review batch failed variant[{index}] has inconsistent outputs"
                )
            continue
        if status != "completed":
            raise ReconcileValidationError(
                f"review batch variant[{index}] has unsupported status {status!r}"
            )
        completed_seen += 1
        if failure_reason or selected_count <= 0:
            raise ReconcileValidationError(
                f"review batch completed variant[{index}] has inconsistent status fields"
            )
        if set(outputs) != {"summary", "selection"}:
            raise ReconcileValidationError(
                f"review batch completed variant[{index}] must link summary and selection outputs"
            )
        if not child_registry_path.is_file():
            raise ReconcileValidationError(
                f"review batch completed variant[{index}] Registry is missing"
            )
        try:
            child_registry = ArtifactRegistry(child_registry_path, child_job_id)
        except Exception as exc:
            raise ReconcileValidationError(
                f"review batch completed variant[{index}] Registry is invalid: {exc}"
            ) from exc
        for output_name, expected_type in (
            ("summary", "summary_file"),
            ("selection", "summary_selection"),
        ):
            link = _require_mapping(
                outputs.get(output_name),
                label=f"review batch variant[{index}] {output_name} link",
            )
            artifact_id = _require_nonempty_string(
                link.get("artifact_id"),
                label=f"review batch variant[{index}] {output_name} artifact_id",
            )
            artifact_type = _require_nonempty_string(
                link.get("artifact_type"),
                label=f"review batch variant[{index}] {output_name} artifact_type",
            )
            artifact_path = _absolute_lexical_path(_require_nonempty_string(
                link.get("path"),
                label=f"review batch variant[{index}] {output_name} path",
            ))
            content_hash = _require_nonempty_string(
                link.get("content_hash"),
                label=f"review batch variant[{index}] {output_name} content_hash",
            )
            if artifact_type != expected_type or len(content_hash) != 64:
                raise ReconcileValidationError(
                    f"review batch variant[{index}] {output_name} identity is invalid"
                )
            expected_output_path = (
                child_artifacts_path / f"{project_name}_summaries.json"
                if output_name == "summary"
                else child_artifacts_path / "summary_selection_v1.json"
            )
            if os.path.normcase(str(artifact_path)) != os.path.normcase(
                str(expected_output_path)
            ):
                raise ReconcileValidationError(
                    f"review batch variant[{index}] {output_name} path is not canonical"
                )
            _validate_child_owned_path(
                child_workspace_path,
                artifact_path,
                label=f"review batch variant[{index}] {output_name}",
            )
            child_record = child_registry.get(artifact_id)
            if (
                child_record is None
                or child_record.status != "ready"
                or child_record.artifact_type != artifact_type
                or os.path.normcase(str(_absolute_lexical_path(child_record.path)))
                != os.path.normcase(str(artifact_path))
                or child_record.content_hash != content_hash
            ):
                raise ReconcileValidationError(
                    f"review batch variant[{index}] {output_name} Registry identity is invalid"
                )
            if not artifact_path.is_file() or file_sha256(artifact_path) != content_hash:
                raise ReconcileValidationError(
                    f"review batch variant[{index}] {output_name} content hash changed"
                )
            if str(child_record.metadata.get("selection_hash") or "") != selection_hash:
                raise ReconcileValidationError(
                    f"review batch variant[{index}] {output_name} selection hash is inconsistent"
                )
            if child_record.metadata.get("selected_count") != selected_count:
                raise ReconcileValidationError(
                    f"review batch variant[{index}] {output_name} selected count is inconsistent"
                )
            if output_name == "summary":
                summary_payload = _read_json_array(Path(artifact_path))
                if len(summary_payload) != selected_count:
                    raise ReconcileValidationError(
                        f"review batch variant[{index}] summary count is inconsistent"
                    )
            else:
                selection_payload = _read_json_object(Path(artifact_path))
                if selection_payload.get("selected_count") != selected_count:
                    raise ReconcileValidationError(
                        f"review batch variant[{index}] selection count is inconsistent"
                    )
            output_identities.add(
                (child_job_id, artifact_id, artifact_type, str(artifact_path), content_hash)
            )

        paper_root = child_artifacts_path / "paper_artifacts"
        _validate_child_owned_path(
            child_workspace_path,
            paper_root,
            label=f"review batch variant[{index}] paper artifacts directory",
        )
        paper_prefix = f"derived-paper:{selection_hash}:"
        paper_records = tuple(
            child_record
            for child_record in child_registry.list_records()
            if child_record.artifact_id.startswith(paper_prefix)
        )
        if len(paper_records) != selected_count:
            raise ReconcileValidationError(
                f"review batch variant[{index}] paper artifact count is inconsistent"
            )
        for paper_record in paper_records:
            paper_path = _absolute_lexical_path(paper_record.path)
            if (
                paper_record.status != "ready"
                or paper_record.artifact_type != "paper_artifact"
                or os.path.normcase(str(paper_path.parent))
                != os.path.normcase(str(paper_root))
            ):
                raise ReconcileValidationError(
                    f"review batch variant[{index}] paper artifact identity is invalid"
                )
            _validate_child_owned_path(
                child_workspace_path,
                paper_path,
                label=f"review batch variant[{index}] paper artifact",
            )
            if (
                not paper_path.is_file()
                or file_sha256(paper_path) != paper_record.content_hash
            ):
                raise ReconcileValidationError(
                    f"review batch variant[{index}] paper artifact content hash changed"
                )

    if completed_count != completed_seen or failed_count != failed_seen:
        raise ReconcileValidationError("review_batch_manifest variant counts are inconsistent")
    expected_status = "completed" if failed_seen == 0 else "needs_review"
    if str(payload.get("status") or "") != expected_status:
        raise ReconcileValidationError("review_batch_manifest aggregate status is inconsistent")
    metadata_checks = {
        "batch_id": batch_id,
        "projection_generation": projection_generation,
        "variant_count": len(variants),
        "completed_variant_count": completed_seen,
        "failed_variant_count": failed_seen,
        "stage1_model_calls": 0,
    }
    if derivation_id:
        metadata_checks["derivation_id"] = derivation_id
    if any(record.metadata.get(key) != value for key, value in metadata_checks.items()):
        raise ReconcileValidationError("review_batch_manifest Registry metadata is inconsistent")

    if any(
        dependency.dependency_kind != "external_job"
        for dependency in record.depends_on
    ):
        raise ReconcileValidationError(
            "review_batch_manifest may contain only external job dependencies"
        )
    dependency_identities = {
        (
            dependency.job_id,
            dependency.artifact_id,
            dependency.artifact_type,
            dependency.path,
            dependency.content_hash,
        )
        for dependency in record.depends_on
    }
    if dependency_identities != {parent_identity, *output_identities}:
        raise ReconcileValidationError(
            "review_batch_manifest Registry dependencies do not match its linked outputs"
        )
    if derivation_id:
        reservation_path = (
            coordinator_artifacts_dir
            / "review_batch_manifests"
            / f".{derivation_id}.projection.generation"
        )
        try:
            reservation_info = os.lstat(reservation_path)
        except OSError as exc:
            raise ReconcileValidationError(
                "review_batch_manifest projection generation reservation is missing"
            ) from exc
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        if stat.S_ISLNK(reservation_info.st_mode) or (
            reparse_flag
            and int(getattr(reservation_info, "st_file_attributes", 0)) & reparse_flag
        ):
            raise ReconcileValidationError(
                "review_batch_manifest projection generation reservation is a reparse point"
            )
        reservation = _read_json_object(reservation_path)
        expected_reservation = {
            "artifact_type": "review_batch_projection_generation",
            "artifact_version": "v1",
            "schema_version": PROJECTION_GENERATION_SCHEMA_VERSION,
            "batch_id": batch_id,
            "derivation_id": derivation_id,
            "projection_generation": projection_generation,
            "coordinator_job_id": record.job_id,
        }
        if reservation != expected_reservation:
            raise ReconcileValidationError(
                "review_batch_manifest projection generation reservation is inconsistent"
            )


def validate_review_batch_manifest_for_bootstrap(
    record: ArtifactRecord,
    path: Path,
    registry: ArtifactRegistry,
) -> Mapping[str, Any]:
    """Fully validate batch links before runner bootstraps external Registries."""

    payload = validate_review_batch_manifest_location(record, path, registry)
    _validate_review_batch_manifest(record, path)
    return payload


DEFAULT_SCHEMA_VALIDATORS: dict[str, SchemaValidator] = {
    "job_outcome": _validate_job_outcome,
    STAGE_TERMINAL_ARTIFACT_TYPE: _validate_stage_terminal,
    "source_bundle": _validate_source_bundle,
    "summary_file": _validate_summary_file,
    "review_docx": _validate_docx,
    "source_pdf": _validate_pdf,
    "validation_run_result": _validate_validation_run_result,
    "runtime_job_spec": _validate_json_object,
    "stage1_progress_snapshot": _validate_json_object,
    "summary_source_manifest": _validate_summary_source_manifest,
    "summary_selection": _validate_summary_selection,
    "review_batch_manifest": _validate_review_batch_manifest,
    "paper_artifact": _validate_paper_artifact,
    "evidence_manifest": _validate_evidence_manifest,
    "normalized_text": _validate_nonempty_text,
    "chunks": _validate_json_array,
    "page_index": _validate_json_array,
    "review_draft": _validate_review_draft,
    "citation_manifest": _validate_citation_manifest,
    "citation_ref_catalog": _validate_citation_ref_catalog,
    "provider_receipt_ledger": _validate_provider_receipt_ledger,
    "provider_receipt_closure": _validate_provider_receipt_closure,
    "stage1_canonical_summaries": _validate_json_object,
    "audit_record": _validate_audit_record,
    "validation_report_projection": _validate_nonempty_text,
    "manual_review_projection": _validate_json_object,
    "validation_completion_projection": _validate_json_object,
    "claim_alignment_audit_projection": _validate_json_object,
    "final_outline": _validate_final_outline,
    "outline_stage_health": _validate_outline_stage_health,
    "outline_adoption_pointer": _validate_outline_adoption_pointer,
}

for _current_outline_artifact_type in CURRENT_OUTLINE_V3_ARTIFACT_TYPES:
    if _current_outline_artifact_type in {"stage1_canonical_summaries", "outline_v3_node_dag"}:
        continue
    if _current_outline_artifact_type in _OUTLINE_V3_ARTIFACT_TYPES:
        continue
    DEFAULT_SCHEMA_VALIDATORS[_current_outline_artifact_type] = validate_current_outline_artifact
DEFAULT_SCHEMA_VALIDATORS["stage1_canonical_summaries"] = validate_current_outline_artifact
DEFAULT_SCHEMA_VALIDATORS["outline_v3_node_dag"] = validate_current_outline_artifact

for _outline_artifact_type in _OUTLINE_V3_ARTIFACT_TYPES:
    DEFAULT_SCHEMA_VALIDATORS[_outline_artifact_type] = _validate_outline_v3_artifact


class RuntimeReconciler:
    """Repair only facts derivable from durable artifacts; it has no provider surface."""

    def __init__(
        self,
        workspace: object,
        registry: ArtifactRegistry,
        *,
        schema_validators: Mapping[str, SchemaValidator] | None = None,
        external_registry_resolver: ExternalRegistryResolver | None = None,
    ) -> None:
        self.workspace = workspace
        self.registry = registry
        self.external_registry_resolver = external_registry_resolver
        self.schema_validators = dict(DEFAULT_SCHEMA_VALIDATORS)
        self.schema_validators.update(dict(schema_validators or {}))
        self.stage_store = StageTerminalStore(workspace, registry)

    def _registry_for_ref(
        self,
        ref: ArtifactDependencyRefV2,
        *,
        local_registry: ArtifactRegistry | None = None,
    ) -> ArtifactRegistry:
        if ref.dependency_kind == "local_job":
            target = local_registry or self.registry
            if ref.job_id and ref.job_id != target.job_id:
                raise ReconcileValidationError(
                    f"local dependency {ref.artifact_id!r} names another job: {ref.job_id}"
                )
            return target
        if self.external_registry_resolver is None:
            raise ReconcileValidationError(
                f"external dependency cannot be resolved without a resolver: {ref.job_id}/{ref.artifact_id}"
            )
        resolved = self.external_registry_resolver(ref.job_id)
        if resolved is None:
            raise ReconcileValidationError(
                f"external dependency Registry is unavailable: {ref.job_id}/{ref.artifact_id}"
            )
        return resolved

    def validate_dependency_ref(
        self,
        ref: ArtifactDependencyRefV2,
        *,
        visited: set[tuple[str, str]] | None = None,
        registry: ArtifactRegistry | None = None,
        refreshed_registries: set[tuple[str, str]] | None = None,
    ) -> ArtifactRecord:
        target_registry = self._registry_for_ref(ref, local_registry=registry)
        active_refreshed = refreshed_registries if refreshed_registries is not None else set()
        registry_key = (
            os.path.normcase(os.path.abspath(target_registry.registry_path)),
            target_registry.job_id,
        )
        if registry_key not in active_refreshed:
            target_registry.reload()
            active_refreshed.add(registry_key)
        record = target_registry.get(ref.artifact_id)
        if record is None:
            raise ReconcileValidationError(f"dependency is not registered: {ref.job_id}/{ref.artifact_id}")
        if ref.job_id and record.job_id != ref.job_id:
            raise ReconcileValidationError(f"dependency job_id mismatch: {ref.artifact_id}")
        if ref.artifact_type and record.artifact_type != ref.artifact_type:
            raise ReconcileValidationError(f"dependency artifact_type mismatch: {ref.artifact_id}")
        if ref.path and os.path.normcase(os.path.abspath(os.fspath(ref.path))) != os.path.normcase(
            os.path.abspath(os.fspath(record.path))
        ):
            raise ReconcileValidationError(f"dependency path mismatch: {ref.artifact_id}")
        if ref.content_hash and record.content_hash != ref.content_hash:
            raise ReconcileValidationError(f"dependency declared hash mismatch: {ref.artifact_id}")
        self.validate_record(
            record,
            registry=target_registry,
            visited=visited,
            refreshed_registries=active_refreshed,
        )
        return record

    def validate_record(
        self,
        record: ArtifactRecord,
        *,
        registry: ArtifactRegistry | None = None,
        visited: set[tuple[str, str]] | None = None,
        refreshed_registries: set[tuple[str, str]] | None = None,
    ) -> None:
        active_registry = registry or self.registry
        active_refreshed = refreshed_registries if refreshed_registries is not None else set()
        key = (record.job_id, record.artifact_id)
        active_visited = visited if visited is not None else set()
        if key in active_visited:
            raise ReconcileValidationError(f"artifact dependency cycle detected at {record.artifact_id}")
        active_visited.add(key)
        try:
            if record.job_id != active_registry.job_id:
                raise ReconcileValidationError(
                    f"artifact job_id does not match Registry owner: {record.artifact_id}"
                )
            if record.status != "ready":
                raise ReconcileValidationError(
                    f"artifact is not ready: {record.artifact_id} ({record.status})"
                )
            path = Path(record.path)
            if not path.is_file():
                raise ReconcileValidationError(f"artifact file is missing: {record.artifact_id}")
            if not record.content_hash:
                raise ReconcileValidationError(f"artifact content hash is missing: {record.artifact_id}")
            actual_hash = file_sha256(path)
            if actual_hash != record.content_hash:
                raise ReconcileValidationError(f"artifact content hash mismatch: {record.artifact_id}")
            if record.artifact_type == "review_batch_manifest":
                validate_review_batch_manifest_location(record, path, active_registry)
            validator = self.schema_validators.get(record.artifact_type)
            if validator is None:
                raise ReconcileValidationError(
                    f"no schema validator is registered for artifact type {record.artifact_type!r}"
                )
            validator(record, path)
            for dependency in record.depends_on:
                self.validate_dependency_ref(
                    dependency,
                    visited=active_visited,
                    registry=active_registry,
                    refreshed_registries=active_refreshed,
                )
        finally:
            active_visited.remove(key)

    def _validate_stage_recovery_identity(self, record: TerminalStageRecordV1) -> None:
        if record.stage_name != "validate":
            return
        canonical_refs = [
            ref
            for ref in record.output_artifact_refs
            if ref.artifact_type == "validation_run_result"
        ]
        if len(canonical_refs) != 1:
            raise ReconcileValidationError(
                "completed Validation stage requires exactly one canonical validation run result"
            )
        from validation.run_result import ValidationRunResultV1

        canonical = self.validate_dependency_ref(canonical_refs[0])
        validation = ValidationRunResultV1.from_dict(
            _read_json_object(Path(canonical.path))
        )
        if validation.attempt_id != record.attempt_id:
            raise ReconcileValidationError(
                "completed Validation result attempt_id does not match its terminal record"
            )

    def _completed_stage_record(self, stage_name: str) -> TerminalStageRecordV1 | None:
        candidates = [
            (record, path)
            for record, path in self.stage_store.load_records()
            if record.stage_name == stage_name and record.status == "succeeded"
        ]
        for record, path in sorted(candidates, key=lambda item: item[0].finished_at, reverse=True):
            if record.job_id != self.registry.job_id:
                continue
            registered = self.registry.get(record.record_id)
            if registered is None:
                continue
            if (
                registered.artifact_role != STAGE_TERMINAL_ROLE
                or registered.artifact_type != STAGE_TERMINAL_ARTIFACT_TYPE
                or registered.artifact_version != STAGE_TERMINAL_ARTIFACT_VERSION
                or Path(registered.path).resolve() != path.resolve()
                or tuple(registered.depends_on) != tuple(record.output_artifact_refs)
            ):
                continue
            try:
                self.validate_record(registered)
                for output_ref in record.output_artifact_refs:
                    self.validate_dependency_ref(output_ref)
                self._validate_stage_recovery_identity(record)
            except ReconcileValidationError:
                continue
            return record
        return None

    def stage_is_complete(self, stage_name: str) -> bool:
        return self._completed_stage_record(stage_name) is not None

    def load_completed_stage_result(self, stage_name: str) -> StageResult | None:
        """Rehydrate a completed stage from its durable terminal outputs."""

        terminal = self._completed_stage_record(stage_name)
        if terminal is None:
            return None

        artifacts: list[StageArtifactRef] = []
        resolved_outputs: list[tuple[ArtifactDependencyRefV2, ArtifactRecord]] = []
        for output_ref in terminal.output_artifact_refs:
            output = self.validate_dependency_ref(output_ref)
            resolved_outputs.append((output_ref, output))
            artifacts.append(
                StageArtifactRef(
                    artifact_role=output.artifact_role,
                    artifact_type=output.artifact_type,
                    artifact_version=output.artifact_version,
                    path=output.path,
                    artifact_id=output.artifact_id,
                )
            )

        metadata: dict[str, Any] = {
            "recovered_from_terminal": terminal.record_id,
            "model_call_count": terminal.model_call_count,
        }
        if stage_name == "validate":
            canonical_outputs = [
                output
                for output_ref, output in resolved_outputs
                if output_ref.artifact_type == "validation_run_result"
            ]
            if len(canonical_outputs) != 1:
                raise ReconcileValidationError(
                    "completed Validation stage requires exactly one canonical validation run result"
                )
            from validation.run_result import ValidationRunResultV1

            validation = ValidationRunResultV1.from_dict(
                _read_json_object(Path(canonical_outputs[0].path))
            )
            if validation.job_id != self.registry.job_id:
                raise ReconcileValidationError(
                    "completed Validation result belongs to another job"
                )
            if validation.attempt_id != terminal.attempt_id:
                raise ReconcileValidationError(
                    "completed Validation result belongs to another attempt"
                )
            if not validation.contract_satisfied:
                raise ReconcileValidationError(
                    "completed Validation terminal does not reference a contract-satisfied result"
                )
            metadata.update(
                {
                    "manual_review_count": validation.claim_verdict_counts.get(
                        "needs_review", 0
                    ),
                    "validation_run_id": validation.validation_run_id,
                    "execution_status": validation.execution_status.value,
                    "validation_disposition": validation.validation_disposition.value,
                    "claim_verdict_counts": dict(validation.claim_verdict_counts),
                    "contradicted_count": validation.contradicted_count,
                }
            )

        return StageResult(
            stage_name=stage_name,
            success=True,
            artifacts=artifacts,
            metadata=metadata,
        )

    def _repair_outcome_registration(self) -> tuple[bool, str | None, JobOutcomeV1 | None]:
        path = Path(str(getattr(self.workspace, "artifact_path")("job_outcome_v1.json")))
        if not path.is_file():
            return False, None, None
        synthetic = ArtifactRecord(
            artifact_id="job_outcome",
            artifact_role="job_outcome",
            artifact_type="job_outcome",
            artifact_version="v1",
            path=str(path.resolve()),
            producer="runtime.reconcile.RuntimeReconciler",
            job_id=self.registry.job_id,
            status="ready",
            content_hash=file_sha256(path),
        )
        try:
            _validate_job_outcome(synthetic, path)
            outcome = JobOutcomeV1.from_dict(_read_json_object(path))
        except (TypeError, ValueError) as exc:
            raise ReconcileValidationError(f"job outcome is not repairable: {exc}") from exc
        if outcome.job_id != self.registry.job_id:
            raise ReconcileValidationError("job outcome belongs to another job")
        current = self.registry.get("job_outcome")
        if current is not None:
            if current.job_id != self.registry.job_id or current.artifact_type != "job_outcome":
                raise ReconcileValidationError("job outcome Registry identity conflicts with this workspace")
            if (
                current.artifact_role != "job_outcome"
                or current.artifact_version != "v1"
                or Path(current.path).resolve() != path.resolve()
            ):
                raise ReconcileValidationError("job outcome Registry projection is inconsistent")
            if current.status != "ready":
                raise ReconcileValidationError(
                    f"job outcome Registry record is not ready: {current.status}"
                )
            if current.content_hash == synthetic.content_hash:
                self.validate_record(current)
                return False, None, outcome
        self.registry.register_file(
            artifact_role="job_outcome",
            artifact_type="job_outcome",
            artifact_version="v1",
            path=path,
            producer="runtime.reconcile.RuntimeReconciler",
            artifact_id="job_outcome",
            metadata={
                "job_status": outcome.job_status,
                "job_disposition": outcome.job_disposition,
                "canonical_ready": outcome.canonical_ready,
                "requires_attention": outcome.requires_attention,
                "outcome_revision": outcome.outcome_revision,
            },
        )
        return True, "job_outcome", outcome

    def _repair_terminal_registration(
        self,
        record: TerminalStageRecordV1,
        path: Path,
    ) -> bool:
        if record.job_id != self.registry.job_id:
            raise ReconcileValidationError("stage terminal belongs to another job")
        for output_ref in record.output_artifact_refs:
            self.validate_dependency_ref(output_ref)
        current = self.registry.get(record.record_id)
        actual_hash = file_sha256(path)
        if current is not None:
            if current.job_id != self.registry.job_id or current.artifact_type != STAGE_TERMINAL_ARTIFACT_TYPE:
                raise ReconcileValidationError("stage terminal Registry identity conflicts with this workspace")
            if (
                current.artifact_role != STAGE_TERMINAL_ROLE
                or current.artifact_version != STAGE_TERMINAL_ARTIFACT_VERSION
                or Path(current.path).resolve() != path.resolve()
                or tuple(current.depends_on) != tuple(record.output_artifact_refs)
            ):
                raise ReconcileValidationError("stage terminal Registry projection is inconsistent")
            if current.status != "ready":
                raise ReconcileValidationError(
                    f"stage terminal Registry record is not ready: {current.status}"
                )
            if current.content_hash != actual_hash:
                raise ReconcileValidationError("immutable stage terminal hash does not match its Registry record")
            self.validate_record(current)
            return False
        self.registry.register_file(
            artifact_role=STAGE_TERMINAL_ROLE,
            artifact_type=STAGE_TERMINAL_ARTIFACT_TYPE,
            artifact_version=STAGE_TERMINAL_ARTIFACT_VERSION,
            path=path,
            producer="runtime.reconcile.RuntimeReconciler",
            artifact_id=record.record_id,
            depends_on=record.output_artifact_refs,
            metadata={
                "stage_name": record.stage_name,
                "stage_status": record.status,
                "attempt_id": record.attempt_id,
                "model_call_count": record.model_call_count,
                "reconstructed_by_reconcile": record.reconstructed_by_reconcile,
            },
        )
        return True

    def _reconstruct_stage(self, recovery: ProvenStageRecovery) -> TerminalStageRecordV1:
        for output_ref in recovery.output_artifact_refs:
            self.validate_dependency_ref(output_ref)
        record = TerminalStageRecordV1.create(
            job_id=self.registry.job_id,
            attempt_id=recovery.attempt_id,
            stage_name=recovery.stage_name,
            status="succeeded",
            producer="runtime.reconcile.RuntimeReconciler",
            input_artifact_refs=recovery.input_artifact_refs,
            output_artifact_refs=recovery.output_artifact_refs,
            model_call_count=recovery.model_call_count,
            reconstructed_by_reconcile=True,
            terminal_reason="reconstructed from schema-valid registered artifacts",
        )
        self.stage_store.persist(record)
        return record

    def _repair_owned_pointer(
        self,
        outcome: JobOutcomeV1 | None,
    ) -> tuple[bool, tuple[ReconcileIssue, ...]]:
        if outcome is None:
            return False, ()
        pointer_path = Path(str(getattr(self.workspace, "latest_pointer_path")()))
        try:
            payload = json.loads(pointer_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return False, ()
        if not isinstance(payload, dict) or str(payload.get("job_id") or "") != self.registry.job_id:
            return False, ()

        resume_report_path = Path(
            str(getattr(self.workspace, "artifact_path")("resume_state_report.json"))
        )
        try:
            resume_report = _read_json_object(resume_report_path)
        except ReconcileValidationError as exc:
            return False, (
                ReconcileIssue(
                    "invalid_resume_state_report_identity",
                    str(exc),
                    artifact_id="resume_state_report",
                ),
            )

        project_name = str(getattr(self.workspace, "project_name", "") or "")
        resume_identity_errors: list[str] = []
        if str(resume_report.get("job_id") or "") != self.registry.job_id:
            resume_identity_errors.append("job_id")
        if str(resume_report.get("created_from_job_id") or "") != self.registry.job_id:
            resume_identity_errors.append("created_from_job_id")
        if str(resume_report.get("project_name") or "") != project_name:
            resume_identity_errors.append("project_name")
        fingerprint_bundle = resume_report.get("fingerprint_bundle")
        if not _valid_fingerprint_bundle(fingerprint_bundle):
            resume_identity_errors.append("fingerprint_bundle")
        resume_state = str(resume_report.get("state") or "")
        if not resume_state:
            resume_identity_errors.append("state")
        if resume_identity_errors:
            return False, (
                ReconcileIssue(
                    "invalid_resume_state_report_identity",
                    "resume state report identity is invalid: "
                    + ", ".join(resume_identity_errors),
                    artifact_id="resume_state_report",
                ),
            )

        expected_workspace_path = Path(str(getattr(self.workspace, "root_dir"))).resolve()
        expected_registry_path = Path(
            str(getattr(getattr(self.workspace, "paths"), "registry_path"))
        ).resolve()
        identity_mismatches: list[str] = []
        if str(payload.get("project_name") or "") != project_name:
            identity_mismatches.append("project_name")
        try:
            pointer_workspace_path = Path(str(payload.get("workspace_path") or "")).resolve()
        except (OSError, RuntimeError, ValueError):
            pointer_workspace_path = Path()
        if pointer_workspace_path != expected_workspace_path:
            identity_mismatches.append("workspace_path")
        try:
            pointer_registry_path = Path(
                str(payload.get("artifact_registry_path") or "")
            ).resolve()
        except (OSError, RuntimeError, ValueError):
            pointer_registry_path = Path()
        if pointer_registry_path != expected_registry_path:
            identity_mismatches.append("artifact_registry_path")
        if payload.get("fingerprint_bundle") != fingerprint_bundle:
            identity_mismatches.append("fingerprint_bundle")

        needs_repair = bool(
            identity_mismatches
            or str(payload.get("status") or "") != outcome.job_status
            or str(payload.get("resume_state") or "") != resume_state
        )
        if not needs_repair:
            return False, ()
        repaired = bool(
            getattr(self.workspace, "write_latest_pointer_if_owned")(
                resume_state=resume_state,
                fingerprint_bundle=fingerprint_bundle,
                status=outcome.job_status,
            )
        )
        issues: tuple[ReconcileIssue, ...] = ()
        if identity_mismatches:
            issues = (
                ReconcileIssue(
                    "latest_pointer_identity_mismatch",
                    "latest pointer identity was inconsistent: "
                    + ", ".join(identity_mismatches),
                    artifact_id="latest_pointer",
                ),
            )
        return repaired, issues

    def reconcile(
        self,
        *,
        stage_recoveries: Sequence[ProvenStageRecovery] = (),
    ) -> ReconcileResult:
        repaired: list[str] = []
        reconstructed: list[str] = []
        issues: list[ReconcileIssue] = []

        outcome_repaired = False
        outcome: JobOutcomeV1 | None = None
        try:
            outcome_repaired, repaired_outcome_id, outcome = self._repair_outcome_registration()
            if repaired_outcome_id:
                repaired.append(repaired_outcome_id)
        except ReconcileValidationError as exc:
            issues.append(ReconcileIssue("invalid_job_outcome", str(exc), artifact_id="job_outcome"))

        records: tuple[tuple[TerminalStageRecordV1, Path], ...] = ()
        try:
            records = self.stage_store.load_records()
        except (TypeError, ValueError) as exc:
            issues.append(ReconcileIssue("invalid_stage_terminal", str(exc)))

        for record, path in records:
            try:
                if self._repair_terminal_registration(record, path):
                    repaired.append(record.record_id)
            except ReconcileValidationError as exc:
                issues.append(
                    ReconcileIssue(
                        "unresolved_stage_terminal",
                        str(exc),
                        artifact_id=record.record_id,
                        stage_name=record.stage_name,
                    )
                )

        stage_names = {record.stage_name for record, _path in records}
        for recovery in stage_recoveries:
            if self.stage_is_complete(recovery.stage_name):
                stage_names.add(recovery.stage_name)
                continue
            try:
                record = self._reconstruct_stage(recovery)
                reconstructed.append(record.record_id)
                stage_names.add(record.stage_name)
            except (TypeError, ValueError) as exc:
                issues.append(
                    ReconcileIssue(
                        "stage_reconstruction_not_proven",
                        str(exc),
                        stage_name=recovery.stage_name,
                    )
                )

        completed = tuple(sorted(stage for stage in stage_names if self.stage_is_complete(stage)))
        pointer_repaired, pointer_issues = self._repair_owned_pointer(outcome)
        issues.extend(pointer_issues)
        return ReconcileResult(
            job_id=self.registry.job_id,
            completed_stages=completed,
            repaired_artifact_ids=tuple(dict.fromkeys(repaired)),
            reconstructed_stage_records=tuple(reconstructed),
            outcome_repaired=outcome_repaired,
            pointer_repaired=pointer_repaired,
            issues=tuple(issues),
        )
