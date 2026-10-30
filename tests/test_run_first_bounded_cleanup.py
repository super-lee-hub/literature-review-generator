from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path

import pytest

from scripts import run_first_bounded_cleanup as cleanup
from scripts.run_first_bounded_cleanup import CleanupRefused, run_bounded_cleanup


def _write_inventory(
    destination: Path,
    root: Path,
    files: list[tuple[str, bytes, str | None]],
) -> str:
    entries = []
    for relative_path, content, hash_override in files:
        entries.append(
            {
                "relative_path": relative_path,
                "bytes": len(content),
                "sha256": hash_override or hashlib.sha256(content).hexdigest(),
            }
        )
    root_bytes = sum(row["bytes"] for row in entries)
    payload = {
        "schema_version": "run-first-recovery-cleanup-inventory/v1",
        "proposed_minimal_cleanup_manifest": {
            "status": "proposal_only_not_executed",
            "cleanup_code_or_deletion_performed": False,
            "candidate_total_bytes": root_bytes,
            "cleanup_roots": [
                {
                    "absolute_root_candidate": str(root),
                    "exists": True,
                    "bytes": root_bytes,
                    "file_count": len(entries),
                    "files": entries,
                    "registry_or_evidence_present": False,
                    "reparse_points_skipped": 0,
                }
            ],
        },
    }
    destination.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return hashlib.sha256(destination.read_bytes()).hexdigest()


def test_dry_run_is_default_and_apply_deletes_only_explicit_hashed_file(
    tmp_path: Path,
) -> None:
    allowed_parent = tmp_path / "allowed"
    allowed_parent.mkdir()
    root = allowed_parent / "run-first-fixture-unit-controlled"
    root.mkdir()
    known = root / "rebuildable.pdf"
    known.write_bytes(b"known rebuildable fixture bytes")
    sibling = allowed_parent / "preserve.txt"
    sibling.write_text("outside the enumerated root", encoding="utf-8")
    manifest_path = allowed_parent / "inventory.json"
    inventory_sha = _write_inventory(
        manifest_path,
        root,
        [("rebuildable.pdf", known.read_bytes(), None)],
    )

    dry_run = run_bounded_cleanup(
        manifest_path,
        allowed_parent=allowed_parent,
    )
    assert dry_run["dry_run"] is True
    assert dry_run["result"] == "dry_run_ready"
    assert dry_run["planned_paths"] == [str(known)]
    assert dry_run["deleted_paths"] == []
    assert known.is_file()
    assert root.is_dir()

    receipt_path = allowed_parent / "cleanup-receipt.json"
    applied = run_bounded_cleanup(
        manifest_path,
        allowed_parent=allowed_parent,
        apply=True,
        confirm_inventory_sha256=inventory_sha,
        receipt_path=receipt_path,
    )
    assert applied["result"] == "applied"
    assert applied["dry_run"] is False
    assert applied["deleted_paths"] == [str(known)]
    assert applied["roots"][0]["result"] == "applied"
    assert applied["roots"][0]["post_delete_verification"]["passed"] is True
    assert applied["post_delete_verification"]["remaining_enumerated_files"] == 0
    assert not known.exists()
    assert root.is_dir()  # The tool never removes directories.
    assert sibling.read_text(encoding="utf-8") == "outside the enumerated root"
    assert receipt_path.is_file()


def test_hash_mismatch_refuses_apply_without_deleting_file(tmp_path: Path) -> None:
    allowed_parent = tmp_path / "allowed"
    allowed_parent.mkdir()
    root = allowed_parent / "hash-mismatch"
    root.mkdir()
    target = root / "item.bin"
    target.write_bytes(b"current bytes")
    manifest_path = allowed_parent / "inventory.json"
    inventory_sha = _write_inventory(
        manifest_path,
        root,
        [("item.bin", target.read_bytes(), "0" * 64)],
    )

    result = run_bounded_cleanup(
        manifest_path,
        allowed_parent=allowed_parent,
        apply=True,
        confirm_inventory_sha256=inventory_sha,
        receipt_path=allowed_parent / "receipt.json",
    )
    assert result["result"] == "refused"
    assert result["deleted_paths"] == []
    assert target.read_bytes() == b"current bytes"


def test_apply_requires_exact_inventory_sha_confirmation(tmp_path: Path) -> None:
    allowed_parent = tmp_path / "allowed"
    allowed_parent.mkdir()
    root = allowed_parent / "confirmation-required"
    root.mkdir()
    target = root / "item.bin"
    target.write_bytes(b"keep until exact manifest is confirmed")
    manifest_path = allowed_parent / "inventory.json"
    _write_inventory(
        manifest_path,
        root,
        [("item.bin", target.read_bytes(), None)],
    )

    with pytest.raises(CleanupRefused, match="exact inventory SHA-256"):
        run_bounded_cleanup(
            manifest_path,
            allowed_parent=allowed_parent,
            apply=True,
            confirm_inventory_sha256="f" * 64,
            receipt_path=allowed_parent / "receipt.json",
        )
    assert target.read_bytes() == b"keep until exact manifest is confirmed"
    assert not (allowed_parent / "receipt.json").exists()


def test_relative_path_escape_refuses_apply_and_preserves_outside_file(
    tmp_path: Path,
) -> None:
    allowed_parent = tmp_path / "allowed"
    allowed_parent.mkdir()
    root = allowed_parent / "safe-root"
    root.mkdir()
    outside = allowed_parent / "outside.txt"
    outside.write_text("preserve", encoding="utf-8")
    manifest_path = allowed_parent / "inventory.json"
    inventory_sha = _write_inventory(
        manifest_path,
        root,
        [(r"..\outside.txt", outside.read_bytes(), None)],
    )

    result = run_bounded_cleanup(
        manifest_path,
        allowed_parent=allowed_parent,
        apply=True,
        confirm_inventory_sha256=inventory_sha,
        receipt_path=allowed_parent / "receipt.json",
    )
    assert result["result"] == "refused"
    assert result["deleted_paths"] == []
    assert outside.read_text(encoding="utf-8") == "preserve"


def test_root_outside_allowed_parent_is_refused(tmp_path: Path) -> None:
    allowed_parent = tmp_path / "allowed"
    allowed_parent.mkdir()
    outside_root = tmp_path / "outside-root"
    outside_root.mkdir()
    target = outside_root / "item.bin"
    target.write_bytes(b"do not delete")
    manifest_path = tmp_path / "inventory.json"
    inventory_sha = _write_inventory(
        manifest_path,
        outside_root,
        [("item.bin", target.read_bytes(), None)],
    )

    result = run_bounded_cleanup(
        manifest_path,
        allowed_parent=allowed_parent,
        apply=True,
        confirm_inventory_sha256=inventory_sha,
        receipt_path=tmp_path / "receipt.json",
    )
    assert result["result"] == "refused"
    assert result["deleted_paths"] == []
    assert target.read_bytes() == b"do not delete"


def test_reparse_descendant_is_refused_without_following_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    allowed_parent = tmp_path / "allowed"
    allowed_parent.mkdir()
    root = allowed_parent / "reparse-root"
    root.mkdir()
    outside = allowed_parent / "outside-directory"
    outside.mkdir()
    target = outside / "target.bin"
    target.write_bytes(b"outside target")
    link = root / "linked.bin"
    actual_symlink = True
    try:
        os.symlink(target, link)
    except (NotImplementedError, OSError):
        # Some Windows hosts deny symlink creation. Simulate the OS reparse
        # attribute for one isolated regular file to exercise the same refusal
        # branch without starting a shell or following an external target.
        actual_symlink = False
        marker_bytes = b"x" * 997
        link.write_bytes(marker_bytes)
        real_reparse_check = cleanup._is_reparse_info
        monkeypatch.setattr(
            cleanup,
            "_is_reparse_info",
            lambda info: real_reparse_check(info)
            or (stat.S_ISREG(info.st_mode) and info.st_size == len(marker_bytes)),
        )

    manifest_path = allowed_parent / "inventory.json"
    inventory_sha = _write_inventory(
        manifest_path,
        root,
        [("linked.bin", link.read_bytes(), None)],
    )
    try:
        result = run_bounded_cleanup(
            manifest_path,
            allowed_parent=allowed_parent,
            apply=True,
            confirm_inventory_sha256=inventory_sha,
            receipt_path=allowed_parent / "receipt.json",
        )
        assert result["result"] == "refused"
        assert result["deleted_paths"] == []
        assert target.read_bytes() == b"outside target"
        if actual_symlink:
            assert link.is_symlink()
        else:
            assert link.read_bytes() == marker_bytes
    finally:
        if actual_symlink and os.path.lexists(link):
            link.unlink()


def test_unlisted_file_blocks_the_entire_root_before_apply(tmp_path: Path) -> None:
    allowed_parent = tmp_path / "allowed"
    allowed_parent.mkdir()
    root = allowed_parent / "unexpected-entry"
    root.mkdir()
    listed = root / "listed.bin"
    listed.write_bytes(b"listed")
    extra = root / "unlisted.bin"
    extra.write_bytes(b"must preserve")
    manifest_path = allowed_parent / "inventory.json"
    inventory_sha = _write_inventory(
        manifest_path,
        root,
        [("listed.bin", listed.read_bytes(), None)],
    )

    result = run_bounded_cleanup(
        manifest_path,
        allowed_parent=allowed_parent,
        apply=True,
        confirm_inventory_sha256=inventory_sha,
        receipt_path=allowed_parent / "receipt.json",
    )
    assert result["result"] == "refused"
    assert result["deleted_paths"] == []
    assert listed.read_bytes() == b"listed"
    assert extra.read_bytes() == b"must preserve"
