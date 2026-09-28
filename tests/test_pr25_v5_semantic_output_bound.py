"""Semantic output limits are explicit and shared by planning and execution."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from ai_interface import (
    build_anthropic_messages_payload,
    build_chat_completions_payload,
    build_responses_payload,
)
from outline.v3_executor import OutlineV3ExecutionError, OutlineV3Executor
from runtime.provider_context import ProviderContextProfile
from runtime.orchestrator import _OutlineProviderTransportAdapter
from services.job_workspace import JobWorkspace
from services.settings import ApplicationSettings


def _executor(tmp_path: Path, **kwargs: object) -> OutlineV3Executor:
    workspace = JobWorkspace.create(str(tmp_path), "output-bound", job_id="output-bound-job")
    profile = ProviderContextProfile.conservative(
        provider="fixture",
        model="outline-v3",
        endpoint_type="internal",
        model_context_limit=128_000,
        max_output_tokens=16_384,
    )
    return OutlineV3Executor(
        job_id=workspace.job_id,
        summaries=[],
        workspace=workspace,
        provider_profile=profile,
        **kwargs,
    )


def test_outline_semantic_output_bound_defaults_and_configures(tmp_path: Path) -> None:
    assert ApplicationSettings.from_config({}).outline.semantic_output_max_tokens == 4_096
    settings = ApplicationSettings.from_config(
        {"Outline": {"semantic_output_max_tokens": "8192"}}
    )
    assert settings.outline.semantic_output_max_tokens == 8_192

    default = _executor(tmp_path / "default")
    configured = _executor(
        tmp_path / "configured",
        semantic_output_max_tokens=settings.outline.semantic_output_max_tokens,
    )
    assert default._semantic_output_token_limit(default.profile) == 4_096
    assert configured._semantic_output_token_limit(configured.profile) == 8_192
    assert (
        default.build_current_node_binding("topic_synthesis")["relevant_runtime_config_hash"]
        != configured.build_current_node_binding("topic_synthesis")["relevant_runtime_config_hash"]
    )


@pytest.mark.parametrize("invalid", [0, -1, True, "bogus"])
def test_outline_semantic_output_bound_rejects_invalid_values(
    tmp_path: Path, invalid: object
) -> None:
    with pytest.raises(ValueError, match="semantic_output_max_tokens"):
        ApplicationSettings.from_config(
            {"Outline": {"semantic_output_max_tokens": invalid}}
        )
    with pytest.raises(ValueError, match="semantic_output_max_tokens"):
        _executor(tmp_path / str(invalid), semantic_output_max_tokens=invalid)


def test_wire_token_limit_matches_admitted_semantic_reservation() -> None:
    profile = ProviderContextProfile.conservative(
        provider="claude_chat_reasoning",
        model="claude-opus-5-5",
        endpoint_type="chat_completions",
        model_context_limit=1_000_000,
        max_output_tokens=65_536,
    )
    adapter = _OutlineProviderTransportAdapter(
        api_config={
            "max_tokens": 65_536,
            "max_completion_tokens": 65_536,
            "max_output_tokens": 65_536,
        },
        profile=profile,
        logger=None,
        system_prompt="Return JSON.",
    )
    with patch(
        "ai_interface._call_ai_api_detailed_uninstrumented",
        return_value={"status": "failed"},
    ) as transport:
        adapter._call("topic_synthesis_provider:batch:1", {}, attempt_limit=1, output_tokens=8_192)
    sent_config = transport.call_args.args[1]
    assert transport.call_args.kwargs["max_tokens"] == 8_192
    assert {sent_config[field] for field in (
        "max_tokens", "max_completion_tokens", "max_output_tokens"
    )} == {8_192}

    with pytest.raises(ValueError, match="output allowance"):
        adapter._call("topic_synthesis_provider:batch:1", {}, attempt_limit=1, output_tokens=0)


def test_manual_thinking_cannot_raise_wire_cap_above_reservation() -> None:
    profile = ProviderContextProfile.conservative(
        provider="anthropic",
        model="claude-opus-4-5",
        endpoint_type="anthropic",
        model_context_limit=128_000,
        max_output_tokens=16_384,
    )
    adapter = _OutlineProviderTransportAdapter(
        api_config={
            "provider_family": "anthropic",
            "endpoint_type": "anthropic",
            "model": "claude-opus-4-5",
            "api_base": "https://api.anthropic.com",
            "max_output_tokens": 16_384,
            "thinking_budget_tokens": 5_000,
        },
        profile=profile,
        logger=None,
        system_prompt="Return JSON.",
    )
    with patch("ai_interface._call_ai_api_detailed_uninstrumented") as transport:
        with pytest.raises(ValueError, match="thinking budget exceeds"):
            adapter._call(
                "topic_synthesis_provider:batch:1", {}, attempt_limit=1,
                output_tokens=4_096,
            )
    transport.assert_not_called()


def test_manual_thinking_overflow_blocks_semantic_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executor = _executor(tmp_path, semantic_output_max_tokens=4_096)
    profile = ProviderContextProfile.conservative(
        provider="anthropic", model="claude-opus-4-5",
        endpoint_type="anthropic", model_context_limit=128_000,
        max_output_tokens=16_384,
    )
    route = SimpleNamespace(
        model="claude-opus-4-5",
        config_identity={"thinking_budget_tokens": "5000"},
    )
    monkeypatch.setattr(executor, "_role_route", lambda _role: route)
    with pytest.raises(OutlineV3ExecutionError, match="planned output allowance"):
        executor._semantic_output_token_limit(profile)


@pytest.mark.parametrize(
    "provider,model,endpoint,builder,payload_field",
    [
        ("anthropic", "claude-opus-5-5", "anthropic", build_anthropic_messages_payload, "max_tokens"),
        ("claude_chat_reasoning", "claude-opus-5-5", "chat_completions", build_chat_completions_payload, "max_tokens"),
        ("openai_responses", "gpt-5.6-sol", "responses", build_responses_payload, "max_output_tokens"),
    ],
)
def test_final_provider_payload_uses_admitted_semantic_limit(
    provider: str, model: str, endpoint: str, builder: object, payload_field: str
) -> None:
    profile = ProviderContextProfile.conservative(
        provider=provider, model=model, endpoint_type=endpoint,
        model_context_limit=128_000, max_output_tokens=65_536,
    )
    adapter = _OutlineProviderTransportAdapter(
        api_config={
            "provider_family": provider,
            "endpoint_type": endpoint,
            "model": model,
            "api_base": "https://example.test/v1",
            "max_tokens": 65_536,
            "max_completion_tokens": 65_536,
            "max_output_tokens": 65_536,
        },
        profile=profile,
        logger=None,
        system_prompt="Return JSON.",
    )
    with patch(
        "ai_interface._call_ai_api_detailed_uninstrumented",
        return_value={"status": "failed"},
    ) as transport:
        adapter._call("topic_synthesis_provider:batch:1", {}, attempt_limit=1, output_tokens=8_192)
    config = transport.call_args.args[1]
    payload = builder(
        "{}", config, "Return JSON.", max_tokens=transport.call_args.kwargs["max_tokens"],
        temperature=0.0, response_format="json",
    )
    assert payload[payload_field] == 8_192
