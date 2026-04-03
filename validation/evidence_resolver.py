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

        candidates.extend(self._resolve_from_preprocess_chunks(cited_span))
        candidates.extend(self._resolve_from_normalized_text(cited_span))
        if selected_visual_refs:
            candidates.extend(self._resolve_from_visual_refs(selected_visual_refs, cited_span))

        candidates.sort(key=lambda x: (-x.confidence, x.window_rank))
        return candidates

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
