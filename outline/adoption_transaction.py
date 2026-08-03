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


ADOPTION_POINTER_ARTIFACT_ID = "outline-v3:adoption:current"
ADOPTION_POINTER_ARTIFACT_TYPE = "outline_adoption_pointer"
ADOPTION_POINTER_ARTIFACT_VERSION = "v1"
ADOPTION_POINTER_ARTIFACT_ROLE = "outline_v3_adoption_current"


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
        [_dependency(record)]
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


def current_adoption_record(registry: ArtifactRegistry) -> ArtifactRecord | None:
    """Resolve the versioned adoption selected by the durable current pointer.

    The pointer is the only current-state selector.  The legacy fixed identity
    is accepted as a read-only compatibility fallback so existing workspaces
    can be migrated without silently losing their adoption evidence.
    """

    pointer = registry.get(ADOPTION_POINTER_ARTIFACT_ID)
    if pointer is not None and pointer.status == "ready":
        try:
            pointer_payload = _load_json(pointer)
        except ValueError:
            pointer_payload = {}
        target_id = str(pointer_payload.get("current_adoption_artifact_id") or "").strip()
        target_hash = str(pointer_payload.get("current_adoption_hash") or "").strip()
        target = registry.get(target_id) if target_id else None
        if (
            target is not None
            and target.status == "ready"
            and target.artifact_type == "adopted_outline"
            and target.artifact_version == "v3"
            and (not target_hash or target.content_hash == target_hash)
        ):
            return target

    legacy = registry.get("outline-v3:adoption")
    if legacy is not None and legacy.status == "ready":
        return legacy

    candidates = [
        record
        for record in registry.list_records()
        if record.status == "ready"
        and record.artifact_type == "adopted_outline"
        and record.artifact_version == "v3"
    ]
    return max(candidates, key=lambda item: (item.created_at, item.artifact_id), default=None)


def _write_current_pointer(
    workspace: JobWorkspace,
    registry: ArtifactRegistry,
    adopted_record: ArtifactRecord,
) -> ArtifactRecord:
    pointer_path = Path(workspace.artifact_path("outline_v3_adoption_current.json"))
    atomic_write_json(
        str(pointer_path),
        {
            "artifact_type": ADOPTION_POINTER_ARTIFACT_TYPE,
            "artifact_version": ADOPTION_POINTER_ARTIFACT_VERSION,
            "job_id": workspace.job_id,
            "role": "current",
            "current_adoption_artifact_id": adopted_record.artifact_id,
            "current_adoption_hash": adopted_record.content_hash,
            "adoption_identity": adopted_record.artifact_id,
            "updated_at": adopted_record.created_at,
        },
    )
    return registry.register_file(
        artifact_id=ADOPTION_POINTER_ARTIFACT_ID,
        artifact_role=ADOPTION_POINTER_ARTIFACT_ROLE,
        artifact_type=ADOPTION_POINTER_ARTIFACT_TYPE,
        artifact_version=ADOPTION_POINTER_ARTIFACT_VERSION,
        path=pointer_path,
        producer="outline.adoption_transaction.OutlineAdoptionTransaction",
        depends_on=[_dependency(adopted_record)],
        metadata={
            "role": "current",
            "current_adoption_artifact_id": adopted_record.artifact_id,
            "current_adoption_hash": adopted_record.content_hash,
        },
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
    actor: str = ""
    expected_hash: str = ""
    mutation_performed: bool = False
    read_only: bool = False
    current_pointer_artifact_id: str = ADOPTION_POINTER_ARTIFACT_ID

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "job_id": self.job_id,
            "source_artifact_id": self.source_artifact_id,
            "adopted_artifact_id": self.adopted_artifact_id,
            "adopted_path": self.adopted_path,
            "audit_artifact_id": self.audit_artifact_id,
            "reason": self.reason,
            "actor": self.actor,
            "expected_hash": self.expected_hash,
            "mutation_performed": self.mutation_performed,
            "read_only": self.read_only,
            "current_pointer_artifact_id": self.current_pointer_artifact_id,
        }


class OutlineAdoptionTransaction:
    """Apply an explicit, immutable adoption transaction to Outline V3 output."""

    def __init__(self, workspace: JobWorkspace, registry: ArtifactRegistry) -> None:
        self.workspace = workspace
        self.registry = registry

    def adopt(
        self,
        *,
        source_artifact_id: str,
        actor: str,
        reason: str,
        expected_hash: str,
    ) -> AdoptionTransactionResult:
        source = _ready_record(self.registry, source_artifact_id)
        if source.artifact_type != "final_outline" or source.artifact_version != "v3":
            return AdoptionTransactionResult(
                status="blocked",
                job_id=self.workspace.job_id,
                source_artifact_id=source_artifact_id,
                reason="adoption requires a registered current Outline final_outline",
            )
        actor = str(actor or "").strip()
        reason = str(reason or "").strip()
        expected_hash = str(expected_hash or "").strip()
        if not actor:
            return AdoptionTransactionResult(
                status="blocked",
                job_id=self.workspace.job_id,
                source_artifact_id=source_artifact_id,
                reason="adoption actor is required",
                actor=actor,
                expected_hash=expected_hash,
            )
        if not reason:
            return AdoptionTransactionResult(
                status="blocked",
                job_id=self.workspace.job_id,
                source_artifact_id=source_artifact_id,
                reason="adoption reason is required",
                actor=actor,
                expected_hash=expected_hash,
            )
        if not expected_hash:
            return AdoptionTransactionResult(
                status="blocked",
                job_id=self.workspace.job_id,
                source_artifact_id=source_artifact_id,
                reason="expected final-outline hash is required",
                actor=actor,
                expected_hash=expected_hash,
            )
        if expected_hash != source.content_hash:
            return AdoptionTransactionResult(
                status="blocked",
                job_id=self.workspace.job_id,
                source_artifact_id=source_artifact_id,
                reason="expected final-outline hash does not match the current Registry record",
                actor=actor,
                expected_hash=expected_hash,
            )

        final_payload = _load_json(source).get("payload")
        if not isinstance(final_payload, Mapping):
            raise ValueError("final outline payload is missing")
        coverage = _ready_record(self.registry, "outline-v3:coverage_audit")
        stability = _ready_record(self.registry, "outline-v3:stability_audit")
        receipt_closure = _ready_record(self.registry, "outline-v3:provider_receipt_closure")
        health = _ready_record(self.registry, "outline-v3:stage_health")
        coverage_payload = _load_json(coverage).get("payload")
        stability_payload = _load_json(stability).get("payload")
        receipt_closure_payload = _load_json(receipt_closure).get("payload")
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
                actor=actor,
                expected_hash=expected_hash,
            )
        if not isinstance(receipt_closure_payload, Mapping) or not bool(receipt_closure_payload.get("complete")):
            return AdoptionTransactionResult(
                status="blocked",
                job_id=self.workspace.job_id,
                source_artifact_id=source_artifact_id,
                reason="provider receipt closure is not complete",
                actor=actor,
                expected_hash=expected_hash,
            )
        expected_gate_hashes = {
            "coverage_audit_hash": coverage.content_hash,
            "stability_audit_hash": stability.content_hash,
            "provider_receipt_closure_hash": receipt_closure.content_hash,
        }
        mismatched_gates = [
            key for key, value in expected_gate_hashes.items()
            if str(health_payload.get(key) or "") != value
        ]
        if mismatched_gates:
            return AdoptionTransactionResult(
                status="blocked",
                job_id=self.workspace.job_id,
                source_artifact_id=source_artifact_id,
                reason="stage health does not bind the current gate hashes: " + ", ".join(mismatched_gates),
                actor=actor,
                expected_hash=expected_hash,
            )

        adopted_id = f"outline-v3:adoption:{source.content_hash[:16]}"
        existing = self.registry.get(adopted_id)
        if existing is not None and existing.status == "ready":
            _write_current_pointer(self.workspace, self.registry, existing)
            return AdoptionTransactionResult(
                status="already_adopted",
                job_id=self.workspace.job_id,
                source_artifact_id=source_artifact_id,
                adopted_artifact_id=existing.artifact_id,
                adopted_path=existing.path,
                reason="the current outline is already adopted",
                actor=actor,
                expected_hash=expected_hash,
                current_pointer_artifact_id=ADOPTION_POINTER_ARTIFACT_ID,
            )

        dependency_records = (source, coverage, stability, receipt_closure, health)
        dependency_hashes = {record.artifact_id: record.content_hash for record in dependency_records}
        adopted = AdoptedOutline(
            job_id=self.workspace.job_id,
            dependency_hashes=dependency_hashes,
            payload={
                "status": "adopted",
                "adoption_id": adopted_id,
                "adoption_identity": adopted_id,
                "current_pointer_artifact_id": ADOPTION_POINTER_ARTIFACT_ID,
                "current_pointer_role": "current",
                "actor": actor,
                "reason": reason,
                "expected_hash": expected_hash,
                "final_outline_hash": source.content_hash,
                "coverage_audit_hash": coverage.content_hash,
                "stability_audit_hash": stability.content_hash,
                "stage_health_hash": health.content_hash,
                "provider_receipt_closure_hash": receipt_closure.content_hash,
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
            metadata={"actor": actor, "reason": reason, "expected_hash": expected_hash},
        )
        _write_current_pointer(self.workspace, self.registry, adopted_record)

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
            actor=actor,
            reason=reason,
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
                "require_provider_receipt_closure": True,
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
            reason=reason,
            actor=actor,
            expected_hash=expected_hash,
            mutation_performed=True,
            current_pointer_artifact_id=ADOPTION_POINTER_ARTIFACT_ID,
        )


__all__ = [
    "ADOPTION_POINTER_ARTIFACT_ID",
    "ADOPTION_POINTER_ARTIFACT_ROLE",
    "ADOPTION_POINTER_ARTIFACT_TYPE",
    "ADOPTION_POINTER_ARTIFACT_VERSION",
    "AdoptionTransactionResult",
    "OutlineAdoptionTransaction",
    "current_adoption_record",
]
