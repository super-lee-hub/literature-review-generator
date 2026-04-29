from services.model_selection import get_free_mode_api_config, get_outline_api_config


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
