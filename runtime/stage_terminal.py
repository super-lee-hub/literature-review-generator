from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Iterable, Literal, Mapping, Sequence, cast
import uuid

from runtime.attempt_store import _write_json_exclusive
from services.artifact_registry import ArtifactDependencyRefV2, ArtifactRecord, ArtifactRegistry
from services.job_workspace import utc_now_iso


STAGE_TERMINAL_ARTIFACT_TYPE = "runtime_stage_terminal"
STAGE_TERMINAL_ARTIFACT_VERSION = "v1"
STAGE_TERMINAL_ROLE = "runtime_stage_terminal"
STAGE_TERMINAL_DIR = "runtime_stage_terminals"

TerminalStageStatus = Literal["succeeded", "failed", "cancelled", "blocked"]
_TERMINAL_STATUSES = frozenset({"succeeded", "failed", "cancelled", "blocked"})

_STAGE_OUTPUT_CONTRACTS: Mapping[str, tuple[frozenset[str], ...]] = {
    "source_intake": (frozenset({"source_bundle"}),),
    "analyze": (frozenset({"summary_file"}),),
    "derive_review_batch": (frozenset({"review_batch_manifest"}),),
    "outline": (
        frozenset({"adopted_final_outline"}),
        frozenset({"literature_review_outline"}),
    ),
    "review": (
        frozenset({"review_draft", "citation_manifest", "review_docx"}),
    ),
    "validate": (frozenset({"validation_run_result"}),),
}


class StageTerminalContractError(ValueError):
    pass


def validate_stage_output_contract(
    stage_name: str,
    output_artifact_refs: Sequence[ArtifactDependencyRefV2],
) -> None:
    """Require the canonical durable outputs for known runtime stages."""

    alternatives = _STAGE_OUTPUT_CONTRACTS.get(stage_name)
    if alternatives is None:
        return
    artifact_types = {ref.artifact_type for ref in output_artifact_refs}
    if any(required.issubset(artifact_types) for required in alternatives):
        return
    expected = " or ".join(
        "+".join(sorted(required)) for required in alternatives
    )
    raise StageTerminalContractError(
        f"succeeded stage {stage_name!r} requires canonical outputs: {expected}"
    )


def _normalize_refs(
    refs: Iterable[ArtifactDependencyRefV2 | Mapping[str, Any]],
    *,
    default_job_id: str,
) -> tuple[ArtifactDependencyRefV2, ...]:
    normalized: list[ArtifactDependencyRefV2] = []
    for ref in refs:
        value = (
            ref
            if isinstance(ref, ArtifactDependencyRefV2)
            else ArtifactDependencyRefV2.from_dict(ref, default_job_id=default_job_id)
        )
        normalized.append(value)
    return tuple(normalized)


@dataclass(frozen=True)
class TerminalStageRecordV1:
    artifact_type: str
    artifact_version: str
    record_id: str
    job_id: str
    attempt_id: str
    stage_name: str
    status: TerminalStageStatus
    producer: str
    input_artifact_refs: tuple[ArtifactDependencyRefV2, ...]
    output_artifact_refs: tuple[ArtifactDependencyRefV2, ...]
    model_call_count: int
    started_at: str
    finished_at: str
    terminal_reason: str = ""
    reconstructed_by_reconcile: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "input_artifact_refs",
            _normalize_refs(self.input_artifact_refs, default_job_id=self.job_id),
        )
        object.__setattr__(
            self,
            "output_artifact_refs",
            _normalize_refs(self.output_artifact_refs, default_job_id=self.job_id),
        )
        self.validate()

    def validate(self) -> None:
        if self.artifact_type != STAGE_TERMINAL_ARTIFACT_TYPE:
            raise StageTerminalContractError(f"unsupported artifact_type: {self.artifact_type}")
        if self.artifact_version != STAGE_TERMINAL_ARTIFACT_VERSION:
            raise StageTerminalContractError(f"unsupported artifact_version: {self.artifact_version}")
        required = (self.record_id, self.job_id, self.attempt_id, self.stage_name, self.producer)
        if any(not value.strip() for value in required):
            raise StageTerminalContractError(
                "record_id, job_id, attempt_id, stage_name, and producer are required"
            )
        if self.status not in _TERMINAL_STATUSES:
            raise StageTerminalContractError(f"unsupported terminal stage status: {self.status}")
        if self.model_call_count < 0:
            raise StageTerminalContractError("model_call_count cannot be negative")
        if not self.started_at or not self.finished_at:
            raise StageTerminalContractError("started_at and finished_at are required")
        if self.status == "succeeded" and not self.output_artifact_refs:
            raise StageTerminalContractError("a succeeded stage requires at least one output artifact")
        if self.status == "succeeded":
            validate_stage_output_contract(self.stage_name, self.output_artifact_refs)
        identities = [
            (ref.dependency_kind, ref.job_id, ref.artifact_id, ref.content_hash)
            for ref in self.output_artifact_refs
        ]
        if len(set(identities)) != len(identities):
            raise StageTerminalContractError("output artifact references must be unique")

    @classmethod
    def create(
        cls,
        *,
        job_id: str,
        attempt_id: str,
        stage_name: str,
        status: TerminalStageStatus,
        producer: str,
        output_artifact_refs: Sequence[ArtifactDependencyRefV2 | Mapping[str, Any]],
        input_artifact_refs: Sequence[ArtifactDependencyRefV2 | Mapping[str, Any]] = (),
        model_call_count: int = 0,
        started_at: str | None = None,
        finished_at: str | None = None,
        terminal_reason: str = "",
        reconstructed_by_reconcile: bool = False,
        record_id: str | None = None,
    ) -> "TerminalStageRecordV1":
        now = finished_at or utc_now_iso()
        return cls(
            artifact_type=STAGE_TERMINAL_ARTIFACT_TYPE,
            artifact_version=STAGE_TERMINAL_ARTIFACT_VERSION,
            record_id=record_id or f"stage-terminal-{uuid.uuid4().hex}",
            job_id=job_id,
            attempt_id=attempt_id,
            stage_name=stage_name,
            status=status,
            producer=producer,
            input_artifact_refs=_normalize_refs(input_artifact_refs, default_job_id=job_id),
            output_artifact_refs=_normalize_refs(output_artifact_refs, default_job_id=job_id),
            model_call_count=model_call_count,
            started_at=started_at or now,
            finished_at=now,
            terminal_reason=terminal_reason.strip(),
            reconstructed_by_reconcile=reconstructed_by_reconcile,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_type": self.artifact_type,
            "artifact_version": self.artifact_version,
            "record_id": self.record_id,
            "job_id": self.job_id,
            "attempt_id": self.attempt_id,
            "stage_name": self.stage_name,
            "status": self.status,
            "producer": self.producer,
            "input_artifact_refs": [ref.to_dict() for ref in self.input_artifact_refs],
            "output_artifact_refs": [ref.to_dict() for ref in self.output_artifact_refs],
            "model_call_count": self.model_call_count,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "terminal_reason": self.terminal_reason,
            "reconstructed_by_reconcile": self.reconstructed_by_reconcile,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "TerminalStageRecordV1":
        job_id = str(payload.get("job_id") or "")
        return cls(
            artifact_type=str(payload.get("artifact_type") or ""),
            artifact_version=str(payload.get("artifact_version") or ""),
            record_id=str(payload.get("record_id") or ""),
            job_id=job_id,
            attempt_id=str(payload.get("attempt_id") or ""),
            stage_name=str(payload.get("stage_name") or ""),
            status=cast(TerminalStageStatus, str(payload.get("status") or "")),
            producer=str(payload.get("producer") or ""),
            input_artifact_refs=_normalize_refs(
                payload.get("input_artifact_refs") or (), default_job_id=job_id
            ),
            output_artifact_refs=_normalize_refs(
                payload.get("output_artifact_refs") or (), default_job_id=job_id
            ),
            model_call_count=int(payload.get("model_call_count") or 0),
            started_at=str(payload.get("started_at") or ""),
            finished_at=str(payload.get("finished_at") or ""),
            terminal_reason=str(payload.get("terminal_reason") or ""),
            reconstructed_by_reconcile=bool(payload.get("reconstructed_by_reconcile", False)),
        )


class StageTerminalStore:
    def __init__(self, workspace: object, registry: ArtifactRegistry) -> None:
        self.workspace = workspace
        self.registry = registry
        self.directory = Path(str(getattr(workspace, "artifact_path")(STAGE_TERMINAL_DIR)))

    def path_for(self, record: TerminalStageRecordV1) -> Path:
        safe_stage = "".join(char if char.isalnum() or char in "-_" else "_" for char in record.stage_name)
        return self.directory / safe_stage / f"{record.record_id}.json"

    def persist(self, record: TerminalStageRecordV1) -> ArtifactRecord:
        record.validate()
        if record.job_id != self.registry.job_id:
            raise StageTerminalContractError("stage terminal job_id does not match Registry owner")
        path = self.path_for(record)
        _write_json_exclusive(path, record.to_dict())
        return self.registry.register_file(
            artifact_role=STAGE_TERMINAL_ROLE,
            artifact_type=STAGE_TERMINAL_ARTIFACT_TYPE,
            artifact_version=STAGE_TERMINAL_ARTIFACT_VERSION,
            path=path,
            producer="runtime.stage_terminal.StageTerminalStore",
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

    def load_records(self) -> tuple[tuple[TerminalStageRecordV1, Path], ...]:
        if not self.directory.exists():
            return ()
        records: list[tuple[TerminalStageRecordV1, Path]] = []
        for path in sorted(self.directory.glob("*/*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise StageTerminalContractError(f"cannot read stage terminal {path}: {exc}") from exc
            if not isinstance(payload, dict):
                raise StageTerminalContractError(f"stage terminal must be an object: {path}")
            record = TerminalStageRecordV1.from_dict(payload)
            if record.job_id != self.registry.job_id:
                raise StageTerminalContractError(
                    f"stage terminal job_id does not match Registry owner: {path}"
                )
            if path != self.path_for(record):
                raise StageTerminalContractError(f"stage terminal path does not match record identity: {path}")
            records.append((record, path))
        return tuple(records)
