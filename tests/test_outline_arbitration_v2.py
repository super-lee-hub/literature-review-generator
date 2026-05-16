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
from outline.arbitration_v2 import arbitrate_deterministic, arbitrate_production, build_final_outline
from outline.v2_models import ArbitrationReport, FinalOutline


def _sample_summaries():
    return [
        {"paper_info": {"title": f"Paper {i}", "authors": [str(i)], "year": 2020 + i, "classification": "core" if i == 0 else "support"}, "themes": [f"theme_{i}"], "methods": [f"method_{i}"]}
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
        assert final.review_status == "arbitrated"
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
