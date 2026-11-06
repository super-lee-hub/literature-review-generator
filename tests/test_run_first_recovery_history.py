from __future__ import annotations

import json
import subprocess
import zipfile
from pathlib import Path

import pytest

from scripts import build_run_first_recovery_package as builder
from scripts.build_run_first_recovery_package import (
    PackageRefused,
    build_recovery_package,
    restore_recovery_package,
)
from tests.test_build_run_first_recovery_package import _git, _make_repo, _write


PRIVATE_PATH = "acceptance_artifacts/f1_20260915/synthetic-private-fulltext.pdf"
PRIVATE_BYTES = b"SYNTHETIC-PRIVATE-FULLTEXT-FIXTURE-ONLY"


def test_staged_private_fixture_deletion_is_rejected_before_bundle_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "source-repo"
    _make_repo(repo)
    _write(repo, PRIVATE_PATH, PRIVATE_BYTES)
    _git(repo, "add", "-f", "--", PRIVATE_PATH)
    _git(repo, "commit", "-q", "--no-gpg-sign", "-m", "synthetic private fixture")
    frozen_sha = _git(repo, "rev-parse", "--verify", "HEAD")

    assert _git(repo, "ls-tree", "-r", "--name-only", frozen_sha, "--", PRIVATE_PATH) == PRIVATE_PATH
    private_oid = _git(repo, "rev-parse", f"{frozen_sha}:{PRIVATE_PATH}")
    _git(repo, "rm", "--cached", "--", PRIVATE_PATH)

    assert _git(repo, "ls-files", "--", PRIVATE_PATH) == ""
    assert (repo / Path(PRIVATE_PATH)).read_bytes() == PRIVATE_BYTES
    assert _git(repo, "status", "--porcelain=v1", "--untracked-files=all")

    bundle_calls: list[bool] = []

    def fail_if_bundle_starts(*_args: object, **_kwargs: object) -> Path:
        bundle_calls.append(True)
        raise AssertionError("dirty preflight must reject before bundle creation")

    monkeypatch.setattr(builder, "_create_single_commit_bundle", fail_if_bundle_starts)
    destination = tmp_path / "staged-deletion-must-not-package.zip"

    with pytest.raises(PackageRefused, match="dirty"):
        build_recovery_package(
            source_root=repo,
            frozen_sha=frozen_sha,
            destination=destination,
        )

    assert not bundle_calls
    assert not destination.exists()
    assert _git(repo, "rev-parse", f"{frozen_sha}:{PRIVATE_PATH}") == private_oid


def test_depth_one_package_restore_omits_private_blob_from_ancestor_history(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "source-repo"
    _make_repo(repo)
    _write(repo, PRIVATE_PATH, PRIVATE_BYTES)
    _git(repo, "add", "-f", "--", PRIVATE_PATH)
    _git(repo, "commit", "-q", "--no-gpg-sign", "-m", "synthetic private ancestor")
    private_oid = _git(repo, "rev-parse", f"HEAD:{PRIVATE_PATH}")
    assert private_oid

    _git(repo, "rm", "--", PRIVATE_PATH)
    builder_relative = "scripts/build_run_first_recovery_package.py"
    builder_bytes = Path(builder.__file__).read_bytes()
    _write(repo, builder_relative, builder_bytes)
    _git(repo, "add", "--", builder_relative)
    _git(repo, "commit", "-q", "--no-gpg-sign", "-m", "clean source without private fixture")
    frozen_sha = _git(repo, "rev-parse", "--verify", "HEAD")

    assert _git(repo, "status", "--porcelain=v1", "--untracked-files=all") == ""
    assert _git(repo, "ls-tree", "-r", "--name-only", frozen_sha, "--", PRIVATE_PATH) == ""
    assert _git(repo, "ls-tree", "-r", "--name-only", frozen_sha, "--", builder_relative) == builder_relative
    _git(repo, "cat-file", "-e", private_oid)

    package_path = tmp_path / "run-first-source.zip"
    build_report = build_recovery_package(
        source_root=repo,
        frozen_sha=frozen_sha,
        destination=package_path,
    )

    with zipfile.ZipFile(package_path, "r") as package:
        manifest = json.loads(package.read(builder.PACKAGE_MANIFEST_NAME).decode("utf-8"))
        file_paths = {row["path"] for row in manifest["files"]}
        expected_members = file_paths | {
            builder.SNAPSHOT_BUNDLE_NAME,
            builder.PACKAGE_MANIFEST_NAME,
        }
        assert set(package.namelist()) == expected_members
        assert PRIVATE_PATH not in file_paths
        assert manifest["source_revision"]["git_commit"] == frozen_sha
        assert manifest["snapshot"]["shallow_commit"] == frozen_sha
        assert manifest["snapshot"]["shallow_boundary"] == frozen_sha

    restored = tmp_path / "restored-source"
    restore_report = restore_recovery_package(
        package_path=package_path,
        expected_package_sha256=str(build_report["package_sha256"]),
        expected_commit=frozen_sha,
        destination=restored,
    )

    assert restore_report["result"] == "restored_verified"
    assert restore_report["source_git_commit"] == frozen_sha
    assert restore_report["git_history_commits"] == 1
    assert restore_report["working_tree_clean"] is True
    assert restore_report["remotes"] == []
    assert _git(restored, "rev-parse", "HEAD") == frozen_sha
    assert _git(restored, "rev-list", "--count", "HEAD") == "1"
    assert _git(restored, "status", "--porcelain=v1", "--untracked-files=all") == ""
    assert not (restored / Path(PRIVATE_PATH)).exists()

    missing_blob = subprocess.run(
        ["git", "-C", str(restored), "cat-file", "-e", private_oid],
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert missing_blob.returncode != 0
