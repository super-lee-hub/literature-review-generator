import pytest

from config_validator import validate_all_config
from services.config_values import StrictConfigValueError
from services.settings import ApplicationSettings


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


def _anthropic_outline(config):
    """Point [Outline_API] at a native Anthropic Messages endpoint."""

    config.setdefault("Outline_API", {}).update(
        {
            "api_key": "sk-anthropic",
            "model": "claude-opus-5",
            "api_base": "https://chat.178266.xyz",
            "endpoint_type": "anthropic",
            "provider_family": "anthropic",
        }
    )
    return config


def test_validate_all_config_accepts_native_anthropic_combo():
    """provider_family=anthropic + endpoint_type=anthropic is a real, supported pair."""

    config = _anthropic_outline(_base_config())

    valid, messages = validate_all_config(config)

    assert valid is True
    assert not any("anthropic" in message for message in messages), messages


def test_validate_all_config_rejects_half_configured_anthropic():
    """Either half alone is impossible: the transport cannot be built."""

    config = _anthropic_outline(_base_config())
    config["Outline_API"]["endpoint_type"] = "responses"

    valid, messages = validate_all_config(config)

    assert valid is False
    assert any("requires endpoint_type=anthropic" in message for message in messages)


def test_validate_all_config_rejects_anthropic_endpoint_without_the_family():
    config = _anthropic_outline(_base_config())
    config["Outline_API"]["provider_family"] = "deepseek"

    valid, messages = validate_all_config(config)

    assert valid is False
    assert any("requires provider_family=anthropic" in message for message in messages)


def test_thinking_is_accepted_on_the_anthropic_transport():
    """It used to be rejected as 'only valid for DeepSeek reasoning'."""

    config = _anthropic_outline(_base_config())
    config["Outline_API"]["thinking"] = "enabled"

    valid, messages = validate_all_config(config)

    assert valid is True
    assert not any("thinking is only valid" in message for message in messages), messages


def test_thinking_budget_tokens_warns_on_adaptive_models():
    config = _anthropic_outline(_base_config())
    config["Outline_API"]["thinking_budget_tokens"] = "4096"

    valid, messages = validate_all_config(config)

    # A warning, not an error: budget_tokens is simply ignored on adaptive models.
    assert valid is True
    assert any("thinking_budget_tokens" in message for message in messages)


def test_unsupported_effort_level_is_reported():
    config = _anthropic_outline(_base_config())
    config["Outline_API"]["model"] = "claude-opus-4-5-20251101"
    config["Outline_API"]["reasoning_effort"] = "xhigh"

    valid, messages = validate_all_config(config)

    assert valid is True
    assert any("will be reduced to" in message for message in messages), messages


def test_validate_all_config_rejects_mismatched_provider_endpoint_combo():
    """An impossible transport combination is an error, not an advisory.

    Previously this only produced a warning while ``valid`` stayed True, so
    ``doctor`` looked green on a section that cannot build a request.
    """

    config = _base_config()
    config["Writer_API"]["endpoint_type"] = "chat_completions"

    valid, messages = validate_all_config(config)

    assert valid is False
    assert any("selected provider family requires endpoint_type=responses" in message for message in messages)


def test_validate_all_config_accepts_custom_openai_responses_provider():
    config = _base_config()
    config["Writer_API"].update(
        {
            "model": "gpt-5.6-sol",
            "api_base": "https://api.example.com/v1",
            "provider_family": "openai_responses",
            "reasoning_effort": "xhigh",
        }
    )

    valid, warnings = validate_all_config(config)

    assert valid is True
    writer_warnings = [warning for warning in warnings if "[Writer_API]" in warning]
    assert not any(
        marker in warning
        for warning in writer_warnings
        for marker in ("provider_family", "endpoint_type", "reasoning fields")
    )


def test_validate_all_config_accepts_custom_claude_chat_provider():
    config = _base_config()
    config["Writer_API"].update(
        {
            "model": "claude-opus-4-8",
            "api_base": "https://api.example.com/v1",
            "endpoint_type": "chat_completions",
            "provider_family": "claude_chat_reasoning",
            "reasoning_effort": "xhigh",
        }
    )

    valid, warnings = validate_all_config(config)

    assert valid is True
    writer_warnings = [warning for warning in warnings if "[Writer_API]" in warning]
    assert not any(
        marker in warning
        for warning in writer_warnings
        for marker in ("provider_family", "endpoint_type", "reasoning fields")
    )


def test_validate_all_config_warns_for_reasoning_fields_on_generic_provider():
    config = _base_config()
    config["Backup_Reader_API"]["reasoning_effort"] = "high"

    valid, warnings = validate_all_config(config)

    assert valid is True
    assert any("reasoning fields are set" in warning for warning in warnings)


@pytest.mark.parametrize(
    ("section", "key", "value"),
    [
        ("Stage1_Visual", "page_format", "webp"),
        ("Stage1_Visual", "crop_format", "tiff"),
        ("Stage1_Visual", "page_jpeg_quality", "101"),
        ("Stage1_Visual", "render_all_nonblank_pages", "false"),
        ("Stage1_Input", "image_transport", "url"),
    ],
)
def test_validate_all_config_rejects_invalid_stage1_visual_transport_values(
    section: str,
    key: str,
    value: str,
) -> None:
    config = _base_config()
    config[section] = {key: value}

    valid, messages = validate_all_config(config)

    assert valid is False
    assert messages


@pytest.mark.parametrize(
    ("section", "key", "value"),
    [
        ("Stage1_Input", "send_selected_visuals", "truue"),
        ("Stage1_Input", "send_original_pdf", "sometimes"),
        ("Stage1_Input", "mode", "vision"),
        ("Stage1_Visual", "enabled", "maybe"),
        ("Stage1_Visual", "crop_padding_ratio", "0.26"),
        ("Stage1_Visual", "crop_padding_ratio", "not-a-number"),
    ],
)
def test_validate_all_config_rejects_unknown_or_out_of_range_stage1_values(
    section: str,
    key: str,
    value: str,
) -> None:
    config = _base_config()
    config[section] = {key: value}

    valid, messages = validate_all_config(config)

    assert valid is False
    assert messages
    assert key in messages[0]


def test_application_settings_does_not_coerce_unknown_stage1_boolean() -> None:
    config = _base_config()
    config["Stage1_Input"] = {"send_selected_visuals": "truue"}

    with pytest.raises(StrictConfigValueError, match="send_selected_visuals"):
        ApplicationSettings.from_config(config)


def test_application_settings_normalizes_explicit_stage1_values() -> None:
    config = _base_config()
    config["Stage1_Input"] = {
        "send_selected_visuals": "YES",
        "send_original_pdf": "AUTO",
        "mode": "VISION_FIRST",
        "image_transport": "BASE64",
    }
    config["Stage1_Visual"] = {"enabled": "0", "crop_padding_ratio": "0.125"}

    settings = ApplicationSettings.from_config(config)

    assert settings.section("Stage1_Input")["send_selected_visuals"] == "true"
    assert settings.section("Stage1_Input")["send_original_pdf"] == "auto"
    assert settings.section("Stage1_Input")["mode"] == "vision_first"
    assert settings.section("Stage1_Input")["image_transport"] == "base64"
    assert settings.section("Stage1_Visual")["enabled"] == "false"
    assert settings.section("Stage1_Visual")["crop_padding_ratio"] == "0.125"
