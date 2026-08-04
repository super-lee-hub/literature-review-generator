from __future__ import annotations

import json
import os
import secrets
import tempfile
import threading
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Dict


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


import time


_POINTER_LOCKS_GUARD = threading.Lock()
_POINTER_LOCKS: dict[str, threading.RLock] = {}


def _pointer_process_lock(path: str) -> threading.RLock:
    key = os.path.normcase(os.path.abspath(path))
    with _POINTER_LOCKS_GUARD:
        return _POINTER_LOCKS.setdefault(key, threading.RLock())


@contextmanager
def _latest_pointer_lock(pointer_path: str):
    process_lock = _pointer_process_lock(pointer_path)
    with process_lock:
        lock_path = pointer_path + ".lock"
        os.makedirs(os.path.dirname(lock_path), exist_ok=True)
        with open(lock_path, "a+b") as handle:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"latest pointer ownership lock\n")
                handle.flush()
                os.fsync(handle.fileno())
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

def atomic_write_json(path: str, payload: Any) -> None:
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)

    max_retries = 3
    retry_delay = 1.0

    for attempt in range(max_retries):
        fd, temp_path = tempfile.mkstemp(prefix=".tmp-", suffix=".json", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.replace(temp_path, path)
                return  # 成功写入，退出函数
            except (PermissionError, OSError, IOError) as e:
                if attempt < max_retries - 1:
                    # 退避重试
                    time.sleep(retry_delay)
                    continue
                else:
                    # 达到最大重试次数，抛出异常
                    raise
        finally:
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except Exception:
                    pass


def publish_json_artifact(
    publication_context: Any,
    registry: Any,
    path: str | os.PathLike[str],
    payload: Any,
    **register_kwargs: Any,
) -> Any:
    """Publish JSON through the explicit local/queue byte boundary.

    The context owns staging, lease validation, immutable finalization, and
    Registry registration.  Callers receive the Registry record so downstream
    dependencies use the finalized path rather than a mutable legacy target.
    """

    result = publication_context.publish_json(
        path,
        payload,
        registry=registry,
        register_kwargs=register_kwargs,
    )
    artifact = getattr(result, "artifact", None)
    if artifact is None:
        raise RuntimeError("publication context did not return a registered artifact")
    return artifact


def publish_bytes_artifact(
    publication_context: Any,
    registry: Any,
    path: str | os.PathLike[str],
    payload: bytes,
    **register_kwargs: Any,
) -> Any:
    """Publish non-JSON bytes through the same immutable byte boundary."""

    result = publication_context.publish_bytes(
        path,
        payload,
        registry=registry,
        register_kwargs=register_kwargs,
    )
    artifact = getattr(result, "artifact", None)
    if artifact is None:
        raise RuntimeError("publication context did not return a registered artifact")
    return artifact


@dataclass(frozen=True)
class WorkspacePaths:
    root_dir: str
    artifacts_dir: str
    checkpoints_dir: str
    logs_dir: str
    reports_dir: str
    registry_path: str


@dataclass(frozen=True)
class LatestJobPointer:
    project_name: str
    job_id: str
    workspace_path: str
    artifact_registry_path: str
    resume_state: str
    fingerprint_bundle: Dict[str, Any]
    status: str
    updated_at: str


class JobWorkspace:
    def __init__(self, base_output_dir: str, project_name: str, job_id: str) -> None:
        self.base_output_dir = os.path.abspath(base_output_dir)
        self.project_name = project_name
        self.job_id = job_id
        self.paths = WorkspacePaths(
            root_dir=os.path.join(self.base_output_dir, f"{project_name}__{job_id}"),
            artifacts_dir=os.path.join(self.base_output_dir, f"{project_name}__{job_id}", "artifacts"),
            checkpoints_dir=os.path.join(self.base_output_dir, f"{project_name}__{job_id}", "checkpoints"),
            logs_dir=os.path.join(self.base_output_dir, f"{project_name}__{job_id}", "logs"),
            reports_dir=os.path.join(self.base_output_dir, f"{project_name}__{job_id}", "reports"),
            registry_path=os.path.join(self.base_output_dir, f"{project_name}__{job_id}", "artifact_registry.json"),
        )

    @classmethod
    def create(cls, base_output_dir: str, project_name: str, job_id: str | None = None) -> "JobWorkspace":
        workspace = cls(base_output_dir=base_output_dir, project_name=project_name, job_id=job_id or cls.generate_job_id())
        workspace.ensure_exists()
        return workspace

    @staticmethod
    def generate_job_id() -> str:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        return f"{timestamp}_{secrets.token_hex(4)}"

    @classmethod
    def from_workspace_path(cls, workspace_path: str, project_name: str, job_id: str | None = None) -> "JobWorkspace":
        workspace_path = os.path.abspath(workspace_path)
        base_output_dir = os.path.dirname(workspace_path)
        derived_job_id = job_id
        prefix = f"{project_name}__"
        basename = os.path.basename(workspace_path)
        if derived_job_id is None and basename.startswith(prefix):
            derived_job_id = basename[len(prefix):]
        workspace = cls(base_output_dir=base_output_dir, project_name=project_name, job_id=derived_job_id or cls.generate_job_id())
        workspace.ensure_exists()
        return workspace

    def ensure_exists(self) -> None:
        os.makedirs(self.paths.root_dir, exist_ok=True)
        os.makedirs(self.paths.artifacts_dir, exist_ok=True)
        os.makedirs(self.paths.checkpoints_dir, exist_ok=True)
        os.makedirs(self.paths.logs_dir, exist_ok=True)
        os.makedirs(self.paths.reports_dir, exist_ok=True)

    @property
    def root_dir(self) -> str:
        return self.paths.root_dir

    def artifact_path(self, filename: str) -> str:
        return os.path.join(self.paths.artifacts_dir, filename)

    def checkpoint_path(self, filename: str) -> str:
        return os.path.join(self.paths.checkpoints_dir, filename)

    def report_path(self, filename: str) -> str:
        return os.path.join(self.paths.reports_dir, filename)

    def log_path(self, filename: str) -> str:
        return os.path.join(self.paths.logs_dir, filename)

    def project_pointer_dir(self) -> str:
        return os.path.join(self.base_output_dir, self.project_name)

    def latest_pointer_path(self) -> str:
        return os.path.join(self.project_pointer_dir(), "_latest_job.json")

    def write_latest_pointer(
        self,
        *,
        resume_state: str,
        fingerprint_bundle: Dict[str, Any],
        status: str,
    ) -> str:
        pointer = LatestJobPointer(
            project_name=self.project_name,
            job_id=self.job_id,
            workspace_path=self.paths.root_dir,
            artifact_registry_path=self.paths.registry_path,
            resume_state=resume_state,
            fingerprint_bundle=fingerprint_bundle,
            status=status,
            updated_at=utc_now_iso(),
        )
        pointer_dir = self.project_pointer_dir()
        os.makedirs(pointer_dir, exist_ok=True)
        pointer_path = self.latest_pointer_path()
        with _latest_pointer_lock(pointer_path):
            atomic_write_json(pointer_path, asdict(pointer))
        return pointer_path

    def write_latest_pointer_if_owned(
        self,
        *,
        resume_state: str,
        fingerprint_bundle: Dict[str, Any],
        status: str,
    ) -> bool:
        """Finalize the project pointer only while this job still owns it."""

        pointer_path = self.latest_pointer_path()
        with _latest_pointer_lock(pointer_path):
            try:
                with open(pointer_path, "r", encoding="utf-8") as handle:
                    current = json.load(handle)
            except (FileNotFoundError, json.JSONDecodeError, OSError):
                return False
            if not isinstance(current, dict) or str(current.get("job_id") or "") != self.job_id:
                return False
            pointer = LatestJobPointer(
                project_name=self.project_name,
                job_id=self.job_id,
                workspace_path=self.paths.root_dir,
                artifact_registry_path=self.paths.registry_path,
                resume_state=resume_state,
                fingerprint_bundle=fingerprint_bundle,
                status=status,
                updated_at=utc_now_iso(),
            )
            atomic_write_json(pointer_path, asdict(pointer))
            return True

