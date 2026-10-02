from __future__ import annotations

import json
import hashlib

import ai_interface
import pytest

from models import APIConfig
from outline.semantic_chunking import (
    _safe_text,
    _text_values,
    _topic_candidates,
    build_paper_content_layers,
)
from outline.v3_evidence import build_outline_evidence_views
from outline.v3_models import PaperContentLayers, PaperIndexCard, SourceFieldLedgerEntry
from outline.v3_relations import _safe_text as relation_safe_text
from runtime.provider_runtime import (
    ProviderAggregateBudgetV1,
    ProviderAggregateBudgetV2,
    ProviderBudgetController,
    ProviderBudgetExceeded,
    ProviderRuntime,
    ProviderRuntimeContractError,
    AcceptanceExecutionContextV1,
    acceptance_context_environment,
    acceptance_execution_context_from_environment,
    bind_acceptance_execution_context,
    authorized_provider_call_limit,
    provider_aggregate_budget_from_mapping,
)
from runtime.release_acceptance import (
    ReleaseAcceptanceBudget,
    ReleaseAcceptancePlanV2,
    ReleaseAcceptanceSpecError,
)
from services.model_selection import _section_to_api_config
from runtime.stage_planning import (
    ProviderStageRequestInventoryV1,
    build_full_stage_request_plan_v1,
    build_provider_request_plan_row_v1,
)
from tests.test_pr25_all_stage_plan_v2 import _plans, _profile, _request_payload


@pytest.mark.parametrize(
    ("requested", "expected"),
    [(None, 24), (3, 3), (24, 24), (33, 33), (80, 80), (500, 500), (0, 0), (-1, 0)],
)
def test_explicit_run_call_budget_is_not_truncated_to_24(
    requested: int | None,
    expected: int,
) -> None:
    assert authorized_provider_call_limit(requested) == expected


@pytest.mark.parametrize("requested", [True, "invalid"])
def test_invalid_run_call_budget_is_rejected(requested: object) -> None:
    with pytest.raises(ValueError):
        authorized_provider_call_limit(requested)  # type: ignore[arg-type]


@pytest.mark.parametrize("limit", [3, 24, 33, 80, 500])
def test_strict_acceptance_budget_preserves_explicit_call_limit(limit: int) -> None:
    budget = ReleaseAcceptanceBudget(
        max_provider_calls_total=limit,
        max_output_tokens_total=4_096,
        max_retry_attempts_total=0,
        max_wall_seconds=60,
    )
    assert budget.to_provider_budget().max_provider_calls_total == limit


def test_new_release_acceptance_budget_defaults_to_24() -> None:
    assert ReleaseAcceptanceBudget().max_provider_calls_total == 24
    assert authorized_provider_call_limit(None) == 24


def test_negative_and_invalid_acceptance_budget_values_are_rejected() -> None:
    with pytest.raises(ValueError):
        ReleaseAcceptanceBudget(max_provider_calls_total=-1)
    with pytest.raises(ValueError):
        ProviderAggregateBudgetV2.from_mapping(
            {
                "schema_version": ProviderAggregateBudgetV2.SCHEMA_VERSION,
                "max_provider_calls_total": "many",
                "max_output_tokens_total": 4_096,
                "max_retry_attempts_total": 0,
                "max_wall_seconds": 60,
            }
        )


def test_strict_acceptance_budget_requires_each_explicit_limit() -> None:
    with pytest.raises(ReleaseAcceptanceSpecError, match="explicit dimensions"):
        ReleaseAcceptanceBudget.from_mapping(
            {
                "schema_version": ProviderAggregateBudgetV2.SCHEMA_VERSION,
                "max_provider_calls_total": 4,
                "max_output_tokens_total": 4_096,
                "max_wall_seconds": 60,
            }
        )


def test_provider_aggregate_budget_v1_and_v2_decode_by_explicit_schema() -> None:
    legacy = provider_aggregate_budget_from_mapping(
        {
            "max_provider_calls_total": 4,
            "max_output_tokens_total": 4_096,
            "max_retry_attempts_total": 0,
            "max_wall_seconds": 60,
        }
    )
    strict = provider_aggregate_budget_from_mapping(
        {
            "schema_version": ProviderAggregateBudgetV2.SCHEMA_VERSION,
            "max_provider_calls_total": 4,
            "max_output_tokens_total": 4_096,
            "max_retry_attempts_total": 0,
            "max_wall_seconds": 60,
        }
    )

    assert isinstance(legacy, ProviderAggregateBudgetV1)
    assert isinstance(strict, ProviderAggregateBudgetV2)
    assert legacy.max_retry_attempts_total == strict.max_retry_attempts_total == 0


def test_strict_zero_limits_block_calls_outputs_retries_and_wall_time() -> None:
    base = {
        "max_provider_calls_total": 1,
        "max_output_tokens_total": 1,
        "max_retry_attempts_total": 1,
        "max_wall_seconds": 60,
    }
    strict = lambda **overrides: ProviderAggregateBudgetV2(**(base | overrides))

    with pytest.raises(ProviderBudgetExceeded, match="call budget"):
        ProviderBudgetController(strict(max_provider_calls_total=0)).admit()
    with pytest.raises(ProviderBudgetExceeded, match="output-token budget"):
        ProviderBudgetController(strict(max_output_tokens_total=0)).admit(
            requested_output_tokens=1
        )
    with pytest.raises(ProviderBudgetExceeded, match="retry budget"):
        ProviderBudgetController(
            strict(max_provider_calls_total=3, max_retry_attempts_total=0)
        ).admit(
            requested_retry_attempts=1
        )
    with pytest.raises(ProviderBudgetExceeded, match="wall-clock budget"):
        ProviderBudgetController(strict(max_wall_seconds=0)).admit()


def test_legacy_v1_zero_retry_budget_keeps_historical_unbounded_semantics() -> None:
    budget = ProviderAggregateBudgetV1(
        max_provider_calls_total=4,
        max_output_tokens_total=4_096,
        max_retry_attempts_total=0,
        max_wall_seconds=60,
    )
    reservation = ProviderBudgetController(budget).admit(
        requested_output_tokens=128,
        requested_retry_attempts=1,
    )

    assert reservation.retry_attempts == 1
    assert reservation.provider_calls == 2


def test_unversioned_acceptance_budget_keeps_v1_zero_semantics_and_plan_hash_shape() -> None:
    legacy = ReleaseAcceptanceBudget.from_mapping(
        {
            "max_provider_calls_total": 4,
            "max_output_tokens_total": 4_096,
            "max_retry_attempts_total": 0,
            "max_wall_seconds": 60,
        }
    )
    assert isinstance(legacy.to_provider_budget(), ProviderAggregateBudgetV1)
    assert "schema_version" not in legacy.to_dict()

    plan = ReleaseAcceptancePlanV2(
        parent_run_id="legacy-plan",
        budget=legacy,
        scenarios={},
    )
    legacy_payload = {
        "schema_version": "release-acceptance-plan-v2",
        "parent_run_id": "legacy-plan",
        "final_executable_sha": "",
        "budget": {
            "max_provider_calls_total": 4,
            "max_output_tokens_total": 4_096,
            "max_retry_attempts_total": 0,
            "max_wall_seconds": 60,
        },
        "third_party_acknowledged": False,
        "third_party_hosts": [],
        "external_host_acknowledgement": None,
        "scenarios": {},
    }
    expected_hash = hashlib.sha256(
        json.dumps(legacy_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    assert plan.plan_sha256() == expected_hash


def test_release_acceptance_zero_limits_preserve_v1_and_v2_semantics() -> None:
    strict = ReleaseAcceptanceBudget.from_mapping(
        {
            "schema_version": ProviderAggregateBudgetV2.SCHEMA_VERSION,
            "max_provider_calls_total": 0,
            "max_output_tokens_total": 0,
            "max_retry_attempts_total": 0,
            "max_wall_seconds": 0,
        }
    )
    strict_provider_budget = strict.to_provider_budget()
    assert isinstance(strict_provider_budget, ProviderAggregateBudgetV2)
    assert strict_provider_budget.max_provider_calls_total == 0
    assert strict_provider_budget.max_output_tokens_total == 0
    assert strict_provider_budget.max_retry_attempts_total == 0
    assert strict_provider_budget.max_wall_seconds == 0

    legacy = ReleaseAcceptanceBudget.from_mapping(
        {
            "max_provider_calls_total": 0,
            "max_output_tokens_total": 0,
            "max_retry_attempts_total": 0,
            "max_wall_seconds": 0,
        }
    )
    assert isinstance(legacy.to_provider_budget(), ProviderAggregateBudgetV1)
    reservation = ProviderBudgetController(legacy.to_provider_budget()).admit(
        requested_output_tokens=8,
        requested_retry_attempts=1,
    )
    assert reservation.provider_calls == 2
    assert reservation.output_tokens == 8
    assert reservation.retry_attempts == 1


def test_strict_budget_state_resumes_and_rejects_mode_mismatch(tmp_path) -> None:
    state_path = tmp_path / "strict-budget.json"
    strict = ProviderAggregateBudgetV2(
        max_provider_calls_total=3,
        max_output_tokens_total=1_000,
        max_retry_attempts_total=0,
        max_wall_seconds=60,
    )
    first = ProviderBudgetController(strict)
    first.bind_state_path(state_path, acceptance_run_id="strict-resume")
    reservation = first.admit(requested_output_tokens=100)
    first.complete(reservation, {"attempts": 1, "output_tokens": 80})

    resumed = ProviderBudgetController(strict)
    resumed.bind_state_path(
        state_path,
        acceptance_run_id="strict-resume",
        state_started=True,
    )
    assert resumed.snapshot()["calls_used"] == 1

    legacy = ProviderAggregateBudgetV1(
        max_provider_calls_total=3,
        max_output_tokens_total=1_000,
        max_retry_attempts_total=0,
        max_wall_seconds=60,
    )
    with pytest.raises(ProviderRuntimeContractError, match="semantics"):
        ProviderBudgetController(legacy).bind_state_path(
            state_path,
            acceptance_run_id="strict-resume",
            state_started=True,
        )


def test_legacy_v1_zero_retry_state_resumes_without_reinterpreting_limit(tmp_path) -> None:
    state_path = tmp_path / "legacy-budget.json"
    legacy = ProviderAggregateBudgetV1(
        max_provider_calls_total=4,
        max_output_tokens_total=4_096,
        max_retry_attempts_total=0,
        max_wall_seconds=60,
    )
    first = ProviderBudgetController(legacy)
    first.bind_state_path(state_path, acceptance_run_id="legacy-resume")
    first.admit(requested_output_tokens=100, requested_retry_attempts=1)
    assert json.loads(state_path.read_text(encoding="utf-8"))["schema_version"] == (
        "provider-aggregate-budget-v4"
    )

    resumed = ProviderBudgetController(
        ProviderAggregateBudgetV1.from_mapping(legacy.to_dict())
    )
    resumed.bind_state_path(
        state_path,
        acceptance_run_id="legacy-resume",
        state_started=True,
    )
    snapshot = resumed.snapshot()
    assert snapshot["calls_reserved"] == 2
    assert snapshot["retry_attempts_reserved"] == 1


def _budget_plan(budget: ProviderAggregateBudgetV1 | ProviderAggregateBudgetV2) -> dict[str, object]:
    stage_plan, route_plan = _plans(requested_stages=("outline",))
    route = route_plan.route_for_role("candidate_provider_generation")
    row = build_provider_request_plan_row_v1(
        stage_name="outline",
        request_id="budget-semantics-probe",
        source_builder="test strict/legacy budget projection",
        route=route,
        request_payload=_request_payload("budget-semantics-probe"),
        profile=_profile(route),
        retry_attempts=1,
        requested_output_tokens=128,
        wall_seconds_upper_bound=1,
    )
    return build_full_stage_request_plan_v1(
        stage_plan=stage_plan,
        reachable_route_plan=route_plan,
        stage_inventories=(
            ProviderStageRequestInventoryV1(
                stage_name="outline",
                source_builder="test strict/legacy budget projection",
                requests=(row,),
            ),
        ),
        aggregate_budget=budget,
    )


def test_planner_reports_strict_zero_as_exceeded_and_legacy_zero_as_unbounded() -> None:
    strict = _budget_plan(
        ProviderAggregateBudgetV2(
            max_provider_calls_total=0,
            max_output_tokens_total=0,
            max_retry_attempts_total=0,
            max_wall_seconds=0,
        )
    )
    legacy = _budget_plan(
        ProviderAggregateBudgetV1(
            max_provider_calls_total=0,
            max_output_tokens_total=0,
            max_retry_attempts_total=0,
            max_wall_seconds=0,
        )
    )

    assert strict["budget_status"]["provider_calls"] == "exceeded"
    assert strict["budget_status"]["provider_retries"] == "exceeded"
    assert strict["budget_status"]["requested_output_tokens"] == "exceeded"
    assert strict["budget_status"]["wall_time"] == "exceeded"
    assert strict["limits"]["effective_provider_call_limit"] == 0
    assert legacy["budget_status"]["provider_calls"] == "unbounded"
    assert legacy["budget_status"]["provider_retries"] == "unbounded"
    assert legacy["budget_status"]["requested_output_tokens"] == "unbounded"
    assert legacy["budget_status"]["wall_time"] == "unbounded"
    assert legacy["limits"]["effective_provider_call_limit"] is None


def test_zero_retry_budget_rejects_retry_reservation() -> None:
    controller = ProviderBudgetController(
        ProviderAggregateBudgetV2(
            max_provider_calls_total=4,
            max_output_tokens_total=4_096,
            max_retry_attempts_total=0,
            max_wall_seconds=60,
        )
    )

    with pytest.raises(ProviderBudgetExceeded, match="retry"):
        controller.admit(requested_output_tokens=128, requested_retry_attempts=1)


def test_outline_local_call_cap_is_checked_before_aggregate_reservation(tmp_path) -> None:
    from tests.test_outline_v3_semantic_execution import _executor

    executor = _executor(tmp_path, stability_mode="off", max_provider_calls=0)
    budget = ProviderAggregateBudgetV2(
        max_provider_calls_total=4,
        max_output_tokens_total=16_384,
        max_retry_attempts_total=0,
        max_wall_seconds=60,
    )
    state_path = tmp_path / "provider-budget.json"
    controller = ProviderBudgetController(budget)
    context = AcceptanceExecutionContextV1(
        acceptance_run_id="reservation-leak-test",
        final_executable_sha="a" * 40,
        absolute_deadline_epoch=100,
        provider_budget=budget,
        provider_budget_state_path=str(state_path),
        evidence_root=str(tmp_path),
        process_event_log=str(tmp_path / "events.jsonl"),
        scenario_state_path=str(tmp_path / "scenario.json"),
        owner_authorized=True,
    )

    with bind_acceptance_execution_context(context, controller):
        with pytest.raises(Exception, match="budget exhausted"):
            executor._provider_call(
                "candidate_1_provider_generation",
                {"task": "must be blocked before admission"},
                output_tokens=1_024,
            )

    assert controller.snapshot()["calls_reserved"] == 0


def test_invalid_offline_fixture_is_checked_before_aggregate_reservation(tmp_path) -> None:
    from tests.test_outline_v3_semantic_execution import _executor

    executor = _executor(tmp_path, stability_mode="off", max_provider_calls=4)
    executor.provider = None

    def invalid_fixture(_node_id: str, _request: dict[str, object]) -> dict[str, object]:
        raise ValueError("fixture missing")

    executor._fixture_response = invalid_fixture
    budget = ProviderAggregateBudgetV2(
        max_provider_calls_total=4,
        max_output_tokens_total=16_384,
        max_retry_attempts_total=0,
        max_wall_seconds=60,
    )
    controller = ProviderBudgetController(budget)
    context = AcceptanceExecutionContextV1(
        acceptance_run_id="fixture-reservation-test",
        final_executable_sha="c" * 40,
        absolute_deadline_epoch=100,
        provider_budget=budget,
        provider_budget_state_path=str(tmp_path / "provider-budget.json"),
        evidence_root=str(tmp_path),
        process_event_log=str(tmp_path / "events.jsonl"),
        scenario_state_path=str(tmp_path / "scenario.json"),
        owner_authorized=True,
    )

    with bind_acceptance_execution_context(context, controller):
        with pytest.raises(ValueError, match="fixture missing"):
            executor._provider_call(
                "candidate_1_provider_generation",
                {"task": "local fixture"},
                output_tokens=1_024,
            )

    assert controller.snapshot()["calls_reserved"] == 0


def test_unversioned_v1_zero_retry_budget_keeps_historical_semantics() -> None:
    budget = provider_aggregate_budget_from_mapping(
        {
            "max_provider_calls_total": 4,
            "max_output_tokens_total": 4_096,
            "max_retry_attempts_total": 0,
            "max_wall_seconds": 60,
        }
    )
    assert isinstance(budget, ProviderAggregateBudgetV1)
    reservation = ProviderBudgetController(budget).admit(
        requested_output_tokens=128,
        requested_retry_attempts=1,
    )
    assert reservation.retry_attempts == 1


def test_strict_zero_retry_budget_context_environment_round_trip_keeps_v2_schema(
    tmp_path,
    monkeypatch,
) -> None:
    budget = ProviderAggregateBudgetV2(
        max_provider_calls_total=4,
        max_output_tokens_total=4_096,
        max_retry_attempts_total=0,
        max_wall_seconds=60,
    )
    context = AcceptanceExecutionContextV1(
        acceptance_run_id="run-v2",
        final_executable_sha="a" * 40,
        absolute_deadline_epoch=100,
        provider_budget=budget,
        provider_budget_state_path=str(tmp_path / "budget.json"),
        evidence_root=str(tmp_path),
        process_event_log=str(tmp_path / "events.jsonl"),
        scenario_state_path=str(tmp_path / "scenario.json"),
        owner_authorized=True,
    )

    environment = acceptance_context_environment(context, base_environment={})
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    restored = acceptance_execution_context_from_environment()
    assert restored is not None
    assert isinstance(restored.provider_budget, ProviderAggregateBudgetV2)
    assert restored.provider_budget.max_retry_attempts_total == 0


@pytest.mark.parametrize("call_limit", [0, 3])
def test_strict_zero_retry_allows_only_the_initial_authorized_attempt(monkeypatch, call_limit: int) -> None:
    calls = 0

    def post(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        response = ai_interface.requests.Response()
        response.status_code = 429
        response._content = b'{"error":{"message":"synthetic rate limit"}}'
        return response

    monkeypatch.setattr(ai_interface, "_post_with_proxy_mode", post)
    budget = ProviderAggregateBudgetV2(
        max_provider_calls_total=call_limit,
        max_output_tokens_total=4_096,
        max_retry_attempts_total=0,
        max_wall_seconds=60,
    )
    runtime = ProviderRuntime(
        aggregate_budget=ProviderBudgetController(budget),
        test_only=True,
    )

    result = ai_interface._call_ai_api_detailed(
        "prompt",
        {
            "api_key": "test-key",
            "model": "test-model",
            "api_base": "https://provider.example.test/v1",
        },
        "system",
        max_tokens=100,
        retry_attempts=2,
        provider_runtime=runtime,
    )

    assert calls == (1 if call_limit else 0)
    assert result["status"] == "failed"
    if not call_limit:
        assert result["error_kind"] == "budget_exhausted"
    else:
        assert result["attempts"] == 1
    snapshot = runtime.aggregate_budget.snapshot()
    assert snapshot["retry_attempts_used"] == 0
    assert snapshot["retry_attempts_reserved"] == 0


def test_strict_budget_state_uses_v5_and_rejects_legacy_rebinding(tmp_path) -> None:
    path = tmp_path / "provider-budget.json"
    strict = ProviderAggregateBudgetV2(
        max_provider_calls_total=2,
        max_output_tokens_total=4_096,
        max_retry_attempts_total=0,
        max_wall_seconds=60,
    )
    ProviderBudgetController(strict).bind_state_path(
        path,
        acceptance_run_id="run-v2",
        state_started=False,
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == "provider-aggregate-budget-v5"
    assert payload["budget"]["schema_version"] == ProviderAggregateBudgetV2.SCHEMA_VERSION

    legacy = ProviderAggregateBudgetV1(
        max_provider_calls_total=2,
        max_output_tokens_total=4_096,
        max_retry_attempts_total=0,
        max_wall_seconds=60,
    )
    with pytest.raises(ProviderRuntimeContractError, match="semantics"):
        ProviderBudgetController(legacy).bind_state_path(
            path,
            acceptance_run_id="run-v2",
            state_started=True,
        )


def test_new_release_acceptance_budgets_are_strict_but_unversioned_specs_stay_v1() -> None:
    fresh = ReleaseAcceptanceBudget(
        max_provider_calls_total=4,
        max_output_tokens_total=4_096,
        max_retry_attempts_total=0,
        max_wall_seconds=60,
    )
    assert isinstance(fresh.to_provider_budget(), ProviderAggregateBudgetV2)
    assert fresh.to_dict()["schema_version"] == ProviderAggregateBudgetV2.SCHEMA_VERSION
    parsed = ReleaseAcceptanceBudget.from_mapping(fresh.to_dict())
    assert isinstance(parsed.to_provider_budget(), ProviderAggregateBudgetV2)
    assert parsed.max_retry_attempts_total == 0

    legacy = ReleaseAcceptanceBudget.from_mapping(
        {
            "max_provider_calls_total": 4,
            "max_output_tokens_total": 4_096,
            "max_retry_attempts_total": 2,
            "max_wall_seconds": 60,
        }
    )
    assert isinstance(legacy.to_provider_budget(), ProviderAggregateBudgetV1)
    assert "schema_version" not in legacy.to_dict()


def test_integer_zero_optional_config_values_survive_normalization() -> None:
    config: APIConfig = _section_to_api_config(
        {
            "api_key": "unit-test-key",
            "model": "test-model",
            "api_base": "https://provider.example.test/v1",
            "transport_retries": 0,
            "reasoning_reserve_tokens": 0,
            "safety_margin_tokens": 0,
        }
    )

    assert config["transport_retries"] == 0
    assert config["reasoning_reserve_tokens"] == 0
    assert config["safety_margin_tokens"] == 0


def test_numeric_zero_is_retained_in_literal_evidence_projection() -> None:
    assert _safe_text(0) == "0"
    assert relation_safe_text(0) == "0"
    assert _text_values({"effect": 0, "p": 0.04, "direction": "negative"}) == [
        "0",
        "0.04",
        "negative",
    ]


def test_numeric_zero_result_materializes_a_typed_evidence_unit() -> None:
    from tests.test_outline_v3_semantic_execution import _summary

    summary = _summary("paper-zero", "Zero", "A non-significant result.")
    summary["core_analysis"]["zero_results"] = 0
    evidence = build_outline_evidence_views([summary])
    layers = build_paper_content_layers([summary], evidence)
    dossier = layers.dossier_by_paper["paper-zero"]

    assert dossier.zero_results == ["0"]
    assert dossier.evidence_ids_by_field["zero_results"]
    assert dossier.evidence_text_by_id[dossier.evidence_ids_by_field["zero_results"][0]] == "0"


def test_source_field_ledger_round_trip_preserves_numeric_zero() -> None:
    entry = SourceFieldLedgerEntry.from_dict(
        {
            "source_field_id": "source-field:zero",
            "source_path": "ai_summary.core_analysis.zero_results",
            "source_value": 0,
            "derived_value": 0,
            "disposition": "exact",
            "canonical_field": "zero_results",
            "scope": "paper",
            "interpretation_required": True,
            "source_summary_hash": "source-hash",
        }
    )

    assert entry.source_value == "0"
    assert entry.derived_value == "0"


def test_method_theory_pairing_does_not_remove_other_shared_topics() -> None:
    paper_ids = {"paper-a", "paper-b", "paper-c"}
    cards = [
        PaperIndexCard(
            paper_id=paper_id,
            key_constructs=["fairness"],
            theories=["equity theory"],
            mechanisms=["reference-price comparison"],
            method_category="survey",
            key_boundaries=["online retail"],
        )
        for paper_id in sorted(paper_ids)
    ]

    groups, _paper_topics = _topic_candidates(PaperContentLayers(index_cards=cards))

    for dimension, label in (
        ("construct", "fairness"),
        ("mechanism", "reference-price comparison"),
        ("context", "online retail"),
    ):
        assert any(
            group.get("dimension") == dimension
            and group.get("label") == label
            and set(group.get("paper_ids") or ()) == paper_ids
            for group in groups.values()
        ), f"shared {dimension} task {label!r} disappeared after adding method labels"

    paired_topics = [group for group in groups.values() if group.get("paired_dimensions")]
    assert len(paired_topics) == len(paper_ids)
    assert all(topic.get("theory_labels") == ["equity theory"] for topic in paired_topics)
