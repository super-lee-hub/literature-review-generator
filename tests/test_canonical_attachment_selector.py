from __future__ import annotations

from pathlib import Path

import fitz  # type: ignore

import runtime.canonical_attachment_selector as selector
from runtime.canonical_attachment_selector import (
    DUPLICATE_IDENTICAL,
    PRIMARY_FULLTEXT,
    SCANNED_PRIMARY,
    SUPPLEMENT,
    TRANSLATION_DERIVATIVE,
    VERSION_OF_RECORD,
    WRONG_ATTACHMENT,
    canonicalize_attachment_candidates,
)


def _text_pdf(path: Path, text: str) -> None:
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 72), text)
    document.save(path)
    document.close()


def _image_pdf(path: Path) -> None:
    from PIL import Image, ImageDraw  # type: ignore
    from io import BytesIO

    image = Image.new("RGB", (800, 1100), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((60, 60, 740, 1040), outline="black", width=8)
    draw.text((100, 120), "Scanned article page", fill="black")
    stream = BytesIO()
    image.save(stream, format="PNG")
    document = fitz.open()
    page = document.new_page(width=595, height=842)
    page.insert_image(page.rect, stream=stream.getvalue())
    document.save(path)
    document.close()


def _candidate(
    path: Path,
    key: str,
    *,
    verdict: str = "match",
    canonical_ready: bool = True,
    reasons: list[str] | None = None,
    date_added: str = "2026-01-01 00:00:00",
) -> dict:
    return {
        "path": str(path),
        "attachment_key": key,
        "date_added": date_added,
        "identity_verdict": verdict,
        "canonical_ready": canonical_ready,
        "identity_reasons": reasons or [],
        "sha256": "",
    }


def test_selector_prefers_vor_and_records_translation_supplement_and_duplicate(tmp_path: Path) -> None:
    vor = tmp_path / "published.pdf"
    duplicate = tmp_path / "published-copy.pdf"
    translation = tmp_path / "paper.zh-CN.TB_dual.pdf"
    supplement = tmp_path / "web-appendices.pdf"
    _text_pdf(vor, "Version of Record\nPaper title\nAuthor\n2024")
    duplicate.write_bytes(vor.read_bytes())
    _text_pdf(translation, "Paper title\nAuthor\n2024")
    _text_pdf(supplement, "Web Appendices for Paper title\nAuthor\n2024")

    result = canonicalize_attachment_candidates(
        [
            _candidate(vor, "VOR", date_added="2026-01-01 00:00:00"),
            _candidate(duplicate, "DUP", date_added="2026-01-02 00:00:00"),
            _candidate(translation, "TRN"),
            _candidate(supplement, "SUP"),
        ],
        parent_match_method="doi",
        parent_count=1,
    )

    assert result["status"] == "selected"
    assert result["selected_attachment_key"] == "VOR"
    assert result["selected_role"] == PRIMARY_FULLTEXT
    assert result["selected_version_class"] == VERSION_OF_RECORD
    by_key = {item["attachment_key"]: item for item in result["candidates"]}
    assert by_key["DUP"]["role"] == DUPLICATE_IDENTICAL
    assert by_key["TRN"]["role"] == TRANSLATION_DERIVATIVE
    assert by_key["SUP"]["role"] == SUPPLEMENT
    assert result["events"]["DUPLICATE_COLLAPSED"] == 1
    assert result["events"]["TRANSLATION_EXCLUDED"] == 1
    assert result["events"]["SUPPLEMENT_EXCLUDED"] == 1
    assert result["events"]["VERSION_OF_RECORD_SELECTED"] == 1


def test_selector_allows_complete_image_only_pdf_as_scanned_primary(tmp_path: Path) -> None:
    scanned = tmp_path / "scanned.pdf"
    _image_pdf(scanned)

    result = canonicalize_attachment_candidates(
        [
            _candidate(
                scanned,
                "SCAN",
                verdict="ambiguous",
                canonical_ready=False,
                reasons=["normalized_title_not_confirmed"],
            )
        ],
        parent_match_method="doi",
        parent_count=1,
    )

    assert result["status"] == "selected"
    assert result["selected_role"] == SCANNED_PRIMARY
    assert result["selected_attachment_key"] == "SCAN"
    assert result["candidates"][0]["quality"]["scanned_like"] is True
    assert result["candidates"][0]["quality"]["render_complete"] is True


def test_selector_keeps_positive_identity_conflict_blocked(tmp_path: Path) -> None:
    wrong = tmp_path / "wrong.pdf"
    _text_pdf(wrong, "Different paper\n10.9999/wrong")

    result = canonicalize_attachment_candidates(
        [_candidate(wrong, "WRONG", verdict="mismatch", canonical_ready=False)],
        parent_match_method="doi",
        parent_count=1,
    )

    assert result["status"] == "unresolved"
    assert result["candidates"][0]["role"] == WRONG_ATTACHMENT
    assert result["events"]["IDENTITY_MISMATCH"] == 1
    assert result["events"]["STILL_UNRESOLVED"] == 1


def test_selector_does_not_treat_body_cjk_noise_as_translation(monkeypatch) -> None:
    monkeypatch.setattr(
        selector,
        "inspect_pdf_quality",
        lambda _path: {
            "exists": True,
            "render_complete": True,
            "scanned_like": False,
            "page_count": 20,
            "text_chars": 24_000,
            "text_nonzero_pages": 20,
            "cjk_chars": 1_500,
            "latin_chars": 22_000,
            "metadata": {},
            "first_page_excerpt": "English article title and abstract",
        },
    )

    result = canonicalize_attachment_candidates(
        [
            {
                "path": "C:/fixture/english-original.pdf",
                "attachment_key": "ENGLISH",
                "date_added": "2026-01-01 00:00:00",
                "identity_verdict": "match",
                "canonical_ready": True,
                "identity_reasons": [],
                "sha256": "a" * 64,
            }
        ],
        parent_match_method="doi",
        parent_count=1,
    )

    assert result["status"] == "selected"
    assert result["selected_role"] == PRIMARY_FULLTEXT
    assert result["events"].get("TRANSLATION_EXCLUDED", 0) == 0


def test_selector_does_not_treat_article_supplementary_link_as_supplement(tmp_path: Path) -> None:
    article = tmp_path / "going-native.pdf"
    _text_pdf(article, "Going Native: Article title\nView supplementary material")

    result = canonicalize_attachment_candidates(
        [_candidate(article, "ARTICLE")],
        parent_match_method="doi",
        parent_count=1,
    )

    assert result["status"] == "selected"
    assert result["selected_role"] == PRIMARY_FULLTEXT
    assert result["events"].get("SUPPLEMENT_EXCLUDED", 0) == 0
