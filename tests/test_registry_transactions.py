from __future__ import annotations

import json
import multiprocessing
import os
import threading
from pathlib import Path
from queue import Empty
from typing import Any

import pytest

from services import artifact_registry as artifact_registry_module
from services.artifact_registry import (
    ArtifactConflict,
    ArtifactDependencyRefV2,
    ArtifactRegistry,
    RegistryCorruption,
    RegistryLockTimeout,
    RegistryRevisionConflict,
)


def _write_artifact(path: Path, value: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"value": value}), encoding="utf-8")
    return path


def _concurrent_register_worker(
    registry_path: str,
    artifact_path: str,
    artifact_id: str,
    ready_queue: Any,
    result_queue: Any,
    start_event: Any,
) -> None:
    try:
        registry = ArtifactRegistry(registry_path, "job-concurrent")
        ready_queue.put((artifact_id, registry.revision))
        if not start_event.wait(timeout=10):
            raise TimeoutError("parent did not release concurrent registry workers")
        registry.register_file(
            artifact_id=artifact_id,
            artifact_role="test",
            artifact_type="test_artifact",
            artifact_version="v1",
            path=artifact_path,
            producer="tests.concurrent-worker",
        )
        result_queue.put((artifact_id, "ok"))
    except BaseException as exc:  # pragma: no cover - only used to report subprocess failure
        result_queue.put((artifact_id, f"{type(exc).__name__}: {exc}"))


def test_v1_registry_is_read_additively_and_next_write_emits_v2(tmp_path: Path) -> None:
    legacy_path = _write_artifact(tmp_path / "legacy.json", "legacy")
    registry_path = tmp_path / "artifact_registry.json"
    registry_path.write_text(
        json.dumps(
            {
                "artifact_registry_version": "v1",
                "job_id": "job-legacy",
                "artifacts": [
                    {
                        "artifact_id": "legacy-artifact",
                        "artifact_role": "summary",
                        "artifact_type": "summary_file",
                        "artifact_version": "v1",
                        "path": str(legacy_path),
                        "producer": "legacy",
                        "job_id": "job-legacy",
                        "status": "ready",
                        "content_hash": "legacy-hash",
                        "depends_on": [
                            {
                                "artifact_type": "source_pdf",
                                "path": str(tmp_path / "source.pdf"),
                                "content_hash": "source-hash",
                            }
                        ],
                        "created_at": "2026-07-13T00:00:00+00:00",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    registry = ArtifactRegistry(registry_path, "job-legacy")
    legacy = registry.get("legacy-artifact")

    assert registry.revision == 0
    assert legacy is not None
    assert legacy.depends_on[0].dependency_kind == "local_job"
    assert legacy.depends_on[0].job_id == "job-legacy"
    assert legacy.depends_on[0].artifact_id == "source_pdf:source.pdf"

    new_path = _write_artifact(tmp_path / "new.json", "new")
    registry.register_file(
        artifact_id="new-artifact",
        artifact_role="summary",
        artifact_type="summary_file",
        artifact_version="v2",
        path=new_path,
        producer="tests",
    )

    payload = json.loads(registry_path.read_text(encoding="utf-8"))
    assert payload["artifact_registry_version"] == "v2"
    assert payload["revision"] == 1
    assert {item["artifact_id"] for item in payload["artifacts"]} == {
        "legacy-artifact",
        "new-artifact",
    }
    assert set(payload["artifacts"][0]["depends_on"][0]) == {
        "dependency_kind",
        "job_id",
        "artifact_id",
        "artifact_type",
        "path",
        "content_hash",
    }


def test_dependency_v2_round_trip_preserves_external_identity(tmp_path: Path) -> None:
    registry_path = tmp_path / "artifact_registry.json"
    artifact_path = _write_artifact(tmp_path / "child.json", "child")
    dependency = ArtifactDependencyRefV2(
        dependency_kind="external_job",
        job_id="parent-job",
        artifact_id="parent-summary",
        artifact_type="summary_file",
        path=str(tmp_path / "parent" / "summary.json"),
        content_hash="a" * 64,
    )

    ArtifactRegistry(registry_path, "child-job").register_file(
        artifact_id="child-review",
        artifact_role="review",
        artifact_type="review_draft",
        artifact_version="v1",
        path=artifact_path,
        producer="tests",
        depends_on=[dependency],
        metadata={"identity_verdict": "match", "canonical_ready": True},
    )

    loaded = ArtifactRegistry(registry_path, "child-job").get("child-review")
    assert loaded is not None
    assert loaded.depends_on == [dependency]
    assert loaded.metadata == {"identity_verdict": "match", "canonical_ready": True}


def test_ready_registration_requires_an_existing_file(tmp_path: Path) -> None:
    registry_path = tmp_path / "artifact_registry.json"
    registry = ArtifactRegistry(registry_path, "job-missing")

    with pytest.raises(FileNotFoundError, match="ready artifact does not exist"):
        registry.register_file(
            artifact_id="missing",
            artifact_role="test",
            artifact_type="test",
            artifact_version="v1",
            path=tmp_path / "missing.json",
            producer="tests",
        )

    assert registry.revision == 0
    assert registry.get("missing") is None
    assert not registry_path.exists()


def test_corrupt_registry_raises_instead_of_falling_back_to_empty(tmp_path: Path) -> None:
    registry_path = tmp_path / "artifact_registry.json"
    registry_path.write_text("{not-json", encoding="utf-8")

    with pytest.raises(RegistryCorruption, match="cannot read registry"):
        ArtifactRegistry(registry_path, "job-corrupt")


def test_two_stale_instances_merge_without_lost_update(tmp_path: Path) -> None:
    registry_path = tmp_path / "artifact_registry.json"
    first_path = _write_artifact(tmp_path / "first.json", "first")
    second_path = _write_artifact(tmp_path / "second.json", "second")
    first = ArtifactRegistry(registry_path, "job-stale")
    second = ArtifactRegistry(registry_path, "job-stale")

    first.register_file(
        artifact_id="first",
        artifact_role="test",
        artifact_type="test",
        artifact_version="v1",
        path=first_path,
        producer="tests",
    )
    second.register_file(
        artifact_id="second",
        artifact_role="test",
        artifact_type="test",
        artifact_version="v1",
        path=second_path,
        producer="tests",
    )

    reloaded = ArtifactRegistry(registry_path, "job-stale")
    assert reloaded.revision == 2
    assert {record.artifact_id for record in reloaded.list_records()} == {"first", "second"}
    assert {record.artifact_id for record in second.list_records()} == {"first", "second"}


def test_mutable_head_registration_updates_same_artifact_identity(tmp_path: Path) -> None:
    registry_path = tmp_path / "artifact_registry.json"
    artifact_path = _write_artifact(tmp_path / "job_outcome_v1.json", "running")
    registry = ArtifactRegistry(registry_path, "job-head")
    first = registry.register_file(
        artifact_id="job_outcome",
        artifact_role="job_outcome",
        artifact_type="job_outcome",
        artifact_version="v1",
        path=artifact_path,
        producer="runtime.lifecycle",
    )

    _write_artifact(artifact_path, "completed")
    second = registry.register_file(
        artifact_id="job_outcome",
        artifact_role="job_outcome",
        artifact_type="job_outcome",
        artifact_version="v1",
        path=artifact_path,
        producer="runtime.lifecycle",
    )
    reloaded = ArtifactRegistry(registry_path, "job-head")

    assert second.content_hash != first.content_hash
    assert reloaded.get("job_outcome") == second
    assert len(reloaded.list_records()) == 1


def test_explicit_expected_revision_is_compare_and_swap(tmp_path: Path) -> None:
    registry_path = tmp_path / "artifact_registry.json"
    first_path = _write_artifact(tmp_path / "first.json", "first")
    second_path = _write_artifact(tmp_path / "second.json", "second")
    first = ArtifactRegistry(registry_path, "job-cas")
    stale = ArtifactRegistry(registry_path, "job-cas")

    first.register_file(
        artifact_id="first",
        artifact_role="test",
        artifact_type="test",
        artifact_version="v1",
        path=first_path,
        producer="tests",
        expected_revision=0,
    )

    with pytest.raises(RegistryRevisionConflict, match="expected registry revision 0, found 1"):
        stale.register_file(
            artifact_id="second",
            artifact_role="test",
            artifact_type="test",
            artifact_version="v1",
            path=second_path,
            producer="tests",
            expected_revision=0,
        )

    assert ArtifactRegistry(registry_path, "job-cas").get("second") is None


def test_stale_compatibility_save_cannot_overwrite_newer_registry(tmp_path: Path) -> None:
    registry_path = tmp_path / "artifact_registry.json"
    artifact_path = _write_artifact(tmp_path / "artifact.json", "artifact")
    current = ArtifactRegistry(registry_path, "job-save-cas")
    stale = ArtifactRegistry(registry_path, "job-save-cas")
    current.register_file(
        artifact_id="artifact",
        artifact_role="test",
        artifact_type="test",
        artifact_version="v1",
        path=artifact_path,
        producer="tests",
    )

    with pytest.raises(RegistryRevisionConflict, match="expected registry revision 0, found 1"):
        stale.save()

    reloaded = ArtifactRegistry(registry_path, "job-save-cas")
    assert reloaded.revision == 1
    assert reloaded.get("artifact") is not None


def test_artifact_identity_conflict_does_not_modify_durable_state(tmp_path: Path) -> None:
    registry_path = tmp_path / "artifact_registry.json"
    first_path = _write_artifact(tmp_path / "first.json", "first")
    second_path = _write_artifact(tmp_path / "second.json", "second")
    registry = ArtifactRegistry(registry_path, "job-conflict")
    registry.register_file(
        artifact_id="stable-id",
        artifact_role="test",
        artifact_type="type-a",
        artifact_version="v1",
        path=first_path,
        producer="tests",
    )

    with pytest.raises(ArtifactConflict, match="artifact_id 'stable-id' conflicts"):
        registry.register_file(
            artifact_id="stable-id",
            artifact_role="test",
            artifact_type="type-b",
            artifact_version="v1",
            path=second_path,
            producer="tests",
        )

    reloaded = ArtifactRegistry(registry_path, "job-conflict")
    assert reloaded.revision == 1
    assert reloaded.get("stable-id") is not None
    assert reloaded.get("stable-id").artifact_type == "type-a"  # type: ignore[union-attr]


def test_atomic_replace_failure_does_not_update_memory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    registry_path = tmp_path / "artifact_registry.json"
    artifact_path = _write_artifact(tmp_path / "artifact.json", "artifact")
    registry = ArtifactRegistry(registry_path, "job-atomic")

    def fail_replace(source: str, destination: str) -> None:
        raise OSError(f"injected replace failure: {source} -> {destination}")

    monkeypatch.setattr(artifact_registry_module.os, "replace", fail_replace)
    with pytest.raises(OSError, match="injected replace failure"):
        registry.register_file(
            artifact_id="artifact",
            artifact_role="test",
            artifact_type="test",
            artifact_version="v1",
            path=artifact_path,
            producer="tests",
        )

    assert registry.revision == 0
    assert registry.get("artifact") is None
    assert not registry_path.exists()
    assert list(tmp_path.glob(".artifact_registry.json.*.tmp")) == []


def test_registry_lock_timeout_is_typed(tmp_path: Path) -> None:
    registry_path = tmp_path / "artifact_registry.json"
    artifact_path = _write_artifact(tmp_path / "artifact.json", "artifact")
    holder = ArtifactRegistry(registry_path, "job-lock")
    contender = ArtifactRegistry(
        registry_path,
        "job-lock",
        registry_lock_timeout_seconds=0.05,
        registry_lock_retry_interval_ms=5,
    )
    entered = threading.Event()
    release = threading.Event()

    def hold_lock() -> None:
        with holder._transaction_lock():
            entered.set()
            assert release.wait(timeout=5)

    thread = threading.Thread(target=hold_lock, daemon=True)
    thread.start()
    assert entered.wait(timeout=5)
    try:
        with pytest.raises(RegistryLockTimeout, match="timed out acquiring process registry lock"):
            contender.register_file(
                artifact_id="artifact",
                artifact_role="test",
                artifact_type="test",
                artifact_version="v1",
                path=artifact_path,
                producer="tests",
            )
    finally:
        release.set()
        thread.join(timeout=5)
    assert not thread.is_alive()


def test_two_processes_register_without_lost_update(tmp_path: Path) -> None:
    registry_path = tmp_path / "artifact_registry.json"
    first_path = _write_artifact(tmp_path / "first.json", "first")
    second_path = _write_artifact(tmp_path / "second.json", "second")
    context = multiprocessing.get_context("spawn")
    ready_queue = context.Queue()
    result_queue = context.Queue()
    start_event = context.Event()
    processes = [
        context.Process(
            target=_concurrent_register_worker,
            args=(
                str(registry_path),
                str(path),
                artifact_id,
                ready_queue,
                result_queue,
                start_event,
            ),
        )
        for artifact_id, path in (("first", first_path), ("second", second_path))
    ]

    for process in processes:
        process.start()
    try:
        ready = [ready_queue.get(timeout=15) for _ in processes]
        assert sorted(ready) == [("first", 0), ("second", 0)]
        start_event.set()
        results = [result_queue.get(timeout=15) for _ in processes]
    except Empty as exc:  # pragma: no cover - test reports a clearer process failure below
        raise AssertionError("registry worker did not report in time") from exc
    finally:
        start_event.set()
        for process in processes:
            process.join(timeout=15)
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)

    assert sorted(results) == [("first", "ok"), ("second", "ok")]
    assert [process.exitcode for process in processes] == [0, 0]
    reloaded = ArtifactRegistry(registry_path, "job-concurrent")
    assert reloaded.revision == 2
    assert {record.artifact_id for record in reloaded.list_records()} == {"first", "second"}


def test_lock_file_contents_do_not_grant_or_deny_lock_ownership(tmp_path: Path) -> None:
    registry_path = tmp_path / "artifact_registry.json"
    lock_path = Path(f"{registry_path}.lock")
    lock_path.write_text("stale pid=999999 owner metadata", encoding="utf-8")
    artifact_path = _write_artifact(tmp_path / "artifact.json", "artifact")

    record = ArtifactRegistry(registry_path, "job-diagnostic-lock").register_file(
        artifact_id="artifact",
        artifact_role="test",
        artifact_type="test",
        artifact_version="v1",
        path=artifact_path,
        producer="tests",
    )

    assert record.artifact_id == "artifact"
