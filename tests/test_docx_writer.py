#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
单元测试 - Word文档生成器
"""

import pytest
import os
import tempfile
from unittest.mock import Mock, patch, MagicMock
from docx import Document  # type: ignore


class TestDocxWriter:
    """Word文档生成器测试"""

    def setup_method(self):
        """初始化"""
        self.docx_writer = __import__('docx_writer', fromlist=['create_word_document'])

    @patch('docx_writer.Document')
    def test_create_word_document_basic(self, mock_document_class):
        """测试创建基本Word文档"""
        from unittest.mock import MagicMock
        # 模拟Document类
        mock_doc = MagicMock()
        mock_paragraph = MagicMock()
        mock_doc.add_heading.return_value = mock_paragraph
        mock_doc.add_paragraph.return_value = mock_paragraph
        mock_document_class.return_value = mock_doc

        # 测试数据
        outline = {
            "title": "Test Literature Review",
            "sections": [
                {"heading": "Introduction", "content": "Test introduction"},
                {"heading": "Methodology", "content": "Test methodology"}
            ]
        }

        summaries = [
            {"title": "Paper 1", "summary": "Summary of paper 1"}
        ]

        # 创建临时输出目录
        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = os.path.join(tmpdir, "test_output")

            # 调用函数
            result = self.docx_writer.create_word_document(
                outline, summaries, output_path, {}
            )

            # 验证结果
            assert result is not None
            assert os.path.exists(result)

            # 验证Document被正确调用
            assert mock_document_class.called

    @patch('docx_writer.Document')
    def test_create_document_with_styling(self, mock_document_class):
        """测试带样式的文档创建"""
        from unittest.mock import MagicMock
        mock_doc = MagicMock()
        mock_paragraph = MagicMock()
        mock_run = MagicMock()
        mock_paragraph.add_run.return_value = mock_run
        mock_doc.add_heading.return_value = mock_paragraph
        mock_doc.add_paragraph.return_value = mock_paragraph
        mock_document_class.return_value = mock_doc

        outline = {
            "title": "Styled Document",
            "sections": []
        }

        summaries = []

        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = os.path.join(tmpdir, "styled_output")

            # 配置样式
            config = {
                "font_name": "Times New Roman",
                "font_size_body": 12,
                "font_size_heading1": 16,
                "font_size_heading2": 14
            }

            result = self.docx_writer.create_word_document(
                outline, summaries, output_path, config
            )

            assert result is not None

    def test_word_document_structure(self):
        """测试Word文档结构"""
        from unittest.mock import MagicMock
        # 使用真实的Document类来验证结构（但不保存文件）
        with patch('docx_writer.Document') as mock_document_class:
            mock_doc = MagicMock(spec=Document)
            mock_paragraph = MagicMock()
            mock_doc.add_heading = MagicMock(return_value=mock_paragraph)
            mock_doc.add_paragraph = MagicMock(return_value=mock_paragraph)
            mock_document_class.return_value = mock_doc

            outline = {
                "title": "Test",
                "sections": [
                    {"heading": "Section 1", "content": "Content 1"},
                    {"heading": "Section 2", "content": "Content 2"}
                ]
            }

            summaries = [
                {"title": "Paper 1", "key_findings": ["Finding 1"], "summary": "Summary 1"}
            ]

            with tempfile.TemporaryDirectory() as tmpdir:
                output_path = os.path.join(tmpdir, "test")

                result = self.docx_writer.create_word_document(
                    outline, summaries, output_path, {}
                )

                # 验证文档结构
                assert mock_doc.add_heading.called
                assert mock_doc.add_paragraph.called

    @patch('docx_writer.Document')
    def test_handle_empty_outline(self, mock_document_class):
        """测试处理空大纲"""
        from unittest.mock import MagicMock
        mock_doc = MagicMock()
        mock_document_class.return_value = mock_doc

        outline = {
            "title": "Empty Document",
            "sections": []
        }

        summaries = []

        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = os.path.join(tmpdir, "empty_output")

            result = self.docx_writer.create_word_document(
                outline, summaries, output_path, {}
            )

            assert result is not None

    @patch('docx_writer.Document')
    @patch('docx_writer.os.makedirs')
    def test_create_output_directory(self, mock_makedirs, mock_document_class):
        """测试创建输出目录"""
        from unittest.mock import MagicMock
        mock_doc = MagicMock()
        mock_document_class.return_value = mock_doc

        outline = {"title": "Test", "sections": []}
        summaries = []

        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = os.path.join(tmpdir, "new_directory", "output")

            # 确保目录不存在
            assert not os.path.exists(output_path)

            result = self.docx_writer.create_word_document(
                outline, summaries, output_path, {}
            )

            # 验证目录被创建
            mock_makedirs.assert_called_once()

            assert result is not None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
