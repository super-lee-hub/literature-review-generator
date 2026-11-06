from __future__ import annotations

import configparser
import json
from dataclasses import replace
from pathlib import Path
from typing import Any


from runtime.control_plane import ReviewControlPlane
from runtime.provider_context import ProviderContextProfile
from runtime.provider_routes import build_reachable_provider_route_plan
from services.model_capabilities import resolve_model_capability
from services.model_selection import get_api_config_for_section
from tests.test_outline_v3_semantic_execution import _summary
from tests.test_reviewctl import _spec

API_SECTIONS = (
    "Primary_Reader_API",
    "Backup_Reader_API",
    "Writer_API",
    "Outline_API",
    "Free_Mode_API",
    "Validator_API",
)
REQUESTED_STAGES = ("analyze", "outline", "review", "validate")
SEMANTIC_BUILDERS = {
    "outline.v3_executor.OutlineV3Executor._build_topic_provider_request",
    "outline.v3_executor.OutlineV3Executor._run_bounded_semantic_provider_call:cross_group_comparison",
    "outline.v3_executor.OutlineV3Executor._run_bounded_semantic_provider_call:global_synthesis",
}
CANDIDATE_BUILDER = "outline.v3_executor.OutlineV3Executor._run_hierarchical_candidate_generation"


def _write_plan_case(
    tmp_path: Path,
    *,
    source_target: int,
    semantic_target: int,
    explicit_outline_reserves: bool,
) -> tuple[dict[str, Any], dict[str, dict[str, str]], Any]:
    spec = replace(
        _spec(tmp_path),
        action="run_all",
        metadata={"requested_stages": list(REQUESTED_STAGES)},
    )
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(Path(__file__).resolve().parents[1] / "config.ini.example", encoding="utf-8")
    parser["Paths"]["output_path"] = str(tmp_path / "output")
    for section in API_SECTIONS:
        parser[section]["api_key"] = "local-fixture-only"
        parser[section]["api_base"] = "http://127.0.0.1:1/v1"
    outline = parser["Outline_API"]
    outline["max_context_tokens"] = "200000"
    outline["max_output_tokens"] = "16000"
    outline["provider_max_output_tokens"] = "12000"
    outline["max_output_tokens_limit"] = "10000"
    if explicit_outline_reserves:
        outline["reasoning_reserve_tokens"] = "0"
        outline["safety_margin_tokens"] = "0"
    else:
        outline.pop("reasoning_reserve_tokens", None)
        outline.pop("safety_margin_tokens", None)
    parser["Outline"]["semantic_output_max_tokens"] = str(semantic_target)
    parser["OutlineStability"]["mode"] = "off"
    parser["OutlineStability"]["max_source_prompt_tokens"] = str(source_target)
    with Path(spec.config).open("w", encoding="utf-8", newline="\n") as stream:
        parser.write(stream)
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(spec.to_dict()), encoding="utf-8")
    plan = ReviewControlPlane(repo_root=tmp_path).plan(str(spec_path))
    config = {section: dict(parser.items(section)) for section in parser.sections()}
    route_plan = build_reachable_provider_route_plan(
        config,
        action=spec.action,
        requested_stages=REQUESTED_STAGES,
        free_mode_enabled=False,
    )
    profiles: dict[str, ProviderContextProfile] = {}
    for route in route_plan.routes:
        if route.stage != "outline":
            continue
        api_config = get_api_config_for_section(config, route.section_name)
        capability = resolve_model_capability(api_config)
        output_tokens = max(1, int(api_config.get("max_output_tokens") or 4_096))
        if route.semantic_role == "evidence_critique":
            output_tokens = min(output_tokens, 16_000)
        profiles[route.semantic_role] = ProviderContextProfile.from_api_config(
            api_config,
            max_output_tokens=output_tokens,
            default_provider=capability.provider_family,
            default_model=str(api_config.get("model") or ""),
            default_endpoint_type=capability.endpoint_type,
            default_reasoning_reserve=2_048,
            default_safety_margin=1_024,
        )
    return plan, config, profiles


def test_full_stage_outline_exposures_use_route_profiles_and_stay_unadmitted(
    tmp_path: Path,
) -> None:
    for source_target, semantic_target, reserves in (
        (0, 0, False),
        (64_000, 4_096, True),
    ):
        case_root = tmp_path / f"source-{source_target}-semantic-{semantic_target}"
        case_root.mkdir()
        plan, _config, profiles = _write_plan_case(
            case_root,
            source_target=source_target,
            semantic_target=semantic_target,
            explicit_outline_reserves=reserves,
        )
        full = plan["full_stage_request_plan"]
        exposures = [
            row for row in full["unknown_exposures"]
            if row["stage_name"] == "outline"
        ]
        assert exposures
        for row in exposures:
            profile = profiles[row["semantic_role"]]
            expected_input = (
                min(profile.input_budget, source_target)
                if source_target > 0
                else profile.input_budget
            )
            assert row["input_tokens_per_call_upper_bound"] == expected_input
            assert row["reasoning_tokens_per_call_upper_bound"] == profile.reasoning_reserve

        candidate_profile = profiles["candidate_provider_generation"]
        assert candidate_profile.reasoning_reserve == (0 if reserves else 2_048)
        assert candidate_profile.safety_margin == (0 if reserves else 1_024)
        assert candidate_profile.max_output_tokens == 10_000
        candidate = next(
            row for row in exposures
            if row.get("request_builder_id") == CANDIDATE_BUILDER
        )
        assert candidate["output_tokens_per_call_upper_bound"] == 10_000
        semantic_rows = [
            row for row in exposures
            if row.get("request_builder_id") in SEMANTIC_BUILDERS
        ]
        assert len(semantic_rows) == 3
        semantic_target_cap = min(
            candidate_profile.max_output_tokens,
            semantic_target if semantic_target > 0 else candidate_profile.max_output_tokens,
        )
        assert all(
            row["output_tokens_per_call_upper_bound"] == semantic_target_cap
            for row in semantic_rows
        )
        assert all(row["exposure_status"] == "unbounded" for row in exposures)
        assert full["budget_status"]["admission"] == "incomplete_unknown_exposure"
        assert plan["ready_for_provider_admission"] is False
        assert full["boundary"]["no_provider_posts"] is True


def test_chunk_plan_shadow_profiles_use_outline_default_reserves_without_posting(
    tmp_path: Path,
) -> None:
    spec = _spec(tmp_path)
    config_path = Path(spec.config)
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(Path(__file__).resolve().parents[1] / "config.ini.example", encoding="utf-8")
    parser["Paths"]["output_path"] = str(tmp_path / "output")
    for section in API_SECTIONS:
        parser[section]["api_key"] = "local-fixture-only"
        parser[section]["api_base"] = "http://127.0.0.1:1/v1"
    for section in API_SECTIONS:
        parser[section].pop("reasoning_reserve_tokens", None)
        parser[section].pop("safety_margin_tokens", None)
    outline = parser["Outline_API"]
    outline["max_context_tokens"] = "200000"
    outline["max_output_tokens"] = "16000"
    outline["provider_max_output_tokens"] = "12000"
    outline["max_output_tokens_limit"] = "10000"
    parser["Outline"]["candidate_count"] = "2"
    parser["Outline"]["technical_shard_target_tokens"] = "0"
    parser["OutlineStability"]["max_source_prompt_tokens"] = "0"
    with config_path.open("w", encoding="utf-8", newline="\n") as stream:
        parser.write(stream)

    summaries = [
        _summary("paper-a", "Paper A", "Treatment improved the short-term outcome."),
        _summary("paper-b", "Paper B", "Treatment improved the long-term outcome."),
    ]
    for summary in summaries:
        summary["core_analysis"]["research_gap"] = ""
    summary_path = tmp_path / "summaries.json"
    summary_path.write_text(json.dumps(summaries), encoding="utf-8")

    result = ReviewControlPlane(repo_root=tmp_path).chunk_plan(
        [summary_path],
        job_id="configured-outline-window-shadow",
        candidate_count=2,
        config_path=config_path,
    )

    route = result["semantic_route_preflight_summary"]["role_routes"][
        "candidate_provider_generation"
    ]
    assert result["provider_posts_emitted"] == 0
    assert result["read_only"] is True
    assert route["profile_limits"] == {
        "model_context_limit": 200_000,
        "input_budget": 146_928,
        "max_output_tokens": 10_000,
        "reasoning_reserve": 2_048,
        "safety_margin": 1_024,
    }



def test_stage1_backup_retry_ceiling_does_not_replace_route_profile(tmp_path: Path) -> None:
    spec = replace(
        _spec(tmp_path),
        action="run_all",
        metadata={"requested_stages": list(REQUESTED_STAGES)},
    )
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(Path(__file__).resolve().parents[1] / "config.ini.example", encoding="utf-8")
    parser["Paths"]["output_path"] = str(tmp_path / "output")
    for section in API_SECTIONS:
        parser[section]["api_key"] = "local-fixture-only"
        parser[section]["api_base"] = "http://127.0.0.1:1/v1"
    backup = parser["Backup_Reader_API"]
    backup["provider_family"] = "generic"
    backup["model"] = "backup-reader-fixture"
    backup["endpoint_type"] = "chat_completions"
    backup["max_context_tokens"] = "42680"
    backup["max_output_tokens"] = "16000"
    backup["reasoning_reserve_tokens"] = "4096"
    backup["safety_margin_tokens"] = "2048"
    backup.pop("provider_max_output_tokens", None)
    backup.pop("max_output_tokens_limit", None)
    parser["Stage1_Input"]["primary_reader_only"] = "false"
    parser["Stage1_Input"]["stage1_synthesis_max_output_tokens"] = "64000"
    parser["Stage1_Input"]["stage1_length_retry_max_attempts"] = "1"
    parser["Stage1_Input"]["stage1_length_retry_ceiling_tokens"] = "128000"
    with Path(spec.config).open("w", encoding="utf-8", newline="\n") as stream:
        parser.write(stream)
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(spec.to_dict()), encoding="utf-8")

    plan = ReviewControlPlane(repo_root=tmp_path).plan(str(spec_path))
    full = plan["full_stage_request_plan"]
    backup_exposure = next(
        row for row in full["unknown_exposures"]
        if row["stage_name"] == "analyze" and row["semantic_role"] == "backup_reader"
    )

    assert backup_exposure["output_tokens_per_call_upper_bound"] == 128_000
    assert backup_exposure["input_tokens_per_call_upper_bound"] == 12_000
    assert backup_exposure["reasoning_tokens_per_call_upper_bound"] == 4_096
    assert backup_exposure["exposure_status"] == "unbounded"
    assert full["budget_status"]["admission"] == "incomplete_unknown_exposure"
    assert plan["ready_for_provider_admission"] is False
    assert full["boundary"]["no_provider_posts"] is True


def test_chunk_plan_shadow_profile_uses_outline_defaults_and_hard_output_cap(
    tmp_path: Path,
) -> None:
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(Path(__file__).resolve().parents[1] / "config.ini.example", encoding="utf-8")
    parser["Paths"]["output_path"] = str(tmp_path / "output")
    for section in API_SECTIONS:
        parser[section]["api_key"] = "local-fixture-only"
        parser[section]["api_base"] = "http://127.0.0.1:1/v1"
    outline = parser["Outline_API"]
    outline["max_context_tokens"] = "200000"
    outline["max_output_tokens"] = "16000"
    outline["provider_max_output_tokens"] = "12000"
    outline["max_output_tokens_limit"] = "10000"
    outline.pop("reasoning_reserve_tokens", None)
    outline.pop("safety_margin_tokens", None)
    parser["Outline"]["candidate_count"] = "2"
    parser["Outline"]["technical_shard_target_tokens"] = "0"
    parser["Outline"]["semantic_output_max_tokens"] = "0"
    parser["OutlineStability"]["max_source_prompt_tokens"] = "0"
    config_path = tmp_path / "shadow-config.ini"
    with config_path.open("w", encoding="utf-8", newline="\n") as stream:
        parser.write(stream)

    summaries = [
        _summary("paper-a", "Paper A", "Treatment improved the short-term outcome."),
        _summary("paper-b", "Paper B", "Treatment improved the long-term outcome."),
    ]
    for summary in summaries:
        summary["core_analysis"]["research_gap"] = ""
    summary_path = tmp_path / "summaries.json"
    summary_path.write_text(json.dumps(summaries), encoding="utf-8")

    result = ReviewControlPlane(repo_root=tmp_path).chunk_plan(
        [summary_path],
        job_id="configured-outline-window-shadow",
        candidate_count=2,
        config_path=config_path,
    )

    route = result["semantic_route_preflight_summary"]["role_routes"][
        "candidate_provider_generation"
    ]
    assert result["provider_posts_emitted"] == 0
    assert result["read_only"] is True
    assert route["profile_limits"] == {
        "model_context_limit": 200_000,
        "input_budget": 146_928,
        "max_output_tokens": 10_000,
        "reasoning_reserve": 2_048,
        "safety_margin": 1_024,
    }
