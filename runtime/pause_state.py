"""Durable user pause state for provider admission.

Pause is intentionally separate from cancellation.  A paused job keeps its
durable artifacts and in-flight evidence; it simply refuses any new provider
admission until an explicit resume clears the marker.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path
from typing import Any, Mapping

from services.artifact_registry import ArtifactRecord, ArtifactRegistry, file_sha256
from services.job_workspace import JobWorkspace, atomic_write_json, utc_now_iso


PAUSE_STATE_ARTIFACT_TYPE = "pause_state"
PAUSE_STATE_ARTIFACT_VERSION = "v1"
PAUSED_BY_USER = "PAUSED_BY_USER"
RUNNABLE = "RUNNABLE"


class PauseRequestedError(RuntimeError):
    """Raised at a safe boundary when a paused job would start new work."""


@dataclass(frozen=True)
class PauseStateV1:
    state_id: str
    job_id: str
    state: str = RUNNABLE
    requested_at: str = ""
    requested_by: str = ""
    reason: str = ""
    checkpoint: str = "provider_admission"
    in_flight_policy: str = "retain_as_in_flight_or_unknown"

    @property
    def paused(self) -> bool:
        return self.state == PAUSED_BY_USER

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "PauseStateV1":
        state = str(payload.get("state") or RUNNABLE)
        if state not in {RUNNABLE, PAUSED_BY_USER}:
            raise ValueError(f"unsupported pause state: {state}")
        return cls(
            state_id=str(payload.get("state_id") or ""),
            job_id=str(payload.get("job_id") or ""),
            state=state,
            requested_at=str(payload.get("requested_at") or ""),
            requested_by=str(payload.get("requested_by") or ""),
            reason=str(payload.get("reason") or ""),
            checkpoint=str(payload.get("checkpoint") or "provider_admission"),
            in_flight_policy=str(payload.get("in_flight_policy") or "retain_as_in_flight_or_unknown"),
        )


class PauseStateStore:
    """Read/write a hash-registered pause marker for one durable workspace."""

    def __init__(self, workspace: JobWorkspace, registry: ArtifactRegistry | None = None) -> None:
        self.workspace = workspace
        self.registry = registry
        self.path = Path(workspace.artifact_path(f"pause_state/{workspace.job_id}.json"))
        self.artifact_id = f"pause_state:{workspace.job_id}"

    def read(self) -> PauseStateV1 | None:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise PauseRequestedError(
                f"CONTROL_STATE_INVALID: pause marker cannot be read: {type(exc).__name__}"
            ) from exc
        if not isinstance(payload, Mapping):
            raise PauseRequestedError("CONTROL_STATE_INVALID: pause marker must be a JSON object")
        try:
            state = PauseStateV1.from_dict(payload)
        except (TypeError, ValueError) as exc:
            raise PauseRequestedError(f"CONTROL_STATE_INVALID: {exc}") from exc
        if not state.state_id or state.job_id != self.workspace.job_id:
            raise PauseRequestedError("CONTROL_STATE_INVALID: pause marker identity does not match the job")
        if state.state not in {RUNNABLE, PAUSED_BY_USER}:
            raise PauseRequestedError("CONTROL_STATE_INVALID: pause marker has an unsupported state")
        if self.registry is not None:
            record = self.registry.get(self.artifact_id)
            if (
                record is None
                or record.status != "ready"
                or str(record.path) != str(self.path)
                or str(record.content_hash or "") != file_sha256(self.path)
            ):
                raise PauseRequestedError(
                    "CONTROL_STATE_INVALID: pause marker is not backed by the current Registry record"
                )
        return state

    def _persist(self, state: PauseStateV1, *, producer: str) -> ArtifactRecord | None:
        atomic_write_json(str(self.path), state.to_dict())
        if self.registry is None:
            return None
        return self.registry.register_file(
            artifact_id=self.artifact_id,
            artifact_role="pause_state",
            artifact_type=PAUSE_STATE_ARTIFACT_TYPE,
            artifact_version=PAUSE_STATE_ARTIFACT_VERSION,
            path=self.path,
            producer=producer,
            metadata={
                "state": state.state,
                "requested_by": state.requested_by,
                "in_flight_policy": state.in_flight_policy,
            },
        )

    def request(
        self,
        *,
        requested_by: str = "reviewctl",
        reason: str = "user_requested",
        checkpoint: str = "provider_admission",
    ) -> PauseStateV1:
        state = PauseStateV1(
            state_id=f"pause:{self.workspace.job_id}",
            job_id=self.workspace.job_id,
            state=PAUSED_BY_USER,
            requested_at=utc_now_iso(),
            requested_by=requested_by,
            reason=reason,
            checkpoint=checkpoint,
        )
        self._persist(state, producer="runtime.pause_state.request")
        return state

    def clear(self, *, cleared_by: str = "reviewctl", reason: str = "explicit_resume") -> PauseStateV1:
        state = PauseStateV1(
            state_id=f"pause:{self.workspace.job_id}",
            job_id=self.workspace.job_id,
            state=RUNNABLE,
            requested_at=utc_now_iso(),
            requested_by=cleared_by,
            reason=reason,
            checkpoint="provider_admission",
        )
        self._persist(state, producer="runtime.pause_state.clear")
        return state

    def is_paused(self) -> bool:
        state = self.read()
        return bool(state and state.paused)

    def assert_runnable(self, *, node_id: str = "") -> None:
        state = self.read()
        if state is not None and state.paused:
            suffix = f" before node {node_id}" if node_id else ""
            raise PauseRequestedError(
                f"{PAUSED_BY_USER}: new provider admission is blocked{suffix}; "
                "in-flight/unknown evidence must be retained and explicit resume is required"
            )


__all__ = [
    "PAUSE_STATE_ARTIFACT_TYPE",
    "PAUSE_STATE_ARTIFACT_VERSION",
    "PAUSED_BY_USER",
    "RUNNABLE",
    "PauseRequestedError",
    "PauseStateV1",
    "PauseStateStore",
]
