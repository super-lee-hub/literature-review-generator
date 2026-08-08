from __future__ import annotations

from types import SimpleNamespace

import pytest

from validation.run_result import (
    ClaimValidationResultV1,
    ClaimVerdict,
    ValidationExecutionStatus,
    ValidationInputArtifactsV1,
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
        input_artifacts=ValidationInputArtifactsV1(
            review_draft_id="review-1",
            review_draft_hash="a" * 64,
            citation_manifest_id="citations-1",
            citation_manifest_hash="b" * 64,
            evidence_manifest_ids=("evidence-1", "evidence-2"),
            evidence_manifest_hashes=("c" * 64, "d" * 64),
        ),
        expected_claim_count=len(claims),
        review_has_citations=True,
        evidence_complete=True,
        repair_status="not_requested",
        recheck_status="not_required",
        degradation_reasons=("non_blocking_diagnostic",),
    )

    restored = ValidationRunResultV1.from_dict(result.to_dict())

    assert restored == result
    assert restored.total_claims == 7
    assert restored.contradicted_count == 1
    assert restored.claim_verdict_counts == {verdict.value: 1 for verdict in ClaimVerdict}
    assert restored.validation_disposition is ValidationRunDisposition.NEEDS_REVIEW
    assert restored.validated_claim_count == 7
    assert restored.expected_claim_count == 7
    assert restored.evidence_complete is True
    assert restored.review_cleanliness is ValidationRunDisposition.NEEDS_REVIEW
    assert restored.input_artifacts.review_draft_id == "review-1"
    assert restored.input_artifacts.evidence_manifest_hashes == (
        "c" * 64,
        "d" * 64,
    )
    assert restored.repair_status == "not_requested"
    assert restored.recheck_status == "not_required"
    assert restored.degradation_reasons == ("non_blocking_diagnostic",)
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
    assert result.review_cleanliness is ValidationRunDisposition.UNVALIDATED
    assert result.evidence_complete is False
    assert "validation_evidence_incomplete" in result.degradation_reasons
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


def test_run_disposition_reducer_never_treats_zero_claim_context_as_clean() -> None:
    assert (
        reduce_validation_disposition(ValidationExecutionStatus.SUCCEEDED, ())
        is ValidationRunDisposition.NEEDS_REVIEW
    )
    assert (
        reduce_validation_disposition(
            ValidationExecutionStatus.SUCCEEDED,
            (),
            expected_claim_count=0,
            validated_claim_count=0,
            review_has_citations=False,
        )
        is ValidationRunDisposition.NEEDS_REVIEW
    )


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


def test_succeeded_run_with_unmet_expected_claim_count_fails_closed() -> None:
    claim = ClaimValidationResultV1.from_validation_result(_legacy_result("supported"))

    result = ValidationRunResultV1.create(
        job_id="job-1",
        execution_status="succeeded",
        claim_results=[claim],
        expected_claim_count=2,
        review_has_citations=True,
        evidence_complete=True,
    )

    assert result.validated_claim_count == 1
    assert result.evidence_complete is False
    assert result.validation_disposition is ValidationRunDisposition.NEEDS_REVIEW
    assert result.review_cleanliness is ValidationRunDisposition.NEEDS_REVIEW
    assert "expected_claim_count_unmet" in result.degradation_reasons
    assert result.contract_satisfied is False


def test_succeeded_run_with_citations_and_zero_claims_fails_closed() -> None:
    result = ValidationRunResultV1.create(
        job_id="job-1",
        execution_status="succeeded",
        expected_claim_count=0,
        review_has_citations=True,
        evidence_complete=True,
    )

    assert result.validated_claim_count == 0
    assert result.evidence_complete is False
    assert result.validation_disposition is ValidationRunDisposition.NEEDS_REVIEW
    assert result.review_cleanliness is ValidationRunDisposition.NEEDS_REVIEW
    assert "citations_present_without_validated_claims" in result.degradation_reasons
    assert result.contract_satisfied is False


def test_succeeded_zero_claim_run_without_citation_inventory_fails_closed() -> None:
    result = ValidationRunResultV1.create(
        job_id="job-1",
        execution_status="succeeded",
    )

    assert result.review_has_citations is None
    assert result.validation_disposition is ValidationRunDisposition.NEEDS_REVIEW
    assert result.review_cleanliness is ValidationRunDisposition.NEEDS_REVIEW
    assert "citation_presence_unknown_for_zero_claims" in result.degradation_reasons
    assert result.contract_satisfied is False


def test_empty_report_projection_requires_explicit_citation_free_declaration() -> None:
    report = SimpleNamespace(
        report_id="empty-report",
        total_citations=0,
        citation_results=[],
    )

    unknown = ValidationRunResultV1.from_report(report, job_id="job-1")
    citation_free = ValidationRunResultV1.from_report(
        report,
        job_id="job-1",
        review_has_citations=False,
    )

    assert unknown.validation_disposition is ValidationRunDisposition.NEEDS_REVIEW
    assert citation_free.validation_disposition is ValidationRunDisposition.NEEDS_REVIEW


def test_explicit_citation_free_review_is_not_clean_with_zero_claims() -> None:
    result = ValidationRunResultV1.create(
        job_id="job-1",
        execution_status="succeeded",
        input_artifacts=ValidationInputArtifactsV1(
            review_draft_id="review-1",
            review_draft_hash="a" * 64,
            citation_manifest_id="citation-1",
            citation_manifest_hash="b" * 64,
        ),
        expected_claim_count=0,
        review_has_citations=False,
        evidence_complete=True,
    )

    assert result.validation_disposition is ValidationRunDisposition.NEEDS_REVIEW
    assert result.review_cleanliness is ValidationRunDisposition.NEEDS_REVIEW
    assert result.contract_satisfied is False


def test_clean_run_without_verified_input_identities_does_not_satisfy_contract() -> None:
    result = ValidationRunResultV1.create(
        job_id="job-1",
        execution_status="succeeded",
        expected_claim_count=0,
        review_has_citations=False,
        evidence_complete=True,
    )

    assert result.validation_disposition is ValidationRunDisposition.NEEDS_REVIEW
    assert result.contract_satisfied is False


def test_cited_run_requires_verified_evidence_manifest_identity() -> None:
    claim = ClaimValidationResultV1.from_validation_result(_legacy_result("supported"))
    result = ValidationRunResultV1.create(
        job_id="job-1",
        execution_status="succeeded",
        claim_results=[claim],
        input_artifacts=ValidationInputArtifactsV1(
            review_draft_id="review-1",
            review_draft_hash="a" * 64,
            citation_manifest_id="citation-1",
            citation_manifest_hash="b" * 64,
        ),
        expected_claim_count=1,
        review_has_citations=True,
        evidence_complete=True,
    )

    assert result.contract_satisfied is False


@pytest.mark.parametrize(
    ("repair_status", "recheck_status", "reason"),
    [
        ("failed", "not_required", "repair_status:failed"),
        ("applied", "pending", "recheck_status:pending"),
    ],
)
def test_incomplete_repair_or_recheck_cannot_be_clean(
    repair_status: str,
    recheck_status: str,
    reason: str,
) -> None:
    claim = ClaimValidationResultV1.from_validation_result(_legacy_result("supported"))

    result = ValidationRunResultV1.create(
        job_id="job-1",
        execution_status="succeeded",
        claim_results=[claim],
        repair_status=repair_status,
        recheck_status=recheck_status,
    )

    assert result.validation_disposition is ValidationRunDisposition.NEEDS_REVIEW
    assert reason in result.degradation_reasons


def test_tampered_clean_disposition_is_rejected_when_expected_claims_are_unmet() -> None:
    claim = ClaimValidationResultV1.from_validation_result(_legacy_result("supported"))
    payload = ValidationRunResultV1.create(
        job_id="job-1",
        execution_status="succeeded",
        claim_results=[claim],
        expected_claim_count=2,
        review_has_citations=True,
    ).to_dict()
    payload["validation_disposition"] = "clean"
    payload["review_cleanliness"] = "clean"

    with pytest.raises(ValidationRunResultError, match="validation_disposition"):
        ValidationRunResultV1.from_dict(payload)


def test_incomplete_input_artifact_identity_is_rejected() -> None:
    with pytest.raises(ValidationRunResultError, match="review draft artifact identity"):
        ValidationRunResultV1.create(
            job_id="job-1",
            execution_status="failed",
            input_artifacts={"review_draft_id": "review-1"},
        )


@pytest.mark.parametrize(
    "invalid_hash",
    (
        "a" * 63,
        "A" * 64,
        "g" * 64,
    ),
)
def test_input_artifact_hashes_require_lowercase_sha256(invalid_hash: str) -> None:
    candidates = (
        ValidationInputArtifactsV1(
            review_draft_id="review-1",
            review_draft_hash=invalid_hash,
            citation_manifest_id="citation-1",
            citation_manifest_hash="b" * 64,
        ),
        ValidationInputArtifactsV1(
            review_draft_id="review-1",
            review_draft_hash="a" * 64,
            citation_manifest_id="citation-1",
            citation_manifest_hash=invalid_hash,
        ),
        ValidationInputArtifactsV1(
            review_draft_id="review-1",
            review_draft_hash="a" * 64,
            citation_manifest_id="citation-1",
            citation_manifest_hash="b" * 64,
            evidence_manifest_ids=("evidence-1",),
            evidence_manifest_hashes=(invalid_hash,),
        ),
    )
    for candidate in candidates:
        with pytest.raises(
            ValidationRunResultError,
            match="64-character lowercase SHA-256",
        ):
            candidate.validate()


def test_evidence_manifest_artifact_ids_must_be_unique() -> None:
    with pytest.raises(ValidationRunResultError, match="artifact ids must be unique"):
        ValidationInputArtifactsV1(
            review_draft_id="review-1",
            review_draft_hash="a" * 64,
            citation_manifest_id="citation-1",
            citation_manifest_hash="b" * 64,
            evidence_manifest_ids=("evidence-1", "evidence-1"),
            evidence_manifest_hashes=("c" * 64, "c" * 64),
        ).validate()


def test_incomplete_current_payload_is_rejected() -> None:
    claim = ClaimValidationResultV1.from_validation_result(_legacy_result("supported"))
    payload = ValidationRunResultV1.create(
        job_id="job-1",
        execution_status="succeeded",
        claim_results=[claim],
    ).to_dict()
    for key in (
        "input_artifacts",
        "expected_claim_count",
        "validated_claim_count",
        "review_has_citations",
        "evidence_complete",
        "review_cleanliness",
        "repair_status",
        "recheck_status",
        "degradation_reasons",
    ):
        payload.pop(key)

    with pytest.raises(ValidationRunResultError, match="missing current fields"):
        ValidationRunResultV1.from_dict(payload)


def test_non_current_report_payload_is_rejected() -> None:
    non_current = {
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

    with pytest.raises(ValidationRunResultError, match="unexpected artifact_type"):
        ValidationRunResultV1.from_dict(non_current)
