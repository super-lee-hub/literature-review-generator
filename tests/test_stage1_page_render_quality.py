from __future__ import annotations

import json
from pathlib import Path

import fitz  # type: ignore

from tests.test_current_stage1_generation import _canonical_summary, _service


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
