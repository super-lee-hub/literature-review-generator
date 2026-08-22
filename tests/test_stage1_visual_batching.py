from __future__ import annotations

from pathlib import Path

from services.stage1_visual_scan import (
    build_visual_scan_user_content,
    plan_visual_scan_batches,
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
