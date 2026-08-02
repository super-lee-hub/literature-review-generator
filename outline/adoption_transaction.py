"""Registry-backed, explicit Outline adoption transaction.

The existing v2 adoption models already contain the required gates.  This
service makes those gates available to ``reviewctl adopt`` without invoking a
new outline generation run and without replacing a READY source artifact.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from outline.adoption import adopt_final_outline, write_adopted_outline
from outline.v2_models import CoverageAudit, FinalOutline, compute_content_hash
from outline.stage_health import OutlineStageHealthV1
from services.artifact_registry import ArtifactRecord, ArtifactRegistry, RegistryError
from services.audit_record import AuditArtifactRefV1, AuditRecordV1
from services.job_workspace import JobWorkspace, atomic_write_json


def _load_json(record: ArtifactRecord) -> dict[str, Any]:
    try:
        payload = json.loads(Path(record.path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read {record.artifact_id}: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise ValueError(f"artifact {record.artifact_id} must contain a JSON object")
    return dict(payload)


def _ready_record(registry: ArtifactRegistry, artifact_id: str) -> ArtifactRecord:
    record = registry.get(artifact_id)
    if record is None:
        raise ValueError(f"required adoption artifact is missing: {artifact_id}")
    if record.status != "ready":
        raise ValueError(f"required adoption artifact is not ready: {artifact_id}")
    registry.verify_ready_dependencies(
        [
            {
                "artifact_id": record.artifact_id,
                "artifact_type": record.artifact_type,
                "path": record.path,
                "content_hash": record.content_hash,
            }
        ]
    )
    return record


def _choose_record(
    registry: ArtifactRegistry,
    *,
    artifact_type: str,
    artifact_version: str,
    preferred_ids: tuple[str, ...],
) -> ArtifactRecord:
    records = [
        record
        for record in registry.list_records()
        if record.status == "ready"
        and record.artifact_type == artifact_type
        and record.artifact_version == artifact_version
    ]
    for artifact_id in preferred_ids:
        for record in records:
            if record.artifact_id == artifact_id:
                return _ready_record(registry, record.artifact_id)
    if not records:
        raise ValueError(f"required adoption artifact is missing: {artifact_type}/{artifact_version}")
    selected = max(records, key=lambda item: (item.created_at, item.artifact_id))
    return _ready_record(registry, selected.artifact_id)


def _dependency_payload(record: ArtifactRecord) -> dict[str, str]:
    return {
        "artifact_id": record.artifact_id,
        "artifact_type": record.artifact_type,
        "path": record.path,
        "content_hash": record.content_hash,
    }


@dataclass(frozen=True)
class AdoptionTransactionResult:
    status: str
    job_id: str
    source_artifact_id: str
    adopted_artifact_id: str = ""
    adopted_path: str = ""
    audit_artifact_id: str = ""
    reason: str = ""
    mutation_performed: bool = False
    read_only: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "job_id": self.job_id,
            "source_artifact_id": self.source_artifact_id,
            "adopted_artifact_id": self.adopted_artifact_id,
            "adopted_path": self.adopted_path,
            "audit_artifact_id": self.audit_artifact_id,
            "reason": self.reason,
            "mutation_performed": self.mutation_performed,
            "read_only": self.read_only,
        }


class OutlineAdoptionTransaction:
    """Apply an explicit, fully hash-bound adoption transaction."""

    def __init__(self, workspace: JobWorkspace, registry: ArtifactRegistry) -> None:
        self.workspace = workspace
        self.registry = registry

    def adopt(self, *, source_artifact_id: str, adopted_by: str) -> AdoptionTransactionResult:
        source = _ready_record(self.registry, source_artifact_id)
        if source.artifact_type != "final_outline" or source.artifact_version != "v2":
            return AdoptionTransactionResult(
                status="blocked",
                job_id=self.workspace.job_id,
                source_artifact_id=source_artifact_id,
                reason="reviewctl adoption currently requires a registered Outline v2 final_outline; v3 candidate plans remain non-canonical",
                mutation_performed=False,
            )

        final_outline = FinalOutline.from_dict(_load_json(source))
        if final_outline.created_from_job_id != self.workspace.job_id:
            raise ValueError("final outline belongs to a different job")
        audit_record = _choose_record(
            self.registry,
            artifact_type="outline_coverage_audit",
            artifact_version="v1",
            preferred_ids=("outline_coverage_audit",),
        )
        health_record = _choose_record(
            self.registry,
            artifact_type="outline_stage_health",
            artifact_version="v1",
            preferred_ids=("outline_stage_health",),
        )
        coverage_audit = CoverageAudit.from_dict(_load_json(audit_record))
        stage_health = OutlineStageHealthV1.from_dict(_load_json(health_record))
        adopted, message = adopt_final_outline(
            final_outline,
            coverage_audit,
            self.workspace.job_id,
            adopted_by,
            stage_health,
        )
        if adopted is None:
            return AdoptionTransactionResult(
                status="blocked",
                job_id=self.workspace.job_id,
                source_artifact_id=source_artifact_id,
                reason=message,
                mutation_performed=False,
            )

        adopted_artifact_id = "adopted_final_outline"
        existing = self.registry.get(adopted_artifact_id)
        if existing is not None:
            if existing.status != "ready":
                return AdoptionTransactionResult(
                    status="blocked",
                    job_id=self.workspace.job_id,
                    source_artifact_id=source_artifact_id,
                    reason="adopted_final_outline already exists but is not READY",
                    mutation_performed=False,
                )
            existing_payload = _load_json(existing)
            if (
                str(existing_payload.get("source_final_outline_hash") or "")
                == compute_content_hash(final_outline.to_dict())
            ):
                return AdoptionTransactionResult(
                    status="already_adopted",
                    job_id=self.workspace.job_id,
                    source_artifact_id=source_artifact_id,
                    adopted_artifact_id=existing.artifact_id,
                    adopted_path=existing.path,
                    reason="the same final outline is already adopted",
                    mutation_performed=False,
                )
            return AdoptionTransactionResult(
                status="blocked",
                job_id=self.workspace.job_id,
                source_artifact_id=source_artifact_id,
                reason="adopted_final_outline is immutable and already points to a different source",
                mutation_performed=False,
            )

        source_hash = compute_content_hash(final_outline.to_dict())
        adopted_path = Path(
            self.workspace.artifact_path(f"adopted_final_outline_{source_hash[:12]}.json")
        )
        write_adopted_outline(adopted, str(adopted_path))
        dependencies = [_dependency_payload(item) for item in (source, audit_record, health_record)]
        adopted_record = self.registry.register_file(
            artifact_id=adopted_artifact_id,
            artifact_role="adopted_final_outline",
            artifact_type="adopted_final_outline",
            artifact_version="v1",
            path=adopted_path,
            producer="outline.adoption_transaction.OutlineAdoptionTransaction",
            depends_on=dependencies,
            metadata={
                "adopted_by": adopted_by,
                "source_final_outline_hash": source_hash,
                "source_coverage_audit_hash": compute_content_hash(coverage_audit.to_dict()),
                "explicit_gate": True,
            },
        )

        refs = [
            AuditArtifactRefV1(
                artifact_id=item.artifact_id,
                artifact_type=item.artifact_type,
                job_id=item.job_id,
                content_hash=item.content_hash,
            )
            for item in (source, audit_record, health_record)
        ]
        output_ref = AuditArtifactRefV1(
            artifact_id=adopted_record.artifact_id,
            artifact_type=adopted_record.artifact_type,
            job_id=adopted_record.job_id,
            content_hash=adopted_record.content_hash,
        )
        audit = AuditRecordV1.create(
            audit_type="outline_manual_adoption",
            job_id=self.workspace.job_id,
            attempt_id=f"outline-adoption:{source_hash[:16]}",
            producer="outline.adoption_transaction.OutlineAdoptionTransaction",
            actor=adopted_by,
            reason="explicit reviewctl adoption after canonical Outline gates",
            scope={"operation": "explicit_adoption", "source_artifact_id": source_artifact_id},
            target_artifacts=refs,
            input_artifact_refs=refs,
            output_artifact_refs=[output_ref],
            input_hashes={
                "final_outline": source.content_hash,
                "coverage_audit": audit_record.content_hash,
                "stage_health": health_record.content_hash,
            },
            policy_snapshot={
                "require_stage_health": True,
                "require_coverage_audit": True,
                "require_canonical_completion": True,
                "v3_candidate_adoption": False,
            },
            disposition="adopted",
            audit_id=f"outline-adoption:{source_hash[:24]}",
        )
        audit_path = Path(
            self.workspace.artifact_path(f"{audit.audit_id.replace(':', '-')}.json")
        )
        atomic_write_json(str(audit_path), audit.to_dict())
        audit_registered = self.registry.register_file(
            artifact_id=audit.audit_id,
            artifact_role="audit_record",
            artifact_type="audit_record",
            artifact_version="v1",
            path=audit_path,
            producer="outline.adoption_transaction.OutlineAdoptionTransaction",
            depends_on=[*dependencies, _dependency_payload(adopted_record)],
        )
        return AdoptionTransactionResult(
            status="succeeded",
            job_id=self.workspace.job_id,
            source_artifact_id=source_artifact_id,
            adopted_artifact_id=adopted_record.artifact_id,
            adopted_path=adopted_record.path,
            audit_artifact_id=audit_registered.artifact_id,
            reason=message,
            mutation_performed=True,
        )


__all__ = ["AdoptionTransactionResult", "OutlineAdoptionTransaction"]
