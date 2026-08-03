from __future__ import annotations

import time

from services.queue_service import PersistentQueueService, QueueJobSpec, QueueState


def _add_job(service: PersistentQueueService, job_id: str = "job-1") -> None:
    service.add_job(
        QueueJobSpec(
            job_id=job_id,
            job_type="review",
            project_name="lease-test",
        )
    )


def test_queue_claim_is_cross_instance_cas_and_lease_release(tmp_path) -> None:
    queue_file = tmp_path / "queue.json"
    first = PersistentQueueService(queue_file)
    second = PersistentQueueService(queue_file)
    _add_job(first)

    lease = first.claim_job("job-1", worker_id="worker-a", lease_seconds=30)
    assert lease is not None
    assert second.claim_job("job-1", worker_id="worker-b", lease_seconds=30) is None
    assert not second.heartbeat(
        "job-1",
        lease_id=lease.lease_id,
        worker_id="worker-b",
        lease_seconds=30,
    )
    assert second.heartbeat(
        "job-1",
        lease_id=lease.lease_id,
        worker_id="worker-a",
        lease_seconds=30,
    )
    assert not second.release_lease(
        "job-1",
        lease_id=lease.lease_id,
        worker_id="worker-b",
        state=QueueState.COMPLETED,
    )
    assert second.release_lease(
        "job-1",
        lease_id=lease.lease_id,
        worker_id="worker-a",
        state=QueueState.COMPLETED,
    )
    assert PersistentQueueService(queue_file).get_job_runtime("job-1").state == QueueState.COMPLETED


def test_expired_queue_lease_is_recovered_for_a_new_worker(tmp_path) -> None:
    queue_file = tmp_path / "queue.json"
    first = PersistentQueueService(queue_file)
    second = PersistentQueueService(queue_file)
    _add_job(first)

    first.claim_job("job-1", worker_id="crashed-worker", lease_seconds=1)
    time.sleep(1.1)
    assert second.recover_expired_leases() == ["job-1"]
    recovered = second.claim_job("job-1", worker_id="recovery-worker", lease_seconds=30)
    assert recovered is not None
    assert recovered.worker_id == "recovery-worker"
