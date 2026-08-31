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


def test_title_match_without_author_or_year_evidence_is_ambiguous() -> None:
    result = evaluate_source_identity(
        _paper(doi=""),
        _paper(doi="", authors=[], year=""),
    )

    assert result.identity_verdict == "ambiguous"
    assert result.artifact_status == "quarantined"
    assert result.canonical_ready is False
    assert result.reasons == ("insufficient_identity_evidence",)


def test_title_and_author_match_without_year_is_match() -> None:
    result = evaluate_source_identity(
        _paper(doi=""),
        _paper(doi="", year=""),
    )

    assert result.identity_verdict == "match"
    assert result.canonical_ready is True


def test_title_and_year_match_without_author_is_match() -> None:
    result = evaluate_source_identity(
        _paper(doi=""),
        _paper(doi="", authors=[]),
    )

    assert result.identity_verdict == "match"
    assert result.canonical_ready is True


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


def test_text_inspection_with_multiple_distinct_dois_is_ambiguous() -> None:
    text = (
        "Trust and Consumer Choice\nAlice Smith\n"
        "https://doi.org/10.1234/trust.2024\n"
        "Prior work used doi:10.9999/other.2020"
    )

    result = inspect_text_identity(_paper(), text)

    assert result.identity_verdict == "ambiguous"
    assert result.canonical_ready is False
    assert result.observed["doi"] == ""
    assert result.reasons == ("multiple_distinct_doi_candidates",)


def test_wrong_pdf_that_only_cites_expected_doi_is_ambiguous(tmp_path) -> None:
    import fitz  # type: ignore

    pdf_path = tmp_path / "wrong-paper.pdf"
    document = fitz.open()
    page = document.new_page()
    page.insert_text(
        (72, 72),
        (
            "A Different Paper\nBob Jones\nDOI 10.9999/wrong.2024\n"
            "References\nSmith (2024). https://doi.org/10.1234/trust.2024"
        ),
    )
    document.save(pdf_path)
    document.close()

    result = inspect_pdf_identity(_paper(), str(pdf_path))

    assert result.identity_verdict == "ambiguous"
    assert result.artifact_status == "quarantined"
    assert result.canonical_ready is False
    assert result.observed["doi"] == ""
    assert result.reasons == ("multiple_distinct_doi_candidates",)


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


def test_pdf_inspection_ignores_expected_doi_prefix_extension_artifact(tmp_path) -> None:
    import fitz  # type: ignore

    pdf_path = tmp_path / "concatenated-doi.pdf"
    document = fitz.open()
    page = document.new_page()
    page.insert_text(
        (72, 72),
        "Trust and Consumer Choice\nAlice Smith\nDOI 10.1234/trust.2024journalismcarlson",
    )
    document.set_metadata({"subject": "DOI 10.1234/trust.2024"})
    document.save(pdf_path)
    document.close()

    result = inspect_pdf_identity(_paper(), str(pdf_path))

    assert result.identity_verdict == "match"
    assert result.canonical_ready is True


def test_pdf_inspection_uses_first_page_author_and_year_without_doi_or_metadata(
    tmp_path,
) -> None:
    import fitz  # type: ignore

    pdf_path = tmp_path / "paper-with-text-identity.pdf"
    document = fitz.open()
    page = document.new_page()
    page.insert_text(
        (72, 72),
        "Trust and Consumer Choice\nAlice Smith\nPublished in 2024",
    )
    document.save(pdf_path)
    document.close()

    result = inspect_pdf_identity(_paper(doi=""), str(pdf_path))

    assert result.identity_verdict == "match"
    assert result.canonical_ready is True
    assert result.observed["authors"] == ["Alice Smith"]
    assert result.observed["year"] == "2024"


def test_pdf_inspection_uses_date_and_normalizes_pdf_ligatures(tmp_path) -> None:
    import fitz  # type: ignore

    pdf_path = tmp_path / "date-and-ligature.pdf"
    document = fitz.open()
    page = document.new_page()
    page.insert_text(
        (72, 72),
        "Eﬀects of Explicit Sponsorship Disclosure on User Engagement\nZike Cao\n2024",
    )
    document.save(pdf_path)
    document.close()

    result = inspect_pdf_identity(
        {
            "title": "Effects of Explicit Sponsorship Disclosure on User Engagement",
            "authors": ["Zike Cao"],
            "date": "2024-00-00 2024",
            "doi": "",
        },
        str(pdf_path),
    )

    assert result.identity_verdict == "match"
    assert result.canonical_ready is True


def test_pdf_inspection_accepts_fullwidth_doi_and_known_url_suffix_artifact(tmp_path) -> None:
    import fitz  # type: ignore

    pdf_path = tmp_path / "fullwidth-doi.pdf"
    document = fitz.open()
    page = document.new_page()
    page.insert_text(
        (72, 72),
        "Smiling Signals Intrinsic Motivation\nYimin Cheng\n"
        "doi 10.1093/jcr/ucz023/5510554",
    )
    document.set_metadata({"subject": "10．1093／jcr／ucz023／5510554"})
    document.save(pdf_path)
    document.close()

    result = inspect_pdf_identity(
        {
            "title": "Smiling Signals Intrinsic Motivation",
            "authors": ["Yimin Cheng"],
            "date": "2019",
            "doi": "10.1093/jcr/ucz023",
        },
        str(pdf_path),
    )

    assert result.identity_verdict == "match"
    assert result.observed["doi"] == "10.1093/jcr/ucz023"
