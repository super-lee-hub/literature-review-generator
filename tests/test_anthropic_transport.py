"""Regression tests for the native Anthropic Messages transport.

Before this transport existed the project could only speak ``chat/completions``
and ``responses``, so a Claude model behind an Anthropic Messages endpoint was
unreachable. These tests pin the wire contract that differs from the OpenAI
protocols: the top-level ``system`` field, ``max_tokens``, the ``x-api-key``
auth header, extended thinking, and block-list response parsing.
"""

from __future__ import annotations

from typing import Any

import pytest

from ai_interface import (
    DEFAULT_ANTHROPIC_PATH,
    DEFAULT_ANTHROPIC_VERSION,
    _convert_chat_content_to_anthropic_content,
    anthropic_request_target,
    build_anthropic_messages_payload,
    parse_anthropic_messages_response,
)
from services.model_capabilities import (
    _normalize_endpoint,
    apply_reasoning_policy,
    is_reasoning_active,
    resolve_model_capability,
)

ANTHROPIC_CONFIG: dict[str, Any] = {
    "model": "claude-opus-5",
    "api_base": "https://chat.178266.xyz",
    "endpoint_type": "anthropic",
    "provider_family": "anthropic",
}


# ---------------------------------------------------------------------------
# Capability resolution
# ---------------------------------------------------------------------------


def test_anthropic_endpoint_resolves_the_anthropic_capability() -> None:
    capability = resolve_model_capability(ANTHROPIC_CONFIG)

    assert capability.endpoint_type == "anthropic"
    assert capability.provider_family == "anthropic"
    assert capability.reasoning_param_style == "anthropic_thinking"
    # The wire parameter really is max_tokens, unlike the responses protocol.
    assert capability.max_token_param == "max_tokens"


@pytest.mark.parametrize(
    "raw",
    ["anthropic", "Anthropic", "anthropic-messages", "messages"],
)
def test_endpoint_type_aliases_normalize_to_anthropic(raw: str) -> None:
    assert _normalize_endpoint(raw) == "anthropic"


def test_anthropic_is_not_inferred_from_model_name_alone() -> None:
    """A Claude model id cannot tell an Anthropic endpoint from an OpenAI-shaped
    gateway that proxies Claude. Guessing would pick the wrong wire format, so
    the transport must stay explicit.
    """

    capability = resolve_model_capability(
        {"model": "claude-opus-5", "api_base": "https://example.test"}
    )
    assert capability.endpoint_type != "anthropic"


def test_deepseek_and_responses_configs_are_untouched() -> None:
    deepseek = resolve_model_capability(
        {"model": "deepseek-v4-flash", "api_base": "https://api.deepseek.com"}
    )
    responses = resolve_model_capability(
        {"model": "gpt-5.6-sol", "endpoint_type": "responses"}
    )

    assert deepseek.endpoint_type == "chat_completions"
    assert responses.endpoint_type == "responses"


# ---------------------------------------------------------------------------
# Request target: URL and auth headers
# ---------------------------------------------------------------------------


def test_request_target_uses_x_api_key_not_bearer() -> None:
    url, headers = anthropic_request_target("https://chat.178266.xyz", {}, "secret-token")

    assert url == f"https://chat.178266.xyz/{DEFAULT_ANTHROPIC_PATH}"
    assert headers["x-api-key"] == "secret-token"
    assert headers["anthropic-version"] == DEFAULT_ANTHROPIC_VERSION
    assert "Authorization" not in headers


def test_request_target_strips_trailing_slash_without_doubling_it() -> None:
    url, _ = anthropic_request_target("https://chat.178266.xyz/", {}, "k")

    assert url == "https://chat.178266.xyz/v1/messages"
    assert "://" in url and "//v1" not in url.replace("://", "")


def test_request_target_honours_configured_path_and_version() -> None:
    url, headers = anthropic_request_target(
        "https://gateway.test",
        {"anthropic_path": "/v2/messages/", "anthropic_version": "2024-01-01"},
        "k",
    )

    assert url == "https://gateway.test/v2/messages"
    assert headers["anthropic-version"] == "2024-01-01"


# ---------------------------------------------------------------------------
# Payload construction
# ---------------------------------------------------------------------------


def test_system_prompt_is_a_top_level_field() -> None:
    payload = build_anthropic_messages_payload(
        "hello", ANTHROPIC_CONFIG, "be brief",
        max_tokens=1024, temperature=0.3, response_format="text",
    )

    assert payload["system"] == "be brief"
    assert payload["messages"] == [{"role": "user", "content": "hello"}]
    # A system message inside messages is an Anthropic protocol error.
    assert all(message["role"] != "system" for message in payload["messages"])


def test_max_tokens_comes_from_max_output_tokens_config() -> None:
    config = {**ANTHROPIC_CONFIG, "max_output_tokens": "8000"}
    payload = build_anthropic_messages_payload(
        "hello", config, "sys", max_tokens=1024, temperature=0.3, response_format="text",
    )

    assert payload["max_tokens"] == 8000


def test_thinking_budget_raises_max_tokens_above_the_budget() -> None:
    """Anthropic rejects max_tokens <= budget_tokens."""

    config = {**ANTHROPIC_CONFIG, "thinking_budget_tokens": "5000", "max_output_tokens": "4000"}
    payload = build_anthropic_messages_payload(
        "hello", config, "sys", max_tokens=1024, temperature=0.3, response_format="text",
    )

    assert payload["thinking"] == {"type": "enabled", "budget_tokens": 5000}
    assert payload["max_tokens"] > 5000


def test_temperature_is_omitted_while_thinking_is_active() -> None:
    payload = build_anthropic_messages_payload(
        "hello", ANTHROPIC_CONFIG, "sys",
        max_tokens=1024, temperature=0.7, response_format="text",
    )

    assert payload["thinking"]["type"] == "enabled"
    assert "temperature" not in payload


def test_temperature_is_kept_when_thinking_is_disabled() -> None:
    config = {**ANTHROPIC_CONFIG, "thinking": "disabled"}
    payload = build_anthropic_messages_payload(
        "hello", config, "sys", max_tokens=1024, temperature=0.7, response_format="text",
    )

    assert payload["temperature"] == 0.7
    assert not is_reasoning_active(payload, resolve_model_capability(config))


def test_json_response_format_is_an_instruction_not_a_parameter() -> None:
    """Anthropic Messages has no response_format parameter; pretending otherwise
    would silently drop the JSON contract."""

    payload = build_anthropic_messages_payload(
        "hello", ANTHROPIC_CONFIG, "be brief",
        max_tokens=1024, temperature=0.3, response_format="json",
    )

    assert "response_format" not in payload
    assert "JSON" in payload["system"]


def test_reasoning_policy_marks_thinking_active() -> None:
    capability = resolve_model_capability(ANTHROPIC_CONFIG)
    payload: dict[str, Any] = {}
    apply_reasoning_policy(payload, ANTHROPIC_CONFIG, capability)

    assert payload["thinking"] == {"type": "enabled"}
    assert is_reasoning_active(payload, capability)


# ---------------------------------------------------------------------------
# Content conversion
# ---------------------------------------------------------------------------


def test_data_url_image_becomes_a_base64_source() -> None:
    converted = _convert_chat_content_to_anthropic_content(
        [
            {"type": "text", "text": "看图"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        ]
    )

    assert converted[0] == {"type": "text", "text": "看图"}
    assert converted[1] == {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"},
    }


def test_remote_url_image_becomes_a_url_source() -> None:
    converted = _convert_chat_content_to_anthropic_content(
        [{"type": "image_url", "image_url": {"url": "https://cdn.test/a.png"}}]
    )

    assert converted == [{"type": "image", "source": {"type": "url", "url": "https://cdn.test/a.png"}}]


def test_plain_string_content_passes_through() -> None:
    assert _convert_chat_content_to_anthropic_content("hello") == "hello"


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------


def test_only_text_blocks_are_treated_as_answer_content() -> None:
    content, finish = parse_anthropic_messages_response(
        {
            "content": [
                {"type": "thinking", "thinking": "internal"},
                {"type": "text", "text": "答案"},
            ],
            "stop_reason": "end_turn",
        }
    )

    assert content == "答案"
    assert finish == "stop"


def test_multiple_text_blocks_are_joined() -> None:
    content, _ = parse_anthropic_messages_response(
        {"content": [{"type": "text", "text": "甲"}, {"type": "text", "text": "乙"}], "stop_reason": "end_turn"}
    )

    assert content == "甲\n乙"


@pytest.mark.parametrize(
    "stop_reason,expected",
    [
        ("end_turn", "stop"),
        ("max_tokens", "length"),
        ("tool_use", "tool_calls"),
    ],
)
def test_stop_reason_is_mapped_to_the_project_finish_reason(
    stop_reason: str, expected: str
) -> None:
    _, finish = parse_anthropic_messages_response(
        {"content": [{"type": "text", "text": "x"}], "stop_reason": stop_reason}
    )

    assert finish == expected


def test_missing_content_does_not_raise() -> None:
    content, finish = parse_anthropic_messages_response({"stop_reason": "end_turn"})

    assert content == ""
    assert finish == "stop"
