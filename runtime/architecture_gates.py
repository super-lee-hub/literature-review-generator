from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence


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


_PUBLICATION_WRITE_CALLS = frozenset(
    {
        "atomic_write_bytes",
        "atomic_write_json",
        "write_bytes",
        "write_text",
        "replace",
        "rename",
    }
)
_PUBLICATION_REGISTRY_CALLS = frozenset({"register_file", "register_files_atomic"})
_PUBLICATION_EXCEPTION_MARKER = "publication-boundary-exception:"
_PUBLICATION_IMPLEMENTATION_MARKER = "publication-boundary-implementation:"
_PRIVATE_WRITE_MARKERS = frozenset(
    {
        ".publication-staging",
        "cache",
        "caches",
        "temporary",
        "temp/",
        "tmp/",
        "rendering-source",
    }
)


def _call_name(call: ast.Call) -> str:
    function = call.func
    if isinstance(function, ast.Name):
        return function.id
    if isinstance(function, ast.Attribute):
        return function.attr
    return ""


def _qualified_call_name(call: ast.Call) -> str:
    function = call.func
    if isinstance(function, ast.Attribute) and isinstance(function.value, ast.Name):
        return f"{function.value.id}.{function.attr}"
    return _call_name(call)


def _expression_text(node: ast.AST, source: str) -> str:
    try:
        return ast.get_source_segment(source, node) or ast.unparse(node)
    except (AttributeError, ValueError):
        return ""


def _write_target(call: ast.Call) -> ast.AST | None:
    name = _call_name(call)
    qualified_name = _qualified_call_name(call)
    if name in {"write_bytes", "write_text"}:
        if isinstance(call.func, ast.Attribute):
            return call.func.value
        return None
    if qualified_name in {"os.replace", "os.rename"}:
        return call.args[1] if len(call.args) > 1 else None
    if name in {"replace", "rename"}:
        if isinstance(call.func, ast.Attribute):
            return call.func.value
        return None
    return call.args[0] if call.args else None


def _target_names(node: ast.AST | None) -> set[str]:
    if node is None:
        return set()
    if isinstance(node, ast.Name):
        return {node.id}
    if isinstance(node, ast.Attribute):
        return {node.attr}
    if isinstance(node, ast.Call) and node.args:
        return _target_names(node.args[0])
    return set()


def _looks_private_or_temporary(target_text: str) -> bool:
    normalized = target_text.replace("\\", "/").casefold()
    return any(marker in normalized for marker in _PRIVATE_WRITE_MARKERS)


def _is_canonical_target(
    target_text: str,
    target_names: set[str],
    canonical_names: set[str] | None = None,
) -> bool:
    normalized = target_text.replace("\\", "/").casefold()
    if _looks_private_or_temporary(normalized):
        return False
    canonical_markers = (
        "canonical",
        "review_draft",
        "citation_manifest",
        "review_docx",
        "job_outcome",
        "current_artifact",
        "current_set",
        "stage_terminal",
        "source_bundle",
        "summary_file",
        "provider_receipt",
        "evidence_manifest",
    )
    return bool(target_names.intersection(canonical_names or set())) or any(
        marker in normalized for marker in canonical_markers
    )


def _has_exception_marker(source_lines: Sequence[str], line_number: int) -> bool:
    start = max(0, line_number - 2)
    end = min(len(source_lines), line_number + 1)
    return any(_PUBLICATION_EXCEPTION_MARKER in source_lines[index] for index in range(start, end))


def _has_implementation_marker(source_lines: Sequence[str], line_number: int) -> bool:
    start = max(0, line_number - 2)
    end = min(len(source_lines), line_number + 1)
    return any(_PUBLICATION_IMPLEMENTATION_MARKER in source_lines[index] for index in range(start, end))


_CANONICAL_PATH_MARKERS = (
    "canonical",
    "review_draft",
    "citation_manifest",
    "review_docx",
    "job_outcome",
    "current_artifact",
    "current_set",
    "stage_terminal",
    "source_bundle",
    "summary_file",
    "provider_receipt",
    "evidence_manifest",
)


def _assignment_targets(node: ast.Assign | ast.AnnAssign | ast.NamedExpr) -> list[ast.AST]:
    if isinstance(node, ast.Assign):
        return list(node.targets)
    return [node.target]


def _contains_canonical_marker(node: ast.AST) -> bool:
    for child in ast.walk(node):
        values: list[str] = []
        if isinstance(child, ast.Name):
            values.append(child.id)
        elif isinstance(child, ast.Attribute):
            values.append(child.attr)
        elif isinstance(child, ast.Constant) and isinstance(child.value, str):
            values.append(child.value)
        for value in values:
            normalized = value.replace("\\", "/").casefold()
            if any(marker in normalized for marker in _CANONICAL_PATH_MARKERS):
                return True
    return False


def _scan_function_publication_bypasses(
    tree: ast.AST,
    *,
    path: Path,
    source: str,
) -> list[tuple[str, str]]:
    """Scan all function scopes in one AST traversal.

    Nested functions are isolated from their parent scope so a helper's
    private staging write cannot be paired with a caller's Registry call.
    """

    source_lines = source.splitlines()
    findings: set[tuple[str, str]] = set()

    class _Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.scopes: list[dict[str, Any]] = []

        def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
            state: dict[str, Any] = {
                "canonical_names": set(),
                "writes": [],
                "registrations": [],
            }
            self.scopes.append(state)
            for statement in node.body:
                self.visit(statement)
            self.scopes.pop()
            for write_line, target_text, target_names in state["writes"]:
                for register_line, register_names in state["registrations"]:
                    if register_line <= write_line:
                        continue
                    if target_names and register_names and not target_names.intersection(register_names):
                        continue
                    if not _is_canonical_target(target_text, target_names, state["canonical_names"]):
                        continue
                    if _has_exception_marker(source_lines, write_line) or _has_implementation_marker(
                        source_lines, write_line
                    ):
                        continue
                    findings.add((str(path), "canonical_publication_boundary_bypass"))
                    break

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self._visit_function(node)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            self._visit_function(node)

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            # Methods own their publication scopes; decorators and bases do
            # not contain a canonical writer body.
            for statement in node.body:
                self.visit(statement)

        def visit_Assign(self, node: ast.Assign) -> None:
            if self.scopes:
                if _contains_canonical_marker(node.value):
                    for target in _assignment_targets(node):
                        self.scopes[-1]["canonical_names"].update(_target_names(target))
            self.generic_visit(node)

        def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
            if self.scopes and node.value is not None:
                if _contains_canonical_marker(node.value):
                    self.scopes[-1]["canonical_names"].update(_target_names(node.target))
            self.generic_visit(node)

        def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
            if self.scopes:
                if _contains_canonical_marker(node.value):
                    self.scopes[-1]["canonical_names"].update(_target_names(node.target))
            self.generic_visit(node)

        def visit_Call(self, node: ast.Call) -> None:
            if self.scopes:
                name = _call_name(node)
                if name in _PUBLICATION_WRITE_CALLS:
                    target = _write_target(node)
                    self.scopes[-1]["writes"].append(
                        (
                            int(getattr(node, "lineno", 0) or 0),
                            _expression_text(target, source) if target is not None else "",
                            _target_names(target),
                        )
                    )
                elif name in _PUBLICATION_REGISTRY_CALLS:
                    path_nodes = [keyword.value for keyword in node.keywords if keyword.arg == "path"]
                    names = set().union(*(_target_names(value) for value in path_nodes)) if path_nodes else set()
                    self.scopes[-1]["registrations"].append(
                        (int(getattr(node, "lineno", 0) or 0), names)
                    )
            self.generic_visit(node)

    _Visitor().visit(tree)
    return sorted(findings)


def scan_paths_for_publication_boundary_bypasses(paths: Iterable[Path]) -> list[tuple[str, str]]:
    """Find canonical writes followed by a separate Registry registration.

    The check is intentionally syntax-based and conservative.  A writer may
    opt out only on the exact write line with a narrowly documented exception
    marker for private staging, caches, temporary rendering sources, or
    read-only legacy compatibility code.
    """

    findings: list[tuple[str, str]] = []
    for path in sorted(paths, key=lambda value: str(value)):
        try:
            source = path.read_text(encoding="utf-8", errors="ignore")
            tree = ast.parse(source, filename=str(path))
        except (OSError, SyntaxError, UnicodeError):
            continue
        findings.extend(_scan_function_publication_bypasses(tree, path=path, source=source))
    return sorted(set(findings))


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
    path_list = list(paths)
    for path in sorted(path_list, key=lambda value: str(value)):
        text = path.read_text(encoding="utf-8", errors="ignore")
        for needle, label in patterns.items():
            if needle in text:
                findings.append((str(path), label))
    findings.extend(scan_paths_for_publication_boundary_bypasses(path_list))
    return findings
