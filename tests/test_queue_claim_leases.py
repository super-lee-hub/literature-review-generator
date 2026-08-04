from __future__ import annotations

import hashlib
import io
import multiprocessing
from pathlib import Path
import threading
import time
from typing import Any
import zipfile

import pytest

from services.artifact_registry import ArtifactRegistry
from services.job_workspace import atomic_write_json
from services.queue_service import PersistentQueueService, QueueJobSpec, QueueLease, QueueState
from services.queue_service import QueueRunner
from services.queue_service import QueuePublicationRejected


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


def _spawn_stale_registry_mutator(
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
    """Exercise the lease-bound Registry facade from a real spawn child."""

    try:
        service = PersistentQueueService(queue_file)
        stale_lease = QueueLease(
            job_id=job_id,
            lease_id=lease_id,
            worker_id=worker_id,
            expires_at="",
            revision=0,
            lease_generation=lease_generation,
            fence_token=fence_token,
        )
        registry = service.publication_context(stale_lease).registry(registry_path, job_id)
        register_rejected = False
        switch_rejected = False
        try:
            registry.register_file(
                artifact_id="stale:spawn-register",
                artifact_role="test",
                artifact_type="test",
                artifact_version="v1",
                path=artifact_path,
                producer="tests",
            )
        except QueuePublicationRejected:
            register_rejected = True
        try:
            registry.switch_current_artifact_set(None)  # type: ignore[arg-type]
        except QueuePublicationRejected:
            switch_rejected = True
        result_queue.put({"register_rejected": register_rejected, "switch_rejected": switch_rejected})
    except BaseException as exc:  # pragma: no cover - surfaced through the parent assertion
        result_queue.put({"error": f"{type(exc).__name__}: {exc}"})


def _spawn_stage_then_finalize(
    queue_file: str,
    job_id: str,
    target_path: str,
    registry_path: str,
    artifact_kind: str,
    payload: bytes,
    lease_id: str,
    worker_id: str,
    lease_generation: int,
    fence_token: str,
    staged_event: Any,
    release_event: Any,
    result_queue: Any,
) -> None:
    """Stage bytes in a real child, then try the fenced publication boundary."""

    try:
        service = PersistentQueueService(queue_file)
        stale_lease = QueueLease(
            job_id=job_id,
            lease_id=lease_id,
            worker_id=worker_id,
            expires_at="",
            revision=0,
            lease_generation=lease_generation,
            fence_token=fence_token,
        )
        context = service.publication_context(stale_lease)
        registry = context.registry(registry_path, job_id)
        staged = context.stage_bytes(target_path, payload)
        staged_event.set()
        if not release_event.wait(30):
            raise RuntimeError("staged publication release gate timed out")
        try:
            publication = context.finalize_staged(
                staged,
                registry=registry,
                register_kwargs={
                    "artifact_id": f"spawn:{artifact_kind}",
                    "artifact_role": "spawn_test_artifact",
                    "artifact_type": artifact_kind,
                    "artifact_version": "v1",
                    "producer": "tests.test_queue_claim_leases",
                },
            )
        except QueuePublicationRejected as exc:
            result_queue.put(
                {
                    "finalize_rejected": True,
                    "reason": str(exc),
                    "staged_path": staged.staging_path,
                    "content_hash": staged.content_hash,
                }
            )
        else:
            result_queue.put(
                {
                    "finalize_rejected": False,
                    "final_path": publication.final_path,
                    "staged_path": staged.staging_path,
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


def test_stale_queue_owned_registry_rejects_direct_register_and_switch(tmp_path) -> None:
    queue_file = tmp_path / "queue.json"
    registry_path = tmp_path / "workspace" / "artifact_registry.json"
    artifact_path = tmp_path / "workspace" / "artifact.json"
    artifact_path.parent.mkdir()
    atomic_write_json(str(artifact_path), {"artifact": "current"})
    service = PersistentQueueService(queue_file)
    _add_job(service, "direct-fenced-job")

    stale_lease = service.claim_job("direct-fenced-job", worker_id="stale", lease_seconds=1)
    assert stale_lease is not None
    time.sleep(1.1)
    assert service.recover_expired_leases() == ["direct-fenced-job"]
    current_lease = service.claim_job("direct-fenced-job", worker_id="current", lease_seconds=30)
    assert current_lease is not None

    stale_registry = service.publication_context(stale_lease).registry(
        registry_path,
        "direct-fenced-job",
    )
    with pytest.raises(QueuePublicationRejected):
        stale_registry.register_file(
            artifact_id="stale:direct-register",
            artifact_role="test",
            artifact_type="test",
            artifact_version="v1",
            path=artifact_path,
            producer="tests",
        )
    with pytest.raises(QueuePublicationRejected):
        stale_registry.switch_current_artifact_set(None)  # type: ignore[arg-type]

    current_registry = service.publication_context(current_lease).registry(
        registry_path,
        "direct-fenced-job",
    )
    registered = current_registry.register_file(
        artifact_id="current:direct-register",
        artifact_role="test",
        artifact_type="test",
        artifact_version="v1",
        path=artifact_path,
        producer="tests",
    )
    assert registered.status == "ready"


def test_stale_spawned_worker_cannot_publish_through_queue_owned_registry(tmp_path) -> None:
    queue_file = tmp_path / "queue.json"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry_path = workspace / "artifact_registry.json"
    artifact_path = workspace / "artifact.json"
    atomic_write_json(str(artifact_path), {"artifact": "spawn-fenced"})
    service = PersistentQueueService(queue_file)
    _add_job(service, "spawn-registry-fenced-job")

    stale_lease = service.claim_job("spawn-registry-fenced-job", worker_id="stale", lease_seconds=1)
    assert stale_lease is not None
    time.sleep(1.1)
    assert service.recover_expired_leases() == ["spawn-registry-fenced-job"]
    current_lease = service.claim_job("spawn-registry-fenced-job", worker_id="current", lease_seconds=30)
    assert current_lease is not None

    current_registry = service.publication_context(current_lease).registry(
        registry_path,
        "spawn-registry-fenced-job",
    )
    current_record = current_registry.register_file(
        artifact_id="current:spawn-register",
        artifact_role="test",
        artifact_type="test",
        artifact_version="v1",
        path=artifact_path,
        producer="tests",
    )

    context = multiprocessing.get_context("spawn")
    result_queue = context.Queue()
    process = context.Process(
        target=_spawn_stale_registry_mutator,
        args=(
            str(queue_file),
            "spawn-registry-fenced-job",
            str(artifact_path),
            str(registry_path),
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

    assert result == {"register_rejected": True, "switch_rejected": True}
    records = ArtifactRegistry(registry_path, "spawn-registry-fenced-job").list_records()
    assert any(record.artifact_id == current_record.artifact_id for record in records)
    assert not any(record.artifact_id == "stale:spawn-register" for record in records)
    assert PersistentQueueService(queue_file).get_job_runtime("spawn-registry-fenced-job").fence_token == current_lease.fence_token


def test_spawned_staging_cannot_finalize_after_recovery_for_json_docx_and_export_zip(tmp_path) -> None:
    """A staged byte set stays private when recovery changes the lease fence."""

    def zip_payload(filename: str, value: bytes) -> bytes:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(filename, value)
        return buffer.getvalue()

    payloads = {
        "json": b'{"artifact":"json"}',
        "docx": zip_payload("[Content_Types].xml", b"<Types/>"),
        "export_zip": zip_payload("provenance_manifest.json", b"{}"),
    }
    context = multiprocessing.get_context("spawn")
    for artifact_kind, payload in payloads.items():
        queue_file = tmp_path / f"{artifact_kind}.queue.json"
        workspace = tmp_path / artifact_kind
        workspace.mkdir()
        registry_path = workspace / "artifact_registry.json"
        target_path = workspace / f"{artifact_kind}.artifact"
        service = PersistentQueueService(queue_file)
        job_id = f"spawn-stage-{artifact_kind}"
        _add_job(service, job_id)
        stale_lease = service.claim_job(job_id, worker_id="stale-stage-worker", lease_seconds=1)
        assert stale_lease is not None
        staged_event = context.Event()
        release_event = context.Event()
        result_queue = context.Queue()
        current_lease = None
        process = context.Process(
            target=_spawn_stage_then_finalize,
            args=(
                str(queue_file),
                job_id,
                str(target_path),
                str(registry_path),
                artifact_kind,
                payload,
                stale_lease.lease_id,
                stale_lease.worker_id,
                stale_lease.lease_generation,
                stale_lease.fence_token,
                staged_event,
                release_event,
                result_queue,
            ),
        )
        try:
            process.start()
            assert staged_event.wait(20)
            time.sleep(1.1)
            assert service.recover_expired_leases() == [job_id]
            current_lease = service.claim_job(job_id, worker_id="current-stage-worker", lease_seconds=30)
            assert current_lease is not None
            release_event.set()
            process.join(30)
            assert not process.is_alive()
            assert process.exitcode == 0
            result = result_queue.get(timeout=5)
        finally:
            release_event.set()
            if process.is_alive():
                process.terminate()
            process.join(timeout=5)
            result_queue.close()
            result_queue.join_thread()
            if current_lease is not None:
                service.release_lease(
                    job_id,
                    lease_id=current_lease.lease_id,
                    worker_id=current_lease.worker_id,
                    lease_generation=current_lease.lease_generation,
                    fence_token=current_lease.fence_token,
                    state=QueueState.COMPLETED,
                )

        assert "error" not in result, result
        assert result["finalize_rejected"] is True, result
        assert not target_path.exists()
        staged_path = Path(result["staged_path"])
        assert staged_path.is_file()
        assert staged_path.read_bytes() == payload
        assert not registry_path.exists()
        assert zipfile.is_zipfile(io.BytesIO(payload)) == (artifact_kind != "json")


def test_queue_publication_keeps_immutable_orphan_when_registry_fails_after_rename(
    tmp_path,
    monkeypatch,
) -> None:
    queue_file = tmp_path / "queue.json"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry_path = workspace / "artifact_registry.json"
    target_path = workspace / "artifact.json"
    payload = b'{"artifact":"orphan"}'
    service = PersistentQueueService(queue_file)
    job_id = "registry-failure-job"
    _add_job(service, job_id)
    lease = service.claim_job(job_id, worker_id="registry-worker", lease_seconds=30)
    assert lease is not None
    context = service.publication_context(lease)
    registry = context.registry(registry_path, job_id)
    staged = context.stage_bytes(target_path, payload)

    def fail_register(*args: Any, **kwargs: Any) -> Any:
        raise OSError("injected Registry failure after byte finalization")

    monkeypatch.setattr(ArtifactRegistry, "register_file", fail_register)
    with pytest.raises(OSError, match="injected Registry failure"):
        context.finalize_staged(
            staged,
            registry=registry,
            register_kwargs={
                "artifact_id": "registry-failure-artifact",
                "artifact_role": "test",
                "artifact_type": "test",
                "artifact_version": "v1",
                "producer": "tests",
            },
        )

    final_path = target_path.with_name(
        f"{target_path.stem}__{hashlib.sha256(payload).hexdigest()[:24]}{target_path.suffix}"
    )
    assert final_path.is_file()
    assert final_path.read_bytes() == payload
    assert not target_path.exists()
    assert not registry_path.exists()
    assert not Path(staged.staging_path).exists()
    assert service.release_lease(
        job_id,
        lease_id=lease.lease_id,
        worker_id=lease.worker_id,
        lease_generation=lease.lease_generation,
        fence_token=lease.fence_token,
        state=QueueState.COMPLETED,
    )


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
