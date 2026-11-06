from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from outline.semantic_chunking import (
    build_paper_content_layers,
    build_semantic_chunk_plan,
)
from outline.v3_evidence import build_outline_evidence_views
from outline.v3_models import (
    EvidenceClaim,
    InterpretationDependency,
    PaperContentLayers,
    PaperEvidenceDossier,
    PaperIndexCard,
    ResearchUnit,
    compute_v3_hash,
)
from tests.test_outline_v3_semantic_execution import _summary


def _gap_summaries() -> list[dict[str, Any]]:
    return [
        _summary("paper-a", "A", "A finding."),
        _summary("paper-b", "B", "B finding."),
    ]


def _plan_for_summaries(summaries: Sequence[Mapping[str, Any]]) -> Any:
    evidence = build_outline_evidence_views(summaries, "topic-deferral-test")
    content_layers = build_paper_content_layers(
        summaries,
        evidence,
        job_id="topic-deferral-test",
    )
    return build_semantic_chunk_plan(
        content_layers,
        candidate_count=2,
        physical_call_limit=24,
    )


def test_two_author_proposed_gaps_receive_exact_local_deferrals() -> None:
    plan = _plan_for_summaries(_gap_summaries())
    coverage = plan.coverage
    expected_claim_rows = [
        {
            "paper_key": "paper-a",
            "claim_id": "claim:paper-a:research_gaps:2d7560a664a6523b",
        },
        {
            "paper_key": "paper-b",
            "claim_id": "claim:paper-b:research_gaps:2d7560a664a6523b",
        },
    ]
    expected_evidence_rows = [
        ("paper-a", "evidence:paper-a:research_gaps:eef902322bb0f7d1"),
        ("paper-b", "evidence:paper-b:research_gaps:4f1921ee47966fd8"),
    ]

    deferred = coverage["deferred_source_claims"]
    assert [
        {"paper_key": row["paper_key"], "claim_id": row["claim_id"]}
        for row in deferred
    ] == expected_claim_rows
    assert coverage["deferred_source_claim_count"] == 2
    assert coverage["deferred_source_claim_identity_hash"] == compute_v3_hash(
        expected_claim_rows
    )
    assert coverage["deferred_source_claim_evidence_identity_count"] == 2
    assert coverage["deferred_source_claim_evidence_identity_hash"] == compute_v3_hash(
        expected_evidence_rows
    )
    assert coverage["deferred_source_evidence_count"] == 2
    assert coverage["deferred_source_evidence_identity_hash"] == compute_v3_hash(
        expected_evidence_rows
    )
    assert coverage["whole_source_claim_identity_count"] == (
        coverage["topic_unit_source_claim_identity_count"]
        + coverage["deferred_source_claim_count"]
    )
    assert coverage["source_claim_disposition_status"] == "complete"
    assert coverage["topic_unit_source_claim_selection_scope"] == (
        "topic_required_evidence_plus_outlier_unbound_claims_and_active_interpretation_dependency_closure"
    )
    assert coverage["deferred_source_claim_policy"] == (
        "complete_dossiers_remain_locally_available_for_directed_retrieval"
    )
    for row, evidence_id in zip(deferred, (value for _paper, value in expected_evidence_rows)):
        assert row["claim_type"] == "author_proposed_gap"
        assert row["study_id"] == ""
        assert row["source_locator"] == "stage1:ai_summary.research_gaps"
        assert row["source_field_names"] == ["research_gaps"]
        assert row["evidence_ids"] == [evidence_id]
        assert row["deferred_reason_code"] == (
            "author_proposed_gap_not_selected_by_topic_dimensions"
        )
        assert row["retained_in_local_dossier"] is True


def test_source_claim_deferrals_are_invariant_to_summary_order() -> None:
    summaries = _gap_summaries()
    forward = _plan_for_summaries(summaries).coverage
    reverse = _plan_for_summaries(list(reversed(summaries))).coverage

    assert forward["deferred_source_claims"] == reverse["deferred_source_claims"]
    for field in (
        "whole_source_claim_identity_hash",
        "topic_unit_source_claim_identity_hash",
        "deferred_source_claim_identity_hash",
        "deferred_source_claim_evidence_identity_hash",
        "deferred_source_evidence_identity_hash",
    ):
        assert forward[field] == reverse[field]


def test_required_qualifier_claim_is_selected_by_dependency_closure_not_deferred() -> None:
    primary = EvidenceClaim(
        claim_id="claim:paper-q:effect",
        claim_type="empirical_finding",
        text="The intervention raised purchase intent.",
        evidence_ids=["evidence:paper-q:finding"],
        source_locator="stage1:ai_summary.findings",
        source_summary_hash="a" * 64,
    )
    qualifier = EvidenceClaim(
        claim_id="claim:paper-q:boundary",
        claim_type="author_interpretation",
        text="Only for familiar shoppers.",
        evidence_ids=["evidence:paper-q:boundary"],
        source_locator="stage1:ai_summary.limitations",
        source_summary_hash="a" * 64,
    )
    dependency = InterpretationDependency(
        primary_claim_id=primary.claim_id,
        required_source_claim_ids=[qualifier.claim_id],
        required_evidence_ids=qualifier.evidence_ids,
        scope="paper",
        reason="boundary",
    )
    unit = ResearchUnit(
        study_id="paper-q:study:paper_level",
        parent_paper_id="paper-q",
        findings=[primary.text],
        mechanisms=["social proof mechanism"],
        claims=[primary, qualifier],
        evidence_ids=[
            "evidence:paper-q:finding",
            "evidence:paper-q:boundary",
            "evidence:paper-q:mechanism",
        ],
        source_summary_hash="a" * 64,
        interpretation_dependencies=[dependency],
    )
    dossier = PaperEvidenceDossier(
        dossier_id="dossier:paper-q",
        paper_id="paper-q",
        source_summary_hash="a" * 64,
        findings=[primary.text],
        research_units=[unit],
        claims=[primary, qualifier],
        mechanism_evidence=["Social proof mechanism."],
        evidence_ids_by_field={
            "findings": ["evidence:paper-q:finding"],
            "mechanism_evidence": ["evidence:paper-q:mechanism"],
            "moderators_boundaries": ["evidence:paper-q:boundary"],
            "limitations": ["evidence:paper-q:boundary"],
        },
        evidence_text_by_id={
            "evidence:paper-q:finding": primary.text,
            "evidence:paper-q:boundary": qualifier.text,
            "evidence:paper-q:mechanism": "Social proof mechanism.",
        },
    )
    content_layers = PaperContentLayers(
        index_cards=[
            PaperIndexCard(
                paper_id="paper-q",
                mechanisms=["social proof mechanism"],
                evidence_package_id="dossier:paper-q",
                source_summary_hash="a" * 64,
            )
        ],
        dossiers=[dossier],
        source_summary_hashes=["a" * 64],
    )

    plan = build_semantic_chunk_plan(
        content_layers,
        candidate_count=1,
        physical_call_limit=24,
    )

    assert len(plan.topics) == 1
    topic = plan.topics[0]
    assert topic.dimensions == ["mechanism"]
    assert "evidence:paper-q:boundary" not in topic.required_evidence_ids
    assert plan.coverage["deferred_source_claims"] == []
    assert plan.coverage["topic_unit_source_claim_identity_count"] == 2
    assert plan.coverage["deferred_source_claim_count"] == 0
    assert plan.coverage["deferred_source_evidence_count"] == 0
    assert plan.coverage["deferred_source_evidence_identity_hash"] == compute_v3_hash([])
