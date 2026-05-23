from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
import hashlib
import threading
from typing import Any, Callable, Dict, List, Optional, Sequence

from services.citation_manifest import normalize_citation_set_key
from . import PreprocessEvidenceLoader
from .evidence_resolver import (
    EvidenceCandidate,
    EvidenceResolver,
    EvidenceResolverContext,
    SOURCE_GROUNDED_RESOLVER_TIERS,
)


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


class EvidenceStatus(Enum):
    CLEAN_SUPPORTED = "clean_supported"
    EVIDENCE_GAP = "evidence_gap"
    UNSUPPORTED = "unsupported"
    WRONG_SOURCE = "wrong_source"
    NEEDS_REVIEW = "needs_review"


class ValidationDisposition(Enum):
    KEEP_AS_IS = "keep_as_is"
    NARROWED_AND_KEPT = "narrowed_and_kept"
    MANUAL_REVIEW = "manual_review"
    FAIL = "fail"
    SUMMARY_REPAIR = "summary_repair"
    REVIEW_REPAIR = "review_repair"
    BOTH_REPAIR = "both_repair"


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
    evidence_status: str = EvidenceStatus.EVIDENCE_GAP.value
    disposition: str = ValidationDisposition.KEEP_AS_IS.value
    block_context: str = ""
    claim_units: List[Dict[str, Any]] = field(default_factory=list)
    target_claim_unit: Dict[str, Any] = field(default_factory=dict)
    claim_type: str = ""
    claim_type_confidence: float = 0.0
    adjudication_status: str = ""
    adjudication_stage: str = "preflight"
    escalated: bool = False


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
    narrowed_and_kept_count: int = 0
    evidence_gap_count: int = 0


def _sentence_spans(block_text: str) -> List[tuple[int, int, str]]:
    text = block_text or ""
    spans: List[tuple[int, int, str]] = []
    start = 0
    for match in re.finditer(r"[。！？!?\.]+(?:\s+|$)", text):
        end = match.end()
        chunk = text[start:end].strip()
        if chunk:
            spans.append((start, end, chunk))
        start = end
    if start < len(text):
        chunk = text[start:].strip()
        if chunk:
            spans.append((start, len(text), chunk))
    if not spans and text.strip():
        spans.append((0, len(text), text.strip()))
    return spans


def _strip_citation_tokens(text: str) -> str:
    return " ".join(re.sub(r"\[\[cite:[^\]]+\]\]", "", text or "").split()).strip()


def _sentence_span_entries(block_text: str) -> List[Dict[str, Any]]:
    return [
        {
            "sentence_index": sentence_index,
            "span_start": span_start,
            "span_end": span_end,
            "text": sentence_text,
        }
        for sentence_index, (span_start, span_end, sentence_text) in enumerate(_sentence_spans(block_text), start=1)
    ]


_FUTURE_DIRECTION_HINTS = (
    "future research",
    "future studies",
    "future work",
    "further research",
    "should explore",
    "could explore",
    "in future",
    "future directions",
    "future direction",
    "后续研究",
    "未来研究",
    "未来方向",
)
_LIMITATION_METHOD_HINTS = (
    "limitation",
    "limitations",
    "limited by",
    "methodological",
    "methodology",
    "measurement",
    "sample size",
    "generalizability",
    "bias",
    "constraint",
    "局限",
    "方法",
)
_SYNTHESIS_HINTS = (
    "collectively",
    "together",
    "across studies",
    "overall",
    "the literature",
    "combined",
    "jointly",
    "synthes",
)


def _classify_claim_type(
    claim_text: str,
    claim_context: str,
    paper_count: int,
) -> tuple[str, float, str]:
    combined = f"{claim_context} {claim_text}".lower()
    if any(hint in combined for hint in _FUTURE_DIRECTION_HINTS):
        return ("future_direction", 0.9, "future-direction cue detected in claim or section context")
    if any(hint in combined for hint in _LIMITATION_METHOD_HINTS):
        return (
            "limitation_method_critique",
            0.85,
            "limitation or methodological critique cue detected in claim or section context",
        )
    if paper_count > 1 or any(hint in combined for hint in _SYNTHESIS_HINTS):
        rationale = "multi-paper citation set defaults to synthesis" if paper_count > 1 else "synthesis cue detected in claim text"
        return ("synthesis", 0.75 if paper_count > 1 else 0.65, rationale)
    return ("result", 0.6, "defaulted to result because no stronger claim-type cue matched")


def _serialize_evidence_candidate(
    candidate: EvidenceCandidate,
    *,
    paper_id: str,
    claim_unit_id: str,
) -> Dict[str, Any]:
    return {
        "paper_id": paper_id,
        "claim_unit_id": claim_unit_id,
        "match_reason": candidate.match_reason,
        "resolver_tier": candidate.resolver_tier,
        "confidence": candidate.confidence,
        "page_span": list(candidate.page_span or []),
        "chunk_ids": list(candidate.chunk_ids or []),
        "text_excerpt": candidate.text_excerpt,
        "negative_evidence_reason": candidate.negative_evidence_reason,
        "caption_excerpt": candidate.caption_excerpt,
        "visual_refs": list(candidate.visual_refs or []),
        "evidence_scope": candidate.evidence_scope,
        "source_grounded": candidate.resolver_tier in SOURCE_GROUNDED_RESOLVER_TIERS,
    }


def _split_claim_into_segments(claim_text: str, *, max_segments: int = 6) -> List[str]:
    text = " ".join(str(claim_text or "").split()).strip()
    if not text:
        return []

    segments: List[str] = [text]
    split_patterns = [
        r"[；;]+",
        r"(?:(?<=，)|(?<=,))\s*(?=(?:同时|此外|另外|然而|但|而且|并且|相反|且|而|并|也|进而|从而))",
    ]
    if len(text) >= 60:
        split_patterns.append(r"[，,]+")

    for pattern in split_patterns:
        if len(segments) > 1:
            break
        candidate_parts = [part.strip() for part in re.split(pattern, text) if part and part.strip()]
        filtered_parts = [part for part in candidate_parts if len(part) >= 8]
        if len(filtered_parts) > 1:
            segments = filtered_parts[:max_segments]

    deduped: List[str] = []
    for segment in segments:
        if segment and segment not in deduped:
            deduped.append(segment)
    return deduped or [text]


def _support_counts(candidates: Sequence[EvidenceCandidate]) -> Dict[str, int]:
    source_grounded = [item for item in candidates if item.resolver_tier in SOURCE_GROUNDED_RESOLVER_TIERS]
    return {
        "high": sum(1 for item in source_grounded if item.confidence >= 0.8),
        "medium": sum(1 for item in source_grounded if 0.5 <= item.confidence < 0.8),
    }


def _dedupe_evidence_candidates(candidates: Sequence[EvidenceCandidate]) -> List[EvidenceCandidate]:
    unique: Dict[tuple[Any, ...], EvidenceCandidate] = {}
    for candidate in candidates:
        key = (
            candidate.resolver_tier,
            candidate.match_reason,
            candidate.text_excerpt,
            tuple(candidate.page_span or []),
            tuple(candidate.chunk_ids or []),
            candidate.evidence_scope,
        )
        existing = unique.get(key)
        if existing is None or candidate.confidence > existing.confidence:
            unique[key] = candidate
    return sorted(unique.values(), key=lambda item: (-item.confidence, item.window_rank))


def _unique_non_empty(values: Sequence[Any]) -> List[str]:
    return list(dict.fromkeys(str(item).strip() for item in values if str(item).strip()))


def _claim_unit_alignment_status(claim_unit: Dict[str, Any]) -> str:
    status = str(claim_unit.get("alignment_status") or "").strip()
    return status if status in {"explicit", "inferred", "ambiguous", "legacy_fallback"} else "legacy_fallback"


def _claim_unit_alignment_confidence(claim_unit: Dict[str, Any]) -> float:
    try:
        return float(claim_unit.get("alignment_confidence") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _paper_ids_for_claim_unit(
    claim_unit: Dict[str, Any],
    *,
    bundle_paper_ids: Sequence[str],
) -> tuple[List[str], str]:
    alignment_status = _claim_unit_alignment_status(claim_unit)
    supporting_paper_ids = _unique_non_empty(claim_unit.get("supporting_paper_ids", []))
    if alignment_status == "ambiguous":
        return _unique_non_empty(bundle_paper_ids), "bundle_fallback"
    if alignment_status in {"explicit", "inferred"} and supporting_paper_ids:
        return supporting_paper_ids, "claim_unit_supporting_paper_ids"
    claim_unit_paper_ids = _unique_non_empty(claim_unit.get("paper_ids", []))
    if claim_unit_paper_ids:
        return claim_unit_paper_ids, "claim_unit_paper_ids"
    return _unique_non_empty(bundle_paper_ids), "bundle_fallback"


def _source_grounded_excerpts_for_paper_packets(
    per_paper_packets: Dict[str, Dict[str, List[Dict[str, Any]]]],
    *,
    paper_ids: Sequence[str],
    claim_unit_id: str,
    limit: int = 8,
) -> List[Dict[str, Any]]:
    excerpts: List[Dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for paper_id in paper_ids:
        for candidate in per_paper_packets.get(paper_id, {}).get(claim_unit_id, []):
            if not candidate.get("source_grounded"):
                continue
            text = str(candidate.get("text_excerpt") or candidate.get("caption_excerpt") or "").strip()
            if not text:
                continue
            key = (
                paper_id,
                candidate.get("resolver_tier"),
                text,
                tuple(candidate.get("page_span") or []),
                tuple(candidate.get("chunk_ids") or []),
            )
            if key in seen:
                continue
            seen.add(key)
            excerpts.append(dict(candidate))
            if len(excerpts) >= limit:
                return excerpts
    return excerpts


def _build_claim_unit(
    *,
    bundle: Dict[str, Any],
    block_id: str,
    sentence_index: int,
    span_start: Any,
    span_end: Any,
    claim_text: str,
    citation_tokens: List[str],
    block_anchor_hash: str,
    claim_unit_id: str,
    paper_ids: Optional[List[str]] = None,
) -> Dict[str, Any]:
    citation_set_key = str(bundle.get("citation_set_key") or bundle.get("bundle_id") or "unknown")
    resolved_paper_ids = list(paper_ids or bundle.get("paper_ids", []))
    claim_unit = {
        "claim_unit_id": claim_unit_id,
        "citation_set_key": citation_set_key,
        "validation_bundle_id": f"{citation_set_key}:{claim_unit_id}",
        "paper_ids": resolved_paper_ids,
        "block_id": block_id,
        "sentence_index": sentence_index,
        "span_start": span_start,
        "span_end": span_end,
        "claim_text": claim_text,
        "citation_tokens": citation_tokens,
        "block_anchor_hash": block_anchor_hash,
        "supporting_paper_ids": [],
        "supporting_paper_keys": [],
        "supporting_occurrence_ids": [],
        "alignment_status": "legacy_fallback",
        "alignment_confidence": 0.0,
    }
    return claim_unit


def _fallback_claim_unit(
    *,
    bundle: Dict[str, Any],
    citation_set_key: str,
    claim_text: str,
    block_ids: List[str],
) -> Dict[str, Any]:
    return _build_claim_unit(
        bundle=bundle,
        block_id=block_ids[0] if block_ids else "",
        sentence_index=1,
        span_start=None,
        span_end=None,
        claim_text=claim_text,
        citation_tokens=list(bundle.get("citation_tokens", [])),
        block_anchor_hash="",
        claim_unit_id=citation_set_key,
    )


def _paper_identity_hint(
    *,
    paper_id: str,
    paper_artifact: Dict[str, Any],
    paper_metadata: Dict[str, Any],
) -> Dict[str, Any]:
    paper_info = paper_artifact.get("paper_info", {})
    analysis = paper_artifact.get("analysis", {})
    analysis_metadata = analysis.get("paper_metadata", {})
    analysis_paper_info = analysis.get("paper_info", {})
    return {
        "paper_id": paper_id,
        "canonical_paper_key": paper_artifact.get("paper_identity", {}).get("canonical_paper_key", ""),
        "source_paper_id": paper_artifact.get("paper_identity", {}).get("source_paper_id", ""),
        "title": paper_info.get("title")
        or analysis_metadata.get("title")
        or analysis_paper_info.get("title")
        or paper_metadata.get("title", ""),
        "authors": paper_info.get("authors")
        or analysis_metadata.get("authors")
        or analysis_paper_info.get("authors")
        or [],
        "year": paper_info.get("year")
        or analysis_metadata.get("year")
        or analysis_paper_info.get("year")
        or "",
    }


def _compat_conclusion_for_state(
    evidence_status: str,
    disposition: str,
) -> ValidationConclusion:
    if evidence_status == EvidenceStatus.WRONG_SOURCE.value:
        return ValidationConclusion.WRONG_SOURCE
    if evidence_status == EvidenceStatus.CLEAN_SUPPORTED.value and disposition == ValidationDisposition.KEEP_AS_IS.value:
        return ValidationConclusion.SUPPORTED
    if disposition == ValidationDisposition.NARROWED_AND_KEPT.value:
        return ValidationConclusion.PARTIAL_SUPPORT
    if evidence_status == EvidenceStatus.NEEDS_REVIEW.value or disposition == ValidationDisposition.MANUAL_REVIEW.value:
        return ValidationConclusion.NEEDS_REVIEW
    if evidence_status == EvidenceStatus.EVIDENCE_GAP.value:
        return ValidationConclusion.PARTIAL_SUPPORT
    return ValidationConclusion.UNSUPPORTED


def _build_review_validation_report(citation_results: List[CitationValidationResult]) -> ReviewValidationReport:
    return ReviewValidationReport(
        report_id=f"validation_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}",
        created_at=datetime.now().isoformat(),
        total_citations=len(citation_results),
        supported_count=sum(
            1
            for item in citation_results
            if item.evidence_status == EvidenceStatus.CLEAN_SUPPORTED.value
            and item.disposition == ValidationDisposition.KEEP_AS_IS.value
        ),
        partial_support_count=sum(1 for item in citation_results if item.conclusion == ValidationConclusion.PARTIAL_SUPPORT),
        unsupported_count=sum(1 for item in citation_results if item.conclusion == ValidationConclusion.UNSUPPORTED),
        wrong_source_count=sum(1 for item in citation_results if item.conclusion == ValidationConclusion.WRONG_SOURCE),
        needs_review_count=sum(1 for item in citation_results if item.conclusion == ValidationConclusion.NEEDS_REVIEW),
        citation_results=citation_results,
        narrowed_and_kept_count=sum(1 for item in citation_results if item.disposition == ValidationDisposition.NARROWED_AND_KEPT.value),
        evidence_gap_count=sum(1 for item in citation_results if item.evidence_status == EvidenceStatus.EVIDENCE_GAP.value),
    )


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
        self._resolver_context_cache: Dict[str, EvidenceResolverContext] = {}
        self._resolver_context_cache_lock = threading.Lock()

    def validate(
        self,
        progress_callback: Optional[Callable[[int, int, Dict[str, Any]], None]] = None,
        max_workers: int = 1,
    ) -> ReviewValidationReport:
        citation_sets = self._get_citation_sets_from_manifest()
        total = len(citation_sets)
        try:
            requested_workers = int(max_workers or 1)
        except (TypeError, ValueError):
            requested_workers = 1
        worker_count = min(max(1, requested_workers), max(total, 1))
        if worker_count <= 1 or total <= 1:
            citation_results: List[CitationValidationResult] = []
            for index, bundle in enumerate(citation_sets, start=1):
                if progress_callback is not None:
                    progress_callback(index, total, bundle)
                citation_results.append(self._validate_citation_set(bundle))
            return _build_review_validation_report(citation_results)

        ordered_results: List[Optional[CitationValidationResult]] = [None] * total
        completed = 0
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            future_to_bundle = {
                executor.submit(self._validate_citation_set, bundle): (index, bundle)
                for index, bundle in enumerate(citation_sets)
            }
            for future in as_completed(future_to_bundle):
                index, bundle = future_to_bundle[future]
                ordered_results[index] = future.result()
                completed += 1
                if progress_callback is not None:
                    progress_callback(completed, total, bundle)

        citation_results = [item for item in ordered_results if item is not None]
        return _build_review_validation_report(citation_results)

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

    def _build_claim_units_for_bundle(self, bundle: Dict[str, Any]) -> List[Dict[str, Any]]:
        claim_units = [dict(item) for item in bundle.get("claim_units", []) if isinstance(item, dict)]
        if claim_units:
            return claim_units

        claim_texts = [str(item).strip() for item in bundle.get("claim_texts", []) if str(item).strip()]
        block_ids = [str(item).strip() for item in bundle.get("block_ids", []) if str(item).strip()]
        claim_units = []

        for block_id in block_ids:
            block = self._get_block_from_review_draft(block_id)
            block_text = str(block.get("text") or "").strip() if block else ""
            if not block_text:
                continue
            block_anchor_hash = str(block.get("anchor_hash") or "")
            sentences = block.get("span_map", {}).get("sentences", []) if block else []
            if not sentences:
                sentences = _sentence_span_entries(block_text)

            for sentence in sentences:
                cleaned_sentence = _strip_citation_tokens(str(sentence.get("text") or ""))
                if claim_texts and cleaned_sentence not in claim_texts:
                    continue
                claim_unit_id = hashlib.sha256(
                    f"{block_id}:{sentence.get('sentence_index', len(claim_units) + 1)}:{cleaned_sentence}".encode("utf-8")
                ).hexdigest()[:16]
                claim_units.append(
                    _build_claim_unit(
                        bundle=bundle,
                        block_id=block_id,
                        sentence_index=int(sentence.get("sentence_index") or len(claim_units) + 1),
                        span_start=sentence.get("span_start"),
                        span_end=sentence.get("span_end"),
                        claim_text=cleaned_sentence,
                        citation_tokens=list(bundle.get("citation_tokens", [])),
                        block_anchor_hash=block_anchor_hash,
                        claim_unit_id=claim_unit_id,
                    )
                )

        if claim_units:
            return claim_units

        for claim_index, claim_text in enumerate(claim_texts, start=1):
            claim_units.append(
                _build_claim_unit(
                    bundle=bundle,
                    block_id=block_ids[0] if block_ids else "",
                    sentence_index=claim_index,
                    span_start=None,
                    span_end=None,
                    claim_text=claim_text,
                    citation_tokens=list(bundle.get("citation_tokens", [])),
                    block_anchor_hash="",
                    claim_unit_id=f"{bundle.get('citation_set_key', 'unknown')}:{claim_index}",
                )
            )
        return claim_units

    def _resolver_context_for_paper(self, paper_id: str, paper_artifact: Dict[str, Any]) -> EvidenceResolverContext:
        with self._resolver_context_cache_lock:
            cached_context = self._resolver_context_cache.get(paper_id)
        if cached_context is not None:
            return cached_context

        paper_preprocess_evidence = self.preprocess_evidence.get(paper_id, {}) or paper_artifact.get("analysis", {}).get("preprocess", {})
        paper_specific_metadata = self.paper_metadata.get(paper_id, {})
        evidence = self.evidence_loader.load_evidence(
            normalized_text_path=paper_preprocess_evidence.get("markdown_path"),
            plain_text_path=paper_preprocess_evidence.get("plain_text_path"),
            page_index_path=paper_preprocess_evidence.get("page_index_path"),
            chunks_path=paper_preprocess_evidence.get("chunks_path"),
            structured_json_path=paper_preprocess_evidence.get("structured_json_path"),
            manifest_path=paper_preprocess_evidence.get("manifest_path") or paper_preprocess_evidence.get("prepare_manifest_path"),
            visual_artifacts_path=paper_preprocess_evidence.get("visual_artifacts_path"),
            diagnostics_path=paper_preprocess_evidence.get("diagnostics_path"),
        )
        context = EvidenceResolverContext(
            paper_key=paper_id,
            paper_identity=paper_artifact.get("paper_identity", {}),
            preprocess_artifacts={
                "normalized_text": paper_preprocess_evidence.get("normalized_text") or evidence.normalized_text,
                "plain_text": paper_preprocess_evidence.get("plain_text") or evidence.plain_text,
                "page_index": paper_preprocess_evidence.get("page_index") or evidence.page_index,
                "chunks": paper_preprocess_evidence.get("chunks") or evidence.chunks,
                "structured_json": paper_preprocess_evidence.get("structured_json") or evidence.structured_json,
                "manifest": paper_preprocess_evidence.get("manifest") or evidence.manifest,
                "visual_artifacts": paper_preprocess_evidence.get("visual_artifacts") or evidence.visual_artifacts,
                "diagnostics": paper_preprocess_evidence.get("diagnostics") or evidence.diagnostics,
            },
            paper_artifact=paper_artifact,
            preprocess_evidence=paper_preprocess_evidence,
            paper_metadata=paper_specific_metadata,
        )
        with self._resolver_context_cache_lock:
            return self._resolver_context_cache.setdefault(paper_id, context)

    def _validate_citation_set(self, bundle: Dict[str, Any]) -> CitationValidationResult:
        citation_set_key = str(bundle.get("citation_set_key") or bundle.get("bundle_id") or "unknown")
        paper_ids = [str(item).strip() for item in bundle.get("paper_ids", []) if str(item).strip()]
        block_ids = [str(item).strip() for item in bundle.get("block_ids", []) if str(item).strip()]
        claim_texts = [str(item).strip() for item in bundle.get("claim_texts", []) if str(item).strip()]
        claim_context = "; ".join(str(item).strip() for item in bundle.get("section_titles", []) if str(item).strip())
        claim_units = self._build_claim_units_for_bundle(bundle)
        used_block_text = False
        claim_text = "\n".join(claim_texts or [item.get("claim_text", "") for item in claim_units]).strip()
        claim_type, claim_type_confidence, claim_type_rationale = _classify_claim_type(
            claim_text,
            claim_context,
            len(paper_ids),
        )

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
                    "claim_type": claim_type,
                    "claim_type_confidence": claim_type_confidence,
                    "claim_type_rationale": claim_type_rationale,
                    "adjudication_status": "preflight",
                    "adjudication_stage": "preflight",
                    "escalated": False,
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
                evidence_status=EvidenceStatus.WRONG_SOURCE.value,
                disposition=ValidationDisposition.FAIL.value,
                claim_units=claim_units,
                claim_type=claim_type,
                claim_type_confidence=claim_type_confidence,
                adjudication_status="preflight",
                adjudication_stage="preflight",
                escalated=False,
            )

        evidence_candidates: List[EvidenceCandidate] = []
        missing_papers: List[str] = []
        claim_unit_results: List[Dict[str, Any]] = []
        paper_identity_hints: Dict[str, Dict[str, Any]] = {}
        per_paper_evidence_packets: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}
        expected_supporting_paper_ids_all: List[str] = []
        checked_paper_ids_all: List[str] = []
        contributing_paper_ids_all: List[str] = []

        claim_units_to_validate = claim_units or [
            _fallback_claim_unit(
                bundle=bundle,
                citation_set_key=citation_set_key,
                claim_text=claim_text,
                block_ids=block_ids,
            )
        ]
        for claim_unit in claim_units_to_validate:
            unit_claim_text = str(claim_unit.get("claim_text") or "").strip()
            unit_evidence_candidates: List[EvidenceCandidate] = []
            unit_missing_papers: List[str] = []
            per_paper_support: Dict[str, Dict[str, Any]] = {}
            any_visual_refs = False
            claim_unit_id = str(claim_unit.get("claim_unit_id") or citation_set_key)
            alignment_status = _claim_unit_alignment_status(claim_unit)
            alignment_confidence = _claim_unit_alignment_confidence(claim_unit)
            expected_supporting_paper_ids = _unique_non_empty(claim_unit.get("supporting_paper_ids", []))
            pooled_paper_ids = _unique_non_empty(claim_unit.get("pooled_paper_ids", []))
            unit_paper_ids, paper_resolution_source = _paper_ids_for_claim_unit(
                claim_unit,
                bundle_paper_ids=paper_ids,
            )
            checked_paper_ids = [] if alignment_status == "ambiguous" else list(unit_paper_ids)
            identity_check_paper_ids = _unique_non_empty(
                (pooled_paper_ids or unit_paper_ids) if alignment_status == "ambiguous" else checked_paper_ids
            )
            claim_segments = _split_claim_into_segments(unit_claim_text) if len(checked_paper_ids) > 1 else ([unit_claim_text] if unit_claim_text else [])
            segment_coverages: List[Dict[str, Any]] = []
            if not claim_segments and unit_claim_text:
                claim_segments = [unit_claim_text]
            for segment_index, segment_text in enumerate(claim_segments, start=1):
                segment_coverages.append(
                    {
                        "segment_id": f"{claim_unit_id}:{segment_index}",
                        "segment_index": segment_index,
                        "text": segment_text,
                        "supported_by_high": [],
                        "supported_by_medium": [],
                    }
                )

            for paper_id in identity_check_paper_ids:
                if paper_id == "unknown" or paper_id not in self.paper_artifacts:
                    unit_missing_papers.append(paper_id)

            for paper_id in checked_paper_ids:
                if paper_id in unit_missing_papers:
                    continue
                paper_artifact = self.paper_artifacts.get(paper_id)
                paper_identity_hints.setdefault(
                    paper_id,
                    _paper_identity_hint(
                        paper_id=paper_id,
                        paper_artifact=paper_artifact,
                        paper_metadata=self.paper_metadata.get(paper_id, {}),
                    ),
                )

                resolver = EvidenceResolver(self._resolver_context_for_paper(paper_id, paper_artifact))
                selected_visual_refs = paper_artifact.get("stage1_inputs", {}).get("selected_visual_refs", []) or []
                any_visual_refs = any_visual_refs or bool(selected_visual_refs)
                whole_claim_candidates = resolver.resolve_evidence(
                    cited_span=unit_claim_text or str((bundle.get("citation_tokens") or [""])[0]),
                    locator=None,
                    selected_visual_refs=selected_visual_refs,
                )
                segment_support: List[Dict[str, Any]] = []
                paper_candidates: List[EvidenceCandidate] = list(whole_claim_candidates)
                for coverage in segment_coverages:
                    segment_text = str(coverage.get("text") or "").strip()
                    segment_candidates = resolver.resolve_evidence(
                        cited_span=segment_text or unit_claim_text or str((bundle.get("citation_tokens") or [""])[0]),
                        locator=None,
                        selected_visual_refs=selected_visual_refs,
                    )
                    paper_candidates.extend(segment_candidates)
                    support = _support_counts(segment_candidates)
                    segment_support.append(
                        {
                            "segment_id": coverage["segment_id"],
                            "segment_index": coverage["segment_index"],
                            "text": segment_text,
                            "high": support["high"],
                            "medium": support["medium"],
                        }
                    )
                    if support["high"] > 0:
                        coverage["supported_by_high"].append(paper_id)
                    elif support["medium"] > 0:
                        coverage["supported_by_medium"].append(paper_id)

                whole_claim_support = _support_counts(whole_claim_candidates)
                paper_candidates = _dedupe_evidence_candidates(paper_candidates)
                unit_evidence_candidates.extend(paper_candidates)
                per_paper_evidence_packets.setdefault(paper_id, {})[claim_unit_id] = [
                    _serialize_evidence_candidate(
                        candidate,
                        paper_id=paper_id,
                        claim_unit_id=claim_unit_id,
                    )
                    for candidate in paper_candidates
                ]
                per_paper_support[paper_id] = {
                    "whole_claim": whole_claim_support,
                    "segments": segment_support,
                    "high": whole_claim_support["high"],
                    "medium": whole_claim_support["medium"],
                    "supports_any_segment_high": any(item["high"] > 0 for item in segment_support),
                    "supports_any_segment_medium": any((item["high"] > 0 or item["medium"] > 0) for item in segment_support),
                }

            unit_evidence_status = EvidenceStatus.EVIDENCE_GAP.value
            unit_disposition = ValidationDisposition.MANUAL_REVIEW.value
            unit_root_causes: List[RootCause] = [RootCause.INSUFFICIENT_CONTEXT]
            unit_low_confidence = False
            reason = "insufficient_source_grounded_evidence"

            if unit_missing_papers:
                unit_evidence_status = EvidenceStatus.WRONG_SOURCE.value
                unit_disposition = ValidationDisposition.FAIL.value
                unit_root_causes = [RootCause.CITATION_MAPPING_ERROR]
                reason = "paper_not_found_in_artifacts"
            elif alignment_status == "ambiguous":
                unit_evidence_status = EvidenceStatus.NEEDS_REVIEW.value
                unit_disposition = ValidationDisposition.MANUAL_REVIEW.value
                unit_root_causes = [RootCause.INSUFFICIENT_CONTEXT, RootCause.LOW_CONFIDENCE]
                unit_low_confidence = True
                reason = "ambiguous_claim_paper_alignment"
            else:
                per_paper_high = [stats["high"] > 0 for stats in per_paper_support.values()]
                per_paper_medium = [stats["high"] > 0 or stats["medium"] > 0 for stats in per_paper_support.values()]
                visual_candidates = [item for item in unit_evidence_candidates if item.evidence_scope == "visual"]
                fully_high_covered = bool(segment_coverages) and all(bool(item["supported_by_high"]) for item in segment_coverages)
                fully_medium_covered = bool(segment_coverages) and all(
                    bool(item["supported_by_high"] or item["supported_by_medium"]) for item in segment_coverages
                )
                paper_contributes_medium = all(bool(stats.get("supports_any_segment_high") or stats.get("supports_any_segment_medium")) for stats in per_paper_support.values())
                partially_covered = any(bool(item["supported_by_high"] or item["supported_by_medium"]) for item in segment_coverages)

                if len(checked_paper_ids) > 1 and len(segment_coverages) > 1 and fully_high_covered and paper_contributes_medium:
                    unit_evidence_status = EvidenceStatus.CLEAN_SUPPORTED.value
                    unit_disposition = ValidationDisposition.KEEP_AS_IS.value
                    unit_root_causes = []
                    reason = "source_grounded_support"
                elif len(checked_paper_ids) > 1 and len(segment_coverages) > 1 and fully_medium_covered:
                    unit_evidence_status = EvidenceStatus.EVIDENCE_GAP.value
                    unit_disposition = ValidationDisposition.REVIEW_REPAIR.value if paper_contributes_medium else ValidationDisposition.MANUAL_REVIEW.value
                    unit_root_causes = [RootCause.INSUFFICIENT_CONTEXT]
                    reason = "partial_source_grounded_support"
                elif len(checked_paper_ids) > 1 and len(segment_coverages) > 1 and partially_covered:
                    unit_evidence_status = EvidenceStatus.EVIDENCE_GAP.value
                    unit_disposition = ValidationDisposition.MANUAL_REVIEW.value
                    unit_root_causes = [RootCause.INSUFFICIENT_CONTEXT]
                elif per_paper_high and all(per_paper_high):
                    unit_evidence_status = EvidenceStatus.CLEAN_SUPPORTED.value
                    unit_disposition = ValidationDisposition.KEEP_AS_IS.value
                    unit_root_causes = []
                    reason = "source_grounded_support"
                elif len(checked_paper_ids) == 1 and per_paper_medium and all(per_paper_medium) and claim_type != "future_direction":
                    unit_evidence_status = EvidenceStatus.CLEAN_SUPPORTED.value
                    unit_disposition = ValidationDisposition.KEEP_AS_IS.value
                    unit_root_causes = []
                    reason = "source_grounded_support"
                elif visual_candidates and any_visual_refs:
                    unit_evidence_status = EvidenceStatus.NEEDS_REVIEW.value
                    unit_disposition = ValidationDisposition.MANUAL_REVIEW.value
                    unit_root_causes = [RootCause.VISUAL_UNDERSTANDING_GAP, RootCause.LOW_CONFIDENCE]
                    unit_low_confidence = True
                    reason = "visual_evidence_needs_review"
                elif any(per_paper_medium):
                    unit_evidence_status = EvidenceStatus.EVIDENCE_GAP.value
                    unit_disposition = ValidationDisposition.REVIEW_REPAIR.value
                    unit_root_causes = [RootCause.INSUFFICIENT_CONTEXT]
                    reason = "partial_source_grounded_support"
                else:
                    unit_evidence_status = EvidenceStatus.EVIDENCE_GAP.value
                    unit_disposition = ValidationDisposition.MANUAL_REVIEW.value
                    unit_root_causes = [RootCause.INSUFFICIENT_CONTEXT]

            contributing_paper_ids = [
                paper_id
                for paper_id, stats in per_paper_support.items()
                if bool(stats.get("high") or stats.get("medium") or stats.get("supports_any_segment_high") or stats.get("supports_any_segment_medium"))
            ]
            unsupported_expected_paper_ids = [
                paper_id
                for paper_id in (expected_supporting_paper_ids or checked_paper_ids)
                if paper_id not in contributing_paper_ids
            ]
            source_grounded_evidence_excerpts = _source_grounded_excerpts_for_paper_packets(
                per_paper_evidence_packets,
                paper_ids=checked_paper_ids,
                claim_unit_id=claim_unit_id,
            )
            expected_supporting_paper_ids_all.extend(expected_supporting_paper_ids)
            checked_paper_ids_all.extend(checked_paper_ids)
            contributing_paper_ids_all.extend(contributing_paper_ids)
            claim_unit_results.append(
                {
                    "claim_unit_id": claim_unit_id,
                    "validation_bundle_id": claim_unit.get("validation_bundle_id", citation_set_key),
                    "citation_set_key": claim_unit.get("citation_set_key", citation_set_key),
                    "paper_ids": list(claim_unit.get("paper_ids", paper_ids)),
                    "checked_paper_ids": checked_paper_ids,
                    "expected_supporting_paper_ids": expected_supporting_paper_ids,
                    "unsupported_expected_paper_ids": unsupported_expected_paper_ids,
                    "contributing_paper_ids": contributing_paper_ids,
                    "alignment_status": alignment_status,
                    "alignment_confidence": alignment_confidence,
                    "paper_resolution_source": paper_resolution_source,
                    "reason": reason,
                    "evidence_excerpts": source_grounded_evidence_excerpts,
                    "pooled_paper_ids": pooled_paper_ids,
                    "pooled_occurrence_ids": _unique_non_empty(claim_unit.get("pooled_occurrence_ids", [])),
                    "block_id": claim_unit.get("block_id", block_ids[0] if block_ids else ""),
                    "sentence_index": claim_unit.get("sentence_index", 1),
                    "span_start": claim_unit.get("span_start"),
                    "span_end": claim_unit.get("span_end"),
                    "claim_text": unit_claim_text,
                    "evidence_status": unit_evidence_status,
                    "disposition": unit_disposition,
                    "root_causes": [item.value for item in unit_root_causes],
                    "per_paper_support": per_paper_support,
                    "segment_coverages": segment_coverages,
                    "missing_papers": unit_missing_papers,
                    "low_confidence": unit_low_confidence,
                }
            )
            evidence_candidates.extend(unit_evidence_candidates)
            missing_papers.extend(unit_missing_papers)

        evidence_excerpt_list = [item.text_excerpt for item in evidence_candidates if item.text_excerpt][:8]
        missing_papers = list(dict.fromkeys(missing_papers))
        expected_supporting_paper_ids_all = list(dict.fromkeys(expected_supporting_paper_ids_all))
        checked_paper_ids_all = list(dict.fromkeys(checked_paper_ids_all))
        contributing_paper_ids_all = list(dict.fromkeys(contributing_paper_ids_all))
        target_claim_unit = next(
            (
                unit
                for unit in claim_units
                if any(
                    item.get("claim_unit_id") == unit.get("claim_unit_id")
                    and item.get("disposition") != ValidationDisposition.KEEP_AS_IS.value
                    for item in claim_unit_results
                )
            ),
            claim_units[0] if claim_units else {},
        )
        target_block_id = str(target_claim_unit.get("block_id") or (block_ids[0] if block_ids else "")).strip()
        target_block = self._get_block_from_review_draft(target_block_id) if target_block_id else None
        block_context = str(target_block.get("text") or "").strip() if target_block else ""
        details: Dict[str, Any] = {
            "citation_set_key": citation_set_key,
            "paper_ids": paper_ids,
            "checked_paper_ids": checked_paper_ids_all,
            "expected_supporting_paper_ids": expected_supporting_paper_ids_all,
            "unsupported_expected_paper_ids": [
                paper_id for paper_id in expected_supporting_paper_ids_all if paper_id not in contributing_paper_ids_all
            ],
            "contributing_paper_ids": contributing_paper_ids_all,
            "block_ids": block_ids,
            "bundle": bundle,
            "claim_units": claim_units,
            "target_claim_unit": target_claim_unit,
            "claim_unit_results": claim_unit_results,
            "paper_identity_hints": paper_identity_hints,
            "per_paper_evidence_packets": per_paper_evidence_packets,
            "missing_papers": missing_papers,
            "used_block_text": used_block_text,
            "block_context": block_context,
            "claim_type": claim_type,
            "claim_type_confidence": claim_type_confidence,
            "claim_type_rationale": claim_type_rationale,
            "adjudication_status": "preflight",
            "adjudication_stage": "preflight",
            "escalated": False,
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
                evidence_status=EvidenceStatus.WRONG_SOURCE.value,
                disposition=ValidationDisposition.FAIL.value,
                block_context=block_context,
                claim_units=claim_units,
                target_claim_unit=target_claim_unit,
                claim_type=claim_type,
                claim_type_confidence=claim_type_confidence,
                adjudication_status="preflight",
                adjudication_stage="preflight",
                escalated=False,
            )

        aggregate_statuses = {item["evidence_status"] for item in claim_unit_results}
        aggregate_dispositions = {item["disposition"] for item in claim_unit_results}
        low_confidence = any(bool(item.get("low_confidence")) for item in claim_unit_results)

        evidence_status = EvidenceStatus.EVIDENCE_GAP.value
        disposition = ValidationDisposition.MANUAL_REVIEW.value
        root_causes: List[RootCause] = [RootCause.INSUFFICIENT_CONTEXT]
        reasoning = "The citation set contains claim units that need narrower, better-grounded validation."
        repair_hint = "Narrow the claim to the smallest proposition directly supported by the cited evidence."

        if EvidenceStatus.WRONG_SOURCE.value in aggregate_statuses:
            evidence_status = EvidenceStatus.WRONG_SOURCE.value
            disposition = ValidationDisposition.FAIL.value
            root_causes = [RootCause.CITATION_MAPPING_ERROR]
            reasoning = "At least one claim unit could not be mapped to the cited paper artifacts."
            repair_hint = "Repair the citation-to-paper mapping before attempting review repair."
        elif all(
            item["evidence_status"] == EvidenceStatus.CLEAN_SUPPORTED.value
            and item["disposition"] == ValidationDisposition.KEEP_AS_IS.value
            for item in claim_unit_results
        ):
            evidence_status = EvidenceStatus.CLEAN_SUPPORTED.value
            disposition = ValidationDisposition.KEEP_AS_IS.value
            root_causes = []
            reasoning = "Every claim unit in the exact citation set has strong supporting evidence."
            repair_hint = ""
        elif ValidationDisposition.REVIEW_REPAIR.value in aggregate_dispositions:
            evidence_status = EvidenceStatus.EVIDENCE_GAP.value
            disposition = ValidationDisposition.REVIEW_REPAIR.value
            root_causes = [RootCause.INSUFFICIENT_CONTEXT]
            reasoning = "Some claim units have partial support and should be narrowed before being kept."
            repair_hint = "Rewrite only the targeted claim unit more conservatively while preserving the block structure."
        elif EvidenceStatus.NEEDS_REVIEW.value in aggregate_statuses or ValidationDisposition.MANUAL_REVIEW.value in aggregate_dispositions:
            evidence_status = EvidenceStatus.NEEDS_REVIEW.value if EvidenceStatus.NEEDS_REVIEW.value in aggregate_statuses else EvidenceStatus.EVIDENCE_GAP.value
            disposition = ValidationDisposition.MANUAL_REVIEW.value
            root_causes = [RootCause.VISUAL_UNDERSTANDING_GAP, RootCause.LOW_CONFIDENCE] if EvidenceStatus.NEEDS_REVIEW.value in aggregate_statuses else [RootCause.INSUFFICIENT_CONTEXT]
            reasoning = "The available evidence is not strong enough for safe automatic narrowing."
            repair_hint = "Review the cited source manually or improve the evidence retrieval bundle."

        conclusion = _compat_conclusion_for_state(evidence_status, disposition)

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
            evidence_status=evidence_status,
            disposition=disposition,
            block_context=block_context,
            claim_units=claim_units,
            target_claim_unit=target_claim_unit,
            claim_type=claim_type,
            claim_type_confidence=claim_type_confidence,
            adjudication_status="preflight",
            adjudication_stage="preflight",
            escalated=False,
        )
