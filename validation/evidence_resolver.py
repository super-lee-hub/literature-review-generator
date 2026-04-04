from __future__ import annotations

import os
import json
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional, Sequence


@dataclass(frozen=True)
class EvidenceCandidate:
    match_reason: str
    resolver_tier: str
    window_rank: int
    confidence: float
    artifact_path: str
    page_span: Optional[List[int]]
    chunk_ids: Optional[List[str]]
    text_excerpt: str
    negative_evidence_reason: Optional[str]
    visual_refs: Optional[List[Dict[str, Any]]]
    caption_excerpt: Optional[str]
    evidence_scope: str


@dataclass
class EvidenceResolverContext:
    paper_key: str
    paper_identity: Dict[str, Any]
    preprocess_artifacts: Dict[str, Any]
    paper_artifact: Dict[str, Any]


class EvidenceResolver:
    def __init__(self, context: EvidenceResolverContext):
        self.context = context

    def resolve_evidence(
        self,
        cited_span: str,
        locator: Optional[str] = None,
        selected_visual_refs: Optional[List[Dict[str, Any]]] = None,
    ) -> List[EvidenceCandidate]:
        candidates: List[EvidenceCandidate] = []

        # Explicit tier order as required
        # Tier 1: Locator + Page Index
        candidates.extend(self._resolve_from_locator_page_index(cited_span, locator))
        # Tier 2: Preprocess chunks
        candidates.extend(self._resolve_from_preprocess_chunks(cited_span))
        # Tier 3: Normalized text
        candidates.extend(self._resolve_from_normalized_text(cited_span))
        # Tier 4: Plain text fallback
        candidates.extend(self._resolve_from_plain_text(cited_span))
        # Tier 5: Visual refs
        if selected_visual_refs:
            candidates.extend(self._resolve_from_visual_refs(selected_visual_refs, cited_span))

        # Now add negative evidence if no candidates were found
        if not candidates:
            candidates.append(self._create_negative_evidence_candidate(cited_span))

        # Sort by confidence (descending) and window_rank (ascending)
        candidates.sort(key=lambda x: (-x.confidence, x.window_rank))
        return candidates

    def _create_negative_evidence_candidate(self, cited_span: str) -> EvidenceCandidate:
        """Create a negative evidence candidate with clear reason."""
        # Determine the reason based on what artifacts were available
        available_artifacts = []
        if self.context.preprocess_artifacts.get("chunks"):
            available_artifacts.append("preprocess_chunks")
        if self.context.preprocess_artifacts.get("normalized_text"):
            available_artifacts.append("normalized_text")
        if self.context.preprocess_artifacts.get("plain_text"):
            available_artifacts.append("plain_text")
        
        if not available_artifacts:
            reason = "no_preprocess_artifacts_available"
        else:
            reason = f"cited_text_not_found_in_any_tier"
        
        return EvidenceCandidate(
            match_reason="negative_evidence",
            resolver_tier="negative",
            window_rank=0,
            confidence=0.0,
            artifact_path=self.context.paper_artifact.get("source", {}).get("source_pdf", ""),
            page_span=None,
            chunk_ids=None,
            text_excerpt="",
            negative_evidence_reason=reason,
            visual_refs=None,
            caption_excerpt=None,
            evidence_scope="negative",
        )

    def _calculate_confidence(self, cited_span: str, context_text: str) -> float:
        """Calculate confidence based on text matching quality."""
        if not cited_span or not context_text:
            return 0.0
        
        # Base confidence for exact match
        base_confidence = 0.8
        
        # Adjust based on cited span length relative to context
        span_length = len(cited_span)
        context_length = len(context_text)
        
        # Longer spans relative to context give higher confidence
        length_ratio = min(span_length / max(context_length, 1), 1.0)
        length_bonus = length_ratio * 0.15
        
        # Adjust based on how early the match appears in context
        match_pos = context_text.lower().find(cited_span.lower())
        if match_pos == 0:
            position_bonus = 0.05
        elif match_pos < len(context_text) * 0.2:
            position_bonus = 0.03
        else:
            position_bonus = 0.0
        
        # Calculate final confidence (capped at 0.95 to leave room for higher tiers)
        confidence = base_confidence + length_bonus + position_bonus
        return min(confidence, 0.95)

    def _resolve_from_locator_page_index(self, cited_span: str, locator: Optional[str]) -> List[EvidenceCandidate]:
        """Resolve evidence from locator/page index if available."""
        candidates: List[EvidenceCandidate] = []
        page_index = self.context.preprocess_artifacts.get("page_index", [])
        
        if locator:
            # If we have a locator, try to find relevant pages
            for idx, page_entry in enumerate(page_index):
                page_text = page_entry.get("text", "")
                if cited_span.lower() in page_text.lower():
                    confidence = self._calculate_confidence(cited_span, page_text)
                    confidence = min(confidence + 0.05, 0.98)  # Page index with locator gets a small boost
                    candidates.append(
                        EvidenceCandidate(
                            match_reason="page_index_locator_match",
                            resolver_tier="locator_page_index",
                            window_rank=idx,
                            confidence=confidence,
                            artifact_path=self.context.paper_artifact.get("source", {}).get("source_pdf", ""),
                            page_span=page_entry.get("page_range", [page_entry.get("page_number", 0)]),
                            chunk_ids=None,
                            text_excerpt=page_text[:500],
                            negative_evidence_reason=None,
                            visual_refs=None,
                            caption_excerpt=None,
                            evidence_scope="page",
                        )
                    )
        
        # Also check page index without locator for matches
        if not locator:
            for idx, page_entry in enumerate(page_index):
                page_text = page_entry.get("text", "")
                if cited_span.lower() in page_text.lower():
                    confidence = self._calculate_confidence(cited_span, page_text)
                    candidates.append(
                        EvidenceCandidate(
                            match_reason="page_index_match",
                            resolver_tier="locator_page_index",
                            window_rank=idx,
                            confidence=confidence,
                            artifact_path=self.context.paper_artifact.get("source", {}).get("source_pdf", ""),
                            page_span=page_entry.get("page_range", [page_entry.get("page_number", 0)]),
                            chunk_ids=None,
                            text_excerpt=page_text[:500],
                            negative_evidence_reason=None,
                            visual_refs=None,
                            caption_excerpt=None,
                            evidence_scope="page",
                        )
                    )
        
        return candidates

    def _resolve_from_preprocess_chunks(self, cited_span: str) -> List[EvidenceCandidate]:
        candidates: List[EvidenceCandidate] = []
        chunks = self.context.preprocess_artifacts.get("chunks", [])
        for idx, chunk in enumerate(chunks):
            chunk_text = chunk.get("text", "")
            if cited_span.lower() in chunk_text.lower():
                confidence = self._calculate_confidence(cited_span, chunk_text)
                candidates.append(
                    EvidenceCandidate(
                        match_reason="chunk_text_match",
                        resolver_tier="preprocess_chunks",
                        window_rank=idx,
                        confidence=confidence,
                        artifact_path=self.context.paper_artifact.get("source", {}).get("source_pdf", ""),
                        page_span=chunk.get("page_range"),
                        chunk_ids=[chunk.get("chunk_id", str(idx))],
                        text_excerpt=chunk_text[:500],
                        negative_evidence_reason=None,
                        visual_refs=None,
                        caption_excerpt=None,
                        evidence_scope="chunk",
                    )
                )
        return candidates

    def _resolve_from_normalized_text(self, cited_span: str) -> List[EvidenceCandidate]:
        candidates: List[EvidenceCandidate] = []
        normalized_text = self.context.preprocess_artifacts.get("normalized_text", "")
        if normalized_text and cited_span.lower() in normalized_text.lower():
            excerpt_start = max(0, normalized_text.lower().find(cited_span.lower()) - 200)
            excerpt_end = min(len(normalized_text), excerpt_start + len(cited_span) + 400)
            confidence = self._calculate_confidence(cited_span, normalized_text)
            # Normalized text gets slightly lower base confidence
            confidence = max(0.6, confidence * 0.9)
            candidates.append(
                EvidenceCandidate(
                    match_reason="normalized_text_match",
                    resolver_tier="normalized_text",
                    window_rank=0,
                    confidence=confidence,
                    artifact_path=self.context.paper_artifact.get("source", {}).get("source_pdf", ""),
                    page_span=None,
                    chunk_ids=None,
                    text_excerpt=normalized_text[excerpt_start:excerpt_end],
                    negative_evidence_reason=None,
                    visual_refs=None,
                    caption_excerpt=None,
                    evidence_scope="full_text",
                )
            )
        return candidates

    def _resolve_from_plain_text(self, cited_span: str) -> List[EvidenceCandidate]:
        """Resolve evidence from plain text fallback."""
        candidates: List[EvidenceCandidate] = []
        plain_text = self.context.preprocess_artifacts.get("plain_text", "")
        if plain_text and cited_span.lower() in plain_text.lower():
            excerpt_start = max(0, plain_text.lower().find(cited_span.lower()) - 200)
            excerpt_end = min(len(plain_text), excerpt_start + len(cited_span) + 400)
            confidence = self._calculate_confidence(cited_span, plain_text)
            # Plain text fallback gets lower confidence
            confidence = max(0.5, confidence * 0.85)
            candidates.append(
                EvidenceCandidate(
                    match_reason="plain_text_fallback_match",
                    resolver_tier="plain_text_fallback",
                    window_rank=0,
                    confidence=confidence,
                    artifact_path=self.context.paper_artifact.get("source", {}).get("source_pdf", ""),
                    page_span=None,
                    chunk_ids=None,
                    text_excerpt=plain_text[excerpt_start:excerpt_end],
                    negative_evidence_reason=None,
                    visual_refs=None,
                    caption_excerpt=None,
                    evidence_scope="plain_text",
                )
            )
        return candidates

    def _resolve_from_visual_refs(
        self, visual_refs: List[Dict[str, Any]], cited_span: str
    ) -> List[EvidenceCandidate]:
        candidates: List[EvidenceCandidate] = []
        for idx, visual in enumerate(visual_refs):
            caption = visual.get("caption", "")
            if cited_span.lower() in caption.lower():
                confidence = self._calculate_confidence(cited_span, caption)
                # Visual caption gets slightly lower base confidence
                confidence = max(0.7, confidence * 0.85)
                candidates.append(
                    EvidenceCandidate(
                        match_reason="visual_caption_match",
                        resolver_tier="visual_refs",
                        window_rank=idx,
                        confidence=confidence,
                        artifact_path=visual.get("path", ""),
                        page_span=visual.get("page_range"),
                        chunk_ids=None,
                        text_excerpt="",
                        negative_evidence_reason=None,
                        visual_refs=[visual],
                        caption_excerpt=caption,
                        evidence_scope="visual",
                    )
                )
        return candidates


def build_evidence_resolver_context(
    paper_artifact: Dict[str, Any],
    preprocess_artifacts_path: Optional[str] = None,
) -> EvidenceResolverContext:
    preprocess_artifacts: Dict[str, Any] = {}
    
    # 首先从paper_artifact的analysis.preprocess字段获取预处理信息
    analysis_preprocess = paper_artifact.get("analysis", {}).get("preprocess", {})
    if analysis_preprocess:
        preprocess_artifacts = analysis_preprocess
    
    # 然后从preprocess_artifacts_path加载，覆盖已有信息
    if preprocess_artifacts_path and os.path.exists(preprocess_artifacts_path):
        try:
            with open(preprocess_artifacts_path, "r", encoding="utf-8") as f:
                preprocess_artifacts = json.load(f)
        except Exception:
            pass

    return EvidenceResolverContext(
        paper_key=paper_artifact.get("paper_identity", {}).get("canonical_paper_key", ""),
        paper_identity=paper_artifact.get("paper_identity", {}),
        preprocess_artifacts=preprocess_artifacts,
        paper_artifact=paper_artifact,
    )
