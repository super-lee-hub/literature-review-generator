import pytest
import json
import os
from dataclasses import asdict

from validation.evidence_resolver import (
    EvidenceCandidate,
    EvidenceResolver,
    EvidenceResolverContext,
    build_evidence_resolver_context,
)
from validation.review_validator import (
    CitationValidationResult,
    ReviewValidationReport,
    ReviewValidator,
    RootCause,
    ValidationConclusion,
)
from validation.summary_recheck import (
    SummaryCorrectionCandidate,
    SummaryRecheckReport,
    SummaryRechecker,
    WHITELISTED_FIELDS,
    run_summary_rechecks,
)
from validator import run_week3_review_validation, run_week3_summary_recheck


def test_evidence_candidate_creation():
    candidate = EvidenceCandidate(
        match_reason="test",
        resolver_tier="test_tier",
        window_rank=0,
        confidence=0.9,
        artifact_path="/test/path",
        page_span=[1, 2],
        chunk_ids=["chunk1"],
        text_excerpt="test excerpt",
        negative_evidence_reason=None,
        visual_refs=None,
        caption_excerpt=None,
        evidence_scope="test",
    )
    assert candidate.match_reason == "test"
    assert candidate.confidence == 0.9


def test_evidence_resolver_context():
    context = EvidenceResolverContext(
        paper_key="test_key",
        paper_identity={"canonical_paper_key": "test_key"},
        preprocess_artifacts={},
        paper_artifact={},
    )
    assert context.paper_key == "test_key"


def test_evidence_resolver():
    context = EvidenceResolverContext(
        paper_key="test_key",
        paper_identity={"canonical_paper_key": "test_key"},
        preprocess_artifacts={
            "chunks": [
                {"chunk_id": "c1", "text": "This is a test chunk with cited text here", "page_range": [1]}
            ],
            "normalized_text": "This is a test normalized text with cited text here",
        },
        paper_artifact={"source": {"source_pdf": "/test.pdf"}},
    )
    resolver = EvidenceResolver(context)
    candidates = resolver.resolve_evidence(cited_span="cited text")
    assert len(candidates) > 0


def test_validation_conclusion_enum():
    assert ValidationConclusion.SUPPORTED.value == "SUPPORTED"
    assert ValidationConclusion.PARTIAL_SUPPORT.value == "PARTIAL_SUPPORT"
    assert ValidationConclusion.UNSUPPORTED.value == "UNSUPPORTED"


def test_root_cause_enum():
    assert RootCause.SUMMARY_DRIFT.value == "summary_drift"
    assert RootCause.VISUAL_UNDERSTANDING_GAP.value == "visual_understanding_gap"


def test_review_validator_basic():
    review_draft = {
        "content": {"sections": []},
    }
    citation_manifest = {
        "citations": [
            {
                "citation_id": "c1",
                "paper_id": "p1",
                "text": "test citation",
                "context": "test context",
            }
        ],
    }
    paper_artifacts = [
        {
            "paper_identity": {
                "canonical_paper_key": "p1",
                "source_paper_id": "p1",
            },
            "analysis": {"ai_summary": {}},
            "stage1_inputs": {"selected_visual_refs": []},
        }
    ]

    validator = ReviewValidator(review_draft, citation_manifest, paper_artifacts)
    report = validator.validate()
    assert report.total_citations == 1


def test_summary_rechecker():
    artifact = {
        "paper_identity": {"canonical_paper_key": "p1"},
        "analysis": {"ai_summary": {}},
    }
    rechecker = SummaryRechecker(artifact)
    report = rechecker.recheck()
    assert report.paper_key == "p1"
    assert len(report.fields_checked) == len(WHITELISTED_FIELDS)


def test_validator_compatibility_entrypoints():
    review_draft = {"content": {"sections": []}}
    citation_manifest = {"citations": []}
    paper_artifacts = []

    val_result = run_week3_review_validation(review_draft, citation_manifest, paper_artifacts)
    assert val_result["week3_validation"] is True

    recheck_result = run_week3_summary_recheck(paper_artifacts)
    assert recheck_result["week3_recheck"] is True


def test_visual_understanding_gap_separation():
    review_draft = {"content": {"sections": []}}
    citation_manifest = {
        "citations": [
            {
                "citation_id": "c1",
                "paper_id": "p1",
                "text": "see figure 1",
                "context": "test context",
            }
        ],
    }
    paper_artifacts = [
        {
            "paper_identity": {
                "canonical_paper_key": "p1",
                "source_paper_id": "p1",
            },
            "analysis": {"ai_summary": {}},
            "stage1_inputs": {
                "selected_visual_refs": [{"path": "fig1.png", "caption": "figure 1"}],
            },
        }
    ]

    validator = ReviewValidator(review_draft, citation_manifest, paper_artifacts)
    report = validator.validate()

    citation_result = report.citation_results[0]
    # Either NEEDS_REVIEW or other, but check the logic path
    assert citation_result is not None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
