"""Bounded preflight projections and exact-request admission regressions."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from typing import Any, Mapping

import pytest

from outline import v3_executor as executor_module
from outline.v3_executor import OutlineProviderCallPlan, OutlineV3ExecutionError, OutlineV3Executor
from runtime.provider_context import ProviderContextProfile
from services.artifact_registry import ArtifactRegistry
from services.job_workspace import JobWorkspace
from tests.test_outline_v3_semantic_execution import _summary
from tests.test_run_first_candidate_packing import _input as _candidate_scope_fixture


def _executor(
    tmp_path,
    *,
    summaries=None,
    profile: ProviderContextProfile | None = None,
    candidate_count: int = 2,
    max_source_prompt_tokens: int = 32_000,
) -> OutlineV3Executor:
    workspace = JobWorkspace.create(str(tmp_path), "future-preflight", job_id="future-preflight-job")
    registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    return OutlineV3Executor(
        job_id=workspace.job_id,
        summaries=summaries or [
            _summary("A", "Paper A", "The effect was negative within the measured sample."),
            _summary("B", "Paper B", "The effect was zero within the measured sample."),
        ],
        workspace=workspace,
        artifact_registry=registry,
        provider=lambda *_args, **_kwargs: pytest.fail("preflight test must not call a provider"),
        provider_profile=profile,
        candidate_count=candidate_count,
        stability_mode="off",
        max_provider_calls=24,
        max_source_prompt_tokens=max_source_prompt_tokens,
    )


def _call_plan(executor, node_id: str) -> OutlineProviderCallPlan:
    route = executor._role_route(node_id)
    return OutlineProviderCallPlan(
        artifact_type="outline_provider_call_plan",
        artifact_version="v1",
        job_id=executor.job_id,
        stage_name="outline_v3",
        closure_epoch_id=executor.closure_epoch_id,
        logical_attempt_identity=executor.logical_attempt_identity,
        variant_name="canonical",
        node_id=node_id,
        call_id=f"{node_id}:call",
        provider=route.provider_name,
        model=route.model,
        endpoint_type=route.endpoint_type,
        estimated_input_tokens=1_000,
        estimated_output_tokens=1_000,
        estimated_reasoning_tokens=0,
        estimated_cached_input_tokens=0,
        estimated_cache_write_tokens=0,
        estimated_total_tokens=2_000,
        input_cost_per_1k_tokens=None,
        output_cost_per_1k_tokens=None,
        reasoning_cost_per_1k_tokens=None,
        cache_read_cost_per_1k_tokens=None,
        cache_write_cost_per_1k_tokens=None,
        estimated_cost=None,
        pricing_source="",
        pricing_policy="estimate_only_not_billing_v1",
        cost_status="unknown",
        assumptions=("bounded local fixture",),
        confidence="medium",
        upper_bound=True,
        transport_expected=True,
        configured_transport_retry_reserve=0,
        physical_attempt_upper_bound=1,
    )


def _patch_candidate_preflight_inputs(monkeypatch, executor, paper_keys: tuple[str, ...]) -> None:
    evidence = SimpleNamespace(
        views=[SimpleNamespace(paper_key=key, view_hash=f"view:{key}") for key in paper_keys],
        source_summary_hashes=[f"summary:{key}" for key in paper_keys],
    )
    relation = SimpleNamespace(
        relation_id="relation:fixture",
        to_dict=lambda: {"relation_id": "relation:fixture", "paper_keys": list(paper_keys)},
    )
    relation_map = SimpleNamespace(relations=[relation])
    semantic_plan = SimpleNamespace(coverage={"selected_relation_ids": []}, topics=[])
    content_layers = SimpleNamespace()
    monkeypatch.setattr(executor_module, "build_outline_evidence_views", lambda *_: evidence)
    monkeypatch.setattr(executor_module, "build_global_corpus_ledger", lambda *_: object())
    monkeypatch.setattr(executor_module, "build_multi_view_matrix", lambda *_: object())
    monkeypatch.setattr(executor_module, "build_global_relation_map", lambda *_args, **_kwargs: relation_map)
    monkeypatch.setattr(
        executor_module,
        "build_paper_content_layers",
        lambda *_args, **_kwargs: content_layers,
    )
    monkeypatch.setattr(
        executor_module,
        "build_semantic_chunk_plan",
        lambda *_args, **_kwargs: semantic_plan,
    )
    monkeypatch.setattr(
        executor,
        "_compact_candidate_evidence_refs",
        lambda views, *_args, **_kwargs: [
            {
                "paper_key": str(view.paper_key),
                "projection": "registry_complete_evidence_ref_v1",
                "dossier_id": f"dossier:{view.paper_key}",
                "source_summary_hash": f"summary:{view.paper_key}",
            }
            for view in views
        ],
    )


def test_unmaterialized_legacy_paper_shard_is_not_used_as_an_input_cap_verdict(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    executor = _executor(tmp_path)
    executor.semantic_provider_synthesis_enabled = True
    _patch_candidate_preflight_inputs(monkeypatch, executor, ("A", "B"))
    monkeypatch.setattr(executor, "_attach_prompt_authority", lambda _node_id, request: dict(request))

    shard_requests = [
        (
            f"candidate_1_provider_generation:local:legacy_{index}",
            {"shard_id": f"legacy_{index}"},
            ["A", "B"],
            [],
            {"fixture": "unmaterialized_legacy_request"},
        )
        for index in (1, 2)
    ]
    monkeypatch.setattr(executor, "_candidate_shard_requests", lambda **_kwargs: shard_requests)
    estimator_inputs: list[Mapping[str, Any]] = []

    def estimate_request(request: Mapping[str, Any]) -> dict[str, Any]:
        estimator_inputs.append(request)
        return {"estimated_input_tokens": 33_581, "within_budget": True}

    profile = SimpleNamespace(
        input_budget=100_000,
        max_output_tokens=4_096,
        estimate_request=estimate_request,
        estimate_tokens=lambda _request: 33_581,
    )
    input_reserve, projected_calls = executor._candidate_hierarchical_preflight(
        executor.summaries, profile, variant_name="canonical",
    )

    assert (input_reserve, projected_calls) == (32_000, 2)
    assert len(estimator_inputs) == 1  # The invented paper shards are not cap-checked as real requests.
    row = executor._candidate_preflight_projection_state["canonical:candidate_1"]
    assert row["status"] == "unmaterialized_legacy_paper_shard_projection"
    assert row["bound_status"] == "legacy_paper_projection_is_not_a_candidate_group_assignment"


def _source_closed_executor(tmp_path, monkeypatch, scope, context):
    profile = ProviderContextProfile.conservative(
        provider="local", model="preflight-model", endpoint_type="chat_completions",
        model_context_limit=128_000, max_output_tokens=4_096,
        reasoning_reserve=0, safety_margin=128,
    )
    papers = sorted(set(scope.paper_keys))
    executor = _executor(
        tmp_path,
        summaries=[_summary(key, f"Paper {key}", f"Source result for {key}.") for key in papers],
        profile=profile,
        candidate_count=1,
    )
    executor.semantic_provider_synthesis_enabled = True
    executor._candidate_output_scope = scope
    _patch_candidate_preflight_inputs(monkeypatch, executor, tuple(papers))
    monkeypatch.setattr(executor, "_attach_prompt_authority", lambda _node_id, request: dict(request))
    monkeypatch.setattr(executor, "_relation_packing_target", lambda _profile: 1)
    return executor, profile, context


def test_materialized_candidate_preflight_packs_complete_source_closed_groups(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope, context = _candidate_scope_fixture()
    executor, profile, context = _source_closed_executor(tmp_path, monkeypatch, scope, context)
    input_reserve, request_count = executor._candidate_hierarchical_preflight(
        executor.summaries,
        profile,
        variant_name="canonical",
        candidate_id="candidate_1",
        shared_semantic_context=context,
    )

    requests = executor._candidate_shard_requests(
        generation_node_id="candidate_1_provider_generation",
        provider_request={
            "candidate_id": "candidate_1",
            "paper_keys": list(scope.paper_keys),
            "relation_ids": [],
            "relations": [],
            "evidence": [
                {"paper_key": key, "projection": "registry_complete_evidence_ref_v1"}
                for key in scope.paper_keys
            ],
            "candidate_output_scope": executor._candidate_output_scope_wire(scope.paper_keys),
            "shared_semantic_context": context,
            "output_contract": {
                "preferred_candidate_wire_version": "candidate-slot-output/v1",
                "claim_prose_reserve_tokens": 256,
                "section_prose_reserve_tokens": 64,
            },
        },
        evidence_views=[SimpleNamespace(paper_key=key) for key in scope.paper_keys],
        relation_candidates=[],
    )
    assigned: dict[str, str] = {}
    for node_id, shard, papers, _relations, request in requests:
        group_ids = list(shard["claim_group_ids"])
        assert set(request["candidate_output_scope"]["claim_group_ids"]) == set(group_ids)
        assert set(request["hierarchy"]["claim_group_ids"]) == set(group_ids)
        assert set(request["hierarchy"]["paper_keys"]) == set(papers)
        assert shard["group_assignment"] == {
            group_id: node_id for group_id in group_ids
        }
        local_scope = scope.for_groups(group_ids, allowed_paper_keys=papers)
        required_fields = {
            field_id for slot in local_scope.claim_slots for field_id in slot.source_field_ids
        }
        projected_fields = {
            row["source_field_id"]
            for row in request["shared_semantic_context"]["interpretation_source_tables"]["source_fields"]
        }
        assert required_fields == projected_fields
        for group_id in group_ids:
            assert group_id not in assigned
            assigned[group_id] = node_id

    assert set(assigned) == {group.claim_group_id for group in scope.claim_groups}
    assert 1 <= request_count == len(requests)
    assert 0 < input_reserve <= 32_000
    state = executor._candidate_preflight_projection_state["canonical:candidate_1"]
    assert state["status"] == "materialized_source_closed_group_plan"
    assert state["bound_status"] == "serializer_measured_complete_assertion_groups"


def test_source_closed_indivisible_group_over_cap_is_rejected_with_its_full_closure(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope, context = _candidate_scope_fixture()
    context["interpretation_source_tables"]["source_fields"][0]["source_value"] *= 5_000
    executor, profile, context = _source_closed_executor(tmp_path, monkeypatch, scope, context)

    with pytest.raises(
        OutlineV3ExecutionError,
        match=r"source-closed candidate group cannot fit route input/output limits.*candidate group AB cannot fit intact",
    ):
        executor._candidate_hierarchical_preflight(
            executor.summaries,
            profile,
            variant_name="canonical",
            candidate_id="candidate_1",
            shared_semantic_context=context,
        )


def test_compact_arbitration_projection_excludes_all_source_navigation_cards(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = ProviderContextProfile.conservative(
        provider="local", model="preflight-model", endpoint_type="chat_completions",
        model_context_limit=128_000, max_output_tokens=4_096,
        reasoning_reserve=0, safety_margin=128,
    )

    def make_plans(root, card_size: int):
        executor = _executor(root, profile=profile, candidate_count=2)
        executor.semantic_provider_synthesis_enabled = True
        executor.enabled_semantic_roles = frozenset({
            "candidate_provider_generation", "structure_critique", "coverage_critique",
            "evidence_critique", "arbitration",
        })
        card = {"paper_key": "paper-a", "navigation_text": "x" * card_size}
        monkeypatch.setattr(
            executor_module,
            "build_outline_evidence_views",
            lambda *_: SimpleNamespace(views=[], source_summary_hashes=["summary-set"]),
        )
        monkeypatch.setattr(
            executor_module,
            "build_paper_content_layers",
            lambda *_args, **_kwargs: SimpleNamespace(
                index_cards=[SimpleNamespace(to_dict=lambda: dict(card))],
                content_hash="c" * 64,
                source_summary_hashes=["summary-set"],
            ),
        )

        def candidate_preflight(_summaries, route_profile, *, variant_name, candidate_id=None, **_kwargs):
            candidate = candidate_id or "candidate_1"
            executor._candidate_preflight_projection_state[f"{variant_name}:{candidate}"] = {
                "variant_name": variant_name,
                "candidate_id": candidate,
                "status": "unmaterialized_legacy_paper_shard_projection",
                "projected_call_count": 2,
                "input_cap": 32_000,
                "bound_status": "legacy projection only",
            }
            return 32_000, 2

        monkeypatch.setattr(executor, "_candidate_hierarchical_preflight", candidate_preflight)
        return executor, executor._build_provider_call_plans()

    compact_executor, compact_rows = make_plans(tmp_path / "small", 10)
    large_executor, large_rows = make_plans(tmp_path / "large", 80_000)
    compact_arbitration = next(row for row in compact_rows if row.node_id == "arbitration")
    large_arbitration = next(row for row in large_rows if row.node_id == "arbitration")

    assert compact_arbitration.estimated_input_tokens == large_arbitration.estimated_input_tokens
    assert compact_arbitration.upper_bound is False
    assert any("compact candidate support" in item for item in compact_arbitration.assumptions)
    assert "navigation_text" not in repr(
        large_executor._prospective_arbitration_requests["canonical:arbitration"]
    )
    assert compact_executor._prospective_arbitration_requests["canonical:arbitration"][
        "exact_support_catalog_materialized"
    ] is False


def test_materialized_provider_request_still_fails_the_32k_jit_cap(tmp_path, monkeypatch) -> None:
    profile = ProviderContextProfile.conservative(
        provider="local", model="preflight-model", endpoint_type="chat_completions",
        model_context_limit=128_000, max_output_tokens=4_096,
        reasoning_reserve=0, safety_margin=128,
    )
    executor = _executor(tmp_path, profile=profile, max_source_prompt_tokens=32_000)
    monkeypatch.setattr(executor, "_attach_prompt_authority", lambda _node_id, request: dict(request))
    measured: list[Mapping[str, Any]] = []

    def estimate_request(request: Mapping[str, Any]) -> dict[str, Any]:
        measured.append(request)
        return {"estimated_input_tokens": 33_581, "within_budget": True}

    monkeypatch.setattr(ProviderContextProfile, "estimate_request", lambda _self, request: estimate_request(request))
    with pytest.raises(OutlineV3ExecutionError, match=r"serialized input estimate 33581 exceeds effective input cap 32000"):
        executor._provider_call("arbitration", {"candidate_ids": ["candidate_1"]})
    assert measured


def test_future_preflight_is_provisional_but_keeps_known_call_ceiling_rejection(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    executor = _executor(tmp_path)
    route = executor._role_route("candidate_1_provider_generation")
    plan = OutlineProviderCallPlan(
        artifact_type="outline_provider_call_plan",
        artifact_version="v1",
        job_id=executor.job_id,
        stage_name="outline_v3",
        closure_epoch_id=executor.closure_epoch_id,
        logical_attempt_identity=executor.logical_attempt_identity,
        variant_name="canonical",
        node_id="candidate_1_provider_generation",
        call_id="candidate_1_provider_generation:call",
        provider=route.provider_name,
        model=route.model,
        endpoint_type=route.endpoint_type,
        estimated_input_tokens=1_000,
        estimated_output_tokens=1_000,
        estimated_reasoning_tokens=0,
        estimated_cached_input_tokens=0,
        estimated_cache_write_tokens=0,
        estimated_total_tokens=2_000,
        input_cost_per_1k_tokens=None,
        output_cost_per_1k_tokens=None,
        reasoning_cost_per_1k_tokens=None,
        cache_read_cost_per_1k_tokens=None,
        cache_write_cost_per_1k_tokens=None,
        estimated_cost=None,
        pricing_source="",
        pricing_policy="estimate_only_not_billing_v1",
        cost_status="unknown",
        assumptions=("fixture future request",),
        confidence="medium",
        upper_bound=True,
        transport_expected=True,
        configured_transport_retry_reserve=0,
        physical_attempt_upper_bound=1,
    )

    def build_partial_plan():
        executor._critique_preflight_shards = {}
        executor._candidate_preflight_projection_state = {
            "canonical:candidate_1": {
                "variant_name": "canonical",
                "candidate_id": "candidate_1",
                "status": "unmaterialized_legacy_paper_shard_projection",
                "projected_call_count": 1,
                "input_cap": 32_000,
                "bound_status": "legacy projection only",
            },
        }
        executor._prospective_arbitration_requests = {
            "canonical:arbitration": {
                "status": "awaiting_candidate_and_critique_outputs",
                "exact_support_catalog_materialized": False,
            },
        }
        return (plan,)

    monkeypatch.setattr(executor, "_build_provider_call_plans", build_partial_plan)
    executor._preflight_stability_budget()
    assert executor.stability_preflight["preflight_status"] == "awaiting_exact_future_request_materialization"
    assert executor.stability_preflight["candidate_group_preflight_status"] == "unmaterialized_future_requests"
    assert executor.stability_preflight["prospective_arbitration_request_status"] == "awaiting_exact_candidate_and_critique_outputs"
    assert executor.stability_preflight["cost_status"] == "unknown"
    assert executor.stability_preflight["future_request_input_cap_policy"].startswith("serializer_measure")


def test_finite_call_allowance_still_rejects_known_provider_calls(tmp_path, monkeypatch) -> None:
    executor = _executor(tmp_path)
    executor.max_provider_calls = 0
    route = executor._role_route("candidate_1_provider_generation")
    plan = OutlineProviderCallPlan(
        artifact_type="outline_provider_call_plan",
        artifact_version="v1",
        job_id=executor.job_id,
        stage_name="outline_v3",
        closure_epoch_id=executor.closure_epoch_id,
        logical_attempt_identity=executor.logical_attempt_identity,
        variant_name="canonical",
        node_id="candidate_1_provider_generation",
        call_id="candidate_1_provider_generation:call",
        provider=route.provider_name,
        model=route.model,
        endpoint_type=route.endpoint_type,
        estimated_input_tokens=1_000,
        estimated_output_tokens=1_000,
        estimated_reasoning_tokens=0,
        estimated_cached_input_tokens=0,
        estimated_cache_write_tokens=0,
        estimated_total_tokens=2_000,
        input_cost_per_1k_tokens=None,
        output_cost_per_1k_tokens=None,
        reasoning_cost_per_1k_tokens=None,
        cache_read_cost_per_1k_tokens=None,
        cache_write_cost_per_1k_tokens=None,
        estimated_cost=None,
        pricing_source="",
        pricing_policy="estimate_only_not_billing_v1",
        cost_status="unknown",
        assumptions=("fixture future request",),
        confidence="medium",
        upper_bound=True,
        transport_expected=True,
        configured_transport_retry_reserve=0,
        physical_attempt_upper_bound=1,
    )

    def build_partial_plan():
        executor._critique_preflight_shards = {}
        executor._candidate_preflight_projection_state = {
            "canonical:candidate_1": {
                "variant_name": "canonical", "candidate_id": "candidate_1",
                "status": "unmaterialized_legacy_paper_shard_projection",
                "projected_call_count": 1, "input_cap": 32_000,
                "bound_status": "legacy projection only",
            },
        }
        executor._prospective_arbitration_requests = {
            "canonical:arbitration": {
                "status": "awaiting_candidate_and_critique_outputs",
                "exact_support_catalog_materialized": False,
            },
        }
        return (plan,)

    monkeypatch.setattr(executor, "_build_provider_call_plans", build_partial_plan)
    with pytest.raises(OutlineV3ExecutionError, match="max_provider_calls_exceeded"):
        executor._preflight_stability_budget()
    assert executor.stability_preflight["preflight_status"] == "rejected"
    assert executor.stability_preflight["rejection_reason"] == "max_provider_calls_exceeded"


def test_unmaterialized_call_upper_over_quota_waits_when_known_floor_fits(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    executor = _executor(tmp_path, candidate_count=2)
    executor.max_provider_calls = 1
    executor.max_estimated_total_tokens = 2_000
    executor.max_estimated_cost = 50.0
    executor.input_cost_per_1k_tokens = 1.0
    executor.output_cost_per_1k_tokens = 1.0
    executor.reasoning_cost_per_1k_tokens = 1.0
    executor._pricing_is_explicit = True
    executor._pricing_unknown_due_to_multiple_routes = False
    executor.enabled_semantic_roles = frozenset({"candidate_provider_generation"})
    plan = replace(
        _call_plan(executor, "candidate_1_provider_generation"),
        input_cost_per_1k_tokens=1.0,
        output_cost_per_1k_tokens=1.0,
        reasoning_cost_per_1k_tokens=1.0,
        estimated_cost=2.0,
        cost_status="estimate",
    )

    def future_candidate_shards(*_args, variant_name, candidate_id=None, **_kwargs):
        candidate = candidate_id or "candidate_1"
        executor._candidate_preflight_projection_state[f"{variant_name}:{candidate}"] = {
            "variant_name": variant_name,
            "candidate_id": candidate,
            "status": "unmaterialized_legacy_paper_shard_projection",
            "projected_call_count": 2,
            "input_cap": 32_000,
            "bound_status": "legacy projection only",
        }
        return 32_000, 2

    monkeypatch.setattr(executor, "_build_provider_call_plans", lambda: (plan,))
    monkeypatch.setattr(executor, "_candidate_hierarchical_preflight", future_candidate_shards)
    executor._candidate_preflight_projection_state = {}
    executor._critique_preflight_shards = {}
    executor._preflight_stability_budget()

    assert executor.stability_preflight["known_provider_calls_lower_bound"] == 1
    assert executor.stability_preflight["estimated_provider_calls"] == 3
    assert executor.stability_preflight["known_estimated_total_tokens_lower_bound"] <= 2_000
    assert executor.stability_preflight["estimated_total_tokens"] > 2_000
    assert executor.stability_preflight["known_estimated_cost_lower_bound"] <= 50.0
    assert executor.stability_preflight["estimated_cost_projected_before_materialization"] > 50.0
    assert executor.stability_preflight["preflight_status"] == "awaiting_exact_future_request_materialization"
    assert executor.stability_preflight.get("rejection_reason") is None


def test_known_estimated_total_reserve_over_limit_is_rejected(tmp_path, monkeypatch) -> None:
    executor = _executor(tmp_path)
    executor.enabled_semantic_roles = frozenset({"relation_adjudication"})
    executor.max_estimated_total_tokens = 1_000
    plan = _call_plan(executor, "relation_adjudication")
    provider_posts: list[str] = []
    executor.provider = lambda *_args, **_kwargs: provider_posts.append("unexpected")
    monkeypatch.setattr(executor, "_build_provider_call_plans", lambda: (plan,))
    monkeypatch.setattr(
        executor,
        "_relation_hierarchical_preflight",
        lambda *_args, **_kwargs: (32_000, 1, {"hierarchical_needed": False}),
    )
    executor._critique_preflight_shards = {}

    with pytest.raises(OutlineV3ExecutionError, match="max_estimated_total_tokens_exceeded"):
        executor._preflight_stability_budget()

    assert executor.stability_preflight["known_estimated_total_tokens_lower_bound"] > 1_000
    assert executor.stability_preflight["preflight_status"] == "rejected"
    assert provider_posts == []


def test_known_estimated_cost_reserve_over_limit_is_rejected(tmp_path, monkeypatch) -> None:
    executor = _executor(tmp_path)
    executor.enabled_semantic_roles = frozenset({"relation_adjudication"})
    executor.max_estimated_total_tokens = None
    executor.max_estimated_cost = 1.0
    executor.input_cost_per_1k_tokens = 1.0
    executor.output_cost_per_1k_tokens = 1.0
    executor.reasoning_cost_per_1k_tokens = 1.0
    executor._pricing_is_explicit = True
    executor._pricing_unknown_due_to_multiple_routes = False
    plan = replace(
        _call_plan(executor, "relation_adjudication"),
        input_cost_per_1k_tokens=1.0,
        output_cost_per_1k_tokens=1.0,
        reasoning_cost_per_1k_tokens=1.0,
        estimated_cost=2.0,
        cost_status="estimate",
    )
    provider_posts: list[str] = []
    executor.provider = lambda *_args, **_kwargs: provider_posts.append("unexpected")
    monkeypatch.setattr(executor, "_build_provider_call_plans", lambda: (plan,))
    monkeypatch.setattr(
        executor,
        "_relation_hierarchical_preflight",
        lambda *_args, **_kwargs: (32_000, 1, {"hierarchical_needed": False}),
    )
    executor._critique_preflight_shards = {}

    with pytest.raises(OutlineV3ExecutionError, match="max_estimated_cost_exceeded"):
        executor._preflight_stability_budget()

    assert executor.stability_preflight["known_estimated_cost_lower_bound"] > 1.0
    assert executor.stability_preflight["preflight_status"] == "rejected"
    assert provider_posts == []


def test_cross_reducer_preflight_clears_relations_after_first_level_and_reserves_once(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    executor = _executor(tmp_path)
    executor.enabled_semantic_roles = frozenset({"relation_adjudication"})
    executor.semantic_provider_synthesis_enabled = True
    executor.semantic_relation_candidates = [
        {"relation_id": "relation:fixture", "paper_keys": ["A", "B"]}
    ]
    executor.semantic_cross_group_questions = ["Compare the supported findings."]
    fragments = [
        {
            "topic_id": "topic-1",
            "fragment_id": f"fragment-{index}",
            "paper_ids": ["A" if index % 2 == 0 else "B"],
            "cross_group_item_tokens_upper_bound": 20_000,
        }
        for index in range(4)
    ]
    executor.semantic_topic_ids = ["topic-1"]
    plan = _call_plan(executor, "relation_adjudication")
    monkeypatch.setattr(executor, "_build_provider_call_plans", lambda: (plan,))
    executor._critique_preflight_shards = {}
    monkeypatch.setattr(
        executor,
        "_topic_provider_batch_count",
        lambda *_args: (
            setattr(executor, "semantic_request_plan", [
                {
                    "node_id": "topic_synthesis_provider:batch:1",
                    "request_hash": "a" * 64,
                    "estimated_input_tokens": 1_000,
                    "estimated_output_tokens": 4_096,
                    "estimated_reasoning_tokens": 0,
                    "topic_fragments": fragments,
                    "cross_group_fragment_plans": fragments,
                }
            ]) or 1
        ),
    )
    monkeypatch.setattr(executor, "_semantic_transport_retry_count", lambda: 0)
    monkeypatch.setattr(executor, "_attach_prompt_authority", lambda _node_id, request: dict(request))
    monkeypatch.setattr(
        executor,
        "_relation_hierarchical_preflight",
        lambda *_args, **_kwargs: (32_000, 1, {"hierarchical_needed": False}),
    )

    profile = SimpleNamespace(
        input_budget=32_000,
        max_output_tokens=4_096,
        reasoning_reserve=0,
        endpoint_type="chat_completions",
        estimate_request=lambda request: {
            "estimated_input_tokens": 1_000 + 5_000 * len(request.get("relation_candidates") or ())
        },
        estimate_tokens=lambda request: 1_000 + 5_000 * len(request.get("relation_candidates") or ()),
    )
    monkeypatch.setattr(
        executor,
        "_role_route",
        lambda _node_id: SimpleNamespace(profile=profile, provider_name="local", model="fixture"),
    )

    executor._preflight_stability_budget()

    cross_rows = [
        row for row in executor.stability_preflight["semantic_request_plan"]
        if str(row.get("node_id") or "").startswith("cross_group_comparison_provider")
    ]
    first_level = [row for row in cross_rows if ":reduce:1:" in str(row.get("node_id") or "")]
    later_level = [row for row in cross_rows if row.get("wrapper_level") == "final_relations_cleared"]
    assert len(first_level) == 4
    assert sum(int(row.get("relation_context_reserve_tokens") or 0) for row in first_level) == 5_000
    assert all(row["request_payload_status"] == "output_dependent" for row in first_level)
    assert all(
        row["relation_group_packing_status"] == "first_level_group_capacity_reserves_context_once"
        for row in first_level
    )
    assert len(later_level) == 1
    assert later_level[0]["relation_context_reserve_tokens"] == 0
    assert all(row["static_wrapper_canonical_bytes"] > 0 for row in cross_rows)
    assert executor.stability_preflight["known_provider_calls_lower_bound"] == 4
    assert executor.stability_preflight["known_token_reserve_floor_basis"][
        "exact_hashed_topic_request_count"
    ] == 1
    assert executor.stability_preflight["semantic_reducer_call_bound_status"] == (
        "output_dependent_upper_bound_pending_materialization"
    )
    assert executor.stability_preflight["preflight_status"] == (
        "awaiting_exact_future_request_materialization"
    )
