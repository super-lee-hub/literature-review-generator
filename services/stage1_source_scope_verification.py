"""Run-local graph reuse with fresh source bytes around each JIT preparation."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from threading import get_ident
from time import monotonic
from typing import Any, Self

from runtime import artifact_validators
from services.artifact_registry import (
    ArtifactDependencyRefV2,
    ArtifactRecord,
    ArtifactRegistry,
    RegistrySnapshotChanged,
    UnverifiedDependency,
    file_sha256,
)

_SOURCE_TYPES = frozenset({
    "stage1_source_scope", "source_bundle", "runtime_job_spec", "source_pdf",
})
_CONTROL_POLL_SECONDS = 0.25


class Stage1SourceScopeVerification:
    """Share only an unchanged local graph; guard must end before any POST.

    ``verify`` outside an eligible guard retains the original complete proof.
    Guard entry and successful exit re-read every participating file and exact
    Record. Nothing waits until run exit to validate a successful JIT operation.
    """

    def __init__(
        self,
        registry: ArtifactRegistry,
        root: ArtifactRecord,
        *,
        is_local_route: Callable[[], bool],
        check_cancelled: Callable[[], None] | None = None,
        external_registry_resolver: Callable[[str], ArtifactRegistry | None] | None = None,
    ) -> None:
        self.registry = registry
        self._root = deepcopy(root)
        self._registry_identity = (registry.job_id, registry.registry_path)
        self._is_local_route = is_local_route
        self._check_cancelled = check_cancelled or (lambda: None)
        self._resolver = external_registry_resolver
        self._records: dict[str, ArtifactRecord] = {}
        self._policy: tuple[Any, ...] = ()
        self._eligible = False
        self._active = False
        self._used = False
        self._guard_thread: int | None = None
        self._guard_optimized = False
        self._next_control_poll = 0.0

    def _check_control(self, *, force: bool = False) -> None:
        if force or monotonic() >= self._next_control_poll:
            self._check_cancelled()
            # A slow durable control read must not trigger another read on
            # the very next source row merely because that read took time.
            self._next_control_poll = monotonic() + _CONTROL_POLL_SECONDS

    def _schema_policy(self) -> tuple[Any, ...]:
        # These are the actual dispatch objects used by ArtifactRegistry.
        # Preserve object identities and relevant mutable version-map values.
        versions = artifact_validators.CURRENT_OUTLINE_ARTIFACT_VERSIONS
        verifier = self.registry._verify_ready_artifact
        return (
            getattr(verifier, "__func__", verifier),
            artifact_validators.validate_registered_artifact,
            artifact_validators.validate_current_outline_artifact,
            artifact_validators._validate_current_production_artifact,
            artifact_validators.CURRENT_PRODUCTION_ARTIFACT_TYPES,
            artifact_validators.OUTLINE_V3_ARTIFACT_TYPES,
            versions,
            tuple((kind, tuple(sorted(versions.get(kind, ())))) for kind in sorted(_SOURCE_TYPES)),
        )

    def _same_policy(self, policy: tuple[Any, ...]) -> bool:
        return all(old is new for old, new in zip(self._policy[:-1], policy[:-1])) and (
            self._policy[-1:] == policy[-1:]
        )

    def _assert_active(self) -> None:
        if not self._active:
            raise RuntimeError("Stage 1 source verification scope is not active")
        if self._registry_identity != (self.registry.job_id, self.registry.registry_path):
            raise UnverifiedDependency("Stage 1 source Registry identity changed")

    def _assert_root(self, root: ArtifactRecord) -> None:
        if root != self._root or root.job_id != self.registry.job_id or root.status != "ready":
            raise UnverifiedDependency("Stage 1 source verification root changed or is foreign")

    def _local_records(self, records: Mapping[str, ArtifactRecord]) -> dict[str, ArtifactRecord]:
        if (self._root.artifact_id != "stage1_source_scope"
                or self._root.artifact_type != "stage1_source_scope"):
            return {}
        selected: dict[str, ArtifactRecord] = {}
        pending = [self._root]
        while pending:
            self._check_control()
            record = pending.pop()
            if record.artifact_id in selected:
                continue
            if (record.job_id != self.registry.job_id or record.status != "ready"
                    or record.artifact_type not in _SOURCE_TYPES or record.artifact_version != "v1"
                    or (record.artifact_type == "source_pdf" and record.depends_on)):
                return {}
            if record.artifact_type != "source_pdf" and record.artifact_id != record.artifact_type:
                return {}
            selected[record.artifact_id] = deepcopy(record)
            for dependency in record.depends_on:
                if dependency.dependency_kind != "local_job" or dependency.job_id != self.registry.job_id:
                    return {}
                target = records.get(dependency.artifact_id)
                if target is None or ArtifactDependencyRefV2.from_record(target) != dependency:
                    return {}
                pending.append(target)
        return selected

    def __enter__(self) -> Self:
        if self._used:
            raise RuntimeError("Stage 1 source verification scope cannot be reused")
        self._used = True
        self._check_control(force=True)
        self._assert_root(self._root)
        revision, values = self.registry.capture_record_snapshot()
        records = {record.artifact_id: record for record in values}
        if records.get(self._root.artifact_id) != self._root:
            raise UnverifiedDependency("Stage 1 source verification root is stale")
        self._policy = self._schema_policy()
        eligible = self._local_records(records) if self._is_local_route() is True else {}
        self.registry.verify_ready_artifact_closures(
            (self._root,), external_registry_resolver=self._resolver,
            expected_record_snapshot=(revision, records),
        )
        final_revision, final_values = self.registry.capture_record_snapshot()
        if (final_revision != revision or records != {row.artifact_id: row for row in final_values}
                or not self._same_policy(self._schema_policy())):
            raise RegistrySnapshotChanged("Stage 1 source proof construction changed during verification")
        self._records = eligible
        self._eligible = bool(eligible)
        self._check_control(force=True)
        self._active = True
        return self

    def __exit__(self, *_exc: object) -> None:
        try:
            if self._active and (not _exc or _exc[0] is None):
                self._check_control(force=True)
        finally:
            self._active = False
            self._eligible = False
            self._guard_thread = None
            self._guard_optimized = False
            self._records.clear()
            self._policy = ()

    def _check_sources(self) -> None:
        self._assert_active()
        self._check_control()
        if self._is_local_route() is not True:
            raise UnverifiedDependency("Stage 1 local source verification route changed during JIT")
        if not self._same_policy(self._schema_policy()):
            raise RegistrySnapshotChanged("Stage 1 source schema verifier changed during JIT")
        start_stamp = self._registry_stamp()
        start_revision, start_records = self._check_records()
        for record in self._records.values():
            self._check_control()
            try:
                actual_hash = file_sha256(record.path)
            except OSError as exc:
                raise UnverifiedDependency(f"Stage 1 source file is unreadable: {record.artifact_id}") from exc
            if actual_hash != record.content_hash:
                raise UnverifiedDependency(f"Stage 1 source bytes changed: {record.artifact_id}")
        end_revision, end_records = self._check_records()
        if ((start_revision, start_records) != (end_revision, end_records)
                or start_stamp != self._registry_stamp()):
            self._check_control(force=True)
            raise RegistrySnapshotChanged("Stage 1 Registry changed during source byte verification")
        if not self._same_policy(self._schema_policy()):
            raise RegistrySnapshotChanged("Stage 1 source schema verifier changed during JIT")
        self._check_control()

    def _registry_stamp(self) -> tuple[int, ...]:
        try:
            stat = Path(self.registry.registry_path).stat()
        except OSError as exc:
            raise UnverifiedDependency("Stage 1 source Registry file is unreadable") from exc
        return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns

    def _check_records(self) -> tuple[int, dict[str, ArtifactRecord]]:
        revision, values = self.registry.capture_record_snapshot()
        current = {record.artifact_id: record for record in values}
        for key, expected in self._records.items():
            self._check_control()
            if current.get(key) != expected:
                raise RegistrySnapshotChanged(f"Stage 1 participating source Record changed: {key}")
        return revision, current

    @contextmanager
    def guard(self, root: ArtifactRecord) -> Iterator[Stage1SourceScopeVerification]:
        self._assert_active()
        self._assert_root(root)
        self._check_control(force=True)
        if self._guard_thread is not None:
            raise RuntimeError("Stage 1 JIT source guard is already active")
        if self._eligible and not self._same_policy(self._schema_policy()):
            self._eligible = False  # A new schema policy needs the original full proof.
        self._guard_thread = get_ident()
        self._guard_optimized = self._eligible and self._is_local_route() is True
        try:
            if self._guard_optimized:
                self._check_sources()
            yield self
            if self._guard_optimized:
                self._check_sources()
            self._check_control(force=True)
        finally:
            self._guard_thread = None
            self._guard_optimized = False

    def verify(self, root: ArtifactRecord) -> ArtifactRecord:
        self._assert_active()
        self._assert_root(root)
        self._check_control(force=not self._guard_optimized)
        if self._guard_thread is not None and self._guard_thread != get_ident():
            raise RuntimeError("Stage 1 JIT source guard belongs to another thread")
        if self._guard_optimized:
            if self._is_local_route() is not True or not self._same_policy(self._schema_policy()):
                raise UnverifiedDependency("Stage 1 source route or schema verifier changed during JIT")
            return deepcopy(self._root)  # Provisional until guard's successful exit.
        verified = self.registry.verify_ready_artifact_closure(
            root, external_registry_resolver=self._resolver,
        )
        self._check_control(force=True)
        return verified
