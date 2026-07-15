from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import threading

import pytest

from runtime.attempt_store import AttemptStore
from runtime.job_spec import RuntimeJobSpec, RuntimeSourceSpec
from runtime.runner import AgentRuntimeRunner, RuntimeRunnerError
from runtime.stage_contracts import StageArtifactRef, StageResult
from runtime.stage_terminal import StageTerminalStore
from services.job_workspace import atomic_write_json
from services.artifact_registry import ArtifactRegistry
from services.job_workspace import JobWorkspace
from tests.test_runtime_bridge_helpers import build_legacy_main, build_success_summary
from validation.run_result import (
    ClaimValidationResultV1,
    ValidationInputArtifactsV1,
    ValidationRunResultV1,
)


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

    def handle(stage_name, request):
        calls.append(stage_name)
        if stage_name == "stage1_analyze":
            item = request.source_bundle.paper_work_items[0]
            summary = build_success_summary(
                pdf_path,
                paper_key=item.canonical_paper_key,
            )
            summary["paper_info"]["source_paper_id"] = item.source_paper_id
            return {"summaries": [summary], "model_call_count": 1}
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


def test_two_new_runs_with_identical_inputs_create_distinct_jobs(tmp_path: Path) -> None:
    calls: list[str] = []
    spec = _spec(tmp_path, action="analyze")

    first = AgentRuntimeRunner(
        spec,
        legacy_main=build_legacy_main(),
        stage_handler=_handler(tmp_path, calls),
        origin_dir=tmp_path,
    ).run()
    second = AgentRuntimeRunner(
        spec,
        legacy_main=build_legacy_main(),
        stage_handler=_handler(tmp_path, calls),
        origin_dir=tmp_path,
    ).run()

    assert first.job_id != second.job_id
    assert first.workspace_path != second.workspace_path
    assert first.attempt_number == second.attempt_number == 1
    assert calls == ["stage1_analyze", "stage1_analyze"]


def test_new_run_rejects_an_existing_explicit_job_workspace(tmp_path: Path) -> None:
    calls: list[str] = []
    spec = _spec(tmp_path, action="analyze", job_id="fixed-job")
    first = AgentRuntimeRunner(
        spec,
        legacy_main=build_legacy_main(),
        stage_handler=_handler(tmp_path, calls),
        origin_dir=tmp_path,
    ).run()

    with pytest.raises(RuntimeRunnerError, match="workspace already exists.*use resume"):
        AgentRuntimeRunner(
            spec,
            legacy_main=build_legacy_main(),
            stage_handler=_handler(tmp_path, calls),
            origin_dir=tmp_path,
        ).run()

    workspace = JobWorkspace.from_workspace_path(
        first.workspace_path,
        "runner-demo",
        first.job_id,
    )
    history = AttemptStore(
        workspace,
        ArtifactRegistry(workspace.paths.registry_path, first.job_id),
    ).load_history()
    assert [item.status for item in history] == ["pending", "running", "succeeded"]
    assert calls == ["stage1_analyze"]


@pytest.mark.parametrize(
    "failure_kind",
    [
        "empty",
        "missing-contract",
        "wrong-identity",
        "empty-ai-summary",
        "noncanonical-ai-summary",
    ],
)
def test_runner_rejects_incomplete_or_misidentified_stage1_results(
    tmp_path: Path,
    failure_kind: str,
) -> None:
    pdf_path = tmp_path / "papers" / "alpha.pdf"

    def malformed_stage1(stage_name, request):
        assert stage_name == "stage1_analyze"
        if failure_kind == "empty":
            summaries = []
        elif failure_kind == "missing-contract":
            summaries = [{}]
        elif failure_kind == "wrong-identity":
            item = request.source_bundle.paper_work_items[0]
            summary = build_success_summary(pdf_path, paper_key="foreign-paper")
            summary["paper_info"]["source_paper_id"] = item.source_paper_id
            summaries = [summary]
        else:
            item = request.source_bundle.paper_work_items[0]
            summary = build_success_summary(pdf_path, paper_key=item.canonical_paper_key)
            summary["paper_info"]["source_paper_id"] = item.source_paper_id
            summary["ai_summary"] = (
                {}
                if failure_kind == "empty-ai-summary"
                else {"summary": "Legacy, noncanonical summary."}
            )
            summaries = [summary]
        return {"summaries": summaries, "model_call_count": 1}

    result = AgentRuntimeRunner(
        _spec(tmp_path, action="analyze"),
        legacy_main=build_legacy_main(),
        stage_handler=malformed_stage1,
        origin_dir=tmp_path,
    ).run()

    assert result.job_status == "failed"
    assert result.canonical_ready is False
    assert result.completed_stages == ("source_intake",)
    assert "stage1" in result.message


def test_runner_rejects_registered_summary_with_invalid_payload_before_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def persist_invalid_summary(_bridge, session, *_args, **_kwargs):
        path = session.context.workspace.artifact_path("invalid_summary.json")
        atomic_write_json(path, [{}])
        record = session.context.registry.register_file(
            artifact_role="summary",
            artifact_type="summary_file",
            artifact_version="v1",
            path=path,
            producer="tests.test_runtime_runner",
            artifact_id="invalid_summary",
        )
        return StageResult(
            stage_name="stage1_analyze",
            success=True,
            artifacts=[
                StageArtifactRef(
                    artifact_role=record.artifact_role,
                    artifact_type=record.artifact_type,
                    artifact_version=record.artifact_version,
                    path=record.path,
                    artifact_id=record.artifact_id,
                )
            ],
        )

    monkeypatch.setattr(
        "runtime.orchestrator.AgentRuntimeBridge.persist_stage1_results",
        persist_invalid_summary,
    )
    result = AgentRuntimeRunner(
        _spec(tmp_path, action="analyze"),
        legacy_main=build_legacy_main(),
        stage_handler=_handler(tmp_path, []),
        origin_dir=tmp_path,
    ).run()

    assert result.job_status == "failed"
    assert result.completed_stages == ("source_intake",)
    workspace = JobWorkspace.from_workspace_path(
        result.workspace_path,
        "runner-demo",
        result.job_id,
    )
    terminals = StageTerminalStore(
        workspace,
        ArtifactRegistry(workspace.paths.registry_path, result.job_id),
    ).load_records()
    assert not any(
        terminal.stage_name == "analyze" and terminal.status == "succeeded"
        for terminal, _path in terminals
    )


def test_runner_deduplicates_explicit_requested_stages(tmp_path: Path) -> None:
    calls: list[str] = []
    spec = replace(
        _spec(tmp_path, action="analyze"),
        metadata={"requested_stages": ["analyze", "analyze"]},
    )

    result = AgentRuntimeRunner(
        spec,
        legacy_main=build_legacy_main(),
        stage_handler=_handler(tmp_path, calls),
        origin_dir=tmp_path,
    ).run()

    assert result.job_status == "completed"
    assert calls == ["stage1_analyze"]
    persisted = json.loads(
        (Path(result.workspace_path) / "artifacts/runtime_job_spec_v1.json").read_text(
            encoding="utf-8"
        )
    )
    assert persisted["metadata"]["requested_stages"] == ["analyze"]


def test_resume_rejects_changed_source_fingerprint(tmp_path: Path) -> None:
    calls: list[str] = []
    runner = AgentRuntimeRunner(
        _spec(tmp_path, action="analyze"),
        legacy_main=build_legacy_main(),
        stage_handler=_handler(tmp_path, calls),
        origin_dir=tmp_path,
    )
    first = runner.run()
    original_outcome = Path(first.job_outcome_path).read_bytes()
    (tmp_path / "papers" / "alpha.pdf").write_bytes(b"%PDF-1.4\n%changed\n")

    with pytest.raises(RuntimeRunnerError, match="resume fingerprint"):
        AgentRuntimeRunner(
            replace(runner.job_spec, job_id=first.job_id),
            legacy_main=build_legacy_main(),
            stage_handler=_handler(tmp_path, calls),
        ).resume()

    assert calls == ["stage1_analyze"]
    assert Path(first.job_outcome_path).read_bytes() == original_outcome


def test_resume_rejects_tampered_persisted_spec_before_starting_attempt(tmp_path: Path) -> None:
    runner = AgentRuntimeRunner(
        _spec(tmp_path, action="analyze"),
        legacy_main=build_legacy_main(),
        stage_handler=_handler(tmp_path, []),
        origin_dir=tmp_path,
    )
    first = runner.run()
    workspace = JobWorkspace.from_workspace_path(first.workspace_path, "runner-demo", first.job_id)
    registry = ArtifactRegistry(workspace.paths.registry_path, first.job_id)
    attempt_store = AttemptStore(workspace, registry)
    history_before = attempt_store.load_history()
    immutable_paths = (
        Path(first.job_outcome_path),
        Path(workspace.latest_pointer_path()),
        Path(workspace.paths.registry_path),
        Path(workspace.artifact_path("resume_state_report.json")),
    )
    spec_path = Path(workspace.artifact_path("runtime_job_spec_v1.json"))
    persisted = json.loads(spec_path.read_text(encoding="utf-8"))
    persisted["action"] = "run_all"
    spec_path.write_text(json.dumps(persisted), encoding="utf-8")
    bytes_before = {path: path.read_bytes() for path in immutable_paths}

    with pytest.raises(RuntimeRunnerError, match="persisted runtime job spec"):
        AgentRuntimeRunner(
            replace(runner.job_spec, job_id=first.job_id),
            legacy_main=build_legacy_main(),
            stage_handler=_handler(tmp_path, []),
        ).resume()

    registry.reload()
    history_after = AttemptStore(workspace, registry).load_history()
    assert history_after == history_before
    assert {path: path.read_bytes() for path in immutable_paths} == bytes_before


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


def test_finalize_fault_preserves_original_error_after_terminal_attempt(tmp_path: Path) -> None:
    def crash(point: str, _context) -> None:
        if point == "after_stage_terminal_before_job_outcome":
            raise RuntimeError("finalize crash")

    runner = AgentRuntimeRunner(
        _spec(tmp_path, action="analyze"),
        legacy_main=build_legacy_main(),
        stage_handler=_handler(tmp_path, []),
        origin_dir=tmp_path,
        fault_injector=crash,
    )

    with pytest.raises(RuntimeError, match="finalize crash"):
        runner.run()

    workspace_path = next((tmp_path / "output").glob("runner-demo__*"))
    job_id = workspace_path.name.split("__", 1)[1]
    workspace = JobWorkspace.from_workspace_path(str(workspace_path), "runner-demo", job_id)
    history = AttemptStore(
        workspace,
        ArtifactRegistry(workspace.paths.registry_path, job_id),
    ).load_history()
    assert [item.status for item in history] == ["pending", "running", "succeeded"]


def test_terminal_attempt_registry_failure_preserves_commit_error_and_orphan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_register = ArtifactRegistry.register_file

    def fail_terminal_registration(self, *args, **kwargs):
        metadata = kwargs.get("metadata") or {}
        if (
            kwargs.get("artifact_type") == "job_attempt"
            and metadata.get("attempt_status") == "succeeded"
        ):
            raise RuntimeError("terminal attempt registry commit failed")
        return original_register(self, *args, **kwargs)

    monkeypatch.setattr(ArtifactRegistry, "register_file", fail_terminal_registration)
    runner = AgentRuntimeRunner(
        _spec(tmp_path, action="analyze"),
        legacy_main=build_legacy_main(),
        stage_handler=_handler(tmp_path, []),
        origin_dir=tmp_path,
    )

    with pytest.raises(RuntimeError, match="terminal attempt registry commit failed"):
        runner.run()

    workspace_path = next((tmp_path / "output").glob("runner-demo__*"))
    job_id = workspace_path.name.split("__", 1)[1]
    workspace = JobWorkspace.from_workspace_path(str(workspace_path), "runner-demo", job_id)
    registry = ArtifactRegistry(workspace.paths.registry_path, job_id)
    history = AttemptStore(workspace, registry).load_history()
    assert [item.status for item in history] == ["pending", "running", "succeeded"]
    terminal = history[-1]
    assert registry.get(f"job-attempt:{terminal.attempt_id}:000003:succeeded") is None

    monkeypatch.setattr(ArtifactRegistry, "register_file", original_register)
    AgentRuntimeRunner.reconcile(workspace.root_dir)
    registry.reload()
    assert registry.get(f"job-attempt:{terminal.attempt_id}:000003:succeeded") is not None


def test_concurrent_resume_is_rejected_without_mutating_running_attempt_head(
    tmp_path: Path,
) -> None:
    base = AgentRuntimeRunner(
        _spec(tmp_path, action="analyze"),
        legacy_main=build_legacy_main(),
        stage_handler=_handler(tmp_path, []),
        origin_dir=tmp_path,
    )
    first = base.run()
    summary_path = Path(first.workspace_path) / "artifacts" / "runner-demo_summaries.json"
    summaries = json.loads(summary_path.read_text(encoding="utf-8"))
    summaries[0]["text_length"] += 1
    summary_path.write_text(json.dumps(summaries), encoding="utf-8")

    entered = threading.Event()
    release = threading.Event()
    calls: list[str] = []
    delegate = _handler(tmp_path, calls)

    def blocking_handler(stage_name, request):
        if stage_name == "stage1_analyze":
            entered.set()
            if not release.wait(timeout=20):
                raise RuntimeError("test timed out waiting to release resume")
        return delegate(stage_name, request)

    resume_spec = replace(base.job_spec, job_id=first.job_id)
    holder: dict[str, object] = {}

    def run_resume() -> None:
        try:
            holder["result"] = AgentRuntimeRunner(
                resume_spec,
                legacy_main=build_legacy_main(),
                stage_handler=blocking_handler,
            ).resume()
        except BaseException as exc:  # pragma: no cover - asserted below.
            holder["error"] = exc

    thread = threading.Thread(target=run_resume, daemon=True)
    thread.start()
    assert entered.wait(timeout=20)

    running = AgentRuntimeRunner.status(first.workspace_path)
    assert running.job_status == "running"
    assert running.attempt_number == 2
    assert running.resumed_from_attempt == 1
    outcome_path = Path(first.job_outcome_path)
    pointer_path = Path(first.workspace_path).parent / "runner-demo" / "_latest_job.json"
    durable_before = (outcome_path.read_bytes(), pointer_path.read_bytes())

    with pytest.raises(RuntimeRunnerError, match="another runtime attempt is already active"):
        AgentRuntimeRunner(
            resume_spec,
            legacy_main=build_legacy_main(),
            stage_handler=blocking_handler,
        ).resume()

    assert (outcome_path.read_bytes(), pointer_path.read_bytes()) == durable_before
    release.set()
    thread.join(timeout=20)
    assert not thread.is_alive()
    assert "error" not in holder
    resumed = holder["result"]
    assert getattr(resumed, "job_status") == "completed"
    assert getattr(resumed, "attempt_number") == 2


def test_runner_maps_keyboard_interrupt_to_cancelled_terminal_state(tmp_path: Path) -> None:
    def interrupt(stage_name, request):
        del stage_name, request
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


def test_runner_persists_system_exit_terminal_state_before_reraising(tmp_path: Path) -> None:
    exit_error = SystemExit(17)

    def exit_stage(stage_name, request):
        del stage_name, request
        raise exit_error

    runner = AgentRuntimeRunner(
        _spec(tmp_path, action="analyze"),
        legacy_main=build_legacy_main(),
        stage_handler=exit_stage,
        origin_dir=tmp_path,
    )

    with pytest.raises(SystemExit) as raised:
        runner.run()

    assert raised.value is exit_error
    workspace_path = next((tmp_path / "output").glob("runner-demo__*"))
    job_id = workspace_path.name.split("__", 1)[1]
    workspace = JobWorkspace.from_workspace_path(str(workspace_path), "runner-demo", job_id)
    registry = ArtifactRegistry(workspace.paths.registry_path, job_id)
    history = AttemptStore(workspace, registry).load_history()
    assert [item.status for item in history] == ["pending", "running", "cancelled"]
    assert history[-1].terminal_reason == "17"

    outcome = json.loads(
        Path(workspace.artifact_path("job_outcome_v1.json")).read_text(encoding="utf-8")
    )
    assert outcome["job_status"] == "cancelled"
    assert outcome["canonical_ready"] is False
    assert outcome["failed_stage"] == "analyze"

    latest = json.loads(Path(workspace.latest_pointer_path()).read_text(encoding="utf-8"))
    assert latest["status"] == "cancelled"


def test_runner_rejects_unregistered_outline_dependency(tmp_path: Path) -> None:
    lure = tmp_path / "unregistered-outline.md"
    lure.write_text("# lure", encoding="utf-8")
    spec = replace(
        _spec(tmp_path, action="generate_review"),
        summary_sources=(str(tmp_path / "summary.json"),),
        metadata={"requested_stages": ["review"]},
    )
    (tmp_path / "summary.json").write_text("[]", encoding="utf-8")

    def review_only(stage_name, request):
        del stage_name, request
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
    assert reconciled.pointer_repaired is False
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


@pytest.mark.parametrize(
    ("disposition", "stage_success", "validation_required", "require_clean", "expected_status", "expected_ready"),
    [
        ("clean", True, True, True, "completed", True),
        ("findings", True, True, False, "completed", True),
        ("findings", True, True, True, "completed", False),
        ("needs_review", True, True, False, "completed", False),
        ("unvalidated", False, False, False, "completed", True),
        ("unvalidated", False, True, True, "failed", False),
    ],
)
def test_runner_applies_persisted_validation_readiness_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    disposition: str,
    stage_success: bool,
    validation_required: bool,
    require_clean: bool,
    expected_status: str,
    expected_ready: bool,
) -> None:
    def fake_validation(_bridge, session, *, attempt_id: str = "", **_kwargs):
        artifacts = []
        if stage_success:
            path = session.context.workspace.artifact_path("validation-fixture.json")
            status_by_disposition = {
                "findings": "evidence_gap",
                "needs_review": "wrong_source",
            }
            claim_results = []
            if disposition in status_by_disposition:
                claim_results.append(
                    ClaimValidationResultV1.from_validation_result(
                        {
                            "citation_id": "fixture-citation",
                            "claim_text": "Fixture claim",
                            "evidence_status": status_by_disposition[disposition],
                        }
                    )
                )
            validation_result = ValidationRunResultV1.create(
                job_id=session.context.workspace.job_id,
                attempt_id=attempt_id,
                execution_status="succeeded",
                claim_results=claim_results,
                input_artifacts=ValidationInputArtifactsV1(
                    review_draft_id="fixture-review-draft",
                    review_draft_hash="a" * 64,
                    citation_manifest_id="fixture-citation-manifest",
                    citation_manifest_hash="b" * 64,
                    evidence_manifest_ids=("fixture-evidence",) if claim_results else (),
                    evidence_manifest_hashes=("c" * 64,) if claim_results else (),
                ),
                review_has_citations=bool(claim_results),
                evidence_complete=True,
            )
            assert validation_result.validation_disposition.value == disposition
            atomic_write_json(path, validation_result.to_dict())
            record = session.context.registry.register_file(
                artifact_role="runtime_fixture",
                artifact_type="validation_run_result",
                artifact_version="v1",
                path=path,
                producer="tests.test_runtime_runner",
                artifact_id="validation_fixture",
            )
            artifacts = [
                StageArtifactRef(
                    artifact_role=record.artifact_role,
                    artifact_type=record.artifact_type,
                    artifact_version=record.artifact_version,
                    path=record.path,
                    artifact_id=record.artifact_id,
                )
            ]
        return StageResult(
            stage_name="stage4_validate",
            success=stage_success,
            artifacts=artifacts,
            metadata={
                "execution_status": "succeeded" if stage_success else "skipped",
                "validation_disposition": disposition,
            },
        )

    monkeypatch.setattr(
        "runtime.orchestrator.AgentRuntimeBridge.run_validation",
        fake_validation,
    )
    spec = replace(
        _spec(tmp_path),
        metadata={
            "requested_stages": ["validate"],
            "validation_required": validation_required,
            "require_clean_validation": require_clean,
            "allow_unvalidated_when_validation_optional": not validation_required,
        },
    )

    result = AgentRuntimeRunner(
        spec,
        legacy_main=build_legacy_main(),
        origin_dir=tmp_path,
    ).run()

    assert result.job_status == expected_status
    assert result.job_disposition == (disposition if expected_status == "completed" else "unvalidated")
    assert result.canonical_ready is expected_ready
    outcome = json.loads(Path(result.job_outcome_path).read_text(encoding="utf-8"))
    assert outcome["required_stages"] == (
        ["source_intake", "validate"]
        if validation_required
        else ["source_intake"]
    )
    assert outcome["readiness_policy_snapshot"]["validation_required"] is validation_required
    assert outcome["readiness_policy_snapshot"]["require_clean_validation"] is require_clean


def test_resume_restores_clean_validation_result_without_rerunning_validator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    validation_calls = 0

    def fake_validation(_bridge, session, *, attempt_id: str = "", **_kwargs):
        nonlocal validation_calls
        validation_calls += 1
        validation_result = ValidationRunResultV1.create(
            job_id=session.context.workspace.job_id,
            attempt_id=attempt_id,
            execution_status="succeeded",
            claim_results=(),
            input_artifacts=ValidationInputArtifactsV1(
                review_draft_id="fixture-review-draft",
                review_draft_hash="a" * 64,
                citation_manifest_id="fixture-citation-manifest",
                citation_manifest_hash="b" * 64,
            ),
            review_has_citations=False,
            evidence_complete=True,
        )
        path = Path(session.context.workspace.artifact_path("validation-fixture.json"))
        atomic_write_json(str(path), validation_result.to_dict())
        record = session.context.registry.register_file(
            artifact_role="validation",
            artifact_type="validation_run_result",
            artifact_version="v1",
            path=path,
            producer="tests.test_runtime_runner",
            artifact_id=validation_result.validation_run_id,
        )
        return StageResult(
            stage_name="stage4_validate",
            success=True,
            artifacts=[
                StageArtifactRef(
                    artifact_role=record.artifact_role,
                    artifact_type=record.artifact_type,
                    artifact_version=record.artifact_version,
                    path=record.path,
                    artifact_id=record.artifact_id,
                )
            ],
            metadata={
                "execution_status": validation_result.execution_status.value,
                "validation_disposition": validation_result.validation_disposition.value,
            },
        )

    monkeypatch.setattr(
        "runtime.orchestrator.AgentRuntimeBridge.run_validation",
        fake_validation,
    )
    spec = replace(
        _spec(tmp_path),
        metadata={
            "requested_stages": ["validate"],
            "validation_required": True,
            "require_clean_validation": True,
        },
    )
    runner = AgentRuntimeRunner(
        spec,
        legacy_main=build_legacy_main(),
        origin_dir=tmp_path,
    )

    first = runner.run()
    resumed = AgentRuntimeRunner(
        replace(runner.job_spec, job_id=first.job_id),
        legacy_main=build_legacy_main(),
    ).resume()

    assert validation_calls == 1
    assert first.job_disposition == resumed.job_disposition == "clean"
    assert first.canonical_ready is resumed.canonical_ready is True
    assert resumed.completed_stages == ("source_intake", "validate")


def test_runner_explicit_empty_stage_set_is_source_only(tmp_path: Path) -> None:
    def fail_if_called(stage_name, request):
        del stage_name, request
        raise AssertionError("no stage expected")

    spec = replace(_spec(tmp_path), metadata={"requested_stages": []})
    result = AgentRuntimeRunner(
        spec,
        legacy_main=build_legacy_main(),
        stage_handler=fail_if_called,
        origin_dir=tmp_path,
    ).run()

    assert result.job_status == "completed"
    assert result.canonical_ready is True
    payload = json.loads(Path(result.job_outcome_path).read_text(encoding="utf-8"))
    assert payload["required_stages"] == ["source_intake"]


def test_status_rejects_outcome_from_another_job(tmp_path: Path) -> None:
    result = AgentRuntimeRunner(
        _spec(tmp_path, action="analyze"),
        legacy_main=build_legacy_main(),
        stage_handler=_handler(tmp_path, []),
        origin_dir=tmp_path,
    ).run()
    payload = json.loads(Path(result.job_outcome_path).read_text(encoding="utf-8"))
    payload["job_id"] = "foreign-job"
    Path(result.job_outcome_path).write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(RuntimeRunnerError, match="another workspace"):
        AgentRuntimeRunner.status(result.workspace_path)


def test_reconcile_repairs_owned_pointer_identity_once(tmp_path: Path) -> None:
    result = AgentRuntimeRunner(
        _spec(tmp_path, action="analyze"),
        legacy_main=build_legacy_main(),
        stage_handler=_handler(tmp_path, []),
        origin_dir=tmp_path,
    ).run()
    workspace = JobWorkspace.from_workspace_path(
        result.workspace_path,
        "runner-demo",
        result.job_id,
    )
    pointer_path = Path(workspace.latest_pointer_path())
    pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    pointer.update(
        {
            "project_name": "foreign-project",
            "workspace_path": str(tmp_path / "foreign-workspace"),
            "artifact_registry_path": str(tmp_path / "foreign-registry.json"),
            "fingerprint_bundle": {"corrupted": True},
            "status": "running",
            "resume_state": "corrupted",
        }
    )
    atomic_write_json(str(pointer_path), pointer)

    first = AgentRuntimeRunner.reconcile(result.workspace_path)

    assert first.pointer_repaired is True
    assert any(issue.code == "latest_pointer_identity_mismatch" for issue in first.issues)
    repaired = json.loads(pointer_path.read_text(encoding="utf-8"))
    resume_report = json.loads(
        Path(workspace.artifact_path("resume_state_report.json")).read_text(encoding="utf-8")
    )
    assert repaired["project_name"] == workspace.project_name
    assert Path(repaired["workspace_path"]).resolve() == Path(workspace.root_dir).resolve()
    assert Path(repaired["artifact_registry_path"]).resolve() == Path(
        workspace.paths.registry_path
    ).resolve()
    assert repaired["fingerprint_bundle"] == resume_report["fingerprint_bundle"]
    assert repaired["status"] == result.job_status
    assert repaired["resume_state"] == resume_report["state"]

    second = AgentRuntimeRunner.reconcile(result.workspace_path)
    assert second.pointer_repaired is False
    assert second.issues == ()


@pytest.mark.parametrize("corruption", ["identity", "fingerprint"])
def test_reconcile_rejects_invalid_resume_report_as_pointer_source(
    tmp_path: Path,
    corruption: str,
) -> None:
    result = AgentRuntimeRunner(
        _spec(tmp_path, action="analyze"),
        legacy_main=build_legacy_main(),
        stage_handler=_handler(tmp_path, []),
        origin_dir=tmp_path,
    ).run()
    workspace = JobWorkspace.from_workspace_path(
        result.workspace_path,
        "runner-demo",
        result.job_id,
    )
    pointer_path = Path(workspace.latest_pointer_path())
    pointer_before = pointer_path.read_bytes()
    report_path = Path(workspace.artifact_path("resume_state_report.json"))
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if corruption == "identity":
        report["job_id"] = "foreign-job"
    else:
        report["fingerprint_bundle"]["combined_hash"] = "0" * 64
    atomic_write_json(str(report_path), report)

    reconciled = AgentRuntimeRunner.reconcile(result.workspace_path)

    assert reconciled.pointer_repaired is False
    assert any(
        issue.code == "invalid_resume_state_report_identity"
        for issue in reconciled.issues
    )
    assert pointer_path.read_bytes() == pointer_before
