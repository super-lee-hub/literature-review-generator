"""Role-specific critique for Outline Intelligence v2.

Production v2 uses Writer_API for structure critique and Primary_Reader_API
for coverage critique. Normalizes raw provider output into project-owned JSON.
Test doubles only in test/dev fixture mode.
"""

from __future__ import annotations

import uuid
import json
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence

from outline.v2_models import (
    CritiqueItem,
    CritiqueRun,
    OutlineCandidate,
    OutlineCandidates,
    OutlineCritiquesV2,
    V2_CRITIQUE_CATEGORIES,
    compute_content_hash,
)


ModelCaller = Callable[[str, str, Dict[str, Any]], Any]


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _normalize_category(raw: str) -> str:
    """Normalize a raw critique category to a known v2 category."""
    raw_lower = raw.strip().lower().replace(" ", "_").replace("-", "_")
    if raw_lower in V2_CRITIQUE_CATEGORIES:
        return raw_lower
    # Fallback mapping
    mapping = {
        "missing_theme": "missing_theme",
        "weak_support": "weak_support_from_summaries",
        "redundant": "redundant_section",
        "ordering": "ordering_issue",
        "overclaim": "overclaim",
        "scope": "scope_mismatch",
        "missing_coverage": "missing_paper_coverage",
        "orphan": "orphan_paper",
        "flow_transition": "weak_flow_transition",
        "unsupported_gap": "unsupported_gap_claim",
        "overload": "section_overload",
        "poor_synthesis": "poor_synthesis",
        "misplacement": "paper_misplacement",
        "unjustified_exclusion": "unjustified_exclusion",
    }
    for key, val in mapping.items():
        if key in raw_lower:
            return val
    return ""


def _sanitize_unknown_category(raw: str) -> str:
    normalized = raw.strip().lower().replace(" ", "_").replace("-", "_")
    return "".join(ch for ch in normalized if ch.isalnum() or ch == "_")[:80]


def normalize_critique_output(raw_output: Any, critic_model: str, critic_role: str) -> CritiqueRun:
    """Normalize raw provider output into a project-owned CritiqueRun.

    Malformed/partial output is downgraded to diagnostics.
    Raw provider blobs are never stored as canonical artifact content.
    """
    critiques: List[CritiqueItem] = []
    diagnostics: List[str] = []

    if isinstance(raw_output, str):
        try:
            import json as _json
            raw_output = _json.loads(raw_output)
        except Exception:
            diagnostics.append(f"Failed to parse critic output as JSON from {critic_model}")
            raw_output = {}

    if not isinstance(raw_output, dict):
        diagnostics.append(f"Unexpected critic output type: {type(raw_output).__name__}")
        return CritiqueRun(
            run_id=str(uuid.uuid4()),
            critic_role=critic_role,
            critic_model=critic_model,
            critiques=[],
            diagnostics=diagnostics,
        )

    items = raw_output.get("critiques", raw_output.get("items", []))
    if not isinstance(items, list):
        diagnostics.append("critiques field is not a list")
        items = []

    for i, item in enumerate(items):
        if not isinstance(item, dict):
            diagnostics.append(f"critique item {i} is not a dict, skipping")
            continue

        raw_category = str(item.get("category", ""))
        category = _normalize_category(raw_category)
        if not category:
            sanitized = _sanitize_unknown_category(raw_category)
            diagnostics.append(
                f"Unknown critique category preserved as diagnostic: {raw_category!r}"
            )
            category = sanitized or "unknown_category"

        critique = CritiqueItem(
            critique_id=item.get("critique_id", f"crit_{uuid.uuid4().hex[:8]}"),
            category=category,
            severity=item.get("severity", "medium"),
            description=str(item.get("description", item.get("detail", ""))),
            target_candidate_id=str(item.get("target_candidate_id", item.get("candidate_id", ""))),
            target_section_id=str(item.get("target_section_id", item.get("section_id", ""))),
            evidence_refs=item.get("evidence_refs", item.get("refs", [])),
            suggested_fix=str(item.get("suggested_fix", item.get("fix", ""))),
            created_by=critic_model,
            created_at=_utc_now_iso(),
        )
        critiques.append(critique)

    if not critiques:
        diagnostics.append(f"No valid critiques extracted from {critic_model} output")

    return CritiqueRun(
        run_id=str(uuid.uuid4()),
        critic_role=critic_role,
        critic_model=critic_model,
        critiques=critiques,
        diagnostics=diagnostics,
    )


def run_structure_critique_deterministic(
    candidates: OutlineCandidates,
    critic_model: str = "test_double",
) -> CritiqueRun:
    """Run structure critique (Writer_API role) deterministically.

    Checks for common structural issues in candidate outlines.
    This is a test/dev fixture. Production uses real Writer_API call.
    """
    critiques: List[CritiqueItem] = []
    diagnostics: List[str] = []
    now = _utc_now_iso()

    for candidate in candidates.candidates:
        # Check for sections without purpose
        for section in candidate.sections:
            if not section.purpose or len(section.purpose) < 5:
                critiques.append(CritiqueItem(
                    critique_id=str(uuid.uuid4()),
                    category="scope_mismatch",
                    severity="medium",
                    description=f"Section '{section.title}' has missing or unclear purpose",
                    target_candidate_id=candidate.candidate_id,
                    target_section_id=section.section_id,
                    evidence_refs=[],
                    suggested_fix="Add a clear purpose statement",
                    created_by=critic_model,
                    created_at=now,
                ))

            if not section.assigned_papers:
                critiques.append(CritiqueItem(
                    critique_id=str(uuid.uuid4()),
                    category="missing_paper_coverage",
                    severity="high",
                    description=f"Section '{section.title}' has no assigned papers",
                    target_candidate_id=candidate.candidate_id,
                    target_section_id=section.section_id,
                    evidence_refs=[],
                    suggested_fix="Assign supporting papers to this section",
                    created_by=critic_model,
                    created_at=now,
                ))

        # Every candidate gets at least one structural critique
        critiques.append(CritiqueItem(
            critique_id=str(uuid.uuid4()),
            category="ordering_issue",
            severity="low",
            description=f"Candidate '{candidate.candidate_id}' uses strategy '{candidate.strategy_label}'; verify section ordering matches review narrative",
            target_candidate_id=candidate.candidate_id,
            target_section_id="",
            evidence_refs=[],
            suggested_fix="Review section ordering for narrative coherence",
            created_by=critic_model,
            created_at=now,
        ))

        # Check for redundant sections
        titles = [s.title.lower() for s in candidate.sections]
        for i, title in enumerate(titles):
            if titles.count(title) > 1:
                critiques.append(CritiqueItem(
                    critique_id=str(uuid.uuid4()),
                    category="redundant_section",
                    severity="low",
                    description=f"Section '{candidate.sections[i].title}' appears redundant",
                    target_candidate_id=candidate.candidate_id,
                    target_section_id=candidate.sections[i].section_id,
                    evidence_refs=[],
                    suggested_fix="Consider merging with similar section",
                    created_by=critic_model,
                    created_at=now,
                ))

    if not critiques:
        diagnostics.append("No structural issues detected — this is unexpected for deterministic mode")

    return CritiqueRun(
        run_id=str(uuid.uuid4()),
        critic_role="structure",
        critic_model=critic_model,
        critiques=critiques,
        diagnostics=diagnostics,
    )


def run_coverage_critique_deterministic(
    candidates: OutlineCandidates,
    critic_model: str = "test_double",
) -> CritiqueRun:
    """Run coverage critique (Primary_Reader_API role) deterministically.

    Checks for paper coverage and gap support.
    This is a test/dev fixture. Production uses real Primary_Reader_API call.
    """
    critiques: List[CritiqueItem] = []
    now = _utc_now_iso()

    for candidate in candidates.candidates:
        # Collect all assigned papers
        assigned_paper_keys: set[str] = set()
        for section in candidate.sections:
            for ap in section.assigned_papers:
                assigned_paper_keys.add(ap.get("paper_key", ""))

        # Check for flow steps without corresponding sections
        all_flow_steps: set[str] = set()
        for section in candidate.sections:
            all_flow_steps.update(section.source_flow_steps)

        # Check for orphan sections (no flow step mapping)
        for section in candidate.sections:
            if not section.source_flow_steps:
                critiques.append(CritiqueItem(
                    critique_id=str(uuid.uuid4()),
                    category="orphan_paper",
                    severity="medium",
                    description=f"Section '{section.title}' has no flow step mapping",
                    target_candidate_id=candidate.candidate_id,
                    target_section_id=section.section_id,
                    evidence_refs=[],
                    suggested_fix="Map section to a synthesis flow step",
                    created_by=critic_model,
                    created_at=now,
                ))

    return CritiqueRun(
        run_id=str(uuid.uuid4()),
        critic_role="coverage",
        critic_model=critic_model,
        critiques=critiques,
        diagnostics=[],
    )


def _critique_prompt(candidates: OutlineCandidates, critic_role: str) -> str:
    return (
        f"Run an Outline Intelligence v2 {critic_role} critique. Return strict JSON "
        "with a top-level critiques list. Each critique must include category, "
        "severity, description, target_candidate_id/target_section_id when relevant, "
        "evidence_refs, and suggested_fix.\n\n"
        + json.dumps(
            {
                "critic_role": critic_role,
                "allowed_categories": V2_CRITIQUE_CATEGORIES,
                "outline_candidates": candidates.to_dict(),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def run_critique_production(
    candidates: OutlineCandidates,
    critic_model: str,
    critic_role: str,
    model_caller: ModelCaller | None,
) -> CritiqueRun:
    """Run a role-specific critique through the configured production model route."""
    if model_caller is None:
        raise RuntimeError(
            f"Production v2 {critic_role} critique requires a model_caller for {critic_model}"
        )
    raw_output = model_caller(
        critic_model,
        _critique_prompt(candidates, critic_role),
        {"stage": f"{critic_role}_critique"},
    )
    return normalize_critique_output(raw_output, critic_model, critic_role)


def build_critiques_v2(
    structure_run: CritiqueRun,
    coverage_run: CritiqueRun,
    candidate_ids: List[str],
) -> OutlineCritiquesV2:
    """Build the combined OutlineCritiquesV2 artifact from critique runs."""
    all_critiques = list(structure_run.critiques) + list(coverage_run.critiques)
    return OutlineCritiquesV2(
        source_candidate_ids=candidate_ids,
        critique_runs=[structure_run, coverage_run],
        critiques=all_critiques,
    )
