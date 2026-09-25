from __future__ import annotations

import configparser
from pathlib import Path

import pytest

from runtime.control_plane import ControlPlaneError, ReviewControlPlane
from runtime.cancellation import CancellationRequestStore
from runtime.job_spec import RuntimeJobSpec, RuntimeSourceSpec, save_runtime_job_spec
from runtime.orchestrator import AgentRuntimeBridge
from runtime.pause_state import PauseStateStore
from runtime.runner import AgentRuntimeRunner
from tests.test_current_runtime_full_e2e import _test_config, _write_pdf


def _local_config(tmp_path: Path) -> Path:
    config_path = _test_config(tmp_path)
    parser = configparser.ConfigParser()
    parser.read(config_path, encoding="utf-8")
    for section in (
        "Primary_Reader_API",
        "Backup_Reader_API",
        "Writer_API",
        "Outline_API",
        "Free_Mode_API",
        "Validator_API",
    ):
        parser[section]["api_key"] = "test-key"
        parser[section]["api_base"] = "http://127.0.0.1:18080/v1"
    parser["Preprocess"]["enabled"] = "false"
    parser["Preprocess"]["parser_mode"] = "local"
    parser["Preprocess"]["primary_parser"] = "local"
    with config_path.open("w", encoding="utf-8") as handle:
        parser.write(handle)
    return config_path


def test_public_resume_rejection_keeps_job_paused(tmp_path: Path) -> None:
    papers = tmp_path / "papers"
    papers.mkdir()
    _write_pdf(papers / "paper.pdf", "Paper", "A bounded finding.")
    config_path = _local_config(tmp_path)
    workspace_path = tmp_path / "workspace"
    spec = RuntimeJobSpec(
        project_name="pause-control",
        source=RuntimeSourceSpec(mode="direct", pdf_folder=str(papers)),
        job_id="pause-control-job",
        config=str(config_path),
        action="run_all",
        queue_file=str(tmp_path / "queue.json"),
        workspace_path=str(workspace_path),
    )

    session = AgentRuntimeBridge(spec).bootstrap(
        resume_requested=False,
        claim_latest_pointer=False,
        publish_running_state=False,
    )
    workspace = session.context.workspace
    # Deliberately persist the raw spec instead of the normalized lifecycle
    # payload so resume identity preflight has a deterministic rejection.
    save_runtime_job_spec(workspace.artifact_path("runtime_job_spec_v1.json"), spec)

    control = ReviewControlPlane(repo_root=Path(__file__).resolve().parents[1])
    paused = control.pause(
        workspace=workspace.root_dir,
        requested_by="tests.public_pause_resume",
        reason="pause before identity rejection",
    )
    assert paused["status"] == "paused"

    opened_workspace, opened_registry = AgentRuntimeRunner._open_workspace(workspace.root_dir)
    state = PauseStateStore(opened_workspace, opened_registry).read()
    assert state is not None and state.paused
    cancellation = CancellationRequestStore(opened_workspace, opened_registry)
    cancellation.request(requested_by="tests.public_pause_resume", reason="keep until valid resume")

    with pytest.raises(ControlPlaneError, match="resume identity preflight"):
        control.resume(workspace=workspace.root_dir)

    reopened_workspace, reopened_registry = AgentRuntimeRunner._open_workspace(workspace.root_dir)
    state_after = PauseStateStore(reopened_workspace, reopened_registry).read()
    assert state_after is not None and state_after.paused
    assert CancellationRequestStore(reopened_workspace, reopened_registry).is_requested()
