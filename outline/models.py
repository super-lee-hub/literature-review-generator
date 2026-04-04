"""Outline models for Week 5.

Defines the JSON-first outline representation with critique and arbitration support.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class CritiqueCategory(Enum):
    """Fixed taxonomy for outline critique."""
    MISSING_THEME = "missing_theme"
    WEAK_SUPPORT_FROM_SUMMARIES = "weak_support_from_summaries"
    REDUNDANT_SECTION = "redundant_section"
    ORDERING_ISSUE = "ordering_issue"
    OVERCLAIM = "overclaim"
    SCOPE_MISMATCH = "scope_mismatch"


class ReviewStatus(Enum):
    """Review status for outline document."""
    DRAFT = "draft"
    UNDER_REVIEW = "under_review"
    CRITIQUED = "critiqued"
    ARBITRATED = "arbitrated"
    ADOPTED = "adopted"
    REJECTED = "rejected"


class ArbitrationDecision(Enum):
    """Arbitration decisions for critiques."""
    ACCEPT = "accept"  # Accept the critique, modify outline
    REJECT = "reject"  # Reject the critique, keep current
    DEFER = "defer"    # Defer decision, needs human review


@dataclass(frozen=True)
class OutlineSection:
    """A section in the outline.
    
    Each section has stable identity and references to supporting summaries.
    """
    section_id: str
    title: str
    purpose: str
    supporting_summary_refs: List[str]  # References to summary hashes/IDs
    children: List[OutlineSection] = field(default_factory=list)
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "section_id": self.section_id,
            "title": self.title,
            "purpose": self.purpose,
            "supporting_summary_refs": self.supporting_summary_refs,
            "children": [c.to_dict() for c in self.children],
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> OutlineSection:
        children_data = data.get("children", [])
        children = [cls.from_dict(c) for c in children_data]
        return cls(
            section_id=data["section_id"],
            title=data["title"],
            purpose=data.get("purpose", ""),
            supporting_summary_refs=data.get("supporting_summary_refs", []),
            children=children,
        )


@dataclass(frozen=True)
class OutlineCritique:
    """A critique of an outline section or the entire outline.
    
    Uses fixed taxonomy for critique categories.
    """
    critique_id: str
    created_at: str
    created_by: str  # Model or peer that created the critique
    target_section_id: Optional[str]  # None for whole-outline critiques
    category: CritiqueCategory
    description: str
    severity: str  # "high", "medium", "low"
    suggested_fix: Optional[str]
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "critique_id": self.critique_id,
            "created_at": self.created_at,
            "created_by": self.created_by,
            "target_section_id": self.target_section_id,
            "category": self.category.value,
            "description": self.description,
            "severity": self.severity,
            "suggested_fix": self.suggested_fix,
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> OutlineCritique:
        return cls(
            critique_id=data["critique_id"],
            created_at=data["created_at"],
            created_by=data["created_by"],
            target_section_id=data.get("target_section_id"),
            category=CritiqueCategory(data["category"]),
            description=data["description"],
            severity=data.get("severity", "medium"),
            suggested_fix=data.get("suggested_fix"),
        )


@dataclass(frozen=True)
class CritiqueArbitration:
    """Arbitration decision for a specific critique."""
    critique_id: str
    decision: ArbitrationDecision
    reason: str
    arbitrated_at: str
    arbitrated_by: str
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "critique_id": self.critique_id,
            "decision": self.decision.value,
            "reason": self.reason,
            "arbitrated_at": self.arbitrated_at,
            "arbitrated_by": self.arbitrated_by,
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> CritiqueArbitration:
        return cls(
            critique_id=data["critique_id"],
            decision=ArbitrationDecision(data["decision"]),
            reason=data["reason"],
            arbitrated_at=data["arbitrated_at"],
            arbitrated_by=data["arbitrated_by"],
        )


@dataclass(frozen=True)
class OutlineArbitrationResult:
    """Result of arbitration process for all critiques.
    
    Contains decisions for each critique and the resulting outline state.
    """
    result_id: str
    created_at: str
    created_from_job_id: str
    outline_id: str
    arbitrations: List[CritiqueArbitration]
    accepted_critiques: List[str]  # critique_ids that were accepted
    rejected_critiques: List[str]  # critique_ids that were rejected
    deferred_critiques: List[str]  # critique_ids that were deferred
    modified_sections: List[str]  # section_ids that were modified
    artifact_type: str = "outline_arbitration_result"
    artifact_version: str = "v1"
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "artifact_type": self.artifact_type,
            "artifact_version": self.artifact_version,
            "result_id": self.result_id,
            "created_at": self.created_at,
            "created_from_job_id": self.created_from_job_id,
            "outline_id": self.outline_id,
            "arbitrations": [a.to_dict() for a in self.arbitrations],
            "accepted_critiques": self.accepted_critiques,
            "rejected_critiques": self.rejected_critiques,
            "deferred_critiques": self.deferred_critiques,
            "modified_sections": self.modified_sections,
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> OutlineArbitrationResult:
        arbitrations_data = data.get("arbitrations", [])
        arbitrations = [CritiqueArbitration.from_dict(a) for a in arbitrations_data]
        return cls(
            result_id=data["result_id"],
            created_at=data["created_at"],
            created_from_job_id=data["created_from_job_id"],
            outline_id=data["outline_id"],
            arbitrations=arbitrations,
            accepted_critiques=data.get("accepted_critiques", []),
            rejected_critiques=data.get("rejected_critiques", []),
            deferred_critiques=data.get("deferred_critiques", []),
            modified_sections=data.get("modified_sections", []),
        )


@dataclass(frozen=True)
class OutlineDocument:
    """JSON-first outline document.
    
    This is the primary outline representation. Markdown is a projection only.
    """
    artifact_type: str
    artifact_version: str
    created_from_job_id: str
    created_at: str
    outline_id: str
    outline_version: str
    source_summary_hashes: List[str]
    generator_model: str
    review_status: ReviewStatus
    sections: List[OutlineSection]
    critiques: List[OutlineCritique]
    arbitration_result_id: Optional[str]
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "artifact_type": self.artifact_type,
            "artifact_version": self.artifact_version,
            "created_from_job_id": self.created_from_job_id,
            "created_at": self.created_at,
            "outline_id": self.outline_id,
            "outline_version": self.outline_version,
            "source_summary_hashes": self.source_summary_hashes,
            "generator_model": self.generator_model,
            "review_status": self.review_status.value,
            "sections": [s.to_dict() for s in self.sections],
            "critiques": [c.to_dict() for c in self.critiques],
            "arbitration_result_id": self.arbitration_result_id,
            "metadata": self.metadata,
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> OutlineDocument:
        sections_data = data.get("sections", [])
        sections = [OutlineSection.from_dict(s) for s in sections_data]
        
        critiques_data = data.get("critiques", [])
        critiques = [OutlineCritique.from_dict(c) for c in critiques_data]
        
        return cls(
            artifact_type=data.get("artifact_type", "outline_document"),
            artifact_version=data.get("artifact_version", "v1"),
            created_from_job_id=data["created_from_job_id"],
            created_at=data["created_at"],
            outline_id=data["outline_id"],
            outline_version=data.get("outline_version", "v1"),
            source_summary_hashes=data.get("source_summary_hashes", []),
            generator_model=data.get("generator_model", ""),
            review_status=ReviewStatus(data.get("review_status", "draft")),
            sections=sections,
            critiques=critiques,
            arbitration_result_id=data.get("arbitration_result_id"),
            metadata=data.get("metadata", {}),
        )
    
    def with_critiques(self, new_critiques: List[OutlineCritique]) -> OutlineDocument:
        """Return a new OutlineDocument with additional critiques."""
        all_critiques = list(self.critiques) + new_critiques
        return OutlineDocument(
            artifact_type=self.artifact_type,
            artifact_version=self.artifact_version,
            created_from_job_id=self.created_from_job_id,
            created_at=self.created_at,
            outline_id=self.outline_id,
            outline_version=self.outline_version,
            source_summary_hashes=self.source_summary_hashes,
            generator_model=self.generator_model,
            review_status=ReviewStatus.CRITIQUED if new_critiques else self.review_status,
            sections=self.sections,
            critiques=all_critiques,
            arbitration_result_id=self.arbitration_result_id,
            metadata=self.metadata,
        )
    
    def with_arbitration(self, arbitration_result: OutlineArbitrationResult) -> OutlineDocument:
        """Return a new OutlineDocument with arbitration applied."""
        return OutlineDocument(
            artifact_type=self.artifact_type,
            artifact_version=self.artifact_version,
            created_from_job_id=self.created_from_job_id,
            created_at=self.created_at,
            outline_id=self.outline_id,
            outline_version=self.outline_version,
            source_summary_hashes=self.source_summary_hashes,
            generator_model=self.generator_model,
            review_status=ReviewStatus.ARBITRATED,
            sections=self.sections,  # May be modified based on accepted critiques
            critiques=self.critiques,
            arbitration_result_id=arbitration_result.result_id,
            metadata=self.metadata,
        )
    
    def with_adopted_status(self) -> OutlineDocument:
        """Return a new OutlineDocument marked as adopted."""
        return OutlineDocument(
            artifact_type=self.artifact_type,
            artifact_version=self.artifact_version,
            created_from_job_id=self.created_from_job_id,
            created_at=self.created_at,
            outline_id=self.outline_id,
            outline_version=self.outline_version,
            source_summary_hashes=self.source_summary_hashes,
            generator_model=self.generator_model,
            review_status=ReviewStatus.ADOPTED,
            sections=self.sections,
            critiques=self.critiques,
            arbitration_result_id=self.arbitration_result_id,
            metadata=self.metadata,
        )
    
    def get_section_by_id(self, section_id: str) -> Optional[OutlineSection]:
        """Find a section by ID (recursively searches children)."""
        for section in self.sections:
            if section.section_id == section_id:
                return section
            for child in section.children:
                if child.section_id == section_id:
                    return child
        return None
    
    def to_markdown(self) -> str:
        """Convert outline to markdown projection.
        
        This is a projection only - the JSON is the source of truth.
        """
        lines = [f"# Literature Review Outline\n"]
        lines.append(f"<!-- Generated: {self.created_at} -->")
        lines.append(f"<!-- Model: {self.generator_model} -->")
        lines.append(f"<!-- Status: {self.review_status.value} -->\n")
        
        for i, section in enumerate(self.sections, 1):
            lines.extend(self._section_to_markdown(section, level=2, number=i))
        
        return "\n".join(lines)
    
    def _section_to_markdown(self, section: OutlineSection, level: int, number: int) -> List[str]:
        """Convert a section to markdown lines."""
        lines = []
        prefix = "#" * level
        lines.append(f"{prefix} {number}. {section.title}\n")
        lines.append(f"**Purpose:** {section.purpose}\n")
        
        if section.supporting_summary_refs:
            lines.append("**Supporting Summaries:**")
            for ref in section.supporting_summary_refs:
                lines.append(f"- {ref}")
            lines.append("")
        
        for j, child in enumerate(section.children, 1):
            lines.extend(self._section_to_markdown(child, level=level + 1, number=j))
        
        return lines


@dataclass(frozen=True)
class ReviewedOutlineDocument:
    """A reviewed outline that has been explicitly adopted.
    
    This becomes the downstream truth only after explicit adopt.
    """
    artifact_type: str
    artifact_version: str
    created_from_job_id: str
    created_at: str
    original_outline_id: str
    reviewed_outline_id: str
    outline: OutlineDocument
    adopted_at: str
    adopted_by: str
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "artifact_type": self.artifact_type,
            "artifact_version": self.artifact_version,
            "created_from_job_id": self.created_from_job_id,
            "created_at": self.created_at,
            "original_outline_id": self.original_outline_id,
            "reviewed_outline_id": self.reviewed_outline_id,
            "outline": self.outline.to_dict(),
            "adopted_at": self.adopted_at,
            "adopted_by": self.adopted_by,
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> ReviewedOutlineDocument:
        return cls(
            artifact_type=data.get("artifact_type", "reviewed_outline_document"),
            artifact_version=data.get("artifact_version", "v1"),
            created_from_job_id=data["created_from_job_id"],
            created_at=data["created_at"],
            original_outline_id=data["original_outline_id"],
            reviewed_outline_id=data["reviewed_outline_id"],
            outline=OutlineDocument.from_dict(data["outline"]),
            adopted_at=data["adopted_at"],
            adopted_by=data["adopted_by"],
        )
