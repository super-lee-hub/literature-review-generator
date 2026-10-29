from __future__ import annotations

from runtime.provider_runtime import ProviderAggregateBudgetV2
from runtime.run_budget_binding import resolve_run_provider_budget
from services.configuration_service import default_config_sections
from services.stage1_output_budget import stage1_output_budget_sequence


def test_documented_default_covers_effective_stage1_output_retry_ceiling() -> None:
    config = default_config_sections()
    stage1_settings = config["Stage1_Input"]
    primary_reader = config["Primary_Reader_API"]

    assert primary_reader["max_output_tokens"] == "32000"
    assert stage1_output_budget_sequence(
        "synthesis", stage1_settings, provider_config=primary_reader,
    ) == (64_000, 128_000)

    budget, authority = resolve_run_provider_budget({}, config)

    assert authority == "documented_default"
    assert budget.max_provider_calls_total == 24
    assert budget.max_output_tokens_total == 24 * 128_000
    assert budget.max_retry_attempts_total == 2
    assert budget.max_wall_seconds == 24 * 900


def test_stage1_ceiling_uses_an_active_backup_reader_when_primary_is_unconfigured() -> None:
    config = {
        "OutlineStability": {"max_provider_calls": "3"},
        "Runtime": {"transport_retries": "1"},
        "Primary_Reader_API": {"model": "", "max_output_tokens": "32000"},
        "Backup_Reader_API": {
            "model": "controlled-reader",
            "provider_max_output_tokens": "20000",
            "max_output_tokens": "4096",
            "total_timeout_seconds": "75",
        },
        "Writer_API": {
            "model": "writer",
            "max_output_tokens": "8192",
            "total_timeout_seconds": "90",
        },
        "Stage1_Input": {
            "stage1_visual_scan_max_output_tokens": "3000",
            "stage1_synthesis_max_output_tokens": "9000",
            "stage1_length_retry_max_attempts": "1",
            "stage1_length_retry_ceiling_tokens": "16000",
        },
    }

    budget, _authority = resolve_run_provider_budget({}, config)

    assert stage1_output_budget_sequence(
        "synthesis", config["Stage1_Input"],
        provider_config=config["Backup_Reader_API"],
    ) == (9000, 16000)
    assert budget.max_provider_calls_total == 3
    assert budget.max_output_tokens_total == 3 * 16000
    assert budget.max_retry_attempts_total == 1
    assert budget.max_wall_seconds == 3 * 90


def test_unconfigured_reader_does_not_apply_stage1_defaults_to_another_route() -> None:
    config = {
        "OutlineStability": {"max_provider_calls": "4"},
        "Runtime": {"transport_retries": "2"},
        "Primary_Reader_API": {"model": "", "max_output_tokens": "32000"},
        "Backup_Reader_API": {"model": "", "max_output_tokens": "16000"},
        "Writer_API": {
            "model": "writer",
            "max_output_tokens": "8192",
            "total_timeout_seconds": "90",
        },
        "Stage1_Input": {
            "stage1_synthesis_max_output_tokens": "1000000",
            "stage1_length_retry_ceiling_tokens": "2000000",
        },
    }

    budget, _authority = resolve_run_provider_budget({}, config)

    assert budget.max_provider_calls_total == 4
    assert budget.max_output_tokens_total == 4 * 8192
    assert budget.max_retry_attempts_total == 2
    assert budget.max_wall_seconds == 4 * 90


def test_explicit_zero_v2_budget_is_unchanged_by_stage1_defaults() -> None:
    config = default_config_sections()
    explicit = {
        "schema_version": ProviderAggregateBudgetV2.SCHEMA_VERSION,
        "max_provider_calls_total": 0,
        "max_output_tokens_total": 0,
        "max_retry_attempts_total": 0,
        "max_wall_seconds": 0,
    }

    budget, authority = resolve_run_provider_budget(
        {"provider_budget": explicit}, config,
    )

    assert budget == ProviderAggregateBudgetV2.from_mapping(explicit)
    assert budget.to_dict() == explicit
    assert authority == "explicit_runtime_spec"


def test_documented_default_remains_finite_without_configured_routes() -> None:
    budget, authority = resolve_run_provider_budget({}, {})

    assert authority == "documented_default"
    assert budget == ProviderAggregateBudgetV2(
        max_provider_calls_total=24,
        max_output_tokens_total=24 * 32_000,
        max_retry_attempts_total=2,
        max_wall_seconds=24 * 600,
    )
