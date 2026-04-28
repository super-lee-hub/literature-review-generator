from services.stage1_input_selector import select_stage1_input


def _repeat(text: str, count: int = 12) -> str:
    return "\n".join([text] * count)


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
