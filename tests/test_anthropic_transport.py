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
    _call_ai_api_detailed_uninstrumented,
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
    anthropic_temperature_deprecated,
    anthropic_thinking_mode,
    apply_reasoning_policy,
    is_reasoning_active,
    resolve_anthropic_effort,
    resolve_anthropic_messages_url,
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
# Canonical URL resolution (one resolver, shared by runtime and probe)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "api_base,anthropic_path,expected",
    [
        # Bare host: the default path is appended once.
        ("https://host", "", "https://host/v1/messages"),
        ("https://host/", "", "https://host/v1/messages"),
        # The base already carries /v1: it must not be repeated.
        ("https://host/v1", "", "https://host/v1/messages"),
        ("https://host/v1/", "", "https://host/v1/messages"),
        ("https://host/v1", "v1/messages", "https://host/v1/messages"),
        ("https://host/v1", "/v1/messages", "https://host/v1/messages"),
        # The base already points straight at the endpoint.
        ("https://host/v1/messages", "", "https://host/v1/messages"),
        ("https://host/v1/messages", "v1/messages", "https://host/v1/messages"),
        ("https://host/v1/messages/", "/v1/messages/", "https://host/v1/messages"),
        # A base path that only looks similar is preserved, not stripped.
        ("https://host/proxy", "", "https://host/proxy/v1/messages"),
        ("https://host/api/v1", "v1/messages", "https://host/api/v1/messages"),
        # Custom explicit paths are honoured, never rewritten.
        ("https://host", "v2/messages", "https://host/v2/messages"),
        ("https://host/v1", "custom/gateway/messages", "https://host/v1/custom/gateway/messages"),
        ("https://host", "/custom/messages", "https://host/custom/messages"),
        # A custom gateway path that shares a leading segment still splices once.
        ("https://host/v1", "v1/gateway/messages", "https://host/v1/gateway/messages"),
        # Ports and non-default hosts survive.
        ("https://host:8443/v1", "", "https://host:8443/v1/messages"),
    ],
)
def test_anthropic_url_is_joined_exactly_once(
    api_base: str, anthropic_path: str, expected: str
) -> None:
    assert resolve_anthropic_messages_url(api_base, anthropic_path) == expected


@pytest.mark.parametrize(
    "api_base",
    [
        "https://host",
        "https://host/",
        "https://host/v1",
        "https://host/v1/",
        "https://host/v1/messages",
    ],
)
def test_resolved_url_never_duplicates_a_segment(api_base: str) -> None:
    url = resolve_anthropic_messages_url(api_base, "v1/messages")

    assert url.count("/v1/") <= 1, url
    assert url.count("/messages") == 1, url


def test_resolved_url_preserves_the_configured_host_identity() -> None:
    """The host that was configured must be the host that is requested."""

    for api_base in ("https://gate.test/v1", "https://gate.test:9443", "https://gate.test/v1/messages"):
        url = resolve_anthropic_messages_url(api_base, "v1/messages")
        assert url.split("://", 1)[1].split("/", 1)[0] == api_base.split("://", 1)[1].split("/", 1)[0].rstrip("/")


def test_runtime_and_connection_probe_resolve_the_same_url(monkeypatch) -> None:
    """The bug this replaces: probe PASSes against a URL the runtime never requests.

    Both sides are driven through the same base/path inputs and must land on the
    same string, for every shape of base that a gateway operator might enter.
    """

    from config_validator import test_api_connection

    captured: list[str] = []

    class _Response:
        status_code = 200

        def json(self) -> dict[str, object]:
            return {"content": [{"type": "text", "text": "ok"}]}

    def _fake_post(url: str, **_kwargs: object) -> _Response:
        captured.append(url)
        return _Response()

    monkeypatch.setattr("config_validator.requests.post", _fake_post)

    for api_base in ("https://gate.test", "https://gate.test/", "https://gate.test/v1", "https://gate.test/v1/", "https://gate.test/v1/messages"):
        for configured_path in ("", "v1/messages", "/v1/messages"):
            captured.clear()
            runtime_url, _headers = anthropic_request_target(
                api_base, {"anthropic_path": configured_path}, "k"
            )
            test_api_connection(
                "k",
                api_base,
                "claude-opus-5",
                endpoint_type="anthropic",
                provider_family="anthropic",
                anthropic_path=configured_path,
            )
            assert captured == [runtime_url], (
                f"probe requested {captured} but runtime would request {runtime_url!r} "
                f"for api_base={api_base!r} path={configured_path!r}"
            )


def test_production_path_posts_to_the_canonical_url(monkeypatch) -> None:
    """Through the real transport, not just the string helper.

    A unit test of the resolver would still pass if the runtime simply never
    called it. This drives the transport that actually builds the URL and body
    (``_call_ai_api_detailed_uninstrumented``) with a mocked socket, and asserts
    both where it connected and what it sent.
    """

    requested: list[str] = []
    sent: list[dict[str, Any]] = []

    class _Response:
        status_code = 200
        headers: dict[str, str] = {}

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return {
                "content": [{"type": "text", "text": "ok"}],
                "stop_reason": "end_turn",
                "model": "claude-opus-5",
                "usage": {"input_tokens": 10, "output_tokens": 2},
            }

    def _fake_post(url: str, **kwargs: Any) -> _Response:
        requested.append(url)
        sent.append(dict(kwargs.get("json") or {}))
        return _Response()

    monkeypatch.setattr("ai_interface.requests.post", _fake_post)
    monkeypatch.setattr("ai_interface.should_bypass_environment_proxy", lambda _config: False)

    for api_base, expected_url in (
        ("https://gate.test", "https://gate.test/v1/messages"),
        ("https://gate.test/v1", "https://gate.test/v1/messages"),
        ("https://gate.test/v1/messages", "https://gate.test/v1/messages"),
    ):
        requested.clear()
        sent.clear()
        result = _call_ai_api_detailed_uninstrumented(
            "hello",
            {**ANTHROPIC_CONFIG, "api_key": "k", "api_base": api_base},
            "sys",
            max_tokens=1024,
            temperature=0.7,
            response_format="text",
        )
        assert result.get("status") == "success", result
        assert requested == [expected_url], f"api_base={api_base!r} produced {requested}"

        body = sent[0]
        assert body["thinking"] == {"type": "adaptive"}
        # Opus 5: effort stays at the documented default and temperature is gone.
        assert body["output_config"] == {"effort": "high"}
        assert "temperature" not in body


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


def test_temperature_is_withheld_on_opus_5_even_when_thinking_is_disabled() -> None:
    """Current generation: temperature is deprecated, not merely reasoning-gated.

    Anthropic's API usage primer: "On Claude 4.7 and later models and Claude
    Mythos Preview, ``temperature`` is deprecated and only its default value is
    accepted, even when thinking is off."

    This test previously asserted ``payload["temperature"] == 0.7``. That pinned
    a request the provider is documented to reject, and it is exactly the
    assumption that had to be broken.
    """

    config = {**ANTHROPIC_CONFIG, "thinking": "disabled"}
    payload = build_anthropic_messages_payload(
        "hello", config, "sys", max_tokens=1024, temperature=0.7, response_format="text",
    )

    assert "temperature" not in payload
    assert not is_reasoning_active(payload, resolve_model_capability(config))


@pytest.mark.parametrize(
    "model,expected_deprecated",
    [
        ("claude-opus-5", True),
        ("claude-opus-4-8", True),
        ("claude-opus-4-7", True),
        ("claude-sonnet-5", True),
        # Named explicitly by the deprecation note despite its version stamp.
        ("claude-mythos-preview", True),
        # 4.6 and earlier are not in the note: only the "thinking enabled -> 1"
        # rule applies to them.
        ("claude-opus-4-6", False),
        ("claude-opus-4-5", False),
        ("claude-sonnet-4-5", False),
        ("claude-opus-4-1", False),
    ],
)
def test_temperature_deprecation_follows_the_model_generation(
    model: str, expected_deprecated: bool
) -> None:
    assert anthropic_temperature_deprecated(model) is expected_deprecated


def test_legacy_claude_keeps_temperature_when_thinking_is_disabled() -> None:
    """The legacy contract still applies where the protocol still allows it.

    The deprecation note is scoped to 4.7+ and Mythos Preview, so a manual
    generation with thinking off keeps its configured sampling.
    """

    config = {
        **ANTHROPIC_CONFIG,
        "model": "claude-opus-4-5-20251101",
        "thinking": "disabled",
    }
    payload = build_anthropic_messages_payload(
        "hello", config, "sys", max_tokens=1024, temperature=0.7, response_format="text",
    )

    assert payload["temperature"] == 0.7


def test_legacy_claude_drops_temperature_when_thinking_is_enabled() -> None:
    """"Temperature must be set to 1 (or left unset) whenever thinking is
    enabled, on all models" -- including the legacy generations."""

    config = {
        **ANTHROPIC_CONFIG,
        "model": "claude-opus-4-5-20251101",
        "thinking_budget_tokens": "2000",
        "max_output_tokens": "8000",
    }
    payload = build_anthropic_messages_payload(
        "hello", config, "sys", max_tokens=1024, temperature=0.7, response_format="text",
    )

    assert payload["thinking"]["type"] == "enabled"
    assert "temperature" not in payload


def test_unrecognised_model_on_anthropic_gets_no_custom_temperature() -> None:
    """Generic Anthropic-compatible: fail conservative.

    There is no Claude generation evidence for an unknown model on an
    Anthropic-shaped endpoint, so nothing is relaxed on a guess. Withholding a
    sampling knob costs the caller determinism tuning; sending an unsupported
    one costs a 400.
    """

    config = {**ANTHROPIC_CONFIG, "model": "some-gateway-model"}
    payload = build_anthropic_messages_payload(
        "hello", config, "sys", max_tokens=1024, temperature=0.7, response_format="text",
    )

    assert "temperature" not in payload


def test_top_p_and_top_k_never_reach_the_anthropic_transport() -> None:
    """They are not part of this transport's emitted sampling contract."""

    config = {**ANTHROPIC_CONFIG, "model": "claude-opus-4-5-20251101", "thinking": "disabled"}
    payload = build_anthropic_messages_payload(
        "hello", config, "sys", max_tokens=1024, temperature=0.7, response_format="text",
    )

    assert "top_p" not in payload
    assert "top_k" not in payload


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
    # Opus 5 deprecates temperature outright, so disabling thinking does not
    # bring a custom value back.
    assert "temperature" not in payload


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
