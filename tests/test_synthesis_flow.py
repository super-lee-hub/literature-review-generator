"""Tests for synthesis flow builder."""

import pytest
from outline.literature_map import build_literature_map
from outline.synthesis_flow import build_synthesis_flow
from outline.quality_rules import is_low_quality_title
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


def test_core_analysis_streams_promote_without_top_level_themes():
    summaries = []
    for i in range(3):
        summaries.append(
            {
                "paper_info": {"title": f"Schema Paper {i}", "authors": [f"A{i}"], "year": 2020 + i},
                "ai_summary": {
                    "paper_metadata": {"title": f"Schema Paper {i}", "authors": [f"A{i}"], "year": 2020 + i},
                    "routing": {
                        "paper_type": "empirical",
                        "paper_subtype_normalized": "survey",
                        "classification_status": "resolved",
                        "route_confidence": "high",
                    },
                    "core_analysis": {
                        "summary": "Digital trust research.",
                        "key_points": ["digital trust", "promotion fairness"],
                        "methodology": "survey experiment",
                        "findings": "Trust increases adoption.",
                    },
                    "specialized_details": {
                        "empirical": {
                            "analysis_technique": "regression",
                            "core_variables": {
                                "independent": ["promotion fairness"],
                                "dependent": ["digital trust"],
                            },
                        }
                    },
                    "quality_audit": {"extraction_confidence": "high"},
                },
            }
        )

    lit_map = build_literature_map(summaries, "job-schema")
    flow = build_synthesis_flow(lit_map, "job-schema")

    assert lit_map.research_streams
    assert any("Digital trust" in step.claim or "Promotion fairness" in step.claim for step in flow.flow_steps)
    assert not flow.placeholder_flow


def test_thin_stream_not_promoted_as_main_section():
    lit_map = build_literature_map(
        [
            {
                "paper_info": {"title": "Lone Theme", "authors": ["A"], "year": 2022},
                "themes": ["one-off theme"],
            },
            {
                "paper_info": {"title": "Other Theme", "authors": ["B"], "year": 2023},
                "themes": ["other one-off"],
            },
        ],
        "job-thin",
    )
    flow = build_synthesis_flow(lit_map, "job-thin")

    assert all(
        step.role_in_review != "synthesize_stream"
        for step in flow.flow_steps
        if any("thin_stream" in diagnostic for diagnostic in step.diagnostics)
    )
    assert flow.placeholder_flow is True


def test_placeholder_diagnostic_flow_is_marked():
    lit_map = build_literature_map([], "job-empty")
    flow = build_synthesis_flow(lit_map, "job-empty")

    assert flow.placeholder_flow is True
    assert flow.flow_steps[0].placeholder_flow is True
    assert "diagnostic_only_flow" in flow.diagnostics


def test_noise_streams_do_not_become_main_flow_sections():
    lit_map = build_literature_map(
        [
            {
                "paper_info": {"title": f"Noise Paper {i}", "authors": [f"A{i}"], "year": 2020 + i},
                "themes": ["p001", "1986", "high"],
                "methods": ["experiment"],
            }
            for i in range(3)
        ],
        "job-noisy-flow",
    )
    flow = build_synthesis_flow(lit_map, "job-noisy-flow")

    claims = [step.claim.casefold() for step in flow.flow_steps]
    assert not any("synthesis of research stream: high" in claim for claim in claims)
    assert not any("experiment scholarship" in claim for claim in claims)
    assert flow.placeholder_flow is True
    assert "diagnostic_only_flow" in flow.diagnostics
    assert any("no_substantive_streams" in diagnostic for step in flow.flow_steps for diagnostic in step.diagnostics)


def test_method_term_alone_does_not_force_method_section_but_topic_phrases_survive():
    lit_map = build_literature_map(
        [
            {
                "paper_info": {"title": f"Topic Paper {i}", "authors": [f"A{i}"], "year": 2020 + i},
                "themes": ["consumer response model", "price effect heterogeneity"],
                "methods": ["experiment"],
            }
            for i in range(3)
        ],
        "job-topic-flow",
    )
    flow = build_synthesis_flow(lit_map, "job-topic-flow")

    claims = [step.claim for step in flow.flow_steps]
    assert not any(claim == "Experiment scholarship" for claim in claims)
    assert any("Consumer response model" in claim for claim in claims)
    assert any("Price effect heterogeneity" in claim for claim in claims)
    for step in flow.flow_steps:
        if step.role_in_review == "synthesize_stream":
            assert step.support_refs


def test_synthesis_titles_do_not_use_generic_scholarship_suffix():
    lit_map = build_literature_map(
        [
            {
                "paper_info": {"title": f"Fairness Paper {i}", "authors": [f"A{i}"], "year": 2020 + i},
                "themes": ["price fairness"],
            }
            for i in range(3)
        ],
        "job-title-quality",
    )
    flow = build_synthesis_flow(lit_map, "job-title-quality")

    claims = [step.claim for step in flow.flow_steps]
    assert "Price fairness scholarship" not in claims
    assert any(claim == "Synthesis of Price fairness" for claim in claims)


def test_long_method_text_does_not_become_methodological_title():
    long_method = "采用博弈论方法构建了一个非常长的动态定价公平判断模型并进行了多阶段仿真实验"
    lit_map = build_literature_map(
        [
            {
                "paper_info": {"title": f"Method Paper {i}", "authors": [f"A{i}"], "year": 2020 + i},
                "themes": ["price fairness"],
                "methods": [long_method],
            }
            for i in range(3)
        ],
        "job-long-method-title",
    )
    flow = build_synthesis_flow(lit_map, "job-long-method-title")

    assert not any(long_method in step.claim for step in flow.flow_steps)


def test_sentence_like_repeated_stage1_signals_stay_supporting_context():
    long_theme = "消费者遭受不公平价格后会主动抵制甚至传播负面口碑宁愿付出额外代价也要惩罚企业"
    summaries = [
        {
            "paper_info": {"title": f"Promotion Paper {i}", "authors": [f"A{i}"], "year": 2020 + i},
            "themes": ["price fairness", long_theme],
            "variables": ["purchase intention"],
            "findings": [long_theme],
        }
        for i in range(3)
    ]

    lit_map = build_literature_map(summaries, "job-sentence-stream")
    flow = build_synthesis_flow(lit_map, "job-sentence-stream")

    main_claims = [
        step.claim
        for step in flow.flow_steps
        if step.role_in_review == "synthesize_stream" and not step.placeholder_flow
    ]
    assert any("Price fairness" in claim for claim in main_claims)
    assert any("Purchase intention" in claim for claim in main_claims)
    assert not any(long_theme in claim for claim in main_claims)
    assert any(
        long_theme in diagnostic and step.role_in_review == "supporting_context"
        for step in flow.flow_steps
        for diagnostic in step.diagnostics
    )


def test_specific_gap_agenda_title_is_not_treated_as_placeholder():
    assert not is_low_quality_title("Identified Gaps and Future Research Agenda")
    assert is_low_quality_title("Identified gaps")
