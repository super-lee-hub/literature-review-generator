from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import time

import pytest

from outline.semantic_chunking import (
    TopicRoute,
    build_paper_content_layers,
    build_semantic_chunk_plan,
    build_topic_synthesis_plan,
)
from outline.v3_evidence import (
    build_global_corpus_ledger,
    build_multi_view_matrix,
    build_outline_evidence_views,
    build_review_intent,
)
from outline.v3_relations import build_global_relation_map
from outline.v3_executor import OutlineV3Executor
from outline.v3_models import TopicSynthesis
from runtime.pause_state import PauseRequestedError
from runtime.provider_runtime import (
    ProviderAggregateBudgetV2,
    ProviderBudgetController,
    ProviderBudgetExceeded,
    authorized_provider_call_limit,
)
from tests.test_outline_v3_semantic_execution import _executor


def _topic(executor: OutlineV3Executor, question: str = "Assess the finding") -> tuple[TopicSynthesis, dict[str, TopicRoute], object, object]:
    evidence = build_outline_evidence_views(executor.summaries, executor.job_id)
    layers = build_paper_content_layers(executor.summaries, evidence, job_id=executor.job_id)
    topic = TopicSynthesis(
        topic_id="topic:control",
        fragment_id="fragment:control",
        question=question,
        paper_ids=["paper-a"],
        supporting_evidence_ids=list(layers.dossier_by_paper["paper-a"].evidence_ids),
    )
    route = TopicRoute(topic_id=topic.topic_id, question=question, paper_ids=list(topic.paper_ids), dimensions=["finding"])
    return topic, {topic.topic_id: route}, evidence, layers


def test_topic_planning_observes_cancellation_before_second_topic(tmp_path: Path) -> None:
    executor = _executor(tmp_path, stability_mode="off")
    topic, routes, evidence, layers = _topic(executor)
    calls = 0
    original_build = executor._build_topic_provider_request

    def cancel() -> None:
        nonlocal calls
        calls += 1
        if calls >= 3:
            raise RuntimeError("cancelled")

    executor.cancellation_checker = cancel
    # The post-build boundary must observe a cancellation requested during
    # request serialization, before a second topic is started.
    def build_then_cancel(*args, **kwargs):
        nonlocal calls
        result = original_build(*args, **kwargs)
        calls = max(calls, 1)
        return result
    executor._build_topic_provider_request = build_then_cancel
    with pytest.raises(RuntimeError, match="cancelled"):
        executor._plan_topic_provider_batches(
            [topic, replace(topic, topic_id="topic:control-2", fragment_id="fragment:control-2")],
            topic_routes={**routes, "topic:control-2": replace(routes[topic.topic_id], topic_id="topic:control-2")},
            evidence_model=evidence,
            content_layers_model=layers,
            profile=executor.profile,
        )


def test_topic_planning_observes_durable_pause(tmp_path: Path) -> None:
    executor = _executor(tmp_path, stability_mode="off")
    topic, routes, evidence, layers = _topic(executor)
    original_build = executor._build_topic_provider_request
    def build_then_pause(*args, **kwargs):
        result = original_build(*args, **kwargs)
        executor._pause_state.request(requested_by="test", reason="pause during planning")
        return result
    executor._build_topic_provider_request = build_then_pause
    with pytest.raises(PauseRequestedError):
        executor._plan_topic_provider_batches(
            [topic], topic_routes=routes, evidence_model=evidence,
            content_layers_model=layers, profile=executor.profile,
        )


def test_topic_planning_observes_original_durable_deadline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    executor = _executor(tmp_path, stability_mode="off")
    controller = ProviderBudgetController(ProviderAggregateBudgetV2(
        max_provider_calls_total=10, max_output_tokens_total=1000,
        max_retry_attempts_total=0, max_wall_seconds=30,
    ))
    controller._absolute_deadline_epoch = time.time() + 30
    monkeypatch.setattr("outline.v3_executor.provider_budget_controller_from_environment", lambda: controller)
    topic, routes, evidence, layers = _topic(executor)
    original_build = executor._build_topic_provider_request
    def build_then_expire(*args, **kwargs):
        result = original_build(*args, **kwargs)
        controller._absolute_deadline_epoch = 1.0
        return result
    executor._build_topic_provider_request = build_then_expire
    with pytest.raises(ProviderBudgetExceeded, match="local planning"):
        executor._plan_topic_provider_batches([topic], topic_routes=routes, evidence_model=evidence, content_layers_model=layers, profile=executor.profile)


def test_topic_plan_cache_reuses_exact_plan_and_invalidates_identity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    executor = _executor(tmp_path, stability_mode="off")
    topic, routes, evidence, layers = _topic(executor)
    original = executor._plan_topic_provider_batches_from_snapshot
    calls = 0

    def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(executor, "_plan_topic_provider_batches_from_snapshot", counted)
    executor._plan_topic_provider_batches([topic], topic_routes=routes, evidence_model=evidence, content_layers_model=layers, profile=executor.profile)
    executor._plan_topic_provider_batches([topic], topic_routes=routes, evidence_model=evidence, content_layers_model=layers, profile=executor.profile)
    assert calls == 1

    changed_topic = replace(topic, question="Changed source question")
    changed_route = {topic.topic_id: replace(routes[topic.topic_id], question="Changed source question")}
    executor._plan_topic_provider_batches([changed_topic], topic_routes=changed_route, evidence_model=evidence, content_layers_model=layers, profile=executor.profile)
    assert calls == 2

    # Mutating a source fact invalidates the snapshot-derived identity even
    # when callers continue passing the same topic and projected hash fields.
    layers.dossiers[0].findings.append("new source fact")
    executor._plan_topic_provider_batches([topic], topic_routes=routes, evidence_model=evidence, content_layers_model=layers, profile=executor.profile)
    assert calls == 3

    changed_profile = replace(executor.profile, model="outline-v3-changed")
    executor._plan_topic_provider_batches([topic], topic_routes=routes, evidence_model=evidence, content_layers_model=layers, profile=changed_profile)
    assert calls == 4


def test_topic_plan_cache_reuses_between_preflight_and_execution_models(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    executor = _executor(tmp_path, stability_mode="off")
    original = executor._plan_topic_provider_batches_from_snapshot
    calls = 0

    def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(executor, "_plan_topic_provider_batches_from_snapshot", counted)
    assert executor._topic_provider_batch_count(executor.summaries, executor.profile) >= 1
    assert calls == 1

    # Rebuild the same semantic inputs through the execution path, as run()
    # does after preflight. The snapshot-derived key should match despite
    # separate model instances, so the complete planner is not repeated.
    evidence = build_outline_evidence_views(executor.summaries, executor.job_id)
    ledger = build_global_corpus_ledger(evidence)
    matrix = build_multi_view_matrix(evidence)
    relation_map = build_global_relation_map(
        evidence, matrix, ledger,
        required_comparisons=build_review_intent(executor.review_intent_input).required_comparisons,
    )
    layers = build_paper_content_layers(executor.summaries, evidence, job_id=executor.job_id)
    semantic_plan = build_semantic_chunk_plan(
        layers,
        relation_map,
        candidate_count=executor.candidate_count,
        physical_call_limit=authorized_provider_call_limit(executor.max_provider_calls),
    )
    topic_plan = build_topic_synthesis_plan(semantic_plan)
    routes = {topic.topic_id: topic for topic in semantic_plan.topics}
    executor._plan_topic_provider_batches(
        topic_plan,
        topic_routes=routes,
        evidence_model=evidence,
        content_layers_model=layers,
        profile=executor.profile,
    )
    assert calls == 1
