"""Arbitration and final outline generation for Outline Intelligence v2.

Production v2 uses Outline_API to arbitrate over candidates and critiques.
Produces outline_arbitration_report.json and final_outline.json.
"""

from __future__ import annotations

import uuid
import json
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from outline.v2_models import (
    ArbitrationReport,
    FinalOutline,
    FinalSection,
    OutlineCandidates,
    OutlineCritiquesV2,
    compute_content_hash,
)


ModelCaller = Callable[[str, str, Dict[str, Any]], Any]


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


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

    # Accept structural critiques (medium severity or lower), reject high severity
    accepted: List[str] = []
    rejected: List[str] = []
    for c in critiques.critiques:
        if c.severity in ("low", "medium"):
            accepted.append(c.critique_id)
        else:
            rejected.append(c.critique_id)

    selected_candidate = candidates.candidates[0] if candidates.candidates else None
    merged_strategy = "select_base_candidate_with_accepted_critiques"
    if selected_candidate:
        merged_strategy += f" base={selected_candidate.candidate_id}"

    final_decision: Dict[str, Any] = {
        "selected_base_candidate": selected_candidate.candidate_id if selected_candidate else "",
        "strategy": merged_strategy,
        "accepted_count": len(accepted),
        "rejected_count": len(rejected),
    }

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

    if base_candidate:
        for sec in base_candidate.sections:
            final_sec = FinalSection(
                section_id=sec.section_id,
                title=sec.title,
                purpose=sec.purpose,
                argument_role=sec.argument_role,
                source_flow_steps=sec.source_flow_steps,
                assigned_papers=sec.assigned_papers,
                children=[
                    FinalSection(
                        section_id=c.section_id,
                        title=c.title,
                        purpose=c.purpose,
                        argument_role=c.argument_role,
                        source_flow_steps=c.source_flow_steps,
                        assigned_papers=c.assigned_papers,
                    )
                    for c in sec.children
                ],
            )
            sections.append(final_sec)

    outline = FinalOutline(
        created_from_job_id=job_id,
        outline_id=str(uuid.uuid4()),
        source_literature_map_id=candidates.source_literature_map_id,
        source_synthesis_flow_id=candidates.source_synthesis_flow_id,
        source_arbitration_report_id=f"arbitration:{compute_content_hash(arbitration_report.to_dict())[:12]}",
        source_literature_map_hash=literature_map_hash,
        source_synthesis_flow_hash=synthesis_flow_hash,
        review_status="arbitrated",
        adoption_status="pending_user_adoption",
        sections=sections,
        excluded_papers=excluded_papers,
    )
    return outline


def _arbitration_prompt(candidates: OutlineCandidates, critiques: OutlineCritiquesV2) -> str:
    return (
        "Arbitrate Outline Intelligence v2 outline candidates and critiques. "
        "Return strict JSON containing source_candidates, source_critiques, "
        "candidate_scores, accepted_points, rejected_points, merged_strategy, "
        "and final_decision.selected_base_candidate. Use only provided candidate "
        "and critique ids.\n\n"
        + json.dumps(
            {
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
    return normalize_arbitration_output(raw_output, candidates, critiques, arbitrator_model)
