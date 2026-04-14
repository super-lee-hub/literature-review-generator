from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional, Sequence

from services.citation_manifest import normalize_citation_set_key
from . import PreprocessEvidenceLoader
from .evidence_resolver import EvidenceCandidate, EvidenceResolver, EvidenceResolverContext


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
    COMPOUND_DRIFT = "compound_drift"
    LOW_CONFIDENCE = "low_confidence"


@dataclass
class CitationValidationResult:
    citation_id: str
    paper_id: str
    conclusion: ValidationConclusion
    root_causes: List[RootCause]
    evidence_candidates: List[EvidenceCandidate]
    details: Dict[str, Any]
    claim_text: str
    claim_context: str
    evidence_excerpt_list: List[str]
    reasoning_summary: str
    repair_hint: str
    citation_set_key: str = ""
    paper_ids: List[str] = field(default_factory=list)
    block_ids: List[str] = field(default_factory=list)
    low_confidence: bool = False


@dataclass
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
        preprocess_evidence: Optional[Dict[str, Any]] = None,
        paper_metadata: Optional[Dict[str, Any]] = None,
    ):
        self.review_draft = review_draft or {}
        self.citation_manifest = citation_manifest or {}
        self.paper_artifacts: Dict[str, Dict[str, Any]] = {}
        for artifact in paper_artifacts:
            identity = artifact.get("paper_identity", {})
            for key in (
                str(identity.get("canonical_paper_key") or "").strip(),
                str(identity.get("source_paper_id") or "").strip(),
            ):
                if key:
                    self.paper_artifacts[key] = artifact
        self.preprocess_evidence = preprocess_evidence or {}
        self.paper_metadata = paper_metadata or {}
        self.evidence_loader = PreprocessEvidenceLoader()

    def validate(self) -> ReviewValidationReport:
        citation_sets = self._get_citation_sets_from_manifest()
        citation_results = [self._validate_citation_set(bundle) for bundle in citation_sets]
        return ReviewValidationReport(
            report_id=f"validation_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}",
            created_at=datetime.now().isoformat(),
            total_citations=len(citation_sets),
            supported_count=sum(1 for item in citation_results if item.conclusion == ValidationConclusion.SUPPORTED),
            partial_support_count=sum(1 for item in citation_results if item.conclusion == ValidationConclusion.PARTIAL_SUPPORT),
            unsupported_count=sum(1 for item in citation_results if item.conclusion == ValidationConclusion.UNSUPPORTED),
            wrong_source_count=sum(1 for item in citation_results if item.conclusion == ValidationConclusion.WRONG_SOURCE),
            needs_review_count=sum(1 for item in citation_results if item.conclusion == ValidationConclusion.NEEDS_REVIEW),
            citation_results=citation_results,
        )

    def _get_block_from_review_draft(self, block_id: str) -> Optional[Dict[str, Any]]:
        sections = self.review_draft.get("content", {}).get("sections", [])
        for section in sections:
            for block in section.get("blocks", []):
                if block.get("block_id") == block_id:
                    return block
        return None

    def _get_occurrences_from_manifest(self) -> List[Dict[str, Any]]:
        occurrences = self.citation_manifest.get("occurrences", [])
        if occurrences:
            return occurrences
        return self.citation_manifest.get("citations", [])

    def _get_citation_sets_from_manifest(self) -> List[Dict[str, Any]]:
        bundles = self.citation_manifest.get("citation_sets", [])
        if bundles:
            return bundles

        fallback_bundles: Dict[str, Dict[str, Any]] = {}
        for occurrence in self._get_occurrences_from_manifest():
            paper_id = str(occurrence.get("paper_id") or "").strip()
            paper_key = str(occurrence.get("paper_key") or paper_id).strip()
            citation_set_key = normalize_citation_set_key([paper_id], [paper_key]) or "unknown"
            bundle = fallback_bundles.setdefault(
                citation_set_key,
                {
                    "bundle_id": str(occurrence.get("occurrence_id") or occurrence.get("citation_id") or f"bundle_{len(fallback_bundles) + 1}"),
                    "citation_set_key": citation_set_key,
                    "paper_ids": [paper_id] if paper_id else [],
                    "paper_keys": [paper_key] if paper_key else [],
                    "occurrence_ids": [],
                    "block_ids": [],
                    "section_numbers": [],
                    "section_titles": [],
                    "claim_texts": [],
                    "citation_tokens": [],
                },
            )
            occurrence_id = str(occurrence.get("occurrence_id") or occurrence.get("citation_id") or "").strip()
            if occurrence_id and occurrence_id not in bundle["occurrence_ids"]:
                bundle["occurrence_ids"].append(occurrence_id)
            block_id = str(occurrence.get("block_id") or "").strip()
            if block_id and block_id not in bundle["block_ids"]:
                bundle["block_ids"].append(block_id)
            section_number = int(occurrence.get("section_number") or 0)
            if section_number and section_number not in bundle["section_numbers"]:
                bundle["section_numbers"].append(section_number)
            section_title = str(occurrence.get("section_title") or "").strip()
            if section_title and section_title not in bundle["section_titles"]:
                bundle["section_titles"].append(section_title)
            claim_text = str(occurrence.get("context_before") or occurrence.get("context") or occurrence.get("text") or "").strip()
            if claim_text and claim_text not in bundle["claim_texts"]:
                bundle["claim_texts"].append(claim_text)
            citation_token = str(occurrence.get("citation_token") or occurrence.get("text") or "").strip()
            if citation_token and citation_token not in bundle["citation_tokens"]:
                bundle["citation_tokens"].append(citation_token)
        return list(fallback_bundles.values())

    def _resolver_context_for_paper(self, paper_id: str, paper_artifact: Dict[str, Any]) -> EvidenceResolverContext:
        paper_preprocess_evidence = self.preprocess_evidence.get(paper_id, {})
        paper_specific_metadata = self.paper_metadata.get(paper_id, {})
        evidence = self.evidence_loader.load_evidence(
            plain_text_path=paper_preprocess_evidence.get("plain_text_path"),
            page_index_path=paper_preprocess_evidence.get("page_index_path"),
            chunks_path=paper_preprocess_evidence.get("chunks_path"),
            structured_json_path=paper_preprocess_evidence.get("structured_json_path"),
            manifest_path=paper_preprocess_evidence.get("manifest_path"),
            visual_artifacts_path=paper_preprocess_evidence.get("visual_artifacts_path"),
            diagnostics_path=paper_preprocess_evidence.get("diagnostics_path"),
        )
        return EvidenceResolverContext(
            paper_key=paper_id,
            paper_identity=paper_artifact.get("paper_identity", {}),
            preprocess_artifacts={
                "normalized_text": evidence.normalized_text,
                "plain_text": evidence.plain_text,
                "page_index": evidence.page_index,
                "chunks": evidence.chunks,
                "structured_json": evidence.structured_json,
                "manifest": evidence.manifest,
                "visual_artifacts": evidence.visual_artifacts,
                "diagnostics": evidence.diagnostics,
            },
            paper_artifact=paper_artifact,
            preprocess_evidence=paper_preprocess_evidence,
            paper_metadata=paper_specific_metadata,
        )

    def _validate_citation_set(self, bundle: Dict[str, Any]) -> CitationValidationResult:
        citation_set_key = str(bundle.get("citation_set_key") or bundle.get("bundle_id") or "unknown")
        paper_ids = [str(item).strip() for item in bundle.get("paper_ids", []) if str(item).strip()]
        block_ids = [str(item).strip() for item in bundle.get("block_ids", []) if str(item).strip()]
        claim_texts = [str(item).strip() for item in bundle.get("claim_texts", []) if str(item).strip()]
        claim_context = "; ".join(str(item).strip() for item in bundle.get("section_titles", []) if str(item).strip())

        block_claims: List[str] = []
        for block_id in block_ids:
            block = self._get_block_from_review_draft(block_id)
            block_text = str(block.get("text") or "").strip() if block else ""
            if block_text:
                block_claims.append(block_text)
        used_block_text = bool(block_claims)
        claim_text = "\n".join(block_claims or claim_texts).strip()

        if not paper_ids:
            return CitationValidationResult(
                citation_id=str(bundle.get("bundle_id") or citation_set_key),
                paper_id=citation_set_key,
                conclusion=ValidationConclusion.WRONG_SOURCE,
                root_causes=[RootCause.CITATION_MAPPING_ERROR],
                evidence_candidates=[],
                details={
                    "citation_set_key": citation_set_key,
                    "bundle": bundle,
                    "reason": "empty_citation_set",
                    "used_block_text": used_block_text,
                },
                claim_text=claim_text,
                claim_context=claim_context,
                evidence_excerpt_list=[],
                reasoning_summary="The citation set could not be resolved to any source paper.",
                repair_hint="Check whether the citation tokens can still be mapped to real papers.",
                citation_set_key=citation_set_key,
                paper_ids=[],
                block_ids=block_ids,
                low_confidence=False,
            )

        evidence_candidates: List[EvidenceCandidate] = []
        missing_papers: List[str] = []
        per_paper_support: Dict[str, Dict[str, int]] = {}
        any_visual_refs = False

        for paper_id in paper_ids:
            paper_artifact = self.paper_artifacts.get(paper_id)
            if not paper_artifact:
                missing_papers.append(paper_id)
                continue

            resolver = EvidenceResolver(self._resolver_context_for_paper(paper_id, paper_artifact))
            selected_visual_refs = paper_artifact.get("stage1_inputs", {}).get("selected_visual_refs", []) or []
            any_visual_refs = any_visual_refs or bool(selected_visual_refs)
            candidates = resolver.resolve_evidence(
                cited_span=claim_text or str((bundle.get("citation_tokens") or [""])[0]),
                locator=None,
                selected_visual_refs=selected_visual_refs,
            )
            evidence_candidates.extend(candidates)
            per_paper_support[paper_id] = {
                "high": sum(1 for item in candidates if item.confidence >= 0.8),
                "medium": sum(1 for item in candidates if 0.5 <= item.confidence < 0.8),
            }

        evidence_excerpt_list = [item.text_excerpt for item in evidence_candidates if item.text_excerpt][:8]
        details: Dict[str, Any] = {
            "citation_set_key": citation_set_key,
            "paper_ids": paper_ids,
            "block_ids": block_ids,
            "bundle": bundle,
            "per_paper_support": per_paper_support,
            "missing_papers": missing_papers,
            "used_block_text": used_block_text,
        }

        if missing_papers:
            details["reason"] = "paper_not_found_in_artifacts"
            return CitationValidationResult(
                citation_id=str(bundle.get("bundle_id") or citation_set_key),
                paper_id=paper_ids[0] if len(paper_ids) == 1 else citation_set_key,
                conclusion=ValidationConclusion.WRONG_SOURCE,
                root_causes=[RootCause.CITATION_MAPPING_ERROR],
                evidence_candidates=evidence_candidates,
                details=details,
                claim_text=claim_text,
                claim_context=claim_context,
                evidence_excerpt_list=evidence_excerpt_list,
                reasoning_summary=f"{len(missing_papers)} cited paper(s) could not be resolved to validation artifacts.",
                repair_hint="Repair the citation-to-paper mapping first, then rerun validation.",
                citation_set_key=citation_set_key,
                paper_ids=paper_ids,
                block_ids=block_ids,
                low_confidence=False,
            )

        per_paper_high = [stats["high"] > 0 for stats in per_paper_support.values()]
        per_paper_medium = [stats["high"] > 0 or stats["medium"] > 0 for stats in per_paper_support.values()]
        visual_candidates = [item for item in evidence_candidates if item.evidence_scope == "visual"]

        if per_paper_high and all(per_paper_high):
            conclusion = ValidationConclusion.SUPPORTED
            root_causes: List[RootCause] = []
            reasoning = "Every paper in the exact citation set has high-confidence supporting evidence."
            repair_hint = ""
            low_confidence = False
        elif per_paper_medium and all(per_paper_medium):
            conclusion = ValidationConclusion.PARTIAL_SUPPORT
            root_causes = [RootCause.INSUFFICIENT_CONTEXT]
            reasoning = "Each cited paper has at least medium-confidence evidence, but the support is not strong enough for a full pass."
            repair_hint = "Make the claim more specific or reduce the strength of the wording."
            low_confidence = False
        elif visual_candidates and any_visual_refs:
            conclusion = ValidationConclusion.NEEDS_REVIEW
            root_causes = [RootCause.VISUAL_UNDERSTANDING_GAP, RootCause.LOW_CONFIDENCE]
            reasoning = "The bundle is supported mainly by visual evidence, so it should be kept for manual review."
            repair_hint = "Manually inspect the referenced figures or tables before editing the review."
            low_confidence = True
        else:
            conclusion = ValidationConclusion.UNSUPPORTED
            root_causes = [RootCause.INSUFFICIENT_CONTEXT]
            reasoning = "Not enough evidence was found for this exact citation set."
            repair_hint = "Check for miscitation, overclaiming, or rewrite the passage more conservatively."
            low_confidence = False

        return CitationValidationResult(
            citation_id=str(bundle.get("bundle_id") or citation_set_key),
            paper_id=paper_ids[0] if len(paper_ids) == 1 else citation_set_key,
            conclusion=conclusion,
            root_causes=root_causes,
            evidence_candidates=evidence_candidates,
            details=details,
            claim_text=claim_text,
            claim_context=claim_context,
            evidence_excerpt_list=evidence_excerpt_list,
            reasoning_summary=reasoning,
            repair_hint=repair_hint,
            citation_set_key=citation_set_key,
            paper_ids=paper_ids,
            block_ids=block_ids,
            low_confidence=low_confidence,
        )
