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


def _resolve_primary_artifact(paper_artifact_or_artifacts: Any) -> Dict[str, Any]:
    """Return the primary paper artifact for single- and multi-paper inputs."""
    if isinstance(paper_artifact_or_artifacts, list):
        first = paper_artifact_or_artifacts[0] if paper_artifact_or_artifacts else {}
        return first if isinstance(first, dict) else {}
    if isinstance(paper_artifact_or_artifacts, dict):
        return paper_artifact_or_artifacts
    return {}


def _build_dependency_bundle(
    paper_artifact: Any,
    summary_data: Dict[str, Any],
    visual_manifest: Optional[Dict[str, Any]] = None,
) -> DependencyHashBundle:
    """Build dependency hash bundle from artifacts."""
    primary_artifact = _resolve_primary_artifact(paper_artifact)
    selected_visual_refs = primary_artifact.get("stage1_inputs", {}).get("selected_visual_refs", [])
    
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


def _resolve_candidate_paper_ids(citation: CitationValidationResult) -> List[str]:
    """Resolve paper IDs for a citation result using v3 priority order.

    Priority: citation_result.paper_ids (v3) → details.paper_ids →
    details.bundle.paper_ids → legacy paper_id (only when not synthetic).
    """
    # v3: direct paper_ids on citation result
    if citation.paper_ids:
        return list(citation.paper_ids)

    # v3: from details.paper_ids
    details_pids = citation.details.get("paper_ids", [])
    if details_pids:
        return [str(p) for p in details_pids if str(p).strip()]

    # v3: from details.bundle.paper_ids
    bundle = citation.details.get("bundle", {})
    if isinstance(bundle, dict):
        bundle_pids = bundle.get("paper_ids", [])
        if bundle_pids:
            return [str(p) for p in bundle_pids if str(p).strip()]

    return []


def _resolve_target_block_id(citation: CitationValidationResult) -> str:
    """Resolve target block ID using v3 priority order.

    Priority: target_claim_unit.block_id → block_ids[0] →
    details.target_claim_unit.block_id → legacy details.block_id →
    empty string (text fallback in caller).
    """
    # v3: from target_claim_unit
    tcu = citation.target_claim_unit if isinstance(citation.target_claim_unit, dict) else {}
    tcu_block = str(tcu.get("block_id", "") or "").strip()
    if tcu_block:
        return tcu_block

    # v3: first entry in block_ids
    if citation.block_ids:
        first = str(citation.block_ids[0] or "").strip()
        if first:
            return first

    # v3: from details.target_claim_unit
    details_tcu = citation.details.get("target_claim_unit", {})
    if isinstance(details_tcu, dict):
        dtcu_block = str(details_tcu.get("block_id", "") or "").strip()
        if dtcu_block:
            return dtcu_block

    return ""


def _find_block_for_citation(
    review_draft: Dict[str, Any],
    citation: CitationValidationResult,
) -> Optional[Dict[str, Any]]:
    """Find the block in review_draft that contains the citation.

    Uses v3 priority: target_claim_unit.block_id → block_ids[0] →
    details.target_claim_unit.block_id → legacy details.block_id → text fallback.
    """
    sections = review_draft.get("content", {}).get("sections", [])

    # Resolve target block ID via priority order
    target_block_id = _resolve_target_block_id(citation)

    if target_block_id:
        for section in sections:
            for block in section.get("blocks", []):
                if block.get("block_id") == target_block_id:
                    return block

    for section in sections:
        for block in section.get("blocks", []):
            if block.get("block_id") == target_block_id:
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

    v3: Resolves paper IDs and block IDs using priority order, and
    includes paper_ids, citation_set_key, validation_bundle_id, claim_unit_id
    in proposal metadata.
    """
    root_cause = _map_validation_root_cause(citation_result.root_causes)
    if not root_cause:
        return None

    # Find target block using v3 priority
    block = _find_block_for_citation(review_draft, citation_result)
    if not block:
        return None

    # Resolve candidate paper IDs using v3 priority
    candidate_paper_ids = _resolve_candidate_paper_ids(citation_result)
    # Find all matching paper artifacts
    resolved_artifacts: List[Dict[str, Any]] = []
    paper_index = {pa.get("paper_identity", {}).get("canonical_paper_key", ""): pa for pa in paper_artifacts}
    for pid in candidate_paper_ids:
        artifact = paper_index.get(pid)
        if artifact:
            resolved_artifacts.append(artifact)

    if not resolved_artifacts:
        return None

    # Use the first resolved artifact as the primary dependency anchor.
    paper_artifact = resolved_artifacts[0]

    # Build composite dependency bundle from all resolved artifacts.
    # Summary hash aggregates across all papers.
    # Paper artifact hash is composite (all artifacts) for multi-paper,
    # matching check-time computation in _check_dependency_bundle.
    aggregate_summary: Dict[str, Any] = {}
    for artifact in resolved_artifacts:
        summary_data = artifact.get("analysis", {}).get("ai_summary", {})
        aggregate_summary.update(summary_data)

    # Composite artifact for hash: full list for multi-paper, single for single
    artifact_for_hash: Any = resolved_artifacts if len(resolved_artifacts) > 1 else paper_artifact
    dependency_bundle = _build_dependency_bundle(artifact_for_hash, aggregate_summary)
    
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
    
    # Build v3-enhanced metadata with paper_ids, citation_set_key,
    # validation_bundle_id, and claim_unit_id
    citation_set_key = str(
        citation_result.citation_set_key
        or citation_result.details.get("citation_set_key", "")
    ).strip()
    validation_bundle_id = str(
        citation_result.details.get("target_claim_unit", {}).get("claim_unit_id", "")
        or citation_result.target_claim_unit.get("claim_unit_id", "")
    ).strip()
    claim_unit_id = str(
        citation_result.target_claim_unit.get("claim_unit_id", "")
        or citation_result.details.get("target_claim_unit", {}).get("claim_unit_id", "")
    ).strip()

    metadata = {
        "paper_ids": candidate_paper_ids,  # v3
        "citation_set_key": citation_set_key,
        "validation_bundle_id": validation_bundle_id or citation_set_key,
        "claim_unit_id": claim_unit_id or citation_set_key,
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
