from __future__ import annotations

from runtime.completion_evaluator import CanonicalCompletionEvaluator


def _ready_evidence(**overrides):
    evidence = {
        "job_id": "job-1",
        "job_status": "completed",
        "required_stages": ["analyze", "outline"],
        "completed_stages": ["analyze", "outline"],
        "artifact_registry_verified": True,
        "canonical_artifacts": {"job_outcome": True, "adopted_final_outline": True},
        "declared_canonical_ready": True,
        "validation_required": True,
        "require_clean_validation": True,
        "validation_status": "clean",
        "provider_receipts_complete": True,
    }
    evidence.update(overrides)
    return evidence


def test_canonical_completion_evaluator_is_the_only_complete_path() -> None:
    result = CanonicalCompletionEvaluator.evaluate(_ready_evidence())

    assert result.status == "complete"
    assert result.canonical_ready is True
    assert result.requires_attention is False
    assert result.evidence_hash


def test_canonical_completion_evaluator_blocks_unverified_or_degraded_evidence() -> None:
    result = CanonicalCompletionEvaluator.evaluate(
        _ready_evidence(
            artifact_registry_verified=False,
            provider_receipts_complete=False,
            degradation_reasons=["source_identity_ambiguous"],
        )
    )

    assert result.status == "blocked"
    assert result.canonical_ready is False
    assert "artifact_registry_unverified" in result.reasons
    assert "provider_receipts_incomplete" in result.reasons


def test_canonical_completion_evaluator_never_trusts_a_false_declared_ready_flag() -> None:
    result = CanonicalCompletionEvaluator.evaluate(_ready_evidence(declared_canonical_ready=False))

    assert result.status == "blocked"
    assert result.canonical_ready is False
    assert "declared_canonical_ready:false" in result.reasons
