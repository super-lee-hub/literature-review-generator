from __future__ import annotations

from pathlib import Path

import fitz  # type: ignore

from tests.test_current_stage1_generation import _canonical_summary, _service


def test_long_paper_scans_every_nonblank_page_and_publishes_observations(tmp_path: Path) -> None:
    pdf_path = tmp_path / "long.pdf"
    document = fitz.open()
    for page_no in range(15):
        page = document.new_page()
        page.insert_text((72, 72), f"Page {page_no + 1} Figure {page_no + 1} evidence")
    document.save(pdf_path)
    document.close()
    scan_calls: list[int] = []

    def reader(**kwargs):
        if kwargs.get("purpose") == "visual_scan":
            batch = kwargs["visual_scan_batch"]
            scan_calls.append(int(batch["batch_index"]))
            return {
                "status": "success",
                "content": {
                    "artifact_type": "stage1_visual_observations",
                    "artifact_version": "v1",
                    "observations": [
                        {
                            "visual_id": visual_id,
                            "page_no": page_no,
                            "bbox": [0, 0, 1, 1],
                            "artifact_type": "page_snapshot",
                            "visible_text": ["visible"],
                            "title_or_caption": None,
                            "axes_or_headers": [],
                            "legend_or_notes": [],
                            "quantitative_values": [],
                            "relationships": [],
                            "layout_observations": [],
                            "ocr_conflicts": [],
                            "confidence": "high",
                            "needs_manual_review": False,
                        }
                        for visual_id, page_no in zip(batch["visual_ids"], batch["page_nos"])
                    ],
                },
            }
        return {"status": "success", "content": _canonical_summary()}

    service, bundle = _service(
        tmp_path,
        pdf_path,
        reader,
        config_overrides={
            "Stage1_Visual": {"enabled": "true"},
            "Stage1_Input": {
                "send_selected_visuals": "true",
                "single_call_max_pages": "12",
                "visual_scan_batch_size": "4",
            },
            "Primary_Reader_API": {
                "model": "deepseek-v4-flash-vision-exp",
                "api_base": "https://api.deepseek.com",
                "provider_family": "deepseek",
            },
        },
    )
    result = service.run(bundle)
    coverage = result.summaries[0]["stage1_input"]["visual_coverage"]
    assert scan_calls == [0, 1, 2, 3]
    assert coverage["nonblank_pages"] == 15
    assert coverage["visually_scanned_pages"] == 15
    assert coverage["coverage_status"] == "complete"
    assert result.summaries[0]["ai_summary"]["quality_audit"]["needs_manual_review"] is not True
    assert len([record for record in service.registry.list_records() if record.artifact_type == "stage1_visual_observations"]) == 4
