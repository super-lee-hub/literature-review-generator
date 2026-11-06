from __future__ import annotations

import configparser
import json
from pathlib import Path
from typing import Any

import pytest

from runtime.control_plane import ReviewControlPlane
from runtime.provider_runtime import (
    ProviderAggregateBudgetV2,
    ProviderBudgetController,
    bind_provider_run_budget,
    project_shared_retry_slots_v1,
)
from runtime.run_budget_binding import resolve_run_provider_budget
from tests.test_outline_v3_semantic_execution import _executor, _summary


def _write_config(path: Path, *, retries: int = 2, calls: int = 24) -> None:
    parser = configparser.ConfigParser(interpolation=None)
    api = {
        "api_key": "local-fixture-only",
        "model": "provider-free-plan-model",
        "provider_family": "generic",
        "endpoint_type": "chat_completions",
        "api_base": "http://127.0.0.1:1/v1",
        "max_context_tokens": "16000",
        "max_output_tokens": "1024",
        "reasoning_reserve_tokens": "256",
        "safety_margin_tokens": "256",
    }
    parser.read_dict(
        {
            "Paths": {"output_path": str(path.parent / "output")},
            "Outline_API": api,
            "OutlineModels": {
                "outline_model": "Outline_API",
                "relation_adjudicator_model": "Outline_API",
                "structure_critic_model": "Outline_API",
                "coverage_critic_model": "Outline_API",
                "evidence_critic_model": "Outline_API",
                "arbitrator_model": "Outline_API",
            },
            "Outline": {"candidate_count": "1"},
            "OutlineStability": {
                "mode": "off",
                "max_provider_calls": str(calls),
                "max_source_prompt_tokens": "32000",
            },
            "Runtime": {"transport_retries": str(retries)},
        }
    )
    with path.open("w", encoding="utf-8") as stream:
        parser.write(stream)


def _write_summaries(path: Path) -> None:
    path.write_text(
        json.dumps(
            [
                _summary("paper-a", "Paper A", "Treatment improved the short-term outcome."),
                _summary("paper-b", "Paper B", "Treatment improved the long-term outcome."),
            ]
        ),
        encoding="utf-8",
    )


def _prospective_budget(*, calls: int = 200, retries: int = 2) -> dict[str, Any]:
    return ProviderAggregateBudgetV2(
        max_provider_calls_total=calls,
        max_output_tokens_total=1_000_000,
        max_retry_attempts_total=retries,
        max_wall_seconds=3_600,
    ).to_dict()


def test_chunk_plan_cli_preserves_explicit_zero_budget_without_authorizing_execution(tmp_path, capsys):
    import reviewctl

    summaries = tmp_path / "summaries.json"
    config = tmp_path / "config.ini"
    budget_file = tmp_path / "budget.json"
    _write_summaries(summaries)
    _write_config(config)
    budget_file.write_text(json.dumps(_prospective_budget(calls=0, retries=0)), encoding="utf-8")
    assert reviewctl.main([
        "--config", str(config), "chunk-plan", "--summary-file", str(summaries),
        "--provider-budget-file", str(budget_file), "--json",
    ]) == 0
    payload = json.loads(capsys.readouterr().out)
    preflight = payload["semantic_route_preflight_summary"]
    assert preflight["max_provider_calls"] == 0
    assert preflight["aggregate_retry_pool_remaining"] == 0
    assert preflight["retry_projection_scope"] == "prospective_run_resources"
    assert preflight["preflight_status"] == "rejected"
    assert payload["provider_posts_emitted"] == 0
    assert not list(tmp_path.rglob("*budget*state*"))


def test_chunk_plan_uses_prospective_default_retry_pool_without_binding_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    summaries = tmp_path / "summaries.json"
    config = tmp_path / "config.ini"
    _write_summaries(summaries)
    _write_config(config, retries=2, calls=24)
    # Planning must use the explicit run-budget resolver, not instantiate a
    # controller from inherited compatibility environment values.
    monkeypatch.setenv("AUTO_GENERATE_ACCEPTANCE_BUDGET_JSON", "not-json")
    monkeypatch.setenv("AUTO_GENERATE_ACCEPTANCE_BUDGET_STATE_PATH", str(tmp_path / "ambient.json"))

    result = ReviewControlPlane(repo_root=tmp_path).chunk_plan(
        [summaries], config_path=config, candidate_count=1
    )

    preflight = result["semantic_route_preflight_summary"]
    assert result["provider_posts_emitted"] == 0
    assert preflight["retry_projection_scope"] == "prospective_run_resources"
    assert preflight["retry_projection_budget_source"] == "documented_default"
    assert preflight["max_provider_calls"] == 24
    assert preflight["aggregate_retry_pool_remaining"] == 2
    assert preflight["estimated_provider_physical_attempts_upper_bound"] == (
        preflight["estimated_provider_calls"]
        + preflight["retry_attempts_upper_bound"]
    )
    assert preflight["retry_attempts_upper_bound"] == min(
        preflight["configured_retry_slots_upper_bound"], 2
    )
    assert preflight["aggregate_retry_pool_applied_to"] == "physical_attempts_only"
    assert preflight["token_retry_reserve_scope"] == "per_call_conservative_ceiling"
    assert preflight["cost_retry_reserve_scope"] == "per_call_conservative_ceiling"
    assert not (tmp_path / "ambient.json").exists()


def test_chunk_plan_explicit_budget_overrides_config_call_cap_and_preserves_zero_retries(
    tmp_path: Path,
) -> None:
    summaries = tmp_path / "summaries.json"
    config = tmp_path / "config.ini"
    _write_summaries(summaries)
    _write_config(config, retries=8, calls=24)
    budget = _prospective_budget(calls=200, retries=0)

    default_plan = ReviewControlPlane(repo_root=tmp_path).chunk_plan(
        [summaries], config_path=config, candidate_count=1,
        prospective_provider_budget=budget,
    )
    lower_physical_limit = ReviewControlPlane(repo_root=tmp_path).chunk_plan(
        [summaries], config_path=config, candidate_count=1,
        prospective_provider_budget=budget, physical_call_limit=30,
    )

    preflight = default_plan["semantic_route_preflight_summary"]
    assert default_plan["provider_posts_emitted"] == 0
    assert preflight["retry_projection_scope"] == "prospective_run_resources"
    assert preflight["retry_projection_budget_source"] == "explicit_runtime_spec"
    assert preflight["max_provider_calls"] == 200
    assert preflight["aggregate_retry_pool_remaining"] == 0
    assert preflight["retry_attempts_upper_bound"] == 0
    assert preflight["estimated_provider_physical_attempts_upper_bound"] == (
        preflight["estimated_provider_calls"]
    )
    assert lower_physical_limit["semantic_route_preflight_summary"]["max_provider_calls"] == 30


def test_active_durable_retry_pool_takes_precedence_over_smaller_prospective_pool(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    summaries = tmp_path / "summaries.json"
    config = tmp_path / "config.ini"
    _write_summaries(summaries)
    _write_config(config, retries=8, calls=24)
    active_budget = ProviderAggregateBudgetV2(
        max_provider_calls_total=200,
        max_output_tokens_total=2_000_000,
        max_retry_attempts_total=4,
        max_wall_seconds=3_600,
    )
    active_controller = ProviderBudgetController(active_budget)
    active_controller.bind_state_path(
        tmp_path / "active_provider_budget.json",
        acceptance_run_id="",
        state_started=False,
    )
    monkeypatch.setenv("AUTO_GENERATE_ACCEPTANCE_BUDGET_JSON", "not-json")
    prospective = _prospective_budget(calls=200, retries=1)

    with bind_provider_run_budget(active_controller):
        result = ReviewControlPlane(repo_root=tmp_path).chunk_plan(
            [summaries], config_path=config, candidate_count=1,
            prospective_provider_budget=prospective,
        )

    preflight = result["semantic_route_preflight_summary"]
    assert result["provider_posts_emitted"] == 0
    assert preflight["retry_projection_scope"] == "active_run"
    assert preflight["retry_projection_budget_source"] == "bound_durable_controller"
    assert preflight["aggregate_retry_pool_remaining"] == 4
    assert preflight["retry_attempts_upper_bound"] == min(
        preflight["configured_retry_slots_upper_bound"], 4
    )


def test_executor_without_run_budget_labels_projection_per_call_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AUTO_GENERATE_ACCEPTANCE_BUDGET_JSON", "not-json")
    executor = _executor(
        tmp_path,
        provider=None,
        stability_mode="off",
        max_provider_calls=24,
        candidate_count=1,
    )

    executor._preflight_stability_budget()

    preflight = executor.stability_preflight
    assert preflight["retry_projection_scope"] == "per_call_only"
    assert preflight["retry_projection_budget_source"] == ""
    assert preflight["aggregate_retry_pool_remaining"] is None
    assert preflight["estimated_provider_physical_attempts_upper_bound"] == (
        preflight["estimated_provider_calls"]
        + preflight["retry_attempts_upper_bound"]
    )


def test_prospective_shared_pool_projects_151_logical_calls_as_153_attempts() -> None:
    budget, source = resolve_run_provider_budget(
        {},
        {
            "OutlineStability": {"max_provider_calls": "24"},
            "Runtime": {"transport_retries": "2"},
        },
    )
    configured_retry_slots = 151 * 2
    prospective_retry_slots = min(
        configured_retry_slots,
        budget.max_retry_attempts_total,
    )
    projection = project_shared_retry_slots_v1(
        151,
        configured_retry_slots_upper_bound=prospective_retry_slots,
    )

    assert source == "documented_default"
    assert budget.max_provider_calls_total == 24
    assert budget.max_retry_attempts_total == 2
    assert projection.logical_calls > budget.max_provider_calls_total
    assert projection.physical_attempts_upper_bound == 153
    assert projection.physical_attempts_upper_bound > budget.max_provider_calls_total
    assert projection.retry_attempts_upper_bound == 2
