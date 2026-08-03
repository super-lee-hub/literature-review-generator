from __future__ import annotations

import configparser
import json
import os
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, Generic, List, Optional, TypeVar

T = TypeVar("T")

_QUEUE_PROCESS_LOCKS_GUARD = threading.Lock()
_QUEUE_PROCESS_LOCKS: dict[str, threading.RLock] = {}


def _queue_process_lock(path: Path) -> threading.RLock:
    key = os.path.normcase(str(path.resolve()))
    with _QUEUE_PROCESS_LOCKS_GUARD:
        return _QUEUE_PROCESS_LOCKS.setdefault(key, threading.RLock())


class JobCancelledError(RuntimeError):
    pass


class QueueState(Enum):
    PENDING = "pending"
    RUNNING = "running"
    CANCEL_REQUESTED = "cancel_requested"
    CANCEL_ACKNOWLEDGED = "cancel_acknowledged"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class CancelToken:
    def __init__(self) -> None:
        self._cancelled = False
        self._lock = threading.Lock()

    def request_cancel(self) -> None:
        with self._lock:
            self._cancelled = True

    def is_cancelled(self) -> bool:
        with self._lock:
            return self._cancelled

    def check_cancelled(self) -> None:
        if self.is_cancelled():
            raise JobCancelledError("Job was cancelled")


@dataclass
class QueueJobSpec:
    job_id: str
    job_type: str
    project_name: str
    parameters: Dict[str, Any] = field(default_factory=dict)
    created_at: str = ""
    depends_on_job_ids: List[str] = field(default_factory=list)
    source_snapshot: Dict[str, Any] = field(default_factory=dict)
    input_fingerprint: str = ""
    config_fingerprint: str = ""
    current_stage: str = ""
    workspace_path: str = ""
    canonical_output_root: str = ""
    log_path: str = ""
    produced_artifacts: List[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.created_at:
            from services.job_workspace import utc_now_iso
            self.created_at = utc_now_iso()

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "QueueJobSpec":
        # 处理可能不存在的字段
        data.setdefault('source_snapshot', {})
        data.setdefault('input_fingerprint', '')
        data.setdefault('config_fingerprint', '')
        data.setdefault('current_stage', '')
        data.setdefault('workspace_path', '')
        data.setdefault('canonical_output_root', '')
        data.setdefault('log_path', '')
        data.setdefault('produced_artifacts', [])
        return cls(**data)


@dataclass
class QueueJobRuntime:
    job_id: str
    state: QueueState = QueueState.PENDING
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    error_message: Optional[str] = None
    result_summary: Optional[Dict[str, Any]] = None
    retry_count: int = 0
    current_stage: str = ""
    workspace_path: str = ""
    canonical_output_root: str = ""
    log_path: str = ""
    produced_artifacts: List[str] = field(default_factory=list)
    progress_snapshot: Dict[str, Any] = field(default_factory=dict)
    cancel_requested: bool = False
    cancel_requested_at: Optional[str] = None
    cancel_reason: Optional[str] = None
    lease_id: str = ""
    worker_id: str = ""
    lease_expires_at: Optional[str] = None
    heartbeat_at: Optional[str] = None
    revision: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "job_id": self.job_id,
            "state": self.state.value,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "error_message": self.error_message,
            "result_summary": self.result_summary,
            "retry_count": self.retry_count,
            "current_stage": self.current_stage,
            "workspace_path": self.workspace_path,
            "canonical_output_root": self.canonical_output_root,
            "log_path": self.log_path,
            "produced_artifacts": self.produced_artifacts,
            "progress_snapshot": self.progress_snapshot,
            "cancel_requested": self.cancel_requested,
            "cancel_requested_at": self.cancel_requested_at,
            "cancel_reason": self.cancel_reason,
            "lease_id": self.lease_id,
            "worker_id": self.worker_id,
            "lease_expires_at": self.lease_expires_at,
            "heartbeat_at": self.heartbeat_at,
            "revision": self.revision,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "QueueJobRuntime":
        return cls(
            job_id=data["job_id"],
            state=QueueState(data["state"]),
            started_at=data.get("started_at"),
            completed_at=data.get("completed_at"),
            error_message=data.get("error_message"),
            result_summary=data.get("result_summary"),
            retry_count=data.get("retry_count", 0),
            current_stage=data.get("current_stage", ""),
            workspace_path=data.get("workspace_path", ""),
            canonical_output_root=data.get("canonical_output_root", ""),
            log_path=data.get("log_path", ""),
            produced_artifacts=data.get("produced_artifacts", []),
            progress_snapshot=data.get("progress_snapshot", {}),
            cancel_requested=bool(data.get("cancel_requested", False)),
            cancel_requested_at=data.get("cancel_requested_at"),
            cancel_reason=data.get("cancel_reason"),
            lease_id=str(data.get("lease_id") or ""),
            worker_id=str(data.get("worker_id") or ""),
            lease_expires_at=data.get("lease_expires_at"),
            heartbeat_at=data.get("heartbeat_at"),
            revision=max(0, int(data.get("revision") or 0)),
        )


@dataclass(frozen=True)
class QueueLease:
    """Cross-process claim returned by the queue's compare-and-swap boundary."""

    job_id: str
    lease_id: str
    worker_id: str
    expires_at: str
    revision: int


@dataclass
class QueuedJobHandle(Generic[T]):
    cancel_token: CancelToken
    status: str = "pending"
    result: Optional[T] = None
    error: Optional[BaseException] = None

    def cancel(self) -> None:
        self.cancel_token.request_cancel()


class QueueRuntimeProgressTracker:
    """ProgressTracker adapter that mirrors workflow progress into queue runtime state."""

    def __init__(self, on_update: Callable[[Dict[str, Any]], None]) -> None:
        from services.progress_service import ProgressTracker

        self._tracker = ProgressTracker()
        self._on_update = on_update

    def reset(self, **kwargs: Any) -> None:
        self._tracker.reset(**kwargs)
        self._on_update(self.snapshot())

    def emit(self, **kwargs: Any) -> None:
        self._tracker.emit(**kwargs)
        self._on_update(self.snapshot())

    def finish(self, **kwargs: Any) -> None:
        self._tracker.finish(**kwargs)
        self._on_update(self.snapshot())

    def snapshot(self) -> Dict[str, Any]:
        return self._tracker.snapshot()


class InProcessQueueService:
    def __init__(self) -> None:
        self._lock = threading.Lock()

    def run(self, func: Callable[..., T], *args: Any, cancel_token: CancelToken | None = None, **kwargs: Any) -> QueuedJobHandle[T]:
        token = cancel_token or CancelToken()
        handle: QueuedJobHandle[T] = QueuedJobHandle(cancel_token=token)
        with self._lock:
            handle.status = "running"
            try:
                token.check_cancelled()
                try:
                    handle.result = func(*args, cancel_token=token, **kwargs)
                except TypeError:
                    handle.result = func(*args, **kwargs)
                handle.status = "completed"
            except JobCancelledError as exc:
                handle.error = exc
                handle.status = "cancelled"
            except BaseException as exc:  # pragma: no cover
                handle.error = exc
                handle.status = "failed"
        return handle


class PersistentQueueService:
    def __init__(self, queue_file_path: str | Path) -> None:
        self.queue_file_path = Path(queue_file_path).expanduser().resolve()
        queue_parent = self.queue_file_path.parent
        self._canonical_output_root = (
            queue_parent.parent
            if queue_parent.name.casefold() == "_queue"
            else queue_parent
        ).resolve()
        self._lock = threading.Lock()
        self._jobs: Dict[str, QueueJobSpec] = {}
        self._runtimes: Dict[str, QueueJobRuntime] = {}
        self._revision = 0
        self._lock_path = self.queue_file_path.with_name(self.queue_file_path.name + ".lock")
        self._load()

    @contextmanager
    def _store_lock(self):
        """Hold the process and OS lock for one read/modify/write transaction."""

        self.queue_file_path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            with _queue_process_lock(self._lock_path):
                with self._lock_path.open("a+b") as handle:
                    handle.seek(0, os.SEEK_END)
                    if handle.tell() == 0:
                        handle.write(b"persistent queue lock\n")
                        handle.flush()
                        os.fsync(handle.fileno())
                    handle.seek(0)
                    if os.name == "nt":
                        import msvcrt

                        acquired = False
                        while not acquired:
                            try:
                                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                                acquired = True
                            except OSError:
                                time.sleep(0.01)
                    else:
                        import fcntl

                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                    try:
                        self._load_unlocked()
                        yield
                    finally:
                        handle.seek(0)
                        if os.name == "nt":
                            import msvcrt

                            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                        else:
                            import fcntl

                            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _load(self) -> None:
        with self._store_lock():
            return

    def _load_unlocked(self) -> None:
        if self.queue_file_path.exists():
            try:
                data = json.loads(self.queue_file_path.read_text(encoding="utf-8"))
                self._jobs = {
                    job_id: QueueJobSpec.from_dict(job_data)
                    for job_id, job_data in data.get("jobs", {}).items()
                }
                self._runtimes = {
                    job_id: QueueJobRuntime.from_dict(runtime_data)
                    for job_id, runtime_data in data.get("runtimes", {}).items()
                }
                self._revision = max(0, int(data.get("revision") or 0))
                self._normalize_loaded_jobs()
            except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                self._jobs = {}
                self._runtimes = {}
                self._revision = 0
        else:
            self._jobs = {}
            self._runtimes = {}
            self._revision = 0

    def _resolve_path(self, raw_path: Any, *, base: Path | None = None) -> str:
        value = str(raw_path or "").strip()
        if not value:
            return ""
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = (base or self._canonical_output_root) / path
        return str(path.resolve())

    def _config_output_root(self, parameters: Dict[str, Any]) -> str:
        config_path_raw = str(parameters.get("config") or "").strip()
        if not config_path_raw:
            return ""
        config_path = Path(self._resolve_path(config_path_raw))
        if not config_path.is_file():
            return ""
        try:
            parser = configparser.ConfigParser()
            parser.read(config_path, encoding="utf-8")
            raw_output = parser.get("Paths", "output_path", fallback="").strip()
        except (OSError, configparser.Error):
            return ""
        return self._resolve_path(raw_output, base=config_path.parent)

    def _normalize_job_spec(self, job_spec: QueueJobSpec) -> QueueJobSpec:
        parameters = dict(job_spec.parameters or {})
        explicit_workspace = self._resolve_path(
            job_spec.workspace_path or parameters.get("workspace_path")
        )
        canonical_root = self._resolve_path(job_spec.canonical_output_root)
        if explicit_workspace:
            canonical_root = str(Path(explicit_workspace).parent.resolve())
        if not canonical_root:
            for candidate in (
                parameters.get("output_dir"),
                parameters.get("output_path"),
            ):
                candidate_root = self._resolve_path(candidate)
                if candidate_root:
                    canonical_root = candidate_root
                    break
        if not canonical_root:
            canonical_root = self._config_output_root(parameters) or str(self._canonical_output_root)

        project_name = str(job_spec.project_name or parameters.get("project_name") or "project").strip()
        if not explicit_workspace:
            explicit_workspace = str(
                (Path(canonical_root) / f"{project_name}__{job_spec.job_id}").resolve()
            )
        parameters.setdefault("project_name", project_name)
        parameters["job_id"] = job_spec.job_id
        parameters["workspace_path"] = explicit_workspace
        parameters.setdefault("queue_file", str(self.queue_file_path))
        return replace(
            job_spec,
            parameters=parameters,
            workspace_path=explicit_workspace,
            canonical_output_root=canonical_root,
            log_path=str(Path(explicit_workspace) / "logs" / "job.log"),
        )

    def _normalize_loaded_jobs(self) -> None:
        self._jobs = {
            job_id: self._normalize_job_spec(job)
            for job_id, job in self._jobs.items()
        }
        for job_id, job in self._jobs.items():
            runtime = self._runtimes.get(job_id)
            if runtime is not None:
                runtime.workspace_path = runtime.workspace_path or job.workspace_path
                runtime.canonical_output_root = (
                    runtime.canonical_output_root or job.canonical_output_root
                )
                runtime.log_path = runtime.log_path or job.log_path

    def _save(self) -> None:
        """Write the already-locked in-memory snapshot atomically."""

        self.queue_file_path.parent.mkdir(parents=True, exist_ok=True)
        self._revision += 1
        data = {
            "jobs": {job_id: job.to_dict() for job_id, job in self._jobs.items()},
            "runtimes": {job_id: runtime.to_dict() for job_id, runtime in self._runtimes.items()},
            "schema_version": "queue-v2",
            "revision": self._revision,
            "last_updated": self._utc_now(),
        }
        temp_path = self.queue_file_path.with_suffix(".tmp")
        temp_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        temp_path.replace(self.queue_file_path)

    @staticmethod
    def _now_datetime() -> datetime:
        return datetime.now(timezone.utc)

    @classmethod
    def _lease_expiry(cls, lease_seconds: int) -> str:
        return (cls._now_datetime() + timedelta(seconds=max(1, int(lease_seconds)))).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    @classmethod
    def _lease_is_expired(cls, expires_at: str | None) -> bool:
        if not expires_at:
            return True
        try:
            raw = str(expires_at).replace("Z", "+00:00")
            expiry = datetime.fromisoformat(raw)
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
            return expiry <= cls._now_datetime()
        except (TypeError, ValueError):
            return True

    @staticmethod
    def _utc_now() -> str:
        from services.job_workspace import utc_now_iso
        return utc_now_iso()

    def add_job(self, job_spec: QueueJobSpec) -> str:
        with self._store_lock():
            normalized = self._normalize_job_spec(job_spec)
            self._jobs[normalized.job_id] = normalized
            if normalized.job_id not in self._runtimes:
                self._runtimes[normalized.job_id] = QueueJobRuntime(
                    job_id=normalized.job_id,
                    workspace_path=normalized.workspace_path,
                    canonical_output_root=normalized.canonical_output_root,
                    log_path=normalized.log_path,
                )
            else:
                runtime = self._runtimes[normalized.job_id]
                runtime.workspace_path = normalized.workspace_path
                runtime.canonical_output_root = normalized.canonical_output_root
                runtime.log_path = normalized.log_path
            self._save()
        return normalized.job_id

    def get_job(self, job_id: str) -> Optional[QueueJobSpec]:
        with self._store_lock():
            return self._jobs.get(job_id)

    def get_job_runtime(self, job_id: str) -> Optional[QueueJobRuntime]:
        with self._store_lock():
            runtime = self._runtimes.get(job_id)
            if runtime is None:
                return None
            return replace(
                runtime,
                result_summary=dict(runtime.result_summary) if runtime.result_summary else None,
                produced_artifacts=list(runtime.produced_artifacts),
                progress_snapshot=dict(runtime.progress_snapshot),
            )

    def list_job_runtimes(self) -> List[QueueJobRuntime]:
        """Return runtime snapshots for read-only observers."""

        with self._store_lock():
            return [
                replace(
                    runtime,
                    result_summary=dict(runtime.result_summary) if runtime.result_summary else None,
                    produced_artifacts=list(runtime.produced_artifacts),
                    progress_snapshot=dict(runtime.progress_snapshot),
                )
                for runtime in self._runtimes.values()
            ]

    def update_job_stage(self, job_id: str, stage: str) -> bool:
        with self._store_lock():
            runtime = self._runtimes.get(job_id)
            if runtime is None:
                return False
            runtime.current_stage = str(stage or "")
            runtime.revision += 1
            self._save()
            return True

    def update_job_runtime_info(self, job_id: str, info: Dict[str, Any]) -> bool:
        with self._store_lock():
            runtime = self._runtimes.get(job_id)
            if runtime is None:
                return False
            if "workspace_path" in info:
                runtime.workspace_path = str(info["workspace_path"] or "")
            if "canonical_output_root" in info:
                runtime.canonical_output_root = str(info["canonical_output_root"] or "")
            if "log_path" in info:
                runtime.log_path = str(info["log_path"] or "")
            if "produced_artifacts" in info:
                runtime.produced_artifacts = [str(item) for item in info["produced_artifacts"] or []]
            runtime.revision += 1
            self._save()
            return True

    def update_job_progress_snapshot(self, job_id: str, snapshot: Dict[str, Any]) -> bool:
        with self._store_lock():
            runtime = self._runtimes.get(job_id)
            if runtime is None:
                return False
            runtime.progress_snapshot = dict(snapshot)
            stage = str(snapshot.get("stage") or "").strip()
            if stage:
                runtime.current_stage = stage
            runtime.revision += 1
            self._save()
            return True

    def list_jobs(self) -> List[QueueJobSpec]:
        with self._store_lock():
            return list(self._jobs.values())

    def list_jobs_by_state(self, state: QueueState) -> List[QueueJobSpec]:
        with self._store_lock():
            return [
                job
                for job_id, job in self._jobs.items()
                if job_id in self._runtimes and self._runtimes[job_id].state == state
            ]

    def update_job_state(self, job_id: str, state: QueueState) -> bool:
        with self._store_lock():
            if job_id not in self._runtimes:
                return False
            runtime = self._runtimes[job_id]
            allowed = {
                QueueState.PENDING: {QueueState.RUNNING, QueueState.CANCEL_REQUESTED, QueueState.CANCELLED},
                QueueState.RUNNING: {QueueState.COMPLETED, QueueState.FAILED, QueueState.CANCEL_REQUESTED, QueueState.CANCELLED},
                QueueState.CANCEL_REQUESTED: {QueueState.CANCEL_ACKNOWLEDGED, QueueState.CANCELLED},
                QueueState.CANCEL_ACKNOWLEDGED: {QueueState.CANCELLED},
                QueueState.COMPLETED: set(),
                QueueState.FAILED: set(),
                QueueState.CANCELLED: set(),
            }
            if state != runtime.state and state not in allowed.get(runtime.state, set()):
                return False
            if state in {QueueState.COMPLETED, QueueState.FAILED} and runtime.cancel_requested:
                return False
            runtime.state = state
            if state == QueueState.RUNNING and not runtime.started_at:
                runtime.started_at = self._utc_now()
            if state in (QueueState.COMPLETED, QueueState.FAILED, QueueState.CANCEL_ACKNOWLEDGED, QueueState.CANCELLED):
                runtime.completed_at = self._utc_now()
                runtime.lease_id = ""
                runtime.worker_id = ""
                runtime.lease_expires_at = None
                runtime.heartbeat_at = None
            runtime.revision += 1
            self._save()
        return True

    def claim_job(
        self,
        job_id: str,
        *,
        worker_id: str,
        lease_seconds: int = 60,
    ) -> QueueLease | None:
        """Atomically claim a pending job or recover an expired worker lease."""

        resolved_worker = str(worker_id or "").strip()
        if not resolved_worker:
            raise ValueError("worker_id is required for a queue claim")
        with self._store_lock():
            runtime = self._runtimes.get(job_id)
            if runtime is None or runtime.state in {
                QueueState.COMPLETED,
                QueueState.FAILED,
                QueueState.CANCELLED,
                QueueState.CANCEL_ACKNOWLEDGED,
                QueueState.CANCEL_REQUESTED,
            }:
                return None
            if runtime.state == QueueState.RUNNING and not self._lease_is_expired(runtime.lease_expires_at):
                return None
            if runtime.state == QueueState.RUNNING:
                runtime.state = QueueState.PENDING
                runtime.error_message = "worker lease expired; job reclaimed"
                runtime.lease_id = ""
                runtime.worker_id = ""
                runtime.lease_expires_at = None
                runtime.heartbeat_at = None
            if runtime.state != QueueState.PENDING or runtime.cancel_requested:
                return None
            lease_id = f"{resolved_worker}:{uuid.uuid4().hex}"
            expires_at = self._lease_expiry(lease_seconds)
            now = self._utc_now()
            runtime.state = QueueState.RUNNING
            runtime.started_at = runtime.started_at or now
            runtime.worker_id = resolved_worker
            runtime.lease_id = lease_id
            runtime.lease_expires_at = expires_at
            runtime.heartbeat_at = now
            runtime.revision += 1
            self._save()
            return QueueLease(job_id, lease_id, resolved_worker, expires_at, runtime.revision)

    def heartbeat(
        self,
        job_id: str,
        *,
        lease_id: str,
        worker_id: str,
        lease_seconds: int = 60,
    ) -> bool:
        """Extend a claim only when its worker/lease pair still owns it."""

        with self._store_lock():
            runtime = self._runtimes.get(job_id)
            if runtime is None or runtime.state != QueueState.RUNNING:
                return False
            if runtime.lease_id != str(lease_id) or runtime.worker_id != str(worker_id):
                return False
            if self._lease_is_expired(runtime.lease_expires_at):
                return False
            runtime.heartbeat_at = self._utc_now()
            runtime.lease_expires_at = self._lease_expiry(lease_seconds)
            runtime.revision += 1
            self._save()
            return True

    def release_lease(
        self,
        job_id: str,
        *,
        lease_id: str,
        worker_id: str,
        state: QueueState,
        error_message: str | None = None,
    ) -> bool:
        """CAS-release a worker lease and persist the terminal queue state."""

        if state not in {QueueState.COMPLETED, QueueState.FAILED, QueueState.CANCELLED}:
            raise ValueError("lease release requires a terminal queue state")
        with self._store_lock():
            runtime = self._runtimes.get(job_id)
            if runtime is None:
                return False
            if runtime.lease_id != str(lease_id) or runtime.worker_id != str(worker_id):
                return False
            if state in {QueueState.COMPLETED, QueueState.FAILED} and runtime.cancel_requested:
                return False
            runtime.state = state
            runtime.completed_at = self._utc_now()
            runtime.error_message = error_message if error_message is not None else runtime.error_message
            runtime.lease_id = ""
            runtime.worker_id = ""
            runtime.lease_expires_at = None
            runtime.heartbeat_at = None
            runtime.revision += 1
            self._save()
            return True

    def recover_expired_leases(self) -> list[str]:
        """Move crashed workers' expired RUNNING jobs back to PENDING."""

        recovered: list[str] = []
        with self._store_lock():
            for job_id, runtime in self._runtimes.items():
                if runtime.state != QueueState.RUNNING or not self._lease_is_expired(runtime.lease_expires_at):
                    continue
                runtime.state = QueueState.PENDING
                runtime.error_message = "worker lease expired; job available for recovery"
                runtime.lease_id = ""
                runtime.worker_id = ""
                runtime.lease_expires_at = None
                runtime.heartbeat_at = None
                runtime.revision += 1
                recovered.append(job_id)
            if recovered:
                self._save()
        return recovered

    def set_job_error(self, job_id: str, error_message: str) -> bool:
        with self._store_lock():
            if job_id not in self._runtimes:
                return False
            self._runtimes[job_id].error_message = error_message
            self._runtimes[job_id].revision += 1
            self._save()
        return True

    def set_job_result(self, job_id: str, result_summary: Dict[str, Any]) -> bool:
        with self._store_lock():
            if job_id not in self._runtimes:
                return False
            self._runtimes[job_id].result_summary = result_summary
            self._runtimes[job_id].revision += 1
            self._save()
        return True

    def increment_retry_count(self, job_id: str) -> int:
        with self._store_lock():
            if job_id not in self._runtimes:
                return 0
            self._runtimes[job_id].retry_count += 1
            self._runtimes[job_id].revision += 1
            self._save()
            return self._runtimes[job_id].retry_count

    def reset_job(self, job_id: str) -> bool:
        with self._store_lock():
            if job_id not in self._runtimes:
                return False
            previous = self._runtimes[job_id]
            self._runtimes[job_id] = QueueJobRuntime(
                job_id=job_id,
                retry_count=previous.retry_count,
                workspace_path=self._jobs[job_id].workspace_path if job_id in self._jobs else "",
                canonical_output_root=(
                    self._jobs[job_id].canonical_output_root if job_id in self._jobs else ""
                ),
                log_path=self._jobs[job_id].log_path if job_id in self._jobs else "",
            )
            self._save()
        return True

    def request_cancel(self, job_id: str, *, reason: str = "user_requested") -> bool:
        """Persist a cooperative cancellation request for a live job."""

        with self._store_lock():
            runtime = self._runtimes.get(job_id)
            if runtime is None or runtime.state in (
                QueueState.COMPLETED,
                QueueState.FAILED,
                QueueState.CANCEL_ACKNOWLEDGED,
                QueueState.CANCELLED,
            ):
                return False
            runtime.cancel_requested = True
            runtime.cancel_requested_at = self._utc_now()
            runtime.cancel_reason = reason
            if runtime.state == QueueState.PENDING:
                # A pending job has no worker checkpoint to wait for.  Mark it
                # cancelled atomically so it cannot become runnable after the
                # request is persisted.
                runtime.state = QueueState.CANCELLED
                runtime.completed_at = self._utc_now()
            else:
                runtime.state = QueueState.CANCEL_REQUESTED
            runtime.revision += 1
            self._save()
        return True

    def acknowledge_cancel(self, job_id: str, *, worker: str = "queue-worker") -> bool:
        """Record that the worker observed and stopped at a safe checkpoint."""

        with self._store_lock():
            runtime = self._runtimes.get(job_id)
            if runtime is None or runtime.state != QueueState.CANCEL_REQUESTED:
                return False
            runtime.state = QueueState.CANCEL_ACKNOWLEDGED
            runtime.error_message = f"cancellation acknowledged by {worker}"
            runtime.completed_at = self._utc_now()
            runtime.lease_id = ""
            runtime.worker_id = ""
            runtime.lease_expires_at = None
            runtime.heartbeat_at = None
            runtime.revision += 1
            self._save()
        return True

    def clear_cancel_request(self, job_id: str) -> bool:
        with self._store_lock():
            runtime = self._runtimes.get(job_id)
            if runtime is None:
                return False
            runtime.cancel_requested = False
            runtime.cancel_requested_at = None
            runtime.cancel_reason = None
            runtime.revision += 1
            self._save()
        return True

    def is_cancel_requested(self, job_id: str) -> bool:
        with self._store_lock():
            runtime = self._runtimes.get(job_id)
            return bool(runtime and runtime.cancel_requested)

    def remove_job(self, job_id: str) -> bool:
        with self._store_lock():
            if job_id in self._jobs:
                del self._jobs[job_id]
            if job_id in self._runtimes:
                del self._runtimes[job_id]
            self._save()
        return True

    def get_failed_jobs(self) -> List[QueueJobSpec]:
        return self.list_jobs_by_state(QueueState.FAILED)

    def retry_failed_jobs(self) -> List[str]:
        failed_jobs = self.get_failed_jobs()
        retried_job_ids = []
        for job in failed_jobs:
            self.reset_job(job.job_id)
            self.increment_retry_count(job.job_id)
            retried_job_ids.append(job.job_id)
        return retried_job_ids

    def save_queue(self, file_path: str | Path) -> None:
        """保存队列到文件"""
        save_path = Path(file_path)
        with self._store_lock():
            save_path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "schema_version": "queue-v2",
                "revision": self._revision,
                "jobs": {job_id: job.to_dict() for job_id, job in self._jobs.items()},
                "runtimes": {job_id: runtime.to_dict() for job_id, runtime in self._runtimes.items()},
                "last_updated": self._utc_now(),
            }
            temp_path = save_path.with_suffix(".tmp")
            temp_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
            temp_path.replace(save_path)

    def load_queue(self, file_path: str | Path) -> None:
        """从文件加载队列"""
        load_path = Path(file_path)
        if load_path.exists():
            try:
                data = json.loads(load_path.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    raise ValueError("queue export must be an object")
                with self._store_lock():
                    # 加载任务
                    for job_id, job_data in data.get("jobs", {}).items():
                        self._jobs[job_id] = QueueJobSpec.from_dict(job_data)
                    # 加载运行时信息
                    for job_id, runtime_data in data.get("runtimes", {}).items():
                        self._runtimes[job_id] = QueueJobRuntime.from_dict(runtime_data)
                    self._normalize_loaded_jobs()
                    # 保存到当前队列文件
                    self._save()
            except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                pass

    def reorder_jobs(self, job_ids: List[str]) -> None:
        """重排任务顺序"""
        # 按指定顺序重新排列任务
        with self._store_lock():
            ordered_jobs = {}
            for job_id in job_ids:
                if job_id in self._jobs:
                    ordered_jobs[job_id] = self._jobs[job_id]
            # 保留未在列表中的任务
            for job_id, job in self._jobs.items():
                if job_id not in ordered_jobs:
                    ordered_jobs[job_id] = job
            self._jobs = ordered_jobs
            self._save()


def create_queue_job_id() -> str:
    return f"job_{uuid.uuid4().hex[:12]}"


class QueueRunner:
    def __init__(self, queue_service: PersistentQueueService, job_runner: Any) -> None:
        self.queue_service = queue_service
        self.job_runner = job_runner
        self._running = False
        self._lock = threading.Lock()
        self._cancel_tokens: Dict[str, CancelToken] = {}
        self._workspace_locks: Dict[str, threading.Lock] = {}
        self._worker_id = f"queue-runner:{uuid.uuid4().hex}"
        self._leases: Dict[str, QueueLease] = {}
        self._heartbeat_stops: Dict[str, threading.Event] = {}
        self._heartbeat_threads: Dict[str, threading.Thread] = {}
        self._lease_lost: Dict[str, threading.Event] = {}
        # The durable lease is 120 seconds; keep the cross-process heartbeat
        # comfortably below that window even while a provider call is quiet.
        self._heartbeat_interval_seconds = 30.0

    def is_running(self) -> bool:
        with self._lock:
            return self._running

    def _get_workspace_lock(self, workspace_path: str) -> threading.Lock:
        """获取工作区的锁，确保同一工作区不会并发执行"""
        with self._lock:
            if workspace_path not in self._workspace_locks:
                self._workspace_locks[workspace_path] = threading.Lock()
            return self._workspace_locks[workspace_path]

    @staticmethod
    def _external_cancel_requested(job_spec: QueueJobSpec) -> bool:
        """Read a cross-process cancellation marker without mutating state."""

        from runtime.cancellation import CancellationRequestStore
        from services.job_workspace import JobWorkspace

        runtime_workspace = str(job_spec.workspace_path or "").strip()
        if not runtime_workspace:
            return False
        workspace_path = Path(runtime_workspace).expanduser().resolve()
        project_name = workspace_path.name.rsplit("__", 1)[0]
        workspace = JobWorkspace.from_workspace_path(str(workspace_path), project_name, job_spec.job_id)
        return CancellationRequestStore(workspace).is_requested()

    @staticmethod
    def _clear_external_cancel(job_spec: QueueJobSpec) -> None:
        from runtime.cancellation import CancellationRequestStore
        from services.job_workspace import JobWorkspace

        runtime_workspace = str(job_spec.workspace_path or "").strip()
        if not runtime_workspace:
            return
        workspace_path = Path(runtime_workspace).expanduser().resolve()
        project_name = workspace_path.name.rsplit("__", 1)[0]
        workspace = JobWorkspace.from_workspace_path(str(workspace_path), project_name, job_spec.job_id)
        store = CancellationRequestStore(workspace)
        if store.read() is not None:
            store.clear(cleared_by="queue_runner", reason="retry")

    def _acknowledge_cancelled(self, job_id: str) -> None:
        runtime = self.queue_service.get_job_runtime(job_id)
        if runtime is None:
            return
        if self._lease_is_lost(job_id):
            return
        lease = self._leases.get(job_id)
        if lease is not None and runtime.state in {QueueState.CANCEL_REQUESTED, QueueState.RUNNING}:
            if self.queue_service.release_lease(
                job_id,
                lease_id=lease.lease_id,
                worker_id=lease.worker_id,
                state=QueueState.CANCELLED,
            ):
                return
        if runtime.state == QueueState.CANCEL_REQUESTED:
            self.queue_service.acknowledge_cancel(job_id)
            self.queue_service.update_job_state(job_id, QueueState.CANCELLED)
        elif runtime.state == QueueState.PENDING:
            self.queue_service.request_cancel(job_id, reason="queue_runner_checkpoint")

    def _mark_lease_lost(self, job_id: str) -> None:
        with self._lock:
            event = self._lease_lost.get(job_id)
            if event is None:
                event = threading.Event()
                self._lease_lost[job_id] = event
            event.set()
            cancel_token = self._cancel_tokens.get(job_id)
        if cancel_token is not None:
            cancel_token.request_cancel()

    def _lease_is_lost(self, job_id: str) -> bool:
        with self._lock:
            event = self._lease_lost.get(job_id)
            return bool(event and event.is_set())

    def _start_heartbeat(self, job_id: str, lease: QueueLease, cancel_token: CancelToken) -> None:
        stop_event = threading.Event()
        lost_event = threading.Event()
        interval = max(
            0.1,
            min(float(self._heartbeat_interval_seconds), 120.0 / 3.0),
        )
        with self._lock:
            self._heartbeat_stops[job_id] = stop_event
            self._lease_lost[job_id] = lost_event

        def heartbeat_loop() -> None:
            while not stop_event.wait(interval):
                if self.queue_service.heartbeat(
                    job_id,
                    lease_id=lease.lease_id,
                    worker_id=lease.worker_id,
                    lease_seconds=120,
                ):
                    continue
                self._mark_lease_lost(job_id)
                cancel_token.request_cancel()
                return

        thread = threading.Thread(
            target=heartbeat_loop,
            name=f"queue-heartbeat:{job_id}",
            daemon=True,
        )
        with self._lock:
            self._heartbeat_threads[job_id] = thread
        thread.start()

    def _stop_heartbeat(self, job_id: str) -> bool:
        with self._lock:
            stop_event = self._heartbeat_stops.get(job_id)
            thread = self._heartbeat_threads.get(job_id)
        if stop_event is not None:
            stop_event.set()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        return self._lease_is_lost(job_id)

    def _clear_heartbeat(self, job_id: str) -> None:
        self._stop_heartbeat(job_id)
        with self._lock:
            self._heartbeat_stops.pop(job_id, None)
            self._heartbeat_threads.pop(job_id, None)
            self._lease_lost.pop(job_id, None)

    def _process_job(self, job_spec: QueueJobSpec) -> None:
        cancel_token = CancelToken()
        with self._lock:
            self._cancel_tokens[job_spec.job_id] = cancel_token

        try:
            if self._external_cancel_requested(job_spec):
                cancel_token.request_cancel()
                self.queue_service.request_cancel(job_spec.job_id, reason="external_cancel_request")
                self._acknowledge_cancelled(job_spec.job_id)
                return
            # 检查任务是否已经被取消
            if self.queue_service.is_cancel_requested(job_spec.job_id):
                cancel_token.request_cancel()
                self._acknowledge_cancelled(job_spec.job_id)
                return
            runtime = self.queue_service.get_job_runtime(job_spec.job_id)
            if runtime and (runtime.state == QueueState.CANCELLED or runtime.cancel_requested):
                self._acknowledge_cancelled(job_spec.job_id)
                return

            # 检查依赖任务状态 — 严格优先级: missing → failed → cancelled → not-completed → completed
            for dep_job_id in job_spec.depends_on_job_ids:
                dep_runtime = self.queue_service.get_job_runtime(dep_job_id)
                # Missing dependency
                if not dep_runtime:
                    self.queue_service.update_job_state(job_spec.job_id, QueueState.FAILED)
                    self.queue_service.set_job_error(job_spec.job_id, f"Dependency job {dep_job_id} not found")
                    return
                # Failed dependency → propagate failure
                if dep_runtime.state == QueueState.FAILED:
                    self.queue_service.update_job_state(job_spec.job_id, QueueState.FAILED)
                    self.queue_service.set_job_error(
                        job_spec.job_id,
                        f"Dependency job {dep_job_id} failed: {dep_runtime.error_message or 'no error details'}",
                    )
                    return
                # Cancelled dependency → propagate cancellation (no error_message)
                if dep_runtime.state == QueueState.CANCELLED:
                    self.queue_service.update_job_state(job_spec.job_id, QueueState.CANCELLED)
                    self.queue_service.set_job_result(
                        job_spec.job_id,
                        {
                            "cancelled_by_dependency": dep_job_id,
                            "reason": f"Cancelled because dependency job {dep_job_id} was cancelled",
                            "dependency_result_summary": dep_runtime.result_summary,
                        },
                    )
                    return
                # Pending or running dependency → keep this job waiting
                if dep_runtime.state not in (QueueState.COMPLETED,):
                    return
            
            # 更新任务状态为运行中
            lease = self.queue_service.claim_job(
                job_spec.job_id,
                worker_id=self._worker_id,
                lease_seconds=120,
            )
            if lease is None:
                return
            with self._lock:
                self._leases[job_spec.job_id] = lease
            self._start_heartbeat(job_spec.job_id, lease, cancel_token)
            
            # 再次检查任务状态，确保没有被取消
            runtime = self.queue_service.get_job_runtime(job_spec.job_id)
            if runtime and (runtime.state == QueueState.CANCELLED or runtime.cancel_requested):
                cancel_token.request_cancel()
                self._acknowledge_cancelled(job_spec.job_id)
                return
            
            # 更新当前阶段
            self._update_job_stage(job_spec.job_id, "initializing")
            
            # 从job_spec参数构建JobRunRequest
            params = dict(job_spec.parameters)
            params.setdefault("project_name", job_spec.project_name)
            params.setdefault("job_id", job_spec.job_id)
            params["workspace_path"] = job_spec.workspace_path
            params["queue_file"] = str(self.queue_service.queue_file_path)
            # Build JobRunRequest from queued parameters
            from services.job_runner import build_job_request_from_mapping
            request = build_job_request_from_mapping(params)
            progress_tracker = QueueRuntimeProgressTracker(
                lambda snapshot, jid=job_spec.job_id: self._update_job_progress_snapshot(jid, snapshot)
            )
            request = replace(request, progress_tracker=progress_tracker)
            
            
            # 计算工作区路径（基于项目名称和任务ID）
            workspace_path = str(Path(job_spec.workspace_path).expanduser().resolve())
            
            # 获取工作区锁
            workspace_lock = self._get_workspace_lock(workspace_path)
            
            # 用工作区锁保护任务执行
            with workspace_lock:
                # 更新当前阶段
                self._update_job_stage(job_spec.job_id, "executing")
                
                # 执行任务，传入cancel_token
                result = self.job_runner.run(request, cancel_token=cancel_token)
            
            # 最后检查一次任务状态
            if self._lease_is_lost(job_spec.job_id):
                raise RuntimeError(f"queue lease lost for job {job_spec.job_id}")
            runtime = self.queue_service.get_job_runtime(job_spec.job_id)
            if runtime and (runtime.state == QueueState.CANCELLED or runtime.cancel_requested):
                self._acknowledge_cancelled(job_spec.job_id)
                return
            
            # 更新当前阶段
            self._update_job_stage(job_spec.job_id, "completing")
            self._update_job_runtime_info(job_spec.job_id, {
                "workspace_path": result.workspace_path,
                "canonical_output_root": job_spec.canonical_output_root,
                "log_path": result.log_path if hasattr(result, 'log_path') else job_spec.log_path,
                "produced_artifacts": result.produced_artifacts if hasattr(result, 'produced_artifacts') else [],
            })
            
            job_status = str(getattr(result, "job_status", "failed") or "failed")
            if job_status == "completed":
                # 更新任务状态为完成
                self._release_job(job_spec.job_id, QueueState.COMPLETED)
                
                # 更新任务结果和工件信息
                result_summary = {
                    "exit_code": result.exit_code,
                    "message": result.message,
                    "workspace_path": result.workspace_path,
                    "job_id": result.job_id,
                    "resume_state": result.resume_state,
                }
                self.queue_service.set_job_result(job_spec.job_id, result_summary)
            else:
                # 检查是否因为取消而失败
                # Queue lifecycle follows canonical execution status.  The
                # legacy success projection describes canonical readiness.
                if job_status == "cancelled":
                    self._release_job(job_spec.job_id, QueueState.CANCELLED)
                else:
                    self._release_job(
                        job_spec.job_id,
                        QueueState.FAILED,
                        error_message=str(getattr(result, "message", "") or ""),
                    )
        except JobCancelledError:
            self._acknowledge_cancelled(job_spec.job_id)
        except Exception as e:
            if self._lease_is_lost(job_spec.job_id):
                # The old worker no longer owns the durable lease.  Leaving
                # the runtime RUNNING lets another process recover it after
                # expiry; this worker must not release or complete it.
                return
            # 检查是否是因为取消而导致的异常
            runtime = self.queue_service.get_job_runtime(job_spec.job_id)
            if runtime and (runtime.state == QueueState.CANCELLED or runtime.cancel_requested):
                self._acknowledge_cancelled(job_spec.job_id)
                return
            self._release_job(job_spec.job_id, QueueState.FAILED, error_message=str(e))
        finally:
            self._clear_heartbeat(job_spec.job_id)
            with self._lock:
                if job_spec.job_id in self._cancel_tokens:
                    del self._cancel_tokens[job_spec.job_id]
                self._leases.pop(job_spec.job_id, None)
    
    def _update_job_stage(self, job_id: str, stage: str) -> None:
        """更新任务的当前阶段"""
        self._heartbeat(job_id)
        self.queue_service.update_job_stage(job_id, stage)

    def _heartbeat(self, job_id: str) -> None:
        if self._lease_is_lost(job_id):
            raise RuntimeError(f"queue lease lost for job {job_id}")
        lease = self._leases.get(job_id)
        if lease is None:
            return
        if not self.queue_service.heartbeat(
            job_id,
            lease_id=lease.lease_id,
            worker_id=lease.worker_id,
            lease_seconds=120,
        ):
            self._mark_lease_lost(job_id)
            raise RuntimeError(f"queue lease lost for job {job_id}")

    def _release_job(
        self,
        job_id: str,
        state: QueueState,
        *,
        error_message: str | None = None,
    ) -> None:
        lease_lost = self._stop_heartbeat(job_id)
        if lease_lost:
            raise RuntimeError(f"queue lease lost for job {job_id}")
        lease = self._leases.get(job_id)
        if lease is None:
            self.queue_service.update_job_state(job_id, state)
            if error_message:
                self.queue_service.set_job_error(job_id, error_message)
            return
        if not self.queue_service.release_lease(
            job_id,
            lease_id=lease.lease_id,
            worker_id=lease.worker_id,
            state=state,
            error_message=error_message,
        ):
            raise RuntimeError(f"queue lease release lost for job {job_id}")
    
    def _update_job_runtime_info(self, job_id: str, info: Dict[str, Any]) -> None:
        """更新任务的运行时信息"""
        self.queue_service.update_job_runtime_info(job_id, info)

    def _update_job_progress_snapshot(self, job_id: str, snapshot: Dict[str, Any]) -> None:
        """Persist the latest workflow progress snapshot for GUI queue inspection."""
        self.queue_service.update_job_progress_snapshot(job_id, snapshot)

    def run(self) -> None:
        with self._lock:
            if self._running:
                return
            self._running = True

        try:
            self.queue_service.recover_expired_leases()
            processed_ids: set = set()
            max_passes = len(self.queue_service.list_jobs()) * 2 + 10  # safety bound
            passes = 0
            while passes < max_passes:
                passes += 1
                with self._lock:
                    if not self._running:
                        break

                # 获取待处理的任务
                pending_jobs = self.queue_service.list_jobs_by_state(QueueState.PENDING)
                active_jobs = [j for j in pending_jobs if j.job_id not in processed_ids]

                if not active_jobs:
                    break

                made_progress = False
                for job in active_jobs:
                    with self._lock:
                        if not self._running:
                            break

                    # 检查任务状态，确保没有被其他进程处理
                    runtime = self.queue_service.get_job_runtime(job.job_id)
                    if runtime and runtime.state == QueueState.PENDING:
                        self._process_job(job)
                        # Only exclude job from re-evaluation if its state
                        # actually changed; jobs that stayed PENDING
                        # (dependency not ready) must be retried later.
                        runtime_after = self.queue_service.get_job_runtime(job.job_id)
                        if runtime_after and runtime_after.state != QueueState.PENDING:
                            processed_ids.add(job.job_id)
                            made_progress = True
                        # After each job, re-scan pending list so newly-failed
                        # or cancelled dependents are picked up in this drain
                        if job.depends_on_job_ids:
                            break

                if not made_progress and not active_jobs:
                    break
        finally:
            with self._lock:
                self._running = False

    def stop(self) -> None:
        with self._lock:
            self._running = False
            heartbeat_stops = list(self._heartbeat_stops.values())
        for stop_event in heartbeat_stops:
            stop_event.set()

    def cancel_job(self, job_id: str) -> bool:
        """取消指定的任务
        
        Args:
            job_id: 要取消的任务ID
            
        Returns:
            是否成功取消
        """
        runtime = self.queue_service.get_job_runtime(job_id)
        if not runtime or runtime.state not in (QueueState.PENDING, QueueState.RUNNING):
            return False

        self.queue_service.request_cancel(job_id, reason="queue_runner_cancel")
        
        # 如果任务正在运行，请求取消令牌
        with self._lock:
            if job_id in self._cancel_tokens:
                self._cancel_tokens[job_id].request_cancel()
        
        return True

    def run_single_job(self, job_id: str) -> bool:
        job = self.queue_service.get_job(job_id)
        if not job:
            return False
        
        runtime = self.queue_service.get_job_runtime(job_id)
        if not runtime or runtime.state not in (QueueState.PENDING, QueueState.FAILED, QueueState.CANCELLED):
            return False
        
        # 重置任务状态
        self.queue_service.reset_job(job_id)
        self.queue_service.clear_cancel_request(job_id)
        self._clear_external_cancel(job)
        self._process_job(job)
        return True
