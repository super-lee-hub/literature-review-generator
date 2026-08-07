from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from runtime.lifecycle import BootstrappedRuntimeContext, publish_running_job_runtime
from runtime.reconcile import RuntimeReconciler
from runtime.runner import AgentRuntimeRunner
from services.artifact_registry import ArtifactRecord, ArtifactRegistry
from services.job_outcome import (
    JobOutcomeCompatibilityProjectionV1,
    JobOutcomeContractError,
    JobOutcomeV1,
    load_canonical_job_outcome,
    publish_job_outcome_compatibility_projection,
    validate_job_outcome_compatibility_projection,
)
from services.job_workspace import JobWorkspace, atomic_write_json, publish_json_artifact
from services.queue_service import LocalPublicationContext, PersistentQueueService, QueueJobSpec


POLICY = {
    "validation_required": False,
    "require_clean_validation": False,
    "allow_unvalidated_when_validation_optional": True,
}


class _ProjectionFailingContext(LocalPublicationContext):
    def write_compatibility_json(self, target_path: str | Path, payload: Any) -> str:
        raise OSError("injected projection write failure")


def _outcome(
    job_id: str,
    *,
    revision: int = 1,
    disposition: str = "clean",
    canonical_ready: bool = True,
) -> JobOutcomeV1:
    return JobOutcomeV1.create(
        job_id=job_id,
        attempt_number=1,
        job_status="completed",
        job_disposition=disposition,  # type: ignore[arg-type]
        canonical_ready=canonical_ready,
        requires_attention=not canonical_ready,
        readiness_policy_snapshot=POLICY,
        outcome_revision=revision,
    )


def _publish_canonical(
    workspace: JobWorkspace,
    registry: ArtifactRegistry,
    outcome: JobOutcomeV1,
    *,
    publication_context: Any | None = None,
) -> ArtifactRecord:
    context = publication_context or LocalPublicationContext()
    return publish_json_artifact(
        context,
        registry,
        workspace.artifact_path("job_outcome_v1.json"),
        outcome.to_dict(),
        artifact_role="job_outcome",
        artifact_type="job_outcome",
        artifact_version="v1",
        producer="tests.test_job_outcome_compatibility_projection",
        artifact_id="job_outcome",
        metadata={
            "job_status": outcome.job_status,
            "job_disposition": outcome.job_disposition,
            "canonical_ready": outcome.canonical_ready,
            "requires_attention": outcome.requires_attention,
            "outcome_revision": outcome.outcome_revision,
        },
    )


def _publish_projection(
    workspace: JobWorkspace,
    registry: ArtifactRegistry,
    record: ArtifactRecord,
    outcome: JobOutcomeV1,
    *,
    publication_context: Any | None = None,
) -> JobOutcomeCompatibilityProjectionV1:
    result = publish_job_outcome_compatibility_projection(
        path=workspace.artifact_path("job_outcome_v1.json"),
        registry=registry,
        canonical_record=record,
        outcome=outcome,
        producer="tests.test_job_outcome_compatibility_projection",
        publication_context=publication_context,
    )
    assert result.written, result.warning
    assert result.projection is not None
    return result.projection


def test_canonical_outcome_survives_projection_write_failure(tmp_path: Path) -> None:
    workspace = JobWorkspace.create(str(tmp_path), "demo", job_id="projection-failure")
    publication_context = _ProjectionFailingContext()
    registry = publication_context.registry(workspace.paths.registry_path, workspace.job_id)
    context = BootstrappedRuntimeContext(
        project_name=workspace.project_name,
        output_base_dir=str(tmp_path),
        pointer_path=workspace.latest_pointer_path(),
        workspace=workspace,
        registry=registry,
        settings=None,  # type: ignore[arg-type]
        summary_path=workspace.artifact_path("summaries.json"),
        progress_path=workspace.artifact_path("progress.json"),
        checkpoint_path=workspace.checkpoint_path("checkpoint.json"),
        fingerprint_bundle={"job_fingerprint": "projection-failure"},
        resume_report=SimpleNamespace(state="not_resumable"),
        resume_report_path=workspace.artifact_path("resume_state_report.json"),
        source_inventory={},
        source_inventory_path=workspace.artifact_path("source_inventory.json"),
        source_canonical_ready=True,
        source_degradation_reasons=(),
        readiness_policy_snapshot=POLICY,
        required_stages=(),
        job_outcome_path=workspace.artifact_path("job_outcome_v1.json"),
        publication_context=publication_context,
    )

    with pytest.warns(RuntimeWarning, match="injected projection write failure"):
        published = publish_running_job_runtime(context, claim_latest_pointer=True)

    canonical, record = load_canonical_job_outcome(registry)
    assert canonical == published
    assert Path(record.path).is_file()
    assert Path(record.path).resolve() != Path(
        workspace.artifact_path("job_outcome_v1.json")
    ).resolve()
    assert not Path(workspace.artifact_path("job_outcome_v1.json")).exists()


def test_old_hash_projection_is_repaired_from_registry_head(tmp_path: Path) -> None:
    workspace = JobWorkspace.create(str(tmp_path), "demo", job_id="old-hash")
    registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    first = _outcome(workspace.job_id)
    first_record = _publish_canonical(workspace, registry, first)
    first_projection = _publish_projection(workspace, registry, first_record, first)

    current = _outcome(
        workspace.job_id,
        revision=2,
        disposition="findings",
        canonical_ready=False,
    )
    current_record = _publish_canonical(workspace, registry, current)
    with pytest.raises(JobOutcomeContractError, match="canonical_job_outcome_artifact_hash"):
        validate_job_outcome_compatibility_projection(
            workspace.artifact_path("job_outcome_v1.json"),
            registry,
        )

    status = AgentRuntimeRunner.status(workspace.root_dir)
    assert status.job_disposition == "findings"
    assert status.job_outcome_path == current_record.path
    result = AgentRuntimeRunner.reconcile(workspace.root_dir)
    assert result.outcome_repaired is True
    assert "job_outcome_compatibility_projection" in result.repaired_artifact_ids
    repaired = validate_job_outcome_compatibility_projection(
        workspace.artifact_path("job_outcome_v1.json"),
        registry,
    )
    assert repaired.canonical_job_outcome_artifact_hash == current_record.content_hash
    assert repaired.projection_generation > first_projection.projection_generation


def test_stale_queue_worker_cannot_overwrite_projection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [datetime(2026, 8, 7, 0, 0, tzinfo=timezone.utc)]
    monkeypatch.setattr(
        PersistentQueueService,
        "_now_datetime",
        classmethod(lambda cls: clock[0]),
    )
    queue = PersistentQueueService(tmp_path / "queue.json")
    job_id = "stale-projection"
    queue.add_job(QueueJobSpec(job_id=job_id, job_type="review", project_name="demo"))
    stale_lease = queue.claim_job(job_id, worker_id="stale", lease_seconds=30)
    assert stale_lease is not None
    stale_context = queue.publication_context(stale_lease)
    workspace = JobWorkspace.create(str(tmp_path), "demo", job_id=job_id)
    registry = stale_context.registry(workspace.paths.registry_path, job_id)
    outcome = _outcome(job_id)
    record = _publish_canonical(
        workspace,
        registry,
        outcome,
        publication_context=stale_context,
    )
    _publish_projection(
        workspace,
        registry,
        record,
        outcome,
        publication_context=stale_context,
    )
    projection_path = Path(workspace.artifact_path("job_outcome_v1.json"))
    before = projection_path.read_bytes()

    clock[0] += timedelta(seconds=31)
    assert queue.recover_expired_leases() == [job_id]
    assert queue.claim_job(job_id, worker_id="current", lease_seconds=30) is not None
    result = publish_job_outcome_compatibility_projection(
        path=projection_path,
        registry=registry,
        canonical_record=record,
        outcome=outcome,
        producer="tests.stale-worker",
        publication_context=stale_context,
    )

    assert result.written is False
    assert "lease" in result.warning
    assert projection_path.read_bytes() == before


def test_tampered_projection_does_not_change_decision_and_reconcile_repairs_it(
    tmp_path: Path,
) -> None:
    workspace = JobWorkspace.create(str(tmp_path), "demo", job_id="tampered-projection")
    registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    outcome = _outcome(workspace.job_id)
    record = _publish_canonical(workspace, registry, outcome)
    projection = _publish_projection(workspace, registry, record, outcome)
    projection_path = Path(workspace.artifact_path("job_outcome_v1.json"))
    tampered = projection.to_dict()
    tampered["canonical_job_outcome_artifact_hash"] = "0" * 64
    tampered["projection_generation"] = 19
    atomic_write_json(str(projection_path), tampered)

    status = AgentRuntimeRunner.status(workspace.root_dir)
    assert status.job_status == "completed"
    assert status.job_disposition == "clean"
    canonical, canonical_record = load_canonical_job_outcome(registry)
    assert canonical == outcome
    assert canonical_record.content_hash == record.content_hash

    result = AgentRuntimeRunner.reconcile(workspace.root_dir)
    assert result.outcome_repaired is True
    repaired = validate_job_outcome_compatibility_projection(projection_path, registry)
    assert repaired.canonical_job_outcome_artifact_hash == record.content_hash
    assert repaired.projection_generation == 20


def test_reconcile_rebuilds_missing_projection(tmp_path: Path) -> None:
    workspace = JobWorkspace.create(str(tmp_path), "demo", job_id="missing-projection")
    registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    outcome = _outcome(workspace.job_id)
    record = _publish_canonical(workspace, registry, outcome)
    projection_path = Path(workspace.artifact_path("job_outcome_v1.json"))
    assert not projection_path.exists()

    result = AgentRuntimeRunner.reconcile(workspace.root_dir)

    assert result.outcome_repaired is True
    projection = validate_job_outcome_compatibility_projection(projection_path, registry)
    assert projection.canonical_job_outcome_artifact_id == record.artifact_id
    assert projection.canonical_job_outcome_artifact_hash == record.content_hash


def test_reconcile_records_projection_write_failure_without_changing_canonical_outcome(
    tmp_path: Path,
) -> None:
    workspace = JobWorkspace.create(str(tmp_path), "demo", job_id="reconcile-write-failure")
    registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    outcome = _outcome(workspace.job_id)
    record = _publish_canonical(workspace, registry, outcome)
    projection_path = Path(workspace.artifact_path("job_outcome_v1.json"))
    assert not projection_path.exists()
    setattr(registry, "publication_context", _ProjectionFailingContext())

    result = RuntimeReconciler(workspace, registry).reconcile()

    assert result.outcome_repaired is False
    assert "job_outcome_compatibility_projection" not in result.repaired_artifact_ids
    issue = next(
        issue
        for issue in result.issues
        if issue.code == "job_outcome_projection_write_failed"
    )
    assert issue.artifact_id == "job_outcome_compatibility_projection"
    assert "injected projection write failure" in issue.message
    canonical, canonical_record = load_canonical_job_outcome(registry)
    assert canonical == outcome
    assert canonical_record.content_hash == record.content_hash
    assert not projection_path.exists()


def test_canonical_status_reader_never_trusts_fixed_path_projection(tmp_path: Path) -> None:
    workspace = JobWorkspace.create(str(tmp_path), "demo", job_id="registry-reader")
    registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    outcome = _outcome(
        workspace.job_id,
        disposition="findings",
        canonical_ready=False,
    )
    record = _publish_canonical(workspace, registry, outcome)
    projection = _publish_projection(workspace, registry, record, outcome)
    forged = projection.to_dict()
    forged["job_id"] = "forged-job"
    forged["canonical_job_outcome_artifact_hash"] = "f" * 64
    atomic_write_json(workspace.artifact_path("job_outcome_v1.json"), forged)

    status = AgentRuntimeRunner.status(workspace.root_dir)

    assert status.job_id == workspace.job_id
    assert status.job_disposition == "findings"
    assert status.job_outcome_path == record.path
    with pytest.raises(JobOutcomeContractError, match="Registry head"):
        validate_job_outcome_compatibility_projection(
            workspace.artifact_path("job_outcome_v1.json"),
            registry,
        )
