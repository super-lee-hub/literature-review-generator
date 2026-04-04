from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional, Sequence
from enum import Enum

from validation.evidence_resolver import EvidenceCandidate, EvidenceResolver, build_evidence_resolver_context


class ValidationConclusion(Enum):
    SUPPORTED = "SUPPORTED"
    PARTIAL_SUPPORT = "PARTIAL_SUPPORT"
    UNSUPPORTED = "UNSUPPORTED"
    WRONG_SOURCE = "WRONG_SOURCE"
    NEEDS_REVIEW = "NEEDS_REVIEW"


class RootCause(Enum):
    SUMMARY_DRIFT = "summary_drift"
    REVIEW_DRIFT = "review_drift"
    CITATION_MAPPING_ERROR = "citation_mapping_error"
    INSUFFICIENT_CONTEXT = "insufficient_context"
    VISUAL_UNDERSTANDING_GAP = "visual_understanding_gap"


@dataclass(frozen=True)
class CitationValidationResult:
    citation_id: str
    paper_id: str
    conclusion: ValidationConclusion
    root_causes: List[RootCause]
    evidence_candidates: List[EvidenceCandidate]
    details: Dict[str, Any]


@dataclass(frozen=True)
class ReviewValidationReport:
    report_id: str
    created_at: str
    total_citations: int
    supported_count: int
    partial_support_count: int
    unsupported_count: int
    wrong_source_count: int
    needs_review_count: int
    citation_results: List[CitationValidationResult]


class ReviewValidator:
    def __init__(
        self,
        review_draft: Dict[str, Any],
        citation_manifest: Dict[str, Any],
        paper_artifacts: Sequence[Dict[str, Any]],
    ):
        self.review_draft = review_draft
        self.citation_manifest = citation_manifest
        self.paper_artifacts = {
            pa.get("paper_identity", {}).get("canonical_paper_key", ""): pa
            for pa in paper_artifacts
        }
        self.paper_artifacts.update({
            pa.get("paper_identity", {}).get("source_paper_id", ""): pa
            for pa in paper_artifacts
        })

    def validate(self) -> ReviewValidationReport:
        from datetime import datetime

        # Primary path: consume v2 occurrences/clusters/bibliography
        occurrences = self._get_occurrences_from_manifest()
        citation_results: List[CitationValidationResult] = []

        for occurrence in occurrences:
            citation_results.append(self._validate_occurrence(occurrence))

        supported_count = sum(1 for r in citation_results if r.conclusion == ValidationConclusion.SUPPORTED)
        partial_count = sum(1 for r in citation_results if r.conclusion == ValidationConclusion.PARTIAL_SUPPORT)
        unsupported_count = sum(1 for r in citation_results if r.conclusion == ValidationConclusion.UNSUPPORTED)
        wrong_source_count = sum(1 for r in citation_results if r.conclusion == ValidationConclusion.WRONG_SOURCE)
        needs_review_count = sum(1 for r in citation_results if r.conclusion == ValidationConclusion.NEEDS_REVIEW)

        return ReviewValidationReport(
            report_id=f"validation_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}",
            created_at=datetime.now().isoformat(),
            total_citations=len(occurrences),
            supported_count=supported_count,
            partial_support_count=partial_count,
            unsupported_count=unsupported_count,
            wrong_source_count=wrong_source_count,
            needs_review_count=needs_review_count,
            citation_results=citation_results,
        )

    def _get_block_from_review_draft(self, block_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve a block from review_draft_v2 using block_id."""
        sections = self.review_draft.get("content", {}).get("sections", [])
        for section in sections:
            blocks = section.get("blocks", [])
            for block in blocks:
                if block.get("block_id") == block_id:
                    return block
        return None

    def _get_occurrences_from_manifest(self) -> List[Dict[str, Any]]:
        """Extract occurrences from citation manifest, preferring v2 structure."""
        # Primary: v2 occurrences (primary runtime truth source)
        occurrences = self.citation_manifest.get("occurrences", [])
        if occurrences:
            return occurrences
        
        # Fallback: v1 citations (legacy compatibility)
        citations = self.citation_manifest.get("citations", [])
        return citations

    def _validate_citation_generic(
        self,
        citation_id: str,
        paper_id: str,
        cited_text: str,
        context: str,
        block_text: Optional[str] = None,
        locator: Optional[str] = None,
    ) -> CitationValidationResult:
        """Generic citation validation logic shared by occurrence and citation.
        
        Primary path prefers block_text from review_draft_v2 over just context.
        """
        paper_artifact = self.paper_artifacts.get(paper_id)
        details: Dict[str, Any] = {"used_block_text": bool(block_text)}

        if not paper_artifact:
            details["reason"] = "paper_not_found_in_artifacts"
            return CitationValidationResult(
                citation_id=citation_id,
                paper_id=paper_id,
                conclusion=ValidationConclusion.WRONG_SOURCE,
                root_causes=[RootCause.CITATION_MAPPING_ERROR],
                evidence_candidates=[],
                details=details,
            )

        resolver_context = build_evidence_resolver_context(paper_artifact)
        resolver = EvidenceResolver(resolver_context)
        selected_visual_refs = paper_artifact.get("stage1_inputs", {}).get("selected_visual_refs", [])
        
        # Prioritize block text from review_draft_v2 for evidence resolution
        # Use block_text if available, otherwise fall back to cited_text
        search_span = block_text if block_text else cited_text
        
        evidence_candidates = resolver.resolve_evidence(
            cited_span=search_span,
            locator=locator,
            selected_visual_refs=selected_visual_refs,
        )

        conclusion, root_causes = self._classify_citation(
            cited_text=cited_text,
            context=context,
            evidence_candidates=evidence_candidates,
            has_visual_refs=bool(selected_visual_refs),
        )

        return CitationValidationResult(
            citation_id=citation_id,
            paper_id=paper_id,
            conclusion=conclusion,
            root_causes=root_causes,
            evidence_candidates=evidence_candidates,
            details=details,
        )

    def _validate_occurrence(self, occurrence: Dict[str, Any]) -> CitationValidationResult:
        """Validate a single citation occurrence (v2-style, primary path).
        
        This uses block_id to retrieve the full block text from review_draft_v2
        as the primary context for evidence resolution.
        """
        occurrence_id = occurrence.get("occurrence_id") or occurrence.get("citation_id", "")
        paper_id = occurrence.get("paper_id", "")
        cited_text = occurrence.get("citation_token") or occurrence.get("text", "")
        context = occurrence.get("context_before") or occurrence.get("context", "")
        block_id = occurrence.get("block_id", "")
        
        # Get block text from review_draft_v2 for richer context
        block = self._get_block_from_review_draft(block_id)
        block_text = block.get("text") if block else None
        locator = None  # Could be extended in future if needed
        
        return self._validate_citation_generic(
            citation_id=occurrence_id,
            paper_id=paper_id,
            cited_text=cited_text,
            context=context,
            block_text=block_text,
            locator=locator,
        )

    def _validate_citation(self, citation: Dict[str, Any]) -> CitationValidationResult:
        """Validate a single citation (v1-style, legacy fallback)."""
        citation_id = citation.get("citation_id", "")
        paper_id = citation.get("paper_id", "")
        cited_text = citation.get("text", "")
        context = citation.get("context", "")

        return self._validate_citation_generic(
            citation_id=citation_id,
            paper_id=paper_id,
            cited_text=cited_text,
            context=context,
        )

    def _classify_citation(
        self,
        cited_text: str,
        context: str,
        evidence_candidates: List[EvidenceCandidate],
        has_visual_refs: bool,
    ) -> tuple[ValidationConclusion, List[RootCause]]:
        high_confidence = [c for c in evidence_candidates if c.confidence >= 0.8]
        medium_confidence = [c for c in evidence_candidates if 0.5 <= c.confidence < 0.8]
        visual_candidates = [c for c in evidence_candidates if c.evidence_scope == "visual"]

        if high_confidence:
            return ValidationConclusion.SUPPORTED, []
        elif medium_confidence:
            return ValidationConclusion.PARTIAL_SUPPORT, [RootCause.INSUFFICIENT_CONTEXT]
        elif visual_candidates and has_visual_refs:
            return ValidationConclusion.NEEDS_REVIEW, [RootCause.VISUAL_UNDERSTANDING_GAP]
        else:
            return ValidationConclusion.UNSUPPORTED, [RootCause.INSUFFICIENT_CONTEXT]
