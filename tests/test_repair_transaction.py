from __future__ import annotations

from pathlib import Path

from services.artifact_registry import ArtifactRegistry
from services.job_workspace import atomic_write_json
from tests.test_validation_closure import _bundle
from validation.repair_transaction import RepairTransactionService
from validation.run_result import (
    ClaimValidationResultV1,
    ClaimVerdict,
    ValidationInputArtifactsV1,
    ValidationRunResultV1,
)


def test_report_first_plan_persists_hash_bound_transaction(tmp_path: Path) -> None:
    workspace, registry = _bundle(tmp_path)
    draft = registry.get("review_draft")
    manifest = registry.get("citation_manifest:v3")
    assert draft is not None and manifest is not None
    claim = ClaimValidationResultV1(
        claim_result_id="claim-1",
        claim_unit_ids=(),
        citation_set_key="",
        paper_ids=(),
        block_ids=("b1",),
        claim_text="A claim.",
        claim_context="",
        verdict=ClaimVerdict.NEEDS_REVIEW,
        reasoning_summary="needs a human mapping decision",
        repair_hint="manual review",
        root_causes=("citation_mapping_error",),
        span_start=None,
        span_end=None,
        alignment_status="ambiguous",
        alignment_confidence=0.0,
        low_confidence=True,
        details={},
        evidence_candidates=(),
    )
    validation = ValidationRunResultV1.create(
        job_id=workspace.job_id,
        execution_status="succeeded",
        claim_results=(claim,),
        attempt_id="repair-attempt",
        created_at="2099-01-01T00:00:00Z",
        input_artifacts=ValidationInputArtifactsV1(
            review_draft_id=draft.artifact_id,
            review_draft_hash=draft.content_hash,
            citation_manifest_id=manifest.artifact_id,
            citation_manifest_hash=manifest.content_hash,
        ),
        expected_claim_count=1,
        review_has_citations=True,
        evidence_complete=True,
    )
    validation_path = Path(workspace.artifact_path("validation/findings.json"))
    atomic_write_json(str(validation_path), validation.to_dict())
    validation_record = registry.register_file(
        artifact_id="validation-run:repair",
        artifact_role="validation_run_result",
        artifact_type="validation_run_result",
        artifact_version="v1",
        path=validation_path,
        producer="tests",
        depends_on=[
            {
                "artifact_id": draft.artifact_id,
                "artifact_type": draft.artifact_type,
                "path": draft.path,
                "content_hash": draft.content_hash,
            },
            {
                "artifact_id": manifest.artifact_id,
                "artifact_type": manifest.artifact_type,
                "path": manifest.path,
                "content_hash": manifest.content_hash,
            },
        ],
    )

    result = RepairTransactionService(workspace, registry).create_report_only_plan()
    assert result["status"] == "available"
    assert result["transaction_id"]
    transaction = registry.get(result["transaction_id"])
    assert transaction is not None and transaction.status == "ready"
    assert result["closure"]["validation_artifact"]["artifact_id"] == validation_record.artifact_id
