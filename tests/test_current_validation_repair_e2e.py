from __future__ import annotations

from typing import Any

from validation.repair_transaction import _targeted_revalidate
from validation.run_result import (
    ValidationInputArtifactsV1,
    ValidationRunDisposition,
    ValidationRunResultV1,
)


def _valid_repair_inputs() -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    draft = {
        "artifact_type": "review_draft",
        "artifact_version": "v3",
        "content": {
            "sections": [
                {
                    "section_id": "section_1",
                    "blocks": [
                        {
                            "block_id": "s1_b1",
                            "block_kind": "paragraph",
                            "text": "The treatment improved the outcome.",
                        }
                    ],
                }
            ]
        },
    }
    manifest = {
        "artifact_type": "citation_manifest",
        "artifact_version": "v3",
        "occurrences": [
            {
                "occurrence_id": "occ_1",
                "block_id": "s1_b1",
                "ref_id": "R001",
                "paper_id": "paper-1",
                "paper_key": "paper-1",
            }
        ],
        "paper_entries": [{"paper_id": "paper-1", "paper_key": "paper-1"}],
        "bibliography": [{"paper_id": "paper-1", "paper_key": "paper-1"}],
    }
    paper_artifacts = [
        {
            "paper_identity": {
                "canonical_paper_key": "paper-1",
                "source_paper_id": "paper-1",
            }
        }
    ]
    catalog = {
        "entries": [
            {
                "ref_id": "R001",
                "status": "active",
                "paper_id": "paper-1",
                "canonical_paper_key": "paper-1",
            }
        ]
    }
    return draft, manifest, paper_artifacts, catalog


def test_current_targeted_revalidation_accepts_grounded_derived_review() -> None:
    draft, manifest, papers, catalog = _valid_repair_inputs()

    result = _targeted_revalidate(draft, manifest, papers, catalog)

    assert result["passed"] is True
    assert result["mapped_occurrence_count"] == 1
    assert result["unresolved_occurrence_count"] == 0


def test_current_targeted_revalidation_blocks_unknown_ref_and_mapping() -> None:
    draft, manifest, papers, catalog = _valid_repair_inputs()
    manifest["occurrences"][0]["block_id"] = "missing-block"
    manifest["occurrences"][0]["ref_id"] = "R999"
    manifest["occurrences"][0]["paper_id"] = "unknown"

    result = _targeted_revalidate(draft, manifest, papers, catalog)

    assert result["passed"] is False
    assert result["unresolved_occurrence_count"] == 1
    assert any("citation_block_mapping_error" in item for item in result["diagnostics"])
    assert "citation_ref_id_unresolved:R999" in result["diagnostics"]


def test_current_validation_contract_does_not_promote_missing_claims_to_clean() -> None:
    result = ValidationRunResultV1.create(
        job_id="validation-job",
        execution_status="succeeded",
        input_artifacts=ValidationInputArtifactsV1(),
        expected_claim_count=1,
        validated_claim_count=0,
        review_has_citations=True,
        evidence_complete=True,
    )

    assert result.validation_disposition is ValidationRunDisposition.NEEDS_REVIEW
    assert result.contract_satisfied is False
