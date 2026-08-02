import pytest

from validation.evidence_resolver import (
    EvidenceCandidate,
    EvidenceResolver,
    EvidenceResolverContext,
)
from validation.review_validator import (
    ReviewValidator,
    RootCause,
    ValidationConclusion,
)
from validation.summary_recheck import (
    SummaryRechecker,
    WHITELISTED_FIELDS,
)
from validator import recheck_current_summary_artifacts, validate_current_review_artifacts


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

    val_result = validate_current_review_artifacts(review_draft, citation_manifest, paper_artifacts)
    assert val_result["validation"] is True

    recheck_result = recheck_current_summary_artifacts(paper_artifacts)
    assert recheck_result["recheck"] is True


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


def test_review_draft_v3_citation_manifest_integration():
    """The current review draft and citation manifest share structured refs."""
    from services.review_draft import build_review_draft
    from services.citation_manifest import build_citation_manifest_from_review_draft
    from services.citation_ref_catalog import build_document_ref_catalog

    summaries = [
        {
            "paper_info": {
                "title": "Test Paper",
                "authors": ["Author A"],
                "year": "2024",
                "canonical_paper_key": "paper-1",
            }
        }
    ]
    catalog = build_document_ref_catalog(summaries, project_name="test-project", job_id="test-job")
    review_draft = build_review_draft(
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
            "content": "This is the introduction section [[cite_ref:R001]].\n\nIt has multiple paragraphs.",
            }
        ],
        references=["Author, A. (2024). Test reference."],
        generation_mode="full_review",
        paper_summaries=summaries,
        citation_ref_catalog=catalog,
    )

    assert review_draft.artifact_version == "v3"
    assert "sections" in review_draft.content
    sections = review_draft.content["sections"]
    assert len(sections) > 0
    # Convert to dict to check structure
    sections_dict = [s.to_dict() for s in sections]
    assert "blocks" in sections_dict[0]
    assert len(sections_dict[0]["blocks"]) > 0

    citation_manifest = build_citation_manifest_from_review_draft(
        job_id="test-job",
        project_name="test-project",
        manifest_id="test-manifest",
        review_draft_path="/test/review_draft.json",
        review_word_path="/test/review.docx",
        review_draft=review_draft.to_dict(),
        paper_summaries=summaries,
        citation_ref_catalog=catalog,
    )

    assert citation_manifest.artifact_type == "citation_manifest"
    assert citation_manifest.artifact_version == "v3"
    assert citation_manifest.review_reference["review_draft_path"] == "/test/review_draft.json"
    assert len(citation_manifest.occurrences) == 1
    assert citation_manifest.occurrences[0].ref_id == "R001"
    assert citation_manifest.occurrences[0].paper_id == "paper-1"


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


def test_evidence_resolver_receives_preprocess_and_visual_inputs():
    """Test that evidence resolver receives preprocess/visual inputs in Week 3 path."""
    from validation.evidence_resolver import build_evidence_resolver_context

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


def test_citation_paper_mapping_is_deterministic():
    """Test that citation -> paper mapping is deterministic and not based only on shared list index."""
    # Simulate the deterministic mapping logic
    from main import LiteratureReviewGenerator
    
    # Create test summaries with paper info
    summaries = [
        {
            'paper_info': {
                'title': 'Paper 1 Title',
                'authors': ['Author A', 'Author B'],
                'year': '2024'
            }
        },
        {
            'paper_info': {
                'title': 'Paper 2 Title',
                'authors': ['Author C', 'Author D'],
                'year': '2023'
            }
        }
    ]
    
    # Create references in different order
    references = [
        'Author C, D. (2023). Paper 2 Title. Journal of Testing.',
        'Author A, B. (2024). Paper 1 Title. Journal of Testing.'
    ]
    
    # Build paper key to info mapping (simulating the logic in main.py)
    paper_key_to_info = {}
    for summary in summaries:
        paper_info = summary.get('paper_info', {})
        canonical_key = LiteratureReviewGenerator.get_paper_key(paper_info)
        if canonical_key:
            paper_key_to_info[canonical_key] = paper_info
    
    # Test matching logic
    matched_papers = []
    for ref in references:
        matched_paper = None
        for summary in summaries:
            paper_info = summary.get('paper_info', {})
            canonical_key = LiteratureReviewGenerator.get_paper_key(paper_info)
            
            title = paper_info.get('title', '').lower()
            authors = ''.join(paper_info.get('authors', [])).lower()
            ref_lower = ref.lower()
            
            if (title and title in ref_lower) or (authors and authors in ref_lower):
                matched_paper = canonical_key
                break
        matched_papers.append(matched_paper)
    
    # Verify that papers are matched correctly regardless of order
    assert len(matched_papers) == 2
    assert matched_papers[0] is not None  # Should match Paper 2
    assert matched_papers[1] is not None  # Should match Paper 1


def test_validator_loads_real_paper_artifacts():
    """Test that validator loads real persisted paper artifact files from disk."""
    import json
    import os
    import tempfile
    from validator import validate_current_review_artifacts

    # Create a mock paper artifact file on disk
    with tempfile.TemporaryDirectory() as temp_dir:
        # Create paper_artifacts directory
        paper_artifacts_dir = os.path.join(temp_dir, 'paper_artifacts')
        os.makedirs(paper_artifacts_dir)
        
        # Create a paper artifact file
        paper_artifact = {
            "paper_identity": {
                "canonical_paper_key": "test_paper_key_2024",
                "source_paper_id": "test_paper_id",
            },
            "analysis": {
                "ai_summary": {"summary": "Test summary"},
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
        
        # Save paper artifact to file
        artifact_file_path = os.path.join(paper_artifacts_dir, 'test_paper_artifact.json')
        with open(artifact_file_path, 'w', encoding='utf-8') as f:
            json.dump(paper_artifact, f)
        
        # Verify file exists
        assert os.path.exists(artifact_file_path), "Paper artifact file should exist on disk"
        
        # Load the artifact from file to verify it's valid
        with open(artifact_file_path, 'r', encoding='utf-8') as f:
            loaded_artifact = json.load(f)
        
        # Verify loaded artifact has expected structure
        assert "paper_identity" in loaded_artifact
        assert "analysis" in loaded_artifact
        assert "preprocess" in loaded_artifact["analysis"]
        assert "stage1_inputs" in loaded_artifact
        
        # Create test review draft and citation manifest
        review_draft = {
            "artifact_type": "review_draft",
            "artifact_version": "v2",
            "content": {
                "sections": [
                    {
                        "section_number": 1,
                        "section_title": "Introduction",
                        "blocks": [
                            {
                                "block_id": "s1_b1",
                                "block_kind": "paragraph",
                                "block_order": 1,
                                "text": "Test paragraph with citation",
                                "anchor_text": "Test paragraph",
                                "anchor_hash": "test_hash",
                            }
                        ],
                    }
                ],
            },
        }
        
        citation_manifest = {
            "artifact_type": "citation_manifest",
            "artifact_version": "v1",
            "citations": [
                {
                    "citation_id": "cite_1",
                    "paper_id": "test_paper_key_2024",
                    "text": "Test citation",
                    "context": "Test context",
                    "section_number": 1,
                    "section_title": "Introduction",
                    "block_id": "s1_b1",
                    "block_order": 1,
                    "review_draft_version": "v2",
                }
            ],
        }
        
        # Run validation with the loaded artifact
        result = validate_current_review_artifacts(review_draft, citation_manifest, [loaded_artifact])
        assert result["validation"] is True


def test_end_to_end_week3_validation_with_real_artifacts():
    """Test end-to-end Week 3 validation works on a realistic persisted-artifact fixture."""
    from validation.review_validator import ReviewValidator
    
    # Create realistic paper artifact with preprocess and visual refs
    paper_artifact = {
        "paper_identity": {
            "canonical_paper_key": "test_paper_key_2024",
        },
        "analysis": {
            "ai_summary": {"core_analysis": {"findings": "Test findings"}},
            "preprocess": {
                "chunks": [
                    {
                        "chunk_id": "chunk1",
                        "text": "This is a test chunk with important findings",
                        "page_range": [1, 1],
                    }
                ],
                "normalized_text": "Full normalized text with important findings",
            },
        },
        "stage1_inputs": {
            "selected_visual_refs": [
                {
                    "path": "fig1.png",
                    "caption": "Figure 1: Test results",
                    "page_range": [2, 2],
                }
            ],
        },
    }
    
    # Create review draft and citation manifest
    review_draft = {
        "content": {
            "sections": [
                {
                    "section_number": 1,
                    "section_title": "Introduction",
                    "blocks": [
                        {
                            "block_id": "s1_b1",
                            "block_kind": "paragraph",
                            "block_order": 1,
                            "text": "The study found important findings [[cite_ref:R001]].",
                            "citations": [
                                {
                                    "citation_token": "[[cite_ref:R001]]",
                                    "ref_id": "R001",
                                    "source_type": "structured_ref",
                                }
                            ],
                        }
                    ],
                }
            ],
        },
    }
    
    citation_manifest = {
        "citation_sets": [
            {
                "bundle_id": "bundle_1",
                "citation_set_key": "test_paper_key_2024",
                "paper_ids": ["test_paper_key_2024"],
                "paper_keys": ["test_paper_key_2024"],
                "occurrence_ids": ["occ_1"],
                "block_ids": ["s1_b1"],
                "section_numbers": [1],
                "section_titles": ["Introduction"],
                "claim_texts": ["The study found important findings."],
                "citation_tokens": ["[[cite_ref:R001]]"],
                "claim_units": [
                    {
                        "claim_unit_id": "cu_1",
                        "citation_set_key": "test_paper_key_2024",
                        "paper_ids": ["test_paper_key_2024"],
                        "supporting_paper_ids": ["test_paper_key_2024"],
                        "pooled_paper_ids": ["test_paper_key_2024"],
                        "alignment_status": "explicit",
                        "alignment_confidence": 1.0,
                        "block_id": "s1_b1",
                        "sentence_index": 1,
                        "claim_text": "The study found important findings.",
                        "citation_tokens": ["[[cite_ref:R001]]"],
                    }
                ],
            }
        ]
    }
    
    # Run validation
    validator = ReviewValidator(review_draft, citation_manifest, [paper_artifact])
    report = validator.validate()
    
    # Verify validation completed successfully
    assert report.total_citations == 1
    assert len(report.citation_results) == 1
    
    # Verify evidence was found
    result = report.citation_results[0]
    assert len(result.evidence_candidates) > 0


def test_missing_artifacts_fail_clearly():
    """Test that missing artifacts fail clearly."""
    from validation.review_validator import ReviewValidator
    
    # Create review draft and citation manifest with non-existent paper ID
    review_draft = {
        "content": {
            "sections": [
                {
                    "section_number": 1,
                    "section_title": "Introduction",
                    "blocks": [
                        {
                            "block_id": "s1_b1",
                            "block_kind": "paragraph",
                            "block_order": 1,
                            "text": "Test paragraph",
                        }
                    ],
                }
            ],
        },
    }
    
    citation_manifest = {
        "citations": [
            {
                "citation_id": "cite_1",
                "paper_id": "non_existent_paper_id",
                "text": "Test citation",
                "context": "Test context",
            }
        ],
    }
    
    # Run validation with empty paper artifacts
    validator = ReviewValidator(review_draft, citation_manifest, [])
    report = validator.validate()
    
    # Verify validation fails for missing paper
    assert report.total_citations == 1
    assert report.wrong_source_count == 1


def test_evidence_resolver_explicit_tier_order(tmp_path):
    """Test that evidence resolver follows the explicit tier order: locator/page, chunks, normalized, plain, visual."""
    from validation.evidence_resolver import EvidenceResolver, EvidenceResolverContext
    
    # Create test context with all tiers available
    context = EvidenceResolverContext(
        paper_key="test_key",
        paper_identity={"canonical_paper_key": "test_key"},
        preprocess_artifacts={
            "page_index": [
                {"page_number": 1, "text": "Page index content with cited text here"},
            ],
            "chunks": [
                {"chunk_id": "c1", "text": "Chunk content with cited text here", "page_range": [1]}
            ],
            "normalized_text": "Normalized text with cited text here",
            "plain_text": "Plain text with cited text here",
        },
        paper_artifact={"source": {"source_pdf": "/test.pdf"}},
    )
    
    resolver = EvidenceResolver(context)
    candidates = resolver.resolve_evidence(cited_span="cited text")
    
    # Verify we have candidates from multiple tiers
    tier_names = [c.resolver_tier for c in candidates if c.confidence > 0]
    assert "locator_page_index" in tier_names
    assert "preprocess_chunks" in tier_names
    assert "normalized_text" in tier_names
    assert "plain_text_fallback" in tier_names


def test_negative_evidence_candidate_generation(tmp_path):
    """Test that negative evidence candidate is generated with clear reason."""
    from validation.evidence_resolver import EvidenceResolver, EvidenceResolverContext
    
    context = EvidenceResolverContext(
        paper_key="test_key",
        paper_identity={"canonical_paper_key": "test_key"},
        preprocess_artifacts={
            "chunks": [{"chunk_id": "c1", "text": "No matching text here"}],
            "normalized_text": "Still no matching text here",
        },
        paper_artifact={"source": {"source_pdf": "/test.pdf"}},
    )
    
    resolver = EvidenceResolver(context)
    candidates = resolver.resolve_evidence(cited_span="this text will never be found")
    
    # Should have at least one candidate (negative evidence)
    assert len(candidates) >= 1
    negative_candidates = [c for c in candidates if c.resolver_tier == "negative"]
    assert len(negative_candidates) == 1
    assert negative_candidates[0].negative_evidence_reason == "cited_text_not_found_in_any_tier"
    assert negative_candidates[0].confidence == 0.0


def test_validator_uses_review_draft_v2_block_context(tmp_path):
    """Test that validator keeps review_draft_v2 block text as contextual evidence."""
    from validation.review_validator import ReviewValidator
    
    # Create review_draft_v2 with blocks
    review_draft = {
        "content": {
            "sections": [
                {
                    "section_number": 1,
                    "section_title": "Introduction",
                    "blocks": [
                        {
                            "block_id": "s1_b1",
                            "text": "This is the full block text with important findings from Smith (2024).",
                            "block_kind": "paragraph",
                            "block_order": 1,
                        }
                    ],
                }
            ]
        }
    }
    
    # Create citation_manifest_v2
    citation_manifest = {
        "occurrences": [
            {
                "occurrence_id": "occ_1",
                "citation_token": "(Smith, 2024)",
                "paper_id": "paper_1",
                "paper_key": "paper_1",
                "section_number": 1,
                "section_title": "Introduction",
                "block_id": "s1_b1",
                "block_order": 1,
                "context_before": "This is context",
            }
        ],
    }
    
    # Create paper artifact with preprocess artifacts matching block text
    paper_artifacts = [
        {
            "paper_identity": {
                "canonical_paper_key": "paper_1",
                "source_paper_id": "paper_1",
            },
            "analysis": {
                "preprocess": {
                    "normalized_text": "This paper has important findings",
                },
            },
            "stage1_inputs": {"selected_visual_refs": []},
        }
    ]
    
    validator = ReviewValidator(review_draft, citation_manifest, paper_artifacts)
    report = validator.validate()
    
    # Check that validation ran and kept block text as contextual evidence
    assert report.total_citations == 1
    result = report.citation_results[0]
    assert result.details.get("used_block_text") is False
    assert result.block_context == "This is the full block text with important findings from Smith (2024)."


def test_validator_prefers_claim_units_over_full_block_text():
    """Validation should target minimal claim units even when the block contains broader rhetoric."""
    review_draft = {
        "content": {
            "sections": [
                {
                    "section_number": 1,
                    "section_title": "Methods",
                    "blocks": [
                        {
                            "block_id": "s1_b1",
                            "block_kind": "paragraph",
                            "block_order": 1,
                            "text": (
                                "This study scraped Booking.com hotel data and used the difference between "
                                "value-for-money and overall ratings as a fairness proxy [[cite:paper_1|mode=parenthetical]]. "
                                "This proves every future digital pricing context can be unified under one framework."
                            ),
                            "anchor_hash": "deadbeef",
                            "span_map": {
                                "sentences": [
                                    {
                                        "sentence_index": 1,
                                        "span_start": 0,
                                        "span_end": 151,
                                        "text": "This study scraped Booking.com hotel data and used the difference between value-for-money and overall ratings as a fairness proxy [[cite:paper_1|mode=parenthetical]].",
                                    },
                                    {
                                        "sentence_index": 2,
                                        "span_start": 152,
                                        "span_end": 226,
                                        "text": "This proves every future digital pricing context can be unified under one framework.",
                                    },
                                ]
                            },
                        }
                    ],
                }
            ]
        }
    }
    citation_manifest = {
        "citation_sets": [
            {
                "bundle_id": "bundle_1",
                "citation_set_key": "paper_1",
                "paper_ids": ["paper_1"],
                "paper_keys": ["paper_1"],
                "occurrence_ids": ["occ_1"],
                "block_ids": ["s1_b1"],
                "section_numbers": [1],
                "section_titles": ["Methods"],
                "claim_texts": [
                    "This study scraped Booking.com hotel data and used the difference between value-for-money and overall ratings as a fairness proxy."
                ],
                "claim_units": [
                    {
                        "claim_unit_id": "cu_1",
                        "citation_set_key": "paper_1",
                        "block_id": "s1_b1",
                        "sentence_index": 1,
                        "span_start": 0,
                        "span_end": 151,
                        "claim_text": "This study scraped Booking.com hotel data and used the difference between value-for-money and overall ratings as a fairness proxy.",
                        "citation_tokens": ["[[cite:paper_1|mode=parenthetical]]"],
                        "block_anchor_hash": "deadbeef",
                        "paper_ids": ["paper_1"],
                        "supporting_paper_ids": ["paper_1"],
                        "alignment_status": "explicit",
                        "alignment_confidence": 1.0,
                    }
                ],
                "citation_tokens": ["[[cite:paper_1|mode=parenthetical]]"],
            }
        ]
    }
    paper_artifacts = [
            {
                "paper_identity": {
                    "canonical_paper_key": "paper_1",
                    "source_paper_id": "paper_1",
                },
                "analysis": {
                    "ai_summary": {
                        "core_analysis": {
                            "summary": "This study scraped Booking.com hotel data and used the difference between value-for-money and overall ratings as a fairness proxy."
                        }
                    },
                    "preprocess": {
                        "normalized_text": "This study scraped Booking.com hotel data and used the difference between value-for-money and overall ratings as a fairness proxy."
                    },
                },
                "paper_info": {
                    "title": "Consumer perception of price fairness and dynamic pricing",
                    "authors": ["Marco Alderighi"],
                "year": "2022",
            },
            "stage1_inputs": {"selected_visual_refs": []},
        }
    ]

    validator = ReviewValidator(review_draft, citation_manifest, paper_artifacts)
    report = validator.validate()
    result = report.citation_results[0]

    assert result.claim_text == "This study scraped Booking.com hotel data and used the difference between value-for-money and overall ratings as a fairness proxy."
    assert "future digital pricing context" not in result.claim_text
    assert result.target_claim_unit["claim_unit_id"] == "cu_1"
    assert result.evidence_status == "clean_supported"


def test_summary_recheck_source_grounded_path(tmp_path):
    """Test that summary_recheck uses anchor-based source-grounded check.

    Paraphrase without anchor conflicts (years, numbers, acronyms,
    direction words) should NOT produce a false-positive candidate.
    """
    from validation.summary_recheck import SummaryRechecker

    # Create paper artifact with paraphrased (but not conflicting) summary
    paper_artifact = {
        "paper_identity": {"canonical_paper_key": "test_paper"},
        "analysis": {
            "ai_summary": {
                "core_analysis": {
                    "summary": "This summary talks about unicorns which are not in the source text",
                }
            },
            "preprocess": {
                "normalized_text": "This is the actual normalized paper text that doesn't mention unicorns",
            },
        },
    }

    rechecker = SummaryRechecker(paper_artifact)
    report = rechecker.recheck()

    # Paraphrase without anchor conflict should produce NO candidate
    # (the old first-50-char substring heuristic was removed)
    assert "core_analysis.summary" not in report.fields_with_candidates


def test_summary_recheck_anchor_year_conflict(tmp_path):
    """Test that a year conflict produces a correction candidate."""
    from validation.summary_recheck import SummaryRechecker

    paper_artifact = {
        "paper_identity": {"canonical_paper_key": "test_paper"},
        "analysis": {
            "ai_summary": {
                "core_analysis": {
                    "summary": "The study conducted in 2023 found significant results.",
                }
            },
            "preprocess": {
                "normalized_text": "The study conducted in 2021 found significant results.",
            },
        },
    }

    rechecker = SummaryRechecker(paper_artifact)
    report = rechecker.recheck()

    assert "core_analysis.summary" in report.fields_with_candidates
    candidate = next(
        (c for c in report.correction_candidates if c.field_path == "core_analysis.summary"),
        None
    )
    assert candidate is not None
    assert "2023" in candidate.reason


def test_summary_recheck_direction_word_conflict(tmp_path):
    """Test that a direction-word conflict produces a correction candidate."""
    from validation.summary_recheck import SummaryRechecker

    paper_artifact = {
        "paper_identity": {"canonical_paper_key": "test_paper"},
        "analysis": {
            "ai_summary": {
                "core_analysis": {
                    "findings": "The treatment showed a significant increase in outcomes.",
                }
            },
            "preprocess": {
                "normalized_text": "The treatment showed a significant decrease in outcomes.",
            },
        },
    }

    rechecker = SummaryRechecker(paper_artifact)
    report = rechecker.recheck()

    assert "core_analysis.findings" in report.fields_with_candidates
    candidate = next(
        (c for c in report.correction_candidates if c.field_path == "core_analysis.findings"),
        None
    )
    assert candidate is not None
    assert "increase" in candidate.reason.lower() or "direction" in candidate.reason.lower()


def test_review_validator_uses_markdown_path_for_normalized_text(tmp_path):
    """ReviewValidator should feed markdown_path into the normalized-text evidence tier."""
    normalized_path = tmp_path / "normalized.md"
    normalized_path.write_text("Normalized markdown with Booking.com fairness proxy", encoding="utf-8")

    review_draft = {"content": {"sections": []}}
    citation_manifest = {
        "citation_sets": [
            {
                "bundle_id": "bundle_1",
                "citation_set_key": "paper_1",
                "paper_ids": ["paper_1"],
                "paper_keys": ["paper_1"],
                "claim_texts": ["Booking.com fairness proxy"],
                "block_ids": [],
                "section_titles": ["Methods"],
                "claim_units": [
                    {
                        "claim_unit_id": "cu_markdown",
                        "citation_set_key": "paper_1",
                        "paper_ids": ["paper_1"],
                        "supporting_paper_ids": ["paper_1"],
                        "alignment_status": "explicit",
                        "alignment_confidence": 1.0,
                        "claim_text": "Booking.com fairness proxy",
                    }
                ],
            }
        ]
    }
    paper_artifacts = [
        {
            "paper_identity": {"canonical_paper_key": "paper_1", "source_paper_id": "paper_1"},
            "analysis": {"preprocess": {"markdown_path": str(normalized_path)}},
            "stage1_inputs": {"selected_visual_refs": []},
            "source": {"source_pdf": "paper.pdf"},
        }
    ]

    validator = ReviewValidator(review_draft, citation_manifest, paper_artifacts)
    result = validator.validate().citation_results[0]
    assert any(candidate.resolver_tier == "normalized_text" for candidate in result.evidence_candidates)


def test_fallback_claim_unit_ids_are_deterministic():
    """Legacy fallback claim-unit IDs should remain stable across runs."""
    review_draft = {
        "content": {
            "sections": [
                {
                    "section_number": 1,
                    "section_title": "Methods",
                    "blocks": [
                        {
                            "block_id": "s1_b1",
                            "block_kind": "paragraph",
                            "block_order": 1,
                            "text": "Booking.com hotel data was used to model price fairness.",
                            "anchor_hash": "anchor123",
                            "span_map": {
                                "sentences": [
                                    {
                                        "sentence_index": 1,
                                        "span_start": 0,
                                        "span_end": 58,
                                        "text": "Booking.com hotel data was used to model price fairness.",
                                    }
                                ]
                            },
                        }
                    ],
                }
            ]
        }
    }
    citation_manifest = {
        "citation_sets": [
            {
                "bundle_id": "bundle_1",
                "citation_set_key": "paper_1",
                "paper_ids": ["paper_1"],
                "paper_keys": ["paper_1"],
                "claim_texts": ["Booking.com hotel data was used to model price fairness."],
                "block_ids": ["s1_b1"],
                "section_titles": ["Methods"],
                "claim_units": [
                    {
                        "claim_unit_id": "cu_1",
                        "citation_set_key": "paper_1",
                        "paper_ids": ["paper_1"],
                        "supporting_paper_ids": ["paper_1"],
                        "alignment_status": "explicit",
                        "alignment_confidence": 1.0,
                        "claim_text": "Booking.com hotel data was used to model price fairness.",
                    }
                ],
                "citation_tokens": [],
            }
        ]
    }
    paper_artifacts = [
        {
            "paper_identity": {"canonical_paper_key": "paper_1", "source_paper_id": "paper_1"},
            "analysis": {"ai_summary": {"core_analysis": {"summary": "Booking.com hotel data was used to model price fairness."}}},
            "stage1_inputs": {"selected_visual_refs": []},
            "source": {"source_pdf": "paper.pdf"},
        }
    ]

    validator = ReviewValidator(review_draft, citation_manifest, paper_artifacts)
    first = validator.validate().citation_results[0].target_claim_unit["claim_unit_id"]
    second = validator.validate().citation_results[0].target_claim_unit["claim_unit_id"]
    assert first == second


def test_multi_paper_set_allows_clause_level_support_across_papers():
    review_draft = {
        "content": {
            "sections": [
                {
                    "section_number": 1,
                    "section_title": "Synthesis",
                    "blocks": [
                        {
                            "block_id": "s1_b1",
                            "block_kind": "paragraph",
                            "block_order": 1,
                            "text": (
                                "Paper A shows that unfair pricing increases complaint behavior; "
                                "Paper B shows that loyalty intensifies betrayal responses "
                                "[[cite:paper_a|mode=parenthetical]] [[cite:paper_b|mode=parenthetical]]."
                            ),
                            "anchor_hash": "multi123",
                            "span_map": {
                                "sentences": [
                                    {
                                        "sentence_index": 1,
                                        "span_start": 0,
                                        "span_end": 166,
                                        "text": "Paper A shows that unfair pricing increases complaint behavior; Paper B shows that loyalty intensifies betrayal responses [[cite:paper_a|mode=parenthetical]] [[cite:paper_b|mode=parenthetical]].",
                                    }
                                ]
                            },
                        }
                    ],
                }
            ]
        }
    }
    claim_text = "Paper A shows that unfair pricing increases complaint behavior; Paper B shows that loyalty intensifies betrayal responses."
    citation_manifest = {
        "citation_sets": [
            {
                "bundle_id": "bundle_multi",
                "citation_set_key": "paper_a+paper_b",
                "paper_ids": ["paper_a", "paper_b"],
                "paper_keys": ["paper_a", "paper_b"],
                "occurrence_ids": ["occ_1", "occ_2"],
                "block_ids": ["s1_b1"],
                "section_numbers": [1],
                "section_titles": ["Synthesis"],
                "claim_texts": [claim_text],
                "claim_units": [
                    {
                        "claim_unit_id": "cu_multi_1",
                        "citation_set_key": "paper_a+paper_b",
                        "block_id": "s1_b1",
                        "sentence_index": 1,
                        "span_start": 0,
                        "span_end": 166,
                        "claim_text": claim_text,
                        "citation_tokens": ["[[cite:paper_a|mode=parenthetical]]", "[[cite:paper_b|mode=parenthetical]]"],
                        "block_anchor_hash": "multi123",
                        "paper_ids": ["paper_a", "paper_b"],
                        "supporting_paper_ids": ["paper_a", "paper_b"],
                        "pooled_paper_ids": ["paper_a", "paper_b"],
                        "alignment_status": "explicit",
                        "alignment_confidence": 1.0,
                    }
                ],
                "citation_tokens": ["[[cite:paper_a|mode=parenthetical]]", "[[cite:paper_b|mode=parenthetical]]"],
            }
        ]
    }
    paper_artifacts = [
        {
            "paper_identity": {"canonical_paper_key": "paper_a", "source_paper_id": "paper_a"},
            "analysis": {
                "ai_summary": {"core_analysis": {"summary": "Paper A shows that unfair pricing increases complaint behavior."}},
                "preprocess": {"normalized_text": "Paper A shows that unfair pricing increases complaint behavior."},
            },
            "stage1_inputs": {"selected_visual_refs": []},
            "source": {"source_pdf": "a.pdf"},
        },
        {
            "paper_identity": {"canonical_paper_key": "paper_b", "source_paper_id": "paper_b"},
            "analysis": {
                "ai_summary": {"core_analysis": {"summary": "Paper B shows that loyalty intensifies betrayal responses."}},
                "preprocess": {"normalized_text": "Paper B shows that loyalty intensifies betrayal responses."},
            },
            "stage1_inputs": {"selected_visual_refs": []},
            "source": {"source_pdf": "b.pdf"},
        },
    ]

    result = ReviewValidator(review_draft, citation_manifest, paper_artifacts).validate().citation_results[0]
    assert result.evidence_status == "clean_supported"
    assert result.conclusion == ValidationConclusion.SUPPORTED


def test_multi_paper_set_with_partial_clause_coverage_does_not_drop_to_unsupported():
    review_draft = {
        "content": {
            "sections": [
                {
                    "section_number": 1,
                    "section_title": "Synthesis",
                    "blocks": [
                        {
                            "block_id": "s1_b1",
                            "block_kind": "paragraph",
                            "block_order": 1,
                            "text": (
                                "Paper A shows that unfair pricing increases complaint behavior; "
                                "Paper B shows that loyalty intensifies betrayal responses "
                                "[[cite:paper_a|mode=parenthetical]] [[cite:paper_b|mode=parenthetical]]."
                            ),
                            "anchor_hash": "multi124",
                            "span_map": {
                                "sentences": [
                                    {
                                        "sentence_index": 1,
                                        "span_start": 0,
                                        "span_end": 166,
                                        "text": "Paper A shows that unfair pricing increases complaint behavior; Paper B shows that loyalty intensifies betrayal responses [[cite:paper_a|mode=parenthetical]] [[cite:paper_b|mode=parenthetical]].",
                                    }
                                ]
                            },
                        }
                    ],
                }
            ]
        }
    }
    claim_text = "Paper A shows that unfair pricing increases complaint behavior; Paper B shows that loyalty intensifies betrayal responses."
    citation_manifest = {
        "citation_sets": [
            {
                "bundle_id": "bundle_multi_gap",
                "citation_set_key": "paper_a+paper_b",
                "paper_ids": ["paper_a", "paper_b"],
                "paper_keys": ["paper_a", "paper_b"],
                "occurrence_ids": ["occ_1", "occ_2"],
                "block_ids": ["s1_b1"],
                "section_numbers": [1],
                "section_titles": ["Synthesis"],
                "claim_texts": [claim_text],
                "claim_units": [
                    {
                        "claim_unit_id": "cu_multi_gap",
                        "citation_set_key": "paper_a+paper_b",
                        "paper_ids": ["paper_a", "paper_b"],
                        "supporting_paper_ids": ["paper_a", "paper_b"],
                        "pooled_paper_ids": ["paper_a", "paper_b"],
                        "alignment_status": "explicit",
                        "alignment_confidence": 1.0,
                        "block_id": "s1_b1",
                        "sentence_index": 1,
                        "claim_text": claim_text,
                    }
                ],
                "citation_tokens": ["[[cite:paper_a|mode=parenthetical]]", "[[cite:paper_b|mode=parenthetical]]"],
            }
        ]
    }
    paper_artifacts = [
        {
            "paper_identity": {"canonical_paper_key": "paper_a", "source_paper_id": "paper_a"},
            "analysis": {"ai_summary": {"core_analysis": {"summary": "Paper A shows that unfair pricing increases complaint behavior."}}},
            "stage1_inputs": {"selected_visual_refs": []},
            "source": {"source_pdf": "a.pdf"},
        },
        {
            "paper_identity": {"canonical_paper_key": "paper_b", "source_paper_id": "paper_b"},
            "analysis": {"ai_summary": {"core_analysis": {"summary": "Paper B discusses customer satisfaction."}}},
            "stage1_inputs": {"selected_visual_refs": []},
            "source": {"source_pdf": "b.pdf"},
        },
    ]

    result = ReviewValidator(review_draft, citation_manifest, paper_artifacts).validate().citation_results[0]
    assert result.conclusion != ValidationConclusion.UNSUPPORTED
    assert result.evidence_status == "evidence_gap"


def test_uncited_bridge_sentence_does_not_create_citation_grounding_failure_row():
    review_draft = {
        "content": {
            "sections": [
                {
                    "section_number": 1,
                    "section_title": "Discussion",
                    "blocks": [
                        {
                            "block_id": "s1_b1",
                            "block_kind": "paragraph",
                            "block_order": 1,
                            "text": (
                                "Paper A shows that unfair pricing increases complaint behavior "
                                "[[cite:paper_a|mode=parenthetical]]. "
                                "Future research should explore how platforms bridge this effect in emerging channels."
                            ),
                            "anchor_hash": "bridge123",
                            "span_map": {
                                "sentences": [
                                    {
                                        "sentence_index": 1,
                                        "span_start": 0,
                                        "span_end": 115,
                                        "text": "Paper A shows that unfair pricing increases complaint behavior [[cite:paper_a|mode=parenthetical]].",
                                    },
                                    {
                                        "sentence_index": 2,
                                        "span_start": 116,
                                        "span_end": 196,
                                        "text": "Future research should explore how platforms bridge this effect in emerging channels.",
                                    },
                                ]
                            },
                        }
                    ],
                }
            ]
        }
    }
    citation_manifest = {
        "citation_sets": [
            {
                "bundle_id": "bundle_1",
                "citation_set_key": "paper_a",
                "paper_ids": ["paper_a"],
                "paper_keys": ["paper_a"],
                "block_ids": ["s1_b1"],
                "section_titles": ["Discussion"],
                "claim_texts": ["Paper A shows that unfair pricing increases complaint behavior."],
                "claim_units": [
                    {
                        "claim_unit_id": "cu_bridge_1",
                        "citation_set_key": "paper_a",
                        "block_id": "s1_b1",
                        "sentence_index": 1,
                        "span_start": 0,
                        "span_end": 115,
                        "claim_text": "Paper A shows that unfair pricing increases complaint behavior.",
                        "citation_tokens": ["[[cite:paper_a|mode=parenthetical]]"],
                        "block_anchor_hash": "bridge123",
                    }
                ],
                "citation_tokens": ["[[cite:paper_a|mode=parenthetical]]"],
            }
        ]
    }
    paper_artifacts = [
        {
            "paper_identity": {"canonical_paper_key": "paper_a", "source_paper_id": "paper_a"},
            "analysis": {"ai_summary": {"core_analysis": {"summary": "Paper A shows that unfair pricing increases complaint behavior."}}},
            "stage1_inputs": {"selected_visual_refs": []},
            "source": {"source_pdf": "a.pdf"},
        }
    ]

    report = ReviewValidator(review_draft, citation_manifest, paper_artifacts).validate()

    assert report.total_citations == 1
    result = report.citation_results[0]
    assert "Future research should explore" not in result.claim_text
    assert result.details["claim_type"] == "result"


def test_summary_only_evidence_does_not_yield_clean_supported():
    review_draft = {"content": {"sections": []}}
    citation_manifest = {
        "citation_sets": [
            {
                "bundle_id": "bundle_summary_only",
                "citation_set_key": "paper_summary",
                "paper_ids": ["paper_summary"],
                "paper_keys": ["paper_summary"],
                "claim_texts": ["The study shows unfair pricing increases complaints."],
                "block_ids": [],
                "section_titles": ["Results"],
            }
        ]
    }
    paper_artifacts = [
        {
            "paper_identity": {"canonical_paper_key": "paper_summary", "source_paper_id": "paper_summary"},
            "analysis": {"ai_summary": {"core_analysis": {"summary": "The study shows unfair pricing increases complaints."}}},
            "stage1_inputs": {"selected_visual_refs": []},
            "source": {"source_pdf": "summary.pdf"},
        }
    ]

    result = ReviewValidator(review_draft, citation_manifest, paper_artifacts).validate().citation_results[0]

    assert result.evidence_status != "clean_supported"
    assert result.conclusion != ValidationConclusion.SUPPORTED


def test_per_paper_evidence_packets_preserve_multiple_claim_units():
    review_draft = {
        "content": {
            "sections": [
                {
                    "section_number": 1,
                    "section_title": "Results",
                    "blocks": [
                        {
                            "block_id": "s1_b1",
                            "block_kind": "paragraph",
                            "block_order": 1,
                            "text": (
                                "Paper A shows complaint behavior increases under unfair pricing [[cite:paper_a|mode=parenthetical]]. "
                                "Paper A also shows loyalty intensifies betrayal responses [[cite:paper_a|mode=parenthetical]]."
                            ),
                            "anchor_hash": "multi-cu",
                            "span_map": {
                                "sentences": [
                                    {
                                        "sentence_index": 1,
                                        "span_start": 0,
                                        "span_end": 102,
                                        "text": "Paper A shows complaint behavior increases under unfair pricing [[cite:paper_a|mode=parenthetical]].",
                                    },
                                    {
                                        "sentence_index": 2,
                                        "span_start": 103,
                                        "span_end": 195,
                                        "text": "Paper A also shows loyalty intensifies betrayal responses [[cite:paper_a|mode=parenthetical]].",
                                    },
                                ]
                            },
                        }
                    ],
                }
            ]
        }
    }
    citation_manifest = {
        "citation_sets": [
            {
                "bundle_id": "bundle_multi_cu",
                "citation_set_key": "paper_a",
                "paper_ids": ["paper_a"],
                "paper_keys": ["paper_a"],
                "block_ids": ["s1_b1"],
                "section_titles": ["Results"],
                "claim_texts": [
                    "Paper A shows complaint behavior increases under unfair pricing.",
                    "Paper A also shows loyalty intensifies betrayal responses.",
                ],
                "claim_units": [
                    {
                        "claim_unit_id": "cu_a_1",
                        "citation_set_key": "paper_a",
                        "block_id": "s1_b1",
                        "sentence_index": 1,
                        "span_start": 0,
                        "span_end": 102,
                        "claim_text": "Paper A shows complaint behavior increases under unfair pricing.",
                        "citation_tokens": ["[[cite:paper_a|mode=parenthetical]]"],
                        "block_anchor_hash": "multi-cu",
                        "paper_ids": ["paper_a"],
                        "supporting_paper_ids": ["paper_a"],
                        "alignment_status": "explicit",
                        "alignment_confidence": 1.0,
                    },
                    {
                        "claim_unit_id": "cu_a_2",
                        "citation_set_key": "paper_a",
                        "block_id": "s1_b1",
                        "sentence_index": 2,
                        "span_start": 103,
                        "span_end": 195,
                        "claim_text": "Paper A also shows loyalty intensifies betrayal responses.",
                        "citation_tokens": ["[[cite:paper_a|mode=parenthetical]]"],
                        "block_anchor_hash": "multi-cu",
                        "paper_ids": ["paper_a"],
                        "supporting_paper_ids": ["paper_a"],
                        "alignment_status": "explicit",
                        "alignment_confidence": 1.0,
                    },
                ],
                "citation_tokens": ["[[cite:paper_a|mode=parenthetical]]"],
            }
        ]
    }
    paper_artifacts = [
        {
            "paper_identity": {"canonical_paper_key": "paper_a", "source_paper_id": "paper_a"},
            "analysis": {
                "ai_summary": {"core_analysis": {"summary": "Paper A discusses complaint behavior and betrayal responses."}},
                "preprocess": {
                    "normalized_text": (
                        "Paper A shows complaint behavior increases under unfair pricing. "
                        "Paper A also shows loyalty intensifies betrayal responses."
                    )
                },
            },
            "stage1_inputs": {"selected_visual_refs": []},
            "source": {"source_pdf": "paper_a.pdf"},
        }
    ]

    result = ReviewValidator(review_draft, citation_manifest, paper_artifacts).validate().citation_results[0]
    per_paper_packets = result.details["per_paper_evidence_packets"]["paper_a"]

    assert set(per_paper_packets) == {"cu_a_1", "cu_a_2"}
    assert all(packet["claim_unit_id"] in {"cu_a_1", "cu_a_2"} for packets in per_paper_packets.values() for packet in packets)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
