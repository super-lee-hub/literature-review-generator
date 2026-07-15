from __future__ import annotations

import json
from pathlib import Path

import pytest

from runtime.attempt_store import AttemptStore, AttemptStoreCorruption
from services.artifact_registry import ArtifactRegistry
from services.job_workspace import JobWorkspace


def _store(tmp_path: Path) -> tuple[AttemptStore, ArtifactRegistry]:
    workspace = JobWorkspace.create(str(tmp_path), "demo", job_id="job-1")
    registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    return AttemptStore(workspace, registry), registry


def test_attempt_store_persists_append_only_transition_snapshots(tmp_path: Path) -> None:
    store, registry = _store(tmp_path)

    started = store.start(job_id="job-1", producer="tests")
    terminal = store.finish(started.attempt, "succeeded", reason="done")

    history = store.load_history()
    assert [item.status for item in history] == ["pending", "running", "succeeded"]
    assert terminal.is_terminal is True
    assert len(registry.list_records()) == 3
    assert all(record.artifact_type == "job_attempt" for record in registry.list_records())
    paths = sorted(store.directory.glob("snapshot-*.json"))
    assert [json.loads(path.read_text(encoding="utf-8"))["snapshot_sequence"] for path in paths] == [1, 2, 3]


def test_stale_running_attempt_is_interrupted_before_new_pending_and_running(tmp_path: Path) -> None:
    store, _registry = _store(tmp_path)
    first = store.start(job_id="job-1", producer="tests")

    resumed = store.start(job_id="job-1", producer="tests-resume")

    history = store.load_history()
    assert [item.status for item in history] == [
        "pending",
        "running",
        "interrupted",
        "pending",
        "running",
    ]
    assert resumed.recovered_attempt is not None
    assert resumed.recovered_attempt.attempt_id == first.attempt.attempt_id
    assert resumed.attempt.attempt_number == 2
    assert resumed.attempt.resumed_from_attempt == 1


def test_attempt_store_rejects_sequence_gaps_and_non_head_finish(tmp_path: Path) -> None:
    store, _registry = _store(tmp_path)
    first = store.start(job_id="job-1", producer="tests")
    store.finish(first.attempt, "failed")
    second = store.start(job_id="job-1", producer="tests")

    with pytest.raises(AttemptStoreCorruption, match="durable running attempt head"):
        store.finish(first.attempt, "failed")

    (store.directory / "snapshot-000002.json").rename(store.directory / "snapshot-000009.json")
    with pytest.raises(AttemptStoreCorruption, match="sequence has a gap"):
        store.load_history()

    assert second.attempt.status == "running"


def test_attempt_store_registers_snapshot_left_by_registry_failure(tmp_path: Path) -> None:
    store, registry = _store(tmp_path)
    started = store.start(job_id="job-1", producer="tests")
    target_record = registry.get(f"job-attempt:{started.attempt.attempt_id}:000002:running")
    assert target_record is not None

    payload = json.loads(Path(registry.registry_path).read_text(encoding="utf-8"))
    payload["artifacts"] = [
        item for item in payload["artifacts"] if item["artifact_id"] != target_record.artifact_id
    ]
    payload["revision"] += 1
    Path(registry.registry_path).write_text(json.dumps(payload), encoding="utf-8")
    registry.reload()

    repaired = store.register_orphaned_snapshots()

    assert target_record.artifact_id in {item.artifact_id for item in repaired}
    assert registry.get(target_record.artifact_id) is not None
