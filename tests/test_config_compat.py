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
    assert settings.repair_policy == "report_only"


def test_read_validation_settings_reads_repair_policy() -> None:
    settings = read_validation_settings({"Validation": {"repair_policy": "auto_safe"}})

    assert settings.repair_policy == "auto_safe"


def test_read_validation_settings_rejects_invalid_repair_policy() -> None:
    try:
        read_validation_settings({"Validation": {"repair_policy": "auto-ish"}})
    except ValueError as exc:
        assert "Invalid [Validation].repair_policy" in str(exc)
    else:
        raise AssertionError("invalid repair_policy should fail loudly")


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
    assert normalized["Validation"]["repair_policy"] == "report_only"
    assert normalized["Performance"]["enable_stage1_validation"] == "true"
    assert normalized["Performance"]["enable_stage2_validation"] == "false"


def test_apply_validation_compat_sections_reads_legacy_checkpoint_flag_from_performance() -> None:
    normalized = apply_validation_compat_sections(
        {
            "Performance": {
                "keep_checkpoints_after_completion": "true",
            }
        }
    )

    assert normalized["Validation"]["keep_checkpoints_after_completion"] == "true"


def test_apply_validation_compat_sections_preserves_validation_extensions() -> None:
    normalized = apply_validation_compat_sections(
        {
            "Performance": {
                "enable_stage2_validation": "true",
            },
            "Validation": {
                "max_workers": "4",
                "evidence_resolver_enabled": "true",
            },
        }
    )

    assert normalized["Validation"]["stage2_enabled"] == "true"
    assert normalized["Validation"]["max_workers"] == "4"
    assert normalized["Validation"]["evidence_resolver_enabled"] == "true"


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
    assert view.repair_policy() == "report_only"
    assert config["Validation"]["stage1_enabled"] == "true"
    assert config["Performance"]["enable_stage1_validation"] == "true"
