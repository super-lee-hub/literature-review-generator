from __future__ import annotations

import configparser
import json
from dataclasses import replace
from pathlib import Path

import pytest

from config_loader import load_config
from runtime.control_plane import ReviewControlPlane
from runtime.job_spec import RuntimeJobSpec, RuntimeSourceSpec
from runtime.provider_runtime import ProviderAggregateBudgetV2
from services.model_selection import get_api_config_for_section
from services.stage1_output_budget import (
    provider_output_token_limit,
    stage1_output_budget_sequence,
)


def _stage1_spec(tmp_path: Path) -> RuntimeJobSpec:
    pdf_folder = tmp_path / "pdfs"
    pdf_folder.mkdir()
    (pdf_folder / "paper.pdf").write_bytes(b"%PDF-1.4\nlocal planner fixture\n")
    config_path = tmp_path / "config.ini"
    parser = configparser.ConfigParser(interpolation=None)
    reader_config = {
        "api_key": "local-fixture-only",
        "provider_family": "generic",
        "endpoint_type": "chat_completions",
        "api_base": "http://127.0.0.1:1/v1",
        "max_context_tokens": "128000",
        "max_output_tokens": "32000",
        "reasoning_reserve_tokens": "0",
        "safety_margin_tokens": "0",
        "transport_retries": "0",
    }
    parser.read_dict(
        {
            "Application": {"config_schema": "4"},
            "Paths": {"output_path": str(tmp_path / "output")},
            "Runtime": {"transport_retries": "0"},
            "Stage1_Input": {
                "stage1_visual_scan_max_output_tokens": "64000",
                "stage1_synthesis_max_output_tokens": "128000",
                "stage1_length_retry_ceiling_tokens": "128000",
                "stage1_length_retry_max_attempts": "1",
            },
            "Primary_Reader_API": {
                **reader_config,
                "model": "fixture-unlisted-primary-reader-model",
            },
            "Backup_Reader_API": {
                **reader_config,
                "model": "fixture-unlisted-backup-reader-model",
            },
        }
    )
    with config_path.open("w", encoding="utf-8") as stream:
        parser.write(stream)
    return RuntimeJobSpec(
        project_name="stage1-exposure-plan",
        source=RuntimeSourceSpec(mode="direct", pdf_folder=str(pdf_folder)),
        config=str(config_path),
        action="analyze",
        metadata={"requested_stages": ["analyze"]},
    )


def _reader_exposures(plan: dict[str, object], role: str) -> list[dict[str, object]]:
    return [
        item
        for item in plan["unknown_exposures"]  # type: ignore[index]
        if item["stage_name"] == "analyze" and item["semantic_role"] == role
    ]


def test_reader_exposure_uses_stage1_budgets_above_api_default(tmp_path: Path) -> None:
    spec = _stage1_spec(tmp_path)
    config = load_config(spec.config, action=spec.action, allow_template_credentials=False)
    stage1_settings = config.get("Stage1_Input") or {}
    primary_config = get_api_config_for_section(config, "Primary_Reader_API")
    backup_config = get_api_config_for_section(config, "Backup_Reader_API")

    assert int(primary_config["max_output_tokens"]) == 32_000
    assert provider_output_token_limit(primary_config) is None
    assert stage1_output_budget_sequence(
        "visual_scan", stage1_settings, provider_config=primary_config
    ) == (64_000, 128_000)
    assert stage1_output_budget_sequence(
        "synthesis", stage1_settings, provider_config=primary_config
    ) == (128_000,)

    plan = ReviewControlPlane(repo_root=tmp_path)._full_stage_request_plan_for_spec(
        spec,
        aggregate_budget_override=ProviderAggregateBudgetV2(24, 500_000, 0, 600),
    )
    primary_exposures = _reader_exposures(plan, "primary_reader")
    backup_exposures = _reader_exposures(plan, "backup_reader")

    assert len(primary_exposures) == 2
    assert len(backup_exposures) == 1
    assert all(item["output_tokens_per_call_upper_bound"] == 128_000 for item in primary_exposures)
    assert backup_exposures[0]["output_tokens_per_call_upper_bound"] == max(
        stage1_output_budget_sequence(
            "synthesis", stage1_settings, provider_config=backup_config
        )
    )
    assert backup_exposures[0]["output_tokens_per_call_upper_bound"] > int(
        backup_config["max_output_tokens"]
    )
    assert plan["boundary"]["no_provider_posts"] is True


def test_stage1_projection_preserves_explicit_zero_aggregate_output_budget(
    tmp_path: Path,
) -> None:
    spec = _stage1_spec(tmp_path)
    zero_budget = ProviderAggregateBudgetV2(24, 0, 0, 600)

    plan = ReviewControlPlane(repo_root=tmp_path)._full_stage_request_plan_for_spec(
        spec,
        aggregate_budget_override=zero_budget,
    )

    assert plan["aggregate_budget"]["max_output_tokens_total"] == 0
    assert plan["run_resource_limits_finite"] is True
    assert all(
        item["output_tokens_per_call_upper_bound"] == 128_000
        for item in _reader_exposures(plan, "primary_reader")
    )
    assert plan["boundary"]["no_provider_posts"] is True


def test_public_plan_keeps_reader_inventory_when_stage1_budget_exceeds_route_default(
    tmp_path: Path,
) -> None:
    spec = _stage1_spec(tmp_path)
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(
        json.dumps(spec.to_dict()),
        encoding="utf-8",
    )

    plan = ReviewControlPlane(repo_root=tmp_path).plan(str(spec_path))
    projection = plan["full_stage_request_plan"]

    assert projection.get("status") != "blocked_before_request_inventory"
    assert projection["run_resource_limits_finite"] is True
    assert projection["aggregate_budget"]["schema_version"] == (
        ProviderAggregateBudgetV2.SCHEMA_VERSION
    )
    assert projection["aggregate_budget"]["max_provider_calls_total"] == 24
    assert projection["aggregate_budget"]["max_output_tokens_total"] > 0
    assert projection["aggregate_budget_source"] == "documented_finite_run_default"
    assert projection["boundary"]["no_provider_posts"] is True
    assert all(
        item["output_tokens_per_call_upper_bound"] == 128_000
        for item in _reader_exposures(projection, "primary_reader")
    )
    assert _reader_exposures(projection, "backup_reader")[0][
        "output_tokens_per_call_upper_bound"
    ] == 128_000


def test_cold_reader_lower_bound_uses_canonical_paper_work_items(tmp_path: Path) -> None:
    spec = _stage1_spec(tmp_path)
    pdf_folder = Path(spec.source.pdf_folder)
    for name in ("paper-b.pdf", "paper-c.pdf"):
        (pdf_folder / name).write_bytes(f"%PDF-1.4\nlocal planner fixture {name}\n".encode())

    plan = ReviewControlPlane(repo_root=tmp_path)._full_stage_request_plan_for_spec(
        spec,
        aggregate_budget_override=ProviderAggregateBudgetV2(24, 500_000, 0, 600),
    )
    primary = [
        item
        for item in _reader_exposures(plan, "primary_reader")
        if not item["conditional_on"]
    ]

    assert len(primary) == 1
    assert primary[0]["logical_calls_known_lower_bound"] == 3
    basis = primary[0]["known_lower_bound_basis"]
    assert basis["schema_version"] == "stage1-paper-work-items/v1"
    assert basis["paper_work_item_count"] == 3
    assert len(basis["paper_work_items_sha256"]) == 64
    assert plan["totals"]["logical_calls_known"] == 0
    assert plan["totals"]["logical_calls_known_lower_bound"] == 3
    assert plan["totals"]["physical_attempts_known_lower_bound"] == 3
    assert plan["totals"]["logical_calls_upper_bound"] is None
    assert plan["boundary"]["no_provider_posts"] is True


@pytest.mark.parametrize("excluded_basis", ["summary", "f1", "quarantine"])
def test_reader_floor_stays_unknown_without_plain_cold_source_authority(
    tmp_path: Path,
    excluded_basis: str,
) -> None:
    spec = _stage1_spec(tmp_path)
    if excluded_basis == "summary":
        spec = replace(spec, summary_file=str(tmp_path / "existing-summary.json"))
    elif excluded_basis == "f1":
        spec = replace(
            spec,
            metadata={
                **dict(spec.metadata),
                "f1_corpus_binding": {
                    "schema_version": "f1-corpus-binding-v1",
                    "manifest_path": str(tmp_path / "manifest.json"),
                    "manifest_sha256": "a" * 64,
                    "source_ids": ["paper-a"],
                },
            },
        )
    else:
        spec = replace(
            spec,
            metadata={
                **dict(spec.metadata),
                "allow_partial_source_quarantine": True,
            },
        )

    plan = ReviewControlPlane(repo_root=tmp_path)._full_stage_request_plan_for_spec(
        spec,
        aggregate_budget_override=ProviderAggregateBudgetV2(24, 500_000, 0, 600),
    )
    primary = _reader_exposures(plan, "primary_reader")

    assert primary
    assert all(item["logical_calls_known_lower_bound"] == 0 for item in primary)
    assert all(item["known_lower_bound_basis"] is None for item in primary)
    assert plan["totals"]["logical_calls_known_lower_bound"] == 0
    assert plan["totals"]["physical_attempts_known_lower_bound"] == 0
    assert plan["totals"]["logical_calls_upper_bound"] is None
    assert plan["boundary"]["no_provider_posts"] is True


def test_paper_work_item_basis_changes_when_pdf_content_changes(tmp_path: Path) -> None:
    from runtime.source_intake import (
        build_source_bundle_for_request,
        stage1_paper_work_item_basis,
    )

    spec = _stage1_spec(tmp_path)
    request = spec.to_job_request()
    before = stage1_paper_work_item_basis(
        build_source_bundle_for_request(request, project_name=spec.project_name)
    )
    pdf_path = Path(spec.source.pdf_folder) / "paper.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\nchanged local planner fixture\n")
    after = stage1_paper_work_item_basis(
        build_source_bundle_for_request(request, project_name=spec.project_name)
    )

    assert before["paper_work_item_count"] == after["paper_work_item_count"] == 1
    assert before["paper_work_items_sha256"] != after["paper_work_items_sha256"]
