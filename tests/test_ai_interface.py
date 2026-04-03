#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
单元测试 - AI接口
"""

import pytest
import time
import threading
from unittest.mock import Mock, patch, MagicMock
from requests.exceptions import RequestException, Timeout, ConnectionError  # type: ignore
from ai_interface import _normalize_type_specific_details


def _runtime_config(retries: str = "3", timeout: str = "600"):
    return {
        "Performance": {
            "api_retry_attempts": retries,
        },
        "API_Parameters": {
            "timeout_seconds": timeout,
        },
    }


class TestRateLimiter:
    """RateLimiter类测试"""

    def test_rate_limiter_initialization(self):
        """测试RateLimiter初始化"""
        # 导入RateLimiter
        ai_interface = __import__('ai_interface', fromlist=['RateLimiter'])
        RateLimiter = ai_interface.RateLimiter

        # 创建实例
        limiter = RateLimiter(1000, 100, 2000, 200)

        # 验证属性
        assert limiter.primary_tpm_capacity == 1000
        assert limiter.primary_rpm_capacity == 100
        assert limiter.backup_tpm_capacity == 2000
        assert limiter.backup_rpm_capacity == 200

    def test_consume_primary_tokens(self):
        """测试消耗主要令牌"""
        ai_interface = __import__('ai_interface', fromlist=['RateLimiter'])
        RateLimiter = ai_interface.RateLimiter

        limiter = RateLimiter(1000, 100, 2000, 200)

        # 消耗令牌 (tokens_needed, requests_needed, engine_type)
        limiter.consume(100, 1, 'primary')

        # 验证状态
        status = limiter.get_status('primary')
        assert 'tpm_tokens' in status
        assert 'tpm_capacity' in status

    def test_rate_limiter_thread_safety(self):
        """测试RateLimiter线程安全性"""
        ai_interface = __import__('ai_interface', fromlist=['RateLimiter'])
        RateLimiter = ai_interface.RateLimiter

        limiter = RateLimiter(10000, 1000, 20000, 2000)
        results = []

        def consume_tokens():
            for _ in range(10):
                limiter.consume(10, 1, 'primary')
                results.append(True)

        # 创建多个线程
        threads = [threading.Thread(target=consume_tokens) for _ in range(5)]

        # 启动所有线程
        for thread in threads:
            thread.start()

        # 等待所有线程完成
        for thread in threads:
            thread.join()

        # 验证所有操作都成功
        assert len(results) == 50


class TestAIIinterface:
    """AI接口测试"""

    def setup_method(self):
        """初始化"""
        self.ai_interface = __import__('ai_interface', fromlist=[
            'get_summary_from_ai',
            '_call_ai_api',
            'RateLimiter'
        ])

    @patch('ai_interface.requests.post')
    def test_call_ai_api_success(self, mock_post):
        """测试API调用成功"""
        # 模拟成功的API响应
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "choices": [{
                "message": {
                    "content": '{"summary": "Test summary", "key_findings": ["Finding 1"]}'
                }
            }]
        }
        mock_post.return_value = mock_response

        # 调用函数
        with patch('ai_interface.load_config', return_value=_runtime_config()):
            result = self.ai_interface._call_ai_api(
                "test prompt",
                {"api_key": "test_key", "model": "test_model"},
                "primary"
            )

        # 验证结果
        assert result is not None
        assert "summary" in result

    @patch('ai_interface.requests.post')
    def test_call_ai_api_rate_limit(self, mock_post):
        """测试API调用遇到速率限制"""
        # 模拟429错误
        mock_response = Mock()
        mock_response.status_code = 429
        mock_response.json.return_value = {"error": "Rate limit exceeded"}
        mock_response.raise_for_status.side_effect = Exception("HTTP 429")
        mock_post.return_value = mock_response

        # 应该返回None（API调用失败）
        with patch('ai_interface.load_config', return_value=_runtime_config()), patch('ai_interface.time.sleep', return_value=None):
            result = self.ai_interface._call_ai_api(
                "test prompt",
                {"api_key": "test_key", "model": "test_model"},
                "system prompt"
            )

        assert result is None

    @patch('ai_interface.requests.post')
    def test_call_ai_api_network_error(self, mock_post):
        """测试网络错误处理"""
        # 模拟网络错误
        mock_post.side_effect = ConnectionError("Network error")

        # 应该返回None（网络错误被捕获）
        with patch('ai_interface.load_config', return_value=_runtime_config()), patch('ai_interface.time.sleep', return_value=None):
            result = self.ai_interface._call_ai_api(
                "test prompt",
                {"api_key": "test_key", "model": "test_model"},
                "system prompt"
            )

        assert result is None

    @patch('ai_interface.requests.post')
    def test_call_ai_api_timeout(self, mock_post):
        """测试超时处理"""
        # 模拟超时
        mock_post.side_effect = Timeout("Request timeout")

        # 应该返回None（超时被捕获）
        with patch('ai_interface.load_config', return_value=_runtime_config()), patch('ai_interface.time.sleep', return_value=None):
            result = self.ai_interface._call_ai_api(
                "test prompt",
                {"api_key": "test_key", "model": "test_model"},
                "system prompt"
            )

        assert result is None

    @patch('ai_interface.requests.post')
    def test_call_ai_api_invalid_json(self, mock_post):
        """测试无效JSON响应"""
        # 模拟无效JSON响应
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.side_effect = ValueError("Invalid JSON")
        mock_post.return_value = mock_response

        # 应该能够处理并返回None或抛出错误
        with patch('ai_interface.load_config', return_value=_runtime_config()):
            result = self.ai_interface._call_ai_api(
                "test prompt",
                {"api_key": "test_key", "model": "test_model"},
                "primary"
            )

        # 验证错误处理
        assert result is None or "error" in str(result).lower()

    @patch('ai_interface.requests.post')
    def test_call_ai_api_uses_configured_retry_attempts(self, mock_post):
        """Uses configured retry attempts."""
        mock_post.side_effect = ConnectionError("Network error")

        with patch('ai_interface.load_config', return_value=_runtime_config(retries="4")), patch('ai_interface.time.sleep', return_value=None):
            result = self.ai_interface._call_ai_api(
                "test prompt",
                {"api_key": "test_key", "model": "test_model"},
                "system prompt"
            )

        assert result is None
        assert mock_post.call_count == 4

    @patch('ai_interface.requests.post')
    def test_call_ai_api_invalid_retry_config_falls_back_to_default(self, mock_post):
        """Falls back to default retries for invalid config."""
        mock_post.side_effect = ConnectionError("Network error")

        with patch('ai_interface.load_config', return_value=_runtime_config(retries="invalid")), patch('ai_interface.time.sleep', return_value=None):
            result = self.ai_interface._call_ai_api(
                "test prompt",
                {"api_key": "test_key", "model": "test_model"},
                "system prompt"
            )

        assert result is None
        assert mock_post.call_count == 3


if __name__ == "__main__":
    pytest.main([__file__, "-v"])


def test_normalize_type_specific_details_projects_review_route_fields() -> None:
    normalized = _normalize_type_specific_details(
        {
            "paper_type": "systematic review",
            "review_details": {
                "review_type": "systematic review",
                "search_databases": ["Scopus", "Web of Science"],
                "main_themes": ["trust", "adoption"],
            },
            "future_research_directions": ["test more longitudinal designs"],
        }
    )

    assert normalized["paper_type"] == "review"
    assert normalized["paper_subtype"] == "systematic review"
    assert normalized["review_details"]["review_type"] == "systematic review"
    assert normalized["future_research_directions"] == ["test more longitudinal designs"]


def test_normalize_type_specific_details_infers_empirical_route_from_branch_content() -> None:
    normalized = _normalize_type_specific_details(
        {
            "paper_type": "",
            "route_confidence": "",
            "classification_rationale": "",
            "empirical_details": {
                "data_source_and_size": "survey, n=420",
                "analysis_technique": "SEM",
                "core_variables": {
                    "independent": ["trust"],
                    "dependent": ["adoption"],
                },
            },
        }
    )

    assert normalized["paper_type"] == "empirical"
    assert normalized["paper_subtype"] == ""
    assert normalized["route_confidence"] == "low"
    assert normalized["data_source_and_size"] == "survey, n=420"
    assert normalized["analysis_technique"] == "SEM"


def test_normalize_type_specific_details_marks_uncertain_when_no_route_evidence() -> None:
    normalized = _normalize_type_specific_details({"paper_type": "", "paper_subtype": ""})

    assert normalized["paper_type"] == "uncertain"
    assert normalized["paper_subtype"] == ""
    assert normalized["classification_rationale"] == "insufficient evidence to assign a stable primary type"
