from __future__ import annotations

import pytest

from services.citation_manifest import build_citation_manifest_from_review_draft
from services.citation_ref_catalog import build_document_ref_catalog
from services.review_draft import _build_block_span_map, build_review_draft
from services.sentence_segmenter import SENTENCE_SEGMENTER_VERSION, segment_sentences
from validation.review_validator import ReviewValidator


@pytest.mark.parametrize(
    ("text", "expected_raw"),
    [
        ("第一句。第二句。", ["第一句。", "第二句。"]),
        ("第一句。 [[cite_ref:x]]", ["第一句。 [[cite_ref:x]]"]),
        (
            "Value was 3.14. Dr. Smith reported it.",
            ["Value was 3.14.", "Dr. Smith reported it."],
        ),
        ("第一句[[cite_ref:x]]。第二句。", ["第一句[[cite_ref:x]]。", "第二句。"]),
        (
            "第一句。\n[[cite_ref:x]]\n第二句。",
            ["第一句。\n[[cite_ref:x]]", "第二句。"],
        ),
    ],
)
def test_segmenter_preserves_exact_raw_spans(text: str, expected_raw: list[str]) -> None:
    spans = segment_sentences(text)

    assert [span.raw_text for span in spans] == expected_raw
    assert [span.display_text for span in spans] == [item.strip() for item in expected_raw]
    assert all(text[span.span_start:span.span_end] == span.raw_text for span in spans)


def test_sentence_spans_do_not_consume_inter_sentence_whitespace() -> None:
    text = "First sentence.  Second sentence."

    first, second = segment_sentences(text)

    assert first.raw_text == "First sentence."
    assert second.raw_text == "Second sentence."
    assert text[first.span_end:second.span_start] == "  "


def test_review_span_map_is_additive_and_versioned() -> None:
    text = "  第一句。第二句。  "

    span_map = _build_block_span_map(text)

    assert span_map["segmenter_version"] == SENTENCE_SEGMENTER_VERSION
    assert [entry["text"] for entry in span_map["sentences"]] == ["第一句。", "第二句。"]
    for entry in span_map["sentences"]:
        assert entry["raw_text"] == text[entry["span_start"]:entry["span_end"]]
        assert entry["display_text"] == entry["text"]


def _summaries() -> list[dict]:
    return [
        {
            "status": "success",
            "paper_info": {
                "title": "Paper One",
                "authors": ["Author One"],
                "year": "2024",
                "canonical_paper_key": "paper_one",
            },
            "ai_summary": {"core_analysis": {"summary": "Evidence one."}},
        },
        {
            "status": "success",
            "paper_info": {
                "title": "Paper Two",
                "authors": ["Author Two"],
                "year": "2025",
                "canonical_paper_key": "paper_two",
            },
            "ai_summary": {"core_analysis": {"summary": "Evidence two."}},
        },
    ]


def test_manifest_binds_standalone_citations_to_preceding_sentences() -> None:
    summaries = _summaries()
    catalog = build_document_ref_catalog(summaries, project_name="project", job_id="job")
    draft = build_review_draft(
        job_id="job",
        project_name="project",
        draft_id="draft",
        outline_artifact_id="outline",
        outline_source_path="outline.json",
        summary_file="summaries.json",
        review_word_path="review.docx",
        sections=[
            {
                "section_number": 1,
                "section_title": "Section",
                "content": "第一句。 [[cite_ref:R001]]\n第二句。\n[[cite_ref:R002]]",
            }
        ],
        references=[],
        generation_mode="full_review",
        paper_summaries=summaries,
        citation_ref_catalog=catalog,
    )

    manifest = build_citation_manifest_from_review_draft(
        job_id="job",
        project_name="project",
        manifest_id="manifest",
        review_draft_path="review.json",
        review_word_path="review.docx",
        review_draft=draft.to_dict(),
        paper_summaries=summaries,
        citation_ref_catalog=catalog,
    ).to_dict()

    claim_by_set = {
        item["citation_set_key"]: item["claim_units"][0]
        for item in manifest["citation_sets"]
    }
    assert claim_by_set["paper_one"]["claim_text"] == "第一句。"
    assert claim_by_set["paper_two"]["claim_text"] == "第二句。"
    assert claim_by_set["paper_one"]["span_end"] < claim_by_set["paper_two"]["span_start"]


def test_validator_rebuilds_legacy_span_map_from_block_text() -> None:
    block_text = "第一句。第二句。"
    review_draft = {
        "content": {
            "sections": [
                {
                    "blocks": [
                        {
                            "block_id": "s1_b1",
                            "text": block_text,
                            "anchor_hash": "anchor",
                            "span_map": {
                                "sentences": [
                                    {
                                        "sentence_index": 1,
                                        "span_start": 0,
                                        "span_end": len(block_text),
                                        "text": block_text,
                                    }
                                ]
                            },
                        }
                    ]
                }
            ]
        }
    }
    validator = ReviewValidator(review_draft, {"citation_sets": []}, [])
    bundle = {
        "citation_set_key": "paper_one",
        "paper_ids": ["paper_one"],
        "block_ids": ["s1_b1"],
        "claim_texts": ["第一句。", "第二句。"],
        "citation_tokens": [],
    }

    claim_units = validator._build_claim_units_for_bundle(bundle)

    assert [(unit["span_start"], unit["span_end"]) for unit in claim_units] == [(0, 4), (4, 8)]
    assert [unit["claim_text"] for unit in claim_units] == ["第一句。", "第二句。"]
