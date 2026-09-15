"""Small, fail-closed durability primitives shared by runtime state writers."""

from __future__ import annotations

import errno
import os
from contextlib import contextmanager
import time
from pathlib import Path
import threading
from typing import Any, Callable


class AtomicReplaceTimeoutError(OSError):
    """Raised when a retryable Windows replace never becomes available."""

    def __init__(self, target: str | os.PathLike[str], attempts: int, timeout_seconds: float) -> None:
        self.target = str(target)
        self.attempts = int(attempts)
        self.timeout_seconds = float(timeout_seconds)
        super().__init__(
            f"timed out atomically replacing {self.target!r} after "
            f"{self.timeout_seconds:.3f}s ({self.attempts} attempts)"
        )


class InterProcessLockTimeout(TimeoutError):
    """Raised when a durable file lock cannot be acquired in time."""


_FILE_LOCK_GUARD = threading.Lock()
_FILE_LOCKS: dict[str, threading.RLock] = {}


def _process_file_lock(path: Path) -> threading.RLock:
    key = str(path.resolve()).casefold()
    with _FILE_LOCK_GUARD:
        return _FILE_LOCKS.setdefault(key, threading.RLock())


@contextmanager
def interprocess_file_lock(
    target: str | os.PathLike[str],
    *,
    timeout_seconds: float = 5.0,
    retry_interval_seconds: float = 0.025,
):
    """Acquire a bounded lock shared by threads and independent processes.

    The lock lives beside 'target' as '<target>.lock'. The caller still owns
    the target's atomic publication; this helper only serializes the
    read/modify/write transaction that decides what may be published.
    """

    target_path = Path(target).expanduser().resolve()
    lock_path = target_path.with_name(target_path.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    timeout = max(0.0, float(timeout_seconds))
    process_lock = _process_file_lock(lock_path)
    if not process_lock.acquire(timeout=timeout):
        raise InterProcessLockTimeout(f"timed out acquiring process lock: {lock_path}")
    handle = None
    acquired = False
    try:
        handle = lock_path.open("a+b")
        if handle.seek(0, os.SEEK_END) == 0:
            handle.write(b"0")
            handle.flush()
            os.fsync(handle.fileno())
        handle.seek(0)
        deadline = time.monotonic() + timeout
        while not acquired:
            try:
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except (BlockingIOError, OSError) as exc:
                if time.monotonic() >= deadline:
                    raise InterProcessLockTimeout(
                        f"timed out acquiring process lock: {lock_path}"
                    ) from exc
                time.sleep(max(0.001, float(retry_interval_seconds)))
        yield
    finally:
        if handle is not None:
            if acquired:
                try:
                    handle.seek(0)
                    if os.name == "nt":
                        import msvcrt

                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
            handle.close()
        process_lock.release()


def is_retryable_atomic_replace_error(exc: BaseException) -> bool:
    """Return whether ``os.replace`` may safely be retried.

    Windows antivirus/indexer/share contention commonly reports WinError 5,
    32, or 33.  Other platforms do not get a broad ``PermissionError`` retry:
    a permission failure there is normally an actual authorization defect.
    """

    if not isinstance(exc, OSError) or os.name != "nt":
        return False
    winerror = getattr(exc, "winerror", None)
    if winerror in {5, 32, 33}:
        return True
    return isinstance(exc, PermissionError) and getattr(exc, "errno", None) in {
        errno.EACCES,
        errno.EBUSY,
        errno.EPERM,
    }


def atomic_replace_with_retry(
    source: str | os.PathLike[str],
    target: str | os.PathLike[str],
    *,
    timeout_seconds: float = 5.0,
    initial_backoff_seconds: float = 0.025,
    max_backoff_seconds: float = 0.5,
    replace: Callable[[str, str], Any] | None = None,
    sleep: Callable[[float], Any] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> None:
    """Replace ``target`` with a durable temp file under a bounded deadline.

    The caller owns writing and fsyncing ``source``.  This helper only handles
    the narrow, known-safe Windows sharing boundary; all other errors are
    raised immediately and the final retryable error is chained into the typed
    timeout.
    """

    source_path = os.fspath(source)
    target_path = os.fspath(target)
    timeout = max(0.0, float(timeout_seconds))
    deadline = monotonic() + timeout
    backoff = max(0.001, float(initial_backoff_seconds))
    maximum = max(backoff, float(max_backoff_seconds))
    attempts = 0
    last_error: OSError | None = None

    while True:
        attempts += 1
        try:
            (replace or os.replace)(source_path, target_path)
            return
        except OSError as exc:
            if not is_retryable_atomic_replace_error(exc):
                raise
            last_error = exc
            now = monotonic()
            if now >= deadline:
                raise AtomicReplaceTimeoutError(target_path, attempts, timeout) from last_error
            sleep(min(backoff, max(0.0, deadline - now)))
            backoff = min(maximum, backoff * 2.0)


def fsync_file(path: str | os.PathLike[str], payload: bytes) -> None:
    """Write and fsync bytes to an already-created temp path."""

    with Path(path).open("wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
