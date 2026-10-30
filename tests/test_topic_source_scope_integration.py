"""Actual producer/planner wiring for the explicit selected-source contract."""
from __future__ import annotations

from copy import deepcopy

import pytest

from outline.semantic_chunking import TopicRoute
from outline.v3_executor import OutlineV3ExecutionError
from outline.v3_models import TopicSynthesis
from runtime.provider_runtime import hash_json
from runtime.source_revision import PROVIDER_SEMANTIC_CONTRACT_V2
from tests.test_topic_source_scope import _request, source_case as _shared_source_case


@pytest.fixture(name="source_case")
def selected_source_case(tmp_path):
    return _shared_source_case.__wrapped__(tmp_path)


def _wire(version, *, papers=("paper-4", "paper-5"), ordinal=1):
    executor = version["executor"]
    request = _request(version, papers=papers, ordinal=ordinal)
    node = executor._topic_provider_node_id(request, ordinal)
    return executor._attach_prompt_authority(node, request)


def test_actual_v2_builder_and_prompt_authority_are_stable_for_unaffected_sources(source_case):
    before, after = source_case
    for version in source_case:
        version["executor"].provider_semantic_contract_version = PROVIDER_SEMANTIC_CONTRACT_V2
    old, new = _wire(before), _wire(after, ordinal=99)
    assert old == new and hash_json(old) == hash_json(new)
    assert old["provider_semantic_contract_version"] == PROVIDER_SEMANTIC_CONTRACT_V2
    assert "content_layers_hash" not in old["evidence_projection"]
    assert old["evidence_projection"]["selected_source_projection"]["paper_keys"] == ["paper-4", "paper-5"]
    assert _wire(before, papers=("paper-0", "paper-9")) != _wire(after, papers=("paper-0", "paper-9"))


def _topics(version):
    topics, routes = [], {}
    for name, papers in (("U45", ["paper-4", "paper-5"]), ("A09", ["paper-0", "paper-9"])):
        topic = TopicSynthesis(
            topic_id=f"topic:{name}", fragment_id=f"fragment:{name}", paper_ids=papers,
            supporting_evidence_ids=[evidence for paper in papers
                                     for evidence in version["layers"].dossier_by_paper[paper].evidence_ids],
        )
        topics.append(topic)
        routes[topic.topic_id] = TopicRoute(
            topic_id=topic.topic_id, question="Compare the selected findings and source boundaries.",
            paper_ids=papers, dimensions=["finding", "boundary"],
        )
    return topics, routes


def test_v2_planner_keeps_distinct_ownership_calls_independent_and_exact(source_case):
    for version in source_case:
        executor = version["executor"]
        executor.provider_semantic_contract_version = PROVIDER_SEMANTIC_CONTRACT_V2
        topics, routes = _topics(version)
        _expanded, batches, rows = executor._plan_topic_provider_batches(
            topics, topic_routes=routes, evidence_model=version["evidence"],
            content_layers_model=version["layers"],
            profile=executor._node_route("candidate_1_provider_generation").profile,
        )
        assert len(batches) == len(rows) == 2
        for index, (batch, row) in enumerate(zip(batches, rows, strict=True), start=1):
            request = executor._build_topic_provider_request(
                batch, topic_routes=routes, evidence_model=version["evidence"],
                content_layers_model=version["layers"], batch_index=index,
            )
            assert row["node_id"] == executor._topic_provider_node_id(request, index)
            assert row["batch_id"] == request["hierarchy"]["batch_id"]
            assert len({tuple(sorted((*topic.paper_ids, *topic.bridge_paper_ids))) for topic in batch}) == 1


def test_v2_preflight_identity_and_pilot_materialization_use_stable_batch_ids(source_case):
    for version in source_case:
        executor = version["executor"]
        executor.provider_semantic_contract_version = PROVIDER_SEMANTIC_CONTRACT_V2
        topics, routes = _topics(version)
        _expanded, batches, rows = executor._plan_topic_provider_batches(
            topics, topic_routes=routes, evidence_model=version["evidence"],
            content_layers_model=version["layers"],
            profile=executor._node_route("candidate_1_provider_generation").profile,
        )
        first_identity_hash = executor._compute_topic_provider_plan_identity_hash(rows)
        _expanded, reversed_batches, reversed_rows = executor._plan_topic_provider_batches(
            list(reversed(topics)), topic_routes=routes, evidence_model=version["evidence"],
            content_layers_model=version["layers"],
            profile=executor._node_route("candidate_1_provider_generation").profile,
        )
        assert executor._compute_topic_provider_plan_identity_hash(reversed_rows) == first_identity_hash

        selected = rows[0]
        output_cap = executor._semantic_output_token_limit(
            executor._node_route("candidate_1_provider_generation").profile
        )
        executor.outline_pilot = {
            "selected_topic_batch_ids": [selected["node_id"]],
            "selected_request_hashes": {selected["node_id"]: selected["request_hash"]},
            "source_summary_set_hash": hash_json(executor.summaries),
            "acceptance_run_id": "test-acceptance-run",
            "max_physical_attempts": 10,
            "max_output_tokens_all_attempts": 10 * output_cap,
        }
        materialized = executor._materialize_topic_pilot_plan(
            topic_batches=batches,
            topic_routes=routes,
            evidence_model=version["evidence"],
            content_layers_model=version["layers"],
            profile=executor._node_route("candidate_1_provider_generation").profile,
        )
        assert len(materialized) == 1
        batch_index, batch, request = materialized[0]
        assert batch_index in {1, 2}
        assert executor._topic_provider_node_id(batch, batch_index) == selected["node_id"]
        assert request["hierarchy"]["batch_identity_hash"]
        assert request["hierarchy"]["batch_id"] != f"topic_batch_{batch_index}"


def test_legacy_builder_retains_global_hash_and_ordinal_identity(source_case):
    version = source_case[0]
    executor = version["executor"]
    first = _request(version, ordinal=2)
    second = _request(version, ordinal=3)
    assert "provider_semantic_contract_version" not in first
    assert first["evidence_projection"]["content_layers_hash"] == version["layers"].content_hash
    assert first["hierarchy"]["batch_id"] == "topic_batch_2"
    assert executor._topic_provider_node_id(first, 2) == "topic_synthesis_provider:batch:2"
    restored = deepcopy(second)
    restored["hierarchy"]["batch_id"] = "topic_batch_2"
    assert first == restored


def test_full_identity_collision_cannot_be_accepted_from_short_node_id(source_case, monkeypatch):
    import outline.v3_executor as module

    executor = source_case[0]["executor"]
    executor.provider_semantic_contract_version = PROVIDER_SEMANTIC_CONTRACT_V2
    request = _request(source_case[0])
    node = executor._topic_provider_node_id(request, 1)
    real = module.stable_topic_batch_identity_v2(request)
    monkeypatch.setattr(module, "stable_topic_batch_identity_v2", lambda _: {
        **real, "provider_node_id": node, "identity_hash": "0" * 64,
    })
    with pytest.raises(OutlineV3ExecutionError, match="identity collision"):
        executor._topic_provider_node_id(request, 2)
