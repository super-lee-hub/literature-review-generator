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


def test_completion_aggregates_each_requested_provider_stage() -> None:
    stage_map = {
        "requested_stages": ["analyze", "outline", "review"],
        "current_set_required": False,
        "current_set_id": "",
        "provider_closures_by_stage": {
            "analyze": {"complete": False},
            "outline": {"complete": True},
            "review": {"complete": True},
        },
        "blocking_issues": ["provider_closure_incomplete:analyze"],
    }

    result = CanonicalCompletionEvaluator.evaluate(
        _ready_evidence(
            required_stages=["analyze", "outline", "review"],
            completed_stages=["analyze", "outline", "review"],
            current_stage_closure_map=stage_map,
        )
    )

    assert result.status == "blocked"
    assert result.canonical_ready is False
    assert "current_stage_closure:provider_closure_incomplete:analyze" in result.reasons


def test_completion_rejects_stage_map_that_omits_a_required_provider_stage() -> None:
    result = CanonicalCompletionEvaluator.evaluate(
        _ready_evidence(
            current_stage_closure_map={
                "requested_stages": ["analyze"],
                "current_set_required": False,
                "provider_closures_by_stage": {
                    "analyze": {"complete": True},
                },
                "blocking_issues": [],
            }
        )
    )

    assert result.status == "blocked"
    assert "current_stage_closure:stage_set_mismatch" in result.reasons
    assert "current_stage_closure:provider_stage_set_mismatch" in result.reasons
