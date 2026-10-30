"""CLI budget compatibility coverage for persisted V1 and strict V2 budgets."""

from __future__ import annotations

import argparse

import pytest

from runtime.release_acceptance import ReleaseAcceptanceSpec
from scripts.release_acceptance import _effective_budget


def _command_args(*, retry_ceiling: int = 2) -> argparse.Namespace:
    return argparse.Namespace(
        max_provider_calls_total=24,
        max_output_tokens_total=5_000_000,
        max_retry_attempts_total=retry_ceiling,
        max_wall_seconds=900,
    )


def test_effective_v2_budget_preserves_zero_retry_ceiling_and_schema() -> None:
    acceptance_spec = ReleaseAcceptanceSpec.from_mapping(
        {
            "budget": {
                "schema_version": "provider-aggregate-budget-contract-v2",
                "max_provider_calls_total": 12,
                "max_output_tokens_total": 100_000,
                "max_retry_attempts_total": 0,
                "max_wall_seconds": 600,
            }
        }
    )

    budget = _effective_budget({}, _command_args(retry_ceiling=0), acceptance_spec)

    assert budget == {
        "schema_version": "provider-aggregate-budget-contract-v2",
        "max_provider_calls_total": 12,
        "max_output_tokens_total": 100_000,
        "max_retry_attempts_total": 0,
        "max_wall_seconds": 600,
    }


def test_effective_legacy_v1_budget_keeps_wire_shape_and_enforces_limits() -> None:
    acceptance_spec = ReleaseAcceptanceSpec.from_mapping(
        {
            "budget": {
                "max_provider_calls_total": 12,
                "max_output_tokens_total": 100_000,
                "max_retry_attempts_total": 1,
                "max_wall_seconds": 600,
            }
        }
    )

    budget = _effective_budget({}, _command_args(retry_ceiling=1), acceptance_spec)

    assert budget == {
        "max_provider_calls_total": 12,
        "max_output_tokens_total": 100_000,
        "max_retry_attempts_total": 1,
        "max_wall_seconds": 600,
    }
    assert "schema_version" not in budget

    with pytest.raises(ValueError, match="max_retry_attempts_total"):
        _effective_budget({}, _command_args(retry_ceiling=0), acceptance_spec)
