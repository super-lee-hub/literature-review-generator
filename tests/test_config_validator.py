from config_validator import validate_all_config


def _base_config():
    return {
        "Paths": {"output_path": "./output"},
        "Primary_Reader_API": {
            "api_key": "sk-primary",
            "model": "deepseek-v4-pro",
            "api_base": "https://api.deepseek.com",
            "endpoint_type": "chat_completions",
            "provider_family": "deepseek",
            "thinking": "enabled",
            "reasoning_effort": "max",
        },
        "Backup_Reader_API": {
            "api_key": "sk-backup",
            "model": "gemini-2.5-pro",
            "api_base": "https://api.videocaptioner.cn/v1",
        },
        "Writer_API": {
            "api_key": "sk-writer",
            "model": "gpt-5.5",
            "api_base": "https://aihubmix.com/v1",
            "endpoint_type": "responses",
            "provider_family": "aihubmix_openai",
            "reasoning_effort": "high",
        },
    }


def test_validate_all_config_accepts_default_reasoning_transport_combo():
    valid, warnings = validate_all_config(_base_config())

    assert valid is True
    assert not any("provider_family" in warning or "endpoint_type" in warning for warning in warnings)


def test_validate_all_config_warns_for_mismatched_provider_endpoint_combo():
    config = _base_config()
    config["Writer_API"]["endpoint_type"] = "chat_completions"

    valid, warnings = validate_all_config(config)

    assert valid is True
    assert any("aihubmix_openai reasoning config requires endpoint_type=responses" in warning for warning in warnings)


def test_validate_all_config_warns_for_reasoning_fields_on_generic_provider():
    config = _base_config()
    config["Backup_Reader_API"]["reasoning_effort"] = "high"

    valid, warnings = validate_all_config(config)

    assert valid is True
    assert any("reasoning fields are set" in warning for warning in warnings)
