"""Tests for sparse v3 relation candidates and shared candidate plans."""

import pytest

from outline.v3_evidence import (
    build_coverage_contract,
    build_global_corpus_ledger,
    build_multi_view_matrix,
    build_outline_evidence_views,
    build_review_intent,
)
from outline.v3_relations import (
    RELATION_TYPES,
    build_global_relation_map,
    build_outline_candidate_plans,
    build_organizing_axes,
)

from tests.test_outline_v3_evidence import _summary


@pytest.mark.parametrize("candidate_count", [0, -1, 13])
def test_organizing_axes_reject_counts_outside_the_public_range(candidate_count):
    with pytest.raises(ValueError, match="between 1 and 12"):
        build_organizing_axes(candidate_count=candidate_count)


def test_composed_axes_keep_the_preferred_primary_dimension_order():
    intent = build_review_intent({"preferred_organizing_logic": "context_boundaries"})
    axes = build_organizing_axes(intent, candidate_count=6)
    primary = axes[0]
    assert primary.axis_id == "context_boundaries"
    assert axes[5].axis_id.startswith("context_boundaries_then_")
    assert axes[5].preferred_dimensions[:len(primary.preferred_dimensions)] == primary.preferred_dimensions


@pytest.mark.parametrize("candidate_count", [1, 3, 5, 6, 8, 12])
def test_candidate_plan_cardinality_matches_the_requested_transport_graph(candidate_count):
    evidence = build_outline_evidence_views([_relation_summary(
        "10.1000/a", "A", "controlled context", "experiment", "The result supports a bounded effect.",
    )])
    ledger = build_global_corpus_ledger(evidence)
    matrix = build_multi_view_matrix(evidence)
    relation_map = build_global_relation_map(evidence, matrix, ledger)
    intent = build_review_intent({})
    coverage = build_coverage_contract(ledger, intent)
    plans = build_outline_candidate_plans(
        ledger, matrix, relation_map, intent, coverage, candidate_count=candidate_count,
    )
    assert len(plans.candidates) == candidate_count
    assert [item.candidate_id for item in plans.candidates] == [
        f"candidate_{index}" for index in range(1, candidate_count + 1)
    ]
    assert len({item.organizing_logic for item in plans.candidates}) == candidate_count
    axes_by_id = {item.axis_id: item for item in plans.axes}
    assert all(item.axis_id in axes_by_id for item in plans.candidates)
    original_axes = build_organizing_axes(intent)
    assert plans.axes[:min(5, candidate_count)] == original_axes[:min(5, candidate_count)]
    for item in plans.axes[5:]:
        primary, secondary = item.axis_id.split("_then_", 1)
        assert primary != secondary
        assert primary in {axis.axis_id for axis in original_axes}
        assert secondary in {axis.axis_id for axis in original_axes}
        assert item.rationale and "primary" in item.rationale


def _relation_summary(doi: str, title: str, context: str, method: str, finding: str, *, paper_type: str = "empirical"):
    result = _summary(doi, title)
    result["ai_summary"]["routing"]["paper_type"] = paper_type
    result["ai_summary"]["core_analysis"]["findings"] = finding
    result["ai_summary"]["core_analysis"]["methodology"] = method
    result["ai_summary"]["specialized_details"]["empirical"]["sample_characteristics_or_context"] = context
    return result


def test_relation_map_is_sparse_evidence_linked_and_order_invariant():
    first = _relation_summary(
        "10.1000/a", "A", "online retail", "survey", "The result supports trust.",
    )
    second = _relation_summary(
        "10.1000/b", "B", "laboratory retail", "experiment", "The result contradicts earlier evidence.",
    )
    third = _relation_summary(
        "10.1000/c", "C", "online retail", "conceptual analysis", "The framework qualifies the result.",
        paper_type="conceptual",
    )
    evidence_a = build_outline_evidence_views([first, second, third])
    evidence_b = build_outline_evidence_views([third, first, second])
    matrix_a = build_multi_view_matrix(evidence_a)
    matrix_b = build_multi_view_matrix(evidence_b)
    ledger_a = build_global_corpus_ledger(evidence_a)
    ledger_b = build_global_corpus_ledger(evidence_b)

    relation_a = build_global_relation_map(evidence_a, matrix_a, ledger_a)
    relation_b = build_global_relation_map(evidence_b, matrix_b, ledger_b)

    assert relation_a.content_hash == relation_b.content_hash
    assert relation_a.paper_keys == ["10.1000/a", "10.1000/b", "10.1000/c"]
    relation_types = {relation.relation_type for relation in relation_a.relations}
    assert {"uses_same_theory", "studies_same_construct", "studies_same_mechanism"}.issubset(relation_types)
    assert "different_context" in relation_types
    assert "different_method" in relation_types
    assert "contradicts" in relation_types
    assert "qualifies" in relation_types
    assert "conceptual_integration" in relation_types
    assert relation_types <= set(RELATION_TYPES)
    for relation in relation_a.relations:
        assert relation.paper_keys == sorted(relation.paper_keys)
        assert relation.confidence in {"low", "medium", "high"}
        assert relation.evidence_fields
        assert relation.source_fields


def test_candidate_plans_share_global_inputs_but_use_distinct_axes_and_provider_nodes():
    summaries = [
        _relation_summary("10.1000/a", "A", "online retail", "survey", "supports"),
        _relation_summary("10.1000/b", "B", "laboratory retail", "experiment", "qualifies"),
    ]
    evidence = build_outline_evidence_views(summaries)
    ledger = build_global_corpus_ledger(evidence)
    matrix = build_multi_view_matrix(evidence)
    relation_map = build_global_relation_map(evidence, matrix, ledger)
    intent = build_review_intent({
        "review_question": "How is trust explained?",
        "preferred_organizing_logic": "controversy",
    })
    coverage = build_coverage_contract(ledger, intent)

    plans = build_outline_candidate_plans(
        ledger, matrix, relation_map, intent, coverage, candidate_count=5,
    )

    assert len(plans.candidates) == 5
    assert len({candidate.organizing_logic for candidate in plans.candidates}) == 5
    assert plans.candidates[0].organizing_logic == "controversy"
    assert len({candidate.provider_generation_node_id for candidate in plans.candidates}) == 5
    assert all(candidate.provider_generation_node_id != candidate.candidate_id for candidate in plans.candidates)
    assert all(candidate.shared_artifact_hashes == plans.shared_artifact_hashes for candidate in plans.candidates)
    assert all("global_corpus_ledger" in candidate.required_node_ids for candidate in plans.candidates)
    assert all("organizing_axes" in candidate.required_node_ids for candidate in plans.candidates)


def test_shared_label_cohort_preserves_members_without_pair_cap_or_false_comparisons():
    summaries = [
        _relation_summary(f"10.1000/{letter}", letter, "online retail", "survey", "supports")
        for letter in ("a", "b", "c", "d")
    ]
    evidence = build_outline_evidence_views(summaries)
    ledger = build_global_corpus_ledger(evidence)
    matrix = build_multi_view_matrix(evidence)
    relation_map = build_global_relation_map(
        evidence,
        matrix,
        ledger,
        max_pairs_per_label=2,
    )

    # The former implementation reported six possible pairs, emitted the
    # first two by paper-key order, and blocked adoption. Shared membership is
    # now represented by a complete cohort; absent a source-backed contrast,
    # pairwise work remains explicitly deferred rather than implied complete.
    cohort = next(
        item for item in relation_map.cohorts
        if item["dimension"] == "construct" and item["label"] == "consumer trust"
    )
    assert cohort["member_keys"] == relation_map.paper_keys
    assert cohort["disposition"] == "deferred"
    assert cohort["reason_codes"] == ["shared_label_only_no_source_contrast_signal"]
    assert relation_map.comparison_coverage["selected_pair_count"] == 0
    assert relation_map.comparison_coverage["all_pairwise_comparisons_completed"] is False
    assert not any(
        item.get("code") == "relation_label_pair_cap"
        for item in relation_map.blocking_diagnostics
    )
