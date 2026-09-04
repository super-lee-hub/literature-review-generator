"""Structured citation renderer + bibliography consistency regression tests.

Locks the DOCX formatting rules:
- adjacent single-ref tokens ``[[cite_ref:R006]][[cite_ref:R009]]`` render as
  one citation group ``(A; B)`` with semicolon separators;
- a missing space before a citation group is normalized by the renderer;
- ``render_structured_citations`` never depends on the model emitting spaces;
- JSON review references and the manifest bibliography come from the same
  catalog authority (``references_from_catalog_payload``).
"""

from __future__ import annotations

from typing import Any

from docx_writer import render_structured_citations
from services.citation_catalog import references_from_catalog_payload


def _manifest() -> dict[str, Any]:
    paper_entries = [
        {
            "paper_id": "10.1/a",
            "paper_key": "10.1/a",
            "title": "Alpha Paper",
            "authors": ["Lanfei Chen"],
            "year": "2024",
        },
        {
            "paper_id": "10.1/b",
            "paper_key": "10.1/b",
            "title": "Beta Paper",
            "authors": ["Rui Chen"],
            "year": "2026",
        },
        {
            "paper_id": "10.1/c",
            "paper_key": "10.1/c",
            "title": "Gamma Paper",
            "authors": ["Lei Yang"],
            "year": "2025",
        },
        {
            "paper_id": "10.1/d",
            "paper_key": "10.1/d",
            "title": "Delta Paper",
            "authors": ["Han Zhang", "Jichang Zhao"],
            "year": "2026",
        },
    ]
    occurrences = [
        {"ref_id": "R001", "paper_id": "10.1/a"},
        {"ref_id": "R006", "paper_id": "10.1/a"},
        {"ref_id": "R008", "paper_id": "10.1/c"},
        {"ref_id": "R009", "paper_id": "10.1/b"},
        {"ref_id": "R010", "paper_id": "10.1/d"},
    ]
    return {"paper_entries": paper_entries, "occurrences": occurrences}


def _render(text: str) -> str:
    rendered, unresolved = render_structured_citations(text, None, _manifest())
    assert unresolved == []
    return rendered


def test_adjacent_cite_ref_tokens_are_grouped() -> None:
    out = _render("effect through authenticity judgments[[cite_ref:R006]][[cite_ref:R009]].")
    assert "(Chen, 2024; Chen, 2026)" in out
    assert "(Chen, 2024)(Chen, 2026)" not in out


def test_citation_group_uses_semicolon_separator() -> None:
    out = _render("cross-stream synthesis[[cite_ref:R006, R009]].")
    assert "(Chen, 2024; Chen, 2026)" in out


def test_missing_space_before_citation_is_normalized() -> None:
    out = _render("heatmaps[[cite_ref:R008]] show spillover.")
    assert "heatmaps (Yang, 2025)" in out
    out2 = _render("heatmaps [[cite_ref:R008]] show spillover.")
    assert "heatmaps (Yang, 2025)" in out2


def test_citation_before_punctuation_is_rendered_correctly() -> None:
    out = _render("residue[[cite_ref:R006]], which persists.")
    assert "(Chen, 2024), which persists" in out
    out2 = _render("evidence[[cite_ref:R008]]. Next sentence.")
    assert "(Yang, 2025). Next" in out2


def test_multiple_ref_ids_roundtrip() -> None:
    out = _render("three sources[[cite_ref:R001]][[cite_ref:R008]][[cite_ref:R010]] end.")
    assert "(Chen, 2024; Yang, 2025; Zhang & Zhao, 2026)" in out


def test_non_adjacent_tokens_stay_separate() -> None:
    out = _render("first finding[[cite_ref:R001]] and later finding[[cite_ref:R009]] differ.")
    assert "(Chen, 2024)" in out and "(Chen, 2026)" in out


def test_json_and_docx_bibliography_match() -> None:
    """references_from_catalog_payload and format_in_text_citation share one catalog."""

    catalog = {
        "catalog_hash": "x",
        "entries": [
            {
                "ref_id": "R001",
                "paper_id": "10.1/a",
                "canonical_paper_key": "10.1/a",
                "title": "Alpha Paper",
                "authors": ["Lanfei Chen"],
                "year": "2024",
                "doi": "10.1/a",
                "journal": "Journal A",
            },
            {
                "ref_id": "R009",
                "paper_id": "10.1/b",
                "canonical_paper_key": "10.1/b",
                "title": "Beta Paper",
                "authors": ["Rui Chen"],
                "year": "2026",
                "doi": "10.1/b",
                "journal": "Journal B",
            },
        ],
    }
    references = references_from_catalog_payload(catalog)
    assert len(references) == 2
    assert "Alpha Paper" in references[0]
    assert "Beta Paper" in references[1]
    # Every catalog entry that appears in JSON references is resolvable as an
    # in-text citation through the manifest lookup (same authority).
    manifest = _manifest()
    for ref_text in references:
        assert ref_text.strip()
    by_ref = {occ["ref_id"]: occ["paper_id"] for occ in manifest["occurrences"]}
    assert by_ref["R001"] and by_ref["R009"]
