from __future__ import annotations

import threading
import time

from services.queue_service import PersistentQueueService, QueueJobSpec, QueueState
from services.queue_service import QueueRunner


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


def test_queue_runner_heartbeats_while_job_runner_is_blocked(tmp_path) -> None:
    queue_file = tmp_path / "queue.json"
    service = PersistentQueueService(queue_file)
    job_id = "job-heartbeat"
    service.add_job(
        QueueJobSpec(
            job_id=job_id,
            job_type="review",
            project_name="lease-test",
            parameters={
                "config": "config.ini",
                "project_name": "lease-test",
                "pdf_folder": "D:/papers",
                "action": "review",
            },
        )
    )
    started = threading.Event()
    release = threading.Event()

    class _BlockedRunner:
        def run(self, request, cancel_token=None):
            started.set()
            assert release.wait(5)
            return type(
                "_Result",
                (),
                {
                    "job_status": "completed",
                    "exit_code": 0,
                    "message": "ok",
                    "workspace_path": str(tmp_path / "workspace"),
                    "job_id": job_id,
                    "resume_state": "fresh",
                    "produced_artifacts": [],
                    "log_path": "",
                },
            )()

    runner = QueueRunner(service, _BlockedRunner())
    runner._heartbeat_interval_seconds = 0.1
    thread = threading.Thread(target=runner.run_single_job, args=(job_id,))
    thread.start()
    assert started.wait(2)
    initial = PersistentQueueService(queue_file).get_job_runtime(job_id)
    assert initial is not None and initial.heartbeat_at
    initial_heartbeat = initial.heartbeat_at

    deadline = time.time() + 2
    observed = initial_heartbeat
    while time.time() < deadline and observed == initial_heartbeat:
        time.sleep(0.05)
        current = PersistentQueueService(queue_file).get_job_runtime(job_id)
        assert current is not None
        observed = current.heartbeat_at
    assert observed and observed != initial_heartbeat

    release.set()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert PersistentQueueService(queue_file).get_job_runtime(job_id).state == QueueState.COMPLETED
