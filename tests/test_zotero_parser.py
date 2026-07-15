#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
单元测试 - Zotero解析器
"""

import pytest
import os
import tempfile
import json
from unittest.mock import Mock, patch
from pathlib import Path


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

    def test_parse_gb18030_report_preserves_chinese(self, tmp_path: Path):
        """测试GB18030编码报告不会被错误转码成乱码"""
        zotero_content = "中文标题\n作者\t张三\n期刊\t中文期刊"
        report_path = tmp_path / "zotero_gbk.txt"
        report_path.write_bytes(zotero_content.encode("gb18030"))

        expected = [{"title": "中文标题"}]
        with patch.object(self.zotero_parser, "parse_standard_zotero_format", return_value=expected) as mock_parse:
            result = self.zotero_parser.parse_zotero_report(str(report_path))

        assert result == expected
        assert mock_parse.call_count == 1
        assert mock_parse.call_args[0][0] == zotero_content

    def test_parse_simple_key_value_format_preserves_full_author_names(self):
        """测试键值对格式不会把英文作者名按单个字母拆碎"""
        content = "\n".join(
            [
                "标题: Example Paper",
                "作者: Smith, John; Doe, Jane",
                "年份: 2024",
                "期刊: Journal of Testing",
                "DOI: 10.1234/example",
                "---",
            ]
        )

        result = self.zotero_parser.parse_simple_key_value_format(content)

        assert result
        assert result[0]["authors"] == ["Smith, John", "Doe, Jane"]
        assert "issue" not in result[0]

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


def test_parse_result_v1_joins_wrapped_standard_fields(tmp_path: Path) -> None:
    report_path = tmp_path / "wrapped-report.txt"
    report_path.write_text(
        "\n".join(
            [
                "*",
                "A Long Study of",
                "Consumer Fairness",
                "作者\tSmith, John",
                "作者\tDoe, Jane",
                "摘要\tFirst abstract line",
                "continued abstract line",
                "网址\thttps://example.com/",
                "article",
                "刊名\tJournal of Testing",
                "DOI\t10.1234/",
                "wrapped",
                "附件",
                "  o KEY/paper.pdf",
            ]
        ),
        encoding="utf-8",
    )

    parser = __import__("zotero_parser", fromlist=["parse_zotero_report_result"])
    result = parser.parse_zotero_report_result(str(report_path))

    assert result.status == "ok"
    assert result.parser_route == "standard"
    assert result.report_hash
    assert result.parser_version == "zotero-parser-v1"
    assert result.stats.wrapped_fields_joined == 3
    assert result.papers[0]["title"] == "A Long Study of Consumer Fairness"
    assert result.papers[0]["abstract"] == "First abstract line continued abstract line"
    assert result.papers[0]["url"] == "https://example.com/article"
    assert result.papers[0]["doi"] == "10.1234/wrapped"
    assert result.papers[0]["journal"] == "Journal of Testing"
    assert result.papers[0]["authors"] == ["Smith, John", "Doe, Jane"]
    abstract_source = result.records[0].field_sources["abstract"][0]
    assert abstract_source.line_end == abstract_source.line_start + 1
    serialized = result.to_dict()
    json.dumps(serialized, ensure_ascii=False)
    assert parser.ZoteroParseResultV1.from_dict(serialized).to_dict() == serialized


def test_parse_result_v1_reports_partial_without_dropping_good_entries(tmp_path: Path) -> None:
    report_path = tmp_path / "partial-report.txt"
    report_path.write_text(
        "*\nGood Paper\n作者\tAlice Smith\n*\n作者\tMissing Title",
        encoding="utf-8",
    )

    parser = __import__("zotero_parser", fromlist=["parse_zotero_report_result"])
    result = parser.parse_zotero_report_result(str(report_path))

    assert result.status == "partial"
    assert [paper["title"] for paper in result.papers] == ["Good Paper"]
    assert result.stats.detected_entries == 2
    assert result.stats.skipped_entries == 1
    assert any(item.code == "missing_title" and item.entry_index == 2 for item in result.diagnostics)


def test_parse_result_v1_failure_preserves_legacy_empty_projection(tmp_path: Path) -> None:
    missing = tmp_path / "missing.txt"
    parser = __import__("zotero_parser", fromlist=["parse_zotero_report_result"])

    result = parser.parse_zotero_report_result(str(missing))

    assert result.status == "failed"
    assert result.diagnostics[0].code == "source_missing"
    assert parser.parse_zotero_report(str(missing)) == []
