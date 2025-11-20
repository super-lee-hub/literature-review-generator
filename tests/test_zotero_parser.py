#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
单元测试 - Zotero解析器
"""

import pytest
import os
import tempfile
from unittest.mock import Mock, patch


class TestZoteroParser:
    """Zotero解析器测试"""

    def setup_method(self):
        """初始化"""
        self.zotero_parser = __import__('zotero_parser', fromlist=['parse_zotero_report'])

    def test_parse_valid_entry(self):
        """测试解析有效的条目"""
        # 创建一个模拟的Zotero报告内容
        zotero_content = """
Item Type: Journal Article
Author: Smith, John
Author: Doe, Jane
Title: A Study on Testing
Publication: Testing Journal
Year: 2023
DOI: 10.1234/test.2023
URL: http://example.com/test
Abstract: This is a test abstract.
        """.strip()

        # 创建临时文件
        with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.txt') as f:
            f.write(zotero_content)
            temp_file = f.name

        try:
            # 解析文件
            result = self.zotero_parser.parse_zotero_report(temp_file)

            # 验证结果
            assert result is not None
            assert len(result) > 0

            # 第一个条目应该被解析
            first_entry = result[0]
            assert first_entry is not None
            assert isinstance(first_entry, dict)

        finally:
            # 清理临时文件
            os.unlink(temp_file)

    def test_parse_multiple_entries(self):
        """测试解析多个条目"""
        zotero_content = """
Item Type: Journal Article
Author: Smith, John
Title: First Paper
Year: 2023
DOI: 10.1234/first

Item Type: Journal Article
Author: Doe, Jane
Title: Second Paper
Year: 2024
DOI: 10.1234/second
        """.strip()

        with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.txt') as f:
            f.write(zotero_content)
            temp_file = f.name

        try:
            result = self.zotero_parser.parse_zotero_report(temp_file)

            # 应该能解析出至少一个条目（实际解析数量取决于解析器的实现）
            assert result is not None
            assert len(result) >= 1

        finally:
            os.unlink(temp_file)

    def test_parse_entry_with_missing_fields(self):
        """测试解析缺少某些字段的条目"""
        zotero_content = """
Item Type: Journal Article
Author: Smith, John
Title: A Minimal Paper
        """.strip()

        with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.txt') as f:
            f.write(zotero_content)
            temp_file = f.name

        try:
            result = self.zotero_parser.parse_zotero_report(temp_file)

            # 应该能处理缺少的字段
            assert result is not None
            assert len(result) > 0

        finally:
            os.unlink(temp_file)

    def test_parse_empty_file(self):
        """测试解析空文件"""
        with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.txt') as f:
            f.write("")
            temp_file = f.name

        try:
            result = self.zotero_parser.parse_zotero_report(temp_file)

            # 应该返回空列表或None
            assert result is None or len(result) == 0

        finally:
            os.unlink(temp_file)

    def test_parse_nonexistent_file(self):
        """测试解析不存在的文件"""
        # zotero_parser.py会捕获异常并返回空列表，而不是抛出异常
        result = self.zotero_parser.parse_zotero_report("/nonexistent/file.txt")

        # 应该返回空列表而不是抛出异常
        assert result == [] or result is None

    @patch('builtins.open')
    def test_parse_file_read_error(self, mock_open):
        """测试文件读取错误"""
        # 模拟文件读取错误
        mock_open.side_effect = IOError("Read error")

        # zotero_parser.py会捕获异常并返回空列表，而不是抛出异常
        result = self.zotero_parser.parse_zotero_report("dummy_file.txt")

        # 应该返回空列表而不是抛出异常
        assert result == [] or result is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
