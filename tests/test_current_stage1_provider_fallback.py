from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

import ai_interface
from test_current_stage1_generation import (
    _canonical_summary,
    _service,
    _visual_config_overrides,
    _write_pdf,
    _write_visual_pdf,
)


def test_current_stage1_default_reader_falls_back_from_primary_to_backup(
    tmp_path: Path, monkeypatch: Any
) -> None:
    pdf_path = tmp_path / "fallback-paper.pdf"
    _write_pdf(pdf_path)
    engines: list[str] = []

    def fake_detailed(
        prompt_text: str,
        primary_api_config: Mapping[str, Any],
        backup_api_config: Mapping[str, Any],
        *,
        engine_type: str = "primary",
        **kwargs: Any,
    ) -> Mapping[str, Any]:
        engines.append(engine_type)
        if engine_type == "primary":
            return {"status": "failed", "error_kind": "quota_exhausted", "message": "test quota"}
        return {"status": "success", "content": _canonical_summary()}

    monkeypatch.setattr(ai_interface, "get_summary_from_ai_detailed", fake_detailed)
    service, bundle = _service(tmp_path, pdf_path, reader=None)
    result = service.run(bundle)

    assert result.generated_count == 1
    assert engines[:2] == ["primary", "backup"]


def test_stage1_backup_success_is_recorded_as_text_only_after_visual_scan(
    tmp_path: Path,
) -> None:
    pdf_path = tmp_path / "visual-fallback-paper.pdf"
    _write_visual_pdf(pdf_path)

    def reader(**kwargs: Any) -> Mapping[str, Any]:
        if kwargs.get("purpose") == "visual_scan":
            batch = kwargs["visual_scan_batch"]
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
        return {
            "status": "success",
            "engine_type": "backup",
            "fallback_reason": "quota",
            "content": _canonical_summary(),
        }

    service, bundle = _service(
        tmp_path,
        pdf_path,
        reader,
        config_overrides=_visual_config_overrides(),
    )
    result = service.run(bundle)
    summary = result.summaries[0]

    assert result.generated_count == 1
    assert summary["provider"]["route"] == "Backup_Reader_API"
    assert summary["provider"]["successful_engine"] == "backup"
    assert summary["provider"]["successful_input_mode"] == "text_only"
    assert summary["provider"]["images_actually_sent_count"] == 0
    assert summary["provider"]["scan_coverage_status"] == "not_required"
    assert summary["provider"]["final_synthesis_modality"] == "text_only"
    assert summary["provider"]["final_raw_visual_recheck_status"] == "not_run_fallback"
    assert summary["provider"]["evidence_coverage_status"] == "degraded"
    assert summary["provider"]["visual_coverage_status"] == "degraded"
    assert "final_raw_visual_recheck_missing" in summary["ai_summary"]["quality_audit"]["conflict_flags"]
    assert summary["ai_summary"]["quality_audit"]["needs_manual_review"] is True
