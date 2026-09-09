from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path

import pytest

from runtime.release_acceptance import (
    ParentAcceptanceResultV2,
    ReleaseAcceptancePlanV2,
    ReleaseAcceptanceSpec,
    ReleaseAcceptanceSpecError,
    ScenarioExecutionReceiptV1,
    ProcessInterruptionEventV1,
)
from runtime.provider_runtime import (
    AcceptanceExecutionContextV1,
    ProviderAggregateBudgetV1,
    ProviderBudgetController,
    bind_acceptance_execution_context,
    acceptance_context_environment,
)


def _child(gate: str, runtime_spec: str) -> dict[str, object]:
    return {
        "scenario_id": gate,
        "gate": gate,
        "runtime_spec": runtime_spec,
        "workspace": f"workspace-{gate}",
        "execution_mode": "runtime",
        "budget_domain": "live",
        "prerequisites": [],
    }


def _plan_payload() -> dict[str, object]:
    return {
        "schema_version": "release-acceptance-plan-v2",
        "parent_run_id": "parent-acceptance-1",
        "budget": {
            "max_provider_calls_total": 12,
            "max_output_tokens_total": 1000,
            "max_retry_attempts_total": 1,
            "max_wall_seconds": 900,
        },
        "scenarios": {
            "C": _child("C", "c-runtime.json"),
            "D": _child("D", "d-runtime.json"),
            "Q": _child("Q", "q-runtime.json"),
        },
    }


def test_parent_plan_requires_independent_child_specs_for_incompatible_gates() -> None:
    plan = ReleaseAcceptancePlanV2.from_mapping(_plan_payload())

    assert plan.parent_run_id == "parent-acceptance-1"
    assert plan.child("C").runtime_spec != plan.child("D").runtime_spec
    assert plan.child("D").runtime_spec != plan.child("Q").runtime_spec

    with pytest.raises(ReleaseAcceptanceSpecError, match="independent child"):
        ReleaseAcceptanceSpec.from_mapping(
            {
                "runtime_spec": "one-runtime.json",
                "gates": ["C", "D", "Q"],
            }
        )


def _receipt_payload(**overrides: object) -> dict[str, object]:
    started = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    payload: dict[str, object] = {
        "artifact_type": "scenario_execution_receipt",
        "artifact_version": "v1",
        "schema_version": "scenario-execution-receipt-v1",
        "parent_acceptance_run_id": "parent-acceptance-1",
        "scenario_id": "C",
        "gate": "C",
        "final_executable_sha": "a" * 40,
        "plan_sha256": "b" * 64,
        "runtime_spec_sha256": "c" * 64,
        "input_identity_sha256": "d" * 64,
        "workspace_identity_sha256": "e" * 64,
        "executor_pid": 123,
        "executor_process_creation_identity": "123:456.0",
        "executor_host_id": "host-1",
        "started_at": started,
        "completed_at": started,
        "action_type": "one-paper-runtime",
        "workspace": "workspace-C",
        "job_id": "job-C",
        "attempt_id": "attempt-C",
        "budget_domain": "live",
        "status": "PASSED",
        "exit_status": 0,
        "produced_evidence_refs": [],
    }
    payload.update(overrides)
    return payload


def test_scenario_receipt_rejects_foreign_parent_child_sha_and_budget_domain() -> None:
    receipt = ScenarioExecutionReceiptV1.from_mapping(_receipt_payload())

    assert receipt.parent_acceptance_run_id == "parent-acceptance-1"
    assert receipt.scenario_id == "C"
    assert receipt.status == "PASSED"

    for overrides, message in (
        ({"scenario_id": "D"}, "scenario"),
        ({"executor_pid": 0}, "executor"),
    ):
        with pytest.raises(ReleaseAcceptanceSpecError, match=message):
            ScenarioExecutionReceiptV1.from_mapping(_receipt_payload(**overrides))

    foreign_parent = ParentAcceptanceResultV2.from_child_results(
        parent_acceptance_run_id="parent-acceptance-1",
        final_executable_sha="a" * 40,
        child_results={
            "C": {
                "status": "PASS",
                "receipt": _receipt_payload(
                    parent_acceptance_run_id="other-parent",
                    final_executable_sha="f" * 40,
                ),
            }
        },
        required_scenarios=("C",),
    )
    assert foreign_parent.status == "NOT_VERIFIED"

    offline_live = ParentAcceptanceResultV2.from_child_results(
        parent_acceptance_run_id="parent-acceptance-1",
        final_executable_sha="a" * 40,
        child_results={
            "C": {
                "status": "PASS",
                "receipt": _receipt_payload(budget_domain="offline-k"),
            }
        },
        required_scenarios=("C",),
    )
    assert offline_live.status == "NOT_VERIFIED"


def test_parent_result_cannot_turn_offline_executor_receipt_into_live_ready() -> None:
    result = ParentAcceptanceResultV2.from_child_results(
        parent_acceptance_run_id="parent-acceptance-1",
        final_executable_sha="a" * 40,
        child_results={
            "K": {
                "status": "PASS_OFFLINE",
                "live_pass": False,
                "ready_to_merge": False,
                "receipt": _receipt_payload(
                    scenario_id="K",
                    gate="K",
                    action_type="offline-contention",
                    budget_domain="offline-k",
                    status="PASSED",
                ),
            }
        },
        required_scenarios=("K",),
    )

    assert result.status == "PASS_OFFLINE"
    assert result.live_pass is False
    assert result.ready_to_merge is False
    assert result.to_dict()["terminal_status"] != "READY_TO_MERGE"


def test_parent_acceptance_plan_persists_independent_blocked_children_without_owner_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from runtime.control_plane import ReviewControlPlane

    monkeypatch.delenv("AUTO_GENERATE_RUN_LIVE_ACCEPTANCE", raising=False)
    payload = _plan_payload()
    payload["state_path"] = "acceptance-state.json"
    plan_path = tmp_path / "acceptance-plan.json"
    plan_path.write_text(json.dumps(payload), encoding="utf-8")

    result = ReviewControlPlane(repo_root=Path.cwd()).acceptance_run(plan_path)

    assert result["status"] == "blocked"
    assert set(result["scenarios"]) == {"C", "D", "Q"}
    assert all(item["status"] == "BLOCKED" for item in result["scenarios"].values())
    assert result["parent_result"]["live_pass"] is False
    state = json.loads((tmp_path / "acceptance-state.json").read_text(encoding="utf-8"))
    assert state["plan_sha256"]
    assert set(state["child_states"]) == {"C", "D", "Q"}


def test_parent_acceptance_plan_dispatches_each_runtime_child_independently(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from runtime.control_plane import ReviewControlPlane

    monkeypatch.setenv("AUTO_GENERATE_RUN_LIVE_ACCEPTANCE", "1")
    payload = _plan_payload()
    payload["state_path"] = "acceptance-state.json"
    for gate in ("C", "D", "Q"):
        (tmp_path / f"{gate.lower()}-runtime.json").write_text(
            json.dumps(
                {
                    "project_name": f"acceptance-{gate.lower()}",
                    "job_id": f"job-{gate.lower()}",
                    "source": {"mode": "direct", "pdf_folder": "missing-papers"},
                }
            ),
            encoding="utf-8",
        )
    calls: list[str] = []
    control = ReviewControlPlane(repo_root=Path.cwd())

    def fake_run(runtime_spec: str | Path) -> dict[str, object]:
        calls.append(str(runtime_spec))
        gate = Path(runtime_spec).stem[0].upper()
        return {
            "status": "complete",
            "job_status": "completed",
            "completion_status": "complete",
            "success": True,
            "job_id": f"job-{gate.lower()}",
            "workspace_path": str(tmp_path / f"workspace-{gate.lower()}"),
        }

    monkeypatch.setattr(control, "run", fake_run)
    plan_path = tmp_path / "acceptance-plan.json"
    plan_path.write_text(json.dumps(payload), encoding="utf-8")

    result = control.acceptance_run(plan_path)

    assert result["status"] == "blocked"
    assert calls == [
        str(tmp_path / "c-runtime.json"),
        str(tmp_path / "d-runtime.json"),
        str(tmp_path / "q-runtime.json"),
    ]
    assert {gate: result["scenarios"][gate]["status"] for gate in ("C", "D", "Q")} == {
        "C": "NOT_VERIFIED",
        "D": "BLOCKED",
        "Q": "NOT_VERIFIED",
    }


def test_acceptance_context_child_environment_does_not_mutate_parent_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    keys = (
        "AUTO_GENERATE_ACCEPTANCE_BUDGET_JSON",
        "AUTO_GENERATE_ACCEPTANCE_BUDGET_STATE_PATH",
        "AUTO_GENERATE_ACCEPTANCE_RUN_ID",
        "AUTO_GENERATE_ACCEPTANCE_CONTEXT_JSON",
    )
    for key in keys:
        monkeypatch.delenv(key, raising=False)
    budget = ProviderAggregateBudgetV1(max_provider_calls_total=2)
    controller = ProviderBudgetController(budget)
    context = AcceptanceExecutionContextV1(
        acceptance_run_id="parent-env-test",
        final_executable_sha="a" * 40,
        absolute_deadline_epoch=controller.snapshot()["absolute_deadline_epoch"],
        provider_budget=budget,
        provider_budget_state_path=str(tmp_path / "budget.json"),
        evidence_root=str(tmp_path / "evidence"),
        process_event_log=str(tmp_path / "events.jsonl"),
        scenario_state_path=str(tmp_path / "state.json"),
        owner_authorized=True,
    )
    before = {key: os.environ.get(key) for key in keys}

    with bind_acceptance_execution_context(context, controller):
        assert {key: os.environ.get(key) for key in keys} == before
        child_env = acceptance_context_environment(context, base_environment={})

    assert {key: os.environ.get(key) for key in keys} == before
    assert json.loads(child_env["AUTO_GENERATE_ACCEPTANCE_CONTEXT_JSON"])["acceptance_run_id"] == "parent-env-test"
    assert child_env["AUTO_GENERATE_ACCEPTANCE_BUDGET_STATE_PATH"] == str(
        tmp_path / "budget.json"
    )


def test_stage1_snapshot_replaces_partial_target_atomically(tmp_path: Path) -> None:
    from preprocess.service import PreprocessResult
    from services.job_workspace import JobWorkspace
    from services.stage1_analysis_service import Stage1AnalysisService

    service = object.__new__(Stage1AnalysisService)
    service.workspace = JobWorkspace.create(str(tmp_path / "output"), "snapshot", "job")
    generation_dir = tmp_path / "cache" / "generation-1"
    generation_dir.mkdir(parents=True)
    paths: dict[str, Path] = {}
    for index, field_name in enumerate(
        (
            "markdown_path",
            "plain_text_path",
            "page_index_path",
            "chunks_path",
            "diagnostics_path",
            "ocr_diagnostics_path",
            "ocr_artifact_path",
            "structured_json_path",
            "manifest_path",
            "stage1_input_path",
            "stage1_input_manifest_path",
            "stage1_quality_report_path",
        )
    ):
        source = generation_dir / f"{field_name}-{index}.dat"
        source.write_bytes(f"complete-{field_name}".encode("utf-8"))
        paths[field_name] = source
    result = PreprocessResult(
        pdf_path="source.pdf",
        cache_dir=str(tmp_path / "cache"),
        **{key: str(value) for key, value in paths.items()},
        markdown_text="",
        plain_text="",
        stage1_input_text="",
        page_index=[],
        page_diagnostics=[],
        low_quality=False,
        scanned_like=False,
        used_ocr=False,
        extractor_used="fitz",
        chunk_count=0,
        local_rag_enabled=False,
        local_rag_built=False,
        local_rag_persist_dir="",
        layout_fidelity="page_text",
        conversion_used="native_pdf",
        mineru_attempted=False,
        mineru_succeeded=False,
        mineru_token_present=False,
        mineru_remote_requested=False,
        mineru_remote_enabled=False,
        mineru_base_url="",
        selected_text_source="plain_text",
        stage1_quality_level="good",
    )
    digest = hashlib.sha256(b"paper").hexdigest()[:24]
    relative_target = f"source_evidence/{digest}/generation-1/{paths['markdown_path'].name}"
    target = Path(service.workspace.artifact_path(relative_target))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"partial-copy")

    snapped = service._snapshot_preprocess_authority(result, paper_key="paper")

    assert Path(snapped.markdown_path).read_bytes() == paths["markdown_path"].read_bytes()
    assert target.read_bytes() == paths["markdown_path"].read_bytes()


def test_terminate_interruption_event_cannot_claim_a_graceful_exit() -> None:
    with pytest.raises(ReleaseAcceptanceSpecError, match="non-zero"):
        ProcessInterruptionEventV1.from_mapping(
            {
                "artifact_type": "process_interruption_event",
                "artifact_version": "v1",
                "schema_version": "process-interruption-event-v1",
                "event_id": "event-1",
                "acceptance_run_id": "run-1",
                "scenario_id": "E",
                "job_id": "job-1",
                "attempt_id": "attempt-1",
                "pid": 123,
                "process_creation_identity": "123:1.0",
                "started_at": "2026-01-01T00:00:00Z",
                "interrupted_at": "2026-01-01T00:00:01Z",
                "interruption_method": "terminate",
                "exit_code": 0,
                "last_durable_stage": "analyze",
            }
        )
