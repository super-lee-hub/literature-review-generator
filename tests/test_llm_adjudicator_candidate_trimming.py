from validation.llm_adjudicator import (
    _candidate_evidence_position_rank,
    _trim_candidates_for_stage,
)


def _candidate(
    text: str,
    *,
    confidence: float,
    scope: str = "chunk",
    chunk_id: str,
    resolver_tier: str = "preprocess_chunks",
    page_span: list[int] | None = None,
) -> dict:
    return {
        "resolver_tier": resolver_tier,
        "match_reason": "chunk_text_match",
        "confidence": confidence,
        "text_excerpt": text,
        "page_span": page_span or [],
        "chunk_ids": [chunk_id],
        "evidence_scope": scope,
        "source_grounded": True,
    }


def test_stronger_trim_keeps_body_results_over_high_confidence_appendix_noise():
    candidates = [
        _candidate(
            f"Appendix scale item {index}: manipulation-check measure and demographics.",
            confidence=0.99 - index * 0.01,
            scope="appendix_scale",
            chunk_id=f"appendix-{index}",
        )
        for index in range(6)
    ]
    candidates.append(
        _candidate(
            "Results show that perceived unfairness reduced continuance intention in the main analysis.",
            confidence=0.61,
            scope="results_body",
            chunk_id="results-1",
        )
    )

    trimmed = _trim_candidates_for_stage(candidates, stage="stronger")
    excerpts = [item["text_excerpt"] for item in trimmed]

    assert len(trimmed) == 6
    assert any("Results show" in excerpt for excerpt in excerpts)
    assert sum("Appendix scale item" in excerpt for excerpt in excerpts) == 5


def test_stronger_trim_keeps_measurement_evidence_over_reference_page_noise():
    reference_candidates = [
        _candidate(
            (
                f"Author, A. ({2000 + index}). Pricing study {index}. Journal of Retailing. "
                f"Writer, B. ({1990 + index}). Consumer analysis. Journal of Research. "
                f"Scholar, C. ({1980 + index}). Market results. Journal of Marketing."
            ),
            confidence=0.99 - index * 0.01,
            scope="page",
            chunk_id=f"references-{index}",
            resolver_tier="locator_page_index",
            page_span=[12],
        )
        for index in range(6)
    ]
    body_scale = _candidate(
        "Study 1 developed and validated a 17-item pricing persuasion knowledge scale with test-retest reliability.",
        confidence=0.61,
        scope="page",
        chunk_id="scale-body",
        resolver_tier="locator_page_index",
        page_span=[4],
    )
    body_result = _candidate(
        "Results showed that persuasion knowledge predicted product choice and purchase interest.",
        confidence=0.60,
        scope="page",
        chunk_id="results-body",
        resolver_tier="locator_page_index",
        page_span=[8],
    )

    trimmed = _trim_candidates_for_stage(
        [*reference_candidates, body_scale, body_result],
        stage="stronger",
    )
    excerpts = [item["text_excerpt"] for item in trimmed]

    assert any("17-item" in excerpt for excerpt in excerpts)
    assert any("predicted product choice" in excerpt for excerpt in excerpts)
    assert sum("Journal of Retailing" in excerpt for excerpt in excerpts) <= 4


def test_stronger_trim_dedupes_same_excerpt_across_resolver_tiers():
    repeated = "The study developed and validated a pricing persuasion knowledge scale."
    duplicates = [
        _candidate(
            repeated,
            confidence=0.95 - index * 0.01,
            scope="page" if index % 2 == 0 else "chunk",
            chunk_id=f"duplicate-{index}",
            resolver_tier=(
                "locator_page_index" if index % 2 == 0 else "preprocess_chunks"
            ),
            page_span=[4],
        )
        for index in range(6)
    ]
    distinct = _candidate(
        "The discussion identifies consumer education and realistic shopping as future research priorities.",
        confidence=0.50,
        scope="page",
        chunk_id="discussion-10",
        resolver_tier="locator_page_index",
        page_span=[10],
    )

    trimmed = _trim_candidates_for_stage([*duplicates, distinct], stage="stronger")
    excerpts = [item["text_excerpt"] for item in trimmed]

    assert excerpts.count(repeated) == 1
    assert distinct["text_excerpt"] in excerpts


def test_stronger_trim_demographics_overrides_generic_education_marker():
    demographic_candidates = [
        _candidate(
            (
                f"Participant demographics row {index} reports age, gender, income, "
                "and education level."
            ),
            confidence=0.99 - index * 0.01,
            scope="sample_characteristics",
            chunk_id=f"demographics-{index}",
        )
        for index in range(6)
    ]
    body_result = _candidate(
        "Results show that persuasion knowledge predicted purchase interest.",
        confidence=0.50,
        scope="results_body",
        chunk_id="results-body",
    )

    trimmed = _trim_candidates_for_stage(
        [*demographic_candidates, body_result],
        stage="stronger",
    )
    excerpts = [item["text_excerpt"] for item in trimmed]

    assert _candidate_evidence_position_rank(demographic_candidates[0]) > (
        _candidate_evidence_position_rank(body_result)
    )
    assert body_result["text_excerpt"] in excerpts
    assert sum("Participant demographics row" in excerpt for excerpt in excerpts) == 5


def test_stronger_trim_prioritizes_explicit_consumer_knowledge_phrase():
    generic_results = [
        _candidate(
            f"Results from analysis {index} report a statistically significant effect.",
            confidence=0.99 - index * 0.01,
            scope="results_body",
            chunk_id=f"results-{index}",
        )
        for index in range(6)
    ]
    consumer_knowledge = _candidate(
        "Consumer knowledge helps buyers distinguish a seller's persuasive technique.",
        confidence=0.50,
        scope="discussion_body",
        chunk_id="consumer-knowledge",
    )

    trimmed = _trim_candidates_for_stage(
        [*generic_results, consumer_knowledge],
        stage="stronger",
    )
    excerpts = [item["text_excerpt"] for item in trimmed]

    assert _candidate_evidence_position_rank(consumer_knowledge) < (
        _candidate_evidence_position_rank(generic_results[0])
    )
    assert consumer_knowledge["text_excerpt"] in excerpts
    assert sum("Results from analysis" in excerpt for excerpt in excerpts) == 5
