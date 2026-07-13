from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from services.artifact_registry import (
    ArtifactDependencyRefV2,
    ArtifactRecord,
    ArtifactRegistry,
    RegistryCorruption,
)
from services.audit_record import AuditArtifactRefV1, AuditRecordV1
from services.job_outcome import JobOutcomeV1
from services.job_workspace import atomic_write_json, utc_now_iso


class DependencyLifecycleError(RuntimeError):
    """Base error for cross-workspace artifact lifecycle operations."""


class ArtifactDependencyBlocked(DependencyLifecycleError):
    """Raised when an artifact still has live cross-job dependents."""


@dataclass(frozen=True)
class ExternalDependent:
    registry_path: str
    child_job_id: str
    artifact: ArtifactRecord
    dependency: ArtifactDependencyRefV2


def _canonical_identity(ref: ArtifactDependencyRefV2) -> tuple[str, str, str]:
    return (ref.job_id, ref.artifact_id, ref.content_hash)


def discover_workspace_registries(output_root: str | Path) -> tuple[Path, ...]:
    root = Path(output_root).expanduser().resolve()
    return tuple(sorted(path for path in root.rglob("artifact_registry.json") if path.is_file()))


def _load_registry(path: Path) -> ArtifactRegistry:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RegistryCorruption(f"cannot inspect dependent registry {path}: {exc}") from exc
    job_id = str(payload.get("job_id") or "") if isinstance(payload, Mapping) else ""
    if not job_id:
        raise RegistryCorruption(f"dependent registry has no job_id: {path}")
    return ArtifactRegistry(str(path), job_id)


def find_external_dependents(
    output_root: str | Path,
    target: ArtifactDependencyRefV2,
    *,
    excluding_registry: str | Path | None = None,
) -> tuple[ExternalDependent, ...]:
    """Find live dependents using canonical identity, never path coincidence."""

    target_identity = _canonical_identity(target)
    excluded = Path(excluding_registry).resolve() if excluding_registry else None
    found: list[ExternalDependent] = []
    for registry_path in discover_workspace_registries(output_root):
        if excluded is not None and registry_path.resolve() == excluded:
            continue
        registry = _load_registry(registry_path)
        for record in registry.list_records():
            if record.status == "invalid":
                continue
            for dependency in record.depends_on:
                if dependency.dependency_kind != "external_job":
                    continue
                if _canonical_identity(dependency) == target_identity:
                    found.append(
                        ExternalDependent(
                            registry_path=str(registry_path),
                            child_job_id=record.job_id,
                            artifact=record,
                            dependency=dependency,
                        )
                    )
    return tuple(found)


def _audit_path(registry_path: Path, audit_id: str) -> Path:
    target = registry_path.parent / "artifacts" / "audits" / f"{audit_id}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    return target


def _invalidate_job_outcome(registry: ArtifactRegistry, registry_path: Path, audit_id: str) -> None:
    outcome_path = registry_path.parent / "artifacts" / "job_outcome_v1.json"
    if not outcome_path.is_file():
        return
    payload = json.loads(outcome_path.read_text(encoding="utf-8"))
    outcome = JobOutcomeV1.from_dict(payload)
    reasons = list(outcome.degradation_reasons)
    if "dependency_force_deleted" not in reasons:
        reasons.append("dependency_force_deleted")
    updated = JobOutcomeV1.create(
        job_id=outcome.job_id,
        attempt_number=outcome.attempt_number,
        resumed_from_attempt=outcome.resumed_from_attempt,
        job_status="completed" if outcome.job_status not in {"failed", "cancelled"} else outcome.job_status,
        job_disposition="needs_review",
        canonical_ready=False,
        requires_attention=True,
        readiness_policy_version=outcome.readiness_policy_version,
        readiness_policy_snapshot=dict(outcome.readiness_policy_snapshot),
        required_stages=outcome.required_stages,
        completed_stages=outcome.completed_stages,
        failed_stage=outcome.failed_stage,
        degradation_reasons=reasons,
        created_at=outcome.created_at,
        updated_at=utc_now_iso(),
        outcome_revision=outcome.outcome_revision + 1,
    )
    atomic_write_json(str(outcome_path), updated.to_dict())
    existing = next(
        (record for record in registry.list_records() if Path(record.path).resolve() == outcome_path.resolve()),
        None,
    )
    if existing is not None:
        registry.register_file(
            artifact_role=existing.artifact_role,
            artifact_type=existing.artifact_type,
            artifact_version=existing.artifact_version,
            path=outcome_path,
            producer="services.dependency_lifecycle.force_dependency_break",
            artifact_id=existing.artifact_id,
            depends_on=existing.depends_on,
            metadata={**existing.metadata, "force_delete_audit_id": audit_id},
        )


def guard_artifact_delete(
    *,
    output_root: str | Path,
    target: ArtifactDependencyRefV2,
    parent_registry_path: str | Path | None = None,
    force: bool = False,
    actor: str = "",
    reason: str = "",
    attempt_id: str = "dependency-force-delete",
) -> tuple[str, ...]:
    """Refuse a live dependency break or invalidate children with durable audits."""

    dependents = find_external_dependents(
        output_root,
        target,
        excluding_registry=parent_registry_path,
    )
    if not dependents:
        return ()
    if not force:
        identities = [f"{item.child_job_id}:{item.artifact.artifact_id}" for item in dependents]
        raise ArtifactDependencyBlocked(
            f"artifact has live external dependents: {', '.join(sorted(identities))}"
        )
    if not actor.strip() or not reason.strip():
        raise DependencyLifecycleError("force deletion requires actor and reason")

    audit_paths: list[str] = []
    by_registry: dict[str, list[ExternalDependent]] = {}
    for dependent in dependents:
        by_registry.setdefault(dependent.registry_path, []).append(dependent)
    for raw_registry_path, group in sorted(by_registry.items()):
        registry_path = Path(raw_registry_path)
        registry = _load_registry(registry_path)
        child_job_id = group[0].child_job_id
        audit = AuditRecordV1.create(
            audit_type="dependency_force_delete",
            job_id=child_job_id,
            attempt_id=attempt_id,
            producer="services.dependency_lifecycle.guard_artifact_delete",
            actor=actor,
            reason=reason,
            scope={
                "operation": "force_delete",
                "affected_child_artifact_ids": [item.artifact.artifact_id for item in group],
            },
            target_artifacts=[
                AuditArtifactRefV1(
                    artifact_id=target.artifact_id,
                    artifact_type=target.artifact_type,
                    job_id=target.job_id,
                    content_hash=target.content_hash,
                )
            ],
            input_artifact_refs=[
                AuditArtifactRefV1(
                    artifact_id=item.artifact.artifact_id,
                    artifact_type=item.artifact.artifact_type,
                    job_id=item.child_job_id,
                    content_hash=item.artifact.content_hash,
                )
                for item in group
            ],
            input_hashes={"parent_content_hash": target.content_hash},
            policy_snapshot={"policy_version": "dependency-force-delete-v1"},
            disposition="authorized_dependency_break",
        )
        path = _audit_path(registry_path, audit.audit_id)
        atomic_write_json(str(path), audit.to_dict())
        audit_record = registry.register_file(
            artifact_role="audit",
            artifact_type="audit_record",
            artifact_version="v1",
            path=path,
            producer="services.dependency_lifecycle.guard_artifact_delete",
            artifact_id=audit.audit_id,
            metadata={"audit_type": audit.audit_type, "record_hash": audit.record_hash},
        )
        for dependent in group:
            registry.update_record(
                dependent.artifact.artifact_id,
                status="invalid",
                metadata_updates={
                    "requires_attention": True,
                    "invalid_reason": "dependency_force_deleted",
                    "force_delete_audit_id": audit_record.artifact_id,
                    "broken_dependency": dependent.dependency.to_dict(),
                },
            )
        _invalidate_job_outcome(registry, registry_path, audit.audit_id)
        audit_paths.append(str(path))
    return tuple(audit_paths)


def guard_workspace_delete(
    *,
    workspace_path: str | Path,
    output_root: str | Path,
    force: bool = False,
    actor: str = "",
    reason: str = "",
    attempt_id: str = "dependency-force-delete",
) -> tuple[str, ...]:
    """Apply dependency protection to every non-invalid artifact in a workspace."""

    workspace = Path(workspace_path).expanduser().resolve()
    registry_path = workspace / "artifact_registry.json"
    if not registry_path.is_file():
        raise DependencyLifecycleError(
            f"workspace deletion requires an artifact registry: {registry_path}"
        )
    registry = _load_registry(registry_path)
    audit_paths: list[str] = []
    for record in registry.list_records():
        if record.status == "invalid" or not record.content_hash:
            continue
        audit_paths.extend(
            guard_artifact_delete(
                output_root=output_root,
                target=ArtifactDependencyRefV2(
                    dependency_kind="external_job",
                    job_id=record.job_id,
                    artifact_id=record.artifact_id,
                    artifact_type=record.artifact_type,
                    path=record.path,
                    content_hash=record.content_hash,
                ),
                parent_registry_path=registry_path,
                force=force,
                actor=actor,
                reason=reason,
                attempt_id=attempt_id,
            )
        )
    return tuple(audit_paths)


def materialize_external_dependency(
    *,
    registry: ArtifactRegistry,
    dependent_artifact_id: str,
    external: ArtifactDependencyRefV2,
    local_copy_path: str | Path,
    producer: str = "services.dependency_lifecycle.materialize_external_dependency",
) -> ArtifactRecord:
    """Replace one external dependency edge with a verified child-local copy."""

    if external.dependency_kind != "external_job":
        raise DependencyLifecycleError("materialization requires an external_job dependency")
    source = Path(external.path)
    if not source.is_file():
        raise DependencyLifecycleError(f"external dependency file not found: {source}")
    from services.artifact_registry import file_sha256

    if file_sha256(source) != external.content_hash:
        raise DependencyLifecycleError("external dependency hash changed before materialization")
    destination = Path(local_copy_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    local = registry.register_file(
        artifact_role="materialized_dependency",
        artifact_type=external.artifact_type,
        artifact_version="v1",
        path=destination,
        producer=producer,
        artifact_id=f"materialized:{external.job_id}:{external.artifact_id}",
        metadata={"materialized_from": external.to_dict()},
    )
    dependent = registry.get(dependent_artifact_id)
    if dependent is None:
        raise DependencyLifecycleError(f"dependent artifact not found: {dependent_artifact_id}")
    replacement = ArtifactDependencyRefV2(
        dependency_kind="local_job",
        job_id=registry.job_id,
        artifact_id=local.artifact_id,
        artifact_type=local.artifact_type,
        path=local.path,
        content_hash=local.content_hash,
    )
    updated_dependencies = [
        replacement if item == external else item
        for item in dependent.depends_on
    ]
    if all(item != replacement for item in updated_dependencies):
        raise DependencyLifecycleError("dependent artifact does not reference the requested external edge")
    return registry.update_record(
        dependent_artifact_id,
        depends_on=updated_dependencies,
        metadata_updates={
            "materialized_dependency": local.artifact_id,
            "retired_external_dependency": external.to_dict(),
        },
    )
