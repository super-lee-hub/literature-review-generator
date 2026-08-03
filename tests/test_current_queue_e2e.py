from __future__ import annotations

import json
from pathlib import Path

from reviewctl import main
from services.queue_service import PersistentQueueService, QueueJobSpec, QueueState


def _last_json(output: str) -> dict:
    return json.loads(output.strip().splitlines()[-1])


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


def test_current_queue_runner_cooperatively_acknowledges_running_cancel(tmp_path: Path) -> None:
    queue_file = tmp_path / "queue.json"
    service = PersistentQueueService(queue_file)
    service.add_job(QueueJobSpec(job_id="job-a", job_type="test", project_name="p"))
    service.update_job_state("job-a", QueueState.RUNNING)
    assert service.request_cancel("job-a", reason="test") is True
    assert service.acknowledge_cancel("job-a", worker="test-worker") is True
    assert service.update_job_state("job-a", QueueState.CANCELLED) is True
    assert service.get_job_runtime("job-a").state == QueueState.CANCELLED  # type: ignore[union-attr]
