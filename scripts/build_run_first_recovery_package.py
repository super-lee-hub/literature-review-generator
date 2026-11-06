"""Build and restore a hash-bound, single-commit Run First recovery ZIP."""

from __future__ import annotations

import argparse
import configparser
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import zipfile
from collections import Counter
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

PACKAGE_SCHEMA = "run-first-recovery-source-package/v2"
PACKAGE_MANIFEST_NAME = "RUN_FIRST_RECOVERY_PACKAGE_MANIFEST.json"
SNAPSHOT_BUNDLE_NAME = "RUN_FIRST_SOURCE_SNAPSHOT.bundle"
MAX_PACKAGE_FILES = 100_000
MAX_PACKAGE_BYTES = 512 * 1024 * 1024
MAX_SINGLE_FILE_BYTES = 32 * 1024 * 1024
MAX_MANIFEST_BYTES = 64 * 1024 * 1024
REPARSE_FLAG = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
REQUIRED_PATHS = frozenset(
    {
        "AGENTS.md",
        "config.ini.example",
        "requirements-py311-windows.lock",
        "requirements-py311-windows-dev.lock",
        "requirements-py311-windows-prod.lock",
        "docs/audits/pr25/RUN_FIRST_USER_MANUAL.md",
        "docs/en/RECOVERY_RUNBOOK.md",
        "scripts/build_run_first_recovery_package.py",
        "prompts/registry.json",
    }
)
EXCLUDED_DIRECTORY_PARTS = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "env",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "__pycache__",
        ".env",
        "node_modules",
        "tmp",
        "temp",
        "temporary",
        "output",
        "acceptance_artifacts",
        "cache",
        "caches",
        "registry",
        "registries",
        "ledger",
        "ledgers",
        "raw_response",
        "raw_responses",
        "raw_spool",
        "raw_spools",
        "budget",
        "budgets",
        "workspace",
        "workspaces",
        "private",
        "private_fulltext",
        "fulltext",
        "fulltexts",
        "original",
        "originals",
        "corpus",
        "zotero",
        "data",
    }
)
EXCLUDED_SUFFIXES = frozenset(
    {
        ".pdf",
        ".sqlite",
        ".sqlite3",
        ".db",
        ".pem",
        ".key",
        ".p12",
        ".pfx",
        ".kdbx",
    }
)
SECRET_PATTERNS = tuple(
    re.compile(pattern)
    for pattern in (
        rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
        rb"\bsk-[A-Za-z0-9_-]{20,}\b",
        rb"\bgh[pousr]_[A-Za-z0-9]{20,}\b",
        rb"\bAKIA[0-9A-Z]{16}\b",
    )
)
SECRET_VALUE_KEYS = frozenset(
    {
        "api_key",
        "api_token",
        "access_token",
        "refresh_token",
        "client_secret",
        "password",
        "secret",
        "authorization",
        "credential",
        "credentials",
    }
)
PLACEHOLDER_VALUE = re.compile(
    r"^(?:"
    r"|none|null|redacted|<redacted>|"
    r"<\s*(?:your|replace|insert|enter|placeholder|redacted)[^<>]*>"
    r"|(?:your|replace|insert|enter|set|change|sample|example|dummy|placeholder)[-_a-z0-9. ]*"
    r")$",
    re.IGNORECASE,
)
SOURCE_TEXT_SUFFIXES = frozenset({".py", ".md"})


class PackageRefused(RuntimeError):
    """The requested source tree or package destination failed validation."""


def _normcase(path: str | Path) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def _is_reparse(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & REPARSE_FLAG
    )


def _check_path_chain(path: Path) -> None:
    for component in reversed((path, *path.parents)):
        try:
            info = component.lstat()
        except OSError as exc:
            raise PackageRefused("source or destination path is missing") from exc
        if _is_reparse(info):
            raise PackageRefused("source or destination path crosses a junction or symlink")


def _run_git(
    args: list[str],
    *,
    cwd: Path | None = None,
    input_bytes: bytes | None = None,
    env: Mapping[str, str] | None = None,
    timeout: int = 60,
) -> bytes:
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=str(cwd) if cwd is not None else None,
            input=input_bytes,
            capture_output=True,
            check=False,
            timeout=timeout,
            env=dict(env) if env is not None else None,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PackageRefused("local Git operation could not complete") from exc
    if completed.returncode != 0:
        raise PackageRefused("local Git operation rejected the recovery source")
    return completed.stdout


def _git(
    root: Path,
    *args: str,
    binary: bool = False,
    env: Mapping[str, str] | None = None,
    timeout: int = 60,
) -> bytes | str:
    output = _run_git(
        ["-C", str(root), *args],
        env=env,
        timeout=timeout,
    )
    if binary:
        return output
    try:
        return output.decode("utf-8").strip()
    except UnicodeError as exc:
        raise PackageRefused("local Git returned invalid UTF-8 metadata") from exc


def _isolated_git_environment(empty_global_config: Path) -> dict[str, str]:
    environment = os.environ.copy()
    environment["GIT_CONFIG_NOSYSTEM"] = "1"
    environment["GIT_CONFIG_GLOBAL"] = str(empty_global_config)
    return environment


def _enable_windows_longpaths(repository: Path, *, env: Mapping[str, str]) -> None:
    """Persist Git's long-path mode in this temporary checkout only."""
    if os.name != "nt":
        return
    _run_git(
        [
            "-c",
            "core.longpaths=true",
            "-C",
            str(repository),
            "config",
            "--local",
            "core.longpaths",
            "true",
        ],
        env=env,
    )


def _verified_clean_source(source_root: str | Path, expected_sha: str) -> tuple[Path, str, str]:
    if not isinstance(expected_sha, str) or not re.fullmatch(
        r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})", expected_sha
    ):
        raise PackageRefused("an explicit full frozen Git SHA is required")
    raw_root = Path(source_root).expanduser()
    if not raw_root.is_absolute() or ".." in raw_root.parts:
        raise PackageRefused("source root must be absolute and normalized")
    root = Path(os.path.abspath(raw_root))
    _check_path_chain(root)
    try:
        resolved_root = root.resolve(strict=True)
    except OSError as exc:
        raise PackageRefused("source root is unavailable") from exc
    if _normcase(root) != _normcase(resolved_root) or not resolved_root.is_dir():
        raise PackageRefused("source root resolves through an alias or is not a directory")

    git_root = str(_git(resolved_root, "rev-parse", "--show-toplevel"))
    if _normcase(git_root) != _normcase(resolved_root):
        raise PackageRefused("source root is not the Git repository top level")
    canonical_sha = str(_git(resolved_root, "rev-parse", "--verify", f"{expected_sha}^{{commit}}"))
    head_sha = str(_git(resolved_root, "rev-parse", "--verify", "HEAD^{commit}"))
    if canonical_sha.casefold() != expected_sha.casefold() or head_sha.casefold() != canonical_sha.casefold():
        raise PackageRefused("HEAD does not match the explicit frozen Git SHA")
    dirty = _git(
        resolved_root,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
    )
    if str(dirty):
        raise PackageRefused("source checkout is dirty; package refused")
    tree_sha = str(_git(resolved_root, "rev-parse", f"{canonical_sha}^{{tree}}"))
    return resolved_root, canonical_sha, tree_sha


def _git_tree_entries(root: Path, commit_sha: str) -> list[dict[str, str]]:
    output = _git(root, "ls-tree", "-r", "-z", "--full-tree", commit_sha, binary=True)
    assert isinstance(output, bytes)
    entries: list[dict[str, str]] = []
    for raw in output.split(b"\0"):
        if not raw:
            continue
        try:
            header, raw_path = raw.split(b"\t", 1)
            mode, object_type, object_id = header.decode("ascii").split(" ", 2)
            path = raw_path.decode("utf-8")
        except (UnicodeError, ValueError) as exc:
            raise PackageRefused("Git tree contains an unsupported path or entry") from exc
        pure_path = PurePosixPath(path)
        if (
            pure_path.is_absolute()
            or not pure_path.parts
            or any(part in {"", ".", ".."} for part in pure_path.parts)
            or "\\" in path
            or any(":" in part for part in pure_path.parts)
        ):
            raise PackageRefused("Git tree contains a non-portable package path")
        entries.append(
            {
                "mode": mode,
                "object_type": object_type,
                "object_id": object_id,
                "path": pure_path.as_posix(),
            }
        )
    return entries


def _exclusion_reason(path: str, mode: str, object_type: str) -> str | None:
    parts = [part.casefold() for part in PurePosixPath(path).parts]
    basename = parts[-1]
    suffix = Path(basename).suffix.casefold()
    if mode == "120000":
        return "git_symlink_not_packaged"
    if mode == "160000" or object_type != "blob":
        return "git_submodule_or_non_blob"
    if mode not in {"100644", "100755"}:
        return "unsupported_git_mode"
    if any(part in EXCLUDED_DIRECTORY_PARTS for part in parts[:-1]):
        return "excluded_data_or_runtime_directory"
    if (
        basename == "config.ini"
        or (basename.startswith("config.ini.") and basename != "config.ini.example")
        or basename == ".env"
        or (basename.startswith(".env") and basename != ".env.example")
    ):
        return "local_configuration_or_environment"
    if suffix not in SOURCE_TEXT_SUFFIXES and re.search(
        r"(^|[._-])(credentials?|secrets?|tokens?|private_key)([._-]|$)", basename
    ):
        return "credential_named_file"
    if suffix not in SOURCE_TEXT_SUFFIXES and (
        basename in {"budget.json", "budget_ledger.json", "resume_state.json"}
        or (basename == "registry.json" and path != "prompts/registry.json")
    ):
        return "runtime_record_or_raw_response"
    if suffix not in SOURCE_TEXT_SUFFIXES and basename.startswith(
        (
            "raw_response",
            "raw-response",
            "raw_spool",
            "raw-spool",
            "provider_receipt",
            "provider_budget",
            "run_budget",
            "budget_ledger",
            "artifact_registry",
        )
    ):
        return "runtime_record_or_raw_response"
    if suffix in EXCLUDED_SUFFIXES:
        return "private_binary_or_database"
    if suffix not in SOURCE_TEXT_SUFFIXES and any(
        name in basename for name in ("fulltext", "full-text", "original_pdf", "source_pdf")
    ):
        return "private_full_text_or_original_source"
    if basename.endswith(".env"):
        return "local_environment"
    return None


def _is_secret_value_key(name: str) -> bool:
    normalized = name.strip().casefold().replace("-", "_")
    return normalized in SECRET_VALUE_KEYS or any(
        normalized.endswith(f"_{suffix}")
        for suffix in ("api_key", "api_token", "token", "secret", "password", "credential")
    )


def _validate_config_template(path: str, content: bytes) -> None:
    try:
        text = content.decode("utf-8")
    except UnicodeError as exc:
        raise PackageRefused(f"{path} is not UTF-8") from exc
    if path == ".env.example":
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith(("#", ";")) or "=" not in line:
                continue
            key, _separator, raw_value = line.partition("=")
            if _is_secret_value_key(key) and not PLACEHOLDER_VALUE.fullmatch(
                raw_value.strip().strip("\"'")
            ):
                raise PackageRefused(f"{path} contains a non-placeholder credential value")
        return

    parser = configparser.ConfigParser(interpolation=None, strict=True)
    try:
        parser.read_string(text)
    except configparser.Error as exc:
        raise PackageRefused(f"{path} is not valid ConfigParser INI syntax") from exc

    entries = list(parser.defaults().items())
    for section in parser.sections():
        entries.extend(parser.items(section, raw=True))
    for key, value in entries:
        if _is_secret_value_key(key) and not PLACEHOLDER_VALUE.fullmatch(value.strip().strip("\"'")):
            raise PackageRefused(f"{path} contains a non-placeholder credential value")


def _check_for_secret_material(content: bytes) -> None:
    if any(pattern.search(content) for pattern in SECRET_PATTERNS):
        raise PackageRefused("tracked source contains secret-like material; package refused")


def _destination_path(destination: str | Path, source_root: Path) -> Path:
    raw = Path(destination).expanduser()
    if not raw.is_absolute() or ".." in raw.parts:
        raise PackageRefused("package destination must be absolute and normalized")
    parent = Path(os.path.abspath(raw.parent))
    _check_path_chain(parent)
    resolved_parent = parent.resolve(strict=True)
    if _normcase(parent) != _normcase(resolved_parent) or not resolved_parent.is_dir():
        raise PackageRefused("package destination parent is not a normal directory")
    final_path = resolved_parent / raw.name
    if _path_is_within(final_path, source_root):
        raise PackageRefused("recovery package destination must be outside the source checkout")
    if os.path.lexists(final_path):
        raise PackageRefused("recovery package destination already exists")
    return final_path


def _path_is_within(path: Path, root: Path) -> bool:
    try:
        return os.path.commonpath((_normcase(path), _normcase(root))) == _normcase(root)
    except ValueError:
        return False


def _git_blob_sizes(root: Path, entries: list[dict[str, str]]) -> dict[str, int]:
    request = b"".join(
        entry["object_id"].encode("ascii") + b"\n" for entry in entries
    )
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), "cat-file", "--batch-check"],
            input=request,
            capture_output=True,
            check=False,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PackageRefused("Git blob size verification could not complete") from exc
    if completed.returncode != 0:
        raise PackageRefused("Git blob size verification failed")
    lines = completed.stdout.splitlines()
    if len(lines) != len(entries):
        raise PackageRefused("Git blob size inventory is incomplete")
    sizes: dict[str, int] = {}
    for entry, line in zip(entries, lines, strict=True):
        try:
            object_id, object_type, raw_size = line.decode("ascii").split(" ", 2)
            size = int(raw_size)
        except (UnicodeError, ValueError) as exc:
            raise PackageRefused("Git reported an invalid blob size") from exc
        if object_id != entry["object_id"] or object_type != "blob" or size < 0:
            raise PackageRefused("Git blob metadata did not match the frozen tree")
        if size > MAX_SINGLE_FILE_BYTES:
            raise PackageRefused("tracked source file exceeds the package size limit")
        sizes[object_id] = size
    return sizes


def _git_blob_batch(root: Path, entries: list[dict[str, str]], sizes: Mapping[str, int]):
    """Yield one frozen Git blob at a time without spawning one process per file."""

    try:
        process = subprocess.Popen(
            ["git", "-C", str(root), "cat-file", "--batch"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
        )
    except OSError as exc:
        raise PackageRefused("Git blob stream could not start") from exc
    try:
        if process.stdin is None or process.stdout is None:
            raise PackageRefused("Git blob stream did not open its pipes")
        for entry in entries:
            object_id = entry["object_id"]
            process.stdin.write(object_id.encode("ascii") + b"\n")
            process.stdin.flush()
            header = process.stdout.readline()
            try:
                returned_id, object_type, raw_size = header.decode("ascii").strip().split(" ", 2)
                size = int(raw_size)
            except (UnicodeError, ValueError) as exc:
                raise PackageRefused("Git blob stream returned an invalid header") from exc
            if returned_id != object_id or object_type != "blob" or size != sizes[object_id]:
                raise PackageRefused("Git blob stream differed from its frozen size inventory")
            remaining = size
            chunks: list[bytes] = []
            while remaining:
                chunk = process.stdout.read(remaining)
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            content = b"".join(chunks)
            if len(content) != size or process.stdout.read(1) != b"\n":
                raise PackageRefused("Git blob stream returned truncated content")
            yield entry, content
        process.stdin.close()
        if process.wait(timeout=60) != 0:
            raise PackageRefused("Git blob stream exited unsuccessfully")
    except subprocess.TimeoutExpired as exc:
        raise PackageRefused("Git blob stream timed out") from exc
    finally:
        if process.poll() is None:
            if process.stdin is not None:
                process.stdin.close()
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def _shallow_marker(repository: Path) -> Path:
    raw_git_dir = _git(repository, "rev-parse", "--git-dir")
    if not isinstance(raw_git_dir, str):
        raise PackageRefused("snapshot Git directory could not be resolved")
    git_dir = Path(raw_git_dir)
    if not git_dir.is_absolute():
        git_dir = repository / git_dir
    _check_path_chain(git_dir)
    if not git_dir.is_dir():
        raise PackageRefused("snapshot Git directory is unavailable")
    return git_dir / "shallow"


def _write_shallow_boundary(repository: Path, commit_sha: str) -> None:
    if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", commit_sha):
        raise PackageRefused("snapshot shallow boundary is not a full commit SHA")
    marker = _shallow_marker(repository)
    try:
        marker.write_text(f"{commit_sha}\n", encoding="ascii", newline="\n")
    except OSError as exc:
        raise PackageRefused("snapshot shallow boundary could not be written") from exc


def _assert_single_commit_checkout(
    repository: Path,
    *,
    commit_sha: str,
    tree_sha: str,
    env: Mapping[str, str],
) -> list[dict[str, str]]:
    head = _git(repository, "rev-parse", "--verify", "HEAD^{commit}", env=env)
    tree = _git(repository, "rev-parse", "--verify", f"{commit_sha}^{{tree}}", env=env)
    count = _git(repository, "rev-list", "--count", "HEAD", env=env)
    status = _git(
        repository,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
        env=env,
    )
    remotes = _git(repository, "remote", env=env)
    fsck = _git(repository, "fsck", "--connectivity-only", env=env, timeout=120)
    del fsck
    marker = _shallow_marker(repository)
    try:
        shallow_values = marker.read_text(encoding="ascii").splitlines()
    except (OSError, UnicodeError) as exc:
        raise PackageRefused("restored shallow boundary could not be read") from exc
    if (
        head != commit_sha
        or tree != tree_sha
        or count != "1"
        or status
        or remotes
        or shallow_values != [commit_sha]
    ):
        raise PackageRefused("restored snapshot failed exact-SHA, shallow, or clean-tree checks")
    hooks = repository / ".git" / "hooks"
    if hooks.exists() and any(hooks.iterdir()):
        raise PackageRefused("restored snapshot unexpectedly contains Git hooks")
    return _git_tree_entries(repository, commit_sha)


def _clone_snapshot_bundle(
    bundle_path: Path,
    destination: Path,
    *,
    commit_sha: str,
    tree_sha: str,
    template_dir: Path,
    env: Mapping[str, str],
    materialize: bool = True,
) -> list[dict[str, str]]:
    _run_git(
        [
            "-c",
            "core.longpaths=true",
            "clone",
            "--no-checkout",
            "--template",
            str(template_dir),
            str(bundle_path),
            str(destination),
        ],
        env=env,
        timeout=300,
    )
    _enable_windows_longpaths(destination, env=env)
    head = _git(destination, "rev-parse", "--verify", "HEAD^{commit}", env=env)
    if head != commit_sha:
        raise PackageRefused("snapshot bundle head differs from its manifest")
    remotes = _git(destination, "remote", env=env)
    if remotes.splitlines() != ["origin"]:
        raise PackageRefused("snapshot clone has an unexpected remote configuration")
    _git(destination, "remote", "remove", "origin", env=env)
    _git(destination, "config", "--local", "core.autocrlf", "false", env=env)
    _git(destination, "config", "--local", "core.filemode", "false", env=env)
    _write_shallow_boundary(destination, commit_sha)
    rows = _git_tree_entries(destination, commit_sha)
    if materialize:
        hooks = destination / ".git" / "hooks"
        if hooks.exists() and any(hooks.iterdir()):
            raise PackageRefused("snapshot clone unexpectedly contains Git hooks")
        return _materialize_snapshot_checkout(
            destination,
            commit_sha=commit_sha,
            tree_sha=tree_sha,
            env=env,
        )
    return rows


def _materialize_snapshot_checkout(
    repository: Path,
    *,
    commit_sha: str,
    tree_sha: str,
    env: Mapping[str, str],
) -> list[dict[str, str]]:
    _git(repository, "checkout", "--detach", commit_sha, env=env, timeout=300)
    rows = _assert_single_commit_checkout(
        repository,
        commit_sha=commit_sha,
        tree_sha=tree_sha,
        env=env,
    )
    expected_modules = {
        "launch_gui": Path("launch_gui.py"),
        "reviewctl": Path("reviewctl.py"),
        "gui.app": Path("gui") / "app.py",
        "runtime.control_plane": Path("runtime") / "control_plane.py",
    }
    for module_path in expected_modules.values():
        candidate = repository / module_path
        try:
            info = candidate.lstat()
        except OSError as exc:
            raise PackageRefused("restored snapshot is missing an entry module") from exc
        if _is_reparse(info) or not stat.S_ISREG(info.st_mode):
            raise PackageRefused("restored entry module is not a regular file")
    return rows


def _create_single_commit_bundle(
    source_root: Path,
    *,
    commit_sha: str,
    tree_sha: str,
    temp_root: Path,
) -> Path:
    template_dir = temp_root / "empty-git-template"
    template_dir.mkdir()
    empty_global_config = temp_root / "empty-git-global-config"
    empty_global_config.write_bytes(b"")
    env = _isolated_git_environment(empty_global_config)
    snapshot_repo = temp_root / "source-shallow-clone"
    _run_git(
        [
            "-c",
            "core.longpaths=true",
            "clone",
            "--depth=1",
            "--single-branch",
            "--no-tags",
            "--no-checkout",
            "--template",
            str(template_dir),
            source_root.as_uri(),
            str(snapshot_repo),
        ],
        env=env,
        timeout=300,
    )
    _enable_windows_longpaths(snapshot_repo, env=env)
    if _git(snapshot_repo, "rev-parse", "--verify", "HEAD^{commit}", env=env) != commit_sha:
        raise PackageRefused("depth-one snapshot did not resolve the frozen commit")
    shallow_path = _shallow_marker(snapshot_repo)
    try:
        shallow_values = shallow_path.read_text(encoding="ascii").splitlines()
    except (OSError, UnicodeError) as exc:
        raise PackageRefused("depth-one snapshot did not record its shallow boundary") from exc
    if shallow_values != [commit_sha]:
        raise PackageRefused("depth-one snapshot contains an unexpected history boundary")

    bundle_path = temp_root / SNAPSHOT_BUNDLE_NAME
    _git(
        snapshot_repo,
        "bundle",
        "create",
        "--version=3",
        str(bundle_path),
        "HEAD",
        env=env,
        timeout=300,
    )
    refs = _git(snapshot_repo, "bundle", "list-heads", str(bundle_path), env=env)
    if refs.splitlines() != [f"{commit_sha} HEAD"]:
        raise PackageRefused("snapshot bundle contains an unexpected ref set")

    probe = temp_root / "bundle-restore-probe"
    restored_rows = _clone_snapshot_bundle(
        bundle_path,
        probe,
        commit_sha=commit_sha,
        tree_sha=tree_sha,
        template_dir=template_dir,
        env=env,
    )
    source_rows = _git_tree_entries(source_root, commit_sha)
    source_identity = {
        (row["path"], row["mode"], row["object_id"]) for row in source_rows
    }
    restored_identity = {
        (row["path"], row["mode"], row["object_id"]) for row in restored_rows
    }
    if source_identity != restored_identity:
        raise PackageRefused("restored bundle tree differs from the frozen Git tree")
    return bundle_path


def _is_full_hash(value: Any, *, length: int | None = None) -> bool:
    return (
        isinstance(value, str)
        and (length is None or len(value) == length)
        and len(value) in {40, 64}
        and all(char in "0123456789abcdef" for char in value.casefold())
    )


def _validate_package_manifest(payload: Mapping[str, Any]) -> tuple[dict[str, Mapping[str, Any]], Mapping[str, Any]]:
    if payload.get("package_schema") != PACKAGE_SCHEMA:
        raise PackageRefused("unsupported recovery package manifest schema")
    if payload.get("tracked_tree_only") is not True or payload.get("git_bundle_included") is not True:
        raise PackageRefused("recovery package lacks its exact source snapshot")
    revision = payload.get("source_revision")
    snapshot = payload.get("snapshot")
    if not isinstance(revision, Mapping) or not isinstance(snapshot, Mapping):
        raise PackageRefused("recovery package source identity is incomplete")
    commit_sha = revision.get("git_commit")
    tree_sha = revision.get("git_tree")
    if (
        not isinstance(commit_sha, str)
        or not isinstance(tree_sha, str)
        or not _is_full_hash(commit_sha)
        or not _is_full_hash(tree_sha)
    ):
        raise PackageRefused("recovery package commit or tree identity is invalid")
    if (
        snapshot.get("member") != SNAPSHOT_BUNDLE_NAME
        or snapshot.get("bundle_version") != 3
        or snapshot.get("bundle_ref") != "HEAD"
        or snapshot.get("shallow_boundary") != commit_sha
        or snapshot.get("shallow_commit") != commit_sha
    ):
        raise PackageRefused("recovery package shallow snapshot binding is invalid")
    snapshot_bytes = snapshot.get("bytes")
    snapshot_sha = snapshot.get("sha256")
    if (
        isinstance(snapshot_bytes, bool)
        or not isinstance(snapshot_bytes, int)
        or snapshot_bytes <= 0
        or snapshot_bytes > MAX_PACKAGE_BYTES
        or not isinstance(snapshot_sha, str)
        or len(snapshot_sha) != 64
        or any(char not in "0123456789abcdef" for char in snapshot_sha.casefold())
    ):
        raise PackageRefused("recovery package snapshot size or hash is invalid")

    raw_files = payload.get("files")
    if not isinstance(raw_files, list) or not raw_files:
        raise PackageRefused("recovery package source file manifest is empty")
    files: dict[str, Mapping[str, Any]] = {}
    total_bytes = 0
    for row in raw_files:
        if not isinstance(row, Mapping):
            raise PackageRefused("recovery package file entry is malformed")
        path = row.get("path")
        if not isinstance(path, str):
            raise PackageRefused("recovery package file path is malformed")
        pure = PurePosixPath(path)
        if (
            pure.is_absolute()
            or not pure.parts
            or any(part in {"", ".", ".."} or ":" in part for part in pure.parts)
            or "\\" in path
            or pure.as_posix() != path
        ):
            raise PackageRefused("recovery package contains an unsafe member path")
        if path in files:
            raise PackageRefused("recovery package contains duplicate source paths")
        mode = row.get("git_mode")
        object_id = row.get("git_object_id")
        size = row.get("bytes")
        sha256 = row.get("sha256")
        source_sha256 = row.get("source_git_blob_sha256")
        if mode not in {"100644", "100755"} or not _is_full_hash(object_id, length=len(commit_sha)):
            raise PackageRefused("recovery package file is not an ordinary frozen Git blob")
        if _exclusion_reason(path, str(mode), "blob") is not None:
            raise PackageRefused("recovery package contains a prohibited source path")
        if (
            isinstance(size, bool)
            or not isinstance(size, int)
            or size < 0
            or size > MAX_SINGLE_FILE_BYTES
            or not isinstance(sha256, str)
            or len(sha256) != 64
            or any(char not in "0123456789abcdef" for char in sha256.casefold())
            or source_sha256 != sha256
        ):
            raise PackageRefused("recovery package file size or hash binding is invalid")
        files[path] = row
        total_bytes += size
    if set(REQUIRED_PATHS) - set(files):
        raise PackageRefused("recovery package lacks required source files")
    if (
        payload.get("file_count") != len(files)
        or payload.get("total_source_blob_bytes") != total_bytes
        or payload.get("total_package_bytes") != total_bytes
        or payload.get("total_uncompressed_bytes") != total_bytes + snapshot_bytes
        or total_bytes + snapshot_bytes > MAX_PACKAGE_BYTES
        or len(files) + 2 > MAX_PACKAGE_FILES
    ):
        raise PackageRefused("recovery package member counts or byte totals are inconsistent")
    return files, snapshot


def _verify_archive(archive_path: Path, expected_manifest: Mapping[str, Any]) -> str:
    file_rows, snapshot = _validate_package_manifest(expected_manifest)
    with zipfile.ZipFile(archive_path, "r") as package:
        bad_member = package.testzip()
        if bad_member is not None:
            raise PackageRefused("recovery ZIP failed CRC verification")
        names = package.namelist()
        if len(names) != len(set(names)):
            raise PackageRefused("recovery ZIP contains duplicate members")
        if names.count(PACKAGE_MANIFEST_NAME) != 1:
            raise PackageRefused("recovery ZIP must contain exactly one package manifest")
        manifest_payload = json.loads(package.read(PACKAGE_MANIFEST_NAME).decode("utf-8"))
        if not isinstance(manifest_payload, Mapping) or manifest_payload != expected_manifest:
            raise PackageRefused("recovery ZIP package manifest failed readback")
        expected_members = {*file_rows, SNAPSHOT_BUNDLE_NAME, PACKAGE_MANIFEST_NAME}
        if set(names) != expected_members:
            raise PackageRefused("recovery ZIP members differ from its exact manifest")
        for name, row in file_rows.items():
            content = package.read(name)
            if (
                len(content) != row.get("bytes")
                or hashlib.sha256(content).hexdigest() != row.get("sha256")
            ):
                raise PackageRefused("recovery ZIP member failed independent hash verification")
        bundle_info = package.getinfo(SNAPSHOT_BUNDLE_NAME)
        if bundle_info.file_size != snapshot.get("bytes"):
            raise PackageRefused("recovery Git snapshot failed independent hash verification")
        bundle_digest = hashlib.sha256()
        bundle_size = 0
        with package.open(SNAPSHOT_BUNDLE_NAME, "r") as bundle_member:
            while chunk := bundle_member.read(1024 * 1024):
                bundle_size += len(chunk)
                bundle_digest.update(chunk)
        if bundle_size != snapshot.get("bytes") or bundle_digest.hexdigest() != snapshot.get("sha256"):
            raise PackageRefused("recovery Git snapshot failed independent hash verification")
    with archive_path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def build_recovery_package(
    *,
    source_root: str | Path,
    frozen_sha: str,
    destination: str | Path,
) -> dict[str, Any]:
    """Build an exact source tree plus a verified depth-one Git bundle."""

    root, commit_sha, tree_sha = _verified_clean_source(source_root, frozen_sha)
    output_path = _destination_path(destination, root)
    tree_entries = _git_tree_entries(root, commit_sha)
    included_entries: list[dict[str, str]] = []
    excluded_counts: Counter[str] = Counter()
    for entry in tree_entries:
        reason = _exclusion_reason(entry["path"], entry["mode"], entry["object_type"])
        if reason:
            excluded_counts[reason] += 1
            continue
        included_entries.append(entry)
    if excluded_counts:
        excluded_total = sum(excluded_counts.values())
        reasons = ", ".join(
            f"{name}={count}" for name, count in sorted(excluded_counts.items())
        )
        raise PackageRefused(
            f"exact-SHA snapshot refused: {excluded_total} tracked path(s) match "
            f"the excluded-data policy ({reasons})"
        )
    included_paths = {entry["path"] for entry in included_entries}
    missing_required = sorted(REQUIRED_PATHS - included_paths)
    if missing_required:
        raise PackageRefused("frozen source lacks required recovery package files")
    blob_sizes = _git_blob_sizes(root, included_entries)
    expected_total = sum(blob_sizes[row["object_id"]] for row in included_entries)
    if len(included_entries) + 2 > MAX_PACKAGE_FILES or expected_total > MAX_PACKAGE_BYTES:
        raise PackageRefused("tracked source exceeds the recovery package size limit")

    included_entries.sort(key=lambda row: row["path"])
    for entry, source_content in _git_blob_batch(root, included_entries, blob_sizes):
        if entry["path"] in {"config.ini.example", ".env.example"}:
            _validate_config_template(entry["path"], source_content)
        _check_for_secret_material(source_content)

    manifest: dict[str, Any] | None = None
    manifest_bytes = b""
    archive_sha = ""
    bundle_sha256 = ""
    bundle_bytes = 0
    package_placed = False
    try:
        with tempfile.TemporaryDirectory(
            prefix=f".run-first-source-{commit_sha[:10]}-",
            dir=output_path.parent,
        ) as temporary_directory:
            temporary_root = Path(temporary_directory)
            _check_path_chain(temporary_root)
            # Git metadata needs a short root; ZIP publication stays on the
            # destination volume so the final replacement remains atomic.
            with tempfile.TemporaryDirectory(prefix=".run-first-git-") as git_directory:
                git_root = Path(git_directory)
                _check_path_chain(git_root)
                git_bundle_path = _create_single_commit_bundle(
                    root,
                    commit_sha=commit_sha,
                    tree_sha=tree_sha,
                    temp_root=git_root,
                )
                bundle_sha256 = _sha256_file(git_bundle_path)
                bundle_path = temporary_root / SNAPSHOT_BUNDLE_NAME
                shutil.copyfile(git_bundle_path, bundle_path)
                _check_path_chain(bundle_path)
            bundle_bytes = bundle_path.stat().st_size
            if bundle_bytes <= 0 or expected_total + bundle_bytes > MAX_PACKAGE_BYTES:
                raise PackageRefused("source tree and Git snapshot exceed the package size limit")
            with bundle_path.open("rb") as bundle_handle:
                if hashlib.file_digest(bundle_handle, "sha256").hexdigest() != bundle_sha256:
                    raise PackageRefused("copied Git snapshot differs from its verified source")
            temporary_path = temporary_root / "recovery-package.zip"
            file_rows: list[dict[str, Any]] = []
            with zipfile.ZipFile(
                temporary_path,
                mode="x",
                compression=zipfile.ZIP_DEFLATED,
                compresslevel=6,
            ) as package:
                package_total_bytes = 0
                for entry, source_content in _git_blob_batch(root, included_entries, blob_sizes):
                    if entry["path"] in {"config.ini.example", ".env.example"}:
                        _validate_config_template(entry["path"], source_content)
                    _check_for_secret_material(source_content)
                    source_sha256 = hashlib.sha256(source_content).hexdigest()
                    package.writestr(entry["path"], source_content)
                    package_total_bytes += len(source_content)
                    file_rows.append(
                        {
                            "path": entry["path"],
                            "git_mode": entry["mode"],
                            "git_object_id": entry["object_id"],
                            "bytes": len(source_content),
                            "sha256": source_sha256,
                            "source_git_blob_sha256": source_sha256,
                        }
                    )
                if len(file_rows) != len(included_entries):
                    raise PackageRefused("not all frozen source blobs were packaged")
                package.write(bundle_path, arcname=SNAPSHOT_BUNDLE_NAME)
                manifest = {
                    "package_schema": PACKAGE_SCHEMA,
                    "created_at_utc": datetime.now(timezone.utc).isoformat(),
                    "source_revision": {"git_commit": commit_sha, "git_tree": tree_sha},
                    "tracked_tree_only": True,
                    "git_bundle_included": True,
                    "snapshot": {
                        "member": SNAPSHOT_BUNDLE_NAME,
                        "bundle_version": 3,
                        "bundle_ref": "HEAD",
                        "bytes": bundle_bytes,
                        "sha256": bundle_sha256,
                        "shallow_commit": commit_sha,
                        "shallow_boundary": commit_sha,
                    },
                    "configuration_templates": {
                        "paths": [
                            path for path in ("config.ini.example", ".env.example")
                            if path in included_paths
                        ],
                        "policy": "credential fields must be explicit placeholders; source blob bytes are preserved",
                    },
                    "excluded_tracked_entry_counts_by_reason": {},
                    "files": file_rows,
                    "file_count": len(file_rows),
                    "total_source_blob_bytes": expected_total,
                    "total_package_bytes": package_total_bytes,
                    "total_uncompressed_bytes": package_total_bytes + bundle_bytes,
                }
                _validate_package_manifest(manifest)
                manifest_bytes = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
                if len(manifest_bytes) > MAX_MANIFEST_BYTES:
                    raise PackageRefused("recovery package manifest exceeds its size limit")
                package.writestr(PACKAGE_MANIFEST_NAME, manifest_bytes)
            if manifest is None:
                raise PackageRefused("package manifest was not generated")
            if os.path.lexists(output_path):
                raise PackageRefused("recovery package destination appeared during build")
            archive_sha = _verify_archive(temporary_path, manifest)
            os.replace(temporary_path, output_path)
            package_placed = True
            if _verify_archive(output_path, manifest) != archive_sha:
                raise PackageRefused("recovery package changed during final placement")
    except Exception:
        if package_placed:
            try:
                output_path.unlink(missing_ok=True)
            except OSError:
                pass
        raise
    return {
        "package_schema": PACKAGE_SCHEMA,
        "source_git_commit": commit_sha,
        "source_git_tree": tree_sha,
        "package_path": str(output_path),
        "package_bytes": output_path.stat().st_size,
        "package_sha256": archive_sha,
        "package_manifest_member": PACKAGE_MANIFEST_NAME,
        "package_manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "included_file_count": len(manifest["files"]),
        "included_bytes": int(manifest["total_package_bytes"]),
        "snapshot_bundle_sha256": bundle_sha256,
        "snapshot_bundle_bytes": bundle_bytes,
        "excluded_tracked_entry_counts_by_reason": {},
        "git_bundle_included": True,
    }


def _checked_package_file(value: str | Path) -> tuple[Path, int]:
    raw = Path(value).expanduser()
    if not raw.is_absolute() or ".." in raw.parts:
        raise PackageRefused("recovery package path must be absolute and normalized")
    path = Path(os.path.abspath(raw))
    _check_path_chain(path)
    try:
        info = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise PackageRefused("recovery package file is unavailable") from exc
    if _is_reparse(info) or not stat.S_ISREG(info.st_mode) or _normcase(path) != _normcase(resolved):
        raise PackageRefused("recovery package must be a regular non-reparse file")
    if info.st_size > MAX_PACKAGE_BYTES + MAX_MANIFEST_BYTES:
        raise PackageRefused("recovery package archive exceeds its size limit")
    return resolved, info.st_size


def _restore_destination_path(value: str | Path) -> Path:
    raw = Path(value).expanduser()
    if not raw.is_absolute() or ".." in raw.parts:
        raise PackageRefused("restore destination must be absolute and normalized")
    destination = Path(os.path.abspath(raw))
    parent = destination.parent
    _check_path_chain(parent)
    try:
        resolved_parent = parent.resolve(strict=True)
    except OSError as exc:
        raise PackageRefused("restore destination parent is unavailable") from exc
    if _normcase(resolved_parent) != _normcase(parent) or not resolved_parent.is_dir():
        raise PackageRefused("restore destination parent resolves through an alias")
    destination = resolved_parent / destination.name
    if os.path.lexists(destination):
        raise PackageRefused("restore destination already exists")
    return destination


def _sha256_file(path: Path) -> str:
    try:
        with path.open("rb") as handle:
            return hashlib.file_digest(handle, "sha256").hexdigest()
    except OSError as exc:
        raise PackageRefused("recovery package file could not be hashed") from exc


def _verify_bundle_tree_against_files(
    repository: Path,
    *,
    commit_sha: str,
    expected_tree_sha: str,
    file_rows: Mapping[str, Mapping[str, Any]],
    package: zipfile.ZipFile,
    env: Mapping[str, str],
) -> list[dict[str, str]]:
    observed_tree = _git(repository, "rev-parse", "--verify", f"{commit_sha}^{{tree}}", env=env)
    if observed_tree != expected_tree_sha:
        raise PackageRefused("snapshot bundle tree hash differs from its manifest")
    entries = _git_tree_entries(repository, commit_sha)
    tree_identity = {
        entry["path"]: (entry["mode"], entry["object_id"]) for entry in entries
    }
    manifest_identity = {
        path: (str(row.get("git_mode")), str(row.get("git_object_id")))
        for path, row in file_rows.items()
    }
    if tree_identity != manifest_identity:
        raise PackageRefused("snapshot bundle tree entries differ from source file manifest")
    sizes = _git_blob_sizes(repository, entries)
    for entry, blob in _git_blob_batch(repository, entries, sizes):
        row = file_rows[entry["path"]]
        blob_sha = hashlib.sha256(blob).hexdigest()
        if (
            len(blob) != row.get("bytes")
            or blob_sha != row.get("sha256")
            or blob_sha != row.get("source_git_blob_sha256")
        ):
            raise PackageRefused("snapshot Git object differs from its hash-bound ZIP member")
        if entry["path"] in {"config.ini.example", ".env.example"}:
            _validate_config_template(entry["path"], blob)
        _check_for_secret_material(blob)
        try:
            zip_bytes = package.read(entry["path"])
        except (KeyError, OSError, zipfile.BadZipFile) as exc:
            raise PackageRefused("snapshot source member could not be read") from exc
        if zip_bytes != blob:
            raise PackageRefused("snapshot Git object bytes differ from ZIP source bytes")
    return entries


def _verify_module_origins(root: Path) -> dict[str, str]:
    from importlib.machinery import PathFinder

    search_rows = {
        "launch_gui": (root, Path("launch_gui.py")),
        "reviewctl": (root, Path("reviewctl.py")),
        "gui.app": (root / "gui", Path("gui") / "app.py"),
        "runtime.control_plane": (root / "runtime", Path("runtime") / "control_plane.py"),
    }
    origins: dict[str, str] = {}
    for name, (search_path, relative) in search_rows.items():
        expected = (root / relative).resolve(strict=True)
        spec = PathFinder.find_spec(name, [str(search_path)])
        if spec is None or not spec.origin:
            raise PackageRefused("restored source is missing a required entry module origin")
        actual = Path(spec.origin).resolve(strict=True)
        if _normcase(actual) != _normcase(expected):
            raise PackageRefused("restored module resolves outside the verified snapshot")
        origins[name] = str(actual)
    return origins


def restore_recovery_package(
    *,
    package_path: str | Path,
    expected_package_sha256: str,
    expected_commit: str,
    destination: str | Path,
) -> dict[str, Any]:
    """Restore a clean, exact-SHA checkout from a verified one-commit bundle."""

    source, source_size = _checked_package_file(package_path)
    destination_path = _restore_destination_path(destination)
    if not isinstance(expected_package_sha256, str):
        raise PackageRefused("restore requires the exact external package SHA-256")
    expected_digest = expected_package_sha256.strip().casefold()
    if len(expected_digest) != 64 or any(char not in "0123456789abcdef" for char in expected_digest):
        raise PackageRefused("restore requires the exact external package SHA-256")
    if not _is_full_hash(expected_commit):
        raise PackageRefused("restore requires the exact full source commit SHA")
    archive_digest = _sha256_file(source)
    if archive_digest != expected_digest:
        raise PackageRefused("recovery package archive SHA-256 does not match the supplied receipt")

    try:
        with zipfile.ZipFile(source, "r") as package:
            if package.testzip() is not None:
                raise PackageRefused("recovery ZIP failed CRC verification")
            names = package.namelist()
            if len(names) != len(set(names)):
                raise PackageRefused("recovery ZIP contains duplicate members")
            if names.count(PACKAGE_MANIFEST_NAME) != 1:
                raise PackageRefused("recovery ZIP must contain exactly one package manifest")
            manifest_info = package.getinfo(PACKAGE_MANIFEST_NAME)
            if manifest_info.file_size > MAX_MANIFEST_BYTES:
                raise PackageRefused("recovery package manifest exceeds its size limit")
            payload = json.loads(package.read(PACKAGE_MANIFEST_NAME).decode("utf-8"))
            if not isinstance(payload, Mapping):
                raise PackageRefused("recovery package manifest root must be an object")
            file_rows, snapshot = _validate_package_manifest(payload)
            revision = payload["source_revision"]
            commit_sha = str(revision["git_commit"]).casefold()
            tree_sha = str(revision["git_tree"]).casefold()
            if commit_sha != expected_commit.casefold():
                raise PackageRefused("recovery package commit differs from the supplied source SHA")
            expected_members = {*file_rows, SNAPSHOT_BUNDLE_NAME, PACKAGE_MANIFEST_NAME}
            if set(names) != expected_members:
                raise PackageRefused("recovery ZIP members differ from its exact manifest")

            for path, row in file_rows.items():
                info = package.getinfo(path)
                if info.file_size != row.get("bytes") or info.file_size > MAX_SINGLE_FILE_BYTES:
                    raise PackageRefused("recovery ZIP source member size differs from its manifest")
                try:
                    content = package.read(path)
                except (KeyError, OSError, zipfile.BadZipFile) as exc:
                    raise PackageRefused("recovery ZIP source member could not be read") from exc
                if hashlib.sha256(content).hexdigest() != row.get("sha256"):
                    raise PackageRefused("recovery ZIP source member failed SHA-256 verification")
                if path in {"config.ini.example", ".env.example"}:
                    _validate_config_template(path, content)
                _check_for_secret_material(content)

            bundle_info = package.getinfo(SNAPSHOT_BUNDLE_NAME)
            if bundle_info.file_size != snapshot.get("bytes"):
                raise PackageRefused("recovery Git snapshot size differs from its manifest")
            with tempfile.TemporaryDirectory(
                prefix=f".run-first-restore-{commit_sha[:10]}-",
                dir=destination_path.parent,
            ) as temporary_directory:
                temporary_root = Path(temporary_directory)
                empty_global_config = temporary_root / "empty-git-global-config"
                empty_global_config.write_bytes(b"")
                env = _isolated_git_environment(empty_global_config)
                template_dir = temporary_root / "empty-git-template"
                template_dir.mkdir()
                bundle_path = temporary_root / SNAPSHOT_BUNDLE_NAME
                bundle_hash = hashlib.sha256()
                bundle_size = 0
                try:
                    with package.open(SNAPSHOT_BUNDLE_NAME, "r") as bundle_member, bundle_path.open(
                        "xb"
                    ) as output:
                        while chunk := bundle_member.read(1024 * 1024):
                            bundle_size += len(chunk)
                            bundle_hash.update(chunk)
                            output.write(chunk)
                        output.flush()
                        os.fsync(output.fileno())
                except (OSError, KeyError, zipfile.BadZipFile) as exc:
                    raise PackageRefused("recovery Git snapshot could not be staged") from exc
                if bundle_size != snapshot.get("bytes") or bundle_hash.hexdigest() != snapshot.get("sha256"):
                    raise PackageRefused("recovery Git snapshot failed SHA-256 verification")

                if _sha256_file(source) != expected_digest:
                    raise PackageRefused("recovery package changed during validation")
                staging = temporary_root / "verified-checkout"
                rows = _clone_snapshot_bundle(
                    bundle_path,
                    staging,
                    commit_sha=commit_sha,
                    tree_sha=tree_sha,
                    template_dir=template_dir,
                    env=env,
                    materialize=False,
                )
                verified_rows = _verify_bundle_tree_against_files(
                    staging,
                    commit_sha=commit_sha,
                    expected_tree_sha=tree_sha,
                    file_rows=file_rows,
                    package=package,
                    env=env,
                )
                if {
                    (row["path"], row["mode"], row["object_id"]) for row in rows
                } != {
                    (row["path"], row["mode"], row["object_id"]) for row in verified_rows
                }:
                    raise PackageRefused("restored bundle tree changed during verification")

                _materialize_snapshot_checkout(
                    staging,
                    commit_sha=commit_sha,
                    tree_sha=tree_sha,
                    env=env,
                )
                for path, row in file_rows.items():
                    output_path = staging.joinpath(*PurePosixPath(path).parts)
                    try:
                        info = output_path.lstat()
                        with output_path.open("rb") as handle:
                            observed_sha = hashlib.file_digest(handle, "sha256").hexdigest()
                    except OSError as exc:
                        raise PackageRefused("restored source file failed readback") from exc
                    if _is_reparse(info) or not stat.S_ISREG(info.st_mode) or observed_sha != row.get("sha256"):
                        raise PackageRefused("restored source file differs from verified ZIP bytes")
                origins = _verify_module_origins(staging)
                if _sha256_file(source) != expected_digest:
                    raise PackageRefused("recovery package changed before restore completed")
                if os.path.lexists(destination_path):
                    raise PackageRefused("restore destination appeared during verification")
                os.replace(staging, destination_path)
                _assert_single_commit_checkout(
                    destination_path,
                    commit_sha=commit_sha,
                    tree_sha=tree_sha,
                    env=env,
                )
                origins = _verify_module_origins(destination_path)
    except (zipfile.BadZipFile, UnicodeError, json.JSONDecodeError) as exc:
        raise PackageRefused("recovery ZIP or manifest is malformed") from exc

    return {
        "result": "restored_verified",
        "source_git_commit": commit_sha,
        "source_git_tree": tree_sha,
        "package_path": str(source),
        "package_sha256": archive_digest,
        "package_bytes": source_size,
        "restore_path": str(destination_path),
        "git_history_commits": 1,
        "shallow_boundary": commit_sha,
        "working_tree_clean": True,
        "remotes": [],
        "module_origins": origins,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="operation", required=True)
    build_parser = commands.add_parser("build", help="build a verified exact-SHA source package")
    build_parser.add_argument("--source-root", type=Path, required=True)
    build_parser.add_argument("--frozen-sha", required=True)
    build_parser.add_argument("--destination", type=Path, required=True)
    restore_parser = commands.add_parser("restore", help="restore and verify a source package")
    restore_parser.add_argument("--package", type=Path, required=True)
    restore_parser.add_argument("--expected-package-sha256", required=True)
    restore_parser.add_argument("--expected-commit", required=True)
    restore_parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.operation == "build":
            report = build_recovery_package(
                source_root=args.source_root,
                frozen_sha=args.frozen_sha,
                destination=args.destination,
            )
        else:
            report = restore_recovery_package(
                package_path=args.package,
                expected_package_sha256=args.expected_package_sha256,
                expected_commit=args.expected_commit,
                destination=args.destination,
            )
    except (PackageRefused, OSError, ValueError, TypeError) as exc:
        print(json.dumps({"result": "refused", "reason": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps({"result": "verified", **report}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
