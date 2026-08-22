from __future__ import annotations

import json
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
    synthesis_prompts: list[str] = []

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
                            "quantitative_values": ["17.3%"],
                            "relationships": ["X positively predicts Y"],
                            "layout_observations": [],
                            "ocr_conflicts": ["OCR says 13.7%, visual says 17.3%"],
                            "confidence": "high",
                            "needs_manual_review": False,
                        }
                        for visual_id, page_no in zip(batch["visual_ids"], batch["page_nos"])
                    ],
                },
            }
        synthesis_prompts.append(str(kwargs["prompt_text"]))
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
    final_prompt = result.summaries[0]["stage1_input"]["prompt_text"]
    assert synthesis_prompts
    assert synthesis_prompts[-1] == final_prompt
    assert '"17.3%"' in final_prompt
    assert "X positively predicts Y" in final_prompt
    assert "OCR says 13.7%, visual says 17.3%" in final_prompt
    assert '"visible"' in final_prompt
    assert result.summaries[0]["stage1_input"]["selected_visual_refs"]
    closure_record = service.registry.get("stage1:provider_receipt_closure")
    assert closure_record is not None
    closure_payload = json.loads(Path(closure_record.path).read_text(encoding="utf-8"))
    assert closure_payload["payload"]["complete"] is True
    assert len([record for record in service.registry.list_records() if record.artifact_type == "stage1_visual_observations"]) == 4


def test_long_paper_visual_run_reuses_without_rescan_or_provider_transport(tmp_path: Path) -> None:
    pdf_path = tmp_path / "long-reuse.pdf"
    document = fitz.open()
    for page_no in range(15):
        page = document.new_page()
        page.insert_text((72, 72), f"Page {page_no + 1} Figure {page_no + 1} evidence")
    document.save(pdf_path)
    document.close()
    calls: list[str] = []

    def reader(**kwargs):
        if kwargs.get("purpose") == "visual_scan":
            batch = kwargs["visual_scan_batch"]
            calls.extend(f"scan:{visual_id}" for visual_id in batch["visual_ids"])
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
                            "quantitative_values": ["17.3%"],
                            "relationships": ["X positively predicts Y"],
                            "layout_observations": [],
                            "ocr_conflicts": ["OCR says 13.7%, visual says 17.3%"],
                            "confidence": "high",
                            "needs_manual_review": False,
                        }
                        for visual_id, page_no in zip(batch["visual_ids"], batch["page_nos"])
                    ],
                },
            }
        calls.append("synthesis")
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
    first = service.run(bundle)
    first_call_count = len(calls)
    second = service.run(bundle, existing_summaries=first.summaries)

    assert first_call_count == 16
    assert calls.count("synthesis") == 1
    assert len(calls) == first_call_count
    assert second.generated_count == 0
    assert second.reused_count == 1
    assert second.expected_provider_transport_count == 0
    assert second.actual_provider_transport_count == 0
    assert second.summaries[0]["provider"]["transport_count"] == 0
    assert second.summaries[0]["stage1_reuse"]["binding"]["visual_input_manifest_hash"]
    assert second.summaries[0]["stage1_reuse"]["binding"]["visual_coverage_hash"]
