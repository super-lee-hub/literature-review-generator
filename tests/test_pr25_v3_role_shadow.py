from __future__ import annotations

import configparser
import json
from pathlib import Path
from typing import Any

from runtime.control_plane import (
    ReviewControlPlane,
    _provider_free_shadow_capacity_comparisons,
)
from tests.test_outline_v3_semantic_execution import _summary


def _write_role_config(path: Path) -> dict[str, dict[str, str]]:
    parser = configparser.ConfigParser(interpolation=None)
    parser.read_dict(
        {
            "Paths": {"output_path": str(path.parent / "output")},
            "Generation_API": {
                "api_key": "local-fixture-only",
                "model": "generation-role-model",
                "provider_family": "generic",
                "endpoint_type": "chat_completions",
                "api_base": "http://127.0.0.1:1/v1",
                "max_context_tokens": "16000",
                "max_output_tokens": "1024",
                "reasoning_reserve_tokens": "256",
                "safety_margin_tokens": "256",
                "transport_retries": "0",
            },
            "Relation_API": {
                "api_key": "local-fixture-only",
                "model": "relation-role-model",
                "provider_family": "generic",
                "endpoint_type": "chat_completions",
                "api_base": "http://127.0.0.1:1/v1",
                "max_context_tokens": "64000",
                "max_output_tokens": "4096",
                "reasoning_reserve_tokens": "1024",
                "safety_margin_tokens": "512",
                "transport_retries": "0",
            },
            "Critic_API": {
                "api_key": "local-fixture-only",
                "model": "critic-role-model",
                "provider_family": "generic",
                "endpoint_type": "chat_completions",
                "api_base": "http://127.0.0.1:1/v1",
                "max_context_tokens": "32000",
                "max_output_tokens": "512",
                "reasoning_reserve_tokens": "128",
                "safety_margin_tokens": "256",
                "transport_retries": "0",
            },
            "OutlineModels": {
                "outline_model": "Generation_API",
                "relation_adjudicator_model": "Relation_API",
                "structure_critic_model": "Critic_API",
                "coverage_critic_model": "Critic_API",
                "evidence_critic_model": "Critic_API",
                "arbitrator_model": "Generation_API",
            },
            "Outline": {"candidate_count": "2", "technical_shard_target_tokens": "0"},
            "OutlineStability": {
                "mode": "off",
                "max_provider_calls": "24",
                "max_source_prompt_tokens": "32000",
            },
            "Runtime": {"transport_retries": "0"},
        }
    )
    with path.open("w", encoding="utf-8") as stream:
        parser.write(stream)
    return {
        section: dict(parser.items(section))
        for section in parser.sections()
    }


def _profile_limits(route: dict[str, Any]) -> dict[str, int]:
    return {
        key: int(route["profile_limits"][key])
        for key in ("model_context_limit", "input_budget", "max_output_tokens")
    }


def test_provider_free_capacity_comparison_keeps_runtime_limit_and_unknowns_explicit() -> None:
    comparison = _provider_free_shadow_capacity_comparisons(
        topic_call_lower_bound=33,
        logical_call_upper_bound=None,
        physical_attempt_upper_bound=None,
        actual_runtime_call_limit=24,
        actual_preflight_status="rejected",
    )

    assert [item["shadow_physical_call_limit"] for item in comparison] == [
        24, 48, 64, 80,
    ]
    assert comparison[0]["status"] == "blocked_known_topic_call_lower_bound"
    assert [item["status"] for item in comparison[1:]] == [
        "incomplete_upper_bound", "incomplete_upper_bound", "incomplete_upper_bound",
    ]
    assert all(
        item["upper_bound_completeness_status"] == "incomplete_upper_bound"
        for item in comparison[1:]
    )
    assert all(item["actual_runtime_call_limit"] == 24 for item in comparison)
    assert all(item["provider_admission_authorized"] is False for item in comparison)
    assert all(item["provider_posts_emitted"] == 0 for item in comparison)


def test_provider_free_capacity_comparison_labels_64_call_scenario_as_shadow_only() -> None:
    comparison = _provider_free_shadow_capacity_comparisons(
        topic_call_lower_bound=33,
        logical_call_upper_bound=59,
        physical_attempt_upper_bound=59,
        actual_runtime_call_limit=24,
        actual_preflight_status="rejected",
    )

    assert [item["status"] for item in comparison] == [
        "blocked_known_topic_call_lower_bound",
        "blocked_estimated_upper_bound",
        "within_shadow_capacity",
        "within_shadow_capacity",
    ]
    assert all(item["actual_runtime_call_limit"] == 24 for item in comparison)
    assert all(item["provider_admission_authorized"] is False for item in comparison)
    assert all(
        item["upper_bound_completeness_status"] == "materialized_upper_bound"
        for item in comparison
    )


def test_chunk_plan_uses_distinct_outline_role_routes_without_transport(tmp_path: Path) -> None:
    summary_path = tmp_path / "summaries.json"
    summary_path.write_text(
        json.dumps(
            [
                _summary("paper-a", "Paper A", "Treatment improved the short-term outcome."),
                _summary("paper-b", "Paper B", "Treatment improved the long-term outcome."),
            ]
        ),
        encoding="utf-8",
    )
    config_path = tmp_path / "config.ini"
    _write_role_config(config_path)

    result = ReviewControlPlane(repo_root=tmp_path).chunk_plan(
        [summary_path],
        job_id="role-route-shadow",
        candidate_count=2,
        config_path=config_path,
    )

    assert result["provider_posts_emitted"] == 0
    assert result["read_only"] is True

    preflight = result["semantic_route_preflight_summary"]
    role_routes = preflight.get("role_routes")
    assert role_routes is not None, (
        "chunk-plan exposes only one generator profile/identity today; "
        f"semantic_route_identity_hash={result.get('semantic_route_identity_hash')}"
    )

    relation_route = role_routes["relation_adjudication"]
    generation_route = role_routes["candidate_provider_generation"]
    critic_route = role_routes["structure_critique"]
    assert relation_route["config_section"] == "Relation_API"
    assert relation_route["model"] == "relation-role-model"
    assert generation_route["config_section"] == "Generation_API"
    assert generation_route["model"] == "generation-role-model"
    assert critic_route["config_section"] == "Critic_API"
    assert critic_route["model"] == "critic-role-model"

    relation_identity = preflight["relation_preflight_route"]
    assert relation_identity["role"] == "relation_adjudication"
    assert relation_identity["config_section"] == relation_route["config_section"]
    assert relation_identity["model"] == relation_route["model"]

    assert _profile_limits(relation_route) == {
        "model_context_limit": 64000,
        "input_budget": 45568,
        "max_output_tokens": 4096,
    }
    assert _profile_limits(generation_route) == {
        "model_context_limit": 16000,
        "input_budget": 11264,
        "max_output_tokens": 1024,
    }
    assert _profile_limits(critic_route) == {
        "model_context_limit": 32000,
        "input_budget": 24704,
        "max_output_tokens": 512,
    }

    generation_rows = [
        row
        for row in result["semantic_request_plan"]
        if str(row.get("node_id") or "").startswith("topic_synthesis_provider:")
    ]
    assert generation_rows
    for row in generation_rows:
        route_identity = row["route_identity"]
        assert row["role"] == "candidate_provider_generation"
        assert route_identity["config_section"] == generation_route["config_section"]
        assert route_identity["model"] == generation_route["model"]

    assert relation_route["model"] != generation_route["model"]
    assert critic_route["model"] not in {
        relation_route["model"],
        generation_route["model"],
    }
    assert result["semantic_request_plan_count_kind"] == (
        "materialized_rows_excludes_conditional_reducer_reserve"
    )
    assert result["semantic_request_calls_reserved_upper_bound"] >= result["semantic_request_plan_count"]
    assert result["semantic_cross_request_count_upper_bound"] == (
        result["semantic_cross_materialized_request_count"]
        + preflight["semantic_conditional_reducer_call_reserve"]
    )
    assert result["semantic_request_physical_attempts_upper_bound"] == (
        preflight["semantic_physical_attempts_upper_bound"]
    )
    topic_rows = [
        row
        for row in result["semantic_request_plan"]
        if str(row.get("node_id") or "").startswith("topic_synthesis_provider:batch:")
    ]
    runtime_fragment_count = sum(len(row.get("topic_fragments") or ()) for row in topic_rows)
    planner_item_count = sum(len(row.get("cross_group_fragment_plans") or ()) for row in topic_rows)
    assert runtime_fragment_count == 5
    assert result["semantic_cross_group_runtime_fragment_count"] == runtime_fragment_count
    assert result["semantic_cross_group_planner_item_count"] == planner_item_count
    assert planner_item_count == runtime_fragment_count
    assert result["semantic_cross_group_fragment_bounds_status"] == "materialized_upper_bound"
    assert result["semantic_request_upper_bound_status"] == "materialized_upper_bound"
    assert result["semantic_request_input_tokens_all_attempts_upper_bound"] == (
        preflight["semantic_input_tokens_all_attempts_upper_bound"]
    )
    shadow_rows = result["provider_free_shadow_capacity_comparison"]
    assert [item["shadow_physical_call_limit"] for item in shadow_rows] == [
        24, 48, 64, 80,
    ]
    assert all(
        item["logical_call_upper_bound"] == preflight["estimated_provider_calls"]
        and item["physical_attempt_upper_bound"]
        == preflight["estimated_provider_physical_attempts_upper_bound"]
        and item["comparison_scope"] == "outline_v3_provider_call_plan"
        and item["provider_admission_authorized"] is False
        for item in shadow_rows
    )
    assert "local-fixture-only" not in json.dumps(result)
    assert "api_base" not in json.dumps(result)


def test_three_paper_plan_does_not_reserve_unused_reducer_stage_capacity(
    tmp_path: Path,
) -> None:
    summary_path = tmp_path / "summaries.json"
    summary_path.write_text(
        json.dumps(
            [
                _summary(
                    f"paper-{index}",
                    f"Paper {index}",
                    f"Treatment improved outcome in context {index}.",
                )
                for index in range(3)
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    config_path = tmp_path / "config.ini"
    _write_role_config(config_path)

    result = ReviewControlPlane(repo_root=tmp_path).chunk_plan(
        [summary_path],
        job_id="three-paper-reducer-budget",
        candidate_count=2,
        physical_call_limit=24,
        config_path=config_path,
    )

    assert result["provider_posts_emitted"] == 0
    assert result["semantic_physical_call_limit"] == 24
    assert result["semantic_preflight_status"] == "accepted"
    preflight = result["semantic_route_preflight_summary"]
    assert preflight["semantic_conditional_reducer_call_reserve"] == 0
    assert preflight["semantic_synthesis_calls_reserved"] == (
        result["semantic_topic_batch_count"]
        + result["semantic_cross_materialized_request_count"]
        + result["semantic_global_request_count_upper_bound"]
    )
    assert result["semantic_request_calls_reserved_upper_bound"] <= 24


def test_chunk_plan_uses_runtime_smoke_default_when_mode_is_omitted(tmp_path: Path) -> None:
    summary_path = tmp_path / "summaries.json"
    summary_path.write_text(
        json.dumps([
            _summary("paper-a", "Paper A", "The first supported finding."),
            _summary("paper-b", "Paper B", "The second supported finding."),
        ]),
        encoding="utf-8",
    )
    config_path = tmp_path / "config.ini"
    _write_role_config(config_path)
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace("mode = off\n", ""),
        encoding="utf-8",
    )

    result = ReviewControlPlane(repo_root=tmp_path).chunk_plan(
        [summary_path], config_path=config_path,
    )

    assert result["provider_posts_emitted"] == 0
    assert result["semantic_route_preflight_summary"]["stability_mode_in_shadow"] == "smoke"
    assert result["semantic_route_preflight_summary"]["semantic_repair_enabled_in_shadow"] is False
    assert result["semantic_route_preflight_summary"]["rejection_reason"] != "stability_provider_or_route_missing"


def test_chunk_plan_resolves_retry_fallback_per_role(tmp_path: Path) -> None:
    summary_path = tmp_path / "summaries.json"
    summary_path.write_text(
        json.dumps([
            _summary("paper-a", "Paper A", "The first supported finding."),
            _summary("paper-b", "Paper B", "The second supported finding."),
        ]),
        encoding="utf-8",
    )
    config_path = tmp_path / "config.ini"
    _write_role_config(config_path)
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(config_path, encoding="utf-8")
    parser["Runtime"]["transport_retries"] = "2"
    parser["Generation_API"]["transport_retries"] = "0"
    parser.remove_option("Relation_API", "transport_retries")
    parser.remove_option("Critic_API", "transport_retries")
    with config_path.open("w", encoding="utf-8") as stream:
        parser.write(stream)

    result = ReviewControlPlane(repo_root=tmp_path).chunk_plan(
        [summary_path], config_path=config_path,
    )
    roles = result["semantic_route_preflight_summary"]["role_routes"]

    assert roles["candidate_provider_generation"]["transport_retry_reserve"] == 0
    assert roles["relation_adjudication"]["transport_retry_reserve"] == 2
    assert roles["structure_critique"]["transport_retry_reserve"] == 2
    assert result["provider_posts_emitted"] == 0
