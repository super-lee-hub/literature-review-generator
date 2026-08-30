from services.model_capabilities import resolve_model_capability
from services.model_selection import get_free_mode_api_config, get_outline_api_config, get_writer_api_config


def test_get_outline_api_config_resolves_its_own_section_without_writer_fallback() -> None:
    config = {
        "Writer_API": {
            "api_key": "writer-key",
            "model": "writer-model",
            "api_base": "https://writer.example.com/v1",
            "proxy_mode": "direct",
        },
        "Outline_API": {
            "api_key": "loaded_from_.env_file",
            "model": "outline-model",
            "api_base": "https://outline.example.com/v1",
        },
    }

    api_config = get_outline_api_config(config)

    # No mongrel fallback: [Outline_API] is its own route authority, even when
    # incomplete. It must not borrow the Writer gateway's model/address.
    assert api_config["model"] == "outline-model"
    assert api_config["api_base"] == "https://outline.example.com/v1"
    assert api_config["model"] != "writer-model"
    assert api_config["api_base"] != "https://writer.example.com/v1"


def test_get_free_mode_api_config_resolves_its_own_section_without_outline_fallback() -> None:
    config = {
        "Outline_API": {
            "api_key": "outline-key",
            "model": "outline-model",
            "api_base": "https://outline.example.com/v1",
            "proxy_mode": "environment",
        },
        "Free_Mode_API": {
            "api_key": "free-key",
            "model": "planner-model",
            "api_base": "https://planner.example.com/v1",
        },
    }

    api_config = get_free_mode_api_config(config)

    assert api_config["model"] == "planner-model"
    assert api_config["api_base"] == "https://planner.example.com/v1"
    assert api_config["model"] != "outline-model"
    assert api_config["api_base"] != "https://outline.example.com/v1"


def test_api_config_preserves_reasoning_transport_fields() -> None:
    config = {
        "Writer_API": {
            "api_key": "writer-key",
            "model": "gpt-5.5",
            "api_base": "https://aihubmix.com/v1",
            "proxy_mode": "environment",
            "endpoint_type": "responses",
            "provider_family": "aihubmix_openai",
            "reasoning_effort": "high",
            "text_verbosity": "high",
            "max_output_tokens": "32000",
            "force_highest_reasoning": "true",
            "omit_temperature_when_reasoning": "true",
        },
    }

    api_config = get_writer_api_config(config)

    assert api_config.get("endpoint_type") == "responses"
    assert api_config.get("provider_family") == "aihubmix_openai"
    assert api_config.get("reasoning_effort") == "high"
    assert api_config.get("text_verbosity") == "high"
    assert api_config.get("max_output_tokens") == "32000"
    assert api_config.get("force_highest_reasoning") == "true"
    assert api_config.get("omit_temperature_when_reasoning") == "true"


def test_legacy_config_without_endpoint_type_stays_on_chat_completions() -> None:
    api_config = get_writer_api_config(
        {
            "Writer_API": {
                "api_key": "writer-key",
                "model": "gpt-5.5",
                "api_base": "https://aihubmix.com/v1",
            }
        }
    )

    capability = resolve_model_capability(api_config)

    assert "endpoint_type" not in api_config
    assert capability.endpoint_type == "chat_completions"
    assert capability.provider_family == "aihubmix_openai"
    assert capability.reasoning_param_style == "none"


def test_custom_openai_responses_provider_supports_reasoning() -> None:
    capability = resolve_model_capability(
        {
            "api_key": "writer-key",
            "model": "gpt-5.6-sol",
            "api_base": "https://api.example.com/v1",
            "endpoint_type": "responses",
            "provider_family": "openai_responses",
            "reasoning_effort": "xhigh",
        }
    )

    assert capability.endpoint_type == "responses"
    assert capability.provider_family == "openai_responses"
    assert capability.supports_reasoning is True
    assert capability.reasoning_param_style == "responses_reasoning"


def test_custom_claude_chat_provider_supports_reasoning() -> None:
    capability = resolve_model_capability(
        {
            "api_key": "outline-key",
            "model": "claude-opus-4-8",
            "api_base": "https://api.example.com/v1",
            "endpoint_type": "chat_completions",
            "provider_family": "claude_chat_reasoning",
            "reasoning_effort": "xhigh",
        }
    )

    assert capability.endpoint_type == "chat_completions"
    assert capability.provider_family == "claude_chat_reasoning"
    assert capability.supports_reasoning is True
    assert capability.reasoning_param_style == "chat_reasoning"
