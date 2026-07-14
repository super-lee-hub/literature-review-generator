from services.model_capabilities import resolve_model_capability
from services.model_selection import get_free_mode_api_config, get_outline_api_config, get_writer_api_config


def test_get_outline_api_config_falls_back_to_writer_when_outline_key_is_placeholder() -> None:
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

    assert api_config == {
        "api_key": "writer-key",
        "model": "writer-model",
        "api_base": "https://writer.example.com/v1",
        "proxy_mode": "direct",
    }


def test_get_free_mode_api_config_falls_back_to_outline_when_free_mode_key_is_placeholder() -> None:
    config = {
        "Outline_API": {
            "api_key": "outline-key",
            "model": "outline-model",
            "api_base": "https://outline.example.com/v1",
            "proxy_mode": "environment",
        },
        "Free_Mode_API": {
            "api_key": "YOUR_FREE_MODE_API_KEY_HERE",
            "model": "planner-model",
            "api_base": "https://planner.example.com/v1",
        },
    }

    api_config = get_free_mode_api_config(config)

    assert api_config == {
        "api_key": "outline-key",
        "model": "outline-model",
        "api_base": "https://outline.example.com/v1",
        "proxy_mode": "environment",
    }


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
