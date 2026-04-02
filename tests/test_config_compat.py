from services.config_compat import CompatConfigView, apply_validation_compat_sections, read_validation_settings


def test_read_validation_settings_prefers_validation_section() -> None:
    config = {
        "Performance": {
            "enable_stage1_validation": "false",
            "enable_stage2_validation": "false",
        },
        "Validation": {
            "stage1_enabled": "true",
            "stage2_enabled": "false",
            "keep_checkpoints_after_completion": "true",
        },
    }

    settings = read_validation_settings(config)

    assert settings.stage1_enabled is True
    assert settings.stage2_enabled is False
    assert settings.keep_checkpoints_after_completion is True


def test_apply_validation_compat_sections_mirrors_legacy_performance_flags() -> None:
    normalized = apply_validation_compat_sections(
        {
            "Performance": {
                "enable_stage1_validation": "true",
                "enable_stage2_validation": "false",
            }
        }
    )

    assert normalized["Validation"]["stage1_enabled"] == "true"
    assert normalized["Validation"]["stage2_enabled"] == "false"
    assert normalized["Performance"]["enable_stage1_validation"] == "true"
    assert normalized["Performance"]["enable_stage2_validation"] == "false"


def test_compat_config_view_updates_raw_config_in_place() -> None:
    config = {
        "Performance": {
            "enable_stage1_validation": "false",
            "enable_stage2_validation": "true",
        },
        "Validation": {
            "stage1_enabled": "true",
            "stage2_enabled": "true",
            "keep_checkpoints_after_completion": "false",
        },
    }

    view = CompatConfigView.from_config(config)

    assert view.stage1_validation_enabled() is True
    assert view.stage2_validation_enabled() is True
    assert config["Validation"]["stage1_enabled"] == "true"
    assert config["Performance"]["enable_stage1_validation"] == "true"
