"""Synthesis flow builder for Outline Intelligence v2.

Derives synthesis_flow.json from literature_map.json.
Every non-trivial claim has support refs or an explicit diagnostic marker.
"""

from __future__ import annotations

from typing import Any, Dict, List

from outline.quality_rules import (
    is_method_only_stream_label,
    is_placeholder_title,
    stream_promotion_tier,
    synthesis_title_for_stream,
)
from outline.v2_models import FlowStep, LiteratureMap, SynthesisFlow, compute_content_hash


def _distinct_refs(refs: List[str]) -> List[str]:
    return list(dict.fromkeys(ref for ref in refs if ref))


def _next_step_id(steps: List[FlowStep]) -> str:
    return f"flow_step_{len(steps) + 1:03d}"


def _build_conservative_flow_steps(literature_map: LiteratureMap) -> List[FlowStep]:
    """Build flow steps conservatively from the literature map."""
    steps: List[FlowStep] = []
    nodes = literature_map.paper_nodes

    if not nodes:
        steps.append(FlowStep(
            flow_step_id="flow_step_001",
            claim="No papers available for synthesis",
            role_in_review="diagnostic",
            support_refs=[],
            overclaim_risk="low",
            diagnostics=["empty_literature_map"],
            placeholder_flow=True,
        ))
        return steps

    promoted_streams = []
    thin_streams = []
    for stream in literature_map.research_streams:
        paper_keys = _distinct_refs(list(stream.get("paper_keys") or []))
        stream_name = str(stream.get("stream_name") or "").strip()
        source_fields = list(stream.get("source_fields") or [])
        tier = int(stream.get("promotion_tier") or stream_promotion_tier(stream_name, source_fields, len(paper_keys)))
        if tier <= 0:
            thin_streams.append({
                **stream,
                "thin_stream": True,
                "diagnostic_reason": str(stream.get("diagnostic_reason") or "stream_not_promoted_to_main_flow"),
            })
        elif source_fields == ["methods"] and (
            is_method_only_stream_label(stream_name)
            or len(stream_name) > 60
            or (any(ord(ch) > 127 for ch in stream_name) and len(stream_name) > 30)
        ):
            thin_streams.append({
                **stream,
                "thin_stream": True,
                "diagnostic_reason": "method_only_stream_not_promoted",
            })
        elif len(paper_keys) < 2 or stream.get("thin_stream"):
            thin_streams.append(stream)
        else:
            promoted_streams.append(stream)

    promoted_streams = sorted(
        promoted_streams,
        key=lambda stream: (
            int(stream.get("promotion_tier") or 0),
            len(set(stream.get("paper_keys") or [])),
            len(stream.get("source_fields") or []),
            str(stream.get("stream_name") or ""),
        ),
        reverse=True,
    )[:12]

    problem_refs = _distinct_refs([
        node.paper_key
        for node in nodes
        if node.themes or node.theories or node.findings or node.abstract_snippet
    ])
    if promoted_streams and len(problem_refs) >= 2:
        steps.append(FlowStep(
            flow_step_id=_next_step_id(steps),
            claim="Problem space and corpus boundaries",
            role_in_review="establish_problem_space",
            support_refs=problem_refs[:8],
            overclaim_risk="low" if len(problem_refs) >= 3 else "medium",
            diagnostics=["corpus_scope_from_stage1_metadata"],
            placeholder_flow=False,
        ))

    for stream in promoted_streams:
        stream_name = str(stream.get("stream_name") or "").strip()
        claim = synthesis_title_for_stream(stream_name)
        if not claim:
            steps.append(FlowStep(
                flow_step_id=_next_step_id(steps),
                claim=stream_name or "unnamed_stream",
                role_in_review="diagnostic",
                support_refs=_distinct_refs(list(stream.get("paper_keys") or []))[:5],
                overclaim_risk="high",
                diagnostics=["placeholder_stream_label"],
                placeholder_flow=True,
            ))
            continue

        paper_keys = _distinct_refs(list(stream.get("paper_keys") or []))
        source_fields = list(stream.get("source_fields") or [])
        diagnostics: List[str] = []
        if len(paper_keys) < 2:
            diagnostics.append(f"thin_stream: '{stream_name}' has only {len(paper_keys)} papers")
        steps.append(FlowStep(
            flow_step_id=_next_step_id(steps),
            claim=claim,
            role_in_review="synthesize_stream",
            support_refs=paper_keys[:8],
            overclaim_risk="low" if len(paper_keys) >= 3 else "medium",
            diagnostics=diagnostics + [f"source_fields={','.join(source_fields)}"],
            placeholder_flow=False,
        ))

    if len(promoted_streams) >= 2:
        co_refs: List[str] = []
        for stream in promoted_streams[:4]:
            for ref in stream.get("paper_keys") or []:
                if ref not in co_refs:
                    co_refs.append(str(ref))
        if len(co_refs) >= 2:
            steps.append(FlowStep(
                flow_step_id=_next_step_id(steps),
                claim="Connections across mechanisms and contexts",
                role_in_review="connect_mechanism",
                support_refs=co_refs[:8],
                overclaim_risk="medium",
                diagnostics=["derived_from_multi_stream_co_occurrence"],
                placeholder_flow=False,
            ))

    # Keep thin streams traceable without promoting them to main sections.
    for stream in thin_streams[:8]:
        stream_name = str(stream.get("stream_name") or "unnamed_stream")
        paper_keys = _distinct_refs(list(stream.get("paper_keys") or []))
        display_name = stream_name
        if (
            len(display_name) > 60
            or (any(ord(ch) > 127 for ch in display_name) and len(display_name) > 30)
            or str(stream.get("diagnostic_reason") or "").startswith("method_only")
        ):
            display_name = "diagnostic method signal"
        steps.append(FlowStep(
            flow_step_id=_next_step_id(steps),
            claim=f"Thin stream evidence: {display_name}",
            role_in_review="supporting_context",
            support_refs=paper_keys[:5],
            overclaim_risk="high",
            diagnostics=[
                f"thin_stream: '{stream_name}' has only {len(paper_keys)} papers",
                *([str(stream.get("diagnostic_reason"))] if stream.get("diagnostic_reason") else []),
            ],
            placeholder_flow=is_placeholder_title(stream_name),
        ))

    if thin_streams and not promoted_streams:
        for idx, step in enumerate(steps):
            steps[idx] = FlowStep(
                flow_step_id=step.flow_step_id,
                claim=step.claim,
                role_in_review=step.role_in_review,
                support_refs=step.support_refs,
                overclaim_risk=step.overclaim_risk,
                diagnostics=list(step.diagnostics),
                placeholder_flow=True,
            )

    # Gap and method steps are only promoted when they have distinct multi-paper support.
    gap_refs = _distinct_refs([node.paper_key for node in nodes if node.gaps or node.limitations])
    if len(gap_refs) >= 2:
        steps.append(FlowStep(
            flow_step_id=_next_step_id(steps),
            claim="Cross-paper limitations and future research agenda",
            role_in_review="identify_gaps",
            support_refs=gap_refs[:8],
            overclaim_risk="medium",
            diagnostics=["gaps_derived_from_stage1_fields"],
            placeholder_flow=False,
        ))

    method_refs = _distinct_refs([node.paper_key for node in nodes if node.methods])
    method_terms = _distinct_refs([
        method
        for node in nodes
        for method in node.methods
        if method and not is_method_only_stream_label(method)
    ])
    if promoted_streams and len(method_refs) >= 3 and len(method_terms) >= 2:
        steps.append(FlowStep(
            flow_step_id=_next_step_id(steps),
            claim="Methodological synthesis across substantive evidence streams",
            role_in_review="methodological_synthesis",
            support_refs=method_refs[:8],
            overclaim_risk="low",
            diagnostics=["method_step_requires_multi_paper_non_generic_methods"],
            placeholder_flow=False,
        ))

    if not steps:
        all_refs = [node.paper_key for node in nodes[:5]]
        steps.append(FlowStep(
            flow_step_id="flow_step_001",
            claim="Corpus has no multi-paper research streams",
            role_in_review="diagnostic",
            support_refs=all_refs,
            overclaim_risk="high",
            diagnostics=["diagnostic_only_flow", "no_promotable_streams"],
            placeholder_flow=True,
        ))

    substantive_steps = [
        step for step in steps
        if not step.placeholder_flow
        and step.role_in_review not in {"diagnostic", "supporting_context"}
        and len(set(step.support_refs)) >= 2
    ]
    if not substantive_steps:
        if "no_substantive_streams" not in [diag.split(":", 1)[0] for step in steps for diag in step.diagnostics]:
            steps = [
                FlowStep(
                    flow_step_id=step.flow_step_id,
                    claim=step.claim,
                    role_in_review=step.role_in_review,
                    support_refs=step.support_refs,
                    overclaim_risk=step.overclaim_risk,
                    diagnostics=list(step.diagnostics) + ["no_substantive_streams: diagnostic flow only"],
                    placeholder_flow=True,
                )
                for step in steps
            ]

    return steps


def build_synthesis_flow(
    literature_map: LiteratureMap,
    job_id: str,
    flow_strategy: str = "conservative",
) -> SynthesisFlow:
    """Build a synthesis flow from the literature map."""
    flow_steps = _build_conservative_flow_steps(literature_map)
    lit_map_hash = compute_content_hash(literature_map.to_dict())

    transitions: List[Dict[str, str]] = []
    for i in range(len(flow_steps) - 1):
        transitions.append({
            "from": flow_steps[i].flow_step_id,
            "to": flow_steps[i + 1].flow_step_id,
            "type": "sequence",
        })

    gap_candidates = [node for node in literature_map.paper_nodes if node.gaps or node.limitations]
    central_gap = None
    if len(gap_candidates) >= 2:
        central_gap = {
            "description": "Aggregated limitations and research gaps across multiple papers",
            "support_refs": [node.paper_key for node in gap_candidates[:8]],
            "confidence": "medium" if len(gap_candidates) >= 3 else "low",
            "diagnostic": "stage1_gap_fields",
        }

    placeholder_flow = bool(flow_steps) and all(
        step.placeholder_flow or step.role_in_review == "diagnostic" for step in flow_steps
    )
    diagnostics: List[str] = []
    if placeholder_flow:
        diagnostics.append("diagnostic_only_flow")
    if not literature_map.research_streams:
        diagnostics.append("empty_research_streams")

    return SynthesisFlow(
        created_from_job_id=job_id,
        source_literature_map_id=f"literature_map:{lit_map_hash[:12]}",
        flow_strategy=flow_strategy,
        flow_steps=flow_steps,
        transitions=transitions,
        central_gap=central_gap,
        diagnostics=diagnostics,
        placeholder_flow=placeholder_flow,
    )
