from __future__ import annotations

import json
import socket
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from runtime.provider_receipt_closure import ExpectedProviderCall
from services.artifact_registry import ArtifactDependencyRefV2, ArtifactRegistry
from services.job_workspace import atomic_write_json
from services.queue_service import (
    PersistentQueueService,
    QueueJobSpec,
    QueuePublicationRejected,
)
from tests.test_validation_adjudication_reuse import _service


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def deny(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("finalize path-index controls must not open a socket")

    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket.socket, "connect_ex", deny)
    monkeypatch.setattr(socket, "create_connection", deny)


def _register(
    registry: ArtifactRegistry,
    root: Path,
    artifact_id: str,
    *,
    path: Path | None = None,
    status: str = "ready",
) -> Any:
    path = path or root / f"{artifact_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text('{"source": "retained"}', encoding="utf-8")
    return registry.register_file(
        artifact_id=artifact_id,
        artifact_role="unit_output",
        artifact_type="unit_output",
        artifact_version="v1",
        path=path,
        producer="pure-control",
        status=status,
        metadata={"flag": False},
    )


def _calls(service: Any, paths: list[Path | str]) -> None:
    service._expected_provider_calls = {
        f"call-{index}": ExpectedProviderCall(
            call_id=f"call-{index}",
            job_id=service.job_id,
            attempt_id=service.attempt_id,
            stage_name="stage4_validate",
            node_id=f"node-{index}",
            closure_epoch_id=service.closure_epoch_id,
            artifact_path=str(path),
        )
        for index, path in enumerate(paths)
    }


def _rewrite(registry: ArtifactRegistry, artifact_id: str, **changes: Any) -> None:
    # Deliberately retain the revision to test full-record, not revision-only fences.
    payload = json.loads(Path(registry.registry_path).read_text(encoding="utf-8"))
    row = next(row for row in payload["artifacts"] if row["artifact_id"] == artifact_id)
    row.update(changes)
    atomic_write_json(registry.registry_path, payload)


def test_finalize_lookup_and_tail_are_linear_for_repeated_expected_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(tmp_path)
    registry = service.artifact_registry
    paths = [
        Path(_register(registry, tmp_path, f"unrelated-{i:03d}").path)
        for i in range(40)
    ]
    target = _register(registry, tmp_path, "zz-target-a")
    _register(registry, tmp_path, "zz-target-b", path=Path(target.path))
    registry.reload()
    _calls(service, [Path(target.path)] * 15)
    controlled = {*paths, Path(target.path)}
    resolves = 0
    snapshots = 0
    resolve = Path.resolve
    capture = registry.capture_record_snapshot

    def counted_resolve(path: Path, *args: Any, **kwargs: Any) -> Path:
        nonlocal resolves
        if path in controlled:
            resolves += 1
        return resolve(path, *args, **kwargs)

    def counted_snapshot() -> Any:
        nonlocal snapshots
        snapshots += 1
        return capture()

    monkeypatch.setattr(Path, "resolve", counted_resolve)
    monkeypatch.setattr(registry, "capture_record_snapshot", counted_snapshot)
    result = service.finalize_provider_receipts()
    assert [ref.artifact_id for ref in result["closure_record"].depends_on] == [
        target.artifact_id
    ]
    print(
        json.dumps(
            {
                "record_prefix": 41,
                "expected_count": 15,
                "resolves": resolves,
                "snapshots": snapshots,
            }
        )
    )
    assert resolves == 2 * (41 + 15)
    assert snapshots == 2


@pytest.mark.parametrize("mode", ["empty", "missing", "aliases", "early_stop"])
def test_finalize_keeps_ordered_physical_alias_empty_and_missing_semantics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    service = _service(tmp_path)
    registry = service.artifact_registry
    first = _register(registry, tmp_path, "a-first")
    _register(registry, tmp_path, "b-second", path=Path(first.path))
    other = _register(registry, tmp_path, "c-other")
    _register(
        registry,
        tmp_path,
        "00-quarantined",
        path=Path(first.path),
        status="quarantined",
    )
    nested = tmp_path / "nested"
    nested.mkdir()
    physical_alias = nested / ".." / Path(first.path).name
    _rewrite(registry, first.artifact_id, path=str(physical_alias))
    registry.reload()
    expected_ids: list[str] = []
    if mode == "empty":
        _calls(service, [])
    elif mode == "missing":
        _calls(service, [tmp_path / "not-registered.json", ""])
    elif mode == "aliases":
        _calls(
            service,
            [
                Path(other.path),
                Path(first.path),
                Path(other.path),
                tmp_path / "not-registered.json",
            ],
        )
        expected_ids = [other.artifact_id, first.artifact_id]
    else:
        bad = _register(registry, tmp_path, "zz-unneeded-tail")
        _calls(service, [Path(first.path)])
        expected_ids = [first.artifact_id]
        resolve = Path.resolve

        def reject_unneeded_tail(path: Path, *args: Any, **kwargs: Any) -> Path:
            assert str(path) != bad.path, (
                "matched lookup resolved an unnecessary trailing record"
            )
            return resolve(path, *args, **kwargs)

        monkeypatch.setattr(Path, "resolve", reject_unneeded_tail)
    if mode == "empty":

        def reject_snapshot() -> Any:
            pytest.fail("no wanted paths must not scan Registry")

        monkeypatch.setattr(registry, "capture_record_snapshot", reject_snapshot)
    result = service.finalize_provider_receipts()
    assert [
        ref.artifact_id for ref in result["closure_record"].depends_on
    ] == expected_ids


@pytest.mark.parametrize(
    "change",
    [
        "miss_ready",
        "status",
        "metadata_false_zero",
        "typed_edge",
        "path",
        "bytes",
        "bytes_and_hash",
        "removed",
        "alias_earlier",
        "record_retarget",
        "expected_retarget",
        "unrelated_append",
        "alias_later",
        "unrelated_metadata",
    ],
)
def test_finalize_rechecks_selection_and_bytes_without_blanket_revision_fence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    service = _service(tmp_path)
    registry = service.artifact_registry
    target = _register(
        registry,
        tmp_path,
        "m-target",
        status="quarantined" if change == "miss_ready" else "ready",
    )
    other = _register(registry, tmp_path, "z-other")
    nested = tmp_path / "nested"
    nested.mkdir()
    record_alias = nested / ".." / Path(target.path).name
    expected_alias = nested / ".." / "expected-alias.json"
    if change == "record_retarget":
        _rewrite(registry, target.artifact_id, path=str(record_alias))
    _calls(
        service,
        [expected_alias if change == "expected_retarget" else Path(target.path)],
    )
    registry.reload()
    snapshots = 0
    capture = registry.capture_record_snapshot
    resolve = Path.resolve
    expected_resolves = 0

    def physical_resolve(path: Path, *args: Any, **kwargs: Any) -> Path:
        nonlocal expected_resolves
        if change == "expected_retarget" and str(path) == str(expected_alias):
            expected_resolves += 1
            return resolve(Path(target.path if expected_resolves == 1 else other.path))
        if (
            change == "record_retarget"
            and str(path) == str(record_alias)
            and snapshots >= 2
        ):
            return resolve(Path(other.path))
        return resolve(path, *args, **kwargs)

    def capture_with_late_change() -> Any:
        nonlocal snapshots
        snapshots += 1
        if snapshots == 2:
            if change in {"miss_ready", "status"}:
                _rewrite(
                    registry,
                    target.artifact_id,
                    status="ready" if change == "miss_ready" else "quarantined",
                )
            elif change == "metadata_false_zero":
                _rewrite(registry, target.artifact_id, metadata={"flag": 0})
            elif change == "typed_edge":
                _rewrite(
                    registry,
                    target.artifact_id,
                    depends_on=[ArtifactDependencyRefV2.from_record(other).to_dict()],
                )
            elif change == "path":
                _rewrite(registry, target.artifact_id, path=other.path)
            elif change in {"bytes", "bytes_and_hash"}:
                path = Path(target.path)
                path.write_bytes(path.read_bytes() + b" ")
                if change == "bytes_and_hash":
                    import hashlib

                    _rewrite(
                        registry,
                        target.artifact_id,
                        content_hash=hashlib.sha256(path.read_bytes()).hexdigest(),
                    )
            elif change == "removed":
                payload = json.loads(Path(registry.registry_path).read_text())
                payload["artifacts"] = [
                    row
                    for row in payload["artifacts"]
                    if row["artifact_id"] != target.artifact_id
                ]
                atomic_write_json(registry.registry_path, payload)
            elif change in {"alias_earlier", "alias_later", "unrelated_append"}:
                independent = ArtifactRegistry(registry.registry_path, registry.job_id)
                _register(
                    independent,
                    tmp_path,
                    "a-new-alias" if change == "alias_earlier" else "zz-new-append",
                    path=Path(target.path) if change != "unrelated_append" else None,
                )
            elif change == "unrelated_metadata":
                _rewrite(registry, other.artifact_id, metadata={"flag": 0})
        return capture()

    monkeypatch.setattr(Path, "resolve", physical_resolve)
    monkeypatch.setattr(registry, "capture_record_snapshot", capture_with_late_change)
    allowed = change in {"unrelated_append", "alias_later", "unrelated_metadata"}
    if allowed:
        result = service.finalize_provider_receipts()
        assert [ref.artifact_id for ref in result["closure_record"].depends_on] == [
            target.artifact_id
        ]
    else:
        with pytest.raises(RuntimeError, match="provider output"):
            service.finalize_provider_receipts()
        assert not any(
            record.artifact_type == "provider_receipt_closure"
            for record in registry.list_records()
        )
    assert snapshots == 2


def test_finalize_still_rejects_reclaimed_queue_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [datetime(2026, 10, 25, tzinfo=timezone.utc)]
    monkeypatch.setattr(
        PersistentQueueService, "_now_datetime", staticmethod(lambda: clock[0])
    )
    service = _service(tmp_path)
    queue = PersistentQueueService(tmp_path / "queue.json")
    queue.add_job(
        QueueJobSpec(
            job_id=service.job_id, job_type="review", project_name="validation"
        )
    )
    lease = queue.claim_job(service.job_id, worker_id="old", lease_seconds=60)
    assert lease is not None
    publication = queue.publication_context(lease)
    registry = publication.registry(
        service.workspace.paths.registry_path, service.job_id
    )
    service.artifact_registry = registry
    service.publication_context = publication
    target = _register(registry, tmp_path, "target")
    _calls(service, [Path(target.path)])
    before = Path(registry.registry_path).read_bytes()
    revision = registry.revision
    clock[0] += timedelta(seconds=120)
    assert queue.recover_expired_leases() == [service.job_id]
    assert (
        queue.claim_job(service.job_id, worker_id="new", lease_seconds=60) is not None
    )
    with pytest.raises(QueuePublicationRejected):
        service.finalize_provider_receipts()
    assert Path(registry.registry_path).read_bytes() == before
    assert registry.revision == revision
