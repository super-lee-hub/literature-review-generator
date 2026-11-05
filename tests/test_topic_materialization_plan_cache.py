from __future__ import annotations

import json
import socket
from dataclasses import replace
from typing import Any, Mapping

import pytest

from outline.semantic_chunking import TopicRoute
from outline.topic_materialization_snapshot import snapshot_topic_materialization
from outline.v3_models import PaperEvidenceDossier, TopicSynthesis
from runtime.provider_runtime import hash_json
from tests.test_topic_source_scope import source_case


@pytest.fixture(autouse=True)
def _deny_provider_transport(monkeypatch):
    def denied(*_args, **_kwargs):
        pytest.fail("topic materialization planning must not dispatch provider transport")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)


@pytest.fixture
def source_scope_case(tmp_path):
    # Match the existing materialization tests' fixture without importing a fixture
    # across test modules.
    return getattr(source_case, "__wrapped__")(tmp_path)


def _topic_route(
    version: Mapping[str, Any],
    *,
    papers: tuple[str, ...] = ("paper-4", "paper-5"),
    dimensions: tuple[str, ...] = ("finding", "boundary"),
    supporting_ids: list[str] | None = None,
    chunk_target: int = 0,
    include_unbound: bool = False,
) -> tuple[TopicSynthesis, TopicRoute]:
    dossiers = version["layers"].dossier_by_paper
    if supporting_ids is None:
        supporting_ids = sorted(
            {
                str(value)
                for paper_id in papers
                for value in dossiers[paper_id].evidence_ids
                if str(value)
            }
        )
    topic_id = "topic:materialization-cache"
    topic = TopicSynthesis(
        topic_id=topic_id,
        fragment_id="fragment:materialization-cache-v1",
        paper_ids=list(papers),
        supporting_evidence_ids=list(supporting_ids),
        evidence_chunk_target_tokens=chunk_target,
    )
    route = TopicRoute(
        topic_id=topic_id,
        question="Compare findings and their recorded boundaries.",
        paper_ids=list(papers),
        dimensions=list(dimensions),
        include_unbound_claims=include_unbound,
    )
    return topic, route


def _build_request(
    executor,
    topic: TopicSynthesis,
    route: TopicRoute,
    evidence_model: Any,
    content_layers_model: Any,
    *,
    materialization_cache: dict[tuple[Any, ...], list[dict[str, Any]]] | None,
) -> dict[str, Any]:
    return executor._build_topic_provider_request(
        [topic],
        topic_routes={topic.topic_id: route},
        evidence_model=evidence_model,
        content_layers_model=content_layers_model,
        batch_index=1,
        materialization_cache=materialization_cache,
    )


def _plan_payload(result) -> dict[str, Any]:
    expanded, batches, plan_rows = result
    return {
        "expanded": [topic.to_dict() for topic in expanded],
        "batches": [[topic.to_dict() for topic in batch] for batch in batches],
        "plan_rows": plan_rows,
    }


def _compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _with_paper_set(topic: TopicSynthesis, paper_ids: tuple[str, ...], layers) -> TopicSynthesis:
    # Match _split_topic_plan_for_budget.with_paper_set for measured split inputs.
    selected_papers = sorted({str(value) for value in paper_ids if str(value)})
    dossiers = layers.dossier_by_paper
    selected_evidence_ids = {
        str(value)
        for paper_id in selected_papers
        for value in getattr(dossiers.get(paper_id), "evidence_ids", [])
        if str(value)
    }
    return replace(
        topic,
        paper_ids=selected_papers,
        bridge_paper_ids=[
            value for value in topic.bridge_paper_ids if value in selected_papers
        ],
        fragment_id=f"{topic.topic_id}:papers:{hash_json(selected_papers)[:12]}",
        evidence_unit_indexes={},
        evidence_chunk_target_tokens=0,
        supporting_evidence_ids=[
            value for value in topic.supporting_evidence_ids
            if str(value) in selected_evidence_ids
        ],
    )


def _estimated_topic_request_tokens(
    executor,
    topic: TopicSynthesis,
    route: TopicRoute,
    evidence_model: Any,
    content_layers_model: Any,
    profile,
    *,
    batch_index: int,
) -> int:
    request = _build_request(
        executor,
        topic,
        route,
        evidence_model,
        content_layers_model,
        materialization_cache=None,
    )
    node_id = f"topic_synthesis:preflight:{batch_index}"
    attached = executor._attach_prompt_authority(node_id, request)
    budget = profile.estimate_request(attached)
    return int(budget.get("estimated_input_tokens") or profile.estimate_tokens(request))


def test_request_cache_preserves_hash_and_isolates_mutated_returns(
    source_scope_case, monkeypatch
) -> None:
    version = source_scope_case[0]
    executor = version["executor"]
    topic, route = _topic_route(version)
    materializations: list[tuple[str, Mapping[str, Any]]] = []
    original = executor._complete_topic_evidence_units

    def counted(view, dossier, **kwargs):
        assert kwargs.get("chunk_indexes") is None
        assert kwargs.get("token_estimator") is None
        materializations.append((str(view.paper_key), kwargs))
        return original(view, dossier, **kwargs)

    monkeypatch.setattr(executor, "_complete_topic_evidence_units", counted)

    with snapshot_topic_materialization(version["evidence"], version["layers"]) as snapshot:
        assert snapshot.cached is True
        uncached = _build_request(
            executor,
            topic,
            route,
            snapshot.evidence_model,
            snapshot.content_layers_model,
            materialization_cache=None,
        )
        expected_hash = hash_json(uncached)

        materializations.clear()
        cache: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
        first = _build_request(
            executor,
            topic,
            route,
            snapshot.evidence_model,
            snapshot.content_layers_model,
            materialization_cache=cache,
        )
        second = _build_request(
            executor,
            topic,
            route,
            snapshot.evidence_model,
            snapshot.content_layers_model,
            materialization_cache=cache,
        )

        assert hash_json(first) == expected_hash
        assert hash_json(second) == expected_hash
        assert _compact_json(first) == _compact_json(uncached)
        assert _compact_json(second) == _compact_json(uncached)
        assert len(materializations) == len(topic.paper_ids)

        unit = first["evidence_units"][0]
        assert unit["source_field_ledger"]
        unit["source_field_ledger"][0]["source_value"] = "caller-only cache poison"

        third = _build_request(
            executor,
            topic,
            route,
            snapshot.evidence_model,
            snapshot.content_layers_model,
            materialization_cache=cache,
        )
        assert hash_json(third) == expected_hash
        assert len(materializations) == len(topic.paper_ids)


@pytest.mark.parametrize(
    ("changed", "expected_misses"),
    [
        ("fields", 2),
        ("required_ids", 2),
        ("chunk_target", 2),
        ("include_unbound", 2),
    ],
)
def test_request_cache_separates_callsite_materialization_inputs(
    source_scope_case, monkeypatch, changed: str, expected_misses: int
) -> None:
    version = source_scope_case[0]
    executor = version["executor"]
    base_topic, base_route = _topic_route(version)
    materializations: list[str] = []
    original = executor._complete_topic_evidence_units

    def counted(view, dossier, **kwargs):
        materializations.append(str(view.paper_key))
        return original(view, dossier, **kwargs)

    monkeypatch.setattr(executor, "_complete_topic_evidence_units", counted)

    with snapshot_topic_materialization(version["evidence"], version["layers"]) as snapshot:
        cache: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
        _build_request(
            executor, base_topic, base_route,
            snapshot.evidence_model, snapshot.content_layers_model,
            materialization_cache=cache,
        )
        assert len(materializations) == len(base_topic.paper_ids)

        if changed == "fields":
            topic, route = _topic_route(version, dimensions=("method",))
        elif changed == "required_ids":
            topic, route = _topic_route(version, supporting_ids=[])
        elif changed == "chunk_target":
            topic, route = _topic_route(version, chunk_target=100_000)
        else:
            topic, route = _topic_route(version, include_unbound=True)

        cached_variant = _build_request(
            executor, topic, route,
            snapshot.evidence_model, snapshot.content_layers_model,
            materialization_cache=cache,
        )
        assert len(materializations) == len(base_topic.paper_ids) + expected_misses

        uncached_variant = _build_request(
            executor, topic, route,
            snapshot.evidence_model, snapshot.content_layers_model,
            materialization_cache=None,
        )
        assert hash_json(cached_variant) == hash_json(uncached_variant)
        assert _compact_json(cached_variant) == _compact_json(uncached_variant)


@pytest.mark.parametrize("split", [False, True], ids=["whole-topic", "paper-split"])
def test_snapshot_planner_rows_match_with_and_without_request_cache(
    source_scope_case, monkeypatch, split: bool
) -> None:
    version = source_scope_case[0]
    executor = version["executor"]
    topic, route = _topic_route(version)
    profile = executor._role_route("candidate_1_provider_generation").profile
    materializations: list[str] = []
    original = executor._complete_topic_evidence_units

    def counted(view, dossier, **kwargs):
        materializations.append(str(view.paper_key))
        return original(view, dossier, **kwargs)

    monkeypatch.setattr(executor, "_complete_topic_evidence_units", counted)

    with snapshot_topic_materialization(version["evidence"], version["layers"]) as snapshot:
        assert snapshot.cached is True
        if split:
            full_estimate = _estimated_topic_request_tokens(
                executor,
                topic,
                route,
                snapshot.evidence_model,
                snapshot.content_layers_model,
                profile,
                batch_index=1,
            )
            single_estimates = [
                _estimated_topic_request_tokens(
                    executor,
                    _with_paper_set(topic, (paper_id,), snapshot.content_layers_model),
                    route,
                    snapshot.evidence_model,
                    snapshot.content_layers_model,
                    profile,
                    batch_index=index,
                )
                for index, paper_id in enumerate(topic.paper_ids, start=1)
            ]
            max_single_estimate = max(single_estimates)
            assert 0 < max_single_estimate < full_estimate
            assert full_estimate - max_single_estimate >= 2
            cap = (max_single_estimate + full_estimate) // 2
            assert max_single_estimate < cap < full_estimate
            assert cap < profile.input_budget
            executor.max_source_prompt_tokens = cap
            materializations.clear()

        uncached = executor._plan_topic_provider_batches_from_snapshot(
            [topic],
            topic_routes={topic.topic_id: route},
            evidence_model=snapshot.evidence_model,
            content_layers_model=snapshot.content_layers_model,
            profile=profile,
            materialization_cache=None,
        )
        uncached_hash = hash_json(_plan_payload(uncached))
        uncached_count = len(materializations)
        materializations.clear()

        cached = executor._plan_topic_provider_batches_from_snapshot(
            [topic],
            topic_routes={topic.topic_id: route},
            evidence_model=snapshot.evidence_model,
            content_layers_model=snapshot.content_layers_model,
            profile=profile,
            materialization_cache={},
        )
        assert hash_json(_plan_payload(cached)) == uncached_hash
        assert _compact_json(_plan_payload(cached)) == _compact_json(_plan_payload(uncached))
        assert [row["request_hash"] for row in cached[2]] == [
            row["request_hash"] for row in uncached[2]
        ]
        assert len(materializations) < uncached_count
        if split:
            assert len(uncached[0]) > 1


def test_new_snapshot_scope_materializes_changed_qualifier(
    source_scope_case,
) -> None:
    before, after = source_scope_case
    qualifier = "Paper 9 applies only in the revised measured subgroup."

    def request_for(version):
        topic, route = _topic_route(
            version,
            papers=("paper-0", "paper-9"),
            dimensions=("context",),
        )
        with snapshot_topic_materialization(version["evidence"], version["layers"]) as snapshot:
            assert snapshot.cached is True
            return _build_request(
                version["executor"],
                topic,
                route,
                snapshot.evidence_model,
                snapshot.content_layers_model,
                materialization_cache={},
            )

    old_request = request_for(before)
    new_request = request_for(after)
    assert hash_json(old_request) != hash_json(new_request)
    assert qualifier not in json.dumps(old_request, ensure_ascii=False)
    assert qualifier in json.dumps(new_request, ensure_ascii=False)


def test_passthrough_snapshot_forwards_no_materialization_cache(
    source_scope_case, monkeypatch
) -> None:
    version = source_scope_case[0]
    executor = version["executor"]
    layers = version["layers"]

    class CustomDossier(PaperEvidenceDossier):
        pass

    source = layers.dossier_by_paper["paper-4"]
    custom = CustomDossier.from_dict(source.to_dict())
    dossier_index = next(
        index for index, item in enumerate(layers.dossiers) if item.paper_id == "paper-4"
    )
    layers.dossiers[dossier_index] = custom

    topic, route = _topic_route(version)
    captured: list[Any] = []

    def capture(*_args, **kwargs):
        captured.append(kwargs.get("materialization_cache"))
        return [], [], []

    monkeypatch.setattr(executor, "_plan_topic_provider_batches_from_snapshot", capture)
    executor._plan_topic_provider_batches(
        [topic],
        topic_routes={topic.topic_id: route},
        evidence_model=version["evidence"],
        content_layers_model=layers,
        profile=executor._role_route("candidate_1_provider_generation").profile,
    )

    assert captured == [None]
