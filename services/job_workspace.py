from __future__ import annotations

import json
import ntpath
import os
import re
import secrets
import stat
import tempfile
import threading
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Dict

from services.durable_io import AtomicReplaceTimeoutError, atomic_replace_with_retry


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


import time


_POINTER_LOCKS_GUARD = threading.Lock()
_POINTER_LOCKS: dict[str, threading.RLock] = {}
DEFAULT_POINTER_LOCK_TIMEOUT_SECONDS = 5.0
DEFAULT_POINTER_LOCK_RETRY_INTERVAL_MS = 50


class WorkspacePathError(ValueError):
    """Raised when a workspace identity or child path is unsafe."""


class PointerLockTimeout(TimeoutError):
    """Raised when the latest-job pointer lock cannot be acquired in time."""


def _validate_path_component(value: str, *, field_name: str) -> str:
    candidate = str(value or "")
    if not candidate or candidate in {".", ".."}:
        raise WorkspacePathError(f"{field_name} must be a non-empty path component")
    if candidate != candidate.strip() or "/" in candidate or "\\" in candidate:
        raise WorkspacePathError(f"{field_name} must not contain separators or surrounding whitespace")
    if ntpath.isabs(candidate) or ntpath.splitdrive(candidate)[0]:
        raise WorkspacePathError(f"{field_name} must not be absolute")
    if any(ord(char) < 32 for char in candidate) or re.search(r'[<>:"|?*]', candidate):
        raise WorkspacePathError(f"{field_name} contains invalid Windows path characters")
    if candidate.endswith((".", " ")):
        raise WorkspacePathError(f"{field_name} must not end with a dot or space")
    reserved = {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(1, 10)} | {
        f"LPT{i}" for i in range(1, 10)
    }
    if candidate.split(".", 1)[0].upper() in reserved:
        raise WorkspacePathError(f"{field_name} uses a reserved Windows name")
    return candidate


def validate_path_component(value: str, *, field_name: str = "path component") -> str:
    """Validate one user-controlled component before it enters a filename."""

    return _validate_path_component(value, field_name=field_name)


def _is_reparse_path(path: str | os.PathLike[str]) -> bool:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return False
    flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return stat.S_ISLNK(info.st_mode) or bool(
        flag and int(getattr(info, "st_file_attributes", 0)) & flag
    )


def _descendant_path(
    base: str | os.PathLike[str],
    candidate: str | os.PathLike[str],
    *,
    allow_existing_reparse_leaf: bool = False,
) -> str:
    # realpath matters on Windows too: an existing junction/symlink must not
    # turn a lexical descendant into a path outside the configured root.
    lexical_base = os.path.abspath(os.fspath(base))
    lexical_candidate = os.path.abspath(os.fspath(candidate))
    base_path = os.path.realpath(lexical_base)
    candidate_path = os.path.realpath(lexical_candidate)
    try:
        common = os.path.commonpath([base_path, candidate_path])
    except ValueError as exc:
        raise WorkspacePathError("workspace path is on a different drive") from exc
    if os.path.normcase(common) != os.path.normcase(base_path):
        # A caller may need the lexical name of an already-created reparse
        # leaf in order to reject it before any batch writes.  Permit only
        # that one narrow inspection case: the lexical parent must still
        # resolve inside the root.  A missing child, a nested reparse point,
        # and any traversal outside the root remain fail-closed.
        if allow_existing_reparse_leaf and os.path.lexists(lexical_candidate):
            parent_path = os.path.realpath(os.path.dirname(lexical_candidate))
            try:
                parent_common = os.path.commonpath([base_path, parent_path])
            except ValueError:
                parent_common = ""
            if os.path.normcase(parent_common) == os.path.normcase(base_path):
                return lexical_candidate
        raise WorkspacePathError(f"path escapes configured output root: {candidate_path}")
    return lexical_candidate if allow_existing_reparse_leaf else candidate_path


def _pointer_process_lock(path: str) -> threading.RLock:
    key = os.path.normcase(os.path.abspath(path))
    with _POINTER_LOCKS_GUARD:
        return _POINTER_LOCKS.setdefault(key, threading.RLock())


@contextmanager
def _latest_pointer_lock(
    pointer_path: str,
    *,
    timeout_seconds: float = DEFAULT_POINTER_LOCK_TIMEOUT_SECONDS,
    retry_interval_ms: int = DEFAULT_POINTER_LOCK_RETRY_INTERVAL_MS,
):
    process_lock = _pointer_process_lock(pointer_path)
    timeout = max(0.0, float(timeout_seconds))
    if not process_lock.acquire(timeout=timeout):
        raise PointerLockTimeout(f"timed out acquiring pointer process lock: {pointer_path}")
    try:
        lock_path = pointer_path + ".lock"
        os.makedirs(os.path.dirname(lock_path), exist_ok=True)
        with open(lock_path, "a+b") as handle:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"latest pointer ownership lock\n")
                handle.flush()
                os.fsync(handle.fileno())
            handle.seek(0)
            acquired = False
            deadline = time.monotonic() + timeout
            if os.name == "nt":
                import msvcrt

                while not acquired:
                    try:
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                        acquired = True
                    except OSError as exc:
                        if time.monotonic() >= deadline:
                            raise PointerLockTimeout(f"timed out acquiring pointer lock: {lock_path}") from exc
                        time.sleep(max(1, int(retry_interval_ms)) / 1000.0)
            else:
                import fcntl

                while not acquired:
                    try:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        acquired = True
                    except (BlockingIOError, OSError) as exc:
                        if time.monotonic() >= deadline:
                            raise PointerLockTimeout(f"timed out acquiring pointer lock: {lock_path}") from exc
                        time.sleep(max(1, int(retry_interval_ms)) / 1000.0)
            try:
                yield
            finally:
                if acquired:
                    handle.seek(0)
                    if os.name == "nt":
                        import msvcrt

                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        process_lock.release()

def atomic_write_json(path: str, payload: Any) -> None:
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)

    fd, temp_path = tempfile.mkstemp(prefix=".tmp-", suffix=".json", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        atomic_replace_with_retry(temp_path, path, timeout_seconds=5.0)
    except AtomicReplaceTimeoutError:
        raise
    finally:
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
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
        self.base_output_dir = os.path.abspath(os.path.expanduser(str(base_output_dir)))
        self.project_name = _validate_path_component(project_name, field_name="project_name")
        self.job_id = _validate_path_component(job_id, field_name="job_id")
        root_dir = _descendant_path(
            self.base_output_dir,
            os.path.join(self.base_output_dir, f"{self.project_name}__{self.job_id}"),
            allow_existing_reparse_leaf=True,
        )
        self.paths = WorkspacePaths(
            root_dir=root_dir,
            artifacts_dir=_descendant_path(
                root_dir,
                os.path.join(root_dir, "artifacts"),
                allow_existing_reparse_leaf=True,
            ),
            checkpoints_dir=_descendant_path(
                root_dir,
                os.path.join(root_dir, "checkpoints"),
                allow_existing_reparse_leaf=True,
            ),
            logs_dir=_descendant_path(
                root_dir,
                os.path.join(root_dir, "logs"),
                allow_existing_reparse_leaf=True,
            ),
            reports_dir=_descendant_path(
                root_dir,
                os.path.join(root_dir, "reports"),
                allow_existing_reparse_leaf=True,
            ),
            registry_path=_descendant_path(
                root_dir,
                os.path.join(root_dir, "artifact_registry.json"),
                allow_existing_reparse_leaf=True,
            ),
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
        safe_project = _validate_path_component(project_name, field_name="project_name")
        prefix = f"{safe_project}__"
        basename = os.path.basename(workspace_path)
        if basename.startswith(prefix):
            path_job_id = basename[len(prefix):]
            if derived_job_id is not None and str(derived_job_id) != path_job_id:
                raise WorkspacePathError(
                    "explicit job_id does not match the requested workspace path"
                )
            derived_job_id = path_job_id
        elif derived_job_id is not None:
            raise WorkspacePathError("workspace path does not match project_name__job_id identity")
        workspace = cls(base_output_dir=base_output_dir, project_name=project_name, job_id=derived_job_id or cls.generate_job_id())
        if os.path.normcase(os.path.realpath(workspace.paths.root_dir)) != os.path.normcase(
            os.path.realpath(workspace_path)
        ):
            raise WorkspacePathError("workspace path identity does not match project_name/job_id")
        workspace.ensure_exists()
        return workspace

    def ensure_exists(self) -> None:
        for path in (
            self.paths.root_dir,
            self.paths.artifacts_dir,
            self.paths.checkpoints_dir,
            self.paths.logs_dir,
            self.paths.reports_dir,
            self.paths.registry_path,
        ):
            if _is_reparse_path(path):
                raise WorkspacePathError(f"workspace path must not be a symlink or reparse point: {path}")
        os.makedirs(self.paths.root_dir, exist_ok=True)
        os.makedirs(self.paths.artifacts_dir, exist_ok=True)
        os.makedirs(self.paths.checkpoints_dir, exist_ok=True)
        os.makedirs(self.paths.logs_dir, exist_ok=True)
        os.makedirs(self.paths.reports_dir, exist_ok=True)

    @property
    def root_dir(self) -> str:
        return self.paths.root_dir

    def artifact_path(self, filename: str) -> str:
        return _descendant_path(
            self.paths.artifacts_dir,
            os.path.join(self.paths.artifacts_dir, filename),
            allow_existing_reparse_leaf=True,
        )

    def checkpoint_path(self, filename: str) -> str:
        return _descendant_path(self.paths.checkpoints_dir, os.path.join(self.paths.checkpoints_dir, filename))

    def report_path(self, filename: str) -> str:
        return _descendant_path(self.paths.reports_dir, os.path.join(self.paths.reports_dir, filename))

    def log_path(self, filename: str) -> str:
        return _descendant_path(self.paths.logs_dir, os.path.join(self.paths.logs_dir, filename))

    def project_pointer_dir(self) -> str:
        return _descendant_path(self.base_output_dir, os.path.join(self.base_output_dir, self.project_name))

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

