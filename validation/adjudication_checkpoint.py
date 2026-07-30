"""Immutable checkpoints around paid Validation adjudication calls."""

from __future__ import annotations

import errno
import json
import os
from pathlib import Path
import threading
import time
from contextlib import contextmanager
from typing import Any, Mapping

from services.job_workspace import atomic_write_json, utc_now_iso
from validation.edge_checkpoint import canonical_hash
from validation.run_result import VALIDATION_RUN_SCHEMA_VERSION


ADJUDICATION_CHECKPOINT_VERSION = "v1"
ADJUDICATION_PROMPT_VERSION = "validation_adjudication_prompt_v1"
_KEY_LOCKS_GUARD = threading.RLock()
_KEY_LOCKS: dict[str, tuple[threading.RLock, int]] = {}
_SINGLE_FLIGHT_LOCAL = threading.local()
_LOCK_RETRY_SECONDS = 0.05


@contextmanager
def _os_file_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b", buffering=0) as lock_file:
        if os.name == "nt":
            import msvcrt

            lock_file.seek(0, os.SEEK_END)
            if lock_file.tell() == 0:
                lock_file.write(b"\0")

            while True:
                lock_file.seek(0)
                try:
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError as exc:
                    if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                        raise
                    time.sleep(_LOCK_RETRY_SECONDS)
            try:
                yield
            finally:
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            return

        import fcntl

        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


@contextmanager
def _reentrant_process_lock(identity: str, lock_path: Path):
    depths = getattr(_SINGLE_FLIGHT_LOCAL, "depths", None)
    if depths is None:
        depths = {}
        _SINGLE_FLIGHT_LOCAL.depths = depths

    depth = depths.get(identity, 0)
    if depth:
        depths[identity] = depth + 1
        try:
            yield
        finally:
            depths[identity] = depth
        return

    with _os_file_lock(lock_path):
        depths[identity] = 1
        try:
            yield
        finally:
            depths.pop(identity, None)


def sanitized_route_hash(api_config: Mapping[str, Any]) -> str:
    secret_fragments = ("key", "token", "secret", "password", "authorization")
    sanitized = {
        str(key): value
        for key, value in api_config.items()
        if not any(fragment in str(key).lower() for fragment in secret_fragments)
    }
    return canonical_hash(sanitized)


class AdjudicationCheckpointStore:
    def __init__(self, root_dir: str | os.PathLike[str]):
        self.root_dir = Path(root_dir).resolve()
        self.root_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def key_for(self, *, packet: Mapping[str, Any], stage: str, route_hash: str) -> str:
        return canonical_hash(
            {
                "artifact_version": ADJUDICATION_CHECKPOINT_VERSION,
                "packet": packet,
                "stage": stage,
                "route_hash": route_hash,
                "prompt_version": ADJUDICATION_PROMPT_VERSION,
                "adjudication_schema_version": VALIDATION_RUN_SCHEMA_VERSION,
            }
        )

    def load(self, key: str) -> dict[str, Any] | None:
        path = self.root_dir / f"{key}.json"
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
        if (
            payload.get("artifact_type") != "validation_adjudication_checkpoint"
            or payload.get("artifact_version") != ADJUDICATION_CHECKPOINT_VERSION
            or payload.get("checkpoint_key") != key
        ):
            return None
        result = payload.get("result")
        return dict(result) if isinstance(result, Mapping) else None

    @contextmanager
    def single_flight(self, key: str):
        checkpoint_path = self.root_dir / f"{key}.json"
        identity = os.path.normcase(str(checkpoint_path))
        lock_path = checkpoint_path.with_name(f"{checkpoint_path.name}.lock")
        with _KEY_LOCKS_GUARD:
            lock, users = _KEY_LOCKS.get(identity, (threading.RLock(), 0))
            _KEY_LOCKS[identity] = (lock, users + 1)
        try:
            with lock:
                with _reentrant_process_lock(identity, lock_path):
                    yield
        finally:
            with _KEY_LOCKS_GUARD:
                current_lock, current_users = _KEY_LOCKS[identity]
                if current_users <= 1:
                    del _KEY_LOCKS[identity]
                else:
                    _KEY_LOCKS[identity] = (current_lock, current_users - 1)

    def save(self, key: str, result: Mapping[str, Any]) -> tuple[str, bool]:
        path = self.root_dir / f"{key}.json"
        with self._lock:
            existing = self.load(key)
            if existing is not None:
                if canonical_hash(existing) != canonical_hash(result):
                    raise ValueError("immutable adjudication checkpoint collision")
                return str(path), False
            atomic_write_json(
                str(path),
                {
                    "artifact_type": "validation_adjudication_checkpoint",
                    "artifact_version": ADJUDICATION_CHECKPOINT_VERSION,
                    "checkpoint_key": key,
                    "result": dict(result),
                    "created_at": utc_now_iso(),
                },
            )
        return str(path), True
