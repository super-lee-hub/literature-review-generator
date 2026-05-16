"""Tests for role-specific critique v2."""

import pytest
from outline.literature_map import build_literature_map
from outline.synthesis_flow import build_synthesis_flow
from outline.candidates import generate_candidates_deterministic
from outline.critique_v2 import (
    build_critiques_v2,
    normalize_critique_output,
    run_critique_production,
    run_coverage_critique_deterministic,
    run_structure_critique_deterministic,
)
from outline.v2_models import OutlineCritiquesV2


def _sample_summaries():
    return [
        {"paper_info": {"title": f"Paper {i}", "authors": [str(i)], "year": 2020 + i, "classification": "core" if i == 0 else "support"}, "themes": [f"theme_{i}"], "methods": [f"method_{i}"]}
        for i in range(3)
    ]


def _make_candidates(count=3):
    lit_map = build_literature_map(_sample_summaries(), "job-001")
    flow = build_synthesis_flow(lit_map, "job-001")
    return generate_candidates_deterministic(lit_map, flow, candidate_count=count)


class TestRoleSpecificCritique:

    def test_structure_critique_produces_run(self):
        candidates = _make_candidates()
        run = run_structure_critique_deterministic(candidates, "Writer_API")
        assert run.critic_role == "structure"
        assert run.critic_model == "Writer_API"
        # A run is always produced; critiques may vary by candidate content
        assert isinstance(run.critiques, list)

    def test_coverage_critique_produces_run(self):
        candidates = _make_candidates()
        run = run_coverage_critique_deterministic(candidates, "Primary_Reader_API")
        assert run.critic_role == "coverage"
        assert run.critic_model == "Primary_Reader_API"

    def test_critiques_v2_includes_both_runs(self):
        candidates = _make_candidates()
        structure_run = run_structure_critique_deterministic(candidates, "Writer_API")
        coverage_run = run_coverage_critique_deterministic(candidates, "Primary_Reader_API")
        critiques_v2 = build_critiques_v2(
            structure_run, coverage_run,
            [c.candidate_id for c in candidates.candidates],
        )

        assert isinstance(critiques_v2, OutlineCritiquesV2)
        assert len(critiques_v2.critique_runs) == 2
        roles = {r.critic_role for r in critiques_v2.critique_runs}
        assert "structure" in roles
        assert "coverage" in roles

    def test_critiques_have_valid_categories(self):
        candidates = _make_candidates()
        structure_run = run_structure_critique_deterministic(candidates)
        coverage_run = run_coverage_critique_deterministic(candidates)
        critiques_v2 = build_critiques_v2(
            structure_run, coverage_run,
            [c.candidate_id for c in candidates.candidates],
        )

        valid_categories = {
            "missing_theme", "weak_support_from_summaries", "redundant_section",
            "ordering_issue", "overclaim", "scope_mismatch",
            "missing_paper_coverage", "orphan_paper", "weak_flow_transition",
            "unsupported_gap_claim", "section_overload", "poor_synthesis",
            "paper_misplacement", "unjustified_exclusion",
        }
        for critique in critiques_v2.critiques:
            assert critique.critique_id != ""
            assert critique.category in valid_categories
            assert critique.severity in ("high", "medium", "low")

    def test_source_candidate_ids_present(self):
        candidates = _make_candidates()
        structure_run = run_structure_critique_deterministic(candidates)
        coverage_run = run_coverage_critique_deterministic(candidates)
        critiques_v2 = build_critiques_v2(
            structure_run, coverage_run,
            [c.candidate_id for c in candidates.candidates],
        )
        assert len(critiques_v2.source_candidate_ids) == 3


class TestCritiqueNormalization:

    def test_normalize_valid_json_output(self):
        raw = {
            "critiques": [
                {
                    "critique_id": "c1",
                    "category": "missing_paper_coverage",
                    "severity": "high",
                    "description": "Missing paper X",
                    "target_section_id": "sec_1",
                    "suggested_fix": "Add paper X to section",
                }
            ]
        }
        run = normalize_critique_output(raw, "Writer_API", "structure")
        assert len(run.critiques) == 1
        assert run.critiques[0].category == "missing_paper_coverage"

    def test_normalize_malformed_output_gracefully(self):
        run = normalize_critique_output("not valid json", "Writer_API", "structure")
        assert len(run.critiques) == 0
        assert len(run.diagnostics) > 0

    def test_normalize_partial_output(self):
        raw = {"critiques": [
            {"category": "missing_theme", "description": "Missing theme"},
            None,
            "not a dict",
        ]}
        run = normalize_critique_output(raw, "Writer_API", "structure")
        assert len(run.critiques) == 1
        assert len(run.diagnostics) > 0

    def test_normalize_empty_output(self):
        run = normalize_critique_output({}, "Writer_API", "structure")
        assert len(run.diagnostics) > 0


class TestProductionCritique:

    def test_production_critique_requires_model_caller(self):
        candidates = _make_candidates()
        with pytest.raises(RuntimeError):
            run_critique_production(candidates, "Writer_API", "structure", None)

    def test_production_critique_uses_model_caller(self):
        candidates = _make_candidates()
        calls = []

        def fake_caller(route, prompt, metadata):
            calls.append((route, metadata["stage"]))
            return {"critiques": [{"category": "missing_theme", "description": "x"}]}

        run = run_critique_production(candidates, "Writer_API", "structure", fake_caller)
        assert len(run.critiques) == 1
        assert calls == [("Writer_API", "structure_critique")]

    def test_unknown_category_is_preserved_with_diagnostic(self):
        run = normalize_critique_output(
            {"critiques": [{"category": "Novel Useful Category", "description": "x"}]},
            "Writer_API",
            "structure",
        )
        assert run.critiques[0].category == "novel_useful_category"
        assert any("Unknown critique category" in item for item in run.diagnostics)
