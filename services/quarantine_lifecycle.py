from __future__ import annotations

from pathlib import Path
import re

from services.artifact_registry import (
    ArtifactDependencyRefV2,
    ArtifactNotFound,
    ArtifactRecord,
    ArtifactRegistry,
)
from services.audit_record import AuditArtifactRefV1, AuditRecordV1
from services.job_workspace import JobWorkspace, atomic_write_json, utc_now_iso


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class QuarantineLifecycleError(RuntimeError):
    """Base error for audited quarantine state transitions."""


class IdentityOverrideError(QuarantineLifecycleError):
    """Raised when a requested identity override is not policy-safe."""


class QuarantineReleaseError(QuarantineLifecycleError):
    """Raised when a generic quarantine release is not allowed."""


def _require_operator(actor: str, reason: str, attempt_id: str) -> None:
    if not actor.strip() or not reason.strip() or not attempt_id.strip():
        raise QuarantineLifecycleError("audited quarantine actions require actor, reason, and attempt_id")


def _quarantined_target(registry: ArtifactRegistry, artifact_id: str) -> ArtifactRecord:
    registry.reload()
    target = registry.get(artifact_id)
    if target is None:
        raise ArtifactNotFound(f"artifact not found: {artifact_id}")
    if target.status != "quarantined":
        raise QuarantineReleaseError(f"artifact is not quarantined: {artifact_id}")
    if not target.content_hash:
        raise QuarantineReleaseError("quarantined artifact must have a content hash")
    return target


def _audit_ref(record: ArtifactRecord) -> AuditArtifactRefV1:
    return AuditArtifactRefV1(
        artifact_id=record.artifact_id,
        artifact_type=record.artifact_type,
        job_id=record.job_id,
        content_hash=record.content_hash,
    )


def _persist_audit(
    *,
    workspace: JobWorkspace,
    registry: ArtifactRegistry,
    target: ArtifactRecord,
    audit: AuditRecordV1,
) -> str:
    path = Path(workspace.artifact_path(f"audits/{audit.audit_id}.json"))
    atomic_write_json(str(path), audit.to_dict())
    registry.register_file(
        artifact_role="audit_record",
        artifact_type="audit_record",
        artifact_version="v1",
        path=path,
        producer=audit.producer,
        artifact_id=audit.audit_id,
        depends_on=[
            ArtifactDependencyRefV2(
                dependency_kind="local_job",
                job_id=target.job_id,
                artifact_id=target.artifact_id,
                artifact_type=target.artifact_type,
                path=target.path,
                content_hash=target.content_hash,
            )
        ],
        metadata={"audit_type": audit.audit_type, "record_hash": audit.record_hash},
    )
    return str(path)


def override_ambiguous_identity(
    *,
    workspace: JobWorkspace,
    registry: ArtifactRegistry,
    artifact_id: str,
    selected_candidate_hash: str,
    actor: str,
    reason: str,
    attempt_id: str,
) -> str:
    """Release one ambiguous identity only after exact-candidate human review."""

    _require_operator(actor, reason, attempt_id)
    target = _quarantined_target(registry, artifact_id)
    if target.artifact_type != "source_identity":
        raise IdentityOverrideError("identity override requires a source_identity artifact")
    verdict = str(target.metadata.get("identity_verdict") or "")
    if verdict == "mismatch":
        raise IdentityOverrideError("mismatch identities must be corrected in the source inventory and rerun")
    if verdict != "ambiguous":
        raise IdentityOverrideError(f"identity override requires an ambiguous verdict, found {verdict or 'missing'}")

    selected_hash = selected_candidate_hash.strip().lower().removeprefix("sha256:")
    registered_hash = str(target.metadata.get("candidate_hash") or "").strip().lower().removeprefix("sha256:")
    if not _SHA256_RE.fullmatch(selected_hash) or selected_hash != registered_hash:
        raise IdentityOverrideError("selected candidate hash does not match the quarantined identity candidate hash")

    target_ref = _audit_ref(target)
    audit = AuditRecordV1.create(
        audit_type="identity_override",
        job_id=workspace.job_id,
        attempt_id=attempt_id,
        producer="services.quarantine_lifecycle.override_ambiguous_identity",
        actor=actor,
        reason=reason,
        scope={
            "operation": "ambiguous_identity_override",
            "identity_verdict": verdict,
            "selected_candidate_hash": selected_hash,
        },
        target_artifacts=[target_ref],
        input_artifact_refs=[target_ref],
        output_artifact_refs=[target_ref],
        input_hashes={
            "quarantined_identity": target.content_hash,
            "selected_candidate": selected_hash,
        },
        policy_snapshot={
            "policy_version": "identity-override-v1",
            "mismatch_override_allowed": False,
            "exact_candidate_hash_required": True,
        },
        disposition="authorized_identity_override",
    )
    audit_path = _persist_audit(
        workspace=workspace,
        registry=registry,
        target=target,
        audit=audit,
    )
    registry.update_record(
        target.artifact_id,
        status="ready",
        metadata_updates={
            "canonical_ready": True,
            "effective_identity_verdict": "match",
            "selected_candidate_hash": selected_hash,
            "identity_override_audit_id": audit.audit_id,
            "identity_override_at": utc_now_iso(),
        },
    )
    return audit_path


def release_quarantined_artifact(
    *,
    workspace: JobWorkspace,
    registry: ArtifactRegistry,
    artifact_id: str,
    actor: str,
    reason: str,
    attempt_id: str,
) -> str:
    """Release a non-identity artifact with an immutable audit record."""

    _require_operator(actor, reason, attempt_id)
    target = _quarantined_target(registry, artifact_id)
    if target.artifact_type == "source_identity":
        raise QuarantineReleaseError("source identity release requires the identity override action")

    target_ref = _audit_ref(target)
    audit = AuditRecordV1.create(
        audit_type="artifact_quarantine_release",
        job_id=workspace.job_id,
        attempt_id=attempt_id,
        producer="services.quarantine_lifecycle.release_quarantined_artifact",
        actor=actor,
        reason=reason,
        scope={
            "operation": "artifact_quarantine_release",
            "artifact_role": target.artifact_role,
            "prior_status": target.status,
        },
        target_artifacts=[target_ref],
        input_artifact_refs=[target_ref],
        output_artifact_refs=[target_ref],
        input_hashes={"quarantined_artifact": target.content_hash},
        policy_snapshot={
            "policy_version": "artifact-quarantine-release-v1",
            "identity_artifact_requires_specialized_override": True,
        },
        disposition="authorized_quarantine_release",
    )
    audit_path = _persist_audit(
        workspace=workspace,
        registry=registry,
        target=target,
        audit=audit,
    )
    registry.update_record(
        target.artifact_id,
        status="ready",
        metadata_updates={
            "quarantine_release_audit_id": audit.audit_id,
            "quarantine_released_at": utc_now_iso(),
        },
    )
    return audit_path
