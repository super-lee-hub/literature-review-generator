"""V2 artifact models for Outline Intelligence v2 Complete Vertical Slice.

Defines project-owned JSON dataclasses for all v2 artifacts.
Separate from models.py to preserve Week 5 compatibility.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


def compute_content_hash(data: Any) -> str:
    """Compute a stable SHA-256 content hash for artifact identity checks."""
    content = json.dumps(data, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Literature Map
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PaperNode:
    """A stable paper node derived from a source summary or paper artifact."""
    paper_key: str
    source_summary_hash: str
    canonical_paper_key: str = ""
    identity_source: str = ""
    aliases: List[str] = field(default_factory=list)
    source_records: List[Dict[str, Any]] = field(default_factory=list)
    title: str = ""
    authors: List[str] = field(default_factory=list)
    year: Optional[int] = None
    abstract_snippet: str = ""
    themes: List[str] = field(default_factory=list)
    methods: List[str] = field(default_factory=list)
    theories: List[str] = field(default_factory=list)
    variables: List[str] = field(default_factory=list)
    gaps: List[str] = field(default_factory=list)
    findings: List[str] = field(default_factory=list)
    limitations: List[str] = field(default_factory=list)
    classification: str = "unknown"  # core | background_only | peripheral | support
    must_use: bool = False
    diagnostics: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "paper_key": self.paper_key,
            "source_summary_hash": self.source_summary_hash,
            "canonical_paper_key": self.canonical_paper_key or self.paper_key,
            "identity_source": self.identity_source,
            "aliases": self.aliases,
            "source_records": self.source_records,
            "title": self.title,
            "authors": self.authors,
            "year": self.year,
            "abstract_snippet": self.abstract_snippet,
            "themes": self.themes,
            "methods": self.methods,
            "theories": self.theories,
            "variables": self.variables,
            "gaps": self.gaps,
            "findings": self.findings,
            "limitations": self.limitations,
            "classification": self.classification,
            "must_use": self.must_use,
            "diagnostics": self.diagnostics,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> PaperNode:
        paper_key = data["paper_key"]
        return cls(
            paper_key=paper_key,
            source_summary_hash=data.get("source_summary_hash", ""),
            canonical_paper_key=data.get("canonical_paper_key") or paper_key,
            identity_source=data.get("identity_source", ""),
            aliases=data.get("aliases", []),
            source_records=data.get("source_records", []),
            title=data.get("title", ""),
            authors=data.get("authors", []),
            year=data.get("year"),
            abstract_snippet=data.get("abstract_snippet", ""),
            themes=data.get("themes", []),
            methods=data.get("methods", []),
            theories=data.get("theories", []),
            variables=data.get("variables", []),
            gaps=data.get("gaps", []),
            findings=data.get("findings", []),
            limitations=data.get("limitations", []),
            classification=data.get("classification", "unknown"),
            must_use=data.get("must_use", False),
            diagnostics=data.get("diagnostics", []),
        )


@dataclass(frozen=True)
class LiteratureMap:
    artifact_type: str = "literature_map"
    artifact_version: str = "v1"
    created_from_job_id: str = ""
    created_at: str = ""
    source_summary_hashes: List[str] = field(default_factory=list)
    paper_nodes: List[PaperNode] = field(default_factory=list)
    research_streams: List[Dict[str, Any]] = field(default_factory=list)
    theoretical_dimensions: List[Dict[str, Any]] = field(default_factory=list)
    method_clusters: List[Dict[str, Any]] = field(default_factory=list)
    empirical_contexts: List[Dict[str, Any]] = field(default_factory=list)
    key_tensions: List[Dict[str, Any]] = field(default_factory=list)
    candidate_gaps: List[Dict[str, Any]] = field(default_factory=list)
    paper_classification: Dict[str, List[str]] = field(default_factory=dict)
    blocking_diagnostics: List[Dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "artifact_type": self.artifact_type,
            "artifact_version": self.artifact_version,
            "created_from_job_id": self.created_from_job_id,
            "created_at": self.created_at,
            "source_summary_hashes": self.source_summary_hashes,
            "paper_nodes": [n.to_dict() for n in self.paper_nodes],
            "research_streams": self.research_streams,
            "theoretical_dimensions": self.theoretical_dimensions,
            "method_clusters": self.method_clusters,
            "empirical_contexts": self.empirical_contexts,
            "key_tensions": self.key_tensions,
            "candidate_gaps": self.candidate_gaps,
            "paper_classification": self.paper_classification,
            "blocking_diagnostics": self.blocking_diagnostics,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> LiteratureMap:
        return cls(
            artifact_type=data.get("artifact_type", "literature_map"),
            artifact_version=data.get("artifact_version", "v1"),
            created_from_job_id=data.get("created_from_job_id", ""),
            created_at=data.get("created_at", ""),
            source_summary_hashes=data.get("source_summary_hashes", []),
            paper_nodes=[PaperNode.from_dict(n) for n in data.get("paper_nodes", [])],
            research_streams=data.get("research_streams", []),
            theoretical_dimensions=data.get("theoretical_dimensions", []),
            method_clusters=data.get("method_clusters", []),
            empirical_contexts=data.get("empirical_contexts", []),
            key_tensions=data.get("key_tensions", []),
            candidate_gaps=data.get("candidate_gaps", []),
            paper_classification=data.get("paper_classification", {}),
            blocking_diagnostics=data.get("blocking_diagnostics", []),
        )


# ---------------------------------------------------------------------------
# Synthesis Flow
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FlowStep:
    flow_step_id: str
    claim: str
    role_in_review: str
    support_refs: List[str] = field(default_factory=list)
    overclaim_risk: str = "low"  # low | medium | high
    diagnostics: List[str] = field(default_factory=list)
    placeholder_flow: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "flow_step_id": self.flow_step_id,
            "claim": self.claim,
            "role_in_review": self.role_in_review,
            "support_refs": self.support_refs,
            "overclaim_risk": self.overclaim_risk,
            "diagnostics": self.diagnostics,
            "placeholder_flow": self.placeholder_flow,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> FlowStep:
        return cls(
            flow_step_id=data["flow_step_id"],
            claim=data.get("claim", ""),
            role_in_review=data.get("role_in_review", ""),
            support_refs=data.get("support_refs", []),
            overclaim_risk=data.get("overclaim_risk", "low"),
            diagnostics=data.get("diagnostics", []),
            placeholder_flow=data.get("placeholder_flow", False),
        )


@dataclass(frozen=True)
class SynthesisFlow:
    artifact_type: str = "synthesis_flow"
    artifact_version: str = "v1"
    created_from_job_id: str = ""
    source_literature_map_id: str = ""
    flow_strategy: str = ""
    flow_steps: List[FlowStep] = field(default_factory=list)
    transitions: List[Dict[str, str]] = field(default_factory=list)
    central_gap: Optional[Dict[str, Any]] = None
    diagnostics: List[str] = field(default_factory=list)
    placeholder_flow: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "artifact_type": self.artifact_type,
            "artifact_version": self.artifact_version,
            "created_from_job_id": self.created_from_job_id,
            "source_literature_map_id": self.source_literature_map_id,
            "flow_strategy": self.flow_strategy,
            "flow_steps": [s.to_dict() for s in self.flow_steps],
            "transitions": self.transitions,
            "central_gap": self.central_gap,
            "diagnostics": self.diagnostics,
            "placeholder_flow": self.placeholder_flow,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> SynthesisFlow:
        return cls(
            artifact_type=data.get("artifact_type", "synthesis_flow"),
            artifact_version=data.get("artifact_version", "v1"),
            created_from_job_id=data.get("created_from_job_id", ""),
            source_literature_map_id=data.get("source_literature_map_id", ""),
            flow_strategy=data.get("flow_strategy", ""),
            flow_steps=[FlowStep.from_dict(s) for s in data.get("flow_steps", [])],
            transitions=data.get("transitions", []),
            central_gap=data.get("central_gap"),
            diagnostics=data.get("diagnostics", []),
            placeholder_flow=data.get("placeholder_flow", False),
        )


# ---------------------------------------------------------------------------
# Outline Candidates
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CandidateSection:
    section_id: str
    title: str
    purpose: str = ""
    argument_role: str = ""
    source_flow_steps: List[str] = field(default_factory=list)
    assigned_papers: List[Dict[str, str]] = field(default_factory=list)
    children: List[CandidateSection] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "section_id": self.section_id,
            "title": self.title,
            "purpose": self.purpose,
            "argument_role": self.argument_role,
            "source_flow_steps": self.source_flow_steps,
            "assigned_papers": self.assigned_papers,
            "children": [c.to_dict() for c in self.children],
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> CandidateSection:
        return cls(
            section_id=data["section_id"],
            title=data["title"],
            purpose=data.get("purpose", ""),
            argument_role=data.get("argument_role", ""),
            source_flow_steps=data.get("source_flow_steps", []),
            assigned_papers=data.get("assigned_papers", []),
            children=[cls.from_dict(c) for c in data.get("children", [])],
        )


@dataclass(frozen=True)
class OutlineCandidate:
    candidate_id: str
    strategy_label: str = ""
    sections: List[CandidateSection] = field(default_factory=list)
    summary: str = ""
    provenance: str = "provider"  # provider | deterministic_fallback | deterministic_topup | test_double

    def to_dict(self) -> Dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "strategy_label": self.strategy_label,
            "sections": [s.to_dict() for s in self.sections],
            "summary": self.summary,
            "provenance": self.provenance,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> OutlineCandidate:
        return cls(
            candidate_id=data["candidate_id"],
            strategy_label=data.get("strategy_label", ""),
            sections=[CandidateSection.from_dict(s) for s in data.get("sections", [])],
            summary=data.get("summary", ""),
            provenance=data.get("provenance", "provider"),
        )


@dataclass(frozen=True)
class OutlineCandidates:
    artifact_type: str = "outline_candidates"
    artifact_version: str = "v1"
    source_literature_map_id: str = ""
    source_synthesis_flow_id: str = ""
    candidate_count: int = 0
    candidates: List[OutlineCandidate] = field(default_factory=list)
    generator_model: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "artifact_type": self.artifact_type,
            "artifact_version": self.artifact_version,
            "source_literature_map_id": self.source_literature_map_id,
            "source_synthesis_flow_id": self.source_synthesis_flow_id,
            "candidate_count": self.candidate_count,
            "candidates": [c.to_dict() for c in self.candidates],
            "generator_model": self.generator_model,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> OutlineCandidates:
        return cls(
            artifact_type=data.get("artifact_type", "outline_candidates"),
            artifact_version=data.get("artifact_version", "v1"),
            source_literature_map_id=data.get("source_literature_map_id", ""),
            source_synthesis_flow_id=data.get("source_synthesis_flow_id", ""),
            candidate_count=data.get("candidate_count", 0),
            candidates=[OutlineCandidate.from_dict(c) for c in data.get("candidates", [])],
            generator_model=data.get("generator_model", ""),
        )


# ---------------------------------------------------------------------------
# Critique V2
# ---------------------------------------------------------------------------

# Extended critique taxonomy for v2
V2_CRITIQUE_CATEGORIES = [
    "missing_theme",
    "weak_support_from_summaries",
    "redundant_section",
    "ordering_issue",
    "overclaim",
    "scope_mismatch",
    "missing_paper_coverage",
    "orphan_paper",
    "weak_flow_transition",
    "unsupported_gap_claim",
    "section_overload",
    "poor_synthesis",
    "paper_misplacement",
    "unjustified_exclusion",
]


@dataclass(frozen=True)
class CritiqueItem:
    critique_id: str
    category: str
    severity: str = "medium"  # high | medium | low
    description: str = ""
    target_candidate_id: str = ""
    target_section_id: str = ""
    evidence_refs: List[str] = field(default_factory=list)
    suggested_fix: str = ""
    created_by: str = ""
    created_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "critique_id": self.critique_id,
            "category": self.category,
            "severity": self.severity,
            "description": self.description,
            "target_candidate_id": self.target_candidate_id,
            "target_section_id": self.target_section_id,
            "evidence_refs": self.evidence_refs,
            "suggested_fix": self.suggested_fix,
            "created_by": self.created_by,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> CritiqueItem:
        return cls(
            critique_id=data.get("critique_id", ""),
            category=data.get("category", ""),
            severity=data.get("severity", "medium"),
            description=data.get("description", ""),
            target_candidate_id=data.get("target_candidate_id", ""),
            target_section_id=data.get("target_section_id", ""),
            evidence_refs=data.get("evidence_refs", []),
            suggested_fix=data.get("suggested_fix", ""),
            created_by=data.get("created_by", ""),
            created_at=data.get("created_at", ""),
        )


@dataclass(frozen=True)
class CritiqueRun:
    run_id: str
    critic_role: str  # structure | coverage
    critic_model: str
    critiques: List[CritiqueItem] = field(default_factory=list)
    diagnostics: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "critic_role": self.critic_role,
            "critic_model": self.critic_model,
            "critiques": [c.to_dict() for c in self.critiques],
            "diagnostics": self.diagnostics,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> CritiqueRun:
        return cls(
            run_id=data.get("run_id", ""),
            critic_role=data.get("critic_role", ""),
            critic_model=data.get("critic_model", ""),
            critiques=[CritiqueItem.from_dict(c) for c in data.get("critiques", [])],
            diagnostics=data.get("diagnostics", []),
        )


@dataclass(frozen=True)
class OutlineCritiquesV2:
    artifact_type: str = "outline_critiques"
    artifact_version: str = "v1"
    source_candidate_ids: List[str] = field(default_factory=list)
    critique_runs: List[CritiqueRun] = field(default_factory=list)
    critiques: List[CritiqueItem] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "artifact_type": self.artifact_type,
            "artifact_version": self.artifact_version,
            "source_candidate_ids": self.source_candidate_ids,
            "critique_runs": [r.to_dict() for r in self.critique_runs],
            "critiques": [c.to_dict() for c in self.critiques],
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> OutlineCritiquesV2:
        return cls(
            artifact_type=data.get("artifact_type", "outline_critiques"),
            artifact_version=data.get("artifact_version", "v1"),
            source_candidate_ids=data.get("source_candidate_ids", []),
            critique_runs=[CritiqueRun.from_dict(r) for r in data.get("critique_runs", [])],
            critiques=[CritiqueItem.from_dict(c) for c in data.get("critiques", [])],
        )


# ---------------------------------------------------------------------------
# Arbitration V2
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ArbitrationReport:
    artifact_type: str = "outline_arbitration_report"
    artifact_version: str = "v1"
    source_candidates: List[str] = field(default_factory=list)
    source_critiques: List[str] = field(default_factory=list)
    candidate_scores: Dict[str, float] = field(default_factory=dict)
    accepted_points: List[str] = field(default_factory=list)
    rejected_points: List[str] = field(default_factory=list)
    merged_strategy: str = ""
    final_decision: Dict[str, Any] = field(default_factory=dict)
    arbitrator_model: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "artifact_type": self.artifact_type,
            "artifact_version": self.artifact_version,
            "source_candidates": self.source_candidates,
            "source_critiques": self.source_critiques,
            "candidate_scores": self.candidate_scores,
            "accepted_points": self.accepted_points,
            "rejected_points": self.rejected_points,
            "merged_strategy": self.merged_strategy,
            "final_decision": self.final_decision,
            "arbitrator_model": self.arbitrator_model,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> ArbitrationReport:
        return cls(
            artifact_type=data.get("artifact_type", "outline_arbitration_report"),
            artifact_version=data.get("artifact_version", "v1"),
            source_candidates=data.get("source_candidates", []),
            source_critiques=data.get("source_critiques", []),
            candidate_scores=data.get("candidate_scores", {}),
            accepted_points=data.get("accepted_points", []),
            rejected_points=data.get("rejected_points", []),
            merged_strategy=data.get("merged_strategy", ""),
            final_decision=data.get("final_decision", {}),
            arbitrator_model=data.get("arbitrator_model", ""),
        )


# ---------------------------------------------------------------------------
# Final Outline V2
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FinalSection:
    section_id: str
    title: str
    purpose: str = ""
    argument_role: str = ""
    source_flow_steps: List[str] = field(default_factory=list)
    assigned_papers: List[Dict[str, str]] = field(default_factory=list)
    children: List[FinalSection] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "section_id": self.section_id,
            "title": self.title,
            "purpose": self.purpose,
            "argument_role": self.argument_role,
            "source_flow_steps": self.source_flow_steps,
            "assigned_papers": self.assigned_papers,
            "children": [c.to_dict() for c in self.children],
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> FinalSection:
        return cls(
            section_id=data["section_id"],
            title=data["title"],
            purpose=data.get("purpose", ""),
            argument_role=data.get("argument_role", ""),
            source_flow_steps=data.get("source_flow_steps", []),
            assigned_papers=data.get("assigned_papers", []),
            children=[cls.from_dict(c) for c in data.get("children", [])],
        )


@dataclass(frozen=True)
class FinalOutline:
    artifact_type: str = "final_outline"
    artifact_version: str = "v2"
    created_from_job_id: str = ""
    outline_id: str = ""
    source_literature_map_id: str = ""
    source_synthesis_flow_id: str = ""
    source_arbitration_report_id: str = ""
    source_literature_map_hash: str = ""
    source_synthesis_flow_hash: str = ""
    review_status: str = "arbitrated"
    adoption_status: str = "pending_user_adoption"
    sections: List[FinalSection] = field(default_factory=list)
    excluded_papers: List[Dict[str, str]] = field(default_factory=list)
    applied_critique_ids: List[str] = field(default_factory=list)
    unresolved_critique_ids: List[str] = field(default_factory=list)
    blocking_critique_ids: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "artifact_type": self.artifact_type,
            "artifact_version": self.artifact_version,
            "created_from_job_id": self.created_from_job_id,
            "outline_id": self.outline_id,
            "source_literature_map_id": self.source_literature_map_id,
            "source_synthesis_flow_id": self.source_synthesis_flow_id,
            "source_arbitration_report_id": self.source_arbitration_report_id,
            "source_literature_map_hash": self.source_literature_map_hash,
            "source_synthesis_flow_hash": self.source_synthesis_flow_hash,
            "review_status": self.review_status,
            "adoption_status": self.adoption_status,
            "sections": [s.to_dict() for s in self.sections],
            "excluded_papers": self.excluded_papers,
            "applied_critique_ids": self.applied_critique_ids,
            "unresolved_critique_ids": self.unresolved_critique_ids,
            "blocking_critique_ids": self.blocking_critique_ids,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> FinalOutline:
        return cls(
            artifact_type=data.get("artifact_type", "final_outline"),
            artifact_version=data.get("artifact_version", "v2"),
            created_from_job_id=data.get("created_from_job_id", ""),
            outline_id=data.get("outline_id", ""),
            source_literature_map_id=data.get("source_literature_map_id", ""),
            source_synthesis_flow_id=data.get("source_synthesis_flow_id", ""),
            source_arbitration_report_id=data.get("source_arbitration_report_id", ""),
            source_literature_map_hash=data.get("source_literature_map_hash", ""),
            source_synthesis_flow_hash=data.get("source_synthesis_flow_hash", ""),
            review_status=data.get("review_status", "arbitrated"),
            adoption_status=data.get("adoption_status", "pending_user_adoption"),
            sections=[FinalSection.from_dict(s) for s in data.get("sections", [])],
            excluded_papers=data.get("excluded_papers", []),
            applied_critique_ids=data.get("applied_critique_ids", []),
            unresolved_critique_ids=data.get("unresolved_critique_ids", []),
            blocking_critique_ids=data.get("blocking_critique_ids", []),
        )

    def to_markdown(self) -> str:
        """Project final outline to Markdown for human-readable consumption."""
        lines = ["# Literature Review Outline (V2)\n"]
        lines.append(f"<!-- Status: {self.review_status} -->")
        lines.append(f"<!-- Adoption: {self.adoption_status} -->\n")

        for i, section in enumerate(self.sections, 1):
            self._section_to_markdown(section, lines, level=2, number=i)

        return "\n".join(lines)

    @staticmethod
    def _section_to_markdown(section: FinalSection, lines: List[str], level: int, number: int) -> None:
        prefix = "#" * level
        lines.append(f"{prefix} {number}. {section.title}\n")
        if section.purpose:
            lines.append(f"**Purpose:** {section.purpose}\n")
        if section.argument_role:
            lines.append(f"**Role:** {section.argument_role}\n")
        if section.assigned_papers:
            lines.append("**Assigned Papers:**")
            for p in section.assigned_papers:
                lines.append(f"- {p.get('paper_key', '?')}: {p.get('role', '?')} ({p.get('reason', '')})")
            lines.append("")

        for j, child in enumerate(section.children, 1):
            FinalOutline._section_to_markdown(child, lines, level + 1, j)


# ---------------------------------------------------------------------------
# Coverage Audit
# ---------------------------------------------------------------------------

BLOCKING_ISSUE_TYPES = [
    "missing_paper_node",
    "invalid_flow_ref",
    "unclassified_paper",
    "orphan_paper",
    "missing_core_paper",
    "unsupported_gap_claim",
    "flow_step_uncovered",
    "section_without_supporting_papers",
    "canonical_coverage_below_threshold",
    "duplicate_canonical_assignment",
    "insufficient_effective_sections",
    "placeholder_section",
    "placeholder_sections_present",
    "empty_research_streams",
    "diagnostic_only_flow",
    "final_outline_blocked",
    "blocking_critique_unresolved",
    "unjustified_exclusion",
    "stream_uncovered",
]


@dataclass(frozen=True)
class CoverageIssue:
    issue_type: str
    description: str
    paper_key: str = ""
    section_id: str = ""
    flow_step_id: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "issue_type": self.issue_type,
            "description": self.description,
            "paper_key": self.paper_key,
            "section_id": self.section_id,
            "flow_step_id": self.flow_step_id,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> CoverageIssue:
        return cls(
            issue_type=data.get("issue_type", ""),
            description=data.get("description", ""),
            paper_key=data.get("paper_key", ""),
            section_id=data.get("section_id", ""),
            flow_step_id=data.get("flow_step_id", ""),
        )


@dataclass(frozen=True)
class CoverageAudit:
    artifact_type: str = "outline_coverage_audit"
    artifact_version: str = "v1"
    passed: bool = False
    blocking_issues: List[CoverageIssue] = field(default_factory=list)
    warnings: List[CoverageIssue] = field(default_factory=list)
    coverage_metrics: Dict[str, Any] = field(default_factory=dict)
    quality_gate_policy_snapshot: Dict[str, Any] = field(default_factory=dict)
    raw_node_coverage_ratio: float = 0.0
    canonical_paper_coverage_ratio: float = 0.0
    duplicate_assignment_count: int = 0
    effective_section_count: int = 0
    placeholder_section_count: int = 0
    source_final_outline_id: str = ""
    source_final_outline_hash: str = ""
    source_literature_map_hash: str = ""
    source_synthesis_flow_hash: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "artifact_type": self.artifact_type,
            "artifact_version": self.artifact_version,
            "passed": self.passed,
            "blocking_issues": [i.to_dict() for i in self.blocking_issues],
            "warnings": [w.to_dict() for w in self.warnings],
            "coverage_metrics": self.coverage_metrics,
            "quality_gate_policy_snapshot": self.quality_gate_policy_snapshot,
            "raw_node_coverage_ratio": self.raw_node_coverage_ratio,
            "canonical_paper_coverage_ratio": self.canonical_paper_coverage_ratio,
            "duplicate_assignment_count": self.duplicate_assignment_count,
            "effective_section_count": self.effective_section_count,
            "placeholder_section_count": self.placeholder_section_count,
            "source_final_outline_id": self.source_final_outline_id,
            "source_final_outline_hash": self.source_final_outline_hash,
            "source_literature_map_hash": self.source_literature_map_hash,
            "source_synthesis_flow_hash": self.source_synthesis_flow_hash,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> CoverageAudit:
        return cls(
            artifact_type=data.get("artifact_type", "outline_coverage_audit"),
            artifact_version=data.get("artifact_version", "v1"),
            passed=data.get("passed", False),
            blocking_issues=[CoverageIssue.from_dict(i) for i in data.get("blocking_issues", [])],
            warnings=[CoverageIssue.from_dict(w) for w in data.get("warnings", [])],
            coverage_metrics=data.get("coverage_metrics", {}),
            quality_gate_policy_snapshot=data.get("quality_gate_policy_snapshot", {}),
            raw_node_coverage_ratio=float(data.get("raw_node_coverage_ratio", 0.0) or 0.0),
            canonical_paper_coverage_ratio=float(data.get("canonical_paper_coverage_ratio", 0.0) or 0.0),
            duplicate_assignment_count=int(data.get("duplicate_assignment_count", 0) or 0),
            effective_section_count=int(data.get("effective_section_count", 0) or 0),
            placeholder_section_count=int(data.get("placeholder_section_count", 0) or 0),
            source_final_outline_id=data.get("source_final_outline_id", ""),
            source_final_outline_hash=data.get("source_final_outline_hash", ""),
            source_literature_map_hash=data.get("source_literature_map_hash", ""),
            source_synthesis_flow_hash=data.get("source_synthesis_flow_hash", ""),
        )


# ---------------------------------------------------------------------------
# Adopted Final Outline
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AdoptedFinalOutline:
    artifact_type: str = "adopted_final_outline"
    artifact_version: str = "v1"
    created_from_job_id: str = ""
    source_final_outline_id: str = ""
    source_final_outline_hash: str = ""
    source_coverage_audit_id: str = ""
    source_coverage_audit_hash: str = ""
    adopted_at: str = ""
    adopted_by: str = ""
    outline: FinalOutline = field(default_factory=FinalOutline)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "artifact_type": self.artifact_type,
            "artifact_version": self.artifact_version,
            "created_from_job_id": self.created_from_job_id,
            "source_final_outline_id": self.source_final_outline_id,
            "source_final_outline_hash": self.source_final_outline_hash,
            "source_coverage_audit_id": self.source_coverage_audit_id,
            "source_coverage_audit_hash": self.source_coverage_audit_hash,
            "adopted_at": self.adopted_at,
            "adopted_by": self.adopted_by,
            "outline": self.outline.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> AdoptedFinalOutline:
        return cls(
            artifact_type=data.get("artifact_type", "adopted_final_outline"),
            artifact_version=data.get("artifact_version", "v1"),
            created_from_job_id=data.get("created_from_job_id", ""),
            source_final_outline_id=data.get("source_final_outline_id", ""),
            source_final_outline_hash=data.get("source_final_outline_hash", ""),
            source_coverage_audit_id=data.get("source_coverage_audit_id", ""),
            source_coverage_audit_hash=data.get("source_coverage_audit_hash", ""),
            adopted_at=data.get("adopted_at", ""),
            adopted_by=data.get("adopted_by", ""),
            outline=FinalOutline.from_dict(data.get("outline", {})),
        )

    def to_markdown(self) -> str:
        """Project adopted final outline to Markdown."""
        return self.outline.to_markdown()
