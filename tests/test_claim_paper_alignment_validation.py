import json
import os
import re
import types

from services.citation_manifest import build_citation_manifest_v3_from_review_draft
from services.citation_ref_catalog import build_document_ref_catalog
from services.review_draft import build_review_draft_v2
from validation.claim_alignment_audit import build_claim_alignment_audit
from validation.review_validator import ReviewValidator, ValidationConclusion
import validator


def _review_draft(text: str) -> dict:
    citations = [
        {
            "local_ref_id": f"s1_b1_cite_{index}",
            "citation_token": match.group(0),
            "paper_key": match.group(1).strip(),
            "paper_id": match.group(1).strip(),
            "raw_text": match.group(0),
            "mode": "parenthetical",
            "locator": None,
            "block_id": "s1_b1",
            "span_start": match.start(),
            "span_end": match.end(),
            "source_type": "structured_token",
        }
        for index, match in enumerate(re.finditer(r"\[\[cite:([^|\]]+)(?:\|[^\]]+)*\]\]", text), start=1)
    ]
    return {
        "content": {
            "sections": [
                {
                    "section_number": 1,
                    "section_title": "Findings",
                    "blocks": [
                        {
                            "block_id": "s1_b1",
                            "block_kind": "paragraph",
                            "block_order": 1,
                            "text": text,
                            "anchor_hash": "anchor1",
                            "citations": citations,
                        }
                    ],
                }
            ]
        }
    }


def _paper(paper_id: str, text: str, title: str | None = None) -> dict:
    return {
        "paper_identity": {"canonical_paper_key": paper_id, "source_paper_id": paper_id},
        "paper_info": {"title": title or f"Paper {paper_id}", "authors": ["A. Author"], "year": "2026"},
        "analysis": {"preprocess": {"normalized_text": text}},
        "stage1_inputs": {"selected_visual_refs": []},
        "source": {"source_pdf": f"{paper_id}.pdf"},
    }


def _claim_unit(
    claim_unit_id: str,
    claim_text: str,
    supporting_paper_ids: list[str] | None,
    *,
    alignment_status: str = "explicit",
    paper_ids: list[str] | None = None,
    pooled_paper_ids: list[str] | None = None,
) -> dict:
    unit = {
        "claim_unit_id": claim_unit_id,
        "citation_set_key": "A+B+C",
        "validation_bundle_id": f"A+B+C:{claim_unit_id}",
        "paper_ids": paper_ids or ["A", "B", "C"],
        "block_id": "s1_b1",
        "sentence_index": 1,
        "span_start": 0,
        "span_end": len(claim_text),
        "claim_text": claim_text,
        "citation_tokens": [f"[[cite:{paper_id}]]" for paper_id in (paper_ids or ["A", "B", "C"])],
        "block_anchor_hash": "anchor1",
        "supporting_paper_ids": supporting_paper_ids or [],
        "supporting_paper_keys": supporting_paper_ids or [],
        "supporting_occurrence_ids": [f"occ_{paper_id}" for paper_id in (supporting_paper_ids or [])],
        "alignment_status": alignment_status,
        "alignment_confidence": 0.9 if alignment_status in {"explicit", "inferred"} else 0.35,
    }
    if pooled_paper_ids:
        unit["pooled_paper_ids"] = pooled_paper_ids
        unit["pooled_occurrence_ids"] = [f"occ_{paper_id}" for paper_id in pooled_paper_ids]
    return unit


def _manifest(claim_units: list[dict], paper_ids: list[str] | None = None) -> dict:
    paper_ids = paper_ids or ["A", "B", "C"]
    return {
        "artifact_version": "v3",
        "citation_sets": [
            {
                "bundle_id": "bundle_abc",
                "citation_set_key": "+".join(paper_ids),
                "paper_ids": paper_ids,
                "paper_keys": paper_ids,
                "occurrence_ids": [f"occ_{paper_id}" for paper_id in paper_ids],
                "block_ids": ["s1_b1"],
                "section_numbers": [1],
                "section_titles": ["Findings"],
                "claim_texts": [unit["claim_text"] for unit in claim_units],
                "claim_units": claim_units,
                "citation_tokens": [f"[[cite:{paper_id}]]" for paper_id in paper_ids],
            }
        ],
    }


def test_reliable_mapped_claims_each_pass_against_own_paper_only():
    review_draft = _review_draft("Claim1 A support [[cite:A]]. Claim2 B support [[cite:B]]. Claim3 C support [[cite:C]].")
    claim_units = [
        _claim_unit("cu_a", "Claim1 A support.", ["A"], paper_ids=["A"]),
        _claim_unit("cu_b", "Claim2 B support.", ["B"], paper_ids=["B"]),
        _claim_unit("cu_c", "Claim3 C support.", ["C"], paper_ids=["C"]),
    ]
    paper_artifacts = [
        _paper("A", "Claim1 A support."),
        _paper("B", "Claim2 B support."),
        _paper("C", "Claim3 C support."),
    ]

    result = ReviewValidator(review_draft, _manifest(claim_units), paper_artifacts).validate().citation_results[0]

    assert result.conclusion == ValidationConclusion.SUPPORTED
    by_id = {item["claim_unit_id"]: item for item in result.details["claim_unit_results"]}
    assert by_id["cu_a"]["checked_paper_ids"] == ["A"]
    assert by_id["cu_b"]["checked_paper_ids"] == ["B"]
    assert by_id["cu_c"]["checked_paper_ids"] == ["C"]
    assert all(item["paper_resolution_source"] == "claim_unit_supporting_paper_ids" for item in by_id.values())


def test_supporting_paper_ids_limit_claim_to_expected_paper():
    review_draft = _review_draft("Claim1 A support [[cite:A]] [[cite:B]] [[cite:C]].")
    claim_units = [_claim_unit("cu_a", "Claim1 A support.", ["A"], paper_ids=["A", "B", "C"])]
    paper_artifacts = [
        _paper("A", "Claim1 A support."),
        _paper("B", "Unrelated B evidence."),
        _paper("C", "Unrelated C evidence."),
    ]

    result = ReviewValidator(review_draft, _manifest(claim_units), paper_artifacts).validate().citation_results[0]
    unit = result.details["claim_unit_results"][0]

    assert unit["checked_paper_ids"] == ["A"]
    assert set(result.details["per_paper_evidence_packets"]) == {"A"}
    assert unit["unsupported_expected_paper_ids"] == []


def test_reliable_alignment_marks_cited_non_contributing_paper():
    review_draft = _review_draft("Claim1 A support [[cite:A]] [[cite:B]].")
    claim_units = [_claim_unit("cu_ab", "Claim1 A support.", ["A", "B"], paper_ids=["A", "B"])]
    paper_artifacts = [_paper("A", "Claim1 A support."), _paper("B", "Different topic only.")]

    result = ReviewValidator(review_draft, _manifest(claim_units, ["A", "B"]), paper_artifacts).validate().citation_results[0]
    unit = result.details["claim_unit_results"][0]

    assert unit["checked_paper_ids"] == ["A", "B"]
    assert unit["contributing_paper_ids"] == ["A"]
    assert unit["unsupported_expected_paper_ids"] == ["B"]
    assert unit["evidence_status"] == "evidence_gap"


def test_pooled_sentence_end_citations_are_ambiguous_not_wrong_source_or_supported():
    review_draft = _review_draft("Claim1 A support; Claim2 B support; Claim3 C support [[cite:A]] [[cite:B]] [[cite:C]].")
    claim_units = [
        _claim_unit(
            "cu_pooled",
            "Claim1 A support; Claim2 B support; Claim3 C support.",
            [],
            alignment_status="ambiguous",
            paper_ids=["A", "B", "C"],
            pooled_paper_ids=["A", "B", "C"],
        )
    ]
    paper_artifacts = [
        _paper("A", "Claim1 A support."),
        _paper("B", "Claim2 B support."),
        _paper("C", "Claim3 C support."),
    ]

    result = ReviewValidator(review_draft, _manifest(claim_units), paper_artifacts).validate().citation_results[0]
    unit = result.details["claim_unit_results"][0]

    assert result.conclusion == ValidationConclusion.NEEDS_REVIEW
    assert result.conclusion != ValidationConclusion.WRONG_SOURCE
    assert result.evidence_status == "needs_review"
    assert unit["checked_paper_ids"] == []
    assert unit["reason"] == "ambiguous_claim_paper_alignment"


def test_ambiguous_alignment_still_flags_missing_paper_identity():
    review_draft = _review_draft("Claim1 A support; missing source claim [[cite:A]] [[cite:missing]].")
    claim_units = [
        _claim_unit(
            "cu_pooled_missing",
            "Claim1 A support; missing source claim.",
            [],
            alignment_status="ambiguous",
            paper_ids=["A", "missing"],
            pooled_paper_ids=["A", "missing"],
        )
    ]

    result = ReviewValidator(review_draft, _manifest(claim_units, ["A", "missing"]), [_paper("A", "Claim1 A support.")]).validate().citation_results[0]
    unit = result.details["claim_unit_results"][0]

    assert result.conclusion == ValidationConclusion.WRONG_SOURCE
    assert result.details["reason"] == "paper_not_found_in_artifacts"
    assert unit["checked_paper_ids"] == []
    assert unit["missing_papers"] == ["missing"]
    assert unit["reason"] == "paper_not_found_in_artifacts"


def test_legacy_claim_units_without_alignment_fields_fallback_to_paper_ids():
    review_draft = _review_draft("Legacy claim A support [[cite:A]].")
    claim_units = [
        {
            "claim_unit_id": "legacy_cu",
            "citation_set_key": "A+B",
            "paper_ids": ["A"],
            "block_id": "s1_b1",
            "sentence_index": 1,
            "claim_text": "Legacy claim A support.",
            "citation_tokens": ["[[cite:A]]"],
            "block_anchor_hash": "anchor1",
        }
    ]
    paper_artifacts = [_paper("A", "Legacy claim A support."), _paper("B", "Unrelated")]

    result = ReviewValidator(review_draft, _manifest(claim_units, ["A", "B"]), paper_artifacts).validate().citation_results[0]
    unit = result.details["claim_unit_results"][0]

    assert unit["alignment_status"] == "legacy_fallback"
    assert unit["paper_resolution_source"] == "claim_unit_paper_ids"
    assert unit["checked_paper_ids"] == ["A"]


def test_manifest_builder_generates_alignment_fields():
    review_draft = _review_draft("Claim1 A support. Claim2 B support. Claim3 C support [[cite:A]] [[cite:B]] [[cite:C]].")
    summaries = [
        {"status": "success", "paper_info": {"title": "A", "canonical_paper_key": "A"}, "ai_summary": {"paper_metadata": {"title": "A"}}},
        {"status": "success", "paper_info": {"title": "B", "canonical_paper_key": "B"}, "ai_summary": {"paper_metadata": {"title": "B"}}},
        {"status": "success", "paper_info": {"title": "C", "canonical_paper_key": "C"}, "ai_summary": {"paper_metadata": {"title": "C"}}},
    ]

    manifest = build_citation_manifest_v3_from_review_draft(
        job_id="job",
        project_name="project",
        manifest_id="manifest",
        review_draft_path="review.json",
        review_word_path="review.docx",
        review_draft_v2=review_draft,
        paper_summaries=summaries,
    ).to_dict()
    claim_unit = manifest["citation_sets"][0]["claim_units"][0]

    assert claim_unit["alignment_status"] == "ambiguous"
    assert claim_unit["alignment_confidence"] > 0
    assert claim_unit["supporting_paper_ids"] == []
    assert set(claim_unit["pooled_paper_ids"]) == {"A", "B", "C"}


def test_manifest_builder_infers_grouped_citations_when_prior_claims_are_locally_cited():
    review_draft = _review_draft(
        "Claim1 A support [[cite:A]]. "
        "Claim2 B and C joint support [[cite:B]] [[cite:C]]."
    )
    summaries = [
        {"status": "success", "paper_info": {"title": "A", "canonical_paper_key": "A"}, "ai_summary": {"paper_metadata": {"title": "A"}}},
        {"status": "success", "paper_info": {"title": "B", "canonical_paper_key": "B"}, "ai_summary": {"paper_metadata": {"title": "B"}}},
        {"status": "success", "paper_info": {"title": "C", "canonical_paper_key": "C"}, "ai_summary": {"paper_metadata": {"title": "C"}}},
    ]

    manifest = build_citation_manifest_v3_from_review_draft(
        job_id="job",
        project_name="project",
        manifest_id="manifest",
        review_draft_path="review.json",
        review_word_path="review.docx",
        review_draft_v2=review_draft,
        paper_summaries=summaries,
    ).to_dict()
    grouped = next(
        unit
        for citation_set in manifest["citation_sets"]
        for unit in citation_set["claim_units"]
        if set(unit.get("supporting_paper_ids") or []) == {"B", "C"}
    )

    assert grouped["alignment_status"] == "inferred"
    assert grouped["alignment_confidence"] == 0.74
    assert "pooled_paper_ids" not in grouped


def test_manifest_builder_associates_post_punctuation_citations_with_prior_claim():
    review_draft = _review_draft(
        "Claim1 A support. [[cite:A]] "
        "Claim2 B and C joint support. [[cite:B]] [[cite:C]]"
    )
    summaries = [
        {"status": "success", "paper_info": {"title": "A", "canonical_paper_key": "A"}, "ai_summary": {"paper_metadata": {"title": "A"}}},
        {"status": "success", "paper_info": {"title": "B", "canonical_paper_key": "B"}, "ai_summary": {"paper_metadata": {"title": "B"}}},
        {"status": "success", "paper_info": {"title": "C", "canonical_paper_key": "C"}, "ai_summary": {"paper_metadata": {"title": "C"}}},
    ]

    manifest = build_citation_manifest_v3_from_review_draft(
        job_id="job",
        project_name="project",
        manifest_id="manifest",
        review_draft_path="review.json",
        review_word_path="review.docx",
        review_draft_v2=review_draft,
        paper_summaries=summaries,
    ).to_dict()
    grouped = next(
        unit
        for citation_set in manifest["citation_sets"]
        for unit in citation_set["claim_units"]
        if set(unit.get("supporting_paper_ids") or []) == {"B", "C"}
    )

    assert grouped["alignment_status"] == "inferred"
    assert grouped["alignment_confidence"] == 0.74


def test_manifest_builder_infers_one_structured_group_token_despite_prior_uncited_claim():
    token = "[[cite_ref:R001,R002]]"
    summaries = [
        {"status": "success", "paper_info": {"title": "B", "canonical_paper_key": "B"}, "ai_summary": {"paper_metadata": {"title": "B"}}},
        {"status": "success", "paper_info": {"title": "C", "canonical_paper_key": "C"}, "ai_summary": {"paper_metadata": {"title": "C"}}},
    ]
    catalog = build_document_ref_catalog(
        summaries,
        project_name="project",
        job_id="job",
    )
    review_draft = build_review_draft_v2(
        job_id="job",
        project_name="project",
        draft_id="draft",
        outline_artifact_id="outline",
        outline_source_path="outline.md",
        summary_file="summaries.json",
        review_word_path="review.docx",
        sections=[
            {
                "section_number": 1,
                "section_title": "Findings",
                "content": (
                    "Uncited bridge claim. "
                    f"Claim jointly supported by B and C {token}."
                ),
            }
        ],
        references=[],
        generation_mode="full_review",
        paper_summaries=summaries,
        citation_ref_catalog=catalog,
    ).to_dict()

    manifest = build_citation_manifest_v3_from_review_draft(
        job_id="job",
        project_name="project",
        manifest_id="manifest",
        review_draft_path="review.json",
        review_word_path="review.docx",
        review_draft_v2=review_draft,
        paper_summaries=summaries,
        citation_ref_catalog=catalog,
    ).to_dict()
    grouped = next(
        unit
        for citation_set in manifest["citation_sets"]
        for unit in citation_set["claim_units"]
        if set(unit.get("supporting_paper_ids") or []) == {"B", "C"}
    )

    assert grouped["alignment_status"] == "inferred"
    assert grouped["alignment_confidence"] == 0.82
    assert "pooled_paper_ids" not in grouped


def test_manifest_builder_infers_adjacent_structured_tokens_as_one_joint_tail():
    token = "[[cite_ref:R001]] [[cite_ref:R002]]"
    summaries = [
        {"status": "success", "paper_info": {"title": "B", "canonical_paper_key": "B"}, "ai_summary": {"paper_metadata": {"title": "B"}}},
        {"status": "success", "paper_info": {"title": "C", "canonical_paper_key": "C"}, "ai_summary": {"paper_metadata": {"title": "C"}}},
    ]
    catalog = build_document_ref_catalog(
        summaries,
        project_name="project",
        job_id="job",
    )
    review_draft = build_review_draft_v2(
        job_id="job",
        project_name="project",
        draft_id="draft",
        outline_artifact_id="outline",
        outline_source_path="outline.md",
        summary_file="summaries.json",
        review_word_path="review.docx",
        sections=[
            {
                "section_number": 1,
                "section_title": "Findings",
                "content": (
                    "Uncited bridge claim. "
                    f"Claim jointly supported by B and C {token}."
                ),
            }
        ],
        references=[],
        generation_mode="full_review",
        paper_summaries=summaries,
        citation_ref_catalog=catalog,
    ).to_dict()

    manifest = build_citation_manifest_v3_from_review_draft(
        job_id="job",
        project_name="project",
        manifest_id="manifest",
        review_draft_path="review.json",
        review_word_path="review.docx",
        review_draft_v2=review_draft,
        paper_summaries=summaries,
        citation_ref_catalog=catalog,
    ).to_dict()
    grouped = next(
        unit
        for citation_set in manifest["citation_sets"]
        for unit in citation_set["claim_units"]
        if set(unit.get("supporting_paper_ids") or []) == {"B", "C"}
    )

    assert grouped["alignment_status"] == "inferred"
    assert grouped["alignment_confidence"] == 0.78
    assert "pooled_paper_ids" not in grouped


def test_claim_alignment_audit_contains_reviewable_rows():
    review_draft = _review_draft("Claim1 A support; Claim2 B support [[cite:A]] [[cite:B]].")
    claim_units = [
        _claim_unit(
            "cu_pooled",
            "Claim1 A support; Claim2 B support.",
            [],
            alignment_status="ambiguous",
            paper_ids=["A", "B"],
            pooled_paper_ids=["A", "B"],
        )
    ]
    report = ReviewValidator(review_draft, _manifest(claim_units, ["A", "B"]), [_paper("A", "Claim1 A support."), _paper("B", "Claim2 B support.")]).validate()

    audit = build_claim_alignment_audit(report)

    assert audit["ambiguous_claim_paper_alignment"]
    row = audit["ambiguous_claim_paper_alignment"][0]
    assert row["claim_text"] == "Claim1 A support; Claim2 B support."
    assert row["pooled_paper_ids"] == ["A", "B"]
    assert row["reason"] == "ambiguous_claim_paper_alignment"


def test_write_validation_reports_emits_alignment_audit(tmp_path):
    report = ReviewValidator(
        _review_draft("Claim A support [[cite:A]]."),
        _manifest([_claim_unit("cu_a", "Claim A support.", ["A"], paper_ids=["A"])], ["A"]),
        [_paper("A", "Claim A support.", title="Audit Paper A")],
    ).validate()
    workspace = types.SimpleNamespace(
        project_name="project",
        paths=types.SimpleNamespace(reports_dir=str(tmp_path)),
    )
    generator = types.SimpleNamespace(job_workspace=workspace, project_name="project")

    paths = validator._write_validation_reports(
        generator,
        report,
        [],
        validator.ValidationRepairPolicy.REPORT_ONLY,
    )

    assert os.path.exists(paths["claim_alignment_audit_json"])
    assert os.path.exists(paths["claim_alignment_audit_md"])
    audit_payload = json.loads(open(paths["claim_alignment_audit_json"], "r", encoding="utf-8").read())
    assert audit_payload["supported_sample"]


def test_ai_adjudication_cannot_promote_ambiguous_alignment_to_supported(monkeypatch):
    result = ReviewValidator(
        _review_draft("Claim1 A support; Claim2 B support [[cite:A]] [[cite:B]]."),
        _manifest(
            [
                _claim_unit(
                    "cu_pooled",
                    "Claim1 A support; Claim2 B support.",
                    [],
                    alignment_status="ambiguous",
                    paper_ids=["A", "B"],
                    pooled_paper_ids=["A", "B"],
                )
            ],
            ["A", "B"],
        ),
        [_paper("A", "Claim1 A support."), _paper("B", "Claim2 B support.")],
    ).validate().citation_results[0]

    mapped = validator._map_ai_bundle_result(
        result,
        {
            "status": "supported",
            "confidence": 0.99,
            "repair_scope": "none",
            "disposition": "keep_as_is",
            "adjudication_stage": "primary",
        },
    )

    assert mapped.conclusion == ValidationConclusion.NEEDS_REVIEW
    assert mapped.evidence_status == "needs_review"
    assert mapped.details["adjudication_status"] == "ambiguous_claim_paper_alignment"


def test_adjudication_packet_uses_checked_paper_ids_not_bundle_pooled_set():
    result = ReviewValidator(
        _review_draft("Claim A support [[cite:A]] [[cite:B]] [[cite:C]]."),
        _manifest([_claim_unit("cu_a", "Claim A support.", ["A"], paper_ids=["A", "B", "C"])]),
        [_paper("A", "Claim A support."), _paper("B", "Different"), _paper("C", "Different")],
    ).validate().citation_results[0]

    packet = validator.build_adjudication_packet(result)

    assert packet.paper_ids == ["A"]
    assert set(packet.per_paper_evidence_packets) == {"A"}


def test_ai_summary_only_cannot_produce_clean_supported_after_adjudication():
    result = ReviewValidator(
        _review_draft("Summary-only claim [[cite:A]]."),
        _manifest([_claim_unit("cu_a", "Summary-only claim.", ["A"], paper_ids=["A"])], ["A"]),
        [
            {
                "paper_identity": {"canonical_paper_key": "A", "source_paper_id": "A"},
                "analysis": {"ai_summary": {"core_analysis": {"summary": "Summary-only claim."}}},
                "stage1_inputs": {"selected_visual_refs": []},
                "source": {"source_pdf": "A.pdf"},
            }
        ],
    ).validate().citation_results[0]
    assert result.details["claim_unit_results"][0]["evidence_excerpts"] == []

    mapped = validator._map_ai_bundle_result(
        result,
        {
            "status": "supported",
            "confidence": 0.99,
            "repair_scope": "none",
            "disposition": "keep_as_is",
            "adjudication_stage": "primary",
        },
    )

    assert mapped.conclusion != ValidationConclusion.SUPPORTED
    assert mapped.evidence_status == "evidence_gap"
    assert mapped.disposition == "manual_review"
