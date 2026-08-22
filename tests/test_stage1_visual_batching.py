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
