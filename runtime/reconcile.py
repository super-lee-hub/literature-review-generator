from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence
import zipfile

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
from services.job_workspace import atomic_write_json
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


@dataclass(frozen=True)
class LegacyMigrationResult:
    job_id: str
    legacy_summary_path: str
    job_outcome_path: str
    audit_record_path: str
    migrated_artifact_ids: tuple[str, ...]
    compatibility_status: str
    canonical_ready: bool
    requires_attention: bool

    @property
    def migrated(self) -> bool:
        return bool(self.migrated_artifact_ids)


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReconcileValidationError(f"invalid JSON {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ReconcileValidationError(f"JSON artifact must be an object: {path}")
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


def _validate_recognizable_legacy_summary_file(path: Path) -> None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReconcileValidationError(f"invalid legacy summary JSON {path}: {exc}") from exc
    if not isinstance(payload, list) or not payload:
        raise ReconcileValidationError("legacy summary_file must contain summaries")
    paper_keys: set[str] = set()
    for index, item in enumerate(payload):
        if not isinstance(item, Mapping):
            raise ReconcileValidationError("legacy summary entries must be JSON objects")
        if str(item.get("status") or "").strip().casefold() != "success":
            raise ReconcileValidationError(f"legacy summary entry {index} is not successful")
        paper_info = item.get("paper_info")
        if not isinstance(paper_info, Mapping):
            raise ReconcileValidationError(f"legacy summary entry {index} has no paper_info")
        paper_key = _require_nonempty_string(
            paper_info.get("canonical_paper_key"),
            label=f"legacy summary entry {index} canonical_paper_key",
        )
        if paper_key in paper_keys:
            raise ReconcileValidationError(
                f"legacy summary_file has duplicate paper identity: {paper_key}"
            )
        ai_summary = item.get("ai_summary")
        if not isinstance(ai_summary, Mapping) or not ai_summary:
            raise ReconcileValidationError(f"legacy summary entry {index} has no ai_summary")
        paper_keys.add(paper_key)


def _validate_legacy_summary_file(_record: ArtifactRecord, path: Path) -> None:
    _validate_recognizable_legacy_summary_file(path)


def _validate_legacy_summary_source(_record: ArtifactRecord, path: Path) -> None:
    from services.summary_reuse import SummaryCatalog, SummarySource

    catalog = SummaryCatalog.from_sources(
        [
            SummarySource(
                path=str(path.resolve()),
                source_type="explicit",
                priority=0,
                label="legacy_summary_source",
            )
        ]
    )
    if not catalog.records:
        raise ReconcileValidationError(
            "legacy_summary_source has no reusable successful summary records"
        )


def _legacy_summary_path(workspace: object) -> Path:
    project_name = str(getattr(workspace, "project_name", "") or "")
    if not project_name:
        raise ReconcileValidationError("legacy workspace has no project_name")
    return Path(
        str(getattr(workspace, "artifact_path")(f"{project_name}_summaries.json"))
    )


def project_legacy_workspace_outcome(workspace: object) -> JobOutcomeV1 | None:
    """Read-only fail-closed projection for a summary-only legacy workspace."""

    project_name = str(getattr(workspace, "project_name", "") or "")
    job_id = str(getattr(workspace, "job_id", "") or "")
    if not project_name or not job_id:
        return None
    summary_path = _legacy_summary_path(workspace)
    if not summary_path.is_file():
        return None
    try:
        _validate_recognizable_legacy_summary_file(summary_path)
    except ReconcileValidationError:
        return None
    return JobOutcomeV1.legacy_unverified(
        job_id=job_id,
        required_stages=("source_intake", "analyze"),
        degradation_reasons=("legacy_summary_without_runtime_contract",),
    )


def _detect_legacy_unverified_workspace(
    workspace: object,
) -> tuple[JobOutcomeV1, str] | None:
    """Identify a legacy workspace without repairing any durable projection."""

    outcome_path = Path(
        str(getattr(workspace, "artifact_path")("job_outcome_v1.json"))
    )
    if outcome_path.is_file():
        try:
            outcome = JobOutcomeV1.from_dict(_read_json_object(outcome_path))
        except (ReconcileValidationError, TypeError, ValueError):
            outcome = project_legacy_workspace_outcome(workspace)
            if outcome is None:
                return None
            return outcome, "invalid_job_outcome"
        if outcome.compatibility_status == "legacy_unverified":
            return outcome, "job_outcome"
        return None

    outcome = project_legacy_workspace_outcome(workspace)
    if outcome is None:
        return None
    return outcome, "legacy_summary_file"


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


def _validate_validation_run_result(_record: ArtifactRecord, path: Path) -> None:
    from validation.run_result import ValidationRunResultV1

    ValidationRunResultV1.from_dict(_read_json_object(path))


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


def _validate_outline_stage_health(record: ArtifactRecord, path: Path) -> None:
    from outline.stage_health import OutlineStageHealthV1

    payload = _read_json_object(path)
    _require_contract_header(
        record,
        payload,
        artifact_type="outline_stage_health",
        versions=("v1",),
    )
    health = OutlineStageHealthV1.from_dict(payload)
    _require_owned_job(record, payload, field="job_id")
    if health.execution_mode not in {"production", "test_dev"}:
        raise ReconcileValidationError("outline_stage_health execution_mode is invalid")
    if not health.stages:
        raise ReconcileValidationError("outline_stage_health has no stage entries")
    _require_nonempty_string(health.source_final_outline_hash, label="source_final_outline_hash")
    _require_nonempty_string(health.source_coverage_audit_hash, label="source_coverage_audit_hash")
    _require_nonempty_string(health.created_at, label="outline_stage_health created_at")
    stage_names: set[str] = set()
    for entry in health.stages:
        stage_name = _require_nonempty_string(entry.stage_name, label="outline stage name")
        if stage_name in stage_names:
            raise ReconcileValidationError(f"duplicate outline stage health entry: {stage_name}")
        stage_names.add(stage_name)
        _require_nonempty_string(entry.provider_route, label=f"{stage_name} provider_route")
        if entry.execution_status not in {"succeeded", "failed", "skipped"}:
            raise ReconcileValidationError(f"invalid execution_status for outline stage {stage_name}")
        if entry.attempts < 0:
            raise ReconcileValidationError(f"negative attempt count for outline stage {stage_name}")


def _validate_final_outline_payload(
    record: ArtifactRecord,
    payload: Mapping[str, Any],
    *,
    require_owner: bool = True,
) -> object:
    from outline.v2_models import FinalOutline

    _require_contract_header(
        record,
        payload,
        artifact_type="final_outline",
        versions=("v2",),
    )
    if require_owner:
        _require_owned_job(record, payload, field="created_from_job_id")
    outline = FinalOutline.from_dict(dict(payload))
    for field_name in (
        "outline_id",
        "source_literature_map_id",
        "source_synthesis_flow_id",
        "source_arbitration_report_id",
        "source_literature_map_hash",
        "source_synthesis_flow_hash",
    ):
        _require_nonempty_string(getattr(outline, field_name), label=f"final_outline {field_name}")
    if not outline.sections:
        raise ReconcileValidationError("final_outline has no sections")

    def validate_sections(sections: Sequence[object]) -> None:
        for section in sections:
            _require_nonempty_string(getattr(section, "section_id", ""), label="final section_id")
            _require_nonempty_string(getattr(section, "title", ""), label="final section title")
            validate_sections(getattr(section, "children", ()))

    validate_sections(outline.sections)
    return outline


def _validate_final_outline(record: ArtifactRecord, path: Path) -> None:
    _validate_final_outline_payload(record, _read_json_object(path))


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
    from services.review_draft import ReviewDraftV1, ReviewDraftV2

    payload = _read_json_object(path)
    version = _require_contract_header(
        record,
        payload,
        artifact_type="review_draft",
        versions=("v1", "v2"),
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
    draft_class = ReviewDraftV1 if version == "v1" else ReviewDraftV2
    draft = draft_class(
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
        if version == "v1":
            _require_nonempty_string(section_data.get("content"), label="review section content")
            continue
        blocks = _require_list(section_data.get("blocks"), label="review section blocks")
        if not blocks:
            raise ReconcileValidationError("review_draft v2 section has no blocks")
        for block in blocks:
            block_data = _require_mapping(block, label="review block")
            _require_nonempty_string(block_data.get("block_id"), label="review block_id")
            _require_nonempty_string(block_data.get("text"), label="review block text")


def _validate_citation_manifest(record: ArtifactRecord, path: Path) -> None:
    from services.citation_manifest import CitationManifestV2

    payload = _read_json_object(path)
    version = _require_contract_header(
        record,
        payload,
        artifact_type="citation_manifest",
        versions=("v1", "v2", "v3"),
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
    if version == "v1":
        _require_list(payload.get("citations"), label="citation manifest citations")
        return

    CitationManifestV2.from_dict(dict(payload))
    if version == "v2":
        return

    for field_name in (
        "paper_entries",
        "occurrences",
        "clusters",
        "citation_sets",
        "bibliography",
    ):
        _require_list(payload.get(field_name), label=f"citation_manifest {field_name}")
    migration = _require_mapping(payload.get("migration_report"), label="citation migration_report")
    if str(migration.get("contract_version") or "") != "v3":
        raise ReconcileValidationError("citation migration_report contract_version is invalid")
    _require_nonempty_string(migration.get("load_source"), label="citation migration load_source")
    if str(payload.get("review_draft_version") or "") != "v2":
        raise ReconcileValidationError("citation_manifest v3 requires review_draft v2")
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


def _validate_outline_model(
    record: ArtifactRecord,
    path: Path,
    *,
    artifact_type: str,
    artifact_version: str,
    model_type: type[Any],
) -> Any:
    payload = _read_json_object(path)
    _require_contract_header(
        record,
        payload,
        artifact_type=artifact_type,
        versions=(artifact_version,),
    )
    return model_type.from_dict(payload)


def _validate_literature_map(record: ArtifactRecord, path: Path) -> None:
    from outline.v2_models import LiteratureMap

    model = _validate_outline_model(
        record,
        path,
        artifact_type="literature_map",
        artifact_version="v1",
        model_type=LiteratureMap,
    )
    if model.created_from_job_id != record.job_id:
        raise ReconcileValidationError("literature_map does not belong to its Registry job")
    _require_nonempty_string(model.created_at, label="literature_map created_at")
    if not model.paper_nodes or not model.source_summary_hashes:
        raise ReconcileValidationError("literature_map has no source papers")


def _validate_synthesis_flow(record: ArtifactRecord, path: Path) -> None:
    from outline.v2_models import SynthesisFlow

    model = _validate_outline_model(
        record,
        path,
        artifact_type="synthesis_flow",
        artifact_version="v1",
        model_type=SynthesisFlow,
    )
    if model.created_from_job_id != record.job_id:
        raise ReconcileValidationError("synthesis_flow does not belong to its Registry job")
    _require_nonempty_string(model.source_literature_map_id, label="source_literature_map_id")
    _require_nonempty_string(model.flow_strategy, label="synthesis flow_strategy")
    if not model.flow_steps:
        raise ReconcileValidationError("synthesis_flow has no flow steps")


def _validate_outline_candidates(record: ArtifactRecord, path: Path) -> None:
    from outline.v2_models import OutlineCandidates

    model = _validate_outline_model(
        record,
        path,
        artifact_type="outline_candidates",
        artifact_version="v1",
        model_type=OutlineCandidates,
    )
    _require_nonempty_string(model.source_literature_map_id, label="candidate literature_map id")
    _require_nonempty_string(model.source_synthesis_flow_id, label="candidate synthesis_flow id")
    if model.candidate_count != len(model.candidates) or not model.candidates:
        raise ReconcileValidationError("outline_candidates candidate_count is inconsistent")
    for candidate in model.candidates:
        _require_nonempty_string(candidate.candidate_id, label="outline candidate_id")
        if not candidate.sections:
            raise ReconcileValidationError("outline candidate has no sections")


def _validate_outline_critiques(record: ArtifactRecord, path: Path) -> None:
    from outline.v2_models import OutlineCritiquesV2

    model = _validate_outline_model(
        record,
        path,
        artifact_type="outline_critiques",
        artifact_version="v1",
        model_type=OutlineCritiquesV2,
    )
    if not model.source_candidate_ids or not model.critique_runs:
        raise ReconcileValidationError("outline_critiques has no completed critique runs")
    for run in model.critique_runs:
        _require_nonempty_string(run.run_id, label="critique run_id")
        if run.critic_role not in {"structure", "coverage"}:
            raise ReconcileValidationError("outline critique role is invalid")


def _validate_outline_arbitration(record: ArtifactRecord, path: Path) -> None:
    from outline.v2_models import ArbitrationReport

    model = _validate_outline_model(
        record,
        path,
        artifact_type="outline_arbitration_report",
        artifact_version="v1",
        model_type=ArbitrationReport,
    )
    if not model.source_candidates or not model.final_decision:
        raise ReconcileValidationError("outline arbitration has no final decision")
    _require_nonempty_string(model.arbitrator_model, label="outline arbitrator_model")


def _validate_outline_coverage_audit(record: ArtifactRecord, path: Path) -> None:
    from outline.v2_models import CoverageAudit

    model = _validate_outline_model(
        record,
        path,
        artifact_type="outline_coverage_audit",
        artifact_version="v1",
        model_type=CoverageAudit,
    )
    _require_nonempty_string(model.source_final_outline_id, label="coverage source_final_outline_id")
    _require_nonempty_string(model.source_final_outline_hash, label="coverage source_final_outline_hash")
    _require_nonempty_string(model.source_literature_map_hash, label="coverage literature_map hash")
    _require_nonempty_string(model.source_synthesis_flow_hash, label="coverage synthesis_flow hash")


def _validate_adopted_final_outline(record: ArtifactRecord, path: Path) -> None:
    from outline.v2_models import AdoptedFinalOutline

    model = _validate_outline_model(
        record,
        path,
        artifact_type="adopted_final_outline",
        artifact_version="v1",
        model_type=AdoptedFinalOutline,
    )
    if model.created_from_job_id != record.job_id:
        raise ReconcileValidationError("adopted outline does not belong to its Registry job")
    for field_name in (
        "source_final_outline_id",
        "source_final_outline_hash",
        "source_coverage_audit_id",
        "source_coverage_audit_hash",
        "adopted_at",
        "adopted_by",
    ):
        _require_nonempty_string(getattr(model, field_name), label=f"adopted outline {field_name}")
    outline_payload = _require_mapping(_read_json_object(path).get("outline"), label="adopted outline")
    synthetic_record = ArtifactRecord(
        artifact_id=record.artifact_id,
        artifact_role=record.artifact_role,
        artifact_type="final_outline",
        artifact_version="v2",
        job_id=record.job_id,
        path=record.path,
        content_hash=record.content_hash,
        producer=record.producer,
        created_at=record.created_at,
        status=record.status,
        depends_on=record.depends_on,
        metadata=record.metadata,
    )
    _validate_final_outline_payload(synthetic_record, outline_payload)


def _validate_json_object(_record: ArtifactRecord, path: Path) -> None:
    _read_json_object(path)


def _validate_json_array(_record: ArtifactRecord, path: Path) -> None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReconcileValidationError(f"invalid JSON {path}: {exc}") from exc
    if not isinstance(payload, list):
        raise ReconcileValidationError(f"JSON artifact must be an array: {path}")


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
        if audit_ref_identities != dependency_identities:
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


DEFAULT_SCHEMA_VALIDATORS: Mapping[str, SchemaValidator] = {
    "job_outcome": _validate_job_outcome,
    STAGE_TERMINAL_ARTIFACT_TYPE: _validate_stage_terminal,
    "source_bundle": _validate_source_bundle,
    "summary_file": _validate_summary_file,
    "legacy_summary_file": _validate_legacy_summary_file,
    "legacy_summary_source": _validate_legacy_summary_source,
    "literature_review_outline": _validate_nonempty_text,
    "review_docx": _validate_docx,
    "source_pdf": _validate_pdf,
    "validation_run_result": _validate_validation_run_result,
    "runtime_job_spec": _validate_json_object,
    "stage1_progress_snapshot": _validate_json_object,
    "summary_source_manifest": _validate_summary_source_manifest,
    "summary_selection": _validate_summary_selection,
    "paper_artifact": _validate_paper_artifact,
    "evidence_manifest": _validate_evidence_manifest,
    "normalized_text": _validate_nonempty_text,
    "chunks": _validate_json_array,
    "page_index": _validate_json_array,
    "review_draft": _validate_review_draft,
    "citation_manifest": _validate_citation_manifest,
    "citation_ref_catalog": _validate_citation_ref_catalog,
    "audit_record": _validate_audit_record,
    "validation_report_projection": _validate_nonempty_text,
    "manual_review_projection": _validate_json_object,
    "validation_completion_projection": _validate_json_object,
    "claim_alignment_audit_projection": _validate_json_object,
    "literature_map": _validate_literature_map,
    "synthesis_flow": _validate_synthesis_flow,
    "candidate_generation_report": _validate_json_object,
    "outline_candidates": _validate_outline_candidates,
    "outline_critiques": _validate_outline_critiques,
    "outline_arbitration_report": _validate_outline_arbitration,
    "final_outline": _validate_final_outline,
    "outline_coverage_audit": _validate_outline_coverage_audit,
    "outline_stage_health": _validate_outline_stage_health,
    "adopted_final_outline": _validate_adopted_final_outline,
}


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

    def legacy_read_only_result(self) -> ReconcileResult | None:
        """Return the fail-closed legacy result before any repair is attempted."""

        detected = _detect_legacy_unverified_workspace(self.workspace)
        if detected is None:
            return None
        _outcome, artifact_id = detected
        return ReconcileResult(
            job_id=self.registry.job_id,
            completed_stages=(),
            repaired_artifact_ids=(),
            reconstructed_stage_records=(),
            outcome_repaired=False,
            pointer_repaired=False,
            issues=(
                ReconcileIssue(
                    "legacy_unverified_workspace",
                    "legacy_unverified workspace requires explicit migrate-legacy or rerun",
                    artifact_id=artifact_id,
                ),
            ),
        )

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
    ) -> ArtifactRecord:
        target_registry = self._registry_for_ref(ref, local_registry=registry)
        record = target_registry.get(ref.artifact_id)
        if record is None:
            raise ReconcileValidationError(f"dependency is not registered: {ref.job_id}/{ref.artifact_id}")
        if ref.job_id and record.job_id != ref.job_id:
            raise ReconcileValidationError(f"dependency job_id mismatch: {ref.artifact_id}")
        if ref.artifact_type and record.artifact_type != ref.artifact_type:
            raise ReconcileValidationError(f"dependency artifact_type mismatch: {ref.artifact_id}")
        if ref.content_hash and record.content_hash != ref.content_hash:
            raise ReconcileValidationError(f"dependency declared hash mismatch: {ref.artifact_id}")
        self.validate_record(record, registry=target_registry, visited=visited)
        return record

    def validate_record(
        self,
        record: ArtifactRecord,
        *,
        registry: ArtifactRegistry | None = None,
        visited: set[tuple[str, str]] | None = None,
    ) -> None:
        active_registry = registry or self.registry
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
                )
        finally:
            active_visited.remove(key)

    def stage_is_complete(self, stage_name: str) -> bool:
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
            except ReconcileValidationError:
                continue
            return True
        return False

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
                "compatibility_status": outcome.compatibility_status,
                "outcome_revision": outcome.outcome_revision,
            },
        )
        return True, "job_outcome", outcome

    def migrate_legacy(self, *, actor: str, reason: str) -> LegacyMigrationResult:
        from services.audit_record import AuditArtifactRefV1, AuditRecordV1

        normalized_actor = actor.strip()
        normalized_reason = reason.strip()
        if not normalized_actor or not normalized_reason:
            raise ReconcileValidationError("legacy migration requires actor and reason")

        summary_path = _legacy_summary_path(self.workspace)
        _validate_recognizable_legacy_summary_file(summary_path)
        outcome_path = Path(
            str(getattr(self.workspace, "artifact_path")("job_outcome_v1.json"))
        )
        audit_id = "audit-legacy-workspace-migration-v1"
        audit_path = Path(
            str(getattr(self.workspace, "artifact_path")(f"audits/{audit_id}.json"))
        )
        allowed_artifact_ids = {"legacy_summary_file", "job_outcome", audit_id}
        unexpected = sorted(
            record.artifact_id
            for record in self.registry.list_records()
            if record.artifact_id not in allowed_artifact_ids
        )
        if unexpected:
            raise ReconcileValidationError(
                "legacy migration requires a summary-only workspace; found registered artifacts: "
                + ", ".join(unexpected)
            )

        summary_hash = file_sha256(summary_path)
        summary_record = self.registry.get("legacy_summary_file")
        outcome_record = self.registry.get("job_outcome")
        audit_record = self.registry.get(audit_id)

        if summary_record is not None:
            if (
                summary_record.job_id != self.registry.job_id
                or summary_record.artifact_role != "legacy_input"
                or summary_record.artifact_type != "legacy_summary_file"
                or summary_record.artifact_version != "v1"
                or Path(summary_record.path).resolve() != summary_path.resolve()
                or summary_record.content_hash != summary_hash
            ):
                raise ReconcileValidationError("legacy summary Registry record is inconsistent")
            self.validate_record(summary_record)

        if outcome_path.is_file():
            try:
                outcome = JobOutcomeV1.from_dict(_read_json_object(outcome_path))
            except (TypeError, ValueError) as exc:
                raise ReconcileValidationError(f"legacy migration outcome is invalid: {exc}") from exc
        else:
            if self.registry.get("job_outcome") is not None:
                raise ReconcileValidationError(
                    "legacy migration outcome Registry record has no durable file"
                )
            outcome = project_legacy_workspace_outcome(self.workspace)
            if outcome is None:
                raise ReconcileValidationError("workspace is not a recognizable legacy workspace")

        if outcome.job_id != self.registry.job_id:
            raise ReconcileValidationError("legacy migration outcome belongs to another job")
        if (
            outcome.compatibility_status != "legacy_unverified"
            or outcome.canonical_ready
            or not outcome.requires_attention
        ):
            raise ReconcileValidationError(
                "legacy migration refuses native or non-fail-closed job outcomes"
            )
        migration_created_at = outcome.created_at

        if outcome_record is not None:
            if (
                outcome_record.job_id != self.registry.job_id
                or outcome_record.artifact_role != "job_outcome"
                or outcome_record.artifact_type != "job_outcome"
                or outcome_record.artifact_version != "v1"
                or Path(outcome_record.path).resolve() != outcome_path.resolve()
                or outcome_record.content_hash != file_sha256(outcome_path)
            ):
                raise ReconcileValidationError(
                    "legacy migration outcome Registry content hash mismatch or identity conflict"
                )
            self.validate_record(outcome_record)

        def build_audit(outcome_hash: str) -> AuditRecordV1:
            summary_ref = AuditArtifactRefV1(
                artifact_id="legacy_summary_file",
                artifact_type="legacy_summary_file",
                job_id=self.registry.job_id,
                content_hash=summary_hash,
            )
            outcome_ref = AuditArtifactRefV1(
                artifact_id="job_outcome",
                artifact_type="job_outcome",
                job_id=self.registry.job_id,
                content_hash=outcome_hash,
            )
            return AuditRecordV1.create(
                audit_id=audit_id,
                audit_type="legacy_reuse",
                job_id=self.registry.job_id,
                attempt_id="legacy-migration-v1",
                producer="runtime.reconcile.RuntimeReconciler.migrate_legacy",
                actor=normalized_actor,
                reason=normalized_reason,
                scope={
                    "operation": "legacy_workspace_migration",
                    "source_contract": "summary_only_legacy_workspace",
                    "canonical_upgrade": False,
                },
                target_artifacts=[summary_ref, outcome_ref],
                input_artifact_refs=[summary_ref],
                output_artifact_refs=[outcome_ref],
                input_hashes={"legacy_summary_file": summary_hash},
                policy_snapshot={
                    "provider_calls_allowed": False,
                    "compatibility_status": "legacy_unverified",
                    "canonical_ready": False,
                    "requires_attention": True,
                },
                disposition="migrated_fail_closed",
                created_at=migration_created_at,
            )

        # Validate operator-controlled audit fields before the first durable mutation.
        build_audit("0" * 64)

        if audit_path.is_file():
            if not outcome_path.is_file():
                raise ReconcileValidationError(
                    "legacy migration audit exists without a durable job outcome"
                )
            expected_audit = build_audit(file_sha256(outcome_path))
            try:
                persisted_audit = AuditRecordV1.from_dict(_read_json_object(audit_path))
            except (TypeError, ValueError) as exc:
                raise ReconcileValidationError(f"legacy migration audit is invalid: {exc}") from exc
            if persisted_audit.record_hash != expected_audit.record_hash:
                raise ReconcileValidationError(
                    "legacy migration audit differs from the requested actor or reason"
                )
            if audit_record is not None:
                expected_dependencies = (
                    ArtifactDependencyRefV2(
                        dependency_kind="local_job",
                        job_id=self.registry.job_id,
                        artifact_id="legacy_summary_file",
                        artifact_type="legacy_summary_file",
                        path=str(summary_path.resolve()),
                        content_hash=summary_hash,
                    ),
                    ArtifactDependencyRefV2(
                        dependency_kind="local_job",
                        job_id=self.registry.job_id,
                        artifact_id="job_outcome",
                        artifact_type="job_outcome",
                        path=str(outcome_path.resolve()),
                        content_hash=file_sha256(outcome_path),
                    ),
                )
                if (
                    audit_record.job_id != self.registry.job_id
                    or audit_record.artifact_role != "audit_record"
                    or audit_record.artifact_type != "audit_record"
                    or audit_record.artifact_version != "v1"
                    or Path(audit_record.path).resolve() != audit_path.resolve()
                    or audit_record.content_hash != file_sha256(audit_path)
                    or tuple(audit_record.depends_on) != expected_dependencies
                ):
                    raise ReconcileValidationError(
                        "legacy migration audit Registry record is inconsistent"
                    )
                self.validate_record(audit_record)
        elif audit_record is not None:
            raise ReconcileValidationError(
                "legacy migration audit Registry record has no durable file"
            )

        migrated: list[str] = []
        if summary_record is None:
            summary_record = self.registry.register_file(
                artifact_role="legacy_input",
                artifact_type="legacy_summary_file",
                artifact_version="v1",
                path=summary_path,
                producer="runtime.reconcile.RuntimeReconciler.migrate_legacy",
                artifact_id="legacy_summary_file",
                metadata={
                    "compatibility_status": "legacy_unverified",
                    "canonical_ready": False,
                },
            )
            migrated.append(summary_record.artifact_id)

        if not outcome_path.is_file():
            atomic_write_json(str(outcome_path), outcome.to_dict())
        outcome_repaired, outcome_artifact_id, persisted_outcome = (
            self._repair_outcome_registration()
        )
        if persisted_outcome is None:
            raise ReconcileValidationError("legacy migration could not persist job outcome")
        outcome = persisted_outcome
        if outcome_repaired and outcome_artifact_id:
            migrated.append(outcome_artifact_id)
        outcome_record = self.registry.get("job_outcome")
        if outcome_record is None:
            raise ReconcileValidationError("legacy migration outcome is not registered")

        audit = build_audit(outcome_record.content_hash)
        if audit_path.is_file():
            persisted_audit = AuditRecordV1.from_dict(_read_json_object(audit_path))
            if persisted_audit.record_hash != audit.record_hash:
                raise ReconcileValidationError("legacy migration audit changed after preflight")
        else:
            atomic_write_json(str(audit_path), audit.to_dict())

        dependency_refs = tuple(
            ArtifactDependencyRefV2(
                dependency_kind="local_job",
                job_id=record.job_id,
                artifact_id=record.artifact_id,
                artifact_type=record.artifact_type,
                path=record.path,
                content_hash=record.content_hash,
            )
            for record in (summary_record, outcome_record)
        )
        audit_record = self.registry.get(audit.audit_id)
        if audit_record is None:
            audit_record = self.registry.register_file(
                artifact_role="audit_record",
                artifact_type="audit_record",
                artifact_version="v1",
                path=audit_path,
                producer=audit.producer,
                artifact_id=audit.audit_id,
                depends_on=dependency_refs,
                metadata={
                    "audit_type": audit.audit_type,
                    "record_hash": audit.record_hash,
                    "operation": "legacy_workspace_migration",
                },
            )
            migrated.append(audit_record.artifact_id)
        self.validate_record(audit_record)

        return LegacyMigrationResult(
            job_id=self.registry.job_id,
            legacy_summary_path=str(summary_path.resolve()),
            job_outcome_path=str(outcome_path.resolve()),
            audit_record_path=str(audit_path.resolve()),
            migrated_artifact_ids=tuple(migrated),
            compatibility_status=outcome.compatibility_status,
            canonical_ready=outcome.canonical_ready,
            requires_attention=outcome.requires_attention,
        )

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
        legacy_result = self.legacy_read_only_result()
        if legacy_result is not None:
            return legacy_result

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
