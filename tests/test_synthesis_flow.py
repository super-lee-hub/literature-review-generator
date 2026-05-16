"""Tests for synthesis flow builder."""

import pytest
from outline.literature_map import build_literature_map
from outline.synthesis_flow import build_synthesis_flow
from outline.v2_models import SynthesisFlow


def _sample_summaries():
    return [
        {"paper_info": {"title": "Paper A", "authors": ["A"], "year": 2023, "classification": "core"}, "themes": ["theme_alpha", "theme_beta"], "methods": ["method_x"]},
        {"paper_info": {"title": "Paper B", "authors": ["B"], "year": 2022, "classification": "support"}, "themes": ["theme_alpha"], "methods": ["method_y"]},
        {"paper_info": {"title": "Paper C", "authors": ["C"], "year": 2021}, "themes": ["theme_gamma"], "limitations": ["small sample size"]},
    ]


class TestSynthesisFlow:

    def test_build_flow_from_literature_map(self):
        lit_map = build_literature_map(_sample_summaries(), "job-001")
        flow = build_synthesis_flow(lit_map, "job-001")

        assert isinstance(flow, SynthesisFlow)
        assert flow.artifact_type == "synthesis_flow"
        assert flow.artifact_version == "v1"
        assert len(flow.flow_steps) > 0

    def test_flow_consumes_literature_map_only_after_map_exists(self):
        lit_map = build_literature_map(_sample_summaries(), "job-001")
        flow = build_synthesis_flow(lit_map, "job-001")
        assert flow.source_literature_map_id != ""

    def test_every_flow_step_has_id_role_support_refs(self):
        lit_map = build_literature_map(_sample_summaries(), "job-001")
        flow = build_synthesis_flow(lit_map, "job-001")

        for step in flow.flow_steps:
            assert step.flow_step_id != ""
            assert step.role_in_review != ""
            assert isinstance(step.support_refs, list)

    def test_unsupported_central_gap_not_silently_promoted(self):
        summaries = [{"title": "Paper Only", "authors": ["A"]}]
        lit_map = build_literature_map(summaries, "job-001")
        flow = build_synthesis_flow(lit_map, "job-001")

        if flow.central_gap:
            assert flow.central_gap.get("confidence") == "low"
            assert "diagnostic" in flow.central_gap

    def test_overclaim_risk_on_thin_streams(self):
        summaries = [
            {"title": "Lone Paper", "themes": ["solo_theme"], "paper_info": {"title": "Lone Paper", "authors": ["X"]}}
        ]
        lit_map = build_literature_map(summaries, "job-001")
        flow = build_synthesis_flow(lit_map, "job-001")

        thin_steps = [s for s in flow.flow_steps if s.overclaim_risk == "high"]
        assert len(thin_steps) >= 1

    def test_transitions_between_consecutive_steps(self):
        lit_map = build_literature_map(_sample_summaries(), "job-001")
        flow = build_synthesis_flow(lit_map, "job-001")

        if len(flow.flow_steps) > 1:
            assert len(flow.transitions) == len(flow.flow_steps) - 1
            for t in flow.transitions:
                assert "from" in t
                assert "to" in t

    def test_empty_map_produces_diagnostic_step(self):
        lit_map = build_literature_map([], "job-001")
        flow = build_synthesis_flow(lit_map, "job-001")

        assert len(flow.flow_steps) == 1
        assert flow.flow_steps[0].role_in_review == "diagnostic"
        assert "empty_literature_map" in flow.flow_steps[0].diagnostics



def test_flow_step_ids_are_dense_after_optional_steps():
    lit_map = build_literature_map(
        [
            {"paper_info": {"title": "Core", "classification": "core"}, "themes": ["a"]},
            {"paper_info": {"title": "Support", "classification": "support"}, "themes": ["b"], "methods": ["survey"]},
        ],
        "job-001",
    )
    flow = build_synthesis_flow(lit_map, "job-001")
    assert [step.flow_step_id for step in flow.flow_steps] == [
        f"flow_step_{i:03d}" for i in range(1, len(flow.flow_steps) + 1)
    ]
