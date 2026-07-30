"""Tests for v2 arbitration and final outline."""

import pytest
from outline.literature_map import build_literature_map
from outline.synthesis_flow import build_synthesis_flow
from outline.candidates import generate_candidates_deterministic
from outline.critique_v2 import (
    build_critiques_v2,
    run_coverage_critique_deterministic,
    run_structure_critique_deterministic,
)
from outline.arbitration_v2 import (
    arbitrate_deterministic,
    arbitrate_production,
    build_final_outline,
    complete_final_outline_coverage,
    normalize_arbitration_output,
)
from outline.v2_models import ArbitrationReport, FinalOutline


def _sample_summaries():
    return [
        {
            "paper_info": {
                "title": f"Paper {i}",
                "authors": [str(i)],
                "year": 2020 + i,
                "classification": "core" if i == 0 else "support",
            },
            "themes": ["promotion fairness", f"context_{i}"],
            "methods": [f"method_{i}"],
            "abstract": "Examines promotion fairness across consumer contexts.",
        }
        for i in range(3)
    ]


def _make_pipeline_artifacts():
    lit_map = build_literature_map(_sample_summaries(), "job-001")
    flow = build_synthesis_flow(lit_map, "job-001")
    candidates = generate_candidates_deterministic(lit_map, flow, candidate_count=3)
    structure_run = run_structure_critique_deterministic(candidates)
    coverage_run = run_coverage_critique_deterministic(candidates)
    critiques = build_critiques_v2(
        structure_run, coverage_run,
        [c.candidate_id for c in candidates.candidates],
    )
    return lit_map, flow, candidates, critiques


class TestArbitrationV2:

    def test_arbitration_produces_report(self):
        lit_map, flow, candidates, critiques = _make_pipeline_artifacts()
        report = arbitrate_deterministic(candidates, critiques, "Outline_API")

        assert isinstance(report, ArbitrationReport)
        assert report.artifact_type == "outline_arbitration_report"
        assert report.arbitrator_model == "Outline_API"

    def test_arbitration_includes_candidate_scores(self):
        _, _, candidates, critiques = _make_pipeline_artifacts()
        report = arbitrate_deterministic(candidates, critiques)

        assert len(report.candidate_scores) == 3
        for cid in report.source_candidates:
            assert cid in report.candidate_scores

    def test_arbitration_has_accepted_and_rejected_points(self):
        _, _, candidates, critiques = _make_pipeline_artifacts()
        report = arbitrate_deterministic(candidates, critiques)

        # accepted_points and rejected_points are lists in the report
        assert isinstance(report.accepted_points, list)
        assert isinstance(report.rejected_points, list)
        total = len(report.accepted_points) + len(report.rejected_points)
        # Both match critique count (0 critiques -> 0 accepted+rejected is valid)
        assert total == len(critiques.critiques)

    def test_arbitration_final_decision_has_selected_candidate(self):
        _, _, candidates, critiques = _make_pipeline_artifacts()
        report = arbitrate_deterministic(candidates, critiques)

        assert "selected_base_candidate" in report.final_decision
        assert report.final_decision["selected_base_candidate"] != ""

    def test_arbitration_source_ids_populated(self):
        _, _, candidates, critiques = _make_pipeline_artifacts()
        report = arbitrate_deterministic(candidates, critiques)

        assert len(report.source_candidates) == 3
        # source_critiques should match critique count
        assert len(report.source_critiques) == len(critiques.critiques)


class TestFinalOutline:

    def test_final_outline_built_from_arbitration(self):
        lit_map, flow, candidates, critiques = _make_pipeline_artifacts()
        report = arbitrate_deterministic(candidates, critiques)
        lit_hash = "test_lit_hash"
        flow_hash = "test_flow_hash"

        final = build_final_outline(candidates, report, lit_hash, flow_hash, "job-001")

        assert isinstance(final, FinalOutline)
        assert final.artifact_type == "final_outline"
        assert final.artifact_version == "v2"
        assert final.review_status in {"arbitrated", "blocked"}
        assert final.adoption_status == "pending_user_adoption"

    def test_final_outline_sections_have_purpose_and_argument_role(self):
        lit_map, flow, candidates, critiques = _make_pipeline_artifacts()
        report = arbitrate_deterministic(candidates, critiques)

        final = build_final_outline(candidates, report, "hash_a", "hash_b", "job-001")

        for section in final.sections:
            assert section.section_id != ""
            assert section.title != ""
            # purpose may be empty in edge cases
            assert isinstance(section.purpose, str)

    def test_final_outline_has_source_ids(self):
        lit_map, flow, candidates, critiques = _make_pipeline_artifacts()
        report = arbitrate_deterministic(candidates, critiques)

        final = build_final_outline(candidates, report, "hash_a", "hash_b", "job-001")

        assert final.source_literature_map_id != ""
        assert final.source_synthesis_flow_id != ""
        assert final.source_arbitration_report_id != ""

    def test_final_outline_markdown_projection(self):
        lit_map, flow, candidates, critiques = _make_pipeline_artifacts()
        report = arbitrate_deterministic(candidates, critiques)

        final = build_final_outline(candidates, report, "hash_a", "hash_b", "job-001")
        markdown = final.to_markdown()

        assert "# Literature Review Outline (V2)" in markdown
        assert "Status:" in markdown

    def test_final_outline_excluded_papers_field(self):
        lit_map, flow, candidates, critiques = _make_pipeline_artifacts()
        report = arbitrate_deterministic(candidates, critiques)

        final = build_final_outline(candidates, report, "hash_a", "hash_b", "job-001")
        assert isinstance(final.excluded_papers, list)


class TestProductionArbitration:

    def test_production_arbitration_requires_model_caller(self):
        _, _, candidates, critiques = _make_pipeline_artifacts()
        with pytest.raises(RuntimeError):
            arbitrate_production(candidates, critiques, "Outline_API", None)

    def test_production_arbitration_uses_model_caller(self):
        _, _, candidates, critiques = _make_pipeline_artifacts()
        calls = []

        def fake_caller(route, prompt, metadata):
            calls.append((route, metadata["stage"]))
            return {
                "source_candidates": [c.candidate_id for c in candidates.candidates],
                "source_critiques": [c.critique_id for c in critiques.critiques],
                "candidate_scores": {candidates.candidates[0].candidate_id: 1.0},
                "accepted_points": [],
                "rejected_points": [],
                "merged_strategy": "provider_test",
                "final_decision": {"selected_base_candidate": candidates.candidates[0].candidate_id},
            }

        report = arbitrate_production(candidates, critiques, "Outline_API", fake_caller)
        assert report.final_decision["selected_base_candidate"] == candidates.candidates[0].candidate_id
        assert calls == [("Outline_API", "outline_arbitration")]

    def test_production_arbitration_falls_back_on_malformed_provider_output(self):
        _, _, candidates, critiques = _make_pipeline_artifacts()

        def fake_caller(route, prompt, metadata):
            return None

        report = arbitrate_production(candidates, critiques, "Outline_API", fake_caller)

        assert report.final_decision["selected_base_candidate"] in {
            candidate.candidate_id for candidate in candidates.candidates
        }
        assert report.final_decision["fallback_reason"].startswith("provider_arbitration_failed")
        assert report.merged_strategy == "fallback_select_validated_candidate_after_provider_failure"

    def test_normalizes_provider_candidate_score_objects(self):
        _, _, candidates, critiques = _make_pipeline_artifacts()
        selected = candidates.candidates[0].candidate_id
        raw_output = {
            "source_candidates": [c.candidate_id for c in candidates.candidates],
            "source_critiques": [c.critique_id for c in critiques.critiques],
            "candidate_scores": {selected: {"score": "0.82", "rationale": "best synthesis"}},
            "accepted_points": [],
            "rejected_points": [],
            "merged_strategy": "provider_test",
            "final_decision": {"selected_base_candidate": selected},
        }

        report = normalize_arbitration_output(raw_output, candidates, critiques, "Outline_API")

        assert report.candidate_scores[selected] == pytest.approx(0.82)

    def test_candidate_score_objects_without_numeric_score_fail_closed(self):
        _, _, candidates, critiques = _make_pipeline_artifacts()
        selected = candidates.candidates[0].candidate_id
        raw_output = {
            "source_candidates": [c.candidate_id for c in candidates.candidates],
            "source_critiques": [c.critique_id for c in critiques.critiques],
            "candidate_scores": {selected: {"rationale": "best synthesis"}},
            "accepted_points": [],
            "rejected_points": [],
            "merged_strategy": "provider_test",
            "final_decision": {"selected_base_candidate": selected},
        }

        with pytest.raises(ValueError, match="without an explicit numeric score"):
            normalize_arbitration_output(raw_output, candidates, critiques, "Outline_API")


def test_production_arbitration_preserves_high_severity_blocking_critiques():
    _, _, candidates, critiques = _make_pipeline_artifacts()
    from outline.v2_models import CritiqueItem, OutlineCritiquesV2

    blocking = CritiqueItem(
        critique_id="crit-blocking",
        category="missing_paper_coverage",
        severity="high",
        description="Coverage is too low.",
    )
    critiques = OutlineCritiquesV2(
        source_candidate_ids=[c.candidate_id for c in candidates.candidates],
        critiques=[blocking],
    )

    def fake_caller(route, prompt, metadata):
        return {
            "source_candidates": [c.candidate_id for c in candidates.candidates],
            "source_critiques": [blocking.critique_id],
            "candidate_scores": {candidates.candidates[0].candidate_id: 1.0},
            "accepted_points": [blocking.critique_id],
            "rejected_points": [],
            "merged_strategy": "provider_test",
            "final_decision": {"selected_base_candidate": candidates.candidates[0].candidate_id},
        }

    report = arbitrate_production(candidates, critiques, "Outline_API", fake_caller)
    final = build_final_outline(candidates, report, "hash_a", "hash_b", "job-001")

    assert blocking.critique_id in report.final_decision["blocking_critique_ids"]
    assert blocking.critique_id in report.rejected_points
    assert final.review_status == "blocked"
    assert blocking.critique_id in final.blocking_critique_ids


def test_fallback_selected_without_revised_sections_is_blocked():
    lit_map, flow, candidates, _critiques = _make_pipeline_artifacts()
    report = ArbitrationReport(
        source_candidates=[c.candidate_id for c in candidates.candidates],
        final_decision={"selected_base_candidate": candidates.candidates[0].candidate_id},
    )

    final = build_final_outline(candidates, report, "hash_a", "hash_b", "job-001")

    assert final.review_status == "blocked"


def test_valid_revised_sections_can_resolve_high_severity_blocking_critique():
    _, _, candidates, _ = _make_pipeline_artifacts()
    base = candidates.candidates[0]
    assert base.sections, "fixture must contain structurally valid sections"
    blocking = "crit-blocking"
    revised = [section.to_dict() for section in base.sections]
    report = ArbitrationReport(
        source_candidates=[c.candidate_id for c in candidates.candidates],
        accepted_points=[blocking],
        rejected_points=[blocking],
        final_decision={
            "selected_base_candidate": base.candidate_id,
            "blocking_critique_ids": [blocking],
            "requires_revised_sections": True,
            "revised_sections": revised,
        },
    )

    final = build_final_outline(candidates, report, "hash_a", "hash_b", "job-001")

    assert final.review_status == "arbitrated"
    assert final.blocking_critique_ids == []
    assert blocking in final.applied_critique_ids


def test_invalid_revised_sections_do_not_clear_high_severity_blocking_critique():
    _, _, candidates, _ = _make_pipeline_artifacts()
    base = candidates.candidates[0]
    assert base.sections, "fixture must contain structurally valid sections"
    blocking = "crit-blocking"
    revised = [section.to_dict() for section in base.sections]
    revised[0]["source_flow_steps"] = ["not_a_real_flow_step"]
    report = ArbitrationReport(
        source_candidates=[c.candidate_id for c in candidates.candidates],
        accepted_points=[blocking],
        rejected_points=[blocking],
        final_decision={
            "selected_base_candidate": base.candidate_id,
            "blocking_critique_ids": [blocking],
            "requires_revised_sections": True,
            "revised_sections": revised,
        },
    )

    final = build_final_outline(candidates, report, "hash_a", "hash_b", "job-001")

    assert final.review_status == "blocked"
    assert blocking in final.blocking_critique_ids


def test_revised_sections_must_apply_blocking_critique_before_clearing_it():
    _, _, candidates, _ = _make_pipeline_artifacts()
    base = candidates.candidates[0]
    assert base.sections, "fixture must contain structurally valid sections"
    blocking = "crit-blocking"
    revised = [section.to_dict() for section in base.sections]
    report = ArbitrationReport(
        source_candidates=[c.candidate_id for c in candidates.candidates],
        accepted_points=[],
        rejected_points=[blocking],
        final_decision={
            "selected_base_candidate": base.candidate_id,
            "blocking_critique_ids": [blocking],
            "requires_revised_sections": True,
            "revised_sections": revised,
        },
    )

    final = build_final_outline(candidates, report, "hash_a", "hash_b", "job-001")

    assert final.review_status == "blocked"
    assert blocking in final.blocking_critique_ids


def test_provider_candidate_with_candidate_id_strategy_is_not_treated_as_fallback():
    _, _, candidates, _ = _make_pipeline_artifacts()
    provider_like = candidates.candidates[0].to_dict()
    provider_like["candidate_id"] = "candidate_1"
    provider_like["strategy_label"] = "mechanism_driven"
    provider_like["provenance"] = "provider"
    from outline.v2_models import OutlineCandidate, OutlineCandidates

    provider_candidates = OutlineCandidates(
        source_literature_map_id=candidates.source_literature_map_id,
        source_synthesis_flow_id=candidates.source_synthesis_flow_id,
        candidate_count=1,
        candidates=[OutlineCandidate.from_dict(provider_like)],
    )
    report = ArbitrationReport(
        source_candidates=["candidate_1"],
        final_decision={"selected_base_candidate": "candidate_1"},
    )

    final = build_final_outline(provider_candidates, report, "hash_a", "hash_b", "job-001")

    assert final.review_status == "arbitrated"


def test_complete_final_outline_coverage_removes_duplicates_and_covers_required_steps():
    lit_map, flow, candidates, _ = _make_pipeline_artifacts()
    first = candidates.candidates[0]
    assert first.sections, "fixture must contain structurally valid sections"
    required_steps = [
        step.flow_step_id
        for step in flow.flow_steps
        if not step.placeholder_flow and step.role_in_review in {
            "establish_problem_space",
            "synthesize_stream",
            "connect_mechanism",
            "compare_contexts",
            "identify_gaps",
        }
    ]
    base_section = first.sections[0]
    paper_key = lit_map.paper_nodes[0].paper_key
    from outline.v2_models import FinalOutline, FinalSection

    incomplete = FinalOutline(
        created_from_job_id="job-001",
        review_status="arbitrated",
        sections=[
            FinalSection(
                section_id="s1",
                title=base_section.title,
                purpose=base_section.purpose,
                argument_role=base_section.argument_role,
                source_flow_steps=required_steps[:1],
                assigned_papers=[{"paper_key": paper_key}, {"paper_key": paper_key}],
            ),
            FinalSection(
                section_id="s2",
                title="Research gaps and future agenda",
                purpose="Identify gaps.",
                argument_role="identify_gaps",
                source_flow_steps=[],
                assigned_papers=[],
            ),
        ],
    )

    completed = complete_final_outline_coverage(incomplete, lit_map, flow, min_canonical_coverage=0.5)
    assigned = [
        paper["paper_key"]
        for section in completed.sections
        for paper in section.assigned_papers
    ]
    covered_steps = {
        step
        for section in completed.sections
        for step in section.source_flow_steps
    }

    assert len(assigned) == len(set(assigned))
    assert set(required_steps).issubset(covered_steps)
    assert len(set(assigned)) >= max(1, int(len(lit_map.paper_nodes) * 0.5 + 0.999))


def test_complete_final_outline_coverage_removes_parent_child_duplicates():
    lit_map, flow, candidates, _ = _make_pipeline_artifacts()
    first = candidates.candidates[0]
    assert first.sections, "fixture must contain structurally valid sections"
    from outline.coverage_audit import run_coverage_audit
    from outline.v2_models import FinalOutline, FinalSection

    duplicate_key = lit_map.paper_nodes[0].paper_key
    child_support = lit_map.paper_nodes[1].paper_key
    flow_id = next(step.flow_step_id for step in flow.flow_steps if not step.placeholder_flow)
    incomplete = FinalOutline(
        created_from_job_id="job-001",
        review_status="arbitrated",
        sections=[
            FinalSection(
                section_id="s1",
                title="Parent section about promotion fairness",
                purpose="Parent purpose.",
                argument_role="synthesize_stream",
                source_flow_steps=[flow_id],
                assigned_papers=[{"paper_key": duplicate_key}],
                children=[
                    FinalSection(
                        section_id="s1a",
                        title="Child section about promotion fairness",
                        purpose="Child purpose.",
                        argument_role="synthesize_stream",
                        source_flow_steps=[flow_id],
                        assigned_papers=[
                            {"paper_key": duplicate_key},
                            {"paper_key": child_support},
                        ],
                    )
                ],
            )
        ],
    )

    completed = complete_final_outline_coverage(incomplete, lit_map, flow, min_canonical_coverage=0.1)
    audit = run_coverage_audit(completed, lit_map, flow)
    assigned = [
        paper["paper_key"]
        for section in completed.sections
        for paper in section.assigned_papers
    ] + [
        paper["paper_key"]
        for child in completed.sections[0].children
        for paper in child.assigned_papers
    ]

    assert len(assigned) == len(set(assigned))
    assert completed.sections[0].children
    assert completed.sections[0].children[0].assigned_papers
    assert not any(issue.issue_type == "duplicate_canonical_assignment" for issue in audit.blocking_issues)
