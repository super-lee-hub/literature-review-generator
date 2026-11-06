from __future__ import annotations

import hashlib
import pytest

from services.citation_manifest import _build_citation_set_bundles, CitationOccurrence, CitationSpan
from services.review_draft import build_review_draft


def _bound_manifest_input(text: str, *, duplicate: bool = False):
    binding = {
        "writer_task_id": "task-1", "writer_output_unit_id": "unit-1",
        "writer_task_basis_hash": "a" * 64,
    }
    blocks = [{"block_id": "block-1", "text": text, **binding}]
    if duplicate:
        blocks.append({"block_id": "block-2", "text": text, **binding})
    occurrences = [CitationOccurrence(
        occurrence_id=f"occ-{index}", citation_token="[[cite_ref:R001]]",
        paper_id="paper-1", paper_key="paper-1", section_number=1,
        section_title="Evidence", block_id=block["block_id"], block_order=index,
    ) for index, block in enumerate(blocks, start=1)]
    return {"content": {"sections": [{"section_number": 1, "blocks": blocks}]}}, occurrences


@pytest.mark.parametrize("text", [
    "First claim [[cite_ref:R001]]. Second claim [[cite_ref:R001]].",
    "First claim [[cite_ref:R001]]. An uncited factual assertion.",
])
def test_manifest_rejects_expanding_one_bound_unit_into_multiple_sentences(text: str) -> None:
    draft, occurrences = _bound_manifest_input(text)
    with pytest.raises(ValueError, match="one factual sentence"):
        _build_citation_set_bundles(occurrences=occurrences, review_draft=draft)


def test_manifest_rejects_duplicate_bound_output_unit() -> None:
    draft, occurrences = _bound_manifest_input("A claim [[cite_ref:R001]].", duplicate=True)
    with pytest.raises(ValueError, match="duplicated"):
        _build_citation_set_bundles(occurrences=occurrences, review_draft=draft)


def test_draft_rejects_partial_binding() -> None:
    with pytest.raises(ValueError, match="complete task"):
        build_review_draft(
            job_id="task-binding", project_name="task-binding", draft_id="draft",
            outline_artifact_id="outline", outline_source_path="outline.json",
            summary_file="summaries.json", review_word_path="review.docx",
            sections=[{"section_number": 1, "blocks": [{
                "block_id": "block-1", "text": "A claim.", "writer_task_id": "task-1",
            }]}], references=[], generation_mode="review_v3",
        )


def test_writer_task_identity_survives_draft_and_manifest() -> None:
    text = "A bounded claim [[cite_ref:R001]]."
    binding = {
        "writer_task_id": "writer-task:source-claim-1",
        "writer_output_unit_id": "writer-unit:source-claim-1",
        "writer_task_basis_hash": "a" * 64,
    }
    draft = build_review_draft(
        job_id="task-binding", project_name="task-binding", draft_id="draft",
        outline_artifact_id="outline", outline_source_path="outline.json",
        summary_file="summaries.json", review_word_path="review.docx",
        sections=[{"section_number": 1, "section_title": "Evidence", "blocks": [
            {"block_id": "block-1", "text": text, **binding},
        ]}], references=[], generation_mode="review_v3",
    ).to_dict()
    block = draft["content"]["sections"][0]["blocks"][0]
    assert {name: block[name] for name in binding} == binding
    token_start = text.index("[[cite_ref:")
    token_end = text.index("]]", token_start) + 2
    occurrence = CitationOccurrence(
        occurrence_id="occ-1", citation_token="[[cite_ref:R001]]",
        paper_id="paper-1", paper_key="paper-1", section_number=1,
        section_title="Evidence", block_id="block-1", block_order=1,
        spans=[CitationSpan("span-1", token_start, token_end, "[[cite_ref:R001]]")],
    )
    bundles = _build_citation_set_bundles(occurrences=[occurrence], review_draft=draft)
    assert len(bundles) == 1
    assert len(bundles[0].claim_units) == 1
    unit = bundles[0].claim_units[0]
    assert {name: unit[name] for name in binding} == binding
    marker = f"block-1:1:{unit['claim_text']}"
    assert unit["claim_unit_id"] == hashlib.sha256(marker.encode("utf-8")).hexdigest()[:16]


def test_legacy_draft_does_not_mint_writer_task_identity() -> None:
    draft = build_review_draft(
        job_id="legacy", project_name="legacy", draft_id="draft",
        outline_artifact_id="outline", outline_source_path="outline.json",
        summary_file="summaries.json", review_word_path="review.docx",
        sections=[{"section_number": 1, "section_title": "Evidence", "blocks": [
            {"block_id": "block-1", "text": "Existing prose."},
        ]}], references=[], generation_mode="review_v3",
    ).to_dict()
    block = draft["content"]["sections"][0]["blocks"][0]
    assert "writer_task_id" not in block
    assert "writer_output_unit_id" not in block
    assert "writer_task_basis_hash" not in block
