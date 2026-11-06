from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from runtime.provider_runtime import (
    ProviderAggregateBudgetV2,
    ProviderRuntimeContractError,
)
from tests.run_first_scale_budgets import (
    resolve_scale_test_budgets,
    validate_scale_admission_overrides,
    validate_scale_seed_budget,
)


def _seed(*, calls: int = 600, output: int = 600 * 64_000) -> dict[str, Any]:
    return ProviderAggregateBudgetV2(calls, output, 0, 1800).to_dict()


@pytest.mark.parametrize("raw", [None, "", "  "])
def test_absent_admission_keeps_product_defaults(raw: str | None) -> None:
    assert validate_scale_admission_overrides(raw) == {}


def test_admission_allows_only_a_finite_positive_integer() -> None:
    assert validate_scale_admission_overrides(
        '{"max_estimated_total_tokens": 18000000}'
    ) == {"max_estimated_total_tokens": 18_000_000}


@pytest.mark.parametrize(
    "raw",
    [
        "{}",
        "[]",
        "null",
        "broken",
        '{"max_estimated_total_tokens": 1, "max_provider_calls": 2000}',
        '{"max_estimated_total_tokens": 1, "max_estimated_total_tokens": 2}',
        '{"max_estimated_total_tokens": true}',
        '{"max_estimated_total_tokens": 0}',
        '{"max_estimated_total_tokens": -1}',
        '{"max_estimated_total_tokens": 1.5}',
        '{"max_estimated_total_tokens": "100"}',
        '{"max_estimated_total_tokens": 1e999}',
        '{"max_estimated_total_tokens": NaN}',
        '{"max_estimated_total_tokens": Infinity}',
    ],
)
def test_invalid_admission_is_rejected_without_coercion(raw: str) -> None:
    with pytest.raises((TypeError, ValueError)):
        validate_scale_admission_overrides(raw)


@pytest.mark.parametrize("count", [10, 63, 64, 100, 200, 500, 600])
def test_seed_accepts_exact_initial_reader_floors(count: int) -> None:
    original = _seed(calls=count, output=count * 64_000)
    before = copy.deepcopy(original)
    budget = validate_scale_seed_budget(
        original, paper_count=count, reader_initial_output_cap=64_000
    )
    assert isinstance(budget, ProviderAggregateBudgetV2)
    assert budget.to_dict() == before
    assert original == before


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_provider_calls_total", 599),
        ("max_output_tokens_total", 600 * 64_000 - 1),
        ("max_retry_attempts_total", 1),
        ("max_wall_seconds", 0),
        ("max_wall_seconds", float("inf")),
        ("max_wall_seconds", float("nan")),
        ("max_provider_calls_total", True),
        ("max_output_tokens_total", True),
        ("max_retry_attempts_total", True),
        ("max_wall_seconds", True),
        ("max_provider_calls_total", 600.5),
    ],
)
def test_seed_rejects_underfunded_or_invalid_limits(field: str, value: Any) -> None:
    payload = _seed()
    payload[field] = value
    with pytest.raises((TypeError, ValueError, ProviderRuntimeContractError)):
        validate_scale_seed_budget(
            payload, paper_count=600, reader_initial_output_cap=64_000
        )


@pytest.mark.parametrize("missing", list(_seed()))
def test_seed_reuses_production_required_fields(missing: str) -> None:
    payload = _seed()
    del payload[missing]
    with pytest.raises(ProviderRuntimeContractError):
        validate_scale_seed_budget(
            payload, paper_count=600, reader_initial_output_cap=64_000
        )


def test_seed_rejects_unknown_fields_and_legacy_schema() -> None:
    for payload in ({**_seed(), "extra": 1}, {**_seed(), "schema_version": "v1"}):
        with pytest.raises(ProviderRuntimeContractError):
            validate_scale_seed_budget(
                payload, paper_count=600, reader_initial_output_cap=64_000
            )


@pytest.mark.parametrize("value", [False, 0, -1, 1.5, "600"])
@pytest.mark.parametrize("argument", ["paper_count", "reader_initial_output_cap"])
def test_seed_dimensions_must_be_positive_integers(argument: str, value: Any) -> None:
    arguments = {"paper_count": 600, "reader_initial_output_cap": 64_000}
    arguments[argument] = value
    with pytest.raises(ValueError):
        validate_scale_seed_budget(None, **arguments)


def test_absent_controls_have_no_seed_and_do_not_read_warm_budget() -> None:
    environ = {"RUN_FIRST_SCALE_PROVIDER_BUDGET_JSON": "invalid warm budget untouched"}
    before = dict(environ)
    assert resolve_scale_test_budgets(
        environ, paper_count=600, reader_initial_output_cap=64_000
    ) == ({}, None)
    assert environ == before


def test_compatible_default_seed_is_validated_without_mutation() -> None:
    default = _seed(calls=200, output=200 * 64_000)
    before = copy.deepcopy(default)
    overrides, budget = resolve_scale_test_budgets(
        {}, paper_count=200, reader_initial_output_cap=64_000,
        default_seed_budget=default,
    )
    assert overrides == {}
    assert budget is not None and budget.to_dict() == before
    assert default == before
    with pytest.raises(ValueError, match="call allowance"):
        resolve_scale_test_budgets(
            {}, paper_count=600, reader_initial_output_cap=64_000,
            default_seed_budget=default,
        )


def test_explicit_seed_is_independent_of_undersized_warm_default() -> None:
    warm = ProviderAggregateBudgetV2(200, 6_400_000, 0, 1800).to_dict()
    before = copy.deepcopy(warm)
    overrides, budget = resolve_scale_test_budgets(
        {
            "RUN_FIRST_SCALE_ADMISSION_JSON": '{"max_estimated_total_tokens": 18000000}',
            "RUN_FIRST_SCALE_SEED_BUDGET_JSON": json.dumps(_seed()),
        },
        paper_count=600, reader_initial_output_cap=64_000,
        default_seed_budget=warm,
    )
    assert overrides == {"max_estimated_total_tokens": 18_000_000}
    assert budget is not None and budget.max_provider_calls_total == 600
    assert budget.max_output_tokens_total == 38_400_000
    assert warm == before


def test_seed_file_is_read_only_and_equivalent_to_json(tmp_path: Path) -> None:
    budget_file = tmp_path / "seed.json"
    raw = json.dumps(_seed()).encode("utf-8")
    budget_file.write_bytes(raw)
    common = {"paper_count": 600, "reader_initial_output_cap": 64_000}
    from_file = resolve_scale_test_budgets(
        {"RUN_FIRST_SCALE_SEED_BUDGET_FILE": str(budget_file)}, **common
    )
    from_json = resolve_scale_test_budgets(
        {"RUN_FIRST_SCALE_SEED_BUDGET_JSON": raw.decode("utf-8")}, **common
    )
    assert from_file == from_json
    assert budget_file.read_bytes() == raw
    assert tuple(tmp_path.iterdir()) == (budget_file,)


def test_seed_json_and_file_are_mutually_exclusive_before_reading() -> None:
    with pytest.raises(ValueError, match="set only one"):
        resolve_scale_test_budgets(
            {
                "RUN_FIRST_SCALE_SEED_BUDGET_JSON": json.dumps(_seed()),
                "RUN_FIRST_SCALE_SEED_BUDGET_FILE": "does-not-exist.json",
            },
            paper_count=600, reader_initial_output_cap=64_000,
        )


@pytest.mark.parametrize(
    "raw",
    ["[]", "null", "broken", "{}", '{"schema_version": "v1"}',
     json.dumps(_seed()).replace('"max_wall_seconds": 1800.0',
                               '"max_wall_seconds": 1800, "max_wall_seconds": 1800'),
     json.dumps(_seed()).replace("1800.0", "NaN")],
)
def test_malformed_seed_json_and_files_are_rejected(raw: str, tmp_path: Path) -> None:
    budget_file = tmp_path / "bad-seed.json"
    budget_file.write_text(raw, encoding="utf-8")
    for environ in (
        {"RUN_FIRST_SCALE_SEED_BUDGET_JSON": raw},
        {"RUN_FIRST_SCALE_SEED_BUDGET_FILE": str(budget_file)},
    ):
        with pytest.raises((TypeError, ValueError, ProviderRuntimeContractError)):
            resolve_scale_test_budgets(
                environ, paper_count=600, reader_initial_output_cap=64_000
            )


def test_missing_seed_file_is_actionable(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="RUN_FIRST_SCALE_SEED_BUDGET_FILE is unreadable"):
        resolve_scale_test_budgets(
            {"RUN_FIRST_SCALE_SEED_BUDGET_FILE": str(tmp_path / "missing.json")},
            paper_count=600, reader_initial_output_cap=64_000,
        )


def test_local_config_override_changes_only_admission_floor_and_seed_metadata_is_independent(tmp_path):
    import configparser
    from tests.test_run_first_scale_integration import _local_config
    first = tmp_path / "baseline"
    second = tmp_path / "override"
    first.mkdir()
    second.mkdir()
    baseline = _local_config(first, "http://127.0.0.1:54321/v1", provider_call_limit=200)
    effective = _local_config(second, "http://127.0.0.1:54321/v1", provider_call_limit=200,
                              admission_overrides={"max_estimated_total_tokens": 7000000})
    before = configparser.ConfigParser(interpolation=None)
    after = configparser.ConfigParser(interpolation=None)
    before.read(baseline, encoding="utf-8")
    after.read(effective, encoding="utf-8")
    changed = {(section, key) for section in before.sections() for key in before[section]
               if before[section][key] != after[section][key]}
    assert changed == {("OutlineStability", "max_estimated_total_tokens"),
                       ("Paths", "output_path"), ("Preprocess", "cache_dir")}
    for section in before.sections():
        if "api_base" in before[section]:
            assert before[section]["api_base"] == after[section]["api_base"] == "http://127.0.0.1:54321/v1"
            assert before[section]["transport_retries"] == after[section]["transport_retries"] == "0"
    assert after["OutlineStability"]["max_provider_calls"] == "200"


def test_seed_reader_floor_uses_the_shipped_stage1_input_policy():
    import configparser
    from pathlib import Path
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(Path(__file__).resolve().parents[1] / "config.ini.example", encoding="utf-8")
    assert int(parser["Stage1_Input"]["stage1_synthesis_max_output_tokens"]) == 64000


def test_local_outline_output_experiment_changes_only_its_route(tmp_path):
    import configparser

    from tests.test_run_first_scale_integration import _local_config

    baseline_dir = tmp_path / "baseline"
    experiment_dir = tmp_path / "experiment"
    baseline_dir.mkdir()
    experiment_dir.mkdir()
    before = configparser.ConfigParser(interpolation=None)
    after = configparser.ConfigParser(interpolation=None)
    before.read(_local_config(baseline_dir, "http://127.0.0.1:54321/v1"), encoding="utf-8")
    after.read(_local_config(experiment_dir, "http://127.0.0.1:54321/v1",
                             outline_output_tokens=32_000), encoding="utf-8")
    changed = {(section, key) for section in before.sections() for key in before[section]
               if before[section][key] != after[section][key]}
    assert changed == {("Outline_API", "max_output_tokens"),
                       ("Paths", "output_path"), ("Preprocess", "cache_dir")}
    assert before["Outline_API"]["max_output_tokens"] == "16000"
    assert after["Outline_API"]["max_output_tokens"] == "32000"


@pytest.mark.parametrize("invalid", [0, -1, True, False, 1.5, "32000"])
def test_local_outline_output_experiment_rejects_invalid_allowance(tmp_path, invalid):
    from tests.test_run_first_scale_integration import _local_config

    with pytest.raises(AssertionError, match="positive integer"):
        _local_config(tmp_path, "http://127.0.0.1:54321/v1", outline_output_tokens=invalid)
