from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field, replace
from typing import Any, Dict, List, Optional, Sequence

from services.visual_artifact_resolver import normalize_visual_artifact


_CORE_ANALYSIS_FIELD_DEFAULTS: Sequence[tuple[str, Any]] = (
    ("summary", ""),
    ("key_points", []),
    ("methodology", ""),
    ("methods", ""),
    ("findings", ""),
    ("key_findings", []),
    ("conclusions", ""),
    ("conclusion", ""),
    ("relevance", ""),
    ("limitations", ""),
    ("theoretical_framework", ""),
    ("research_gap", ""),
    ("future_research_directions", []),
)

SOURCE_GROUNDED_RESOLVER_TIERS = frozenset(
    {
        "locator_page_index",
        "preprocess_chunks",
        "normalized_text",
        "plain_text_fallback",
        "visual_refs",
    }
)
SUMMARY_HINT_RESOLVER_TIERS = frozenset({"ai_summary"})


def build_bilingual_retrieval_queries(
    cited_span: str,
    paper_artifact: Dict[str, Any],
    *,
    max_english_terms: int = 12,
) -> List[str]:
    """Add Stage 1 English concepts for recall while preserving the source claim."""
    queries = [str(cited_span or "").strip()]
    ai_summary = paper_artifact.get("analysis", {}).get("ai_summary", {}) or {}
    candidate_values: List[Any] = []
    for container in (
        ai_summary.get("core_analysis", {}),
        ai_summary.get("routing", {}),
        ai_summary.get("specialized_details", {}),
    ):
        if not isinstance(container, dict):
            continue
        for key, value in container.items():
            lowered = str(key).lower()
            if any(
                marker in lowered
                for marker in (
                    "keyword",
                    "concept",
                    "theme",
                    "mechanism",
                    "theor",
                    "key_point",
                    "finding",
                )
            ):
                candidate_values.extend(value if isinstance(value, list) else [value])
    english_terms: List[str] = []
    for value in candidate_values:
        for phrase in re.findall(r"[A-Za-z][A-Za-z-]*(?:\s+[A-Za-z][A-Za-z-]*){0,4}", str(value)):
            normalized = " ".join(phrase.lower().split())
            if len(normalized) > 2 and normalized not in english_terms:
                english_terms.append(normalized)
            if len(english_terms) >= max_english_terms:
                break
        if len(english_terms) >= max_english_terms:
            break
    queries.extend(english_terms)
    return [query for query in queries if query]


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
    preprocess_evidence: Dict[str, Any] = field(default_factory=dict)
    paper_metadata: Dict[str, Any] = field(default_factory=dict)


class EvidenceResolver:
    def __init__(self, context: EvidenceResolverContext):
        self.context = context
        self._stop_words = {
            "的",
            "了",
            "是",
            "在",
            "我",
            "有",
            "和",
            "就",
            "不",
            "人",
            "都",
            "一",
            "一个",
            "上",
            "也",
            "很",
            "到",
            "说",
            "要",
            "去",
            "你",
            "会",
            "着",
            "没有",
            "看",
            "好",
            "自己",
            "这",
            "the",
            "and",
            "with",
            "that",
            "this",
            "from",
            "were",
            "was",
            "for",
            "into",
            "under",
        }

    @staticmethod
    def _flatten_summary_fields(fields: Sequence[Any]) -> List[Any]:
        flattened_fields: List[Any] = []
        for field_value in fields:
            if isinstance(field_value, list):
                flattened_fields.extend(field_value)
                continue
            flattened_fields.append(field_value)
        return flattened_fields

    def _paper_title_for_summary_match(self) -> str:
        paper_artifact = self.context.paper_artifact
        for title in (
            paper_artifact.get("paper_metadata", {}).get("title"),
            paper_artifact.get("analysis", {}).get("paper_metadata", {}).get("title"),
            paper_artifact.get("analysis", {}).get("paper_info", {}).get("title"),
        ):
            if title:
                return str(title)

        canonical_paper_key = paper_artifact.get("paper_identity", {}).get("canonical_paper_key", "")
        if not canonical_paper_key:
            return ""
        return str(canonical_paper_key).split("_")[0].replace("_", " ")

    def _significant_words(self, text: str) -> List[str]:
        return [
            word
            for word in re.findall(r"\b\w+\b", str(text or "").lower())
            if word not in self._stop_words and len(word) > 2
        ]

    def _build_excerpt_for_overlap(self, context_text: str, matching_words: Sequence[str]) -> str:
        lowered = context_text.lower()
        positions = [lowered.find(word) for word in matching_words if word and lowered.find(word) >= 0]
        anchor = min(positions) if positions else 0
        start = max(anchor - 200, 0)
        end = min(len(context_text), start + 500)
        return context_text[start:end]

    def _source_grounded_overlap(
        self,
        cited_span: str,
        context_text: str,
    ) -> tuple[float, str]:
        if not cited_span or not context_text:
            return (0.0, "")
        lowered_context = context_text.lower()
        lowered_span = cited_span.lower()
        if lowered_span in lowered_context:
            return (self._calculate_confidence(cited_span, context_text), self._build_excerpt_for_overlap(context_text, [lowered_span]))

        significant_words = self._significant_words(cited_span)
        if not significant_words:
            return (0.0, "")

        matching_words = [word for word in significant_words if word in lowered_context]
        overlap_ratio = len(matching_words) / max(len(significant_words), 1)
        if len(matching_words) < 3 and overlap_ratio < 0.45:
            return (0.0, "")

        confidence = min(0.58 + overlap_ratio * 0.32, 0.9)
        return (confidence, self._build_excerpt_for_overlap(context_text, matching_words))

    def resolve_evidence(
        self,
        cited_span: str,
        locator: Optional[str] = None,
        selected_visual_refs: Optional[List[Dict[str, Any]]] = None,
        retrieval_queries: Optional[Sequence[str]] = None,
        max_windows: int = 8,
    ) -> List[EvidenceCandidate]:
        """Resolve bounded source-grounded evidence windows for one span.

        ``max_windows`` caps the ranked windows returned per call so validation
        adjudication sees a bounded window set (default 8) rather than
        unbounded full-text excerpts.  Applied after de-duplication, before
        the negative-evidence fallback.
        """
        if max_windows <= 0:
            max_windows = 8
        candidates: List[EvidenceCandidate] = []

        # Explicit tier order as required
        # Tier 0: AI Summary (highest priority)
        candidates.extend(self._resolve_from_ai_summary(cited_span))
        recall_queries: List[str] = list(
            dict.fromkeys(
                str(item)
                for item in (cited_span, *(retrieval_queries or ()))
                if str(item)
            )
        )
        for query_index, query in enumerate(recall_queries):
            grounded: List[EvidenceCandidate] = []
            # Tier 1: Locator + Page Index
            grounded.extend(self._resolve_from_locator_page_index(query, locator))
            # Tier 2: Preprocess chunks
            grounded.extend(self._resolve_from_preprocess_chunks(query))
            # Tier 3: Normalized text
            grounded.extend(self._resolve_from_normalized_text(query))
            # Tier 4: Plain text fallback
            grounded.extend(self._resolve_from_plain_text(query))
            if query_index:
                grounded = [
                    replace(item, match_reason=f"bilingual_retrieval:{item.match_reason}")
                    for item in grounded
                ]
            candidates.extend(grounded)
        # Tier 5: Visual refs
        if selected_visual_refs:
            candidates.extend(self._resolve_from_visual_refs(selected_visual_refs, cited_span))

        deduped: List[EvidenceCandidate] = []
        seen = set()
        for candidate in candidates:
            identity = (
                candidate.resolver_tier,
                candidate.artifact_path,
                candidate.text_excerpt,
                tuple(candidate.page_span or ()),
                tuple(candidate.chunk_ids or ()),
            )
            if identity not in seen:
                seen.add(identity)
                deduped.append(candidate)
        candidates = deduped

        # Now add negative evidence if no candidates were found
        if not candidates:
            candidates.append(self._create_negative_evidence_candidate(cited_span))

        # Sort by confidence (descending) and window_rank (ascending), but
        # never let ai_summary hints starve source-grounded evidence: the
        # adjudication support counts only recognize source-grounded tiers
        # (locator_page_index / preprocess_chunks / normalized_text /
        # plain_text_fallback / visual_refs), so a pile of high-confidence
        # summary hints must not push the real paper windows out of the
        # bounded packet.  Source-grounded candidates fill the window budget
        # first; summary hints only take the remaining slots.
        grounded = [
            c for c in candidates if c.resolver_tier in SOURCE_GROUNDED_RESOLVER_TIERS
        ]
        hints = [c for c in candidates if c.resolver_tier in SUMMARY_HINT_RESOLVER_TIERS]
        other = [
            c
            for c in candidates
            if c.resolver_tier not in SOURCE_GROUNDED_RESOLVER_TIERS
            and c.resolver_tier not in SUMMARY_HINT_RESOLVER_TIERS
        ]
        grounded.sort(key=lambda x: (-x.confidence, x.window_rank))
        hints.sort(key=lambda x: (-x.confidence, x.window_rank))
        other.sort(key=lambda x: (-x.confidence, x.window_rank))
        max_hints = max(1, min(2, max_windows))
        if grounded:
            chosen = grounded[:max_windows]
            remaining = max_windows - len(chosen)
            if remaining > 0:
                chosen.extend(hints[: min(max_hints, remaining)])
        else:
            chosen = hints[:max_windows] if hints else other[:max_windows]
        if not chosen:
            chosen = [
                c for c in candidates
                if c.resolver_tier == "negative" or c.match_reason == "no_evidence"
            ] or [self._create_negative_evidence_candidate(cited_span)]
        return chosen[:max_windows]

    def _resolve_from_ai_summary(self, cited_span: str) -> List[EvidenceCandidate]:
        """Resolve evidence from AI summary in paper artifact."""
        candidates: List[EvidenceCandidate] = []
        
        # Get AI summary from paper artifact
        ai_summary = self.context.paper_artifact.get("analysis", {}).get("ai_summary", {})
        if not ai_summary:
            return candidates
        
        # Extract relevant fields from AI summary
        core_analysis = ai_summary.get("core_analysis", {})
        summary_fields = [
            core_analysis.get(field_name, default_value)
            for field_name, default_value in _CORE_ANALYSIS_FIELD_DEFAULTS
        ]
        
        # Check specialized details
        specialized_details = ai_summary.get("specialized_details", {})
        for detail_type in ["empirical", "review", "conceptual"]:
            detail = specialized_details.get(detail_type, {})
            if detail:
                for key, value in detail.items():
                    if isinstance(value, str):
                        summary_fields.append(value)
                    elif isinstance(value, list):
                        summary_fields.extend(value)
        
        # Flatten the list
        flattened_fields = self._flatten_summary_fields(summary_fields)
        
        # Check each field for matches
        for idx, field_text in enumerate(flattened_fields):
            if isinstance(field_text, str) and field_text:
                # More flexible matching: check if any significant words from cited_span are in field_text
                # Split cited_span into words and remove common stop words
                cited_words = re.findall(r'\b\w+\b', cited_span.lower())
                # Filter out common stop words
                stop_words = set(['的', '了', '是', '在', '我', '有', '和', '就', '不', '人', '都', '一', '一个', '上', '也', '很', '到', '说', '要', '去', '你', '会', '着', '没有', '看', '好', '自己', '这'])
                significant_words = [word for word in cited_words if word not in stop_words and len(word) > 1]
                
                # Check if any significant words are in field_text
                if significant_words:
                    matching_words = [word for word in significant_words if word in field_text.lower()]
                    
                    # More flexible matching criteria
                    # If at least 1 significant word matches and there are few significant words
                    # OR at least 2 significant words match
                    # OR more than 20% of significant words match
                    if (len(significant_words) <= 3 and len(matching_words) >= 1) or \
                       len(matching_words) >= 2 or \
                       (len(significant_words) > 0 and len(matching_words) / len(significant_words) > 0.2):
                        overlap_ratio = len(matching_words) / max(len(significant_words), 1)
                        if cited_span.lower() in field_text.lower():
                            confidence = min(self._calculate_confidence(cited_span, field_text) + 0.1, 0.99)
                        else:
                            confidence = min(0.55 + (overlap_ratio * 0.3), 0.79 if overlap_ratio < 0.8 else 0.85)
                        candidates.append(
                            EvidenceCandidate(
                                match_reason="ai_summary_match",
                                resolver_tier="ai_summary",
                                window_rank=idx,
                                confidence=confidence,
                                artifact_path=self.context.paper_artifact.get("source", {}).get("source_pdf", ""),
                                page_span=None,
                                chunk_ids=None,
                                text_excerpt=field_text[:500],
                                negative_evidence_reason=None,
                                visual_refs=None,
                                caption_excerpt=None,
                                evidence_scope="ai_summary",
                            )
                        )
                
                # Additional check: if cited_span is in Chinese and field_text is in English,
                # check if the paper title or key concepts from the paper are mentioned in the field_text
                elif any(char >= '\u4e00' and char <= '\u9fff' for char in cited_span):
                    paper_title = self._paper_title_for_summary_match()
                    if paper_title:
                        # Split title into significant words
                        title_words = re.findall(r'\b\w+\b', paper_title.lower())
                        title_significant_words = [word for word in title_words if len(word) > 2]
                        if title_significant_words:
                            # Check if any title words are in field_text
                            title_matching_words = [word for word in title_significant_words if word in field_text.lower()]
                            if len(title_matching_words) >= 2:
                                confidence = 0.85  # Slightly lower confidence for title-based matching
                                candidates.append(
                                    EvidenceCandidate(
                                        match_reason="ai_summary_title_match",
                                        resolver_tier="ai_summary",
                                        window_rank=idx,
                                        confidence=confidence,
                                        artifact_path=self.context.paper_artifact.get("source", {}).get("source_pdf", ""),
                                        page_span=None,
                                        chunk_ids=None,
                                        text_excerpt=field_text[:500],
                                        negative_evidence_reason=None,
                                        visual_refs=None,
                                        caption_excerpt=None,
                                        evidence_scope="ai_summary",
                                    )
                                )
        
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
            reason = "cited_text_not_found_in_any_tier"
        
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
                confidence, excerpt = self._source_grounded_overlap(cited_span, page_text)
                if confidence > 0:
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
                            text_excerpt=excerpt or page_text[:500],
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
                confidence, excerpt = self._source_grounded_overlap(cited_span, page_text)
                if confidence > 0:
                    candidates.append(
                        EvidenceCandidate(
                            match_reason="page_index_match",
                            resolver_tier="locator_page_index",
                            window_rank=idx,
                            confidence=confidence,
                            artifact_path=self.context.paper_artifact.get("source", {}).get("source_pdf", ""),
                            page_span=page_entry.get("page_range", [page_entry.get("page_number", 0)]),
                            chunk_ids=None,
                            text_excerpt=excerpt or page_text[:500],
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
            confidence, excerpt = self._source_grounded_overlap(cited_span, chunk_text)
            if confidence > 0:
                candidates.append(
                    EvidenceCandidate(
                        match_reason="chunk_text_match",
                        resolver_tier="preprocess_chunks",
                        window_rank=idx,
                        confidence=confidence,
                        artifact_path=self.context.paper_artifact.get("source", {}).get("source_pdf", ""),
                        page_span=chunk.get("page_range"),
                        chunk_ids=[chunk.get("chunk_id", str(idx))],
                        text_excerpt=excerpt or chunk_text[:500],
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
        if normalized_text:
            confidence, excerpt = self._source_grounded_overlap(cited_span, normalized_text)
            if confidence <= 0:
                return candidates
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
                    text_excerpt=excerpt or normalized_text[:500],
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
        if plain_text:
            confidence, excerpt = self._source_grounded_overlap(cited_span, plain_text)
            if confidence <= 0:
                return candidates
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
                    text_excerpt=excerpt or plain_text[:500],
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
            # 归一化视觉证据
            normalized_visual = normalize_visual_artifact(visual)
            caption = normalized_visual.get("caption_excerpt", "")
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
                        artifact_path=normalized_visual.get("image_path", ""),
                        page_span=normalized_visual.get("page_range"),
                        chunk_ids=None,
                        text_excerpt="",
                        negative_evidence_reason=None,
                        visual_refs=[normalized_visual],
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
