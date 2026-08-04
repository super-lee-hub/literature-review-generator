from __future__ import annotations

import multiprocessing
import time

from services.queue_service import PersistentQueueService, QueueState
from tests.test_queue_claim_leases import (
    _add_job,
    _spawn_claim_worker,
    _spawn_stale_mutator,
)


def test_multiprocess_claim_has_one_winner_and_persists_fence_token(tmp_path) -> None:
    queue_file = tmp_path / "queue.json"
    service = PersistentQueueService(queue_file)
    _add_job(service, "multiprocess-job")

    context = multiprocessing.get_context("spawn")
    start_event = context.Event()
    result_queue = context.Queue()
    processes = [
        context.Process(
            target=_spawn_claim_worker,
            args=(queue_file.as_posix(), "multiprocess-job", worker_id, start_event, result_queue),
        )
        for worker_id in ("process-a", "process-b")
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
    runtime = PersistentQueueService(queue_file).get_job_runtime("multiprocess-job")
    assert runtime is not None
    assert runtime.worker_id == winners[0]["worker_id"]
    assert runtime.fence_token == winners[0]["fence_token"]


def test_multiprocess_stale_worker_cannot_publish_after_recovery(tmp_path) -> None:
    queue_file = tmp_path / "queue.json"
    service = PersistentQueueService(queue_file)
    _add_job(service, "multiprocess-fenced-job")

    stale_lease = service.claim_job("multiprocess-fenced-job", worker_id="stale", lease_seconds=1)
    assert stale_lease is not None
    time.sleep(1.1)
    assert service.recover_expired_leases() == ["multiprocess-fenced-job"]
    current_lease = service.claim_job("multiprocess-fenced-job", worker_id="current", lease_seconds=30)
    assert current_lease is not None

    context = multiprocessing.get_context("spawn")
    result_queue = context.Queue()
    process = context.Process(
        target=_spawn_stale_mutator,
        args=(
            queue_file.as_posix(),
            "multiprocess-fenced-job",
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
    final_runtime = PersistentQueueService(queue_file).get_job_runtime("multiprocess-fenced-job")
    assert final_runtime is not None
    assert final_runtime.state == QueueState.RUNNING
    assert final_runtime.worker_id == "current"
    assert final_runtime.fence_token == current_lease.fence_token
