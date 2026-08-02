"""Hash-bound adoption for the current Outline Intelligence artifacts."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from outline.v3_artifacts import AdoptedOutline
from services.artifact_registry import ArtifactDependencyRefV2, ArtifactRecord, ArtifactRegistry
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
    if record is None or record.status != "ready":
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


def _dependency(record: ArtifactRecord) -> ArtifactDependencyRefV2:
    return ArtifactDependencyRefV2(
        dependency_kind="local_job",
        job_id=record.job_id,
        artifact_id=record.artifact_id,
        artifact_type=record.artifact_type,
        path=record.path,
        content_hash=record.content_hash,
    )


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
    """Apply an explicit, immutable adoption transaction to Outline V3 output."""

    def __init__(self, workspace: JobWorkspace, registry: ArtifactRegistry) -> None:
        self.workspace = workspace
        self.registry = registry

    def adopt(self, *, source_artifact_id: str, adopted_by: str) -> AdoptionTransactionResult:
        source = _ready_record(self.registry, source_artifact_id)
        if source.artifact_type != "final_outline" or source.artifact_version != "v3":
            return AdoptionTransactionResult(
                status="blocked",
                job_id=self.workspace.job_id,
                source_artifact_id=source_artifact_id,
                reason="adoption requires a registered current Outline final_outline",
            )
        if not str(adopted_by or "").strip():
            return AdoptionTransactionResult(
                status="blocked",
                job_id=self.workspace.job_id,
                source_artifact_id=source_artifact_id,
                reason="adoption actor is required",
            )

        final_payload = _load_json(source).get("payload")
        if not isinstance(final_payload, Mapping):
            raise ValueError("final outline payload is missing")
        coverage = _ready_record(self.registry, "outline-v3:coverage_audit")
        stability = _ready_record(self.registry, "outline-v3:stability_audit")
        health = _ready_record(self.registry, "outline-v3:stage_health")
        coverage_payload = _load_json(coverage).get("payload")
        stability_payload = _load_json(stability).get("payload")
        health_payload = _load_json(health).get("payload")
        if not isinstance(coverage_payload, Mapping) or not bool(coverage_payload.get("passed")):
            return AdoptionTransactionResult(
                status="blocked",
                job_id=self.workspace.job_id,
                source_artifact_id=source_artifact_id,
                reason="coverage audit did not pass",
            )
        if not isinstance(stability_payload, Mapping) or stability_payload.get("status") != "stable":
            return AdoptionTransactionResult(
                status="blocked",
                job_id=self.workspace.job_id,
                source_artifact_id=source_artifact_id,
                reason="stability audit is not stable",
            )
        if not isinstance(health_payload, Mapping) or not bool(health_payload.get("adoption_eligible")):
            return AdoptionTransactionResult(
                status="blocked",
                job_id=self.workspace.job_id,
                source_artifact_id=source_artifact_id,
                reason="outline stage health is not adoption eligible",
            )

        adopted_id = "outline-v3:adoption"
        existing = self.registry.get(adopted_id)
        if existing is not None and existing.status == "ready":
            return AdoptionTransactionResult(
                status="already_adopted",
                job_id=self.workspace.job_id,
                source_artifact_id=source_artifact_id,
                adopted_artifact_id=existing.artifact_id,
                adopted_path=existing.path,
                reason="the current outline is already adopted",
            )

        dependency_records = (source, coverage, stability, health)
        dependency_hashes = {record.artifact_id: record.content_hash for record in dependency_records}
        adopted = AdoptedOutline(
            job_id=self.workspace.job_id,
            dependency_hashes=dependency_hashes,
            payload={
                "status": "adopted",
                "adopted_by": str(adopted_by).strip(),
                "final_outline_hash": source.content_hash,
                "coverage_audit_hash": coverage.content_hash,
                "stability_audit_hash": stability.content_hash,
                "stage_health_hash": health.content_hash,
                "final_outline": dict(final_payload),
            },
        )
        adopted_path = Path(self.workspace.artifact_path("outline_v3_adoption.json"))
        atomic_write_json(str(adopted_path), adopted.to_dict())
        adopted_record = self.registry.register_file(
            artifact_id=adopted_id,
            artifact_role="outline_v3_adoption",
            artifact_type=adopted.artifact_type,
            artifact_version=adopted.artifact_version,
            path=adopted_path,
            producer="outline.adoption_transaction.OutlineAdoptionTransaction",
            depends_on=[_dependency(record) for record in dependency_records],
            metadata={"adopted_by": str(adopted_by).strip()},
        )

        refs = [
            AuditArtifactRefV1(
                artifact_id=record.artifact_id,
                artifact_type=record.artifact_type,
                job_id=record.job_id,
                content_hash=record.content_hash,
            )
            for record in dependency_records
        ]
        audit = AuditRecordV1.create(
            audit_type="outline_manual_adoption",
            job_id=self.workspace.job_id,
            attempt_id=f"outline-adoption:{source.content_hash[:16]}",
            producer="outline.adoption_transaction.OutlineAdoptionTransaction",
            actor=str(adopted_by).strip(),
            reason="explicit adoption after current Outline gates",
            scope={"operation": "explicit_adoption", "source_artifact_id": source_artifact_id},
            target_artifacts=refs,
            input_artifact_refs=refs,
            output_artifact_refs=[
                AuditArtifactRefV1(
                    artifact_id=adopted_record.artifact_id,
                    artifact_type=adopted_record.artifact_type,
                    job_id=adopted_record.job_id,
                    content_hash=adopted_record.content_hash,
                )
            ],
            input_hashes=dependency_hashes,
            policy_snapshot={
                "require_stage_health": True,
                "require_coverage_audit": True,
                "require_stability_audit": True,
                "require_current_outline": True,
            },
            disposition="adopted",
            audit_id=f"outline-adoption:{source.content_hash[:24]}",
        )
        audit_path = Path(self.workspace.artifact_path(f"{audit.audit_id.replace(':', '-')}.json"))
        atomic_write_json(str(audit_path), audit.to_dict())
        audit_record = self.registry.register_file(
            artifact_id=audit.audit_id,
            artifact_role="audit_record",
            artifact_type="audit_record",
            artifact_version="v1",
            path=audit_path,
            producer="outline.adoption_transaction.OutlineAdoptionTransaction",
            depends_on=[*(_dependency(record) for record in dependency_records), _dependency(adopted_record)],
        )
        return AdoptionTransactionResult(
            status="succeeded",
            job_id=self.workspace.job_id,
            source_artifact_id=source_artifact_id,
            adopted_artifact_id=adopted_record.artifact_id,
            adopted_path=adopted_record.path,
            audit_artifact_id=audit_record.artifact_id,
            reason="current Outline output adopted",
            mutation_performed=True,
        )


__all__ = ["AdoptionTransactionResult", "OutlineAdoptionTransaction"]
