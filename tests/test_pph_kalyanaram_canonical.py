from __future__ import annotations

import json
from pathlib import Path

from runtime.stage_contracts import PaperWorkItem
from scripts import pph_kalyanaram_canonical as canonical
from summary_schema import normalize_ai_summary


def _draft_record(source_pdf: Path) -> dict:
    return {
        "status": "success",
        "paper_info": {
            "title": "Empirical Generalizations from Reference Price Research",
            "authors": ["Gurumurthy Kalyanaram", "Russell S. Winer"],
            "year": "1995",
            "journal": "Marketing Science",
            "doi": "10.1287/mksc.14.3.G161",
            "source_paper_id": "YMCVAMMM",
            "canonical_paper_key": "doi:10.1287/mksc.14.3.G161",
            "paper_key_aliases": ["YMCVAMMM"],
        },
        "ai_summary": normalize_ai_summary(
            {
                "routing": {
                    "paper_type": "review",
                    "paper_subtype_raw": "narrative review",
                    "classification_status": "resolved",
                    "route_confidence": "high",
                },
                "core_analysis": {
                    "summary": "The review synthesizes reference-price evidence.",
                    "key_points": ["Past prices contribute to reference-price formation."],
                    "methodology": "Narrative review.",
                    "findings": "Reference prices affect choice and asymmetric price response.",
                    "conclusions": "The paper supports a bounded reference-price bridge.",
                    "relevance": "Reference-price background only.",
                    "limitations": "No direct platform unfairness test.",
                },
                "paper_metadata": {
                    "title": "Empirical Generalizations from Reference Price Research",
                    "authors": ["Gurumurthy Kalyanaram", "Russell S. Winer"],
                    "year": "1995",
                    "journal": "Marketing Science",
                    "doi": "10.1287/mksc.14.3.G161",
                },
            }
        ),
        "preprocess": {"used_ocr": False},
        "stage1_input": {
            "source_pdf": str(source_pdf),
            "zotero_parent_key": "YMCVAMMM",
        },
        "text_length": 0,
        "processing_time": "2026-07-29T15:45:00+08:00",
    }


def _work_item(source_pdf: Path) -> PaperWorkItem:
    fingerprint = canonical.file_sha256(source_pdf)
    descriptor = {
        "source_mode": "zotero",
        "source_paper_id": canonical.TARGET_DOI,
        "canonical_paper_key": canonical.TARGET_DOI,
        "paper_key_aliases": [
            canonical.TARGET_DOI,
            "empirical generalizations from reference price research",
        ],
        "source_pdf": str(source_pdf),
        "source_pdf_fingerprint": fingerprint,
        "metadata_confidence": "high",
        "metadata_source_priority_snapshot": [
            "zotero_metadata",
            "attachment_match",
            "filename",
        ],
    }
    paper_info = {
        "title": "Empirical Generalizations from Reference Price Research",
        "authors": ["Kalyanaram, Gurumurthy", "Winer, Russell S."],
        "year": "1995",
        "journal": "Marketing Science",
        "doi": "10.1287/mksc.14.3.G161",
        "source_mode": "zotero",
        "source_paper_id": canonical.TARGET_DOI,
        "canonical_paper_key": canonical.TARGET_DOI,
        "paper_key_aliases": list(descriptor["paper_key_aliases"]),
        "source_pdf": str(source_pdf),
        "pdf_path": str(source_pdf),
        "source_pdf_fingerprint": fingerprint,
        "source_descriptor": descriptor,
    }
    return PaperWorkItem(
        paper_info=paper_info,
        source_descriptor=descriptor,
        source_mode="zotero",
        canonical_paper_key=canonical.TARGET_DOI,
        source_paper_id=canonical.TARGET_DOI,
        source_pdf=str(source_pdf),
    )


def test_build_ocr_evidence_materializes_ten_nonempty_pages(tmp_path: Path) -> None:
    ocr_dir = tmp_path / "ocr"
    ocr_dir.mkdir()
    for page_number in range(1, 11):
        (ocr_dir / f"page-{page_number:02d}.txt").write_text(
            f"Page {page_number} reference-price evidence.",
            encoding="utf-8",
        )

    result = canonical.build_ocr_evidence(ocr_dir, tmp_path / "evidence")

    normalized = result.markdown_path.read_text(encoding="utf-8")
    chunks = json.loads(result.chunks_path.read_text(encoding="utf-8"))
    page_index = json.loads(result.page_index_path.read_text(encoding="utf-8"))
    assert "## PDF page 01" in normalized
    assert "## PDF page 10" in normalized
    assert len(chunks) == 10
    assert len(page_index) == 10
    assert all(item["text"].strip() for item in chunks)
    assert [item["page_number"] for item in page_index] == list(range(1, 11))


def test_materialize_summary_uses_runtime_identity_and_real_evidence(
    tmp_path: Path,
) -> None:
    source_pdf = tmp_path / "paper.pdf"
    source_pdf.write_bytes(b"%PDF-1.4\n%fixture\n")
    ocr_dir = tmp_path / "ocr"
    ocr_dir.mkdir()
    for page_number in range(1, 11):
        (ocr_dir / f"page-{page_number:02d}.txt").write_text(
            f"Page {page_number} reference-price evidence.",
            encoding="utf-8",
        )
    evidence = canonical.build_ocr_evidence(ocr_dir, tmp_path / "evidence")

    summary = canonical.materialize_summary(
        _draft_record(source_pdf),
        _work_item(source_pdf),
        evidence,
        zotero_parent_key="YMCVAMMM",
        zotero_attachment_key="SN9S4LYQ",
    )

    paper = summary["paper_info"]
    preprocess = summary["preprocess"]
    assert paper["canonical_paper_key"] == canonical.TARGET_DOI
    assert paper["source_paper_id"] == canonical.TARGET_DOI
    assert "YMCVAMMM" in paper["paper_key_aliases"]
    assert preprocess["used_ocr"] is True
    assert preprocess["markdown_path"] == str(evidence.markdown_path)
    assert preprocess["chunks_path"] == str(evidence.chunks_path)
    assert preprocess["page_index_path"] == str(evidence.page_index_path)
    assert summary["stage1_input"]["zotero_parent_key"] == "YMCVAMMM"
    assert summary["stage1_input"]["zotero_attachment_key"] == "SN9S4LYQ"
    assert summary["text_length"] == len(
        evidence.markdown_path.read_text(encoding="utf-8")
    )
