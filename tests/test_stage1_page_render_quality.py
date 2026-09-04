from __future__ import annotations

import json
from pathlib import Path

import fitz  # type: ignore
import pytest

import preprocess.visual_artifacts as visual_artifacts
from preprocess.visual_artifacts import Stage1VisualArtifactBuilder
from services.artifact_registry import file_sha256
from tests.test_current_stage1_generation import _canonical_summary, _service, _write_visual_pdf


def test_page_snapshots_are_clear_bounded_and_hashed(tmp_path: Path) -> None:
    pdf_path = tmp_path / "quality.pdf"
    document = fitz.open()
    for page_no in range(2):
        page = document.new_page()
        page.insert_text((72, 72), f"Page {page_no + 1} Figure and table evidence")
    document.save(pdf_path)
    document.close()

    def reader(**kwargs):
        return {"status": "success", "content": _canonical_summary()}

    service, bundle = _service(
        tmp_path,
        pdf_path,
        reader,
        config_overrides={
            "Stage1_Visual": {"enabled": "true"},
            "Stage1_Input": {"send_selected_visuals": "true"},
            "Primary_Reader_API": {
                "model": "deepseek-v4-flash-vision-exp",
                "api_base": "https://api.deepseek.com",
                "provider_family": "deepseek",
            },
        },
    )
    service.run(bundle)
    manifest_record = next(record for record in service.registry.list_records() if record.artifact_type == "visual_manifest")
    payload = json.loads(Path(manifest_record.path).read_text(encoding="utf-8"))
    pages = [item for item in payload["visuals"] if item["artifact_type"] == "page_snapshot"]
    assert len(pages) == 2
    for item in pages:
        assert item["image_format"] == "jpg"
        assert max(item["width"], item["height"]) >= 2000
        assert item["width"] * item["height"] <= 16_000_000
        assert item["image_bytes"] > 0
        assert len(item["image_sha256"]) == 64


def test_page_and_crop_pixel_budgets_are_applied_independently(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pdf_path = tmp_path / "pixel-budgets.pdf"
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 72), "Figure 1. Pixel budget test")
    document.save(pdf_path)
    document.close()

    policy = json.loads(json.dumps(visual_artifacts._DEFAULT_SELECTION_POLICY))
    policy["rendering"]["page_max_pixels"] = 111
    policy["rendering"]["crop_max_pixels"] = 222
    captured: list[tuple[bool, int]] = []

    def capture_render(self: Stage1VisualArtifactBuilder, **kwargs: object) -> bool:
        captured.append((kwargs.get("clip") is None, int(kwargs["max_rendered_pixels"])))
        return False

    monkeypatch.setattr(Stage1VisualArtifactBuilder, "_render_pixmap_if_safe", capture_render)
    builder = Stage1VisualArtifactBuilder()
    builder._materialize_visuals(
        source_pdf=str(pdf_path),
        page_candidates=[
            {
                "page_no": 1,
                "score": 1,
                "caption_excerpt": "",
                "nearby_text_excerpt": "",
                "selection_reason": "test",
                "dedupe_group_id": "page-1",
            }
        ],
        figure_candidates=[
            {
                "page_no": 1,
                "bbox": [72, 72, 200, 160],
                "score": 1,
                "caption_excerpt": "Figure 1",
                "nearby_text_excerpt": "",
                "selection_reason": "test",
                "dedupe_group_id": "figure-1",
                "artifact_type": "figure_crop",
            }
        ],
        layout_candidates=[],
        policy=policy,
        bundle_dir=str(tmp_path / "renders"),
        paper_key="pixel-budget-paper",
        artifact_hash="pixel-budget-hash",
    )

    assert captured == [(True, 111), (False, 222)]


def test_configured_png_pages_and_jpeg_crops_report_render_truth(tmp_path: Path) -> None:
    pdf_path = tmp_path / "format-truth.pdf"
    document = fitz.open()
    framework_page = document.new_page()
    framework_page.insert_text(
        (72, 72),
        "Research framework and conceptual framework. The page-level layout is "
        "itself the evidence unit for the proposed mechanism and workflow. "
        "The surrounding paragraph defines the constructs and study design.",
    )
    figure_page = document.new_page()
    figure_page.insert_text(
        (72, 72),
        "Figure 1. Research framework and treatment-to-outcome pathway. "
        "The surrounding paragraph explains the construct definitions and results.",
    )
    pixmap = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 320, 180), False)
    pixmap.clear_with(150)
    figure_page.insert_image(fitz.Rect(72, 110, 392, 290), pixmap=pixmap)
    document.save(pdf_path)
    document.close()

    def reader(**kwargs):
        return {"status": "success", "content": _canonical_summary()}

    service, bundle = _service(
        tmp_path,
        pdf_path,
        reader,
        config_overrides={
            "Stage1_Visual": {
                "enabled": "true",
                "page_format": "png",
                "crop_format": "jpeg",
                "page_jpeg_quality": "80",
            },
            "Stage1_Input": {"send_selected_visuals": "true"},
            "Primary_Reader_API": {
                "model": "deepseek-v4-flash-vision-exp",
                "api_base": "https://api.deepseek.com",
                "provider_family": "deepseek",
            },
        },
    )
    service.run(bundle)
    manifest_record = next(
        record for record in service.registry.list_records() if record.artifact_type == "visual_manifest"
    )
    payload = json.loads(Path(manifest_record.path).read_text(encoding="utf-8"))
    pages = [item for item in payload["visuals"] if item["artifact_type"] == "page_snapshot"]
    crops = [item for item in payload["visuals"] if item["artifact_type"] != "page_snapshot"]

    assert pages and crops
    assert all(item["image_format"] == "png" and Path(item["image_path"]).suffix == ".png" for item in pages)
    assert all(item["image_format"] == "jpg" and Path(item["image_path"]).suffix == ".jpg" for item in crops)
    for item in [*pages, *crops]:
        path = Path(item["image_path"])
        assert path.is_file()
        assert item["image_bytes"] == path.stat().st_size
        assert item["image_sha256"] == file_sha256(str(path))
