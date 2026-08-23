from __future__ import annotations

from pathlib import Path

from services.stage1_visual_scan import (
    build_visual_scan_user_content,
    plan_visual_scan_batches,
    select_final_visual_refs_after_scan,
    validate_visual_observations,
)


def _refs(count: int) -> list[dict[str, object]]:
    return [
        {
            "visual_id": f"page-{index:03d}",
            "page_no": index,
            "bbox": [0, 0, 10, 10],
            "artifact_type": "page_snapshot",
            "image_path": str(Path(__file__).resolve()),
            "caption_excerpt": f"caption {index}",
            "nearby_text_excerpt": f"ocr {index}",
        }
        for index in range(1, count + 1)
    ]


def test_long_visual_input_is_partitioned_by_page_order() -> None:
    batches = plan_visual_scan_batches(_refs(25), batch_size=10)
    assert [len(batch.visual_refs) for batch in batches] == [10, 10, 5]
    assert batches[0].visual_ids[0] == "page-001"
    assert batches[-1].visual_ids[-1] == "page-025"


def test_each_scan_image_is_preceded_by_its_label() -> None:
    batch = plan_visual_scan_batches(_refs(2), batch_size=10)[0]
    content = build_visual_scan_user_content(batch)
    assert [item["type"] for item in content] == ["text", "local_image_path", "text", "local_image_path"]
    assert "visual_id=page-001" in str(content[0]["text"])
    assert content[1]["visual_id"] == "page-001"


def _observation(visual_id: str, page_no: int, *, value: bool = False) -> dict[str, object]:
    return {
        "visual_id": visual_id,
        "page_no": page_no,
        "bbox": [0, 0, 10, 10],
        "artifact_type": "page_snapshot",
        "visible_text": ["visible"],
        "title_or_caption": None,
        "axes_or_headers": ["x"] if value else [],
        "legend_or_notes": [],
        "quantitative_values": ["20%"] if value else [],
        "relationships": ["up"] if value else [],
        "layout_observations": [],
        "ocr_conflicts": [],
        "confidence": "high",
        "needs_manual_review": False,
    }


def test_visual_observation_validation_requires_exact_sent_coverage() -> None:
    refs = _refs(2)
    batch = plan_visual_scan_batches(refs, batch_size=2)[0]
    valid = {
        "artifact_type": "stage1_visual_observations",
        "artifact_version": "v1",
        "observations": [_observation("page-001", 1), _observation("page-002", 2)],
    }
    normalized = validate_visual_observations(
        valid,
        allowed_visual_ids=batch.visual_ids,
        expected_visual_refs=batch.visual_refs,
        sent_visual_ids=batch.visual_ids,
    )
    assert len(normalized["observations"]) == 2

    for observations in ([], [_observation("page-001", 1)], [_observation("page-001", 1), _observation("page-001", 1)]):
        invalid = {**valid, "observations": observations}
        try:
            validate_visual_observations(
                invalid,
                allowed_visual_ids=batch.visual_ids,
                expected_visual_refs=batch.visual_refs,
                sent_visual_ids=batch.visual_ids,
            )
        except ValueError:
            pass
        else:
            raise AssertionError("partial or duplicate observations must fail closed")


def test_final_visual_selection_uses_observation_content_after_scan() -> None:
    refs = _refs(2)
    refs[0]["selection_score"] = 1
    refs[1]["selection_score"] = 1
    observations = [_observation("page-001", 1), _observation("page-002", 2, value=True)]
    selected = select_final_visual_refs_after_scan(refs, observations, max_refs=1)
    assert [item["visual_id"] for item in selected] == ["page-002"]


def _child_ref(
    visual_id: str,
    page_no: int,
    artifact_type: str,
    *,
    selection_score: float,
    bbox: list[int],
    caption: str = "",
) -> dict[str, object]:
    return {
        "visual_id": visual_id,
        "page_no": page_no,
        "bbox": bbox,
        "artifact_type": artifact_type,
        "image_path": str(Path(__file__).resolve()),
        "selection_score": selection_score,
        "caption_excerpt": caption,
        "nearby_text_excerpt": caption,
        "dedupe_group_id": visual_id,
    }


def test_child_crop_inherits_same_page_observation_and_table_beats_decorative_figure() -> None:
    refs = [
        {
            **_refs(1)[0],
            "selection_score": 0.1,
            "caption_excerpt": "page 7 overview",
            "nearby_text_excerpt": "page 7 overview",
        },
        _child_ref(
            "table-007-01",
            7,
            "table_crop",
            selection_score=0.5,
            bbox=[0, 0, 40, 40],
            caption="Table 2 percentage results",
        ),
        _child_ref(
            "figure-007-01",
            7,
            "figure_crop",
            selection_score=12.0,
            bbox=[50, 50, 90, 90],
            caption="decorative illustration",
        ),
    ]
    refs[0]["visual_id"] = "page-007"
    refs[0]["page_no"] = 7
    observations = [
        {
            **_observation("page-007", 7),
            "axes_or_headers": ["Outcome", "Treatment"],
            "quantitative_values": ["17.3%"],
            "ocr_conflicts": ["OCR says 13.7%, visual says 17.3%"],
            "relationships": [],
        }
    ]

    selected = select_final_visual_refs_after_scan(refs, observations, max_refs=1)

    assert [item["visual_id"] for item in selected] == ["table-007-01"]
    assert selected[0]["source_page_visual_id"] == "page-007"
    assert selected[0]["source_observation_visual_id"] == "page-007"
    assert selected[0]["score_components"]["artifact_type_match"] == 4.0


def test_framework_observation_selects_figure_crop_without_direct_crop_observation() -> None:
    refs = [
        {
            **_refs(1)[0],
            "visual_id": "page-008",
            "page_no": 8,
            "selection_score": 0.1,
        },
        _child_ref(
            "figure-008-01",
            8,
            "figure_crop",
            selection_score=0.2,
            bbox=[0, 0, 40, 40],
            caption="framework figure",
        ),
        _child_ref(
            "table-008-01",
            8,
            "table_crop",
            selection_score=8.0,
            bbox=[50, 50, 90, 90],
            caption="appendix table",
        ),
    ]
    observations = [
        {
            **_observation("page-008", 8),
            "visible_text": ["framework"],
            "axes_or_headers": [],
            "quantitative_values": [],
            "relationships": ["theory -> mechanism -> outcome"],
        }
    ]

    selected = select_final_visual_refs_after_scan(refs, observations, max_refs=1)

    assert [item["visual_id"] for item in selected] == ["figure-008-01"]
    assert selected[0]["source_page_visual_id"] == "page-008"
    assert selected[0]["source_observation_visual_id"] == "page-008"


def test_formula_observation_selects_formula_crop() -> None:
    refs = [
        _child_ref(
            "formula-009-01",
            9,
            "formula_crop",
            selection_score=0.1,
            bbox=[0, 0, 40, 40],
            caption="Equation 1 regression coefficient",
        ),
        _child_ref(
            "figure-009-01",
            9,
            "figure_crop",
            selection_score=6.0,
            bbox=[50, 50, 90, 90],
            caption="decorative figure",
        ),
    ]
    observations = [
        {
            **_observation("page-009", 9),
            "visible_text": ["Equation 1"],
            "layout_observations": ["equation contains beta coefficient"],
            "relationships": [],
            "quantitative_values": [],
            "axes_or_headers": [],
        }
    ]

    selected = select_final_visual_refs_after_scan(refs, observations, max_refs=1)

    assert [item["visual_id"] for item in selected] == ["formula-009-01"]


def test_substantive_low_heuristic_crop_beats_weak_high_heuristic_crop() -> None:
    refs = [
        _child_ref(
            "figure-010-weak",
            10,
            "figure_crop",
            selection_score=100.0,
            bbox=[0, 0, 40, 40],
            caption="illustration",
        ),
        _child_ref(
            "table-011-substantive",
            11,
            "table_crop",
            selection_score=0.1,
            bbox=[50, 50, 90, 90],
            caption="Table 3 outcome percentages",
        ),
    ]
    observations = [
        {**_observation("page-010", 10), "visible_text": ["illustration"]},
        {
            **_observation("page-011", 11),
            "quantitative_values": ["17.3%"],
            "relationships": [],
            "axes_or_headers": ["Outcome"],
            "ocr_conflicts": ["OCR says 13.7%"],
        },
    ]

    selected = select_final_visual_refs_after_scan(refs, observations, max_refs=1)

    assert [item["visual_id"] for item in selected] == ["table-011-substantive"]
