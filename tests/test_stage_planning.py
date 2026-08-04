from __future__ import annotations

import pytest

from runtime.stage_planning import StagePlanError, build_stage_plan, stage_plan_from_metadata


def test_run_all_adds_validation_when_validation_is_enabled() -> None:
    plan = build_stage_plan(
        action="run_all",
        requested_stages=None,
        validation_enabled=True,
    )

    assert plan.requested_stages == ("analyze", "outline", "review", "validate")
    assert plan.required_stages == (
        "source_intake",
        "analyze",
        "outline",
        "review",
        "validate",
    )
    assert plan.current_artifact_set_required is True


def test_run_all_omits_optional_validation_when_validation_is_disabled() -> None:
    plan = build_stage_plan(
        action="run_all",
        requested_stages=None,
        validation_enabled=False,
    )

    assert plan.requested_stages == ("analyze", "outline", "review")
    assert plan.validation_required is False
    assert plan.validation_status == "not_requested"
    assert plan.current_artifact_set_required is True
    assert plan.allow_unvalidated_when_validation_optional is True


def test_derivation_coordinator_is_never_canonical_without_a_current_set() -> None:
    plan = build_stage_plan(
        action="derive_review_batch",
        requested_stages=None,
        validation_enabled=False,
    )

    assert plan.current_artifact_set_required is True


def test_required_validation_cannot_be_dropped_by_disabled_policy() -> None:
    with pytest.raises(StagePlanError, match="validation is required"):
        build_stage_plan(
            action="run_all",
            requested_stages=("analyze", "outline", "review", "validate"),
            validation_enabled=False,
            validation_required=True,
        )


def test_optional_explicit_validation_is_removed_when_disabled() -> None:
    plan = build_stage_plan(
        action="run_all",
        requested_stages=("analyze", "outline", "review", "validate"),
        validation_enabled=False,
        validation_required=False,
    )

    assert plan.requested_stages == ("analyze", "outline", "review")
    assert plan.validation_status == "not_requested"


def test_required_policy_without_validate_stage_is_rejected() -> None:
    with pytest.raises(StagePlanError, match="no validate stage"):
        build_stage_plan(
            action="run_all",
            requested_stages=("analyze", "outline", "review"),
            validation_enabled=True,
            validation_required=True,
        )


def test_stage_plan_round_trips_as_durable_metadata() -> None:
    plan = build_stage_plan(
        action="run_all",
        requested_stages=None,
        validation_enabled=True,
        validation_required=True,
        require_clean_validation=True,
    )

    restored = stage_plan_from_metadata({"stage_plan": plan.to_dict()})

    assert restored == plan
