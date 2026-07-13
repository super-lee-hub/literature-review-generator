from __future__ import annotations

from types import SimpleNamespace

import pytest

from validation.run_result import (
    ClaimValidationResultV1,
    ClaimVerdict,
    ValidationExecutionStatus,
    ValidationRunDisposition,
    ValidationRunResultError,
    ValidationRunResultV1,
    claim_verdict_for_result,
    reduce_validation_disposition,
)


def _legacy_result(
    status: str,
    *,
    citation_id: str | None = None,
    conclusion: str = "",
    disposition: str = "keep_as_is",
    low_confidence: bool = False,
    reason: str = "",
) -> SimpleNamespace:
    claim_unit_id = f"unit-{citation_id or status}"
    return SimpleNamespace(
        citation_id=citation_id or status,
        citation_set_key=citation_id or status,
        paper_id="paper-1",
        paper_ids=["paper-1"],
        block_ids=["block-1"],
        claim_text=f"claim for {citation_id or status}",
        claim_context="context",
        conclusion=SimpleNamespace(value=conclusion),
        root_causes=[],
        evidence_candidates=[],
        reasoning_summary="reasoning",
        repair_hint="",
        low_confidence=low_confidence,
        evidence_status=status,
        disposition=disposition,
        claim_units=[{"claim_unit_id": claim_unit_id}],
        target_claim_unit={"claim_unit_id": claim_unit_id, "span_start": 0, "span_end": 12},
        details={
            "claim_unit_results": [
                {
                    "claim_unit_id": claim_unit_id,
                    "reason": reason,
                    "alignment_status": "exact" if not reason else "ambiguous",
                    "alignment_confidence": 1.0 if not reason else 0.4,
                }
            ]
        },
    )


@pytest.mark.parametrize(
    ("status", "disposition", "expected"),
    [
        ("supported", "keep_as_is", ClaimVerdict.SUPPORTED),
        ("supported", "narrowed_and_kept", ClaimVerdict.PARTIAL_SUPPORT),
        ("partial_support", "review_repair", ClaimVerdict.PARTIAL_SUPPORT),
        ("evidence_gap", "review_repair", ClaimVerdict.EVIDENCE_GAP),
        ("unsupported", "fail", ClaimVerdict.UNSUPPORTED),
        ("contradicted", "fail", ClaimVerdict.CONTRADICTED),
        ("wrong_source", "fail", ClaimVerdict.WRONG_SOURCE),
        ("needs_review", "manual_review", ClaimVerdict.NEEDS_REVIEW),
    ],
)
def test_claim_verdict_projection_is_explicit(
    status: str,
    disposition: str,
    expected: ClaimVerdict,
) -> None:
    assert claim_verdict_for_result(
        _legacy_result(status, disposition=disposition)
    ) is expected


def test_no_source_grounded_evidence_never_becomes_unsupported() -> None:
    result = _legacy_result(
        "evidence_gap",
        conclusion="UNSUPPORTED",
        disposition="manual_review",
    )

    assert claim_verdict_for_result(result) is ClaimVerdict.EVIDENCE_GAP


def test_unknown_adjudication_status_fails_closed_to_needs_review() -> None:
    assert claim_verdict_for_result(_legacy_result("invented_status")) is ClaimVerdict.NEEDS_REVIEW


def test_ambiguous_claim_paper_alignment_is_needs_review() -> None:
    result = _legacy_result(
        "evidence_gap",
        reason="ambiguous_claim_paper_alignment",
    )

    assert claim_verdict_for_result(result) is ClaimVerdict.NEEDS_REVIEW


def test_validation_run_result_round_trip_counts_all_verdicts() -> None:
    statuses = [
        "supported",
        "partial_support",
        "evidence_gap",
        "unsupported",
        "contradicted",
        "wrong_source",
        "needs_review",
    ]
    claims = [
        ClaimValidationResultV1.from_validation_result(
            _legacy_result(status, citation_id=f"claim-{index}")
        )
        for index, status in enumerate(statuses)
    ]
    result = ValidationRunResultV1.create(
        job_id="job-1",
        execution_status="succeeded",
        claim_results=claims,
        report_id="validation-1",
    )

    restored = ValidationRunResultV1.from_dict(result.to_dict())

    assert restored == result
    assert restored.total_claims == 7
    assert restored.contradicted_count == 1
    assert restored.claim_verdict_counts == {verdict.value: 1 for verdict in ClaimVerdict}
    assert restored.validation_disposition is ValidationRunDisposition.NEEDS_REVIEW
    assert restored.contract_satisfied is True


@pytest.mark.parametrize("execution_status", ["failed", "skipped", "cancelled"])
def test_non_succeeded_execution_is_unvalidated(execution_status: str) -> None:
    result = ValidationRunResultV1.create(
        job_id="job-1",
        execution_status=execution_status,
        failure_reason="terminal",
    )

    assert result.execution_status.value == execution_status
    assert result.validation_disposition is ValidationRunDisposition.UNVALIDATED
    assert result.contract_satisfied is False


@pytest.mark.parametrize(
    ("verdicts", "expected"),
    [
        ([ClaimVerdict.SUPPORTED], ValidationRunDisposition.CLEAN),
        ([ClaimVerdict.SUPPORTED, ClaimVerdict.EVIDENCE_GAP], ValidationRunDisposition.FINDINGS),
        ([ClaimVerdict.UNSUPPORTED], ValidationRunDisposition.FINDINGS),
        ([ClaimVerdict.CONTRADICTED], ValidationRunDisposition.NEEDS_REVIEW),
        ([ClaimVerdict.WRONG_SOURCE], ValidationRunDisposition.NEEDS_REVIEW),
        ([ClaimVerdict.NEEDS_REVIEW], ValidationRunDisposition.NEEDS_REVIEW),
    ],
)
def test_run_disposition_reducer(
    verdicts: list[ClaimVerdict],
    expected: ValidationRunDisposition,
) -> None:
    assert reduce_validation_disposition(ValidationExecutionStatus.SUCCEEDED, verdicts) is expected


def test_tampered_counts_are_rejected() -> None:
    claim = ClaimValidationResultV1.from_validation_result(_legacy_result("supported"))
    payload = ValidationRunResultV1.create(
        job_id="job-1",
        execution_status="succeeded",
        claim_results=[claim],
    ).to_dict()
    payload["claim_verdict_counts"]["supported"] = 0

    with pytest.raises(ValidationRunResultError, match="claim_verdict_counts"):
        ValidationRunResultV1.from_dict(payload)


def test_legacy_report_reader_is_explicitly_unverified() -> None:
    legacy = {
        "report_id": "legacy-report",
        "created_at": "2026-07-13T00:00:00Z",
        "total_citations": 1,
        "citation_results": [
            {
                "citation_id": "legacy-claim",
                "paper_id": "paper-1",
                "paper_ids": ["paper-1"],
                "claim_text": "legacy claim",
                "conclusion": "SUPPORTED",
                "evidence_status": "clean_supported",
                "disposition": "keep_as_is",
            }
        ],
    }

    restored = ValidationRunResultV1.from_dict(legacy)

    assert restored.compatibility_status == "legacy_unverified"
    assert restored.execution_status is ValidationExecutionStatus.SKIPPED
    assert restored.validation_disposition is ValidationRunDisposition.UNVALIDATED
    assert restored.contract_satisfied is False
