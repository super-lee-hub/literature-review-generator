from __future__ import annotations

from typing import Any

import pytest

from runtime.provider_context import ProviderContextProfile
from runtime.provider_routes import ReachableProviderRoute, ReachableProviderRoutePlan
from runtime.provider_runtime import ProviderAggregateBudgetV2, hash_json
from runtime.stage_planning import (
    ProviderExposureCardinalityBasisV1,
    ProviderStageRequestInventoryV1,
    StagePlanError,
    UnplannedProviderExposureV1,
    build_full_stage_request_plan_v1,
    build_provider_request_plan_row_v1,
    build_stage_plan,
)


def _route_plan() -> tuple[ReachableProviderRoute, ReachableProviderRoutePlan]:
    stage_plan = build_stage_plan(
        action="generate_outline",
        requested_stages=("outline",),
        validation_enabled=False,
    )
    route = ReachableProviderRoute(
        stage="outline",
        semantic_role="candidate_provider_generation",
        section_name="Outline_API",
        enabled=True,
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


def _profile(route: ReachableProviderRoute) -> ProviderContextProfile:
    return ProviderContextProfile.conservative(
        provider=route.provider_family,
        model=route.model,
        endpoint_type=route.endpoint_type,
        model_context_limit=32_000,
        max_output_tokens=1_000,
        reasoning_reserve=100,
        safety_margin=100,
    )


def _row(
    route: ReachableProviderRoute,
    request_id: str,
    *,
    output: int = 100,
    retries: int | None = 0,
    wall: float = 2.0,
    payload_text: str = "same scoped request",
    path: str = "",
    group: str = "",
    retry_policy: str = "configured_reserve",
    pricing: bool = True,
):
    return build_provider_request_plan_row_v1(
        stage_name="outline",
        request_id=request_id,
        source_builder="synthetic conditional-path request builder",
        route=route,
        request_payload={"task": payload_text},
        profile=_profile(route),
        retry_attempts=retries,
        requested_output_tokens=output,
        reasoning_reserve_tokens=20,
        conditional_on="selected reachable branch" if path or group else "",
        conditional_path_id=path,
        alternative_group_id=group,
        pricing_per_1k_tokens=(
            {
                "input_cost_per_1k_tokens": 1.0,
                "output_cost_per_1k_tokens": 2.0,
                "reasoning_cost_per_1k_tokens": 3.0,
            }
            if pricing
            else None
        ),
        pricing_source="synthetic-test-rates" if pricing else "",
        wall_seconds_upper_bound=wall,
        retry_policy=retry_policy,  # type: ignore[arg-type]
    )


def _exposure(
    route: ReachableProviderRoute,
    *,
    path: str,
    group: str,
    count: int | None,
    output: int = 100,
    retries: int | None = 0,
    wall: float | None = 2.0,
    input_tokens: int | None = 10,
) -> UnplannedProviderExposureV1:
    basis = (
        ProviderExposureCardinalityBasisV1(
            basis_artifact="artifacts/conditional-path-basis.json",
            basis_artifact_sha256="a" * 64,
            maximum_count=count,
            derivation_rule="one bounded request per declared work item",
            output_schema="conditional-path-request/v1",
        )
        if count is not None
        else None
    )
    return UnplannedProviderExposureV1(
        stage_name="outline",
        semantic_role=route.semantic_role,
        reason="branch request follows its source-bound decision",
        route_identity=route.identity,
        conditional_on="selected reachable branch",
        logical_calls_upper_bound=count,
        input_tokens_per_call_upper_bound=input_tokens,
        output_tokens_per_call_upper_bound=output,
        reasoning_tokens_per_call_upper_bound=5,
        context_tokens_per_call_upper_bound=(
            None if input_tokens is None else input_tokens + output + 5
        ),
        context_limit_tokens_per_call=1_000 if input_tokens is not None else None,
        retry_attempts_per_call_upper_bound=retries,
        wall_seconds_per_call_upper_bound=wall,
        request_builder_id="tests.conditional_path.exposure",
        exposure_status="bounded_conditional" if count is not None else "unbounded",
        cardinality_basis=basis,
        conditional_path_id=path,
        alternative_group_id=group,
    )


def _plan(
    route_plan: ReachableProviderRoutePlan,
    rows: tuple[Any, ...],
    exposures: tuple[UnplannedProviderExposureV1, ...] = (),
    *,
    calls: int = 100,
    output: int = 100_000,
    retries: int = 100,
    wall: float = 10_000.0,
) -> dict[str, Any]:
    return build_full_stage_request_plan_v1(
        stage_plan=route_plan.stage_plan,
        reachable_route_plan=route_plan,
        stage_inventories=(
            ProviderStageRequestInventoryV1(
                stage_name="outline",
                source_builder="synthetic test inventory",
                requests=rows,
                unknown_exposures=exposures,
            ),
        ),
        aggregate_budget=ProviderAggregateBudgetV2(
            max_provider_calls_total=calls,
            max_output_tokens_total=output,
            max_retry_attempts_total=retries,
            max_wall_seconds=wall,
        ),
    )


def test_path_tags_change_only_projection_identity_and_are_omitted_when_absent() -> None:
    route, route_plan = _route_plan()
    untagged = _row(route, "shared-decision")
    first = _row(route, "shared-decision", path="direct", group="decision")
    second = _row(route, "shared-decision", path="overflow", group="decision")

    assert untagged.request_key_hash == hash_json(
        {"stage": "outline", "request_id": "shared-decision"}
    )
    assert "conditional_path_id" not in untagged.to_dict()
    assert "alternative_group_id" not in untagged.to_dict()
    assert first.request_key_hash != second.request_key_hash
    assert first.request_estimate.request_hash == second.request_estimate.request_hash
    assert first.to_dict()["conditional_path_id"] == "direct"
    assert second.to_dict()["alternative_group_id"] == "decision"

    plan = _plan(route_plan, ())
    assert "conditional_paths_are_projection_only" not in plan["boundary"]


def test_exclusive_alternatives_count_common_rows_once_and_sum_each_path() -> None:
    route, route_plan = _route_plan()
    direct = [
        _row(route, "decision", output=40, retries=1, wall=2, payload_text="common", path="direct", group="decision"),
        _row(route, "direct-only", output=60, retries=0, wall=3, payload_text="direct", path="direct", group="decision"),
    ]
    overflow = [
        _row(route, "decision", output=40, retries=1, wall=2, payload_text="common", path="overflow", group="decision"),
        _row(route, "overflow-one", output=100, retries=2, wall=4, payload_text="overflow-one", path="overflow", group="decision"),
        _row(route, "overflow-two", output=50, retries=1, wall=5, payload_text="overflow-two", path="overflow", group="decision"),
    ]
    rows = tuple(direct + overflow)
    plan = _plan(route_plan, rows, retries=2)
    totals = plan["totals"]

    assert totals["logical_calls_known"] == 3
    assert totals["logical_calls_conditional"] == 3
    assert totals["logical_calls_conditional_upper_bound"] == 3
    assert totals["logical_calls_upper_bound"] == 3
    assert totals["logical_calls_known_lower_bound"] == 2
    assert totals["physical_attempts_known_lower_bound"] == 2
    assert totals["physical_attempts_upper_bound"] == 5
    assert totals["aggregate_output_tokens_reserved"] == 190
    assert totals["aggregate_output_tokens_known_lower_bound"] == 100
    assert totals["retry_attempts_configured_upper_bound"] == 4
    assert totals["retry_attempts_required_reserve"] == 1
    assert totals["retry_attempts_possible_upper_bound"] == 2
    assert totals["wall_seconds_upper_bound"] == 11.0
    assert plan["budget_status"]["admission"] == "conditional_within_budget"
    assert plan["boundary"]["exclusive_alternatives_use_minimum_floors_and_maximum_uppers"] is True
    assert plan["boundary"]["conditional_paths_are_projection_only"] is True

    expected_input_once = max(
        sum(row.request_estimate.estimated_input_tokens for row in direct),
        sum(row.request_estimate.estimated_input_tokens for row in overflow),
    )
    expected_reasoning_once = max(
        sum(row.request_estimate.reasoning_reserve_tokens for row in direct),
        sum(row.request_estimate.reasoning_reserve_tokens for row in overflow),
    )
    expected_context_once = max(
        sum(row.request_estimate.estimated_total_tokens for row in direct),
        sum(row.request_estimate.estimated_total_tokens for row in overflow),
    )
    expected_cost_once = max(
        sum(float(row.estimated_cost or 0.0) for row in direct),
        sum(float(row.estimated_cost or 0.0) for row in overflow),
    )
    max_retries_on_path = 4
    retry_slots_used = min(max_retries_on_path, 2)
    assert totals["estimated_input_tokens_one_attempt"] == expected_input_once
    assert totals["estimated_input_tokens_all_attempts"] == expected_input_once + max(
        row.request_estimate.estimated_input_tokens for row in rows
    ) * retry_slots_used
    assert totals["estimated_reasoning_tokens_all_attempts"] == expected_reasoning_once + 20 * retry_slots_used
    assert totals["estimated_context_tokens_one_attempt"] == expected_context_once
    assert totals["estimated_context_tokens_all_attempts"] == expected_context_once + max(
        row.request_estimate.estimated_total_tokens for row in rows
    ) * retry_slots_used
    assert totals["estimated_output_tokens_all_attempts"] == 190 + 100 * retry_slots_used
    assert totals["estimated_cost"] == pytest.approx(
        expected_cost_once
        + max(float(row.estimated_cost or 0.0) for row in rows) * retry_slots_used
    )


def test_independent_groups_cooccur_and_zero_exposure_keeps_alternative_floor() -> None:
    route, route_plan = _route_plan()
    rows = (
        _row(route, "g1a", output=10, path="one", group="g1"),
        _row(route, "g1b1", output=5, path="two", group="g1"),
        _row(route, "g1b2", output=5, path="two", group="g1"),
    )
    exposures = (
        _exposure(route, path="zero", group="g2", count=0, output=20),
        _exposure(route, path="three", group="g2", count=3, output=20),
    )

    plan = _plan(route_plan, rows, exposures, retries=0)
    totals = plan["totals"]

    assert totals["logical_calls_known"] == 2
    assert totals["logical_calls_upper_bound"] == 5
    assert totals["logical_calls_known_lower_bound"] == 1
    assert totals["logical_calls_conditional_upper_bound"] == 5
    assert totals["aggregate_output_tokens_reserved"] == 70
    assert totals["aggregate_output_tokens_known_lower_bound"] == 10
    assert totals["wall_seconds_upper_bound"] == 10.0
    assert totals["bounded_logical_call_exposure_count"] == 1
    assert plan["unknown_exposures"][0]["conditional_path_id"] == "zero"
    assert plan["unknown_exposures"][1]["alternative_group_id"] == "g2"
    assert plan["boundary"]["independent_alternative_groups_cooccur"] is True


def test_shared_retries_are_one_global_pool_across_independent_path_groups() -> None:
    route, route_plan = _route_plan()
    rows = (
        _row(route, "cheap", output=10, retries=2, path="selected", group="g1", retry_policy="shared_optional"),
        _row(route, "expensive", output=100, retries=3, path="selected", group="g2", retry_policy="shared_optional"),
    )

    plan = _plan(route_plan, rows, retries=2)
    totals = plan["totals"]

    assert totals["retry_attempts_configured_upper_bound"] == 5
    assert totals["retry_attempts_required_reserve"] == 0
    assert totals["retry_attempts_possible_upper_bound"] == 2
    assert totals["aggregate_output_tokens_reserved"] == 110
    assert totals["estimated_output_tokens_all_attempts"] == 310
    assert plan["boundary"]["shared_retry_cap_remains_global_across_paths"] is True


def test_underfunded_path_upper_still_blocks_even_when_known_floor_fits() -> None:
    route, route_plan = _route_plan()
    rows = (
        _row(route, "small", output=10, path="small", group="g"),
        _row(route, "large-1", output=10, path="large", group="g"),
        _row(route, "large-2", output=10, path="large", group="g"),
        _row(route, "large-3", output=10, path="large", group="g"),
    )

    plan = _plan(route_plan, rows, calls=3, retries=0)

    assert plan["totals"]["logical_calls_known_lower_bound"] == 1
    assert plan["totals"]["logical_calls_upper_bound"] == 3
    assert plan["budget_status"]["provider_calls"] == "within_limit"
    assert plan["budget_status"]["admission"] == "conditional_within_budget"

    rejected = _plan(route_plan, rows, calls=2, retries=0)
    assert rejected["budget_status"]["provider_calls"] == "exceeded"
    assert rejected["budget_status"]["admission"] == "blocked_budget"
    assert rejected["ready_for_transport"] is False


def test_unbounded_path_exposure_stays_unknown_and_not_transport_ready() -> None:
    route, route_plan = _route_plan()
    exposure = _exposure(
        route,
        path="overflow",
        group="decision",
        count=None,
        input_tokens=None,
        wall=None,
    )

    plan = _plan(route_plan, (), (exposure,))

    assert plan["totals"]["logical_calls_upper_bound"] is None
    assert plan["totals"]["estimated_input_tokens_one_attempt"] is None
    assert plan["totals"]["wall_seconds_upper_bound"] is None
    assert plan["budget_status"]["admission"] == "incomplete_unknown_exposure"
    assert plan["ready_for_transport"] is False


@pytest.mark.parametrize(
    ("path", "group", "conditional_on"),
    [
        ("path-only", "", "selected"),
        ("", "group-only", "selected"),
        (" ", " ", "selected"),
        ("path", "group", ""),
    ],
)
def test_builder_rejects_incomplete_conditional_path_tags(
    path: str,
    group: str,
    conditional_on: str,
) -> None:
    route, _route_plan_value = _route_plan()
    with pytest.raises(StagePlanError):
        build_provider_request_plan_row_v1(
            stage_name="outline",
            request_id="bad-path-tags",
            source_builder="test",
            route=route,
            request_payload={"task": "test"},
            profile=_profile(route),
            retry_attempts=0,
            conditional_on=conditional_on,
            conditional_path_id=path,
            alternative_group_id=group,
        )


def test_exposure_rejects_incomplete_conditional_path_tag_pair() -> None:
    route, _route_plan_value = _route_plan()
    with pytest.raises(StagePlanError, match="provided together"):
        _exposure(route, path="path-only", group="", count=1)
