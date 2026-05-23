"""Tests for DOCX bibliography rendering behavior."""


import pytest

from docx_writer import generate_apa_references_from_manifest, rebuild_review_docx_from_structured_artifacts


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
            def success(self, msg):
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
    references = generate_apa_references_from_manifest(citation_manifest, generator, allow_compat_fallback=True)
    
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
    references = generate_apa_references_from_manifest(citation_manifest, generator, allow_compat_fallback=True)
    
    # 验证结果（只包含被引用的条目）
    assert len(references) == 1
    assert 'Author A, B. (2023). Test Paper 1. Journal of Testing.' in references
    assert 'Author C. (2024). Test Paper 2. Journal of Testing.' not in references


def test_rendered_docx_uses_manifest_ref_id_and_exact_bibliography(tmp_path):
    generator = MockGenerator()
    review_draft = {
        "content": {
            "sections": [
                {
                    "section_number": 1,
                    "section_title": "Intro",
                    "blocks": [{"text": "Structured claim [[cite_ref:R001]]. Legacy mention [[cite:paper_2]]."}],
                }
            ]
        }
    }
    manifest = {
        "paper_entries": [
            {
                "paper_id": "paper_1",
                "paper_key": "paper_1",
                "title": "Structured Paper",
                "authors": ["Alice Smith"],
                "year": "2024",
            },
            {
                "paper_id": "paper_2",
                "paper_key": "paper_2",
                "title": "Uncited Paper",
                "authors": ["Bob Jones"],
                "year": "2025",
            },
        ],
        "occurrences": [{"ref_id": "R001", "paper_id": "paper_1", "paper_key": "paper_1"}],
        "bibliography": [
            {
                "entry_id": "bib_001",
                "paper_id": "paper_1",
                "paper_key": "paper_1",
                "citation_text": "Alice Smith (2024). Structured Paper.",
                "is_cited": True,
            }
        ],
    }

    output = tmp_path / "review.docx"

    with pytest.raises(ValueError):
        rebuild_review_docx_from_structured_artifacts(
            generator,
            review_draft,
            manifest,
            str(output),
        )

    review_draft["content"]["sections"][0]["blocks"][0]["text"] = "Structured claim [[cite_ref:R001]]."
    rebuild_review_docx_from_structured_artifacts(
        generator,
        review_draft,
        manifest,
        str(output),
    )

    from docx import Document

    text = "\n".join(paragraph.text for paragraph in Document(str(output)).paragraphs)
    assert "(Smith, 2024)" in text
    assert "Alice Smith (2024). Structured Paper." in text
    assert "Uncited Paper" not in text


def test_rendered_docx_resolves_combined_cite_ref_token(tmp_path):
    generator = MockGenerator()
    review_draft = {
        "content": {
            "sections": [
                {
                    "section_number": 1,
                    "section_title": "Intro",
                    "blocks": [{"text": "Combined support [[cite_ref:R001, R002]]."}],
                }
            ]
        }
    }
    manifest = {
        "paper_entries": [
            {
                "paper_id": "paper_1",
                "paper_key": "paper_1",
                "title": "Structured Paper One",
                "authors": ["Alice Smith"],
                "year": "2024",
            },
            {
                "paper_id": "paper_2",
                "paper_key": "paper_2",
                "title": "Structured Paper Two",
                "authors": ["Bob Jones"],
                "year": "2025",
            },
        ],
        "occurrences": [
            {"ref_id": "R001", "paper_id": "paper_1", "paper_key": "paper_1"},
            {"ref_id": "R002", "paper_id": "paper_2", "paper_key": "paper_2"},
        ],
        "bibliography": [
            {
                "entry_id": "bib_001",
                "paper_id": "paper_1",
                "paper_key": "paper_1",
                "citation_text": "Alice Smith (2024). Structured Paper One.",
                "is_cited": True,
            },
            {
                "entry_id": "bib_002",
                "paper_id": "paper_2",
                "paper_key": "paper_2",
                "citation_text": "Bob Jones (2025). Structured Paper Two.",
                "is_cited": True,
            },
        ],
    }

    output = tmp_path / "review.docx"
    rebuild_review_docx_from_structured_artifacts(
        generator,
        review_draft,
        manifest,
        str(output),
    )

    from docx import Document

    text = "\n".join(paragraph.text for paragraph in Document(str(output)).paragraphs)
    assert "[[cite_ref:" not in text
    assert "(Smith, 2024; Jones, 2025)" in text


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
    references = generate_apa_references_from_manifest(None, generator, allow_compat_fallback=True)
    
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
    references = generate_apa_references_from_manifest(citation_manifest, generator, allow_compat_fallback=True)
    
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
    references = generate_apa_references_from_manifest(citation_manifest, generator, allow_compat_fallback=True)
    
    # 验证结果（应该回退到使用 summaries 生成）
    assert len(references) == 1
    assert 'Author A, Author B' in references[0]
    assert '2023' in references[0]
    assert 'Test Paper 1' in references[0]


def test_manifest_not_available_raises_without_compat_flag():
    generator = MockGenerator()
    with pytest.raises(ValueError):
        generate_apa_references_from_manifest(None, generator)


def test_rebuild_review_docx_raises_when_section_append_fails(tmp_path, monkeypatch):
    generator = MockGenerator()
    review_draft = {
        "content": {
            "sections": [
                {
                    "section_number": 1,
                    "section_title": "Intro",
                    "blocks": [{"text": "Claim [[cite:missing]]."}],
                }
            ]
        }
    }
    manifest = {"paper_entries": [], "bibliography": []}

    monkeypatch.setattr("docx_writer._initialize_review_document", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("docx_writer.append_section_to_word_document", lambda *_args, **_kwargs: False)

    with pytest.raises(ValueError, match="section append failed"):
        rebuild_review_docx_from_structured_artifacts(
            generator,
            review_draft,
            manifest,
            str(tmp_path / "review.docx"),
        )
