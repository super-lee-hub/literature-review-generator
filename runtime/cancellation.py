"""Durable, cooperative cancellation requests.

Cancellation is a state transition, not a process-kill operation.  The marker
is an ordinary hash-verified derived artifact so another process (CLI, GUI, or
queue worker) can request cancellation and the worker can observe it at safe
checkpoints.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from contextlib import AbstractContextManager, nullcontext
import hashlib
import json
from pathlib import Path
from typing import Any, Callable, Mapping, cast
import uuid

from services.artifact_registry import ArtifactRecord, ArtifactRegistry, file_sha256
from services.durable_io import interprocess_file_lock
from services.job_workspace import JobWorkspace, atomic_write_json, utc_now_iso


CANCEL_REQUEST_ARTIFACT_TYPE = "cancel_request"
CANCEL_REQUEST_ARTIFACT_VERSION = "v1"


class CancellationRequestError(ValueError):
    """Raised when a durable cancellation marker is missing or invalid."""


@dataclass(frozen=True)
class CancellationRequestV1:
    request_id: str
    job_id: str
    active: bool
    requested_at: str
    requested_by: str
    reason: str
    checkpoint: str = "cooperative"
    request_token: str = ""

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
            request_token=str(payload.get("request_token") or ""),
        )


class CancellationRequestStore:
    def __init__(self, workspace: JobWorkspace, registry: ArtifactRegistry | None = None) -> None:
        self.workspace = workspace
        self.registry = registry
        self.path = Path(workspace.artifact_path(f"cancel_requests/{workspace.job_id}.json"))
        self.artifact_id = f"cancel_request:{workspace.job_id}"

    def read(self) -> CancellationRequestV1 | None:
        # A read-only probe must not reserve a not-yet-created workspace.
        # New writers create this lock before mutating a marker; legacy reads
        # remain byte-verified without creating a new lock file.
        lock_path = self.path.with_name(self.path.name + ".lock")
        with interprocess_file_lock(self.path) if lock_path.is_file() else nullcontext():
            return self._read_unlocked()

    def _read_unlocked(self) -> CancellationRequestV1 | None:
        try:
            raw = self.path.read_bytes()
            payload = json.loads(raw.decode("utf-8"))
        except FileNotFoundError:
            if self.registry is not None:
                self.registry.reload()
                if self.registry.get(self.artifact_id) is not None:
                    raise CancellationRequestError(
                        "CONTROL_STATE_INVALID: registered cancellation marker is missing"
                    )
            return None
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise CancellationRequestError(
                f"CONTROL_STATE_INVALID: cancellation marker cannot be read: {type(exc).__name__}"
            ) from exc
        if not isinstance(payload, Mapping):
            raise CancellationRequestError("CONTROL_STATE_INVALID: cancellation marker must be an object")
        if type(payload.get("active")) is not bool:
            raise CancellationRequestError("CONTROL_STATE_INVALID: cancellation active flag must be boolean")
        if "request_token" in payload and not isinstance(payload["request_token"], str):
            raise CancellationRequestError("CONTROL_STATE_INVALID: cancellation token must be a string")
        request = CancellationRequestV1.from_dict(payload)
        if (
            request.job_id != self.workspace.job_id
            or request.request_id != f"cancel:{self.workspace.job_id}"
            or not request.requested_at
        ):
            raise CancellationRequestError("CONTROL_STATE_INVALID: cancellation marker identity is invalid")
        if self.registry is not None:
            self.registry.reload()
            record = self.registry.get(self.artifact_id)
            if (
                record is None
                or record.status != "ready"
                or str(record.path) != str(self.path)
                or str(record.content_hash or "") != hashlib.sha256(raw).hexdigest()
                or str(record.content_hash or "") != file_sha256(self.path)
            ):
                raise CancellationRequestError(
                    "CONTROL_STATE_INVALID: cancellation marker is not backed by the current Registry record"
                )
        return request

    def read_snapshot(self) -> tuple[CancellationRequestV1 | None, str]:
        """Capture verified marker bytes for an explicit retry transition."""
        lock_path = self.path.with_name(self.path.name + ".lock")
        if not self.path.exists() and not lock_path.exists():
            if self._read_unlocked() is None:
                return None, ""
        with interprocess_file_lock(self.path):
            request = self._read_unlocked()
            return request, file_sha256(self.path) if request is not None else ""

    def _persist(self, request: CancellationRequestV1, *, producer: str, register_file=None) -> ArtifactRecord | None:
        atomic_write_json(str(self.path), request.to_dict())
        if self.registry is None:
            return None
        register = register_file or self.registry.register_file
        return register(
            artifact_id=self.artifact_id,
            artifact_role="cancel_request",
            artifact_type=CANCEL_REQUEST_ARTIFACT_TYPE,
            artifact_version=CANCEL_REQUEST_ARTIFACT_VERSION,
            path=self.path,
            producer=producer,
            metadata={"active": request.active, "requested_by": request.requested_by},
        )

    def _mutation_guard(
        self, *, clearing: bool = False,
    ) -> AbstractContextManager[Callable[..., ArtifactRecord] | None]:
        guard = getattr(self.registry, "control_mutation_guard", None)
        if not callable(guard):
            return nullcontext(None)
        return cast(
            AbstractContextManager[Callable[..., ArtifactRecord] | None],
            guard(clearing=clearing),
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
            request_token=uuid.uuid4().hex,
        )
        with interprocess_file_lock(self.path):
            with self._mutation_guard() as register:
                self._persist(request, producer="runtime.cancellation.request", register_file=register)
        return request

    def clear(
        self, *, cleared_by: str = "reviewctl", reason: str = "new_attempt",
        expected_hash: str | None = None,
    ) -> CancellationRequestV1:
        if expected_hash is not None and (
            not isinstance(expected_hash, str)
            or (expected_hash and (len(expected_hash) != 64 or any(char not in "0123456789abcdef" for char in expected_hash)))
        ):
            raise CancellationRequestError("CONTROL_STATE_INVALID: expected cancellation hash is invalid")
        request = CancellationRequestV1(
            request_id=f"cancel:{self.workspace.job_id}",
            job_id=self.workspace.job_id,
            active=False,
            requested_at=utc_now_iso(),
            requested_by=cleared_by,
            reason=reason,
            checkpoint="cooperative",
            request_token=uuid.uuid4().hex,
        )
        with interprocess_file_lock(self.path):
            with self._mutation_guard(clearing=True) as register:
                if expected_hash is not None:
                    current = self._read_unlocked()
                    current_hash = file_sha256(self.path) if current is not None else ""
                    if current_hash != expected_hash:
                        raise CancellationRequestError("cancellation marker changed after retry was requested")
                self._persist(request, producer="runtime.cancellation.clear", register_file=register)
        return request

    def is_requested(self) -> bool:
        request = self.read()
        return bool(request and request.active)


__all__ = [
    "CANCEL_REQUEST_ARTIFACT_TYPE",
    "CANCEL_REQUEST_ARTIFACT_VERSION",
    "CancellationRequestError",
    "CancellationRequestV1",
    "CancellationRequestStore",
]
