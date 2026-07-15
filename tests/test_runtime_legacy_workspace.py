from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

import runtime.runner as runtime_runner_module
from runtime.attempt_store import AttemptExecutionLease
from runtime.cli import main as runtime_cli
from runtime.reconcile import RuntimeReconciler
from runtime.runner import AgentRuntimeRunner, RuntimeRunnerError
from runtime.stage_terminal import TerminalStageRecordV1
from services.artifact_registry import ArtifactRegistry, file_sha256
from services.audit_record import AuditRecordV1
from services.job_outcome import AttemptV1, JobOutcomeV1
from services.job_workspace import JobWorkspace, atomic_write_json


LEGACY_UNVERIFIED = "legacy_unverified"


def _make_summary_only_legacy_workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "legacy-project__legacy-job"
    artifacts_dir = workspace / "artifacts"
    artifacts_dir.mkdir(parents=True)
    summary_path = artifacts_dir / "legacy-project_summaries.json"
    summary_path.write_text(
        json.dumps(
            [
                {
                    "status": "success",
                    "paper_info": {
                        "title": "Legacy Workspace Paper",
                        "authors": ["A. Researcher"],
                        "year": "2024",
                        "canonical_paper_key": "legacy-paper",
                    },
                    "ai_summary": {
                        "paper_metadata": {
                            "title": "Legacy Workspace Paper",
                            "authors": ["A. Researcher"],
                            "year": "2024",
                        },
                        "core_analysis": {
                            "summary": "A valid legacy summary used for compatibility testing.",
                            "methodology": "Archival analysis.",
                            "findings": "The legacy artifact remains readable.",
                            "conclusions": "Readability does not imply canonical readiness.",
                        },
                    },
                }
            ],
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return workspace


def _workspace_object(workspace: Path) -> JobWorkspace:
    project_name, job_id = workspace.name.rsplit("__", 1)
    return JobWorkspace(str(workspace.parent), project_name, job_id)


def _migration_paths(workspace: Path) -> tuple[Path, Path, Path, Path]:
    return (
        workspace / "artifacts" / "legacy-project_summaries.json",
        workspace / "artifacts" / "job_outcome_v1.json",
        workspace / "artifacts" / "audits" / "audit-legacy-workspace-migration-v1.json",
        workspace / "artifact_registry.json",
    )


def _durable_bytes(workspace: Path) -> dict[str, bytes]:
    return {
        path.relative_to(workspace).as_posix(): path.read_bytes()
        for path in workspace.rglob("*")
        if path.is_file() and path.name != ".execution.lock"
    }


def _outcome_payload(path: str | Path) -> dict[str, Any] | None:
    outcome_path = Path(path)
    if not outcome_path.is_file():
        return None
    payload = json.loads(outcome_path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def _status_compatibility_markers(status: object) -> set[str]:
    markers = {
        str(getattr(status, "compatibility_status", "")),
        str(getattr(status, "message", "")),
    }
    payload = _outcome_payload(str(getattr(status, "job_outcome_path", "")))
    if payload is not None:
        markers.add(str(payload.get("compatibility_status") or ""))
        markers.update(str(item) for item in payload.get("degradation_reasons") or ())
        policy = payload.get("readiness_policy_snapshot")
        if isinstance(policy, dict):
            markers.add(str(policy.get("compatibility_mode") or ""))
    return markers


def test_status_and_reconcile_project_legacy_workspace_without_migrating(tmp_path: Path) -> None:
    workspace = _make_summary_only_legacy_workspace(tmp_path)
    before = _durable_bytes(workspace)

    status = AgentRuntimeRunner.status(workspace)
    reconciled = AgentRuntimeRunner.reconcile(workspace)

    assert status.job_status == "completed"
    assert status.job_disposition == "unvalidated"
    assert status.canonical_ready is False
    assert status.requires_attention is True
    assert any(LEGACY_UNVERIFIED in marker for marker in _status_compatibility_markers(status))
    assert reconciled.outcome_repaired is False
    assert reconciled.repaired_artifact_ids == ()
    assert any(issue.code == "legacy_unverified_workspace" for issue in reconciled.issues)
    assert _durable_bytes(workspace) == before
    assert not (workspace / "artifact_registry.json").exists()
    assert not (workspace / "artifacts" / "job_outcome_v1.json").exists()


def test_partial_legacy_reconcile_is_read_only_before_all_repairs(tmp_path: Path) -> None:
    workspace = _make_summary_only_legacy_workspace(tmp_path)
    workspace_object = _workspace_object(workspace)

    outcome_path = Path(workspace_object.artifact_path("job_outcome_v1.json"))
    atomic_write_json(
        str(outcome_path),
        JobOutcomeV1.legacy_unverified(job_id=workspace_object.job_id).to_dict(),
    )

    attempt = AttemptV1.new_pending(
        job_id=workspace_object.job_id,
        attempt_number=1,
        producer="tests",
        attempt_id="legacy-attempt",
        created_at="2026-01-01T00:00:00Z",
    )
    attempt_payload = attempt.to_dict()
    attempt_payload["snapshot_sequence"] = 1
    atomic_write_json(
        workspace_object.artifact_path("job_attempts/snapshot-000001.json"),
        attempt_payload,
    )

    terminal = TerminalStageRecordV1.create(
        job_id=workspace_object.job_id,
        attempt_id=attempt.attempt_id,
        stage_name="analyze",
        status="failed",
        producer="tests",
        output_artifact_refs=(),
        started_at="2026-01-01T00:00:00Z",
        finished_at="2026-01-01T00:00:01Z",
        record_id="stage-terminal-legacy",
    )
    atomic_write_json(
        workspace_object.artifact_path(
            "runtime_stage_terminals/analyze/stage-terminal-legacy.json"
        ),
        terminal.to_dict(),
    )

    fingerprint_seed = {
        "config_hash": "1" * 64,
        "source_hash": "2" * 64,
        "request_hash": "3" * 64,
    }
    fingerprint_bundle = {
        **fingerprint_seed,
        "combined_hash": hashlib.sha256(
            json.dumps(
                fingerprint_seed,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest(),
    }
    atomic_write_json(
        workspace_object.artifact_path("resume_state_report.json"),
        {
            "job_id": workspace_object.job_id,
            "created_from_job_id": workspace_object.job_id,
            "project_name": workspace_object.project_name,
            "fingerprint_bundle": fingerprint_bundle,
            "state": "resume-ready",
        },
    )
    pointer_path = Path(workspace_object.latest_pointer_path())
    atomic_write_json(
        str(pointer_path),
        {
            "project_name": workspace_object.project_name,
            "job_id": workspace_object.job_id,
            "workspace_path": workspace_object.root_dir,
            "artifact_registry_path": workspace_object.paths.registry_path,
            "resume_state": "stale",
            "fingerprint_bundle": fingerprint_bundle,
            "status": "running",
            "updated_at": "2026-01-01T00:00:00Z",
        },
    )

    before = _durable_bytes(workspace)
    pointer_before = pointer_path.read_bytes()
    registry_path = Path(workspace_object.paths.registry_path)
    assert not registry_path.exists()

    direct = RuntimeReconciler(
        workspace_object,
        ArtifactRegistry(registry_path, workspace_object.job_id),
    ).reconcile()
    assert direct.repaired_artifact_ids == ()
    assert direct.outcome_repaired is False
    assert direct.pointer_repaired is False
    assert [issue.code for issue in direct.issues] == ["legacy_unverified_workspace"]
    assert _durable_bytes(workspace) == before
    assert pointer_path.read_bytes() == pointer_before
    assert not registry_path.exists()

    public = AgentRuntimeRunner.reconcile(workspace)
    assert public.repaired_artifact_ids == ()
    assert public.outcome_repaired is False
    assert public.pointer_repaired is False
    assert [issue.code for issue in public.issues] == ["legacy_unverified_workspace"]
    assert _durable_bytes(workspace) == before
    assert pointer_path.read_bytes() == pointer_before
    assert not registry_path.exists()


def test_corrupt_outcome_does_not_bypass_legacy_reconcile_read_only_guard(
    tmp_path: Path,
) -> None:
    workspace = _make_summary_only_legacy_workspace(tmp_path)
    workspace_object = _workspace_object(workspace)
    outcome_path = Path(workspace_object.artifact_path("job_outcome_v1.json"))
    outcome_path.write_text("{broken", encoding="utf-8")

    attempt = AttemptV1.new_pending(
        job_id=workspace_object.job_id,
        attempt_number=1,
        producer="tests",
        attempt_id="legacy-corrupt-outcome-attempt",
        created_at="2026-01-01T00:00:00Z",
    )
    attempt_payload = attempt.to_dict()
    attempt_payload["snapshot_sequence"] = 1
    atomic_write_json(
        workspace_object.artifact_path("job_attempts/snapshot-000001.json"),
        attempt_payload,
    )

    before = _durable_bytes(workspace)
    registry_path = Path(workspace_object.paths.registry_path)
    assert not registry_path.exists()

    reconciled = AgentRuntimeRunner.reconcile(workspace)

    assert reconciled.repaired_artifact_ids == ()
    assert reconciled.outcome_repaired is False
    assert reconciled.pointer_repaired is False
    assert [issue.code for issue in reconciled.issues] == ["legacy_unverified_workspace"]
    assert reconciled.issues[0].artifact_id == "invalid_job_outcome"
    assert _durable_bytes(workspace) == before
    assert not registry_path.exists()

def test_public_cli_migrate_legacy_writes_fail_closed_audited_state(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workspace = _make_summary_only_legacy_workspace(tmp_path)

    assert runtime_cli(
        [
            "migrate-legacy",
            str(workspace),
            "--actor",
            "test-operator",
            "--reason",
            "materialize a readable legacy compatibility head",
        ]
    ) == 0
    result_payload = json.loads(capsys.readouterr().out)

    summary_path, outcome_path, audit_path, registry_path = _migration_paths(workspace)
    outcome = JobOutcomeV1.from_dict(json.loads(outcome_path.read_text(encoding="utf-8")))
    registry = ArtifactRegistry(registry_path, "legacy-job")
    summary_record = registry.get("legacy_summary_file")
    outcome_record = registry.get("job_outcome")
    audit_record = registry.get("audit-legacy-workspace-migration-v1")
    assert summary_record is not None
    assert outcome_record is not None
    assert audit_record is not None

    audit = AuditRecordV1.from_dict(json.loads(audit_path.read_text(encoding="utf-8")))
    assert result_payload["migrated_artifact_ids"] == [
        "legacy_summary_file",
        "job_outcome",
        "audit-legacy-workspace-migration-v1",
    ]
    assert outcome.compatibility_status == LEGACY_UNVERIFIED
    assert outcome.job_status == "completed"
    assert outcome.job_disposition == "unvalidated"
    assert outcome.canonical_ready is False
    assert outcome.requires_attention is True
    assert outcome.readiness_policy_snapshot["compatibility_mode"] == LEGACY_UNVERIFIED
    assert "legacy_summary_without_runtime_contract" in outcome.degradation_reasons
    assert audit.audit_type == "legacy_reuse"
    assert audit.actor == "test-operator"
    assert audit.reason == "materialize a readable legacy compatibility head"
    assert audit.scope["operation"] == "legacy_workspace_migration"
    assert audit.scope["canonical_upgrade"] is False
    assert audit.input_hashes == {"legacy_summary_file": file_sha256(summary_path)}
    assert {(ref.artifact_id, ref.content_hash) for ref in audit.input_artifact_refs} == {
        ("legacy_summary_file", summary_record.content_hash)
    }
    assert {(ref.artifact_id, ref.content_hash) for ref in audit.output_artifact_refs} == {
        ("job_outcome", outcome_record.content_hash)
    }
    assert {ref.artifact_id for ref in audit_record.depends_on} == {
        "legacy_summary_file",
        "job_outcome",
    }
    RuntimeReconciler(_workspace_object(workspace), registry).validate_record(audit_record)


def test_migrate_legacy_is_byte_idempotent_for_same_actor_and_reason(tmp_path: Path) -> None:
    workspace = _make_summary_only_legacy_workspace(tmp_path)
    first = AgentRuntimeRunner.migrate_legacy(
        workspace,
        "test-operator",
        "materialize a readable legacy compatibility head",
    )
    before = _durable_bytes(workspace)

    second = AgentRuntimeRunner.migrate_legacy(
        workspace,
        "test-operator",
        "materialize a readable legacy compatibility head",
    )

    assert first.migrated is True
    assert second.migrated is False
    assert second.migrated_artifact_ids == ()
    assert _durable_bytes(workspace) == before


def test_migrate_legacy_rejects_changed_or_unsafe_audit_identity_without_mutation(
    tmp_path: Path,
) -> None:
    workspace = _make_summary_only_legacy_workspace(tmp_path)
    AgentRuntimeRunner.migrate_legacy(workspace, "test-operator", "approved reason")
    before = _durable_bytes(workspace)

    with pytest.raises(RuntimeRunnerError, match="differs from the requested actor or reason"):
        AgentRuntimeRunner.migrate_legacy(workspace, "another-operator", "another reason")
    assert _durable_bytes(workspace) == before

    fresh = _make_summary_only_legacy_workspace(tmp_path / "unsafe")
    fresh_before = _durable_bytes(fresh)
    with pytest.raises(RuntimeRunnerError, match="credential-like value"):
        AgentRuntimeRunner.migrate_legacy(
            fresh,
            "test-operator",
            "api_key=sk-0123456789abcdef",
        )
    assert _durable_bytes(fresh) == fresh_before
    assert not (fresh / "artifact_registry.json").exists()
    assert not (fresh / "artifacts" / "job_outcome_v1.json").exists()
    assert not (fresh / "artifacts" / "audits").exists()


def test_migrate_legacy_requires_actor_reason_and_an_available_execution_lease(
    tmp_path: Path,
) -> None:
    workspace = _make_summary_only_legacy_workspace(tmp_path)
    before = _durable_bytes(workspace)

    with pytest.raises(RuntimeRunnerError, match="requires actor and reason"):
        AgentRuntimeRunner.migrate_legacy(workspace, "", "reason")
    with pytest.raises(RuntimeRunnerError, match="requires actor and reason"):
        AgentRuntimeRunner.migrate_legacy(workspace, "actor", "")
    with pytest.raises(SystemExit):
        runtime_cli(["migrate-legacy", str(workspace)])
    assert _durable_bytes(workspace) == before

    lease = AttemptExecutionLease(_workspace_object(workspace))
    lease.acquire()
    try:
        with pytest.raises(RuntimeRunnerError, match="another runtime attempt is already active"):
            AgentRuntimeRunner.migrate_legacy(workspace, "actor", "reason")
    finally:
        lease.release()
    assert _durable_bytes(workspace) == before


def test_migrate_legacy_loads_registry_only_after_acquiring_execution_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _make_summary_only_legacy_workspace(tmp_path)
    events: list[str] = []
    original_acquire = AttemptExecutionLease.acquire
    original_registry = runtime_runner_module.ArtifactRegistry

    def tracked_acquire(self: AttemptExecutionLease) -> None:
        original_acquire(self)
        events.append("lease")

    def tracked_registry(*args: Any, **kwargs: Any) -> ArtifactRegistry:
        events.append("registry")
        assert events and events[0] == "lease"
        return original_registry(*args, **kwargs)

    monkeypatch.setattr(AttemptExecutionLease, "acquire", tracked_acquire)
    monkeypatch.setattr(runtime_runner_module, "ArtifactRegistry", tracked_registry)

    AgentRuntimeRunner.migrate_legacy(workspace, "actor", "reason")

    assert events[:2] == ["lease", "registry"]


def test_migrate_legacy_prevalidates_foreign_outcome_and_corrupt_audit_before_writes(
    tmp_path: Path,
) -> None:
    foreign = _make_summary_only_legacy_workspace(tmp_path / "foreign")
    _summary, foreign_outcome_path, _audit, foreign_registry_path = _migration_paths(foreign)
    atomic_write_json(
        str(foreign_outcome_path),
        JobOutcomeV1.legacy_unverified(job_id="another-job").to_dict(),
    )
    foreign_before = _durable_bytes(foreign)

    with pytest.raises(RuntimeRunnerError, match="belongs to another job"):
        AgentRuntimeRunner.migrate_legacy(foreign, "actor", "reason")
    assert _durable_bytes(foreign) == foreign_before
    assert not foreign_registry_path.exists()

    corrupt_audit = _make_summary_only_legacy_workspace(tmp_path / "corrupt-audit")
    _summary, outcome_path, audit_path, registry_path = _migration_paths(corrupt_audit)
    atomic_write_json(
        str(outcome_path),
        JobOutcomeV1.legacy_unverified(job_id="legacy-job").to_dict(),
    )
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    audit_path.write_text("{not-json", encoding="utf-8")
    audit_before = _durable_bytes(corrupt_audit)

    with pytest.raises(RuntimeRunnerError, match="audit is invalid"):
        AgentRuntimeRunner.migrate_legacy(corrupt_audit, "actor", "reason")
    assert _durable_bytes(corrupt_audit) == audit_before
    assert not registry_path.exists()


def test_migrate_legacy_rejects_registered_outcome_hash_drift_without_repair(
    tmp_path: Path,
) -> None:
    workspace = _make_summary_only_legacy_workspace(tmp_path)
    workspace_object = _workspace_object(workspace)
    registry = ArtifactRegistry(workspace_object.paths.registry_path, workspace_object.job_id)
    outcome_path = Path(workspace_object.artifact_path("job_outcome_v1.json"))
    outcome = JobOutcomeV1.legacy_unverified(job_id=workspace_object.job_id)
    atomic_write_json(str(outcome_path), outcome.to_dict())
    registry.register_file(
        artifact_role="job_outcome",
        artifact_type="job_outcome",
        artifact_version="v1",
        path=outcome_path,
        producer="tests",
        artifact_id="job_outcome",
    )
    drifted = outcome.to_dict()
    drifted["updated_at"] = "2099-01-01T00:00:00Z"
    atomic_write_json(str(outcome_path), drifted)
    before = _durable_bytes(workspace)

    with pytest.raises(RuntimeRunnerError, match="content hash mismatch"):
        AgentRuntimeRunner.migrate_legacy(workspace, "actor", "reason")

    assert _durable_bytes(workspace) == before

def test_migrate_legacy_rejects_native_and_non_summary_only_workspaces_without_mutation(
    tmp_path: Path,
) -> None:
    native = _make_summary_only_legacy_workspace(tmp_path / "native")
    native_workspace = _workspace_object(native)
    native_registry = ArtifactRegistry(native_workspace.paths.registry_path, native_workspace.job_id)
    native_outcome = JobOutcomeV1.create(
        job_id=native_workspace.job_id,
        attempt_number=1,
        job_status="completed",
        job_disposition="clean",
        canonical_ready=True,
        requires_attention=False,
        readiness_policy_snapshot={"validation_required": False},
    )
    native_outcome_path = Path(native_workspace.artifact_path("job_outcome_v1.json"))
    atomic_write_json(str(native_outcome_path), native_outcome.to_dict())
    native_registry.register_file(
        artifact_role="job_outcome",
        artifact_type="job_outcome",
        artifact_version="v1",
        path=native_outcome_path,
        producer="tests",
        artifact_id="job_outcome",
    )
    native_before = _durable_bytes(native)

    with pytest.raises(RuntimeRunnerError, match="native or non-fail-closed"):
        AgentRuntimeRunner.migrate_legacy(native, "actor", "reason")
    assert _durable_bytes(native) == native_before

    non_summary = _make_summary_only_legacy_workspace(tmp_path / "non-summary")
    non_summary_workspace = _workspace_object(non_summary)
    non_summary_registry = ArtifactRegistry(
        non_summary_workspace.paths.registry_path,
        non_summary_workspace.job_id,
    )
    extra = Path(non_summary_workspace.artifact_path("extra.json"))
    atomic_write_json(str(extra), {"extra": True})
    non_summary_registry.register_file(
        artifact_role="extra",
        artifact_type="runtime_job_spec",
        artifact_version="v1",
        path=extra,
        producer="tests",
        artifact_id="extra",
    )
    non_summary_before = _durable_bytes(non_summary)

    with pytest.raises(RuntimeRunnerError, match="summary-only workspace"):
        AgentRuntimeRunner.migrate_legacy(non_summary, "actor", "reason")
    assert _durable_bytes(non_summary) == non_summary_before
