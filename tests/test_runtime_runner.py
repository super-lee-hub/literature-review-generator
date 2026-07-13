from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path

import pytest

from runtime.attempt_store import AttemptStore
from runtime.job_spec import RuntimeJobSpec, RuntimeSourceSpec
from runtime.runner import AgentRuntimeRunner
from services.artifact_registry import ArtifactRegistry
from services.job_workspace import JobWorkspace
from tests.test_runtime_bridge_helpers import build_legacy_main, build_success_summary


def _spec(tmp_path: Path, *, action: str = "run_all", job_id: str = "") -> RuntimeJobSpec:
    pdf_dir = tmp_path / "papers"
    pdf_dir.mkdir(exist_ok=True)
    (pdf_dir / "alpha.pdf").write_bytes(b"%PDF-1.4\n%alpha\n")
    return RuntimeJobSpec(
        project_name="runner-demo",
        job_id=job_id,
        source=RuntimeSourceSpec(mode="direct", pdf_folder="papers"),
        config="config.ini",
        action=action,
        queue_file="output/_queue/queue.json",
    )


def _handler(tmp_path: Path, calls: list[str]):
    pdf_path = tmp_path / "papers" / "alpha.pdf"

    def handle(stage_name, _request):
        calls.append(stage_name)
        if stage_name == "stage1_analyze":
            return {"summaries": [build_success_summary(pdf_path)], "model_call_count": 1}
        if stage_name == "stage2_outline":
            return {"outline_text": "# Outline\n\n## 1. Findings", "model_call_count": 1}
        if stage_name == "stage3_review":
            return {
                "review_sections": [
                    {
                        "section_number": 1,
                        "section_title": "Findings",
                        "content": "Paper A reports a result. [[cite_ref:R001]]",
                    }
                ],
                "model_call_count": 1,
            }
        raise AssertionError(stage_name)

    return handle


def test_runner_executes_full_chain_and_resume_skips_durable_generation(tmp_path: Path) -> None:
    calls: list[str] = []
    runner = AgentRuntimeRunner(
        _spec(tmp_path),
        legacy_main=build_legacy_main(),
        stage_handler=_handler(tmp_path, calls),
        origin_dir=tmp_path,
    )

    first = runner.run()

    assert first.job_status == "completed"
    assert first.canonical_ready is True
    assert calls == ["stage1_analyze", "stage2_outline", "stage3_review"]
    assert AgentRuntimeRunner.status(first.workspace_path).job_id == first.job_id

    calls.clear()
    resumed = AgentRuntimeRunner(
        replace(runner.job_spec, job_id=first.job_id),
        legacy_main=build_legacy_main(),
        stage_handler=_handler(tmp_path, calls),
    ).resume()

    assert resumed.job_status == "completed"
    assert resumed.attempt_number == 2
    assert calls == []
    workspace = JobWorkspace.from_workspace_path(first.workspace_path, "runner-demo", first.job_id)
    history = AttemptStore(
        workspace,
        ArtifactRegistry(workspace.paths.registry_path, first.job_id),
    ).load_history()
    assert [item.status for item in history] == [
        "pending",
        "running",
        "succeeded",
        "pending",
        "running",
        "succeeded",
    ]


def test_runner_recovers_stale_running_attempt_after_crash_before_registry(tmp_path: Path) -> None:
    injected = False

    def crash(point, _context):
        nonlocal injected
        if point == "after_artifact_write_before_registry" and not injected:
            injected = True
            raise RuntimeError("injected crash")

    runner = AgentRuntimeRunner(
        _spec(tmp_path, action="analyze"),
        legacy_main=build_legacy_main(),
        stage_handler=_handler(tmp_path, []),
        origin_dir=tmp_path,
        fault_injector=crash,
    )
    with pytest.raises(RuntimeError, match="injected crash"):
        runner.run()

    output_dir = tmp_path / "output"
    workspace_path = next(output_dir.glob("runner-demo__*"))
    job_id = workspace_path.name.split("__", 1)[1]
    calls: list[str] = []
    result = AgentRuntimeRunner(
        replace(runner.job_spec, job_id=job_id),
        legacy_main=build_legacy_main(),
        stage_handler=_handler(tmp_path, calls),
    ).resume()

    assert result.job_status == "completed"
    assert result.attempt_number == 2
    assert calls == ["stage1_analyze"]
    workspace = JobWorkspace.from_workspace_path(str(workspace_path), "runner-demo", job_id)
    statuses = [
        item.status
        for item in AttemptStore(
            workspace,
            ArtifactRegistry(workspace.paths.registry_path, job_id),
        ).load_history()
    ]
    assert statuses[:5] == ["pending", "running", "interrupted", "pending", "running"]


def test_runner_maps_keyboard_interrupt_to_cancelled_terminal_state(tmp_path: Path) -> None:
    def interrupt(_stage_name, _request):
        raise KeyboardInterrupt()

    result = AgentRuntimeRunner(
        _spec(tmp_path, action="analyze"),
        legacy_main=build_legacy_main(),
        stage_handler=interrupt,
        origin_dir=tmp_path,
    ).run()

    assert result.job_status == "cancelled"
    assert result.canonical_ready is False
    assert result.failed_stage == "analyze"


def test_runner_rejects_unregistered_outline_dependency(tmp_path: Path) -> None:
    lure = tmp_path / "unregistered-outline.md"
    lure.write_text("# lure", encoding="utf-8")
    spec = replace(
        _spec(tmp_path, action="generate_review"),
        summary_sources=(str(tmp_path / "summary.json"),),
        metadata={"requested_stages": ["review"]},
    )
    (tmp_path / "summary.json").write_text("[]", encoding="utf-8")

    def review_only(_stage_name, _request):
        return {"outline_file": str(lure), "review_sections": []}

    result = AgentRuntimeRunner(
        spec,
        legacy_main=build_legacy_main(),
        stage_handler=review_only,
        origin_dir=tmp_path,
    ).run()

    assert result.job_status == "failed"
    assert "not a ready registered artifact" in result.message


def test_status_is_read_only_and_reconcile_has_no_provider_surface(tmp_path: Path) -> None:
    calls: list[str] = []
    result = AgentRuntimeRunner(
        _spec(tmp_path, action="analyze"),
        legacy_main=build_legacy_main(),
        stage_handler=_handler(tmp_path, calls),
        origin_dir=tmp_path,
    ).run()
    registry_path = Path(result.workspace_path) / "artifact_registry.json"
    before = registry_path.read_bytes()

    status = AgentRuntimeRunner.status(result.workspace_path)
    reconciled = AgentRuntimeRunner.reconcile(result.workspace_path)

    assert status.job_status == "completed"
    assert "analyze" in reconciled.completed_stages
    assert calls == ["stage1_analyze"]
    assert registry_path.read_bytes() == before


def test_runtime_cli_status_and_reconcile_are_public_provider_free_commands(
    tmp_path: Path,
    capsys,
) -> None:
    from runtime.cli import main as runtime_cli

    result = AgentRuntimeRunner(
        _spec(tmp_path, action="analyze"),
        legacy_main=build_legacy_main(),
        stage_handler=_handler(tmp_path, []),
        origin_dir=tmp_path,
    ).run()
    capsys.readouterr()

    assert runtime_cli(["status", result.workspace_path]) == 0
    status_payload = json.loads(capsys.readouterr().out)
    assert status_payload["job_status"] == "completed"

    assert runtime_cli(["reconcile", result.workspace_path]) == 0
    reconcile_payload = json.loads(capsys.readouterr().out)
    assert "analyze" in reconcile_payload["completed_stages"]
