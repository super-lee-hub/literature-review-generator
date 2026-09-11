"""Tests for the current structured-citation DOCX contract."""

from __future__ import annotations

import pytest

from docx_writer import (
    append_section_to_word_document,
    generate_apa_references_from_manifest,
    rebuild_final_docx_from_manifest,
    rebuild_review_docx_from_structured_artifacts,
    scan_docx_for_unresolved_citation_tokens,
)


class MockGenerator:
    def __init__(self) -> None:
        self.logger = type("Logger", (), {"error": lambda _self, _message: None})()


def _manifest() -> dict[str, object]:
    return {
        "paper_entries": [
            {
                "paper_id": "paper_1",
                "paper_key": "paper_1",
                "title": "Structured Paper One",
                "authors": ["Alice Smith"],
                "year": "2024",
            },
            {
                "paper_id": "paper_2",
                "paper_key": "paper_2",
                "title": "Structured Paper Two",
                "authors": ["Bob Jones"],
                "year": "2025",
            },
        ],
        "occurrences": [
            {"ref_id": "R001", "paper_id": "paper_1", "paper_key": "paper_1"},
            {"ref_id": "R002", "paper_id": "paper_2", "paper_key": "paper_2"},
        ],
        "bibliography": [
            {
                "entry_id": "bib_001",
                "paper_id": "paper_1",
                "paper_key": "paper_1",
                "citation_text": "Alice Smith (2024). Structured Paper One.",
                "is_cited": True,
            },
            {
                "entry_id": "bib_002",
                "paper_id": "paper_2",
                "paper_key": "paper_2",
                "citation_text": "Bob Jones (2025). Structured Paper Two.",
                "is_cited": True,
            },
        ],
    }


def test_manifest_first_bibliography() -> None:
    references = generate_apa_references_from_manifest(
        {
            "bibliography": [
                {
                    "citation_text": "Author A, B. (2023). Test Paper 1.",
                    "is_cited": True,
                }
            ]
        },
        MockGenerator(),
    )

    assert references == ["Author A, B. (2023). Test Paper 1."]


def test_manifest_bibliography_contains_only_cited_entries() -> None:
    manifest = _manifest()
    manifest["bibliography"] = [
        manifest["bibliography"][0],
        {**manifest["bibliography"][1], "is_cited": False},
    ]

    references = generate_apa_references_from_manifest(manifest, MockGenerator())

    assert references == ["Alice Smith (2024). Structured Paper One."]


def test_rebuild_review_docx_requires_current_structured_citations(tmp_path) -> None:
    draft = {
        "content": {
            "sections": [
                {
                    "section_number": 1,
                    "section_title": "Intro",
                    "blocks": [{"text": "Structured claim [[cite_ref:R001]]."}],
                }
            ]
        }
    }
    output = tmp_path / "review.docx"
    rebuild_review_docx_from_structured_artifacts(
        MockGenerator(), draft, _manifest(), str(output)
    )

    from docx import Document

    text = "\n".join(paragraph.text for paragraph in Document(str(output)).paragraphs)
    assert "(Smith, 2024)" in text
    assert "Alice Smith (2024). Structured Paper One." in text

    draft["content"]["sections"][0]["blocks"][0]["text"] = (
        "Legacy mention [[cite:paper_1]]."
    )
    with pytest.raises(ValueError, match="section DOCX rendering failed"):
        rebuild_review_docx_from_structured_artifacts(
            MockGenerator(), draft, _manifest(), str(tmp_path / "legacy.docx")
        )


def test_final_docx_rebuild_scan_has_no_unresolved_tokens(tmp_path) -> None:
    draft = {
        "content": {
            "sections": [
                {
                    "section_number": 1,
                    "section_title": "Intro",
                    "blocks": [
                        {
                            "text": (
                                "Claim one [[cite_ref:R001]]. "
                                "Claim two [[cite_ref:R001, R002]]."
                            )
                        }
                    ],
                }
            ]
        }
    }
    output = tmp_path / "final.docx"
    scan_path = tmp_path / "scan.json"

    scan = rebuild_final_docx_from_manifest(
        MockGenerator(), draft, _manifest(), str(output), scan_report_path=str(scan_path)
    )

    assert scan["passed"] is True
    assert scan_path.exists()
    assert scan_docx_for_unresolved_citation_tokens(str(output), _manifest())[
        "unresolved_tokens"
    ] == []


def test_rebuild_review_docx_raises_when_section_append_fails(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("docx_writer.append_section_to_word_document", lambda *_args, **_kwargs: False)

    with pytest.raises(ValueError, match="section DOCX rendering failed"):
        rebuild_review_docx_from_structured_artifacts(
            MockGenerator(),
            {
                "content": {
                    "sections": [
                        {
                            "section_number": 1,
                            "section_title": "Intro",
                            "blocks": [{"text": "Claim [[cite_ref:R001]]."}],
                        }
                    ]
                }
            },
            _manifest(),
            str(tmp_path / "review.docx"),
        )


def test_docx_references_use_real_italics_and_hanging_indent(tmp_path) -> None:
    from docx import Document
    from zipfile import ZipFile

    manifest = _manifest()
    manifest["paper_entries"][0] = {
        **manifest["paper_entries"][0],
        "journal": "Journal of Testing",
        "volume": "12",
        "issue": "3",
        "pages": "44-59",
        "doi": "https://doi.org/10.1234/ABC",
    }
    manifest["bibliography"][0] = {
        **manifest["bibliography"][0],
        "citation_text": "Smith, A. (2024). Structured Paper One. Journal of Testing, 12(3), 44-59.",
    }
    output = tmp_path / "rich.docx"
    rebuild_review_docx_from_structured_artifacts(
        MockGenerator(),
        {
            "content": {
                "sections": [
                    {
                        "section_number": 1,
                        "section_title": "Intro",
                        "blocks": [{"text": "Claim [[cite_ref:R001]]."}],
                    }
                ]
            }
        },
        manifest,
        str(output),
    )

    doc = Document(str(output))
    reference = next(p for p in doc.paragraphs if p.text.startswith("Smith, A."))
    assert "*" not in reference.text
    assert reference.paragraph_format.first_line_indent is not None
    assert reference.paragraph_format.first_line_indent.pt < 0
    assert any(run.italic for run in reference.runs)
    with ZipFile(output) as archive:
        xml = archive.read("word/document.xml").decode("utf-8")
    assert "Journal of Testing" in xml
    assert "w:i" in xml


def test_docx_scanner_rejects_bare_ref_ids_and_uncited_bibliography(tmp_path) -> None:
    from docx import Document

    output = tmp_path / "leak.docx"
    doc = Document()
    doc.add_paragraph("A leaked internal reference is R001.")
    doc.add_heading("References", level=1)
    doc.add_paragraph("Bob Jones (2025). Structured Paper Two.")
    doc.save(output)

    manifest = _manifest()
    manifest["bibliography"][1] = {**manifest["bibliography"][1], "is_cited": False}
    report = scan_docx_for_unresolved_citation_tokens(str(output), manifest)

    assert report["bare_ref_ids"] == ["R001"]
    assert report["uncited_bibliography_entries"]
    assert report["passed"] is False


def test_docx_scanner_rejects_ref_without_valid_manifest_occurrence(tmp_path) -> None:
    from docx import Document

    output = tmp_path / "unknown.docx"
    doc = Document()
    doc.add_paragraph("Claim [[cite_ref:R999]].")
    doc.save(output)

    report = scan_docx_for_unresolved_citation_tokens(str(output), _manifest())

    assert "R999" in report["unresolved_tokens"]
    assert report["passed"] is False
