from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterable, Literal, Mapping, Sequence, Tuple, cast
import uuid

from services.artifact_registry import ArtifactRecord, ArtifactRegistry
from services.job_workspace import utc_now_iso


JOB_OUTCOME_ARTIFACT_TYPE = "job_outcome"
JOB_OUTCOME_ARTIFACT_VERSION = "v1"
JOB_OUTCOME_COMPATIBILITY_PROJECTION_ARTIFACT_TYPE = (
    "job_outcome_compatibility_projection"
)
JOB_OUTCOME_COMPATIBILITY_PROJECTION_ARTIFACT_VERSION = "v1"
JOB_OUTCOME_CANONICAL_ARTIFACT_ID = "job_outcome"
ATTEMPT_ARTIFACT_TYPE = "job_attempt"
ATTEMPT_ARTIFACT_VERSION = "v1"
DEFAULT_READINESS_POLICY_VERSION = "readiness-policy-v1"

JobStatus = Literal["pending", "running", "completed", "failed", "cancelled"]
JobDisposition = Literal["clean", "findings", "needs_review", "unvalidated"]
AttemptStatus = Literal[
    "pending",
    "running",
    "succeeded",
    "failed",
    "cancelled",
    "blocked",
    "interrupted",
]

_JOB_STATUSES = frozenset({"pending", "running", "completed", "failed", "cancelled"})
_JOB_DISPOSITIONS = frozenset({"clean", "findings", "needs_review", "unvalidated"})
_ATTEMPT_STATUSES = frozenset(
    {"pending", "running", "succeeded", "failed", "cancelled", "blocked", "interrupted"}
)
_TERMINAL_ATTEMPT_STATUSES = frozenset(
    {"succeeded", "failed", "cancelled", "blocked", "interrupted"}
)
_ATTEMPT_TRANSITIONS: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "pending": frozenset({"running"}),
        "running": _TERMINAL_ATTEMPT_STATUSES,
        "succeeded": frozenset(),
        "failed": frozenset(),
        "cancelled": frozenset(),
        "blocked": frozenset(),
        "interrupted": frozenset(),
    }
)


class JobOutcomeContractError(ValueError):
    """Raised when a job outcome violates the public lifecycle contract."""


class AttemptTransitionError(ValueError):
    """Raised when an attempt history or transition is invalid."""


@dataclass(frozen=True)
class JobOutcomeCompatibilityProjectionV1:
    """Mutable fixed-path pointer to the Registry-owned canonical outcome."""

    artifact_type: str
    artifact_version: str
    job_id: str
    canonical_job_outcome_artifact_id: str
    canonical_job_outcome_artifact_hash: str
    outcome_revision: int
    projection_generation: int
    created_at: str
    producer: str

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        if self.artifact_type != JOB_OUTCOME_COMPATIBILITY_PROJECTION_ARTIFACT_TYPE:
            raise JobOutcomeContractError(
                f"unsupported compatibility projection artifact_type: {self.artifact_type}"
            )
        if self.artifact_version != JOB_OUTCOME_COMPATIBILITY_PROJECTION_ARTIFACT_VERSION:
            raise JobOutcomeContractError(
                f"unsupported compatibility projection artifact_version: {self.artifact_version}"
            )
        if not self.job_id.strip():
            raise JobOutcomeContractError("compatibility projection job_id is required")
        if self.canonical_job_outcome_artifact_id != JOB_OUTCOME_CANONICAL_ARTIFACT_ID:
            raise JobOutcomeContractError(
                "compatibility projection canonical artifact id is invalid"
            )
        artifact_hash = self.canonical_job_outcome_artifact_hash.lower()
        if len(artifact_hash) != 64 or any(char not in "0123456789abcdef" for char in artifact_hash):
            raise JobOutcomeContractError(
                "compatibility projection canonical artifact hash is invalid"
            )
        if self.outcome_revision < 1:
            raise JobOutcomeContractError(
                "compatibility projection outcome_revision must be positive"
            )
        if self.projection_generation < 1:
            raise JobOutcomeContractError(
                "compatibility projection projection_generation must be positive"
            )
        if not self.created_at.strip() or not self.producer.strip():
            raise JobOutcomeContractError(
                "compatibility projection created_at and producer are required"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_type": self.artifact_type,
            "artifact_version": self.artifact_version,
            "job_id": self.job_id,
            "canonical_job_outcome_artifact_id": self.canonical_job_outcome_artifact_id,
            "canonical_job_outcome_artifact_hash": self.canonical_job_outcome_artifact_hash,
            "outcome_revision": self.outcome_revision,
            "projection_generation": self.projection_generation,
            "created_at": self.created_at,
            "producer": self.producer,
        }

    @classmethod
    def create(
        cls,
        *,
        job_id: str,
        canonical_job_outcome_artifact_id: str,
        canonical_job_outcome_artifact_hash: str,
        outcome_revision: int,
        projection_generation: int,
        producer: str,
        created_at: str | None = None,
    ) -> "JobOutcomeCompatibilityProjectionV1":
        return cls(
            artifact_type=JOB_OUTCOME_COMPATIBILITY_PROJECTION_ARTIFACT_TYPE,
            artifact_version=JOB_OUTCOME_COMPATIBILITY_PROJECTION_ARTIFACT_VERSION,
            job_id=job_id,
            canonical_job_outcome_artifact_id=canonical_job_outcome_artifact_id,
            canonical_job_outcome_artifact_hash=canonical_job_outcome_artifact_hash,
            outcome_revision=outcome_revision,
            projection_generation=projection_generation,
            created_at=created_at or utc_now_iso(),
            producer=producer,
        )

    @classmethod
    def from_dict(
        cls,
        payload: Mapping[str, Any],
    ) -> "JobOutcomeCompatibilityProjectionV1":
        return cls(
            artifact_type=str(payload.get("artifact_type") or ""),
            artifact_version=str(payload.get("artifact_version") or ""),
            job_id=str(payload.get("job_id") or ""),
            canonical_job_outcome_artifact_id=str(
                payload.get("canonical_job_outcome_artifact_id") or ""
            ),
            canonical_job_outcome_artifact_hash=str(
                payload.get("canonical_job_outcome_artifact_hash") or ""
            ),
            outcome_revision=int(payload.get("outcome_revision") or 0),
            projection_generation=int(payload.get("projection_generation") or 0),
            created_at=str(payload.get("created_at") or ""),
            producer=str(payload.get("producer") or ""),
        )


@dataclass(frozen=True)
class JobOutcomeCompatibilityProjectionPublishResult:
    written: bool
    projection: JobOutcomeCompatibilityProjectionV1 | None = None
    warning: str = ""


def _freeze_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {str(key): _freeze_json(item) for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise JobOutcomeContractError(f"value is not JSON serializable: {type(value).__name__}")


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


def _canonical_json(payload: Any) -> str:
    try:
        return json.dumps(
            _thaw_json(_freeze_json(payload)),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise JobOutcomeContractError(f"payload is not canonical JSON: {exc}") from exc


def stable_contract_hash(domain: str, payload: Any) -> str:
    """Return a domain-separated SHA-256 over canonical JSON."""

    if not domain.strip():
        raise JobOutcomeContractError("hash domain is required")
    encoded = f"auto-generate\x00{domain}\x00{_canonical_json(payload)}".encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_readiness_policy_hash(policy_version: str, snapshot: Mapping[str, Any]) -> str:
    if not policy_version.strip():
        raise JobOutcomeContractError("readiness policy version is required")
    return stable_contract_hash(
        "readiness-policy",
        {"policy_version": policy_version, "snapshot": dict(snapshot)},
    )


def _normalized_strings(values: Iterable[Any]) -> Tuple[str, ...]:
    result = tuple(str(value).strip() for value in values if str(value).strip())
    if len(set(result)) != len(result):
        raise JobOutcomeContractError("stage and reason collections must not contain duplicates")
    return result


@dataclass(frozen=True)
class JobOutcomeV1:
    artifact_type: str
    artifact_version: str
    job_id: str
    attempt_number: int
    resumed_from_attempt: int | None
    job_status: JobStatus
    job_disposition: JobDisposition
    canonical_ready: bool
    requires_attention: bool
    created_at: str
    updated_at: str
    outcome_revision: int
    readiness_policy_version: str
    readiness_policy_snapshot: Mapping[str, Any]
    readiness_policy_hash: str
    required_stages: Tuple[str, ...]
    completed_stages: Tuple[str, ...]
    failed_stage: str | None
    degradation_reasons: Tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "readiness_policy_snapshot", _freeze_json(self.readiness_policy_snapshot))
        object.__setattr__(self, "required_stages", _normalized_strings(self.required_stages))
        object.__setattr__(self, "completed_stages", _normalized_strings(self.completed_stages))
        object.__setattr__(self, "degradation_reasons", _normalized_strings(self.degradation_reasons))
        object.__setattr__(self, "failed_stage", str(self.failed_stage).strip() if self.failed_stage else None)
        self.validate()

    @property
    def success(self) -> bool:
        """Return whether the canonical outcome is ready for downstream use."""

        return self.canonical_ready

    def validate(self) -> None:
        if self.artifact_type != JOB_OUTCOME_ARTIFACT_TYPE:
            raise JobOutcomeContractError(f"unsupported artifact_type: {self.artifact_type}")
        if self.artifact_version != JOB_OUTCOME_ARTIFACT_VERSION:
            raise JobOutcomeContractError(f"unsupported artifact_version: {self.artifact_version}")
        if not self.job_id.strip():
            raise JobOutcomeContractError("job_id is required")
        if self.attempt_number < 1:
            raise JobOutcomeContractError("attempt_number must be positive")
        if self.resumed_from_attempt is not None:
            if self.resumed_from_attempt < 1 or self.resumed_from_attempt >= self.attempt_number:
                raise JobOutcomeContractError("resumed_from_attempt must refer to an earlier positive attempt")
        if self.job_status not in _JOB_STATUSES:
            raise JobOutcomeContractError(f"unsupported job_status: {self.job_status}")
        if self.job_disposition not in _JOB_DISPOSITIONS:
            raise JobOutcomeContractError(f"unsupported job_disposition: {self.job_disposition}")
        if not self.created_at or not self.updated_at:
            raise JobOutcomeContractError("created_at and updated_at are required")
        if self.outcome_revision < 1:
            raise JobOutcomeContractError("outcome_revision must be positive")
        if not self.readiness_policy_version.strip():
            raise JobOutcomeContractError("readiness_policy_version is required")
        expected_policy_hash = build_readiness_policy_hash(
            self.readiness_policy_version,
            cast(Mapping[str, Any], self.readiness_policy_snapshot),
        )
        if self.readiness_policy_hash != expected_policy_hash:
            raise JobOutcomeContractError("readiness_policy_hash does not match the persisted policy snapshot")
        if self.canonical_ready and self.job_status != "completed":
            raise JobOutcomeContractError("canonical_ready requires job_status=completed")
        if self.canonical_ready and self.job_disposition == "needs_review":
            raise JobOutcomeContractError("needs_review outcomes cannot be canonical-ready")
        if self.job_status in {"failed", "cancelled"} and self.canonical_ready:
            raise JobOutcomeContractError("failed or cancelled jobs cannot be canonical-ready")
        if self.canonical_ready and not set(self.required_stages).issubset(self.completed_stages):
            missing = sorted(set(self.required_stages) - set(self.completed_stages))
            raise JobOutcomeContractError(
                f"canonical_ready requires all required stages to be completed: {missing}"
            )
        if self.failed_stage and self.failed_stage in self.completed_stages:
            raise JobOutcomeContractError("failed_stage cannot also be completed")

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_type": self.artifact_type,
            "artifact_version": self.artifact_version,
            "job_id": self.job_id,
            "attempt_number": self.attempt_number,
            "resumed_from_attempt": self.resumed_from_attempt,
            "job_status": self.job_status,
            "job_disposition": self.job_disposition,
            "canonical_ready": self.canonical_ready,
            "requires_attention": self.requires_attention,
            "success": self.success,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "outcome_revision": self.outcome_revision,
            "readiness_policy_version": self.readiness_policy_version,
            "readiness_policy_snapshot": _thaw_json(self.readiness_policy_snapshot),
            "readiness_policy_hash": self.readiness_policy_hash,
            "required_stages": list(self.required_stages),
            "completed_stages": list(self.completed_stages),
            "failed_stage": self.failed_stage,
            "degradation_reasons": list(self.degradation_reasons),
        }

    @classmethod
    def create(
        cls,
        *,
        job_id: str,
        attempt_number: int,
        job_status: JobStatus,
        job_disposition: JobDisposition,
        canonical_ready: bool,
        requires_attention: bool,
        readiness_policy_snapshot: Mapping[str, Any],
        required_stages: Sequence[str] = (),
        completed_stages: Sequence[str] = (),
        failed_stage: str | None = None,
        degradation_reasons: Sequence[str] = (),
        resumed_from_attempt: int | None = None,
        readiness_policy_version: str = DEFAULT_READINESS_POLICY_VERSION,
        created_at: str | None = None,
        updated_at: str | None = None,
        outcome_revision: int = 1,
    ) -> "JobOutcomeV1":
        now = updated_at or created_at or utc_now_iso()
        policy_hash = build_readiness_policy_hash(readiness_policy_version, readiness_policy_snapshot)
        return cls(
            artifact_type=JOB_OUTCOME_ARTIFACT_TYPE,
            artifact_version=JOB_OUTCOME_ARTIFACT_VERSION,
            job_id=job_id,
            attempt_number=attempt_number,
            resumed_from_attempt=resumed_from_attempt,
            job_status=job_status,
            job_disposition=job_disposition,
            canonical_ready=canonical_ready,
            requires_attention=requires_attention,
            created_at=created_at or now,
            updated_at=now,
            outcome_revision=outcome_revision,
            readiness_policy_version=readiness_policy_version,
            readiness_policy_snapshot=readiness_policy_snapshot,
            readiness_policy_hash=policy_hash,
            required_stages=tuple(required_stages),
            completed_stages=tuple(completed_stages),
            failed_stage=failed_stage,
            degradation_reasons=tuple(degradation_reasons),
        )

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "JobOutcomeV1":
        native_keys = {
            "job_status",
            "job_disposition",
            "canonical_ready",
            "readiness_policy_version",
            "readiness_policy_snapshot",
        }
        if not native_keys.issubset(payload.keys()):
            raise JobOutcomeContractError("job outcome is missing the current readiness contract")

        policy_version = str(payload.get("readiness_policy_version") or DEFAULT_READINESS_POLICY_VERSION)
        policy_snapshot = dict(payload.get("readiness_policy_snapshot") or {})
        policy_hash = str(payload.get("readiness_policy_hash") or "")
        if not policy_hash:
            policy_hash = build_readiness_policy_hash(policy_version, policy_snapshot)
        return cls(
            artifact_type=str(payload.get("artifact_type") or JOB_OUTCOME_ARTIFACT_TYPE),
            artifact_version=str(payload.get("artifact_version") or JOB_OUTCOME_ARTIFACT_VERSION),
            job_id=str(payload.get("job_id") or ""),
            attempt_number=int(payload.get("attempt_number") or 1),
            resumed_from_attempt=_optional_int(payload.get("resumed_from_attempt")),
            job_status=cast(JobStatus, str(payload.get("job_status") or "pending")),
            job_disposition=cast(JobDisposition, str(payload.get("job_disposition") or "unvalidated")),
            canonical_ready=bool(payload.get("canonical_ready", False)),
            requires_attention=bool(payload.get("requires_attention", False)),
            created_at=str(payload.get("created_at") or utc_now_iso()),
            updated_at=str(payload.get("updated_at") or payload.get("created_at") or utc_now_iso()),
            outcome_revision=int(payload.get("outcome_revision") or 1),
            readiness_policy_version=policy_version,
            readiness_policy_snapshot=policy_snapshot,
            readiness_policy_hash=policy_hash,
            required_stages=tuple(payload.get("required_stages") or ()),
            completed_stages=tuple(payload.get("completed_stages") or ()),
            failed_stage=str(payload.get("failed_stage") or "") or None,
            degradation_reasons=tuple(payload.get("degradation_reasons") or ()),
        )


def load_canonical_job_outcome(
    registry: ArtifactRegistry,
) -> tuple[JobOutcomeV1, ArtifactRecord]:
    """Load the current JobOutcome exclusively through its Registry identity."""

    try:
        registry.reload()
        record = registry.get(JOB_OUTCOME_CANONICAL_ARTIFACT_ID)
    except Exception as exc:
        raise JobOutcomeContractError(f"cannot read canonical job outcome Registry state: {exc}") from exc
    if record is None:
        raise JobOutcomeContractError("canonical job outcome is not registered")
    if (
        record.artifact_id != JOB_OUTCOME_CANONICAL_ARTIFACT_ID
        or record.artifact_role != JOB_OUTCOME_ARTIFACT_TYPE
        or record.artifact_type != JOB_OUTCOME_ARTIFACT_TYPE
        or record.artifact_version != JOB_OUTCOME_ARTIFACT_VERSION
        or record.job_id != registry.job_id
        or record.status != "ready"
    ):
        raise JobOutcomeContractError("canonical job outcome Registry identity is invalid")
    try:
        ArtifactRegistry._verify_ready_artifact(record)
        path = Path(record.path)
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise JobOutcomeContractError(f"canonical job outcome failed hash verification: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise JobOutcomeContractError("canonical job outcome must be a JSON object")
    outcome = JobOutcomeV1.from_dict(payload)
    if outcome.job_id != registry.job_id:
        raise JobOutcomeContractError("canonical job outcome belongs to another job")
    metadata_checks = {
        "job_status": outcome.job_status,
        "job_disposition": outcome.job_disposition,
        "canonical_ready": outcome.canonical_ready,
        "requires_attention": outcome.requires_attention,
        "outcome_revision": outcome.outcome_revision,
    }
    mismatches = [
        field
        for field, expected in metadata_checks.items()
        if field in record.metadata and record.metadata[field] != expected
    ]
    if mismatches:
        raise JobOutcomeContractError(
            "canonical job outcome Registry metadata is inconsistent: "
            + ", ".join(mismatches)
        )
    return outcome, record


def _read_job_outcome_compatibility_projection(
    path: str | Path,
) -> JobOutcomeCompatibilityProjectionV1:
    target = Path(path)
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise JobOutcomeContractError(
            f"job outcome compatibility projection is invalid: {exc}"
        ) from exc
    if not isinstance(payload, Mapping):
        raise JobOutcomeContractError(
            "job outcome compatibility projection must be a JSON object"
        )
    return JobOutcomeCompatibilityProjectionV1.from_dict(payload)


def validate_job_outcome_compatibility_projection(
    path: str | Path,
    registry: ArtifactRegistry,
) -> JobOutcomeCompatibilityProjectionV1:
    """Verify a fixed-path projection against the current Registry ID and hash."""

    projection = _read_job_outcome_compatibility_projection(path)
    outcome, canonical_record = load_canonical_job_outcome(registry)
    mismatches: list[str] = []
    if projection.job_id != canonical_record.job_id:
        mismatches.append("job_id")
    if projection.canonical_job_outcome_artifact_id != canonical_record.artifact_id:
        mismatches.append("canonical_job_outcome_artifact_id")
    if projection.canonical_job_outcome_artifact_hash != canonical_record.content_hash:
        mismatches.append("canonical_job_outcome_artifact_hash")
    if projection.outcome_revision != outcome.outcome_revision:
        mismatches.append("outcome_revision")
    if mismatches:
        raise JobOutcomeContractError(
            "job outcome compatibility projection does not match the Registry head: "
            + ", ".join(mismatches)
        )
    return projection


def publish_job_outcome_compatibility_projection(
    *,
    path: str | Path,
    registry: ArtifactRegistry,
    canonical_record: ArtifactRecord,
    outcome: JobOutcomeV1,
    producer: str,
    publication_context: Any | None = None,
) -> JobOutcomeCompatibilityProjectionPublishResult:
    """Best-effort fixed-path publication after the canonical Registry commit."""

    target = Path(path).expanduser().resolve()
    try:
        current_outcome, current_record = load_canonical_job_outcome(registry)
        expected_identity = (
            canonical_record.artifact_id,
            canonical_record.path,
            canonical_record.content_hash,
        )
        current_identity = (
            current_record.artifact_id,
            current_record.path,
            current_record.content_hash,
        )
        if expected_identity != current_identity or current_outcome != outcome:
            raise JobOutcomeContractError(
                "canonical job outcome changed before compatibility projection publication"
            )
        if Path(current_record.path).resolve() == target:
            raise JobOutcomeContractError(
                "compatibility projection path cannot replace the canonical job outcome"
            )

        previous_generation = 0
        if target.is_file():
            try:
                previous = _read_job_outcome_compatibility_projection(target)
            except (JobOutcomeContractError, TypeError, ValueError):
                previous = None
            if previous is not None and previous.job_id == outcome.job_id:
                previous_generation = previous.projection_generation
        projection = JobOutcomeCompatibilityProjectionV1.create(
            job_id=outcome.job_id,
            canonical_job_outcome_artifact_id=current_record.artifact_id,
            canonical_job_outcome_artifact_hash=current_record.content_hash,
            outcome_revision=outcome.outcome_revision,
            projection_generation=max(
                outcome.outcome_revision,
                previous_generation + 1,
            ),
            producer=producer,
        )
        active_context = publication_context
        if active_context is None:
            from services.queue_service import LocalPublicationContext

            active_context = LocalPublicationContext()
        writer = getattr(active_context, "write_compatibility_json", None)
        if not callable(writer):
            raise JobOutcomeContractError(
                "publication context has no compatibility projection writer"
            )
        writer(target, projection.to_dict())
        persisted = validate_job_outcome_compatibility_projection(target, registry)
        if persisted != projection:
            raise JobOutcomeContractError(
                "job outcome compatibility projection readback changed"
            )
        return JobOutcomeCompatibilityProjectionPublishResult(
            written=True,
            projection=projection,
        )
    except Exception as exc:
        return JobOutcomeCompatibilityProjectionPublishResult(
            written=False,
            warning=f"job outcome compatibility projection was not updated: {exc}",
        )


def _optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    return int(value)


@dataclass(frozen=True)
class AttemptV1:
    artifact_type: str
    artifact_version: str
    attempt_id: str
    job_id: str
    attempt_number: int
    resumed_from_attempt: int | None
    status: AttemptStatus
    producer: str
    created_at: str
    updated_at: str
    started_at: str | None = None
    finished_at: str | None = None
    terminal_reason: str = ""

    def __post_init__(self) -> None:
        self.validate()

    @property
    def is_terminal(self) -> bool:
        return self.status in _TERMINAL_ATTEMPT_STATUSES

    def validate(self) -> None:
        if self.artifact_type != ATTEMPT_ARTIFACT_TYPE:
            raise AttemptTransitionError(f"unsupported artifact_type: {self.artifact_type}")
        if self.artifact_version != ATTEMPT_ARTIFACT_VERSION:
            raise AttemptTransitionError(f"unsupported artifact_version: {self.artifact_version}")
        if not self.attempt_id.strip() or not self.job_id.strip() or not self.producer.strip():
            raise AttemptTransitionError("attempt_id, job_id, and producer are required")
        if self.attempt_number < 1:
            raise AttemptTransitionError("attempt_number must be positive")
        if self.resumed_from_attempt is not None:
            if self.resumed_from_attempt < 1 or self.resumed_from_attempt >= self.attempt_number:
                raise AttemptTransitionError("resumed_from_attempt must refer to an earlier positive attempt")
        if self.status not in _ATTEMPT_STATUSES:
            raise AttemptTransitionError(f"unsupported attempt status: {self.status}")
        if not self.created_at or not self.updated_at:
            raise AttemptTransitionError("created_at and updated_at are required")
        if self.status == "pending" and (self.started_at or self.finished_at):
            raise AttemptTransitionError("pending attempts cannot have start or finish timestamps")
        if self.status == "running" and (not self.started_at or self.finished_at):
            raise AttemptTransitionError("running attempts require started_at and cannot have finished_at")
        if self.is_terminal and (not self.started_at or not self.finished_at):
            raise AttemptTransitionError("terminal attempts require start and finish timestamps")

    @classmethod
    def new_pending(
        cls,
        *,
        job_id: str,
        attempt_number: int,
        producer: str,
        resumed_from_attempt: int | None = None,
        attempt_id: str | None = None,
        created_at: str | None = None,
    ) -> "AttemptV1":
        now = created_at or utc_now_iso()
        return cls(
            artifact_type=ATTEMPT_ARTIFACT_TYPE,
            artifact_version=ATTEMPT_ARTIFACT_VERSION,
            attempt_id=attempt_id or f"attempt-{uuid.uuid4().hex}",
            job_id=job_id,
            attempt_number=attempt_number,
            resumed_from_attempt=resumed_from_attempt,
            status="pending",
            producer=producer,
            created_at=now,
            updated_at=now,
        )

    def transition(
        self,
        target_status: AttemptStatus,
        *,
        at: str | None = None,
        reason: str = "",
    ) -> "AttemptV1":
        allowed = _ATTEMPT_TRANSITIONS[self.status]
        if target_status not in allowed:
            raise AttemptTransitionError(f"illegal attempt transition: {self.status} -> {target_status}")
        now = at or utc_now_iso()
        if self.status == "pending":
            return replace(
                self,
                status=target_status,
                updated_at=now,
                started_at=now,
                terminal_reason="",
            )
        return replace(
            self,
            status=target_status,
            updated_at=now,
            finished_at=now,
            terminal_reason=reason.strip(),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_type": self.artifact_type,
            "artifact_version": self.artifact_version,
            "attempt_id": self.attempt_id,
            "job_id": self.job_id,
            "attempt_number": self.attempt_number,
            "resumed_from_attempt": self.resumed_from_attempt,
            "status": self.status,
            "producer": self.producer,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "terminal_reason": self.terminal_reason,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "AttemptV1":
        raw_status = str(payload.get("status") or "pending").lower()
        status_aliases: Mapping[str, AttemptStatus] = {
            "pending": "pending",
            "running": "running",
            "succeeded": "succeeded",
            "completed": "succeeded",
            "success": "succeeded",
            "failed": "failed",
            "cancelled": "cancelled",
            "canceled": "cancelled",
            "blocked": "blocked",
            "interrupted": "interrupted",
        }
        status = status_aliases.get(raw_status)
        if status is None:
            raise AttemptTransitionError(f"unsupported attempt status: {raw_status}")
        created_at = str(payload.get("created_at") or utc_now_iso())
        started_at = str(payload.get("started_at") or "") or None
        finished_at = str(payload.get("finished_at") or "") or None
        if status != "pending" and started_at is None:
            started_at = created_at
        if status in _TERMINAL_ATTEMPT_STATUSES and finished_at is None:
            finished_at = str(payload.get("updated_at") or created_at)
        return cls(
            artifact_type=str(payload.get("artifact_type") or ATTEMPT_ARTIFACT_TYPE),
            artifact_version=str(payload.get("artifact_version") or ATTEMPT_ARTIFACT_VERSION),
            attempt_id=str(payload.get("attempt_id") or ""),
            job_id=str(payload.get("job_id") or ""),
            attempt_number=int(payload.get("attempt_number") or 1),
            resumed_from_attempt=_optional_int(payload.get("resumed_from_attempt")),
            status=status,
            producer=str(payload.get("producer") or "runtime"),
            created_at=created_at,
            updated_at=str(payload.get("updated_at") or created_at),
            started_at=started_at,
            finished_at=finished_at,
            terminal_reason=str(payload.get("terminal_reason") or payload.get("reason") or ""),
        )


def interrupt_stale_running_and_start_next(
    stale_attempt: AttemptV1,
    *,
    producer: str,
    at: str | None = None,
    new_attempt_id: str | None = None,
    reason: str = "stale running attempt recovered",
) -> tuple[AttemptV1, AttemptV1]:
    if stale_attempt.status != "running":
        raise AttemptTransitionError("only a running attempt can be recovered as interrupted")
    now = at or utc_now_iso()
    interrupted = stale_attempt.transition("interrupted", at=now, reason=reason)
    resumed = AttemptV1.new_pending(
        job_id=stale_attempt.job_id,
        attempt_number=stale_attempt.attempt_number + 1,
        producer=producer,
        resumed_from_attempt=stale_attempt.attempt_number,
        attempt_id=new_attempt_id,
        created_at=now,
    )
    return interrupted, resumed


def append_attempt_snapshot(
    history: Sequence[AttemptV1],
    snapshot: AttemptV1,
) -> tuple[AttemptV1, ...]:
    """Validate and append an immutable attempt snapshot to an append-only history."""

    prior = tuple(history)
    if not prior:
        if snapshot.status != "pending" or snapshot.attempt_number != 1:
            raise AttemptTransitionError("attempt history must begin with pending attempt 1")
        return (snapshot,)

    last = prior[-1]
    if snapshot.job_id != last.job_id:
        raise AttemptTransitionError("attempt history cannot mix job IDs")
    if snapshot.attempt_id == last.attempt_id:
        if snapshot.attempt_number != last.attempt_number:
            raise AttemptTransitionError("an attempt ID cannot change its attempt number")
        if snapshot.status not in _ATTEMPT_TRANSITIONS[last.status]:
            raise AttemptTransitionError(f"illegal appended transition: {last.status} -> {snapshot.status}")
        immutable_fields = ("created_at", "producer", "resumed_from_attempt")
        if any(getattr(snapshot, name) != getattr(last, name) for name in immutable_fields):
            raise AttemptTransitionError("append-only transition changed immutable attempt fields")
        if last.status == "running" and snapshot.started_at != last.started_at:
            raise AttemptTransitionError("append-only terminal transition changed started_at")
        return prior + (snapshot,)

    seen_ids = {item.attempt_id for item in prior}
    if snapshot.attempt_id in seen_ids:
        raise AttemptTransitionError("an earlier attempt ID cannot reappear")
    if not last.is_terminal:
        raise AttemptTransitionError("a new attempt requires a terminal previous attempt")
    if snapshot.status != "pending":
        raise AttemptTransitionError("a new attempt must start pending")
    if snapshot.attempt_number != last.attempt_number + 1:
        raise AttemptTransitionError("attempt numbers must increase by one")
    if snapshot.resumed_from_attempt != last.attempt_number:
        raise AttemptTransitionError("new attempts must identify the immediately preceding attempt")
    return prior + (snapshot,)
