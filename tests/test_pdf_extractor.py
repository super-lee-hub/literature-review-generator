#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
单元测试 - PDF文本提取器
"""

import pytest
import os
import tempfile
from io import BytesIO
from unittest.mock import Mock, patch, MagicMock


class TestPDFExtractor:
    """PDF文本提取器测试"""

    def setup_method(self):
        """每个测试方法的初始化"""
        # 导入模块
        self.pdf_extractor = __import__('pdf_extractor', fromlist=['extract_text_from_pdf'])

    @patch('pdf_extractor.fitz')
    @patch('pdf_extractor.pdfplumber')
    @patch('os.path.getsize')
    def test_extract_with_pdfplumber(self, mock_getsize, mock_pdfplumber, mock_fitz):
        """测试使用pdfplumber提取文本"""
        # 模拟文件大小
        mock_getsize.return_value = 1024 * 1024  # 1MB

        # 模拟pdfplumber
        from unittest.mock import MagicMock
        mock_page = MagicMock()
        mock_page.extract_text.return_value = "Sample PDF text content"
        mock_pdf = MagicMock()
        mock_pdf.pages = [mock_page]
        mock_pdfplumber.open.return_value = mock_pdf

        # 使用临时文件路径
        with tempfile.NamedTemporaryFile(suffix='.pdf', delete=False) as tmp:
            tmp_path = tmp.name

        try:
            # 调用函数
            result = self.pdf_extractor.extract_text_from_pdf(tmp_path)

            # 验证结果
            assert "Sample PDF text content" in result
            mock_pdfplumber.open.assert_called_once()
        finally:
            os.unlink(tmp_path)

    @patch('pdf_extractor.pdfplumber')
    @patch('os.path.getsize')
    def test_pdfplumber_fallback_to_fitz(self, mock_getsize, mock_pdfplumber):
        """测试pdfplumber失败时回退到fitz"""
        # 模拟文件大小
        mock_getsize.return_value = 1024 * 1024  # 1MB

        from unittest.mock import MagicMock
        # 模拟pdfplumber失败
        mock_pdfplumber.open.side_effect = Exception("PDFPlumber failed")

        # 模拟fitz
        mock_page = MagicMock()
        mock_page.get_text.return_value = "Fallback text from fitz"
        mock_doc = MagicMock()
        mock_doc.load_page.return_value = mock_page
        mock_doc.page_count = 1

        # 使用临时文件路径
        with tempfile.NamedTemporaryFile(suffix='.pdf', delete=False) as tmp:
            tmp_path = tmp.name

        try:
            with patch('pdf_extractor.fitz.open', return_value=mock_doc):
                result = self.pdf_extractor.extract_text_from_pdf(tmp_path)

                assert "Fallback text from fitz" in result
        finally:
            os.unlink(tmp_path)

    @patch('pdf_extractor.fitz')
    @patch('pdf_extractor.pdfplumber')
    @patch('os.path.getsize')
    def test_both_engines_fail(self, mock_getsize, mock_pdfplumber, mock_fitz):
        """测试两个引擎都失败的情况"""
        # 模拟文件大小
        mock_getsize.return_value = 1024 * 1024  # 1MB

        # 模拟两个引擎都失败
        mock_pdfplumber.open.side_effect = Exception("PDFPlumber failed")
        mock_fitz.open.side_effect = Exception("Fitz failed")

        # 使用临时文件路径
        with tempfile.NamedTemporaryFile(suffix='.pdf', delete=False) as tmp:
            tmp_path = tmp.name

        try:
            # 调用函数应该返回None（不抛出异常）
            result = self.pdf_extractor.extract_text_from_pdf(tmp_path)
            assert result is None
        finally:
            os.unlink(tmp_path)

    @patch('os.path.getsize')
    def test_extract_empty_text(self, mock_getsize):
        """测试提取空文本的情况"""
        # 模拟文件大小
        mock_getsize.return_value = 1024 * 1024  # 1MB

        from unittest.mock import MagicMock
        # 使用一个临时PDF文件进行测试
        with tempfile.NamedTemporaryFile(suffix='.pdf', delete=False) as tmp:
            tmp_path = tmp.name

        try:
            with patch('pdf_extractor.pdfplumber') as mock_pdfplumber:
                mock_page = MagicMock()
                mock_page.extract_text.return_value = None  # 空文本
                mock_pdf = MagicMock()
                mock_pdf.pages = [mock_page]
                mock_pdfplumber.open.return_value = mock_pdf

                result = self.pdf_extractor.extract_text_from_pdf(tmp_path)

                # 应该返回None（空文本导致失败）
                assert result is None
        finally:
            os.unlink(tmp_path)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
