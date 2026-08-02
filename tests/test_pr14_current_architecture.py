from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from runtime.architecture_gates import (
    collect_scannable_paths,
    scan_paths_for_forbidden_patterns,
)
from runtime.outline_v3_dag import create_outline_v3_node_dag, plan_outline_v3_resume
from runtime.provider_completion import ProviderCompletionEvaluator


def _write_fixture(root: Path, relative_path: str, content: str) -> None:
    path = root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_current_scope_scans_all_production_roots_but_not_tests_or_docs(tmp_path: Path) -> None:
    production_roots = {
        "ai_interface.py",
        "config_loader.py",
        "config_validator.py",
        "context_manager.py",
        "docx_writer.py",
        "file_finder.py",
        "generate_policy_analysis_excel.py",
        "launch_gui.py",
        "main.py",
        "models.py",
        "pdf_extractor.py",
        "placeholder_analyzer.py",
        "report_generator.py",
        "reviewctl.py",
        "setup_wizard.py",
        "summary_schema.py",
        "utils.py",
        "validator.py",
        "zotero_parser.py",
        ".codex/skills",
        "free_mode",
        "gui",
        "outline",
        "preprocess",
        "rag",
        "runtime",
        "services",
        "tools",
        "validation",
    }
    for root_name in production_roots:
        relative_path = f"{root_name}/module.py" if "." not in root_name.rsplit("/", 1)[-1] else root_name
        extension = ".md" if root_name == ".codex/skills" else ".py"
        if root_name == ".codex/skills":
            relative_path = f"{root_name}/current.md"
        elif "." not in root_name.rsplit("/", 1)[-1]:
            relative_path = f"{root_name}/module{extension}"
        _write_fixture(tmp_path, relative_path, "current surface\n")

    _write_fixture(tmp_path, "tests/should_not_scan.py", "dispatch_command(args)\n")
    _write_fixture(tmp_path, "docs/should_not_scan.py", "dispatch_command(args)\n")

    scanned = collect_scannable_paths(tmp_path)
    scanned_relative = {path.relative_to(tmp_path).as_posix() for path in scanned}

    assert "main.py" in scanned_relative
    assert "outline/module.py" in scanned_relative
    assert ".codex/skills/current.md" in scanned_relative
    assert "tests/should_not_scan.py" not in scanned_relative
    assert "docs/should_not_scan.py" not in scanned_relative
    assert all(
        any(
            relative == root or relative.startswith(f"{root}/")
            for root in production_roots
        )
        for relative in scanned_relative
    )


def test_current_v3_surface_has_no_forbidden_legacy_symbols(tmp_path: Path) -> None:
    _write_fixture(
        tmp_path,
        "runtime/current.py",
        "from runtime.outline_v3_dag import create_outline_v3_node_dag\n"
        "from runtime.provider_completion import ProviderCompletionEvaluator\n",
    )
    _write_fixture(
        tmp_path,
        "outline/v3_executor.py",
        "class OutlineV3Executor:\n    pass\n",
    )
    _write_fixture(
        tmp_path,
        "services/settings.py",
        "class ApplicationSettings:\n    pass\n",
    )

    findings = scan_paths_for_forbidden_patterns(collect_scannable_paths(tmp_path))

    assert findings == []


def test_current_scope_reports_config_cli_outline_and_truncation_surfaces(tmp_path: Path) -> None:
    _write_fixture(tmp_path, "config_loader.py", "from services.config_compat import CompatConfigView\n")
    _write_fixture(tmp_path, "main.py", "dispatch_command(args)\n")
    _write_fixture(
        tmp_path,
        "outline/pipeline.py",
        "from outline.v2_models import OutlineV2Config\nclass V2Pipeline:\n    pass\n",
    )
    _write_fixture(
        tmp_path,
        "context_manager.py",
        "def truncate_context_if_needed(value):\n    return value\n"
        "MAX_TOKENS = 950000\n",
    )

    findings = scan_paths_for_forbidden_patterns(collect_scannable_paths(tmp_path))

    assert set(findings) == {
        (str(tmp_path / "config_loader.py"), "legacy_config_compat_module"),
        (str(tmp_path / "config_loader.py"), "legacy_config_view_exact"),
        (str(tmp_path / "main.py"), "legacy_cli_dispatch"),
        (str(tmp_path / "outline/pipeline.py"), "legacy_outline_v2_config_symbol"),
        (str(tmp_path / "outline/pipeline.py"), "legacy_outline_v2_models"),
        (str(tmp_path / "outline/pipeline.py"), "legacy_outline_v2_pipeline_exact"),
        (str(tmp_path / "context_manager.py"), "legacy_context_token_ceiling_exact"),
        (str(tmp_path / "context_manager.py"), "legacy_context_truncation"),
    }


def test_current_scope_reports_exact_pr14_clean_cut_sentinels(tmp_path: Path) -> None:
    sentinels = {
        "RateLimiter": "legacy_rate_limiter_symbol",
        "primary_tpm_limit": "legacy_primary_tpm_limit",
        "primary_rpm_limit": "legacy_primary_rpm_limit",
        "backup_tpm_limit": "legacy_backup_tpm_limit",
        "backup_rpm_limit": "legacy_backup_rpm_limit",
        "enable_outline_intelligence_v2": "legacy_outline_v2_switch_exact",
        "CompatConfigView": "legacy_config_view_exact",
        "legacy_main": "legacy_main_symbol",
        "migrate-legacy": "legacy_workspace_migration_command",
        "legacy_unverified": "legacy_unverified_state",
        "legacy_citation_policy": "legacy_citation_policy_exact",
        "project_legacy_workspace_outcome": "legacy_workspace_projection",
        "V2Pipeline": "legacy_outline_v2_pipeline_exact",
        "max_tokens_for_optimization": "legacy_optimizer_token_budget",
        "950000": "legacy_context_token_ceiling_exact",
        "中间内容已截断": "legacy_truncated_content_marker",
        "manual_repaired_legacy": "legacy_manual_repair_marker",
    }
    for index, (needle, _label) in enumerate(sentinels.items(), start=1):
        _write_fixture(tmp_path, f"runtime/sentinel_{index}.py", needle)

    findings = scan_paths_for_forbidden_patterns(collect_scannable_paths(tmp_path))

    assert {label for _path, label in findings} == set(sentinels.values())


def test_current_outline_dag_hash_and_resume_closure_are_deterministic() -> None:
    first = create_outline_v3_node_dag("job-current", candidate_count=2)
    second = create_outline_v3_node_dag("job-current", candidate_count=2)

    assert first.content_hash == second.content_hash
    assert first.get("global_corpus_ledger").depends_on == ["outline_evidence_views"]
    assert first.get("structure_critique").depends_on == [
        "candidate_1_provider_generation",
        "candidate_2_provider_generation",
    ]

    failed = replace(
        first,
        nodes=[
            replace(node, status="failed" if node.node_id == "structure_critique" else "succeeded")
            for node in first.nodes
        ],
    )
    plan = plan_outline_v3_resume(failed, "structure_critique")

    assert plan.rerun_node_ids == sorted(plan.rerun_node_ids)
    assert plan.rerun_node_ids == [
        "adoption",
        "arbitration",
        "coverage_audit",
        "final_outline",
        "section_evidence_packets",
        "selected_candidate",
        "stability_audit",
        "stage_health",
        "structure_critique",
    ]
    assert "candidate_1_provider_generation" in plan.preserved_node_ids
    assert "candidate_2_provider_generation" in plan.preserved_node_ids


def test_provider_completion_is_fail_closed_for_length_reason_and_schema_failures() -> None:
    complete = ProviderCompletionEvaluator.evaluate(
        {"status": "success", "content": '{"answer": "ok"}'},
        expect_json=True,
    )
    length_limited = ProviderCompletionEvaluator.evaluate(
        {"status": "success", "content": '{"answer": "ok"}', "finish_reason": "length"},
        expect_json=True,
    )
    budget_incomplete = ProviderCompletionEvaluator.evaluate(
        {"status": "incomplete", "content": '{"answer": "ok"}', "incomplete_reason": "budget"},
        expect_json=True,
    )
    malformed = ProviderCompletionEvaluator.evaluate(
        {"status": "success", "content": "not-json"},
        expect_json=True,
    )

    assert complete.status == "complete"
    assert complete.content == {"answer": "ok"}
    assert length_limited.status == "incomplete_length"
    assert budget_incomplete.status == "incomplete_reasoning_budget"
    assert malformed.status == "malformed_json"
