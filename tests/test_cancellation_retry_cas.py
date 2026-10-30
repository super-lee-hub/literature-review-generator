"""Queue retry may clear its prior cancel, never a later cancellation event."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from runtime.cancellation import CancellationRequestError, CancellationRequestStore
from services.artifact_registry import ArtifactRegistry
from services.job_workspace import JobWorkspace
from services.queue_service import (
    JobCancelledError, PersistentQueueService, QueueJobSpec,
    QueuePublicationRejected, QueueState,
)


def _store(tmp_path: Path) -> CancellationRequestStore:
    workspace = JobWorkspace(str(tmp_path / "output"), "queue-cas", "queue-cas-job")
    workspace.ensure_exists()
    registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    return CancellationRequestStore(workspace, registry)


def _hash(store: CancellationRequestStore) -> str:
    return hashlib.sha256(store.path.read_bytes()).hexdigest()


def test_two_identical_cancel_requests_in_one_clock_tick_have_distinct_identity(tmp_path, monkeypatch):
    monkeypatch.setattr("runtime.cancellation.utc_now_iso", lambda: "2026-10-26T00:00:00Z")
    store = _store(tmp_path)
    store.request(requested_by="queue-ui", reason="user_requested")
    prior = _hash(store)
    store.request(requested_by="queue-ui", reason="user_requested")
    assert _hash(store) != prior, "a newer cancellation must not alias the retry's captured old marker"
    assert store.read().active


def test_retry_cannot_clear_a_later_cancel_request(tmp_path):
    store = _store(tmp_path)
    store.request(requested_by="queue-ui", reason="old_attempt")
    prior = _hash(store)
    store.request(requested_by="queue-ui", reason="cancel_after_retry_click")
    current = _hash(store)
    with pytest.raises(CancellationRequestError, match="changed"):
        store.clear(cleared_by="queue_runner", reason="retry", expected_hash=prior)
    assert _hash(store) == current
    assert store.read().active


def test_retry_can_clear_the_exact_cancel_it_captured(tmp_path):
    store = _store(tmp_path)
    store.request(requested_by="queue-ui", reason="old_attempt")
    prior = _hash(store)
    cleared = store.clear(cleared_by="queue_runner", reason="retry", expected_hash=prior)
    assert not cleared.active
    assert not store.read().active
    assert _hash(store) != prior


def test_legacy_cancel_marker_remains_readable(tmp_path):
    store = _store(tmp_path)
    store.request(requested_by="legacy", reason="legacy")
    payload = json.loads(store.path.read_bytes())
    payload.pop("request_token", None)
    store.path.write_text(json.dumps(payload), encoding="utf-8")
    store.registry.register_file(
        artifact_id=store.artifact_id,
        artifact_role="cancel_request",
        artifact_type="cancel_request",
        artifact_version="v1",
        path=store.path,
        producer="test_legacy_cancel",
    )
    assert store.read().active


def test_missing_marker_snapshot_does_not_create_a_workspace(tmp_path):
    workspace = JobWorkspace(str(tmp_path / "output"), "probe", "unstarted")
    store = CancellationRequestStore(workspace)
    assert store.read_snapshot() == (None, "")
    assert not Path(workspace.root_dir).exists()


@pytest.mark.parametrize("fence_loss", ["released_lease", "current_cancel"])
def test_queue_marker_clear_is_fenced_before_byte_mutation(tmp_path, fence_loss):
    queue = PersistentQueueService(tmp_path / "queue.json")
    queue.add_job(QueueJobSpec(job_id="marker-job", job_type="test", project_name="marker"))
    job = queue.get_job("marker-job")
    assert job is not None
    lease = queue.claim_job(job.job_id, worker_id="marker-worker", lease_seconds=120)
    assert lease is not None
    root = Path(job.workspace_path)
    workspace = JobWorkspace(str(root.parent), root.name[:-len("__" + job.job_id)], job.job_id)
    workspace.ensure_exists()
    raw_registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    raw_store = CancellationRequestStore(workspace, raw_registry)
    raw_store.request(requested_by="test", reason="old_attempt")
    prior = _hash(raw_store)
    prior_registry = Path(raw_registry.registry_path).read_bytes()
    fenced_registry = queue.publication_context(lease).registry(raw_registry.registry_path, job.job_id)
    if fence_loss == "released_lease":
        assert queue.release_lease(
            job.job_id, lease_id=lease.lease_id, worker_id=lease.worker_id,
            lease_generation=lease.lease_generation, fence_token=lease.fence_token,
            state=QueueState.FAILED,
        )
    else:
        assert queue.request_cancel(job.job_id, reason="new_current_cancel")
    with pytest.raises((QueuePublicationRejected, JobCancelledError)):
        CancellationRequestStore(workspace, fenced_registry).clear(expected_hash=prior)
    assert _hash(raw_store) == prior
    assert Path(raw_registry.registry_path).read_bytes() == prior_registry
    assert raw_store.read().active
