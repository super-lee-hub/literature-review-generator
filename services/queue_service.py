from __future__ import annotations

import json
import threading
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, Generic, List, Optional, TypeVar

T = TypeVar("T")


class JobCancelledError(RuntimeError):
    pass


class QueueState(Enum):
    PENDING = "pending"
    RUNNING = "running"
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

    def __post_init__(self) -> None:
        if not self.created_at:
            from services.job_workspace import utc_now_iso
            self.created_at = utc_now_iso()

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "QueueJobSpec":
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

    def to_dict(self) -> Dict[str, Any]:
        return {
            "job_id": self.job_id,
            "state": self.state.value,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "error_message": self.error_message,
            "result_summary": self.result_summary,
            "retry_count": self.retry_count,
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
        )


@dataclass
class QueuedJobHandle(Generic[T]):
    cancel_token: CancelToken
    status: str = "pending"
    result: Optional[T] = None
    error: Optional[BaseException] = None

    def cancel(self) -> None:
        self.cancel_token.request_cancel()


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
        self.queue_file_path = Path(queue_file_path)
        self._lock = threading.Lock()
        self._jobs: Dict[str, QueueJobSpec] = {}
        self._runtimes: Dict[str, QueueJobRuntime] = {}
        self._load()

    def _load(self) -> None:
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
            except (json.JSONDecodeError, KeyError):
                self._jobs = {}
                self._runtimes = {}

    def _save(self) -> None:
        self.queue_file_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "jobs": {job_id: job.to_dict() for job_id, job in self._jobs.items()},
            "runtimes": {job_id: runtime.to_dict() for job_id, runtime in self._runtimes.items()},
            "last_updated": self._utc_now(),
        }
        temp_path = self.queue_file_path.with_suffix(".tmp")
        temp_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        temp_path.replace(self.queue_file_path)

    @staticmethod
    def _utc_now() -> str:
        from services.job_workspace import utc_now_iso
        return utc_now_iso()

    def add_job(self, job_spec: QueueJobSpec) -> str:
        with self._lock:
            self._jobs[job_spec.job_id] = job_spec
            if job_spec.job_id not in self._runtimes:
                self._runtimes[job_spec.job_id] = QueueJobRuntime(job_id=job_spec.job_id)
            self._save()
        return job_spec.job_id

    def get_job(self, job_id: str) -> Optional[QueueJobSpec]:
        with self._lock:
            return self._jobs.get(job_id)

    def get_job_runtime(self, job_id: str) -> Optional[QueueJobRuntime]:
        with self._lock:
            return self._runtimes.get(job_id)

    def list_jobs(self) -> List[QueueJobSpec]:
        with self._lock:
            return list(self._jobs.values())

    def list_jobs_by_state(self, state: QueueState) -> List[QueueJobSpec]:
        with self._lock:
            return [
                self._jobs[job_id]
                for job_id, runtime in self._runtimes.items()
                if runtime.state == state and job_id in self._jobs
            ]

    def update_job_state(self, job_id: str, state: QueueState) -> bool:
        with self._lock:
            if job_id not in self._runtimes:
                return False
            runtime = self._runtimes[job_id]
            runtime.state = state
            if state == QueueState.RUNNING and not runtime.started_at:
                runtime.started_at = self._utc_now()
            if state in (QueueState.COMPLETED, QueueState.FAILED, QueueState.CANCELLED):
                runtime.completed_at = self._utc_now()
            self._save()
        return True

    def set_job_error(self, job_id: str, error_message: str) -> bool:
        with self._lock:
            if job_id not in self._runtimes:
                return False
            self._runtimes[job_id].error_message = error_message
            self._save()
        return True

    def set_job_result(self, job_id: str, result_summary: Dict[str, Any]) -> bool:
        with self._lock:
            if job_id not in self._runtimes:
                return False
            self._runtimes[job_id].result_summary = result_summary
            self._save()
        return True

    def increment_retry_count(self, job_id: str) -> int:
        with self._lock:
            if job_id not in self._runtimes:
                return 0
            self._runtimes[job_id].retry_count += 1
            self._save()
            return self._runtimes[job_id].retry_count

    def reset_job(self, job_id: str) -> bool:
        with self._lock:
            if job_id not in self._runtimes:
                return False
            self._runtimes[job_id] = QueueJobRuntime(job_id=job_id)
            self._save()
        return True

    def remove_job(self, job_id: str) -> bool:
        with self._lock:
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


def create_queue_job_id() -> str:
    return f"job_{uuid.uuid4().hex[:12]}"
