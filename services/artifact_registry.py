from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import time
from copy import deepcopy
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Iterator, List, Literal, Mapping, Optional

from services.job_workspace import utc_now_iso


REGISTRY_VERSION = "v2"
SUPPORTED_REGISTRY_VERSIONS = frozenset({REGISTRY_VERSION})
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


class UnverifiedDependency(RegistryError):
    """Raised when a ready artifact dependency cannot be verified durably."""


class UnverifiedArtifact(RegistryError):
    """Raised when a ready artifact's durable file identity cannot be verified."""


class PublicationFenceRejected(RegistryError):
    """Raised when a guarded publication context is no longer authorized."""


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
    def from_record(cls, record: Any, *, dependency_kind: DependencyKind = "local_job") -> "ArtifactDependencyRefV2":
        """Build a complete current reference from a registered artifact record."""

        return cls(
            dependency_kind=dependency_kind,
            job_id=str(getattr(record, "job_id", "")),
            artifact_id=str(getattr(record, "artifact_id", "")),
            artifact_type=str(getattr(record, "artifact_type", "")),
            path=os.fspath(getattr(record, "path", "")),
            content_hash=str(getattr(record, "content_hash", "")),
        )

    @classmethod
    def from_dict(
        cls,
        payload: Mapping[str, Any],
    ) -> "ArtifactDependencyRefV2":
        """Read one strict current dependency reference."""

        required = ("dependency_kind", "job_id", "artifact_id", "artifact_type", "path", "content_hash")
        missing = [key for key in required if key not in payload or not str(payload.get(key) or "").strip()]
        if missing:
            raise RegistryCorruption(
                "current dependency reference is missing required fields: " + ", ".join(missing)
            )
        if set(payload).intersection({"role"}):
            raise RegistryCorruption("legacy dependency field 'role' is not accepted")
        artifact_type = str(payload["artifact_type"])
        path = os.fspath(payload["path"])
        artifact_id = str(payload["artifact_id"])
        job_id = str(payload["job_id"])
        raw_kind = str(payload["dependency_kind"])
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


@dataclass(frozen=True)
class CurrentArtifactSetV1:
    """The only artifact set which completion and promotion may consume."""

    set_id: str
    job_id: str
    promotion_transaction_id: str
    promotion_transaction_hash: str
    review_draft_artifact_id: str
    review_draft_artifact_hash: str
    citation_manifest_artifact_id: str
    citation_manifest_artifact_hash: str
    review_docx_artifact_id: str
    review_docx_artifact_hash: str
    validation_run_result_artifact_id: str
    validation_run_result_artifact_hash: str
    validation_receipt_closure_artifact_id: str
    validation_receipt_closure_artifact_hash: str
    validation_status: str = "clean"
    validation_disposition_artifact_id: str = ""
    validation_disposition_artifact_hash: str = ""
    previous_set_id: str = ""
    actor: str = ""
    reason: str = ""
    created_at: str = field(default_factory=utc_now_iso)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "CurrentArtifactSetV1":
        required = (
            "artifact_type",
            "artifact_version",
            "set_id",
            "job_id",
            "promotion_transaction_id",
            "promotion_transaction_hash",
            "review_draft_artifact_id",
            "review_draft_artifact_hash",
            "citation_manifest_artifact_id",
            "citation_manifest_artifact_hash",
            "review_docx_artifact_id",
            "review_docx_artifact_hash",
            "validation_receipt_closure_artifact_id",
            "validation_receipt_closure_artifact_hash",
            "actor",
            "reason",
            "created_at",
        )
        missing = [key for key in required if not str(payload.get(key) or "").strip()]
        if missing:
            raise RegistryCorruption("current artifact set is missing: " + ", ".join(missing))
        validation_status = str(payload.get("validation_status") or "clean")
        if validation_status not in {"clean", "findings", "not_requested"}:
            raise RegistryCorruption(f"invalid current artifact set validation_status: {validation_status!r}")
        if validation_status == "not_requested":
            conditional = (
                "validation_disposition_artifact_id",
                "validation_disposition_artifact_hash",
            )
        else:
            conditional = (
                "validation_run_result_artifact_id",
                "validation_run_result_artifact_hash",
            )
        conditional_missing = [key for key in conditional if not str(payload.get(key) or "").strip()]
        if conditional_missing:
            raise RegistryCorruption(
                "current artifact set is missing validation evidence: "
                + ", ".join(conditional_missing)
            )
        return cls(
            set_id=str(payload["set_id"]),
            job_id=str(payload["job_id"]),
            promotion_transaction_id=str(payload["promotion_transaction_id"]),
            promotion_transaction_hash=str(payload["promotion_transaction_hash"]),
            review_draft_artifact_id=str(payload["review_draft_artifact_id"]),
            review_draft_artifact_hash=str(payload["review_draft_artifact_hash"]),
            citation_manifest_artifact_id=str(payload["citation_manifest_artifact_id"]),
            citation_manifest_artifact_hash=str(payload["citation_manifest_artifact_hash"]),
            review_docx_artifact_id=str(payload["review_docx_artifact_id"]),
            review_docx_artifact_hash=str(payload["review_docx_artifact_hash"]),
            validation_run_result_artifact_id=str(payload.get("validation_run_result_artifact_id") or ""),
            validation_run_result_artifact_hash=str(payload.get("validation_run_result_artifact_hash") or ""),
            validation_receipt_closure_artifact_id=str(payload["validation_receipt_closure_artifact_id"]),
            validation_receipt_closure_artifact_hash=str(payload["validation_receipt_closure_artifact_hash"]),
            validation_status=validation_status,
            validation_disposition_artifact_id=str(payload.get("validation_disposition_artifact_id") or ""),
            validation_disposition_artifact_hash=str(payload.get("validation_disposition_artifact_hash") or ""),
            previous_set_id=str(payload.get("previous_set_id") or ""),
            actor=str(payload["actor"]),
            reason=str(payload["reason"]),
            created_at=str(payload["created_at"]),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "artifact_type": "current_artifact_set",
            "artifact_version": "v1",
            **asdict(self),
        }

    def target_artifact_pairs(self) -> tuple[tuple[str, str], ...]:
        validation_pair = (
            (self.validation_disposition_artifact_id, self.validation_disposition_artifact_hash)
            if self.validation_status == "not_requested"
            else (self.validation_run_result_artifact_id, self.validation_run_result_artifact_hash)
        )
        return (
            (self.review_draft_artifact_id, self.review_draft_artifact_hash),
            (self.citation_manifest_artifact_id, self.citation_manifest_artifact_hash),
            (self.review_docx_artifact_id, self.review_docx_artifact_hash),
            validation_pair,
            (self.validation_receipt_closure_artifact_id, self.validation_receipt_closure_artifact_hash),
        )


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


class ArtifactRegistry:
    def __init__(
        self,
        registry_path: str | os.PathLike[str],
        job_id: str,
        *,
        registry_lock_timeout_seconds: float = DEFAULT_REGISTRY_LOCK_TIMEOUT_SECONDS,
        registry_lock_retry_interval_ms: int = DEFAULT_REGISTRY_LOCK_RETRY_INTERVAL_MS,
        registry_revision_retry_limit: int = DEFAULT_REGISTRY_REVISION_RETRY_LIMIT,
        publication_guard: Callable[[], Mapping[str, Any]] | None = None,
    ) -> None:
        self.registry_path = os.path.abspath(os.fspath(registry_path))
        self.lock_path = f"{self.registry_path}.lock"
        self.job_id = job_id
        self.registry_lock_timeout_seconds = max(0.0, float(registry_lock_timeout_seconds))
        self.registry_lock_retry_interval_ms = max(1, int(registry_lock_retry_interval_ms))
        self.registry_revision_retry_limit = max(1, int(registry_revision_retry_limit))
        self._publication_guard = publication_guard
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
                ArtifactDependencyRefV2.from_dict(dependency)
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
                job_id=str(item.get("job_id") or raw_job_id),
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
        if existing.artifact_type == "repair_promotion_transaction" and existing.status == "ready":
            immutable_fields = (
                "artifact_role",
                "artifact_type",
                "artifact_version",
                "path",
                "producer",
                "job_id",
                "status",
                "content_hash",
                "depends_on",
                "metadata",
            )
            changed = [
                field_name
                for field_name in immutable_fields
                if getattr(existing, field_name) != getattr(candidate, field_name)
            ]
            if changed:
                raise ArtifactConflict(
                    f"READY promotion transaction is immutable: {candidate.artifact_id!r}; "
                    f"changed fields: {', '.join(changed)}"
                )

    def _normalize_dependencies(
        self,
        dependencies: Iterable[ArtifactDependencyRefV2 | Mapping[str, Any]],
        artifacts: Mapping[str, ArtifactRecord],
        *,
        owner_job_id: str,
        require_ready: bool = False,
        external_registry_resolver: Callable[[str], Optional["ArtifactRegistry"]] | None = None,
    ) -> List[ArtifactDependencyRefV2]:
        normalized: List[ArtifactDependencyRefV2] = []
        for dependency in dependencies:
            if isinstance(dependency, ArtifactDependencyRefV2):
                ref = dependency
            elif isinstance(dependency, Mapping):
                ref = ArtifactDependencyRefV2.from_dict(dependency)
            else:
                raise TypeError(f"unsupported dependency reference: {type(dependency).__name__}")

            ref = ArtifactDependencyRefV2.from_dict(ref.to_dict())
            registered = artifacts.get(ref.artifact_id) if ref.artifact_id else None
            if registered is not None:
                if registered.job_id != ref.job_id:
                    raise UnverifiedDependency(f"dependency job_id mismatch: {ref.artifact_id}")
                if registered.artifact_type != ref.artifact_type:
                    raise UnverifiedDependency(f"dependency artifact_type mismatch: {ref.artifact_id}")
                if os.path.abspath(registered.path) != os.path.abspath(ref.path):
                    raise UnverifiedDependency(f"dependency path mismatch: {ref.artifact_id}")
                if registered.content_hash != ref.content_hash:
                    raise UnverifiedDependency(f"dependency content hash mismatch: {ref.artifact_id}")
            artifact_type = ref.artifact_type
            path = ref.path
            artifact_id = ref.artifact_id
            job_id = ref.job_id
            content_hash = ref.content_hash
            dependency_kind: DependencyKind = ref.dependency_kind
            if job_id and job_id != owner_job_id:
                dependency_kind = "external_job"
            normalized_ref = ArtifactDependencyRefV2(
                dependency_kind=dependency_kind,
                job_id=job_id,
                artifact_id=artifact_id,
                artifact_type=artifact_type,
                path=os.fspath(path),
                content_hash=content_hash,
            )
            if require_ready:
                normalized_ref = self._verify_ready_dependency(
                    normalized_ref,
                    artifacts=artifacts,
                    owner_job_id=owner_job_id,
                    external_registry_resolver=external_registry_resolver,
                )
            normalized.append(normalized_ref)
        return normalized

    def _verify_ready_dependency(
        self,
        ref: ArtifactDependencyRefV2,
        *,
        artifacts: Mapping[str, ArtifactRecord],
        owner_job_id: str,
        external_registry_resolver: Callable[[str], Optional["ArtifactRegistry"]] | None,
    ) -> ArtifactDependencyRefV2:
        if ref.dependency_kind == "local_job":
            if ref.job_id and ref.job_id != owner_job_id:
                raise UnverifiedDependency(
                    f"local dependency names another job: {ref.job_id}/{ref.artifact_id}"
                )
            record = artifacts.get(ref.artifact_id)
        else:
            if not ref.job_id or ref.job_id == owner_job_id:
                raise UnverifiedDependency(
                    f"external dependency has invalid job identity: {ref.job_id}/{ref.artifact_id}"
                )
            if external_registry_resolver is None:
                raise UnverifiedDependency(
                    f"external dependency cannot be verified without a resolver: "
                    f"{ref.job_id}/{ref.artifact_id}"
                )
            target_registry = external_registry_resolver(ref.job_id)
            if target_registry is None:
                raise UnverifiedDependency(
                    f"external dependency Registry is unavailable: {ref.job_id}/{ref.artifact_id}"
                )
            if target_registry.job_id != ref.job_id:
                raise UnverifiedDependency(
                    f"external dependency Registry owner mismatch: {ref.job_id}/{ref.artifact_id}"
                )
            target_registry.reload()
            record = target_registry.get(ref.artifact_id)

        if record is None:
            raise UnverifiedDependency(
                f"dependency is not registered: {ref.job_id or owner_job_id}/{ref.artifact_id}"
            )
        if record.status != "ready":
            raise UnverifiedDependency(
                f"dependency is not ready: {record.job_id}/{record.artifact_id} ({record.status})"
            )
        if ref.job_id and record.job_id != ref.job_id:
            raise UnverifiedDependency(f"dependency job_id mismatch: {ref.artifact_id}")
        if ref.artifact_type and record.artifact_type != ref.artifact_type:
            raise UnverifiedDependency(f"dependency artifact_type mismatch: {ref.artifact_id}")

        record_path = os.path.abspath(os.fspath(record.path))
        if ref.path and os.path.normcase(os.path.abspath(os.fspath(ref.path))) != os.path.normcase(record_path):
            raise UnverifiedDependency(f"dependency path mismatch: {ref.artifact_id}")
        if not os.path.isfile(record_path):
            raise UnverifiedDependency(f"dependency file is missing: {ref.artifact_id}")
        if not record.content_hash:
            raise UnverifiedDependency(f"dependency content hash is missing: {ref.artifact_id}")
        actual_hash = file_sha256(record_path)
        if actual_hash != record.content_hash:
            raise UnverifiedDependency(f"dependency content hash changed: {ref.artifact_id}")
        if ref.content_hash and ref.content_hash != record.content_hash:
            raise UnverifiedDependency(f"dependency declared hash mismatch: {ref.artifact_id}")

        return ArtifactDependencyRefV2(
            dependency_kind=ref.dependency_kind,
            job_id=record.job_id,
            artifact_id=record.artifact_id,
            artifact_type=record.artifact_type,
            path=record_path,
            content_hash=record.content_hash,
        )

    @staticmethod
    def _validate_ready_path(path: str, status: str) -> str:
        abs_path = os.path.abspath(os.fspath(path))
        if status == "ready" and not os.path.exists(abs_path):
            raise FileNotFoundError(f"ready artifact does not exist: {abs_path}")
        if os.path.exists(abs_path) and not os.path.isfile(abs_path):
            raise IsADirectoryError(f"artifact path is not a file: {abs_path}")
        return abs_path

    @classmethod
    def _verify_ready_artifact(cls, record: ArtifactRecord) -> None:
        artifact_path = cls._validate_ready_path(record.path, "ready")
        if not record.content_hash:
            raise UnverifiedArtifact(
                f"artifact content hash is missing: {record.artifact_id}"
            )
        if file_sha256(artifact_path) != record.content_hash:
            raise UnverifiedArtifact(
                f"artifact content hash changed: {record.artifact_id}"
            )
        try:
            from runtime.artifact_validators import validate_registered_artifact

            validate_registered_artifact(record, artifact_path)
        except ValueError as exc:
            raise UnverifiedArtifact(
                f"artifact schema is invalid: {record.artifact_id}: {exc}"
            ) from exc

    @staticmethod
    def _copy_record(record: ArtifactRecord) -> ArtifactRecord:
        return replace(
            record,
            depends_on=list(record.depends_on),
            metadata=deepcopy(record.metadata),
        )

    def _publication_metadata(self) -> Dict[str, Any]:
        """Revalidate a guarded publication immediately before registry commit."""

        if self._publication_guard is None:
            return {}
        payload = self._publication_guard()
        if not isinstance(payload, Mapping):
            raise PublicationFenceRejected("publication guard must return a mapping")
        return deepcopy(dict(payload))

    @staticmethod
    def _with_publication_metadata(
        record: ArtifactRecord,
        publication_metadata: Mapping[str, Any],
        *,
        existing: ArtifactRecord | None = None,
    ) -> ArtifactRecord:
        if not publication_metadata:
            return record
        # READY promotion transactions are immutable after their first durable
        # registration.  The enclosing current-set/pointer records receive the
        # new fence identity when they are switched.
        if (
            existing is not None
            and existing.artifact_type == "repair_promotion_transaction"
            and existing.status == "ready"
        ):
            return replace(record, metadata=deepcopy(existing.metadata))
        metadata = deepcopy(record.metadata)
        metadata["publication_fence"] = deepcopy(dict(publication_metadata))
        return replace(record, metadata=metadata)

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
            if candidate.status == "ready":
                self._verify_ready_artifact(candidate)
            publication_metadata = self._publication_metadata()
            candidate = self._with_publication_metadata(
                candidate,
                publication_metadata,
                existing=existing,
            )
            self._validate_artifact_merge(existing, candidate)
            merged = dict(artifacts)
            merged[candidate.artifact_id] = candidate
            next_revision = disk_revision + 1
            self._write_registry_unlocked(merged, next_revision)
            # Memory changes only after the durable os.replace succeeds.
        self._artifacts = merged
        self._revision = next_revision
        return self._copy_record(candidate)

    def save(
        self,
        *,
        expected_revision: Optional[int] = None,
        external_registry_resolver: Callable[[str], Optional["ArtifactRegistry"]] | None = None,
    ) -> None:
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
            snapshot: Dict[str, ArtifactRecord] = {}
            for record in self._artifacts.values():
                if record.job_id != self.job_id:
                    raise ArtifactConflict(
                        f"artifact {record.artifact_id!r} belongs to {record.job_id!r}, "
                        f"not registry owner {self.job_id!r}"
                    )
                self._validate_artifact_merge(disk_artifacts.get(record.artifact_id), record)
                if record.status == "ready":
                    self._verify_ready_artifact(record)
                    record = replace(
                        record,
                        depends_on=self._normalize_dependencies(
                            record.depends_on,
                            self._artifacts,
                            owner_job_id=record.job_id,
                            require_ready=True,
                            external_registry_resolver=external_registry_resolver,
                        ),
                    )
                snapshot[record.artifact_id] = record
            next_revision = disk_revision + 1
            publication_metadata = self._publication_metadata()
            if publication_metadata:
                snapshot = {
                    artifact_id: self._with_publication_metadata(
                        record,
                        publication_metadata,
                        existing=disk_artifacts.get(artifact_id),
                    )
                    for artifact_id, record in snapshot.items()
                }
            self._write_registry_unlocked(snapshot, next_revision)
            self._artifacts = snapshot
            self._revision = next_revision

    def list_records(self) -> List[ArtifactRecord]:
        return [self._copy_record(record) for record in self._artifacts.values()]

    def get(self, artifact_id: str) -> Optional[ArtifactRecord]:
        record = self._artifacts.get(artifact_id)
        return self._copy_record(record) if record is not None else None

    def verify_ready_dependencies(
        self,
        dependencies: Iterable[ArtifactDependencyRefV2 | Mapping[str, Any]],
        *,
        external_registry_resolver: Callable[[str], Optional["ArtifactRegistry"]] | None = None,
    ) -> List[ArtifactDependencyRefV2]:
        """Validate dependencies against the latest durable Registry state."""

        with self._transaction_lock():
            _revision, artifacts = self._read_registry_unlocked()
            return self._normalize_dependencies(
                dependencies,
                artifacts,
                owner_job_id=self.job_id,
                require_ready=True,
                external_registry_resolver=external_registry_resolver,
            )

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
        external_registry_resolver: Callable[[str], Optional["ArtifactRegistry"]] | None = None,
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
                    depends_on or [],
                    artifacts,
                    owner_job_id=self.job_id,
                    require_ready=status == "ready",
                    external_registry_resolver=external_registry_resolver,
                ),
                metadata=deepcopy(dict(metadata or {})),
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
        external_registry_resolver: Callable[[str], Optional["ArtifactRegistry"]] | None = None,
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
                    depends_on or [],
                    artifacts,
                    owner_job_id=job_id,
                    require_ready=status == "ready",
                    external_registry_resolver=external_registry_resolver,
                ),
                metadata=deepcopy(dict(metadata or {})),
                created_at=utc_now_iso(),
            )

        return self._register_transaction(build_record, expected_revision=expected_revision)

    def update_record(
        self,
        artifact_id: str,
        *,
        status: str | None = None,
        depends_on: Iterable[ArtifactDependencyRefV2 | Mapping[str, Any]] | None = None,
        external_registry_resolver: Callable[[str], Optional["ArtifactRegistry"]] | None = None,
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
            dependency_source = depends_on if depends_on is not None else current.depends_on
            next_dependencies = self._normalize_dependencies(
                dependency_source,
                artifacts,
                owner_job_id=current.job_id,
                require_ready=next_status == "ready",
                external_registry_resolver=external_registry_resolver,
            )
            next_metadata = deepcopy(current.metadata)
            next_metadata.update(deepcopy(dict(metadata_updates or {})))
            return replace(
                current,
                status=next_status,
                depends_on=next_dependencies,
                metadata=next_metadata,
            )

        return self._register_transaction(build_record, expected_revision=expected_revision)

    @staticmethod
    def _write_json_atomic(path: str | os.PathLike[str], payload: Mapping[str, Any]) -> None:
        target = os.path.abspath(os.fspath(path))
        directory = os.path.dirname(target) or os.curdir
        os.makedirs(directory, exist_ok=True)
        encoded = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
        fd, temp_path = tempfile.mkstemp(
            prefix=f".{os.path.basename(target)}.",
            suffix=".tmp",
            dir=directory,
        )
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, target)
            ArtifactRegistry._fsync_directory(directory)
        except Exception:
            try:
                os.unlink(temp_path)
            except FileNotFoundError:
                pass
            raise

    def current_artifact_set_pointer(self) -> ArtifactRecord | None:
        """Return the durable single-pointer record, if one is installed."""

        with self._transaction_lock():
            revision, artifacts = self._read_registry_unlocked()
            self._revision = revision
            self._artifacts = artifacts
            pointer = artifacts.get("current-artifact-set:pointer")
            return self._copy_record(pointer) if pointer is not None else None

    def build_current_artifact_set(
        self,
        *,
        promotion_transaction_id: str,
        promotion_transaction_hash: str,
        review_draft_artifact_id: str,
        review_draft_artifact_hash: str,
        citation_manifest_artifact_id: str,
        citation_manifest_artifact_hash: str,
        review_docx_artifact_id: str,
        review_docx_artifact_hash: str,
        validation_run_result_artifact_id: str,
        validation_run_result_artifact_hash: str,
        validation_receipt_closure_artifact_id: str,
        validation_receipt_closure_artifact_hash: str,
        validation_status: str = "clean",
        actor: str,
        reason: str,
        previous_set_id: str = "",
        validation_disposition_artifact_id: str = "",
        validation_disposition_artifact_hash: str = "",
    ) -> CurrentArtifactSetV1:
        """Create a deterministic set identity from exact artifact IDs/hashes."""

        if validation_status not in {"clean", "findings", "not_requested"}:
            raise ArtifactConflict(f"invalid current artifact set validation_status: {validation_status!r}")
        if validation_status == "not_requested":
            if not validation_disposition_artifact_id or len(validation_disposition_artifact_hash) != 64:
                raise ArtifactConflict(
                    "not_requested current artifact set requires a typed validation disposition"
                )
        elif not validation_run_result_artifact_id or len(validation_run_result_artifact_hash) != 64:
            raise ArtifactConflict(
                "validated current artifact set requires a validation run result"
            )
        created_at = utc_now_iso()
        fields = {
            "job_id": self.job_id,
            "promotion_transaction_id": promotion_transaction_id,
            "promotion_transaction_hash": promotion_transaction_hash,
            "review_draft_artifact_id": review_draft_artifact_id,
            "review_draft_artifact_hash": review_draft_artifact_hash,
            "citation_manifest_artifact_id": citation_manifest_artifact_id,
            "citation_manifest_artifact_hash": citation_manifest_artifact_hash,
            "review_docx_artifact_id": review_docx_artifact_id,
            "review_docx_artifact_hash": review_docx_artifact_hash,
            "validation_run_result_artifact_id": validation_run_result_artifact_id,
            "validation_run_result_artifact_hash": validation_run_result_artifact_hash,
            "validation_receipt_closure_artifact_id": validation_receipt_closure_artifact_id,
            "validation_receipt_closure_artifact_hash": validation_receipt_closure_artifact_hash,
            "validation_status": validation_status,
            "validation_disposition_artifact_id": validation_disposition_artifact_id,
            "validation_disposition_artifact_hash": validation_disposition_artifact_hash,
            "previous_set_id": previous_set_id,
            "actor": actor,
            "reason": reason,
        }
        set_id = "current-artifact-set:" + hashlib.sha256(
            json.dumps(fields, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return CurrentArtifactSetV1(set_id=set_id, created_at=created_at, **fields)

    def switch_current_artifact_set(
        self,
        current_set: CurrentArtifactSetV1,
        *,
        prepared_promotion_record: ArtifactRecord | None = None,
        expected_revision: int | None = None,
    ) -> ArtifactRecord:
        """Atomically register a validated immutable set and switch its pointer.

        Every target is verified against the durable registry and its bytes
        before any pointer mutation.  A failed validation or stale CAS leaves
        the previous pointer untouched.
        """

        if current_set.job_id != self.job_id:
            raise ArtifactConflict("current artifact set belongs to another job")
        if not current_set.set_id.startswith("current-artifact-set:"):
            raise ArtifactConflict("current artifact set ID must be content addressed")
        if current_set.set_id == "current-artifact-set:pointer":
            raise ArtifactConflict("current artifact set ID is reserved for the pointer")

        with self._transaction_lock():
            disk_revision, artifacts = self._read_registry_unlocked()
            if expected_revision is not None and disk_revision != expected_revision:
                raise RegistryRevisionConflict(
                    f"expected registry revision {expected_revision}, found {disk_revision}"
                )
            # Revalidate before any current-set bytes or pointer bytes are
            # written. QueuePublicationContext holds the queue lock while
            # this Registry transaction is active, so this is the short
            # queue -> Registry publication boundary.
            publication_metadata = self._publication_metadata()
            pointer = artifacts.get("current-artifact-set:pointer")
            previous_set_id = str((pointer.metadata if pointer else {}).get("current_set_id") or "")
            if current_set.previous_set_id != previous_set_id:
                if current_set.set_id == previous_set_id:
                    return self._copy_record(pointer) if pointer is not None else ArtifactRecord(
                        artifact_id="current-artifact-set:pointer",
                        artifact_role="current_artifact_set_pointer",
                        artifact_type="current_artifact_set_pointer",
                        artifact_version="v1",
                        path="",
                        producer="ArtifactRegistry",
                        job_id=self.job_id,
                        status="ready",
                        content_hash="",
                    )
                raise RegistryRevisionConflict(
                    f"current artifact set parent is stale: expected {current_set.previous_set_id!r}, "
                    f"found {previous_set_id!r}"
                )

            promotion_record = prepared_promotion_record or artifacts.get(
                current_set.promotion_transaction_id
            )
            if promotion_record is None:
                raise UnverifiedDependency(
                    "current artifact set promotion transaction is not registered: "
                    f"{current_set.promotion_transaction_id}"
                )
            if (
                promotion_record.artifact_id != current_set.promotion_transaction_id
                or promotion_record.artifact_type != "repair_promotion_transaction"
                or promotion_record.artifact_version != "v1"
                or promotion_record.status != "ready"
                or promotion_record.job_id != self.job_id
                or promotion_record.content_hash != current_set.promotion_transaction_hash
            ):
                raise UnverifiedDependency(
                    "current artifact set promotion transaction ID/hash binding is invalid"
                )
            if prepared_promotion_record is not None:
                self._validate_artifact_merge(
                    artifacts.get(prepared_promotion_record.artifact_id),
                    prepared_promotion_record,
                )
            self._verify_ready_artifact(promotion_record)

            target_records: list[ArtifactRecord] = []
            for artifact_id, expected_hash in current_set.target_artifact_pairs():
                record = artifacts.get(artifact_id)
                if record is None:
                    raise UnverifiedDependency(f"current artifact target is not registered: {artifact_id}")
                if record.status != "ready":
                    raise UnverifiedDependency(f"current artifact target is not ready: {artifact_id}")
                if record.job_id != self.job_id:
                    raise UnverifiedDependency(f"current artifact target belongs to another job: {artifact_id}")
                if not expected_hash or record.content_hash != expected_hash:
                    raise UnverifiedDependency(f"current artifact target hash mismatch: {artifact_id}")
                self._verify_ready_artifact(record)
                target_records.append(record)

            set_path = os.path.join(
                os.path.dirname(self.registry_path),
                f"{current_set.set_id.replace(':', '-')}.json",
            )
            self._write_json_atomic(set_path, current_set.to_dict())
            set_content_hash = file_sha256(set_path)
            target_refs = [ArtifactDependencyRefV2.from_record(record) for record in target_records]
            promotion_ref = ArtifactDependencyRefV2.from_record(promotion_record)
            set_record = ArtifactRecord(
                artifact_id=current_set.set_id,
                artifact_role="current_artifact_set",
                artifact_type="current_artifact_set",
                artifact_version="v1",
                path=set_path,
                producer="ArtifactRegistry.switch_current_artifact_set",
                job_id=self.job_id,
                status="ready",
                content_hash=set_content_hash,
                depends_on=[*target_refs, promotion_ref],
                metadata={
                    "promotion_transaction_id": current_set.promotion_transaction_id,
                    "promotion_transaction_hash": current_set.promotion_transaction_hash,
                    "validation_status": current_set.validation_status,
                    "actor": current_set.actor,
                    "reason": current_set.reason,
                    "target_artifact_ids": [record.artifact_id for record in target_records],
                },
                created_at=current_set.created_at,
            )
            self._verify_ready_artifact(set_record)
            set_ref = ArtifactDependencyRefV2.from_record(set_record)
            pointer_record = ArtifactRecord(
                artifact_id="current-artifact-set:pointer",
                artifact_role="current_artifact_set_pointer",
                artifact_type="current_artifact_set_pointer",
                artifact_version="v1",
                path=set_path,
                producer="ArtifactRegistry.switch_current_artifact_set",
                job_id=self.job_id,
                status="ready",
                content_hash=set_content_hash,
                depends_on=[set_ref],
                metadata={
                    "current_set_id": current_set.set_id,
                    "current_set_hash": set_content_hash,
                    "previous_set_id": previous_set_id,
                    "promotion_transaction_id": current_set.promotion_transaction_id,
                    "promotion_transaction_hash": current_set.promotion_transaction_hash,
                    "validation_status": current_set.validation_status,
                    "actor": current_set.actor,
                    "reason": current_set.reason,
                },
                created_at=utc_now_iso(),
            )
            self._verify_ready_artifact(pointer_record)
            merged = dict(artifacts)
            if prepared_promotion_record is not None:
                merged[prepared_promotion_record.artifact_id] = prepared_promotion_record
            existing_set = merged.get(current_set.set_id)
            if existing_set is not None and existing_set.content_hash != set_content_hash:
                raise ArtifactConflict(f"immutable current artifact set changed: {current_set.set_id}")
            merged[current_set.set_id] = set_record
            merged[pointer_record.artifact_id] = pointer_record
            next_revision = disk_revision + 1
            if publication_metadata:
                if prepared_promotion_record is not None:
                    merged[prepared_promotion_record.artifact_id] = self._with_publication_metadata(
                        prepared_promotion_record,
                        publication_metadata,
                        existing=artifacts.get(prepared_promotion_record.artifact_id),
                    )
                set_record = self._with_publication_metadata(
                    set_record,
                    publication_metadata,
                    existing=artifacts.get(set_record.artifact_id),
                )
                pointer_record = self._with_publication_metadata(
                    pointer_record,
                    publication_metadata,
                    existing=artifacts.get(pointer_record.artifact_id),
                )
                merged[current_set.set_id] = set_record
                merged[pointer_record.artifact_id] = pointer_record
            self._write_registry_unlocked(merged, next_revision)
            self._artifacts = merged
            self._revision = next_revision
            return self._copy_record(pointer_record)

    def resolve_current_artifact_set(self) -> CurrentArtifactSetV1 | None:
        """Resolve and verify the one current set and all of its exact bytes."""

        with self._transaction_lock():
            revision, artifacts = self._read_registry_unlocked()
            self._revision = revision
            self._artifacts = artifacts
            pointer = artifacts.get("current-artifact-set:pointer")
            if pointer is None:
                return None
            set_id = str(pointer.metadata.get("current_set_id") or "")
            set_record = artifacts.get(set_id)
            if not set_id or set_record is None or set_record.status != "ready":
                raise UnverifiedArtifact("current artifact set pointer targets a missing or non-ready set")
            if not os.path.isfile(set_record.path) or file_sha256(set_record.path) != set_record.content_hash:
                raise UnverifiedArtifact("current artifact set bytes do not match the registry")
            try:
                payload = json.loads(Path(set_record.path).read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise UnverifiedArtifact("current artifact set payload cannot be read") from exc
            if not isinstance(payload, Mapping):
                raise UnverifiedArtifact("current artifact set payload is not an object")
            resolved = CurrentArtifactSetV1.from_dict(payload)
            if resolved.set_id != set_id or resolved.job_id != self.job_id:
                raise UnverifiedArtifact("current artifact set identity does not match pointer")
            promotion = artifacts.get(resolved.promotion_transaction_id)
            if (
                promotion is None
                or promotion.status != "ready"
                or promotion.content_hash != resolved.promotion_transaction_hash
                or promotion.artifact_type != "repair_promotion_transaction"
                or promotion.artifact_version != "v1"
            ):
                raise UnverifiedArtifact(
                    "current artifact set promotion transaction ID/hash binding is invalid"
                )
            self._verify_ready_artifact(promotion)
            for artifact_id, expected_hash in resolved.target_artifact_pairs():
                record = artifacts.get(artifact_id)
                if record is None or record.status != "ready" or record.content_hash != expected_hash:
                    raise UnverifiedArtifact(f"current artifact set target is not current: {artifact_id}")
                self._verify_ready_artifact(record)
            return resolved
