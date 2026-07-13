from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from runtime.source_intake import (
    build_direct_source_bundle,
    build_source_bundle_for_request,
    build_zotero_source_bundle,
)
from services.job_runner import JobRunRequest
from services.source_identity import evaluate_source_identity


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
        "runtime.source_intake.parse_zotero_report_result",
        lambda _report: SimpleNamespace(
            papers=[
                {
                    "title": "A Zotero Paper",
                    "authors": ["Smith, John"],
                    "year": "2024",
                    "attachments": ["matched.pdf"],
                }
            ],
            status="ok",
            parser_route="standard",
            parser_version="zotero-parser-v1",
            report_hash="report-hash",
            parse_confidence=1.0,
            stats=SimpleNamespace(to_dict=lambda: {"parsed_entries": 1}),
            diagnostics=(),
        ),
    )
    monkeypatch.setattr("runtime.source_intake.create_file_index", lambda _library: object())
    monkeypatch.setattr(
        "runtime.source_intake.inspect_pdf_identity",
        lambda paper, _path: evaluate_source_identity(paper, paper),
    )
    monkeypatch.setattr(
        "runtime.source_intake.resolve_pdf_match",
        lambda *_args, **_kwargs: SimpleNamespace(
            status="matched",
            selected_path=str(matched_pdf),
            candidates=(),
            diagnostics=(),
            to_dict=lambda: {
                "status": "matched",
                "selected_path": str(matched_pdf),
                "candidates": [],
                "diagnostics": [],
            },
        ),
    )

    bundle = build_zotero_source_bundle(
        project_name="demo-zotero",
        zotero_report=str(report_path),
        library_path=str(library_path),
    )

    assert bundle.source_mode == "zotero"
    assert len(bundle.paper_work_items) == 1
    assert bundle.paper_work_items[0].source_pdf == str(matched_pdf.resolve())
    assert bundle.source_snapshot["zotero_report"] == str(report_path.resolve())
    assert bundle.source_snapshot["zotero_parse"]["report_hash"] == "report-hash"


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


def test_zotero_source_intake_surfaces_ambiguous_pdf_candidates(tmp_path: Path) -> None:
    import fitz  # type: ignore

    library = tmp_path / "storage"
    unique_pdf = library / "UNIQUE" / "unique.pdf"
    duplicate_a = library / "AAAA" / "paper.pdf"
    duplicate_b = library / "BBBB" / "paper.pdf"
    for path, marker in ((duplicate_a, b"a"), (duplicate_b, b"b")):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"%PDF-1.4\n" + marker * 2048)
    unique_pdf.parent.mkdir(parents=True, exist_ok=True)
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 72), "Unique Paper")
    document.save(unique_pdf)
    document.close()

    report = tmp_path / "report.txt"
    report.write_text(
        "\n".join(
            [
                "*",
                "Unique Paper",
                "附件\tUNIQUE/unique.pdf",
                "*",
                "Ambiguous Paper",
                "附件\tpaper.pdf",
            ]
        ),
        encoding="utf-8",
    )

    bundle = build_zotero_source_bundle(
        project_name="real-intake",
        zotero_report=str(report),
        library_path=str(library),
    )

    assert [item.paper_info["title"] for item in bundle.paper_work_items] == ["Unique Paper"]
    assert bundle.paper_work_items[0].source_pdf == str(unique_pdf.resolve())
    assert bundle.source_snapshot["missing_titles"] == []
    assert bundle.source_snapshot["ambiguous_matches"][0]["title"] == "Ambiguous Paper"
    assert [
        candidate["path"]
        for candidate in bundle.source_snapshot["ambiguous_matches"][0]["candidates"]
    ] == [
        str(duplicate_a.resolve()),
        str(duplicate_b.resolve()),
    ]
    assert bundle.source_snapshot["zotero_parse"]["status"] == "ok"


def test_zotero_source_intake_failed_parse_does_not_scan_library(
    tmp_path: Path,
    monkeypatch,
) -> None:
    report = tmp_path / "report.txt"
    report.write_text("invalid", encoding="utf-8")
    library = tmp_path / "storage"
    library.mkdir()
    scanned = False

    monkeypatch.setattr(
        "runtime.source_intake.parse_zotero_report_result",
        lambda _path: SimpleNamespace(
            status="failed",
            diagnostics=(SimpleNamespace(code="unknown_format"),),
        ),
    )

    def fail_if_scanned(_path: str):
        nonlocal scanned
        scanned = True
        raise AssertionError("FileIndex must not be built after parse failure")

    monkeypatch.setattr("runtime.source_intake.create_file_index", fail_if_scanned)

    with pytest.raises(ValueError, match="zotero_parse_failed:unknown_format"):
        build_zotero_source_bundle(
            project_name="failed-parse",
            zotero_report=str(report),
            library_path=str(library),
        )

    assert scanned is False


def test_zotero_source_intake_quarantines_identity_mismatch(
    tmp_path: Path,
    monkeypatch,
) -> None:
    report = tmp_path / "report.txt"
    report.write_text("stub", encoding="utf-8")
    library = tmp_path / "library"
    library.mkdir()
    pdf = library / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.4\n" + b"x" * 2048)
    paper = {
        "title": "Expected Paper",
        "authors": ["Alice Smith"],
        "year": "2024",
        "doi": "10.1234/expected",
        "attachments": ["paper.pdf"],
    }
    monkeypatch.setattr(
        "runtime.source_intake.parse_zotero_report_result",
        lambda _path: SimpleNamespace(
            papers=[paper],
            status="ok",
            parser_route="standard",
            parser_version="zotero-parser-v1",
            report_hash="report-hash",
            parse_confidence=1.0,
            stats=SimpleNamespace(to_dict=lambda: {"parsed_entries": 1}),
            diagnostics=(),
        ),
    )
    monkeypatch.setattr("runtime.source_intake.create_file_index", lambda _path: object())
    monkeypatch.setattr(
        "runtime.source_intake.resolve_pdf_match",
        lambda *_args, **_kwargs: SimpleNamespace(
            status="matched",
            selected_path=str(pdf),
            to_dict=lambda: {
                "status": "matched",
                "selected_path": str(pdf),
                "candidates": [],
                "diagnostics": [],
            },
        ),
    )
    monkeypatch.setattr(
        "runtime.source_intake.inspect_pdf_identity",
        lambda expected, path: evaluate_source_identity(
            expected,
            {**expected, "doi": "10.9999/wrong"},
            source_path=path,
        ),
    )

    bundle = build_zotero_source_bundle(
        project_name="quarantine",
        zotero_report=str(report),
        library_path=str(library),
    )

    assert bundle.paper_work_items == []
    assert bundle.source_snapshot["canonical_ready"] is False
    assert bundle.source_snapshot["quarantined_sources"][0]["identity_verdict"] == "mismatch"
