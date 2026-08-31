#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
单元测试 - AI接口
"""

import pytest
from unittest.mock import Mock, patch
from requests.exceptions import Timeout, ConnectionError, HTTPError  # type: ignore
import ai_interface
from runtime.provider_runtime import ProviderRuntime


def _runtime_config(retries: str = "3", timeout: str = "600"):
    return {
        "Runtime": {
            "transport_retries": retries,
            "retry_base_delay_seconds": "0",
            "retry_max_delay_seconds": "0",
        },
        "Primary_Reader_API": {
            "read_timeout_seconds": timeout,
            "transport_retries": retries,
        },
    }


class TestAIIinterface:
    """AI接口测试"""

    def setup_method(self):
        """初始化"""
        self.ai_interface = __import__('ai_interface', fromlist=[
            'get_summary_from_ai',
            '_call_ai_api',
        ])
        self._original_detailed_call = self.ai_interface._call_ai_api_detailed

        def bound_test_runtime(*args, **kwargs):
            if kwargs.get("provider_runtime") is None:
                kwargs["provider_runtime"] = ProviderRuntime(test_only=True)
            return self._original_detailed_call(*args, **kwargs)

        self.ai_interface._call_ai_api_detailed = bound_test_runtime

    def teardown_method(self):
        self.ai_interface._call_ai_api_detailed = self._original_detailed_call

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
    def test_call_ai_api_quota_error_is_not_retried(self, mock_post):
        """Quota and auth style HTTP errors should fail fast."""
        mock_response = Mock()
        mock_response.status_code = 403
        mock_response.json.return_value = {
            "error": {
                "message": "Your account balance is insufficient.",
                "code": "insufficient_user_quota",
            }
        }
        mock_response.raise_for_status.side_effect = HTTPError("HTTP 403")
        mock_post.return_value = mock_response

        with patch('ai_interface.load_config', return_value=_runtime_config(retries="4")), patch('ai_interface.time.sleep', return_value=None) as mock_sleep:
            result = self.ai_interface._call_ai_api(
                "test prompt",
                {"api_key": "test_key", "model": "test_model"},
                "system prompt"
            )

        assert result is None
        assert mock_post.call_count == 1
        mock_sleep.assert_not_called()
        with patch('ai_interface.load_config', return_value=_runtime_config(retries="4")), patch('ai_interface.time.sleep', return_value=None):
            detailed = self.ai_interface._call_ai_api_detailed(
                "test prompt",
                {"api_key": "test_key", "model": "test_model"},
                "system prompt"
            )
        assert detailed["error_kind"] == "quota_exhausted"

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
    def test_call_ai_api_detailed_classifies_transient_network(self, mock_post):
        mock_post.side_effect = Timeout("Proxy disconnected with SSL EOF")

        with patch('ai_interface.load_config', return_value=_runtime_config(retries="1")):
            result = self.ai_interface._call_ai_api_detailed(
                "test prompt",
                {"api_key": "test_key", "model": "test_model"},
                "system prompt"
            )

        assert result["status"] == "failed"
        assert result["error_kind"] == "transient_network"

    @patch('ai_interface.requests.post')
    def test_call_ai_api_detailed_classifies_retryable_http(self, mock_post):
        mock_response = Mock()
        mock_response.status_code = 503
        mock_response.json.return_value = {"error": {"message": "temporarily unavailable"}}
        mock_response.raise_for_status.side_effect = HTTPError("HTTP 503")
        mock_post.return_value = mock_response

        with patch('ai_interface.load_config', return_value=_runtime_config(retries="1")):
            result = self.ai_interface._call_ai_api_detailed(
                "test prompt",
                {"api_key": "test_key", "model": "test_model"},
                "system prompt"
            )

        assert result["status"] == "failed"
        assert result["error_kind"] == "retryable_http"
        assert result["http_status"] == 503

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
                {"api_key": "test_key", "model": "test_model", "transport_retries": "4"},
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
                {"api_key": "test_key", "model": "test_model", "transport_retries": "invalid"},
                "system prompt"
            )

        assert result is None
        assert mock_post.call_count == 3

    @patch('ai_interface.requests.post')
    def test_call_ai_api_retries_without_deprecated_temperature(self, mock_post):
        """Some providers reject temperature for selected models; retry once without it."""
        error_response = Mock()
        error_response.status_code = 400
        error_response.json.return_value = {
            "error": {
                "message": "`temperature` is deprecated for this model.",
            }
        }
        error_response.raise_for_status.side_effect = HTTPError("HTTP 400")

        success_response = Mock()
        success_response.status_code = 200
        success_response.raise_for_status.return_value = None
        success_response.json.return_value = {
            "choices": [{"message": {"content": '{"summary": "ok"}'}}]
        }
        mock_post.side_effect = [error_response, success_response]

        with patch('ai_interface.load_config', return_value=_runtime_config(retries="1")):
            result = self.ai_interface._call_ai_api(
                "test prompt",
                {
                    "api_key": "test_key",
                    "model": "generic-model",
                    "api_base": "https://example.com/v1",
                    "provider_family": "generic",
                },
                "system prompt",
            )

        assert result == {"summary": "ok"}
        assert mock_post.call_count == 2
        assert "temperature" in mock_post.call_args_list[0].kwargs["json"]
        assert "temperature" not in mock_post.call_args_list[1].kwargs["json"]

    @patch('ai_interface.requests.post')
    def test_claude_reasoning_omits_temperature_before_provider_retry(self, mock_post):
        success_response = Mock()
        success_response.status_code = 200
        success_response.raise_for_status.return_value = None
        success_response.json.return_value = {
            "choices": [{"message": {"content": '{"summary": "ok"}'}}]
        }
        mock_post.return_value = success_response

        with patch('ai_interface.load_config', return_value=_runtime_config(retries="1")):
            result = self.ai_interface._call_ai_api(
                "test prompt",
                {
                    "api_key": "test_key",
                    "model": "claude-opus-4-7",
                    "api_base": "https://aihubmix.com/v1",
                    "provider_family": "aihubmix_claude",
                    "reasoning_effort": "max",
                    "reasoning_display": "summarized",
                },
                "system prompt",
            )

        assert result == {"summary": "ok"}
        payload = mock_post.call_args.kwargs["json"]
        assert payload["reasoning"] == {"effort": "max", "display": "summarized"}
        assert "temperature" not in payload

    @patch('ai_interface.requests.post')
    def test_call_ai_api_passes_reasoning_payload_params(self, mock_post):
        success_response = Mock()
        success_response.status_code = 200
        success_response.raise_for_status.return_value = None
        success_response.json.return_value = {
            "choices": [{"message": {"content": '{"summary": "ok"}'}}]
        }
        mock_post.return_value = success_response

        with patch('ai_interface.load_config', return_value=_runtime_config(retries="1")):
            result = self.ai_interface._call_ai_api(
                "test prompt",
                {
                    "api_key": "test_key",
                    "model": "deepseek-v4-pro",
                    "api_base": "https://api.deepseek.com",
                    "thinking": "enabled",
                    "reasoning_effort": "max",
                },
                "system prompt",
            )

        payload = mock_post.call_args.kwargs["json"]
        assert result == {"summary": "ok"}
        assert payload["thinking"] == {"type": "enabled"}
        assert payload["reasoning_effort"] == "max"

    @patch('ai_interface.requests.post')
    def test_gpt_writer_uses_responses_reasoning_payload(self, mock_post):
        success_response = Mock()
        success_response.status_code = 200
        success_response.raise_for_status.return_value = None
        success_response.json.return_value = {"output_text": "plain review text", "status": "completed"}
        mock_post.return_value = success_response

        with patch('ai_interface.load_config', return_value=_runtime_config(retries="1")):
            result = self.ai_interface._call_ai_api(
                "test prompt",
                {
                    "api_key": "test_key",
                    "model": "gpt-5.5",
                    "api_base": "https://aihubmix.com/v1",
                    "endpoint_type": "responses",
                    "provider_family": "aihubmix_openai",
                    "reasoning_effort": "high",
                    "text_verbosity": "high",
                    "max_output_tokens": "32000",
                    "omit_temperature_when_reasoning": "true",
                },
                "system prompt",
                response_format="text",
            )

        assert result == "plain review text"
        assert mock_post.call_args.args[0] == "https://aihubmix.com/v1/responses"
        payload = mock_post.call_args.kwargs["json"]
        assert payload["reasoning"] == {"effort": "high"}
        assert payload["text"] == {"verbosity": "high"}
        assert payload["max_output_tokens"] == 32000
        assert "temperature" not in payload
        assert "top_p" not in payload

    @patch('ai_interface.requests.post')
    def test_responses_json_output_uses_existing_json_parser(self, mock_post):
        success_response = Mock()
        success_response.status_code = 200
        success_response.raise_for_status.return_value = None
        success_response.json.return_value = {
            "output": [
                {
                    "type": "message",
                    "content": [
                        {"type": "output_text", "text": '{"summary": "ok"}'}
                    ],
                }
            ],
            "status": "completed",
        }
        mock_post.return_value = success_response

        with patch('ai_interface.load_config', return_value=_runtime_config(retries="1")):
            result = self.ai_interface._call_ai_api(
                "test prompt",
                {
                    "api_key": "test_key",
                    "model": "gpt-5.5",
                    "api_base": "https://aihubmix.com/v1",
                    "endpoint_type": "responses",
                    "provider_family": "aihubmix_openai",
                    "reasoning_effort": "high",
                    "text_verbosity": "high",
                    "omit_temperature_when_reasoning": "true",
                },
                "system prompt",
            )

        assert result == {"summary": "ok"}
        payload = mock_post.call_args.kwargs["json"]
        assert payload["text"]["format"] == {"type": "json_object"}
        assert payload["text"]["verbosity"] == "high"

    @patch('ai_interface.requests.post')
    def test_json_auto_correction_failure_is_not_treated_as_success(self, mock_post):
        success_response = Mock()
        success_response.status_code = 200
        success_response.raise_for_status.return_value = None
        success_response.json.return_value = {
            "choices": [{"message": {"content": "not valid json"}, "finish_reason": "stop"}]
        }
        mock_post.return_value = success_response

        with patch('ai_interface.load_config', return_value=_runtime_config(retries="1")):
            result = self.ai_interface._call_ai_api_detailed(
                "test prompt",
                {
                    "api_key": "test_key",
                    "model": "gpt-4o",
                    "api_base": "https://api.openai.com/v1",
                },
                "system prompt",
            )

        assert result["status"] == "failed"
        assert result["error_kind"] == "invalid_response"
        assert result["content"] is None

    @patch('ai_interface.requests.post')
    def test_claude_outline_sends_top_level_reasoning_and_retries_without_display(self, mock_post):
        error_response = Mock()
        error_response.status_code = 400
        error_response.json.return_value = {"error": {"message": "unsupported parameter: display"}}
        error_response.raise_for_status.side_effect = HTTPError("HTTP 400")

        success_response = Mock()
        success_response.status_code = 200
        success_response.raise_for_status.return_value = None
        success_response.json.return_value = {
            "choices": [{"message": {"content": '{"summary": "ok"}'}, "finish_reason": "stop"}]
        }
        mock_post.side_effect = [error_response, success_response]

        with patch('ai_interface.load_config', return_value=_runtime_config(retries="1")):
            result = self.ai_interface._call_ai_api(
                "test prompt",
                {
                    "api_key": "test_key",
                    "model": "claude-opus-4-7",
                    "api_base": "https://aihubmix.com/v1",
                    "provider_family": "aihubmix_claude",
                    "reasoning_effort": "xhigh",
                    "reasoning_display": "summarized",
                },
                "system prompt",
            )

        assert result == {"summary": "ok"}
        first_payload = mock_post.call_args_list[0].kwargs["json"]
        second_payload = mock_post.call_args_list[1].kwargs["json"]
        assert first_payload["reasoning"] == {"effort": "xhigh", "display": "summarized"}
        assert second_payload["reasoning"] == {"effort": "xhigh"}
        assert "extra_body" not in first_payload

    @patch('ai_interface.requests.post')
    def test_claude_opus_47_can_send_max_reasoning_display(self, mock_post):
        success_response = Mock()
        success_response.status_code = 200
        success_response.raise_for_status.return_value = None
        success_response.json.return_value = {
            "choices": [{"message": {"content": '{"summary": "ok"}'}, "finish_reason": "stop"}]
        }
        mock_post.return_value = success_response

        with patch('ai_interface.load_config', return_value=_runtime_config(retries="1")):
            result = self.ai_interface._call_ai_api(
                "test prompt",
                {
                    "api_key": "test_key",
                    "model": "claude-opus-4-7",
                    "api_base": "https://aihubmix.com/v1",
                    "provider_family": "aihubmix_claude",
                    "reasoning_effort": "max",
                    "reasoning_display": "summarized",
                    "max_tokens": "16000",
                },
                "system prompt",
            )

        assert result == {"summary": "ok"}
        payload = mock_post.call_args.kwargs["json"]
        assert payload["reasoning"] == {"effort": "max", "display": "summarized"}
        assert payload["max_tokens"] == 16000
        assert "thinking" not in payload
        assert "output_config" not in payload

    @patch('ai_interface.requests.post')
    def test_deepseek_reasoning_effort_max_falls_back_to_high(self, mock_post):
        error_response = Mock()
        error_response.status_code = 400
        error_response.json.return_value = {"error": {"message": "reasoning_effort max is not supported"}}
        error_response.raise_for_status.side_effect = HTTPError("HTTP 400")

        success_response = Mock()
        success_response.status_code = 200
        success_response.raise_for_status.return_value = None
        success_response.json.return_value = {
            "choices": [{"message": {"content": '{"summary": "ok"}'}, "finish_reason": "stop"}]
        }
        mock_post.side_effect = [error_response, success_response]

        with patch('ai_interface.load_config', return_value=_runtime_config(retries="1")):
            result = self.ai_interface._call_ai_api(
                "test prompt",
                {
                    "api_key": "test_key",
                    "model": "deepseek-v4-pro",
                    "api_base": "https://api.deepseek.com",
                    "provider_family": "deepseek",
                    "thinking": "enabled",
                    "reasoning_effort": "max",
                },
                "system prompt",
            )

        assert result == {"summary": "ok"}
        assert mock_post.call_args_list[0].kwargs["json"]["reasoning_effort"] == "max"
        assert mock_post.call_args_list[1].kwargs["json"]["reasoning_effort"] == "high"
        assert "temperature" not in mock_post.call_args_list[0].kwargs["json"]
        assert "top_p" not in mock_post.call_args_list[0].kwargs["json"]

    @patch('ai_interface.requests.post')
    def test_call_ai_api_direct_proxy_mode_bypasses_environment_proxy(self, mock_post, monkeypatch):
        """proxy_mode=direct should ignore HTTP(S)_PROXY environment settings."""
        success_response = Mock()
        success_response.status_code = 200
        success_response.raise_for_status.return_value = None
        success_response.json.return_value = {
            "choices": [{"message": {"content": '{"summary": "ok"}'}}]
        }

        class FakeSession:
            def __init__(self):
                self.trust_env = True
                self.calls = []

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def post(self, *args, **kwargs):
                self.calls.append((args, kwargs))
                return success_response

        session = FakeSession()
        monkeypatch.setattr(ai_interface.requests, "Session", lambda: session)

        with patch('ai_interface.load_config', return_value=_runtime_config(retries="1")):
            result = self.ai_interface._call_ai_api(
                "test prompt",
                {
                    "api_key": "test_key",
                    "model": "test_model",
                    "api_base": "https://api.siliconflow.cn/v1",
                    "proxy_mode": "direct",
                },
                "system prompt",
            )

        assert result == {"summary": "ok"}
        assert session.trust_env is False
        assert len(session.calls) == 1
        mock_post.assert_not_called()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])


def test_stage1_reader_scheduler_alternates_transient_failures(monkeypatch) -> None:
    calls: list[str] = []

    def fake_detailed(*_args, engine_type="primary", **_kwargs):
        calls.append(engine_type)
        if calls == ["primary", "backup", "primary"]:
            return {
                "status": "success",
                "content": {"ok": True},
                "engine_type": engine_type,
                "error_kind": None,
                "message": "",
            }
        return {
            "status": "failed",
            "content": None,
            "engine_type": engine_type,
            "error_kind": "transient_network",
            "message": "timeout",
        }

    monkeypatch.setattr(ai_interface, "_load_api_runtime_settings", lambda: (600, 2))
    monkeypatch.setattr(ai_interface, "get_summary_from_ai_detailed", fake_detailed)

    result = ai_interface.get_summary_from_ai_with_fallback(
        "prompt",
        {"api_key": "primary", "model": "m1", "api_base": "https://primary.example.com/v1"},
        {"api_key": "backup", "model": "m2", "api_base": "https://backup.example.com/v1"},
    )

    assert result == {"ok": True}
    assert calls == ["primary", "backup", "primary"]


def test_stage1_reader_scheduler_disables_quota_engine_for_round(monkeypatch) -> None:
    calls: list[str] = []
    disabled: list[str] = []

    def fake_detailed(*_args, engine_type="primary", **_kwargs):
        calls.append(engine_type)
        if engine_type == "backup":
            return {
                "status": "failed",
                "content": None,
                "engine_type": engine_type,
                "error_kind": "quota_exhausted",
                "message": "balance is insufficient",
            }
        if calls.count("primary") == 2:
            return {
                "status": "success",
                "content": {"ok": True},
                "engine_type": engine_type,
                "error_kind": None,
                "message": "",
            }
        return {
            "status": "failed",
            "content": None,
            "engine_type": engine_type,
            "error_kind": "transient_network",
            "message": "timeout",
        }

    def disable(engine, _result):
        disabled.append(engine)

    monkeypatch.setattr(ai_interface, "_load_api_runtime_settings", lambda: (600, 2))
    monkeypatch.setattr(ai_interface, "get_summary_from_ai_detailed", fake_detailed)

    result = ai_interface.get_summary_from_ai_with_fallback(
        "prompt",
        {"api_key": "primary", "model": "m1", "api_base": "https://primary.example.com/v1"},
        {"api_key": "backup", "model": "m2", "api_base": "https://backup.example.com/v1"},
        disable_engine_callback=disable,
    )

    assert result == {"ok": True}
    assert calls == ["primary", "backup", "primary"]
    assert disabled == ["backup"]


def test_json_length_response_is_incomplete_and_can_trigger_fallback() -> None:
    response = Mock()
    response.status_code = 200

    result = ai_interface._format_success_result(
        '{"summary": "prefix"}',
        "json",
        response,
        "length",
    )

    assert result["status"] == "failed"
    assert result["error_kind"] == "invalid_response"
    assert result["finish_reason"] == "length"


def test_stage1_reader_scheduler_falls_back_after_length_termination(monkeypatch) -> None:
    calls: list[str] = []

    def fake_detailed(*_args, engine_type="primary", **_kwargs):
        calls.append(engine_type)
        if engine_type == "primary":
            return {
                "status": "failed",
                "content": {"partial": True},
                "engine_type": "primary",
                "error_kind": "invalid_response",
                "finish_reason": "length",
                "message": "truncated",
            }
        return {
            "status": "success",
            "content": {"complete": True},
            "engine_type": "backup",
        }

    monkeypatch.setattr(ai_interface, "_load_api_runtime_settings", lambda: (600, 1))
    monkeypatch.setattr(ai_interface, "get_summary_from_ai_detailed", fake_detailed)

    result = ai_interface.get_summary_from_ai_with_fallback(
        "prompt",
        {"api_key": "primary", "model": "m1"},
        {"api_key": "backup", "model": "m2"},
    )

    assert result == {"complete": True}
    assert calls == ["primary", "backup"]
