from __future__ import annotations

import json
from pathlib import Path

from services.artifact_registry import ArtifactDependencyRefV2, ArtifactRegistry, file_sha256
from services.job_workspace import JobWorkspace, atomic_write_json
from validation.repair_transaction import RepairTransactionService
from validation.run_result import (
    ClaimValidationResultV1,
    ClaimVerdict,
    ValidationInputArtifactsV1,
    ValidationRunResultV1,
)


def _write(path: Path, payload: dict) -> Path:
    atomic_write_json(str(path), payload)
    return path


def test_repair_promotion_creates_versioned_outputs_without_replacing_canonical(
    tmp_path: Path,
) -> None:
    workspace = JobWorkspace.create(str(tmp_path), "repair", job_id="repair-promotion-job")
    registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    draft_payload = {
        "artifact_type": "review_draft",
        "artifact_version": "v3",
        "created_from_job_id": workspace.job_id,
        "created_at": "2026-01-01T00:00:00Z",
        "draft_identity": {"draft_id": "canonical-draft", "project_name": "repair"},
        "generation_context": {"section_count": 1},
        "content": {
            "sections": [
                {
                    "section_number": 1,
                    "section_title": "Evidence",
                    "title": "Evidence",
                    "blocks": [{"block_id": "b1", "text": "A grounded claim."}],
                }
            ],
            "references": [],
        },
        "projections": {},
    }
    manifest_payload = {
        "artifact_type": "citation_manifest",
        "artifact_version": "v3",
        "created_from_job_id": workspace.job_id,
        "created_at": "2026-01-01T00:00:00Z",
        "manifest_identity": {"manifest_id": "canonical-manifest", "project_name": "repair"},
        "review_reference": {
            "review_draft_path": "canonical.json",
            "review_word_path": "canonical.docx",
        },
        "paper_entries": [],
        "occurrences": [],
        "clusters": [],
        "citation_sets": [],
        "bibliography": [],
        "review_draft_version": "v3",
        "dependencies": {},
        "render_policy": {"citation_style": "APA7"},
    }
    draft_path = _write(Path(workspace.artifact_path("canonical_draft.json")), draft_payload)
    draft_record = registry.register_file(
        artifact_id="review_draft",
        artifact_role="review_draft",
        artifact_type="review_draft",
        artifact_version="v3",
        path=draft_path,
        producer="tests",
    )
    manifest_path = _write(Path(workspace.artifact_path("canonical_manifest.json")), manifest_payload)
    registry.register_file(
        artifact_id="citation_manifest:v3",
        artifact_role="citation_manifest",
        artifact_type="citation_manifest",
        artifact_version="v3",
        path=manifest_path,
        producer="tests",
        depends_on=[ArtifactDependencyRefV2.from_record(draft_record)],
    )

    derived_draft_path = _write(
        Path(workspace.artifact_path("derived_draft.json")),
        draft_payload,
    )
    derived_draft = registry.register_file(
        artifact_id="review_draft_repaired:repair-tx",
        artifact_role="review_draft_repaired",
        artifact_type="review_draft_repaired",
        artifact_version="v1",
        path=derived_draft_path,
        producer="tests",
        status="quarantined",
    )
    derived_manifest_path = _write(
        Path(workspace.artifact_path("derived_manifest.json")),
        manifest_payload,
    )
    derived_manifest = registry.register_file(
        artifact_id="citation_manifest_repaired:repair-tx",
        artifact_role="citation_manifest_repaired",
        artifact_type="citation_manifest_repaired",
        artifact_version="v1",
        path=derived_manifest_path,
        producer="tests",
        status="quarantined",
    )
    source_payload = {
        "transaction_id": "repair-tx:source",
        "job_id": workspace.job_id,
        "status": "quarantined",
        "applied_artifact_ids": [derived_draft.artifact_id, derived_manifest.artifact_id],
    }
    source_path = _write(Path(workspace.artifact_path("repair_transaction.json")), source_payload)
    source = registry.register_file(
        artifact_id="repair-tx:source",
        artifact_role="repair_transaction",
        artifact_type="repair_transaction",
        artifact_version="v1",
        path=source_path,
        producer="tests",
        status="quarantined",
    )
    versioned_draft_id = f"review_draft:v3:repair:{source.content_hash[:16]}"
    versioned_manifest_id = f"citation_manifest:v3:repair:{source.content_hash[:16]}"
    validation_candidate_draft = registry.register_file(
        artifact_id=versioned_draft_id,
        artifact_role="repair_validation_candidate_review_draft",
        artifact_type="review_draft",
        artifact_version="v3",
        path=derived_draft_path,
        producer="tests",
        status="quarantined",
    )
    validation_candidate_manifest = registry.register_file(
        artifact_id=versioned_manifest_id,
        artifact_role="repair_validation_candidate_citation_manifest",
        artifact_type="citation_manifest",
        artifact_version="v3",
        path=derived_manifest_path,
        producer="tests",
        status="quarantined",
        depends_on=[ArtifactDependencyRefV2.from_record(validation_candidate_draft)],
    )

    revalidation = ValidationRunResultV1.create(
        job_id=workspace.job_id,
        execution_status="succeeded",
        claim_results=(
            ClaimValidationResultV1(
                claim_result_id="claim:repair",
                claim_unit_ids=(),
                citation_set_key="",
                paper_ids=(),
                block_ids=("b1",),
                claim_text="A grounded claim.",
                claim_context="",
                verdict=ClaimVerdict.SUPPORTED,
                reasoning_summary="the repaired artifact is structurally grounded",
                repair_hint="",
                root_causes=(),
                span_start=0,
                span_end=18,
                alignment_status="aligned",
                alignment_confidence=1.0,
                low_confidence=False,
                details={"evidence_status": "clean_supported"},
                evidence_candidates=(),
            ),
        ),
        attempt_id="repair-revalidation",
        input_artifacts=ValidationInputArtifactsV1(
            review_draft_id=validation_candidate_draft.artifact_id,
            review_draft_hash=validation_candidate_draft.content_hash,
            citation_manifest_id=validation_candidate_manifest.artifact_id,
            citation_manifest_hash=validation_candidate_manifest.content_hash,
        ),
        expected_claim_count=1,
        review_has_citations=False,
        evidence_complete=True,
    )
    revalidation_path = _write(
        Path(workspace.artifact_path("repair_revalidation/validation.json")),
        revalidation.to_dict(),
    )
    revalidation_record = registry.register_file(
        artifact_id="validation_run_result_repaired:repair-tx:source",
        artifact_role="validation_run_result_repaired",
        artifact_type="validation_run_result_repaired",
        artifact_version="v1",
        path=revalidation_path,
        producer="tests",
        status="quarantined",
        depends_on=[
            ArtifactDependencyRefV2.from_record(validation_candidate_draft),
            ArtifactDependencyRefV2.from_record(validation_candidate_manifest),
        ],
    )
    closure_path = _write(
        Path(workspace.artifact_path("repair_revalidation/provider_receipt_closure.json")),
        {
            "artifact_type": "provider_receipt_closure",
            "artifact_version": "v1",
            "job_id": workspace.job_id,
            "complete": True,
        },
    )
    closure_record = registry.register_file(
        artifact_id="provider-receipt-closure:stage4_validate:test",
        artifact_role="provider_receipt_closure",
        artifact_type="provider_receipt_closure",
        artifact_version="v1",
        path=closure_path,
        producer="tests",
    )

    result = RepairTransactionService(workspace, registry).promote(
        source.artifact_id,
        actor="researcher",
        reason="explicit human promotion test",
        validation_result={
            "validation_run_result_payload": revalidation.to_dict(),
            "provider_receipt_closure": {"complete": True},
            "provider_receipt_closure_record_id": closure_record.artifact_id,
        },
        validation_record=revalidation_record,
    )

    assert result["status"] == "promoted", result
    assert result["canonical_replacement"] is True
    assert result["canonical_paths_unchanged"] is True
    assert registry.get("review_draft").path == str(draft_path.resolve())  # type: ignore[union-attr]
    assert registry.get("citation_manifest:v3").path == str(manifest_path.resolve())  # type: ignore[union-attr]
    assert file_sha256(validation_candidate_draft.path) == derived_draft.content_hash
    assert file_sha256(validation_candidate_manifest.path) == derived_manifest.content_hash
    for artifact_id in result["versioned_artifact_ids"]:
        record = registry.get(artifact_id)
        assert record is not None and record.status == "ready"
    promotion = registry.get(result["promotion_transaction_id"])
    assert promotion is not None and promotion.status == "ready"
    assert json.loads(Path(promotion.path).read_text(encoding="utf-8"))["status"] == "promoted"
    current_set = registry.resolve_current_artifact_set()
    assert current_set is not None
    assert current_set.review_draft_artifact_id == versioned_draft_id
    assert current_set.citation_manifest_artifact_id == versioned_manifest_id
    assert current_set.validation_receipt_closure_artifact_id == closure_record.artifact_id
    assert registry.get("current-artifact-set:pointer").status == "ready"  # type: ignore[union-attr]


def test_repair_promotion_blocks_rebinding_validation_bytes(tmp_path: Path) -> None:
    workspace = JobWorkspace.create(str(tmp_path), "repair", job_id="repair-rebind-job")
    registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    source_payload = {
        "transaction_id": "repair-tx:rebind",
        "job_id": workspace.job_id,
        "status": "quarantined",
        "applied_artifact_ids": [],
    }
    source_path = _write(
        Path(workspace.artifact_path("repair_transaction.json")),
        source_payload,
    )
    source = registry.register_file(
        artifact_id="repair-tx:rebind",
        artifact_role="repair_transaction",
        artifact_type="repair_transaction",
        artifact_version="v1",
        path=source_path,
        producer="tests",
        status="quarantined",
    )
    revalidation = ValidationRunResultV1.create(
        job_id=workspace.job_id,
        execution_status="succeeded",
        claim_results=(
            ClaimValidationResultV1(
                claim_result_id="claim:rebind",
                claim_unit_ids=(),
                citation_set_key="",
                paper_ids=(),
                block_ids=("b1",),
                claim_text="A grounded claim.",
                claim_context="",
                verdict=ClaimVerdict.SUPPORTED,
                reasoning_summary="the repaired artifact is structurally grounded",
                repair_hint="",
                root_causes=(),
                span_start=0,
                span_end=18,
                alignment_status="aligned",
                alignment_confidence=1.0,
                low_confidence=False,
                details={"evidence_status": "clean_supported"},
                evidence_candidates=(),
            ),
        ),
        attempt_id="repair-rebind-revalidation",
        input_artifacts=ValidationInputArtifactsV1(
            review_draft_id="canonical-draft",
            review_draft_hash="0" * 64,
            citation_manifest_id="canonical-manifest",
            citation_manifest_hash="1" * 64,
        ),
        expected_claim_count=1,
        review_has_citations=False,
        evidence_complete=True,
    )
    revalidation_path = _write(
        Path(workspace.artifact_path("repair_revalidation/validation.json")),
        revalidation.to_dict(),
    )
    revalidation_record = registry.register_file(
        artifact_id="validation_run_result_repaired:repair-tx:rebind",
        artifact_role="validation_run_result_repaired",
        artifact_type="validation_run_result_repaired",
        artifact_version="v1",
        path=revalidation_path,
        producer="tests",
        status="quarantined",
    )
    original_revalidation_hash = file_sha256(revalidation_path)
    promotion_id = f"repair-promotion:{source.content_hash[:16]}"

    result = RepairTransactionService(workspace, registry).promote(
        source.artifact_id,
        actor="researcher",
        reason="assert validation input binding is immutable",
        validation_result={
            "validation_run_result_payload": revalidation.to_dict(),
            "provider_receipt_closure": {"complete": True},
        },
        validation_record=revalidation_record,
    )

    assert result["status"] == "blocked", result
    assert "validation bytes cannot be rebound" in result["reason"]
    assert result["mutation_performed"] is False
    assert file_sha256(revalidation_path) == original_revalidation_hash
    assert registry.get(promotion_id) is None
    assert registry.get("current-artifact-set:pointer") is None
