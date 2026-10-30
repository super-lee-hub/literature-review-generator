from __future__ import annotations

import hashlib
import json
import os
import subprocess
import zipfile
from pathlib import Path

import pytest

from scripts import build_run_first_recovery_package as builder
from scripts.build_run_first_recovery_package import (
    PACKAGE_MANIFEST_NAME,
    PackageRefused,
    build_recovery_package,
)


def _git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr.decode("utf-8", "replace")
    return completed.stdout.decode("utf-8").strip()


def _write(root: Path, relative: str, content: str | bytes) -> None:
    path = root / Path(relative)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = content.encode("utf-8") if isinstance(content, str) else content
    path.write_bytes(payload)


def _make_repo(root: Path) -> str:
    root.mkdir(parents=True)
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "Run First Package Test")
    _git(root, "config", "user.email", "run-first-package@example.invalid")
    files: dict[str, str | bytes] = {
        ".gitignore": "config.ini\n.env*\noutput/\nacceptance_artifacts/\n",
        "AGENTS.md": "# Frozen source fixture\n",
        "config.ini.example": (
            "[DEFAULT]\n"
            "api_key: YOUR_API_KEY_HERE\n"
            "[Primary_Reader_API]\n"
            "api_base: http://127.0.0.1:1/v1\n"
        ),
        ".env.example": "API_KEY=YOUR_API_KEY_HERE\n",
        "requirements-py311-windows.lock": "lock-main==1.0 --hash=sha256:abc\n",
        "requirements-py311-windows-dev.lock": "lock-dev==1.0 --hash=sha256:def\n",
        "requirements-py311-windows-prod.lock": "lock-prod==1.0 --hash=sha256:ghi\n",
        "docs/audits/pr25/RUN_FIRST_USER_MANUAL.md": "# Run First user manual\n",
        "docs/en/RECOVERY_RUNBOOK.md": "# Recovery runbook\n",
        "scripts/build_run_first_recovery_package.py": "def main():\n    return None\n",
        "prompts/registry.json": '{"schema_version":"prompt_registry/v1","prompts":[]}\n',
        "launch_gui.py": "BUILD_STAMP = 'synthetic'\n",
        "reviewctl.py": "def main():\n    return None\n",
        "gui/app.py": "BUILD_STAMP = 'synthetic'\n",
        "runtime/control_plane.py": "class ReviewControlPlane:\n    pass\n",
        "docs/en/runtime/credential-provenance.md": "Credential flow documentation.\n",
        "services/credential_provenance.py": "def get_credential():\n    return None\n",
        "tests/test_credential_provenance.py": "def test_placeholder():\n    assert True\n",
    }
    for relative, content in files.items():
        _write(root, relative, content)
    _git(root, "add", "--all", "--force")
    _git(root, "commit", "-q", "--no-gpg-sign", "-m", "frozen source fixture")
    return _git(root, "rev-parse", "HEAD")


@pytest.mark.parametrize(
    "deep_destination_parent",
    [False, True],
    ids=["short_destination_parent", "deep_destination_parent"],
)
def test_clean_frozen_source_package_preserves_exact_tree_and_has_single_commit_snapshot(
    tmp_path: Path,
    deep_destination_parent: bool,
) -> None:
    repo = tmp_path / "repo"
    frozen_sha = _make_repo(repo)
    # Ignored local data is not in the frozen tree and must not enter the package.
    _write(repo, "config.ini", "[Primary_Reader_API]\napi_key = local-only-value\n")
    _write(repo, ".env", "API_KEY=local-only-value\n")
    _write(repo, ".env.local", "API_KEY=local-value-not-for-package\n")
    _write(repo, "output/untracked-runtime.json", "untracked artifact\n")
    destination_parent = tmp_path / "package-destination"
    if deep_destination_parent:
        destination_parent = destination_parent.joinpath(
            *(
                f"destination-segment-{index:02d}-xxxxxxxxxxxx"
                for index in range(2)
            )
        )
    destination_parent.mkdir(parents=True)
    destination = destination_parent / "run-first-source.zip"

    report = build_recovery_package(
        source_root=repo,
        frozen_sha=frozen_sha,
        destination=destination,
    )
    assert report["source_git_commit"] == frozen_sha
    assert report["package_path"] == str(destination)
    assert report["git_bundle_included"] is True
    assert destination.is_file()
    assert report["snapshot_bundle_bytes"] > 0
    assert len(report["snapshot_bundle_sha256"]) == 64

    with zipfile.ZipFile(destination, "r") as archive:
        assert archive.testzip() is None
        names = set(archive.namelist())
        assert PACKAGE_MANIFEST_NAME in names
        assert {
            "AGENTS.md",
            "config.ini.example",
            "requirements-py311-windows.lock",
            "requirements-py311-windows-dev.lock",
            "requirements-py311-windows-prod.lock",
            "docs/audits/pr25/RUN_FIRST_USER_MANUAL.md",
            "docs/en/RECOVERY_RUNBOOK.md",
            "scripts/build_run_first_recovery_package.py",
            "prompts/registry.json",
            "launch_gui.py",
            "reviewctl.py",
            "gui/app.py",
            "runtime/control_plane.py",
            "docs/en/runtime/credential-provenance.md",
            "services/credential_provenance.py",
            "tests/test_credential_provenance.py",
            ".env.example",
            builder.SNAPSHOT_BUNDLE_NAME,
        }.issubset(names)
        assert not {"config.ini", ".env", ".env.local"}.intersection(names)
        config_example = archive.read("config.ini.example")
        assert config_example == (repo / "config.ini.example").read_bytes()
        assert archive.read(".env.example") == (repo / ".env.example").read_bytes()
        manifest = json.loads(archive.read(PACKAGE_MANIFEST_NAME).decode("utf-8"))
        assert manifest["package_schema"] == "run-first-recovery-source-package/v2"
        assert manifest["source_revision"]["git_commit"] == frozen_sha
        assert manifest["source_revision"]["git_tree"] == _git(repo, "rev-parse", f"{frozen_sha}^{{tree}}")
        assert manifest["git_bundle_included"] is True
        assert manifest["snapshot"]["shallow_boundary"] == frozen_sha
        assert manifest["snapshot"]["shallow_commit"] == frozen_sha
        assert manifest["snapshot"]["bundle_ref"] == "HEAD"
        entries = {row["path"]: row for row in manifest["files"]}
        assert set(entries) == names - {PACKAGE_MANIFEST_NAME, builder.SNAPSHOT_BUNDLE_NAME}
        for name, row in entries.items():
            content = archive.read(name)
            assert len(content) == row["bytes"]
            assert hashlib.sha256(content).hexdigest() == row["sha256"]
            assert row["sha256"] == row["source_git_blob_sha256"]
        config_entry = entries["config.ini.example"]
        assert config_entry["git_object_id"]
        assert config_entry["git_mode"] == "100644"
        assert not config_entry.get("redacted_copy")

    with destination.open("rb") as handle:
        assert hashlib.file_digest(handle, "sha256").hexdigest() == report["package_sha256"]


def test_changed_snapshot_copy_is_refused_before_package_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    frozen_sha = _make_repo(repo)
    destination = tmp_path / "must-not-exist.zip"
    original_copy = builder.shutil.copyfile

    def corrupt_bundle_copy(source, target, **kwargs):
        result = original_copy(source, target, **kwargs)
        if Path(target).name == builder.SNAPSHOT_BUNDLE_NAME:
            with Path(target).open("ab") as handle:
                handle.write(b"changed-after-copy")
        return result

    monkeypatch.setattr(builder.shutil, "copyfile", corrupt_bundle_copy)
    with pytest.raises(PackageRefused, match="copied Git snapshot differs"):
        build_recovery_package(source_root=repo, frozen_sha=frozen_sha, destination=destination)
    assert not destination.exists()


def test_excluded_tracked_data_refuses_exact_sha_package(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    frozen_sha = _make_repo(repo)
    _write(repo, "acceptance_artifacts/private-evidence.json", '{"record":"preserve outside package"}\n')
    _git(repo, "add", "-f", "acceptance_artifacts/private-evidence.json")
    _git(repo, "commit", "-q", "--no-gpg-sign", "-m", "tracked evidence fixture")
    frozen_sha = _git(repo, "rev-parse", "HEAD")
    destination = tmp_path / "must-not-exist.zip"

    with pytest.raises(PackageRefused, match="excluded-data policy"):
        build_recovery_package(source_root=repo, frozen_sha=frozen_sha, destination=destination)
    assert not destination.exists()


def test_non_placeholder_config_template_is_refused_without_package(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _make_repo(repo)
    fake_token = "sk-" + "test-" + "x" * 24
    _write(
        repo,
        "config.ini.example",
        f"[Primary_Reader_API]\napi_key = {fake_token}\n",
    )
    _git(repo, "add", "config.ini.example")
    _git(repo, "commit", "-q", "--no-gpg-sign", "-m", "unsafe example fixture")
    frozen_sha = _git(repo, "rev-parse", "HEAD")
    destination = tmp_path / "must-not-exist.zip"

    with pytest.raises(PackageRefused, match="non-placeholder credential value"):
        build_recovery_package(source_root=repo, frozen_sha=frozen_sha, destination=destination)
    assert not destination.exists()


@pytest.mark.parametrize(
    "config_text",
    [
        "[Primary_Reader_API]\napi_key: local-only-value\n",
        "[DEFAULT]\napi_key: local-only-value\n[Primary_Reader_API]\napi_base = http://127.0.0.1\n",
        (
            "[DEFAULT]\napi_key = YOUR_API_KEY_HERE\n"
            "[Primary_Reader_API]\napi_key: local-only-value\n"
        ),
        (
            "[Primary_Reader_API]\napi_key = YOUR_API_KEY_HERE\n"
            "    local-only-continuation\n"
        ),
    ],
    ids=("colon-delimiter", "default-option", "section-override", "multiline-continuation"),
)
def test_configparser_semantics_reject_credential_values_before_snapshot_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    config_text: str,
) -> None:
    repo = tmp_path / "repo"
    _make_repo(repo)
    _write(repo, "config.ini.example", config_text)
    _git(repo, "add", "config.ini.example")
    _git(repo, "commit", "-q", "--no-gpg-sign", "-m", "unsafe ConfigParser fixture")
    frozen_sha = _git(repo, "rev-parse", "HEAD")
    destination = tmp_path / "must-not-exist.zip"

    def fail_if_bundle_starts(*_args: object, **_kwargs: object) -> Path:
        pytest.fail("unsafe template reached snapshot creation")

    monkeypatch.setattr(builder, "_create_single_commit_bundle", fail_if_bundle_starts)
    with pytest.raises(PackageRefused, match="non-placeholder credential value"):
        build_recovery_package(source_root=repo, frozen_sha=frozen_sha, destination=destination)
    assert not destination.exists()


def test_dirty_checkout_and_frozen_sha_mismatch_are_rejected(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    frozen_sha = _make_repo(repo)
    destination = tmp_path / "should-not-exist.zip"
    (repo / "launch_gui.py").write_text("dirty working tree\n", encoding="utf-8")
    with pytest.raises(PackageRefused, match="dirty"):
        build_recovery_package(
            source_root=repo,
            frozen_sha=frozen_sha,
            destination=destination,
        )
    assert not destination.exists()

    (repo / "launch_gui.py").write_text("BUILD_STAMP = 'synthetic'\n", encoding="utf-8")
    with pytest.raises(PackageRefused):
        build_recovery_package(
            source_root=repo,
            frozen_sha="0" * 40,
            destination=destination,
        )
    assert not destination.exists()


def test_reparse_source_root_and_destination_inside_source_are_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    frozen_sha = _make_repo(repo)
    destination_inside = repo / "package.zip"
    with pytest.raises(PackageRefused, match="outside the source checkout"):
        build_recovery_package(
            source_root=repo,
            frozen_sha=frozen_sha,
            destination=destination_inside,
        )
    assert not destination_inside.exists()

    alias = tmp_path / "repo-alias"
    try:
        os.symlink(repo, alias, target_is_directory=True)
        source_root = alias
    except (NotImplementedError, OSError):
        root_info = repo.lstat()
        real_reparse_check = builder._is_reparse
        monkeypatch.setattr(
            builder,
            "_is_reparse",
            lambda info: real_reparse_check(info)
            or (info.st_dev == root_info.st_dev and info.st_ino == root_info.st_ino),
        )
        source_root = repo
    try:
        with pytest.raises(PackageRefused, match="junction or symlink"):
            build_recovery_package(
                source_root=source_root,
                frozen_sha=frozen_sha,
                destination=tmp_path / "reparse-package.zip",
            )
        assert not (tmp_path / "reparse-package.zip").exists()
    finally:
        if os.path.lexists(alias):
            alias.unlink()
