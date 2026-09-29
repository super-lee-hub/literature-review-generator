"""A stability perturbation must keep the canonical relation task intact."""

from __future__ import annotations

from dataclasses import replace

import pytest

import outline.v3_executor as executor_module
from outline.semantic_chunking import build_paper_content_layers
from outline.v3_evidence import (
    build_global_corpus_ledger,
    build_multi_view_matrix,
    build_outline_evidence_views,
)
from outline.v3_executor import OutlineV3ExecutionError
from outline.v3_relations import build_global_relation_map
from tests.test_outline_v3_semantic_execution import (
    _configured_test_provider,
    _executor,
)


def _relation_inputs(executor):
    evidence = build_outline_evidence_views(executor.summaries, executor.job_id)
    ledger = build_global_corpus_ledger(evidence)
    matrix = build_multi_view_matrix(evidence)
    candidates = build_global_relation_map(evidence, matrix, ledger)
    layers = build_paper_content_layers(
        executor.summaries, evidence, job_id=executor.job_id
    )
    plan = executor_module.build_semantic_chunk_plan(
        layers, candidates, candidate_count=executor.candidate_count,
        physical_call_limit=24,
    )
    return evidence, [item.to_dict() for item in candidates.relations], plan


def test_missing_frozen_relation_id_blocks_variant_before_transport(tmp_path) -> None:
    executor = _executor(tmp_path)
    evidence, candidates, plan = _relation_inputs(executor)
    scope = executor._canonical_stability_relation_scope()
    assert scope["selected_relation_ids"]
    missing_id = scope["selected_relation_ids"][0]
    with pytest.raises(OutlineV3ExecutionError, match="missing a frozen selected relation ID"):
        executor._apply_stability_relation_scope(
            evidence=evidence,
            relation_candidates=[
                row for row in candidates if row["relation_id"] != missing_id
            ],
            semantic_plan=plan,
        )


def test_changed_selected_bundle_blocks_variant_before_transport(tmp_path) -> None:
    executor = _executor(tmp_path)
    evidence, candidates, plan = _relation_inputs(executor)
    scope = executor._canonical_stability_relation_scope()
    assert scope["selected_relation_ids"]
    selected_id = scope["selected_relation_ids"][0]
    changed_bundles = [
        replace(item, findings_left=[*item.findings_left, "unbound extra finding"])
        if item.relation_id == selected_id else item
        for item in plan.relation_bundles
    ]
    with pytest.raises(OutlineV3ExecutionError, match="changed the evidence closure"):
        executor._apply_stability_relation_scope(
            evidence=evidence,
            relation_candidates=candidates,
            semantic_plan=replace(plan, relation_bundles=changed_bundles),
        )


def test_changed_selected_relation_candidate_blocks_variant_before_transport(
    tmp_path,
) -> None:
    executor = _executor(tmp_path)
    evidence, candidates, plan = _relation_inputs(executor)
    scope = executor._canonical_stability_relation_scope()
    selected_id = scope["selected_relation_ids"][0]
    changed_candidates = [dict(item) for item in candidates]
    selected_row = next(
        item for item in changed_candidates
        if item["relation_id"] == selected_id
    )
    selected_row["candidate_scope_probe"] = "changed selected relation content"

    with pytest.raises(
        OutlineV3ExecutionError,
        match="changed selected relation candidate content",
    ):
        executor._apply_stability_relation_scope(
            evidence=evidence,
            relation_candidates=changed_candidates,
            semantic_plan=plan,
        )


def test_changed_unselected_relation_candidate_does_not_expand_frozen_task(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    executor = _executor(tmp_path)
    evidence, candidates, plan = _relation_inputs(executor)
    scope = executor._canonical_stability_relation_scope()
    selected_id = str(candidates[0]["relation_id"])
    selected = {selected_id}
    selected_row = next(
        item for item in candidates if item["relation_id"] == selected_id
    )
    selected_bundle = next(
        item for item in plan.relation_bundles
        if item.relation_id == selected_id
    )
    scope["selected_relation_ids"] = [selected_id]
    scope["selected_relation_candidates_hash"] = executor_module.hash_json([
        executor._relation_scope_value(dict(selected_row))
    ])
    scope["selected_bundle_hashes"] = {
        selected_id: executor_module.hash_json(
            executor._relation_scope_value(selected_bundle.to_dict())
        )
    }
    scope.pop("scope_hash", None)
    scope["scope_hash"] = executor_module.hash_json(scope)
    monkeypatch.setattr(
        executor,
        "_canonical_stability_relation_scope",
        lambda: scope,
    )
    plan = replace(
        plan,
        coverage={**dict(plan.coverage), "selected_relation_ids": [selected_id]},
    )
    changed_candidates = [dict(item) for item in candidates]
    unselected_row = next(
        item for item in changed_candidates
        if item["relation_id"] not in selected
    )
    unselected_row["candidate_scope_probe"] = "changed outside selected task"

    result = executor._apply_stability_relation_scope(
        evidence=evidence,
        relation_candidates=changed_candidates,
        semantic_plan=plan,
    )
    assert set(result.coverage["selected_relation_ids"]) == selected


def test_explicit_empty_frozen_scope_has_no_relation_provider_transport(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = executor_module.build_semantic_chunk_plan

    def empty_selected(*args, **kwargs):
        plan = original(*args, **kwargs)
        return replace(
            plan,
            coverage={**dict(plan.coverage), "selected_relation_ids": []},
        )

    monkeypatch.setattr(executor_module, "build_semantic_chunk_plan", empty_selected)
    relation_calls: list[str] = []

    def provider(node_id, request):
        if "relation_adjudication" in node_id:
            relation_calls.append(node_id)
        return _configured_test_provider(node_id, request)

    result = _executor(tmp_path, provider=provider, stability_mode="smoke").run()
    assert result.ok is True, result.diagnostics
    assert relation_calls == []
