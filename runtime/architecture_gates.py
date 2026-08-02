from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence


DEFAULT_PRODUCTION_ROOTS: tuple[str, ...] = (
    # Root-level production modules are part of the runtime surface even
    # though they do not live below a package directory.
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
    # Package roots are listed explicitly so the gate cannot silently drift
    # into scanning tests, documentation, or generated workspace output.
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
)


DEFAULT_FORBIDDEN_PATTERNS: dict[str, str] = {
    "subprocess.run(['python', 'main" ".py'": "legacy_cli_shellout",
    "subprocess.run([\"python\", \"main" ".py\"": "legacy_cli_shellout",
    "dispatch_" "command(": "legacy_cli_dispatch",
    "workflow_" "facade.run_dispatch(": "workflow_facade_dispatch",
    "_execute_" "legacy_action(": "job_runner_legacy_execution",
    "handle_" "run_all_mode(": "legacy_handle_run_all",
    "handle_" "generate_outline_mode(": "legacy_handle_outline",
    "handle_" "generate_review_mode(": "legacy_handle_review",
    "handle_" "generate_section_mode(": "legacy_handle_section",
    "handle_" "retry_failed(": "legacy_handle_retry_failed",
    "handle_" "retry_review_failed_mode(": "legacy_handle_retry_review_failed",
    "process_" "all_papers(": "legacy_stage1_generation",
    "generate_" "literature_review_outline(": "legacy_outline_generation",
    "generate_" "full_review_from_outline(": "legacy_review_generation",
    # Configuration compatibility and duplicate-truth symbols.
    "services." "config_compat": "legacy_config_compat_module",
    "Compat" "ConfigView": "legacy_config_view",
    "Validation" "CompatSettings": "legacy_validation_compat_settings",
    "apply_validation_" "compat_sections(": "legacy_validation_compat_sections",
    "remove_legacy_" "rate_limit_settings(": "legacy_rate_limit_settings",
    "LEGACY_" "RATE_LIMIT_KEYS": "legacy_rate_limit_keys",
    "legacy_" "citation_policy": "legacy_citation_policy_config",
    "keep_legacy_" "projections": "legacy_projection_switch",
    # Runtime/CLI injection and the historical monolith dispatcher.
    "import main as " "legacy_main": "legacy_main_injection",
    "run_" "dispatch(": "legacy_run_dispatch",
    "--stage-" "handler": "legacy_stage_handler_option",
    "--validator-" "module": "legacy_validator_module_option",
    # Outline v2/legacy adapters must not become a current execution route.
    "outline." "v2_config": "legacy_outline_v2_config",
    "outline." "v2_models": "legacy_outline_v2_models",
    "OutlineV2" "Config": "legacy_outline_v2_config_symbol",
    "OutlineQuality" "GateConfig": "legacy_outline_quality_config",
    "V2" "Pipeline": "legacy_outline_v2_pipeline",
    "OutlineRuntime" "Resolver": "legacy_outline_runtime_resolver",
    "outline." "legacy_adapter": "legacy_outline_adapter_module",
    "OutlineLegacy" "Adapter": "legacy_outline_adapter_symbol",
    "enable_outline_" "intelligence_v2": "legacy_outline_v2_switch",
    "legacy_" "outline_path": "legacy_outline_path",
    # The old context optimizer silently discarded evidence. Current paths
    # must fail on an over-budget request or use deterministic sharding/merge.
    "truncate_context_" "if_needed(": "legacy_context_truncation",
    "optimize_context_" "for_outline(": "legacy_outline_context_optimizer",
    "optimize_context_" "for_synthesis(": "legacy_synthesis_context_optimizer",
    "head_" "text": "legacy_head_tail_truncation",
    "tail_" "text": "legacy_head_tail_truncation",
    "950" "000": "legacy_context_token_ceiling",
    "research_streams" "[:80]": "legacy_research_stream_truncation",
    # Exact PR14 clean-cut sentinels. Keep these entries in the policy module
    # itself; the scanner excludes this file so the catalog does not produce
    # self-findings.
    "RateLimiter": "legacy_rate_limiter_symbol",
    "primary_" "tpm_limit": "legacy_primary_tpm_limit",
    "primary_" "rpm_limit": "legacy_primary_rpm_limit",
    "backup_" "tpm_limit": "legacy_backup_tpm_limit",
    "backup_" "rpm_limit": "legacy_backup_rpm_limit",
    "enable_outline_" "intelligence_v2": "legacy_outline_v2_switch_exact",
    "Compat" "ConfigView": "legacy_config_view_exact",
    "legacy_" "main": "legacy_main_symbol",
    "migrate" "-legacy": "legacy_workspace_migration_command",
    "legacy_" "unverified": "legacy_unverified_state",
    "legacy_" "citation_policy": "legacy_citation_policy_exact",
    "project_" "legacy_workspace_outcome": "legacy_workspace_projection",
    "V2" "Pipeline": "legacy_outline_v2_pipeline_exact",
    "max_tokens_" "for_optimization": "legacy_optimizer_token_budget",
    "950" "000": "legacy_context_token_ceiling_exact",
    "中间内容已截断": "legacy_truncated_content_marker",
    "manual_" "repaired_legacy": "legacy_manual_repair_marker",
}


@dataclass(frozen=True)
class ArchitectureGateScope:
    extra_canonical_roots: Sequence[str] = field(default_factory=tuple)
    include_roots: Sequence[str] = field(default_factory=lambda: DEFAULT_PRODUCTION_ROOTS)
    text_extensions: Sequence[str] = field(default_factory=lambda: (".py", ".md"))
    exclude_roots: Sequence[str] = field(
        default_factory=lambda: (
            ".git",
            ".omx",
            ".pytest_cache",
            "__pycache__",
            "output",
            "tests",
            # The scanner's own pattern catalog necessarily contains the
            # forbidden literals; it is policy code, not a production caller.
            "runtime/architecture_gates.py",
        )
    )

    def canonical_prefixes(self) -> tuple[str, ...]:
        values = [*self.include_roots, *self.extra_canonical_roots]
        return tuple(
            dict.fromkeys(
                str(value).replace("\\", "/").strip("/")
                for value in values
                if str(value).strip()
            )
        )

    def excluded_prefixes(self) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                str(value).replace("\\", "/").strip("/")
                for value in self.exclude_roots
                if str(value).strip()
            )
        )

    def is_scannable(self, relative_path: str | Path) -> bool:
        normalized = str(relative_path).replace("\\", "/").strip("/")
        if not normalized:
            return False
        path_obj = Path(normalized)
        excluded = self.excluded_prefixes()
        if self.text_extensions and path_obj.suffix.lower() not in {
            extension.lower()
            for extension in self.text_extensions
        }:
            return False
        if any(part in excluded for part in path_obj.parts):
            return False
        if any(
            normalized == prefix or normalized.startswith(f"{prefix}/")
            for prefix in excluded
        ):
            return False
        return any(
            normalized == prefix or normalized.startswith(f"{prefix}/")
            for prefix in self.canonical_prefixes()
        )


def collect_scannable_paths(repo_root: str | Path, *, scope: ArchitectureGateScope | None = None) -> list[Path]:
    root = Path(repo_root).resolve()
    gate_scope = scope or ArchitectureGateScope()
    collected: dict[str, Path] = {}
    for prefix in gate_scope.canonical_prefixes():
        candidate = root.joinpath(*prefix.split("/"))
        if candidate.is_file():
            candidates: Iterable[Path] = (candidate,)
        elif candidate.is_dir():
            candidates = candidate.rglob("*")
        else:
            continue
        for path in candidates:
            if not path.is_file():
                continue
            relative = path.relative_to(root).as_posix()
            if gate_scope.is_scannable(relative):
                collected[relative] = path
    return [collected[key] for key in sorted(collected)]


def scan_paths_for_forbidden_patterns(
    paths: Iterable[Path],
    *,
    forbidden_patterns: dict[str, str] | None = None,
) -> list[tuple[str, str]]:
    patterns = dict(forbidden_patterns or DEFAULT_FORBIDDEN_PATTERNS)
    findings: list[tuple[str, str]] = []
    for path in sorted(paths, key=lambda value: str(value)):
        text = path.read_text(encoding="utf-8", errors="ignore")
        for needle, label in patterns.items():
            if needle in text:
                findings.append((str(path), label))
    return findings
