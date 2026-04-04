"""测试结构化引用处理逻辑"""

import json
import os
import tempfile
from typing import Dict, List, Any

import pytest

from services.review_draft import build_review_draft_v2
from services.citation_manifest import build_citation_manifest_v2_from_review_draft


def test_review_draft_v2_with_structured_citations():
    """测试 review_draft_v2 对结构化 block citations 的保留与标准化"""
    # 模拟论文摘要数据
    paper_summaries = [
        {
            'paper_info': {
                'title': 'Test Paper 1',
                'authors': ['Author A', 'Author B'],
                'year': '2023'
            }
        },
        {
            'paper_info': {
                'title': 'Test Paper 2',
                'authors': ['Author C'],
                'year': '2024'
            }
        }
    ]
    
    # 模拟带有结构化 citations 的 sections
    sections = [
        {
            'section_number': 1,
            'section_title': 'Introduction',
            'blocks': [
                {
                    'block_id': 's1_b1',
                    'block_kind': 'paragraph',
                    'block_order': 1,
                    'text': 'This is a test paragraph with citations.',
                    'citations': [
                        {
                            'local_ref_id': 'cit1',
                            'citation_token': '[[cite:test_paper_1|mode=parenthetical]]',
                            'paper_id': 'test_paper_1',
                            'paper_key': 'test_paper_1',
                            'mode': 'parenthetical',
                            'span_start': 10,
                            'span_end': 40
                        }
                    ]
                }
            ]
        }
    ]
    
    # 构建 review_draft_v2
    draft = build_review_draft_v2(
        job_id='test_job',
        project_name='test_project',
        draft_id='test_draft',
        outline_artifact_id='test_outline',
        outline_source_path='test_outline.md',
        summary_file='test_summaries.json',
        review_word_path='test_review.docx',
        sections=sections,
        references=['Test reference 1', 'Test reference 2'],
        generation_mode='full_review',
        paper_summaries=paper_summaries
    )
    
    # 验证结果
    assert draft.artifact_version == 'v2'
    assert len(draft.content['sections']) == 1
    assert len(draft.content['sections'][0].blocks) == 1
    
    block = draft.content['sections'][0].blocks[0]
    assert len(block.citations) == 1
    citation = block.citations[0]
    
    # 验证标准化后的字段
    assert citation['local_ref_id'] == 'cit1'
    assert citation['citation_token'] == '[[cite:test_paper_1|mode=parenthetical]]'
    assert citation['paper_id'] == 'test_paper_1'
    assert citation['paper_key'] == 'test_paper_1'
    assert citation['raw_text'] == '[[cite:test_paper_1|mode=parenthetical]]'
    assert citation['mode'] == 'parenthetical'
    assert citation['source_type'] == 'structured_block'
    assert citation['span_start'] == 10
    assert citation['span_end'] == 40


def test_review_draft_v2_with_mixed_blocks():
    """测试 mixed block 时 structured citations 优先于 regex"""
    # 模拟论文摘要数据
    paper_summaries = [
        {
            'paper_info': {
                'title': 'Test Paper 1',
                'authors': ['Author A', 'Author B'],
                'year': '2023'
            }
        }
    ]
    
    # 模拟混合 blocks：一个带结构化 citations，一个不带
    sections = [
        {
            'section_number': 1,
            'section_title': 'Introduction',
            'blocks': [
                # 带结构化 citations 的 block
                {
                    'block_id': 's1_b1',
                    'block_kind': 'paragraph',
                    'block_order': 1,
                    'text': 'This is a test paragraph with (Author A, 2023) citation.',
                    'citations': [
                        {
                            'local_ref_id': 'cit1',
                            'citation_token': '[[cite:test_paper_1]]',
                            'paper_id': 'test_paper_1',
                            'paper_key': 'test_paper_1'
                        }
                    ]
                },
                # 不带结构化 citations 的 block（应该触发 regex fallback）
                {
                    'block_id': 's1_b2',
                    'block_kind': 'paragraph',
                    'block_order': 2,
                    'text': 'This is another paragraph with (Author A, 2023) citation.'
                }
            ]
        }
    ]
    
    # 构建 review_draft_v2
    draft = build_review_draft_v2(
        job_id='test_job',
        project_name='test_project',
        draft_id='test_draft',
        outline_artifact_id='test_outline',
        outline_source_path='test_outline.md',
        summary_file='test_summaries.json',
        review_word_path='test_review.docx',
        sections=sections,
        references=['Test reference 1'],
        generation_mode='full_review',
        paper_summaries=paper_summaries
    )
    
    # 验证结果
    assert len(draft.content['sections'][0].blocks) == 2
    
    # 第一个 block 应该保留结构化 citations
    block1 = draft.content['sections'][0].blocks[0]
    assert len(block1.citations) == 1
    assert block1.citations[0]['source_type'] == 'structured_block'
    
    # 第二个 block 应该有 regex 提取的 citations
    block2 = draft.content['sections'][0].blocks[1]
    assert len(block2.citations) > 0
    assert block2.citations[0]['source_type'] == 'legacy_regex'


def test_citation_manifest_v2_priority_logic():
    """测试 citation_manifest_v2 的严格优先级逻辑"""
    # 模拟 review_draft_v2 数据
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
                            'text': 'This is a test paragraph with (Author A, 2023) citation.',
                            'citations': [
                                {
                                    'local_ref_id': 'cit1',
                                    'citation_token': '[[cite:test_paper_1]]',
                                    'paper_id': 'test_paper_1',
                                    'paper_key': 'test_paper_1',
                                    'mode': 'parenthetical',
                                    'span_start': 10,
                                    'span_end': 40
                                }
                            ]
                        }
                    ]
                }
            ],
            'references': ['Author A, B. (2023). Test Paper 1. Journal of Testing.']
        }
    }
    
    # 模拟论文摘要数据
    paper_summaries = [
        {
            'paper_info': {
                'title': 'Test Paper 1',
                'authors': ['Author A', 'Author B'],
                'year': '2023'
            }
        }
    ]
    
    # 构建 citation_manifest_v2
    manifest = build_citation_manifest_v2_from_review_draft(
        job_id='test_job',
        project_name='test_project',
        manifest_id='test_manifest',
        review_draft_path='test_draft.json',
        review_word_path='test_review.docx',
        review_draft_v2=review_draft_v2,
        paper_summaries=paper_summaries
    )
    
    # 验证结果
    assert manifest.artifact_version == 'v2'
    assert len(manifest.occurrences) == 1
    assert len(manifest.bibliography) == 1
    
    # 验证 occurrence 包含正确的字段
    occurrence = manifest.occurrences[0]
    assert occurrence.citation_token == '[[cite:test_paper_1]]'
    assert occurrence.paper_id == 'test_paper_1'
    assert len(occurrence.spans) == 1
    
    # 验证 bibliography 只包含被引用的条目
    bibliography_entry = manifest.bibliography[0]
    assert bibliography_entry.is_cited == True
    assert 'Test Paper 1' in bibliography_entry.citation_text


def test_bibliography_cited_only():
    """测试 bibliography 只包含 cited entries"""
    # 模拟 review_draft_v2 数据（只引用了一篇论文）
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
                            'text': 'This is a test paragraph with citation.',
                            'citations': [
                                {
                                    'local_ref_id': 'cit1',
                                    'citation_token': '[[cite:test_paper_1]]',
                                    'paper_id': 'test_paper_1',
                                    'paper_key': 'test_paper_1'
                                }
                            ]
                        }
                    ]
                }
            ],
            'references': [
                'Author A, B. (2023). Test Paper 1. Journal of Testing.',
                'Author C. (2024). Test Paper 2. Journal of Testing.'  # 未被引用
            ]
        }
    }
    
    # 模拟论文摘要数据（两篇论文）
    paper_summaries = [
        {
            'paper_info': {
                'title': 'Test Paper 1',
                'authors': ['Author A', 'Author B'],
                'year': '2023'
            }
        },
        {
            'paper_info': {
                'title': 'Test Paper 2',
                'authors': ['Author C'],
                'year': '2024'
            }
        }
    ]
    
    # 构建 citation_manifest_v2
    manifest = build_citation_manifest_v2_from_review_draft(
        job_id='test_job',
        project_name='test_project',
        manifest_id='test_manifest',
        review_draft_path='test_draft.json',
        review_word_path='test_review.docx',
        review_draft_v2=review_draft_v2,
        paper_summaries=paper_summaries
    )
    
    # 验证 bibliography 只包含被引用的论文
    assert len(manifest.bibliography) == 1
    assert manifest.bibliography[0].paper_id == 'test_paper_1'
