"""Deterministic coverage audit for Outline Intelligence v2.

Audits final_outline.json against literature_map.json and synthesis_flow.json.
Blocks adoption on defined blocking issues. Non-blocking for peripheral papers.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Set

from outline.v2_models import (
    BLOCKING_ISSUE_TYPES,
    CoverageAudit,
    CoverageIssue,
    FinalOutline,
    LiteratureMap,
    SynthesisFlow,
    compute_content_hash,
)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


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


def _collect_covered_flow_steps(sections) -> Set[str]:
    """Recursively collect all covered flow step IDs from sections."""
    steps: Set[str] = set()
    for sec in sections:
        source_steps = getattr(sec, "source_flow_steps", [])
        steps.update(source_steps)
        children = getattr(sec, "children", [])
        steps.update(_collect_covered_flow_steps(children))
    return steps


def run_coverage_audit(
    final_outline: FinalOutline,
    literature_map: LiteratureMap,
    synthesis_flow: SynthesisFlow,
) -> CoverageAudit:
    """Run deterministic coverage audit.

    Returns audit with blocking_issues and warnings.
    """
    blocking: List[CoverageIssue] = []
    warnings: List[CoverageIssue] = []

    # Collect paper coverage
    assigned_papers = _collect_assigned_papers(final_outline.sections)
    all_paper_keys = {n.paper_key for n in literature_map.paper_nodes}
    covered_flow_steps = _collect_covered_flow_steps(final_outline.sections)
    all_flow_step_ids = {s.flow_step_id for s in synthesis_flow.flow_steps}

    # 1. Check for missing paper nodes
    for pk in assigned_papers:
        if pk not in all_paper_keys:
            blocking.append(CoverageIssue(
                issue_type="missing_paper_node",
                description=f"Paper '{pk}' assigned in outline but not in literature map",
                paper_key=pk,
            ))

    # 2. Check for unclassified papers
    for node in literature_map.paper_nodes:
        if node.classification == "unknown":
            blocking.append(CoverageIssue(
                issue_type="unclassified_paper",
                description=f"Paper '{node.paper_key}' ({node.title}) is unclassified",
                paper_key=node.paper_key,
            ))

    # 3. Check for orphan core/must_use papers
    for node in literature_map.paper_nodes:
        if node.must_use and node.paper_key not in assigned_papers:
            blocking.append(CoverageIssue(
                issue_type="missing_core_paper" if node.classification == "core" else "orphan_paper",
                description=f"Core/must_use paper '{node.paper_key}' ({node.title}) not in final outline",
                paper_key=node.paper_key,
            ))

    # 4. Check for orphan non-must-use papers (warning only)
    for node in literature_map.paper_nodes:
        if not node.must_use and node.paper_key not in assigned_papers:
            warnings.append(CoverageIssue(
                issue_type="orphan_paper",
                description=f"Non-core paper '{node.paper_key}' ({node.title}) not in final outline (metrics only)",
                paper_key=node.paper_key,
            ))

    # 5. Check for unsupported gap claims
    central_gap = synthesis_flow.central_gap
    if central_gap and not central_gap.get("support_refs"):
        blocking.append(CoverageIssue(
            issue_type="unsupported_gap_claim",
            description="Central gap claim has no supporting paper refs",
        ))

    # 6. Check for uncovered flow steps
    for step_id in all_flow_step_ids:
        if step_id not in covered_flow_steps:
            blocking.append(CoverageIssue(
                issue_type="flow_step_uncovered",
                description=f"Flow step '{step_id}' is not covered by any final outline section",
                flow_step_id=step_id,
            ))

    # 7. Check for sections without supporting papers
    for sec in final_outline.sections:
        if not sec.assigned_papers:
            blocking.append(CoverageIssue(
                issue_type="section_without_supporting_papers",
                description=f"Section '{sec.title}' has no supporting papers assigned",
                section_id=sec.section_id,
            ))

    # 8. Check for unjustified exclusions
    for excl in final_outline.excluded_papers:
        if not excl.get("reason"):
            blocking.append(CoverageIssue(
                issue_type="unjustified_exclusion",
                description=f"Excluded paper '{excl.get('paper_key', '?')}' has no reason provided",
                paper_key=excl.get("paper_key", ""),
            ))

    # Compute metrics
    total_papers = len(all_paper_keys)
    covered_papers = len(assigned_papers & all_paper_keys)
    total_flow_steps = len(all_flow_step_ids)
    covered_steps = len(covered_flow_steps & all_flow_step_ids)

    coverage_metrics = {
        "total_paper_nodes": total_papers,
        "papers_in_outline": covered_papers,
        "paper_coverage_ratio": round(covered_papers / max(total_papers, 1), 3),
        "total_flow_steps": total_flow_steps,
        "covered_flow_steps": covered_steps,
        "flow_step_coverage_ratio": round(covered_steps / max(total_flow_steps, 1), 3),
        "blocking_issue_count": len(blocking),
        "warning_count": len(warnings),
    }

    # Compute hashes
    final_outline_hash = compute_content_hash(final_outline.to_dict())
    lit_map_hash = compute_content_hash(literature_map.to_dict())
    synth_flow_hash = compute_content_hash(synthesis_flow.to_dict())

    return CoverageAudit(
        passed=len(blocking) == 0,
        blocking_issues=blocking,
        warnings=warnings,
        coverage_metrics=coverage_metrics,
        source_final_outline_id=f"final_outline:{final_outline_hash[:12]}",
        source_final_outline_hash=final_outline_hash,
        source_literature_map_hash=lit_map_hash,
        source_synthesis_flow_hash=synth_flow_hash,
    )
