from __future__ import annotations

from pathlib import Path

from services.prompt_registry import PromptRegistry
from services.stage1_input_builder import Stage1InputBuilder


def test_stage1_multimodal_content_interleaves_labels_and_images(tmp_path: Path) -> None:
    image_path = tmp_path / "page.jpg"
    image_path.write_bytes(b"not-a-real-image")
    registry = PromptRegistry()
    built = Stage1InputBuilder().build(
        prompt_template="{{PAPER_FULL_TEXT}}",
        paper_text="paper",
        reader_api_config={
            "model": "deepseek-v4-flash-vision-exp",
            "api_base": "https://api.deepseek.com",
            "provider_family": "deepseek",
        },
        visual_bundle={
            "selected_visual_refs": [
                {
                    "visual_id": "page-001",
                    "page_no": 1,
                    "bbox": [0, 0, 20, 20],
                    "artifact_type": "page_snapshot",
                    "image_path": str(image_path),
                    "caption_excerpt": "Figure 1",
                    "nearby_text_excerpt": "OCR text",
                }
            ],
            "coverage_report": {"nonblank_pages": 1},
        },
        stage1_input_settings={"send_selected_visuals": "true"},
        prompt_identity=registry.identity("stage1.analysis.user.v3").to_dict(),
    )
    assert built.input_mode == "multimodal"
    assert built.user_message_content is not None
    image_index = next(
        index for index, item in enumerate(built.user_message_content)
        if item.get("type") == "local_image_path"
    )
    assert image_index > 0
    assert built.user_message_content[image_index - 1]["type"] == "text"
    assert "visual_id=page-001" in str(built.user_message_content[image_index - 1]["text"])


def test_stage1_adaptive_page_scan_honors_requested_single_page_batches() -> None:
    """Adaptive page-scan batches follow the configured batch size (exception path)."""
    registry = PromptRegistry()
    page_refs = [
        {
            "visual_id": f"page-{index:03d}",
            "page_no": index,
            "bbox": [0, 0, 20, 20],
            "artifact_type": "page_snapshot",
            "image_path": f"C:/fixture/page-{index:03d}.jpg",
            "image_bytes": 1,
        }
        for index in range(1, 14)
    ]

    built = Stage1InputBuilder().build(
        prompt_template="{{PAPER_FULL_TEXT}}",
        paper_text="paper",
        reader_api_config={
            "model": "deepseek-v4-flash-vision-exp",
            "api_base": "https://api.deepseek.com",
            "provider_family": "deepseek",
        },
        visual_bundle={
            "selected_visual_refs": page_refs,
            "selection_policy_snapshot": {
                "selection_mode": "adaptive_page_scan",
                "selection_contract_version": "stage1_visual_selection/v1",
            },
            "coverage_report": {
                "nonblank_pages": 13,
                "selection_mode": "adaptive_page_scan",
                "selection_contract_version": "stage1_visual_selection/v1",
            },
        },
        stage1_input_settings={
            "send_selected_visuals": "true",
            "visual_scan_batch_size": "1",
        },
        prompt_identity=registry.identity("stage1.analysis.user.v3").to_dict(),
    )

    assert built.visual_coverage["visual_extraction_strategy"] == "adaptive_page_scan"
    assert [len(batch) for batch in built.visual_scan_batches] == [1] * 13


def test_stage1_page_snapshots_without_adaptive_mode_do_not_create_page_scan_batches(tmp_path: Path) -> None:
    """Selective default mode never turns page snapshots into page-scan batches."""
    registry = PromptRegistry()
    page_refs = []
    for index in range(1, 14):
        image_path = tmp_path / f"page-{index:03d}.jpg"
        image_path.write_bytes(b"x")
        page_refs.append(
            {
                "visual_id": f"page-{index:03d}",
                "page_no": index,
                "bbox": [0, 0, 20, 20],
                "artifact_type": "page_snapshot",
                "image_path": str(image_path),
                "image_bytes": 1,
            }
        )

    built = Stage1InputBuilder().build(
        prompt_template="{{PAPER_FULL_TEXT}}",
        paper_text="paper",
        reader_api_config={
            "model": "deepseek-v4-flash-vision-exp",
            "api_base": "https://api.deepseek.com",
            "provider_family": "deepseek",
        },
        visual_bundle={
            "selected_visual_refs": page_refs,
            "coverage_report": {"nonblank_pages": 13},
        },
        stage1_input_settings={"send_selected_visuals": "true"},
        prompt_identity=registry.identity("stage1.analysis.user.v3").to_dict(),
    )

    assert built.visual_coverage["visual_extraction_strategy"] in {
        "direct_synthesis_visuals",
        "none",
    }
    assert built.visual_scan_batches == []
