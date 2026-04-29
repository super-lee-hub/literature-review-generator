import json

import main
from config_loader import ConfigDict
from services.artifact_registry import ArtifactRegistry
from services.config_compat import CompatConfigView
from services.job_workspace import JobWorkspace
from services.progress_state import (
    ResumeStateReport,
    Stage1ProgressSnapshot,
    determine_resume_state,
    write_stage1_progress_snapshot,
)


class _DummyLogger:
    def info(self, *_args, **_kwargs):
        pass

    def warning(self, *_args, **_kwargs):
        pass

    def error(self, *_args, **_kwargs):
        pass

    def success(self, *_args, **_kwargs):
        pass

    def debug(self, *_args, **_kwargs):
        pass


def _resume_report(workspace: JobWorkspace) -> ResumeStateReport:
    return ResumeStateReport(
        artifact_type="resume_state_report",
        artifact_version="v1",
        created_from_job_id=workspace.job_id,
        created_at="2026-04-02T00:00:00Z",
        project_name=workspace.project_name,
        job_id=workspace.job_id,
        state="non_resumable",
        reason="test bootstrap",
        summary_file=workspace.artifact_path(f"{workspace.project_name}_summaries.json"),
        progress_snapshot_file=None,
        checkpoint_file=workspace.checkpoint_path(f"{workspace.project_name}_checkpoint.json"),
        fingerprint_bundle={"request": "demo"},
    )


def test_save_summaries_writes_progress_snapshot_and_registry_record(tmp_path) -> None:
    workspace = JobWorkspace.create(str(tmp_path / "output"), "demo")
    registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    config = ConfigDict({"Paths": {"output_path": str(tmp_path / "output")}, "Validation": {"stage1_enabled": "false", "stage2_enabled": "false"}})
    compat_view = CompatConfigView.from_config(config)

    generator = main.LiteratureReviewGenerator(project_name="demo", pdf_folder=str(tmp_path))
    generator.logger = _DummyLogger()  # type: ignore[assignment]
    generator.config = config
    generator.bind_job_workspace(
        workspace=workspace,
        artifact_registry=registry,
        compat_config=compat_view,
        fingerprint_bundle={"request": "demo"},
        resume_state_report=_resume_report(workspace),
    )
    generator.summaries = [{"status": "success", "paper_info": {"title": "Paper A"}}]
    generator._checkpoint_processed_papers.add("paper-a")

    assert generator.save_summaries() is True

    summary_path = workspace.artifact_path("demo_summaries.json")
    progress_path = workspace.artifact_path("stage1_progress_snapshot.json")
    registry_payload = json.loads((tmp_path / "output" / f"demo__{workspace.job_id}" / "artifact_registry.json").read_text(encoding="utf-8"))

    assert json.loads(open(summary_path, "r", encoding="utf-8").read())[0]["status"] == "success"
    progress_payload = json.loads(open(progress_path, "r", encoding="utf-8").read())
    assert progress_payload["artifact_type"] == "stage1_progress_snapshot"
    assert progress_payload["summary_file"] == summary_path
    assert any(item["artifact_type"] == "summary_file" for item in registry_payload["artifacts"])
    assert any(item["artifact_type"] == "stage1_progress_snapshot" for item in registry_payload["artifacts"])


def test_summaries_without_progress_snapshot_are_weak_resumable(tmp_path) -> None:
    workspace = JobWorkspace.create(str(tmp_path / "output"), "demo")
    summary_path = workspace.artifact_path("demo_summaries.json")
    summary_path_parent = workspace.paths.artifacts_dir
    assert summary_path_parent
    with open(summary_path, "w", encoding="utf-8") as handle:
        json.dump([{"status": "success"}], handle, ensure_ascii=False, indent=2)

    report = determine_resume_state(
        project_name="demo",
        job_id=workspace.job_id,
        summary_file=summary_path,
        progress_snapshot_file=workspace.artifact_path("stage1_progress_snapshot.json"),
        checkpoint_file=workspace.checkpoint_path("demo_checkpoint.json"),
        expected_fingerprint_bundle={"request": "demo"},
    )

    assert report.state == "weak_resumable"


def test_restore_stage1_progress_from_snapshot_recovers_processed_and_failed_sets(tmp_path) -> None:
    workspace = JobWorkspace.create(str(tmp_path / "output"), "demo")
    registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    config = ConfigDict({"Paths": {"output_path": str(tmp_path / "output")}, "Validation": {"stage1_enabled": "false", "stage2_enabled": "false"}})
    compat_view = CompatConfigView.from_config(config)

    generator = main.LiteratureReviewGenerator(project_name="demo", pdf_folder=str(tmp_path))
    generator.logger = _DummyLogger()  # type: ignore[assignment]
    generator.config = config
    generator.bind_job_workspace(
        workspace=workspace,
        artifact_registry=registry,
        compat_config=compat_view,
        fingerprint_bundle={"request": "demo"},
        resume_state_report=_resume_report(workspace),
    )

    snapshot = Stage1ProgressSnapshot(
        artifact_type="stage1_progress_snapshot",
        artifact_version="v1",
        created_from_job_id=workspace.job_id,
        created_at="2026-04-06T00:00:00Z",
        project_name="demo",
        job_id=workspace.job_id,
        summary_file=workspace.artifact_path("demo_summaries.json"),
        summary_count=2,
        processed_papers=["paper-a"],
        failed_papers=["paper-b"],
        fingerprint_bundle={"request": "demo"},
        checkpoint_file=workspace.checkpoint_path("demo_checkpoint.json"),
    )
    snapshot_path = workspace.artifact_path("stage1_progress_snapshot.json")
    write_stage1_progress_snapshot(snapshot_path, snapshot)

    assert generator._restore_stage1_progress_from_snapshot(snapshot_path) is True
    assert generator._checkpoint_processed_papers == {"paper-a"}
    assert generator._checkpoint_failed_papers == {"paper-b"}
    assert generator.processed_count.value == 1
    assert generator.failed_count.value == 1


def test_rebuild_stage1_progress_from_loaded_summaries_marks_processed_papers(tmp_path) -> None:
    workspace = JobWorkspace.create(str(tmp_path / "output"), "demo")
    registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    config = ConfigDict({"Paths": {"output_path": str(tmp_path / "output")}, "Validation": {"stage1_enabled": "false", "stage2_enabled": "false"}})
    compat_view = CompatConfigView.from_config(config)

    generator = main.LiteratureReviewGenerator(project_name="demo", pdf_folder=str(tmp_path))
    generator.logger = _DummyLogger()  # type: ignore[assignment]
    generator.config = config
    generator.bind_job_workspace(
        workspace=workspace,
        artifact_registry=registry,
        compat_config=compat_view,
        fingerprint_bundle={"request": "demo"},
        resume_state_report=_resume_report(workspace),
    )
    generator.summaries = [
        {"status": "success", "paper_info": {"title": "Paper A"}},
        {"status": "failed", "paper_info": {"title": "Paper B"}},
    ]

    assert generator._rebuild_stage1_progress_from_loaded_summaries() is True
    assert generator._checkpoint_processed_papers == {"paper a_unknown_author"}
    assert generator._checkpoint_failed_papers == {"paper b_unknown_author"}
    assert generator.processed_count.value == 1
    assert generator.failed_count.value == 1


def test_merge_stage1_progress_from_loaded_summaries_clears_stale_failures(tmp_path) -> None:
    generator = main.LiteratureReviewGenerator(project_name="demo", pdf_folder=str(tmp_path))
    generator.logger = _DummyLogger()  # type: ignore[assignment]
    generator._checkpoint_processed_papers = {"paper a_unknown_author"}
    generator._checkpoint_failed_papers = {
        "paper b_unknown_author",
        "paper c_unknown_author",
    }
    generator.summaries = [
        {"status": "success", "paper_info": {"title": "Paper B"}},
        {"status": "failed", "paper_info": {"title": "Paper D"}},
    ]

    assert generator._merge_stage1_progress_from_loaded_summaries() is True
    assert generator._checkpoint_processed_papers == {
        "paper a_unknown_author",
        "paper b_unknown_author",
    }
    assert generator._checkpoint_failed_papers == {
        "paper c_unknown_author",
        "paper d_unknown_author",
    }
    assert generator.processed_count.value == 2
    assert generator.failed_count.value == 2
