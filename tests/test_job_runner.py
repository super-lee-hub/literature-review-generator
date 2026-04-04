import json
import threading

import main
from services.job_runner import JobRunRequest, JobRunResult, JobRunner
from services.queue_service import CancelToken


class _DummyLogger:
    def info(self, *_args, **_kwargs):
        pass

    def warning(self, *_args, **_kwargs):
        pass

    def error(self, *_args, **_kwargs):
        pass

    def success(self, *_args, **_kwargs):
        pass


class _DummyGenerator:
    def __init__(self, config_file, project_name, pdf_folder, queue_file=None, zotero_report=None, library_path=None):
        self.config_file = config_file
        self.project_name = project_name
        self.pdf_folder = pdf_folder
        self.queue_file = queue_file
        self.zotero_report = zotero_report
        self.library_path = library_path
        self.config = {"Paths": {"output_path": ""}}
        self.logger = _DummyLogger()
        self.progress_tracker = None
        self.free_mode_profile_path = None
        self.free_mode_idea = None
        self.cancel_token = None
        self.bound_workspace = None
        self.bound_registry = None

    def load_configuration(self):
        return True

    def bind_job_workspace(self, **kwargs):
        self.bound_workspace = kwargs["workspace"]
        self.bound_registry = kwargs["artifact_registry"]
        self.compat_config = kwargs["compat_config"]
        self.job_workspace = kwargs["workspace"]
        self.output_dir = kwargs["workspace"].root_dir
        self.summary_file = kwargs["workspace"].artifact_path(f"{kwargs['workspace'].project_name}_summaries.json")

    def _get_concept_profile_file_path(self):
        return self.job_workspace.artifact_path(f"{self.project_name}_concept_profile.json")

    def load_existing_summaries(self):
        return True


def test_job_runner_creates_workspace_and_pointer(tmp_path, monkeypatch) -> None:
    output_dir = tmp_path / "output"
    called = {}

    class _Generator(_DummyGenerator):
        def __init__(self, config_file, project_name, pdf_folder, queue_file=None, zotero_report=None, library_path=None):
            super().__init__(config_file, project_name, pdf_folder, queue_file)
            self.config = {"Paths": {"output_path": str(output_dir)}}

    def _handle_stage_one(generator, args):
        called["workspace"] = generator.bound_workspace.root_dir
        called["project_name"] = args.project_name

    monkeypatch.setattr(main, "LiteratureReviewGenerator", _Generator)
    monkeypatch.setattr(main, "handle_stage_one_mode", _handle_stage_one)

    runner = JobRunner()
    result = runner.run(
        JobRunRequest(
            config="config.ini",
            project_name="demo",
            pdf_folder=None,
            action="analyze",
        )
    )

    pointer_path = output_dir / "demo" / "_latest_job.json"
    pointer_payload = json.loads(pointer_path.read_text(encoding="utf-8"))

    assert result.success is True
    assert called["project_name"] == "demo"
    assert pointer_payload["status"] == "completed"
    assert pointer_payload["workspace_path"] == called["workspace"]
    assert pointer_payload["artifact_registry_path"].endswith("artifact_registry.json")


def test_job_runner_aborts_immediately_when_cancelled_before_start(monkeypatch) -> None:
    constructed = {"count": 0}

    class _Generator(_DummyGenerator):
        def __init__(self, config_file, project_name, pdf_folder, queue_file=None, zotero_report=None, library_path=None):
            constructed["count"] += 1
            super().__init__(config_file, project_name, pdf_folder, queue_file, zotero_report, library_path)

    token = CancelToken()
    token.request_cancel()

    monkeypatch.setattr(main, "LiteratureReviewGenerator", _Generator)

    result = JobRunner().run(
        JobRunRequest(
            config="config.ini",
            project_name="demo",
            pdf_folder=None,
            action="analyze",
        ),
        cancel_token=token,
    )

    assert result.success is False
    assert result.exit_code == 130
    assert result.workspace_path == ""
    assert constructed["count"] == 0


def test_job_runner_cancels_at_handler_loop_boundary(tmp_path, monkeypatch) -> None:
    output_dir = tmp_path / "output"
    first_loop_boundary = threading.Event()
    continue_loop = threading.Event()
    observed_iterations: list[int] = []
    result_holder: dict[str, JobRunResult] = {}

    class _Generator(_DummyGenerator):
        def __init__(self, config_file, project_name, pdf_folder, queue_file=None, zotero_report=None, library_path=None):
            super().__init__(config_file, project_name, pdf_folder, queue_file)
            self.config = {"Paths": {"output_path": str(output_dir)}}

    def _handle_stage_one(generator, _args):
        for iteration in range(5):
            generator.cancel_token.check_cancelled()
            observed_iterations.append(iteration)
            if iteration == 0:
                first_loop_boundary.set()
                assert continue_loop.wait(timeout=2), "timed out waiting for cancellation trigger"
        return True

    monkeypatch.setattr(main, "LiteratureReviewGenerator", _Generator)
    monkeypatch.setattr(main, "handle_stage_one_mode", _handle_stage_one)

    token = CancelToken()
    runner = JobRunner()

    def _run_job() -> None:
        result_holder["result"] = runner.run(
            JobRunRequest(
                config="config.ini",
                project_name="demo",
                pdf_folder=None,
                action="analyze",
            ),
            cancel_token=token,
        )

    worker = threading.Thread(target=_run_job)
    worker.start()
    assert first_loop_boundary.wait(timeout=2), "handler never reached the first loop boundary"
    token.request_cancel()
    continue_loop.set()
    worker.join(timeout=5)
    assert not worker.is_alive(), "job runner thread did not exit after cancellation"

    result = result_holder["result"]
    assert result.success is False
    assert result.exit_code == 130
    assert observed_iterations == [0]

    pointer_path = output_dir / "demo" / "_latest_job.json"
    pointer_payload = json.loads(pointer_path.read_text(encoding="utf-8"))

    assert pointer_payload["status"] == "cancelled"


def test_job_runner_marks_failed_handler_in_result_pointer_and_resume_report(tmp_path, monkeypatch) -> None:
    output_dir = tmp_path / "output"

    class _Generator(_DummyGenerator):
        def __init__(self, config_file, project_name, pdf_folder, queue_file=None, zotero_report=None, library_path=None):
            super().__init__(config_file, project_name, pdf_folder, queue_file)
            self.config = {"Paths": {"output_path": str(output_dir)}}

    monkeypatch.setattr(main, "LiteratureReviewGenerator", _Generator)
    monkeypatch.setattr(main, "handle_stage_one_mode", lambda _generator, _args: False)

    result = JobRunner().run(
        JobRunRequest(
            config="config.ini",
            project_name="demo",
            pdf_folder=None,
            action="analyze",
        )
    )

    pointer_path = output_dir / "demo" / "_latest_job.json"
    pointer_payload = json.loads(pointer_path.read_text(encoding="utf-8"))
    resume_report_path = output_dir / f"demo__{result.job_id}" / "artifacts" / "resume_state_report.json"
    resume_report = json.loads(resume_report_path.read_text(encoding="utf-8"))
    registry_path = output_dir / f"demo__{result.job_id}" / "artifact_registry.json"
    registry_payload = json.loads(registry_path.read_text(encoding="utf-8"))

    assert result.success is False
    assert result.exit_code == 1
    assert pointer_payload["status"] == "failed"
    assert resume_report["state"] != "strong_resumable"
    assert not any(item["artifact_type"] == "summary_file" for item in registry_payload["artifacts"])
