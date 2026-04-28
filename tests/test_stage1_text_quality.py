from services.stage1_text_quality import FAIL, PASS, WARN, score_text_quality


def _repeat(text: str, count: int = 12) -> str:
    return "\n".join([text] * count)


def test_chinese_garbled_markdown_fails_against_healthy_plain_text() -> None:
    plain_text = _repeat("购后促销的不确定性会影响消费者的价格公平感，并进一步改变满意度和后续购买意愿。", 18)
    garbled_markdown = _repeat(
        "A Sik MAVALOHAATUATOAUAUGTEAERUGAUATUOHUO DOI 10 3969 PROMOTION PRICE FAIRNESS",
        18,
    )

    normalized_result = score_text_quality(
        garbled_markdown,
        reference_text=plain_text,
        expected_language="zh",
    )
    plain_result = score_text_quality(plain_text, expected_language="zh")

    assert normalized_result.decision == FAIL
    assert "cjk_collapse" in normalized_result.reasons
    assert "suspected_garbled_markdown" in normalized_result.reasons
    assert plain_result.decision == PASS


def test_healthy_english_paper_is_not_rejected_for_low_cjk_ratio() -> None:
    english_text = _repeat(
        "This paper examines promotion uncertainty, perceived price fairness, consumer satisfaction, and post-purchase behavior using controlled experiments.",
        12,
    )

    result = score_text_quality(english_text, expected_language="en")

    assert result.decision == PASS
    assert "cjk_collapse" not in result.reasons


def test_normal_length_candidate_with_low_reference_overlap_fails() -> None:
    reference = _repeat(
        "Promotion framing changes consumer price fairness judgments and downstream loyalty through perceived loss.",
        16,
    )
    unrelated = _repeat(
        "Neural rendering systems optimize camera pose estimation and mesh reconstruction for outdoor scenes.",
        16,
    )

    result = score_text_quality(unrelated, reference_text=reference, expected_language="en")

    assert result.decision == FAIL
    assert "low_overlap" in result.reasons


def test_healthy_markdown_passes_against_plain_text_reference() -> None:
    plain_text = _repeat("消费者促销研究关注价格公平感、满意度、信任和重复购买意愿之间的关系。", 16)
    markdown = "# 研究摘要\n\n" + plain_text.replace("价格公平感", "**价格公平感**")

    result = score_text_quality(markdown, reference_text=plain_text, expected_language="zh")

    assert result.decision in {PASS, WARN}
    assert "cjk_collapse" not in result.reasons
    assert "low_overlap" not in result.reasons
