from __future__ import annotations

import json
import threading
import uuid
from dataclasses import asdict, dataclass, field, replace
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
    source_snapshot: Dict[str, Any] = field(default_factory=dict)
    input_fingerprint: str = ""
    config_fingerprint: str = ""
    current_stage: str = ""
    workspace_path: str = ""
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
    log_path: str = ""
    produced_artifacts: List[str] = field(default_factory=list)
    progress_snapshot: Dict[str, Any] = field(default_factory=dict)
    cancel_requested: bool = False
    cancel_requested_at: Optional[str] = None
    cancel_reason: Optional[str] = None

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
            "log_path": self.log_path,
            "produced_artifacts": self.produced_artifacts,
            "progress_snapshot": self.progress_snapshot,
            "cancel_requested": self.cancel_requested,
            "cancel_requested_at": self.cancel_requested_at,
            "cancel_reason": self.cancel_reason,
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
            log_path=data.get("log_path", ""),
            produced_artifacts=data.get("produced_artifacts", []),
            progress_snapshot=data.get("progress_snapshot", {}),
            cancel_requested=bool(data.get("cancel_requested", False)),
            cancel_requested_at=data.get("cancel_requested_at"),
            cancel_reason=data.get("cancel_reason"),
        )


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
                job
                for job_id, job in self._jobs.items()
                if job_id in self._runtimes and self._runtimes[job_id].state == state
            ]

    def update_job_state(self, job_id: str, state: QueueState) -> bool:
        with self._lock:
            if job_id not in self._runtimes:
                return False
            runtime = self._runtimes[job_id]
            if state == QueueState.COMPLETED and runtime.cancel_requested:
                # A late worker result must not overwrite durable cancellation
                # evidence with a false completed state.
                return False
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
            previous = self._runtimes[job_id]
            self._runtimes[job_id] = QueueJobRuntime(
                job_id=job_id,
                retry_count=previous.retry_count,
            )
            self._save()
        return True

    def request_cancel(self, job_id: str, *, reason: str = "user_requested") -> bool:
        """Persist a cooperative cancellation request for a live job."""

        with self._lock:
            runtime = self._runtimes.get(job_id)
            if runtime is None or runtime.state in (
                QueueState.COMPLETED,
                QueueState.FAILED,
                QueueState.CANCELLED,
            ):
                return False
            runtime.cancel_requested = True
            runtime.cancel_requested_at = self._utc_now()
            runtime.cancel_reason = reason
            runtime.state = QueueState.CANCELLED
            runtime.completed_at = self._utc_now()
            self._save()
        return True

    def clear_cancel_request(self, job_id: str) -> bool:
        with self._lock:
            runtime = self._runtimes.get(job_id)
            if runtime is None:
                return False
            runtime.cancel_requested = False
            runtime.cancel_requested_at = None
            runtime.cancel_reason = None
            self._save()
        return True

    def is_cancel_requested(self, job_id: str) -> bool:
        with self._lock:
            runtime = self._runtimes.get(job_id)
            return bool(runtime and runtime.cancel_requested)

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

    def save_queue(self, file_path: str | Path) -> None:
        """保存队列到文件"""
        save_path = Path(file_path)
        save_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
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
                with self._lock:
                    # 加载任务
                    for job_id, job_data in data.get("jobs", {}).items():
                        self._jobs[job_id] = QueueJobSpec.from_dict(job_data)
                    # 加载运行时信息
                    for job_id, runtime_data in data.get("runtimes", {}).items():
                        self._runtimes[job_id] = QueueJobRuntime.from_dict(runtime_data)
                    # 保存到当前队列文件
                    self._save()
            except (json.JSONDecodeError, KeyError):
                pass

    def reorder_jobs(self, job_ids: List[str]) -> None:
        """重排任务顺序"""
        # 按指定顺序重新排列任务
        with self._lock:
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

        import os
        from runtime.cancellation import CancellationRequestStore
        from services.job_workspace import JobWorkspace

        runtime_workspace = str(job_spec.workspace_path or "").strip()
        if runtime_workspace:
            workspace_path = Path(runtime_workspace).expanduser().resolve()
            project_name = workspace_path.name.rsplit("__", 1)[0]
            workspace = JobWorkspace.from_workspace_path(str(workspace_path), project_name, job_spec.job_id)
        else:
            project_name = str(job_spec.parameters.get("project_name") or job_spec.project_name or "project")
            base_output_dir = str(job_spec.parameters.get("output_dir") or os.path.join(os.getcwd(), "output"))
            workspace = JobWorkspace(base_output_dir, project_name, job_spec.job_id)
        return CancellationRequestStore(workspace).is_requested()

    @staticmethod
    def _clear_external_cancel(job_spec: QueueJobSpec) -> None:
        import os
        from runtime.cancellation import CancellationRequestStore
        from services.job_workspace import JobWorkspace

        runtime_workspace = str(job_spec.workspace_path or "").strip()
        if runtime_workspace:
            workspace_path = Path(runtime_workspace).expanduser().resolve()
            project_name = workspace_path.name.rsplit("__", 1)[0]
            workspace = JobWorkspace.from_workspace_path(str(workspace_path), project_name, job_spec.job_id)
        else:
            project_name = str(job_spec.parameters.get("project_name") or job_spec.project_name or "project")
            base_output_dir = str(job_spec.parameters.get("output_dir") or os.path.join(os.getcwd(), "output"))
            workspace = JobWorkspace(base_output_dir, project_name, job_spec.job_id)
        store = CancellationRequestStore(workspace)
        if store.read() is not None:
            store.clear(cleared_by="queue_runner", reason="retry")

    def _process_job(self, job_spec: QueueJobSpec) -> None:
        cancel_token = CancelToken()
        with self._lock:
            self._cancel_tokens[job_spec.job_id] = cancel_token

        try:
            if self._external_cancel_requested(job_spec):
                cancel_token.request_cancel()
                self.queue_service.request_cancel(job_spec.job_id, reason="external_cancel_request")
                return
            # 检查任务是否已经被取消
            if self.queue_service.is_cancel_requested(job_spec.job_id):
                cancel_token.request_cancel()
                self.queue_service.update_job_state(job_spec.job_id, QueueState.CANCELLED)
                return
            runtime = self.queue_service.get_job_runtime(job_spec.job_id)
            if runtime and runtime.state == QueueState.CANCELLED:
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
            self.queue_service.update_job_state(job_spec.job_id, QueueState.RUNNING)
            
            # 再次检查任务状态，确保没有被取消
            runtime = self.queue_service.get_job_runtime(job_spec.job_id)
            if runtime and (runtime.state == QueueState.CANCELLED or runtime.cancel_requested):
                cancel_token.request_cancel()
                return
            
            # 更新当前阶段
            self._update_job_stage(job_spec.job_id, "initializing")
            
            # 从job_spec参数构建JobRunRequest
            params = job_spec.parameters
            project_name = params.get("project_name")
            # Build JobRunRequest from queued parameters
            from services.job_runner import build_job_request_from_mapping
            request = build_job_request_from_mapping(params)
            progress_tracker = QueueRuntimeProgressTracker(
                lambda snapshot, jid=job_spec.job_id: self._update_job_progress_snapshot(jid, snapshot)
            )
            request = replace(request, progress_tracker=progress_tracker)
            
            
            # 计算工作区路径（基于项目名称和任务ID）
            import os
            base_output_dir = os.path.join(os.getcwd(), "output")
            workspace_path = os.path.join(base_output_dir, f"{project_name}__{job_spec.job_id}")
            
            # 获取工作区锁
            workspace_lock = self._get_workspace_lock(workspace_path)
            
            # 用工作区锁保护任务执行
            with workspace_lock:
                # 更新当前阶段
                self._update_job_stage(job_spec.job_id, "executing")
                
                # 执行任务，传入cancel_token
                result = self.job_runner.run(request, cancel_token=cancel_token)
            
            # 最后检查一次任务状态
            runtime = self.queue_service.get_job_runtime(job_spec.job_id)
            if runtime and (runtime.state == QueueState.CANCELLED or runtime.cancel_requested):
                return
            
            # 更新当前阶段
            self._update_job_stage(job_spec.job_id, "completing")
            self._update_job_runtime_info(job_spec.job_id, {
                "workspace_path": result.workspace_path,
                "log_path": result.log_path if hasattr(result, 'log_path') else job_spec.log_path,
                "produced_artifacts": result.produced_artifacts if hasattr(result, 'produced_artifacts') else [],
            })
            
            job_status = str(getattr(result, "job_status", "failed") or "failed")
            if job_status == "completed":
                # 更新任务状态为完成
                self.queue_service.update_job_state(job_spec.job_id, QueueState.COMPLETED)
                
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
                    self.queue_service.update_job_state(job_spec.job_id, QueueState.CANCELLED)
                else:
                    self.queue_service.update_job_state(job_spec.job_id, QueueState.FAILED)
                    self.queue_service.set_job_error(job_spec.job_id, result.message)
        except JobCancelledError:
            self.queue_service.update_job_state(job_spec.job_id, QueueState.CANCELLED)
        except Exception as e:
            # 检查是否是因为取消而导致的异常
            runtime = self.queue_service.get_job_runtime(job_spec.job_id)
            if runtime and runtime.state == QueueState.CANCELLED:
                return
            self.queue_service.update_job_state(job_spec.job_id, QueueState.FAILED)
            self.queue_service.set_job_error(job_spec.job_id, str(e))
        finally:
            with self._lock:
                if job_spec.job_id in self._cancel_tokens:
                    del self._cancel_tokens[job_spec.job_id]
    
    def _update_job_stage(self, job_id: str, stage: str) -> None:
        """更新任务的当前阶段"""
        with self._lock:
            if job_id in self.queue_service._runtimes:
                self.queue_service._runtimes[job_id].current_stage = stage
                self.queue_service._save()
    
    def _update_job_runtime_info(self, job_id: str, info: Dict[str, Any]) -> None:
        """更新任务的运行时信息"""
        with self._lock:
            if job_id in self.queue_service._runtimes:
                runtime = self.queue_service._runtimes[job_id]
                if 'workspace_path' in info:
                    runtime.workspace_path = info['workspace_path']
                if 'log_path' in info:
                    runtime.log_path = info['log_path']
                if 'produced_artifacts' in info:
                    runtime.produced_artifacts = info['produced_artifacts']
                self.queue_service._save()

    def _update_job_progress_snapshot(self, job_id: str, snapshot: Dict[str, Any]) -> None:
        """Persist the latest workflow progress snapshot for GUI queue inspection."""
        with self._lock:
            if job_id in self.queue_service._runtimes:
                runtime = self.queue_service._runtimes[job_id]
                runtime.progress_snapshot = dict(snapshot)
                stage = str(snapshot.get("stage") or "").strip()
                if stage:
                    runtime.current_stage = stage
                self.queue_service._save()

    def run(self) -> None:
        with self._lock:
            if self._running:
                return
            self._running = True

        try:
            processed_ids: set = set()
            max_passes = len(self.queue_service._jobs) * 2 + 10  # safety bound
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
