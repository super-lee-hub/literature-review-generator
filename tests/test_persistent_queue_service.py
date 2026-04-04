import json
from pathlib import Path

import pytest

from services.queue_service import (
    QueueJobSpec,
    QueueJobRuntime,
    QueueState,
    PersistentQueueService,
    create_queue_job_id,
)


def test_create_queue_job_id() -> None:
    job_id = create_queue_job_id()
    assert job_id.startswith("job_")
    assert len(job_id) > 5


def test_queue_job_spec_roundtrip(tmp_path: Path) -> None:
    queue_file = tmp_path / "test_queue.json"
    service = PersistentQueueService(queue_file)
    
    job_id = create_queue_job_id()
    spec = QueueJobSpec(
        job_id=job_id,
        job_type="analyze_papers",
        project_name="test_project",
        parameters={"pdf_folder": "test_folder"},
    )
    
    service.add_job(spec)
    
    retrieved = service.get_job(job_id)
    assert retrieved is not None
    assert retrieved.job_id == job_id
    assert retrieved.job_type == "analyze_papers"
    assert retrieved.project_name == "test_project"
    assert retrieved.parameters["pdf_folder"] == "test_folder"


def test_queue_state_transitions(tmp_path: Path) -> None:
    queue_file = tmp_path / "test_queue.json"
    service = PersistentQueueService(queue_file)
    
    job_id = create_queue_job_id()
    spec = QueueJobSpec(
        job_id=job_id,
        job_type="generate_review",
        project_name="test_project",
    )
    service.add_job(spec)
    
    runtime = service.get_job_runtime(job_id)
    assert runtime is not None
    assert runtime.state == QueueState.PENDING
    
    service.update_job_state(job_id, QueueState.RUNNING)
    runtime = service.get_job_runtime(job_id)
    assert runtime is not None
    assert runtime.state == QueueState.RUNNING
    assert runtime.started_at is not None
    
    service.update_job_state(job_id, QueueState.COMPLETED)
    runtime = service.get_job_runtime(job_id)
    assert runtime is not None
    assert runtime.state == QueueState.COMPLETED
    assert runtime.completed_at is not None


def test_persistence_across_restarts(tmp_path: Path) -> None:
    queue_file = tmp_path / "test_queue.json"
    
    job_id1 = create_queue_job_id()
    job_id2 = create_queue_job_id()
    
    service1 = PersistentQueueService(queue_file)
    service1.add_job(QueueJobSpec(
        job_id=job_id1,
        job_type="analyze",
        project_name="proj1",
    ))
    service1.add_job(QueueJobSpec(
        job_id=job_id2,
        job_type="review",
        project_name="proj2",
    ))
    service1.update_job_state(job_id1, QueueState.COMPLETED)
    
    service2 = PersistentQueueService(queue_file)
    all_jobs = service2.list_jobs()
    assert len(all_jobs) == 2
    
    runtime1 = service2.get_job_runtime(job_id1)
    assert runtime1 is not None
    assert runtime1.state == QueueState.COMPLETED
    
    runtime2 = service2.get_job_runtime(job_id2)
    assert runtime2 is not None
    assert runtime2.state == QueueState.PENDING


def test_error_and_result_tracking(tmp_path: Path) -> None:
    queue_file = tmp_path / "test_queue.json"
    service = PersistentQueueService(queue_file)
    
    job_id = create_queue_job_id()
    service.add_job(QueueJobSpec(
        job_id=job_id,
        job_type="test",
        project_name="proj",
    ))
    
    service.update_job_state(job_id, QueueState.FAILED)
    service.set_job_error(job_id, "API timeout")
    
    runtime = service.get_job_runtime(job_id)
    assert runtime is not None
    assert runtime.error_message == "API timeout"
    
    service.reset_job(job_id)
    service.update_job_state(job_id, QueueState.COMPLETED)
    service.set_job_result(job_id, {"sections_generated": 5})
    
    runtime = service.get_job_runtime(job_id)
    assert runtime is not None
    assert runtime.result_summary == {"sections_generated": 5}


def test_retry_failed_jobs(tmp_path: Path) -> None:
    queue_file = tmp_path / "test_queue.json"
    service = PersistentQueueService(queue_file)
    
    job_id1 = create_queue_job_id()
    job_id2 = create_queue_job_id()
    
    service.add_job(QueueJobSpec(job_id=job_id1, job_type="t1", project_name="p"))
    service.add_job(QueueJobSpec(job_id=job_id2, job_type="t2", project_name="p"))
    
    service.update_job_state(job_id1, QueueState.FAILED)
    service.update_job_state(job_id2, QueueState.COMPLETED)
    
    failed_jobs = service.get_failed_jobs()
    assert len(failed_jobs) == 1
    assert failed_jobs[0].job_id == job_id1
    
    retried = service.retry_failed_jobs()
    assert len(retried) == 1
    assert retried[0] == job_id1
    
    runtime = service.get_job_runtime(job_id1)
    assert runtime is not None
    assert runtime.state == QueueState.PENDING
    assert runtime.retry_count == 1


def test_list_jobs_by_state(tmp_path: Path) -> None:
    queue_file = tmp_path / "test_queue.json"
    service = PersistentQueueService(queue_file)
    
    job_ids = [create_queue_job_id() for _ in range(4)]
    states = [QueueState.PENDING, QueueState.RUNNING, QueueState.COMPLETED, QueueState.FAILED]
    
    for job_id, state in zip(job_ids, states):
        service.add_job(QueueJobSpec(job_id=job_id, job_type="test", project_name="p"))
        service.update_job_state(job_id, state)
    
    pending = service.list_jobs_by_state(QueueState.PENDING)
    assert len(pending) == 1
    assert pending[0].job_id == job_ids[0]
    
    completed = service.list_jobs_by_state(QueueState.COMPLETED)
    assert len(completed) == 1
    assert completed[0].job_id == job_ids[2]


def test_remove_job(tmp_path: Path) -> None:
    queue_file = tmp_path / "test_queue.json"
    service = PersistentQueueService(queue_file)
    
    job_id = create_queue_job_id()
    service.add_job(QueueJobSpec(job_id=job_id, job_type="test", project_name="p"))
    
    assert service.get_job(job_id) is not None
    
    result = service.remove_job(job_id)
    assert result is True
    assert service.get_job(job_id) is None
