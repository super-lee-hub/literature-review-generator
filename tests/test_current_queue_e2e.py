from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

from reviewctl import main
from runtime.job_spec import RuntimeJobSpec, RuntimeSourceSpec
from services.queue_service import (
    JobCancelledError,
    PersistentQueueService,
    QueueJobSpec,
    QueueRunner,
    QueueState,
)


def _last_json(output: str) -> dict:
    return json.loads(output.strip().splitlines()[-1])


def _strict_queue_parameters(
    tmp_path: Path,
    *,
    project_name: str,
    action: str = "analyze",
    pdf_dir: Path | None = None,
) -> tuple[dict, Path]:
    config_path = tmp_path / "queue-config.ini"
    if not config_path.exists():
        config_path.write_text("[Paths]\noutput_path = ./output\n", encoding="utf-8")
    source_dir = pdf_dir or (tmp_path / "queue-pdfs")
    source_dir.mkdir(exist_ok=True)
    pdf_path = source_dir / "paper.pdf"
    if not pdf_path.exists():
        pdf_path.write_bytes(b"%PDF-1.4\nqueue input\n")
    spec = RuntimeJobSpec(
        project_name=project_name,
        source=RuntimeSourceSpec(mode="direct", pdf_folder=str(source_dir)),
        config=str(config_path),
        action=action,
        metadata={},
    )
    return spec.to_dict(), pdf_path


def test_current_queue_cli_supports_mixed_inputs_restart_retry_cancel_and_export(
    tmp_path: Path,
    capsys,
) -> None:
    queue_file = tmp_path / "queue.json"
    parameters_file = tmp_path / "parameters.json"
    parameters_file.write_text(
        json.dumps({"config": "config.ini", "project_name": "file-job", "action": "analyze"}),
        encoding="utf-8",
    )

    assert main([
        "queue-add",
        "--queue-file",
        str(queue_file),
        "--job-id",
        "cli-job",
        "--job-type",
        "analyze",
        "--project-name",
        "cli-project",
        "--parameters",
        '{"action":"analyze","project_name":"cli-project"}',
    ]) == 0
    added = _last_json(capsys.readouterr().out)
    assert added["status"] == "added"

    assert main([
        "queue-add",
        "--queue-file",
        str(queue_file),
        "--job-id",
        "file-job",
        "--project-name",
        "file-project",
        "--parameters-file",
        str(parameters_file),
    ]) == 0
    capsys.readouterr()

    service = PersistentQueueService(queue_file)
    service.update_job_state("cli-job", QueueState.RUNNING)
    service.update_job_state("cli-job", QueueState.FAILED)

    assert main(["queue-retry", "--queue-file", str(queue_file), "--job", "cli-job"]) == 0
    retried = _last_json(capsys.readouterr().out)
    assert retried["retried_job_ids"] == ["cli-job"]
    retried_service = PersistentQueueService(queue_file)
    assert retried_service.get_job_runtime("cli-job") is not None
    assert retried_service.get_job_runtime("cli-job").state == QueueState.PENDING  # type: ignore[union-attr]

    assert main(["queue-cancel", "--queue-file", str(queue_file), "--job", "file-job"]) == 0
    cancelled = _last_json(capsys.readouterr().out)
    assert cancelled["status"] == "requested"
    restarted = PersistentQueueService(queue_file)
    assert restarted.get_job_runtime("file-job").state == QueueState.CANCELLED  # type: ignore[union-attr]

    exported = tmp_path / "queue-export.json"
    assert main([
        "queue-export",
        "--queue-file",
        str(queue_file),
        "--output",
        str(exported),
    ]) == 0
    capsys.readouterr()
    imported_file = tmp_path / "queue-imported.json"
    assert main([
        "queue-import",
        "--queue-file",
        str(imported_file),
        "--input",
        str(exported),
    ]) == 0
    imported = _last_json(capsys.readouterr().out)
    assert imported["status"] == "imported"
    assert {item["job_id"] for item in imported["jobs"]} == {"cli-job", "file-job"}


def test_queue_run_reports_selected_terminal_states_and_returns_nonzero_for_failure_or_cancel(
    tmp_path: Path,
    capsys,
) -> None:
    failed_queue = tmp_path / "failed-queue.json"
    failed_service = PersistentQueueService(failed_queue)
    # No strict runtime spec means the job is rejected before JobRunner can
    # construct a provider/runtime; this stays an offline CLI test.
    failed_service.add_job(
        QueueJobSpec(
            job_id="invalid-inputs",
            job_type="analyze",
            project_name="offline",
            parameters={"action": "analyze"},
        )
    )

    assert main(["queue-run", "--queue-file", str(failed_queue)]) == 1
    failed_payload = _last_json(capsys.readouterr().out)
    assert failed_payload["status"] == "failed"
    assert failed_payload["selected_job_ids"] == ["invalid-inputs"]
    failed_result = failed_payload["job_results"][0]
    assert failed_result["job_id"] == "invalid-inputs"
    assert failed_result["state"] == "failed"
    assert failed_result["exit_code"] is None
    assert failed_result["result_summary"]["status"] == "rejected_input_drift"

    cancelled_queue = tmp_path / "cancelled-queue.json"
    cancelled_service = PersistentQueueService(cancelled_queue)
    cancelled_service.add_job(
        QueueJobSpec(job_id="cancelled", job_type="test", project_name="offline")
    )
    assert cancelled_service.request_cancel("cancelled", reason="test") is True

    assert main(["queue-run", "--queue-file", str(cancelled_queue), "--all"]) == 1
    cancelled_payload = _last_json(capsys.readouterr().out)
    assert cancelled_payload["status"] == "cancelled"
    assert cancelled_payload["job_results"][0]["state"] == "cancelled"


def test_current_queue_runner_cooperatively_acknowledges_running_cancel(tmp_path: Path) -> None:
    queue_file = tmp_path / "queue.json"
    service = PersistentQueueService(queue_file)
    service.add_job(QueueJobSpec(job_id="job-a", job_type="test", project_name="p"))
    service.update_job_state("job-a", QueueState.RUNNING)
    assert service.request_cancel("job-a", reason="test") is True
    assert service.acknowledge_cancel("job-a", worker="test-worker") is True
    assert service.update_job_state("job-a", QueueState.CANCELLED) is True
    assert service.get_job_runtime("job-a").state == QueueState.CANCELLED  # type: ignore[union-attr]


def test_current_queue_persists_canonical_workspace_independent_of_cwd(tmp_path: Path, monkeypatch) -> None:
    queue_file = tmp_path / "output" / "_queue" / "queue.json"
    queue_file.parent.mkdir(parents=True)
    service = PersistentQueueService(queue_file)
    service.add_job(
        QueueJobSpec(
            job_id="job-paths",
            job_type="analyze",
            project_name="project",
            parameters={"output_path": "reports"},
        )
    )

    expected_root = (tmp_path / "output" / "reports").resolve()
    expected_workspace = (expected_root / "project__job-paths").resolve()
    persisted = json.loads(queue_file.read_text(encoding="utf-8"))
    assert persisted["jobs"]["job-paths"]["canonical_output_root"] == str(expected_root)
    assert persisted["jobs"]["job-paths"]["workspace_path"] == str(expected_workspace)

    unrelated_cwd = tmp_path / "unrelated-cwd"
    unrelated_cwd.mkdir()
    monkeypatch.chdir(unrelated_cwd)
    restarted = PersistentQueueService(queue_file)
    job = restarted.get_job("job-paths")
    assert job is not None
    assert job.canonical_output_root == str(expected_root)
    assert job.workspace_path == str(expected_workspace)


def test_current_queue_runner_is_restart_safe_and_reruns_changed_fingerprints(tmp_path: Path) -> None:
    """Exercise the real queue runner across dependencies, persistence, and re-fingerprinting."""

    queue_file = tmp_path / "output" / "_queue" / "queue.json"
    service = PersistentQueueService(queue_file)
    job_a_parameters, source_pdf = _strict_queue_parameters(
        tmp_path,
        project_name="stage-a",
    )
    job_b_parameters, _ = _strict_queue_parameters(
        tmp_path,
        project_name="stage-b",
    )
    service.add_job(
        QueueJobSpec(
            job_id="job-a",
            job_type="analyze",
            project_name="stage-a",
            parameters=job_a_parameters,
        )
    )
    service.add_job(
        QueueJobSpec(
            job_id="job-b",
            job_type="analyze",
            project_name="stage-b",
            depends_on_job_ids=["job-a"],
            parameters=job_b_parameters,
        )
    )

    calls: list[str] = []

    class _Runner:
        def run(self, request, cancel_token=None):
            calls.append(str(request.job_id))
            assert request.progress_tracker is not None
            request.progress_tracker.reset(
                task_type="queue-e2e",
                stage="analyze",
                message="started",
                indeterminate=False,
            )
            request.progress_tracker.emit(
                total=2,
                current=2,
                success_count=2,
                failure_count=0,
                remaining_count=0,
                item_label=str(request.project_name),
                message="finished",
            )
            workspace = Path(str(request.workspace_path))
            log_path = workspace / "logs" / "job.log"
            artifact_path = workspace / "artifacts" / "result.json"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            artifact_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text("queue-e2e log", encoding="utf-8")
            artifact_path.write_text(json.dumps({"job_id": request.job_id}), encoding="utf-8")
            return SimpleNamespace(
                success=True,
                job_status="completed",
                exit_code=0,
                message="completed",
                workspace_path=str(workspace),
                job_id=str(request.job_id),
                resume_state="complete",
                produced_artifacts=[str(artifact_path)],
                log_path=str(log_path),
            )

    QueueRunner(service, _Runner()).run()
    assert calls == ["job-a", "job-b"]
    for job_id in ("job-a", "job-b"):
        runtime = service.get_job_runtime(job_id)
        assert runtime is not None
        assert runtime.state == QueueState.COMPLETED
        assert runtime.progress_snapshot["stage"] == "analyze"
        assert runtime.progress_snapshot["remaining_count"] == 0
        assert Path(runtime.workspace_path).is_dir()
        assert Path(runtime.log_path).is_file()
        assert runtime.produced_artifacts
        assert Path(runtime.produced_artifacts[0]).is_file()

    restarted = PersistentQueueService(queue_file)
    QueueRunner(restarted, _Runner()).run()
    assert calls == ["job-a", "job-b"]

    changed = restarted.get_job("job-b")
    assert changed is not None
    source_pdf.write_bytes(b"%PDF-1.4\nqueue input changed\n")
    restarted.add_job(
        QueueJobSpec(
            job_id=changed.job_id,
            job_type=changed.job_type,
            project_name=changed.project_name,
            depends_on_job_ids=list(changed.depends_on_job_ids),
            parameters=dict(changed.parameters),
        )
    )
    assert restarted.get_job_runtime("job-b").state == QueueState.PENDING  # type: ignore[union-attr]
    QueueRunner(restarted, _Runner()).run()
    assert calls == ["job-a", "job-b", "job-b"]


def test_current_queue_runner_acknowledges_running_cancellation_at_safe_boundary(tmp_path: Path) -> None:
    queue_file = tmp_path / "output" / "_queue" / "queue.json"
    service = PersistentQueueService(queue_file)
    parameters, _ = _strict_queue_parameters(tmp_path, project_name="cancel-me")
    service.add_job(
        QueueJobSpec(
            job_id="job-cancel",
            job_type="analyze",
            project_name="cancel-me",
            parameters=parameters,
        )
    )
    started = threading.Event()
    observed_cancel = threading.Event()

    class _CancellableRunner:
        def run(self, request, cancel_token=None):
            started.set()
            while cancel_token is None or not cancel_token.is_cancelled():
                time.sleep(0.01)
            observed_cancel.set()
            raise JobCancelledError("safe boundary reached")

    queue_runner = QueueRunner(service, _CancellableRunner())
    worker = threading.Thread(target=queue_runner.run, name="queue-e2e-cancel")
    worker.start()
    assert started.wait(5)
    assert queue_runner.cancel_job("job-cancel") is True
    worker.join(5)
    assert not worker.is_alive()
    assert observed_cancel.is_set()

    restarted = PersistentQueueService(queue_file)
    runtime = restarted.get_job_runtime("job-cancel")
    assert runtime is not None
    assert runtime.state == QueueState.CANCELLED
    assert runtime.cancel_requested is True
    assert runtime.completed_at is not None
