import math

import pytest

from outline.candidates import _budgeted_evidence_context, _controlled_literature_index
from outline.literature_map import build_literature_map
from outline.prompt_budget import (
    OutlinePromptBudgetExceeded,
    PromptBudgetV1,
    estimate_prompt_tokens,
)
from outline.synthesis_flow import build_synthesis_flow
from outline.v2_models import LiteratureMap


def test_prompt_budget_uses_exact_context_output_and_ten_percent_margin():
    budget = PromptBudgetV1(model_context_limit=100_001, max_output_tokens=12_000)
    assert budget.safety_margin_tokens == math.ceil(100_001 * 0.10)
    assert budget.input_budget_tokens == 100_001 - 12_000 - math.ceil(100_001 * 0.10)
    assert estimate_prompt_tokens("中文{\"value\":3.14}") > 0


def test_controlled_index_does_not_silently_truncate_research_streams():
    literature_map = LiteratureMap(
        research_streams=[{"stream_name": f"stream-{index}"} for index in range(81)]
    )
    assert len(_controlled_literature_index(literature_map)["research_streams"]) == 81


def test_oversized_corpus_is_synthesized_by_stream_without_dropping_papers():
    summaries = []
    for index in range(14):
        summaries.append(
            {
                "paper_info": {
                    "title": f"Budget paper {index}",
                    "authors": [f"Author {index}"],
                    "year": 2020,
                    "doi": f"10.1000/budget.{index}",
                },
                "themes": ["stream-a" if index < 7 else "stream-b"],
                "findings": "evidence " * 420,
            }
        )
    literature_map = build_literature_map(summaries, "job-budget")
    synthesis_flow = build_synthesis_flow(literature_map, "job-budget")
    budget = PromptBudgetV1(model_context_limit=12_000, max_output_tokens=2_000)
    calls = []

    def fake_caller(route, prompt, metadata):
        assert budget.fits(prompt)
        calls.append((route, prompt, dict(metadata)))
        return {"themes": ["compact"], "evidence_claims": []}

    context = _budgeted_evidence_context(
        literature_map,
        synthesis_flow,
        summaries,
        "Outline_API",
        fake_caller,
        budget,
        candidate_count=1,
        strategy_offset=0,
    )
    assert context is not None
    assert any(metadata["stage"] == "outline_stream_synthesis" for _, _, metadata in calls)
    covered = {
        key
        for synthesis in context["syntheses"]
        for key in synthesis.get("paper_keys", [])
    }
    assert covered == {node.paper_key for node in literature_map.paper_nodes}
    assert all("stage1_summary_packets" in prompt for _, prompt, metadata in calls if metadata["stage"] == "outline_stream_synthesis")


def test_indivisible_oversized_packet_fails_closed():
    budget = PromptBudgetV1(model_context_limit=2_000, max_output_tokens=500)
    summaries = [{
        "paper_info": {"title": "Huge", "authors": ["A"], "year": 2020},
        "findings": "x" * 20_000,
    }]
    literature_map = build_literature_map(summaries, "job-huge")
    synthesis_flow = build_synthesis_flow(literature_map, "job-huge")
    with pytest.raises(OutlinePromptBudgetExceeded):
        _budgeted_evidence_context(
            literature_map,
            synthesis_flow,
            summaries,
            "Outline_API",
            lambda *_args: {},
            budget,
            candidate_count=1,
        strategy_offset=0,
    )
