from __future__ import annotations

from dataclasses import replace

import pytest

import outline.candidate_repair_plan as candidate_repair_plan_module
from outline.candidate_repair_plan import (
    MAX_OUTLINE_CANDIDATE_COUNT,
    SEMANTIC_REPAIR_OUTPUT_SCHEMA_V1,
    SEMANTIC_REPAIR_RULES_V1,
    SEMANTIC_REPAIR_TASK_V1,
    build_primary_candidate_repair_plan_v1,
)
from runtime.provider_context import ProviderContextProfile
from runtime.provider_routes import ReachableProviderRoute, ReachableProviderRoutePlan
from runtime.provider_runtime import ProviderAggregateBudgetV2, hash_json
from runtime.stage_planning import (
    ProviderStageRequestInventoryV1,
    StagePlanError,
    build_full_stage_request_plan_v1,
    build_stage_plan,
)


def _profile() -> ProviderContextProfile:
    return ProviderContextProfile.conservative(
        provider="fixture-provider",
        model="outline-model",
        endpoint_type="chat_completions",
        model_context_limit=32_000,
        max_output_tokens=1_024,
        reasoning_reserve=128,
        safety_margin=256,
    )


def _route() -> ReachableProviderRoute:
    return ReachableProviderRoute(
        stage="outline",
        semantic_role="candidate_provider_generation",
        section_name="Outline_API",
        provider_family="fixture-provider",
        model="outline-model",
        endpoint_type="chat_completions",
        api_base_host="provider.example",
        resolved=True,
    )


def _route_fingerprint(route: ReachableProviderRoute | None = None) -> str:
    resolved = route or _route()
    return hash_json(
        {"route_identity": list(resolved.identity), "section_name": resolved.section_name}
    )


def _plan(
    *,
    candidate_count: int = 2,
    semantic_repair_enabled: bool = True,
    retry_attempts: int = 0,
    wall_seconds: float = 9.0,
    config_source_sha256: str = "a" * 64,
    runtime_spec_sha256: str = "b" * 64,
    source_sha256: str = "c" * 64,
):
    route = _route()
    profile = _profile()
    return build_primary_candidate_repair_plan_v1(
        candidate_count=candidate_count,
        semantic_repair_enabled=semantic_repair_enabled,
        route_identity=route.identity,
        route_config_fingerprint_sha256=_route_fingerprint(route),
        profile=profile,
        effective_input_cap=profile.input_budget,
        retry_attempts_per_call_upper_bound=retry_attempts,
        wall_seconds_per_call_upper_bound=wall_seconds,
        config_source_id="config.ini",
        config_source_sha256=config_source_sha256,
        runtime_spec_sha256=runtime_spec_sha256,
        canonical_source_authority_id="stage1-source-authority:fixture",
        canonical_source_authority_sha256=source_sha256,
    )


def _materialize(
    repair_plan,
    candidate_id: str,
    payload: dict[str, object],
    *,
    route: ReachableProviderRoute | None = None,
    route_config_fingerprint_sha256: str | None = None,
    retry_attempts: int = 0,
    wall_seconds_upper_bound: float = 9.0,
    request_id: str | None = None,
    **binding_overrides: object,
):
    bindings: dict[str, object] = {
        "config_source_id": repair_plan.config_source_id,
        "config_source_sha256": repair_plan.config_source_sha256,
        "runtime_spec_sha256": repair_plan.runtime_spec_sha256,
        "canonical_source_authority_id": repair_plan.canonical_source_authority_id,
        "canonical_source_authority_sha256": repair_plan.canonical_source_authority_sha256,
    }
    binding_route_fingerprint = binding_overrides.pop(
        "route_config_fingerprint_sha256", None
    )
    bindings.update(binding_overrides)
    resolved_route = route or _route()
    return repair_plan.materialize_request_row(
        candidate_id,
        payload,
        route=resolved_route,
        route_config_fingerprint_sha256=(
            route_config_fingerprint_sha256
            or binding_route_fingerprint
            or _route_fingerprint(resolved_route)
        ),
        profile=_profile(),
        retry_attempts=retry_attempts,
        wall_seconds_upper_bound=wall_seconds_upper_bound,
        request_id=request_id,
        **bindings,
    )


def _materialized_payload(candidate_id: str = "candidate_1") -> dict[str, object]:
    return {
        "task": SEMANTIC_REPAIR_TASK_V1,
        "candidate_id": candidate_id,
        "original_provider_output": {
            "sections": [
                {
                    "section_id": f"{candidate_id}_section_1",
                    "paper_keys": ["P001"],
                    "claims": ["evidence-bound claim"],
                }
            ]
        },
        "validation_error": "Candidate paper key is outside its evidence contract.",
        "allowed_paper_ids": ["P001"],
        "allowed_relation_ids": [],
        "repair_rules": list(SEMANTIC_REPAIR_RULES_V1),
        "output_schema": dict(SEMANTIC_REPAIR_OUTPUT_SCHEMA_V1),
    }


def test_primary_repair_exposure_is_bounded_but_not_exact_or_transport_ready() -> None:
    repair_plan = _plan(candidate_count=3, retry_attempts=0)
    exposure = repair_plan.to_exposure()
    assert exposure is not None
    assert exposure.exposure_status == "bounded_conditional"
    assert exposure.logical_calls_upper_bound == 3
    assert exposure.cardinality_basis is not None
    assert exposure.cardinality_basis.maximum_count == 3
    assert exposure.retry_attempts_per_call_upper_bound == 0
    assert exposure.wall_seconds_per_call_upper_bound == 9.0
    assert exposure.context_tokens_per_call_upper_bound == (
        repair_plan.effective_input_cap
        + repair_plan.output_tokens_per_call
        + repair_plan.reasoning_tokens_per_call
        + repair_plan.safety_margin_tokens_per_call
    )

    stage_plan = build_stage_plan(
        action="generate_outline",
        requested_stages=("outline",),
        validation_enabled=False,
    )
    route_plan = ReachableProviderRoutePlan(
        action="generate_outline",
        stage_plan=stage_plan,
        routes=(_route(),),
    )
    projection = build_full_stage_request_plan_v1(
        stage_plan=stage_plan,
        reachable_route_plan=route_plan,
        stage_inventories=(
            ProviderStageRequestInventoryV1(
                stage_name="outline",
                source_builder="outline candidate repair plan",
                unknown_exposures=(exposure,),
            ),
        ),
        aggregate_budget=ProviderAggregateBudgetV2(
            max_provider_calls_total=10,
            max_output_tokens_total=20_000,
            max_retry_attempts_total=4,
            max_wall_seconds=120.0,
        ),
    )

    assert projection["envelope_complete"] is True
    assert projection["exact_requests_materialized"] is False
    assert projection["ready_for_transport"] is False
    assert projection["budget_status"]["admission"] == "conditional_within_budget"


def test_contract_hash_binds_effective_candidate_and_source_config_identities() -> None:
    baseline = _plan(candidate_count=2)
    candidate_change = _plan(candidate_count=3)
    source_change = _plan(source_sha256="d" * 64)
    config_change = _plan(config_source_sha256="e" * 64)
    spec_change = _plan(runtime_spec_sha256="f" * 64)

    assert baseline.contract_sha256 != candidate_change.contract_sha256
    assert baseline.contract_sha256 != source_change.contract_sha256
    assert baseline.contract_sha256 != config_change.contract_sha256
    assert baseline.contract_sha256 != spec_change.contract_sha256
    assert baseline.cardinality_basis().basis_artifact_sha256 == baseline.contract_sha256
    assert baseline.cardinality_basis().maximum_count == baseline.maximum_repair_calls == 2


def test_disabled_repair_has_no_conditional_exposure_or_admission() -> None:
    repair_plan = _plan(semantic_repair_enabled=False)

    assert repair_plan.maximum_repair_calls == 0
    assert repair_plan.repairable_candidate_ids == ()
    assert repair_plan.to_exposure() is None
    with pytest.raises(StagePlanError, match="disabled"):
        repair_plan.admit_primary_repair("candidate_1", attempted_candidate_ids=set())


def test_primary_admission_rejects_foreign_repeated_and_stability_candidates() -> None:
    repair_plan = _plan(candidate_count=2)
    attempted: set[str] = set()

    repair_plan.admit_primary_repair("candidate_1", attempted_candidate_ids=attempted)
    assert attempted == {"candidate_1"}
    with pytest.raises(StagePlanError, match="already attempted"):
        repair_plan.admit_primary_repair("candidate_1", attempted_candidate_ids=attempted)
    with pytest.raises(StagePlanError, match="outside"):
        repair_plan.admit_primary_repair("candidate_3", attempted_candidate_ids=attempted)
    with pytest.raises(StagePlanError, match="stability-prefixed"):
        repair_plan.admit_primary_repair(
            "candidate_1",
            attempted_candidate_ids=attempted,
            scope="stability:reverse-order",
        )


def test_materialized_request_row_is_exact_and_fits_initial_profile_and_deadline() -> None:
    repair_plan = _plan(candidate_count=2, retry_attempts=0, wall_seconds=9.0)
    payload = _materialized_payload()
    row = _materialize(
        repair_plan, "candidate_1", payload, wall_seconds_upper_bound=8.5
    )

    assert row.request_estimate.request_hash == hash_json(payload)
    assert row.request_estimate.estimated_input_tokens <= repair_plan.effective_input_cap
    assert row.retry_attempts == 0
    assert row.wall_seconds_upper_bound == 8.5
    assert row.conditional_on == ""


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload.update(candidate_id="candidate_2"),
        lambda payload: payload.update(output_schema={"candidate_id": "other"}),
        lambda payload: payload.update(repair_rules=[]),
        lambda payload: payload.update(task="other"),
    ],
)
def test_materialized_request_rejects_identity_predicate_or_schema_drift(mutate) -> None:
    repair_plan = _plan(candidate_count=2)
    payload = _materialized_payload()
    mutate(payload)

    with pytest.raises(StagePlanError, match="schema or identity"):
        _materialize(
            repair_plan, "candidate_1", payload
        )


def test_materialized_request_rejects_route_retry_and_deadline_drift() -> None:
    repair_plan = _plan(candidate_count=2, retry_attempts=0, wall_seconds=9.0)
    payload = _materialized_payload()
    wrong_route = replace(_route(), api_base_host="other.example")

    with pytest.raises(StagePlanError, match="route differs"):
        _materialize(repair_plan, "candidate_1", payload, route=wrong_route)
    with pytest.raises(StagePlanError, match="retries exceed"):
        _materialize(repair_plan, "candidate_1", payload, retry_attempts=1)
    with pytest.raises(StagePlanError, match="deadline exceeds"):
        _materialize(repair_plan, "candidate_1", payload, wall_seconds_upper_bound=9.1)
    with pytest.raises(StagePlanError, match="deadline must be positive"):
        _materialize(repair_plan, "candidate_1", payload, wall_seconds_upper_bound=0.0)
    with pytest.raises(StagePlanError, match="request ID"):
        _materialize(repair_plan, "candidate_1", payload, request_id="stability:candidate_1")


@pytest.mark.parametrize(
    ("constant_name", "payload_field", "mutated_value"),
    [
        (
            "SEMANTIC_REPAIR_OUTPUT_SCHEMA_V1",
            "output_schema",
            {"candidate_id": "changed after plan creation"},
        ),
        (
            "SEMANTIC_REPAIR_RULES_V1",
            "repair_rules",
            ("Changed repair policy after plan creation.",),
        ),
        (
            "SEMANTIC_REPAIR_PREDICATE_V1",
            None,
            "different repair trigger",
        ),
    ],
)
def test_materialized_request_rejects_contract_constant_mutation_after_plan_creation(
    monkeypatch: pytest.MonkeyPatch,
    constant_name: str,
    payload_field: str | None,
    mutated_value: object,
) -> None:
    repair_plan = _plan(candidate_count=2)
    payload = _materialized_payload()
    serialized_plan = repair_plan.to_dict()
    monkeypatch.setattr(candidate_repair_plan_module, constant_name, mutated_value)
    if payload_field is not None:
        payload[payload_field] = (
            dict(mutated_value)
            if payload_field == "output_schema"
            else list(mutated_value)
        )

    assert repair_plan.to_dict()["output_schema"] == serialized_plan["output_schema"]
    assert repair_plan.to_dict()["repair_rules"] == serialized_plan["repair_rules"]
    assert repair_plan.to_dict()["conditional_on"] == serialized_plan["conditional_on"]
    with pytest.raises(StagePlanError, match="contract changed after plan creation"):
        _materialize(repair_plan, "candidate_1", payload)


def test_direct_plan_construction_cannot_replace_verified_contract_hash() -> None:
    repair_plan = _plan(candidate_count=2)

    with pytest.raises(StagePlanError, match="contract hash"):
        replace(repair_plan, contract_sha256="0" * 64)
    with pytest.raises(StagePlanError, match="canonical source authority SHA-256"):
        replace(repair_plan, canonical_source_authority_sha256="missing")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("config_source_sha256", "d" * 64),
        ("runtime_spec_sha256", "e" * 64),
        ("canonical_source_authority_sha256", "f" * 64),
        ("route_config_fingerprint_sha256", "1" * 64),
    ],
)
def test_materialized_request_requires_current_config_spec_source_and_route_bindings(
    field: str, value: str
) -> None:
    repair_plan = _plan(candidate_count=2)

    with pytest.raises(StagePlanError, match="changed"):
        _materialize(
            repair_plan,
            "candidate_1",
            _materialized_payload(),
            **{field: value},
        )


@pytest.mark.parametrize("candidate_count", [0, MAX_OUTLINE_CANDIDATE_COUNT + 1, True])
def test_plan_rejects_candidate_count_outside_enforced_producer_contract(candidate_count) -> None:
    with pytest.raises(StagePlanError, match="candidate_count"):
        _plan(candidate_count=candidate_count)


@pytest.mark.parametrize(
    "field,value",
    [
        ("config_source_sha256", ""),
        ("runtime_spec_sha256", "not-a-hash"),
        ("canonical_source_authority_sha256", "0" * 63),
    ],
)
def test_plan_requires_real_config_spec_and_source_hash_bindings(field, value) -> None:
    kwargs = {
        "config_source_sha256": "a" * 64,
        "runtime_spec_sha256": "b" * 64,
        "source_sha256": "c" * 64,
    }
    if field == "canonical_source_authority_sha256":
        kwargs["source_sha256"] = value
    else:
        kwargs[field] = value

    with pytest.raises(StagePlanError, match="SHA-256"):
        _plan(**kwargs)
