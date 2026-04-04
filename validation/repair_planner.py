"""Repair planner for Week 4.

Converts validation findings into mapping-first PatchProposal objects.
Implements priority ordering: citation_mapping_error fixes come first.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence

from validation.repair_models import (
    ApplyGuardResult,
    DependencyHashBundle,
    PatchGranularity,
    PatchProposal,
    PatchTargetSignature,
    RepairPlan,
    RepairPolicy,
    RepairReport,
    RepairRootCause,
)
from validation.review_validator import CitationValidationResult, ReviewValidationReport, RootCause


def _compute_hash(data: Any) -> str:
    """Compute a stable hash for dependency tracking."""
    content = json.dumps(data, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]


def _build_dependency_bundle(
    paper_artifact: Dict[str, Any],
    summary_data: Dict[str, Any],
    visual_manifest: Optional[Dict[str, Any]] = None,
) -> DependencyHashBundle:
    """Build dependency hash bundle from artifacts."""
    selected_visual_refs = paper_artifact.get("stage1_inputs", {}).get("selected_visual_refs", [])
    
    return DependencyHashBundle(
        summary_hash=_compute_hash(summary_data),
        paper_artifact_hash=_compute_hash(paper_artifact),
        visual_manifest_hash=_compute_hash(visual_manifest or {}),
        selected_visual_refs_hash=_compute_hash(selected_visual_refs),
    )


def _map_validation_root_cause(root_causes: List[RootCause]) -> Optional[RepairRootCause]:
    """Map validation RootCause to RepairRootCause."""
    cause_mapping = {
        RootCause.CITATION_MAPPING_ERROR: RepairRootCause.CITATION_MAPPING_ERROR,
        RootCause.VISUAL_UNDERSTANDING_GAP: RepairRootCause.VISUAL_UNDERSTANDING_GAP,
        RootCause.SUMMARY_DRIFT: RepairRootCause.SUMMARY_DRIFT,
        RootCause.REVIEW_DRIFT: RepairRootCause.REVIEW_DRIFT,
        RootCause.INSUFFICIENT_CONTEXT: RepairRootCause.INSUFFICIENT_CONTEXT,
    }
    
    # Return the first matching root cause, prioritizing citation_mapping_error
    priority_order = [
        RootCause.CITATION_MAPPING_ERROR,
        RootCause.VISUAL_UNDERSTANDING_GAP,
        RootCause.SUMMARY_DRIFT,
        RootCause.REVIEW_DRIFT,
        RootCause.INSUFFICIENT_CONTEXT,
    ]
    
    for priority_cause in priority_order:
        if priority_cause in root_causes:
            return cause_mapping.get(priority_cause)
    
    return None


def _find_block_for_citation(
    review_draft: Dict[str, Any],
    citation: CitationValidationResult,
) -> Optional[Dict[str, Any]]:
    """Find the block in review_draft that contains the citation."""
    sections = review_draft.get("content", {}).get("sections", [])
    
    for section in sections:
        for block in section.get("blocks", []):
            # Match by block_id if available in citation details
            if citation.details.get("block_id") == block.get("block_id"):
                return block
            # Fallback: check if citation text appears in block text
            block_text = block.get("text", "")
            if citation.citation_id in block_text or citation.details.get("cited_text", "") in block_text:
                return block
    
    return None


def _create_patch_proposal(
    citation_result: CitationValidationResult,
    review_draft: Dict[str, Any],
    paper_artifacts: Sequence[Dict[str, Any]],
    job_id: str,
) -> Optional[PatchProposal]:
    """Create a PatchProposal from a validation result.
    
    Maps root cause to appropriate fix strategy:
    - citation_mapping_error -> manifest/mapping fix + rerender
    - visual_understanding_gap -> summary recheck with visual bundle first
    """
    root_cause = _map_validation_root_cause(citation_result.root_causes)
    if not root_cause:
        return None
    
    # Find target block
    block = _find_block_for_citation(review_draft, citation_result)
    if not block:
        return None
    
    # Find paper artifact
    paper_artifact = None
    for pa in paper_artifacts:
        if pa.get("paper_identity", {}).get("canonical_paper_key") == citation_result.paper_id:
            paper_artifact = pa
            break
    
    if not paper_artifact:
        return None
    
    # Build dependency bundle
    summary_data = paper_artifact.get("analysis", {}).get("ai_summary", {})
    dependency_bundle = _build_dependency_bundle(paper_artifact, summary_data)
    
    # Determine fix strategy based on root cause
    if root_cause == RepairRootCause.CITATION_MAPPING_ERROR:
        fix_strategy = "manifest_fix_rerender"
        granularity = PatchGranularity.SPAN
    elif root_cause == RepairRootCause.SUMMARY_DRIFT:
        fix_strategy = "targeted_summary_recheck"
        granularity = PatchGranularity.BLOCK
    elif root_cause == RepairRootCause.REVIEW_DRIFT:
        fix_strategy = "block_span_patch"
        granularity = PatchGranularity.SPAN
    elif root_cause == RepairRootCause.VISUAL_UNDERSTANDING_GAP:
        fix_strategy = "summary_recheck_visual_bundle"
        granularity = PatchGranularity.SPAN
    else:
        fix_strategy = "summary_recheck"
        granularity = PatchGranularity.BLOCK
    
    # Create target signature
    block_text = block.get("text", "")
    anchor_text = block_text[:80] if len(block_text) <= 80 else block_text[:80] + "..."
    anchor_hash = hashlib.sha256(block_text.encode("utf-8")).hexdigest()[:8]
    
    target = PatchTargetSignature(
        block_id=block.get("block_id", ""),
        anchor_text=anchor_text,
        anchor_hash=anchor_hash,
    )
    
    # Proposed text depends on fix strategy
    if root_cause == RepairRootCause.CITATION_MAPPING_ERROR:
        # For mapping errors, propose removing the problematic citation
        proposed_text = f"[CITATION_MAPPING_ERROR: {citation_result.citation_id} - needs manual review]"
    elif root_cause == RepairRootCause.REVIEW_DRIFT:
        # For review drift, mark for targeted fix
        proposed_text = block_text  # Keep original, mark for targeted fix
    else:
        # For other errors, mark for recheck
        proposed_text = block_text  # Keep original, mark for recheck
    
    # 构建增强的 metadata，包含 repair hint
    metadata = {
        "paper_id": citation_result.paper_id,
        "validation_conclusion": citation_result.conclusion.value,
        "evidence_candidates_count": len(citation_result.evidence_candidates),
        "claim_text": citation_result.claim_text,
        "claim_context": citation_result.claim_context,
        "reasoning_summary": citation_result.reasoning_summary,
        "repair_hint": citation_result.repair_hint,
    }
    
    return PatchProposal(
        proposal_id=str(uuid.uuid4()),
        citation_id=citation_result.citation_id,
        root_cause=root_cause,
        granularity=granularity,
        target=target,
        original_text=block_text,
        proposed_text=proposed_text,
        confidence=0.7 if root_cause == RepairRootCause.CITATION_MAPPING_ERROR else 0.5,
        fix_strategy=fix_strategy,
        dependency_bundle=dependency_bundle,
        metadata=metadata,
    )


class RepairPlanner:
    """Plans repairs based on validation findings.
    
    Converts validation findings into mapping-first PatchProposal objects.
    Orders proposals by priority: citation_mapping_error comes first.
    """
    
    def __init__(
        self,
        validation_report: ReviewValidationReport,
        review_draft: Dict[str, Any],
        citation_manifest: Dict[str, Any],
        paper_artifacts: Sequence[Dict[str, Any]],
        job_id: str,
    ):
        self.validation_report = validation_report
        self.review_draft = review_draft
        self.citation_manifest = citation_manifest
        self.paper_artifacts = paper_artifacts
        self.job_id = job_id
    
    def create_plan(self, policy: RepairPolicy = RepairPolicy.REPORT_FIRST) -> RepairPlan:
        """Create a repair plan from validation findings."""
        proposals: List[PatchProposal] = []
        
        # Only create proposals for non-supported citations
        for citation_result in self.validation_report.citation_results:
            if citation_result.conclusion.value in ["SUPPORTED"]:
                continue
            
            proposal = _create_patch_proposal(
                citation_result,
                self.review_draft,
                self.paper_artifacts,
                self.job_id,
            )
            if proposal:
                proposals.append(proposal)
        
        # Sort proposals: citation_mapping_error first, then by confidence
        proposals.sort(key=lambda p: (
            0 if p.root_cause == RepairRootCause.CITATION_MAPPING_ERROR else 1,
            -p.confidence,
        ))
        
        return RepairPlan(
            plan_id=str(uuid.uuid4()),
            created_at=datetime.now().isoformat(),
            created_from_job_id=self.job_id,
            validation_report_id=self.validation_report.report_id,
            proposals=proposals,
            policy=policy,
        )
    
    def create_report(self, plan: RepairPlan, apply_result_id: Optional[str] = None) -> RepairReport:
        """Create a repair report artifact."""
        # Group proposals by root cause
        by_cause: Dict[str, int] = {}
        for proposal in plan.proposals:
            cause = proposal.root_cause.value
            by_cause[cause] = by_cause.get(cause, 0) + 1
        
        summary = {
            "total_proposals": len(plan.proposals),
            "by_root_cause": by_cause,
            "mapping_first_count": len(plan.get_mapping_first_proposals()),
            "visual_gap_count": len(plan.get_visual_gap_proposals()),
            "policy": plan.policy.value,
        }
        
        return RepairReport(
            report_id=str(uuid.uuid4()),
            created_at=datetime.now().isoformat(),
            created_from_job_id=self.job_id,
            plan_id=plan.plan_id,
            apply_result_id=apply_result_id,
            summary=summary,
            proposals_detail=[p.to_dict() for p in plan.proposals],
        )


def run_repair_planning(
    validation_report: ReviewValidationReport,
    review_draft: Dict[str, Any],
    citation_manifest: Dict[str, Any],
    paper_artifacts: Sequence[Dict[str, Any]],
    job_id: str,
    policy: RepairPolicy = RepairPolicy.REPORT_FIRST,
) -> Dict[str, Any]:
    """Week 4 entry point for repair planning.
    
    Creates a repair plan from validation findings.
    Default policy is report-first, not silent auto-apply.
    """
    planner = RepairPlanner(
        validation_report=validation_report,
        review_draft=review_draft,
        citation_manifest=citation_manifest,
        paper_artifacts=paper_artifacts,
        job_id=job_id,
    )
    
    plan = planner.create_plan(policy=policy)
    report = planner.create_report(plan)
    
    return {
        "week4_repair_planning": True,
        "plan": plan.to_dict(),
        "report": report.to_dict(),
    }
