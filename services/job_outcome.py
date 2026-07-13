from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
from types import MappingProxyType
from typing import Any, Iterable, Literal, Mapping, Sequence, Tuple, cast
import uuid

from services.job_workspace import utc_now_iso


JOB_OUTCOME_ARTIFACT_TYPE = "job_outcome"
JOB_OUTCOME_ARTIFACT_VERSION = "v1"
ATTEMPT_ARTIFACT_TYPE = "job_attempt"
ATTEMPT_ARTIFACT_VERSION = "v1"
DEFAULT_READINESS_POLICY_VERSION = "readiness-policy-v1"
LEGACY_READINESS_POLICY_VERSION = "legacy-unverified-v1"

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
CompatibilityStatus = Literal["native", "legacy_unverified"]

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
    compatibility_status: CompatibilityStatus = "native"

    def __post_init__(self) -> None:
        object.__setattr__(self, "readiness_policy_snapshot", _freeze_json(self.readiness_policy_snapshot))
        object.__setattr__(self, "required_stages", _normalized_strings(self.required_stages))
        object.__setattr__(self, "completed_stages", _normalized_strings(self.completed_stages))
        object.__setattr__(self, "degradation_reasons", _normalized_strings(self.degradation_reasons))
        object.__setattr__(self, "failed_stage", str(self.failed_stage).strip() if self.failed_stage else None)
        self.validate()

    @property
    def success(self) -> bool:
        """Legacy projection. Queue lifecycle must use ``job_status`` instead."""

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
        if self.compatibility_status not in {"native", "legacy_unverified"}:
            raise JobOutcomeContractError(f"unsupported compatibility_status: {self.compatibility_status}")
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
        if self.failed_stage and self.failed_stage in self.completed_stages:
            raise JobOutcomeContractError("failed_stage cannot also be completed")
        if self.compatibility_status == "legacy_unverified":
            if self.canonical_ready:
                raise JobOutcomeContractError("legacy-unverified outcomes must fail closed")
            if self.job_disposition != "unvalidated":
                raise JobOutcomeContractError("legacy-unverified outcomes must be unvalidated")

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
            "compatibility_status": self.compatibility_status,
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
            return cls._from_legacy_dict(payload)

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
            compatibility_status=cast(
                CompatibilityStatus,
                str(payload.get("compatibility_status") or "native"),
            ),
        )

    @classmethod
    def _from_legacy_dict(cls, payload: Mapping[str, Any]) -> "JobOutcomeV1":
        legacy_status = str(payload.get("job_status") or payload.get("status") or "pending").lower()
        status_map: Mapping[str, JobStatus] = {
            "pending": "pending",
            "queued": "pending",
            "running": "running",
            "completed": "completed",
            "succeeded": "completed",
            "success": "completed",
            "failed": "failed",
            "error": "failed",
            "cancelled": "cancelled",
            "canceled": "cancelled",
        }
        job_status = status_map.get(legacy_status, "failed")
        now = str(payload.get("updated_at") or payload.get("created_at") or utc_now_iso())
        snapshot = MappingProxyType({"compatibility_mode": "legacy_unverified"})
        policy_hash = build_readiness_policy_hash(LEGACY_READINESS_POLICY_VERSION, snapshot)
        legacy_reasons = [str(item) for item in (payload.get("degradation_reasons") or []) if str(item)]
        if "legacy_unverified" not in legacy_reasons:
            legacy_reasons.append("legacy_unverified")
        return cls(
            artifact_type=JOB_OUTCOME_ARTIFACT_TYPE,
            artifact_version=JOB_OUTCOME_ARTIFACT_VERSION,
            job_id=str(payload.get("job_id") or "legacy-unknown"),
            attempt_number=max(int(payload.get("attempt_number") or 1), 1),
            resumed_from_attempt=_optional_int(payload.get("resumed_from_attempt")),
            job_status=job_status,
            job_disposition="unvalidated",
            canonical_ready=False,
            requires_attention=True,
            created_at=str(payload.get("created_at") or now),
            updated_at=now,
            outcome_revision=max(int(payload.get("outcome_revision") or 1), 1),
            readiness_policy_version=LEGACY_READINESS_POLICY_VERSION,
            readiness_policy_snapshot=snapshot,
            readiness_policy_hash=policy_hash,
            required_stages=tuple(payload.get("required_stages") or ()),
            completed_stages=tuple(payload.get("completed_stages") or ()),
            failed_stage=str(payload.get("failed_stage") or "") or None,
            degradation_reasons=tuple(legacy_reasons),
            compatibility_status="legacy_unverified",
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
            producer=str(payload.get("producer") or "legacy_reader"),
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
