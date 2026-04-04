import os
import json
import pytest
from services.review_draft import build_review_draft_v2, ReviewBlock, ReviewSection
from services.citation_manifest import build_citation_manifest_v2_from_review_draft


@pytest.fixture
def sample_paper_summaries():
    """Sample paper summaries for testing"""
    return [
        {
            'paper_info': {
                'title': 'Test Paper 1',
                'authors': ['John Doe', 'Jane Smith'],
                'year': '2020',
                'journal': 'Test Journal'
            }
        },
        {
            'paper_info': {
                'title': 'Test Paper 2',
                'authors': ['Alice Brown', 'Bob Wilson'],
                'year': '2021',
                'journal': 'Another Journal'
            }
        }
    ]


def test_review_draft_v2_persists_structured_citation_refs():
    """Test that review_draft_v2 persists minimal structured citation refs"""
    # Create test sections with structured citations
    test_sections = [
        {
            'section_number': 1,
            'section_title': 'Introduction',
            'content': 'This is a test section with citations.'
        }
    ]
    
    # Build review draft
    draft = build_review_draft_v2(
        job_id='test_job',
        project_name='test_project',
        draft_id='test_draft',
        outline_artifact_id='test_outline',
        outline_source_path='test_outline.md',
        summary_file='test_summaries.json',
        review_word_path='test_review.docx',
        sections=test_sections,
        references=['Test Paper 1', 'Test Paper 2'],
        generation_mode='test'
    )
    
    # Convert to dict and back to simulate serialization/deserialization
    draft_dict = draft.to_dict()
    
    # Verify structure
    assert 'content' in draft_dict
    assert 'sections' in draft_dict['content']
    assert len(draft_dict['content']['sections']) == 1
    
    # Verify blocks have citations field
    section = draft_dict['content']['sections'][0]
    assert 'blocks' in section
    assert len(section['blocks']) == 1
    block = section['blocks'][0]
    assert 'citations' in block
    assert isinstance(block['citations'], list)


def test_citation_manifest_prefers_structured_refs(sample_paper_summaries):
    """Test that citation manifest builder prefers explicit structured refs"""
    # Create review draft structure with structured citations
    review_draft_v2 = {
        'content': {
            'sections': [
                {
                    'section_number': 1,
                    'section_title': 'Introduction',
                    'blocks': [
                        {
                            'block_id': 's1_b1',
                            'block_order': 1,
                            'text': 'This is a test with structured citations.',
                            'citations': [
                                {
                                    'paper_id': 'test paper 1',
                                    'paper_key': 'test paper 1',
                                    'text': '(Doe & Smith, 2020)',
                                    'year': '2020'
                                }
                            ]
                        }
                    ]
                }
            ],
            'references': ['Test Paper 1', 'Test Paper 2']
        }
    }
    
    # Build citation manifest
    manifest = build_citation_manifest_v2_from_review_draft(
        job_id='test_job',
        project_name='test_project',
        manifest_id='test_manifest',
        review_draft_path='test_draft.json',
        review_word_path='test_review.docx',
        review_draft_v2=review_draft_v2,
        paper_summaries=sample_paper_summaries
    )
    
    # Verify structured citations were used
    assert len(manifest.occurrences) == 1
    occurrence = manifest.occurrences[0]
    assert occurrence.paper_id == 'test paper 1'
    assert occurrence.citation_token == '(Doe & Smith, 2020)'


def test_citation_manifest_fallback_to_legacy_extraction(sample_paper_summaries):
    """Test that legacy regex/heuristic fallback works when structured refs are absent"""
    # Create review draft structure without structured citations
    review_draft_v2 = {
        'content': {
            'sections': [
                {
                    'section_number': 1,
                    'section_title': 'Introduction',
                    'blocks': [
                        {
                            'block_id': 's1_b1',
                            'block_order': 1,
                            'text': 'This is a test with traditional citations (Doe, 2020).'
                        }
                    ]
                }
            ],
            'references': ['Test Paper 1', 'Test Paper 2']
        }
    }
    
    # Build citation manifest
    manifest = build_citation_manifest_v2_from_review_draft(
        job_id='test_job',
        project_name='test_project',
        manifest_id='test_manifest',
        review_draft_path='test_draft.json',
        review_word_path='test_review.docx',
        review_draft_v2=review_draft_v2,
        paper_summaries=sample_paper_summaries
    )
    
    # Verify legacy extraction worked
    assert len(manifest.occurrences) > 0
    occurrence = manifest.occurrences[0]
    assert '(Doe, 2020)' in occurrence.citation_token


def test_citation_manifest_handles_mixed_citations(sample_paper_summaries):
    """Test that citation manifest handles both structured and legacy citations"""
    # Create review draft structure with mixed citations
    review_draft_v2 = {
        'content': {
            'sections': [
                {
                    'section_number': 1,
                    'section_title': 'Introduction',
                    'blocks': [
                        {
                            'block_id': 's1_b1',
                            'block_order': 1,
                            'text': 'This has both structured and traditional (Brown, 2021) citations.',
                            'citations': [
                                {
                                    'paper_id': 'test paper 1',
                                    'paper_key': 'test paper 1',
                                    'text': '(Doe & Smith, 2020)',
                                    'year': '2020'
                                }
                            ]
                        }
                    ]
                }
            ],
            'references': ['Test Paper 1', 'Test Paper 2']
        }
    }
    
    # Build citation manifest
    manifest = build_citation_manifest_v2_from_review_draft(
        job_id='test_job',
        project_name='test_project',
        manifest_id='test_manifest',
        review_draft_path='test_draft.json',
        review_word_path='test_review.docx',
        review_draft_v2=review_draft_v2,
        paper_summaries=sample_paper_summaries
    )
    
    # Verify only structured citations were used (structured takes priority)
    assert len(manifest.occurrences) == 1
    occurrence = manifest.occurrences[0]
    assert occurrence.citation_token == '(Doe & Smith, 2020)'
