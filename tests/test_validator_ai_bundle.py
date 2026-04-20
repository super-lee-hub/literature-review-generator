import pytest

from validation.review_validator import CitationValidationResult, ValidationConclusion
from validator import _build_report_from_results, _map_ai_bundle_result, _normalize_ai_confidence


def _base_result() -> CitationValidationResult:
    return CitationValidationResult(
        citation_id="cite-1",
        paper_id="paper-1",
        conclusion=ValidationConclusion.SUPPORTED,
        root_causes=[],
        evidence_candidates=[],
        details={},
        claim_text="Claim text",
        claim_context="Context",
        evidence_excerpt_list=["Evidence"],
        reasoning_summary="Existing reasoning",
        repair_hint="",
        citation_set_key="bundle-1",
        paper_ids=["paper-1"],
        block_ids=["block-1"],
        low_confidence=False,
    )


def test_normalize_ai_confidence_accepts_labels_and_percentages() -> None:
    assert _normalize_ai_confidence("high") == pytest.approx(0.85)
    assert _normalize_ai_confidence("medium") == pytest.approx(0.55)
    assert _normalize_ai_confidence("85%") == pytest.approx(0.85)
    assert _normalize_ai_confidence("0.42") == pytest.approx(0.42)


def test_map_ai_bundle_result_handles_string_confidence_without_crashing() -> None:
    mapped = _map_ai_bundle_result(
        _base_result(),
        {
            "status": "supported",
            "confidence": "high",
            "repair_scope": "none",
            "low_confidence": False,
            "reasoning": "Looks good",
            "repair_hint": "",
            "summary_paper_ids": ["paper-1"],
        },
    )

    assert mapped.conclusion == ValidationConclusion.SUPPORTED
    assert mapped.low_confidence is False
    assert mapped.details["ai_confidence"] == pytest.approx(0.85)
    assert mapped.evidence_status == "supported"
    assert mapped.disposition == "keep_as_is"


def test_map_ai_bundle_result_tracks_narrowed_and_kept_state() -> None:
    mapped = _map_ai_bundle_result(
        _base_result(),
        {
            "status": "supported",
            "confidence": 0.91,
            "repair_scope": "review",
            "disposition": "narrowed_and_kept",
            "low_confidence": False,
            "reasoning": "Narrowed to the supported minimum.",
            "repair_hint": "Keep the narrower version.",
            "summary_paper_ids": ["paper-1"],
        },
    )

    assert mapped.conclusion == ValidationConclusion.PARTIAL_SUPPORT
    assert mapped.disposition == "narrowed_and_kept"


def test_build_report_from_results_counts_narrowed_and_kept_separately() -> None:
    clean = _base_result()
    narrowed = _map_ai_bundle_result(
        _base_result(),
        {
            "status": "supported",
            "confidence": 0.9,
            "repair_scope": "review",
            "disposition": "narrowed_and_kept",
            "low_confidence": False,
            "reasoning": "Narrowed.",
            "repair_hint": "",
            "summary_paper_ids": ["paper-1"],
        },
    )

    report = _build_report_from_results([clean, narrowed])
    assert report.supported_count == 1
    assert report.narrowed_and_kept_count == 1
    assert report.partial_support_count == 1
