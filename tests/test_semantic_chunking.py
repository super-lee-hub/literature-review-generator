from __future__ import annotations


from outline.semantic_chunking import (
    build_paper_content_layers,
    build_relation_evidence_bundle,
    build_semantic_chunk_plan,
)
from outline.v3_evidence import (
    build_global_corpus_ledger,
    build_multi_view_matrix,
    build_outline_evidence_views,
)
from outline.v3_models import RelationCandidate
from outline.v3_relations import build_global_relation_map
from runtime.pause_state import PAUSED_BY_USER, PauseRequestedError, PauseStateStore
from services.artifact_registry import ArtifactRegistry
from services.job_workspace import JobWorkspace

from tests.test_outline_v3_semantic_execution import _summary


def _layers(summaries):
    evidence = build_outline_evidence_views(summaries)
    ledger = build_global_corpus_ledger(evidence)
    matrix = build_multi_view_matrix(evidence)
    relations = build_global_relation_map(evidence, matrix, ledger)
    return build_paper_content_layers(summaries, evidence), relations


def test_content_layers_are_input_order_invariant_and_keep_long_conditionals():
    first = _summary("paper-a", "A", "A result.")
    long_finding = "The effect was positive only when the boundary condition held; " + ("tail qualifier " * 120)
    first["core_analysis"]["findings"] = long_finding
    first["core_analysis"]["limitations"] = "The condition is narrow."
    second = _summary("paper-b", "B", "B result.")

    left, _ = _layers([first, second])
    right, _ = _layers([second, first])

    assert left.content_hash == right.content_hash
    dossier = left.dossier_by_paper["paper-a"]
    assert any(long_finding.strip() == value.strip() for value in dossier.findings)
    assert "tail qualifier" in dossier.evidence_text_by_id[next(iter(dossier.evidence_ids_by_field["findings"]))]


def test_explicit_multi_study_records_remain_separate_units():
    summary = _summary("paper-multi", "Multi", "Paper-level result.")
    summary["studies"] = [
        {"study_id": "S1", "findings": ["S1 result"], "method": "experiment", "sample": "sample 1"},
        {"study_id": "S2", "findings": ["S2 result"], "method": "survey", "sample": "sample 2"},
    ]
    layers, _ = _layers([summary])
    units = layers.dossier_by_paper["paper-multi"].research_units
    assert [unit.study_id for unit in units] == ["paper-multi:study:s1", "paper-multi:study:s2"]
    assert {unit.findings[0] for unit in units} == {"S1 result", "S2 result"}


def test_relation_with_ids_but_missing_findings_or_boundaries_is_insufficient():
    summary_a = _summary("paper-a", "A", "A result")
    summary_b = _summary("paper-b", "B", "B result")
    summary_b["core_analysis"]["findings"] = ""
    summary_b["core_analysis"]["key_points"] = []
    summary_b["core_analysis"]["limitations"] = ""
    summary_b["specialized_details"]["empirical"]["data_source_and_size"] = ""
    summary_b["specialized_details"]["empirical"]["sample_characteristics_or_context"] = ""
    layers, _ = _layers([summary_a, summary_b])
    candidate = RelationCandidate(
        relation_id="relation-missing-evidence",
        relation_type="contradicts",
        paper_keys=["paper-a", "paper-b"],
        dimension="construct",
        evidence_fields={"paper-a": ["construct"], "paper-b": ["construct"]},
    )
    bundle = build_relation_evidence_bundle(candidate, layers)
    assert bundle.paper_ids == ["paper-a", "paper-b"]
    assert bundle.evidence_completeness == "incomplete"
    assert bundle.decision == "insufficient_evidence"
    assert bundle.missing_evidence_ids


def test_candidate_count_only_changes_organization_budget_not_shared_plan_hash():
    summaries = [_summary("paper-a", "A", "A result"), _summary("paper-b", "B", "B result")]
    layers, relations = _layers(summaries)
    plan_two = build_semantic_chunk_plan(layers, relations, candidate_count=2)
    plan_three = build_semantic_chunk_plan(layers, relations, candidate_count=3)
    assert plan_two.shared_content_hash == plan_three.shared_content_hash
    assert plan_two.budgets["provider_posts_emitted"] == 0
    assert plan_three.budgets["provider_posts_emitted"] == 0


def test_pause_state_blocks_new_admission_and_preserves_explicit_state(tmp_path):
    workspace = JobWorkspace.create(str(tmp_path), "pause", "pause-job")
    registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    store = PauseStateStore(workspace, registry)
    paused = store.request(reason="user_requested")
    assert paused.state == PAUSED_BY_USER
    assert store.is_paused()
    try:
        store.assert_runnable(node_id="relation_adjudication")
    except PauseRequestedError as exc:
        assert PAUSED_BY_USER in str(exc)
    else:  # pragma: no cover - assertion branch
        raise AssertionError("paused state admitted a new node")
    cleared = store.clear(reason="explicit_resume")
    assert cleared.paused is False
    store.assert_runnable(node_id="relation_adjudication")


def test_corrupt_pause_state_fails_closed(tmp_path):
    workspace = JobWorkspace.create(str(tmp_path), "pause", "pause-corrupt")
    store = PauseStateStore(workspace, ArtifactRegistry(workspace.paths.registry_path, workspace.job_id))
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text("{", encoding="utf-8")
    try:
        store.assert_runnable(node_id="stage1")
    except PauseRequestedError as exc:
        assert "CONTROL_STATE_INVALID" in str(exc)
    else:  # pragma: no cover - assertion branch
        raise AssertionError("corrupt pause state was admitted")
