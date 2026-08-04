from __future__ import annotations

import multiprocessing
import threading
import time
from typing import Any

from services.artifact_registry import ArtifactRegistry
from services.job_workspace import atomic_write_json
from services.queue_service import PersistentQueueService, QueueJobSpec, QueueState
from services.queue_service import QueueRunner


def _spawn_claim_worker(
    queue_file: str,
    job_id: str,
    worker_id: str,
    start_event: Any,
    result_queue: Any,
) -> None:
    try:
        if not start_event.wait(10):
            raise RuntimeError("claim worker start gate timed out")
        service = PersistentQueueService(queue_file)
        lease = service.claim_job(job_id, worker_id=worker_id, lease_seconds=30)
        result_queue.put(
            {
                "worker_id": worker_id,
                "claimed": lease is not None,
                "lease_generation": lease.lease_generation if lease is not None else 0,
                "fence_token": lease.fence_token if lease is not None else "",
            }
        )
    except BaseException as exc:  # pragma: no cover - surfaced through the parent assertion
        result_queue.put({"worker_id": worker_id, "error": f"{type(exc).__name__}: {exc}"})


def _spawn_stale_mutator(
    queue_file: str,
    job_id: str,
    lease_id: str,
    worker_id: str,
    lease_generation: int,
    fence_token: str,
    result_queue: Any,
) -> None:
    try:
        service = PersistentQueueService(queue_file)
        result_queue.put(
            {
                "progress": service.update_job_progress_snapshot_with_lease(
                    job_id,
                    {"stage": "stale-worker"},
                    lease_id=lease_id,
                    worker_id=worker_id,
                    lease_generation=lease_generation,
                    fence_token=fence_token,
                ),
                "result": service.set_job_result_with_lease(
                    job_id,
                    {"writer": "stale-worker"},
                    lease_id=lease_id,
                    worker_id=worker_id,
                    lease_generation=lease_generation,
                    fence_token=fence_token,
                ),
                "release": service.release_lease(
                    job_id,
                    lease_id=lease_id,
                    worker_id=worker_id,
                    lease_generation=lease_generation,
                    fence_token=fence_token,
                    state=QueueState.FAILED,
                ),
            }
        )
    except BaseException as exc:  # pragma: no cover - surfaced through the parent assertion
        result_queue.put({"error": f"{type(exc).__name__}: {exc}"})


def _spawn_stale_canonical_mutator(
    queue_file: str,
    job_id: str,
    artifact_path: str,
    registry_path: str,
    lease_id: str,
    worker_id: str,
    lease_generation: int,
    fence_token: str,
    result_queue: Any,
) -> None:
    try:
        service = PersistentQueueService(queue_file)
        result_queue.put(
            {
                "canonical": service.register_canonical_artifact_with_lease(
                    job_id,
                    artifact_path,
                    lease_id=lease_id,
                    worker_id=worker_id,
                    lease_generation=lease_generation,
                    fence_token=fence_token,
                    registry_path=registry_path,
                )
            }
        )
    except BaseException as exc:  # pragma: no cover - surfaced through the parent assertion
        result_queue.put({"error": f"{type(exc).__name__}: {exc}"})


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


def test_queue_claim_is_single_winner_across_spawned_processes(tmp_path) -> None:
    queue_file = tmp_path / "queue.json"
    service = PersistentQueueService(queue_file)
    _add_job(service, "spawned-job")

    context = multiprocessing.get_context("spawn")
    start_event = context.Event()
    result_queue = context.Queue()
    processes = [
        context.Process(
            target=_spawn_claim_worker,
            args=(str(queue_file), "spawned-job", worker_id, start_event, result_queue),
        )
        for worker_id in ("spawn-worker-a", "spawn-worker-b")
    ]
    try:
        for process in processes:
            process.start()
        start_event.set()
        for process in processes:
            process.join(30)
        assert all(not process.is_alive() for process in processes)
        assert all(process.exitcode == 0 for process in processes)
        results = [result_queue.get(timeout=5) for _ in processes]
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
            process.join(timeout=5)
        result_queue.close()
        result_queue.join_thread()

    assert all("error" not in result for result in results), results
    winners = [result for result in results if result.get("claimed")]
    assert len(winners) == 1, results
    assert winners[0]["lease_generation"] == 1
    runtime = PersistentQueueService(queue_file).get_job_runtime("spawned-job")
    assert runtime is not None
    assert runtime.worker_id == winners[0]["worker_id"]
    assert runtime.fence_token == winners[0]["fence_token"]


def test_stale_spawned_worker_cannot_publish_after_lease_recovery(tmp_path) -> None:
    queue_file = tmp_path / "queue.json"
    service = PersistentQueueService(queue_file)
    _add_job(service, "fenced-job")

    stale_lease = service.claim_job("fenced-job", worker_id="stale-worker", lease_seconds=1)
    assert stale_lease is not None
    time.sleep(1.1)
    assert service.recover_expired_leases() == ["fenced-job"]
    current_lease = service.claim_job("fenced-job", worker_id="current-worker", lease_seconds=30)
    assert current_lease is not None
    assert current_lease.lease_generation == stale_lease.lease_generation + 1

    context = multiprocessing.get_context("spawn")
    result_queue = context.Queue()
    process = context.Process(
        target=_spawn_stale_mutator,
        args=(
            str(queue_file),
            "fenced-job",
            stale_lease.lease_id,
            stale_lease.worker_id,
            stale_lease.lease_generation,
            stale_lease.fence_token,
            result_queue,
        ),
    )
    try:
        process.start()
        process.join(30)
        assert not process.is_alive()
        assert process.exitcode == 0
        result = result_queue.get(timeout=5)
    finally:
        if process.is_alive():
            process.terminate()
        process.join(timeout=5)
        result_queue.close()
        result_queue.join_thread()

    assert "error" not in result, result
    assert result == {"progress": False, "result": False, "release": False}
    final_runtime = PersistentQueueService(queue_file).get_job_runtime("fenced-job")
    assert final_runtime is not None
    assert final_runtime.state == QueueState.RUNNING
    assert final_runtime.worker_id == "current-worker"
    assert final_runtime.fence_token == current_lease.fence_token


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
