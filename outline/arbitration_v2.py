"""Arbitration and final outline generation for Outline Intelligence v2.

Production v2 uses Outline_API to arbitrate over candidates and critiques.
Produces outline_arbitration_report.json and final_outline.json.
"""

from __future__ import annotations

import uuid
import json
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from outline.critique_v2 import BLOCKING_CRITIQUE_CATEGORIES
from outline.quality_rules import is_low_quality_title, is_required_flow_role
from outline.v2_models import (
    ArbitrationReport,
    FinalOutline,
    FinalSection,
    LiteratureMap,
    OutlineCandidates,
    OutlineCritiquesV2,
    SynthesisFlow,
    compute_content_hash,
)


ModelCaller = Callable[[str, str, Dict[str, Any]], Any]


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _is_deterministic_fallback_candidate(candidate: Any | None) -> bool:
    if candidate is None:
        return False
    provenance = str(getattr(candidate, "provenance", "") or "")
    summary = str(getattr(candidate, "summary", "") or "")
    return (
        provenance in {"deterministic_fallback", "deterministic_topup"}
        or "deterministic top-up" in summary
    )


def _final_section_from_candidate(sec: Any) -> FinalSection:
    return FinalSection(
        section_id=sec.section_id,
        title=sec.title,
        purpose=sec.purpose,
        argument_role=sec.argument_role,
        source_flow_steps=list(sec.source_flow_steps),
        assigned_papers=[dict(paper) for paper in sec.assigned_papers],
        children=[_final_section_from_candidate(child) for child in getattr(sec, "children", [])],
    )


def _section_from_dict(raw: Dict[str, Any]) -> FinalSection:
    return FinalSection(
        section_id=str(raw.get("section_id") or raw.get("id") or f"final_sec_{uuid.uuid4().hex[:6]}"),
        title=str(raw.get("title") or raw.get("heading") or ""),
        purpose=str(raw.get("purpose") or ""),
        argument_role=str(raw.get("argument_role") or raw.get("role") or ""),
        source_flow_steps=[str(item) for item in raw.get("source_flow_steps", [])],
        assigned_papers=[
            {str(k): str(v) for k, v in item.items()}
            for item in raw.get("assigned_papers", [])
            if isinstance(item, dict)
        ],
        children=[
            _section_from_dict(child)
            for child in raw.get("children", [])
            if isinstance(child, dict)
        ],
    )


def _validate_final_sections(
    sections: List[FinalSection],
    *,
    known_flow_steps: set[str] | None = None,
    known_papers: set[str] | None = None,
    min_effective_sections: int = 3,
) -> List[str]:
    errors: List[str] = []
    effective_count = 0
    for section in sections:
        if is_low_quality_title(section.title):
            errors.append(f"low_quality_title:{section.section_id}")
        if not section.purpose.strip() and not section.argument_role.strip():
            errors.append(f"missing_purpose_or_role:{section.section_id}")
        if not section.source_flow_steps:
            errors.append(f"missing_flow_refs:{section.section_id}")
        for flow_step in section.source_flow_steps:
            if known_flow_steps is not None and flow_step not in known_flow_steps:
                errors.append(f"unknown_flow_ref:{section.section_id}:{flow_step}")
        if not section.assigned_papers:
            errors.append(f"missing_assigned_papers:{section.section_id}")
        seen_papers: set[str] = set()
        for paper in section.assigned_papers:
            paper_key = str(paper.get("paper_key") or "")
            if known_papers is not None and paper_key and paper_key not in known_papers:
                errors.append(f"unknown_paper_ref:{section.section_id}:{paper_key}")
            if paper_key in seen_papers:
                errors.append(f"duplicate_paper_ref:{section.section_id}:{paper_key}")
            seen_papers.add(paper_key)
        if (
            not is_low_quality_title(section.title)
            and section.assigned_papers
            and section.source_flow_steps
            and (section.purpose.strip() or section.argument_role.strip())
        ):
            effective_count += 1
        errors.extend(
            _validate_final_sections(
                section.children,
                known_flow_steps=known_flow_steps,
                known_papers=known_papers,
                min_effective_sections=0,
            )
        )
    if min_effective_sections and effective_count < min_effective_sections:
        errors.append(f"insufficient_effective_sections:{effective_count}<{min_effective_sections}")
    return errors


def _candidate_reference_sets(candidates: OutlineCandidates) -> tuple[set[str], set[str]]:
    known_flow_steps: set[str] = set()
    known_papers: set[str] = set()
    for candidate in candidates.candidates:
        for section in candidate.sections:
            stack = [section]
            while stack:
                current = stack.pop()
                known_flow_steps.update(str(item) for item in current.source_flow_steps if item)
                known_papers.update(
                    str(paper.get("paper_key") or "")
                    for paper in current.assigned_papers
                    if paper.get("paper_key")
                )
                stack.extend(current.children)
    return known_flow_steps, known_papers


def _candidate_coverage(candidate: Any) -> int:
    papers: set[str] = set()
    paper_refs: List[str] = []
    steps: set[str] = set()
    for section in getattr(candidate, "sections", []):
        stack = [section]
        while stack:
            current = stack.pop()
            steps.update(str(item) for item in getattr(current, "source_flow_steps", []) if item)
            for paper in getattr(current, "assigned_papers", []):
                if isinstance(paper, dict) and paper.get("paper_key"):
                    paper_key = str(paper.get("paper_key") or "")
                    papers.add(paper_key)
                    paper_refs.append(paper_key)
            stack.extend(getattr(current, "children", []))
    duplicate_refs = max(len(paper_refs) - len(papers), 0)
    return (len(papers) * 20) + (len(steps) * 10) - (duplicate_refs * 8)


def _critique_targets_candidate(critique: Any, candidate_id: str) -> bool:
    target = str(getattr(critique, "target_candidate_id", "") or "")
    return not target or target == candidate_id


def _fallback_arbitration_report(
    candidates: OutlineCandidates,
    critiques: OutlineCritiquesV2,
    arbitrator_model: str,
    *,
    reason: str,
) -> ArbitrationReport:
    """Build a deterministic arbitration report from already validated candidates."""
    candidate_ids = [candidate.candidate_id for candidate in candidates.candidates]
    critique_ids = [critique.critique_id for critique in critiques.critiques]
    def score(candidate: Any) -> int:
        candidate_id = str(getattr(candidate, "candidate_id", "") or "")
        blocking_count = sum(
            1
            for critique in critiques.critiques
            if critique.severity == "high"
            and critique.category in BLOCKING_CRITIQUE_CATEGORIES
            and _critique_targets_candidate(critique, candidate_id)
        )
        return _candidate_coverage(candidate) - (blocking_count * 1000)

    selected = max(candidates.candidates, key=score, default=None)
    selected_id = selected.candidate_id if selected else (candidate_ids[0] if candidate_ids else "")
    scores = {
        candidate.candidate_id: float(_candidate_coverage(candidate))
        for candidate in candidates.candidates
    }
    blocking = [
        critique.critique_id
        for critique in critiques.critiques
        if critique.severity == "high" and critique.category in BLOCKING_CRITIQUE_CATEGORIES
        and _critique_targets_candidate(critique, selected_id)
    ]
    accepted = [
        critique.critique_id
        for critique in critiques.critiques
        if critique.critique_id not in blocking
        and critique.category not in BLOCKING_CRITIQUE_CATEGORIES
    ]
    rejected = list(dict.fromkeys([
        critique.critique_id
        for critique in critiques.critiques
        if critique.critique_id not in accepted
    ] + blocking))
    final_decision: Dict[str, Any] = {
        "selected_base_candidate": selected_id,
        "strategy": "fallback_select_highest_validated_coverage_candidate",
        "fallback_reason": reason,
        "blocking_critique_ids": blocking,
        "requires_revised_sections": False,
    }
    return ArbitrationReport(
        source_candidates=candidate_ids,
        source_critiques=critique_ids,
        candidate_scores=scores,
        accepted_points=accepted,
        rejected_points=rejected,
        merged_strategy="fallback_select_validated_candidate_after_provider_failure",
        final_decision=final_decision,
        arbitrator_model=arbitrator_model,
    )


def arbitrate_deterministic(
    candidates: OutlineCandidates,
    critiques: OutlineCritiquesV2,
    arbitrator_model: str = "test_double",
) -> ArbitrationReport:
    """Run arbitration deterministically (test/dev fixture mode).

    Selects the first candidate and applies accepted critique points.
    Production uses real Outline_API arbitration.
    """
    candidate_ids = [c.candidate_id for c in candidates.candidates]
    critique_ids = [c.critique_id for c in critiques.critiques]

    # Simple scoring: first candidate gets highest score
    candidate_scores: Dict[str, float] = {}
    for i, c in enumerate(candidates.candidates):
        candidate_scores[c.candidate_id] = 1.0 - (i * 0.1)

    # Accept non-blocking critiques, keep high-severity blocking critiques unresolved.
    accepted: List[str] = []
    rejected: List[str] = []
    for c in critiques.critiques:
        if c.severity in ("low", "medium") and c.category not in BLOCKING_CRITIQUE_CATEGORIES:
            accepted.append(c.critique_id)
        else:
            rejected.append(c.critique_id)

    selected_candidate = candidates.candidates[0] if candidates.candidates else None
    merged_strategy = "select_base_candidate_with_accepted_critiques"
    if selected_candidate:
        merged_strategy += f" base={selected_candidate.candidate_id}"

    high_blocking = [
        c.critique_id
        for c in critiques.critiques
        if c.severity == "high" and c.category in BLOCKING_CRITIQUE_CATEGORIES
        and (not selected_candidate or _critique_targets_candidate(c, selected_candidate.candidate_id))
    ]
    requires_revised = bool(selected_candidate and (_is_deterministic_fallback_candidate(selected_candidate) or high_blocking))
    final_decision: Dict[str, Any] = {
        "selected_base_candidate": selected_candidate.candidate_id if selected_candidate else "",
        "strategy": merged_strategy,
        "accepted_count": len(accepted),
        "rejected_count": len(rejected),
        "blocking_critique_ids": high_blocking,
        "requires_revised_sections": requires_revised,
    }
    if selected_candidate and requires_revised and not high_blocking:
        final_decision["revised_sections"] = [section.to_dict() for section in selected_candidate.sections]

    return ArbitrationReport(
        source_candidates=candidate_ids,
        source_critiques=critique_ids,
        candidate_scores=candidate_scores,
        accepted_points=accepted,
        rejected_points=rejected,
        merged_strategy=merged_strategy,
        final_decision=final_decision,
        arbitrator_model=arbitrator_model,
    )


def build_final_outline(
    candidates: OutlineCandidates,
    arbitration_report: ArbitrationReport,
    literature_map_hash: str,
    synthesis_flow_hash: str,
    job_id: str,
) -> FinalOutline:
    """Build final_outline.json from arbitration result.

    Takes the selected base candidate and applies accepted modifications.
    """
    selected_id = arbitration_report.final_decision.get("selected_base_candidate", "")
    base_candidate = None
    for c in candidates.candidates:
        if c.candidate_id == selected_id:
            base_candidate = c
            break

    if base_candidate is None and candidates.candidates:
        base_candidate = candidates.candidates[0]

    sections: List[FinalSection] = []
    excluded_papers: List[Dict[str, str]] = []
    revised_sections = arbitration_report.final_decision.get("revised_sections")
    blocking_critique_ids = [
        str(item) for item in arbitration_report.final_decision.get("blocking_critique_ids", [])
    ]
    unresolved_critique_ids = list(dict.fromkeys(
        [str(item) for item in arbitration_report.rejected_points] + blocking_critique_ids
    ))
    applied_critique_ids = [str(item) for item in arbitration_report.accepted_points]

    valid_revisions = False
    revision_source = "base_candidate"
    if isinstance(revised_sections, list) and revised_sections:
        sections = [_section_from_dict(item) for item in revised_sections if isinstance(item, dict)]
        known_flow_steps, known_papers = _candidate_reference_sets(candidates)
        revision_errors = _validate_final_sections(
            sections,
            known_flow_steps=known_flow_steps,
            known_papers=known_papers,
        )
        valid_revisions = bool(sections) and not revision_errors
        revision_source = "arbitrator_revised_sections" if valid_revisions else "invalid_revised_sections"

    if base_candidate and not valid_revisions:
        sections = [_final_section_from_candidate(sec) for sec in base_candidate.sections]
        selected_is_fallback = _is_deterministic_fallback_candidate(base_candidate)
        if selected_is_fallback and not revised_sections:
            revision_source = "copied_base_blocked"

    review_status = "arbitrated"
    selected_is_fallback = _is_deterministic_fallback_candidate(base_candidate)
    requires_revised = bool(arbitration_report.final_decision.get("requires_revised_sections"))
    if (
        (blocking_critique_ids and not valid_revisions)
        or (selected_is_fallback and not valid_revisions)
        or (requires_revised and not valid_revisions)
    ):
        review_status = "blocked"
    if valid_revisions:
        unresolved_critique_ids = [crit for crit in unresolved_critique_ids if crit not in applied_critique_ids]
        blocking_critique_ids = [crit for crit in blocking_critique_ids if crit not in applied_critique_ids]
        if blocking_critique_ids:
            review_status = "blocked"

    final_decision = dict(arbitration_report.final_decision)
    final_decision["revision_source"] = revision_source
    arbitration_report = ArbitrationReport(
        source_candidates=arbitration_report.source_candidates,
        source_critiques=arbitration_report.source_critiques,
        candidate_scores=arbitration_report.candidate_scores,
        accepted_points=arbitration_report.accepted_points,
        rejected_points=arbitration_report.rejected_points,
        merged_strategy=arbitration_report.merged_strategy,
        final_decision=final_decision,
        arbitrator_model=arbitration_report.arbitrator_model,
    )

    outline = FinalOutline(
        created_from_job_id=job_id,
        outline_id=str(uuid.uuid4()),
        source_literature_map_id=candidates.source_literature_map_id,
        source_synthesis_flow_id=candidates.source_synthesis_flow_id,
        source_arbitration_report_id=f"arbitration:{compute_content_hash(arbitration_report.to_dict())[:12]}",
        source_literature_map_hash=literature_map_hash,
        source_synthesis_flow_hash=synthesis_flow_hash,
        review_status=review_status,
        adoption_status="pending_user_adoption",
        sections=sections,
        excluded_papers=excluded_papers,
        applied_critique_ids=applied_critique_ids,
        unresolved_critique_ids=unresolved_critique_ids,
        blocking_critique_ids=blocking_critique_ids,
    )
    return outline


def _canonical_alias_map(literature_map: LiteratureMap) -> Dict[str, str]:
    aliases: Dict[str, str] = {}
    for node in literature_map.paper_nodes:
        canonical = node.canonical_paper_key or node.paper_key
        for alias in [node.paper_key, canonical, *node.aliases]:
            if alias:
                aliases[str(alias)] = canonical
    return aliases


def _copy_final_section(
    section: FinalSection,
    *,
    source_flow_steps: List[str] | None = None,
    assigned_papers: List[Dict[str, str]] | None = None,
    children: List[FinalSection] | None = None,
) -> FinalSection:
    return FinalSection(
        section_id=section.section_id,
        title=section.title,
        purpose=section.purpose,
        argument_role=section.argument_role,
        source_flow_steps=list(section.source_flow_steps if source_flow_steps is None else source_flow_steps),
        assigned_papers=[dict(paper) for paper in (section.assigned_papers if assigned_papers is None else assigned_papers)],
        children=list(section.children if children is None else children),
    )


def _section_match_score(section: FinalSection, flow_step: Any) -> int:
    role = str(getattr(flow_step, "role_in_review", "") or "")
    claim = str(getattr(flow_step, "claim", "") or "").casefold()
    text = f"{section.title} {section.purpose} {section.argument_role}".casefold()
    score = 0
    if section.argument_role == role:
        score += 8
    if role == "establish_problem_space" and any(token in text for token in ("problem", "concept", "landscape", "space", "背景", "概念")):
        score += 6
    if role == "identify_gaps" and any(token in text for token in ("gap", "future", "agenda", "limitation", "不足", "未来")):
        score += 6
    if role == "connect_mechanism" and any(token in text for token in ("mechanism", "emotion", "trust", "context", "机制", "情绪", "信任")):
        score += 4
    for term in claim.replace("synthesis of ", "").split()[:5]:
        if len(term) >= 4 and term in text:
            score += 2
    return score


def _best_section_index(sections: List[FinalSection], flow_step: Any) -> int:
    if not sections:
        return -1
    scored = [(_section_match_score(section, flow_step), -idx, idx) for idx, section in enumerate(sections)]
    return max(scored)[2]


def complete_final_outline_coverage(
    final_outline: FinalOutline,
    literature_map: LiteratureMap,
    synthesis_flow: SynthesisFlow,
    *,
    min_canonical_coverage: float = 0.50,
) -> FinalOutline:
    """Deterministically close evidence coverage gaps in a selected outline.

    The model owns the section framing; this pass enforces controlled-corpus
    invariants that are mechanical: no duplicate paper assignment, required flow
    steps represented, must-use papers assigned, and minimum canonical coverage.
    """
    if not final_outline.sections:
        return final_outline

    alias_map = _canonical_alias_map(literature_map)
    paper_by_key = {node.paper_key: node for node in literature_map.paper_nodes}
    paper_by_canonical = {node.canonical_paper_key or node.paper_key: node for node in literature_map.paper_nodes}
    sections = [_copy_final_section(section) for section in final_outline.sections]
    used_canonical: set[str] = set()

    def dedupe_section(section: FinalSection) -> FinalSection:
        assigned: List[Dict[str, str]] = []
        for paper in section.assigned_papers:
            paper_key = str(paper.get("paper_key") or "")
            canonical = alias_map.get(paper_key, paper_key)
            if not paper_key or canonical in used_canonical:
                continue
            copied = dict(paper)
            node = paper_by_canonical.get(canonical)
            if node:
                copied["paper_key"] = node.paper_key
            used_canonical.add(canonical)
            assigned.append(copied)
        return _copy_final_section(
            section,
            assigned_papers=assigned,
            children=[dedupe_section(child) for child in section.children],
        )

    sections = [dedupe_section(section) for section in sections]

    flow_by_id = {step.flow_step_id: step for step in synthesis_flow.flow_steps}

    def filter_flow_refs(section: FinalSection) -> FinalSection:
        return _copy_final_section(
            section,
            source_flow_steps=[
                step_id for step_id in section.source_flow_steps
                if step_id in flow_by_id
            ],
            children=[filter_flow_refs(child) for child in section.children],
        )

    sections = [filter_flow_refs(section) for section in sections]

    def section_paths(current_sections: List[FinalSection], prefix: Tuple[int, ...] = ()) -> List[Tuple[Tuple[int, ...], FinalSection]]:
        paths: List[Tuple[Tuple[int, ...], FinalSection]] = []
        for idx, section in enumerate(current_sections):
            path = (*prefix, idx)
            paths.append((path, section))
            paths.extend(section_paths(section.children, path))
        return paths

    def get_section(path: Tuple[int, ...]) -> FinalSection:
        current = sections[path[0]]
        for idx in path[1:]:
            current = current.children[idx]
        return current

    def replace_section(path: Tuple[int, ...], replacement: FinalSection) -> None:
        nonlocal sections

        def replace_in(items: List[FinalSection], depth: int) -> List[FinalSection]:
            updated = list(items)
            idx = path[depth]
            if depth == len(path) - 1:
                updated[idx] = replacement
            else:
                parent = updated[idx]
                updated[idx] = _copy_final_section(
                    parent,
                    children=replace_in(parent.children, depth + 1),
                )
            return updated

        sections = replace_in(sections, 0)

    def covered_flow_step_ids() -> set[str]:
        return {
            step_id
            for _path, section in section_paths(sections)
            for step_id in section.source_flow_steps
            if step_id in flow_by_id
        }

    def best_section_path(flow_step: Any) -> Tuple[int, ...] | None:
        paths = section_paths(sections)
        if not paths:
            return None
        scored = [
            (_section_match_score(section, flow_step), tuple(-part for part in path), path)
            for path, section in paths
        ]
        return max(scored)[2]

    required_steps = [
        step for step in synthesis_flow.flow_steps
        if not step.placeholder_flow and is_required_flow_role(step.role_in_review)
    ]
    covered_steps = covered_flow_step_ids()

    def add_paper_to_section(section_path: Tuple[int, ...], paper_key: str, role: str, reason: str) -> None:
        node = paper_by_key.get(paper_key)
        canonical = alias_map.get(paper_key, paper_key)
        if not node or canonical in used_canonical:
            return
        section = get_section(section_path)
        assigned = [dict(paper) for paper in section.assigned_papers]
        assigned.append({
            "paper_key": node.paper_key,
            "title": node.title,
            "role": role,
            "reason": reason,
        })
        used_canonical.add(canonical)
        replace_section(section_path, _copy_final_section(section, assigned_papers=assigned))

    for step in required_steps:
        if step.flow_step_id not in covered_steps:
            path = best_section_path(step)
            if path is not None:
                section = get_section(path)
                source_flow_steps = list(dict.fromkeys([*section.source_flow_steps, step.flow_step_id]))
                replace_section(path, _copy_final_section(section, source_flow_steps=source_flow_steps))
                covered_steps.add(step.flow_step_id)
        path = best_section_path(step)
        if path is not None:
            for ref in step.support_refs:
                if alias_map.get(ref, ref) not in used_canonical:
                    add_paper_to_section(
                        path,
                        ref,
                        str(step.role_in_review),
                        f"Supports required synthesis flow step {step.flow_step_id}: {step.claim[:120]}",
                    )
                    break

    if sections:
        for node in literature_map.paper_nodes:
            canonical = node.canonical_paper_key or node.paper_key
            if node.must_use and canonical not in used_canonical:
                add_paper_to_section(
                    (0,),
                    node.paper_key,
                    "must-use controlled-corpus evidence",
                    "Core/must-use paper added by deterministic final outline coverage pass",
                )

        target_unique = max(1, int((len(paper_by_canonical) * min_canonical_coverage) + 0.999))
        section_idx = 0
        paths = [path for path, _section in section_paths(sections)] or [(0,)]
        for node in literature_map.paper_nodes:
            if len(used_canonical) >= target_unique:
                break
            canonical = node.canonical_paper_key or node.paper_key
            if canonical in used_canonical:
                continue
            add_paper_to_section(
                paths[section_idx % len(paths)],
                node.paper_key,
                "controlled-corpus coverage evidence",
                "Added to satisfy final outline canonical coverage gate",
            )
            section_idx += 1

        for path, section in list(section_paths(sections)):
            if section.assigned_papers:
                continue
            for node in literature_map.paper_nodes:
                canonical = node.canonical_paper_key or node.paper_key
                if canonical not in used_canonical:
                    add_paper_to_section(
                        path,
                        node.paper_key,
                        section.argument_role or "controlled-corpus section support",
                        "Added because every final outline section requires supporting papers",
                    )
                    break

    blocking_critique_ids = list(final_outline.blocking_critique_ids)
    review_status = "blocked" if blocking_critique_ids else "arbitrated"
    return FinalOutline(
        created_from_job_id=final_outline.created_from_job_id,
        outline_id=final_outline.outline_id,
        source_literature_map_id=final_outline.source_literature_map_id,
        source_synthesis_flow_id=final_outline.source_synthesis_flow_id,
        source_arbitration_report_id=final_outline.source_arbitration_report_id,
        source_literature_map_hash=final_outline.source_literature_map_hash,
        source_synthesis_flow_hash=final_outline.source_synthesis_flow_hash,
        review_status=review_status,
        adoption_status=final_outline.adoption_status,
        sections=sections,
        excluded_papers=[dict(item) for item in final_outline.excluded_papers],
        applied_critique_ids=list(final_outline.applied_critique_ids),
        unresolved_critique_ids=list(final_outline.unresolved_critique_ids),
        blocking_critique_ids=blocking_critique_ids,
    )


def _arbitration_prompt(candidates: OutlineCandidates, critiques: OutlineCritiquesV2) -> str:
    has_high_blocking = any(
        critique.severity == "high" and critique.category in BLOCKING_CRITIQUE_CATEGORIES
        for critique in critiques.critiques
    )
    return (
        "Arbitrate Outline Intelligence v2 outline candidates and critiques. "
        "Return strict JSON containing source_candidates, source_critiques, "
        "candidate_scores, accepted_points, rejected_points, merged_strategy, "
        "and final_decision.selected_base_candidate. If there is any high severity "
        "blocking critique, if the selected candidate is a deterministic fallback, "
        "or if candidates are highly repetitive, final_decision MUST include "
        "revised_sections using the final section schema: section_id, title, "
        "purpose, argument_role, source_flow_steps, assigned_papers, children. "
        "When writing revised_sections, all human-readable prose fields (title, "
        "purpose, assigned_papers role/reason, and child section prose fields) "
        "must be Simplified Chinese for a Chinese literature review. Keep machine "
        "fields such as section_id, argument_role, source_flow_steps, and paper_key "
        "exactly as controlled identifiers; do not translate paper_key values. "
        "Use only provided candidate and critique ids.\n\n"
        + json.dumps(
            {
                "requires_revised_sections_when": {
                    "high_severity_blocking_critique": has_high_blocking,
                    "selected_deterministic_fallback": "candidate provenance is deterministic_fallback/deterministic_topup or summary says deterministic top-up",
                    "candidate_repetition": "high title/purpose/paper/flow overlap",
                },
                "outline_candidates": candidates.to_dict(),
                "outline_critiques": critiques.to_dict(),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def normalize_arbitration_output(
    raw_output: Any,
    candidates: OutlineCandidates,
    critiques: OutlineCritiquesV2,
    arbitrator_model: str,
) -> ArbitrationReport:
    """Normalize provider arbitration JSON into ArbitrationReport."""
    if isinstance(raw_output, str):
        raw_output = json.loads(raw_output)
    if not isinstance(raw_output, dict):
        raise ValueError(f"Unexpected arbitration output type: {type(raw_output).__name__}")

    candidate_ids = [candidate.candidate_id for candidate in candidates.candidates]
    critique_ids = [critique.critique_id for critique in critiques.critiques]
    scores = raw_output.get("candidate_scores", {})
    if not isinstance(scores, dict):
        scores = {}

    final_decision = raw_output.get("final_decision", {})
    if not isinstance(final_decision, dict):
        final_decision = {}
    selected = str(final_decision.get("selected_base_candidate") or "")
    if selected not in candidate_ids:
        raise ValueError("Arbitration output selected an unknown candidate")

    accepted = [str(item) for item in raw_output.get("accepted_points", []) if str(item) in critique_ids]
    rejected = [str(item) for item in raw_output.get("rejected_points", []) if str(item) in critique_ids]
    blocking_critique_ids = [
        critique.critique_id
        for critique in critiques.critiques
        if critique.severity == "high" and critique.category in BLOCKING_CRITIQUE_CATEGORIES
        and _critique_targets_candidate(critique, selected)
    ]
    for critique_id in blocking_critique_ids:
        if critique_id not in rejected:
            rejected.append(critique_id)
    final_decision["blocking_critique_ids"] = list(dict.fromkeys(
        [str(item) for item in final_decision.get("blocking_critique_ids", [])]
        + blocking_critique_ids
    ))
    final_decision["requires_revised_sections"] = bool(
        final_decision.get("requires_revised_sections")
        or blocking_critique_ids
        or any(
            candidate.candidate_id == selected and _is_deterministic_fallback_candidate(candidate)
            for candidate in candidates.candidates
        )
    )

    return ArbitrationReport(
        source_candidates=[str(item) for item in raw_output.get("source_candidates", candidate_ids)],
        source_critiques=[str(item) for item in raw_output.get("source_critiques", critique_ids)],
        candidate_scores={str(key): float(value) for key, value in scores.items()},
        accepted_points=accepted,
        rejected_points=rejected,
        merged_strategy=str(raw_output.get("merged_strategy") or "provider_arbitration"),
        final_decision=final_decision,
        arbitrator_model=arbitrator_model,
    )


def arbitrate_production(
    candidates: OutlineCandidates,
    critiques: OutlineCritiquesV2,
    arbitrator_model: str,
    model_caller: ModelCaller | None,
) -> ArbitrationReport:
    """Run arbitration through the configured production Outline_API route."""
    if model_caller is None:
        raise RuntimeError(
            "Production v2 arbitration requires a model_caller for Outline_API"
        )
    raw_output = model_caller(
        arbitrator_model,
        _arbitration_prompt(candidates, critiques),
        {"stage": "outline_arbitration"},
    )
    try:
        return normalize_arbitration_output(raw_output, candidates, critiques, arbitrator_model)
    except Exception as exc:
        return _fallback_arbitration_report(
            candidates,
            critiques,
            arbitrator_model,
            reason=f"provider_arbitration_failed:{type(exc).__name__}:{exc}",
        )
