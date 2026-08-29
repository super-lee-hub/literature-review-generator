"""Regression tests for the legacy -> current config migration.

The runtime loader is fail-closed, so a legacy config is rejected before any
legacy handling can run. These tests pin the migration that has to happen
*before* validation, and they hold two properties that matter more than any
individual rewrite:

1. every produced config passes ``validate_config_keys``;
2. the migration is idempotent -- running it twice changes nothing.
"""

from __future__ import annotations

import configparser

import pytest

from services.config_migration import (
    VISION_PRIMARY_MODEL,
    migrate_config_text,
)
from services.settings import CONFIG_SCHEMA_VERSION, validate_config_keys


def _parse(text: str) -> dict[str, dict[str, str]]:
    parser = configparser.ConfigParser()
    parser.read_string(text)
    return {section: dict(parser.items(section)) for section in parser.sections()}


def _assert_valid_and_idempotent(migrated: str) -> None:
    errors = validate_config_keys(_parse(migrated))
    assert not errors, errors
    second, second_report = migrate_config_text(migrated)
    assert second == migrated, "migration is not idempotent"
    assert second_report.changed is False


def test_max_tokens_is_renamed_to_max_output_tokens() -> None:
    text = "[Primary_Reader_API]\nmodel = m1\nmax_tokens = 4000\n"
    migrated, report = migrate_config_text(text)

    assert "max_output_tokens = 4000" in migrated
    assert "max_tokens" not in migrated
    assert any("renamed" in change for change in report.changes)
    _assert_valid_and_idempotent(migrated)


def test_existing_max_output_tokens_wins_over_legacy_max_tokens() -> None:
    text = "[Writer_API]\nmax_output_tokens = 9000\nmax_tokens = 4000\n"
    migrated, report = migrate_config_text(text)

    parsed = _parse(migrated)
    assert parsed["Writer_API"]["max_output_tokens"] == "9000"
    assert "max_tokens" not in parsed["Writer_API"]
    assert any("dropped" in change for change in report.changes)
    _assert_valid_and_idempotent(migrated)


def test_empty_max_output_tokens_placeholder_is_filled_not_duplicated() -> None:
    """A present-but-empty target must be filled, not shadowed by a duplicate."""

    text = "[Free_Mode_API]\nmax_output_tokens = \nmax_tokens = 4000\n"
    migrated, report = migrate_config_text(text)

    parsed = _parse(migrated)
    assert parsed["Free_Mode_API"]["max_output_tokens"] == "4000"
    assert any("filled" in change for change in report.changes)
    _assert_valid_and_idempotent(migrated)


def test_dead_retry_sections_are_removed() -> None:
    text = (
        "[Runtime]\nmax_workers = 5\n"
        "[Retry_Settings]\nmax_retry_rounds = 0\n"
        "[Stage2_Retry]\nenabled = true\nmax_retry_rounds = 2\n"
    )
    migrated, report = migrate_config_text(text)

    parsed = _parse(migrated)
    assert "Retry_Settings" not in parsed
    assert "Stage2_Retry" not in parsed
    assert parsed["Runtime"]["max_workers"] == "5"
    assert sum("removed dead legacy section" in c for c in report.changes) == 2
    _assert_valid_and_idempotent(migrated)


def test_test_dev_fixture_mode_is_removed() -> None:
    text = "[Outline]\ncandidate_count = 5\ntest_dev_fixture_mode = false\n"
    migrated, _ = migrate_config_text(text)

    assert "test_dev_fixture_mode" not in migrated
    assert _parse(migrated)["Outline"]["candidate_count"] == "5"
    _assert_valid_and_idempotent(migrated)


def test_config_schema_is_bumped_to_current() -> None:
    migrated, report = migrate_config_text("[Application]\nconfig_schema = 3\n")

    assert _parse(migrated)["Application"]["config_schema"] == str(CONFIG_SCHEMA_VERSION)
    assert any("config_schema" in change for change in report.changes)
    _assert_valid_and_idempotent(migrated)


def test_missing_config_schema_is_added() -> None:
    migrated, _ = migrate_config_text("[Paths]\noutput_path = ./output\n")

    assert _parse(migrated)["Application"]["config_schema"] == str(CONFIG_SCHEMA_VERSION)
    _assert_valid_and_idempotent(migrated)


def test_legacy_default_primary_is_promoted_to_vision() -> None:
    text = "[Primary_Reader_API]\nmodel = deepseek-v4-pro\nmax_tokens = 4000\n"
    migrated, report = migrate_config_text(text)

    assert _parse(migrated)["Primary_Reader_API"]["model"] == VISION_PRIMARY_MODEL
    assert any("promoted" in change for change in report.changes)
    _assert_valid_and_idempotent(migrated)


def test_custom_primary_model_is_left_alone() -> None:
    """A deliberate model choice must survive; only the old default is rewritten."""

    text = "[Primary_Reader_API]\nmodel = my-finetuned-model\n"
    migrated, report = migrate_config_text(text)

    assert _parse(migrated)["Primary_Reader_API"]["model"] == "my-finetuned-model"
    assert not any("promoted" in change for change in report.changes)
    _assert_valid_and_idempotent(migrated)


def test_vision_promotion_can_be_disabled() -> None:
    text = "[Primary_Reader_API]\nmodel = deepseek-v4-pro\n"
    migrated, _ = migrate_config_text(text, promote_vision_primary=False)

    assert _parse(migrated)["Primary_Reader_API"]["model"] == "deepseek-v4-pro"


def test_api_parameters_are_relocated_into_provider_sections() -> None:
    text = (
        "[Outline_API]\nmodel = m\n"
        "[API_Parameters]\noutline_max_tokens = 7000\ntimeout_seconds = 600\n"
    )
    migrated, report = migrate_config_text(text)

    parsed = _parse(migrated)
    assert "API_Parameters" not in parsed
    assert parsed["Outline_API"]["max_output_tokens"] == "7000"
    assert parsed["Outline_API"]["total_timeout_seconds"] == "600"
    assert any("relocating" in change for change in report.changes)
    _assert_valid_and_idempotent(migrated)


def test_api_parameters_never_overwrite_explicit_values() -> None:
    text = (
        "[Writer_API]\nmax_output_tokens = 12000\n"
        "[API_Parameters]\nwriter_max_tokens = 7000\n"
    )
    migrated, _ = migrate_config_text(text)

    assert _parse(migrated)["Writer_API"]["max_output_tokens"] == "12000"
    _assert_valid_and_idempotent(migrated)


def test_unmappable_api_parameters_are_reported_not_guessed() -> None:
    """Unmapped keys are preserved for the operator, not silently discarded."""

    text = "[API_Parameters]\nconcept_max_tokens = 3000\n"
    migrated, report = migrate_config_text(text)

    assert any("concept_max_tokens" in warning for warning in report.warnings)
    # The section itself must not survive: it is not valid in the current schema.
    assert "API_Parameters" not in _parse(migrated)
    # ...but the value must still be there, as a comment, and must not break the
    # parser or the strict key validator.
    assert "# concept_max_tokens = 3000" in migrated
    _assert_valid_and_idempotent(migrated)


def test_unmappable_api_parameters_can_be_dropped_explicitly() -> None:
    text = "[API_Parameters]\nconcept_max_tokens = 3000\n"
    migrated, report = migrate_config_text(text, unknown_legacy="drop")

    assert "concept_max_tokens" not in migrated
    assert "API_Parameters" not in _parse(migrated)
    _assert_valid_and_idempotent(migrated)


def test_unknown_legacy_rejects_an_invalid_mode() -> None:
    with pytest.raises(ValueError):
        migrate_config_text("[Paths]\noutput_path = ./out\n", unknown_legacy="guess")


def test_relocated_value_does_not_collide_with_legacy_max_tokens() -> None:
    """Regression: relocation appends at the end of a section, i.e. *after*
    max_tokens in line order. Deciding the rename against the partially built
    output used to miss it and emit a duplicate max_output_tokens key.
    """

    text = (
        "[Primary_Reader_API]\n"
        "model = deepseek-v4-pro\n"
        "max_tokens = 4000\n"
        "[API_Parameters]\n"
        "primary_max_tokens = 9000\n"
    )
    migrated, _ = migrate_config_text(text)

    parsed = _parse(migrated)
    # The relocated value wins; the legacy key must not also become a second
    # max_output_tokens.
    assert parsed["Primary_Reader_API"]["max_output_tokens"] == "4000"
    _assert_valid_and_idempotent(migrated)


def test_comments_and_blank_lines_survive() -> None:
    text = "# top comment\n\n[Runtime]\n# inner comment\nmax_workers = 5\n"
    migrated, _ = migrate_config_text(text)

    assert "# top comment" in migrated
    assert "# inner comment" in migrated
    _assert_valid_and_idempotent(migrated)


def test_already_current_config_is_left_untouched() -> None:
    text = (
        f"[Application]\nconfig_schema = {CONFIG_SCHEMA_VERSION}\n"
        "[Writer_API]\nmodel = m\nmax_output_tokens = 8000\n"
    )
    migrated, report = migrate_config_text(text)

    assert migrated == text
    assert report.changed is False


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "[Application]\n",
        "[OutlineModels]\noutline_model = Outline_API\n",
    ],
)
def test_degenerate_inputs_do_not_crash(raw: str) -> None:
    migrated, _ = migrate_config_text(raw)
    _assert_valid_and_idempotent(migrated)
