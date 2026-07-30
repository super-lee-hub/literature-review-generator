from __future__ import annotations

import json
from dataclasses import asdict
from difflib import SequenceMatcher
from pathlib import Path
from types import SimpleNamespace

import pytest

from validation.evidence_resolver import (
    SOURCE_GROUNDED_RESOLVER_TIERS,
    EvidenceResolver,
    EvidenceResolverContext,
    build_bilingual_retrieval_queries,
)
from validation.llm_adjudicator import build_adjudication_packet


REPO_ROOT = Path(__file__).resolve().parents[1]
S05_WORKSPACE = (
    REPO_ROOT / "output" / "pph_s05_subjective_knowledge__20260728_063507_e48eec64"
)
HARDESTY_PAPER_PATH = (
    S05_WORKSPACE / "artifacts" / "paper_artifacts" / "c4731d347816e7ef.json"
)
S05_VALIDATION_REPORT_PATH = (
    S05_WORKSPACE
    / "reports"
    / "pph_s05_subjective_knowledge_validation_run_result_v1.json"
)
HARDESTY_CLAIM_RESULT_ID = "claim:500e7b79b9590081c074cb4e"


def _load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _artifact_path(raw_path: str) -> Path:
    path = Path(raw_path)
    return path if path.is_absolute() else REPO_ROOT / path


def _candidate_similarity(left: str, right: str) -> float:
    normalized_left = " ".join(left.lower().split())
    normalized_right = " ".join(right.lower().split())
    return SequenceMatcher(None, normalized_left, normalized_right).ratio()


def _fresh_hardesty_packet():
    if not HARDESTY_PAPER_PATH.exists() or not S05_VALIDATION_REPORT_PATH.exists():
        pytest.skip("real S05 Hardesty validation fixtures are not available")

    paper_artifact = _load_json(HARDESTY_PAPER_PATH)
    report = _load_json(S05_VALIDATION_REPORT_PATH)
    claim_result = next(
        item
        for item in report["claim_results"]
        if item.get("claim_result_id") == HARDESTY_CLAIM_RESULT_ID
    )
    preprocess = paper_artifact["analysis"]["preprocess"]
    context = EvidenceResolverContext(
        paper_key=paper_artifact["paper_identity"]["canonical_paper_key"],
        paper_identity=paper_artifact["paper_identity"],
        preprocess_artifacts={
            "page_index": _load_json(_artifact_path(preprocess["page_index_path"])),
            "chunks": _load_json(_artifact_path(preprocess["chunks_path"])),
            "normalized_text": _artifact_path(preprocess["markdown_path"]).read_text(
                encoding="utf-8"
            ),
            "plain_text": _artifact_path(preprocess["plain_text_path"]).read_text(
                encoding="utf-8"
            ),
        },
        paper_artifact=paper_artifact,
    )
    resolver = EvidenceResolver(context)
    paper_id = claim_result["paper_ids"][0]
    fresh_packets = {paper_id: {}}
    for claim_unit in claim_result["details"]["claim_unit_results"]:
        claim_unit_id = claim_unit["claim_unit_id"]
        claim_text = claim_unit["claim_text"]
        candidates = resolver.resolve_evidence(
            claim_text,
            retrieval_queries=build_bilingual_retrieval_queries(
                claim_text,
                paper_artifact,
            ),
        )
        fresh_packets[paper_id][claim_unit_id] = [
            {
                **asdict(candidate),
                "paper_id": paper_id,
                "claim_unit_id": claim_unit_id,
                "source_grounded": (
                    candidate.resolver_tier in SOURCE_GROUNDED_RESOLVER_TIERS
                ),
            }
            for candidate in candidates
        ]

    details = dict(claim_result["details"])
    details["per_paper_evidence_packets"] = fresh_packets
    result = SimpleNamespace(
        citation_set_key=details["citation_set_key"],
        claim_text=claim_result["claim_text"],
        claim_context=claim_result.get("claim_context", ""),
        block_context=details.get("block_context", ""),
        claim_type=details.get("claim_type", "result"),
        paper_ids=claim_result["paper_ids"],
        claim_units=details.get("claim_units", []),
        target_claim_unit=details.get("target_claim_unit", {}),
        evidence_status=claim_result.get("evidence_status", ""),
        disposition=claim_result.get("disposition", ""),
        details=details,
    )
    return build_adjudication_packet(result, stage="stronger"), paper_id


def test_hardesty_real_fixture_preserves_distinct_claim_evidence():
    packet, paper_id = _fresh_hardesty_packet()
    claim_packets = packet.per_paper_evidence_packets[paper_id]
    measurement_unit_id = packet.claim_unit_results[1]["claim_unit_id"]
    education_unit_id = packet.claim_unit_results[5]["claim_unit_id"]
    measurement_candidates = claim_packets[measurement_unit_id]
    education_candidates = claim_packets[education_unit_id]

    assert any(
        set(candidate.get("page_span") or []).intersection({3, 4, 5})
        and any(
            marker in candidate["text_excerpt"].lower()
            for marker in ("reliability", "content validity", "known group validity")
        )
        for candidate in measurement_candidates
    )
    assert any(
        9 in (candidate.get("page_span") or [])
        and any(
            marker in candidate["text_excerpt"].lower()
            for marker in ("educa", "recognize the persuasive techniques")
        )
        for candidate in education_candidates
    )

    selected_candidates = [
        candidate for candidates in claim_packets.values() for candidate in candidates
    ]
    assert all(
        12 not in (candidate.get("page_span") or [])
        for candidate in selected_candidates
    )
    selected_pages = {
        page
        for candidate in selected_candidates
        for page in (candidate.get("page_span") or [])
    }
    assert len(selected_pages) >= 5

    excerpts = packet.evidence_excerpt_list
    assert any(
        "reliab" in excerpt.lower() or "validity" in excerpt.lower()
        for excerpt in excerpts
    )
    assert any("educa" in excerpt.lower() for excerpt in excerpts)
    assert all(
        _candidate_similarity(left, right) < 0.93
        for index, left in enumerate(excerpts)
        for right in excerpts[index + 1 :]
    )
