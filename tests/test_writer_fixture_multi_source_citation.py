from __future__ import annotations

import re

from services.citation_manifest import build_citation_manifest_from_review_draft
from services.citation_ref_catalog import extract_ref_ids_from_token
from services.review_draft import _extract_citations_from_text
from tests.test_current_runtime_full_e2e import _source_bound_writer_response


def _writer_task_scope() -> dict:
    claim = (
        "Paper A reports a 4-point increase in Study A, while Paper B reports a "
        "2-point decrease in its separate observed sample."
    )
    return {
        "schema_version": "writer_task_scope_wire/v1",
        "usable_for_provider_admission": True,
        "writer_task_basis_hash": "a" * 64,
        "evidence_store": {},
        "tasks": [{
            "writer_task_id": "task_ab",
            "status": "ready",
            "source_claim_ids": ["claim_a", "claim_b"],
            "evidence_ids": ["evidence_a", "evidence_b"],
            "source_field_ids": ["field_a", "field_b"],
            "qualifier_source_claim_ids": ["claim_a_limit", "claim_b_limit"],
            "qualifier_evidence_ids": ["evidence_a_limit", "evidence_b_limit"],
            "qualifier_source_field_ids": ["field_a_limit", "field_b_limit"],
            "output_units": [{
                "unit_kind": "planned_claim",
                "required": True,
                "writer_output_unit_id": "unit_ab",
                "max_text_chars": 4096,
            }],
            "allowed_ref_ids": ["R001", "R002"],
            "planned_claim": claim,
        }],
    }


def test_writer_fixture_cites_every_source_in_a_required_ab_claim() -> None:
    catalog = {
        "entries": [
            {"ref_id": "R001", "status": "active", "paper_id": "paper-a", "canonical_paper_key": "paper-a"},
            {"ref_id": "R002", "status": "active", "paper_id": "paper-b", "canonical_paper_key": "paper-b"},
        ],
    }
    trace: list[dict] = []
    response = _source_bound_writer_response(
        {"writer_task_scope": _writer_task_scope()},
        trace,
    )
    assert response is not None
    block = response["content"]["blocks"][0]
    token_match = re.search(r"\[\[cite_ref:[^\]]+\]\]", str(block["text"]))
    assert token_match is not None
    token = token_match.group(0)
    assert extract_ref_ids_from_token(token) == ["R001", "R002"]

    citations = _extract_citations_from_text(
        str(block["text"]),
        "writer_ab",
        citation_ref_catalog=catalog,
    )
    review_draft = {
        "content": {
            "sections": [{
                "section_number": 1,
                "section_title": "Required source comparison",
                "blocks": [{
                    "block_id": "writer_ab",
                    "block_order": 1,
                    "text": block["text"],
                    "citations": citations,
                }],
            }],
        },
    }
    summaries = [
        {"paper_info": {"canonical_paper_key": "paper-a", "source_paper_id": "paper-a", "title": "Paper A", "authors": ["Author A"], "year": "2020", "doi": "10.5555/paper-a"}},
        {"paper_info": {"canonical_paper_key": "paper-b", "source_paper_id": "paper-b", "title": "Paper B", "authors": ["Author B"], "year": "2021", "doi": "10.5555/paper-b"}},
    ]
    manifest = build_citation_manifest_from_review_draft(
        job_id="writer-fixture-citation-test",
        project_name="Writer fixture citation test",
        manifest_id="citation_manifest",
        review_draft_path="unused-review-draft.json",
        review_word_path="unused-review.docx",
        review_draft=review_draft,
        paper_summaries=summaries,
        citation_ref_catalog=catalog,
    )
    assert len(manifest.citation_sets) == 1
    assert manifest.citation_sets[0].paper_keys == ["paper-a", "paper-b"]
    assert {cluster.paper_key for cluster in manifest.clusters} == {"paper-a", "paper-b"}
    assert trace[0]["tasks"][0]["allowed_ref_ids"] == ["R001", "R002"]
