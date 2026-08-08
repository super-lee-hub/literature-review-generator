"""Durable, cooperative cancellation requests.

Cancellation is a state transition, not a process-kill operation.  The marker
is an ordinary hash-verified derived artifact so another process (CLI, GUI, or
queue worker) can request cancellation and the worker can observe it at safe
checkpoints.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path
from typing import Any, Mapping

from services.artifact_registry import ArtifactRecord, ArtifactRegistry
from services.job_workspace import JobWorkspace, atomic_write_json, utc_now_iso


CANCEL_REQUEST_ARTIFACT_TYPE = "cancel_request"
CANCEL_REQUEST_ARTIFACT_VERSION = "v1"


@dataclass(frozen=True)
class CancellationRequestV1:
    request_id: str
    job_id: str
    active: bool
    requested_at: str
    requested_by: str
    reason: str
    checkpoint: str = "cooperative"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "CancellationRequestV1":
        return cls(
            request_id=str(payload.get("request_id") or ""),
            job_id=str(payload.get("job_id") or ""),
            active=bool(payload.get("active", False)),
            requested_at=str(payload.get("requested_at") or ""),
            requested_by=str(payload.get("requested_by") or ""),
            reason=str(payload.get("reason") or ""),
            checkpoint=str(payload.get("checkpoint") or "cooperative"),
        )


class CancellationRequestStore:
    def __init__(self, workspace: JobWorkspace, registry: ArtifactRegistry | None = None) -> None:
        self.workspace = workspace
        self.registry = registry
        self.path = Path(workspace.artifact_path(f"cancel_requests/{workspace.job_id}.json"))
        self.artifact_id = f"cancel_request:{workspace.job_id}"

    def read(self) -> CancellationRequestV1 | None:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
        if not isinstance(payload, Mapping):
            return None
        request = CancellationRequestV1.from_dict(payload)
        if request.job_id and request.job_id != self.workspace.job_id:
            return None
        return request

    def _persist(self, request: CancellationRequestV1, *, producer: str) -> ArtifactRecord | None:
        atomic_write_json(str(self.path), request.to_dict())
        if self.registry is None:
            return None
        return self.registry.register_file(
            artifact_id=self.artifact_id,
            artifact_role="cancel_request",
            artifact_type=CANCEL_REQUEST_ARTIFACT_TYPE,
            artifact_version=CANCEL_REQUEST_ARTIFACT_VERSION,
            path=self.path,
            producer=producer,
            metadata={"active": request.active, "requested_by": request.requested_by},
        )

    def request(
        self,
        *,
        requested_by: str = "reviewctl",
        reason: str = "user_requested",
        checkpoint: str = "cooperative",
    ) -> CancellationRequestV1:
        request = CancellationRequestV1(
            request_id=f"cancel:{self.workspace.job_id}",
            job_id=self.workspace.job_id,
            active=True,
            requested_at=utc_now_iso(),
            requested_by=requested_by,
            reason=reason,
            checkpoint=checkpoint,
        )
        self._persist(request, producer="runtime.cancellation.request")
        return request

    def clear(self, *, cleared_by: str = "reviewctl", reason: str = "new_attempt") -> CancellationRequestV1:
        request = CancellationRequestV1(
            request_id=f"cancel:{self.workspace.job_id}",
            job_id=self.workspace.job_id,
            active=False,
            requested_at=utc_now_iso(),
            requested_by=cleared_by,
            reason=reason,
            checkpoint="cooperative",
        )
        self._persist(request, producer="runtime.cancellation.clear")
        return request

    def is_requested(self) -> bool:
        request = self.read()
        return bool(request and request.active)


__all__ = [
    "CANCEL_REQUEST_ARTIFACT_TYPE",
    "CANCEL_REQUEST_ARTIFACT_VERSION",
    "CancellationRequestV1",
    "CancellationRequestStore",
]
