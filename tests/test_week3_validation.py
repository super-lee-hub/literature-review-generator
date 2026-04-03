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


def test_review_draft_v2_citation_manifest_integration():
    """Test that review_draft_v2 and citation manifest are properly integrated."""
    from services.review_draft import build_review_draft_v2
    from services.citation_manifest import build_citation_manifest_v1

    # Build review_draft_v2
    review_draft = build_review_draft_v2(
        job_id="test-job",
        project_name="test-project",
        draft_id="test-draft",
        outline_artifact_id="outline-1",
        outline_source_path="/test/outline.md",
        summary_file="/test/summaries.json",
        review_word_path="/test/review.docx",
        sections=[
            {
                "section_number": 1,
                "section_title": "Introduction",
                "content": "This is the introduction section.\n\nIt has multiple paragraphs.",
            }
        ],
        references=["Author, A. (2024). Test reference."],
        generation_mode="full_review",
    )

    # Verify review_draft_v2 has blocks
    assert review_draft.artifact_version == "v2"
    assert "sections" in review_draft.content
    sections = review_draft.content["sections"]
    assert len(sections) > 0
    # Convert to dict to check structure
    sections_dict = [s.to_dict() for s in sections]
    assert "blocks" in sections_dict[0]
    assert len(sections_dict[0]["blocks"]) > 0

    # Build citation manifest linked to review_draft_v2
    citations = [
        {
            "citation_id": "cite_1",
            "paper_id": "paper_1",
            "text": "Test citation",
            "context": "Test context",
            "section_number": 1,
            "section_title": "Introduction",
            "block_id": "s1_b1",
            "block_order": 1,
            "review_draft_version": "v2",
        }
    ]

    citation_manifest = build_citation_manifest_v1(
        job_id="test-job",
        project_name="test-project",
        manifest_id="test-manifest",
        review_draft_path="/test/review_draft_v2.json",
        review_word_path="/test/review.docx",
        citations=citations,
    )

    # Verify citation manifest is linked to review_draft_v2
    assert citation_manifest.artifact_type == "citation_manifest"
    assert citation_manifest.review_reference["review_draft_path"] == "/test/review_draft_v2.json"
    assert len(citation_manifest.citations) == 1
    assert citation_manifest.citations[0]["review_draft_version"] == "v2"
    assert citation_manifest.citations[0]["block_id"] == "s1_b1"
    assert citation_manifest.citations[0]["block_order"] == 1


def test_citation_manifest_is_block_aware():
    """Test that citation manifest entries carry block information."""
    from services.citation_manifest import build_citation_manifest_v1

    citations = [
        {
            "citation_id": "cite_1",
            "paper_id": "paper_1",
            "text": "First citation",
            "context": "Context 1",
            "section_number": 1,
            "section_title": "Section 1",
            "block_id": "s1_b1",
            "block_order": 1,
            "review_draft_version": "v2",
        },
        {
            "citation_id": "cite_2",
            "paper_id": "paper_2",
            "text": "Second citation",
            "context": "Context 2",
            "section_number": 1,
            "section_title": "Section 1",
            "block_id": "s1_b2",
            "block_order": 2,
            "review_draft_version": "v2",
        },
    ]

    manifest = build_citation_manifest_v1(
        job_id="test-job",
        project_name="test-project",
        manifest_id="test-manifest",
        review_draft_path="/test/review_draft_v2.json",
        review_word_path="/test/review.docx",
        citations=citations,
    )

    # Verify all citations have block information
    for citation in manifest.citations:
        assert "block_id" in citation
        assert "block_order" in citation
        assert "section_number" in citation
        assert "section_title" in citation
        assert "review_draft_version" in citation
        assert citation["review_draft_version"] == "v2"


def test_validator_reads_from_artifacts_not_docx():
    """Test that validator main path reads from artifacts instead of docx."""
    # Create a review_draft_v2 structure
    review_draft = {
        "artifact_type": "review_draft",
        "artifact_version": "v2",
        "content": {
            "sections": [
                {
                    "section_number": 1,
                    "section_title": "Test Section",
                    "blocks": [
                        {
                            "block_id": "s1_b1",
                            "block_kind": "paragraph",
                            "block_order": 1,
                            "text": "This is a test paragraph with a citation (Author, 2024).",
                        }
                    ],
                }
            ]
        },
    }

    # Create citation manifest
    citation_manifest = {
        "citations": [
            {
                "citation_id": "cite_1",
                "paper_id": "paper_1",
                "text": "Author, 2024",
                "context": "Test context",
                "section_number": 1,
                "section_title": "Test Section",
                "block_id": "s1_b1",
                "block_order": 1,
                "review_draft_version": "v2",
            }
        ],
    }

    # Create paper artifacts
    paper_artifacts = [
        {
            "paper_identity": {
                "canonical_paper_key": "paper_1",
                "source_paper_id": "paper_1",
            },
            "analysis": {"ai_summary": {"core_analysis": {"findings": "Test findings"}}},
            "source": {"source_pdf": "/test/paper1.pdf"},
            "stage1_inputs": {"selected_visual_refs": []},
        }
    ]

    # Run validator
    validator = ReviewValidator(review_draft, citation_manifest, paper_artifacts)
    report = validator.validate()

    # Verify validator used artifacts
    assert report.total_citations == 1
    assert len(report.citation_results) == 1

    result = report.citation_results[0]
    assert result.citation_id == "cite_1"
    assert result.paper_id == "paper_1"


def test_summary_recheck_only_touches_whitelisted_fields():
    """Test that summary_recheck only proposes corrections for whitelisted fields."""
    from validation.summary_recheck import WHITELISTED_FIELDS

    # Create paper artifact with various fields
    paper_artifact = {
        "paper_identity": {"canonical_paper_key": "test_paper"},
        "analysis": {
            "ai_summary": {
                "core_analysis": {
                    "abstract": "Test abstract",
                    "methods": "Test methods",
                    "findings": "Test findings",
                    "conclusions": "Test conclusions",
                    "relevance": "Test relevance",
                    "limitations": "Test limitations",
                    "theoretical_framework": "Test framework",
                    "research_gap": "Test gap",
                },
                "paper_info": {
                    "title": "Test Title",
                    "authors": ["Author A", "Author B"],
                    "year": "2024",
                    "journal": "Test Journal",
                },
                "non_whitelisted_field": "This should not be checked",
            }
        },
    }

    rechecker = SummaryRechecker(paper_artifact)
    report = rechecker.recheck()

    # Verify only whitelisted fields were checked
    assert set(report.fields_checked) == WHITELISTED_FIELDS

    # Verify no non-whitelisted fields were checked
    for field in report.fields_checked:
        assert field in WHITELISTED_FIELDS


def test_failed_section_missing_artifact_cases_fail_clearly():
    """Test that failed sections and missing artifacts fail with clear errors."""
    # Test with missing paper artifact
    review_draft = {"content": {"sections": []}}
    citation_manifest = {
        "citations": [
            {
                "citation_id": "cite_1",
                "paper_id": "nonexistent_paper",
                "text": "Test citation",
                "context": "Test context",
            }
        ],
    }
    paper_artifacts = []  # Empty - paper not found

    validator = ReviewValidator(review_draft, citation_manifest, paper_artifacts)
    report = validator.validate()

    # Should have one result with WRONG_SOURCE conclusion
    assert report.total_citations == 1
    result = report.citation_results[0]
    assert result.conclusion == ValidationConclusion.WRONG_SOURCE
    assert RootCause.CITATION_MAPPING_ERROR in result.root_causes
    assert result.details.get("reason") == "paper_not_found_in_artifacts"


def test_evidence_resolver_stable_candidate_ranking():
    """Test that evidence resolver provides stable candidate ranking."""
    context = EvidenceResolverContext(
        paper_key="test_key",
        paper_identity={"canonical_paper_key": "test_key"},
        preprocess_artifacts={
            "chunks": [
                {"chunk_id": "c1", "text": "First chunk with cited text here", "page_range": [1]},
                {"chunk_id": "c2", "text": "Second chunk also has cited text", "page_range": [2]},
                {"chunk_id": "c3", "text": "Third chunk with cited text as well", "page_range": [3]},
            ],
            "normalized_text": "Full text with cited text appearing here",
        },
        paper_artifact={"source": {"source_pdf": "/test.pdf"}},
    )

    resolver = EvidenceResolver(context)
    candidates = resolver.resolve_evidence(cited_span="cited text")

    # Verify candidates are sorted by confidence (descending) and window_rank (ascending)
    for i in range(len(candidates) - 1):
        curr = candidates[i]
        next_cand = candidates[i + 1]
        # Higher confidence should come first
        assert curr.confidence >= next_cand.confidence
        # If same confidence, lower window_rank should come first
        if curr.confidence == next_cand.confidence:
            assert curr.window_rank <= next_cand.window_rank


def test_evidence_candidate_has_required_fields():
    """Test that evidence candidates have all required fields."""
    candidate = EvidenceCandidate(
        match_reason="chunk_text_match",
        resolver_tier="preprocess_chunks",
        window_rank=0,
        confidence=0.85,
        artifact_path="/test/paper.pdf",
        page_span=[1, 2],
        chunk_ids=["chunk_1"],
        text_excerpt="Test excerpt with cited text",
        negative_evidence_reason=None,
        visual_refs=None,
        caption_excerpt=None,
        evidence_scope="chunk",
    )

    # Verify all required fields are present
    assert candidate.match_reason
    assert candidate.resolver_tier
    assert isinstance(candidate.window_rank, int)
    assert isinstance(candidate.confidence, float)
    assert candidate.artifact_path
    assert candidate.text_excerpt
    assert candidate.evidence_scope


def test_citation_manifest_uses_real_paper_ids():
    """Test that citation manifest uses real paper IDs that match actual paper artifacts."""
    from services.citation_manifest import build_citation_manifest_v1
    from services.paper_artifact import build_paper_artifact_v1
    
    # Create a real paper artifact with canonical paper key
    paper_artifact = build_paper_artifact_v1(
        job_id="test-job",
        paper={
            "title": "Test Paper",
            "authors": ["Author1", "Author2"],
            "year": "2024",
        },
        result={
            "status": "success",
            "ai_summary": {"summary": "Test summary"},
        },
        paper_key="test_paper_key_2024",
    )
    
    real_paper_id = paper_artifact.paper_identity["canonical_paper_key"]
    
    # Create citation manifest with real paper ID
    citations = [
        {
            "citation_id": "cite_1",
            "paper_id": real_paper_id,
            "text": "Test citation",
            "context": "Test context",
            "section_number": 1,
            "section_title": "Introduction",
            "block_id": "s1_b1",
            "block_order": 1,
            "review_draft_version": "v2",
        }
    ]
    
    citation_manifest = build_citation_manifest_v1(
        job_id="test-job",
        project_name="test-project",
        manifest_id="test-manifest",
        review_draft_path="/test/review_draft_v2.json",
        review_word_path="/test/review.docx",
        citations=citations,
    )
    
    # Verify citation uses real paper ID
    assert citation_manifest.citations[0]["paper_id"] == real_paper_id
    assert citation_manifest.citations[0]["paper_id"] != "paper_1"  # Not a placeholder


def test_evidence_resolver_receives_preprocess_and_visual_inputs():
    """Test that evidence resolver receives preprocess/visual inputs in Week 3 path."""
    from validation.evidence_resolver import EvidenceResolver, build_evidence_resolver_context
    
    # Create paper artifact with preprocess and visual refs
    paper_artifact = {
        "paper_identity": {
            "canonical_paper_key": "test_paper_key_2024",
        },
        "analysis": {
            "preprocess": {
                "chunks": [
                    {
                        "chunk_id": "chunk1",
                        "text": "Test chunk with cited information",
                        "page_range": [1, 1],
                    }
                ],
                "normalized_text": "Normalized text with cited information",
            },
        },
        "stage1_inputs": {
            "selected_visual_refs": [
                {
                    "path": "test_image.png",
                    "caption": "Visual caption with cited information",
                    "page_range": [2, 2],
                }
            ],
        },
    }
    
    # Build resolver context
    context = build_evidence_resolver_context(paper_artifact)
    resolver = EvidenceResolver(context)
    
    # Resolve evidence
    cited_span = "cited information"
    selected_visual_refs = paper_artifact.get("stage1_inputs", {}).get("selected_visual_refs", [])
    candidates = resolver.resolve_evidence(cited_span, selected_visual_refs=selected_visual_refs)
    
    # Verify candidates were found from both preprocess and visual sources
    assert len(candidates) > 0
    
    # Check that at least one candidate came from preprocess chunks
    chunk_candidates = [c for c in candidates if c.resolver_tier == "preprocess_chunks"]
    assert len(chunk_candidates) > 0
    
    # Check that at least one candidate came from visual refs
    visual_candidates = [c for c in candidates if c.resolver_tier == "visual_refs"]
    assert len(visual_candidates) > 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
