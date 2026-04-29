from services.stage1_input_selector import select_stage1_input
from services.stage1_input_completeness import is_blocked_stage1_quality


def _repeat(text: str, count: int = 12) -> str:
    return "\n".join([text] * count)


def _page_index(count: int) -> list[dict]:
    return [{"page_number": index + 1, "text": "page text"} for index in range(count)]


def test_selects_healthy_normalized_markdown() -> None:
    plain_text = _repeat("Promotion uncertainty shapes perceived price fairness and customer satisfaction.", 14)
    markdown = "# Findings\n\n" + plain_text

    selection = select_stage1_input(markdown_text=markdown, plain_text=plain_text, expected_language="en")

    assert selection.selected_source == "normalized_markdown"
    assert selection.quality_level in {"PASS", "WARN"}
    assert selection.selected_text == markdown


def test_falls_back_to_plain_text_when_normalized_markdown_is_garbled() -> None:
    plain_text = _repeat("购后促销的不确定性会影响消费者的价格公平感，并进一步改变满意度和后续购买意愿。", 18)
    garbled_markdown = _repeat(
        "A Sik MAVALOHAATUATOAUAUGTEAERUGAUATUOHUO DOI 10 3969 PROMOTION PRICE FAIRNESS",
        18,
    )

    selection = select_stage1_input(
        markdown_text=garbled_markdown,
        plain_text=plain_text,
        expected_language="zh",
    )

    assert selection.selected_source == "plain_text"
    assert selection.quality_level == "FALLBACK"
    assert selection.selected_text == plain_text
    assert selection.fallback_reason == "normalized_markdown_failed"
    assert "cjk_collapse" in selection.stage1_quality_reasons


def test_all_bad_candidates_request_reprocess_or_block() -> None:
    reprocess_selection = select_stage1_input(
        markdown_text="bad",
        plain_text="",
        allow_reprocess=True,
    )
    blocked_selection = select_stage1_input(
        markdown_text="bad",
        plain_text="",
        allow_reprocess=False,
    )

    assert reprocess_selection.quality_level == "REPROCESS"
    assert reprocess_selection.selected_text == ""
    assert blocked_selection.quality_level == "BLOCK"
    assert blocked_selection.selected_text == ""


def test_multpage_short_stage1_input_requests_reprocess() -> None:
    short_text = _repeat(
        "This paper introduces stored-rule and constructive choice processes in consumer behavior.",
        35,
    )

    selection = select_stage1_input(
        markdown_text=short_text,
        plain_text=short_text,
        page_index=_page_index(11),
        expected_language="en",
    )

    assert selection.quality_level == "REPROCESS"
    assert selection.selected_text == ""
    assert "incomplete_by_page_count" in selection.stage1_quality_reasons
    assert selection.manifest_payload["completeness_metrics"]["page_count"] == 11


def test_short_normalized_markdown_falls_back_to_long_plain_text() -> None:
    short_markdown = _repeat(
        "This paper introduces stored-rule and constructive choice processes in consumer behavior.",
        35,
    )
    long_plain = _repeat(
        "This article discusses consumer choice, methods, results, discussion, conclusions, and references.",
        700,
    )

    selection = select_stage1_input(
        markdown_text=short_markdown,
        plain_text=long_plain,
        page_index=_page_index(11),
        expected_language="en",
    )

    assert selection.selected_source == "plain_text"
    assert selection.quality_level == "FALLBACK"
    assert selection.selected_text == long_plain
    assert "shorter_than_alternative" not in selection.stage1_quality_reasons
    assert is_blocked_stage1_quality(selection.quality_level, selection.stage1_quality_reasons) is False
    normalized_report = next(report for report in selection.candidate_reports if report["source"] == "normalized_markdown")
    assert "shorter_than_alternative" in normalized_report["reasons"]


def test_short_two_page_input_is_not_blocked_by_completeness_gate() -> None:
    short_article = _repeat(
        "This short commentary summarizes a focused research note with discussion and references.",
        30,
    )

    selection = select_stage1_input(
        markdown_text=short_article,
        plain_text=short_article,
        page_index=_page_index(2),
        expected_language="en",
    )

    assert selection.selected_source == "normalized_markdown"
    assert selection.quality_level in {"PASS", "WARN"}
    assert "incomplete_by_page_count" not in selection.stage1_quality_reasons


def test_manifest_and_report_include_candidates_and_final_reason() -> None:
    plain_text = _repeat("购后促销的不确定性会影响消费者的价格公平感，并进一步改变满意度和后续购买意愿。", 12)
    garbled_markdown = _repeat("A Sik MAVALOHAATUATOAUAUGTEAERUGAUATUOHUO", 12)

    selection = select_stage1_input(
        markdown_text=garbled_markdown,
        plain_text=plain_text,
        page_index=[{"page_number": 1, "text": plain_text}],
        expected_language="zh",
    )

    assert selection.manifest_payload["selected_text_source"] == "plain_text"
    assert selection.manifest_payload["stage1_quality_level"] == "FALLBACK"
    assert selection.quality_report_payload["fallback_reason"] == "normalized_markdown_failed"
    assert {report["source"] for report in selection.candidate_reports} == {
        "normalized_markdown",
        "plain_text",
        "markdown_from_plain_text",
    }
