from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Iterator, List, Literal, Mapping, Optional

from services.job_workspace import utc_now_iso


REGISTRY_VERSION = "v2"
SUPPORTED_REGISTRY_VERSIONS = frozenset({"v1", REGISTRY_VERSION})
DEFAULT_REGISTRY_LOCK_TIMEOUT_SECONDS = 5.0
DEFAULT_REGISTRY_LOCK_RETRY_INTERVAL_MS = 50
DEFAULT_REGISTRY_REVISION_RETRY_LIMIT = 3


class RegistryError(RuntimeError):
    """Base class for typed artifact-registry failures."""


class RegistryLockTimeout(RegistryError):
    """Raised when the registry transaction lock cannot be acquired in time."""


class RegistryRevisionConflict(RegistryError):
    """Raised when an explicit compare-and-swap revision is stale."""


class RegistryCorruption(RegistryError):
    """Raised when an existing registry cannot be decoded safely."""


class ArtifactConflict(RegistryError):
    """Raised when an artifact ID is reused for a different identity."""


class ArtifactNotFound(RegistryError):
    """Raised when a locked artifact mutation targets an unknown artifact."""


DependencyKind = Literal["local_job", "external_job"]


def file_sha256(path: str | os.PathLike[str]) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class ArtifactDependencyRefV2:
    dependency_kind: DependencyKind = "local_job"
    job_id: str = ""
    artifact_id: str = ""
    artifact_type: str = ""
    path: str = ""
    content_hash: str = ""

    def to_dict(self) -> Dict[str, str]:
        return asdict(self)

    @classmethod
    def from_dict(
        cls,
        payload: Mapping[str, Any],
        *,
        default_job_id: str = "",
    ) -> "ArtifactDependencyRefV2":
        """Read both V2 dependencies and the legacy type/path/hash projection."""

        artifact_type = str(payload.get("artifact_type") or payload.get("role") or "")
        path = os.fspath(payload.get("path") or "")
        artifact_id = str(payload.get("artifact_id") or "")
        if not artifact_id and artifact_type:
            artifact_id = _legacy_dependency_id(artifact_type, path)
        job_id = str(payload.get("job_id") or default_job_id)
        raw_kind = str(payload.get("dependency_kind") or "local_job")
        if raw_kind not in {"local_job", "external_job"}:
            raise RegistryCorruption(f"invalid dependency_kind: {raw_kind!r}")
        return cls(
            dependency_kind=raw_kind,  # type: ignore[arg-type]
            job_id=job_id,
            artifact_id=artifact_id,
            artifact_type=artifact_type,
            path=path,
            content_hash=str(payload.get("content_hash") or ""),
        )


class ArtifactDependencyRef(ArtifactDependencyRefV2):
    """Backward-compatible constructor for the legacy type/path/hash field order."""

    def __init__(
        self,
        artifact_type: str = "",
        path: str = "",
        content_hash: str = "",
        *,
        dependency_kind: DependencyKind = "local_job",
        job_id: str = "",
        artifact_id: str = "",
    ) -> None:
        object.__setattr__(self, "dependency_kind", dependency_kind)
        object.__setattr__(self, "job_id", job_id)
        object.__setattr__(self, "artifact_id", artifact_id)
        object.__setattr__(self, "artifact_type", artifact_type)
        object.__setattr__(self, "path", path)
        object.__setattr__(self, "content_hash", content_hash)


@dataclass(frozen=True)
class ArtifactRecord:
    artifact_id: str
    artifact_role: str
    artifact_type: str
    artifact_version: str
    path: str
    producer: str
    job_id: str
    status: str
    content_hash: str
    depends_on: List[ArtifactDependencyRefV2] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now_iso)


_PROCESS_LOCKS_GUARD = threading.Lock()
_PROCESS_LOCKS: Dict[str, threading.RLock] = {}


def _process_lock_for(path: str) -> threading.RLock:
    key = os.path.normcase(os.path.abspath(path))
    with _PROCESS_LOCKS_GUARD:
        lock = _PROCESS_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _PROCESS_LOCKS[key] = lock
        return lock


def _legacy_dependency_id(artifact_type: str, path: str) -> str:
    basename = os.path.basename(path) if path else "unknown"
    return f"{artifact_type}:{basename}"


class ArtifactRegistry:
    def __init__(
        self,
        registry_path: str | os.PathLike[str],
        job_id: str,
        *,
        registry_lock_timeout_seconds: float = DEFAULT_REGISTRY_LOCK_TIMEOUT_SECONDS,
        registry_lock_retry_interval_ms: int = DEFAULT_REGISTRY_LOCK_RETRY_INTERVAL_MS,
        registry_revision_retry_limit: int = DEFAULT_REGISTRY_REVISION_RETRY_LIMIT,
    ) -> None:
        self.registry_path = os.path.abspath(os.fspath(registry_path))
        self.lock_path = f"{self.registry_path}.lock"
        self.job_id = job_id
        self.registry_lock_timeout_seconds = max(0.0, float(registry_lock_timeout_seconds))
        self.registry_lock_retry_interval_ms = max(1, int(registry_lock_retry_interval_ms))
        self.registry_revision_retry_limit = max(1, int(registry_revision_retry_limit))
        self._process_lock = _process_lock_for(self.lock_path)
        self._artifacts: Dict[str, ArtifactRecord] = {}
        self._revision = 0
        self._load()

    @property
    def revision(self) -> int:
        return self._revision

    def _load(self) -> None:
        revision, artifacts = self._read_registry_unlocked()
        self._revision = revision
        self._artifacts = artifacts

    def reload(self) -> None:
        """Refresh this instance from durable state under the transaction lock."""

        with self._transaction_lock():
            self._load()

    def _read_registry_unlocked(self) -> tuple[int, Dict[str, ArtifactRecord]]:
        if not os.path.exists(self.registry_path):
            return 0, {}
        try:
            with open(self.registry_path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RegistryCorruption(f"cannot read registry {self.registry_path}: {exc}") from exc

        if not isinstance(payload, dict):
            raise RegistryCorruption("registry root must be a JSON object")
        raw_version = payload.get("artifact_registry_version")
        if not isinstance(raw_version, str) or raw_version not in SUPPORTED_REGISTRY_VERSIONS:
            raise RegistryCorruption(
                f"unsupported artifact_registry_version: {raw_version!r}"
            )
        raw_job_id = payload.get("job_id")
        if not isinstance(raw_job_id, str) or not raw_job_id:
            raise RegistryCorruption(f"invalid registry job_id: {raw_job_id!r}")
        if raw_job_id != self.job_id:
            raise RegistryCorruption(
                f"registry job_id {raw_job_id!r} does not match expected owner {self.job_id!r}"
            )
        raw_revision = payload.get("revision", 0)
        if isinstance(raw_revision, bool) or not isinstance(raw_revision, int) or raw_revision < 0:
            raise RegistryCorruption(f"invalid registry revision: {raw_revision!r}")
        raw_artifacts = payload.get("artifacts", [])
        if not isinstance(raw_artifacts, list):
            raise RegistryCorruption("registry artifacts must be a JSON array")

        default_job_id = raw_job_id
        artifacts: Dict[str, ArtifactRecord] = {}
        for index, item in enumerate(raw_artifacts):
            if not isinstance(item, dict):
                raise RegistryCorruption(f"artifact[{index}] must be a JSON object")
            artifact_id = str(item.get("artifact_id") or "")
            if not artifact_id:
                raise RegistryCorruption(f"artifact[{index}] has no artifact_id")
            dependencies_payload = item.get("depends_on", [])
            if not isinstance(dependencies_payload, list):
                raise RegistryCorruption(f"artifact[{index}].depends_on must be an array")
            dependencies = [
                ArtifactDependencyRefV2.from_dict(dependency, default_job_id=default_job_id)
                for dependency in dependencies_payload
                if isinstance(dependency, dict)
            ]
            if len(dependencies) != len(dependencies_payload):
                raise RegistryCorruption(f"artifact[{index}].depends_on contains a non-object")
            metadata_payload = item.get("metadata", {})
            if not isinstance(metadata_payload, dict):
                raise RegistryCorruption(f"artifact[{index}].metadata must be an object")
            record = ArtifactRecord(
                artifact_id=artifact_id,
                artifact_role=str(item.get("artifact_role") or ""),
                artifact_type=str(item.get("artifact_type") or ""),
                artifact_version=str(item.get("artifact_version") or ""),
                path=os.fspath(item.get("path") or ""),
                producer=str(item.get("producer") or ""),
                job_id=str(item.get("job_id") or default_job_id),
                status=str(item.get("status") or ""),
                content_hash=str(item.get("content_hash") or ""),
                depends_on=dependencies,
                metadata=dict(metadata_payload),
                created_at=str(item.get("created_at") or utc_now_iso()),
            )
            if record.job_id != self.job_id:
                raise RegistryCorruption(
                    f"artifact[{index}].job_id {record.job_id!r} does not match "
                    f"registry owner {self.job_id!r}"
                )
            previous = artifacts.get(artifact_id)
            if previous is not None and previous != record:
                raise RegistryCorruption(f"duplicate artifact_id with divergent records: {artifact_id}")
            artifacts[artifact_id] = record
        return raw_revision, artifacts

    @contextmanager
    def _transaction_lock(self) -> Iterator[None]:
        deadline = time.monotonic() + self.registry_lock_timeout_seconds
        process_timeout = max(0.0, deadline - time.monotonic())
        if not self._process_lock.acquire(timeout=process_timeout):
            raise RegistryLockTimeout(
                f"timed out acquiring process registry lock after "
                f"{self.registry_lock_timeout_seconds:.3f}s: {self.lock_path}"
            )
        lock_handle = None
        try:
            Path(self.lock_path).parent.mkdir(parents=True, exist_ok=True)
            lock_handle = open(self.lock_path, "a+b")
            self._ensure_lock_file_has_lockable_byte(lock_handle)
            while True:
                try:
                    self._acquire_os_lock(lock_handle)
                    break
                except (BlockingIOError, OSError):
                    if time.monotonic() >= deadline:
                        raise RegistryLockTimeout(
                            f"timed out acquiring registry lock after "
                            f"{self.registry_lock_timeout_seconds:.3f}s: {self.lock_path}"
                        )
                    time.sleep(self.registry_lock_retry_interval_ms / 1000.0)
            try:
                yield
            finally:
                self._release_os_lock(lock_handle)
        finally:
            if lock_handle is not None:
                lock_handle.close()
            self._process_lock.release()

    @staticmethod
    def _ensure_lock_file_has_lockable_byte(handle: Any) -> None:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            # Diagnostic-only content; lock validity is exclusively determined by the OS lock.
            handle.write(b"registry transaction lock\n")
            handle.flush()
            os.fsync(handle.fileno())

    @staticmethod
    def _acquire_os_lock(handle: Any) -> None:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    @staticmethod
    def _release_os_lock(handle: Any) -> None:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _write_registry_unlocked(
        self,
        artifacts: Mapping[str, ArtifactRecord],
        revision: int,
    ) -> None:
        payload = {
            "artifact_registry_version": REGISTRY_VERSION,
            "revision": revision,
            "job_id": self.job_id,
            "updated_at": utc_now_iso(),
            "artifacts": [
                {
                    **asdict(record),
                    "depends_on": [dependency.to_dict() for dependency in record.depends_on],
                }
                for _, record in sorted(artifacts.items())
            ],
        }
        try:
            encoded = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise RegistryCorruption(f"registry payload is not JSON serializable: {exc}") from exc

        directory = os.path.dirname(self.registry_path) or os.curdir
        os.makedirs(directory, exist_ok=True)
        fd, temp_path = tempfile.mkstemp(
            prefix=f".{os.path.basename(self.registry_path)}.",
            suffix=".tmp",
            dir=directory,
        )
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, self.registry_path)
            self._fsync_directory(directory)
        except Exception:
            try:
                os.unlink(temp_path)
            except FileNotFoundError:
                pass
            raise

    @staticmethod
    def _fsync_directory(directory: str) -> None:
        if os.name == "nt":
            return
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _validate_artifact_merge(
        existing: Optional[ArtifactRecord],
        candidate: ArtifactRecord,
    ) -> None:
        if existing is None:
            return
        conflicting_fields = [
            name
            for name in ("job_id", "artifact_type")
            if getattr(existing, name) != getattr(candidate, name)
        ]
        if conflicting_fields:
            raise ArtifactConflict(
                f"artifact_id {candidate.artifact_id!r} conflicts on "
                f"{', '.join(conflicting_fields)}"
            )

    def _normalize_dependencies(
        self,
        dependencies: Iterable[ArtifactDependencyRefV2 | Mapping[str, Any]],
        artifacts: Mapping[str, ArtifactRecord],
        *,
        owner_job_id: str,
    ) -> List[ArtifactDependencyRefV2]:
        normalized: List[ArtifactDependencyRefV2] = []
        for dependency in dependencies:
            if isinstance(dependency, ArtifactDependencyRefV2):
                ref = dependency
            elif isinstance(dependency, Mapping):
                ref = ArtifactDependencyRefV2.from_dict(dependency, default_job_id=owner_job_id)
            else:
                raise TypeError(f"unsupported dependency reference: {type(dependency).__name__}")

            registered = artifacts.get(ref.artifact_id) if ref.artifact_id else None
            if registered is None and ref.path:
                normalized_path = os.path.abspath(os.fspath(ref.path))
                registered = next(
                    (
                        record
                        for record in artifacts.values()
                        if os.path.normcase(record.path) == os.path.normcase(normalized_path)
                        and (not ref.artifact_type or record.artifact_type == ref.artifact_type)
                    ),
                    None,
                )
            artifact_type = ref.artifact_type or (registered.artifact_type if registered else "unknown")
            path = ref.path or (registered.path if registered else "")
            artifact_id = ref.artifact_id or (
                registered.artifact_id if registered else _legacy_dependency_id(artifact_type, path)
            )
            job_id = ref.job_id or (registered.job_id if registered else owner_job_id)
            content_hash = ref.content_hash or (registered.content_hash if registered else "")
            dependency_kind: DependencyKind = ref.dependency_kind
            if job_id and job_id != owner_job_id:
                dependency_kind = "external_job"
            normalized.append(
                ArtifactDependencyRefV2(
                    dependency_kind=dependency_kind,
                    job_id=job_id,
                    artifact_id=artifact_id,
                    artifact_type=artifact_type,
                    path=os.fspath(path),
                    content_hash=content_hash,
                )
            )
        return normalized

    @staticmethod
    def _validate_ready_path(path: str, status: str) -> str:
        abs_path = os.path.abspath(os.fspath(path))
        if status == "ready" and not os.path.exists(abs_path):
            raise FileNotFoundError(f"ready artifact does not exist: {abs_path}")
        if os.path.exists(abs_path) and not os.path.isfile(abs_path):
            raise IsADirectoryError(f"artifact path is not a file: {abs_path}")
        return abs_path

    def _register_transaction(
        self,
        build_record: Callable[[Mapping[str, ArtifactRecord]], ArtifactRecord],
        *,
        expected_revision: Optional[int],
    ) -> ArtifactRecord:
        with self._transaction_lock():
            disk_revision, artifacts = self._read_registry_unlocked()
            if expected_revision is not None and disk_revision != expected_revision:
                raise RegistryRevisionConflict(
                    f"expected registry revision {expected_revision}, found {disk_revision}"
                )
            candidate = build_record(artifacts)
            if candidate.job_id != self.job_id:
                raise ArtifactConflict(
                    f"artifact {candidate.artifact_id!r} belongs to {candidate.job_id!r}, "
                    f"not registry owner {self.job_id!r}"
                )
            existing = artifacts.get(candidate.artifact_id)
            self._validate_artifact_merge(existing, candidate)
            if existing is not None and existing.created_at:
                candidate = replace(candidate, created_at=existing.created_at)
            merged = dict(artifacts)
            merged[candidate.artifact_id] = candidate
            next_revision = disk_revision + 1
            self._write_registry_unlocked(merged, next_revision)
            # Memory changes only after the durable os.replace succeeds.
            self._artifacts = merged
            self._revision = next_revision
            return candidate

    def save(self, *, expected_revision: Optional[int] = None) -> None:
        """Persist an explicit in-memory snapshot with compare-and-swap protection.

        Registration callers should use :meth:`register_file` or :meth:`register`.
        This compatibility method defaults to the instance's loaded revision so a
        stale instance cannot overwrite a newer durable registry.
        """

        compare_revision = self._revision if expected_revision is None else expected_revision
        with self._transaction_lock():
            disk_revision, disk_artifacts = self._read_registry_unlocked()
            if disk_revision != compare_revision:
                raise RegistryRevisionConflict(
                    f"expected registry revision {compare_revision}, found {disk_revision}"
                )
            for record in self._artifacts.values():
                if record.job_id != self.job_id:
                    raise ArtifactConflict(
                        f"artifact {record.artifact_id!r} belongs to {record.job_id!r}, "
                        f"not registry owner {self.job_id!r}"
                    )
                self._validate_artifact_merge(disk_artifacts.get(record.artifact_id), record)
            next_revision = disk_revision + 1
            snapshot = dict(self._artifacts)
            self._write_registry_unlocked(snapshot, next_revision)
            self._revision = next_revision

    def list_records(self) -> List[ArtifactRecord]:
        return list(self._artifacts.values())

    def get(self, artifact_id: str) -> Optional[ArtifactRecord]:
        return self._artifacts.get(artifact_id)

    def register_file(
        self,
        *,
        artifact_role: str,
        artifact_type: str,
        artifact_version: str,
        path: str | os.PathLike[str],
        producer: str,
        status: str = "ready",
        depends_on: Iterable[ArtifactDependencyRefV2 | Mapping[str, Any]] | None = None,
        artifact_id: str | None = None,
        expected_revision: int | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> ArtifactRecord:
        def build_record(artifacts: Mapping[str, ArtifactRecord]) -> ArtifactRecord:
            abs_path = self._validate_ready_path(os.fspath(path), status)
            content_hash = file_sha256(abs_path) if os.path.exists(abs_path) else ""
            return ArtifactRecord(
                artifact_id=artifact_id or f"{artifact_type}:{os.path.basename(abs_path)}",
                artifact_role=artifact_role,
                artifact_type=artifact_type,
                artifact_version=artifact_version,
                path=abs_path,
                producer=producer,
                job_id=self.job_id,
                status=status,
                content_hash=content_hash,
                depends_on=self._normalize_dependencies(
                    depends_on or [], artifacts, owner_job_id=self.job_id
                ),
                metadata=dict(metadata or {}),
                created_at=utc_now_iso(),
            )

        return self._register_transaction(build_record, expected_revision=expected_revision)

    def register(
        self,
        *,
        artifact_id: str,
        artifact_type: str,
        artifact_version: str,
        path: str | os.PathLike[str],
        producer: str,
        job_id: str,
        status: str = "ready",
        depends_on: Iterable[ArtifactDependencyRefV2 | Mapping[str, Any]] | None = None,
        artifact_role: str | None = None,
        expected_revision: int | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> ArtifactRecord:
        """Compatibility registration API routed through the V2 transaction."""

        def build_record(artifacts: Mapping[str, ArtifactRecord]) -> ArtifactRecord:
            abs_path = self._validate_ready_path(os.fspath(path), status)
            content_hash = file_sha256(abs_path) if os.path.exists(abs_path) else ""
            return ArtifactRecord(
                artifact_id=artifact_id,
                artifact_role=artifact_role or artifact_type,
                artifact_type=artifact_type,
                artifact_version=artifact_version,
                path=abs_path,
                producer=producer,
                job_id=job_id,
                status=status,
                content_hash=content_hash,
                depends_on=self._normalize_dependencies(
                    depends_on or [], artifacts, owner_job_id=job_id
                ),
                metadata=dict(metadata or {}),
                created_at=utc_now_iso(),
            )

        return self._register_transaction(build_record, expected_revision=expected_revision)

    def update_record(
        self,
        artifact_id: str,
        *,
        status: str | None = None,
        depends_on: Iterable[ArtifactDependencyRefV2 | Mapping[str, Any]] | None = None,
        metadata_updates: Mapping[str, Any] | None = None,
        expected_revision: int | None = None,
    ) -> ArtifactRecord:
        """Update mutable artifact state through the same locked CAS transaction."""

        if status is not None and status not in {"ready", "quarantined", "invalid"}:
            raise ValueError(f"unsupported artifact status: {status}")

        def build_record(artifacts: Mapping[str, ArtifactRecord]) -> ArtifactRecord:
            current = artifacts.get(artifact_id)
            if current is None:
                raise ArtifactNotFound(f"artifact not found: {artifact_id}")
            next_status = status or current.status
            self._validate_ready_path(current.path, next_status)
            next_dependencies = (
                self._normalize_dependencies(depends_on, artifacts, owner_job_id=current.job_id)
                if depends_on is not None
                else list(current.depends_on)
            )
            next_metadata = dict(current.metadata)
            next_metadata.update(dict(metadata_updates or {}))
            return replace(
                current,
                status=next_status,
                depends_on=next_dependencies,
                metadata=next_metadata,
            )

        return self._register_transaction(build_record, expected_revision=expected_revision)
