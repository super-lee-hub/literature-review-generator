from __future__ import annotations

from validation.closure import zero_call_evidence_policy


def test_zero_call_evidence_policy_is_stage_specific() -> None:
    assert "summary_source_manifest" in zero_call_evidence_policy("analyze")
    assert "outline_call_plan" in zero_call_evidence_policy("outline")
    assert "review_replay_evidence" in zero_call_evidence_policy("review")
    assert "validation_disposition" in zero_call_evidence_policy("validate")
    assert "summary_source_manifest" not in zero_call_evidence_policy("outline")
