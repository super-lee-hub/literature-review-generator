from __future__ import annotations

from pathlib import Path

from runtime.architecture_gates import ArchitectureGateScope, collect_scannable_paths, scan_paths_for_forbidden_patterns


def test_architecture_gate_scope_scans_only_runtime_and_skill_surfaces(tmp_path: Path) -> None:
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    (runtime_dir / "job_spec.py").write_text("print('runtime')", encoding="utf-8")
    pycache_dir = runtime_dir / "__pycache__"
    pycache_dir.mkdir()
    (pycache_dir / "job_spec.cpython-311.pyc").write_bytes(b"binary")

    skill_dir = tmp_path / ".codex" / "skills" / "auto-generate-orchestrator"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# skill", encoding="utf-8")

    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_runtime.py").write_text("print('tests')", encoding="utf-8")

    scope = ArchitectureGateScope(extra_canonical_roots=("services/canonical_runtime",))
    scanned = collect_scannable_paths(tmp_path, scope=scope)
    scanned_relative = {path.relative_to(tmp_path).as_posix() for path in scanned}

    assert "runtime/job_spec.py" in scanned_relative
    assert "runtime/__pycache__/job_spec.cpython-311.pyc" not in scanned_relative
    assert ".codex/skills/auto-generate-orchestrator/SKILL.md" in scanned_relative
    assert "tests/test_runtime.py" not in scanned_relative
    assert scope.is_scannable("services/canonical_runtime/bridge.py") is True
    assert scope.is_scannable("tests/test_runtime.py") is False


def test_architecture_gate_scan_reports_forbidden_patterns(tmp_path: Path) -> None:
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    bad_file = runtime_dir / "orchestrator.py"
    bad_file.write_text("dispatch_command(args)\n", encoding="utf-8")

    findings = scan_paths_for_forbidden_patterns([bad_file])

    assert findings == [(str(bad_file), "legacy_cli_dispatch")]


def test_architecture_gate_rejects_canonical_write_then_separate_registry_registration(
    tmp_path: Path,
) -> None:
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    bad_file = runtime_dir / "writer.py"
    bad_file.write_text(
        "from services.job_workspace import atomic_write_json\n"
        "\n"
        "def publish(registry, workspace):\n"
        "    path = workspace.artifact_path('review_draft.json')\n"
        "    atomic_write_json(str(path), {'artifact_type': 'review_draft'})\n"
        "    return registry.register_file(path=path, artifact_id='draft', artifact_type='review_draft', artifact_version='v3', producer='test')\n",
        encoding="utf-8",
    )

    findings = scan_paths_for_forbidden_patterns([bad_file])

    assert findings == [(str(bad_file), "canonical_publication_boundary_bypass")]


def test_architecture_gate_recognizes_os_replace_destination_as_canonical(
    tmp_path: Path,
) -> None:
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    bad_file = runtime_dir / "writer.py"
    bad_file.write_text(
        "import os\n"
        "\n"
        "def publish(registry, workspace, staging):\n"
        "    canonical_path = workspace.artifact_path('review_draft.json')\n"
        "    os.replace(str(staging), str(canonical_path))\n"
        "    return registry.register_file(path=canonical_path, artifact_id='draft', artifact_type='review_draft', artifact_version='v3', producer='test')\n",
        encoding="utf-8",
    )

    findings = scan_paths_for_forbidden_patterns([bad_file])

    assert findings == [(str(bad_file), "canonical_publication_boundary_bypass")]
