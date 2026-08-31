"""Regression coverage for unambiguous Stage 1 readiness status names."""

from __future__ import annotations

from runtime.control_plane import ReviewControlPlane
from runtime.runner import RuntimeExecutionResult


def test_status_projection_distinguishes_summary_and_stage1_authority_readiness() -> None:
    result = RuntimeExecutionResult(
        job_id="job-status",
        workspace_path="C:/workspace",
        job_status="completed",
        job_disposition="unvalidated",
        canonical_ready=True,
        requires_attention=True,
        attempt_number=1,
        resumed_from_attempt=None,
        completed_stages=("source_intake", "analyze"),
        failed_stage=None,
        job_outcome_path="C:/workspace/artifacts/job_outcome.json",
        summary_schema_ready=True,
        visual_qualification_ready=False,
        stage1_authority_ready=False,
        stage1_reuse_eligible=False,
    )

    payload = ReviewControlPlane._status_payload(result)

    assert payload["SUMMARY_SCHEMA_READY"] is True
    assert payload["VISUAL_QUALIFICATION_READY"] is False
    assert payload["STAGE1_AUTHORITY_READY"] is False
    assert payload["STAGE1_REUSE_ELIGIBLE"] is False
