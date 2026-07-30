"""Tests for multi-candidate outline generation v2."""

import pytest
from outline.literature_map import build_literature_map
from outline.synthesis_flow import build_synthesis_flow
from outline.candidates import (
    CandidateGenerationError,
    generate_candidates_deterministic,
    generate_candidates_production,
    generate_candidates_production_with_report,
    normalize_candidate_output,
    normalize_candidate_output_with_report,
    validate_candidate,
    validate_candidate_count,
)
from outline.v2_config import OutlineQualityGateConfig
from outline.v2_models import CandidateSection, OutlineCandidate


def _sample_summaries():
    return [
        {
            "paper_info": {
                "title": f"Paper {i}",
                "authors": [str(i)],
                "year": 2020 + i,
                "classification": "core" if i == 0 else "support",
            },
            "themes": ["promotion fairness" if i < 3 else "consumer trust"],
            "methods": ["survey"],
            "limitations": ["single context"],
        }
        for i in range(6)
    ]


def _make_candidates(count=3):
    lit_map = build_literature_map(_sample_summaries(), "job-001")
    flow = build_synthesis_flow(lit_map, "job-001")
    return generate_candidates_deterministic(lit_map, flow, candidate_count=count)


def _valid_provider_sections(flow_steps, paper_keys):
    return [
        {
            "section_id": "sec_theme",
            "title": "Promotion fairness scholarship",
            "purpose": "Synthesize the promotion fairness stream.",
            "argument_role": "synthesize_stream",
            "source_flow_steps": [flow_steps[0]],
            "assigned_papers": [paper_keys[0], paper_keys[1]],
        },
        {
            "section_id": "sec_trust",
            "title": "Consumer trust scholarship",
            "purpose": "Synthesize the consumer trust stream.",
            "argument_role": "synthesize_stream",
            "source_flow_steps": [flow_steps[1]],
            "assigned_papers": [paper_keys[2], paper_keys[3]],
        },
        {
            "section_id": "sec_gap",
            "title": "Cross-paper gaps and future research agenda",
            "purpose": "Synthesize corpus limitations and gaps.",
            "argument_role": "identify_gaps",
            "source_flow_steps": [flow_steps[2]],
            "assigned_papers": [paper_keys[4], paper_keys[5]],
        },
    ]


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
        assert calls == [("Outline_API", "outline_candidates"), ("Outline_API", "outline_candidates")]

    def test_production_retries_single_strategy_when_sections_are_missing(self):
        lit_map = build_literature_map(_sample_summaries(), "job-semantic-retry")
        flow = build_synthesis_flow(lit_map, "job-semantic-retry")
        flow_steps = [step.flow_step_id for step in flow.flow_steps if not step.placeholder_flow]
        paper_keys = [node.paper_key for node in lit_map.paper_nodes]
        valid_sections = _valid_provider_sections(flow_steps, paper_keys)
        calls = []

        def fake_caller(route, prompt, metadata):
            candidate_index = int(metadata["candidate_index"])
            semantic_retry = int(metadata.get("semantic_retry") or 0)
            calls.append((candidate_index, semantic_retry))
            if candidate_index in {2, 3} and semantic_retry == 0:
                return {"candidates": [{"candidate_id": f"candidate_{candidate_index}"}]}
            return {
                "candidates": [
                    {
                        "candidate_id": f"candidate_{candidate_index}",
                        "sections": valid_sections,
                    }
                ]
            }

        candidates, report = generate_candidates_production_with_report(
            lit_map,
            flow,
            3,
            "Outline_API",
            fake_caller,
        )

        assert candidates.candidate_count == 3
        assert [candidate.candidate_id for candidate in candidates.candidates] == [
            "candidate_1",
            "candidate_2",
            "candidate_3",
        ]
        assert calls == [(1, 0), (2, 0), (2, 1), (3, 0), (3, 1)]
        assert report["provider_valid"] == 3
        assert report["fallback_triggered"] is False
        assert report["provider_strategy_errors"][0]["recovered_by_semantic_retry"] is True
        assert all(
            validate_candidate(candidate, lit_map, flow, strict=True) == []
            for candidate in candidates.candidates
        )

    def test_production_semantic_retry_can_continue_with_two_valid_candidates(self):
        lit_map = build_literature_map(_sample_summaries(), "job-semantic-retry-two-valid")
        flow = build_synthesis_flow(lit_map, "job-semantic-retry-two-valid")
        flow_steps = [step.flow_step_id for step in flow.flow_steps if not step.placeholder_flow]
        paper_keys = [node.paper_key for node in lit_map.paper_nodes]
        valid_sections = _valid_provider_sections(flow_steps, paper_keys)
        calls = []

        def fake_caller(route, prompt, metadata):
            candidate_index = int(metadata["candidate_index"])
            semantic_retry = int(metadata.get("semantic_retry") or 0)
            calls.append((candidate_index, semantic_retry))
            if candidate_index == 1:
                return {
                    "candidates": [
                        {
                            "candidate_id": "provider_should_be_forced_to_candidate_1",
                            "sections": valid_sections,
                        }
                    ]
                }
            if candidate_index == 2 and semantic_retry == 1:
                return {
                    "candidates": [
                        {
                            "candidate_id": "provider_should_be_forced_to_candidate_2",
                            "sections": valid_sections,
                        }
                    ]
                }
            return {"candidates": [{"candidate_id": f"candidate_{candidate_index}", "sections": []}]}

        candidates, report = generate_candidates_production_with_report(
            lit_map,
            flow,
            3,
            "Outline_API",
            fake_caller,
        )

        assert calls == [(1, 0), (2, 0), (2, 1), (3, 0), (3, 1)]
        assert report["minimum_viable_count"] == 2
        assert report["provider_valid"] == 2
        assert report["fallback_triggered"] is False
        assert report["fallback_valid"] == 0
        assert report["final_valid_count"] == 2
        assert report["pipeline_continued"] is True
        assert [
            (item["candidate_index"], item["recovered_by_semantic_retry"])
            for item in report["provider_strategy_errors"]
        ] == [(2, True), (3, False)]
        assert [candidate.candidate_id for candidate in candidates.candidates] == [
            "candidate_1",
            "candidate_2",
        ]
        assert all(
            validate_candidate(candidate, lit_map, flow, strict=True) == []
            for candidate in candidates.candidates
        )

    def test_production_semantic_retry_fails_closed_when_below_minimum_viable(self):
        lit_map = build_literature_map(_sample_summaries(), "job-semantic-retry-below-minimum")
        flow = build_synthesis_flow(lit_map, "job-semantic-retry-below-minimum")
        flow_steps = [step.flow_step_id for step in flow.flow_steps if not step.placeholder_flow]
        paper_keys = [node.paper_key for node in lit_map.paper_nodes]
        valid_sections = _valid_provider_sections(flow_steps, paper_keys)
        calls = []

        def fake_caller(route, prompt, metadata):
            candidate_index = int(metadata["candidate_index"])
            semantic_retry = int(metadata.get("semantic_retry") or 0)
            calls.append((candidate_index, semantic_retry))
            if candidate_index == 1:
                return {
                    "candidates": [
                        {
                            "candidate_id": "provider_should_be_forced_to_candidate_1",
                            "sections": valid_sections,
                        }
                    ]
                }
            return {"candidates": [{"candidate_id": f"candidate_{candidate_index}", "sections": []}]}

        with pytest.raises(CandidateGenerationError) as excinfo:
            generate_candidates_production_with_report(
                lit_map,
                flow,
                3,
                "Outline_API",
                fake_caller,
            )

        report = excinfo.value.report
        assert calls == [(1, 0), (2, 0), (2, 1), (3, 0), (3, 1)]
        assert report["minimum_viable_count"] == 2
        assert report["provider_valid"] == 1
        assert report["fallback_triggered"] is False
        assert report["fallback_valid"] == 0
        assert report["final_valid_count"] == 1
        assert report["pipeline_continued"] is False
        assert [
            (item["candidate_index"], item["recovered_by_semantic_retry"])
            for item in report["provider_strategy_errors"]
        ] == [(2, False), (3, False)]
        assert "minimum viable 2" in str(excinfo.value)

    def test_production_unparseable_single_strategy_does_not_semantic_retry(self):
        lit_map = build_literature_map(_sample_summaries(), "job-semantic-no-retry-parse")
        flow = build_synthesis_flow(lit_map, "job-semantic-no-retry-parse")
        flow_steps = [step.flow_step_id for step in flow.flow_steps if not step.placeholder_flow]
        paper_keys = [node.paper_key for node in lit_map.paper_nodes]
        valid_sections = _valid_provider_sections(flow_steps, paper_keys)
        calls = []

        def fake_caller(route, prompt, metadata):
            candidate_index = int(metadata["candidate_index"])
            semantic_retry = int(metadata.get("semantic_retry") or 0)
            calls.append((candidate_index, semantic_retry))
            if candidate_index == 1 and semantic_retry == 0:
                return None
            if candidate_index == 1:
                raise AssertionError("unparseable provider output must not trigger semantic retry")
            return {
                "candidates": [
                    {
                        "candidate_id": f"provider_should_be_forced_to_candidate_{candidate_index}",
                        "sections": valid_sections,
                    }
                ]
            }

        candidates, report = generate_candidates_production_with_report(
            lit_map,
            flow,
            3,
            "Outline_API",
            fake_caller,
        )

        assert calls == [(1, 0), (2, 0), (3, 0)]
        assert report["minimum_viable_count"] == 2
        assert report["provider_valid"] == 2
        assert report["fallback_triggered"] is False
        assert report["final_valid_count"] == 2
        assert report["pipeline_continued"] is True
        assert report["provider_strategy_errors"][0]["candidate_index"] == 1
        assert report["provider_strategy_errors"][0]["recovered_by_semantic_retry"] is False
        assert report["provider_strategy_errors"][0]["attempts"] == [
            {
                "attempt": 1,
                "top_level_keys": "NoneType",
                "reason": "Unexpected candidate output type: NoneType",
            }
        ]
        assert [candidate.candidate_id for candidate in candidates.candidates] == [
            "candidate_2",
            "candidate_3",
        ]

    def test_normalizes_wrapped_outline_candidate_aliases(self):
        lit_map = build_literature_map(_sample_summaries(), "job-001")
        flow = build_synthesis_flow(lit_map, "job-001")
        flow_steps = [step.flow_step_id for step in flow.flow_steps if not step.placeholder_flow]
        paper_keys = [node.paper_key for node in lit_map.paper_nodes]
        first_sections = _valid_provider_sections(flow_steps, paper_keys)
        first_sections[0]["subsections"] = [
            {
                "heading": "Price fairness",
                "purpose": "Trace price fairness evidence.",
                "role": "supporting_context",
                "flow_steps": [{"flow_step_id": flow_steps[0]}],
                "supporting_papers": [{"paper_id": paper_keys[0]}],
            }
        ]
        second_sections = _valid_provider_sections(flow_steps, paper_keys)
        second_sections[0] = {
            "section_title": "Mechanisms of promotion fairness",
            "purpose": "Compare mechanisms.",
            "argument_role": "synthesize_stream",
            "source_steps": [flow_steps[0]],
            "papers": [{"canonical_paper_key": paper_keys[0], "role": "core"}, paper_keys[1]],
        }

        raw = {
            "result": {
                "outline_candidates": [
                    {
                        "id": "ignored_id",
                        "strategy": "theme_first",
                        "rationale": "Organize by themes.",
                        "outline": {"sections": first_sections},
                    },
                    {
                        "strategy": "mechanism_first",
                        "outline_sections": second_sections,
                    },
                ]
            }
        }

        candidates = normalize_candidate_output(raw, lit_map, flow, 2, "Outline_API")

        assert candidates.candidate_count == 2
        assert candidates.candidates[0].strategy_label == "theme_first"
        first_section = candidates.candidates[0].sections[0]
        assert first_section.title == "Promotion fairness scholarship"
        assert first_section.source_flow_steps == [flow_steps[0]]
        assert first_section.assigned_papers[0]["paper_key"] == paper_keys[0]
        assert first_section.children[0].assigned_papers[0]["paper_key"] == paper_keys[0]

    def test_normalizes_candidate_mapping_output(self):
        lit_map = build_literature_map(_sample_summaries(), "job-001")
        flow = build_synthesis_flow(lit_map, "job-001")
        flow_steps = [step.flow_step_id for step in flow.flow_steps if not step.placeholder_flow]
        paper_keys = [node.paper_key for node in lit_map.paper_nodes]
        sections_a = {
            section["section_id"]: section
            for section in _valid_provider_sections(flow_steps, paper_keys)
        }
        sections_b = _valid_provider_sections(flow_steps, paper_keys)

        raw = {
            "candidates": {
                "candidate_a": {
                    "approach": "stream_a",
                    "sections": sections_a,
                },
                "candidate_b": {
                    "approach": "stream_b",
                    "chapters": sections_b,
                },
            }
        }

        candidates = normalize_candidate_output(raw, lit_map, flow, 2, "Outline_API")

        assert [candidate.candidate_id for candidate in candidates.candidates] == [
            "candidate_a",
            "candidate_b",
        ]
        assert candidates.candidates[0].sections[0].section_id == "sec_theme"
        assert candidates.candidates[1].sections[1].assigned_papers[0]["paper_key"] == paper_keys[2]

    def test_normalizes_chinese_candidate_aliases(self):
        lit_map = build_literature_map(_sample_summaries(), "job-001")
        flow = build_synthesis_flow(lit_map, "job-001")
        flow_steps = [step.flow_step_id for step in flow.flow_steps if not step.placeholder_flow]
        paper_keys = [node.paper_key for node in lit_map.paper_nodes]
        candidate_key = "\u5019\u9009\u5927\u7eb2"
        id_key = "\u7f16\u53f7"
        strategy_key = "\u7b56\u7565"
        description_key = "\u8bf4\u660e"
        sections_key = "\u90e8\u5206"
        title_key = "\u6807\u9898"
        purpose_key = "\u76ee\u7684"
        role_key = "\u89d2\u8272"
        flow_key = "\u6d41\u7a0b\u6b65\u9aa4"
        papers_key = "\u652f\u6491\u6587\u732e"

        raw = {
            candidate_key: [
                {
                    id_key: "\u5019\u9009\u4e00",
                    strategy_key: "\u4e3b\u9898\u9012\u8fdb",
                    description_key: "\u6309\u4e3b\u9898\u9012\u8fdb\u7ec4\u7ec7\u3002",
                    sections_key: [
                        {
                            id_key: "\u7b2c\u4e00\u8282",
                            title_key: "\u4fc3\u9500\u516c\u5e73\u611f",
                            purpose_key: "\u5efa\u7acb\u95ee\u9898\u7a7a\u95f4",
                            role_key: "establish_problem_space",
                            flow_key: [flow_steps[0]],
                            papers_key: [paper_keys[0], paper_keys[1]],
                        },
                        {
                            id_key: "\u7b2c\u4e8c\u8282",
                            title_key: "\u6d88\u8d39\u8005\u4fe1\u4efb\u673a\u5236",
                            purpose_key: "\u7efc\u5408\u4fe1\u4efb\u673a\u5236",
                            role_key: "synthesize_stream",
                            flow_key: [flow_steps[1]],
                            papers_key: [paper_keys[2], paper_keys[3]],
                        },
                        {
                            id_key: "\u7b2c\u4e09\u8282",
                            title_key: "\u8de8\u8bba\u6587\u7814\u7a76\u7f3a\u53e3",
                            purpose_key: "\u7efc\u5408\u7814\u7a76\u7f3a\u53e3",
                            role_key: "identify_gaps",
                            flow_key: [flow_steps[2]],
                            papers_key: [paper_keys[4], paper_keys[5]],
                        },
                    ],
                }
            ]
        }

        candidates = normalize_candidate_output(raw, lit_map, flow, 1, "Outline_API")

        assert candidates.candidates[0].candidate_id == "\u5019\u9009\u4e00"
        assert candidates.candidates[0].strategy_label == "\u4e3b\u9898\u9012\u8fdb"
        assert candidates.candidates[0].sections[0].section_id == "\u7b2c\u4e00\u8282"
        assert candidates.candidates[0].sections[0].assigned_papers[0]["paper_key"] == paper_keys[0]

    def test_rejects_flat_single_candidate_without_real_sections(self):
        lit_map = build_literature_map(_sample_summaries(), "job-001")
        flow = build_synthesis_flow(lit_map, "job-001")
        paper_key = lit_map.paper_nodes[0].paper_key

        raw = {
            "candidate_id": "candidate_from_provider",
            "strategy_label": "provider_flat_strategy",
            "summary": "Provider returned one flattened candidate.",
            "section_id": "flat_sec_1",
            "title": "Promotion review opening",
            "purpose": "Frame the review problem.",
            "argument_role": "establish_problem_space",
            "paper_key": paper_key,
            "role": "core",
            "reason": "Directly supports the section.",
        }

        candidates, report = normalize_candidate_output_with_report(
            raw,
            lit_map,
            flow,
            3,
            "Outline_API",
            allow_deterministic_fallback=True,
        )

        assert report["provider_valid"] == 0
        assert report["fallback_triggered"] is True
        assert report["fallback_valid"] == 3
        assert "candidate_from_provider" not in {candidate.candidate_id for candidate in candidates.candidates}
        assert all(not validate_candidate(candidate, lit_map, flow, strict=True) for candidate in candidates.candidates)

    def test_fallback_topup_uses_real_synthesis_flow(self):
        summaries = [
            {
                "paper_info": {"title": f"Paper {i}", "authors": [f"A{i}"], "year": 2020 + i},
                "themes": ["promotion fairness" if i < 3 else "digital trust"],
                "methods": ["survey"],
                "limitations": ["single context"],
            }
            for i in range(6)
        ]
        lit_map = build_literature_map(summaries, "job-topup")
        flow = build_synthesis_flow(lit_map, "job-topup")
        first = generate_candidates_deterministic(lit_map, flow, 1).candidates[0].to_dict()

        candidates = normalize_candidate_output(
            {"candidates": [first]},
            lit_map,
            flow,
            3,
            "Outline_API",
            allow_deterministic_fallback=True,
        )

        assert candidates.candidate_count == 3
        assert len(candidates.candidates) == 3
        assert all(section.title != "Section title" for c in candidates.candidates for section in c.sections)
        assert "deterministic top-up" in candidates.candidates[1].summary

    def test_candidate_validation_rejects_placeholders_duplicates_and_bad_refs(self):
        lit_map = build_literature_map(_sample_summaries(), "job-validate")
        flow = build_synthesis_flow(lit_map, "job-validate")
        paper = lit_map.paper_nodes[0].paper_key
        bad = OutlineCandidate(
            candidate_id="bad",
            sections=[
                CandidateSection(
                    section_id="s1",
                    title="Research problem framing",
                    purpose="x",
                    source_flow_steps=["missing_flow"],
                    assigned_papers=[{"paper_key": paper}, {"paper_key": paper}],
                )
            ],
        )

        errors = validate_candidate(bad, lit_map, flow)

        assert any("placeholder" in error for error in errors)
        assert any("repeats canonical paper" in error for error in errors)
        assert any("invalid" in error for error in errors)

    def test_candidate_validation_uses_quality_gate_min_effective_sections(self):
        lit_map = build_literature_map(_sample_summaries(), "job-quality-gate")
        flow = build_synthesis_flow(lit_map, "job-quality-gate")
        flow_steps = [step.flow_step_id for step in flow.flow_steps if not step.placeholder_flow]
        paper_keys = [node.paper_key for node in lit_map.paper_nodes]
        candidate = OutlineCandidate(
            candidate_id="two_sections",
            sections=[
                CandidateSection(
                    section_id=section["section_id"],
                    title=section["title"],
                    purpose=section["purpose"],
                    argument_role=section["argument_role"],
                    source_flow_steps=section["source_flow_steps"],
                    assigned_papers=[{"paper_key": key} for key in section["assigned_papers"]],
                )
                for section in _valid_provider_sections(flow_steps, paper_keys)[:2]
            ],
        )

        default_errors = validate_candidate(candidate, lit_map, flow)
        local_errors = validate_candidate(
            candidate,
            lit_map,
            flow,
            quality_gate=OutlineQualityGateConfig(min_effective_sections=2),
        )

        assert any("fewer than 3 effective sections" in error for error in default_errors)
        assert not any("effective sections" in error for error in local_errors)

    def test_provider_candidate_salvages_mixed_valid_and_invalid_flow_refs(self):
        lit_map = build_literature_map(_sample_summaries(), "job-salvage")
        flow = build_synthesis_flow(lit_map, "job-salvage")
        flow_steps = [step.flow_step_id for step in flow.flow_steps if not step.placeholder_flow]
        paper_keys = [node.paper_key for node in lit_map.paper_nodes]
        sections = _valid_provider_sections(flow_steps, paper_keys)
        sections[0]["source_flow_steps"] = [flow_steps[0], "synthetic_method_section"]

        candidates, report = normalize_candidate_output_with_report(
            {"candidates": [{"candidate_id": "mixed_refs", "sections": sections}]},
            lit_map,
            flow,
            1,
            "Outline_API",
            quality_gate=OutlineQualityGateConfig(min_effective_sections=3),
        )

        assert report["provider_valid"] == 1
        assert report["salvage"]
        assert candidates.candidates[0].sections[0].source_flow_steps == [flow_steps[0]]
        assert (
            candidates.candidates[0].sections[0].assigned_papers[0]["provider_original_invalid_refs"]
            == "synthetic_method_section"
        )

    def test_provider_candidate_recovers_one_character_paper_key_typo(self):
        lit_map = build_literature_map(_sample_summaries(), "job-paper-key-typo")
        flow = build_synthesis_flow(lit_map, "job-paper-key-typo")
        flow_steps = [step.flow_step_id for step in flow.flow_steps if not step.placeholder_flow]
        paper_keys = [node.paper_key for node in lit_map.paper_nodes]
        typo_key = paper_keys[0].replace("paper", "poper", 1)
        sections = _valid_provider_sections(flow_steps, paper_keys)
        sections[0]["assigned_papers"] = [typo_key, paper_keys[1]]

        candidates, report = normalize_candidate_output_with_report(
            {"candidates": [{"candidate_id": "typo_candidate", "sections": sections}]},
            lit_map,
            flow,
            1,
            "Outline_API",
            quality_gate=OutlineQualityGateConfig(min_effective_sections=3),
        )

        assigned = candidates.candidates[0].sections[0].assigned_papers[0]
        assert report["provider_valid"] == 1
        assert assigned["paper_key"] == paper_keys[0]
        assert assigned["provider_original_paper_key"] == typo_key

    def test_provider_candidate_salvages_section_with_only_invalid_flow_ref(self):
        lit_map = build_literature_map(_sample_summaries(), "job-invalid-only-flow")
        flow = build_synthesis_flow(lit_map, "job-invalid-only-flow")
        paper_keys = [node.paper_key for node in lit_map.paper_nodes]
        sections = _valid_provider_sections(
            [step.flow_step_id for step in flow.flow_steps if not step.placeholder_flow],
            paper_keys,
        )
        target_step = next(
            step for step in flow.flow_steps
            if not step.placeholder_flow and step.role_in_review == sections[0]["argument_role"]
        )
        sections[0]["source_flow_steps"] = ["provider_made_up_flow_id"]
        sections[0]["assigned_papers"] = list(dict.fromkeys([*target_step.support_refs, paper_keys[0]]))[:2]

        candidates, report = normalize_candidate_output_with_report(
            {"candidates": [{"candidate_id": "invalid_only_flow", "sections": sections}]},
            lit_map,
            flow,
            1,
            "Outline_API",
            quality_gate=OutlineQualityGateConfig(min_effective_sections=3),
        )

        assert report["provider_valid"] == 1
        assert candidates.candidates[0].sections[0].source_flow_steps == [target_step.flow_step_id]
        assert (
            candidates.candidates[0].sections[0].assigned_papers[0]["provider_original_invalid_refs"]
            == "provider_made_up_flow_id"
        )

    def test_production_candidate_prompt_includes_full_stage1_summaries(self):
        lit_map = build_literature_map(_sample_summaries(), "job-full-stage1")
        flow = build_synthesis_flow(lit_map, "job-full-stage1")
        flow_steps = [step.flow_step_id for step in flow.flow_steps if not step.placeholder_flow]
        paper_keys = [node.paper_key for node in lit_map.paper_nodes]
        captured = {}

        def model_caller(route_name, prompt, metadata):
            captured["prompt"] = prompt
            return {
                "candidates": [
                    {
                        "candidate_id": "candidate_1",
                        "sections": _valid_provider_sections(flow_steps, paper_keys),
                    }
                ]
            }

        generate_candidates_production_with_report(
            lit_map,
            flow,
            1,
            "Outline_API",
            model_caller,
            OutlineQualityGateConfig(min_effective_sections=3),
            source_summaries=_sample_summaries(),
        )

        assert '"stage1_summaries_full"' in captured["prompt"]
        assert '"summary_index": 1' in captured["prompt"]
        assert '"paper_info"' in captured["prompt"]

    def test_production_candidate_prompt_omits_runtime_summary_baggage(self):
        summaries = _sample_summaries()
        summaries[0]["preprocess"] = {"diagnostics": "x" * 10000}
        summaries[0]["stage1_input"] = {"manifest": "y" * 10000}
        summaries[0]["attempt_history"] = [{"error": "z" * 10000}]
        summaries[0]["paper_info"]["source_descriptor"] = {"runtime": "w" * 10000}
        lit_map = build_literature_map(summaries, "job-compact-stage1")
        flow = build_synthesis_flow(lit_map, "job-compact-stage1")
        flow_steps = [step.flow_step_id for step in flow.flow_steps if not step.placeholder_flow]
        paper_keys = [node.paper_key for node in lit_map.paper_nodes]
        captured = {}

        def model_caller(route_name, prompt, metadata):
            captured["prompt"] = prompt
            return {
                "candidates": [
                    {
                        "candidate_id": "candidate_1",
                        "sections": _valid_provider_sections(flow_steps, paper_keys),
                    }
                ]
            }

        generate_candidates_production_with_report(
            lit_map,
            flow,
            1,
            "Outline_API",
            model_caller,
            OutlineQualityGateConfig(min_effective_sections=3),
            source_summaries=summaries,
        )

        assert '"ai_summary"' in captured["prompt"]
        assert '"preprocess"' not in captured["prompt"]
        assert '"stage1_input"' not in captured["prompt"]
        assert '"attempt_history"' not in captured["prompt"]
        assert '"source_descriptor"' not in captured["prompt"]
        assert "x" * 1000 not in captured["prompt"]

    def test_production_candidate_generation_calls_once_per_requested_candidate(self):
        lit_map = build_literature_map(_sample_summaries(), "job-split-candidates")
        flow = build_synthesis_flow(lit_map, "job-split-candidates")
        flow_steps = [step.flow_step_id for step in flow.flow_steps if not step.placeholder_flow]
        paper_keys = [node.paper_key for node in lit_map.paper_nodes]
        calls = []

        def model_caller(route_name, prompt, metadata):
            calls.append((route_name, metadata, prompt))
            idx = metadata["candidate_index"]
            return {
                "candidates": [
                    {
                        "candidate_id": f"provider_{idx}",
                        "strategy_label": ["mechanism_driven", "theory_evolution", "gap_driven"][idx - 1],
                        "sections": _valid_provider_sections(flow_steps, paper_keys),
                    }
                ]
            }

        candidates, report = generate_candidates_production_with_report(
            lit_map,
            flow,
            3,
            "Outline_API",
            model_caller,
            OutlineQualityGateConfig(min_effective_sections=3),
            source_summaries=_sample_summaries(),
        )

        assert len(calls) == 3
        assert [call[1]["candidate_index"] for call in calls] == [1, 2, 3]
        assert all('"candidate_count": 1' in call[2] for call in calls)
        assert report["provider_valid"] == 3
        assert len(candidates.candidates) == 3

    def test_split_candidate_generation_forces_unique_provider_ids(self):
        lit_map = build_literature_map(_sample_summaries(), "job-split-id")
        flow = build_synthesis_flow(lit_map, "job-split-id")
        flow_steps = [step.flow_step_id for step in flow.flow_steps if not step.placeholder_flow]
        paper_keys = [node.paper_key for node in lit_map.paper_nodes]

        def model_caller(_route_name, _prompt, _metadata):
            return {
                "candidates": [
                    {
                        "candidate_id": "candidate_1",
                        "strategy_label": "provider_reused_schema_id",
                        "sections": _valid_provider_sections(flow_steps, paper_keys),
                    }
                ]
            }

        candidates, report = generate_candidates_production_with_report(
            lit_map,
            flow,
            3,
            "Outline_API",
            model_caller,
            OutlineQualityGateConfig(min_effective_sections=3),
            source_summaries=_sample_summaries(),
        )

        assert report["provider_valid"] == 3
        assert [candidate.candidate_id for candidate in candidates.candidates] == [
            "candidate_1",
            "candidate_2",
            "candidate_3",
        ]
        assert candidates.candidates[1].sections[0].section_id.startswith("candidate_2")
        assert candidates.candidates[2].sections[0].section_id.startswith("candidate_3")

    def test_fallback_candidates_report_strategy_uniqueness_and_generation_minimum(self):
        lit_map = build_literature_map(_sample_summaries(), "job-fallback-uniqueness")
        flow = build_synthesis_flow(lit_map, "job-fallback-uniqueness")

        candidates, report = normalize_candidate_output_with_report(
            None,
            lit_map,
            flow,
            3,
            "Outline_API",
            allow_deterministic_fallback=True,
        )

        assert len(candidates.candidates) == 3
        assert {c.strategy_label for c in candidates.candidates} == {
            "mechanism_driven",
            "theory_evolution",
            "gap_driven",
        }
        assert report["fallback_strategy_diagnostics"]
        assert report["fallback_uniqueness"]
        assert not all(item["near_duplicate"] for item in report["fallback_uniqueness"])

    def test_bad_provider_candidate_is_rejected_and_fallback_tops_up(self):
        lit_map = build_literature_map(_sample_summaries(), "job-recover")
        flow = build_synthesis_flow(lit_map, "job-recover")
        flow_steps = [step.flow_step_id for step in flow.flow_steps if not step.placeholder_flow]
        paper_keys = [node.paper_key for node in lit_map.paper_nodes]
        invalid_first = {
            "candidate_id": "candidate_bad_first",
            "strategy": "too_thin",
            "sections": [
                {
                    "section_id": "bad_sec",
                    "title": "Bad provider section",
                    "purpose": "Too thin and missing flow refs.",
                    "assigned_papers": [paper_keys[0]],
                }
            ],
        }
        valid_second = {
            "candidate_id": "candidate_provider_valid",
            "strategy": "provider_valid",
            "sections": _valid_provider_sections(flow_steps, paper_keys),
        }
        invalid_third = {
            "candidate_id": "candidate_bad_third",
            "strategy": "bad_refs",
            "sections": [
                {
                    **section,
                    "source_flow_steps": ["not_a_real_flow_step"],
                }
                for section in _valid_provider_sections(flow_steps, paper_keys)
            ],
        }

        candidates, report = normalize_candidate_output_with_report(
            {"candidates": [invalid_first, valid_second, invalid_third]},
            lit_map,
            flow,
            3,
            "Outline_API",
            allow_deterministic_fallback=True,
        )

        assert report["provider_total"] == 3
        assert report["provider_valid"] == 2
        assert report["fallback_triggered"] is True
        assert report["fallback_valid"] == 1
        assert report["final_valid_count"] == 3
        assert report["pipeline_continued"] is True
        assert candidates.candidates[0].candidate_id == "candidate_provider_valid"
        assert candidates.candidates[1].candidate_id == "candidate_bad_third"
        assert report["salvage"]
        assert "candidate_bad_first" not in {candidate.candidate_id for candidate in candidates.candidates}
        assert all(
            not validate_candidate(candidate, lit_map, flow, strict=True)
            for candidate in candidates.candidates
        )

    def test_valid_provider_after_first_three_items_is_preserved_before_fallback(self):
        lit_map = build_literature_map(_sample_summaries(), "job-after-first-three")
        flow = build_synthesis_flow(lit_map, "job-after-first-three")
        flow_steps = [step.flow_step_id for step in flow.flow_steps if not step.placeholder_flow]
        paper_keys = [node.paper_key for node in lit_map.paper_nodes]
        invalid = [
            {
                "candidate_id": f"invalid_{idx}",
                "sections": [{"section_id": f"bad_{idx}", "title": "high", "assigned_papers": [paper_keys[0]]}],
            }
            for idx in range(3)
        ]
        late_valid = {
            "candidate_id": "late_valid_provider",
            "strategy": "late_valid",
            "sections": _valid_provider_sections(flow_steps, paper_keys),
        }

        candidates, report = normalize_candidate_output_with_report(
            {"candidates": [*invalid, late_valid]},
            lit_map,
            flow,
            3,
            "Outline_API",
            allow_deterministic_fallback=True,
        )

        assert report["provider_total"] == 4
        assert report["provider_valid"] == 1
        assert "late_valid_provider" in {candidate.candidate_id for candidate in candidates.candidates}

    def test_fixture_mode_provider_all_invalid_but_fallback_valid_continues(self):
        lit_map = build_literature_map(_sample_summaries(), "job-all-invalid")
        flow = build_synthesis_flow(lit_map, "job-all-invalid")
        paper_keys = [node.paper_key for node in lit_map.paper_nodes]

        candidates, report = normalize_candidate_output_with_report(
            {
                "candidates": [
                    {
                        "candidate_id": "invalid_1",
                        "sections": [{"title": "high", "assigned_papers": [paper_keys[0]]}],
                    },
                    {
                        "candidate_id": "invalid_2",
                        "sections": [{"title": "Section 2", "source_flow_steps": ["missing"]}],
                    },
                ]
            },
            lit_map,
            flow,
            3,
            "Outline_API",
            allow_deterministic_fallback=True,
        )

        assert report["provider_valid"] == 0
        assert report["fallback_triggered"] is True
        assert report["fallback_valid"] == 3
        assert len(candidates.candidates) == 3
        assert all(not validate_candidate(candidate, lit_map, flow, strict=True) for candidate in candidates.candidates)

    def test_fixture_mode_provider_parse_failure_can_recover_with_fallback(self):
        lit_map = build_literature_map(_sample_summaries(), "job-parse-fallback")
        flow = build_synthesis_flow(lit_map, "job-parse-fallback")

        candidates, report = normalize_candidate_output_with_report(
            None,
            lit_map,
            flow,
            3,
            "Outline_API",
            allow_deterministic_fallback=True,
        )

        assert report["provider_total"] == 0
        assert report["fallback_triggered"] is True
        assert report["fallback_valid"] == 3
        assert report["pipeline_continued"] is True
        assert len(candidates.candidates) == 3
        assert all(not validate_candidate(candidate, lit_map, flow, strict=True) for candidate in candidates.candidates)

    def test_production_provider_parse_failure_fails_closed_without_fallback(self):
        lit_map = build_literature_map(_sample_summaries(), "job-parse-fail-closed")
        flow = build_synthesis_flow(lit_map, "job-parse-fail-closed")

        with pytest.raises(CandidateGenerationError) as excinfo:
            normalize_candidate_output_with_report(
                None,
                lit_map,
                flow,
                3,
                "Outline_API",
            )

        report = excinfo.value.report
        assert report["provider_total"] == 0
        assert report["fallback_triggered"] is False
        assert report["fallback_valid"] == 0
        assert report["pipeline_continued"] is False
        assert "fallback is disabled" in str(excinfo.value)

    def test_production_all_invalid_provider_output_fails_closed_without_fallback(self):
        lit_map = build_literature_map(_sample_summaries(), "job-invalid-fail-closed")
        flow = build_synthesis_flow(lit_map, "job-invalid-fail-closed")
        paper_key = lit_map.paper_nodes[0].paper_key

        with pytest.raises(CandidateGenerationError) as excinfo:
            normalize_candidate_output_with_report(
                {"candidates": [{"candidate_id": "invalid", "sections": [{"title": "high", "assigned_papers": [paper_key]}]}]},
                lit_map,
                flow,
                3,
                "Outline_API",
            )

        report = excinfo.value.report
        assert report["provider_valid"] == 0
        assert report["fallback_triggered"] is False
        assert report["final_valid_count"] == 0
        assert report["pipeline_continued"] is False

    def test_production_two_valid_candidates_can_continue_without_fallback(self):
        lit_map = build_literature_map(_sample_summaries(), "job-partial-continue")
        flow = build_synthesis_flow(lit_map, "job-partial-continue")
        flow_steps = [step.flow_step_id for step in flow.flow_steps if not step.placeholder_flow]
        paper_keys = [node.paper_key for node in lit_map.paper_nodes]
        valid_sections = _valid_provider_sections(flow_steps, paper_keys)
        valid_first = {
            "candidate_id": "valid_first",
            "sections": valid_sections,
        }
        valid_second = {
            "candidate_id": "valid_second",
            "sections": valid_sections,
        }
        invalid_third = {
            "candidate_id": "invalid_third",
            "sections": valid_sections[:2],
        }

        candidates, report = normalize_candidate_output_with_report(
            {"candidates": [valid_first, valid_second, invalid_third]},
            lit_map,
            flow,
            3,
            "Outline_API",
        )

        assert report["minimum_viable_count"] == 2
        assert report["provider_valid"] == 2
        assert report["final_valid_count"] == 2
        assert report["pipeline_continued"] is True
        assert candidates.candidate_count == 2
        assert [candidate.candidate_id for candidate in candidates.candidates] == [
            "valid_first",
            "valid_second",
        ]

    def test_fixture_mode_provider_and_fallback_failure_summarizes_rejections(self):
        lit_map = build_literature_map(
            [{"paper_info": {"title": "Only Paper", "authors": ["A"], "year": 2020}, "themes": ["solo"]}],
            "job-fail-closed",
        )
        flow = build_synthesis_flow(lit_map, "job-fail-closed")

        with pytest.raises(CandidateGenerationError) as excinfo:
            normalize_candidate_output_with_report(
                {"candidates": [{"candidate_id": "bad", "sections": []}]},
                lit_map,
                flow,
                3,
                "Outline_API",
                allow_deterministic_fallback=True,
            )

        message = str(excinfo.value)
        assert "Candidate output contained 0 valid candidates" in message
        assert "Candidate bad has no sections" in message
        report = excinfo.value.report
        assert report["fallback_triggered"] is True
        assert report["pipeline_continued"] is False
        assert any(item["source"] == "fallback" for item in report["rejected_reasons"])

    def test_partial_valid_but_insufficient_candidate_set_does_not_continue(self):
        lit_map = build_literature_map(
            [
                {
                    "paper_info": {"title": f"Paper {i}", "authors": [f"A{i}"], "year": 2020 + i},
                    "themes": ["shared substantive topic"],
                }
                for i in range(3)
            ],
            "job-partial-fail",
        )
        flow = build_synthesis_flow(lit_map, "job-partial-fail")
        flow_id = next(step.flow_step_id for step in flow.flow_steps if not step.placeholder_flow)
        paper_keys = [node.paper_key for node in lit_map.paper_nodes]
        valid_but_only_one = {
            "candidate_id": "only_valid",
            "sections": [
                {
                    "section_id": f"sec_{idx}",
                    "title": f"Substantive section {idx}",
                    "purpose": "Synthesize the shared substantive topic.",
                    "argument_role": "synthesize_stream",
                    "source_flow_steps": [flow_id],
                    "assigned_papers": paper_keys,
                }
                for idx in range(3)
            ],
        }

        with pytest.raises(CandidateGenerationError) as excinfo:
            normalize_candidate_output_with_report(
                {"candidates": [valid_but_only_one]},
                lit_map,
                flow,
                3,
                "Outline_API",
            )

        report = excinfo.value.report
        assert report["minimum_viable_count"] == 2
        assert report["provider_valid"] == 1
        assert report["final_valid_count"] == 1
        assert report["pipeline_continued"] is False
