"""Explicit route ceilings survive config, profile, identity, and wire building."""
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from runtime.orchestrator import InternalStageExecutorRegistry
from runtime.provider_context import ProviderContextProfile, writer_output_token_limit
from services.model_selection import get_api_config_for_section
from services.settings import ApplicationSettings
from services.stage1_output_budget import provider_output_token_limit


def _route(ceiling_key, ceiling):
    config = {"Outline_API": {
        "api_key": "test-only", "api_base": "http://127.0.0.1:1/v1",
        "provider_family": "generic", "endpoint_type": "chat_completions",
        "model": "route-ceiling-test", "max_context_tokens": "200000",
        "max_output_tokens": "16000", ceiling_key: ceiling,
        "reasoning_reserve_tokens": "0", "safety_margin_tokens": "0",
    }}
    session = SimpleNamespace(stage_host=SimpleNamespace(
        config=config, settings=ApplicationSettings.from_config(config), logger=None,
    ))
    owner = object.__new__(InternalStageExecutorRegistry)
    return owner._outline_route_for_section(
        session=session, role="candidate_provider_generation", section_name="Outline_API",
    ), config


@pytest.mark.parametrize("key", ["provider_max_output_tokens", "max_output_tokens_limit"])
def test_actual_outline_route_binds_ceiling_to_profile_identity_and_wire(key):
    route, config = _route(key, "6000")
    assert route is not None
    resolved = get_api_config_for_section(config, "Outline_API")
    assert resolved[key] == "6000"
    assert route.profile.max_output_tokens == 6000
    assert route.profile.input_budget == 160000 - 6000
    assert route.config_identity[key] == "6000"
    changed = replace(route, config_identity={**dict(route.config_identity), key: "5000"})
    assert changed.safe_config_fingerprint() != route.safe_config_fingerprint()
    with patch("ai_interface._call_ai_api_detailed_uninstrumented", return_value={"status": "failed"}) as wire:
        route.transport._call("topic_synthesis_provider:1", {}, attempt_limit=1, output_tokens=6000)
        assert wire.call_args.kwargs["max_tokens"] == 6000
        assert {wire.call_args.args[1][field] for field in (
            "max_tokens", "max_completion_tokens", "max_output_tokens",
        )} == {6000}
    with patch("ai_interface._call_ai_api_detailed_uninstrumented") as wire:
        with pytest.raises(ValueError, match="route ceiling"):
            route.transport._call("topic_synthesis_provider:1", {}, attempt_limit=1, output_tokens=6001)
        wire.assert_not_called()


def test_explicit_ceiling_wins_over_default_target_and_writer_minimum():
    config = {"max_output_tokens": 16000, "provider_max_output_tokens": 8000,
              "max_output_tokens_limit": 128}
    profile = ProviderContextProfile.from_api_config(config, max_output_tokens=16000)
    assert profile.max_output_tokens == 128
    assert writer_output_token_limit(config) == 128
    assert provider_output_token_limit(config) == 128


@pytest.mark.parametrize("invalid", [True, 0, -1, "bad", 1.5])
def test_invalid_explicit_ceiling_cannot_silently_expand(invalid):
    with pytest.raises(ValueError, match="positive integer"):
        ProviderContextProfile.from_api_config(
            {"provider_max_output_tokens": invalid}, max_output_tokens=16000,
        )


def test_outline_profile_defaults_and_explicit_zero_reserves_are_distinct():
    default = ProviderContextProfile.from_api_config(
        {}, max_output_tokens=16000, default_reasoning_reserve=2048,
        default_safety_margin=1024,
    )
    assert (default.reasoning_reserve, default.safety_margin) == (2048, 1024)
    zero = ProviderContextProfile.from_api_config(
        {"reasoning_reserve_tokens": "0", "safety_margin_tokens": "0"},
        max_output_tokens=16000, default_reasoning_reserve=2048, default_safety_margin=1024,
    )
    assert (zero.reasoning_reserve, zero.safety_margin) == (0, 0)
