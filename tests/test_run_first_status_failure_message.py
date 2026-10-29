from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from runtime.attempt_store import AttemptStore
from runtime.runner import AgentRuntimeRunner
from services.artifact_registry import ArtifactRegistry
from services.job_outcome import JobOutcomeV1
from services.job_workspace import JobWorkspace, publish_json_artifact
from services.queue_service import LocalPublicationContext


POLICY = {
    "validation_required": False,
    "require_clean_validation": False,
    "allow_unvalidated_when_validation_optional": True,
}
FAILURE_REASON = (
    "built-in stage executor failed for review: Output unit wou1_c19a45a5b4b5acfd739f6d16 "
    "must contain exactly one sentence"
)


def _failed_workspace(
    tmp_path: Path,
    *,
    history_attempts: int = 2,
    outcome_attempt: int = 2,
) -> tuple[JobWorkspace, ArtifactRegistry, AttemptStore]:
    job_id = "status-failure"
    workspace = JobWorkspace.create(str(tmp_path), "demo", job_id=job_id)
    registry = ArtifactRegistry(workspace.paths.registry_path, job_id)
    attempt_store = AttemptStore(workspace, registry)
    for attempt_number in range(1, history_attempts + 1):
        started = attempt_store.start(job_id=job_id, producer="status-test")
        terminal_status = "failed" if attempt_number == history_attempts else "succeeded"
        reason = FAILURE_REASON if terminal_status == "failed" else "prior attempt succeeded"
        attempt_store.finish(started.attempt, terminal_status, reason=reason)

    outcome = JobOutcomeV1.create(
        job_id=job_id,
        attempt_number=outcome_attempt,
        job_status="failed",
        job_disposition="needs_review",
        canonical_ready=False,
        requires_attention=True,
        readiness_policy_snapshot=POLICY,
        failed_stage="review",
    )
    publish_json_artifact(
        LocalPublicationContext(),
        registry,
        workspace.artifact_path("job_outcome_v1.json"),
        outcome.to_dict(),
        artifact_role="job_outcome",
        artifact_type="job_outcome",
        artifact_version="v1",
        producer="tests.test_run_first_status_failure_message",
        artifact_id="job_outcome",
        metadata={
            "job_status": outcome.job_status,
            "job_disposition": outcome.job_disposition,
            "canonical_ready": outcome.canonical_ready,
            "requires_attention": outcome.requires_attention,
            "outcome_revision": outcome.outcome_revision,
        },
    )
    return workspace, registry, attempt_store


def _workspace_file_state(workspace: JobWorkspace) -> dict[str, tuple[bytes, int]]:
    state: dict[str, tuple[bytes, int]] = {}
    root = Path(workspace.root_dir)
    for path in root.rglob("*"):
        if path.is_file():
            state[str(path.relative_to(root))] = (
                path.read_bytes(),
                path.stat().st_mtime_ns,
            )
    return state


def test_status_projects_reason_from_verified_current_failed_attempt_read_only(
    tmp_path: Path,
) -> None:
    workspace, _registry, _attempt_store = _failed_workspace(tmp_path)
    before = _workspace_file_state(workspace)

    status = AgentRuntimeRunner.status(workspace.root_dir)

    assert status.job_status == "failed"
    assert status.attempt_number == 2
    assert status.message == FAILURE_REASON
    assert _workspace_file_state(workspace) == before


def test_status_does_not_project_stale_attempt_reason(tmp_path: Path) -> None:
    workspace, _registry, _attempt_store = _failed_workspace(
        tmp_path,
        history_attempts=1,
        outcome_attempt=2,
    )

    status = AgentRuntimeRunner.status(workspace.root_dir)

    assert status.job_status == "failed"
    assert status.message == ""


def test_status_does_not_project_attempt_record_from_another_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, _registry, _attempt_store = _failed_workspace(tmp_path)
    original_get = ArtifactRegistry.get

    def foreign_attempt_record(self: ArtifactRegistry, artifact_id: str) -> Any:
        record = original_get(self, artifact_id)
        if record is not None and artifact_id.startswith("job-attempt:"):
            return replace(record, job_id="another-job")
        return record

    monkeypatch.setattr(ArtifactRegistry, "get", foreign_attempt_record)

    status = AgentRuntimeRunner.status(workspace.root_dir)

    assert status.job_status == "failed"
    assert status.message == ""


def test_status_does_not_project_tampered_attempt_snapshot(tmp_path: Path) -> None:
    workspace, _registry, attempt_store = _failed_workspace(tmp_path)
    head_path = attempt_store._snapshot_path(6)
    tampered = json.loads(head_path.read_text(encoding="utf-8"))
    tampered["terminal_reason"] = "forged failure message"
    head_path.write_text(json.dumps(tampered), encoding="utf-8")

    status = AgentRuntimeRunner.status(workspace.root_dir)

    assert status.job_status == "failed"
    assert status.message == ""
