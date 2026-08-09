from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys

import pytest

from reviewctl import build_parser
from runtime.job_spec import load_runtime_job_spec


ROOT = Path(__file__).resolve().parents[1]
CURRENT_DOCS = (
    ROOT / "README.md",
    ROOT / "README.en.md",
    ROOT / "README.zh-CN.md",
    ROOT / "AGENTS.md",
    ROOT / "docs/en/developer/architecture.md",
    ROOT / "docs/zh-CN/developer/architecture.md",
    ROOT / "docs/en/ai/skill.md",
    ROOT / "docs/zh-CN/ai/skill.md",
    ROOT / "docs/en/ai/runtime-bridge.md",
    ROOT / "docs/zh-CN/ai/runtime-bridge.md",
    ROOT / "docs/en/ai/handoff.md",
    ROOT / "docs/zh-CN/ai/handoff.md",
    ROOT / "docs/en/reference/feature-matrix.md",
    ROOT / "docs/zh-CN/reference/feature-matrix.md",
    ROOT / ".codex/skills/auto-generate-orchestrator/SKILL.md",
    ROOT / "prompts/README.md",
)
EXAMPLES = (
    ROOT / "examples/runtime_specs/direct-run-all.json",
    ROOT / "examples/runtime_specs/zotero-run-all.json",
    ROOT / "examples/runtime_specs/free-mode-idea.json",
)


def _read_current_docs() -> str:
    return "\n".join(path.read_text(encoding="utf-8") for path in CURRENT_DOCS)


def _parser_commands() -> set[str]:
    parser = build_parser()
    subparsers = next(
        action
        for action in parser._actions
        if isinstance(action, argparse._SubParsersAction)
    )
    return set(subparsers.choices)


def test_current_docs_do_not_advertise_removed_surfaces() -> None:
    text = _read_current_docs()
    stale_markers = (
        "python main.py --setup",
        "python main.py --pdf-folder",
        "python main.py --run-all",
        "python main.py --analyze-only",
        "python main.py --generate-outline",
        "python main.py --generate-review",
        "python main.py --validate-review",
        "--prime-with-folder",
        "--retry-failed",
        "--retry-review-failed",
        "review_draft_v2",
        "services/progress_state.py",
        "validator.py",
        "main.py remains large",
        "codex/platform-hardening-outline-v3",
        "833 passed",
        "855 collected",
    )
    assert [marker for marker in stale_markers if marker in text] == []


def test_current_docs_expose_the_current_runtime_contract() -> None:
    text = _read_current_docs()
    for marker in (
        "python -m reviewctl",
        "RuntimeJobSpec",
        "AgentRuntimeRunner",
        "Outline Intelligence v3",
        "review_draft",
        "ValidationExecutionService",
        "Concept Mode is currently disabled",
    ):
        assert marker in text, marker


def test_readme_documented_subcommands_are_parser_commands() -> None:
    commands = _parser_commands()
    documented = {
        match.group(1)
        for path in (ROOT / "README.md", ROOT / "README.en.md", ROOT / "README.zh-CN.md")
        for match in re.finditer(
            r"python -m reviewctl\s+([a-z][a-z0-9-]*)",
            path.read_text(encoding="utf-8"),
        )
    }
    assert documented <= commands
    assert {"plan", "run", "status", "inspect", "next-action", "resume", "validate"} <= documented


@pytest.mark.parametrize("command", (
    "--help",
    "doctor --help",
    "plan --help",
    "run --help",
    "status --help",
    "inspect --help",
    "next-action --help",
    "resume --help",
    "validate --help",
    "validation-status --help",
    "queue-list --help",
    "queue-add --help",
    "queue-run --help",
    "queue-retry --help",
    "queue-cancel --help",
    "queue-remove --help",
    "queue-export --help",
    "queue-import --help",
))
def test_public_reviewctl_help_smoke(command: str) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "reviewctl", *command.split()],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    assert result.stdout.strip(), command


def test_runtime_spec_examples_load_and_resolve_paths() -> None:
    for path in EXAMPLES:
        raw = json.loads(path.read_text(encoding="utf-8"))
        spec = load_runtime_job_spec(path)
        spec.validate()
        assert spec.action == "run_all"
        assert spec.source.mode in {"direct", "zotero"}
        assert Path(spec.config).is_absolute()
        assert Path(spec.source.pdf_folder or spec.source.zotero_report).is_absolute()
        assert spec.to_job_request().action == "run_all"
        assert not any("concept" in str(key).lower() for key in raw)
        assert not (raw.get("free_mode_profile") and raw.get("free_mode_idea"))


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda path: path.stem)
def test_runtime_spec_example_plan_is_provider_free(path: Path) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "reviewctl", "plan", "--spec", str(path)],
        cwd=ROOT,
        env={
            **dict(os.environ),
            "AUTO_GENERATE_OFFLINE_TESTS": "1",
            "AUTO_GENERATE_RUN_LIVE_API": "0",
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    payload = json.loads(result.stdout)
    assert payload.get("status") not in {"error", "failed"}
