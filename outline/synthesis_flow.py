"""Synthesis flow builder for Outline Intelligence v2.

Derives synthesis_flow.json from literature_map.json.
Every non-trivial claim has support refs or weak/diagnostic marker.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

from outline.v2_models import (
    FlowStep,
    LiteratureMap,
    PaperNode,
    SynthesisFlow,
    compute_content_hash,
)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _build_conservative_flow_steps(literature_map: LiteratureMap) -> List[FlowStep]:
    """Build flow steps conservatively from the literature map.

    Every claim is traceable to paper refs. No unsupported claims are promoted.
    """
    steps: List[FlowStep] = []
    nodes = literature_map.paper_nodes
    streams = literature_map.research_streams

    if not nodes:
        steps.append(FlowStep(
            flow_step_id="flow_step_001",
            claim="No papers available for synthesis",
            role_in_review="diagnostic",
            support_refs=[],
            overclaim_risk="low",
            diagnostics=["empty_literature_map"],
        ))
        return steps

    # Step 1: Problem framing based on core/must_use papers
    def _next_step_id() -> str:
        return f"flow_step_{len(steps) + 1:03d}"

    core_papers = [n for n in nodes if n.classification == "core" or n.must_use]
    framing_papers = [n.paper_key for n in core_papers[:5]] if core_papers else [nodes[0].paper_key]

    steps.append(FlowStep(
        flow_step_id=_next_step_id(),
        claim="Research problem framing from core literature",
        role_in_review="establish_problem_space",
        support_refs=framing_papers,
        overclaim_risk="low",
    ))

    # Step 2: Each stream becomes a synthesis step
    for stream in streams:
        if not stream.get("paper_keys"):
            continue
        stream_name = stream.get("stream_name", "unnamed_stream")
        paper_keys = stream["paper_keys"][:5]
        overclaim_risk = "low" if len(paper_keys) >= 3 else "medium"
        diagnostics: List[str] = []
        if len(paper_keys) < 2:
            diagnostics.append(f"thin_stream: '{stream_name}' has only {len(paper_keys)} papers")
            overclaim_risk = "high"

        steps.append(FlowStep(
            flow_step_id=_next_step_id(),
            claim=f"Synthesis of research stream: {stream_name}",
            role_in_review="synthesize_stream",
            support_refs=paper_keys,
            overclaim_risk=overclaim_risk,
            diagnostics=diagnostics,
        ))

    # Step 3: Identify candidate gaps (conservative: from paper diagnostics)
    gap_papers = [n for n in nodes if n.diagnostics]
    if gap_papers:
        steps.append(FlowStep(
            flow_step_id=_next_step_id(),
            claim="Identified gaps and limitations from literature diagnostics",
            role_in_review="identify_gaps",
            support_refs=[n.paper_key for n in gap_papers[:5]],
            overclaim_risk="medium",
            diagnostics=["gaps_derived_from_diagnostics_only"],
        ))

    # Step 4: Methodological synthesis
    method_papers = [n for n in nodes if n.methods]
    if method_papers:
        steps.append(FlowStep(
            flow_step_id=_next_step_id(),
            claim="Synthesis of methodological approaches across papers",
            role_in_review="methodological_synthesis",
            support_refs=[n.paper_key for n in method_papers[:5]],
            overclaim_risk="low",
        ))

    return steps


def build_synthesis_flow(
    literature_map: LiteratureMap,
    job_id: str,
    flow_strategy: str = "conservative",
) -> SynthesisFlow:
    """Build a synthesis flow from the literature map.

    Conservative strategy: derive flow steps from available paper metadata
    and streams. No unsupported claims are promoted.
    """
    flow_steps = _build_conservative_flow_steps(literature_map)
    lit_map_hash = compute_content_hash(literature_map.to_dict())

    # Build transitions between consecutive steps
    transitions: List[Dict[str, str]] = []
    for i in range(len(flow_steps) - 1):
        transitions.append({
            "from": flow_steps[i].flow_step_id,
            "to": flow_steps[i + 1].flow_step_id,
            "type": "sequence",
        })

    # Central gap detection (conservative: only if enough evidence)
    central_gap = None
    gap_candidates = [n for n in literature_map.paper_nodes if n.limitations]
    if gap_candidates:
        central_gap = {
            "description": "Aggregated limitations across the literature",
            "support_refs": [n.paper_key for n in gap_candidates[:5]],
            "confidence": "low",
            "diagnostic": "conservative_gap_detection",
        }

    return SynthesisFlow(
        created_from_job_id=job_id,
        source_literature_map_id=f"literature_map:{lit_map_hash[:12]}",
        flow_strategy=flow_strategy,
        flow_steps=flow_steps,
        transitions=transitions,
        central_gap=central_gap,
    )
