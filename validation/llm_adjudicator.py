from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional

from ai_interface import _call_ai_api
from models import APIConfig


SOURCE_GROUNDED_TIERS = frozenset(
    {
        "locator_page_index",
        "preprocess_chunks",
        "normalized_text",
        "plain_text_fallback",
        "visual_refs",
    }
)

PRIORITY_EVIDENCE_PHRASES = (
    "consumer education",
    "consumer knowledge",
    "educate consumers",
    "educating consumers",
    "help consumers recognize",
    "recognize the persuasive techniques",
)

SUBSTANTIVE_EVIDENCE_MARKERS = frozenset(
    {
        "anova",
        "analysis",
        "conclusion",
        "conclusions",
        "discussion",
        "educate",
        "education",
        "educational",
        "effect",
        "experiment",
        "finding",
        "findings",
        "hypotheses",
        "hypothesis",
        "model",
        "regression",
        "reliability",
        "result",
        "results",
        "study",
        "validity",
    }
)

NON_SUBSTANTIVE_EVIDENCE_MARKERS = frozenset(
    {
        "age",
        "appendices",
        "appendix",
        "demographic",
        "demographics",
        "gender",
        "income",
        "respondent",
        "respondents",
        "supplement",
        "supplementary",
    }
)

SUBSTANTIVE_EVIDENCE_PHRASES: tuple[str, ...] = ()

NON_SUBSTANTIVE_EVIDENCE_PHRASES = (
    "sample characteristics",
    "participant characteristics",
    "respondent characteristics",
)

REFERENCE_HEADING_PATTERN = re.compile(
    r"^\s*(?:---\s*page\s+\d+\s*---\s*)?(?:references|bibliography)\b",
    re.IGNORECASE,
)
REFERENCE_YEAR_PATTERN = re.compile(r"\((?:19|20)\d{2}[a-z]?\)", re.IGNORECASE)
REFERENCE_VENUE_PATTERN = re.compile(r"\bjournal(?:\s+of)?\b", re.IGNORECASE)


@dataclass(frozen=True)
class AdjudicationPacket:
    citation_set_key: str
    stage: str
    claim_text: str
    claim_context: str
    block_context: str
    claim_type: str
    claim_type_confidence: float
    claim_type_rationale: str
    paper_ids: List[str]
    claim_units: List[Dict[str, Any]]
    target_claim_unit: Dict[str, Any]
    claim_unit_results: List[Dict[str, Any]]
    paper_identity_hints: Dict[str, Dict[str, Any]]
    per_paper_evidence_packets: Dict[str, Dict[str, List[Dict[str, Any]]]]
    evidence_excerpt_list: List[str]
    trimmed_candidate_counts: Dict[str, int]
    legacy_evidence_status: str
    legacy_disposition: str


def _candidate_context_text(candidate: Dict[str, Any]) -> str:
    fields = (
        candidate.get("evidence_scope"),
        candidate.get("match_reason"),
        candidate.get("resolver_tier"),
        candidate.get("artifact_path"),
        candidate.get("text_excerpt"),
        candidate.get("caption_excerpt"),
    )
    return " ".join(str(field or "").lower() for field in fields)


def _contains_marker(context: str, *, words: frozenset[str], phrases: tuple[str, ...]) -> bool:
    if any(phrase in context for phrase in phrases):
        return True
    tokens = set(re.findall(r"[a-z][a-z0-9_-]*", context))
    return bool(tokens.intersection(words))


def _candidate_is_reference_like(candidate: Dict[str, Any]) -> bool:
    metadata = " ".join(
        str(candidate.get(field) or "").lower()
        for field in ("evidence_scope", "match_reason", "resolver_tier")
    )
    if re.search(r"\b(?:reference_page|references|bibliography)\b", metadata):
        return True
    excerpt = str(candidate.get("text_excerpt") or candidate.get("caption_excerpt") or "")
    if REFERENCE_HEADING_PATTERN.search(excerpt):
        return True
    return (
        len(REFERENCE_YEAR_PATTERN.findall(excerpt)) >= 2
        and len(REFERENCE_VENUE_PATTERN.findall(excerpt)) >= 2
    )


def _candidate_evidence_position_rank(candidate: Dict[str, Any]) -> int:
    if _candidate_is_reference_like(candidate):
        return 4
    context = _candidate_context_text(candidate)
    if _contains_marker(
        context,
        words=NON_SUBSTANTIVE_EVIDENCE_MARKERS,
        phrases=NON_SUBSTANTIVE_EVIDENCE_PHRASES,
    ):
        return 3
    if _contains_marker(
        context,
        words=frozenset(),
        phrases=PRIORITY_EVIDENCE_PHRASES,
    ):
        return -1
    if _contains_marker(
        context,
        words=SUBSTANTIVE_EVIDENCE_MARKERS,
        phrases=SUBSTANTIVE_EVIDENCE_PHRASES,
    ):
        return 0
    return 1


def _candidate_dedupe_key(candidate: Dict[str, Any]) -> tuple[Any, ...]:
    text = re.sub(r"\s+", " ", str(candidate.get("text_excerpt") or "")).strip().casefold()
    caption = re.sub(r"\s+", " ", str(candidate.get("caption_excerpt") or "")).strip().casefold()
    if text or caption:
        return ("excerpt", text, caption)
    return (
        "locator",
        candidate.get("resolver_tier"),
        candidate.get("match_reason"),
        tuple(candidate.get("page_span") or []),
        tuple(candidate.get("chunk_ids") or []),
    )


def _normalized_excerpt(candidate: Dict[str, Any]) -> str:
    return re.sub(
        r"\s+",
        " ",
        str(candidate.get("text_excerpt") or candidate.get("caption_excerpt") or ""),
    ).strip().casefold()


def _excerpts_are_near_duplicates(left: str, right: str) -> bool:
    if not left or not right:
        return False
    if left == right:
        return True
    if min(len(left), len(right)) < 80:
        return False
    return SequenceMatcher(None, left, right).ratio() >= 0.93


def _candidate_location_key(candidate: Dict[str, Any]) -> tuple[Any, ...]:
    pages = tuple(candidate.get("page_span") or [])
    if pages:
        return ("pages", pages)
    chunks = tuple(candidate.get("chunk_ids") or [])
    if chunks:
        return ("chunks", chunks)
    return ("window", candidate.get("resolver_tier"), candidate.get("window_rank"))


def _select_diverse_candidates(
    candidates: List[Dict[str, Any]],
    *,
    limit: int,
) -> List[Dict[str, Any]]:
    selected: List[Dict[str, Any]] = []
    deferred_location: List[Dict[str, Any]] = []
    deferred_duplicates: List[Dict[str, Any]] = []
    location_counts: Dict[tuple[Any, ...], int] = {}

    for candidate in candidates:
        excerpt = _normalized_excerpt(candidate)
        if any(
            _excerpts_are_near_duplicates(excerpt, _normalized_excerpt(item))
            for item in selected
        ):
            deferred_duplicates.append(candidate)
            continue
        location = _candidate_location_key(candidate)
        if location_counts.get(location, 0) >= 2:
            deferred_location.append(candidate)
            continue
        selected.append(candidate)
        location_counts[location] = location_counts.get(location, 0) + 1
        if len(selected) >= limit:
            return selected

    for candidate in deferred_location:
        excerpt = _normalized_excerpt(candidate)
        if any(
            _excerpts_are_near_duplicates(excerpt, _normalized_excerpt(item))
            for item in selected
        ):
            continue
        selected.append(candidate)
        if len(selected) >= limit:
            return selected

    for candidate in deferred_duplicates:
        selected.append(candidate)
        if len(selected) >= limit:
            break
    return selected


def _candidate_sort_key(candidate: Dict[str, Any]) -> tuple[int, int, float, str]:
    source_grounded = bool(candidate.get("source_grounded"))
    confidence = float(candidate.get("confidence") or 0.0)
    resolver_tier = str(candidate.get("resolver_tier") or "")
    return (
        0 if source_grounded else 1,
        _candidate_evidence_position_rank(candidate),
        -confidence,
        resolver_tier,
    )


def _trim_candidates_for_stage(
    candidates: List[Dict[str, Any]],
    *,
    stage: str,
) -> List[Dict[str, Any]]:
    max_candidates = 6 if stage == "stronger" else 4
    max_summary_hints = 2 if stage == "stronger" else 1
    deduped: List[Dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for candidate in sorted(candidates, key=_candidate_sort_key):
        key = _candidate_dedupe_key(candidate)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(candidate)

    source_grounded = [item for item in deduped if item.get("resolver_tier") in SOURCE_GROUNDED_TIERS]
    summary_only = [item for item in deduped if item.get("resolver_tier") == "ai_summary"]
    others = [
        item
        for item in deduped
        if item.get("resolver_tier") not in SOURCE_GROUNDED_TIERS
        and item.get("resolver_tier") != "ai_summary"
    ]

    selected: List[Dict[str, Any]] = []
    if source_grounded:
        selected.extend(
            _select_diverse_candidates(source_grounded, limit=max_candidates)
        )
    else:
        selected.extend(others[:max_candidates])

    remaining_slots = max(max_candidates - len(selected), 0)
    if remaining_slots:
        selected.extend(summary_only[: min(max_summary_hints, remaining_slots)])
        remaining_slots = max(max_candidates - len(selected), 0)

    if remaining_slots:
        spillover = [
            item
            for item in (source_grounded[max_candidates:] + others[max_candidates:] + summary_only[max_summary_hints:])
            if item not in selected
        ]
        selected.extend(spillover[:remaining_slots])

    return selected[:max_candidates]


def build_adjudication_packet(result: Any, *, stage: str = "primary") -> AdjudicationPacket:
    claim_type = str(result.details.get("claim_type") or getattr(result, "claim_type", "") or "result").strip() or "result"
    claim_type_confidence = float(result.details.get("claim_type_confidence") or getattr(result, "claim_type_confidence", 0.0) or 0.0)
    claim_type_rationale = str(result.details.get("claim_type_rationale") or "").strip()
    raw_packets = dict(result.details.get("per_paper_evidence_packets") or {})
    checked_paper_ids = [
        str(item).strip()
        for item in result.details.get("checked_paper_ids", [])
        if str(item).strip()
    ]
    packet_paper_ids = list(dict.fromkeys(checked_paper_ids or getattr(result, "paper_ids", []) or list(raw_packets.keys())))
    trimmed_candidate_counts: Dict[str, int] = {}
    per_paper_packets: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}
    target_claim_unit_id = str((getattr(result, "target_claim_unit", {}) or {}).get("claim_unit_id") or "")
    for paper_id in packet_paper_ids:
        paper_packets = raw_packets.get(paper_id, {})
        if isinstance(paper_packets, list):
            normalized_packets = {target_claim_unit_id or "claim_unit": [dict(item) for item in paper_packets]}
        else:
            normalized_packets = {
                str(claim_unit_id): [dict(item) for item in candidates]
                for claim_unit_id, candidates in dict(paper_packets or {}).items()
            }
        trimmed_by_claim: Dict[str, List[Dict[str, Any]]] = {}
        for claim_unit_id, candidates in normalized_packets.items():
            trimmed = _trim_candidates_for_stage(candidates, stage=stage)
            trimmed_candidate_counts[f"{paper_id}:{claim_unit_id}"] = max(len(candidates) - len(trimmed), 0)
            trimmed_by_claim[claim_unit_id] = trimmed
        per_paper_packets[paper_id] = trimmed_by_claim

    evidence_excerpt_list: List[str] = []
    excerpt_groups = [
        packets
        for claim_packets in per_paper_packets.values()
        for packets in claim_packets.values()
    ]
    max_excerpts = 12 if stage == "stronger" else 8
    group_index = 0
    while len(evidence_excerpt_list) < max_excerpts and any(
        group_index < len(group) for group in excerpt_groups
    ):
        for group in excerpt_groups:
            if group_index >= len(group):
                continue
            candidate = group[group_index]
            excerpt = str(
                candidate.get("text_excerpt") or candidate.get("caption_excerpt") or ""
            ).strip()
            normalized = re.sub(r"\s+", " ", excerpt).strip().casefold()
            if excerpt and not any(
                _excerpts_are_near_duplicates(
                    normalized,
                    re.sub(r"\s+", " ", existing).strip().casefold(),
                )
                for existing in evidence_excerpt_list
            ):
                evidence_excerpt_list.append(excerpt)
            if len(evidence_excerpt_list) >= max_excerpts:
                break
        group_index += 1

    return AdjudicationPacket(
        citation_set_key=str(getattr(result, "citation_set_key", "") or result.details.get("citation_set_key") or ""),
        stage=stage,
        claim_text=str(getattr(result, "claim_text", "") or ""),
        claim_context=str(getattr(result, "claim_context", "") or ""),
        block_context=str(getattr(result, "block_context", "") or result.details.get("block_context") or ""),
        claim_type=claim_type,
        claim_type_confidence=claim_type_confidence,
        claim_type_rationale=claim_type_rationale,
        paper_ids=packet_paper_ids,
        claim_units=[dict(item) for item in getattr(result, "claim_units", []) or []],
        target_claim_unit=dict(getattr(result, "target_claim_unit", {}) or {}),
        claim_unit_results=[dict(item) for item in result.details.get("claim_unit_results", []) or []],
        paper_identity_hints={key: dict(value) for key, value in (result.details.get("paper_identity_hints") or {}).items()},
        per_paper_evidence_packets=per_paper_packets,
        evidence_excerpt_list=evidence_excerpt_list,
        trimmed_candidate_counts=trimmed_candidate_counts,
        legacy_evidence_status=str(getattr(result, "evidence_status", "") or result.details.get("evidence_status") or ""),
        legacy_disposition=str(getattr(result, "disposition", "") or result.details.get("disposition") or ""),
    )


def _build_prompts(packet: AdjudicationPacket) -> tuple[str, str]:
    packet_json = json.dumps(asdict(packet), ensure_ascii=False, indent=2)
    stage_label = "stronger" if packet.stage == "stronger" else "primary"
    prompt = (
        f"You are the {stage_label} adjudication stage for a literature-review citation bundle.\n"
        "Your job is to judge whether the cited claim bundle is supportable by the exact cited paper set.\n"
        "Important rules:\n"
        "- The validator judges cited claims only, not uncited bridge prose.\n"
        "- Multiple cited papers may jointly support different sub-claims inside the same sentence.\n"
        "- Do not require every paper to support the full sentence if the set jointly supports it.\n"
        "- Respect checked_paper_ids and expected_supporting_paper_ids on each claim_unit_result; do not infer claim-paper mappings from pooled citations.\n"
        "- If a claim_unit_result reason is ambiguous_claim_paper_alignment, keep it as evidence_gap or low_confidence/manual_review unless source identity is truly missing or wrong.\n"
        "- Distinguish result, synthesis, future-direction, and limitation/method critique claims.\n"
        "- If the evidence is still not strong enough, use an uncertainty/manual-review outcome instead of forcing unsupported.\n"
        "- Prefer source-grounded evidence packets over summary-only hints when both are available.\n"
        "- ai_summary packets are hints/context only and cannot by themselves justify clean source-grounded support.\n\n"
        f"Bundle packet:\n{packet_json}"
    )
    if packet.stage == "stronger":
        prompt += (
            "\n\nThis is the stronger escalation pass. Re-evaluate carefully before leaving the item in manual review. "
            "Only keep manual review when the packet remains genuinely uncertain after deeper reasoning."
        )

    system_prompt = (
        "Return JSON only with keys: status, confidence, repair_scope, disposition, low_confidence, reasoning, "
        "repair_hint, summary_paper_ids, manual_review_reason, claim_type, claim_type_confidence, claim_type_rationale, adjudication_status. "
        "status must be one of supported, partial_support, evidence_gap, unsupported, contradicted, wrong_source, low_confidence. "
        "repair_scope must be one of none, summary, review, both, manual_review. "
        "disposition must be one of keep_as_is, narrowed_and_kept, manual_review, fail, summary_repair, review_repair, both_repair. "
        "adjudication_status should summarize the semantic outcome (for example supported, evidence_gap, uncertain, wrong_source, unsupported, contradicted)."
    )
    return prompt, system_prompt


def run_adjudication_stage(
    generator_instance: Any,
    api_config: Optional[APIConfig],
    packet: AdjudicationPacket,
) -> Optional[Dict[str, Any]]:
    if not api_config or not packet.claim_text.strip() or not packet.paper_ids:
        return None

    try:
        base_max_tokens = int((generator_instance.config.get("API_Parameters") or {}).get("claims_max_tokens", 4096))
        base_temperature = float((generator_instance.config.get("API_Parameters") or {}).get("claims_temperature", 0.2))
    except Exception:
        base_max_tokens = 4096
        base_temperature = 0.2

    if packet.stage == "stronger":
        max_tokens = max(base_max_tokens, 6144)
        temperature = min(base_temperature, 0.15)
    else:
        max_tokens = base_max_tokens
        temperature = base_temperature

    prompt, system_prompt = _build_prompts(packet)
    try:
        report = _call_ai_api(
            prompt,
            api_config,
            system_prompt,
            max_tokens=max_tokens,
            temperature=temperature,
            response_format="json",
            logger=getattr(generator_instance, "logger", None),
        )
    except Exception as exc:
        logger = getattr(generator_instance, "logger", None)
        if logger:
            logger.warning(f"AI {packet.stage} adjudication failed: {exc}")
        return None

    if not isinstance(report, dict):
        return None
    report.setdefault("adjudication_stage", packet.stage)
    report.setdefault("claim_type", packet.claim_type)
    report.setdefault("claim_type_confidence", packet.claim_type_confidence)
    report.setdefault("claim_type_rationale", packet.claim_type_rationale)
    report.setdefault("adjudication_status", str(report.get("status") or packet.legacy_evidence_status or "evidence_gap"))
    return report
