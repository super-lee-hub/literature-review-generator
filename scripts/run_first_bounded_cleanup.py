"""Dry-run-first, exact-inventory cleanup for the five named fixture roots."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import tempfile
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath
from typing import Any

INVENTORY_SCHEMA = "run-first-recovery-cleanup-inventory/v1"
DEFAULT_ALLOWED_PARENT = Path(r"D:\tmp")
DEFAULT_INVENTORY = (
    Path(__file__).resolve().parents[1]
    / "acceptance_artifacts"
    / "run_first_20261004"
    / "RECOVERY_CLEANUP_INVENTORY_20261004.json"
)
MAX_ENTRIES_PER_ROOT = 500_000
REPARSE_FLAG = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


class CleanupRefused(RuntimeError):
    """A cleanup inventory or target failed a safety precondition."""


def _normcase(path: str | Path) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def _is_reparse_info(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & REPARSE_FLAG
    )


def _check_no_reparse_chain(path: Path) -> list[str]:
    """Check each named component with lstat so ancestors are not followed."""

    issues: list[str] = []
    for component in reversed((path, *path.parents)):
        try:
            info = component.lstat()
        except OSError as exc:
            issues.append(f"missing_or_unreadable_path:{component}:{type(exc).__name__}")
            continue
        if _is_reparse_info(info):
            issues.append(f"reparse_point:{component}")
    return issues


def _resolve_checked_directory(value: str | Path, *, label: str) -> Path:
    raw = Path(value).expanduser()
    if not raw.is_absolute() or ".." in PureWindowsPath(str(raw)).parts:
        raise CleanupRefused(f"{label} must be an absolute normalized path")
    absolute = Path(os.path.abspath(raw))
    issues = _check_no_reparse_chain(absolute)
    if issues:
        raise CleanupRefused(f"{label} path contains a reparse point or missing ancestor")
    try:
        resolved = absolute.resolve(strict=True)
    except OSError as exc:
        raise CleanupRefused(f"{label} path is unavailable") from exc
    if _normcase(resolved) != _normcase(absolute) or not resolved.is_dir():
        raise CleanupRefused(f"{label} path resolves through an alias or is not a directory")
    return resolved


def _relative_parts(value: Any) -> tuple[str, ...]:
    if not isinstance(value, str) or not value.strip():
        raise CleanupRefused("manifest relative_path must be a non-empty string")
    relative = PureWindowsPath(value)
    parts = relative.parts
    if (
        relative.is_absolute()
        or relative.drive
        or relative.root
        or not parts
        or any(part in {"", ".", ".."} for part in parts)
        or any(":" in part for part in parts)
    ):
        raise CleanupRefused(f"unsafe manifest relative_path: {value!r}")
    return tuple(parts)


def _relative_key(parts: tuple[str, ...]) -> str:
    return "\\".join(parts).casefold()


def _path_is_within(path: Path, root: Path) -> bool:
    try:
        return os.path.commonpath((_normcase(path), _normcase(root))) == _normcase(root)
    except ValueError:
        return False


def _sha256_regular_file(path: Path) -> tuple[int, str, os.stat_result]:
    try:
        before = path.lstat()
    except OSError as exc:
        raise CleanupRefused(f"listed file is missing or unreadable: {path}") from exc
    if _is_reparse_info(before) or not stat.S_ISREG(before.st_mode):
        raise CleanupRefused(f"listed target is not a regular non-reparse file: {path}")

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as handle:
            opened = os.fstat(handle.fileno())
            if _is_reparse_info(opened) or not stat.S_ISREG(opened.st_mode):
                raise CleanupRefused(f"opened target is not a regular file: {path}")
            digest = hashlib.file_digest(handle, "sha256").hexdigest()
    except CleanupRefused:
        raise
    except OSError as exc:
        raise CleanupRefused(f"listed file changed or cannot be read: {path}") from exc

    try:
        after = path.lstat()
    except OSError as exc:
        raise CleanupRefused(f"listed file changed while hashing: {path}") from exc
    if _is_reparse_info(after) or not stat.S_ISREG(after.st_mode):
        raise CleanupRefused(f"listed file became a reparse or non-regular target: {path}")
    if (
        before.st_size != opened.st_size
        or opened.st_size != after.st_size
        or getattr(before, "st_mtime_ns", None) != getattr(after, "st_mtime_ns", None)
    ):
        raise CleanupRefused(f"listed file changed while hashing: {path}")
    return after.st_size, digest, after


def _expected_files(root_row: Mapping[str, Any]) -> tuple[dict[str, dict[str, Any]], set[str]]:
    rows = root_row.get("files")
    if not isinstance(rows, list) or not rows:
        raise CleanupRefused("cleanup root must contain an explicit non-empty files array")

    expected: dict[str, dict[str, Any]] = {}
    directories: set[str] = set()
    total_bytes = 0
    for row in rows:
        if not isinstance(row, Mapping):
            raise CleanupRefused("cleanup file entry must be an object")
        parts = _relative_parts(row.get("relative_path"))
        key = _relative_key(parts)
        if key in expected:
            raise CleanupRefused("cleanup manifest contains duplicate relative paths")
        raw_bytes = row.get("bytes")
        raw_hash = row.get("sha256")
        if isinstance(raw_bytes, bool) or not isinstance(raw_bytes, int) or raw_bytes < 0:
            raise CleanupRefused("cleanup file byte count must be a non-negative integer")
        if (
            not isinstance(raw_hash, str)
            or len(raw_hash) != 64
            or any(char not in "0123456789abcdef" for char in raw_hash.lower())
        ):
            raise CleanupRefused("cleanup file sha256 must be 64 hexadecimal characters")
        expected[key] = {
            "parts": parts,
            "relative_path": "\\".join(parts),
            "bytes": raw_bytes,
            "sha256": raw_hash.lower(),
        }
        total_bytes += raw_bytes
        for index in range(1, len(parts)):
            directories.add(_relative_key(parts[:index]))

    declared_count = root_row.get("file_count")
    declared_bytes = root_row.get("bytes")
    if declared_count != len(expected) or declared_bytes != total_bytes:
        raise CleanupRefused("cleanup root counts do not match its explicit file entries")
    return expected, directories


def _scan_root(
    root_row: Mapping[str, Any],
    *,
    allowed_parent: Path,
) -> dict[str, Any]:
    raw_root = root_row.get("absolute_root_candidate")
    if not isinstance(raw_root, str) or not Path(raw_root).is_absolute():
        raise CleanupRefused("cleanup root must be an absolute path")
    if ".." in PureWindowsPath(raw_root).parts:
        raise CleanupRefused("cleanup root may not contain parent traversal")
    root = Path(os.path.abspath(raw_root))
    if _normcase(root.parent) != _normcase(allowed_parent):
        raise CleanupRefused("cleanup root must be a direct child of the allowed parent")
    reparse_issues = _check_no_reparse_chain(root)
    if reparse_issues:
        raise CleanupRefused("; ".join(reparse_issues))
    try:
        resolved_root = root.resolve(strict=True)
    except OSError as exc:
        raise CleanupRefused("cleanup root is unavailable") from exc
    if _normcase(resolved_root) != _normcase(root):
        raise CleanupRefused("cleanup root resolves through an alias or reparse point")
    root = resolved_root
    if not root.is_dir():
        raise CleanupRefused("cleanup root is not a directory")

    expected, expected_directories = _expected_files(root_row)
    actual_files: dict[str, dict[str, Any]] = {}
    actual_directories: set[str] = set()
    unexpected: list[str] = []
    reparse_paths: list[str] = []
    scan_issues: list[str] = []
    scanned_entries = 0
    pending = [root]

    while pending:
        current = pending.pop()
        chain_issues = _check_no_reparse_chain(current)
        if chain_issues:
            reparse_paths.extend(
                item.split(":", 1)[-1]
                for item in chain_issues
                if item.startswith("reparse_point:")
            )
            scan_issues.extend(chain_issues)
            continue
        try:
            with os.scandir(current) as iterator:
                entries = sorted(iterator, key=lambda entry: entry.name.casefold())
        except OSError as exc:
            scan_issues.append(f"directory_unreadable:{current}:{type(exc).__name__}")
            continue

        for entry in entries:
            scanned_entries += 1
            if scanned_entries > MAX_ENTRIES_PER_ROOT:
                scan_issues.append("root_entry_limit_exceeded")
                pending.clear()
                break
            path = Path(entry.path)
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError as exc:
                scan_issues.append(f"entry_unreadable:{path}:{type(exc).__name__}")
                continue
            relative = path.relative_to(root)
            parts = tuple(relative.parts)
            key = _relative_key(parts)
            if _is_reparse_info(info):
                reparse_paths.append(str(path))
                scan_issues.append(f"reparse_point:{path}")
            elif stat.S_ISDIR(info.st_mode):
                if key not in expected_directories:
                    unexpected.append(str(relative))
                    continue
                actual_directories.add(key)
                pending.append(path)
            elif stat.S_ISREG(info.st_mode):
                if key not in expected:
                    unexpected.append(str(relative))
                    actual_files[key] = {
                        "path": path,
                        "relative_path": "\\".join(parts),
                        "bytes": info.st_size,
                    }
                else:
                    actual_files[key] = {
                        "path": path,
                        "relative_path": "\\".join(parts),
                        "bytes": info.st_size,
                    }
            else:
                scan_issues.append(f"non_regular_entry:{path}")

    if set(actual_directories) != expected_directories:
        missing_directories = sorted(expected_directories - actual_directories)
        if missing_directories:
            scan_issues.append("missing_expected_directories:" + ",".join(missing_directories))
    if unexpected:
        scan_issues.append("unexpected_entries_present")

    expected_records: list[dict[str, Any]] = []
    for key, item in expected.items():
        actual = actual_files.get(key)
        planned = root.joinpath(*item["parts"])
        if not _path_is_within(planned, root):
            scan_issues.append(f"listed_path_outside_root:{item['relative_path']}")
            continue
        if actual is None:
            scan_issues.append(f"listed_file_missing:{item['relative_path']}")
            expected_records.append(
                {
                    "relative_path": item["relative_path"],
                    "bytes": item["bytes"],
                    "sha256": item["sha256"],
                    "absolute_path": str(planned),
                    "verified": False,
                }
            )
            continue
        if actual["relative_path"] != item["relative_path"]:
            scan_issues.append(f"listed_path_spelling_changed:{item['relative_path']}")
        if actual["bytes"] != item["bytes"]:
            scan_issues.append(f"listed_file_size_mismatch:{item['relative_path']}")
        try:
            observed_bytes, observed_hash, _info = _sha256_regular_file(actual["path"])
        except CleanupRefused as exc:
            scan_issues.append(str(exc))
            observed_bytes, observed_hash = actual["bytes"], ""
        if observed_bytes != item["bytes"] or observed_hash != item["sha256"]:
            scan_issues.append(f"listed_file_hash_mismatch:{item['relative_path']}")
        expected_records.append(
            {
                "relative_path": item["relative_path"],
                "bytes": observed_bytes,
                "sha256": observed_hash,
                "absolute_path": str(actual["path"]),
                "verified": observed_bytes == item["bytes"] and observed_hash == item["sha256"],
            }
        )

    expected_keys = set(expected)
    missing_files = expected_keys - set(actual_files)
    if missing_files and not any(issue.startswith("listed_file_missing:") for issue in scan_issues):
        scan_issues.append("listed_files_missing")

    if root_row.get("exists") is not True:
        scan_issues.append("inventory_did_not_record_root_as_existing")
    if root_row.get("registry_or_evidence_present") is not False:
        scan_issues.append("root_has_or_may_have_registry_or_unique_evidence")
    if root_row.get("reparse_points_skipped") != 0:
        scan_issues.append("inventory_skipped_reparse_points")

    actual_bytes = sum(int(row["bytes"]) for row in actual_files.values())
    result = "ready" if not scan_issues else "refused"
    return {
        "absolute_root": str(root),
        "resolved_root": str(root.resolve(strict=True)),
        "result": result,
        "issues": sorted(set(scan_issues)),
        "unexpected_entries": sorted(set(unexpected)),
        "reparse_checks": {
            "ancestor_chain_checked": True,
            "descendant_entries_scanned": scanned_entries,
            "reparse_paths": sorted(set(reparse_paths)),
        },
        "enumerated_file_count": len(actual_files),
        "enumerated_bytes": actual_bytes,
        "per_file_relative_path_bytes_sha256": expected_records,
        "planned_paths": [row["absolute_path"] for row in expected_records],
        "deleted_paths": [],
        "post_delete_verification": {
            "performed": False,
            "remaining_enumerated_files": None,
        },
        "_private_expected": expected,
    }


def _verify_empty_root_after_unlinks(root: Path, root_row: Mapping[str, Any]) -> dict[str, Any]:
    """Verify listed files are gone and only their original directories remain."""

    try:
        expected, expected_directories = _expected_files(root_row)
    except CleanupRefused as exc:
        return {"performed": True, "passed": False, "issues": [str(exc)], "remaining_files": 0}
    del expected
    issues = _check_no_reparse_chain(root)
    if issues or not root.is_dir():
        return {
            "performed": True,
            "passed": False,
            "issues": issues or ["cleanup_root_missing_after_unlinks"],
            "remaining_files": 0,
        }

    actual_directories: set[str] = set()
    remaining_files: list[str] = []
    reparse_paths: list[str] = []
    unexpected_entries: list[str] = []
    scan_issues: list[str] = []
    pending = [root]
    scanned_entries = 0
    while pending:
        current = pending.pop()
        chain_issues = _check_no_reparse_chain(current)
        if chain_issues:
            reparse_paths.extend(
                item.split(":", 1)[-1]
                for item in chain_issues
                if item.startswith("reparse_point:")
            )
            scan_issues.extend(chain_issues)
            continue
        try:
            with os.scandir(current) as iterator:
                entries = sorted(iterator, key=lambda entry: entry.name.casefold())
        except OSError as exc:
            scan_issues.append(f"directory_unreadable:{current}:{type(exc).__name__}")
            continue
        for entry in entries:
            scanned_entries += 1
            if scanned_entries > MAX_ENTRIES_PER_ROOT:
                scan_issues.append("post_delete_root_entry_limit_exceeded")
                pending.clear()
                break
            path = Path(entry.path)
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError as exc:
                scan_issues.append(f"entry_unreadable:{path}:{type(exc).__name__}")
                continue
            if _is_reparse_info(info):
                reparse_paths.append(str(path))
                scan_issues.append(f"reparse_point_after_unlinks:{path}")
            elif stat.S_ISDIR(info.st_mode):
                relative = path.relative_to(root)
                key = _relative_key(tuple(relative.parts))
                if key not in expected_directories:
                    unexpected_entries.append(str(relative))
                    scan_issues.append(f"unexpected_directory_after_unlinks:{relative}")
                    continue
                actual_directories.add(key)
                pending.append(path)
            elif stat.S_ISREG(info.st_mode):
                remaining_files.append(str(path))
            else:
                unexpected_entries.append(str(path.relative_to(root)))
                scan_issues.append(f"non_regular_entry_after_unlinks:{path}")

    missing_directories = expected_directories - actual_directories
    if missing_directories:
        scan_issues.append(
            "expected_directories_missing_after_unlinks:"
            + ",".join(sorted(missing_directories))
        )
    if remaining_files:
        scan_issues.append("files_remain_after_unlinks")
    return {
        "performed": True,
        "passed": not scan_issues,
        "issues": sorted(set(scan_issues)),
        "remaining_files": len(remaining_files),
        "remaining_paths": remaining_files,
        "unexpected_entries": sorted(set(unexpected_entries)),
        "reparse_paths": sorted(set(reparse_paths)),
        "descendant_entries_scanned": scanned_entries,
        "root_preserved": root.is_dir(),
    }


def _validate_inventory(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    if payload.get("schema_version") != INVENTORY_SCHEMA:
        raise CleanupRefused("unsupported cleanup inventory schema")
    proposal = payload.get("proposed_minimal_cleanup_manifest")
    if not isinstance(proposal, Mapping):
        raise CleanupRefused("cleanup inventory has no proposed minimal manifest")
    if proposal.get("status") != "proposal_only_not_executed":
        raise CleanupRefused("cleanup inventory is not in proposal-only state")
    if proposal.get("cleanup_code_or_deletion_performed") is not False:
        raise CleanupRefused("cleanup inventory says cleanup was already performed")
    roots = proposal.get("cleanup_roots")
    if not isinstance(roots, list) or not roots or any(not isinstance(row, Mapping) for row in roots):
        raise CleanupRefused("cleanup inventory has no valid named cleanup roots")
    excluded = proposal.get("excluded_roots", [])
    if not isinstance(excluded, list) or any(not isinstance(path, str) for path in excluded):
        raise CleanupRefused("cleanup inventory has malformed excluded roots")
    for row in roots:
        candidate = row.get("absolute_root_candidate")
        if not isinstance(candidate, str) or not Path(candidate).is_absolute():
            raise CleanupRefused("cleanup root must be an absolute path")
        for excluded_path in excluded:
            if _path_is_within(Path(candidate), Path(excluded_path)) or _path_is_within(
                Path(excluded_path), Path(candidate)
            ):
                raise CleanupRefused("cleanup candidate overlaps an explicitly excluded root")
    declared_total = proposal.get("candidate_total_bytes")
    observed_total = sum(
        int(row.get("bytes"))
        for row in roots
        if isinstance(row.get("bytes"), int) and not isinstance(row.get("bytes"), bool)
    )
    if declared_total != observed_total:
        raise CleanupRefused("cleanup root byte total does not match the inventory")
    return roots


def _safe_receipt_path(receipt_path: str | Path, root_rows: list[Mapping[str, Any]]) -> Path:
    path = Path(receipt_path).expanduser()
    if not path.is_absolute() or ".." in PureWindowsPath(str(path)).parts:
        raise CleanupRefused("receipt path must be absolute and normalized")
    parent = _resolve_checked_directory(path.parent, label="receipt parent")
    path = parent / path.name
    if path.exists():
        try:
            info = path.lstat()
        except OSError as exc:
            raise CleanupRefused("receipt path is unreadable") from exc
        if _is_reparse_info(info) or not stat.S_ISREG(info.st_mode):
            raise CleanupRefused("existing receipt path is not a regular file")
    for row in root_rows:
        raw_root = row.get("absolute_root_candidate")
        if isinstance(raw_root, str) and _path_is_within(
            path, Path(os.path.abspath(raw_root))
        ):
            raise CleanupRefused("receipt path must be outside every cleanup root")
    return path


def _write_receipt(path: Path, payload: Mapping[str, Any], *, replace_existing: bool) -> None:
    if path.exists() and not replace_existing:
        raise CleanupRefused("receipt path already exists")
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except OSError as exc:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
        raise CleanupRefused(f"could not persist cleanup receipt: {type(exc).__name__}") from exc


def run_bounded_cleanup(
    manifest_path: str | Path,
    *,
    allowed_parent: str | Path = DEFAULT_ALLOWED_PARENT,
    apply: bool = False,
    confirm_inventory_sha256: str | None = None,
    receipt_path: str | Path | None = None,
) -> dict[str, Any]:
    """Validate or apply an exact cleanup manifest; dry-run is the default."""

    raw_source = Path(manifest_path).expanduser()
    if not raw_source.is_absolute() or ".." in PureWindowsPath(str(raw_source)).parts:
        raise CleanupRefused("cleanup manifest path must be absolute and normalized")
    source = Path(os.path.abspath(raw_source))
    manifest_path_issues = _check_no_reparse_chain(source)
    if manifest_path_issues:
        raise CleanupRefused("cleanup manifest path contains a reparse point or missing ancestor")
    try:
        resolved_source = source.resolve(strict=True)
    except OSError as exc:
        raise CleanupRefused("cleanup manifest is unavailable") from exc
    if _normcase(resolved_source) != _normcase(source):
        raise CleanupRefused("cleanup manifest resolves through an alias or reparse point")
    source_info = source.lstat()
    if _is_reparse_info(source_info) or not stat.S_ISREG(source_info.st_mode):
        raise CleanupRefused("cleanup manifest must be a regular non-reparse file")
    manifest_bytes = source.read_bytes()
    inventory_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    if apply and (
        not isinstance(confirm_inventory_sha256, str)
        or confirm_inventory_sha256.strip().lower() != inventory_sha256
    ):
        raise CleanupRefused("apply requires the exact inventory SHA-256 confirmation")
    if apply and receipt_path is None:
        raise CleanupRefused("apply requires an external receipt path")

    try:
        payload = json.loads(manifest_bytes.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise CleanupRefused("cleanup inventory is not valid UTF-8 JSON") from exc
    if not isinstance(payload, Mapping):
        raise CleanupRefused("cleanup inventory root must be a JSON object")
    root_rows = _validate_inventory(payload)
    parent = _resolve_checked_directory(allowed_parent, label="allowed parent")

    roots: list[dict[str, Any]] = []
    for row in root_rows:
        try:
            roots.append(_scan_root(row, allowed_parent=parent))
        except (CleanupRefused, OSError, ValueError) as exc:
            roots.append(
                {
                    "absolute_root": str(row.get("absolute_root_candidate") or ""),
                    "resolved_root": "",
                    "result": "refused",
                    "issues": [str(exc)],
                    "unexpected_entries": [],
                    "reparse_checks": {"ancestor_chain_checked": False, "descendant_entries_scanned": 0, "reparse_paths": []},
                    "enumerated_file_count": 0,
                    "enumerated_bytes": 0,
                    "per_file_relative_path_bytes_sha256": [],
                    "planned_paths": [],
                    "deleted_paths": [],
                    "post_delete_verification": {"performed": False, "remaining_enumerated_files": None},
                    "_private_expected": {},
                }
            )

    all_ready = all(root["result"] == "ready" for root in roots)
    captured_at = datetime.now(timezone.utc).isoformat()
    receipt: dict[str, Any] = {
        "inventory_sha256": inventory_sha256,
        "captured_at_utc": captured_at,
        "dry_run": not apply,
        "absolute_root": str(parent),
        "resolved_root": str(parent),
        "reparse_checks": {
            "all_target_ancestors_checked": all(
                root["reparse_checks"].get("ancestor_chain_checked") is True for root in roots
            ),
            "descendant_entries_scanned": sum(
                int(root["reparse_checks"].get("descendant_entries_scanned") or 0)
                for root in roots
            ),
            "reparse_paths": [
                path for root in roots for path in root["reparse_checks"].get("reparse_paths", [])
            ],
        },
        "enumerated_file_count": sum(int(root["enumerated_file_count"]) for root in roots),
        "enumerated_bytes": sum(int(root["enumerated_bytes"]) for root in roots),
        "per_file_relative_path_bytes_sha256": [
            {
                "absolute_root": root["absolute_root"],
                **{key: row[key] for key in ("relative_path", "bytes", "sha256")},
            }
            for root in roots
            for row in root["per_file_relative_path_bytes_sha256"]
        ],
        "unexpected_entries": [
            {"absolute_root": root["absolute_root"], "relative_path": path}
            for root in roots
            for path in root["unexpected_entries"]
        ],
        "planned_paths": [path for root in roots for path in root["planned_paths"]],
        "deleted_paths": [],
        "post_delete_verification": {"performed": False, "remaining_enumerated_files": None},
        "roots": roots,
        "result": "dry_run_ready" if all_ready else "refused",
    }
    for root in receipt["roots"]:
        root.pop("_private_expected", None)

    receipt_target: Path | None = None
    if receipt_path is not None:
        receipt_target = _safe_receipt_path(receipt_path, root_rows)

    if not apply:
        if receipt_target is not None:
            _write_receipt(receipt_target, receipt, replace_existing=False)
        return receipt

    if not all_ready:
        receipt["result"] = "refused"
        if receipt_target is not None:
            _write_receipt(receipt_target, receipt, replace_existing=False)
        return receipt

    assert receipt_target is not None
    receipt["result"] = "apply_in_progress"
    _write_receipt(receipt_target, receipt, replace_existing=False)
    for root_plan, root_row in zip(roots, root_rows, strict=True):
        root = Path(root_plan["resolved_root"])
        # Re-scan this root immediately before the first unlink. No unlisted
        # entry, reparse point, size or hash change is tolerated.
        current = _scan_root(root_row, allowed_parent=parent)
        if current["result"] != "ready":
            receipt["result"] = "partial_refused_revalidation"
            root_plan["result"] = "refused_revalidation"
            root_plan["issues"] = current["issues"]
            break
        for file_row in root_plan["per_file_relative_path_bytes_sha256"]:
            target = Path(file_row["absolute_path"])
            try:
                chain_issues = _check_no_reparse_chain(target)
                if chain_issues:
                    raise CleanupRefused("reparse point appeared before unlink")
                observed_bytes, observed_hash, _info = _sha256_regular_file(target)
                if (
                    observed_bytes != file_row["bytes"]
                    or observed_hash != file_row["sha256"]
                ):
                    raise CleanupRefused("listed file changed before unlink")
                target.unlink()
            except (OSError, CleanupRefused) as exc:
                root_plan["issues"].append(f"unlink_refused:{target}:{type(exc).__name__}")
                receipt["result"] = "partial_refused_revalidation"
                root_plan["result"] = "partial"
                break
            if os.path.lexists(target):
                root_plan["issues"].append(f"listed_path_remains:{target}")
                receipt["result"] = "partial_post_delete_verification_failed"
                root_plan["result"] = "partial"
                break
            root_plan["deleted_paths"].append(str(target))
            receipt["deleted_paths"].append(str(target))
        if receipt["result"] != "apply_in_progress":
            break
        post_check = _verify_empty_root_after_unlinks(root, root_row)
        root_plan["post_delete_verification"] = post_check
        if post_check.get("passed") is not True:
            root_plan["issues"].extend(post_check.get("issues") or ())
            receipt["result"] = "partial_post_delete_verification_failed"
            root_plan["result"] = "partial"
            break
        root_plan["result"] = "applied"

    if receipt["result"] == "apply_in_progress":
        receipt["result"] = "applied"
    still_present_planned_paths = [
        path
        for root in roots
        for path in root["planned_paths"]
        if os.path.lexists(path)
    ]
    receipt["post_delete_verification"] = {
        "performed": True,
        "remaining_enumerated_files": len(still_present_planned_paths),
        "still_present_planned_paths": still_present_planned_paths,
        "roots_passed": sum(
            1
            for root in roots
            if root.get("post_delete_verification", {}).get("passed") is True
        ),
        "roots_checked": sum(
            1 for root in roots if root.get("post_delete_verification", {}).get("performed") is True
        ),
    }
    _write_receipt(receipt_target, receipt, replace_existing=True)
    return receipt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, help="exact cleanup inventory JSON")
    parser.add_argument("--apply", action="store_true", help="delete only files listed in the manifest")
    parser.add_argument(
        "--confirm-inventory-sha256",
        help="required with --apply; exact SHA-256 of the selected inventory",
    )
    parser.add_argument("--receipt", type=Path, help="optional receipt output outside cleanup roots")
    args = parser.parse_args(argv)
    manifest = args.manifest or DEFAULT_INVENTORY
    if args.apply and args.manifest is None:
        parser.error("--apply requires an explicit --manifest path")
    try:
        receipt = run_bounded_cleanup(
            manifest,
            apply=args.apply,
            confirm_inventory_sha256=args.confirm_inventory_sha256,
            receipt_path=args.receipt,
        )
    except (CleanupRefused, OSError, ValueError, TypeError) as exc:
        print(json.dumps({"result": "refused", "reason": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(receipt, ensure_ascii=False, indent=2))
    return 0 if receipt.get("result") in {"dry_run_ready", "applied"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
