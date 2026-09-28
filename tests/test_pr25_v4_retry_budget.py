from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

import runtime.provider_runtime as provider_runtime_module
from outline.provider_router import OutlineProviderRouter, OutlineRoleRoute
from runtime.provider_context import ProviderContextProfile
from runtime.provider_routes import ReachableProviderRoute, ReachableProviderRoutePlan
from runtime.provider_runtime import ProviderAggregateBudgetV1, ProviderRuntime
from runtime.stage_planning import (
    ProviderStageRequestInventoryV1,
    UnplannedProviderExposureV1,
    build_full_stage_request_plan_v1,
    build_provider_request_plan_row_v1,
    build_stage_plan,
)
from test_outline_v3_semantic_execution import _executor


def _route_and_stage_plan() -> tuple[Any, ReachableProviderRoutePlan]:
    stage_plan = build_stage_plan(
        action="generate_outline",
        requested_stages=("outline",),
        validation_enabled=False,
    )
    route = ReachableProviderRoute(
        stage="outline",
        semantic_role="candidate_provider_generation",
        section_name="Outline_API",
        provider_family="test_provider",
        model="test-model",
        endpoint_type="chat_completions",
        api_base_host="example.test",
        resolved=True,
    )
    return route, ReachableProviderRoutePlan(
        action="generate_outline",
        stage_plan=stage_plan,
        routes=(route,),
    )


def _request_row(route: ReachableProviderRoute, *, retries: int | None, output: int = 1_200):
    profile = ProviderContextProfile.conservative(
        provider=route.provider_family,
        model=route.model,
        endpoint_type=route.endpoint_type,
        model_context_limit=32_000,
        max_output_tokens=4_000,
        reasoning_reserve=256,
        safety_margin=128,
    )
    return build_provider_request_plan_row_v1(
        stage_name="outline",
        request_id="retry-budget-test",
        source_builder="test request builder",
        route=route,
        request_payload={"task": "test only"},
        profile=profile,
        retry_attempts=retries,
        requested_output_tokens=output,
    )


def _plan(inventory: ProviderStageRequestInventoryV1, route_plan: ReachableProviderRoutePlan, cap: int):
    return build_full_stage_request_plan_v1(
        stage_plan=route_plan.stage_plan,
        reachable_route_plan=route_plan,
        stage_inventories=(inventory,),
        aggregate_budget=ProviderAggregateBudgetV1(
            max_provider_calls_total=24,
            max_output_tokens_total=cap,
        ),
    )


def test_output_cap_admission_includes_each_reserved_retry_attempt() -> None:
    route, route_plan = _route_and_stage_plan()
    row = _request_row(route, retries=2)
    inventory = ProviderStageRequestInventoryV1(
        stage_name="outline",
        source_builder="test request plan",
        requests=(row,),
    )

    plan = _plan(inventory, route_plan, cap=3_000)

    assert plan["totals"]["aggregate_output_tokens_reserved"] == 1_200
    assert plan["totals"]["estimated_output_tokens_all_attempts"] == 3_600
    assert plan["totals"]["physical_attempts_upper_bound"] == 3
    assert plan["budget_status"]["requested_output_tokens"] == "exceeded"
    assert plan["budget_status"]["admission"] == "blocked_budget"


def test_zero_retry_output_cap_uses_one_attempt_and_stays_within_limit() -> None:
    route, route_plan = _route_and_stage_plan()
    row = _request_row(route, retries=0)
    inventory = ProviderStageRequestInventoryV1(
        stage_name="outline",
        source_builder="test request plan",
        requests=(row,),
    )

    plan = _plan(inventory, route_plan, cap=1_200)

    assert plan["totals"]["aggregate_output_tokens_reserved"] == 1_200
    assert plan["totals"]["estimated_output_tokens_all_attempts"] == 1_200
    assert plan["totals"]["physical_attempts_upper_bound"] == 1
    assert plan["budget_status"]["requested_output_tokens"] == "within_limit"
    assert plan["budget_status"]["admission"] == "within_budget"


def test_unknown_retry_exposure_keeps_bounded_output_admission_unknown() -> None:
    route, route_plan = _route_and_stage_plan()
    exposure = UnplannedProviderExposureV1(
        stage_name="outline",
        semantic_role=route.semantic_role,
        reason="request attempts cannot yet be bounded",
        route_identity=route.identity,
        logical_calls_upper_bound=1,
        output_tokens_per_call_upper_bound=1_000,
        retry_attempts_per_call_upper_bound=None,
    )
    inventory = ProviderStageRequestInventoryV1(
        stage_name="outline",
        source_builder="test request plan",
        unknown_exposures=(exposure,),
    )

    plan = _plan(inventory, route_plan, cap=5_000)

    assert plan["totals"]["aggregate_output_tokens_reserved"] == 1_000
    assert plan["totals"]["estimated_output_tokens_all_attempts"] is None
    assert plan["totals"]["physical_attempts_upper_bound"] is None
    assert plan["budget_status"]["requested_output_tokens"] == "unknown"
    assert plan["budget_status"]["admission"] == "incomplete_unknown_exposure"


@pytest.mark.parametrize(("retries", "expected_attempts"), [(2, 3), (0, 1)])
def test_outline_provider_call_admits_retry_scaled_output_before_local_transport(
    tmp_path, monkeypatch: pytest.MonkeyPatch, retries: int, expected_attempts: int,
) -> None:
    requested_output = 1_200
    admissions: list[tuple[int, int, Any]] = []
    transport_calls: list[str] = []
    original_admit = ProviderRuntime.admit

    def capture_admission(self: ProviderRuntime, **kwargs: Any):
        admission = original_admit(self, **kwargs)
        admissions.append(
            (
                int(kwargs["requested_output_tokens"]),
                int(kwargs["requested_retry_attempts"]),
                admission,
            )
        )
        return admission

    monkeypatch.setattr(ProviderRuntime, "admit", capture_admission)
    monkeypatch.setattr(provider_runtime_module, "provider_budget_controller_from_environment", lambda: None)

    def offline_transport(node_id: str, _request: dict[str, Any]) -> dict[str, Any]:
        # This callable is an in-process spy; it never opens a socket or posts
        # to a provider. Reaching it proves admission has already completed.
        assert len(admissions) == 1
        reserved_output, reserved_retries, admission = admissions[0]
        assert reserved_output == requested_output * expected_attempts
        assert reserved_retries == retries
        assert admission.estimated_output_tokens == requested_output * expected_attempts
        assert admission.reserved_call_attempts == expected_attempts
        assert admission.reserved_retry_attempts == retries
        transport_calls.append(node_id)
        return {"status": "success", "content": {"accepted": True, "node_id": node_id}}

    executor = _executor(tmp_path, provider=offline_transport, stability_mode="off")
    profile = ProviderContextProfile.conservative(
        provider="offline_fixture",
        model="retry-admission-test",
        endpoint_type="fixture",
        model_context_limit=32_000,
        max_output_tokens=4_000,
        reasoning_reserve=256,
        safety_margin=128,
    )
    route = OutlineRoleRoute(
        role="candidate_provider_generation",
        config_section="Offline_Test_API",
        provider_name="offline_fixture",
        model="retry-admission-test",
        endpoint_type="fixture",
        profile=profile,
        transport=offline_transport,
        api_base="https://offline.example.test/v1",
        config_identity={"transport_retries": str(retries)},
    )
    executor.router = OutlineProviderRouter(
        routes={"candidate_provider_generation": route}
    )

    result = executor._provider_call(
        "candidate_1_provider_generation",
        {"task": "verify retry-scaled admission"},
        output_tokens=requested_output,
    )

    assert result["accepted"] is True
    assert transport_calls == ["candidate_1_provider_generation"]
    assert admissions[0][0] == requested_output * expected_attempts


def test_critique_dynamic_shard_reserves_token_estimates_for_each_retry(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executor = _executor(tmp_path, stability_mode="off")
    monkeypatch.setattr(executor, "_provider_node_ids", lambda: ("structure_critique",))
    (base_plan,) = executor._build_provider_call_plans()
    plan_row = replace(
        base_plan,
        estimated_input_tokens=101,
        estimated_output_tokens=77,
        estimated_reasoning_tokens=29,
        estimated_total_tokens=207,
        configured_transport_retry_reserve=2,
        physical_attempt_upper_bound=3,
    )
    monkeypatch.setattr(executor, "_build_provider_call_plans", lambda: (plan_row,))
    executor._critique_preflight_shards = {("canonical", "structure_critique"): 3}

    executor._preflight_stability_budget()

    profile = executor._role_route("structure_critique").profile
    extra_calls = 2
    attempts_per_extra_call = 3
    critique_input = executor._effective_input_cap(profile)
    critique_output = min(max(1, int(profile.max_output_tokens)), 2_048)
    critique_reasoning = max(0, int(profile.reasoning_reserve))
    expected_input = 101 * 3 + extra_calls * attempts_per_extra_call * critique_input
    expected_output = 77 * 3 + extra_calls * attempts_per_extra_call * critique_output
    expected_reasoning = 29 * 3 + extra_calls * attempts_per_extra_call * critique_reasoning
    expected_total = 207 * 3 + extra_calls * attempts_per_extra_call * (
        critique_input + critique_output + critique_reasoning
    )

    preflight = executor.stability_preflight
    assert preflight["estimated_provider_physical_attempts_upper_bound"] == 9
    assert preflight["hierarchical_critique_shard_calls"] == extra_calls
    assert preflight["estimated_input_tokens"] == expected_input
    assert preflight["estimated_output_tokens"] == expected_output
    assert preflight["estimated_reasoning_tokens"] == expected_reasoning
    assert preflight["estimated_total_tokens"] == expected_total
