"""Grounded-first evidence window selection regression tests.

Guards the invariant that ai_summary hints can never starve source-grounded
evidence out of the bounded adjudication packet: validation support counts
only recognize source-grounded tiers, so 8 high-confidence summary hints must
not hide a real normalized/chunk match behind the window cap.
"""

from __future__ import annotations

from typing import Any, List, Optional

from validation.evidence_resolver import EvidenceCandidate, EvidenceResolver, EvidenceResolverContext


def _candidate(
    *,
    tier: str,
    reason: str,
    rank: int,
    confidence: float,
) -> EvidenceCandidate:
    return EvidenceCandidate(
        match_reason=reason,
        resolver_tier=tier,
        window_rank=rank,
        confidence=confidence,
        artifact_path="/virtual/source.md",
        page_span=None,
        chunk_ids=None,
        text_excerpt=f"{tier}:{rank}:{confidence}",
        negative_evidence_reason=None,
        visual_refs=None,
        caption_excerpt=None,
        evidence_scope="document",
    )


class _FakeResolver(EvidenceResolver):
    def __init__(
        self,
        hints: List[EvidenceCandidate],
        grounded: List[EvidenceCandidate],
    ) -> None:
        context = EvidenceResolverContext(
            paper_key="10.1/x",
            paper_identity={"canonical_paper_key": "10.1/x"},
            preprocess_artifacts={},
            paper_artifact={},
        )
        super().__init__(context)
        self._hints = hints
        self._grounded = grounded

    def _resolve_from_ai_summary(self, cited_span: str) -> List[EvidenceCandidate]:
        return list(self._hints)

    def _resolve_from_locator_page_index(self, query: str, locator: Optional[str]) -> List[EvidenceCandidate]:
        return []

    def _resolve_from_preprocess_chunks(self, query: str) -> List[EvidenceCandidate]:
        return [c for c in self._grounded if c.resolver_tier == "preprocess_chunks"]

    def _resolve_from_normalized_text(self, query: str) -> List[EvidenceCandidate]:
        return [c for c in self._grounded if c.resolver_tier == "normalized_text"]

    def _resolve_from_plain_text(self, query: str) -> List[EvidenceCandidate]:
        return []

    def _resolve_from_visual_refs(
        self, refs: Any, cited_span: str
    ) -> List[EvidenceCandidate]:
        return []

    def _create_negative_evidence_candidate(self, cited_span: str) -> EvidenceCandidate:
        return _candidate(tier="negative", reason="no_evidence", rank=0, confidence=0.0)


def test_source_grounded_evidence_not_starved_by_summary_hints() -> None:
    hints = [
        _candidate(tier="ai_summary", reason="ai_summary_match", rank=i, confidence=0.99)
        for i in range(9)
    ]
    grounded = [
        _candidate(
            tier="normalized_text",
            reason="normalized_match",
            rank=0,
            confidence=0.55,
        )
    ]
    resolver = _FakeResolver(hints, grounded)
    result = resolver.resolve_evidence("some claim span", max_windows=8)
    tiers = [item.resolver_tier for item in result]
    assert "normalized_text" in tiers, f"grounded evidence was starved: {tiers}"
    # Summary hints are capped at 2 when grounded evidence exists.
    assert tiers.count("ai_summary") <= 2
    assert len(result) <= 8


def test_all_hints_when_no_grounded_evidence() -> None:
    hints = [
        _candidate(tier="ai_summary", reason="ai_summary_match", rank=i, confidence=0.95)
        for i in range(10)
    ]
    resolver = _FakeResolver(hints, grounded=[])
    result = resolver.resolve_evidence("span", max_windows=8)
    assert len(result) == 8
    assert all(item.resolver_tier == "ai_summary" for item in result)


def test_grounded_windows_ranked_higher_than_hints() -> None:
    hints = [
        _candidate(tier="ai_summary", reason="hint", rank=0, confidence=0.99)
        for _ in range(5)
    ]
    grounded = [
        _candidate(tier="preprocess_chunks", reason="chunk", rank=0, confidence=0.4),
        _candidate(tier="normalized_text", reason="norm", rank=1, confidence=0.6),
    ]
    resolver = _FakeResolver(hints, grounded)
    result = resolver.resolve_evidence("span", max_windows=6)
    tiers = [item.resolver_tier for item in result]
    assert tiers.count("preprocess_chunks") == 1
    assert tiers.count("normalized_text") == 1
    assert tiers.count("ai_summary") <= 2
