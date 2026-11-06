"""Topic materialization is bound to the exact current plan before transport."""
from __future__ import annotations

from copy import deepcopy
import sys

import pytest

from outline.v3_executor import OutlineV3ExecutionError
from runtime.source_revision import PROVIDER_SEMANTIC_CONTRACT_V2
from tests.test_topic_source_scope import source_case
from tests.test_topic_source_scope_integration import _topics


def _prepared(tmp_path, version):
    case = source_case.__wrapped__(tmp_path)[0]
    executor = case["executor"]
    executor.provider_semantic_contract_version = version
    topics, routes = _topics(case)
    _expanded, batches, rows = executor._plan_topic_provider_batches(
        topics, topic_routes=routes, evidence_model=case["evidence"],
        content_layers_model=case["layers"],
        profile=executor._node_route("candidate_1_provider_generation").profile,
    )
    executor.semantic_request_plan = rows
    return case, routes, batches


def _materialize(case, routes, batch):
    executor = case["executor"]
    request = executor._build_topic_provider_request(
        batch, topic_routes=routes, evidence_model=case["evidence"],
        content_layers_model=case["layers"], batch_index=1,
    )
    return executor._topic_provider_node_id(request, 1), request


@pytest.mark.parametrize("version", [None, PROVIDER_SEMANTIC_CONTRACT_V2])
def test_unchanged_actual_topic_request_matches_the_snapshot_plan(tmp_path, version):
    case, routes, batches = _prepared(tmp_path, version)
    node, request = _materialize(case, routes, batches[0])
    case["executor"]._verify_topic_request_matches_plan(node, request)


@pytest.mark.parametrize("version", [None, PROVIDER_SEMANTIC_CONTRACT_V2])
def test_selected_source_drift_after_planning_rejects_before_transport(tmp_path, version):
    case, routes, batches = _prepared(tmp_path, version)
    selected = batches[0][0].paper_ids[0]
    case["layers"].dossier_by_paper[selected].overall_context.append("A new unplanned source restriction.")
    node, request = _materialize(case, routes, batches[0])
    with pytest.raises(OutlineV3ExecutionError, match="replan sources before transport"):
        case["executor"]._verify_topic_request_matches_plan(node, request)


@pytest.mark.parametrize("fault", ["missing", "duplicate", "batch_id", "prompt_authority"])
def test_missing_or_changed_current_plan_cannot_admit_topic_request(tmp_path, fault):
    case, routes, batches = _prepared(tmp_path, PROVIDER_SEMANTIC_CONTRACT_V2)
    executor = case["executor"]
    node, request = _materialize(case, routes, batches[0])
    row = next(row for row in executor.semantic_request_plan if row["node_id"] == node)
    if fault == "missing":
        executor.semantic_request_plan = []
    elif fault == "duplicate":
        executor.semantic_request_plan.append(deepcopy(row))
    elif fault == "batch_id":
        row["batch_id"] = "foreign-batch"
    else:
        row["request_hash"] = "0" * 64
    with pytest.raises(OutlineV3ExecutionError, match="replan sources before transport"):
        executor._verify_topic_request_matches_plan(node, request)


@pytest.mark.parametrize("version", [None, PROVIDER_SEMANTIC_CONTRACT_V2])
def test_run_rejects_materialization_drift_before_the_topic_provider_is_called(tmp_path, monkeypatch, version):
    from tests.test_outline_v3_semantic_execution import _configured_test_provider, _executor

    calls = []

    def provider(node, request):
        calls.append(node)
        return _configured_test_provider(node, request)

    executor = _executor(tmp_path, provider=provider, stability_mode="off", max_provider_calls=128)
    executor.semantic_provider_synthesis_enabled = True
    executor.provider_semantic_contract_version = version
    original = executor._build_topic_provider_request
    original_semantic_call = executor._run_semantic_provider_call
    semantic_calls = []
    injected = []

    def semantic_call(node, *args, **kwargs):
        semantic_calls.append(node)
        return original_semantic_call(node, *args, **kwargs)

    def changed_request(topics, *args, **kwargs):
        if not injected and sys._getframe(1).f_code.co_name == "run":
            paper = topics[0].paper_ids[0]
            kwargs["content_layers_model"].dossier_by_paper[paper].overall_context.append(
                "An unplanned restriction added after the source snapshot closed."
            )
            injected.append(paper)
        return original(topics, *args, **kwargs)

    monkeypatch.setattr(executor, "_build_topic_provider_request", changed_request)
    monkeypatch.setattr(executor, "_run_semantic_provider_call", semantic_call)
    outcome = executor.run()
    assert outcome.status == "blocked" and not outcome.adopted
    assert any("replan sources before transport" in message for message in outcome.diagnostics), outcome.diagnostics
    assert injected, outcome.diagnostics
    assert not any(node.startswith("topic_synthesis_provider:") for node in semantic_calls)
    assert not any(node.startswith("topic_synthesis_provider:") for node in calls)
