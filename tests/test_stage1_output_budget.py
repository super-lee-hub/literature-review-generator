from __future__ import annotations

import pytest

from services.stage1_output_budget import (
    stage1_output_budget_sequence,
    stage1_output_budget_snapshot,
    stage1_request_timeout_seconds,
    stage1_semantic_retry_max_attempts,
)


def test_stage1_budget_sequence_is_stage_specific_and_bounded() -> None:
    settings = {
        "stage1_visual_scan_max_output_tokens": "16000",
        "stage1_synthesis_max_output_tokens": "32000",
        "stage1_length_retry_max_attempts": "2",
        "stage1_length_retry_ceiling_tokens": "65536",
    }

    assert stage1_output_budget_sequence("visual_scan", settings) == (16000, 32000, 65536)
    assert stage1_output_budget_sequence("synthesis", settings) == (32000, 64000, 65536)
    assert stage1_output_budget_snapshot("visual_scan", settings)["length_retry_count"] == 2


def test_stage1_budget_sequence_can_disable_length_escalation() -> None:
    settings = {
        "stage1_visual_scan_max_output_tokens": "12000",
        "stage1_length_retry_max_attempts": "0",
    }

    assert stage1_output_budget_sequence("visual_scan", settings) == (12000,)


def test_stage1_budget_sequence_rejects_ceiling_below_initial_budget() -> None:
    with pytest.raises(ValueError, match="cannot exceed"):
        stage1_output_budget_sequence(
            "synthesis",
            {
                "stage1_synthesis_max_output_tokens": "32000",
                "stage1_length_retry_ceiling_tokens": "16000",
            },
        )


def test_stage1_timeout_is_independent_from_output_budget() -> None:
    assert stage1_request_timeout_seconds({"stage1_request_timeout_seconds": "240"}) == 240


def test_stage1_semantic_retry_is_finite_and_bounded() -> None:
    assert stage1_semantic_retry_max_attempts({}) == 1
    assert stage1_semantic_retry_max_attempts({"stage1_semantic_retry_max_attempts": "9"}) == 3
    assert stage1_semantic_retry_max_attempts({"stage1_semantic_retry_max_attempts": "0"}) == 0
