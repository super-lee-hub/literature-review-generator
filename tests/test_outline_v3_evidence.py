"""Determinism and fail-closed tests for Outline Intelligence v3 evidence."""

from __future__ import annotations

from outline.v3_evidence import (
    build_coverage_contract,
    build_global_corpus_ledger,
    build_multi_view_matrix,
    build_outline_evidence_views,
    build_review_intent,
    merge_outline_evidence_shards,
    shard_outline_evidence_views,
)
from outline.v3_models import OutlineEvidenceViews


def _summary(
    doi: str,
    title: str,
    *,
    classification: str = "support",
    source_paper_id: str = "",
):
    return {
        "status": "success",
        "paper_info": {
            "title": title,
            "authors": ["Smith, A."],
            "year": 2024,
            "doi": doi,
            "classification": classification,
            "source_paper_id": source_paper_id,
        },
        "ai_summary": {
            "routing": {
                "paper_type": "empirical",
                "classification_status": "resolved",
                "route_confidence": "high",
            },
            "core_analysis": {
                "summary": "A study of fairness and trust.",
                "methodology": "survey experiment",
                "theoretical_framework": "equity theory",
                "findings": "Fairness increases trust.",
                "conclusions": "Context qualifies the effect.",
                "limitations": "Single-country sample.",
                "research_gap": "Limited longitudinal evidence.",
                "future_research_directions": ["Replicate across countries."],
                "relevance": "Directly relevant.",
            },
            "specialized_details": {
                "empirical": {
                    "research_questions_or_hypotheses": ["Does fairness increase trust?"],
                    "data_source_and_size": "Online panel, n=300.",
                    "analysis_technique": "structural equation modeling",
                    "core_variables": {
                        "independent": ["promotion fairness"],
                        "dependent": ["consumer trust"],
                        "mediators": ["perceived value"],
                        "moderators": [],
                        "controls": [],
                        "other_core_constructs": [],
                    },
                    "sample_characteristics_or_context": "Digital retail shoppers.",
                }
            },
            "quality_audit": {
                "needs_manual_review": False,
                "missing_critical_fields": [],
                "conflict_flags": [],
            },
        },
    }


def test_projection_uses_canonical_fields_and_is_input_order_invariant():
    summaries = [
        _summary("10.1000/b", "B Paper", classification="background_only"),
        _summary("10.1000/a", "A Paper", classification="core"),
    ]

    first = build_outline_evidence_views(summaries, "job-a")
    second = build_outline_evidence_views(list(reversed(summaries)), "job-b")

    assert first.content_hash == second.content_hash
    assert [view.paper_key for view in first.views] == ["10.1000/a", "10.1000/b"]
    view = first.views[0]
    assert view.theories == ["equity theory"]
    assert view.constructs == ["consumer trust", "perceived value", "promotion fairness"]
    assert view.mechanisms == ["perceived value"]
    assert view.findings == ["Fairness increases trust."]
    assert view.source_summary_hash == view.source_summary_hashes[0]

    ledger_a = build_global_corpus_ledger(first)
    ledger_b = build_global_corpus_ledger(second)
    assert ledger_a.content_hash == ledger_b.content_hash
    assert [entry.assignment_status for entry in ledger_a.entries] == ["assigned", "background_only"]
    assert all(entry.diagnostic_candidate_topics for entry in ledger_a.entries)
    assert "chapter" not in ledger_a.entries[0].compact_record.casefold()

    matrix_a = build_multi_view_matrix(first)
    matrix_b = build_multi_view_matrix(second)
    assert matrix_a.content_hash == matrix_b.content_hash
    assert matrix_a.matrix["10.1000/a"]["theory"] == ["equity theory"]
    assert matrix_a.matrix["10.1000/a"]["construct"] == [
        "consumer trust",
        "perceived value",
        "promotion fairness",
    ]


def test_unresolved_identity_is_blocking_and_never_uses_list_position():
    result = build_outline_evidence_views([
        {
            "status": "success",
            "paper_info": {"title": "Title only"},
            "ai_summary": _summary("10.1000/minimal", "Minimal")["ai_summary"],
        },
    ])

    assert result.views == []
    assert result.status == "blocked"
    assert any(item["code"] == "missing_stable_paper_identity" for item in result.blocking_diagnostics)
    assert not any(item.paper_key.startswith("source:") for item in result.views)


def test_explicit_alias_crosswalk_can_resolve_an_unstable_source():
    result = build_outline_evidence_views(
        [{
            "status": "success",
            "paper_info": {"title": "Unstable Imported Record", "paper_key_aliases": ["legacy-7"]},
            "ai_summary": _summary("10.1000/minimal", "Minimal")["ai_summary"],
        }],
        alias_crosswalk={"legacy-7": "canonical-paper-7"},
    )

    assert result.status == "ready"
    assert result.views[0].paper_key == "canonical-paper-7"
    assert result.views[0].identity_source == "alias_crosswalk"


def test_failed_source_status_is_blocking_even_when_identity_is_stable():
    result = build_outline_evidence_views([
        {
            "status": "failed",
            "paper_info": {"doi": "10.1000/failed", "title": "Failed"},
            "ai_summary": _summary("10.1000/minimal", "Minimal")["ai_summary"],
        },
    ])

    assert result.views == []
    assert any(item["code"] == "source_summary_not_success" for item in result.blocking_diagnostics)


def test_technical_shards_merge_to_the_same_hash_and_schema():
    evidence = build_outline_evidence_views([
        _summary("10.1000/3", "C Paper"),
        _summary("10.1000/1", "A Paper"),
        _summary("10.1000/2", "B Paper"),
    ])
    shards = shard_outline_evidence_views(evidence, shard_size=1)
    merged = merge_outline_evidence_shards(list(reversed(shards)))

    assert all(isinstance(shard, OutlineEvidenceViews) for shard in shards)
    assert merged.content_hash == evidence.content_hash
    assert [view.paper_key for view in merged.views] == [
        "10.1000/1",
        "10.1000/2",
        "10.1000/3",
    ]


def test_review_intent_and_coverage_contract_are_explicit():
    evidence = build_outline_evidence_views([_summary("10.1000/a", "A Paper", classification="core")])
    ledger = build_global_corpus_ledger(evidence)
    intent = build_review_intent({
        "review_question": "How does fairness affect trust?",
        "target_audience": "Researchers",
        "must_cover": ["theory evolution"],
        "must_not_do": ["Treat streams as chapters"],
    })
    contract = build_coverage_contract(ledger, intent)

    assert intent.preferred_organizing_logic == ""
    assert intent.must_not_do == ["Treat streams as chapters"]
    assert contract.corpus_paper_keys == ["10.1000/a"]
    assert contract.must_use_paper_keys == ["10.1000/a"]
    assert contract.assignment_statuses["10.1000/a"] == "assigned"
