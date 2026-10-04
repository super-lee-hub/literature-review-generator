from __future__ import annotations

from typing import Any

import pytest

from runtime.provider_context import ProviderContextProfile
from runtime.provider_routes import ReachableProviderRoute, ReachableProviderRoutePlan
from runtime.provider_runtime import ProviderAggregateBudgetV1, ProviderAggregateBudgetV2
from runtime.stage_planning import (
    ProviderStageRequestInventoryV1,
    UnplannedProviderExposureV1,
    build_full_stage_request_plan_v1,
    build_provider_request_plan_row_v1,
    build_stage_plan,
)


def _route_plan() -> tuple[Any, ReachableProviderRoutePlan]:
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
    route_plan = ReachableProviderRoutePlan(
        action="generate_outline",
        stage_plan=stage_plan,
        routes=(route,),
    )
    return route, route_plan


def _profile(route: Any) -> ProviderContextProfile:
    return ProviderContextProfile.conservative(
        provider=route.provider_family,
        model=route.model,
        endpoint_type=route.endpoint_type,
        model_context_limit=32_000,
        max_output_tokens=4_000,
        reasoning_reserve=256,
        safety_margin=128,
    )


def _heterogeneous_rows(
    route: Any,
    count: int = 33,
    retry_attempts: tuple[int, ...] | None = None,
    retry_policy: str = "configured_reserve",
    retry_policies: tuple[str, ...] | None = None,
):
    profile = _profile(route)
    if retry_attempts is not None and len(retry_attempts) != count:
        raise ValueError("retry_attempts must have one ceiling per request")
    if retry_policies is not None and len(retry_policies) != count:
        raise ValueError("retry_policies must have one policy per request")
    return tuple(
        build_provider_request_plan_row_v1(
            stage_name="outline",
            request_id=f"shared-retry-{index}",
            source_builder="test request builder with heterogeneous retry costs",
            route=route,
            request_payload={"task": "x" * (index * 37 + 1)},
            profile=profile,
            retry_attempts=2 if retry_attempts is None else retry_attempts[index],
            retry_policy=retry_policy if retry_policies is None else retry_policies[index],
            requested_output_tokens=100 + index * 11,
            reasoning_reserve_tokens=1 + index,
            pricing_per_1k_tokens={
                "input_cost_per_1k_tokens": 1.0,
                "output_cost_per_1k_tokens": 1.0,
                "reasoning_cost_per_1k_tokens": 1.0,
            },
            pricing_source="synthetic test rates",
            wall_seconds_upper_bound=30.0 + index * 2.0,
        )
        for index in range(count)
    )


def _plan(stage_plan: Any, route_plan: Any, inventory: Any, budget: Any):
    return build_full_stage_request_plan_v1(
        stage_plan=stage_plan,
        reachable_route_plan=route_plan,
        stage_inventories=(inventory,),
        aggregate_budget=budget,
    )


def _largest_retry_cost(rows: tuple[Any, ...], cost, retry_cap: int) -> int | float:
    per_attempt_costs = sorted(
        (
            cost(row)
            for row in rows
            for _ in range(int(row.retry_attempts or 0))
        ),
        reverse=True,
    )
    return sum(per_attempt_costs[:retry_cap])


@pytest.mark.parametrize("shared_retry_cap", [0, 6])
def test_v2_shared_retry_cap_bounds_33_known_requests_and_heterogeneous_attempt_costs(
    shared_retry_cap: int,
) -> None:
    route, route_plan = _route_plan()
    rows = _heterogeneous_rows(route)
    inventory = ProviderStageRequestInventoryV1(
        stage_name="outline",
        source_builder="test request plan",
        requests=rows,
    )
    plan = _plan(
        route_plan.stage_plan,
        route_plan,
        inventory,
        ProviderAggregateBudgetV2(
            max_provider_calls_total=200,
            max_output_tokens_total=1_000_000,
            max_retry_attempts_total=shared_retry_cap,
            max_wall_seconds=100_000.0,
        ),
    )

    expected_input_once = sum(
        row.request_estimate.estimated_input_tokens for row in rows
    )
    expected_output_once = sum(
        row.request_estimate.requested_output_tokens for row in rows
    )
    expected_reasoning_once = sum(
        row.request_estimate.reasoning_reserve_tokens for row in rows
    )
    assert plan["totals"]["logical_calls_known"] == 33
    assert plan["totals"]["physical_attempts_known_lower_bound"] == 33
    assert plan["totals"]["physical_attempts_upper_bound"] == 33 + shared_retry_cap
    assert plan["totals"]["retry_attempts_upper_bound"] == shared_retry_cap
    assert plan["totals"]["estimated_input_tokens_one_attempt"] == expected_input_once
    assert plan["totals"]["estimated_input_tokens_all_attempts"] == (
        expected_input_once
        + _largest_retry_cost(
            rows, lambda row: row.request_estimate.estimated_input_tokens, shared_retry_cap
        )
    )
    assert plan["totals"]["estimated_output_tokens_all_attempts"] == (
        expected_output_once
        + _largest_retry_cost(
            rows, lambda row: row.request_estimate.requested_output_tokens, shared_retry_cap
        )
    )
    assert plan["totals"]["estimated_reasoning_tokens_all_attempts"] == (
        expected_reasoning_once
        + _largest_retry_cost(
            rows, lambda row: row.request_estimate.reasoning_reserve_tokens, shared_retry_cap
        )
    )
    assert plan["totals"]["estimated_cost"] == pytest.approx(
        sum(float(row.estimated_cost) for row in rows)
        + _largest_retry_cost(
            rows, lambda row: row.estimated_cost, shared_retry_cap
        )
    )
    # Review rows carry a total logical-call deadline shared by their retries.
    assert plan["totals"]["wall_seconds_upper_bound"] == sum(
        row.wall_seconds_upper_bound for row in rows
    )
    # The configured per-row reservations still exceed this run-wide cap; the
    # projection reports that incompatibility without inventing extra attempts.
    assert plan["budget_status"]["provider_retries"] == "exceeded"
    assert plan["budget_status"]["admission"] == "blocked_budget"


@pytest.mark.parametrize(
    ("shared_retry_cap", "expected_admission"),
    [(0, "within_budget"), (6, "within_budget")],
)
def test_shared_optional_rows_use_shared_retry_cap_without_mandatory_reserve(
    shared_retry_cap: int,
    expected_admission: str,
) -> None:
    route, route_plan = _route_plan()
    rows = _heterogeneous_rows(route, retry_policy="shared_optional")
    inventory = ProviderStageRequestInventoryV1(
        stage_name="outline",
        source_builder="test optional retry request plan",
        requests=rows,
    )
    plan = _plan(
        route_plan.stage_plan,
        route_plan,
        inventory,
        ProviderAggregateBudgetV2(
            max_provider_calls_total=200,
            max_output_tokens_total=1_000_000,
            max_retry_attempts_total=shared_retry_cap,
            max_wall_seconds=100_000.0,
        ),
    )

    assert plan["totals"]["logical_calls_known"] == 33
    assert plan["totals"]["physical_attempts_known_lower_bound"] == 33
    assert plan["totals"]["retry_attempts_configured_upper_bound"] == 66
    assert plan["totals"]["retry_attempts_required_reserve"] == 0
    assert plan["totals"]["retry_attempts_upper_bound"] == shared_retry_cap
    assert plan["totals"]["physical_attempts_upper_bound"] == 33 + shared_retry_cap
    assert plan["budget_status"]["provider_retries"] == "within_limit"
    assert plan["budget_status"]["admission"] == expected_admission
    assert plan["provider_requests"][0]["retry_attempts"] == 2
    assert plan["provider_requests"][0]["retry_policy"] == "shared_optional"
    assert plan["provider_requests"][0]["retry_attempts_required_reserve"] == 0


@pytest.mark.parametrize(
    ("shared_retry_cap", "expected_retry_status", "expected_admission"),
    [
        (1, "exceeded", "blocked_budget"),
        (2, "within_limit", "within_budget"),
    ],
)
def test_mixed_retry_policies_preserve_mandatory_reserve_floor(
    shared_retry_cap: int,
    expected_retry_status: str,
    expected_admission: str,
) -> None:
    route, route_plan = _route_plan()
    rows = _heterogeneous_rows(
        route,
        count=2,
        retry_attempts=(2, 2),
        retry_policies=("configured_reserve", "shared_optional"),
    )
    inventory = ProviderStageRequestInventoryV1(
        stage_name="outline",
        source_builder="test mixed retry policy request plan",
        requests=rows,
    )
    plan = _plan(
        route_plan.stage_plan,
        route_plan,
        inventory,
        ProviderAggregateBudgetV2(
            max_provider_calls_total=20,
            max_output_tokens_total=100_000,
            max_retry_attempts_total=shared_retry_cap,
            max_wall_seconds=1_000.0,
        ),
    )

    assert plan["totals"]["retry_attempts_configured_upper_bound"] == 4
    assert plan["totals"]["retry_attempts_required_reserve"] == 2
    assert plan["totals"]["retry_attempts_upper_bound"] == shared_retry_cap
    assert plan["totals"]["physical_attempts_upper_bound"] == 2 + shared_retry_cap
    assert plan["budget_status"]["provider_retries"] == expected_retry_status
    assert plan["budget_status"]["admission"] == expected_admission


def test_v1_positive_retry_limit_is_shared() -> None:
    route, route_plan = _route_plan()
    rows = _heterogeneous_rows(route)
    inventory = ProviderStageRequestInventoryV1(
        stage_name="outline",
        source_builder="test request plan",
        requests=rows,
    )
    plan = _plan(
        route_plan.stage_plan,
        route_plan,
        inventory,
        ProviderAggregateBudgetV1(
            max_provider_calls_total=200,
            max_output_tokens_total=1_000_000,
            max_retry_attempts_total=6,
            max_wall_seconds=100_000.0,
        ),
    )

    expected_input_once = sum(
        row.request_estimate.estimated_input_tokens for row in rows
    )
    assert plan["totals"]["retry_attempts_upper_bound"] == 6
    assert plan["totals"]["physical_attempts_upper_bound"] == 39
    assert plan["totals"]["estimated_input_tokens_all_attempts"] == (
        expected_input_once
        + _largest_retry_cost(
            rows, lambda row: row.request_estimate.estimated_input_tokens, 6
        )
    )


def test_v2_shared_retry_cap_bounds_unknown_retry_count_without_filling_unknown_usage() -> None:
    route, route_plan = _route_plan()
    exposure = UnplannedProviderExposureV1(
        stage_name="outline",
        semantic_role=route.semantic_role,
        reason="retry ceiling is unknown but shared budget is finite",
        route_identity=route.identity,
        logical_calls_upper_bound=1,
        input_tokens_per_call_upper_bound=75,
        output_tokens_per_call_upper_bound=None,
        reasoning_tokens_per_call_upper_bound=25,
        retry_attempts_per_call_upper_bound=None,
        wall_seconds_per_call_upper_bound=20.0,
    )
    inventory = ProviderStageRequestInventoryV1(
        stage_name="outline",
        source_builder="test request plan",
        unknown_exposures=(exposure,),
    )

    plan = _plan(
        route_plan.stage_plan,
        route_plan,
        inventory,
        ProviderAggregateBudgetV2(
            max_provider_calls_total=20,
            max_output_tokens_total=1_000,
            max_retry_attempts_total=6,
            max_wall_seconds=200.0,
        ),
    )

    assert plan["totals"]["logical_calls_upper_bound"] == 1
    assert plan["totals"]["retry_attempts_upper_bound"] == 6
    assert plan["totals"]["physical_attempts_upper_bound"] == 7
    assert plan["totals"]["estimated_input_tokens_all_attempts"] == 75 * 7
    assert plan["totals"]["estimated_output_tokens_all_attempts"] is None
    assert plan["totals"]["estimated_reasoning_tokens_all_attempts"] == 25 * 7
    assert plan["totals"]["wall_seconds_upper_bound"] == 20.0
    assert plan["budget_status"]["requested_output_tokens"] == "unknown"
    assert plan["budget_status"]["admission"] == "incomplete_unknown_exposure"


def test_v1_zero_retry_limit_remains_legacy_unbounded() -> None:
    route, route_plan = _route_plan()
    rows = _heterogeneous_rows(route)
    inventory = ProviderStageRequestInventoryV1(
        stage_name="outline",
        source_builder="test request plan",
        requests=rows,
    )
    plan = _plan(
        route_plan.stage_plan,
        route_plan,
        inventory,
        ProviderAggregateBudgetV1(
            max_provider_calls_total=200,
            max_output_tokens_total=1_000_000,
            max_retry_attempts_total=0,
        ),
    )

    assert plan["totals"]["logical_calls_known"] == 33
    assert plan["totals"]["retry_attempts_upper_bound"] == 66
    assert plan["totals"]["physical_attempts_upper_bound"] == 99
    assert plan["budget_status"]["provider_retries"] == "unbounded"
    assert plan["totals"]["estimated_input_tokens_all_attempts"] == sum(
        row.request_estimate.estimated_input_tokens * 3 for row in rows
    )
    assert plan["totals"]["estimated_output_tokens_all_attempts"] == sum(
        row.request_estimate.requested_output_tokens * 3 for row in rows
    )



def test_v2_shared_retry_cap_respects_each_requests_retry_ceiling() -> None:
    route, route_plan = _route_plan()
    rows = _heterogeneous_rows(
        route,
        count=4,
        retry_attempts=(0, 1, 3, 1),
    )
    inventory = ProviderStageRequestInventoryV1(
        stage_name="outline",
        source_builder="test request plan",
        requests=rows,
    )
    plan = _plan(
        route_plan.stage_plan,
        route_plan,
        inventory,
        ProviderAggregateBudgetV2(
            max_provider_calls_total=20,
            max_output_tokens_total=100_000,
            max_retry_attempts_total=3,
            max_wall_seconds=1_000.0,
        ),
    )

    expected_output_once = sum(
        row.request_estimate.requested_output_tokens for row in rows
    )
    assert plan["totals"]["physical_attempts_known_lower_bound"] == 4
    assert plan["totals"]["retry_attempts_upper_bound"] == 3
    assert plan["totals"]["physical_attempts_upper_bound"] == 7
    assert plan["totals"]["estimated_output_tokens_all_attempts"] == (
        expected_output_once
        + _largest_retry_cost(
            rows, lambda row: row.request_estimate.requested_output_tokens, 3
        )
    )


