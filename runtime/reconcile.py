from __future__ import annotations

from dataclasses import dataclass
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


def _validate_job_outcome(_record: ArtifactRecord, path: Path) -> None:
    JobOutcomeV1.from_dict(_read_json_object(path))


def _validate_stage_terminal(_record: ArtifactRecord, path: Path) -> None:
    TerminalStageRecordV1.from_dict(_read_json_object(path))


def _validate_source_bundle(_record: ArtifactRecord, path: Path) -> None:
    payload = _read_json_object(path)
    required = {"source_mode", "project_name", "paper_work_items", "source_snapshot"}
    if not required.issubset(payload):
        raise ReconcileValidationError(f"source bundle is missing fields: {sorted(required - payload.keys())}")
    if not isinstance(payload["paper_work_items"], list) or not isinstance(payload["source_snapshot"], dict):
        raise ReconcileValidationError("source bundle collections have invalid types")


def _validate_summary_file(_record: ArtifactRecord, path: Path) -> None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReconcileValidationError(f"invalid summary JSON {path}: {exc}") from exc
    if not isinstance(payload, list):
        raise ReconcileValidationError("summary_file must contain a JSON array")
    if any(not isinstance(item, dict) for item in payload):
        raise ReconcileValidationError("summary_file entries must be JSON objects")


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


def _validate_json_object(_record: ArtifactRecord, path: Path) -> None:
    _read_json_object(path)


def _validate_json_array(_record: ArtifactRecord, path: Path) -> None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReconcileValidationError(f"invalid JSON {path}: {exc}") from exc
    if not isinstance(payload, list):
        raise ReconcileValidationError(f"JSON artifact must be an array: {path}")


DEFAULT_SCHEMA_VALIDATORS: Mapping[str, SchemaValidator] = {
    "job_outcome": _validate_job_outcome,
    STAGE_TERMINAL_ARTIFACT_TYPE: _validate_stage_terminal,
    "source_bundle": _validate_source_bundle,
    "summary_file": _validate_summary_file,
    "literature_review_outline": _validate_nonempty_text,
    "review_docx": _validate_docx,
    "source_pdf": _validate_pdf,
    "validation_run_result": _validate_validation_run_result,
    "runtime_job_spec": _validate_json_object,
    "stage1_progress_snapshot": _validate_json_object,
    "summary_source_manifest": _validate_json_object,
    "paper_artifact": _validate_json_object,
    "evidence_manifest": _validate_json_object,
    "normalized_text": _validate_nonempty_text,
    "chunks": _validate_json_array,
    "page_index": _validate_json_array,
    "review_draft": _validate_json_object,
    "citation_manifest": _validate_json_object,
    "citation_ref_catalog": _validate_json_object,
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

    def _registry_for_ref(self, ref: ArtifactDependencyRefV2) -> ArtifactRegistry:
        if ref.dependency_kind == "local_job":
            if ref.job_id and ref.job_id != self.registry.job_id:
                raise ReconcileValidationError(
                    f"local dependency {ref.artifact_id!r} names another job: {ref.job_id}"
                )
            return self.registry
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
    ) -> ArtifactRecord:
        target_registry = self._registry_for_ref(ref)
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
                self.validate_dependency_ref(dependency, visited=active_visited)
        finally:
            active_visited.remove(key)

    def stage_is_complete(self, stage_name: str) -> bool:
        candidates = [
            (record, path)
            for record, path in self.stage_store.load_records()
            if record.stage_name == stage_name and record.status == "succeeded"
        ]
        for record, _path in sorted(candidates, key=lambda item: item[0].finished_at, reverse=True):
            registered = self.registry.get(record.record_id)
            if registered is None:
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
        current = self.registry.get("job_outcome")
        if current is not None and current.content_hash == synthetic.content_hash:
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
                "outcome_revision": outcome.outcome_revision,
            },
        )
        return True, "job_outcome", outcome

    def _repair_terminal_registration(
        self,
        record: TerminalStageRecordV1,
        path: Path,
    ) -> bool:
        for output_ref in record.output_artifact_refs:
            self.validate_dependency_ref(output_ref)
        current = self.registry.get(record.record_id)
        actual_hash = file_sha256(path)
        if current is not None and current.content_hash == actual_hash:
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

    def _repair_owned_pointer(self, outcome: JobOutcomeV1 | None) -> bool:
        if outcome is None:
            return False
        pointer_path = Path(str(getattr(self.workspace, "latest_pointer_path")()))
        try:
            payload = json.loads(pointer_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return False
        if not isinstance(payload, dict) or str(payload.get("job_id") or "") != self.registry.job_id:
            return False
        resume_state = str(payload.get("resume_state") or "non_resumable")
        fingerprint_bundle = payload.get("fingerprint_bundle")
        if not isinstance(fingerprint_bundle, dict):
            return False
        return bool(
            getattr(self.workspace, "write_latest_pointer_if_owned")(
                resume_state=resume_state,
                fingerprint_bundle=fingerprint_bundle,
                status=outcome.job_status,
            )
        )

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

        for recovery in stage_recoveries:
            if self.stage_is_complete(recovery.stage_name):
                continue
            try:
                record = self._reconstruct_stage(recovery)
                reconstructed.append(record.record_id)
            except (TypeError, ValueError) as exc:
                issues.append(
                    ReconcileIssue(
                        "stage_reconstruction_not_proven",
                        str(exc),
                        stage_name=recovery.stage_name,
                    )
                )

        stage_names = {
            record.stage_name for record, _path in self.stage_store.load_records()
        } if self.stage_store.directory.exists() else set()
        completed = tuple(sorted(stage for stage in stage_names if self.stage_is_complete(stage)))
        pointer_repaired = self._repair_owned_pointer(outcome)
        return ReconcileResult(
            job_id=self.registry.job_id,
            completed_stages=completed,
            repaired_artifact_ids=tuple(dict.fromkeys(repaired)),
            reconstructed_stage_records=tuple(reconstructed),
            outcome_repaired=outcome_repaired,
            pointer_repaired=pointer_repaired,
            issues=tuple(issues),
        )
