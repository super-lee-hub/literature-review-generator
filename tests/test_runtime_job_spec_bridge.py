from __future__ import annotations

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
        metadata={"requested_stages": ["source_intake", "analyze"]},
    )

    save_runtime_job_spec(path, spec)
    loaded = load_runtime_job_spec(path)

    assert loaded == spec
