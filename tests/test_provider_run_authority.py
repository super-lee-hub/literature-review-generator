from __future__ import annotations

import json
import time
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from runtime.provider_run_envelope import ProviderRunEnvelopeV1
from runtime.provider_runtime import (
    AcceptanceExecutionContextV1,
    ProviderAggregateBudgetV1,
    ProviderAggregateBudgetV2,
    ProviderBudgetController,
    ProviderRuntimeContractError,
    ProviderRunScopeProofV1,
    acceptance_context_environment,
    bind_acceptance_execution_context,
    bind_provider_run_plan_binding,
    bind_provider_run_scope_authorization,
    bind_provider_run_scope_proof,
    project_shared_retry_attempts_v1,
    project_shared_retry_slots_v1,
    provider_budget_controller_from_environment,
    provider_run_budget_controller_from_environment,
    provider_run_plan_binding_from_verified_acceptance_plan,
    provider_run_scope_authorization_from_verified_artifact,
    provider_run_scope_payload_from_plan_binding,
    provider_run_scope_proof_from_verified_artifact,
    validate_provider_run_scope_authorization,
    validate_provider_run_scope_authority,
    validate_started_acceptance_execution_context,
)
from services.durable_io import interprocess_file_lock


_SCOPE_HASHES = {
    "runtime_spec_sha256": "1" * 64,
    "effective_config_sha256": "2" * 64,
    "source_snapshot_sha256": "3" * 64,
    "review_goal_sha256": "4" * 64,
    "route_plan_sha256": "5" * 64,
    "stage_work_contract_sha256": "6" * 64,
}


def _controller(
    path: Path,
    *,
    budget: ProviderAggregateBudgetV1 | ProviderAggregateBudgetV2,
    run_id: str = "accepted-run-1",
    state_started: bool = False,
) -> ProviderBudgetController:
    controller = ProviderBudgetController(budget)
    controller.bind_state_path(
        path,
        acceptance_run_id=run_id,
        state_started=state_started,
    )
    return controller


def _acceptance_context(
    controller: ProviderBudgetController,
    *,
    run_id: str = "accepted-run-1",
    deadline: float | None = None,
    executable_sha: str = "a" * 40,
) -> AcceptanceExecutionContextV1:
    assert controller.state_path is not None
    return AcceptanceExecutionContextV1(
        acceptance_run_id=run_id,
        final_executable_sha=executable_sha,
        absolute_deadline_epoch=deadline if deadline is not None else time.time() + 600,
        provider_budget=controller.budget,
        provider_budget_state_path=str(controller.state_path),
        evidence_root=str(controller.state_path.parent / "evidence"),
        process_event_log=str(controller.state_path.parent / "events.jsonl"),
        scenario_state_path=str(controller.state_path.parent / "scenario.json"),
        owner_authorized=True,
        provider_budget_state_started=True,
    )


def _v2_budget() -> ProviderAggregateBudgetV2:
    return ProviderAggregateBudgetV2(
        max_provider_calls_total=8,
        max_output_tokens_total=16_000,
        max_retry_attempts_total=2,
        max_wall_seconds=600,
    )


def _v1_budget() -> ProviderAggregateBudgetV1:
    # Zero keeps historical V1 unbounded semantics. This is intentionally not
    # reinterpreted or migrated by the new-run authority resolver.
    return ProviderAggregateBudgetV1(
        max_provider_calls_total=0,
        max_output_tokens_total=0,
        max_retry_attempts_total=0,
        max_wall_seconds=0,
    )


def _proof(controller: ProviderBudgetController) -> Any:
    envelope = ProviderRunEnvelopeV1(
        job_id="job-accepted-1",
        run_id="accepted-run-1",
        budget_authority="bound_acceptance_context",
        provider_budget=controller.budget,
        scope_hashes=_SCOPE_HASHES,
        projection_identity_hash="7" * 64,
        admission_status="ready_with_jit",
        known_lower_bounds={
            "provider_calls": 0,
            "output_tokens": 0,
            "retry_attempts": 0,
        },
        conditional_work_upper_bounds={
            "logical_calls_upper_bound": 1,
            "physical_attempts_upper_bound": 1,
            "output_tokens_all_attempts": 128,
            "wall_seconds_upper_bound": 60.0,
            "unknown_exposure_count": 0,
        },
    )
    record = SimpleNamespace(
        artifact_id="provider_run_envelope",
        artifact_role="provider_run_envelope",
        status="ready",
        content_hash="8" * 64,
    )
    return provider_run_scope_proof_from_verified_artifact(
        envelope.to_dict(),
        artifact_id=record.artifact_id,
        artifact_role=record.artifact_role,
        artifact_status=record.status,
        artifact_sha256=record.content_hash,
    )


def _plan_binding(
    controller: ProviderBudgetController,
    context: AcceptanceExecutionContextV1,
) -> Any:
    return provider_run_plan_binding_from_verified_acceptance_plan(
        context,
        controller,
        current_executable_sha=context.final_executable_sha,
        acceptance_plan_sha256="a" * 64,
        acceptance_spec_file_sha256="b" * 64,
        scenario_id="C",
        job_id="job-accepted-1",
        runtime_spec_sha256=_SCOPE_HASHES["runtime_spec_sha256"],
        runtime_spec_file_sha256="c" * 64,
        scope_hashes=_SCOPE_HASHES,
        input_identity_sha256="d" * 64,
        workspace_identity_sha256="e" * 64,
    )


def _authorization(
    controller: ProviderBudgetController,
    plan_binding: Any,
) -> Any:
    payload = {
        "schema_version": "accepted-provider-run-scope/v1",
        "job_id": plan_binding.job_id,
        "run_id": plan_binding.acceptance_run_id,
        "provider_budget": controller.budget.to_dict(),
        "scope_hashes": _SCOPE_HASHES,
        "acceptance_plan_sha256": plan_binding.acceptance_plan_sha256,
        "acceptance_spec_file_sha256": plan_binding.acceptance_spec_file_sha256,
        "scenario_id": plan_binding.scenario_id,
        "runtime_spec_sha256": plan_binding.runtime_spec_sha256,
        "runtime_spec_file_sha256": plan_binding.runtime_spec_file_sha256,
        "input_identity_sha256": plan_binding.input_identity_sha256,
        "workspace_identity_sha256": plan_binding.workspace_identity_sha256,
        "final_executable_sha": plan_binding.final_executable_sha,
    }
    return provider_run_scope_authorization_from_verified_artifact(
        payload,
        artifact_id="accepted-runtime-scope:job-accepted-1",
        artifact_type="accepted_runtime_run_scope",
        artifact_version="v1",
        artifact_role="accepted_runtime_run_scope",
        artifact_status="ready",
        artifact_sha256="9" * 64,
    )


def test_legacy_v1_acceptance_context_cannot_authorize_new_run_and_keeps_ledger(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (
        "AUTO_GENERATE_ACCEPTANCE_BUDGET_JSON",
        "AUTO_GENERATE_ACCEPTANCE_BUDGET_STATE_PATH",
        "AUTO_GENERATE_ACCEPTANCE_CONTEXT_JSON",
        "AUTO_GENERATE_ACCEPTANCE_RUN_ID",
    ):
        monkeypatch.delenv(name, raising=False)
    controller = _controller(
        tmp_path / "legacy-state.json",
        budget=_v1_budget(),
        state_started=False,
    )
    context = _acceptance_context(controller)
    state_path = controller.state_path
    assert state_path is not None
    before = state_path.read_bytes()
    explicit_zero = ProviderAggregateBudgetV2(
        max_provider_calls_total=0,
        max_output_tokens_total=0,
        max_retry_attempts_total=0,
        max_wall_seconds=0,
    )

    with bind_acceptance_execution_context(context, controller):
        # Historical APIs still read the same V1 controller and semantics.
        assert provider_budget_controller_from_environment() is controller
        with pytest.raises(ProviderRuntimeContractError, match="conflicts"):
            provider_run_budget_controller_from_environment(
                runtime_spec_budget=explicit_zero
            )

    assert state_path.read_bytes() == before
    snapshot = controller.snapshot()
    assert snapshot["calls_used"] == 0
    assert snapshot["output_tokens_used"] == 0
    assert snapshot["retry_attempts_used"] == 0


def test_inherited_serialized_v1_context_and_legacy_env_budget_are_rejected_without_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (
        "AUTO_GENERATE_ACCEPTANCE_BUDGET_JSON",
        "AUTO_GENERATE_ACCEPTANCE_BUDGET_STATE_PATH",
        "AUTO_GENERATE_ACCEPTANCE_CONTEXT_JSON",
        "AUTO_GENERATE_ACCEPTANCE_RUN_ID",
    ):
        monkeypatch.delenv(name, raising=False)
    controller = _controller(
        tmp_path / "legacy-state.json",
        budget=_v1_budget(),
    )
    context = _acceptance_context(controller)
    state_path = controller.state_path
    assert state_path is not None
    before = state_path.read_bytes()
    bridge = {
        "AUTO_GENERATE_ACCEPTANCE_BUDGET_JSON": json.dumps(
            context.provider_budget.to_dict(), sort_keys=True
        ),
        "AUTO_GENERATE_ACCEPTANCE_BUDGET_STATE_PATH": context.provider_budget_state_path,
        "AUTO_GENERATE_ACCEPTANCE_CONTEXT_JSON": json.dumps(
            context.to_dict(), sort_keys=True
        ),
        "AUTO_GENERATE_ACCEPTANCE_RUN_ID": context.acceptance_run_id,
    }
    for name, value in bridge.items():
        monkeypatch.setenv(name, value)

    explicit_zero = ProviderAggregateBudgetV2(0, 0, 0, 0)
    with pytest.raises(ProviderRuntimeContractError, match="serialized acceptance context"):
        provider_run_budget_controller_from_environment(
            runtime_spec_budget=explicit_zero
        )
    assert state_path.read_bytes() == before

    # No serialized context does not make an unbound compatibility V1 budget
    # ordinary run authority. Reject before the old factory can create state.
    for name in (
        "AUTO_GENERATE_ACCEPTANCE_BUDGET_STATE_PATH",
        "AUTO_GENERATE_ACCEPTANCE_CONTEXT_JSON",
        "AUTO_GENERATE_ACCEPTANCE_RUN_ID",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(
        "AUTO_GENERATE_ACCEPTANCE_BUDGET_JSON",
        json.dumps(_v1_budget().to_dict(), sort_keys=True),
    )
    monkeypatch.setenv("AUTO_GENERATE_ACCEPTANCE_BUDGET_STATE_PATH", str(state_path))
    with pytest.raises(ProviderRuntimeContractError, match="legacy V1 environment budget"):
        provider_run_budget_controller_from_environment(
            runtime_spec_budget=explicit_zero
        )
    assert state_path.read_bytes() == before

    monkeypatch.delenv("AUTO_GENERATE_ACCEPTANCE_BUDGET_STATE_PATH", raising=False)
    missing_state = tmp_path / "must-not-be-created.json"
    monkeypatch.setenv("AUTO_GENERATE_ACCEPTANCE_BUDGET_STATE_PATH", str(missing_state))
    monkeypatch.setenv("AUTO_GENERATE_RUN_LIVE_ACCEPTANCE", "1")
    with pytest.raises(ProviderRuntimeContractError, match="legacy V1 environment budget"):
        provider_run_budget_controller_from_environment(
            runtime_spec_budget=explicit_zero
        )
    assert not missing_state.exists()


def test_serialized_v2_context_is_not_an_in_process_accepted_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (
        "AUTO_GENERATE_ACCEPTANCE_BUDGET_JSON",
        "AUTO_GENERATE_ACCEPTANCE_BUDGET_STATE_PATH",
        "AUTO_GENERATE_ACCEPTANCE_CONTEXT_JSON",
        "AUTO_GENERATE_ACCEPTANCE_RUN_ID",
    ):
        monkeypatch.delenv(name, raising=False)
    controller = _controller(
        tmp_path / "serialized-v2-state.json",
        budget=_v2_budget(),
    )
    context = _acceptance_context(controller)
    state_path = controller.state_path
    assert state_path is not None
    before = state_path.read_bytes()
    for name, value in acceptance_context_environment(context).items():
        monkeypatch.setenv(name, value)

    with pytest.raises(ProviderRuntimeContractError, match="serialized acceptance context"):
        provider_run_budget_controller_from_environment(
            runtime_spec_budget=_v2_budget()
        )
    assert state_path.read_bytes() == before


def test_active_accepted_entry_rejects_conflicting_environment_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (
        "AUTO_GENERATE_ACCEPTANCE_BUDGET_JSON",
        "AUTO_GENERATE_ACCEPTANCE_BUDGET_STATE_PATH",
        "AUTO_GENERATE_ACCEPTANCE_CONTEXT_JSON",
        "AUTO_GENERATE_ACCEPTANCE_RUN_ID",
    ):
        monkeypatch.delenv(name, raising=False)
    controller = _controller(
        tmp_path / "accepted-v2-state.json",
        budget=_v2_budget(),
    )
    context = _acceptance_context(controller)
    for name, value in acceptance_context_environment(context).items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("AUTO_GENERATE_ACCEPTANCE_RUN_ID", "different-run")

    with bind_acceptance_execution_context(context, controller):
        with pytest.raises(ProviderRuntimeContractError, match="state identity conflicts"):
            provider_run_budget_controller_from_environment(
                runtime_spec_budget=_v2_budget()
            )


def test_expired_v1_context_with_zero_wall_budget_is_not_valid_authority(
    tmp_path: Path,
) -> None:
    controller = _controller(
        tmp_path / "expired-legacy.json",
        budget=_v1_budget(),
        state_started=False,
    )
    context = _acceptance_context(controller, deadline=time.time() - 1)
    with bind_acceptance_execution_context(context, controller):
        with pytest.raises(ProviderRuntimeContractError, match="deadline has expired"):
            validate_started_acceptance_execution_context(
                context,
                controller,
                current_executable_sha=context.final_executable_sha,
            )
        with pytest.raises(ProviderRuntimeContractError, match="current"):
            provider_run_budget_controller_from_environment()


def test_active_v2_accepted_continuation_requires_exact_run_scope_proof(
    tmp_path: Path,
) -> None:
    controller = _controller(
        tmp_path / "accepted-v2.json",
        budget=_v2_budget(),
        state_started=False,
    )
    context = _acceptance_context(controller)
    proof = _proof(controller)
    assert proof.scope_hashes == _SCOPE_HASHES

    with bind_acceptance_execution_context(context, controller):
        assert provider_run_budget_controller_from_environment() is controller
        plan_binding = _plan_binding(controller, context)
        with bind_provider_run_plan_binding(plan_binding):
            with pytest.raises(ProviderRuntimeContractError, match="lacks a verified accepted-plan"):
                validate_provider_run_scope_authorization(
                    controller,
                    job_id=proof.job_id,
                    scope_hashes=_SCOPE_HASHES,
                    workspace_identity_sha256=plan_binding.workspace_identity_sha256,
                    current_executable_sha=context.final_executable_sha,
                )
            scope_payload = provider_run_scope_payload_from_plan_binding(
                controller,
                job_id=proof.job_id,
                scope_hashes=_SCOPE_HASHES,
                workspace_identity_sha256=plan_binding.workspace_identity_sha256,
                current_executable_sha=context.final_executable_sha,
            )
            authorization = provider_run_scope_authorization_from_verified_artifact(
                scope_payload,
                artifact_id="accepted-runtime-scope:job-accepted-1",
                artifact_type="accepted_runtime_run_scope",
                artifact_version="v1",
                artifact_role="accepted_runtime_run_scope",
                artifact_status="ready",
                artifact_sha256="9" * 64,
            )
            with bind_provider_run_scope_authorization(authorization):
                # The verified accepted-plan binding authorizes the first run
                # envelope to be materialized, before the envelope artifact exists.
                validate_provider_run_scope_authorization(
                    controller,
                    job_id=proof.job_id,
                    scope_hashes=_SCOPE_HASHES,
                    workspace_identity_sha256=plan_binding.workspace_identity_sha256,
                    current_executable_sha=context.final_executable_sha,
                )
                with pytest.raises(ProviderRuntimeContractError, match="lacks a verified run-scope envelope"):
                    validate_provider_run_scope_authority(
                        controller,
                        job_id=proof.job_id,
                        scope_hashes=_SCOPE_HASHES,
                        workspace_identity_sha256=plan_binding.workspace_identity_sha256,
                        current_executable_sha=context.final_executable_sha,
                    )
                with bind_provider_run_scope_proof(proof):
                    # Reopening the ready run envelope then validates the
                    # continuation against that same approved scope.
                    validate_provider_run_scope_authority(
                        controller,
                        job_id=proof.job_id,
                        scope_hashes=_SCOPE_HASHES,
                        workspace_identity_sha256=plan_binding.workspace_identity_sha256,
                        current_executable_sha=context.final_executable_sha,
                    )
                    with pytest.raises(ProviderRuntimeContractError, match="does not match"):
                        validate_provider_run_scope_authority(
                            controller,
                            job_id="unrelated-job",
                            scope_hashes=_SCOPE_HASHES,
                            workspace_identity_sha256=plan_binding.workspace_identity_sha256,
                            current_executable_sha=context.final_executable_sha,
                        )
                    off_scope = {**_SCOPE_HASHES, "source_snapshot_sha256": "9" * 64}
                    with pytest.raises(ProviderRuntimeContractError, match="does not match"):
                        validate_provider_run_scope_authority(
                            controller,
                            job_id=proof.job_id,
                            scope_hashes=off_scope,
                            workspace_identity_sha256=plan_binding.workspace_identity_sha256,
                            current_executable_sha=context.final_executable_sha,
                        )


def test_accepted_scope_rejects_stale_executable_and_expired_deadline(
    tmp_path: Path,
) -> None:
    controller = _controller(
        tmp_path / "accepted-v2.json",
        budget=_v2_budget(),
        state_started=False,
    )
    expired = _acceptance_context(controller, deadline=time.time() - 1)
    proof = _proof(controller)
    current = _acceptance_context(controller)
    plan_binding = _plan_binding(controller, current)
    authorization = _authorization(controller, plan_binding)
    with bind_acceptance_execution_context(expired, controller):
        with bind_provider_run_plan_binding(plan_binding):
            with bind_provider_run_scope_authorization(authorization):
                with bind_provider_run_scope_proof(proof):
                    with pytest.raises(ProviderRuntimeContractError, match="deadline has expired"):
                        validate_provider_run_scope_authority(
                            controller,
                            job_id=proof.job_id,
                            scope_hashes=_SCOPE_HASHES,
                            workspace_identity_sha256=plan_binding.workspace_identity_sha256,
                            current_executable_sha=expired.final_executable_sha,
                        )

    with bind_acceptance_execution_context(current, controller):
        with bind_provider_run_plan_binding(plan_binding):
            with bind_provider_run_scope_authorization(authorization):
                with bind_provider_run_scope_proof(proof):
                    with pytest.raises(ProviderRuntimeContractError, match="executable SHA"):
                        validate_provider_run_scope_authority(
                            controller,
                            job_id=proof.job_id,
                            scope_hashes=_SCOPE_HASHES,
                            workspace_identity_sha256=plan_binding.workspace_identity_sha256,
                            current_executable_sha="b" * 40,
                        )


@pytest.mark.parametrize(
    ("retry_pool", "used", "reserved", "expected"),
    [(2, 0, 0, 2), (2, 1, 0, 1), (0, 0, 0, 0)],
)
def test_v2_shared_retry_projection_uses_only_remaining_run_pool(
    tmp_path: Path,
    retry_pool: int,
    used: int,
    reserved: int,
    expected: int,
) -> None:
    budget = ProviderAggregateBudgetV2(
        max_provider_calls_total=200,
        max_output_tokens_total=100_000,
        max_retry_attempts_total=retry_pool,
        max_wall_seconds=3_600,
    )
    controller = _controller(tmp_path / f"retry-{retry_pool}-{used}.json", budget=budget)
    # Keep durable state in the requested accounting position without running
    # a network call. These counters are private test setup, then persisted by
    # the controller's normal state writer.
    assert controller.state_path is not None
    with controller._lock:
        with interprocess_file_lock(controller.state_path):
            controller._retry_attempts_used = used
            controller._retry_attempts_reserved = reserved
            controller._persist_state_unlocked()
    projection = project_shared_retry_attempts_v1(
        151,
        [2] * 151,
        aggregate_controller=controller,
    )
    assert projection.configured_retry_slots_upper_bound == 302
    assert projection.aggregate_retry_pool_remaining == expected
    assert projection.retry_attempts_upper_bound == expected
    assert projection.physical_attempts_upper_bound == 151 + expected


def test_unknown_shared_retry_pool_keeps_per_call_upper_and_v1_zero_semantics(
    tmp_path: Path,
) -> None:
    no_controller = project_shared_retry_attempts_v1(151, [2] * 151)
    assert no_controller.retry_attempts_upper_bound == 302
    assert no_controller.physical_attempts_upper_bound == 453

    v1_controller = _controller(
        tmp_path / "legacy-retry-zero.json",
        budget=_v1_budget(),
    )
    legacy = project_shared_retry_attempts_v1(
        151,
        [2] * 151,
        aggregate_controller=v1_controller,
    )
    assert legacy.aggregate_retry_pool_remaining is None
    assert legacy.retry_attempts_upper_bound == 302
    assert legacy.physical_attempts_upper_bound == 453

    strict_zero = ProviderBudgetController(ProviderAggregateBudgetV2(200, 100_000, 0, 3_600))
    strict = project_shared_retry_attempts_v1(
        151,
        [2] * 151,
        aggregate_controller=strict_zero,
    )
    assert strict.retry_attempts_upper_bound == 0
    assert strict.physical_attempts_upper_bound == 151


def test_finite_pool_bounds_unknown_per_call_retry_ceiling() -> None:
    controller = ProviderBudgetController(ProviderAggregateBudgetV2(20, 10_000, 2, 60))
    projection = project_shared_retry_attempts_v1(
        151,
        [None] * 151,
        aggregate_controller=controller,
    )
    assert projection.configured_retry_slots_upper_bound is None
    assert projection.aggregate_retry_pool_remaining == 2
    assert projection.retry_attempts_upper_bound == 2
    assert projection.physical_attempts_upper_bound == 153


def test_scope_proof_cannot_be_constructed_from_an_approval_flag() -> None:
    with pytest.raises(ProviderRuntimeContractError, match="verified Registry envelope"):
        ProviderRunScopeProofV1(
            job_id="job-accepted-1",
            run_id="accepted-run-1",
            provider_budget_sha256="a" * 64,
            scope_hashes=_SCOPE_HASHES,
            artifact_id="provider_run_envelope",
            artifact_sha256="b" * 64,
        )


@pytest.mark.integration
def test_public_runner_rejects_legacy_v1_environment_before_any_post(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.test_run_first_run_envelope import (
        _budget_mapping,
        _run_spec,
        _run_until_budget_rejection,
        _start_local_model_fixture,
    )

    for name in (
        "AUTO_GENERATE_ACCEPTANCE_BUDGET_JSON",
        "AUTO_GENERATE_ACCEPTANCE_BUDGET_STATE_PATH",
        "AUTO_GENERATE_ACCEPTANCE_CONTEXT_JSON",
        "AUTO_GENERATE_ACCEPTANCE_RUN_ID",
        "AUTO_GENERATE_RUN_LIVE_ACCEPTANCE",
    ):
        monkeypatch.delenv(name, raising=False)
    server, thread, api_base = _start_local_model_fixture()
    spec = _run_spec(
        tmp_path,
        api_base,
        budget=_budget_mapping(
            calls=0,
            output_tokens=0,
            retry_attempts=0,
            wall_seconds=0,
        ),
    )
    monkeypatch.setenv(
        "AUTO_GENERATE_ACCEPTANCE_BUDGET_JSON",
        json.dumps(_v1_budget().to_dict(), sort_keys=True),
    )
    try:
        result, rejected = _run_until_budget_rejection(spec)
        assert result is None
        assert "legacy v1" in rejected.casefold()
        assert server.calls == []  # type: ignore[attr-defined]
        workspace = Path(spec.workspace_path)
        if workspace.exists():
            state_files = [
                path
                for path in workspace.rglob("*.json")
                if path.name == "acceptance_budget_state_v1.json"
            ]
            assert not state_files
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


@pytest.mark.integration
def test_control_plane_revalidates_serialized_v2_entry_for_first_run_and_resume(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from runtime.control_plane import ReviewControlPlane
    from runtime.release_acceptance import AcceptanceRunStateV1, ReleaseAcceptancePlanV2
    from runtime.provider_runtime import acceptance_context_environment
    from runtime.runner import AgentRuntimeRunner
    from tests.test_run_first_run_envelope import (
        _run_spec,
        _start_local_model_fixture,
    )
    import runtime.control_plane as control_plane_module
    import runtime.checkout_identity as checkout_identity_module
    import os
    import subprocess
    import sys

    for name in (
        "AUTO_GENERATE_ACCEPTANCE_BUDGET_JSON",
        "AUTO_GENERATE_ACCEPTANCE_BUDGET_STATE_PATH",
        "AUTO_GENERATE_ACCEPTANCE_CONTEXT_JSON",
        "AUTO_GENERATE_ACCEPTANCE_RUN_ID",
        "AUTO_GENERATE_RUN_LIVE_ACCEPTANCE",
    ):
        monkeypatch.delenv(name, raising=False)
    repo_root = Path(__file__).resolve().parents[1]
    try:
        accepted_sha = checkout_identity_module.read_checkout_sha(
            repo_root,
            require_clean=True,
        )
        clean_checkout = True
    except Exception:
        # This branch keeps the integration test useful in an actively edited
        # worktree. The clean-checkout CI branch below launches a real child
        # reviewctl process and verifies serialized-context revalidation.
        accepted_sha = "a" * 40
        clean_checkout = False
    server, thread, api_base = _start_local_model_fixture()
    budget_payload = {
        "schema_version": ProviderAggregateBudgetV2.SCHEMA_VERSION,
        "max_provider_calls_total": 500,
        "max_output_tokens_total": 5_000_000,
        "max_retry_attempts_total": 2,
        "max_wall_seconds": 3_600,
    }
    spec = _run_spec(tmp_path, api_base, budget=budget_payload)
    spec_path = tmp_path / "accepted-runtime-spec.json"
    spec_path.write_text(
        json.dumps(spec.to_dict(), ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    plan_payload = {
        "schema_version": "release-acceptance-plan-v2",
        "parent_run_id": "accepted-v2-first-entry",
        "budget": budget_payload,
        "final_executable_sha": accepted_sha,
        "scenarios": {
            "F": {
                "scenario_id": "F",
                "gate": "F",
                "runtime_spec": str(spec_path),
                "workspace": spec.workspace_path,
                "job_id": spec.job_id,
                "execution_mode": "runtime",
                "budget_domain": "live",
                "prerequisites": [],
            },
        },
    }
    acceptance_spec_path = tmp_path / "acceptance-plan.json"
    acceptance_spec_path.write_text(
        json.dumps(plan_payload, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    accepted_plan = ReleaseAcceptancePlanV2.from_mapping(
        plan_payload,
        origin_dir=tmp_path,
    )
    run_id = accepted_plan.parent_run_id
    budget_state_path = tmp_path / "accepted-provider-budget.json"
    controller = ProviderBudgetController(
        ProviderAggregateBudgetV2.from_mapping(budget_payload)
    )
    controller.bind_state_path(
        budget_state_path,
        acceptance_run_id=run_id,
        state_started=False,
    )
    state_path = tmp_path / "acceptance-run-state.json"
    state = AcceptanceRunStateV1(
        run_id=run_id,
        final_sha=accepted_sha,
        acceptance_spec_path=str(acceptance_spec_path),
        runtime_spec_path="",
        status="planned",
        gates={"F": {"gate": "F", "status": "NOT_RUN"}},
        updated_at="2026-10-04T00:00:00Z",
        provider_budget_state_path=str(budget_state_path),
        plan_sha256=accepted_plan.plan_sha256(),
    )
    state_path.write_text(
        json.dumps(state.to_dict(), ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    context = AcceptanceExecutionContextV1(
        acceptance_run_id=run_id,
        final_executable_sha=accepted_sha,
        absolute_deadline_epoch=controller.durable_deadline_epoch or time.time() + 3600,
        provider_budget=controller.budget,
        provider_budget_state_path=str(budget_state_path),
        evidence_root=str(tmp_path / "evidence"),
        process_event_log=str(tmp_path / "events.jsonl"),
        scenario_state_path=str(state_path),
        owner_authorized=True,
        provider_budget_state_started=True,
    )
    for name, value in acceptance_context_environment(context).items():
        monkeypatch.setenv(name, value)
    # The acceptance context binds this exact clean executable in production;
    # keep the local working-tree test hermetic while exercising the same
    # accepted-plan, state, budget, Registry, and transport path.
    monkeypatch.setattr(
        control_plane_module,
        "read_checkout_sha",
        lambda _root, require_clean=False: accepted_sha,
    )
    monkeypatch.setattr(
        checkout_identity_module,
        "read_checkout_sha",
        lambda _root, require_clean=False: accepted_sha,
    )
    control = ReviewControlPlane(repo_root=repo_root)
    injected: list[str] = []

    def interrupt_after_analyze(point: str, context_payload: Any) -> None:
        if (
            point == "after_registry_write_before_stage_terminal"
            and context_payload.get("stage_name") == "analyze"
            and not injected
        ):
            injected.append("analyze")
            raise RuntimeError("test interruption after accepted source receipts")

    class InterruptingRunner(AgentRuntimeRunner):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            kwargs["fault_injector"] = interrupt_after_analyze
            super().__init__(*args, **kwargs)

    try:
        monkeypatch.setattr(control_plane_module, "AgentRuntimeRunner", InterruptingRunner)
        first = control.run(spec_path)
        assert injected == ["analyze"]
        assert first["job_status"] == "failed", first
        assert server.calls  # type: ignore[attr-defined]
        assert first["job_id"] == spec.job_id
        _workspace, registry = AgentRuntimeRunner._open_workspace(spec.workspace_path)
        scope_record = registry.get("accepted_runtime_run_scope")
        envelope_record = registry.get("provider_run_envelope")
        assert scope_record is not None and scope_record.status == "ready"
        assert envelope_record is not None and envelope_record.status == "ready"
        registry.verify_ready_artifact_closure(scope_record)
        registry.verify_ready_artifact_closure(envelope_record)
        scope_payload = json.loads(Path(scope_record.path).read_text(encoding="utf-8"))
        envelope_payload = json.loads(Path(envelope_record.path).read_text(encoding="utf-8"))
        assert scope_payload["run_id"] == run_id
        assert scope_payload["job_id"] == spec.job_id
        assert scope_payload["scope_hashes"] == envelope_payload["scope_hashes"]
        calls_before_resume = len(server.calls)  # type: ignore[attr-defined]
        reader_calls_before_resume = server.reader_calls  # type: ignore[attr-defined]
        assert reader_calls_before_resume == 4
        budget_before_resume = json.loads(budget_state_path.read_text(encoding="utf-8"))
        assert int(budget_before_resume["calls_used"]) > 0
        from services.artifact_registry import file_sha256

        recovery_source_hashes = {
            relative: file_sha256(repo_root / relative)
            for relative in ("runtime/runner.py", "runtime/stage1_publication_recovery.py")
        }
        stage1_publication_before = {}
        for artifact_id in (
            "summary_file", "summary_source_manifest", "stage1_progress_snapshot",
            "stage1:provider_expected_call_graph", "stage1:provider_receipt_closure",
            "stage1_provider_receipts",
        ):
            record = registry.get(artifact_id)
            assert record is not None and record.status == "ready"
            stage1_publication_before[artifact_id] = {
                "record": asdict(record), "file_sha256": file_sha256(record.path),
            }
        monkeypatch.setattr(control_plane_module, "AgentRuntimeRunner", AgentRuntimeRunner)
        child_environment = os.environ.copy()
        command = [
            sys.executable,
            "-m",
            "reviewctl",
            "resume",
            "--workspace",
            spec.workspace_path,
            "--job",
            spec.job_id,
            "--json",
        ]
        if not clean_checkout:
            # The test checkout is intentionally dirty. In the child only,
            # substitute the current executable identity read; the full
            # accepted plan/state/budget/scope revalidation remains active.
            child_script = tmp_path / "resume_accepted_child.py"
            child_script.write_text(
                "import os, sys\n"
                f"sys.path.insert(0, {json.dumps(str(repo_root))})\n"
                "import runtime.checkout_identity as _identity\n"
                "def _read_test_checkout_sha(_root, require_clean=False):\n"
                "    return os.environ['TEST_ACCEPTED_EXECUTABLE_SHA']\n"
                "_identity.read_checkout_sha = _read_test_checkout_sha\n"
                "import runtime.control_plane as _control_plane\n"
                "_control_plane.read_checkout_sha = _read_test_checkout_sha\n"
                "import reviewctl\n"
                f"sys.argv = ['reviewctl', 'resume', '--workspace', {json.dumps(spec.workspace_path)}, '--job', {json.dumps(spec.job_id)}, '--json']\n"
                "raise SystemExit(reviewctl.main())\n",
                encoding="utf-8",
            )
            child_environment["TEST_ACCEPTED_EXECUTABLE_SHA"] = accepted_sha
            command = [sys.executable, str(child_script)]
        child = subprocess.run(
            command,
            cwd=str(repo_root),
            env=child_environment,
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        assert child.returncode in {0, 1}, child.stdout + child.stderr
        output_lines = [line for line in child.stdout.splitlines() if line.strip()]
        assert output_lines, child.stderr
        resumed = json.loads(output_lines[-1])
        (tmp_path / "resume-result.json").write_text(json.dumps(resumed, ensure_ascii=False, indent=2), encoding="utf-8")
        assert resumed["job_id"] == spec.job_id
        assert resumed["job_status"] == "completed", resumed
        assert server.reader_calls == reader_calls_before_resume, "closed Stage 1 work was reposted after publication interruption"  # type: ignore[attr-defined]
        _workspace, registry = AgentRuntimeRunner._open_workspace(spec.workspace_path)
        final_outline = registry.get("outline-v3:final_outline")
        provider_closure = registry.get("outline-v3:provider_receipt_closure")
        assert final_outline is not None and final_outline.status == "ready"
        assert provider_closure is not None and provider_closure.status == "ready"
        registry.verify_ready_artifact_closure(final_outline)
        registry.verify_ready_artifact_closure(provider_closure)
        for artifact_id, proof in stage1_publication_before.items():
            record = registry.get(artifact_id)
            assert record is not None and asdict(record) == proof["record"]
            assert file_sha256(record.path) == proof["file_sha256"]
        assert len(server.calls) >= calls_before_resume  # type: ignore[attr-defined]
        budget_after_resume = json.loads(budget_state_path.read_text(encoding="utf-8"))
        for field in ("budget", "first_started_epoch", "absolute_deadline_epoch"):
            assert budget_after_resume[field] == budget_before_resume[field]
        assert recovery_source_hashes == {
            relative: file_sha256(repo_root / relative) for relative in recovery_source_hashes
        }
        (tmp_path / "stage1-recovery-readback.json").write_text(
            json.dumps({
                "reader_calls_before": reader_calls_before_resume,
                "reader_calls_after": server.reader_calls,  # type: ignore[attr-defined]
                "stage1_publication_unchanged": stage1_publication_before,
                "budget_before": budget_before_resume,
                "budget_after": budget_after_resume,
                "recovery_source_sha256": recovery_source_hashes,
                "evidence_class": "localhost_subprocess_publication_recovery",
                "clean_checkout": clean_checkout,
            }, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        assert budget_after_resume["calls_used"] >= budget_before_resume["calls_used"]
        assert budget_after_resume["output_tokens_used"] >= budget_before_resume["output_tokens_used"]
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_summed_per_call_retry_ceiling_can_use_known_run_pool() -> None:
    controller = ProviderBudgetController(ProviderAggregateBudgetV2(24, 100_000, 2, 3600))
    projection = project_shared_retry_slots_v1(
        151,
        configured_retry_slots_upper_bound=302,
        aggregate_controller=controller,
    )
    assert projection.configured_retry_slots_upper_bound == 302
    assert projection.aggregate_retry_pool_remaining == 2
    assert projection.retry_attempts_upper_bound == 2
    assert projection.physical_attempts_upper_bound == 153

    unknown_pool = project_shared_retry_slots_v1(
        151,
        configured_retry_slots_upper_bound=302,
    )
    assert unknown_pool.aggregate_retry_pool_remaining is None
    assert unknown_pool.retry_attempts_upper_bound == 302
    assert unknown_pool.physical_attempts_upper_bound == 453
