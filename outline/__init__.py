"""Current Outline Intelligence V3 public surface."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from outline.v3_executor import OutlineV3ExecutionResult, OutlineV3Executor

from outline.v3_artifacts import (
    AdoptedOutline,
    ArbitrationDecision,
    ConfirmedGlobalRelationMap,
    CoverageAudit,
    CoverageCritique,
    EvidenceCritique,
    FinalOutline,
    OutlineArtifact,
    OutlineCandidate,
    OutlineStageHealth,
    RelationAdjudicationResult,
    SectionEvidencePacket,
    SectionEvidencePacketSet,
    SelectedOutlineCandidate,
    StabilityAudit,
    StructureCritique,
)
from outline.v3_evidence import (
    build_coverage_contract,
    build_global_corpus_ledger,
    build_multi_view_matrix,
    build_outline_evidence_views,
    build_review_intent,
    merge_outline_evidence_shards,
    shard_outline_evidence_views,
)
from outline.v3_models import (
    CoverageContract,
    GlobalCorpusLedger,
    GlobalCorpusLedgerEntry,
    MultiViewMatrix,
    MultiViewMatrixRow,
    OutlineEvidenceView,
    OutlineEvidenceViews,
    ReviewIntent,
)
from outline.v3_relations import (
    build_global_relation_map,
    build_organizing_axes,
    build_outline_candidate_plans,
)

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
    "OutlineStageHealth",
    "AdoptedOutline",
    "OutlineV3ExecutionResult",
    "OutlineV3Executor",
    "OutlineEvidenceView",
    "OutlineEvidenceViews",
    "GlobalCorpusLedgerEntry",
    "GlobalCorpusLedger",
    "MultiViewMatrixRow",
    "MultiViewMatrix",
    "ReviewIntent",
    "CoverageContract",
    "build_outline_evidence_views",
    "build_global_corpus_ledger",
    "build_multi_view_matrix",
    "build_review_intent",
    "build_coverage_contract",
    "shard_outline_evidence_views",
    "merge_outline_evidence_shards",
    "build_global_relation_map",
    "build_organizing_axes",
    "build_outline_candidate_plans",
]


def __getattr__(name: str):
    """Load the executor lazily so DAG/model imports stay acyclic."""

    if name in {"OutlineV3ExecutionResult", "OutlineV3Executor"}:
        from outline.v3_executor import OutlineV3ExecutionResult, OutlineV3Executor

        return {
            "OutlineV3ExecutionResult": OutlineV3ExecutionResult,
            "OutlineV3Executor": OutlineV3Executor,
        }[name]
    raise AttributeError(name)
