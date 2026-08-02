"""Repair pipeline data models for Week 4.

This module defines the core data structures for the repair pipeline:
- PatchProposal: A proposed change to fix a validation finding
- RepairPlan: A collection of patch proposals with metadata
- DependencyHashBundle: Dependency tracking for guard enforcement
- PatchTargetSignature: Target location identification
- ApplyGuardResult: Guard check results
- RepairApplyResult: Repair application results
- AppliedPatchRecord: Record of applied patches
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Set


class PatchGranularity(Enum):
    """Granularity levels for patches."""
    BLOCK = "block"  # Patch a single block
    SPAN = "span"    # Patch a span within a block
    # Whole-section or whole-chapter rewrites are NOT allowed


class RepairRootCause(Enum):
    """Root causes that can trigger repairs."""
    CITATION_MAPPING_ERROR = "citation_mapping_error"
    VISUAL_UNDERSTANDING_GAP = "visual_understanding_gap"
    SUMMARY_DRIFT = "summary_drift"
    REVIEW_DRIFT = "review_drift"
    INSUFFICIENT_CONTEXT = "insufficient_context"


class RepairPolicy(Enum):
    """Repair policy decisions."""
    REPORT_FIRST = "report_first"  # Default: report only, don't auto-apply
    AUTO_APPLY_SAFE = "auto_apply_safe"  # Only for safe, verified patches


@dataclass(frozen=True)
class DependencyHashBundle:
    """Bundle of dependency hashes for guard enforcement.
    
    All dependencies must be present and match for repair application.
    """
    summary_hash: str
    paper_artifact_hash: str
    visual_manifest_hash: str
    selected_visual_refs_hash: str
    # These fields are optional for compatibility with the original Week 4
    # bundle.  New repair transactions must carry them whenever the
    # corresponding artifact is in scope; an empty value means that the
    # artifact was not available to the planner, not that it was verified.
    review_draft_hash: str = ""
    citation_manifest_hash: str = ""
    outline_hash: str = ""
    
    def to_dict(self) -> Dict[str, str]:
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: Dict[str, str]) -> DependencyHashBundle:
        return cls(
            summary_hash=data.get("summary_hash", ""),
            paper_artifact_hash=data.get("paper_artifact_hash", ""),
            visual_manifest_hash=data.get("visual_manifest_hash", ""),
            selected_visual_refs_hash=data.get("selected_visual_refs_hash", ""),
            review_draft_hash=data.get("review_draft_hash", ""),
            citation_manifest_hash=data.get("citation_manifest_hash", ""),
            outline_hash=data.get("outline_hash", ""),
        )


@dataclass(frozen=True)
class PatchTargetSignature:
    """Signature identifying the target location for a patch.
    
    Uses anchor_text and anchor_hash for stable identification.
    """
    block_id: str
    anchor_text: str
    anchor_hash: str
    span_start: Optional[int] = None  # Character offset for span patches
    span_end: Optional[int] = None
    
    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PatchProposal:
    """A proposed patch to fix a validation finding.
    
    All patches are mapping-first and block/span granularity only.
    """
    proposal_id: str
    citation_id: str
    root_cause: RepairRootCause
    granularity: PatchGranularity
    target: PatchTargetSignature
    original_text: str
    proposed_text: str
    confidence: float
    fix_strategy: str  # e.g., "manifest_fix", "summary_recheck", "rerender"
    dependency_bundle: DependencyHashBundle
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "proposal_id": self.proposal_id,
            "citation_id": self.citation_id,
            "root_cause": self.root_cause.value,
            "granularity": self.granularity.value,
            "target": self.target.to_dict(),
            "original_text": self.original_text,
            "proposed_text": self.proposed_text,
            "confidence": self.confidence,
            "fix_strategy": self.fix_strategy,
            "dependency_bundle": self.dependency_bundle.to_dict(),
            "metadata": self.metadata,
        }


@dataclass(frozen=True)
class RepairPlan:
    """A plan containing multiple patch proposals.
    
    Ordered by priority: citation_mapping_error fixes come first.
    """
    plan_id: str
    created_at: str
    created_from_job_id: str
    validation_report_id: str
    proposals: List[PatchProposal]
    policy: RepairPolicy
    artifact_type: str = "repair_plan"
    artifact_version: str = "v1"
    dependency_hash_bundle: Optional[DependencyHashBundle] = None
    
    def to_dict(self) -> Dict[str, Any]:
        payload = {
            "artifact_type": self.artifact_type,
            "artifact_version": self.artifact_version,
            "plan_id": self.plan_id,
            "created_at": self.created_at,
            "created_from_job_id": self.created_from_job_id,
            "validation_report_id": self.validation_report_id,
            "policy": self.policy.value,
            "proposals": [p.to_dict() for p in self.proposals],
        }
        if self.dependency_hash_bundle is not None:
            payload["dependency_hash_bundle"] = self.dependency_hash_bundle.to_dict()
        return payload
    
    def get_mapping_first_proposals(self) -> List[PatchProposal]:
        """Get proposals for citation_mapping_error (highest priority)."""
        return [p for p in self.proposals if p.root_cause == RepairRootCause.CITATION_MAPPING_ERROR]
    
    def get_visual_gap_proposals(self) -> List[PatchProposal]:
        """Get proposals for visual_understanding_gap."""
        return [p for p in self.proposals if p.root_cause == RepairRootCause.VISUAL_UNDERSTANDING_GAP]


@dataclass(frozen=True)
class ApplyGuardResult:
    """Result of guard checks before applying a patch."""
    can_apply: bool
    version_guard_passed: bool
    anchor_hash_guard_passed: bool
    dependency_guard_passed: bool
    block_reasons: List[str] = field(default_factory=list)
    auto_safe_guard_passed: bool = True
    
    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RepairApplyResult:
    """Result of applying a repair plan."""
    success: bool
    plan_id: str
    applied_count: int
    rejected_count: int
    applied_proposals: List[str]  # proposal_ids
    rejected_proposals: List[Dict[str, Any]]  # {proposal_id, reason}
    artifact_type: str = "repair_apply_result"
    artifact_version: str = "v1"
    
    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class AppliedPatchRecord:
    """Record of an applied patch for audit trail."""
    record_id: str
    proposal_id: str
    plan_id: str
    applied_at: str
    applied_in_job_id: str
    original_text: str
    applied_text: str
    target_block_id: str
    anchor_hash_before: str
    anchor_hash_after: str
    artifact_type: str = "applied_patch_record"
    artifact_version: str = "v1"
    
    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RepairReport:
    """Comprehensive repair report artifact."""
    report_id: str
    created_at: str
    created_from_job_id: str
    plan_id: str
    apply_result_id: Optional[str]
    summary: Dict[str, Any]
    proposals_detail: List[Dict[str, Any]]
    artifact_type: str = "repair_report"
    artifact_version: str = "v1"
    
    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
