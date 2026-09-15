from pathlib import Path

import pytest

from runtime.job_spec import RuntimeJobSpec, RuntimeSourceSpec
from services.queue_service import (
    QueueJobSpec,
    QueueState,
    PersistentQueueService,
    QueueRunner,
    QueueActiveLeaseError,
    create_queue_job_id,
)


def _strict_queue_parameters(
    tmp_path: Path,
    *,
    project_name: str,
    action: str = "analyze",
    **overrides,
) -> dict:
    config_path = tmp_path / f"{project_name}.ini"
    config_path.write_text("[Paths]\noutput_path = ./output\n", encoding="utf-8")
    pdf_dir = tmp_path / f"{project_name}-pdfs"
    pdf_dir.mkdir(exist_ok=True)
    (pdf_dir / "paper.pdf").write_bytes(b"%PDF-1.4\nqueue input\n")
    spec = RuntimeJobSpec(
        project_name=project_name,
        source=RuntimeSourceSpec(mode="direct", pdf_folder=str(pdf_dir)),
        config=str(config_path),
        action=action,
        metadata={},
        **overrides,
    )
    return spec.to_dict()


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
    service1.update_job_state(job_id1, QueueState.RUNNING)
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
    
    service.update_job_state(job_id1, QueueState.RUNNING)
    service.update_job_state(job_id1, QueueState.FAILED)
    service.update_job_state(job_id2, QueueState.RUNNING)
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
        if state is not QueueState.PENDING:
            service.update_job_state(job_id, QueueState.RUNNING)
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


def test_active_lease_blocks_remove_and_import_replacement(tmp_path: Path) -> None:
    queue_file = tmp_path / "queue.json"
    service = PersistentQueueService(queue_file)
    job_id = "leased-job"
    service.add_job(
        QueueJobSpec(
            job_id=job_id,
            job_type="analyze",
            project_name="leased",
            parameters=_strict_queue_parameters(tmp_path, project_name="leased"),
        )
    )
    lease = service.claim_job(job_id, worker_id="live-worker", lease_seconds=60)
    assert lease is not None

    with pytest.raises(QueueActiveLeaseError, match="active lease"):
        service.remove_job(job_id)
    assert service.get_job(job_id) is not None
    assert service.get_job_runtime(job_id).lease_id == lease.lease_id  # type: ignore[union-attr]

    imported = PersistentQueueService(tmp_path / "incoming.json")
    imported.add_job(
        QueueJobSpec(
            job_id=job_id,
            job_type="analyze",
            project_name="replacement",
            parameters=_strict_queue_parameters(tmp_path, project_name="replacement"),
        )
    )
    export_path = tmp_path / "incoming-export.json"
    imported.save_queue(export_path)

    with pytest.raises(QueueActiveLeaseError, match="target active lease"):
        service.load_queue(export_path)
    assert service.get_job(job_id).project_name == "leased"  # type: ignore[union-attr]
    assert service.get_job_runtime(job_id).lease_id == lease.lease_id  # type: ignore[union-attr]


def test_queue_runner_rejects_input_content_drift_before_runner_invocation(tmp_path: Path) -> None:
    service = PersistentQueueService(tmp_path / "queue.json")
    parameters = _strict_queue_parameters(tmp_path, project_name="drift")
    source_path = tmp_path / "drift-pdfs" / "paper.pdf"
    job_id = "drift-job"
    service.add_job(
        QueueJobSpec(
            job_id=job_id,
            job_type="analyze",
            project_name="drift",
            parameters=parameters,
        )
    )
    source_path.write_bytes(b"%PDF-1.4\nchanged after queueing\n")
    calls: list[str] = []

    class _Runner:
        def run(self, request, cancel_token=None):
            del request, cancel_token
            calls.append("called")
            raise AssertionError("input drift must prevent runner execution")

    assert QueueRunner(service, _Runner()).run_single_job(job_id) is True
    runtime = service.get_job_runtime(job_id)
    assert calls == []
    assert runtime is not None
    assert runtime.state is QueueState.FAILED
    assert runtime.result_summary["status"] == "rejected_input_drift"  # type: ignore[index]
    assert "changed after enqueue" in runtime.error_message  # type: ignore[operator]


def test_invalid_strict_action_mapping_is_rejected_before_runner_invocation(tmp_path: Path) -> None:
    service = PersistentQueueService(tmp_path / "queue.json")
    parameters = _strict_queue_parameters(tmp_path, project_name="strict")
    parameters["run_all"] = True  # A legacy boolean must not override action=analyze.
    job_id = "strict-action"
    service.add_job(
        QueueJobSpec(
            job_id=job_id,
            job_type="analyze",
            project_name="strict",
            parameters=parameters,
        )
    )

    class _Runner:
        def run(self, request, cancel_token=None):
            del request, cancel_token
            raise AssertionError("legacy action flags must not reach a runner")

    assert QueueRunner(service, _Runner()).run_single_job(job_id) is True
    runtime = service.get_job_runtime(job_id)
    assert runtime is not None
    assert runtime.state is QueueState.FAILED
    assert runtime.result_summary["status"] == "rejected_input_drift"  # type: ignore[index]
    assert "freeze_failed" in runtime.error_message  # type: ignore[operator]


def test_dependency_cycles_and_missing_dependencies_become_durable_failures(tmp_path: Path) -> None:
    service = PersistentQueueService(tmp_path / "queue.json")
    service.add_job(
        QueueJobSpec(
            job_id="missing",
            job_type="test",
            project_name="missing",
            depends_on_job_ids=["not-present"],
        )
    )
    service.add_job(
        QueueJobSpec(
            job_id="self-cycle",
            job_type="test",
            project_name="self-cycle",
            depends_on_job_ids=["self-cycle"],
        )
    )
    service.add_job(
        QueueJobSpec(
            job_id="cycle-a",
            job_type="test",
            project_name="cycle-a",
            depends_on_job_ids=["cycle-b"],
        )
    )
    service.add_job(
        QueueJobSpec(
            job_id="cycle-b",
            job_type="test",
            project_name="cycle-b",
            depends_on_job_ids=["cycle-a"],
        )
    )

    rejected = service.reject_invalid_pending_dependencies()
    assert set(rejected) == {"missing", "self-cycle", "cycle-a", "cycle-b"}
    assert "missing dependency" in rejected["missing"]
    assert "dependency cycle" in rejected["self-cycle"]
    for job_id in rejected:
        runtime = service.get_job_runtime(job_id)
        assert runtime is not None
        assert runtime.state is QueueState.FAILED
        assert runtime.result_summary["status"] == "rejected_dependency_graph"  # type: ignore[index]


def test_queue_runner_reconstructs_summary_source_and_reuse_fields(tmp_path: Path) -> None:
    queue_file = tmp_path / "test_queue.json"
    service = PersistentQueueService(queue_file)

    captured = {}

    class _Runner:
        def run(self, request, cancel_token=None):
            captured["request"] = request
            return type(
                "_Result",
                (),
                {
                    "success": False,
                    "job_status": "completed",
                    "exit_code": 0,
                    "message": "ok",
                    "workspace_path": str(tmp_path / "workspace"),
                    "job_id": "job123",
                    "resume_state": "fresh",
                    "produced_artifacts": [],
                    "log_path": "",
                    "report_paths": [],
                    "failure_summary": None,
                },
            )()

    job_id = create_queue_job_id()
    summary_file = tmp_path / "subset.json"
    summary_source = tmp_path / "subset-b.json"
    reuse_file = tmp_path / "reuse-a.json"
    for path in (summary_file, summary_source, reuse_file):
        path.write_text("{}", encoding="utf-8")
    service.add_job(
        QueueJobSpec(
            job_id=job_id,
            job_type="generate_outline",
            project_name="demo",
            parameters=_strict_queue_parameters(
                tmp_path,
                project_name="demo",
                action="generate_outline",
                summary_file=str(summary_file),
                summary_sources=(str(summary_source),),
                reuse_stage1=True,
                reuse_summary_files=(str(reuse_file),),
            ),
        )
    )

    queue_runner = QueueRunner(service, _Runner())
    assert queue_runner.run_single_job(job_id) is True
    assert captured["request"].summary_file == str(summary_file)
    assert captured["request"].summary_sources == (str(summary_file), str(summary_source))
    assert captured["request"].reuse_stage1 is True
    assert captured["request"].reuse_summary_files == (str(reuse_file),)
    runtime = service.get_job_runtime(job_id)
    assert runtime is not None
    # A runner that returns job_status=completed but reports success=False is
    # a failed queue execution, not a completed one.
    assert runtime.state is QueueState.FAILED


def test_queue_runner_persists_progress_snapshot_and_failure_log_path(tmp_path: Path) -> None:
    queue_file = tmp_path / "test_queue.json"
    service = PersistentQueueService(queue_file)
    log_path = tmp_path / "workspace" / "logs" / "job.log"
    log_path.parent.mkdir(parents=True)
    log_path.write_text("job log", encoding="utf-8")

    class _Runner:
        def run(self, request, cancel_token=None):
            assert request.progress_tracker is not None
            request.progress_tracker.reset(
                task_type="文献分析",
                stage="analyze",
                message="开始分析",
                indeterminate=False,
            )
            request.progress_tracker.emit(
                total=3,
                current=1,
                success_count=1,
                failure_count=0,
                remaining_count=2,
                item_label="Paper A",
                message="Paper A 完成",
            )
            return type(
                "_Result",
                (),
                {
                    "success": False,
                    "job_status": "failed",
                    "exit_code": 1,
                    "message": "failed",
                    "workspace_path": str(log_path.parent.parent),
                    "job_id": "job123",
                    "resume_state": "weak_resumable",
                    "produced_artifacts": [str(log_path)],
                    "log_path": str(log_path),
                    "report_paths": [],
                    "failure_summary": "failed",
                },
            )()

    job_id = create_queue_job_id()
    service.add_job(
        QueueJobSpec(
            job_id=job_id,
            job_type="analyze",
            project_name="demo",
            parameters=_strict_queue_parameters(tmp_path, project_name="demo"),
        )
    )

    queue_runner = QueueRunner(service, _Runner())
    assert queue_runner.run_single_job(job_id) is True

    runtime = service.get_job_runtime(job_id)
    assert runtime is not None
    assert runtime.state == QueueState.FAILED
    assert runtime.log_path == str(log_path)
    assert runtime.workspace_path == str(log_path.parent.parent)
    assert runtime.progress_snapshot["stage"] == "analyze"
    assert runtime.progress_snapshot["item_label"] == "Paper A"
    assert runtime.progress_snapshot["success_count"] == 1
    assert runtime.progress_snapshot["remaining_count"] == 2


def test_queue_runner_respects_reordered_job_order(tmp_path: Path) -> None:
    queue_file = tmp_path / "test_queue.json"
    service = PersistentQueueService(queue_file)

    execution_order: list[str] = []

    class _Runner:
        def run(self, request, cancel_token=None):
            execution_order.append(request.project_name)
            return type(
                "_Result",
                (),
                {
                    "success": True,
                    "job_status": "completed",
                    "exit_code": 0,
                    "message": "ok",
                    "workspace_path": str(tmp_path / f"{request.project_name}__workspace"),
                    "job_id": request.project_name,
                    "resume_state": "fresh",
                    "produced_artifacts": [],
                    "log_path": "",
                    "report_paths": [],
                    "failure_summary": None,
                },
            )()

    job_a = create_queue_job_id()
    job_b = create_queue_job_id()
    job_c = create_queue_job_id()

    for job_id, project_name in [(job_a, "A"), (job_b, "B"), (job_c, "C")]:
        service.add_job(
            QueueJobSpec(
                job_id=job_id,
                job_type="analyze",
                project_name=project_name,
                parameters=_strict_queue_parameters(tmp_path, project_name=project_name),
            )
        )

    service.reorder_jobs([job_c, job_a, job_b])

    queue_runner = QueueRunner(service, _Runner())
    queue_runner.run()

    assert execution_order == ["C", "A", "B"]
