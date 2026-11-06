from __future__ import annotations

from pathlib import Path
import threading
from unittest.mock import patch

import pytest

from ai_interface import _call_ai_api_detailed
from runtime.attempt_store import AttemptExecutionLease
from runtime.cancellation import CancellationRequestStore
from runtime.control_plane import ReviewControlPlane
from runtime.outline_v3_dag import OutlineNodeStore
from runtime.pause_state import PauseStateStore
from runtime.provider_runtime import (
    ProviderAggregateBudgetV2,
    ProviderBudgetController,
    ProviderBudgetV1,
    ProviderRuntime,
    ProviderRuntimeLedger,
    RuntimeControlIdentityV1,
    bind_runtime_control_context,
    ProviderBudgetExceeded,
)
from runtime.runner import AgentRuntimeRunner
from tests.test_queue_continuation import (
    _install_reader,
    _new_case,
    _seed_failed_analyze,
    _workspace_and_registry,
)


def test_active_lease_resume_preserves_stop_markers_and_dag_bytes(tmp_path: Path, monkeypatch) -> None:
    case = _new_case(tmp_path)
    _install_reader(monkeypatch)
    _seed_failed_analyze(case, monkeypatch)
    workspace, registry = _workspace_and_registry(case)
    dag_store = OutlineNodeStore(workspace, registry)
    dag = dag_store.ensure(workspace.job_id, candidate_count=1)
    dag_store.record_node("outline_evidence_views", expected_current_dag=dag, status="failed")
    before_dag = {
        str(path.relative_to(dag_store.directory)): path.read_bytes()
        for path in dag_store.directory.rglob("*.json")
    }
    assert before_dag
    pause_store = PauseStateStore(workspace, registry)
    pause_store.request(requested_by="test", reason="pause before lease rejection")
    cancel_store = CancellationRequestStore(workspace, registry)
    cancel_store.request(requested_by="test", reason="cancel before lease rejection")
    before_pause = pause_store.read_snapshot()[1]
    before_cancel = cancel_store.read_snapshot()[1]

    control = ReviewControlPlane(repo_root=Path(__file__).resolve().parents[1])
    with AttemptExecutionLease(workspace):
        with pytest.raises(Exception, match="another runtime attempt is already active"):
            control.resume(workspace=workspace.root_dir)

    assert pause_store.read_snapshot()[1] == before_pause
    assert cancel_store.read_snapshot()[1] == before_cancel
    after_dag = {
        str(path.relative_to(dag_store.directory)): path.read_bytes()
        for path in dag_store.directory.rglob("*.json")
    }
    assert after_dag == before_dag


def test_pause_before_transport_refunds_unsent_actual_ai_wrapper(tmp_path: Path) -> None:
    from services.artifact_registry import ArtifactRegistry
    from services.job_workspace import JobWorkspace

    workspace = JobWorkspace.create(str(tmp_path), "pause", "pause-job")
    registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    identity = RuntimeControlIdentityV1(
        job_id=workspace.job_id,
        workspace_path=workspace.root_dir,
        acceptance_run_id="run:pause-job",
        provider_budget_started=True,
    )
    budget = ProviderAggregateBudgetV2(
        max_provider_calls_total=1,
        max_output_tokens_total=128,
        max_retry_attempts_total=0,
        max_wall_seconds=300,
    )
    state_path = Path(workspace.log_path("budget.json"))
    controller = ProviderBudgetController(budget)
    controller.bind_state_path(state_path, acceptance_run_id=identity.acceptance_run_id)
    original_snapshot = controller.snapshot()
    ledger = ProviderRuntimeLedger(workspace.log_path("provider_receipts.jsonl"))
    provider = ProviderRuntime(
        budget=ProviderBudgetV1(max_calls=1), aggregate_budget=controller,
        ledger=ledger, job_id=workspace.job_id, attempt_id="first",
        stage_name="review", route="local-audit", node_id="section-1", call_id="review:section-1",
    )
    with bind_runtime_control_context(identity, workspace=workspace, registry=registry):
        original_admit = provider.admit

        def admit_then_pause(**kwargs):
            result = original_admit(**kwargs)
            PauseStateStore(workspace, registry).request(requested_by="test", reason="pause before POST")
            return result

        with patch.object(provider, "admit", side_effect=admit_then_pause), patch(
            "ai_interface._call_ai_api_detailed_uninstrumented",
            side_effect=AssertionError("transport must not start"),
        ) as transport:
            with pytest.raises(ProviderBudgetExceeded, match="PAUSED_BY_USER"):
                _call_ai_api_detailed(
                    "local fixture", {"provider_family": "openai", "endpoint_type": "chat_completions",
                    "api_base": "http://127.0.0.1:1/v1", "api_key": "audit-local-only",
                    "model": "gpt-4o-mini", "max_output_tokens": 128},
                    "Return JSON.", max_tokens=128, retry_attempts=1, provider_runtime=provider,
                    provider_route="local-audit",
                )
            assert transport.call_count == 0
    snapshot = controller.snapshot()
    assert snapshot["calls_reserved"] == 0
    assert snapshot["output_tokens_reserved"] == 0
    assert snapshot["absolute_deadline_epoch"] == original_snapshot["absolute_deadline_epoch"]
    assert len(ledger.list_receipts()) == 0
    PauseStateStore(workspace, registry).clear(reason="explicit resume")
    resumed_controller = ProviderBudgetController(budget)
    resumed_controller.bind_state_path(
        state_path, acceptance_run_id=identity.acceptance_run_id, state_started=True,
    )
    resumed = ProviderRuntime(
        budget=ProviderBudgetV1(max_calls=1), aggregate_budget=resumed_controller,
        ledger=ledger, job_id=workspace.job_id, attempt_id="second",
        stage_name="review", route="local-audit", node_id="section-1", call_id="review:section-1",
    )
    with bind_runtime_control_context(identity, workspace=workspace, registry=registry):
        resumed.admit(requested_output_tokens=128)
    assert resumed_controller.snapshot()["calls_reserved"] == 1


def test_started_reservation_is_never_refunded() -> None:
    controller = ProviderBudgetController(ProviderAggregateBudgetV2(
        max_provider_calls_total=1, max_output_tokens_total=128,
        max_retry_attempts_total=0, max_wall_seconds=300,
    ))
    reservation = controller.admit(requested_output_tokens=128)
    controller.mark_transport_started(reservation)
    with pytest.raises(Exception, match="after transport started"):
        controller.release_unstarted(reservation)
    assert controller.snapshot()["calls_reserved"] == 1


def test_public_resume_preserves_new_cancel_and_pause_during_normalization(
    tmp_path: Path, monkeypatch,
) -> None:
    case = _new_case(tmp_path)
    _install_reader(monkeypatch)
    _seed_failed_analyze(case, monkeypatch)
    workspace, registry = _workspace_and_registry(case)
    pause_store = PauseStateStore(workspace, registry)
    pause_store.request(requested_by="test", reason="original pause")
    cancel_store = CancellationRequestStore(workspace, registry)
    before_cancel = cancel_store.read_snapshot()[1]
    before_pause = pause_store.read_snapshot()[1]
    dag_store = OutlineNodeStore(workspace, registry)
    before_dag = {
        str(path.relative_to(dag_store.directory)): path.read_bytes()
        for path in dag_store.directory.rglob("*.json")
    }
    original_normalized = AgentRuntimeRunner._normalized_spec
    injected = False
    request_threads = []

    def normalize_and_request_new_stop(runner, *, resume):
        nonlocal injected
        result = original_normalized(runner, resume=resume)
        if resume and not injected:
            injected = True
            request_threads.extend([
                threading.Thread(
                    target=cancel_store.request,
                    kwargs={"requested_by": "test.concurrent", "reason": "new cancel during normalization"},
                ),
                threading.Thread(
                    target=pause_store.request,
                    kwargs={"requested_by": "test.concurrent", "reason": "new pause during normalization"},
                ),
            ])
            for thread in request_threads:
                thread.start()
        return result

    monkeypatch.setattr(AgentRuntimeRunner, "_normalized_spec", normalize_and_request_new_stop)
    control = ReviewControlPlane(repo_root=Path(__file__).resolve().parents[1])
    captured = {}
    def capture_run_spec(spec_path, **kwargs):
        captured.update(kwargs.get("resume_control_snapshot") or {})
        raise RuntimeError("stop after public snapshot capture")
    monkeypatch.setattr(control, "_run_spec", capture_run_spec)
    with pytest.raises(RuntimeError, match="stop after public snapshot capture"):
        control.resume(workspace=workspace.root_dir)
    for thread in request_threads:
        thread.join(timeout=10)
        assert not thread.is_alive()
    assert captured["cancel_hash"] == before_cancel
    assert captured["pause_hash"] == before_pause
    assert cancel_store.read_snapshot()[1] != before_cancel
    assert pause_store.read_snapshot()[1] != before_pause
    after_dag = {
        str(path.relative_to(dag_store.directory)): path.read_bytes()
        for path in dag_store.directory.rglob("*.json")
    }
    assert after_dag == before_dag
