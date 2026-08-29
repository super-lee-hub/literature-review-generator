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
from runtime.provider_completion import ProviderCompletionEvaluator
from services.model_capabilities import (
    _normalize_endpoint,
    anthropic_effort_levels,
    anthropic_model_key,
    anthropic_thinking_mode,
    apply_reasoning_policy,
    is_reasoning_active,
    resolve_anthropic_effort,
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


def test_manual_thinking_budget_raises_max_tokens_above_the_budget() -> None:
    """Manual extended thinking: the budget must sit below max_tokens.

    Confined to the Claude 4.5-and-earlier manual mode. Adaptive thinking has no
    budget, so this constraint does not apply there.
    """

    config = {
        **ANTHROPIC_CONFIG,
        "model": "claude-opus-4-5-20251101",
        "thinking_budget_tokens": "5000",
        "max_output_tokens": "4000",
    }
    payload = build_anthropic_messages_payload(
        "hello", config, "sys", max_tokens=1024, temperature=0.3, response_format="text",
    )

    assert payload["thinking"] == {"type": "enabled", "budget_tokens": 5000}
    assert payload["max_tokens"] > 5000
    # Adaptive is a 400 on a manual-only model, so it must never appear here.
    assert "output_config" not in payload


def test_temperature_is_omitted_while_thinking_is_active() -> None:
    payload = build_anthropic_messages_payload(
        "hello", ANTHROPIC_CONFIG, "sys",
        max_tokens=1024, temperature=0.7, response_format="text",
    )

    # Opus 5 is adaptive-only; {"type": "enabled"} would be a 400.
    assert payload["thinking"]["type"] == "adaptive"
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

    assert payload["thinking"] == {"type": "adaptive"}
    assert is_reasoning_active(payload, capability)


# ---------------------------------------------------------------------------
# Thinking mode and effort, per Anthropic's current generation split
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "model,expected_mode",
    [
        ("claude-opus-5", "adaptive"),
        ("claude-opus-4-8", "adaptive"),
        ("claude-opus-4-7", "adaptive"),
        ("claude-opus-4-6-20260206", "adaptive"),
        ("claude-opus-4-5-20251101", "manual"),
        ("claude-haiku-4-5-20251001", "manual"),
        ("deepseek-v4-flash", "none"),
    ],
)
def test_thinking_mode_follows_the_model_generation(model: str, expected_mode: str) -> None:
    """4.7+ reject the legacy form with a 400, so the mode cannot be assumed."""

    assert anthropic_thinking_mode(model) == expected_mode


def test_legacy_enabled_is_never_sent_to_opus_5() -> None:
    """Regression: this used to emit {"type": "enabled", "budget_tokens": N}."""

    config = {
        **ANTHROPIC_CONFIG,
        "thinking_budget_tokens": "5000",
        "max_output_tokens": "16000",
    }
    payload = build_anthropic_messages_payload(
        "hello", config, "sys", max_tokens=1024, temperature=0.3, response_format="text",
    )

    assert payload["thinking"] == {"type": "adaptive"}
    # Adaptive thinking has no budget; carrying the legacy knob across would be
    # a silent no-op at best and a rejected request at worst.
    assert "budget_tokens" not in payload["thinking"]
    assert "thinking_budget_tokens" not in payload


def test_effort_defaults_to_high_for_opus_5() -> None:
    payload = build_anthropic_messages_payload(
        "hello", ANTHROPIC_CONFIG, "sys",
        max_tokens=1024, temperature=0.3, response_format="text",
    )

    assert payload["output_config"]["effort"] == "high"


def test_force_highest_reasoning_uses_the_models_top_effort() -> None:
    config = {**ANTHROPIC_CONFIG, "force_highest_reasoning": "true"}
    payload = build_anthropic_messages_payload(
        "hello", config, "sys", max_tokens=1024, temperature=0.3, response_format="text",
    )

    # Opus 5 supports all five levels, so the top is max.
    assert payload["output_config"]["effort"] == "max"


def test_effort_is_clamped_to_what_the_model_accepts() -> None:
    """An unsupported effort level is a rejected request, so it is stepped down."""

    assert resolve_anthropic_effort("xhigh", "claude-opus-4-5") == "high"
    assert resolve_anthropic_effort("max", "claude-opus-4-5") == "high"
    assert resolve_anthropic_effort("xhigh", "claude-opus-4-6") == "high"
    assert resolve_anthropic_effort("max", "claude-opus-4-6") == "max"
    assert resolve_anthropic_effort("xhigh", "claude-opus-5") == "xhigh"
    assert resolve_anthropic_effort("max", "claude-opus-5") == "max"


def test_effort_never_receives_the_adaptive_value() -> None:
    """``adaptive`` is a thinking mode, not an effort level."""

    for model in ("claude-opus-5", "claude-opus-4-6", "claude-opus-4-5"):
        assert "adaptive" not in anthropic_effort_levels(model)
        assert resolve_anthropic_effort("adaptive", model) != "adaptive"


def test_disabled_thinking_withholds_effort() -> None:
    """Opus 5 rejects {"type": "disabled"} at xhigh/max, so effort is dropped."""

    config = {**ANTHROPIC_CONFIG, "thinking": "disabled", "force_highest_reasoning": "true"}
    payload = build_anthropic_messages_payload(
        "hello", config, "sys", max_tokens=1024, temperature=0.3, response_format="text",
    )

    assert payload["thinking"] == {"type": "disabled"}
    assert "output_config" not in payload
    assert payload["temperature"] == 0.3


def test_non_claude_model_through_anthropic_gets_no_thinking_block() -> None:
    """A gateway may serve a non-Claude model on an Anthropic-shaped endpoint."""

    config = {**ANTHROPIC_CONFIG, "model": "deepseek-v4-flash"}
    capability = resolve_model_capability(config)
    payload = build_anthropic_messages_payload(
        "hello", config, "sys", max_tokens=1024, temperature=0.3, response_format="text",
    )

    assert capability.supports_reasoning is False
    assert "thinking" not in payload
    assert "output_config" not in payload


def test_dated_snapshot_ids_resolve_to_their_family() -> None:
    assert anthropic_model_key("claude-opus-4-6-20260206") == "opus-4-6"
    assert anthropic_model_key("claude-opus-5[1m]") == "opus-5"
    assert anthropic_model_key("claude-opus-5") == "opus-5"


def test_high_effort_warns_when_max_tokens_is_too_small() -> None:
    """max_tokens is a cost decision, so it is reported rather than overridden."""

    class _Logger:
        def __init__(self) -> None:
            self.messages: list[str] = []

        def warning(self, message: str, *args: object) -> None:
            self.messages.append(message % args if args else message)

        def info(self, message: str, *args: object) -> None:
            return None

    logger = _Logger()
    config = {**ANTHROPIC_CONFIG, "force_highest_reasoning": "true", "max_output_tokens": "4096"}
    build_anthropic_messages_payload(
        "hello", config, "sys", max_tokens=1024, temperature=0.3,
        response_format="text", logger=logger,
    )

    assert any("max_tokens" in message for message in logger.messages), logger.messages


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
        ("stop_sequence", "stop"),
        ("max_tokens", "length"),
        # Exhausting the context window is truncation, however valid the prefix.
        ("model_context_window_exceeded", "length"),
        ("pause_turn", "incomplete_continuation"),
        ("refusal", "refusal"),
        ("tool_use", "anthropic_tool_use"),
    ],
)
def test_stop_reason_is_mapped_to_the_project_finish_reason(
    stop_reason: str, expected: str
) -> None:
    _, finish = parse_anthropic_messages_response(
        {"content": [{"type": "text", "text": "x"}], "stop_reason": stop_reason}
    )

    assert finish == expected


@pytest.mark.parametrize(
    "stop_reason",
    ["model_context_window_exceeded", "pause_turn", "refusal", "tool_use"],
)
def test_non_terminal_stops_are_never_adopted_as_complete(stop_reason: str) -> None:
    """A well-formed JSON body must not rescue a non-terminal stop.

    This is the failure the review called out: a half-finished outline that
    happens to parse must not be promoted to a completed artifact.
    """

    content, finish_reason = parse_anthropic_messages_response(
        {
            "content": [{"type": "text", "text": '{"sections": [{"title": "A"}]}'}],
            "stop_reason": stop_reason,
        }
    )
    completion = ProviderCompletionEvaluator.evaluate(
        {"content": content, "finish_reason": finish_reason, "status": "success"},
        minimum_output=2,
        expect_json=True,
    )

    assert completion.status != "complete", f"{stop_reason} was treated as complete"
    assert completion.error_kind is not None


def test_missing_content_does_not_raise() -> None:
    content, finish = parse_anthropic_messages_response({"stop_reason": "end_turn"})

    assert content == ""
    assert finish == "stop"
