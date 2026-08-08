#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pytest配置文件
定义测试套件的共享fixtures和配置
"""

import tempfile
import os
import sys
from pathlib import Path

import pytest

_TESTS_DIR = Path(__file__).resolve().parent
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

from offline_guard import (  # noqa: E402
    configure_offline_environment,
    install_offline_guard,
    live_api_skip_reason,
)


configure_offline_environment()
install_offline_guard()


_OPTIONAL_MARKERS = {"live_api", "playwright", "heavy_ocr", "optional"}
_OPTIONAL_NODEIDS: set[str] = set()
_UNEXPECTED_SKIPS: list[str] = []


@pytest.fixture
def temp_dir():
    """提供临时目录fixture"""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield tmpdir


@pytest.fixture
def temp_file():
    """提供临时文件fixture"""
    fd, path = tempfile.mkstemp()
    try:
        os.close(fd)
        yield path
    finally:
        if os.path.exists(path):
            os.unlink(path)


@pytest.fixture
def sample_pdf_content():
    """提供示例PDF内容"""
    return """
    Title: Sample Research Paper
    Abstract: This is a sample abstract for testing purposes.
    Introduction: This is the introduction section.
    Methodology: This describes the research methodology.
    Results: These are the research results.
    Conclusion: This is the conclusion.
    """


@pytest.fixture
def sample_zotero_content():
    """提供示例Zotero报告内容"""
    return """
Item Type: Journal Article
Author: Smith, John
Author: Doe, Jane
Title: A Study on Machine Learning
Publication: Journal of AI
Year: 2023
Volume: 10
Issue: 2
Pages: 123-145
DOI: 10.1234/example.2023.001
URL: http://example.com/paper
Abstract: This paper explores machine learning techniques.
    """


@pytest.fixture
def sample_outline():
    """提供示例大纲结构"""
    return {
        "title": "Literature Review on AI",
        "sections": [
            {
                "heading": "Introduction",
                "content": "Introduction content here"
            },
            {
                "heading": "Literature Review",
                "content": "Literature review content"
            },
            {
                "heading": "Methodology",
                "content": "Methodology content"
            },
            {
                "heading": "Results",
                "content": "Results content"
            },
            {
                "heading": "Conclusion",
                "content": "Conclusion content"
            }
        ]
    }


@pytest.fixture
def sample_summaries():
    """提供示例摘要数据"""
    return [
        {
            "title": "Paper 1: Introduction to AI",
            "authors": ["Smith, J."],
            "year": 2020,
            "summary": "This paper introduces fundamental AI concepts.",
            "key_findings": [
                "AI is rapidly evolving",
                "Machine learning is a key component"
            ],
            "methodology": "Literature review and analysis",
            "results": "AI adoption is increasing",
            "conclusions": "AI will transform many industries",
            "limitations": "Limited to certain domains"
        },
        {
            "title": "Paper 2: Deep Learning Advances",
            "authors": ["Doe, J."],
            "year": 2021,
            "summary": "This paper discusses deep learning progress.",
            "key_findings": [
                "Deep learning models are improving",
                "Neural networks are becoming more complex"
            ],
            "methodology": "Experimental study",
            "results": "Significant accuracy improvements observed",
            "conclusions": "Deep learning shows great promise",
            "limitations": "Requires large datasets"
        }
    ]


@pytest.fixture
def sample_config():
    """提供示例配置"""
    return {
        "Primary_Reader_API": {
            "api_key": "test_key",
            "model": "test_model",
            "api_base": "https://api.example.com/v1"
        },
        "Writer_API": {
            "api_key": "test_key",
            "model": "test_model",
            "api_base": "https://api.example.com/v1"
        },
        "Performance": {
            "max_workers": 2,
            "api_retry_attempts": 3,
        },
        "Styling": {
            "font_name": "Times New Roman",
            "font_size_body": 12,
            "font_size_heading1": 16,
            "font_size_heading2": 14
        }
    }


# pytest配置
def pytest_configure(config):
    """配置pytest"""
    config.addinivalue_line(
        "markers", "slow: marks tests as slow (deselect with '-m \"not slow\"')"
    )
    config.addinivalue_line(
        "markers", "integration: marks tests as integration tests"
    )
    config.addinivalue_line(
        "markers", "unit: marks tests as unit tests"
    )
    config.addinivalue_line(
        "markers", "live_api: requires explicit external API opt-in and credentials"
    )
    config.addinivalue_line(
        "markers", "playwright: optional browser-based GUI tests"
    )
    config.addinivalue_line(
        "markers", "heavy_ocr: optional heavyweight OCR tests"
    )
    config.addinivalue_line(
        "markers", "optional: test may skip for a declared optional capability"
    )


def pytest_collection_modifyitems(config, items):
    """修改测试收集"""
    # 为测试添加标记
    for item in items:
        # 如果文件名包含"test_"前缀，标记为单元测试
        if "integration" in item.nodeid:
            item.add_marker(pytest.mark.integration)
        else:
            item.add_marker(pytest.mark.unit)

        marker_names = {marker.name for marker in item.iter_markers()}
        if marker_names & _OPTIONAL_MARKERS:
            _OPTIONAL_NODEIDS.add(item.nodeid)

        live_api_reason = live_api_skip_reason(marker_names, os.environ)
        if live_api_reason is not None:
            item.add_marker(pytest.mark.skip(reason=live_api_reason))
        if "playwright" in marker_names and os.environ.get("AUTO_GENERATE_RUN_PLAYWRIGHT") != "1":
            item.add_marker(pytest.mark.skip(reason="Playwright test not explicitly enabled"))
        if "heavy_ocr" in marker_names and os.environ.get("AUTO_GENERATE_RUN_HEAVY_OCR") != "1":
            item.add_marker(pytest.mark.skip(reason="heavy OCR test not explicitly enabled"))


def pytest_runtest_logreport(report):
    if report.skipped and report.nodeid not in _OPTIONAL_NODEIDS:
        _UNEXPECTED_SKIPS.append(report.nodeid)


def pytest_terminal_summary(terminalreporter):
    if _UNEXPECTED_SKIPS:
        terminalreporter.section("unexpected required-test skips", red=True, bold=True)
        for nodeid in sorted(set(_UNEXPECTED_SKIPS)):
            terminalreporter.write_line(nodeid)


def pytest_sessionfinish(session, exitstatus):
    if (
        _UNEXPECTED_SKIPS
        and os.environ.get("AUTO_GENERATE_FAIL_ON_UNEXPECTED_SKIP", "1") == "1"
    ):
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
