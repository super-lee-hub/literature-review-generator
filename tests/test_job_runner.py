import json
import os
import threading

import main
from services.job_runner import (
    JobRunRequest,
    JobRunResult,
    JobRunner,
    build_job_request_from_mapping,
    validate_job_request_options,
)
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
        log_path = self.job_workspace.log_path("job.log")
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        with open(log_path, "w", encoding="utf-8") as handle:
            handle.write("workspace job log\n")
        self.bound_registry.register_file(
            artifact_role="log",
            artifact_type="job_log",
            artifact_version="v1",
            path=log_path,
            producer="tests._DummyGenerator.bind_job_workspace",
            artifact_id="job_log",
        )

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
    assert result.log_path.endswith(os.path.join("logs", "job.log"))
    assert os.path.exists(result.log_path)


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


def test_build_job_request_from_mapping_supports_summary_source_and_reuse_fields() -> None:
    request = build_job_request_from_mapping(
        {
            "config": "config.ini",
            "project_name": "demo",
            "pdf_folder": "D:/papers",
            "generate_outline": True,
            "summary_file": "D:/subset.json",
            "summary_sources": ["D:/subset-b.json"],
            "reuse_stage1": True,
            "reuse_summary_files": ["D:/reuse-a.json", "D:/reuse-b.json"],
        }
    )

    assert request.action == "generate_outline"
    assert request.summary_file == "D:/subset.json"
    assert request.summary_sources == ("D:/subset.json", "D:/subset-b.json")
    assert request.reuse_stage1 is True
    assert request.reuse_summary_files == ("D:/reuse-a.json", "D:/reuse-b.json")


def test_stage1_reuse_defaults_on_for_stage1_actions() -> None:
    analyze_request = build_job_request_from_mapping(
        {
            "config": "config.ini",
            "project_name": "demo",
            "pdf_folder": "D:/papers",
            "analyze_only": True,
        }
    )
    run_all_request = build_job_request_from_mapping(
        {
            "config": "config.ini",
            "project_name": "demo",
            "pdf_folder": "D:/papers",
            "run_all": True,
        }
    )

    assert analyze_request.action == "analyze"
    assert analyze_request.reuse_stage1 is True
    assert run_all_request.action == "run_all"
    assert run_all_request.reuse_stage1 is True


def test_stage1_reuse_can_be_explicitly_disabled() -> None:
    request = build_job_request_from_mapping(
        {
            "config": "config.ini",
            "project_name": "demo",
            "pdf_folder": "D:/papers",
            "run_all": True,
            "reuse_stage1": False,
        }
    )

    assert request.reuse_stage1 is False


def test_reuse_summary_file_uses_default_stage1_reuse() -> None:
    request = build_job_request_from_mapping(
        {
            "config": "config.ini",
            "project_name": "demo",
            "pdf_folder": "D:/papers",
            "run_all": True,
            "reuse_summary_files": ["D:/reuse-a.json"],
        }
    )

    assert request.reuse_stage1 is True
    assert request.reuse_summary_files == ("D:/reuse-a.json",)
    assert validate_job_request_options(request) is None


def test_stage1_reuse_is_off_for_downstream_actions_by_default() -> None:
    request = build_job_request_from_mapping(
        {
            "config": "config.ini",
            "project_name": "demo",
            "pdf_folder": "D:/papers",
            "generate_outline": True,
        }
    )

    assert request.action == "generate_outline"
    assert request.reuse_stage1 is False


def test_job_runner_validate_review_uses_validator_module_directly(tmp_path, monkeypatch) -> None:
    output_dir = tmp_path / "output"
    called: dict[str, object] = {}

    class _Generator(_DummyGenerator):
        def __init__(self, config_file, project_name, pdf_folder, queue_file=None, zotero_report=None, library_path=None):
            super().__init__(config_file, project_name, pdf_folder, queue_file, zotero_report, library_path)
            self.config = {"Paths": {"output_path": str(output_dir)}}

    def _fake_run_review_validation(generator):
        called["generator"] = generator
        return {"success": True, "report": None, "manual_review_items": [], "report_file": "report.txt", "manual_report_file": "manual.json"}

    monkeypatch.setattr(main, "LiteratureReviewGenerator", _Generator)
    monkeypatch.setattr("validator.run_review_validation", _fake_run_review_validation)

    result = JobRunner().run(
        JobRunRequest(
            config="config.ini",
            project_name="demo",
            pdf_folder=None,
            action="validate_review",
            validate_review=True,
        )
    )

    assert result.success is True
    assert isinstance(called["generator"], _Generator)


def test_job_runner_validate_review_recovers_lossy_project_name_from_existing_workspace(tmp_path, monkeypatch) -> None:
    output_dir = tmp_path / "output"
    requested_project_name = "_______must"
    recovered_project_name = "\u4fc3\u9500\u7efc\u8ff0\u7b2c\u4e8c\u8282must"
    stale_project_name = "\u4fc3\u9500\u7efc\u8ff0\u7b2c\u4e00\u8282must"
    called: dict[str, object] = {}

    class _Generator(_DummyGenerator):
        def __init__(self, config_file, project_name, pdf_folder, queue_file=None, zotero_report=None, library_path=None):
            super().__init__(config_file, project_name, pdf_folder, queue_file, zotero_report, library_path)
            self.config = {"Paths": {"output_path": str(output_dir)}}

    def _touch_validation_artifacts(project_name: str, job_id: str, *, mtime: int) -> None:
        workspace = output_dir / f"{project_name}__{job_id}"
        (workspace / "artifacts" / "review_drafts").mkdir(parents=True, exist_ok=True)
        (workspace / "artifacts" / "citation_manifests").mkdir(parents=True, exist_ok=True)
        (workspace / "artifacts" / f"{project_name}_summaries.json").write_text("[]", encoding="utf-8")
        (workspace / "artifacts" / "review_drafts" / f"{project_name}_review_draft_v2.json").write_text("{}", encoding="utf-8")
        (workspace / "artifacts" / "citation_manifests" / f"{project_name}_citation_manifest_v3.json").write_text("{}", encoding="utf-8")
        os.utime(workspace, (mtime, mtime))

    empty_workspace = output_dir / f"{requested_project_name}__20260419_164547"
    (empty_workspace / "artifacts").mkdir(parents=True, exist_ok=True)
    _touch_validation_artifacts(stale_project_name, "20260418_041144", mtime=100)
    _touch_validation_artifacts(recovered_project_name, "20260418_041739", mtime=200)

    def _fake_run_review_validation(generator):
        called["project_name"] = generator.project_name
        called["workspace"] = generator.job_workspace.root_dir
        return {"success": True, "report": None, "manual_review_items": [], "report_file": "report.txt", "manual_report_file": "manual.json"}

    monkeypatch.setattr(main, "LiteratureReviewGenerator", _Generator)
    monkeypatch.setattr("validator.run_review_validation", _fake_run_review_validation)

    result = JobRunner().run(
        JobRunRequest(
            config="config.ini",
            project_name=requested_project_name,
            pdf_folder=None,
            action="validate_review",
            validate_review=True,
        )
    )

    pointer_path = output_dir / recovered_project_name / "_latest_job.json"
    pointer_payload = json.loads(pointer_path.read_text(encoding="utf-8"))

    assert result.success is True
    assert called["project_name"] == recovered_project_name
    assert str(called["workspace"]).endswith(f"{recovered_project_name}__20260418_041739")
    assert pointer_payload["workspace_path"].endswith(f"{recovered_project_name}__20260418_041739")


def test_job_runner_does_not_recover_real_chinese_project_name_by_ascii_alias(tmp_path) -> None:
    output_dir = tmp_path / "output"
    existing_project_name = "方法"
    requested_project_name = "偏好"
    workspace = output_dir / f"{existing_project_name}__20260406_064148"
    artifacts = workspace / "artifacts"
    artifacts.mkdir(parents=True)
    (artifacts / f"{existing_project_name}_summaries.json").write_text("[]", encoding="utf-8")

    resolved = JobRunner()._resolve_project_name_from_existing_workspaces(
        base_output_dir=str(output_dir),
        requested_project_name=requested_project_name,
        action="generate_outline",
    )

    assert resolved == requested_project_name
