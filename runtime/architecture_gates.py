from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence


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
}


@dataclass(frozen=True)
class ArchitectureGateScope:
    extra_canonical_roots: Sequence[str] = field(default_factory=tuple)
    include_roots: Sequence[str] = field(default_factory=lambda: ("runtime", ".codex/skills"))
    text_extensions: Sequence[str] = field(default_factory=lambda: (".py", ".md"))
    exclude_roots: Sequence[str] = field(
        default_factory=lambda: (
            ".git",
            ".omx",
            ".pytest_cache",
            "__pycache__",
            "output",
            "tests",
        )
    )

    def canonical_prefixes(self) -> tuple[str, ...]:
        values = [*self.include_roots, *self.extra_canonical_roots]
        return tuple(dict.fromkeys(str(value).replace("\\", "/").strip("/") for value in values if str(value).strip()))

    def excluded_prefixes(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(str(value).replace("\\", "/").strip("/") for value in self.exclude_roots if str(value).strip()))

    def is_scannable(self, relative_path: str | Path) -> bool:
        normalized = str(relative_path).replace("\\", "/").strip("/")
        if not normalized:
            return False
        path_obj = Path(normalized)
        if self.text_extensions and path_obj.suffix.lower() not in {
            extension.lower()
            for extension in self.text_extensions
        }:
            return False
        if any(part in set(self.excluded_prefixes()) for part in path_obj.parts):
            return False
        if any(
            normalized == prefix or normalized.startswith(f"{prefix}/")
            for prefix in self.excluded_prefixes()
        ):
            return False
        return any(
            normalized == prefix or normalized.startswith(f"{prefix}/")
            for prefix in self.canonical_prefixes()
        )


def collect_scannable_paths(repo_root: str | Path, *, scope: ArchitectureGateScope | None = None) -> list[Path]:
    root = Path(repo_root).resolve()
    gate_scope = scope or ArchitectureGateScope()
    collected: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if gate_scope.is_scannable(relative):
            collected.append(path)
    return sorted(collected)


def scan_paths_for_forbidden_patterns(
    paths: Iterable[Path],
    *,
    forbidden_patterns: dict[str, str] | None = None,
) -> list[tuple[str, str]]:
    patterns = dict(forbidden_patterns or DEFAULT_FORBIDDEN_PATTERNS)
    findings: list[tuple[str, str]] = []
    for path in paths:
        text = path.read_text(encoding="utf-8", errors="ignore")
        for needle, label in patterns.items():
            if needle in text:
                findings.append((str(path), label))
    return findings
