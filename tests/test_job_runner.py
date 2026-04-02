import json

import main
from services.job_runner import JobRunRequest, JobRunner


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
    def __init__(self, config_file, project_name, pdf_folder):
        self.config_file = config_file
        self.project_name = project_name
        self.pdf_folder = pdf_folder
        self.config = {"Paths": {"output_path": ""}}
        self.logger = _DummyLogger()
        self.progress_tracker = None
        self.free_mode_profile_path = None
        self.free_mode_idea = None
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
        def __init__(self, config_file, project_name, pdf_folder):
            super().__init__(config_file, project_name, pdf_folder)
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
