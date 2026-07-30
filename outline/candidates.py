"""Multi-candidate outline generation for Outline Intelligence v2.

Generates multiple outline candidates from literature map and synthesis flow.
Production v2 calls configured Outline_API. Test doubles only in test/dev mode.
"""

from __future__ import annotations

import json
import math
import uuid
from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import Any, Callable, Dict, List, Optional, Sequence

from outline.quality_rules import (
    allowed_provider_flow_roles,
    forbidden_provider_flow_roles,
    is_low_quality_title,
    is_placeholder_title,
    is_required_capable_flow_role,
    non_blocking_flow_roles,
)
from outline.v2_config import OutlineQualityGateConfig
from outline.prompt_budget import OutlinePromptBudgetExceeded, PromptBudgetV1, packet_hash
from outline.v2_models import (
    CandidateSection,
    FlowStep,
    LiteratureMap,
    OutlineCandidate,
    OutlineCandidates,
    PaperNode,
    SynthesisFlow,
    compute_content_hash,
)


ModelCaller = Callable[[str, str, Dict[str, Any]], Any]
FALLBACK_GENERATION_MINIMUM = 0.40
FINAL_ADOPTION_THRESHOLD = 0.50


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _build_candidate_sections(
    flow_steps: List[FlowStep],
    paper_nodes: List[PaperNode],
    strategy_label: str,
) -> List[CandidateSection]:
    """Build candidate sections from flow steps.

    Each flow step maps to a top-level section with assigned papers.
    """
    sections: List[CandidateSection] = []
    node_map = {n.paper_key: n for n in paper_nodes}

    valid_steps = [
        step for step in flow_steps
        if not step.placeholder_flow
        and step.role_in_review not in non_blocking_flow_roles()
        and is_required_capable_flow_role(step.role_in_review)
        and len(set(step.support_refs)) >= 2
        and not is_low_quality_title(step.claim)
    ]

    if not valid_steps:
        return []

    bucket_count = min(3, len(valid_steps))
    buckets: List[List[FlowStep]] = [[] for _ in range(bucket_count)]
    for idx, step in enumerate(valid_steps):
        buckets[idx % bucket_count].append(step)

    used_papers: set[str] = set()
    for i, bucket in enumerate(buckets, 1):
        if not bucket:
            continue
        primary_step = bucket[0]
        bucket_refs: List[str] = []
        for step in bucket:
            for ref in step.support_refs:
                if ref not in bucket_refs:
                    bucket_refs.append(ref)

        assigned_papers: List[Dict[str, str]] = []
        target_count = max(2, len(bucket_refs) // max(len(buckets), 1))
        for ref in bucket_refs:
            if ref in used_papers:
                continue
            node = node_map.get(ref)
            title = node.title if node else ref
            assigned_papers.append({
                "paper_key": ref,
                "title": title,
                "role": primary_step.role_in_review,
                "reason": f"Supports {primary_step.claim[:100]}",
            })
            used_papers.add(ref)
            if len(assigned_papers) >= target_count:
                break
        if not assigned_papers:
            for ref in bucket_refs:
                node = node_map.get(ref)
                title = node.title if node else ref
                assigned_papers.append({
                    "paper_key": ref,
                    "title": title,
                    "role": primary_step.role_in_review,
                    "reason": f"Supports {primary_step.claim[:100]}",
                })
                break

        section = CandidateSection(
            section_id=f"cand_sec_{i:03d}",
            title=primary_step.claim[:120] if primary_step.claim else f"Evidence stream {i}",
            purpose=primary_step.role_in_review.replace("_", " "),
            argument_role=primary_step.role_in_review,
            source_flow_steps=[step.flow_step_id for step in bucket],
            assigned_papers=assigned_papers,
            children=[],
        )
        sections.append(section)

    return sections


def _flow_step_map(synthesis_flow: SynthesisFlow) -> Dict[str, FlowStep]:
    return {step.flow_step_id: step for step in synthesis_flow.flow_steps}


def _all_sections(sections: List[CandidateSection]) -> List[CandidateSection]:
    collected: List[CandidateSection] = []
    for section in sections:
        collected.append(section)
        collected.extend(_all_sections(section.children))
    return collected


def _required_capable_step_ids(synthesis_flow: SynthesisFlow) -> set[str]:
    return {
        step.flow_step_id
        for step in synthesis_flow.flow_steps
        if not step.placeholder_flow
        and is_required_capable_flow_role(step.role_in_review)
    }


def _candidate_canonical_coverage(candidate: OutlineCandidate, literature_map: LiteratureMap) -> float:
    total = max(len({node.canonical_paper_key or node.paper_key for node in literature_map.paper_nodes}), 1)
    aliases: Dict[str, str] = {}
    for node in literature_map.paper_nodes:
        canonical = node.canonical_paper_key or node.paper_key
        for alias in [node.paper_key, canonical, *node.aliases]:
            if alias:
                aliases[str(alias)] = canonical
    assigned: set[str] = set()
    for section in _all_sections(candidate.sections):
        for paper in section.assigned_papers:
            paper_key = str(paper.get("paper_key") or "")
            if paper_key:
                assigned.add(aliases.get(paper_key, paper_key))
    return round(len(assigned) / total, 3)


def _paper_alias_map(literature_map: LiteratureMap) -> Dict[str, str]:
    alias_to_key: Dict[str, str] = {}
    for node in literature_map.paper_nodes:
        for alias in [node.paper_key, node.canonical_paper_key, *node.aliases]:
            if alias:
                alias_to_key[str(alias)] = node.paper_key
                alias_to_key.setdefault(str(alias).casefold(), node.paper_key)
        for record in node.source_records:
            for key in ("paper_key_seen", "canonical_paper_key", "source_hash", "canonical_key"):
                value = str(record.get(key) or "")
                if value:
                    alias_to_key[value] = node.paper_key
                    alias_to_key.setdefault(value.casefold(), node.paper_key)
    return alias_to_key


def _nearest_paper_alias(
    paper_key: str,
    alias_to_key: Dict[str, str],
    *,
    min_ratio: float = 0.92,
) -> str:
    """Recover tiny provider typos while keeping unknown papers blocked."""
    if not paper_key:
        return ""
    key_cf = paper_key.casefold()
    best_alias = ""
    best_ratio = 0.0
    second_ratio = 0.0
    for alias in alias_to_key:
        alias_cf = alias.casefold()
        if abs(len(alias_cf) - len(key_cf)) > 2:
            continue
        ratio = SequenceMatcher(None, key_cf, alias_cf).ratio()
        if ratio > best_ratio:
            second_ratio = best_ratio
            best_alias = alias
            best_ratio = ratio
        elif ratio > second_ratio:
            second_ratio = ratio
    if best_alias and best_ratio >= min_ratio and best_ratio - second_ratio >= 0.03:
        return alias_to_key[best_alias]
    return ""


def _section_with_salvaged_flow_refs(
    section: CandidateSection,
    valid_step_ids: set[str],
    flow_steps: Sequence[FlowStep] | None = None,
) -> CandidateSection | None:
    original_refs = list(section.source_flow_steps)
    valid_refs = [ref for ref in original_refs if ref in valid_step_ids]
    invalid_refs = [ref for ref in original_refs if ref not in valid_step_ids]
    if original_refs and not valid_refs:
        inferred = _infer_flow_ref_for_section(section, flow_steps or [], valid_step_ids)
        if inferred:
            valid_refs = [inferred]
        else:
            return None
    if not original_refs and valid_step_ids:
        inferred = _infer_flow_ref_for_section(section, flow_steps or [], valid_step_ids)
        if inferred:
            valid_refs = [inferred]
        else:
            return None
    children = [
        child
        for child in (
            _section_with_salvaged_flow_refs(child, valid_step_ids, flow_steps)
            for child in section.children
        )
        if child is not None
    ]
    assigned_papers = [dict(paper) for paper in section.assigned_papers]
    if invalid_refs:
        for paper in assigned_papers:
            existing = str(paper.get("provider_original_invalid_refs") or "")
            combined = [item for item in existing.split(",") if item] + invalid_refs
            paper["provider_original_invalid_refs"] = ",".join(dict.fromkeys(combined))
    return CandidateSection(
        section_id=section.section_id,
        title=section.title,
        purpose=section.purpose,
        argument_role=section.argument_role,
        source_flow_steps=valid_refs,
        assigned_papers=assigned_papers,
        children=children,
    )


def _infer_flow_ref_for_section(
    section: CandidateSection,
    flow_steps: Sequence[FlowStep],
    valid_step_ids: set[str],
) -> str:
    """Infer a valid flow ref for provider sections with stale/malformed ids."""
    if is_low_quality_title(section.title):
        return ""
    if not section.assigned_papers:
        return ""
    best_score = -1
    best_id = ""
    section_text = " ".join([
        section.title,
        section.purpose,
        section.argument_role,
    ]).casefold()
    section_papers = {
        str(paper.get("paper_key") or "")
        for paper in section.assigned_papers
        if isinstance(paper, dict)
    }
    for step in flow_steps:
        if step.flow_step_id not in valid_step_ids:
            continue
        score = 0
        if section.argument_role and section.argument_role == step.role_in_review:
            score += 20
        step_text = " ".join([step.claim, step.role_in_review]).casefold()
        for token in ("gap", "future", "agenda", "空白", "未来", "议程", "mechanism", "机制", "context", "情境", "method", "方法", "problem", "问题"):
            if token in section_text and token in step_text:
                score += 3
        for ref in step.support_refs:
            if ref in section_papers:
                score += 6
        for word in step_text.split()[:12]:
            if len(word) >= 4 and word in section_text:
                score += 1
        if score > best_score:
            best_score = score
            best_id = step.flow_step_id
    return best_id if best_score > 0 else ""


def salvage_provider_candidate(
    candidate: OutlineCandidate,
    literature_map: LiteratureMap,
    synthesis_flow: SynthesisFlow,
) -> tuple[OutlineCandidate | None, List[str]]:
    """Remove invalid provider flow refs where a section still has valid refs."""
    valid_step_ids = _required_capable_step_ids(synthesis_flow)
    reasons: List[str] = []
    salvaged_sections: List[CandidateSection] = []
    for section in candidate.sections:
        salvaged = _section_with_salvaged_flow_refs(section, valid_step_ids, synthesis_flow.flow_steps)
        if salvaged is None:
            reasons.append(f"section {section.section_id} rejected during salvage: no valid flow refs")
            continue
        if list(salvaged.source_flow_steps) != list(section.source_flow_steps):
            reasons.append(f"section {section.section_id} salvaged invalid flow refs")
        salvaged_sections.append(salvaged)
    if not salvaged_sections:
        return None, reasons or ["candidate has no salvageable sections"]
    return OutlineCandidate(
        candidate_id=candidate.candidate_id,
        strategy_label=candidate.strategy_label,
        sections=salvaged_sections,
        summary=candidate.summary,
        provenance=candidate.provenance,
    ), reasons


def _ensure_minimum_real_sections(
    sections: List[CandidateSection],
    paper_nodes: List[PaperNode],
) -> List[CandidateSection]:
    """Keep only non-placeholder sections; do not invent empty-template top-up."""
    return [section for section in sections if not is_low_quality_title(section.title)]


def _ordered_flow_steps_for_strategy(flow_steps: List[FlowStep], strategy_label: str) -> List[FlowStep]:
    valid = [
        step for step in flow_steps
        if not step.placeholder_flow
        and is_required_capable_flow_role(step.role_in_review)
        and len(set(step.support_refs)) >= 2
        and not is_low_quality_title(step.claim)
    ]
    role_rankings = {
        "mechanism_driven": {
            "establish_problem_space": 0,
            "synthesize_stream": 1,
            "connect_mechanism": 2,
            "compare_contexts": 3,
            "identify_gaps": 4,
            "methodological_synthesis": 5,
        },
        "theory_evolution": {
            "establish_problem_space": 0,
            "compare_contexts": 1,
            "synthesize_stream": 2,
            "connect_mechanism": 3,
            "methodological_synthesis": 4,
            "identify_gaps": 5,
        },
        "gap_driven": {
            "identify_gaps": 0,
            "synthesize_stream": 1,
            "connect_mechanism": 2,
            "compare_contexts": 3,
            "establish_problem_space": 4,
            "methodological_synthesis": 5,
        },
    }
    ranking = role_rankings.get(strategy_label, role_rankings["mechanism_driven"])
    return sorted(valid, key=lambda step: (ranking.get(step.role_in_review, 99), step.flow_step_id))


def _ensure_candidate_minimum_coverage(
    sections: List[CandidateSection],
    paper_nodes: List[PaperNode],
    *,
    min_ratio: float = FALLBACK_GENERATION_MINIMUM,
) -> List[CandidateSection]:
    if not sections or not paper_nodes:
        return sections
    target_unique = min(len(paper_nodes), max(1, math.ceil(len(paper_nodes) * min_ratio)))
    used = {
        str(paper.get("paper_key") or "")
        for section in _all_sections(sections)
        for paper in section.assigned_papers
        if paper.get("paper_key")
    }
    if len(used) >= target_unique:
        return sections

    updated = [
        CandidateSection(
            section_id=section.section_id,
            title=section.title,
            purpose=section.purpose,
            argument_role=section.argument_role,
            source_flow_steps=section.source_flow_steps,
            assigned_papers=[dict(paper) for paper in section.assigned_papers],
            children=section.children,
        )
        for section in sections
    ]
    section_idx = 0
    for node in paper_nodes:
        if len(used) >= target_unique:
            break
        if node.paper_key in used:
            continue
        target = updated[section_idx % len(updated)]
        target.assigned_papers.append({
            "paper_key": node.paper_key,
            "title": node.title,
            "role": target.argument_role or "supporting evidence",
            "reason": "Added to meet deterministic fallback generation coverage minimum",
        })
        used.add(node.paper_key)
        section_idx += 1
    return updated


def _candidate_uniqueness_diagnostics(candidates: List[OutlineCandidate]) -> List[Dict[str, Any]]:
    diagnostics: List[Dict[str, Any]] = []
    for left_idx in range(len(candidates)):
        for right_idx in range(left_idx + 1, len(candidates)):
            left = candidates[left_idx]
            right = candidates[right_idx]
            left_titles = " | ".join(section.title.casefold() for section in left.sections)
            right_titles = " | ".join(section.title.casefold() for section in right.sections)
            title_similarity = round(SequenceMatcher(None, left_titles, right_titles).ratio(), 3)
            left_papers = {
                str(paper.get("paper_key") or "")
                for section in _all_sections(left.sections)
                for paper in section.assigned_papers
            }
            right_papers = {
                str(paper.get("paper_key") or "")
                for section in _all_sections(right.sections)
                for paper in section.assigned_papers
            }
            paper_jaccard = round(
                len(left_papers & right_papers) / max(len(left_papers | right_papers), 1),
                3,
            )
            left_steps = {step for section in _all_sections(left.sections) for step in section.source_flow_steps}
            right_steps = {step for section in _all_sections(right.sections) for step in section.source_flow_steps}
            step_jaccard = round(len(left_steps & right_steps) / max(len(left_steps | right_steps), 1), 3)
            diagnostics.append({
                "candidate_pair": [left.candidate_id, right.candidate_id],
                "section_title_similarity": title_similarity,
                "assigned_paper_jaccard": paper_jaccard,
                "flow_step_jaccard": step_jaccard,
                "near_duplicate": title_similarity >= 0.92 and paper_jaccard >= 0.85 and step_jaccard >= 0.85,
            })
    return diagnostics


def generate_candidates_deterministic(
    literature_map: LiteratureMap,
    synthesis_flow: SynthesisFlow,
    candidate_count: int = 3,
    generator_model: str = "test_double",
    job_id: str = "test",
) -> OutlineCandidates:
    """Generate outline candidates deterministically (test/dev fixture mode).

    Creates candidates from the synthesis flow with slight variations.
    """
    nodes = literature_map.paper_nodes
    steps = synthesis_flow.flow_steps

    strategies = [
        ("mechanism_driven", "Organized by causal mechanisms and processes"),
        ("theory_evolution", "Organized by theoretical development over time"),
        ("gap_driven", "Organized around identified research gaps"),
    ]

    candidates: List[OutlineCandidate] = []
    for i in range(min(candidate_count, len(strategies))):
        label, description = strategies[i]
        ordered_steps = _ordered_flow_steps_for_strategy(steps, label)
        sections = _ensure_minimum_real_sections(_build_candidate_sections(ordered_steps, nodes, label), nodes)

        # Vary sections slightly by candidate
        if i == 1:
            sections = sorted(sections, key=lambda s: s.argument_role)
        elif i == 2:
            gap_sections = [s for s in sections if "gap" in s.argument_role.lower()]
            other_sections = [s for s in sections if "gap" not in s.argument_role.lower()]
            sections = gap_sections + other_sections
        sections = _ensure_candidate_minimum_coverage(sections, nodes)

        candidates.append(OutlineCandidate(
            candidate_id=f"candidate_{i + 1}",
            strategy_label=label,
            sections=sections,
            summary=description,
            provenance="deterministic_fallback",
        ))

    return OutlineCandidates(
        source_literature_map_id=f"literature_map:{compute_content_hash(literature_map.to_dict())[:12]}",
        source_synthesis_flow_id=f"synthesis_flow:{compute_content_hash(synthesis_flow.to_dict())[:12]}",
        candidate_count=len(candidates),
        candidates=candidates,
        generator_model=generator_model,
    )


def _source_summary_packet(summaries: Sequence[Dict[str, Any]] | None) -> List[Dict[str, Any]]:
    semantic_paper_fields = (
        "title",
        "authors",
        "year",
        "date",
        "journal",
        "publication",
        "doi",
        "item_type",
        "tags",
        "abstract",
        "other",
        "canonical_paper_key",
        "source_paper_id",
        "paper_key_aliases",
    )
    semantic_legacy_fields = (
        "summary",
        "key_points",
        "themes",
        "methodology",
        "findings",
        "conclusions",
        "limitations",
        "theoretical_framework",
        "research_gap",
        "future_research_directions",
        "common_core",
        "type_specific_details",
        "routing",
        "paper_metadata",
        "core_analysis",
        "specialized_details",
        "quality_audit",
    )
    packets: List[Dict[str, Any]] = []
    for idx, summary in enumerate(summaries or [], 1):
        if not isinstance(summary, dict):
            continue
        source_paper_info = summary.get("paper_info") or {}
        paper_info = {
            field: source_paper_info[field]
            for field in semantic_paper_fields
            if isinstance(source_paper_info, dict)
            and field in source_paper_info
            and source_paper_info.get(field) not in (None, "", [], {})
        }
        packet: Dict[str, Any] = {
            "summary_index": idx,
            "paper_info": paper_info,
            "status": summary.get("status"),
            "ai_summary": dict(summary.get("ai_summary") or {}),
        }
        for field in semantic_legacy_fields:
            if field in summary and summary.get(field) not in (None, "", [], {}):
                packet[field] = summary[field]
        packets.append(packet)
    return packets


def _controlled_literature_index(literature_map: LiteratureMap) -> Dict[str, Any]:
    """Slim controlled index for provider prompts.

    Full Stage 1 summaries are supplied separately; this index only controls
    admissible paper keys and avoids duplicating source_records into the prompt.
    """
    papers: List[Dict[str, Any]] = []
    for node in literature_map.paper_nodes:
        papers.append({
            "paper_key": node.paper_key,
            "canonical_paper_key": node.canonical_paper_key or node.paper_key,
            "title": node.title,
            "authors": node.authors,
            "year": node.year,
            "classification": node.classification,
            "must_use": node.must_use,
            "aliases": list(node.aliases)[:6],
        })
    streams: List[Dict[str, Any]] = []
    for stream in literature_map.research_streams:
        streams.append({
            "stream_name": stream.get("stream_name"),
            "paper_keys": list(stream.get("paper_keys") or []),
            "source_fields": list(stream.get("source_fields") or []),
            "confidence": stream.get("confidence"),
            "thin_stream": bool(stream.get("thin_stream")),
        })
    return {
        "source_literature_map_id": f"literature_map:{compute_content_hash(literature_map.to_dict())[:12]}",
        "paper_count": len(literature_map.paper_nodes),
        "controlled_paper_keys": [node.paper_key for node in literature_map.paper_nodes],
        "papers": papers,
        "research_streams": streams,
        "paper_classification": literature_map.paper_classification,
        "blocking_diagnostic_count": len(literature_map.blocking_diagnostics),
    }


def _candidate_prompt(
    literature_map: LiteratureMap,
    synthesis_flow: SynthesisFlow,
    candidate_count: int,
    source_summaries: Sequence[Dict[str, Any]] | None = None,
    strategy_offset: int = 0,
    evidence_context: Dict[str, Any] | None = None,
) -> str:
    strategy_examples = [
        ("mechanism_driven", "Organized by causal mechanisms and processes"),
        ("theory_evolution", "Organized by theoretical development over time"),
        ("gap_driven", "Organized around identified research gaps"),
    ]
    schema_candidates = []
    for idx in range(candidate_count):
        candidate_number = strategy_offset + idx + 1
        label, summary = strategy_examples[(strategy_offset + idx) % len(strategy_examples)]
        schema_candidates.append({
            "candidate_id": f"candidate_{candidate_number}",
            "strategy_label": label,
            "summary": summary,
            "sections": [
                {
                    "section_id": f"candidate_{candidate_number}_sec_1",
                    "title": "中文章节标题",
                    "purpose": "用简体中文说明本节为什么存在",
                    "argument_role": "establish_problem_space",
                    "source_flow_steps": ["flow_step_001"],
                    "assigned_papers": [
                        {
                            "paper_key": "paper key from literature_map",
                            "role": "用简体中文说明该文献在本节中的证据角色",
                            "reason": "用简体中文说明该文献为什么属于本节",
                        }
                    ],
                    "children": [],
                }
            ],
        })
    payload = {
        "candidate_count": candidate_count,
        "target_strategy": schema_candidates[0]["strategy_label"] if candidate_count == 1 else "",
        "allowed_flow_roles": {
            "required_capable": [
                "establish_problem_space",
                "synthesize_stream",
                "connect_mechanism",
                "compare_contexts",
                "identify_gaps",
            ],
            "optional": ["methodological_synthesis"],
            "forbidden": ["diagnostic", "supporting_context", "placeholder_flow"],
        },
        "controlled_literature_index": _controlled_literature_index(literature_map),
        "synthesis_flow": synthesis_flow.to_dict(),
        "stage1_summaries_full": (
            _source_summary_packet(source_summaries) if evidence_context is None else []
        ),
        "research_stream_syntheses": evidence_context or {},
        "output_schema": {
            "candidates": schema_candidates
        },
    }
    return (
        "Generate Outline Intelligence v2 outline candidates. "
        f"Return exactly {candidate_count} candidates. Return strict JSON matching "
        "output_schema. The root JSON object must contain a top-level candidates "
        "array; do not wrap it in data/result/output, markdown, or prose. Do not "
        "write the human-facing outline in English. All human-readable prose fields "
        "(strategy_label, summary, section title, purpose, assigned_papers role/reason, "
        "and any child section title/purpose/role/reason) must be written in Simplified "
        "Chinese for a Chinese literature review. Keep machine fields such as "
        "candidate_id, section_id, argument_role, source_flow_steps, and paper_key "
        "exactly as controlled identifiers from the input. Do not translate paper_key "
        "values. Return one root JSON object only. Use only allowed flow roles/steps: "
        f"required-capable={sorted(allowed_provider_flow_roles())}; "
        f"forbidden={sorted(forbidden_provider_flow_roles())}. Every candidate "
        "must contain a sections array, and every section must cite source flow steps and assigned "
        "controlled-corpus paper keys from controlled_literature_index.controlled_paper_keys. "
        "Use stage1_summaries_full when present; otherwise use research_stream_syntheses, "
        "which is a lossless paper-key-indexed hierarchy derived from the complete Stage 1 evidence. "
        "Use this evidence for judgment, not as a replacement for controlled "
        "paper_key values.\n\n"
        + json.dumps(payload, ensure_ascii=False, indent=2)
    )


def _stream_synthesis_prompt(
    stream_name: str,
    paper_keys: Sequence[str],
    packets: Sequence[Dict[str, Any]],
) -> str:
    payload = {
        "stream_name": stream_name,
        "paper_keys": list(paper_keys),
        "packet_hashes": [packet_hash(packet) for packet in packets],
        "stage1_summary_packets": list(packets),
        "output_schema": {
            "stream_name": stream_name,
            "paper_keys": list(paper_keys),
            "themes": [],
            "tensions": [],
            "mechanisms": [],
            "methods": [],
            "gaps": [],
            "evidence_claims": [{"claim": "", "paper_keys": []}],
        },
    }
    return (
        "Synthesize this complete research-stream shard without dropping any paper. "
        "Return strict JSON matching output_schema and ground every evidence claim in paper_keys.\n\n"
        + json.dumps(payload, ensure_ascii=False, indent=2)
    )


def _merge_synthesis_prompt(records: Sequence[Dict[str, Any]]) -> str:
    paper_keys = sorted({key for record in records for key in record.get("paper_keys", [])})
    return (
        "Merge these controlled-corpus research syntheses. Preserve every paper key and return "
        "strict JSON with paper_keys and synthesis.\n\n"
        + json.dumps(
            {
                "paper_keys": paper_keys,
                "syntheses": list(records),
                "output_schema": {"paper_keys": paper_keys, "synthesis": {}},
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def _partition_for_budget(
    items: Sequence[Dict[str, Any]],
    prompt_builder: Callable[[Sequence[Dict[str, Any]]], str],
    budget: PromptBudgetV1,
    *,
    stage: str,
) -> List[List[Dict[str, Any]]]:
    groups: List[List[Dict[str, Any]]] = []
    current: List[Dict[str, Any]] = []
    for item in items:
        candidate = [*current, item]
        if budget.fits(prompt_builder(candidate)):
            current = candidate
            continue
        if not current:
            budget.assert_fits(prompt_builder([item]), stage=stage)
        groups.append(current)
        current = [item]
    if current:
        groups.append(current)
    return groups


def _budgeted_evidence_context(
    literature_map: LiteratureMap,
    synthesis_flow: SynthesisFlow,
    source_summaries: Sequence[Dict[str, Any]] | None,
    generator_model: str,
    model_caller: ModelCaller,
    budget: PromptBudgetV1,
    *,
    candidate_count: int,
    strategy_offset: int,
) -> Dict[str, Any] | None:
    full_prompt = _candidate_prompt(
        literature_map,
        synthesis_flow,
        candidate_count,
        source_summaries,
        strategy_offset,
    )
    if budget.fits(full_prompt):
        return None

    packets = _source_summary_packet(source_summaries)
    nodes = list(literature_map.paper_nodes)
    keyed_packets: List[Dict[str, Any]] = []
    for index, packet in enumerate(packets):
        paper_key = nodes[index].paper_key if index < len(nodes) else f"unmapped:{index + 1}"
        keyed_packets.append({"paper_key": paper_key, "packet": packet})

    stream_by_key: Dict[str, str] = {}
    for index, stream in enumerate(literature_map.research_streams):
        name = str(stream.get("stream_name") or f"stream_{index + 1}")
        for key in stream.get("paper_keys") or ():
            stream_by_key.setdefault(str(key), name)
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for item in keyed_packets:
        grouped.setdefault(stream_by_key.get(item["paper_key"], "unassigned"), []).append(item)

    syntheses: List[Dict[str, Any]] = []
    for stream_name, stream_items in grouped.items():
        def build_prompt(items: Sequence[Dict[str, Any]]) -> str:
            return _stream_synthesis_prompt(
                stream_name,
                [str(item["paper_key"]) for item in items],
                [dict(item["packet"]) for item in items],
            )

        for shard in _partition_for_budget(
            stream_items, build_prompt, budget, stage="outline_stream_synthesis"
        ):
            prompt = build_prompt(shard)
            raw = model_caller(
                generator_model,
                prompt,
                {
                    "stage": "outline_stream_synthesis",
                    "prompt_budget": budget.metadata(prompt),
                },
            )
            syntheses.append(
                {
                    "stream_name": stream_name,
                    "paper_keys": [str(item["paper_key"]) for item in shard],
                    "packet_hashes": [packet_hash(item["packet"]) for item in shard],
                    "synthesis": raw,
                }
            )

    context: Dict[str, Any] = {"level": 0, "syntheses": syntheses}
    merge_level = 0
    while not budget.fits(
        _candidate_prompt(
            literature_map,
            synthesis_flow,
            candidate_count,
            None,
            strategy_offset,
            context,
        )
    ):
        merge_level += 1
        if merge_level > 20:
            raise OutlinePromptBudgetExceeded("research-stream synthesis did not converge")
        groups = _partition_for_budget(
            syntheses, _merge_synthesis_prompt, budget, stage="outline_synthesis_merge"
        )
        if len(groups) == len(syntheses) and all(len(group) == 1 for group in groups):
            raise OutlinePromptBudgetExceeded(
                "indivisible research-stream synthesis exceeds final candidate budget"
            )
        merged: List[Dict[str, Any]] = []
        for group in groups:
            prompt = _merge_synthesis_prompt(group)
            raw = model_caller(
                generator_model,
                prompt,
                {
                    "stage": "outline_synthesis_merge",
                    "prompt_budget": budget.metadata(prompt),
                },
            )
            merged.append(
                {
                    "paper_keys": sorted(
                        {key for item in group for key in item.get("paper_keys", [])}
                    ),
                    "synthesis": raw,
                }
            )
        syntheses = merged
        context = {"level": merge_level, "syntheses": syntheses}
    return context


def _first_present(data: Dict[str, Any], keys: List[str], default: Any = None) -> Any:
    for key in keys:
        if key in data:
            return data[key]
    return default


def _coerce_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, str):
        return [value] if value.strip() else []
    return [value]


def _dict_values_with_ids(raw: Dict[str, Any], id_field: str) -> List[Any]:
    items: List[Any] = []
    for key, value in raw.items():
        if isinstance(value, dict):
            copied = dict(value)
            copied.setdefault(id_field, key)
            items.append(copied)
        else:
            items.append(value)
    return items


def _extract_candidate_items(raw_output: Dict[str, Any]) -> List[Any]:
    """Extract candidate items from common provider wrapper shapes."""
    candidate_keys = [
        "candidates",
        "outline_candidates",
        "outlineCandidates",
        "candidate_outlines",
        "outline_options",
        "outlines",
        "候选大纲",
        "大纲候选",
        "候选方案",
        "大纲方案",
    ]
    for key in candidate_keys:
        value = raw_output.get(key)
        if isinstance(value, list):
            return value
        if isinstance(value, dict):
            if _extract_candidate_sections_value(value):
                return [value]
            return _dict_values_with_ids(value, "candidate_id")

    for wrapper_key in ["data", "result", "output", "response"]:
        wrapper = raw_output.get(wrapper_key)
        if isinstance(wrapper, dict):
            nested = _extract_candidate_items(wrapper)
            if nested:
                return nested

    if _extract_candidate_sections_value(raw_output):
        return [raw_output]

    candidate_items: List[Any] = []
    for key, value in raw_output.items():
        normalized_key = str(key).lower()
        if (
            isinstance(value, dict)
            and (
                normalized_key.startswith(("candidate", "outline"))
                or _extract_candidate_sections_value(value)
            )
            and _extract_candidate_sections_value(value)
        ):
            copied = dict(value)
            copied.setdefault("candidate_id", str(key))
            candidate_items.append(copied)
    return candidate_items


def _force_provider_candidate_identity(item: Dict[str, Any], candidate_index: int) -> Dict[str, Any]:
    """Force stable IDs for per-strategy provider calls.

    DeepSeek often follows the one-candidate schema literally and returns
    candidate_1 for every split request. The pipeline needs unique candidate
    and section ids so critique/arbitration targets cannot collapse.
    """
    candidate_id = f"candidate_{candidate_index}"
    copied = dict(item)
    old_candidate_id = str(copied.get("candidate_id") or "")
    copied["candidate_id"] = candidate_id

    def rewrite_sections(value: Any) -> Any:
        if isinstance(value, list):
            return [rewrite_sections(child) for child in value]
        if not isinstance(value, dict):
            return value
        section = dict(value)
        raw_section_id = str(section.get("section_id") or section.get("id") or "")
        if raw_section_id:
            if old_candidate_id and raw_section_id.startswith(old_candidate_id):
                section["section_id"] = candidate_id + raw_section_id[len(old_candidate_id):]
            elif raw_section_id.startswith("candidate_1"):
                section["section_id"] = candidate_id + raw_section_id[len("candidate_1"):]
            elif not raw_section_id.startswith(candidate_id):
                section["section_id"] = f"{candidate_id}_{raw_section_id}"
        for key in ("sections", "children", "subsections", "outline_sections", "chapters"):
            if key in section:
                section[key] = rewrite_sections(section[key])
        return section

    for key in ("sections", "outline_sections", "outlineSections", "chapters"):
        if key in copied:
            copied[key] = rewrite_sections(copied[key])
    if isinstance(copied.get("outline"), dict):
        copied["outline"] = rewrite_sections(copied["outline"])
    return copied


def _extract_sections_value(item: Dict[str, Any]) -> Any:
    for key in [
        "sections",
        "outline_sections",
        "outlineSections",
        "section_outline",
        "outline_structure",
        "outlineStructure",
        "chapters",
        "章节",
        "大纲",
        "结构",
        "部分",
    ]:
        value = item.get(key)
        if value:
            return value

    outline = item.get("outline")
    if isinstance(outline, dict):
        for key in ["sections", "outline_sections", "chapters"]:
            value = outline.get(key)
            if value:
                return value
        return outline
    if isinstance(outline, list):
        return outline

    return None


def _looks_like_flat_section(item: Dict[str, Any]) -> bool:
    return any(
        key in item
        for key in [
            "section_id",
            "section_title",
            "title",
            "heading",
            "purpose",
            "argument_role",
            "source_flow_steps",
            "flow_steps",
            "paper_key",
            "assigned_papers",
            "papers",
            "paper_keys",
        ]
    )


def _extract_candidate_sections_value(item: Dict[str, Any]) -> Any:
    sections = _extract_sections_value(item)
    if sections:
        return sections
    if _looks_like_flat_section(item):
        return [item]
    return None


def _normalize_flow_step_refs(value: Any) -> List[str]:
    refs: List[str] = []
    for item in _coerce_list(value):
        if isinstance(item, dict):
            ref = _first_present(item, ["flow_step_id", "source_flow_step", "id", "key", "流程步骤"])
            if ref:
                refs.append(str(ref))
        elif item is not None:
            refs.append(str(item))
    return refs


def _normalize_assigned_papers(value: Any, context: Optional[Dict[str, Any]] = None) -> List[Dict[str, str]]:
    papers: List[Dict[str, str]] = []
    if isinstance(value, dict):
        iterable = _dict_values_with_ids(value, "paper_key")
    else:
        iterable = _coerce_list(value)

    for item in iterable:
        if isinstance(item, str):
            paper_key = item.strip()
            if paper_key:
                paper = {"paper_key": paper_key}
                if context:
                    role = context.get("paper_role") or context.get("role")
                    reason = context.get("paper_reason") or context.get("reason")
                    if role:
                        paper["role"] = str(role)
                    if reason:
                        paper["reason"] = str(reason)
                papers.append(paper)
            continue

        if not isinstance(item, dict):
            continue

        copied = {str(key): str(val) for key, val in item.items() if val is not None}
        paper_key = _first_present(
            copied,
            ["paper_key", "canonical_paper_key", "paper_id", "id", "key", "ref"],
        )
        if paper_key:
            copied["paper_key"] = str(paper_key)
        if "paper_key" in copied:
            papers.append(copied)
    if not papers and context:
        paper_key = _first_present(
            context,
            ["paper_key", "canonical_paper_key", "paper_id", "ref"],
        )
        if paper_key:
            paper = {"paper_key": str(paper_key)}
            role = context.get("paper_role") or context.get("role")
            reason = context.get("paper_reason") or context.get("reason")
            if role:
                paper["role"] = str(role)
            if reason:
                paper["reason"] = str(reason)
            papers.append(paper)
    return papers


def validate_candidate(
    candidate: OutlineCandidate,
    literature_map: LiteratureMap,
    synthesis_flow: SynthesisFlow,
    *,
    strict: bool = True,
    quality_gate: OutlineQualityGateConfig | None = None,
) -> List[str]:
    """Validate candidate integrity before arbitration/adoption."""
    errors: List[str] = []
    policy = quality_gate or OutlineQualityGateConfig()
    known_papers = {node.paper_key for node in literature_map.paper_nodes}
    known_steps = {
        step.flow_step_id for step in synthesis_flow.flow_steps
        if not step.placeholder_flow and step.role_in_review != "diagnostic"
        and is_required_capable_flow_role(step.role_in_review)
    }
    sections = _all_sections(candidate.sections)
    if strict:
        for section in sections:
            if is_low_quality_title(section.title):
                errors.append(f"placeholder section title: {section.title!r}")
            if not section.purpose.strip() and not section.argument_role.strip():
                errors.append(f"section {section.section_id} has empty purpose/argument_role")
    if strict:
        effective = [
            section for section in sections
            if not is_low_quality_title(section.title)
            and (section.purpose.strip() or section.argument_role.strip())
            and section.assigned_papers
        ]
        if len(effective) < policy.min_effective_sections:
            errors.append(
                f"candidate {candidate.candidate_id} has fewer than "
                f"{policy.min_effective_sections} effective sections"
            )

    for section in sections:
        if not section.source_flow_steps and known_steps:
            errors.append(f"section {section.section_id} has no source flow refs")
        for step_id in section.source_flow_steps:
            if strict and step_id not in known_steps:
                errors.append(f"section {section.section_id} references invalid/diagnostic flow step {step_id}")
        seen_papers: set[str] = set()
        for paper in section.assigned_papers:
            paper_key = str(paper.get("paper_key") or "")
            if paper_key not in known_papers:
                errors.append(f"section {section.section_id} references unknown paper {paper_key}")
            if paper_key in seen_papers:
                errors.append(f"section {section.section_id} repeats canonical paper {paper_key}")
            seen_papers.add(paper_key)
    return errors


def _parse_candidate_sections(raw_sections: Any) -> List[CandidateSection]:
    sections: List[CandidateSection] = []
    if isinstance(raw_sections, dict):
        nested = _extract_sections_value(raw_sections)
        if nested is not None and nested is not raw_sections:
            raw_sections = nested
        else:
            raw_sections = _dict_values_with_ids(raw_sections, "section_id")
    if not isinstance(raw_sections, list):
        return sections
    for idx, item in enumerate(raw_sections, 1):
        if not isinstance(item, dict):
            continue
        child_value = _first_present(
            item,
            ["children", "subsections", "sub_sections", "subSections", "sections", "子章节", "小节"],
            [],
        )
        children = _parse_candidate_sections(child_value)
        assigned = _first_present(
            item,
            [
                "assigned_papers",
                "papers",
                "paper_keys",
                "supporting_papers",
                "evidence_refs",
                "support_refs",
                "文献",
                "相关文献",
                "支撑文献",
            ],
            [],
        )
        if not assigned:
            assigned = _first_present(
                item,
                ["paper_key", "canonical_paper_key", "paper_id"],
                [],
            )
        source_steps = _first_present(
            item,
            [
                "source_flow_steps",
                "flow_steps",
                "flow_step_ids",
                "source_steps",
                "support_flow_steps",
                "流程步骤",
                "来源流程步骤",
            ],
            [],
        )
        sections.append(CandidateSection(
            section_id=str(_first_present(item, ["section_id", "id", "key", "编号"], f"cand_sec_{idx:03d}")),
            title=str(_first_present(item, ["title", "heading", "section_title", "name", "标题"], f"Section {idx}")),
            purpose=str(_first_present(item, ["purpose", "rationale", "description", "focus", "目的", "说明"], "")),
            argument_role=str(_first_present(item, ["argument_role", "role", "function", "role_in_review", "角色"], "")),
            source_flow_steps=_normalize_flow_step_refs(source_steps),
            assigned_papers=_normalize_assigned_papers(assigned, item),
            children=children,
        ))
    return sections


class CandidateGenerationError(ValueError):
    """Raised when provider + deterministic fallback cannot produce strict-valid candidates."""

    def __init__(self, message: str, report: Dict[str, Any]):
        super().__init__(message)
        self.report = report


def _minimum_viable_candidate_count(candidate_count: int) -> int:
    """Return the minimum strict-valid candidates needed to continue arbitration."""
    return max(1, min(int(candidate_count or 0), 2))


def _report_rejection(
    report: Dict[str, Any],
    *,
    source: str,
    candidate_index: int,
    candidate_id: str,
    stage: str,
    reasons: List[str],
) -> None:
    report.setdefault("rejected_reasons", []).append({
        "source": source,
        "candidate_index": candidate_index,
        "candidate_id": candidate_id,
        "stage": stage,
        "reasons": reasons,
    })


def _base_generation_report(
    *,
    candidate_count: int,
    generator_model: str,
    top_keys: str = "",
) -> Dict[str, Any]:
    return {
        "artifact_type": "candidate_generation_report",
        "artifact_version": "v1",
        "created_at": _utc_now_iso(),
        "generator_model": generator_model,
        "requested_candidate_count": candidate_count,
        "minimum_viable_count": _minimum_viable_candidate_count(candidate_count),
        "provider_top_level_keys": top_keys,
        "provider_total": 0,
        "provider_valid": 0,
        "rejected_reasons": [],
        "fallback_triggered": False,
        "fallback_generated": 0,
        "fallback_valid": 0,
        "fallback_rejected": 0,
        "salvage": [],
        "fallback_strategy_diagnostics": [],
        "fallback_uniqueness": [],
        "candidate_coverage": {},
        "final_valid_count": 0,
        "pipeline_continued": False,
    }


def _finalize_generation_report(
    report: Dict[str, Any],
    candidates: List[OutlineCandidate],
    *,
    pipeline_continued: bool | None = None,
) -> None:
    report["final_valid_count"] = len(candidates)
    report["pipeline_continued"] = bool(candidates) if pipeline_continued is None else pipeline_continued


def _top_up_missing_candidates(
    candidates: List[OutlineCandidate],
    literature_map: LiteratureMap,
    synthesis_flow: SynthesisFlow,
    candidate_count: int,
    generator_model: str,
    report: Dict[str, Any],
    quality_gate: OutlineQualityGateConfig | None = None,
) -> List[OutlineCandidate]:
    if len(candidates) >= candidate_count:
        return candidates
    if not any(
        not step.placeholder_flow
        and step.role_in_review != "diagnostic"
        and len(set(step.support_refs)) >= 2
        for step in synthesis_flow.flow_steps
    ):
        report["fallback_triggered"] = True
        _report_rejection(
            report,
            source="fallback",
            candidate_index=0,
            candidate_id="<not-generated>",
            stage="precondition",
            reasons=["deterministic fallback unavailable: no non-diagnostic multi-paper synthesis flow steps"],
        )
        return candidates

    original_count = len(candidates)
    existing_ids = {candidate.candidate_id for candidate in candidates}
    report["fallback_triggered"] = True
    fallback = generate_candidates_deterministic(
        literature_map,
        synthesis_flow,
        candidate_count=candidate_count,
        generator_model=f"{generator_model}:deterministic_topup",
    )

    topped_up = list(candidates)
    report["fallback_generated"] = len(fallback.candidates)
    report["fallback_uniqueness"] = _candidate_uniqueness_diagnostics(fallback.candidates)
    for idx, fallback_candidate in enumerate(fallback.candidates, 1):
        if len(topped_up) >= candidate_count:
            break
        validation_errors = validate_candidate(
            fallback_candidate,
            literature_map,
            synthesis_flow,
            strict=True,
            quality_gate=quality_gate,
        )
        coverage_ratio = _candidate_canonical_coverage(fallback_candidate, literature_map)
        report.setdefault("candidate_coverage", {})[fallback_candidate.candidate_id] = coverage_ratio
        report.setdefault("fallback_strategy_diagnostics", []).append({
            "candidate_id": fallback_candidate.candidate_id,
            "strategy_label": fallback_candidate.strategy_label,
            "canonical_coverage_ratio": coverage_ratio,
            "generation_minimum": FALLBACK_GENERATION_MINIMUM,
            "final_adoption_threshold": FINAL_ADOPTION_THRESHOLD,
            "adoption_eligible": coverage_ratio >= FINAL_ADOPTION_THRESHOLD,
        })
        if coverage_ratio < FALLBACK_GENERATION_MINIMUM:
            validation_errors.append(
                f"fallback canonical coverage {coverage_ratio:.3f} below generation minimum {FALLBACK_GENERATION_MINIMUM:.3f}"
            )
        if validation_errors:
            report["fallback_rejected"] += 1
            _report_rejection(
                report,
                source="fallback",
                candidate_index=idx,
                candidate_id=fallback_candidate.candidate_id,
                stage="strict_validate",
                reasons=validation_errors,
            )
            continue
        candidate_id = fallback_candidate.candidate_id
        if candidate_id in existing_ids:
            candidate_id = f"candidate_{len(topped_up) + 1}"
        existing_ids.add(candidate_id)
        topped_up.append(OutlineCandidate(
            candidate_id=candidate_id,
            strategy_label=fallback_candidate.strategy_label,
            sections=fallback_candidate.sections,
            summary=(
                f"{fallback_candidate.summary} "
                f"(deterministic top-up after provider returned {original_count} valid candidates)"
            ),
            provenance="deterministic_topup",
        ))
        report["fallback_valid"] += 1
    return topped_up


def _candidate_with_canonical_papers(candidate: OutlineCandidate, literature_map: LiteratureMap) -> OutlineCandidate:
    alias_to_key = _paper_alias_map(literature_map)

    def convert_section(section: CandidateSection) -> CandidateSection:
        converted_papers: List[Dict[str, str]] = []
        for paper in section.assigned_papers:
            copied = dict(paper)
            paper_key = str(copied.get("paper_key") or "")
            if paper_key in alias_to_key:
                copied["paper_key"] = alias_to_key[paper_key]
            elif paper_key.casefold() in alias_to_key:
                copied["paper_key"] = alias_to_key[paper_key.casefold()]
            else:
                recovered = _nearest_paper_alias(paper_key, alias_to_key)
                if recovered:
                    copied["provider_original_paper_key"] = paper_key
                    copied["paper_key"] = recovered
            converted_papers.append(copied)
        return CandidateSection(
            section_id=section.section_id,
            title=section.title,
            purpose=section.purpose,
            argument_role=section.argument_role,
            source_flow_steps=section.source_flow_steps,
            assigned_papers=converted_papers,
            children=[convert_section(child) for child in section.children],
        )

    return OutlineCandidate(
        candidate_id=candidate.candidate_id,
        strategy_label=candidate.strategy_label,
        sections=[convert_section(section) for section in candidate.sections],
        summary=candidate.summary,
        provenance=candidate.provenance,
    )


def _parse_provider_candidate(
    item: Dict[str, Any],
    idx: int,
    literature_map: LiteratureMap,
) -> OutlineCandidate:
    candidate_id = str(_first_present(item, ["candidate_id", "id", "key", "编号"], f"candidate_{idx}"))
    sections = _parse_candidate_sections(_extract_candidate_sections_value(item))
    if not sections:
        raise ValueError(f"Candidate {candidate_id} has no sections")
    candidate = OutlineCandidate(
        candidate_id=candidate_id,
        strategy_label=str(_first_present(item, ["strategy_label", "strategy", "approach", "label", "策略"], f"candidate_{idx}")),
        sections=sections,
        summary=str(_first_present(item, ["summary", "rationale", "description", "摘要", "说明"], "")),
        provenance=str(_first_present(item, ["provenance", "source", "candidate_source"], "provider")),
    )
    return _candidate_with_canonical_papers(candidate, literature_map)


def _strict_validate_or_reasons(
    candidate: OutlineCandidate,
    literature_map: LiteratureMap,
    synthesis_flow: SynthesisFlow,
    quality_gate: OutlineQualityGateConfig | None = None,
) -> List[str]:
    return validate_candidate(candidate, literature_map, synthesis_flow, strict=True, quality_gate=quality_gate)


def _extract_raw_candidate_list(raw_output: Any) -> tuple[List[Any], str]:
    if isinstance(raw_output, str):
        raw_output = json.loads(raw_output)
    if isinstance(raw_output, list):
        return raw_output, "<array-root>"
    if isinstance(raw_output, dict):
        raw_candidates = _extract_candidate_items(raw_output)
        top_keys = ", ".join(str(key) for key in raw_output.keys()) or "<none>"
        if not isinstance(raw_candidates, list):
            raise ValueError("Candidate output missing candidate list")
        return raw_candidates, top_keys
    raise ValueError(f"Unexpected candidate output type: {type(raw_output).__name__}")


def _raw_top_level_keys(raw_output: Any) -> str:
    if isinstance(raw_output, dict):
        return ", ".join(str(key) for key in raw_output.keys()) or "<none>"
    if isinstance(raw_output, list):
        return "<array-root>"
    return type(raw_output).__name__


def normalize_candidate_output_with_report(
    raw_output: Any,
    literature_map: LiteratureMap,
    synthesis_flow: SynthesisFlow,
    candidate_count: int,
    generator_model: str,
    *,
    allow_deterministic_fallback: bool = False,
    quality_gate: OutlineQualityGateConfig | None = None,
) -> tuple[OutlineCandidates, Dict[str, Any]]:
    """Normalize provider JSON and return strict-valid candidates plus diagnostics."""
    try:
        raw_candidates, top_keys = _extract_raw_candidate_list(raw_output)
    except Exception as exc:
        report = _base_generation_report(
            candidate_count=candidate_count,
            generator_model=generator_model,
        )
        _report_rejection(
            report,
            source="provider",
            candidate_index=0,
            candidate_id="<parse>",
            stage="provider_output_parse",
            reasons=[str(exc)],
        )
        candidates: List[OutlineCandidate] = []
        if allow_deterministic_fallback:
            candidates = _top_up_missing_candidates(
                [],
                literature_map,
                synthesis_flow,
                candidate_count,
                generator_model,
                report,
                quality_gate,
            )
        if len(candidates) >= _minimum_viable_candidate_count(candidate_count):
            outline_candidates = OutlineCandidates(
                source_literature_map_id=f"literature_map:{compute_content_hash(literature_map.to_dict())[:12]}",
                source_synthesis_flow_id=f"synthesis_flow:{compute_content_hash(synthesis_flow.to_dict())[:12]}",
                candidate_count=len(candidates),
                candidates=candidates,
                generator_model=generator_model,
            )
            _finalize_generation_report(report, candidates, pipeline_continued=True)
            return outline_candidates, report
        _finalize_generation_report(report, candidates, pipeline_continued=False)
        raise CandidateGenerationError(
            f"Candidate provider output could not be parsed; deterministic fallback "
            f"{'produced ' + str(len(candidates)) + ' valid candidates' if allow_deterministic_fallback else 'is disabled'}; "
            f"expected {candidate_count}: {exc}",
            report,
        ) from exc

    report = _base_generation_report(
        candidate_count=candidate_count,
        generator_model=generator_model,
        top_keys=top_keys,
    )
    if isinstance(raw_output, dict) and raw_output.get("provider_strategy_errors"):
        report["provider_strategy_errors"] = list(raw_output.get("provider_strategy_errors") or [])
    report["provider_total"] = len(raw_candidates)

    candidates: List[OutlineCandidate] = []
    for idx, item in enumerate(raw_candidates, 1):
        if not isinstance(item, dict):
            _report_rejection(
                report,
                source="provider",
                candidate_index=idx,
                candidate_id=f"candidate_{idx}",
                stage="single_candidate_normalize",
                reasons=[f"candidate item is {type(item).__name__}, expected object"],
            )
            continue
        try:
            candidate = _parse_provider_candidate(item, idx, literature_map)
        except Exception as exc:
            candidate_id = str(_first_present(item, ["candidate_id", "id", "key", "编号"], f"candidate_{idx}"))
            _report_rejection(
                report,
                source="provider",
                candidate_index=idx,
                candidate_id=candidate_id,
                stage="single_candidate_normalize",
                reasons=[str(exc)],
            )
            continue

        salvage_notes: List[str] = []
        salvaged_candidate, salvage_notes = salvage_provider_candidate(candidate, literature_map, synthesis_flow)
        if salvaged_candidate is None:
            _report_rejection(
                report,
                source="provider",
                candidate_index=idx,
                candidate_id=candidate.candidate_id,
                stage="salvage",
                reasons=salvage_notes,
            )
            continue
        candidate = salvaged_candidate
        if salvage_notes:
            report.setdefault("salvage", []).append({
                "candidate_id": candidate.candidate_id,
                "notes": salvage_notes,
            })

        validation_errors = _strict_validate_or_reasons(candidate, literature_map, synthesis_flow, quality_gate)
        if validation_errors:
            _report_rejection(
                report,
                source="provider",
                candidate_index=idx,
                candidate_id=candidate.candidate_id,
                stage="strict_validate",
                reasons=validation_errors,
            )
            continue
        candidates.append(candidate)
        report["provider_valid"] += 1

    if allow_deterministic_fallback:
        candidates = _top_up_missing_candidates(
            candidates,
            literature_map,
            synthesis_flow,
            candidate_count,
            generator_model,
            report,
            quality_gate,
        )

    final_rejections: List[str] = []
    for candidate in candidates:
        errors = _strict_validate_or_reasons(candidate, literature_map, synthesis_flow, quality_gate)
        if errors:
            final_rejections.append(f"{candidate.candidate_id}: " + "; ".join(errors[:6]))
    if final_rejections:
        _report_rejection(
            report,
            source="final",
            candidate_index=0,
            candidate_id="<final-set>",
            stage="final_candidate_set_assemble",
            reasons=final_rejections,
        )
        _finalize_generation_report(report, candidates, pipeline_continued=False)
        raise CandidateGenerationError(
            "Final candidate set contains invalid candidates: " + " | ".join(final_rejections),
            report,
        )

    minimum_viable_count = _minimum_viable_candidate_count(candidate_count)
    if len(candidates) < minimum_viable_count:
        _finalize_generation_report(report, candidates, pipeline_continued=False)
        reason_parts = [
            f"Candidate output contained {len(candidates)} valid candidates",
            f"expected {candidate_count}",
            f"minimum viable {minimum_viable_count}",
            f"top-level keys: {top_keys}",
        ]
        rejection_summary = [
            f"{item['source']}#{item['candidate_index']} {item['candidate_id']}: "
            + "; ".join(item.get("reasons", [])[:3])
            for item in report.get("rejected_reasons", [])
        ]
        if rejection_summary:
            reason_parts.append("rejections: " + " | ".join(rejection_summary[:12]))
        raise CandidateGenerationError(
            "; ".join(reason_parts),
            report,
        )

    outline_candidates = OutlineCandidates(
        source_literature_map_id=f"literature_map:{compute_content_hash(literature_map.to_dict())[:12]}",
        source_synthesis_flow_id=f"synthesis_flow:{compute_content_hash(synthesis_flow.to_dict())[:12]}",
        candidate_count=len(candidates),
        candidates=candidates,
        generator_model=generator_model,
    )
    _finalize_generation_report(report, candidates, pipeline_continued=True)
    return outline_candidates, report


def normalize_candidate_output(
    raw_output: Any,
    literature_map: LiteratureMap,
    synthesis_flow: SynthesisFlow,
    candidate_count: int,
    generator_model: str,
    *,
    allow_deterministic_fallback: bool = False,
    quality_gate: OutlineQualityGateConfig | None = None,
) -> OutlineCandidates:
    """Normalize provider JSON into strict-valid project-owned OutlineCandidates."""
    candidates, _report = normalize_candidate_output_with_report(
        raw_output,
        literature_map,
        synthesis_flow,
        candidate_count,
        generator_model,
        allow_deterministic_fallback=allow_deterministic_fallback,
        quality_gate=quality_gate,
    )
    return candidates


def deterministic_candidate_generation_report(
    candidates: OutlineCandidates,
    candidate_count: int,
    generator_model: str,
    literature_map: LiteratureMap | None = None,
    synthesis_flow: SynthesisFlow | None = None,
    quality_gate: OutlineQualityGateConfig | None = None,
) -> Dict[str, Any]:
    """Build a sidecar report for deterministic fixture-mode generation."""
    report = _base_generation_report(
        candidate_count=candidate_count,
        generator_model=generator_model,
        top_keys="<deterministic-fixture>",
    )
    report["fallback_generated"] = len(candidates.candidates)
    if literature_map is not None and synthesis_flow is not None:
        report["fallback_uniqueness"] = _candidate_uniqueness_diagnostics(candidates.candidates)
        valid_count = 0
        for idx, candidate in enumerate(candidates.candidates, 1):
            errors = validate_candidate(
                candidate,
                literature_map,
                synthesis_flow,
                strict=True,
                quality_gate=quality_gate,
            )
            if errors:
                report["fallback_rejected"] += 1
                _report_rejection(
                    report,
                    source="fallback",
                    candidate_index=idx,
                    candidate_id=candidate.candidate_id,
                    stage="strict_validate",
                    reasons=errors,
                )
            else:
                valid_count += 1
            coverage_ratio = _candidate_canonical_coverage(candidate, literature_map)
            report.setdefault("candidate_coverage", {})[candidate.candidate_id] = coverage_ratio
            report.setdefault("fallback_strategy_diagnostics", []).append({
                "candidate_id": candidate.candidate_id,
                "strategy_label": candidate.strategy_label,
                "canonical_coverage_ratio": coverage_ratio,
                "generation_minimum": FALLBACK_GENERATION_MINIMUM,
                "final_adoption_threshold": FINAL_ADOPTION_THRESHOLD,
                "adoption_eligible": coverage_ratio >= FINAL_ADOPTION_THRESHOLD,
            })
        report["fallback_valid"] = valid_count
    else:
        report["fallback_valid"] = len(candidates.candidates)
    report["fallback_triggered"] = True
    _finalize_generation_report(report, candidates.candidates, pipeline_continued=True)
    return report


def generate_candidates_production_with_report(
    literature_map: LiteratureMap,
    synthesis_flow: SynthesisFlow,
    candidate_count: int,
    generator_model: str,
    model_caller: ModelCaller | None,
    quality_gate: OutlineQualityGateConfig | None = None,
    source_summaries: Sequence[Dict[str, Any]] | None = None,
    prompt_budget: PromptBudgetV1 | None = None,
) -> tuple[OutlineCandidates, Dict[str, Any]]:
    """Generate candidates through Outline_API and return diagnostics sidecar."""
    if model_caller is None:
        raise RuntimeError(
            "Production v2 candidate generation requires a model_caller for Outline_API"
        )
    if candidate_count > 1:
        raw_candidates: List[Any] = []
        per_strategy_errors: List[Dict[str, Any]] = []
        shared_evidence_context = (
            _budgeted_evidence_context(
                literature_map,
                synthesis_flow,
                source_summaries,
                generator_model,
                model_caller,
                prompt_budget,
                candidate_count=1,
                strategy_offset=0,
            )
            if prompt_budget is not None
            else None
        )
        for idx in range(candidate_count):
            prompt = _candidate_prompt(
                literature_map,
                synthesis_flow,
                1,
                source_summaries,
                strategy_offset=idx,
                evidence_context=shared_evidence_context,
            )
            if prompt_budget is not None:
                prompt_budget.assert_fits(prompt, stage="outline_candidates")
            raw_output = model_caller(
                generator_model,
                prompt,
                {
                    "stage": "outline_candidates",
                    "candidate_index": idx + 1,
                    "prompt_budget": prompt_budget.metadata(prompt) if prompt_budget else {},
                    "semantic_retry": 0,
                },
            )

            attempt_diagnostics: List[Dict[str, Any]] = []

            def first_candidate_item(raw_value: Any, *, attempt: int) -> tuple[Any | None, bool]:
                try:
                    one_raw, top_keys = _extract_raw_candidate_list(raw_value)
                except Exception as exc:
                    attempt_diagnostics.append({
                        "attempt": attempt,
                        "top_level_keys": _raw_top_level_keys(raw_value),
                        "reason": str(exc),
                    })
                    return None, False
                if not one_raw:
                    attempt_diagnostics.append({
                        "attempt": attempt,
                        "top_level_keys": top_keys,
                        "reason": "candidate output contained no candidate items",
                    })
                    return None, False
                item = one_raw[0]
                if not isinstance(item, dict):
                    attempt_diagnostics.append({
                        "attempt": attempt,
                        "top_level_keys": top_keys,
                        "reason": f"candidate item is {type(item).__name__}, expected object",
                    })
                    return None, False
                if not _extract_candidate_sections_value(item):
                    attempt_diagnostics.append({
                        "attempt": attempt,
                        "top_level_keys": top_keys,
                        "reason": "candidate item has no usable sections",
                    })
                    return None, True
                return _force_provider_candidate_identity(item, idx + 1), False

            item, semantic_retry_allowed = first_candidate_item(raw_output, attempt=1)
            if item is not None:
                raw_candidates.append(item)
                continue
            if not semantic_retry_allowed:
                per_strategy_errors.append({
                    "candidate_index": idx + 1,
                    "recovered_by_semantic_retry": False,
                    "semantic_retry_skipped": True,
                    "attempts": attempt_diagnostics,
                })
                raw_candidates.append({
                    "candidate_id": f"candidate_{idx + 1}",
                    "sections": [],
                    "provider_error": "empty_or_unparseable_single_candidate_response",
                    "provider_attempt_diagnostics": attempt_diagnostics,
                })
                continue

            retry_reason = attempt_diagnostics[-1]["reason"] if attempt_diagnostics else "missing candidate sections"
            retry_prompt = (
                prompt
                + "\n\nSEMANTIC RETRY FOR THIS STRATEGY ONLY:\n"
                + f"The previous JSON for candidate_{idx + 1} was rejected before validation because {retry_reason}. "
                + "Return exactly one candidate object inside a top-level candidates array. "
                + "The candidate must contain a non-empty sections array. Do not return an empty outline, "
                + "diagnostics, prose, or a restatement of the schema."
            )
            if prompt_budget is not None:
                prompt_budget.assert_fits(retry_prompt, stage="outline_candidates")
            retry_raw_output = model_caller(
                generator_model,
                retry_prompt,
                {
                    "stage": "outline_candidates",
                    "candidate_index": idx + 1,
                    "prompt_budget": prompt_budget.metadata(retry_prompt) if prompt_budget else {},
                    "semantic_retry": 1,
                    "previous_provider_error": retry_reason,
                },
            )
            item, _semantic_retry_allowed = first_candidate_item(retry_raw_output, attempt=2)
            if item is not None:
                raw_candidates.append(item)
                per_strategy_errors.append({
                    "candidate_index": idx + 1,
                    "recovered_by_semantic_retry": True,
                    "attempts": attempt_diagnostics,
                })
                continue

            per_strategy_errors.append({
                "candidate_index": idx + 1,
                "recovered_by_semantic_retry": False,
                "attempts": attempt_diagnostics,
            })
            raw_candidates.append({
                "candidate_id": f"candidate_{idx + 1}",
                "sections": [],
                "provider_error": "empty_or_unparseable_single_candidate_response",
                "provider_attempt_diagnostics": attempt_diagnostics,
            })
        raw_output = {"candidates": raw_candidates}
        if per_strategy_errors:
            raw_output["provider_strategy_errors"] = per_strategy_errors
    else:
        evidence_context = (
            _budgeted_evidence_context(
                literature_map,
                synthesis_flow,
                source_summaries,
                generator_model,
                model_caller,
                prompt_budget,
                candidate_count=candidate_count,
                strategy_offset=0,
            )
            if prompt_budget is not None
            else None
        )
        prompt = _candidate_prompt(
            literature_map,
            synthesis_flow,
            candidate_count,
            source_summaries,
            evidence_context=evidence_context,
        )
        if prompt_budget is not None:
            prompt_budget.assert_fits(prompt, stage="outline_candidates")
        raw_output = model_caller(
            generator_model,
            prompt,
            {
                "stage": "outline_candidates",
                "prompt_budget": prompt_budget.metadata(prompt) if prompt_budget else {},
            },
        )
    return normalize_candidate_output_with_report(
        raw_output,
        literature_map,
        synthesis_flow,
        candidate_count,
        generator_model,
        allow_deterministic_fallback=False,
        quality_gate=quality_gate,
    )

def generate_candidates_production(
    literature_map: LiteratureMap,
    synthesis_flow: SynthesisFlow,
    candidate_count: int,
    generator_model: str,
    model_caller: ModelCaller | None,
    quality_gate: OutlineQualityGateConfig | None = None,
) -> OutlineCandidates:
    """Generate candidates through the configured production Outline_API route."""
    candidates, _report = generate_candidates_production_with_report(
        literature_map, synthesis_flow, candidate_count, generator_model, model_caller, quality_gate
    )
    return candidates


def validate_candidate_count(count: int, test_dev_mode: bool = False) -> List[str]:
    """Validate candidate_count. Returns list of error messages."""
    errors = []
    if test_dev_mode:
        if count < 1:
            errors.append(f"candidate_count={count} must be at least 1 even in test/dev mode")
        return errors
    if count < 2:
        errors.append(f"candidate_count={count} is below production minimum of 2")
    if count > 3:
        errors.append(f"candidate_count={count} exceeds production maximum of 3")
    return errors
