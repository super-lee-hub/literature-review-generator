"""Multi-candidate outline generation for Outline Intelligence v2.

Generates multiple outline candidates from literature map and synthesis flow.
Production v2 calls configured Outline_API. Test doubles only in test/dev mode.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

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

    for i, step in enumerate(flow_steps, 1):
        assigned_papers: List[Dict[str, str]] = []
        for ref in step.support_refs[:5]:
            node = node_map.get(ref)
            title = node.title if node else ref
            assigned_papers.append({
                "paper_key": ref,
                "title": title,
                "role": step.role_in_review,
                "reason": f"Supports {step.claim[:100]}",
            })

        section = CandidateSection(
            section_id=f"cand_sec_{i:03d}",
            title=step.claim[:120] if step.claim else f"Section {i}",
            purpose=step.role_in_review.replace("_", " "),
            argument_role=step.role_in_review,
            source_flow_steps=[step.flow_step_id],
            assigned_papers=assigned_papers,
            children=[],
        )
        sections.append(section)

    return sections


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
        sections = _build_candidate_sections(steps, nodes, label)

        # Vary sections slightly by candidate
        if i == 1:
            sections = sorted(sections, key=lambda s: s.argument_role)
        elif i == 2:
            gap_sections = [s for s in sections if "gap" in s.argument_role.lower()]
            other_sections = [s for s in sections if "gap" not in s.argument_role.lower()]
            sections = gap_sections + other_sections

        candidates.append(OutlineCandidate(
            candidate_id=f"candidate_{i + 1}",
            strategy_label=label,
            sections=sections,
            summary=description,
        ))

    return OutlineCandidates(
        source_literature_map_id=f"literature_map:{compute_content_hash(literature_map.to_dict())[:12]}",
        source_synthesis_flow_id=f"synthesis_flow:{compute_content_hash(synthesis_flow.to_dict())[:12]}",
        candidate_count=len(candidates),
        candidates=candidates,
        generator_model=generator_model,
    )


def _candidate_prompt(
    literature_map: LiteratureMap,
    synthesis_flow: SynthesisFlow,
    candidate_count: int,
) -> str:
    payload = {
        "candidate_count": candidate_count,
        "literature_map": literature_map.to_dict(),
        "synthesis_flow": synthesis_flow.to_dict(),
        "output_schema": {
            "candidates": [
                {
                    "candidate_id": "candidate_1",
                    "strategy_label": "mechanism_driven",
                    "summary": "Short strategy rationale",
                    "sections": [
                        {
                            "section_id": "sec_1",
                            "title": "Section title",
                            "purpose": "Why this section exists",
                            "argument_role": "establish_problem_space",
                            "source_flow_steps": ["flow_step_001"],
                            "assigned_papers": [
                                {
                                    "paper_key": "paper key from literature_map",
                                    "role": "supporting evidence role",
                                    "reason": "Why this paper belongs here",
                                }
                            ],
                            "children": [],
                        }
                    ],
                }
            ]
        },
    }
    return (
        "Generate multiple distinct Outline Intelligence v2 outline candidates. "
        "Return strict JSON matching output_schema. Every section must cite source "
        "flow steps and assigned controlled-corpus paper keys.\n\n"
        + json.dumps(payload, ensure_ascii=False, indent=2)
    )


def _parse_candidate_sections(raw_sections: Any) -> List[CandidateSection]:
    sections: List[CandidateSection] = []
    if not isinstance(raw_sections, list):
        return sections
    for idx, item in enumerate(raw_sections, 1):
        if not isinstance(item, dict):
            continue
        children = _parse_candidate_sections(item.get("children", []))
        assigned = item.get("assigned_papers", [])
        if not isinstance(assigned, list):
            assigned = []
        source_steps = item.get("source_flow_steps", [])
        if not isinstance(source_steps, list):
            source_steps = []
        sections.append(CandidateSection(
            section_id=str(item.get("section_id") or f"cand_sec_{idx:03d}"),
            title=str(item.get("title") or f"Section {idx}"),
            purpose=str(item.get("purpose") or ""),
            argument_role=str(item.get("argument_role") or ""),
            source_flow_steps=[str(step) for step in source_steps],
            assigned_papers=[dict(p) for p in assigned if isinstance(p, dict)],
            children=children,
        ))
    return sections


def normalize_candidate_output(
    raw_output: Any,
    literature_map: LiteratureMap,
    synthesis_flow: SynthesisFlow,
    candidate_count: int,
    generator_model: str,
) -> OutlineCandidates:
    """Normalize provider JSON into the project-owned OutlineCandidates model."""
    if isinstance(raw_output, str):
        raw_output = json.loads(raw_output)
    if not isinstance(raw_output, dict):
        raise ValueError(f"Unexpected candidate output type: {type(raw_output).__name__}")

    raw_candidates = raw_output.get("candidates", [])
    if not isinstance(raw_candidates, list):
        raise ValueError("Candidate output missing list field: candidates")

    candidates: List[OutlineCandidate] = []
    for idx, item in enumerate(raw_candidates[:candidate_count], 1):
        if not isinstance(item, dict):
            continue
        sections = _parse_candidate_sections(item.get("sections", []))
        if not sections:
            raise ValueError(f"Candidate {idx} has no sections")
        candidates.append(OutlineCandidate(
            candidate_id=str(item.get("candidate_id") or f"candidate_{idx}"),
            strategy_label=str(item.get("strategy_label") or f"candidate_{idx}"),
            sections=sections,
            summary=str(item.get("summary") or ""),
        ))

    if len(candidates) < candidate_count:
        raise ValueError(
            f"Candidate output contained {len(candidates)} valid candidates; "
            f"expected {candidate_count}"
        )

    return OutlineCandidates(
        source_literature_map_id=f"literature_map:{compute_content_hash(literature_map.to_dict())[:12]}",
        source_synthesis_flow_id=f"synthesis_flow:{compute_content_hash(synthesis_flow.to_dict())[:12]}",
        candidate_count=len(candidates),
        candidates=candidates,
        generator_model=generator_model,
    )


def generate_candidates_production(
    literature_map: LiteratureMap,
    synthesis_flow: SynthesisFlow,
    candidate_count: int,
    generator_model: str,
    model_caller: ModelCaller | None,
) -> OutlineCandidates:
    """Generate candidates through the configured production Outline_API route."""
    if model_caller is None:
        raise RuntimeError(
            "Production v2 candidate generation requires a model_caller for Outline_API"
        )
    prompt = _candidate_prompt(literature_map, synthesis_flow, candidate_count)
    raw_output = model_caller(generator_model, prompt, {"stage": "outline_candidates"})
    return normalize_candidate_output(
        raw_output, literature_map, synthesis_flow, candidate_count, generator_model
    )


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
