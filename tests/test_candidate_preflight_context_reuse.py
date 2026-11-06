"""Regression coverage for operation-local candidate preflight context reuse."""

from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import pytest

from outline import v3_executor as executor_module
from tests.test_outline_v3_semantic_execution import _summary
from tests.test_run_first_future_preflight import _executor


def _fixture_context(executor, monkeypatch: pytest.MonkeyPatch):
    papers = ("A", "B")
    evidence = SimpleNamespace(
        views=[SimpleNamespace(paper_key=key, view_hash=f"view:{key}", to_dict=lambda key=key: {"paper_key": key, "view_hash": f"view:{key}"}) for key in papers],
        source_summary_hashes=[f"summary:{key}" for key in papers],
    )
    ledger = object()
    matrix = object()
    relation = SimpleNamespace(
        relation_id="relation:fixture",
        to_dict=lambda: {"relation_id": "relation:fixture", "paper_keys": list(papers)},
    )
    candidates = SimpleNamespace(relations=[relation])
    layers = SimpleNamespace(
        dossier_by_paper={},
        index_cards=[SimpleNamespace(to_dict=lambda key=key: {"paper_key": key}) for key in papers],
        content_hash="layers:" + ":".join(papers),
        source_summary_hashes=[f"summary:{key}" for key in papers],
    )
    semantic_plan = SimpleNamespace(coverage={"selected_relation_ids": []}, topics=[])
    monkeypatch.setattr(executor_module, "build_outline_evidence_views", lambda *_: evidence)
    monkeypatch.setattr(executor_module, "build_global_corpus_ledger", lambda *_: ledger)
    monkeypatch.setattr(executor_module, "build_multi_view_matrix", lambda *_: matrix)
    monkeypatch.setattr(executor_module, "build_global_relation_map", lambda *_args, **_kwargs: candidates)
    monkeypatch.setattr(executor_module, "build_paper_content_layers", lambda *_args, **_kwargs: layers)
    monkeypatch.setattr(executor_module, "build_semantic_chunk_plan", lambda *_args, **_kwargs: semantic_plan)
    monkeypatch.setattr(executor, "_attach_prompt_authority", lambda _node_id, request: dict(request))
    monkeypatch.setattr(executor, "_relation_packing_target", lambda _profile: 10**9)
    context = {
        "evidence": evidence,
        "ledger": ledger,
        "matrix": matrix,
        "candidates": candidates,
        "content_layers": layers,
        "semantic_plan": semantic_plan,
    }
    return context


def test_cached_preflight_context_preserves_projection_and_skips_source_rebuilds(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    executor = _executor(
        tmp_path,
        summaries=[
            _summary("A", "Paper A", "The effect was negative."),
            _summary("B", "Paper B", "The effect was zero."),
        ],
        candidate_count=2,
    )
    context = _fixture_context(executor, monkeypatch)
    profile = executor._role_route("candidate_1_provider_generation").profile
    calls = {name: 0 for name in (
        "build_outline_evidence_views", "build_global_corpus_ledger",
        "build_multi_view_matrix", "build_global_relation_map",
        "build_paper_content_layers", "build_semantic_chunk_plan",
    )}
    for name in calls:
        original = getattr(executor_module, name)

        def wrapped(*args, _name=name, _original=original, **kwargs):
            calls[_name] += 1
            return _original(*args, **kwargs)

        monkeypatch.setattr(executor_module, name, wrapped)

    uncached = executor._candidate_hierarchical_preflight(
        executor.summaries, profile, variant_name="canonical", candidate_id="candidate_1",
    )
    assert calls == {name: 1 for name in calls}
    for name in calls:
        calls[name] = 0

    cached = executor._candidate_hierarchical_preflight(
        executor.summaries, profile, variant_name="canonical", candidate_id="candidate_1",
        preflight_context=context,
    )
    assert cached == uncached
    assert calls == {name: 0 for name in calls}


def test_build_provider_call_plans_reuses_context_without_changing_plan_rows(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    executor = _executor(
        tmp_path,
        summaries=[
            _summary("A", "Paper A", "The effect was negative."),
            _summary("B", "Paper B", "The effect was zero."),
        ],
        candidate_count=5,
    )
    executor.enabled_semantic_roles = frozenset({"candidate_provider_generation"})
    executor._relation_packing_target = lambda _profile: 10**9
    original_preflight = executor._candidate_hierarchical_preflight
    original_summaries = copy.deepcopy(executor.summaries)
    request_hashes: list[tuple[str, str]] = []
    original_attach = executor._attach_prompt_authority

    def capture_request(node_id, request):
        enriched = original_attach(node_id, request)
        request_hashes.append((node_id, executor_module.hash_json(enriched)))
        return enriched

    monkeypatch.setattr(executor, "_attach_prompt_authority", capture_request)

    def uncached(*args, **kwargs):
        kwargs.pop("preflight_context", None)
        return original_preflight(*args, **kwargs)

    monkeypatch.setattr(executor, "_candidate_hierarchical_preflight", uncached)
    baseline = executor._build_provider_call_plans()
    baseline_rows = tuple(json.dumps(row.to_dict(), sort_keys=True) for row in baseline)
    baseline_requests = tuple(request_hashes)

    request_hashes.clear()
    monkeypatch.setattr(executor, "_candidate_hierarchical_preflight", original_preflight)
    cached = executor._build_provider_call_plans()
    cached_rows = tuple(json.dumps(row.to_dict(), sort_keys=True) for row in cached)
    cached_requests = tuple(request_hashes)

    assert cached_rows == baseline_rows
    assert [row.node_id for row in cached] == [
        f"candidate_{index}_provider_generation" for index in range(1, 6)
    ]
    assert cached_requests == baseline_requests
    assert executor.summaries == original_summaries

    contexts: list[object] = []

    def capture_context(*args, **kwargs):
        contexts.append(kwargs["preflight_context"])
        return original_preflight(*args, **kwargs)

    monkeypatch.setattr(executor, "_candidate_hierarchical_preflight", capture_context)
    executor._build_provider_call_plans()
    executor._build_provider_call_plans()
    assert len(contexts) == 10
    assert all(contexts[index] is contexts[0] for index in range(5))
    assert all(contexts[index] is contexts[5] for index in range(5, 10))
    assert contexts[0] is not contexts[5]

    executor.summaries[0]["core_analysis"]["summary"] += " Source changed."
    observed_source_values: list[str] = []
    original_evidence = executor_module.build_outline_evidence_views

    def observe_source(summaries, *args, **kwargs):
        observed_source_values.append(str(summaries[0]["core_analysis"]["summary"]))
        return original_evidence(summaries, *args, **kwargs)

    monkeypatch.setattr(executor_module, "build_outline_evidence_views", observe_source)
    executor._build_provider_call_plans()
    assert observed_source_values and "Source changed." in observed_source_values[-1]
