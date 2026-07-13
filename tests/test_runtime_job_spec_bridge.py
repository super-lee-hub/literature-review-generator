from __future__ import annotations

import json
from pathlib import Path

import pytest

from runtime.job_spec import RuntimeJobSpec, RuntimeSourceSpec


def test_runtime_job_spec_compiles_direct_source_into_canonical_job_request() -> None:
    spec = RuntimeJobSpec(
        project_name="demo",
        source=RuntimeSourceSpec(mode="direct", pdf_folder="D:/papers"),
        action="run_all",
        summary_sources=("a.json", "b.json"),
        reuse_stage1=True,
        reuse_summary_files=("reuse.json",),
    )

    request = spec.to_job_request()

    assert request.project_name == "demo"
    assert request.source_mode == "direct"
    assert request.pdf_folder == "D:/papers"
    assert request.run_all is True
    assert request.summary_sources == ("a.json", "b.json")
    assert request.reuse_stage1 is True
    assert request.reuse_summary_files == ("reuse.json",)


def test_runtime_job_spec_compiles_zotero_source_into_canonical_job_request() -> None:
    spec = RuntimeJobSpec(
        project_name="demo-zotero",
        source=RuntimeSourceSpec(
            mode="zotero",
            zotero_report="D:/report.txt",
            library_path="D:/library",
        ),
        action="validate_review",
    )

    request = spec.to_job_request()

    assert request.source_mode == "zotero"
    assert request.zotero_report == "D:/report.txt"
    assert request.library_path == "D:/library"
    assert request.validate_review is True


def test_runtime_job_spec_defaults_stage1_reuse_for_stage1_actions() -> None:
    spec = RuntimeJobSpec(
        project_name="demo",
        source=RuntimeSourceSpec(mode="direct", pdf_folder="D:/papers"),
        action="run_all",
    )

    request = spec.to_job_request()

    assert request.reuse_stage1 is True


def test_runtime_job_spec_preserves_stage1_reuse_opt_out() -> None:
    spec = RuntimeJobSpec(
        project_name="demo",
        source=RuntimeSourceSpec(mode="direct", pdf_folder="D:/papers"),
        action="run_all",
        reuse_stage1=False,
    )

    request = spec.to_job_request()

    assert request.reuse_stage1 is False


def test_runtime_job_spec_rejects_unknown_requested_stages() -> None:
    spec = RuntimeJobSpec(
        project_name="demo",
        source=RuntimeSourceSpec(mode="direct", pdf_folder="D:/papers"),
        metadata={"requested_stages": ["analyze", "launch-missiles"]},
    )

    with pytest.raises(ValueError, match="unsupported requested_stages"):
        spec.validate()


def test_runtime_job_spec_rejects_generate_section_without_section_number() -> None:
    spec = RuntimeJobSpec(
        project_name="demo",
        source=RuntimeSourceSpec(mode="direct", pdf_folder="D:/papers"),
        action="generate_section",
    )

    with pytest.raises(ValueError, match="requires generate_section"):
        spec.validate()


def test_runtime_job_spec_rejects_non_positive_generate_section() -> None:
    spec = RuntimeJobSpec(
        project_name="demo",
        source=RuntimeSourceSpec(mode="direct", pdf_folder="D:/papers"),
        action="generate_section",
        generate_section=0,
    )

    with pytest.raises(ValueError, match="greater than 0"):
        spec.validate()


def test_runtime_job_spec_compiles_generate_section_action() -> None:
    spec = RuntimeJobSpec(
        project_name="demo",
        source=RuntimeSourceSpec(mode="direct", pdf_folder="D:/papers"),
        action="generate_section",
        generate_section=2,
    )

    request = spec.to_job_request()

    assert request.action == "generate_section"
    assert request.generate_section == 2


def test_runtime_job_spec_round_trip(tmp_path: Path) -> None:
    from runtime.job_spec import load_runtime_job_spec, save_runtime_job_spec

    path = tmp_path / "job-spec.json"
    spec = RuntimeJobSpec(
        project_name="demo",
        source=RuntimeSourceSpec(mode="direct", pdf_folder="D:/papers"),
        reuse_stage1=True,
        metadata={"requested_stages": ["source_intake", "analyze"]},
    )

    save_runtime_job_spec(path, spec)
    loaded = load_runtime_job_spec(path)

    assert loaded.project_name == spec.project_name
    assert loaded.source.pdf_folder == "D:\\papers"
    assert loaded.config == str((tmp_path / "config.ini").resolve())
    assert loaded.queue_file == str((tmp_path / "output/_queue/queue.json").resolve())
    assert loaded.metadata == spec.metadata


def test_runtime_job_spec_paths_are_resolved_from_spec_not_cwd(tmp_path: Path, monkeypatch) -> None:
    from runtime.job_spec import load_runtime_job_spec

    spec_dir = tmp_path / "spec"
    spec_dir.mkdir()
    payload = {
        "project_name": "relative",
        "source": {"mode": "direct", "pdf_folder": "papers"},
        "config": "config/project.ini",
        "summary_file": "artifacts/summary.json",
        "summary_sources": ["sources/one.json"],
        "reuse_summary_files": ["reuse/two.json"],
        "queue_file": "queue/jobs.json",
    }
    path = spec_dir / "job.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    unrelated_cwd = tmp_path / "cwd"
    unrelated_cwd.mkdir()
    monkeypatch.chdir(unrelated_cwd)

    loaded = load_runtime_job_spec(path)

    assert loaded.source.pdf_folder == str((spec_dir / "papers").resolve())
    assert loaded.config == str((spec_dir / "config/project.ini").resolve())
    assert loaded.summary_file == str((spec_dir / "artifacts/summary.json").resolve())
    assert loaded.summary_sources == (str((spec_dir / "sources/one.json").resolve()),)
    assert loaded.reuse_summary_files == (str((spec_dir / "reuse/two.json").resolve()),)
    assert loaded.queue_file == str((spec_dir / "queue/jobs.json").resolve())
