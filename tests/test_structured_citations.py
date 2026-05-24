"""Tests for document-stable structured citation refs."""

import pytest

from services.citation_manifest import build_citation_manifest_v2_from_review_draft
from services.citation_ref_catalog import build_document_ref_catalog, validate_citation_refs, validate_raw_citation_text
from services.review_draft import build_review_draft_v2


def _summaries():
    return [
        {
            "paper_info": {
                "title": "Test Paper 1",
                "authors": ["Author A", "Author B"],
                "year": "2023",
                "canonical_paper_key": "test_paper_1",
            },
            "ai_summary": {"core_analysis": {"summary": "Evidence about test paper one."}},
        },
        {
            "paper_info": {
                "title": "Test Paper 2",
                "authors": ["Author C"],
                "year": "2024",
                "canonical_paper_key": "test_paper_2",
            },
            "ai_summary": {"core_analysis": {"summary": "Evidence about test paper two."}},
        },
    ]


def _catalog():
    return build_document_ref_catalog(_summaries(), project_name="test_project", job_id="test_job")


def test_review_draft_v2_resolves_cite_ref_via_catalog():
    catalog = _catalog()
    draft = build_review_draft_v2(
        job_id="test_job",
        project_name="test_project",
        draft_id="test_draft",
        outline_artifact_id="test_outline",
        outline_source_path="test_outline.md",
        summary_file="test_summaries.json",
        review_word_path="test_review.docx",
        sections=[
            {
                "section_number": 1,
                "section_title": "Introduction",
                "content": "This is a catalog-backed claim [[cite_ref:R001]].",
            }
        ],
        references=[],
        generation_mode="full_review",
        paper_summaries=_summaries(),
        citation_ref_catalog=catalog,
        citation_ref_catalog_path="catalog.json",
        citation_ref_catalog_hash=catalog["catalog_hash"],
    )

    block = draft.content["sections"][0].blocks[0]
    citation = block.citations[0]
    assert citation["ref_id"] == "R001"
    assert citation["paper_id"] == "test_paper_1"
    assert citation["canonical_paper_key"] == "test_paper_1"
    assert citation["source_type"] == "structured_ref"
    assert draft.generation_context["citation_ref_catalog_path"] == "catalog.json"
    assert draft.generation_context["citation_ref_catalog_hash"] == catalog["catalog_hash"]


def test_review_draft_v2_expands_combined_cite_ref_token():
    catalog = _catalog()
    draft = build_review_draft_v2(
        job_id="test_job",
        project_name="test_project",
        draft_id="test_draft",
        outline_artifact_id="test_outline",
        outline_source_path="test_outline.md",
        summary_file="test_summaries.json",
        review_word_path="test_review.docx",
        sections=[
            {
                "section_number": 1,
                "section_title": "Introduction",
                "content": "This is a catalog-backed claim [[cite_ref:R001, R002]].",
            }
        ],
        references=[],
        generation_mode="full_review",
        paper_summaries=_summaries(),
        citation_ref_catalog=catalog,
    )

    citations = draft.content["sections"][0].blocks[0].citations
    assert [citation["ref_id"] for citation in citations] == ["R001", "R002"]
    assert [citation["paper_id"] for citation in citations] == ["test_paper_1", "test_paper_2"]
    assert all(citation["citation_token"] == "[[cite_ref:R001, R002]]" for citation in citations)


def test_review_draft_v2_records_legacy_warnings_without_truth_fields():
    draft = build_review_draft_v2(
        job_id="test_job",
        project_name="test_project",
        draft_id="test_draft",
        outline_artifact_id="test_outline",
        outline_source_path="test_outline.md",
        summary_file="test_summaries.json",
        review_word_path="test_review.docx",
        sections=[
            {
                "section_number": 1,
                "section_title": "Introduction",
                "content": "Legacy [[cite:test_paper_1]] and APA (Author A, 2023).",
            }
        ],
        references=[],
        generation_mode="full_review",
        paper_summaries=_summaries(),
        citation_ref_catalog=_catalog(),
    )

    citations = draft.content["sections"][0].blocks[0].citations
    assert {citation["source_type"] for citation in citations} == {"legacy_token", "legacy_apa"}
    assert all(citation["paper_id"] is None for citation in citations)
    assert all(citation["canonical_paper_key"] is None for citation in citations)


def test_citation_manifest_truth_only_from_structured_ref():
    catalog = _catalog()
    review_draft_v2 = {
        "content": {
            "sections": [
                {
                    "section_number": 1,
                    "section_title": "Introduction",
                    "blocks": [
                        {
                            "block_id": "s1_b1",
                            "block_order": 1,
                            "text": "Claim one [[cite_ref:R001]]. Legacy [[cite:test_paper_2]].",
                            "citations": [
                                {
                                    "citation_token": "[[cite_ref:R001]]",
                                    "ref_id": "R001",
                                    "source_type": "structured_ref",
                                    "span_start": 10,
                                    "span_end": 27,
                                },
                                {
                                    "citation_token": "[[cite:test_paper_2]]",
                                    "source_type": "legacy_token",
                                    "span_start": 36,
                                    "span_end": 57,
                                },
                            ],
                        }
                    ],
                }
            ]
        }
    }

    manifest = build_citation_manifest_v2_from_review_draft(
        job_id="test_job",
        project_name="test_project",
        manifest_id="test_manifest",
        review_draft_path="test_draft.json",
        review_word_path="test_review.docx",
        review_draft_v2=review_draft_v2,
        paper_summaries=_summaries(),
        citation_ref_catalog=catalog,
    )

    assert len(manifest.occurrences) == 1
    assert manifest.occurrences[0].ref_id == "R001"
    assert manifest.occurrences[0].paper_id == "test_paper_1"
    assert len(manifest.bibliography) == 1
    assert manifest.bibliography[0].paper_id == "test_paper_1"
    assert manifest.fallback_counters["legacy_tokens"] == 1


def test_citation_manifest_expands_combined_cite_ref_token():
    catalog = _catalog()
    review_draft_v2 = {
        "content": {
            "sections": [
                {
                    "section_number": 1,
                    "section_title": "Introduction",
                    "blocks": [
                        {
                            "block_id": "s1_b1",
                            "block_order": 1,
                            "text": "Claim one [[cite_ref:R001, R002]].",
                            "citations": [
                                {
                                    "citation_token": "[[cite_ref:R001, R002]]",
                                    "source_type": "structured_ref",
                                    "span_start": 10,
                                    "span_end": 33,
                                },
                            ],
                        }
                    ],
                }
            ]
        }
    }

    manifest = build_citation_manifest_v2_from_review_draft(
        job_id="test_job",
        project_name="test_project",
        manifest_id="test_manifest",
        review_draft_path="test_draft.json",
        review_word_path="test_review.docx",
        review_draft_v2=review_draft_v2,
        paper_summaries=_summaries(),
        citation_ref_catalog=catalog,
    )

    assert [occurrence.ref_id for occurrence in manifest.occurrences] == ["R001", "R002"]
    assert [occurrence.paper_id for occurrence in manifest.occurrences] == ["test_paper_1", "test_paper_2"]
    assert len(manifest.bibliography) == 2


def test_warn_and_resolve_exact_unique_legacy_only():
    review_draft_v2 = {
        "content": {
            "sections": [
                {
                    "section_number": 1,
                    "section_title": "Introduction",
                    "blocks": [
                        {
                            "block_id": "s1_b1",
                            "block_order": 1,
                            "text": "Legacy [[cite:test_paper_1]].",
                            "citations": [
                                {
                                    "citation_token": "[[cite:test_paper_1]]",
                                    "source_type": "legacy_token",
                                    "span_start": 7,
                                    "span_end": 28,
                                }
                            ],
                        }
                    ],
                }
            ]
        }
    }

    manifest = build_citation_manifest_v2_from_review_draft(
        job_id="test_job",
        project_name="test_project",
        manifest_id="test_manifest",
        review_draft_path="test_draft.json",
        review_word_path="test_review.docx",
        review_draft_v2=review_draft_v2,
        paper_summaries=_summaries(),
        legacy_citation_policy="warn_and_resolve",
    )

    assert len(manifest.occurrences) == 1
    assert manifest.occurrences[0].paper_id == "test_paper_1"
    assert manifest.occurrences[0].source_type == "exact_id"
    assert manifest.legacy_warnings[0]["disposition"] == "warn_and_resolved"


def test_ambiguous_legacy_is_unresolved_needs_review():
    summaries = [
        {"paper_info": {"title": "A", "authors": ["A"], "year": "2024", "canonical_paper_key": "dup"}},
        {"paper_info": {"title": "B", "authors": ["B"], "year": "2024", "canonical_paper_key": "dup"}},
    ]
    review_draft_v2 = {
        "content": {
            "sections": [
                {
                    "section_number": 1,
                    "section_title": "Intro",
                    "blocks": [
                        {
                            "block_id": "s1_b1",
                            "block_order": 1,
                            "text": "Legacy [[cite:dup]].",
                            "citations": [{"citation_token": "[[cite:dup]]", "source_type": "legacy_token"}],
                        }
                    ],
                }
            ]
        }
    }

    manifest = build_citation_manifest_v2_from_review_draft(
        job_id="job",
        project_name="demo",
        manifest_id="manifest",
        review_draft_path="draft.json",
        review_word_path="review.docx",
        review_draft_v2=review_draft_v2,
        paper_summaries=summaries,
        legacy_citation_policy="warn_and_resolve",
    )

    assert manifest.occurrences == []
    assert manifest.legacy_warnings[0]["disposition"] == "NEEDS_REVIEW"


def test_fatal_policy_fails_on_legacy():
    with pytest.raises(ValueError, match="Legacy citation token"):
        build_citation_manifest_v2_from_review_draft(
            job_id="job",
            project_name="demo",
            manifest_id="manifest",
            review_draft_path="draft.json",
            review_word_path="review.docx",
            review_draft_v2={
                "content": {
                    "sections": [
                        {
                            "section_number": 1,
                            "section_title": "Intro",
                            "blocks": [
                                {
                                    "block_id": "s1_b1",
                                    "block_order": 1,
                                    "text": "Legacy [[cite:test_paper_1]].",
                                    "citations": [{"citation_token": "[[cite:test_paper_1]]", "source_type": "legacy_token"}],
                                }
                            ],
                        }
                    ]
                }
            },
            paper_summaries=_summaries(),
            legacy_citation_policy="fatal",
        )


def test_apa_author_year_title_and_pinyin_never_resolve():
    review_draft_v2 = {
        "content": {
            "sections": [
                {
                    "section_number": 1,
                    "section_title": "Intro",
                    "blocks": [
                        {
                            "block_id": "s1_b1",
                            "block_order": 1,
                            "text": "APA (Author A, 2023). Same surname (Author A, 2023). Title Test Paper 1. Pinyin qiu2023.",
                            "citations": [
                                {"citation_token": "(Author A, 2023)", "source_type": "legacy_apa"},
                                {"citation_token": "(Author A, 2023)", "source_type": "legacy_apa"},
                                {"citation_token": "Test Paper 1", "source_type": "legacy_apa"},
                                {"citation_token": "qiu2023", "source_type": "legacy_apa"},
                            ],
                        }
                    ],
                }
            ]
        }
    }

    manifest = build_citation_manifest_v2_from_review_draft(
        job_id="job",
        project_name="demo",
        manifest_id="manifest",
        review_draft_path="draft.json",
        review_word_path="review.docx",
        review_draft_v2=review_draft_v2,
        paper_summaries=_summaries(),
        legacy_citation_policy="warn_and_resolve",
    )

    assert manifest.occurrences == []
    assert manifest.bibliography == []
    assert len(manifest.legacy_warnings) == 4



def test_validate_citation_refs_expands_combined_tokens_and_flags_missing_refs():
    catalog = _catalog()

    result = validate_citation_refs(catalog, "Claim [[cite_ref:R001, R999]].")

    assert result["valid"] is False
    assert result["resolved"] == ["R001"]
    assert result["unresolved"] == ["R999"]
    assert result["tombstoned"] == []



def test_validate_citation_refs_flags_tombstoned_refs_in_combined_tokens():
    catalog = _catalog()
    catalog["entries"][1]["status"] = "tombstoned"

    result = validate_citation_refs(catalog, "Claim [[cite_ref:R001, R002]].")

    assert result["valid"] is False
    assert result["resolved"] == ["R001"]
    assert result["unresolved"] == []
    assert result["tombstoned"] == ["R002"]


def test_validate_raw_citation_text_rejects_illegal_tokens_and_bare_refs():
    catalog = _catalog()

    bad = validate_raw_citation_text(
        catalog,
        "Claim (Author A, 2023) cites bare R001 and bad [[cite:R001]] plus [[cite_ref:R001; R002]].",
    )

    assert bad["valid"] is False
    assert "ILLEGAL_CITATION_TOKEN" in bad["errors"]
    assert "BARE_CATALOG_REF_ID" in bad["errors"]
    assert "RAW_APA_CITATION" in bad["errors"]

    good = validate_raw_citation_text(catalog, "Claim [[cite_ref:R001, R002]].")
    assert good["valid"] is True
    assert good["resolved"] == ["R001", "R002"]


def test_catalog_rerun_id_stability_new_append_and_tombstones():
    summaries = _summaries()
    first = build_document_ref_catalog(summaries, project_name="demo", job_id="job-1")

    assert [entry["ref_id"] for entry in first["entries"]] == ["R001", "R002"]

    second = build_document_ref_catalog(
        [
            summaries[0],
            {
                "paper_info": {
                    "title": "Test Paper 3",
                    "authors": ["Author D"],
                    "year": "2025",
                    "canonical_paper_key": "test_paper_3",
                }
            },
        ],
        project_name="demo",
        job_id="job-2",
        existing_catalog=first,
    )

    active = [entry for entry in second["entries"] if entry["status"] == "active"]
    tombstoned = [entry for entry in second["entries"] if entry["status"] == "tombstoned"]
    assert [(entry["ref_id"], entry["canonical_paper_key"]) for entry in active] == [
        ("R001", "test_paper_1"),
        ("R003", "test_paper_3"),
    ]
    assert [(entry["ref_id"], entry["canonical_paper_key"]) for entry in tombstoned] == [
        ("R002", "test_paper_2")
    ]

    third = build_document_ref_catalog(
        summaries,
        project_name="demo",
        job_id="job-3",
        existing_catalog=second,
    )

    active_third = [entry for entry in third["entries"] if entry["status"] == "active"]
    tombstoned_third = [entry for entry in third["entries"] if entry["status"] == "tombstoned"]
    assert [(entry["ref_id"], entry["canonical_paper_key"]) for entry in active_third] == [
        ("R001", "test_paper_1"),
        ("R004", "test_paper_2"),
    ]
    assert {entry["ref_id"] for entry in tombstoned_third} == {"R002", "R003"}
