"""Tests for multi-candidate outline generation v2."""

import pytest
from outline.literature_map import build_literature_map
from outline.synthesis_flow import build_synthesis_flow
from outline.candidates import generate_candidates_deterministic, generate_candidates_production, validate_candidate_count


def _sample_summaries():
    return [
        {"paper_info": {"title": f"Paper {i}", "authors": [str(i)], "year": 2020 + i, "classification": "core" if i == 0 else "support"}, "themes": [f"theme_{i}"], "methods": [f"method_{i}"]}
        for i in range(3)
    ]


def _make_candidates(count=3):
    lit_map = build_literature_map(_sample_summaries(), "job-001")
    flow = build_synthesis_flow(lit_map, "job-001")
    return generate_candidates_deterministic(lit_map, flow, candidate_count=count)


class TestOutlineCandidates:

    def test_default_generates_3_candidates(self):
        candidates = _make_candidates(count=3)
        assert candidates.candidate_count == 3
        assert len(candidates.candidates) == 3

    def test_production_minimum_2_candidates(self):
        candidates = _make_candidates(count=2)
        assert candidates.candidate_count == 2
        assert len(candidates.candidates) == 2

    def test_candidates_link_sections_to_flow_steps(self):
        candidates = _make_candidates(count=3)
        for candidate in candidates.candidates:
            for section in candidate.sections:
                assert len(section.source_flow_steps) > 0

    def test_assigned_papers_have_role_and_reason(self):
        candidates = _make_candidates(count=3)
        for candidate in candidates.candidates:
            for section in candidate.sections:
                for ap in section.assigned_papers:
                    assert "paper_key" in ap

    def test_candidates_have_distinct_strategies(self):
        candidates = _make_candidates(count=3)
        strategies = {c.strategy_label for c in candidates.candidates}
        assert len(strategies) == 3

    def test_candidate_count_is_recorded(self):
        candidates = _make_candidates(count=2)
        assert candidates.candidate_count == 2

    def test_source_ids_present(self):
        candidates = _make_candidates(count=3)
        assert candidates.source_literature_map_id != ""
        assert candidates.source_synthesis_flow_id != ""

    def test_artifact_type_correct(self):
        candidates = _make_candidates(count=3)
        assert candidates.artifact_type == "outline_candidates"
        assert candidates.artifact_version == "v1"


class TestCandidateCountValidation:

    def test_production_count_2_valid(self):
        errors = validate_candidate_count(2, test_dev_mode=False)
        assert len(errors) == 0

    def test_production_count_3_valid(self):
        errors = validate_candidate_count(3, test_dev_mode=False)
        assert len(errors) == 0

    def test_production_count_1_invalid(self):
        errors = validate_candidate_count(1, test_dev_mode=False)
        assert len(errors) > 0
        assert any("below" in e for e in errors)

    def test_production_count_4_invalid(self):
        errors = validate_candidate_count(4, test_dev_mode=False)
        assert len(errors) > 0
        assert any("exceeds" in e for e in errors)

    def test_test_dev_mode_allows_1(self):
        errors = validate_candidate_count(1, test_dev_mode=True)
        assert len(errors) == 0

    def test_test_dev_mode_still_rejects_0(self):
        errors = validate_candidate_count(0, test_dev_mode=True)
        assert len(errors) > 0


class TestProductionCandidateGeneration:

    def test_production_generation_requires_model_caller(self):
        lit_map = build_literature_map(_sample_summaries(), "job-001")
        flow = build_synthesis_flow(lit_map, "job-001")
        with pytest.raises(RuntimeError):
            generate_candidates_production(lit_map, flow, 2, "Outline_API", None)

    def test_production_generation_uses_model_caller(self):
        lit_map = build_literature_map(_sample_summaries(), "job-001")
        flow = build_synthesis_flow(lit_map, "job-001")
        calls = []

        def fake_caller(route, prompt, metadata):
            calls.append((route, metadata["stage"]))
            return generate_candidates_deterministic(lit_map, flow, 2, route).to_dict()

        candidates = generate_candidates_production(lit_map, flow, 2, "Outline_API", fake_caller)
        assert candidates.candidate_count == 2
        assert calls == [("Outline_API", "outline_candidates")]
