from __future__ import annotations

from config_validator import validate_all_config
from services.configuration_service import default_config_sections


_REQUIRED = ("Primary_Reader_API", "Backup_Reader_API", "Writer_API")


def _config() -> dict[str, dict[str, str]]:
    config = default_config_sections()
    for section in _REQUIRED:
        config[section]["api_key"] = f"sk-{section.lower()}-required"
    return config


def test_optional_provider_may_omit_credentials_but_keep_transport_shape() -> None:
    config = _config()
    config["Outline_API"].pop("api_key")

    valid, messages = validate_all_config(
        config,
        required_provider_sections=_REQUIRED,
    )

    assert valid is True, messages


def test_optional_provider_without_credentials_still_requires_model_and_url() -> None:
    missing_model = _config()
    missing_model["Outline_API"].pop("api_key")
    missing_model["Outline_API"].pop("model")
    valid, messages = validate_all_config(
        missing_model,
        required_provider_sections=_REQUIRED,
    )
    assert valid is False
    assert any("Outline_API" in message and "model" in message for message in messages)

    invalid_url = _config()
    invalid_url["Outline_API"]["api_key"] = ""
    invalid_url["Outline_API"]["api_base"] = "not-a-url"
    valid, messages = validate_all_config(
        invalid_url,
        required_provider_sections=_REQUIRED,
    )
    assert valid is False
    assert any("Outline_API" in message and "URL" in message for message in messages)


def test_required_provider_still_rejects_missing_or_blank_credentials() -> None:
    missing = _config()
    missing["Primary_Reader_API"].pop("api_key")
    valid, messages = validate_all_config(
        missing,
        required_provider_sections=_REQUIRED,
    )
    assert valid is False
    assert any("Primary_Reader_API" in message and "api_key" in message for message in messages)

    blank = _config()
    blank["Primary_Reader_API"]["api_key"] = ""
    valid, messages = validate_all_config(
        blank,
        required_provider_sections=_REQUIRED,
    )
    assert valid is False
    assert any("Primary_Reader_API" in message and "api_key" in message for message in messages)
