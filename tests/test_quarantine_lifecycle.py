from __future__ import annotations

import json
from pathlib import Path

import pytest

from services.artifact_registry import ArtifactRegistry, file_sha256
from services.audit_record import AuditRecordV1
from services.job_workspace import JobWorkspace
from runtime.reconcile import RuntimeReconciler
from services.quarantine_lifecycle import (
    IdentityOverrideError,
    QuarantineReleaseError,
    override_ambiguous_identity,
    release_quarantined_artifact,
)


def _workspace_and_registry(tmp_path: Path) -> tuple[JobWorkspace, ArtifactRegistry]:
    workspace = JobWorkspace.create(str(tmp_path), "demo", job_id="job-123")
    return workspace, ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)


def _register_quarantined_identity(
    workspace: JobWorkspace,
    registry: ArtifactRegistry,
    *,
    verdict: str = "ambiguous",
    candidate_hash: str = "a" * 64,
) -> None:
    path = Path(workspace.artifact_path("source_identity/paper-a.json"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "identity_verdict": verdict,
                "artifact_status": "quarantined",
                "candidate_hash": candidate_hash,
            }
        ),
        encoding="utf-8",
    )
    registry.register_file(
        artifact_role="source_identity",
        artifact_type="source_identity",
        artifact_version="v1",
        path=path,
        producer="test",
        artifact_id="source-identity:paper-a",
        status="quarantined",
        metadata={
            "identity_verdict": verdict,
            "canonical_ready": False,
            "candidate_hash": candidate_hash,
        },
    )


def _read_audit(path: str) -> AuditRecordV1:
    return AuditRecordV1.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))


def test_ambiguous_identity_override_requires_exact_candidate_hash_and_writes_audit(
    tmp_path: Path,
) -> None:
    workspace, registry = _workspace_and_registry(tmp_path)
    candidate_hash = "a" * 64
    _register_quarantined_identity(workspace, registry, candidate_hash=candidate_hash)

    audit_path = override_ambiguous_identity(
        workspace=workspace,
        registry=registry,
        artifact_id="source-identity:paper-a",
        selected_candidate_hash=candidate_hash,
        actor="operator@example.test",
        reason="manually verified the exact PDF candidate",
        attempt_id="attempt-2",
    )

    audit = _read_audit(audit_path)
    assert audit.audit_type == "identity_override"
    assert audit.scope["selected_candidate_hash"] == candidate_hash
    assert audit.input_hashes["selected_candidate"] == candidate_hash
    registry.reload()
    target = registry.get("source-identity:paper-a")
    assert target is not None
    assert target.status == "ready"
    assert target.metadata["canonical_ready"] is True
    assert target.metadata["identity_override_audit_id"] == audit.audit_id
    audit_record = registry.get(audit.audit_id)
    assert audit_record is not None
    assert audit_record.status == "ready"
    assert audit_record.depends_on == []
    RuntimeReconciler(workspace, registry).validate_record(audit_record)


@pytest.mark.parametrize(
    ("verdict", "selected_hash", "message"),
    [
        ("mismatch", "a" * 64, "mismatch"),
        ("ambiguous", "b" * 64, "candidate hash"),
    ],
)
def test_identity_override_rejects_unsafe_release_without_mutation(
    tmp_path: Path,
    verdict: str,
    selected_hash: str,
    message: str,
) -> None:
    workspace, registry = _workspace_and_registry(tmp_path)
    _register_quarantined_identity(workspace, registry, verdict=verdict)

    with pytest.raises(IdentityOverrideError, match=message):
        override_ambiguous_identity(
            workspace=workspace,
            registry=registry,
            artifact_id="source-identity:paper-a",
            selected_candidate_hash=selected_hash,
            actor="operator@example.test",
            reason="manual review",
            attempt_id="attempt-2",
        )

    registry.reload()
    target = registry.get("source-identity:paper-a")
    assert target is not None
    assert target.status == "quarantined"
    assert not [record for record in registry.list_records() if record.artifact_type == "audit_record"]


def test_generic_quarantine_release_changes_registry_state_and_writes_audit(tmp_path: Path) -> None:
    workspace, registry = _workspace_and_registry(tmp_path)
    candidate = Path(workspace.artifact_path("quarantine/candidate.json"))
    candidate.parent.mkdir(parents=True, exist_ok=True)
    candidate.write_text('{"status":"reviewed"}', encoding="utf-8")
    registry.register_file(
        artifact_role="source_candidate",
        artifact_type="source_candidate",
        artifact_version="v1",
        path=candidate,
        producer="test",
        artifact_id="candidate:paper-a",
        status="quarantined",
    )

    audit_path = release_quarantined_artifact(
        workspace=workspace,
        registry=registry,
        artifact_id="candidate:paper-a",
        actor="operator@example.test",
        reason="manual quarantine review completed",
        attempt_id="attempt-3",
    )

    audit = _read_audit(audit_path)
    assert audit.audit_type == "artifact_quarantine_release"
    assert audit.input_hashes["quarantined_artifact"] == file_sha256(candidate)
    registry.reload()
    target = registry.get("candidate:paper-a")
    assert target is not None
    assert target.status == "ready"
    assert target.metadata["quarantine_release_audit_id"] == audit.audit_id
    audit_record = registry.get(audit.audit_id)
    assert audit_record is not None
    assert audit_record.status == "ready"
    assert audit_record.depends_on == []
    RuntimeReconciler(workspace, registry).validate_record(audit_record)


def test_release_persists_ready_audit_before_promoting_quarantined_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, registry = _workspace_and_registry(tmp_path)
    candidate = Path(workspace.artifact_path("quarantine/candidate.json"))
    candidate.parent.mkdir(parents=True, exist_ok=True)
    candidate.write_text('{"status":"reviewed"}', encoding="utf-8")
    registry.register_file(
        artifact_role="source_candidate",
        artifact_type="source_candidate",
        artifact_version="v1",
        path=candidate,
        producer="test",
        artifact_id="candidate:paper-a",
        status="quarantined",
    )

    original_update = registry.update_record

    def fail_target_promotion(artifact_id: str, **kwargs):
        if artifact_id == "candidate:paper-a":
            raise RuntimeError("injected target promotion failure")
        return original_update(artifact_id, **kwargs)

    monkeypatch.setattr(registry, "update_record", fail_target_promotion)

    with pytest.raises(RuntimeError, match="target promotion failure"):
        release_quarantined_artifact(
            workspace=workspace,
            registry=registry,
            artifact_id="candidate:paper-a",
            actor="operator@example.test",
            reason="manual quarantine review completed",
            attempt_id="attempt-3",
        )

    registry.reload()
    target = registry.get("candidate:paper-a")
    assert target is not None
    assert target.status == "quarantined"
    audit_records = [
        record for record in registry.list_records() if record.artifact_type == "audit_record"
    ]
    assert len(audit_records) == 1
    assert audit_records[0].status == "ready"
    assert audit_records[0].depends_on == []


def test_generic_release_cannot_bypass_identity_override_policy(tmp_path: Path) -> None:
    workspace, registry = _workspace_and_registry(tmp_path)
    _register_quarantined_identity(workspace, registry)

    with pytest.raises(QuarantineReleaseError, match="identity override"):
        release_quarantined_artifact(
            workspace=workspace,
            registry=registry,
            artifact_id="source-identity:paper-a",
            actor="operator@example.test",
            reason="attempted generic release",
            attempt_id="attempt-2",
        )
