"""Deterministic coverage audit for Outline Intelligence v2.

Audits final_outline.json against literature_map.json and synthesis_flow.json.
Blocks adoption on canonical coverage, duplicate assignment, placeholder, and
diagnostic-only flow failures.
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Dict, List, Set

from outline.quality_rules import is_low_quality_title, is_required_flow_role
from outline.v2_config import OutlineQualityGateConfig
from outline.v2_models import (
    CoverageAudit,
    CoverageIssue,
    FinalOutline,
    LiteratureMap,
    SynthesisFlow,
    compute_content_hash,
)


def _collect_assigned_papers(sections) -> Set[str]:
    """Recursively collect all assigned paper keys from sections."""
    papers: Set[str] = set()
    for sec in sections:
        for ap in getattr(sec, "assigned_papers", []):
            if isinstance(ap, dict):
                pk = ap.get("paper_key", "")
                if pk:
                    papers.add(pk)
        children = getattr(sec, "children", [])
        papers.update(_collect_assigned_papers(children))
    return papers


def _collect_assigned_paper_list(sections) -> List[str]:
    papers: List[str] = []
    for sec in sections:
        for ap in getattr(sec, "assigned_papers", []):
            if isinstance(ap, dict):
                pk = str(ap.get("paper_key", "")).strip()
                if pk:
                    papers.append(pk)
        papers.extend(_collect_assigned_paper_list(getattr(sec, "children", [])))
    return papers


def _collect_covered_flow_steps(sections) -> Set[str]:
    """Recursively collect all covered flow step IDs from sections."""
    steps: Set[str] = set()
    for sec in sections:
        source_steps = getattr(sec, "source_flow_steps", [])
        steps.update(source_steps)
        children = getattr(sec, "children", [])
        steps.update(_collect_covered_flow_steps(children))
    return steps


def _walk_sections(sections) -> List[Any]:
    all_sections: List[Any] = []
    for sec in sections:
        all_sections.append(sec)
        all_sections.extend(_walk_sections(getattr(sec, "children", [])))
    return all_sections


def _canonical_alias_map(literature_map: LiteratureMap) -> Dict[str, str]:
    aliases: Dict[str, str] = {}
    for node in literature_map.paper_nodes:
        canonical = node.canonical_paper_key or node.paper_key
        aliases[node.paper_key] = canonical
        aliases[canonical] = canonical
        for alias in node.aliases:
            aliases[str(alias)] = canonical
        for record in node.source_records:
            for key in ("paper_key_seen", "canonical_paper_key", "source_hash"):
                value = str(record.get(key) or "")
                if value:
                    aliases[value] = canonical
    return aliases


def _issue(issue_type: str, description: str, **kwargs: str) -> CoverageIssue:
    return CoverageIssue(issue_type=issue_type, description=description, **kwargs)


def run_coverage_audit(
    final_outline: FinalOutline,
    literature_map: LiteratureMap,
    synthesis_flow: SynthesisFlow,
    quality_gate: OutlineQualityGateConfig | None = None,
) -> CoverageAudit:
    """Run deterministic coverage audit."""
    policy = quality_gate or OutlineQualityGateConfig()
    blocking: List[CoverageIssue] = []
    warnings: List[CoverageIssue] = []

    alias_map = _canonical_alias_map(literature_map)
    assigned_raw_list = _collect_assigned_paper_list(final_outline.sections)
    assigned_papers = set(assigned_raw_list)
    assigned_canonical_list = [alias_map.get(paper, paper) for paper in assigned_raw_list]
    assigned_canonical = set(assigned_canonical_list)
    all_raw_keys = {node.paper_key for node in literature_map.paper_nodes}
    all_canonical_keys = {node.canonical_paper_key or node.paper_key for node in literature_map.paper_nodes}

    covered_flow_steps = _collect_covered_flow_steps(final_outline.sections)
    valid_flow_step_ids = {
        step.flow_step_id
        for step in synthesis_flow.flow_steps
        if not step.placeholder_flow and is_required_flow_role(step.role_in_review)
    }
    all_flow_step_ids = {step.flow_step_id for step in synthesis_flow.flow_steps}
    # 1. Missing paper refs.
    for pk in assigned_papers:
        if pk not in alias_map and pk not in all_raw_keys:
            blocking.append(_issue(
                "missing_paper_node",
                f"Paper '{pk}' assigned in outline but not in literature map",
                paper_key=pk,
            ))

    # 2. Classification / must-use coverage.
    for node in literature_map.paper_nodes:
        canonical = node.canonical_paper_key or node.paper_key
        if node.classification == "unknown":
            blocking.append(_issue(
                "unclassified_paper",
                f"Paper '{node.paper_key}' ({node.title}) is unclassified",
                paper_key=node.paper_key,
            ))
        if node.must_use and canonical not in assigned_canonical:
            blocking.append(_issue(
                "missing_core_paper" if node.classification == "core" else "orphan_paper",
                f"Core/must_use paper '{node.paper_key}' ({node.title}) not in final outline",
                paper_key=node.paper_key,
            ))
        elif not node.must_use and canonical not in assigned_canonical:
            warnings.append(_issue(
                "orphan_paper",
                f"Non-core paper '{node.paper_key}' ({node.title}) not in final outline (metrics only)",
                paper_key=node.paper_key,
            ))

    # 3. Flow support.
    central_gap = synthesis_flow.central_gap
    if central_gap and not central_gap.get("support_refs"):
        blocking.append(_issue("unsupported_gap_claim", "Central gap claim has no supporting paper refs"))

    for step_id in valid_flow_step_ids:
        if step_id not in covered_flow_steps:
            blocking.append(_issue(
                "flow_step_uncovered",
                f"Flow step '{step_id}' is not covered by any final outline section",
                flow_step_id=step_id,
            ))
    for step_id in covered_flow_steps:
        if step_id not in all_flow_step_ids:
            blocking.append(_issue(
                "invalid_flow_ref",
                f"Section references unknown flow step '{step_id}'",
                flow_step_id=step_id,
            ))

    # 4. Recursive section checks.
    sections = _walk_sections(final_outline.sections)
    placeholder_sections = [sec for sec in sections if is_low_quality_title(getattr(sec, "title", ""))]
    effective_sections = [
        sec for sec in sections
        if not is_low_quality_title(getattr(sec, "title", ""))
        and getattr(sec, "assigned_papers", [])
        and (getattr(sec, "purpose", "") or getattr(sec, "argument_role", ""))
    ]
    for sec in sections:
        section_papers = [
            alias_map.get(str(ap.get("paper_key") or ""), str(ap.get("paper_key") or ""))
            for ap in getattr(sec, "assigned_papers", [])
            if isinstance(ap, dict)
        ]
        duplicate_in_section = sum(count - 1 for count in Counter(section_papers).values() if count > 1)
        if duplicate_in_section:
            blocking.append(_issue(
                "duplicate_canonical_assignment",
                f"Section '{sec.title}' repeats canonical paper assignment",
                section_id=sec.section_id,
            ))
        if not getattr(sec, "assigned_papers", []):
            blocking.append(_issue(
                "section_without_supporting_papers",
                f"Section '{sec.title}' has no supporting papers assigned",
                section_id=sec.section_id,
            ))
        if is_low_quality_title(getattr(sec, "title", "")):
            blocking.append(_issue(
                "placeholder_section",
                f"Section '{getattr(sec, 'title', '')}' is a placeholder/diagnostic label",
                section_id=getattr(sec, "section_id", ""),
            ))

    # 5. Quality-gate metrics.
    duplicate_assignment_count = sum(
        count - 1 for count in Counter(assigned_canonical_list).values() if count > 1
    )
    raw_covered = len(assigned_papers & all_raw_keys)
    canonical_covered = len(assigned_canonical & all_canonical_keys)
    raw_node_coverage_ratio = round(raw_covered / max(len(all_raw_keys), 1), 3)
    canonical_paper_coverage_ratio = round(canonical_covered / max(len(all_canonical_keys), 1), 3)

    if canonical_paper_coverage_ratio < policy.min_canonical_coverage:
        blocking.append(_issue(
            "canonical_coverage_below_threshold",
            (
                f"Canonical coverage {canonical_paper_coverage_ratio:.3f} below "
                f"{policy.min_canonical_coverage:.3f} for scope {policy.coverage_scope}"
            ),
        ))
    if duplicate_assignment_count > policy.max_duplicate_assignments:
        blocking.append(_issue(
            "duplicate_canonical_assignment",
            (
                f"Duplicate canonical assignments {duplicate_assignment_count} exceed "
                f"max {policy.max_duplicate_assignments}"
            ),
        ))
    if len(effective_sections) < policy.min_effective_sections:
        blocking.append(_issue(
            "insufficient_effective_sections",
            f"Effective sections {len(effective_sections)} below minimum {policy.min_effective_sections}",
        ))
    if policy.block_placeholder_sections and placeholder_sections:
        blocking.append(_issue(
            "placeholder_sections_present",
            f"Placeholder sections present: {len(placeholder_sections)}",
        ))
    if policy.block_empty_research_streams and not literature_map.research_streams:
        blocking.append(_issue("empty_research_streams", "Literature map has no research streams"))
    if synthesis_flow.placeholder_flow or not valid_flow_step_ids:
        blocking.append(_issue("diagnostic_only_flow", "Synthesis flow is diagnostic-only or placeholder"))
    if final_outline.review_status == "blocked":
        blocking.append(_issue("final_outline_blocked", "Final outline review_status is blocked"))
    for critique_id in final_outline.blocking_critique_ids:
        blocking.append(_issue(
            "blocking_critique_unresolved",
            f"Blocking critique remains unresolved: {critique_id}",
        ))

    for excl in final_outline.excluded_papers:
        if not excl.get("reason"):
            blocking.append(_issue(
                "unjustified_exclusion",
                f"Excluded paper '{excl.get('paper_key', '?')}' has no reason provided",
                paper_key=excl.get("paper_key", ""),
            ))

    total_flow_steps = len(valid_flow_step_ids)
    covered_steps = len(covered_flow_steps & valid_flow_step_ids)
    coverage_metrics = {
        "total_paper_nodes": len(all_raw_keys),
        "papers_in_outline": raw_covered,
        "paper_coverage_ratio": raw_node_coverage_ratio,
        "total_canonical_papers": len(all_canonical_keys),
        "canonical_papers_in_outline": canonical_covered,
        "canonical_paper_coverage_ratio": canonical_paper_coverage_ratio,
        "raw_node_coverage_ratio": raw_node_coverage_ratio,
        "duplicate_assignment_count": duplicate_assignment_count,
        "effective_section_count": len(effective_sections),
        "placeholder_section_count": len(placeholder_sections),
        "total_flow_steps": total_flow_steps,
        "covered_flow_steps": covered_steps,
        "flow_step_coverage_ratio": round(covered_steps / max(total_flow_steps, 1), 3),
        "blocking_issue_count": len(blocking),
        "warning_count": len(warnings),
    }

    final_outline_hash = compute_content_hash(final_outline.to_dict())
    lit_map_hash = compute_content_hash(literature_map.to_dict())
    synth_flow_hash = compute_content_hash(synthesis_flow.to_dict())

    return CoverageAudit(
        passed=len(blocking) == 0,
        blocking_issues=blocking,
        warnings=warnings,
        coverage_metrics=coverage_metrics,
        quality_gate_policy_snapshot=policy.to_dict(),
        raw_node_coverage_ratio=raw_node_coverage_ratio,
        canonical_paper_coverage_ratio=canonical_paper_coverage_ratio,
        duplicate_assignment_count=duplicate_assignment_count,
        effective_section_count=len(effective_sections),
        placeholder_section_count=len(placeholder_sections),
        source_final_outline_id=f"final_outline:{final_outline_hash[:12]}",
        source_final_outline_hash=final_outline_hash,
        source_literature_map_hash=lit_map_hash,
        source_synthesis_flow_hash=synth_flow_hash,
    )
