"""测试 docx_writer 模块"""

import os
import tempfile
from typing import Dict, List, Any

import pytest

from docx_writer import generate_apa_references_from_manifest, generate_apa_references


class MockGenerator:
    """模拟 LiteratureReviewGenerator 类"""
    def __init__(self, summaries=None):
        self.summaries = summaries or []
        self.config = {}
        
        # 模拟 logger
        class MockLogger:
            def info(self, msg):
                pass
            def warning(self, msg):
                pass
            def error(self, msg):
                pass
        
        self.logger = MockLogger()


def test_manifest_first_bibliography():
    """测试 bibliography 按 manifest-first 解析"""
    # 模拟 v2 manifest 数据
    citation_manifest = {
        'bibliography': [
            {
                'entry_id': 'bib_0',
                'paper_id': 'test_paper_1',
                'paper_key': 'test_paper_1',
                'citation_text': 'Author A, B. (2023). Test Paper 1. Journal of Testing.',
                'is_cited': True
            }
        ]
    }
    
    # 模拟生成器实例
    generator = MockGenerator()
    
    # 调用函数
    references = generate_apa_references_from_manifest(citation_manifest, generator)
    
    # 验证结果
    assert len(references) == 1
    assert 'Author A, B. (2023). Test Paper 1. Journal of Testing.' in references


def test_v2_bibliography_cited_only():
    """测试 v2 bibliography cited-only 输出"""
    # 模拟 v2 manifest 数据（包含未被引用的条目）
    citation_manifest = {
        'bibliography': [
            {
                'entry_id': 'bib_0',
                'paper_id': 'test_paper_1',
                'paper_key': 'test_paper_1',
                'citation_text': 'Author A, B. (2023). Test Paper 1. Journal of Testing.',
                'is_cited': True
            },
            {
                'entry_id': 'bib_1',
                'paper_id': 'test_paper_2',
                'paper_key': 'test_paper_2',
                'citation_text': 'Author C. (2024). Test Paper 2. Journal of Testing.',
                'is_cited': False  # 未被引用
            }
        ]
    }
    
    # 模拟生成器实例
    generator = MockGenerator()
    
    # 调用函数
    references = generate_apa_references_from_manifest(citation_manifest, generator)
    
    # 验证结果（只包含被引用的条目）
    assert len(references) == 1
    assert 'Author A, B. (2023). Test Paper 1. Journal of Testing.' in references
    assert 'Author C. (2024). Test Paper 2. Journal of Testing.' not in references


def test_manifest_not_available_fallback():
    """测试 manifest 不可用时 legacy fallback 仍可工作"""
    # 模拟 summaries 数据
    summaries = [
        {
            'status': 'success',
            'paper_info': {
                'title': 'Test Paper 1',
                'authors': ['Author A', 'Author B'],
                'year': '2023',
                'journal': 'Journal of Testing'
            }
        }
    ]
    
    # 模拟生成器实例
    generator = MockGenerator(summaries=summaries)
    
    # 调用函数（citation_manifest 为 None）
    references = generate_apa_references_from_manifest(None, generator)
    
    # 验证结果
    assert len(references) == 1
    assert 'Author A, Author B' in references[0]
    assert '2023' in references[0]
    assert 'Test Paper 1' in references[0]


def test_v1_manifest_fallback():
    """测试只有 v1 manifest 时回退到旧方法"""
    # 模拟 v1 manifest 数据
    citation_manifest = {
        'citations': [
            {
                'citation_id': 'cit1',
                'paper_id': 'test_paper_1',
                'text': '(Author A, 2023)',
                'context': 'Test context',
                'section_number': 1,
                'section_title': 'Introduction',
                'block_id': 's1_b1',
                'block_order': 1
            }
        ]
    }
    
    # 模拟 summaries 数据
    summaries = [
        {
            'status': 'success',
            'paper_info': {
                'title': 'Test Paper 1',
                'authors': ['Author A', 'Author B'],
                'year': '2023',
                'journal': 'Journal of Testing'
            }
        }
    ]
    
    # 模拟生成器实例
    generator = MockGenerator(summaries=summaries)
    
    # 调用函数
    references = generate_apa_references_from_manifest(citation_manifest, generator)
    
    # 验证结果（应该回退到使用 summaries 生成）
    assert len(references) == 1
    assert 'Author A, Author B' in references[0]
    assert '2023' in references[0]
    assert 'Test Paper 1' in references[0]


def test_empty_bibliography_fallback():
    """测试 v2 manifest 存在但 bibliography 为空时回退到旧方法"""
    # 模拟 v2 manifest 数据（空 bibliography）
    citation_manifest = {
        'bibliography': []
    }
    
    # 模拟 summaries 数据
    summaries = [
        {
            'status': 'success',
            'paper_info': {
                'title': 'Test Paper 1',
                'authors': ['Author A', 'Author B'],
                'year': '2023',
                'journal': 'Journal of Testing'
            }
        }
    ]
    
    # 模拟生成器实例
    generator = MockGenerator(summaries=summaries)
    
    # 调用函数
    references = generate_apa_references_from_manifest(citation_manifest, generator)
    
    # 验证结果（应该回退到使用 summaries 生成）
    assert len(references) == 1
    assert 'Author A, Author B' in references[0]
    assert '2023' in references[0]
    assert 'Test Paper 1' in references[0]
