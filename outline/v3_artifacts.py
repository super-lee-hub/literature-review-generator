"""Canonical Outline v3 artifact envelopes and typed stage outputs.

Every outline output is a self-describing, hash-addressed artifact.  The
envelope deliberately keeps the semantic payload separate from persistence
metadata so the executor can serialize every node through one contract.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, ClassVar, Mapping

from outline.v3_models import compute_v3_hash


@dataclass(frozen=True)
class OutlineArtifact:
    """Hash-addressed output of one current outline node."""

    artifact_type: ClassVar[str] = "outline_artifact"
    artifact_version: ClassVar[str] = "v3"
    job_id: str = ""
    dependency_hashes: Mapping[str, str] = field(default_factory=dict)
    payload: Mapping[str, Any] = field(default_factory=dict)
    blocking_diagnostics: tuple[Mapping[str, Any], ...] = ()

    @property
    def content_hash(self) -> str:
        return compute_v3_hash(self.canonical_payload())

    @property
    def status(self) -> str:
        return "blocked" if self.blocking_diagnostics else "ready"

    def canonical_payload(self) -> dict[str, Any]:
        return {
            "artifact_type": self.artifact_type,
            "artifact_version": self.artifact_version,
            "job_id": self.job_id,
            "dependency_hashes": {
                str(key): str(value)
                for key, value in sorted(self.dependency_hashes.items())
            },
            "payload": self.payload,
            "blocking_diagnostics": [dict(item) for item in self.blocking_diagnostics],
        }

    def to_dict(self) -> dict[str, Any]:
        value = self.canonical_payload()
        value.update({"status": self.status, "content_hash": self.content_hash})
        return value

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "OutlineArtifact":
        return cls(
            job_id=str(value.get("job_id") or ""),
            dependency_hashes={
                str(key): str(item)
                for key, item in (value.get("dependency_hashes") or {}).items()
            } if isinstance(value.get("dependency_hashes"), Mapping) else {},
            payload=dict(value.get("payload") or {}) if isinstance(value.get("payload"), Mapping) else {},
            blocking_diagnostics=tuple(
                dict(item) for item in value.get("blocking_diagnostics") or () if isinstance(item, Mapping)
            ),
        )


def _typed_artifact(name: str, artifact_type: str) -> type[OutlineArtifact]:
    return type(
        name,
        (OutlineArtifact,),
        {"artifact_type": artifact_type, "__module__": __name__},
    )


RelationAdjudicationResult = _typed_artifact("RelationAdjudicationResult", "relation_adjudication_result")
ConfirmedGlobalRelationMap = _typed_artifact("ConfirmedGlobalRelationMap", "confirmed_global_relation_map")
OutlineCandidate = _typed_artifact("OutlineCandidate", "outline_candidate")
StructureCritique = _typed_artifact("StructureCritique", "structure_critique")
CoverageCritique = _typed_artifact("CoverageCritique", "coverage_critique")
EvidenceCritique = _typed_artifact("EvidenceCritique", "evidence_critique")
ArbitrationDecision = _typed_artifact("ArbitrationDecision", "arbitration_decision")
SelectedOutlineCandidate = _typed_artifact("SelectedOutlineCandidate", "selected_outline_candidate")
SectionEvidencePacket = _typed_artifact("SectionEvidencePacket", "section_evidence_packet")
SectionEvidencePacketSet = _typed_artifact("SectionEvidencePacketSet", "section_evidence_packet_set")
FinalOutline = _typed_artifact("FinalOutline", "final_outline")
CoverageAudit = _typed_artifact("CoverageAudit", "coverage_audit")
StabilityAudit = _typed_artifact("StabilityAudit", "stability_audit")
ProviderReceiptClosureArtifact = _typed_artifact("ProviderReceiptClosureArtifact", "provider_receipt_closure")
OutlineStageHealth = _typed_artifact("OutlineStageHealth", "outline_stage_health")
AdoptedOutline = _typed_artifact("AdoptedOutline", "adopted_outline")


__all__ = [
    "OutlineArtifact",
    "RelationAdjudicationResult",
    "ConfirmedGlobalRelationMap",
    "OutlineCandidate",
    "StructureCritique",
    "CoverageCritique",
    "EvidenceCritique",
    "ArbitrationDecision",
    "SelectedOutlineCandidate",
    "SectionEvidencePacket",
    "SectionEvidencePacketSet",
    "FinalOutline",
    "CoverageAudit",
    "StabilityAudit",
    "ProviderReceiptClosureArtifact",
    "OutlineStageHealth",
    "AdoptedOutline",
]
