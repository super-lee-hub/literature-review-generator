from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from outline.candidate_output_scope import build_candidate_output_scope_v1
from outline.semantic_chunking import build_paper_content_layers, build_relation_evidence_bundle, build_semantic_chunk_plan
from outline.v3_evidence import (
    build_global_corpus_ledger,
    build_multi_view_matrix,
    build_outline_evidence_views,
    build_review_intent,
)
from outline.v3_relations import build_global_relation_map
from services.artifact_registry import ArtifactRegistry
from services.job_workspace import JobWorkspace
from services.writer_source_inventory import load_writer_source_inventory_v1
from tests.test_run_first_scale_integration import _reader_content, _scale_papers
from tests.writer_source_fixture import bind_production_writer_sources


_REQUIRED_QUESTION = (
    "Compare the two source-reported findings within their recorded sample boundaries."
)


def _scale_ten_rows() -> tuple[list[dict[str, Any]], list[Any], dict[str, Any]]:
    papers = _scale_papers(10)
    summaries = [_reader_content(paper) for paper in papers]
    required_comparison = {
        "paper_keys": [papers[0].paper_key, papers[1].paper_key],
        "question": _REQUIRED_QUESTION,
        "relation_type": "reported_magnitude_contrast",
    }
    intent = build_review_intent({"required_comparisons": [required_comparison]})
    return summaries, papers, intent.required_comparisons[0]


def _relation_map(
    summaries: list[dict[str, Any]],
    required_comparisons: list[dict[str, Any]] | tuple[dict[str, Any], ...] = (),
):
    evidence = build_outline_evidence_views(summaries, "required-comparison-scale-10", strict_status=True)
    matrix = build_multi_view_matrix(evidence)
    ledger = build_global_corpus_ledger(evidence)
    return (
        evidence,
        ledger,
        build_global_relation_map(
            evidence,
            matrix,
            ledger,
            required_comparisons=required_comparisons,
        ),
    )


def _relation_scope_inventory(tmp_path, summaries):
    workspace = JobWorkspace.create(
        str(tmp_path), "required-comparison-scale-10", job_id="required-comparison-scale-10",
    )
    registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    service = type("FixtureService", (), {})()
    service.registry = registry
    service.workspace = workspace
    service.summaries = summaries
    bind_production_writer_sources(service, [])
    return load_writer_source_inventory_v1(registry)


def test_scale10_explicit_pair_survives_sparse_magnitude_selection_and_closes_both_sources(tmp_path):
    summaries, papers, requirement = _scale_ten_rows()
    pair = sorted(requirement["paper_keys"])
    evidence, _ledger, general_map = _relation_map(summaries)
    view_by_paper = {view.paper_key: view for view in evidence.views}

    # The general map stays sparse: it does not take the user-required 0/1
    # comparison, and it keeps only adjacent observed magnitude strata. The
    # fixture's +3 result at index 5 lies between the +2 and +4 reports, so the
    # generic contrast witnesses are 2–3 and 3–4 rather than the non-adjacent
    # 0/9 pair.
    assert not any(set(item.paper_keys) == set(pair) for item in general_map.relations)
    duplicate_two_point = next(
        paper.paper_key for paper in papers
        if paper.index == 9 and "2 points" in paper.finding
    )
    assert view_by_paper[papers[1].paper_key].findings == view_by_paper[duplicate_two_point].findings

    layers = build_paper_content_layers(summaries, evidence, job_id="required-comparison-scale-10")
    adjacent_magnitude_pairs = (
        (papers[1].paper_key, papers[5].paper_key),  # +2 to +3
        (papers[5].paper_key, papers[0].paper_key),  # +3 to +4
    )
    for left_key, right_key in adjacent_magnitude_pairs:
        magnitude_rows = [
            item for item in general_map.relations
            if item.relation_type == "reported_magnitude_contrast"
            and set(item.paper_keys) == {left_key, right_key}
        ]
        assert len(magnitude_rows) == 1
        magnitude_row = magnitude_rows[0]
        assert "reported_magnitude_contrast" in magnitude_row.diagnostics
        assert magnitude_row.evidence_fields == {
            left_key: ["findings"],
            right_key: ["findings"],
        }
        assert magnitude_row.supporting_labels == ["primary outcome [point]"]
        magnitude_bundle = build_relation_evidence_bundle(magnitude_row, layers)
        assert magnitude_bundle.is_complete
        assert set(magnitude_bundle.paper_ids) == {left_key, right_key}
        assert set(magnitude_bundle.required_evidence_ids) == set(magnitude_bundle.provided_evidence_ids)
        for paper_key in (left_key, right_key):
            finding_evidence = set(layers.dossier_by_paper[paper_key].evidence_ids_by_field["findings"])
            assert finding_evidence
            assert finding_evidence.issubset(set(magnitude_bundle.required_evidence_ids))
    assert frozenset(pair) not in {
        frozenset(item.paper_keys)
        for item in general_map.relations
        if item.relation_type == "reported_magnitude_contrast"
    }
    assert general_map.comparison_coverage["all_pairwise_comparisons_completed"] is False

    _evidence, _ledger, relation_map = _relation_map(summaries, [requirement])
    required_row = relation_map.comparison_coverage["required_comparisons"][0]
    assert required_row["paper_keys"] == pair
    assert required_row["question"] == _REQUIRED_QUESTION
    assert required_row["requested_relation_type"] == "reported_magnitude_contrast"
    assert required_row["relation_id_is_not_a_conclusion"] is True
    assert required_row["scientific_result_status"] == "pending_adjudication"
    assert required_row["status"] == "selected_source_complete"
    assert required_row["missing_source_fields_by_paper"] == {}

    required_id = required_row["relation_id"]
    required_candidate = next(item for item in relation_map.relations if item.relation_id == required_id)
    assert required_candidate.paper_keys == pair
    assert required_candidate.relation_type == "reported_magnitude_contrast"
    assert "required_user_comparison" in required_candidate.diagnostics
    assert required_candidate.evidence_fields == {
        key: ["findings", "moderators_boundaries"] for key in pair
    }
    assert all(
        "ai_summary.core_analysis.findings" in required_row["source_fields_by_paper"][key]["findings"]
        for key in pair
    )
    assert all(required_row["source_fields_by_paper"][key]["moderators_boundaries"] for key in pair)
    assert required_id in relation_map.comparison_coverage["selected_relation_ids"]
    assert relation_map.comparison_coverage["all_pairwise_comparisons_completed"] is False

    # The required source task retains complete finding and boundary evidence
    # for each side; neither pair identity nor the requested relation type is
    # treated as an adjudicated scientific outcome.
    bundle = build_relation_evidence_bundle(required_candidate, layers)
    assert bundle.paper_ids == pair
    assert bundle.is_complete
    assert bundle.required_evidence_ids == bundle.provided_evidence_ids
    for key in pair:
        dossier = layers.dossier_by_paper[key]
        side_required = set(dossier.evidence_ids_by_field["findings"]) | set(
            dossier.evidence_ids_by_field["moderators_boundaries"]
        )
        assert side_required
        assert side_required.issubset(set(bundle.required_evidence_ids))
        assert any(claim.evidence_ids and claim.study_id == "" for claim in dossier.claims)

    semantic_plan = build_semantic_chunk_plan(
        layers, relation_map, candidate_count=5, physical_call_limit=200,
    )
    assert required_id in semantic_plan.coverage["selected_relation_ids"]
    selected_bundle = next(item for item in semantic_plan.relation_bundles if item.relation_id == required_id)
    assert selected_bundle.is_complete
    assert selected_bundle.paper_ids == pair
    assert selected_bundle.comparison_question == _REQUIRED_QUESTION

    # The same two-sided task becomes a finite source-closed output group. The
    # question is taken from the explicit requirement mapping and retained by
    # the task contract; the candidate map itself makes no conclusion.
    inventory = _relation_scope_inventory(tmp_path, summaries)
    bundle_payload = selected_bundle.to_dict()
    scope = build_candidate_output_scope_v1(
        inventory,
        topic_routes=[],
        selected_relation_ids=[required_id],
        relation_bundles=[bundle_payload],
    )
    group = next(item for item in scope.claim_groups if item.claim_seed)
    local_scope = scope.for_groups([group.claim_group_id], allowed_paper_keys=pair)
    slots = [slot for slot in local_scope.claim_slots if slot.claim_group_id == group.claim_group_id]
    assert group.paper_keys == tuple(pair)
    assert group.to_dict()["task_contract"]["comparison_question"] == _REQUIRED_QUESTION
    assert group.to_dict()["task_contract"]["relation_id_is_not_a_conclusion"] is True
    assert group.claim_kind == "model_authored_relation_comparison_slot"
    assert {slot.paper_key for slot in slots} == set(pair)
    assert all(slot.source_claim_ids and slot.evidence_ids and slot.source_field_ids for slot in slots)
    assert all(slot.study_id == "" for slot in slots)


def test_required_comparison_is_order_invariant_key_renamable_and_empty_input_keeps_old_hash():
    summaries, papers, requirement = _scale_ten_rows()
    evidence, ledger, general_map = _relation_map(summaries)
    empty_explicit = build_global_relation_map(
        evidence,
        build_multi_view_matrix(evidence),
        ledger,
        required_comparisons=[],
    )
    assert empty_explicit.content_hash == general_map.content_hash

    required_map = build_global_relation_map(
        evidence,
        build_multi_view_matrix(evidence),
        ledger,
        required_comparisons=[requirement],
    )
    permuted_summaries = list(reversed(summaries))
    permuted_evidence = build_outline_evidence_views(
        permuted_summaries, "required-comparison-scale-10", strict_status=True,
    )
    permuted_map = build_global_relation_map(
        permuted_evidence,
        build_multi_view_matrix(permuted_evidence),
        build_global_corpus_ledger(permuted_evidence),
        required_comparisons=[requirement],
    )
    assert permuted_map.content_hash == required_map.content_hash

    key_map = {paper.paper_key: f"renamed-scale-paper-{paper.index:04d}" for paper in papers}
    renamed_summaries = deepcopy(summaries)
    for summary in renamed_summaries:
        info = summary["paper_info"]
        old_key = info["canonical_paper_key"]
        info["canonical_paper_key"] = key_map[old_key]
        info["source_paper_id"] = key_map[old_key]
    renamed_requirement = {
        **requirement,
        "paper_keys": [key_map[key] for key in requirement["paper_keys"]],
    }
    renamed_evidence = build_outline_evidence_views(
        renamed_summaries, "required-comparison-scale-10-renamed", strict_status=True,
    )
    renamed_map = build_global_relation_map(
        renamed_evidence,
        build_multi_view_matrix(renamed_evidence),
        build_global_corpus_ledger(renamed_evidence),
        required_comparisons=[renamed_requirement],
    )
    renamed_row = renamed_map.comparison_coverage["required_comparisons"][0]
    assert renamed_row["paper_keys"] == sorted(renamed_requirement["paper_keys"])
    assert renamed_row["question"] == _REQUIRED_QUESTION
    assert renamed_row["status"] == "selected_source_complete"
    renamed_candidate = next(
        item for item in renamed_map.relations if item.relation_id == renamed_row["relation_id"]
    )
    assert set(renamed_candidate.paper_keys) == set(renamed_requirement["paper_keys"])


@pytest.mark.parametrize(
    "required_comparisons,match",
    [
        ("not-an-array", "must be an array"),
        ([{"paper_keys": ["unknown-a", "unknown-b"], "question": "Compare."}], "unknown source paper"),
        ([{"paper_keys": ["rfspaper0000_unknown_author", "rfspaper0000_unknown_author"], "question": "Compare."}], "exactly two distinct"),
        ([{"paper_keys": ["rfspaper0000_unknown_author", "rfspaper0001_unknown_author"]}], "non-empty question"),
    ],
)
def test_required_comparison_rejects_unassignable_or_ambiguous_requests(required_comparisons, match):
    summaries, _papers, _requirement = _scale_ten_rows()
    evidence = build_outline_evidence_views(summaries, "required-comparison-scale-10", strict_status=True)
    with pytest.raises(ValueError, match=match):
        build_global_relation_map(evidence, required_comparisons=required_comparisons)
