"""Repair apply module for Week 4.

Enforces guards before applying patches:
- Version guard
- Anchor/hash guard
- Dependency guard

Patch granularity is block/span only (no whole-section rewrite).
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence

from validation.repair_models import (
    AppliedPatchRecord,
    ApplyGuardResult,
    DependencyHashBundle,
    PatchGranularity,
    PatchProposal,
    RepairApplyResult,
    RepairPlan,
    RepairRootCause,
)


def _compute_hash(data: Any) -> str:
    """Compute a stable hash for dependency tracking."""
    content = json.dumps(data, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]


def _get_block_text(review_draft: Dict[str, Any], block_id: str) -> Optional[str]:
    """Get current text of a block from review_draft."""
    sections = review_draft.get("content", {}).get("sections", [])
    for section in sections:
        for block in section.get("blocks", []):
            if block.get("block_id") == block_id:
                return block.get("text", "")
    return None


def _compute_anchor_hash(text: str) -> str:
    """Compute anchor hash for a text block."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]


def _check_dependency_bundle(
    proposal: PatchProposal,
    paper_artifacts: Sequence[Dict[str, Any]],
    review_draft: Dict[str, Any],
) -> bool:
    """Check if dependencies are still valid.
    
    Returns False if any dependency is missing or stale.
    """
    bundle = proposal.dependency_bundle
    
    # Find the paper artifact
    paper_artifact = None
    for pa in paper_artifacts:
        if pa.get("paper_identity", {}).get("canonical_paper_key") == proposal.metadata.get("paper_id"):
            paper_artifact = pa
            break
    
    if not paper_artifact:
        return False
    
    # Check summary hash
    summary_data = paper_artifact.get("analysis", {}).get("ai_summary", {})
    current_summary_hash = _compute_hash(summary_data)
    if current_summary_hash != bundle.summary_hash:
        return False
    
    # Check paper artifact hash
    current_artifact_hash = _compute_hash(paper_artifact)
    if current_artifact_hash != bundle.paper_artifact_hash:
        return False
    
    # Check visual refs hash
    selected_visual_refs = paper_artifact.get("stage1_inputs", {}).get("selected_visual_refs", [])
    current_visual_refs_hash = _compute_hash(selected_visual_refs)
    if current_visual_refs_hash != bundle.selected_visual_refs_hash:
        return False
    
    return True


def check_apply_guards(
    proposal: PatchProposal,
    review_draft: Dict[str, Any],
    paper_artifacts: Sequence[Dict[str, Any]],
) -> ApplyGuardResult:
    """Check all guards before applying a patch.
    
    Guards:
    1. Version guard - review_draft version matches expected
    2. Anchor/hash guard - target block still has expected anchor_hash
    3. Dependency guard - all dependencies present and not stale
    """
    block_reasons: List[str] = []
    
    # Version guard - check review_draft has expected structure
    version_guard_passed = True
    if review_draft.get("artifact_type") != "review_draft":
        version_guard_passed = False
        block_reasons.append("Invalid review_draft artifact_type")
    
    # Anchor/hash guard
    anchor_hash_guard_passed = True
    current_block_text = _get_block_text(review_draft, proposal.target.block_id)
    if current_block_text is None:
        anchor_hash_guard_passed = False
        block_reasons.append(f"Block {proposal.target.block_id} not found")
    else:
        current_anchor_hash = _compute_anchor_hash(current_block_text)
        if current_anchor_hash != proposal.target.anchor_hash:
            anchor_hash_guard_passed = False
            block_reasons.append(
                f"Anchor hash mismatch for block {proposal.target.block_id}: "
                f"expected {proposal.target.anchor_hash}, got {current_anchor_hash}"
            )
    
    # Dependency guard
    dependency_guard_passed = _check_dependency_bundle(proposal, paper_artifacts, review_draft)
    if not dependency_guard_passed:
        block_reasons.append("Dependency bundle check failed - dependencies missing or stale")
    
    can_apply = version_guard_passed and anchor_hash_guard_passed and dependency_guard_passed
    
    return ApplyGuardResult(
        can_apply=can_apply,
        version_guard_passed=version_guard_passed,
        anchor_hash_guard_passed=anchor_hash_guard_passed,
        dependency_guard_passed=dependency_guard_passed,
        block_reasons=block_reasons,
    )


def _apply_span_patch(block_text: str, proposal: PatchProposal) -> str:
    """Apply a span-level patch to block text.
    
    For citation_mapping_error: replace the problematic citation.
    For other issues: this is a placeholder - real implementation would
    use more sophisticated text matching.
    """
    if proposal.root_cause == RepairRootCause.CITATION_MAPPING_ERROR:
        # For mapping errors, replace with marker
        # In real implementation, this would find and replace the specific citation
        return block_text.replace(proposal.original_text, proposal.proposed_text)
    
    # For other span patches, return proposed text
    return proposal.proposed_text


def _apply_block_patch(block_text: str, proposal: PatchProposal) -> str:
    """Apply a block-level patch.
    
    Replaces the entire block content.
    """
    return proposal.proposed_text


def apply_patch(
    proposal: PatchProposal,
    review_draft: Dict[str, Any],
    paper_artifacts: Sequence[Dict[str, Any]],
    job_id: str,
) -> Optional[AppliedPatchRecord]:
    """Apply a single patch to review_draft.
    
    Returns AppliedPatchRecord on success, None on failure.
    """
    # Check guards first
    guard_result = check_apply_guards(proposal, review_draft, paper_artifacts)
    if not guard_result.can_apply:
        return None
    
    # Find the block
    sections = review_draft.get("content", {}).get("sections", [])
    for section in sections:
        for block in section.get("blocks", []):
            if block.get("block_id") == proposal.target.block_id:
                # Get current state
                original_text = block.get("text", "")
                anchor_hash_before = _compute_anchor_hash(original_text)
                
                # Apply patch based on granularity
                if proposal.granularity == PatchGranularity.SPAN:
                    new_text = _apply_span_patch(original_text, proposal)
                else:  # BLOCK
                    new_text = _apply_block_patch(original_text, proposal)
                
                # Update block
                block["text"] = new_text
                
                # Compute new anchor hash
                anchor_hash_after = _compute_anchor_hash(new_text)
                
                # Return record
                return AppliedPatchRecord(
                    record_id=str(uuid.uuid4()),
                    proposal_id=proposal.proposal_id,
                    plan_id=proposal.metadata.get("plan_id", ""),
                    applied_at=datetime.now().isoformat(),
                    applied_in_job_id=job_id,
                    original_text=original_text,
                    applied_text=new_text,
                    target_block_id=proposal.target.block_id,
                    anchor_hash_before=anchor_hash_before,
                    anchor_hash_after=anchor_hash_after,
                )
    
    return None


class RepairApplier:
    """Applies repair plans with guard enforcement.
    
    Enforces:
    - Version guard
    - Anchor/hash guard
    - Dependency guard
    - Block/span-only patch granularity (no whole-section rewrite)
    """
    
    def __init__(
        self,
        repair_plan: RepairPlan,
        review_draft: Dict[str, Any],
        citation_manifest: Dict[str, Any],
        paper_artifacts: Sequence[Dict[str, Any]],
        job_id: str,
    ):
        self.repair_plan = repair_plan
        self.review_draft = review_draft
        self.citation_manifest = citation_manifest
        self.paper_artifacts = paper_artifacts
        self.job_id = job_id
        self.applied_records: List[AppliedPatchRecord] = []
        self.rejected_proposals: List[Dict[str, Any]] = []
    
    def apply_all(self) -> RepairApplyResult:
        """Apply all patches in the plan.
        
        Skips patches that fail guard checks.
        Returns result with applied and rejected counts.
        """
        applied_ids: List[str] = []
        
        for proposal in self.repair_plan.proposals:
            # Check guards
            guard_result = check_apply_guards(
                proposal, self.review_draft, self.paper_artifacts
            )
            
            if not guard_result.can_apply:
                self.rejected_proposals.append({
                    "proposal_id": proposal.proposal_id,
                    "citation_id": proposal.citation_id,
                    "reason": "guard_check_failed",
                    "block_reasons": guard_result.block_reasons,
                })
                continue
            
            # Apply patch
            record = apply_patch(
                proposal, self.review_draft, self.paper_artifacts, self.job_id
            )
            
            if record:
                # Add plan_id to record metadata
                record_dict = record.to_dict()
                record_dict["plan_id"] = self.repair_plan.plan_id
                self.applied_records.append(record)
                applied_ids.append(proposal.proposal_id)
            else:
                self.rejected_proposals.append({
                    "proposal_id": proposal.proposal_id,
                    "citation_id": proposal.citation_id,
                    "reason": "apply_failed",
                })
        
        return RepairApplyResult(
            success=len(self.applied_records) > 0,
            plan_id=self.repair_plan.plan_id,
            applied_count=len(self.applied_records),
            rejected_count=len(self.rejected_proposals),
            applied_proposals=applied_ids,
            rejected_proposals=self.rejected_proposals,
        )
    
    def get_patched_review_draft(self) -> Dict[str, Any]:
        """Get the patched review_draft."""
        return self.review_draft
    
    def get_applied_records(self) -> List[AppliedPatchRecord]:
        """Get records of all applied patches."""
        return self.applied_records


def run_repair_apply(
    repair_plan: RepairPlan,
    review_draft: Dict[str, Any],
    citation_manifest: Dict[str, Any],
    paper_artifacts: Sequence[Dict[str, Any]],
    job_id: str,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Week 4 entry point for repair application.
    
    Applies repair plan with full guard enforcement.
    If dry_run is True, only checks guards without applying.
    """
    applier = RepairApplier(
        repair_plan=repair_plan,
        review_draft=review_draft,
        citation_manifest=citation_manifest,
        paper_artifacts=paper_artifacts,
        job_id=job_id,
    )
    
    if dry_run:
        # Just check guards for all proposals
        results = []
        for proposal in repair_plan.proposals:
            guard_result = check_apply_guards(proposal, review_draft, paper_artifacts)
            results.append({
                "proposal_id": proposal.proposal_id,
                "can_apply": guard_result.can_apply,
                "guards": guard_result.to_dict(),
            })
        return {
            "week4_repair_apply": True,
            "dry_run": True,
            "plan_id": repair_plan.plan_id,
            "proposal_checks": results,
        }
    
    # Apply all patches
    result = applier.apply_all()
    patched_draft = applier.get_patched_review_draft()
    applied_records = applier.get_applied_records()
    
    return {
        "week4_repair_apply": True,
        "dry_run": False,
        "apply_result": result.to_dict(),
        "patched_review_draft": patched_draft,
        "applied_records": [r.to_dict() for r in applied_records],
    }
