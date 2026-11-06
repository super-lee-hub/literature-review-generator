from __future__ import annotations

from pathlib import Path
from typing import Callable

import fitz
import pytest

from runtime.cancellation import CancellationRequestStore
from runtime.job_spec import RuntimeJobSpec, RuntimeSourceSpec
from runtime.provider_runtime import (
    ProviderAggregateBudgetV1,
    ProviderBudgetController,
    ProviderBudgetExceeded,
    ProviderCancellationRequested,
    ProviderRuntime,
    ProviderRuntimeContractError,
    ProviderRuntimeLedger,
    RuntimeControlIdentityV1,
    bind_runtime_control_context,
)
from runtime.runner import AgentRuntimeRunner
from services.artifact_registry import ArtifactRegistry
from services.job_workspace import JobWorkspace
from tests.test_runtime_bridge_helpers import current_config


StageAction = Callable[[str, object], None]


def _write_one_pdf(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 72), "A small source for the Runner cancellation boundary test.")
    document.save(root / "source.pdf")
    document.close()


def _run_tiny_runner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage_action: StageAction,
):
    source_dir = tmp_path / "papers"
    _write_one_pdf(source_dir)
    config_path = current_config(tmp_path)
    queue_path = tmp_path / "output" / "_queue" / "queue.json"
    queue_path.parent.mkdir(parents=True)
    spec = RuntimeJobSpec(
        project_name="provider-cancel-status",
        job_id="provider-cancel-status-job",
        source=RuntimeSourceSpec(mode="direct", pdf_folder=str(source_dir)),
        action="analyze",
        config=str(config_path),
        queue_file=str(queue_path),
    )
    runner = AgentRuntimeRunner(spec)
    # Run admission planning is unrelated to this status-mapping regression.
    monkeypatch.setattr(runner, "_admit_run_provider_envelope", lambda *_args: None)

    def forbidden_transport(*_args, **_kwargs):
        raise AssertionError("provider admission cancellation tests must not reach HTTP transport")

    monkeypatch.setattr(
        "ai_interface._call_ai_api_detailed_uninstrumented",
        forbidden_transport,
    )

    def execute_stage(_runner, stage: str, *, session, **_kwargs):
        stage_action(stage, session)
        raise AssertionError("stage action must either cancel or fail")

    monkeypatch.setattr(AgentRuntimeRunner, "_execute_stage", execute_stage)
    return runner.run()


def _counter_projection(snapshot: dict[str, object]) -> dict[str, object]:
    return {
        key: snapshot.get(key)
        for key in (
            "calls_used",
            "calls_reserved",
            "output_tokens_used",
            "output_tokens_reserved",
            "retry_attempts_used",
            "retry_attempts_reserved",
        )
    }


def test_runner_finalizes_provider_admission_cancellation_as_cancelled_without_charging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observation: dict[str, object] = {}

    def cancel_at_admission(stage: str, session) -> None:
        assert stage == "analyze"
        workspace = session.context.workspace
        registry = session.context.registry
        CancellationRequestStore(workspace, registry).request(
            requested_by="test",
            reason="cancel at provider admission boundary",
        )
        ledger = ProviderRuntimeLedger(
            Path(workspace.root_dir) / "logs" / "test_provider_receipts.jsonl"
        )
        provider = ProviderRuntime(test_only=True, ledger=ledger)
        controller = provider.aggregate_budget
        assert controller is not None
        before = _counter_projection(controller.snapshot())
        with pytest.raises(ProviderCancellationRequested) as blocked:
            provider.admit()
        after = _counter_projection(controller.snapshot())
        observation.update({
            "admission_error": str(blocked.value),
            "budget_before": before,
            "budget_after": after,
            "receipt_count": len(ledger.list_receipts()),
            "ledger_exists": ledger.path.exists(),
        })
        raise blocked.value

    result = _run_tiny_runner(tmp_path, monkeypatch, cancel_at_admission)

    assert result.job_status == "cancelled", (result.job_status, result.message, observation)
    assert result.failed_stage == "analyze"
    assert "CANCEL_REQUESTED" in str(observation.get("admission_error") or "")
    assert observation["budget_before"] == observation["budget_after"]
    assert observation["receipt_count"] == 0
    assert observation["ledger_exists"] is False


def test_runner_keeps_ordinary_provider_budget_exhaustion_failed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def ordinary_budget_exhaustion(stage: str, _session) -> None:
        assert stage == "analyze"
        raise ProviderBudgetExceeded("provider runtime call budget exhausted")

    result = _run_tiny_runner(tmp_path, monkeypatch, ordinary_budget_exhaustion)

    assert result.job_status == "failed"
    assert result.failed_stage == "analyze"
    assert "provider runtime call budget exhausted" in result.message


def test_runner_keeps_malformed_cancellation_authority_failed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observation: dict[str, str] = {}

    def malformed_control_state(stage: str, session) -> None:
        assert stage == "analyze"
        store = CancellationRequestStore(
            session.context.workspace, session.context.registry
        )
        store.path.parent.mkdir(parents=True, exist_ok=True)
        store.path.write_text("{malformed", encoding="utf-8")
        try:
            ProviderRuntime(test_only=True).admit()
        except ProviderRuntimeContractError as exc:
            observation["error"] = str(exc)
            raise

    result = _run_tiny_runner(tmp_path, monkeypatch, malformed_control_state)

    assert result.job_status == "failed"
    assert result.failed_stage == "analyze"
    assert "CONTROL_STATE_INVALID" in observation["error"]


def test_provider_runtime_releases_unstarted_admission_when_cancel_arrives_before_transport(
    tmp_path: Path,
) -> None:
    workspace = JobWorkspace.create(str(tmp_path), "cancel-before-transport", "cancel-before-transport-job")
    registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    identity = RuntimeControlIdentityV1(
        job_id=workspace.job_id,
        workspace_path=workspace.root_dir,
        acceptance_run_id=f"run:{workspace.job_id}",
        provider_budget_started=True,
    )
    controller = ProviderBudgetController(
        ProviderAggregateBudgetV1(
            max_provider_calls_total=2,
            max_output_tokens_total=2_000,
            max_retry_attempts_total=0,
            max_wall_seconds=60,
        )
    )
    state_path = Path(workspace.root_dir) / "logs" / "acceptance_budget_state_v1.json"
    controller.bind_state_path(
        state_path,
        acceptance_run_id=identity.acceptance_run_id,
        state_started=False,
    )
    ledger = ProviderRuntimeLedger(
        Path(workspace.root_dir) / "logs" / "provider_receipts.jsonl"
    )

    with bind_runtime_control_context(identity, workspace=workspace, registry=registry):
        provider = ProviderRuntime(
            aggregate_budget=controller,
            ledger=ledger,
            test_only=True,
        )
        admission = provider.admit(requested_output_tokens=128)
        assert controller.snapshot()["calls_reserved"] == 1
        CancellationRequestStore(workspace, registry).request(
            requested_by="test",
            reason="cancel before transport-start marker",
        )
        with pytest.raises(ProviderCancellationRequested, match="CANCEL_REQUESTED"):
            provider.mark_transport_started(admission)

    final = controller.snapshot()
    assert final["calls_used"] == 0
    assert final["calls_reserved"] == 0
    assert final["output_tokens_used"] == 0
    assert final["output_tokens_reserved"] == 0
    assert final["retry_attempts_used"] == 0
    assert final["retry_attempts_reserved"] == 0
    assert ledger.list_receipts() == ()
    assert not ledger.path.exists()
