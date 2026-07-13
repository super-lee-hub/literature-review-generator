from __future__ import annotations

import json

from services.source_identity import (
    SourceIdentityResultV1,
    evaluate_source_identity,
    inspect_pdf_identity,
    inspect_text_identity,
)


def _paper(**overrides):
    value = {
        "title": "Trust and Consumer Choice",
        "authors": ["Alice Smith"],
        "year": "2024",
        "doi": "10.1234/trust.2024",
    }
    value.update(overrides)
    return value


def test_nonempty_doi_difference_is_mismatch_and_quarantined() -> None:
    result = evaluate_source_identity(
        _paper(),
        _paper(doi="10.9999/wrong.2024"),
    )

    assert result.identity_verdict == "mismatch"
    assert result.artifact_status == "quarantined"
    assert result.canonical_ready is False


def test_same_doi_is_match_even_when_title_format_differs() -> None:
    result = evaluate_source_identity(
        _paper(),
        _paper(title="TRUST & CONSUMER CHOICE"),
    )

    assert result.identity_verdict == "match"
    assert result.canonical_ready is True


def test_title_author_year_rule_matches_without_doi() -> None:
    result = evaluate_source_identity(
        _paper(doi=""),
        _paper(doi="", authors=[], year=""),
    )

    assert result.identity_verdict == "match"
    assert result.reasons == ("normalized_title_match_without_author_year_conflict",)


def test_title_match_with_conflicting_author_is_ambiguous() -> None:
    result = evaluate_source_identity(
        _paper(doi=""),
        _paper(doi="", authors=["Bob Jones"]),
    )

    assert result.identity_verdict == "ambiguous"
    assert "author_conflict" in result.reasons


def test_result_round_trip_preserves_verdict() -> None:
    result = evaluate_source_identity(_paper(), _paper())
    payload = json.loads(json.dumps(result.to_dict()))

    restored = SourceIdentityResultV1.from_dict(payload)

    assert restored == result


def test_text_inspection_prefers_expected_doi_when_other_doi_is_present() -> None:
    text = (
        "Trust and Consumer Choice\nAlice Smith\n"
        "https://doi.org/10.1234/trust.2024\n"
        "Prior work used doi:10.9999/other.2020"
    )

    result = inspect_text_identity(_paper(), text)

    assert result.identity_verdict == "match"
    assert result.observed["doi"] == "10.1234/trust.2024"


def test_pdf_inspection_uses_first_page_identity(tmp_path) -> None:
    import fitz  # type: ignore

    pdf_path = tmp_path / "paper.pdf"
    document = fitz.open()
    page = document.new_page()
    page.insert_text(
        (72, 72),
        "Trust and Consumer Choice\nAlice Smith\nDOI 10.1234/trust.2024",
    )
    document.save(pdf_path)
    document.close()

    result = inspect_pdf_identity(_paper(), str(pdf_path))

    assert result.identity_verdict == "match"
    assert result.candidate_hash
    assert result.evidence_hash
