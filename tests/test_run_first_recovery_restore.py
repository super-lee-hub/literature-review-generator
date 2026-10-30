from __future__ import annotations

import hashlib
import json
import subprocess
import zipfile
from pathlib import Path

import pytest

from scripts.build_run_first_recovery_package import (
    PACKAGE_MANIFEST_NAME,
    SNAPSHOT_BUNDLE_NAME,
    PackageRefused,
    build_recovery_package,
    restore_recovery_package,
)


def _git(root: Path, *args: str, check: bool = True) -> tuple[int, str, str]:
    completed = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        check=False,
        timeout=30,
    )
    stdout = completed.stdout.decode("utf-8", "replace").strip()
    stderr = completed.stderr.decode("utf-8", "replace").strip()
    if check:
        assert completed.returncode == 0, stderr
    return completed.returncode, stdout, stderr


def _write(root: Path, relative: str, payload: str) -> None:
    path = root / Path(relative)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload, encoding="utf-8", newline="\n")


def _make_safe_repo(root: Path) -> str:
    root.mkdir(parents=True)
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "Run First Restore Test")
    _git(root, "config", "user.email", "restore-test@example.invalid")
    files = {
        ".gitignore": ".env*\noutput/\nacceptance_artifacts/\n",
        "AGENTS.md": "# Verified source fixture\n",
        "config.ini.example": (
            "[DEFAULT]\n"
            "api_key: YOUR_API_KEY_HERE\n"
            "[Primary_Reader_API]\n"
            "api_base: http://127.0.0.1:1/v1\n"
        ),
        ".env.example": "API_KEY=YOUR_API_KEY_HERE\n",
        "requirements-py311-windows.lock": "fixture-main==1.0\n",
        "requirements-py311-windows-dev.lock": "fixture-dev==1.0\n",
        "requirements-py311-windows-prod.lock": "fixture-prod==1.0\n",
        "docs/audits/pr25/RUN_FIRST_USER_MANUAL.md": "# Restore test manual\n",
        "docs/en/RECOVERY_RUNBOOK.md": "# Restore test runbook\n",
        "scripts/build_run_first_recovery_package.py": "def main():\n    return None\n",
        "prompts/registry.json": '{"schema_version":"prompt_registry/v1","prompts":[]}\n',
        "launch_gui.py": "BUILD_STAMP = 'restore-test'\n",
        "reviewctl.py": "def main():\n    return None\n",
        "gui/app.py": "BUILD_STAMP = 'restore-test'\n",
        "runtime/control_plane.py": "class ReviewControlPlane:\n    pass\n",
    }
    for relative, payload in files.items():
        _write(root, relative, payload)
    _git(root, "add", "--all", "--force")
    _git(root, "commit", "-q", "--no-gpg-sign", "-m", "restore test base")
    _write(root, "reviewctl.py", "def main():\n    return 'frozen target'\n")
    _git(root, "add", "reviewctl.py")
    _git(root, "commit", "-q", "--no-gpg-sign", "-m", "restore test target")
    return _git(root, "rev-parse", "HEAD")[1]


def _build_package(tmp_path: Path) -> tuple[Path, Path, str, str]:
    repo = tmp_path / "source-repo"
    frozen_sha = _make_safe_repo(repo)
    package_path = tmp_path / "run-first-source.zip"
    report = build_recovery_package(
        source_root=repo,
        frozen_sha=frozen_sha,
        destination=package_path,
    )
    return repo, package_path, frozen_sha, str(report["package_sha256"])


def _rewrite_zip(source: Path, destination: Path, *, mutate: str) -> None:
    with zipfile.ZipFile(source, "r") as original, zipfile.ZipFile(
        destination,
        "w",
        compression=zipfile.ZIP_DEFLATED,
    ) as output:
        for name in original.namelist():
            content = original.read(name)
            if mutate == "bundle" and name == SNAPSHOT_BUNDLE_NAME:
                content = content[:-1] + bytes([content[-1] ^ 1])
            elif mutate == "boundary" and name == PACKAGE_MANIFEST_NAME:
                payload = json.loads(content.decode("utf-8"))
                payload["snapshot"]["shallow_boundary"] = "0" * 40
                content = (json.dumps(payload, indent=2) + "\n").encode("utf-8")
            output.writestr(name, content)


def _sha256(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def test_restore_recreates_exact_clean_one_commit_tree_without_remotes_or_hooks(
    tmp_path: Path,
) -> None:
    _repo, package_path, frozen_sha, package_sha = _build_package(tmp_path)
    restored = tmp_path / "restored-source"

    report = restore_recovery_package(
        package_path=package_path,
        expected_package_sha256=package_sha,
        expected_commit=frozen_sha,
        destination=restored,
    )

    assert report["result"] == "restored_verified"
    assert report["source_git_commit"] == frozen_sha
    assert report["git_history_commits"] == 1
    assert report["working_tree_clean"] is True
    assert report["remotes"] == []
    assert _git(restored, "rev-parse", "HEAD")[1] == frozen_sha
    assert _git(restored, "rev-parse", "HEAD^{tree}")[1] == _git(
        _repo, "rev-parse", f"{frozen_sha}^{{tree}}"
    )[1]
    assert _git(restored, "rev-list", "--count", "HEAD")[1] == "1"
    assert _git(restored, "status", "--porcelain=v1", "--untracked-files=all")[1] == ""
    assert _git(restored, "remote", "-v")[1] == ""
    assert _git(restored, "fsck", "--connectivity-only")[0] == 0
    assert (restored / ".git" / "shallow").read_text(encoding="ascii").strip() == frozen_sha
    hooks = restored / ".git" / "hooks"
    assert not hooks.exists() or not list(hooks.iterdir())
    assert not (restored / "config.ini").exists()

    expected_origins = {
        "launch_gui": restored / "launch_gui.py",
        "reviewctl": restored / "reviewctl.py",
        "gui.app": restored / "gui" / "app.py",
        "runtime.control_plane": restored / "runtime" / "control_plane.py",
    }
    assert set(report["module_origins"]) == set(expected_origins)
    for module, path in expected_origins.items():
        assert Path(report["module_origins"][module]).resolve() == path.resolve()
        assert path.is_file()


def test_restore_rejects_archive_hash_mismatch_before_creating_destination(
    tmp_path: Path,
) -> None:
    _repo, package_path, frozen_sha, package_sha = _build_package(tmp_path)
    tampered = tmp_path / "tampered-outer.zip"
    tampered.write_bytes(package_path.read_bytes() + b"unbound bytes")
    destination = tmp_path / "must-not-exist"

    with pytest.raises(PackageRefused, match="archive SHA-256"):
        restore_recovery_package(
            package_path=tampered,
            expected_package_sha256=package_sha,
            expected_commit=frozen_sha,
            destination=destination,
        )
    assert not destination.exists()


def test_restore_rejects_bundle_tampering_even_with_new_outer_hash(
    tmp_path: Path,
) -> None:
    _repo, package_path, frozen_sha, _package_sha = _build_package(tmp_path)
    tampered = tmp_path / "tampered-bundle.zip"
    _rewrite_zip(package_path, tampered, mutate="bundle")
    destination = tmp_path / "must-not-exist"

    with pytest.raises(PackageRefused, match="snapshot failed SHA-256"):
        restore_recovery_package(
            package_path=tampered,
            expected_package_sha256=_sha256(tampered),
            expected_commit=frozen_sha,
            destination=destination,
        )
    assert not destination.exists()


def test_restore_rejects_invalid_shallow_boundary_even_with_new_outer_hash(
    tmp_path: Path,
) -> None:
    _repo, package_path, frozen_sha, _package_sha = _build_package(tmp_path)
    tampered = tmp_path / "tampered-boundary.zip"
    _rewrite_zip(package_path, tampered, mutate="boundary")
    destination = tmp_path / "must-not-exist"

    with pytest.raises(PackageRefused, match="shallow snapshot binding"):
        restore_recovery_package(
            package_path=tampered,
            expected_package_sha256=_sha256(tampered),
            expected_commit=frozen_sha,
            destination=destination,
        )
    assert not destination.exists()
