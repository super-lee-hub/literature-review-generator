"""Tests for sparse v3 relation candidates and shared candidate plans."""

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
)

from tests.test_outline_v3_evidence import _summary


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
