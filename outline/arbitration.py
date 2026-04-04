"""Outline arbitration for Week 5.

Peer critique and arbitration-driven outline review.
Explicit adopt gating before reviewed outline becomes downstream truth.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence

from outline.models import (
    ArbitrationDecision,
    CritiqueArbitration,
    CritiqueCategory,
    OutlineArbitrationResult,
    OutlineCritique,
    OutlineDocument,
    OutlineSection,
    ReviewStatus,
    ReviewedOutlineDocument,
)


def create_critique(
    target_section_id: Optional[str],
    category: CritiqueCategory,
    description: str,
    created_by: str,
    severity: str = "medium",
    suggested_fix: Optional[str] = None,
) -> OutlineCritique:
    """Create a new critique.
    
    Uses fixed taxonomy for critique categories.
    """
    return OutlineCritique(
        critique_id=str(uuid.uuid4()),
        created_at=datetime.now().isoformat(),
        created_by=created_by,
        target_section_id=target_section_id,
        category=category,
        description=description,
        severity=severity,
        suggested_fix=suggested_fix,
    )


def run_peer_critique(
    outline: OutlineDocument,
    critic_model: str,
    summaries: Sequence[Dict[str, Any]],
) -> List[OutlineCritique]:
    """Run peer critique on an outline.
    
    This is a structured critique using the fixed taxonomy:
    - missing_theme
    - weak_support_from_summaries
    - redundant_section
    - ordering_issue
    - overclaim
    - scope_mismatch
    
    In a real implementation, this would call an AI model.
    For now, we provide a basic implementation that checks for common issues.
    """
    critiques: List[OutlineCritique] = []
    
    # Check for sections without supporting summaries
    for section in outline.sections:
        if not section.supporting_summary_refs:
            critiques.append(create_critique(
                target_section_id=section.section_id,
                category=CritiqueCategory.WEAK_SUPPORT_FROM_SUMMARIES,
                description=f"Section '{section.title}' has no supporting summary references",
                created_by=critic_model,
                severity="medium",
                suggested_fix="Add relevant summary references or consider removing this section",
            ))
        
        # Check for empty purpose
        if not section.purpose or len(section.purpose) < 10:
            critiques.append(create_critique(
                target_section_id=section.section_id,
                category=CritiqueCategory.SCOPE_MISMATCH,
                description=f"Section '{section.title}' has unclear or missing purpose",
                created_by=critic_model,
                severity="low",
                suggested_fix="Add a clear purpose statement for this section",
            ))
        
        # Check children
        for child in section.children:
            if not child.supporting_summary_refs:
                critiques.append(create_critique(
                    target_section_id=child.section_id,
                    category=CritiqueCategory.WEAK_SUPPORT_FROM_SUMMARIES,
                    description=f"Subsection '{child.title}' has no supporting summary references",
                    created_by=critic_model,
                    severity="low",
                ))
    
    # Check for potential ordering issues (very basic check)
    if len(outline.sections) > 1:
        # Check if introduction comes after other sections
        for i, section in enumerate(outline.sections):
            if "introduction" in section.title.lower() and i > 0:
                critiques.append(create_critique(
                    target_section_id=section.section_id,
                    category=CritiqueCategory.ORDERING_ISSUE,
                    description="Introduction should typically come first",
                    created_by=critic_model,
                    severity="medium",
                    suggested_fix="Move introduction to the beginning of the outline",
                ))
            
            if "conclusion" in section.title.lower() and i < len(outline.sections) - 1:
                critiques.append(create_critique(
                    target_section_id=section.section_id,
                    category=CritiqueCategory.ORDERING_ISSUE,
                    description="Conclusion should typically come last",
                    created_by=critic_model,
                    severity="medium",
                    suggested_fix="Move conclusion to the end of the outline",
                ))
    
    return critiques


def arbitrate_critique(
    critique: OutlineCritique,
    decision: ArbitrationDecision,
    reason: str,
    arbitrated_by: str,
) -> CritiqueArbitration:
    """Create an arbitration decision for a critique."""
    return CritiqueArbitration(
        critique_id=critique.critique_id,
        decision=decision,
        reason=reason,
        arbitrated_at=datetime.now().isoformat(),
        arbitrated_by=arbitrated_by,
    )


def run_arbitration(
    outline: OutlineDocument,
    critiques: Sequence[OutlineCritique],
    arbitrations: Sequence[CritiqueArbitration],
    job_id: str,
    arbitrated_by: str,
) -> OutlineArbitrationResult:
    """Run arbitration on critiques.
    
    Processes all critiques and creates arbitration result.
    """
    accepted: List[str] = []
    rejected: List[str] = []
    deferred: List[str] = []
    modified_sections: List[str] = []
    
    arbitration_list = list(arbitrations)
    
    for critique in critiques:
        # Find arbitration for this critique
        arb = next((a for a in arbitration_list if a.critique_id == critique.critique_id), None)
        
        if not arb:
            # Auto-defer if no arbitration provided
            arb = arbitrate_critique(
                critique=critique,
                decision=ArbitrationDecision.DEFER,
                reason="No arbitration decision provided",
                arbitrated_by=arbitrated_by,
            )
            arbitration_list.append(arb)
        
        if arb.decision == ArbitrationDecision.ACCEPT:
            accepted.append(critique.critique_id)
            if critique.target_section_id:
                modified_sections.append(critique.target_section_id)
        elif arb.decision == ArbitrationDecision.REJECT:
            rejected.append(critique.critique_id)
        else:  # DEFER
            deferred.append(critique.critique_id)
    
    return OutlineArbitrationResult(
        result_id=str(uuid.uuid4()),
        created_at=datetime.now().isoformat(),
        created_from_job_id=job_id,
        outline_id=outline.outline_id,
        arbitrations=arbitration_list,
        accepted_critiques=accepted,
        rejected_critiques=rejected,
        deferred_critiques=deferred,
        modified_sections=list(set(modified_sections)),  # Deduplicate
    )


def apply_accepted_critiques(
    outline: OutlineDocument,
    arbitration_result: OutlineArbitrationResult,
) -> OutlineDocument:
    """Apply accepted critiques to modify the outline.
    
    Implements basic critique application logic:
    - Reorder sections for ordering_issue critiques (move intro to front, conclusion to end)
    - TODO: Add sections for missing_theme critiques
    - TODO: Remove sections for redundant_section critiques
    - TODO: Update section purpose for scope_mismatch critiques
    - TODO: Add summary references for weak_support_from_summaries critiques
    """
    # Get map of accepted critiques
    accepted_critique_ids = set(arbitration_result.accepted_critiques)
    
    # Build map from critique_id to critique
    critique_map = {c.critique_id: c for c in outline.critiques}
    
    # Start with a copy of the sections
    modified_sections = list(outline.sections)
    
    # Process ordering_issue critiques first
    for critique_id in accepted_critique_ids:
        critique = critique_map.get(critique_id)
        if not critique:
            continue
        
        if critique.category == CritiqueCategory.ORDERING_ISSUE:
            # Check if this is an introduction or conclusion ordering issue
            if critique.target_section_id:
                section = outline.get_section_by_id(critique.target_section_id)
                if section:
                    if "introduction" in section.title.lower():
                        # Move introduction to front
                        if modified_sections[0].section_id != section.section_id:
                            # Remove and insert at front
                            modified_sections = [s for s in modified_sections if s.section_id != section.section_id]
                            modified_sections.insert(0, section)
                    elif "conclusion" in section.title.lower():
                        # Move conclusion to end
                        if modified_sections[-1].section_id != section.section_id:
                            # Remove and append at end
                            modified_sections = [s for s in modified_sections if s.section_id != section.section_id]
                            modified_sections.append(section)
    
    # Create new outline with modified sections
    return OutlineDocument(
        artifact_type=outline.artifact_type,
        artifact_version=outline.artifact_version,
        created_from_job_id=outline.created_from_job_id,
        created_at=outline.created_at,
        outline_id=outline.outline_id,
        outline_version=outline.outline_version,
        source_summary_hashes=outline.source_summary_hashes,
        generator_model=outline.generator_model,
        review_status=ReviewStatus.ARBITRATED,
        sections=modified_sections,
        critiques=outline.critiques,
        arbitration_result_id=arbitration_result.result_id,
        metadata={
            **outline.metadata,
            "applied_critiques": list(accepted_critique_ids),
            "modified_at": datetime.now().isoformat(),
        },
    )


def adopt_outline(
    outline: OutlineDocument,
    arbitration_result: OutlineArbitrationResult,
    job_id: str,
    adopted_by: str,
) -> ReviewedOutlineDocument:
    """Explicitly adopt an outline after arbitration.
    
    The reviewed outline becomes the downstream truth only after explicit adopt.
    No silent overwrite of the current outline.
    """
    # First apply arbitration
    modified_outline = apply_accepted_critiques(outline, arbitration_result)
    
    # Mark as adopted
    adopted_outline = modified_outline.with_adopted_status()
    
    return ReviewedOutlineDocument(
        artifact_type="reviewed_outline_document",
        artifact_version="v1",
        created_from_job_id=job_id,
        created_at=datetime.now().isoformat(),
        original_outline_id=outline.outline_id,
        reviewed_outline_id=str(uuid.uuid4()),
        outline=adopted_outline,
        adopted_at=datetime.now().isoformat(),
        adopted_by=adopted_by,
    )


class OutlineArbitrator:
    """Arbitrator for outline critique and adoption."""
    
    def __init__(
        self,
        outline: OutlineDocument,
        job_id: str,
    ):
        self.outline = outline
        self.job_id = job_id
        self.critiques: List[OutlineCritique] = []
        self.arbitrations: List[CritiqueArbitration] = []
    
    def add_critique(
        self,
        target_section_id: Optional[str],
        category: CritiqueCategory,
        description: str,
        created_by: str,
        severity: str = "medium",
        suggested_fix: Optional[str] = None,
    ) -> OutlineCritique:
        """Add a critique to the outline."""
        critique = create_critique(
            target_section_id=target_section_id,
            category=category,
            description=description,
            created_by=created_by,
            severity=severity,
            suggested_fix=suggested_fix,
        )
        self.critiques.append(critique)
        return critique
    
    def add_arbitration(
        self,
        critique_id: str,
        decision: ArbitrationDecision,
        reason: str,
        arbitrated_by: str,
    ) -> CritiqueArbitration:
        """Add an arbitration decision."""
        arbitration = CritiqueArbitration(
            critique_id=critique_id,
            decision=decision,
            reason=reason,
            arbitrated_at=datetime.now().isoformat(),
            arbitrated_by=arbitrated_by,
        )
        self.arbitrations.append(arbitration)
        return arbitration
    
    def run_arbitration(self, arbitrated_by: str) -> OutlineArbitrationResult:
        """Run arbitration on all critiques."""
        return run_arbitration(
            outline=self.outline,
            critiques=self.critiques,
            arbitrations=self.arbitrations,
            job_id=self.job_id,
            arbitrated_by=arbitrated_by,
        )
    
    def adopt(self, adopted_by: str) -> Optional[ReviewedOutlineDocument]:
        """Adopt the outline after arbitration.
        
        Only works if arbitration has been run.
        """
        if not self.arbitrations:
            return None
        
        arbitration_result = self.run_arbitration(adopted_by)
        return adopt_outline(
            outline=self.outline,
            arbitration_result=arbitration_result,
            job_id=self.job_id,
            adopted_by=adopted_by,
        )


def run_outline_critique(
    outline: OutlineDocument,
    critic_model: str,
    summaries: Sequence[Dict[str, Any]],
    job_id: str,
) -> Dict[str, Any]:
    """Week 5 entry point for outline critique.
    
    Runs peer critique using fixed taxonomy.
    """
    critiques = run_peer_critique(
        outline=outline,
        critic_model=critic_model,
        summaries=summaries,
    )
    
    # Return outline with critiques
    outlined_with_critiques = outline.with_critiques(critiques)
    
    return {
        "week5_outline_critique": True,
        "outline": outlined_with_critiques.to_dict(),
        "critiques": [c.to_dict() for c in critiques],
        "critique_count": len(critiques),
    }


def run_outline_arbitration(
    outline: OutlineDocument,
    arbitrations: Sequence[CritiqueArbitration],
    job_id: str,
    arbitrated_by: str,
) -> Dict[str, Any]:
    """Week 5 entry point for outline arbitration.
    
    Runs arbitration on critiques and returns result.
    """
    arbitration_result = run_arbitration(
        outline=outline,
        critiques=outline.critiques,
        arbitrations=arbitrations,
        job_id=job_id,
        arbitrated_by=arbitrated_by,
    )
    
    # Apply arbitration to outline
    arbitrated_outline = apply_accepted_critiques(outline, arbitration_result)
    
    return {
        "week5_outline_arbitration": True,
        "outline": arbitrated_outline.to_dict(),
        "arbitration_result": arbitration_result.to_dict(),
    }


def run_outline_adopt(
    outline: OutlineDocument,
    arbitration_result: OutlineArbitrationResult,
    job_id: str,
    adopted_by: str,
) -> Dict[str, Any]:
    """Week 5 entry point for outline adoption.
    
    Explicitly adopts an outline after arbitration.
    Reviewed outline becomes downstream truth only after explicit adopt.
    """
    reviewed_outline = adopt_outline(
        outline=outline,
        arbitration_result=arbitration_result,
        job_id=job_id,
        adopted_by=adopted_by,
    )
    
    return {
        "week5_outline_adopt": True,
        "reviewed_outline": reviewed_outline.to_dict(),
    }
