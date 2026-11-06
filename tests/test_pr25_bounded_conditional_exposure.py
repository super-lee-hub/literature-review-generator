from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from runtime.provider_routes import ReachableProviderRoute, ReachableProviderRoutePlan
from runtime.provider_context import ProviderContextProfile
from runtime.provider_runtime import ProviderAggregateBudgetV2
from runtime.stage_planning import (
    ProviderExposureCardinalityBasisV1,
    ProviderStageRequestInventoryV1,
    StagePlanError,
    UnplannedProviderExposureV1,
    build_full_stage_request_plan_v1,
    build_provider_request_plan_row_v1,
    build_stage_plan,
)


def _route_plan(*, enabled: bool = True) -> tuple[ReachableProviderRoute, ReachableProviderRoutePlan]:
    stage_plan = build_stage_plan(
        action="generate_outline",
        requested_stages=("outline",),
        validation_enabled=False,
    )
    route = ReachableProviderRoute(
        stage="outline",
        semantic_role="candidate_provider_generation",
        section_name="Outline_API",
        enabled=enabled,
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


def _basis(*, maximum_count: int = 2) -> ProviderExposureCardinalityBasisV1:
    return ProviderExposureCardinalityBasisV1(
        basis_artifact="artifacts/conditional-branch-basis.json",
        basis_artifact_sha256="a" * 64,
        maximum_count=maximum_count,
        derivation_rule="one repair request per eligible finding, capped at two",
        output_schema="outline-repair-request/v1",
    )


def _exposure(
    route: ReachableProviderRoute,
    *,
    maximum_count: int = 2,
    retries: int = 1,
    basis: ProviderExposureCardinalityBasisV1 | None = None,
    **overrides: Any,
) -> UnplannedProviderExposureV1:
    fields: dict[str, Any] = {
        "stage_name": "outline",
        "semantic_role": route.semantic_role,
        "reason": "conditional repair requests follow a failed candidate",
        "route_identity": route.identity,
        "conditional_on": "candidate_failed_and_repair_is_required",
        "logical_calls_upper_bound": maximum_count,
        "input_tokens_per_call_upper_bound": 20,
        "output_tokens_per_call_upper_bound": 100,
        "reasoning_tokens_per_call_upper_bound": 10,
        "context_tokens_per_call_upper_bound": 140,
        "context_limit_tokens_per_call": 1_000,
        "retry_attempts_per_call_upper_bound": retries,
        "wall_seconds_per_call_upper_bound": 5.0,
        "request_builder_id": "outline.v3_executor.repair_request",
        "exposure_status": "bounded_conditional",
        "cardinality_basis": basis or _basis(maximum_count=maximum_count),
    }
    fields.update(overrides)
    return UnplannedProviderExposureV1(**fields)


def _plan(
    route_plan: ReachableProviderRoutePlan,
    exposures: tuple[UnplannedProviderExposureV1, ...],
    *,
    retry_cap: int,
) -> dict[str, Any]:
    return build_full_stage_request_plan_v1(
        stage_plan=route_plan.stage_plan,
        reachable_route_plan=route_plan,
        stage_inventories=(
            ProviderStageRequestInventoryV1(
                stage_name="outline",
                source_builder="outline conditional request projection",
                unknown_exposures=exposures,
            ),
        ),
        aggregate_budget=ProviderAggregateBudgetV2(
            max_provider_calls_total=10,
            max_output_tokens_total=1_000,
            max_retry_attempts_total=retry_cap,
            max_wall_seconds=100.0,
        ),
    )


def _plan_inventory(
    route_plan: ReachableProviderRoutePlan,
    inventory: ProviderStageRequestInventoryV1,
    *,
    retry_cap: int,
) -> dict[str, Any]:
    return build_full_stage_request_plan_v1(
        stage_plan=route_plan.stage_plan,
        reachable_route_plan=route_plan,
        stage_inventories=(inventory,),
        aggregate_budget=ProviderAggregateBudgetV2(
            max_provider_calls_total=10,
            max_output_tokens_total=1_000,
            max_retry_attempts_total=retry_cap,
            max_wall_seconds=100.0,
        ),
    )


def _request_row(
    route: ReachableProviderRoute,
    *,
    retries: int | None,
    wall_seconds: float | None,
):
    profile = ProviderContextProfile.conservative(
        provider=route.provider_family,
        model=route.model,
        endpoint_type=route.endpoint_type,
        model_context_limit=32_000,
        max_output_tokens=1_000,
        reasoning_reserve=100,
        safety_margin=100,
    )
    return build_provider_request_plan_row_v1(
        stage_name="outline",
        request_id="bounded-exposure-exact-row",
        source_builder="test exact request builder",
        route=route,
        request_payload={"task": "test"},
        profile=profile,
        retry_attempts=retries,
        requested_output_tokens=100,
        reasoning_reserve_tokens=10,
        wall_seconds_upper_bound=wall_seconds,
    )


@pytest.mark.parametrize(
    ("retry_cap", "expected_attempts", "expected_output", "expected_context"),
    [
        (0, 2, 200, 280),
        (1, 3, 300, 420),
    ],
)
def test_bounded_conditional_envelope_projects_shared_retry_and_context_totals(
    retry_cap: int,
    expected_attempts: int,
    expected_output: int,
    expected_context: int,
) -> None:
    route, route_plan = _route_plan()

    plan = _plan(route_plan, (_exposure(route),), retry_cap=retry_cap)

    totals = plan["totals"]
    assert totals["logical_calls_upper_bound"] == 2
    assert totals["logical_calls_conditional_upper_bound"] == 2
    assert totals["retry_attempts_upper_bound"] == retry_cap
    assert totals["physical_attempts_upper_bound"] == expected_attempts
    assert totals["estimated_input_tokens_all_attempts"] == 20 * expected_attempts
    assert totals["estimated_output_tokens_all_attempts"] == expected_output
    assert totals["estimated_reasoning_tokens_all_attempts"] == 10 * expected_attempts
    assert totals["estimated_context_tokens_one_attempt"] == 280
    assert totals["estimated_context_tokens_all_attempts"] == expected_context
    assert totals["wall_seconds_upper_bound"] == 10.0
    assert plan["budget_status"]["per_request_context"] == "within_limit"
    assert plan["budget_status"]["admission"] == "conditional_within_budget"
    assert plan["envelope_complete"] is True
    assert plan["exact_requests_materialized"] is False
    assert plan["ready_for_transport"] is False


def test_multiple_bounded_exposures_sum_conditional_cardinality_bounds() -> None:
    route, route_plan = _route_plan()
    first = _exposure(route, maximum_count=2)
    second = replace(
        _exposure(route, maximum_count=3),
        conditional_on="second_repair_condition",
        cardinality_basis=_basis(maximum_count=3),
    )

    plan = _plan(route_plan, (first, second), retry_cap=0)

    assert plan["totals"]["logical_calls_conditional_upper_bound"] == 5
    assert plan["totals"]["logical_calls_upper_bound"] == 5
    assert plan["totals"]["estimated_output_tokens_all_attempts"] == 500
    assert plan["totals"]["bounded_logical_call_exposure_count"] == 2
    assert plan["totals"]["unbounded_logical_call_exposure_count"] == 0


def test_numeric_only_legacy_exposure_stays_unknown_and_not_transport_ready() -> None:
    route, route_plan = _route_plan()
    legacy = UnplannedProviderExposureV1(
        stage_name="outline",
        semantic_role=route.semantic_role,
        reason="legacy numeric bounds have no bound cardinality authority",
        route_identity=route.identity,
        conditional_on="candidate_failed",
        logical_calls_upper_bound=2,
        input_tokens_per_call_upper_bound=20,
        output_tokens_per_call_upper_bound=100,
        reasoning_tokens_per_call_upper_bound=10,
        retry_attempts_per_call_upper_bound=0,
        wall_seconds_per_call_upper_bound=5.0,
    )

    plan = _plan(route_plan, (legacy,), retry_cap=0)

    assert plan["envelope_complete"] is False
    assert plan["exact_requests_materialized"] is False
    assert plan["ready_for_transport"] is False
    assert plan["budget_status"]["admission"] == "incomplete_unknown_exposure"
    assert plan["totals"]["logical_calls_upper_bound"] == 2
    assert plan["totals"]["estimated_output_tokens_all_attempts"] == 200
    assert plan["totals"]["unbounded_logical_call_exposure_count"] == 1


@pytest.mark.parametrize(
    "overrides",
    [
        {"input_tokens_per_call_upper_bound": None},
        {"output_tokens_per_call_upper_bound": None},
        {"reasoning_tokens_per_call_upper_bound": None},
        {"context_tokens_per_call_upper_bound": None},
        {"context_limit_tokens_per_call": None},
        {"retry_attempts_per_call_upper_bound": None},
        {"wall_seconds_per_call_upper_bound": None},
        {"route_identity": ()},
    ],
)
def test_bounded_status_rejects_missing_per_call_or_route_dimension(
    overrides: dict[str, Any],
) -> None:
    route, _ = _route_plan()

    with pytest.raises(StagePlanError):
        _exposure(route, **overrides)


@pytest.mark.parametrize(
    "overrides",
    [
        {"input_tokens_per_call_upper_bound": "20"},
        {"output_tokens_per_call_upper_bound": 100.5},
        {"context_tokens_per_call_upper_bound": True},
        {"context_limit_tokens_per_call": 1.5},
        {"retry_attempts_per_call_upper_bound": 1.0},
    ],
)
def test_bounded_status_rejects_non_integer_dimensions(overrides: dict[str, Any]) -> None:
    route, _ = _route_plan()

    with pytest.raises(StagePlanError):
        _exposure(route, **overrides)


@pytest.mark.parametrize(
    ("retries", "wall_seconds", "missing_input", "expected_admission"),
    [
        (None, 10.0, False, "incomplete_unknown_exposure"),
        (0, None, False, "incomplete_unknown_exposure"),
        (0, 10.0, True, "incomplete_unknown_exposure"),
    ],
)
def test_exact_request_without_finite_retry_wall_or_input_is_not_envelope_complete(
    retries: int | None,
    wall_seconds: float | None,
    missing_input: bool,
    expected_admission: str,
) -> None:
    route, route_plan = _route_plan()
    row = _request_row(route, retries=retries, wall_seconds=wall_seconds)
    if missing_input:
        row = replace(
            row,
            request_estimate=replace(row.request_estimate, estimated_input_tokens=None),
        )
    inventory = ProviderStageRequestInventoryV1(
        stage_name="outline",
        source_builder="test exact request plan",
        requests=(row,),
    )

    plan = _plan_inventory(route_plan, inventory, retry_cap=0)

    assert plan["envelope_complete"] is False
    assert plan["exact_requests_materialized"] is True
    assert plan["ready_for_transport"] is False
    assert plan["budget_status"]["admission"] == expected_admission


@pytest.mark.parametrize(
    "basis_overrides",
    [
        {"basis_artifact_sha256": ""},
        {"basis_artifact_sha256": "not-a-sha256"},
        {"output_schema": ""},
        {"schema_version": "provider-exposure-cardinality-basis-v2"},
    ],
)
def test_bounded_status_rejects_missing_or_unsupported_basis_identity(
    basis_overrides: dict[str, Any],
) -> None:
    route, _ = _route_plan()

    with pytest.raises(StagePlanError):
        basis = replace(_basis(), **basis_overrides)
        _exposure(route, basis=basis)


def test_bounded_status_rejects_basis_count_mismatch() -> None:
    route, _ = _route_plan()

    with pytest.raises(StagePlanError):
        _exposure(route, maximum_count=2, basis=_basis(maximum_count=3))


def test_bounded_status_requires_cardinality_basis() -> None:
    route, _ = _route_plan()

    with pytest.raises(StagePlanError, match="versioned cardinality basis"):
        _exposure(route, cardinality_basis=None)


@pytest.mark.parametrize("missing_field", ["schema_version", "basis_artifact_sha256"])
def test_cardinality_basis_mapping_requires_version_and_hash(missing_field: str) -> None:
    payload = _basis().to_dict()
    payload.pop(missing_field)

    with pytest.raises(StagePlanError, match="fields are incomplete"):
        ProviderExposureCardinalityBasisV1.from_mapping(payload)


def test_bounded_status_rejects_disabled_route() -> None:
    route, route_plan = _route_plan(enabled=False)

    with pytest.raises(StagePlanError, match="disabled or unresolved route"):
        _plan(route_plan, (_exposure(route),), retry_cap=0)
