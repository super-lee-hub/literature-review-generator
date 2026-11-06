"""Strict, test-only scale admission and independent Stage 1 seed limits.

These helpers validate local fixture allowances; they neither change product
configuration nor authorize provider spending. Seed floors cover only the
initial Reader requests, not a guarantee that an entire run can complete.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from runtime.provider_runtime import ProviderAggregateBudgetV2

_ADMISSION_ENV = "RUN_FIRST_SCALE_ADMISSION_JSON"
_SEED_JSON_ENV = "RUN_FIRST_SCALE_SEED_BUDGET_JSON"
_SEED_FILE_ENV = "RUN_FIRST_SCALE_SEED_BUDGET_FILE"


def _json_object(raw_json: str, *, label: str) -> dict[str, Any]:
    def unique_fields(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        fields: dict[str, Any] = {}
        for key, value in pairs:
            if key in fields:
                raise ValueError(f"{label} contains duplicate field {key}")
            fields[key] = value
        return fields

    def reject_constant(value: str) -> Any:
        raise ValueError(f"{label} contains non-finite JSON number {value}")

    try:
        payload = json.loads(
            raw_json, object_pairs_hook=unique_fields, parse_constant=reject_constant
        )
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} must be valid JSON") from exc
    if not isinstance(payload, dict):
        raise TypeError(f"{label} must be a JSON object")
    return payload


def _positive_integer(value: Any, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


def validate_scale_admission_overrides(raw_json: str | None) -> dict[str, int]:
    """Accept only an explicit positive max_estimated_total_tokens override."""
    if raw_json is None or not raw_json.strip():
        return {}
    payload = _json_object(raw_json, label=_ADMISSION_ENV)
    if set(payload) != {"max_estimated_total_tokens"}:
        raise ValueError(
            f"{_ADMISSION_ENV} must contain only max_estimated_total_tokens"
        )
    value = _positive_integer(
        payload["max_estimated_total_tokens"], label="max_estimated_total_tokens"
    )
    return {"max_estimated_total_tokens": value}


def validate_scale_seed_budget(
    payload: Mapping[str, Any] | ProviderAggregateBudgetV2 | None,
    *,
    paper_count: int,
    reader_initial_output_cap: int,
) -> ProviderAggregateBudgetV2 | None:
    """Validate the separate seed job without altering any warm-run budget."""
    count = _positive_integer(paper_count, label="paper_count")
    initial_cap = _positive_integer(
        reader_initial_output_cap, label="reader_initial_output_cap"
    )
    if payload is None:
        return None
    budget = ProviderAggregateBudgetV2.from_mapping(
        payload.to_dict() if isinstance(payload, ProviderAggregateBudgetV2) else payload
    )
    if budget.max_provider_calls_total < count:
        raise ValueError("Stage 1 seed call allowance must cover paper_count")
    if budget.max_output_tokens_total < count * initial_cap:
        raise ValueError(
            "Stage 1 seed output allowance must cover paper_count times "
            "reader_initial_output_cap"
        )
    if budget.max_retry_attempts_total != 0:
        raise ValueError("Stage 1 seed retry allowance must be zero")
    if budget.max_wall_seconds <= 0:
        raise ValueError("Stage 1 seed wall allowance must be positive and finite")
    return budget


def resolve_scale_test_budgets(
    environ: Mapping[str, str],
    *,
    paper_count: int,
    reader_initial_output_cap: int,
    default_seed_budget: Mapping[str, Any] | ProviderAggregateBudgetV2 | None = None,
) -> tuple[dict[str, int], ProviderAggregateBudgetV2 | None]:
    """Read explicit test controls; absence preserves the supplied default.

    JSON and file seed controls are mutually exclusive. A file is read only
    when explicitly named; no files, environment variables, or ledgers are
    written. Callers retain independent ownership of the warm-run allowance.
    """
    overrides = validate_scale_admission_overrides(environ.get(_ADMISSION_ENV))
    raw_seed = environ.get(_SEED_JSON_ENV, "").strip()
    seed_file = environ.get(_SEED_FILE_ENV, "").strip()
    if raw_seed and seed_file:
        raise ValueError(f"set only one of {_SEED_JSON_ENV} or {_SEED_FILE_ENV}")
    payload: Mapping[str, Any] | ProviderAggregateBudgetV2 | None = default_seed_budget
    if raw_seed:
        payload = _json_object(raw_seed, label=_SEED_JSON_ENV)
    elif seed_file:
        try:
            raw = Path(seed_file).expanduser().read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise ValueError(f"{_SEED_FILE_ENV} is unreadable") from exc
        payload = _json_object(raw, label=_SEED_FILE_ENV)
    seed_budget = validate_scale_seed_budget(
        payload,
        paper_count=paper_count,
        reader_initial_output_cap=reader_initial_output_cap,
    )
    return overrides, seed_budget
