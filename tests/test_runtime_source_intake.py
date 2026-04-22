from __future__ import annotations

from pathlib import Path

from runtime.source_intake import (
    build_direct_source_bundle,
    build_source_bundle_for_request,
    build_zotero_source_bundle,
)
from services.job_runner import JobRunRequest


def test_build_direct_source_bundle_discovers_pdf_files(tmp_path: Path) -> None:
    pdf_dir = tmp_path / "papers"
    pdf_dir.mkdir()
    (pdf_dir / "alpha.pdf").write_bytes(b"%PDF-1.4\n%alpha\n")
    nested_dir = pdf_dir / "nested"
    nested_dir.mkdir()
    (nested_dir / "beta.pdf").write_bytes(b"%PDF-1.4\n%beta\n")

    bundle = build_direct_source_bundle(project_name="demo", pdf_folder=str(pdf_dir))

    assert bundle.source_mode == "direct"
    assert len(bundle.paper_work_items) == 2
    assert {Path(item.source_pdf).name for item in bundle.paper_work_items} == {"alpha.pdf", "beta.pdf"}
    assert bundle.source_snapshot["pdf_count"] == 2


def test_build_zotero_source_bundle_uses_parser_and_file_matching(monkeypatch, tmp_path: Path) -> None:
    report_path = tmp_path / "report.txt"
    library_path = tmp_path / "library"
    report_path.write_text("stub", encoding="utf-8")
    library_path.mkdir()
    matched_pdf = library_path / "matched.pdf"
    matched_pdf.write_bytes(b"%PDF-1.4\n%matched\n")

    monkeypatch.setattr(
        "runtime.source_intake.parse_zotero_report",
        lambda _report: [
            {
                "title": "A Zotero Paper",
                "authors": ["Smith, John"],
                "year": "2024",
                "attachments": ["matched.pdf"],
            }
        ],
    )
    monkeypatch.setattr("runtime.source_intake.create_file_index", lambda _library: object())
    monkeypatch.setattr("runtime.source_intake.find_pdf", lambda *_args, **_kwargs: str(matched_pdf))

    bundle = build_zotero_source_bundle(
        project_name="demo-zotero",
        zotero_report=str(report_path),
        library_path=str(library_path),
    )

    assert bundle.source_mode == "zotero"
    assert len(bundle.paper_work_items) == 1
    assert bundle.paper_work_items[0].source_pdf == str(matched_pdf.resolve())
    assert bundle.source_snapshot["zotero_report"] == str(report_path.resolve())


def test_build_source_bundle_for_request_dispatches_by_source_mode(tmp_path: Path) -> None:
    pdf_dir = tmp_path / "papers"
    pdf_dir.mkdir()
    pdf_path = pdf_dir / "alpha.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n%alpha\n")

    request = JobRunRequest(
        config="config.ini",
        project_name="demo",
        pdf_folder=str(pdf_dir),
        action="run_all",
    )

    bundle = build_source_bundle_for_request(request)

    assert bundle.project_name == "demo"
    assert bundle.source_mode == "direct"
    assert bundle.paper_work_items[0].source_pdf == str(pdf_path.resolve())
