from __future__ import annotations

from outline.provider_router import OutlineRoleRoute, safe_config_identity
from runtime.provider_context import ProviderContextProfile
from services.model_selection import get_api_config_for_section
from services.settings import validate_config_keys


def _route(config_identity: dict[str, object]) -> OutlineRoleRoute:
    return OutlineRoleRoute(
        role="candidate_provider_generation",
        config_section="Outline_API",
        provider_name="claude_chat_reasoning",
        model="claude-opus-5-5",
        endpoint_type="chat_completions",
        profile=ProviderContextProfile.conservative(
            provider="claude_chat_reasoning",
            model="claude-opus-5-5",
            endpoint_type="chat_completions",
        ),
        api_base="https://api.example.test/v1",
        config_identity=config_identity,
    )


def test_integer_zero_transport_retries_is_a_bounded_route_identity() -> None:
    implicit = _route({})
    integer_zero = _route({"transport_retries": 0})
    string_zero = _route({"transport_retries": "0"})

    assert safe_config_identity({"transport_retries": 0})["transport_retries"] == "0"
    assert integer_zero.config_identity["transport_retries"] == "0"
    assert integer_zero.safe_config_fingerprint() == string_zero.safe_config_fingerprint()
    assert integer_zero.safe_config_fingerprint() != implicit.safe_config_fingerprint()


def test_provider_stream_changes_the_wire_route_fingerprint() -> None:
    buffered = _route({"transport_retries": "0", "provider_stream": False})
    streamed = _route({"transport_retries": "0", "provider_stream": True})
    streamed_text = _route({"transport_retries": "0", "provider_stream": "TRUE"})

    assert "provider_stream" not in buffered.config_identity
    assert buffered.safe_config_fingerprint() == _route({"transport_retries": "0"}).safe_config_fingerprint()
    assert streamed.config_identity["provider_stream"] == "true"
    assert buffered.safe_config_fingerprint() != streamed.safe_config_fingerprint()
    assert streamed.safe_config_fingerprint() == streamed_text.safe_config_fingerprint()


def test_provider_stream_reaches_the_route_from_current_api_config() -> None:
    section = {
        "api_key": "local-fixture-only",
        "model": "claude-opus-5-5",
        "api_base": "http://127.0.0.1:1/v1",
        "provider_stream": "true",
    }
    config = {"Outline_API": section}

    assert validate_config_keys(config) == []
    route_config = get_api_config_for_section(config, "Outline_API")
    assert route_config["provider_stream"] == "true"
    assert safe_config_identity(route_config)["provider_stream"] == "true"
