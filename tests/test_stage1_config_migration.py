from __future__ import annotations

from services.configuration_service import ensure_config_sections


def test_legacy_default_migrates_reader_and_validator_but_preserves_custom_reader() -> None:
    migrated = ensure_config_sections(
        {
            "Primary_Reader_API": {
                "model": "deepseek-v4-pro",
                "api_base": "https://api.deepseek.com",
                "provider_family": "deepseek",
            },
            "Validator_API": {
                "model": "deepseek-v4-pro",
                "api_base": "https://api.deepseek.com",
                "provider_family": "deepseek",
            },
        }
    )
    assert migrated["Primary_Reader_API"]["model"] == "deepseek-v4-flash-vision-exp"
    assert migrated["Backup_Reader_API"]["model"] == "deepseek-v4-flash"
    assert migrated["Validator_API"]["model"] == "deepseek-v4-flash"

    custom = ensure_config_sections(
        {"Primary_Reader_API": {"model": "my-reader", "api_base": "https://reader.example"}}
    )
    assert custom["Primary_Reader_API"]["model"] == "my-reader"
