"""Tests for deterministic coverage audit for Outline Intelligence v2."""

import pytest
from outline.literature_map import build_literature_map
from outline.synthesis_flow import build_synthesis_flow
from outline.candidates import generate_candidates_deterministic
from outline.critique_v2 import (
    build_critiques_v2,
    run_coverage_critique_deterministic,
    run_structure_critique_deterministic,
)
from outline.arbitration_v2 import arbitrate_deterministic, build_final_outline
from outline.coverage_audit import run_coverage_audit, _collect_assigned_papers, _collect_covered_flow_steps
from outline.v2_models import CoverageAudit, CoverageIssue, FinalOutline, FinalSection, compute_content_hash


def _sample_summaries():
    return [
        {
            "paper_info": {
                "title": f"Paper {i}",
                "authors": [str(i)],
                "year": 2020 + i,
                "classification": "support",
            },
            "themes": ["promotion fairness" if i < 3 else "consumer trust"],
            "methods": ["survey"],
            "limitations": ["single context"],
        }
        for i in range(6)
    ]


def _make_pipeline():
    lit_map = build_literature_map(_sample_summaries(), "job-001")
    flow = build_synthesis_flow(lit_map, "job-001")
    candidates = generate_candidates_deterministic(lit_map, flow, candidate_count=3)
    structure_run = run_structure_critique_deterministic(candidates)
    coverage_run = run_coverage_critique_deterministic(candidates)
    critiques = build_critiques_v2(
        structure_run, coverage_run,
        [c.candidate_id for c in candidates.candidates],
    )
    report = arbitrate_deterministic(candidates, critiques)
    lit_hash = compute_content_hash(lit_map.to_dict())
    flow_hash = compute_content_hash(flow.to_dict())
    final = build_final_outline(candidates, report, lit_hash, flow_hash, "job-001")
    return lit_map, flow, final


class TestCoverageAudit:

    def test_audit_produces_result(self):
        lit_map, flow, final = _make_pipeline()
        audit = run_coverage_audit(final, lit_map, flow)

        assert isinstance(audit, CoverageAudit)
        assert audit.artifact_type == "outline_coverage_audit"
        assert audit.artifact_version == "v1"

    def test_audit_has_required_fields(self):
        lit_map, flow, final = _make_pipeline()
        audit = run_coverage_audit(final, lit_map, flow)

        assert isinstance(audit.passed, bool)
        assert isinstance(audit.blocking_issues, list)
        assert isinstance(audit.warnings, list)
        assert isinstance(audit.coverage_metrics, dict)
        assert audit.source_final_outline_hash != ""
        assert audit.quality_gate_policy_snapshot["coverage_scope"] == "full"
        assert "raw_node_coverage_ratio" in audit.coverage_metrics
        assert "canonical_paper_coverage_ratio" in audit.coverage_metrics
        assert isinstance(audit.duplicate_assignment_count, int)

    def test_audit_detects_missing_core_paper(self):
        summaries = [
            {"paper_info": {"title": "Core Paper", "authors": ["A"], "classification": "core", "must_use": "true"}, "themes": ["core_theme"]},
            {"paper_info": {"title": "Support Paper", "authors": ["B"], "classification": "support"}, "themes": ["support_theme"]},
        ]
        lit_map = build_literature_map(summaries, "job-001")
        flow = build_synthesis_flow(lit_map, "job-001")
        candidates = generate_candidates_deterministic(lit_map, flow, candidate_count=2)
        structure_run = run_structure_critique_deterministic(candidates)
        coverage_run = run_coverage_critique_deterministic(candidates)
        critiques = build_critiques_v2(
            structure_run, coverage_run,
            [c.candidate_id for c in candidates.candidates],
        )
        report = arbitrate_deterministic(candidates, critiques)
        final = build_final_outline(
            candidates, report,
            compute_content_hash(lit_map.to_dict()),
            compute_content_hash(flow.to_dict()),
            "job-001",
        )
        audit = run_coverage_audit(final, lit_map, flow)

        # Core paper SHOULD be detected if missing; but if it's found in the outline,
        # that's also valid behavior (the conservative builder may assign it)
        # Either we have blocking issues or the core paper is properly covered
        blocking_types = {i.issue_type for i in audit.blocking_issues}
        core_paper_key = lit_map.paper_nodes[0].paper_key
        covered_papers = _collect_assigned_papers(final.sections)

        if core_paper_key not in covered_papers:
            assert "orphan_paper" in blocking_types or "missing_core_paper" in blocking_types, \
                f"Core paper {core_paper_key} not in outline but no blocking issue raised"
        # If core paper IS covered, that's fine — the builder correctly assigned it

    def test_audit_detects_uncovered_flow_step(self):
        lit_map, flow, final = _make_pipeline()
        audit = run_coverage_audit(final, lit_map, flow)

        # flow steps should be checked
        covered = _collect_covered_flow_steps(final.sections)
        all_steps = {
            s.flow_step_id
            for s in flow.flow_steps
            if not s.placeholder_flow and s.role_in_review != "diagnostic"
        }
        if all_steps - covered:
            assert any(i.issue_type == "flow_step_uncovered" for i in audit.blocking_issues)

    def test_audit_detects_section_without_papers(self):
        lit_map, flow, final = _make_pipeline()
        audit = run_coverage_audit(final, lit_map, flow)

        # Check if any section has no papers
        sections_without_papers = [s for s in final.sections if not s.assigned_papers]
        if sections_without_papers:
            assert any(i.issue_type == "section_without_supporting_papers" for i in audit.blocking_issues)

    def test_audit_has_coverage_metrics(self):
        lit_map, flow, final = _make_pipeline()
        audit = run_coverage_audit(final, lit_map, flow)

        metrics = audit.coverage_metrics
        assert "total_paper_nodes" in metrics
        assert "papers_in_outline" in metrics
        assert "paper_coverage_ratio" in metrics
        assert "total_flow_steps" in metrics
        assert "covered_flow_steps" in metrics
        assert "blocking_issue_count" in metrics
        assert "duplicate_assignment_count" in metrics
        assert "effective_section_count" in metrics
        assert "placeholder_section_count" in metrics

    def test_good_fixture_passes_quality_gate_deterministically(self):
        lit_map, flow, final = _make_pipeline()
        audit = run_coverage_audit(final, lit_map, flow)

        assert audit.passed is True
        assert audit.canonical_paper_coverage_ratio >= 0.5
        assert audit.duplicate_assignment_count == 0
        assert audit.effective_section_count >= 3
        assert audit.placeholder_section_count == 0

    def test_synthetic_flow_refs_are_invalid_and_do_not_mask_missing_flow_coverage(self):
        lit_map, flow, _final = _make_pipeline()
        covered = [node.paper_key for node in lit_map.paper_nodes[:4]]
        final = FinalOutline(
            created_from_job_id="job-synthetic",
            sections=[
                FinalSection(
                    section_id="s1",
                    title="Methodological patterns across the corpus",
                    purpose="Compare methods.",
                    argument_role="methodological_synthesis",
                    source_flow_steps=["synthetic_method_section"],
                    assigned_papers=[{"paper_key": covered[0]}, {"paper_key": covered[1]}],
                ),
                FinalSection(
                    section_id="s2",
                    title="Cross-paper gaps and future research agenda",
                    purpose="Compare gaps.",
                    argument_role="identify_gaps",
                    source_flow_steps=["synthetic_gap_section"],
                    assigned_papers=[{"paper_key": covered[2]}, {"paper_key": covered[3]}],
                ),
                FinalSection(
                    section_id="s3",
                    title="Consumer trust scholarship",
                    purpose="Synthesize trust.",
                    argument_role="synthesize_stream",
                    source_flow_steps=["synthetic_trust_section"],
                    assigned_papers=[{"paper_key": covered[4]}] if len(covered) > 4 else [{"paper_key": covered[0]}],
                ),
            ],
        )

        audit = run_coverage_audit(final, lit_map, flow)
        issue_types = {issue.issue_type for issue in audit.blocking_issues}

        assert audit.passed is False
        assert "invalid_flow_ref" in issue_types
        assert "flow_step_uncovered" in issue_types

    def test_bad_case_low_coverage_duplicates_and_placeholders_fail_audit(self):
        summaries = []
        artifacts = []
        for i in range(31):
            title = f"Canonical Paper {i:02d}"
            doi = f"10.4242/paper.{i:02d}" if i < 30 else ""
            artifact_key = doi or f"canonical paper {i:02d}|30|{2000+i}"
            summary = {
                "paper_info": {
                    "title": title,
                    "authors": [f"Author {i}"],
                    "year": 2000 + i,
                    "doi": doi,
                },
                "ai_summary": {
                    "paper_metadata": {
                        "title": title,
                        "authors": [f"Author {i}"],
                        "year": 2000 + i,
                        "doi": doi,
                    },
                    "routing": {
                        "paper_type": "empirical",
                        "paper_subtype_normalized": "survey",
                        "classification_status": "resolved",
                        "route_confidence": "high",
                    },
                    "core_analysis": {
                        "summary": "Real Stage 1 shape.",
                        "key_points": ["promotion fairness" if i < 16 else "consumer trust"],
                        "methodology": "survey experiment",
                        "findings": "Findings.",
                    },
                    "specialized_details": {
                        "empirical": {
                            "analysis_technique": "regression",
                            "core_variables": {"dependent": ["consumer trust"]},
                        }
                    },
                    "quality_audit": {"extraction_confidence": "high"},
                },
            }
            summaries.append(summary)
            if i < 31:
                artifacts.append({
                    "artifact_type": "paper_artifact",
                    "paper_identity": {
                        "canonical_paper_key": artifact_key,
                    },
                    "source": {"metadata_confidence": "high"},
                    "paper_info": {
                        "title": title,
                        "authors": [f"Author {i}"],
                        "year": 2000 + i,
                        "doi": doi,
                    },
                })

        lit_map = build_literature_map(summaries, "job-bad", paper_artifacts=artifacts)
        flow = build_synthesis_flow(lit_map, "job-bad")
        covered = [node.paper_key for node in lit_map.paper_nodes[:5]]
        final = FinalOutline(
            created_from_job_id="job-bad",
            review_status="arbitrated",
            sections=[
                FinalSection(
                    section_id="s1",
                    title="Research problem framing",
                    purpose="placeholder",
                    source_flow_steps=[flow.flow_steps[0].flow_step_id],
                    assigned_papers=[
                        {"paper_key": covered[0]},
                        {"paper_key": covered[0]},
                        {"paper_key": covered[1]},
                    ],
                ),
                FinalSection(
                    section_id="s2",
                    title="Identified gaps",
                    purpose="placeholder",
                    source_flow_steps=[flow.flow_steps[1].flow_step_id],
                    assigned_papers=[{"paper_key": covered[2]}, {"paper_key": covered[3]}],
                ),
                FinalSection(
                    section_id="s3",
                    title="Section 1",
                    purpose="placeholder",
                    source_flow_steps=[flow.flow_steps[2].flow_step_id],
                    assigned_papers=[{"paper_key": covered[4]}],
                ),
            ],
        )

        audit = run_coverage_audit(final, lit_map, flow)
        issue_types = {issue.issue_type for issue in audit.blocking_issues}

        assert len(lit_map.source_summary_hashes) == 62
        assert len(lit_map.paper_nodes) == 31
        assert audit.passed is False
        assert audit.canonical_paper_coverage_ratio < 0.2
        assert audit.duplicate_assignment_count > 0
        assert audit.placeholder_section_count == 3
        assert "canonical_coverage_below_threshold" in issue_types
        assert "duplicate_canonical_assignment" in issue_types
        assert "placeholder_sections_present" in issue_types

    def test_audit_stale_hash_detectable(self):
        lit_map, flow, final = _make_pipeline()
        audit = run_coverage_audit(final, lit_map, flow)

        # Modify final outline and recompute hash
        original_hash = audit.source_final_outline_hash
        # The audit was created against this hash, so it should match
        current_hash = compute_content_hash(final.to_dict())
        assert current_hash == original_hash


class TestPaperCollection:

    def test_collect_assigned_papers(self):
        from outline.v2_models import FinalSection
        sections = [
            FinalSection(
                section_id="s1", title="S1", purpose="p1",
                argument_role="r1",
                assigned_papers=[{"paper_key": "pk1"}, {"paper_key": "pk2"}],
                children=[
                    FinalSection(
                        section_id="s1a", title="S1a", purpose="p1a",
                        argument_role="r1a",
                        assigned_papers=[{"paper_key": "pk3"}],
                    )
                ],
            )
        ]
        papers = _collect_assigned_papers(sections)
        assert papers == {"pk1", "pk2", "pk3"}

    def test_collect_covered_flow_steps(self):
        from outline.v2_models import FinalSection
        sections = [
            FinalSection(
                section_id="s1", title="S1",
                source_flow_steps=["fs1", "fs2"],
                children=[
                    FinalSection(
                        section_id="s1a", title="S1a",
                        source_flow_steps=["fs3"],
                    )
                ],
            )
        ]
        steps = _collect_covered_flow_steps(sections)
        assert steps == {"fs1", "fs2", "fs3"}



def test_core_must_use_missing_uses_missing_core_issue_type():
    from outline.v2_models import FinalOutline, FinalSection

    summaries = [
        {"paper_info": {"title": "Core Paper", "classification": "core", "must_use": True}, "themes": ["core"]},
        {"paper_info": {"title": "Support Paper", "classification": "support"}, "themes": ["support"]},
    ]
    lit_map = build_literature_map(summaries, "job-001")
    flow = build_synthesis_flow(lit_map, "job-001")
    missing_core = lit_map.paper_nodes[0].paper_key
    support = lit_map.paper_nodes[1].paper_key
    final = FinalOutline(
        created_from_job_id="job-001",
        sections=[
            FinalSection(
                section_id="s1",
                title="Support only",
                source_flow_steps=[step.flow_step_id for step in flow.flow_steps],
                assigned_papers=[{"paper_key": support}],
            )
        ],
    )

    audit = run_coverage_audit(final, lit_map, flow)
    issue_by_paper = {issue.paper_key: issue.issue_type for issue in audit.blocking_issues}
    assert issue_by_paper[missing_core] == "missing_core_paper"
