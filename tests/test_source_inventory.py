from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from runtime.stage_contracts import build_source_bundle
from services.source_inventory import SourceInventoryV1, build_source_inventory


def _write(path: Path, content: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def test_direct_inventory_uses_bundle_paper_identity_and_round_trips(tmp_path: Path) -> None:
    pdf_root = tmp_path / "papers"
    pdf = _write(pdf_root / "paper.pdf", b"%PDF-1.4\nsource")
    bundle = build_source_bundle(
        source_mode="direct",
        project_name="direct-project",
        papers=[
            {
                "title": "Identity Test",
                "authors": ["Smith, Alice"],
                "year": "2025",
                "doi": "https://doi.org/10.1234/ABC&DEF",
                "pdf_path": str(pdf),
            }
        ],
        source_snapshot={"pdf_folder": str(pdf_root)},
    )

    inventory = build_source_inventory(source_mode="direct", source_bundle=bundle)
    payload = inventory.to_dict()
    restored = SourceInventoryV1.from_dict(json.loads(json.dumps(payload)))

    assert inventory.project_name == "direct-project"
    assert inventory.files[0].source_type == "pdf"
    assert inventory.files[0].canonical_paper_key == "10.1234/abc&def"
    assert inventory.files[0].content_hash
    assert inventory.files[0].relative_path == "paper.pdf"
    assert restored == inventory
    assert restored.fingerprint() == payload["inventory_hash"]


def test_zotero_inventory_hashes_report_pdfs_summaries_and_classification(tmp_path: Path) -> None:
    library = tmp_path / "zotero" / "storage"
    pdf = _write(library / "ABCD" / "paper.pdf", b"%PDF-1.4\npaper")
    report = _write(tmp_path / "zotero" / "report.txt", b"*\nPaper")
    summary = _write(tmp_path / "reuse" / "summary.json", b"[]")
    classification = _write(tmp_path / "selection" / "classes.csv", b"key,class\npaper,A\n")
    bundle = build_source_bundle(
        source_mode="zotero",
        project_name="zotero-project",
        papers=[{"title": "Paper", "pdf_path": str(pdf)}],
        source_snapshot={
            "library_path": str(library),
            "zotero_report": str(report),
        },
    )

    inventory = build_source_inventory(
        source_mode="zotero",
        source_bundle=bundle,
        external_summary_paths=[summary],
        classification_paths=[classification],
    )

    by_type = {record.source_type: record for record in inventory.files}
    assert set(by_type) == {
        "zotero_report",
        "pdf",
        "external_summary",
        "classification_file",
    }
    assert all(record.status == "ready" and len(record.content_hash) == 64 for record in by_type.values())
    assert {root.root_type for root in inventory.source_roots} == {
        "zotero_library",
        "zotero_report",
        "external_summary",
        "classification",
    }


def test_fingerprint_is_input_order_insensitive(tmp_path: Path) -> None:
    pdf_root = tmp_path / "papers"
    first = _write(pdf_root / "first.pdf", b"first")
    second = _write(pdf_root / "second.pdf", b"second")
    summary_a = _write(tmp_path / "summaries" / "a.json", b"[]")
    summary_b = _write(tmp_path / "summaries" / "b.json", b"[]")

    forward = build_source_inventory(
        source_mode="direct",
        pdf_root=pdf_root,
        pdf_paths=[first, second],
        external_summary_paths=[summary_a, summary_b],
    )
    reverse = build_source_inventory(
        source_mode="direct",
        pdf_root=pdf_root,
        pdf_paths=[second, first],
        external_summary_paths=[summary_b, summary_a],
    )

    assert forward.fingerprint_payload() == reverse.fingerprint_payload()
    assert forward.fingerprint() == reverse.fingerprint()


def test_same_path_content_change_changes_inventory_fingerprint(tmp_path: Path) -> None:
    pdf_root = tmp_path / "papers"
    pdf = _write(pdf_root / "paper.pdf", b"first-version")
    before = build_source_inventory(source_mode="direct", pdf_root=pdf_root, pdf_paths=[pdf])

    pdf.write_bytes(b"second-version")
    after = build_source_inventory(source_mode="direct", pdf_root=pdf_root, pdf_paths=[pdf])

    assert before.files[0].path == after.files[0].path
    assert before.files[0].content_hash != after.files[0].content_hash
    assert before.fingerprint() != after.fingerprint()


def test_empty_direct_source_does_not_scan_or_record_cwd(monkeypatch: pytest.MonkeyPatch) -> None:
    def reject_scan(*_args, **_kwargs):
        raise AssertionError("source inventory must not scan a directory")

    monkeypatch.setattr(os, "scandir", reject_scan)
    monkeypatch.setattr(Path, "rglob", reject_scan)

    inventory = build_source_inventory(source_mode="direct")

    assert inventory.files == ()
    assert inventory.source_roots == ()
    assert str(Path.cwd()) not in json.dumps(inventory.to_dict())
    assert {diagnostic.code for diagnostic in inventory.diagnostics} == {"no_pdf_sources"}


def test_summary_only_inventory_needs_no_pdf_or_zotero_source(tmp_path: Path) -> None:
    summary = _write(tmp_path / "summaries" / "parent.json", b"[{\"status\":\"success\"}]")

    inventory = build_source_inventory(
        source_mode="summary_only",
        project_name="child-review",
        external_summary_paths=[summary],
    )

    assert [record.source_type for record in inventory.files] == ["external_summary"]
    assert inventory.files[0].status == "ready"
    assert not {"no_pdf_sources", "missing_zotero_report_source"}.intersection(
        diagnostic.code for diagnostic in inventory.diagnostics
    )


def test_missing_source_is_diagnostic_and_part_of_fingerprint(tmp_path: Path) -> None:
    missing = tmp_path / "summaries" / "missing.json"
    inventory = build_source_inventory(
        source_mode="summary_only",
        external_summary_paths=[missing],
    )

    assert inventory.files[0].status == "missing"
    assert inventory.files[0].content_hash == ""
    assert "source_file_missing" in inventory.files[0].diagnostic_codes
    assert {diagnostic.code for diagnostic in inventory.diagnostics} == {
        "source_file_missing",
        "source_root_missing",
    }
    assert inventory.fingerprint()


def test_round_trip_rejects_tampered_inventory_hash(tmp_path: Path) -> None:
    summary = _write(tmp_path / "summary.json", b"[]")
    payload = build_source_inventory(
        source_mode="summary_only",
        external_summary_paths=[summary],
    ).to_dict()
    payload["inventory_hash"] = "0" * 64

    with pytest.raises(ValueError, match="hash does not match"):
        SourceInventoryV1.from_dict(payload)
