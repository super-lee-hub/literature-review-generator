from __future__ import annotations

import re
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]


def _tracked_text() -> str:
    paths = subprocess.check_output(
        ["git", "ls-files"],
        cwd=ROOT,
        text=True,
    ).splitlines()
    chunks: list[str] = []
    for raw in paths:
        if raw.replace("\\", "/") == "tests/test_repository_hygiene.py":
            continue
        path = ROOT / raw
        if path.suffix.casefold() not in {".md", ".txt", ".py", ".json", ".ini"}:
            continue
        try:
            chunks.append(path.read_text(encoding="utf-8", errors="ignore"))
        except OSError:
            continue
    return "\n".join(chunks)


def test_root_markdown_is_authorized() -> None:
    allowed = {"README.md", "README.en.md", "README.zh-CN.md", "AGENTS.md"}
    actual = {path.name for path in ROOT.glob("*.md")}
    assert actual <= allowed
    assert (ROOT / "docs" / "README.md").is_file()


def test_docs_root_contains_only_index_file_and_directories() -> None:
    files = {path.name for path in (ROOT / "docs").iterdir() if path.is_file()}
    # Release acceptance reports are intentionally durable and live directly
    # under docs so their path is stable for reviewers and CI artifacts.
    allowed_reports = {
        "F1_FINAL_HARDENING_AND_LIVE_ACCEPTANCE_20260906.md",
        "FINAL_HARDENING_AND_LIVE_ACCEPTANCE_20260907.md",
    }
    assert files <= {"README.md", *allowed_reports}
    assert "README.md" in files


def test_test_temp_is_not_tracked() -> None:
    completed = subprocess.run(
        [
            "git",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "maintenance.auto=false",
            "ls-files",
            "--",
            "test_temp",
        ],
        cwd=ROOT,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=True,
        timeout=15,
    )
    assert completed.stdout.splitlines() == []


def test_root_readme_and_agents_links_resolve() -> None:
    for name in ("README.md", "README.en.md", "README.zh-CN.md", "AGENTS.md"):
        path = ROOT / name
        text = path.read_text(encoding="utf-8")
        for raw_target in re.findall(r"\]\(([^)#]+)", text):
            target = raw_target.strip().strip("<>")
            if "://" in target or target.startswith("mailto:"):
                continue
            assert (path.parent / target).resolve().is_file(), (name, target)


def test_docs_markdown_relative_links_resolve() -> None:
    docs_root = ROOT / "docs"
    for path in docs_root.rglob("*.md"):
        text = path.read_text(encoding="utf-8")
        for raw_target in re.findall(r"\]\(([^)#]+)", text):
            target = raw_target.strip().strip("<>")
            if "://" in target or target.startswith("mailto:"):
                continue
            assert (path.parent / target).resolve().exists(), (path, target)


def test_deleted_pointer_documents_have_no_inbound_references() -> None:
    text = _tracked_text()
    for name in (
        "ARCHITECTURE_BASELINE.md",
        "DEVELOPMENT.md",
        "FEATURE_MATRIX.md",
        "MIGRATION_NOTES.md",
        "TRUTH_SOURCES.md",
    ):
        assert name not in text
