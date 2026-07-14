from __future__ import annotations

import json

import pytest

from services.job_outcome import (
    AttemptTransitionError,
    AttemptV1,
    JobOutcomeContractError,
    JobOutcomeV1,
    append_attempt_snapshot,
    build_readiness_policy_hash,
    interrupt_stale_running_and_start_next,
)


POLICY = {
    "validation_requirement": "required",
    "validation_mode": "gate",
    "require_clean_validation": True,
}


def test_job_outcome_round_trip_and_legacy_success_projection() -> None:
    outcome = JobOutcomeV1.create(
        job_id="job-123",
        attempt_number=2,
        resumed_from_attempt=1,
        job_status="completed",
        job_disposition="clean",
        canonical_ready=True,
        requires_attention=False,
        readiness_policy_snapshot=POLICY,
        required_stages=["source_intake", "stage1", "validation"],
        completed_stages=["source_intake", "stage1", "validation"],
        created_at="2026-07-13T00:00:00Z",
        updated_at="2026-07-13T00:05:00Z",
        outcome_revision=4,
    )

    payload = json.loads(json.dumps(outcome.to_dict()))
    restored = JobOutcomeV1.from_dict(payload)

    assert restored == outcome
    assert payload["success"] is True
    assert restored.success is True
    assert restored.readiness_policy_hash == build_readiness_policy_hash(
        restored.readiness_policy_version,
        restored.readiness_policy_snapshot,
    )


def test_readiness_policy_hash_is_stable_and_content_sensitive() -> None:
    reordered = {
        "require_clean_validation": True,
        "validation_mode": "gate",
        "validation_requirement": "required",
    }
    assert build_readiness_policy_hash("readiness-policy-v1", POLICY) == build_readiness_policy_hash(
        "readiness-policy-v1", reordered
    )
    changed = {**POLICY, "require_clean_validation": False}
    assert build_readiness_policy_hash("readiness-policy-v1", POLICY) != build_readiness_policy_hash(
        "readiness-policy-v1", changed
    )


def test_job_outcome_rejects_hash_tampering_and_unsafe_ready_states() -> None:
    outcome = JobOutcomeV1.create(
        job_id="job-123",
        attempt_number=1,
        job_status="completed",
        job_disposition="clean",
        canonical_ready=True,
        requires_attention=False,
        readiness_policy_snapshot=POLICY,
    )
    tampered = outcome.to_dict()
    tampered["readiness_policy_snapshot"]["require_clean_validation"] = False
    with pytest.raises(JobOutcomeContractError, match="readiness_policy_hash"):
        JobOutcomeV1.from_dict(tampered)

    with pytest.raises(JobOutcomeContractError, match="canonical_ready requires"):
        JobOutcomeV1.create(
            job_id="job-123",
            attempt_number=1,
            job_status="running",
            job_disposition="unvalidated",
            canonical_ready=True,
            requires_attention=False,
            readiness_policy_snapshot=POLICY,
        )

    with pytest.raises(JobOutcomeContractError, match="all required stages"):
        JobOutcomeV1.create(
            job_id="job-123",
            attempt_number=1,
            job_status="completed",
            job_disposition="clean",
            canonical_ready=True,
            requires_attention=False,
            readiness_policy_snapshot=POLICY,
            required_stages=("source_intake", "analyze"),
            completed_stages=("source_intake",),
        )

    with pytest.raises(JobOutcomeContractError, match="needs_review"):
        JobOutcomeV1.create(
            job_id="job-123",
            attempt_number=1,
            job_status="completed",
            job_disposition="needs_review",
            canonical_ready=True,
            requires_attention=True,
            readiness_policy_snapshot=POLICY,
        )


def test_legacy_outcome_reader_fails_closed_even_when_success_is_true() -> None:
    restored = JobOutcomeV1.from_dict(
        {
            "job_id": "legacy-job",
            "status": "completed",
            "success": True,
            "created_at": "2026-07-13T00:00:00Z",
        }
    )

    assert restored.job_status == "completed"
    assert restored.job_disposition == "unvalidated"
    assert restored.compatibility_status == "legacy_unverified"
    assert restored.canonical_ready is False
    assert restored.success is False
    assert restored.requires_attention is True
    assert "legacy_unverified" in restored.degradation_reasons


def test_native_outcome_reader_rejects_invalid_resume_number_instead_of_correcting_it() -> None:
    payload = JobOutcomeV1.create(
        job_id="job-123",
        attempt_number=2,
        resumed_from_attempt=1,
        job_status="completed",
        job_disposition="clean",
        canonical_ready=True,
        requires_attention=False,
        readiness_policy_snapshot=POLICY,
    ).to_dict()
    payload["resumed_from_attempt"] = -1
    with pytest.raises(JobOutcomeContractError, match="earlier positive attempt"):
        JobOutcomeV1.from_dict(payload)


def test_attempt_transitions_are_immutable_and_append_only() -> None:
    pending = AttemptV1.new_pending(
        job_id="job-123",
        attempt_number=1,
        producer="tests",
        attempt_id="attempt-1",
        created_at="2026-07-13T00:00:00Z",
    )
    running = pending.transition("running", at="2026-07-13T00:01:00Z")
    succeeded = running.transition("succeeded", at="2026-07-13T00:02:00Z")

    assert pending.status == "pending"
    assert running.status == "running"
    assert succeeded.status == "succeeded"
    history = append_attempt_snapshot((), pending)
    history = append_attempt_snapshot(history, running)
    history = append_attempt_snapshot(history, succeeded)
    assert [item.status for item in history] == ["pending", "running", "succeeded"]

    with pytest.raises(AttemptTransitionError, match="illegal attempt transition"):
        succeeded.transition("running")


def test_stale_running_attempt_is_interrupted_before_new_attempt() -> None:
    running = AttemptV1.new_pending(
        job_id="job-123",
        attempt_number=1,
        producer="runner",
        attempt_id="attempt-1",
        created_at="2026-07-13T00:00:00Z",
    ).transition("running", at="2026-07-13T00:01:00Z")

    interrupted, resumed = interrupt_stale_running_and_start_next(
        running,
        producer="runner",
        at="2026-07-13T00:10:00Z",
        new_attempt_id="attempt-2",
    )

    assert running.status == "running"
    assert interrupted.status == "interrupted"
    assert resumed.status == "pending"
    assert resumed.attempt_number == 2
    assert resumed.resumed_from_attempt == 1

    history = append_attempt_snapshot((), AttemptV1.from_dict({
        "attempt_id": "attempt-1",
        "job_id": "job-123",
        "attempt_number": 1,
        "status": "pending",
        "producer": "runner",
        "created_at": "2026-07-13T00:00:00Z",
    }))
    history = append_attempt_snapshot(history, running)
    history = append_attempt_snapshot(history, interrupted)
    history = append_attempt_snapshot(history, resumed)
    assert [item.status for item in history] == ["pending", "running", "interrupted", "pending"]


def test_attempt_history_rejects_gaps_and_terminal_rewrites() -> None:
    pending = AttemptV1.new_pending(
        job_id="job-123", attempt_number=1, producer="tests", attempt_id="attempt-1"
    )
    running = pending.transition("running")
    failed = running.transition("failed")
    history = append_attempt_snapshot(append_attempt_snapshot((pending,), running), failed)

    invalid_next = AttemptV1.new_pending(
        job_id="job-123",
        attempt_number=3,
        resumed_from_attempt=1,
        producer="tests",
        attempt_id="attempt-3",
    )
    with pytest.raises(AttemptTransitionError, match="increase by one"):
        append_attempt_snapshot(history, invalid_next)

    changed_start = AttemptV1.from_dict({**failed.to_dict(), "started_at": "2026-01-01T00:00:00Z"})
    with pytest.raises(AttemptTransitionError, match="changed started_at"):
        append_attempt_snapshot((pending, running), changed_start)


def test_attempt_legacy_reader_projects_completed_to_succeeded() -> None:
    restored = AttemptV1.from_dict(
        {
            "attempt_id": "attempt-legacy",
            "job_id": "job-legacy",
            "status": "completed",
            "created_at": "2026-07-13T00:00:00Z",
            "updated_at": "2026-07-13T00:02:00Z",
        }
    )
    assert restored.status == "succeeded"
    assert restored.started_at == "2026-07-13T00:00:00Z"
    assert restored.finished_at == "2026-07-13T00:02:00Z"
