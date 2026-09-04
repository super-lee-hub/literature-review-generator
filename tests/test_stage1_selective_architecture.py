"""Red-first acceptance tests for the recovered selective Stage 1 contract."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import fitz  # type: ignore
import pytest

from tests.test_current_stage1_generation import _canonical_summary, _service
from services.stage1_input_builder import Stage1InputBuilder


def _write_text_pdf(path: Path, *, page_count: int = 30, table_pages: tuple[int, ...] = ()) -> None:
    document = fitz.open()
    table_page_set = set(table_pages)
    for page_no in range(1, page_count + 1):
        page = document.new_page()
        if page_no in table_page_set:
            page.insert_text(
                (72, 72),
                f"Table {len([p for p in table_pages if p <= page_no])}. "
                "Regression results. Estimate and standard error are reported below. "
                "The paragraph provides enough digital text for deterministic preprocessing quality checks.",
            )
        else:
            page.insert_text(
                (72, 72),
                f"Page {page_no}. The study reports methods, data, results, and limitations. "
                "This is a digital text layer with a complete paragraph and no layout-sensitive object."
            )
    document.save(path)
    document.close()


def _write_figure_pdf(path: Path, *, figure_count: int = 3) -> None:
    document = fitz.open()
    for figure_no in range(1, figure_count + 1):
        page = document.new_page()
        page.insert_text(
            (72, 72),
            f"Figure {figure_no}. Research framework and treatment-to-outcome pathway. "
            "The surrounding paragraph explains the construct definitions, study design, and observed relationship."
        )
        pixmap = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 320, 180), False)
        pixmap.clear_with(50 + figure_no * 40)
        page.insert_image(fitz.Rect(72, 110, 392, 290), pixmap=pixmap)
    document.save(path)
    document.close()


def _run_reader_calls(tmp_path: Path, pdf_path: Path, *, config_overrides: dict[str, dict[str, Any]]):
    calls: list[dict[str, Any]] = []

    def reader(**kwargs: Any) -> dict[str, Any]:
        calls.append(dict(kwargs))
        return {"status": "success", "content": _canonical_summary()}

    service, bundle = _service(
        tmp_path,
        pdf_path,
        reader,
        config_overrides=config_overrides,
    )
    result = service.run(bundle)
    return service, result, calls


def test_30_page_text_rich_paper_does_not_create_page_scan_calls(tmp_path: Path) -> None:
    pdf_path = tmp_path / "thirty-page-digital.pdf"
    _write_text_pdf(pdf_path)

    service, result, calls = _run_reader_calls(
        tmp_path,
        pdf_path,
        config_overrides={
            "Stage1_Visual": {
                "enabled": "true",
                "selection_mode": "selective",
                "render_all_nonblank_pages": "false",
            },
            "Stage1_Input": {"send_selected_visuals": "true"},
        },
    )

    assert len(calls) == 1
    assert not any(call.get("purpose") in {"visual_scan", "visual_extract"} for call in calls)
    assert len(service.receipt_ledger.list_receipts()) == 1
    coverage = result.summaries[0]["stage1_input"]["visual_coverage"]
    assert coverage["selection_mode"] == "selective"
    assert coverage["required_visual_unit_ids"] == []
    assert coverage["scan_coverage_status"] == "not_required"
    assert coverage["evidence_coverage_status"] == "not_required"


def test_selective_gate_prefers_three_figure_crops_without_page_duplicates(tmp_path: Path) -> None:
    pdf_path = tmp_path / "three-figures.pdf"
    _write_figure_pdf(pdf_path, figure_count=3)

    _service_instance, result, calls = _run_reader_calls(
        tmp_path,
        pdf_path,
        config_overrides={
            "Stage1_Visual": {
                "enabled": "true",
                "selection_mode": "selective",
                "render_all_nonblank_pages": "false",
            },
            "Stage1_Input": {"send_selected_visuals": "true"},
        },
    )

    assert len(calls) == 1
    refs = result.summaries[0]["stage1_input"]["selected_visual_refs"]
    assert len(refs) == 3
    assert {item["artifact_type"] for item in refs} == {"figure_crop"}
    assert result.summaries[0]["stage1_input"]["visual_coverage"][
        "required_visual_unit_ids"
    ] == [item["visual_id"] for item in refs]


def test_thirty_page_paper_with_two_tables_selects_two_objects(tmp_path: Path) -> None:
    pdf_path = tmp_path / "thirty-page-two-tables.pdf"
    _write_text_pdf(pdf_path, table_pages=(7, 20))

    _service_instance, result, calls = _run_reader_calls(
        tmp_path,
        pdf_path,
        config_overrides={
            "Stage1_Visual": {
                "enabled": "true",
                "selection_mode": "selective",
                "render_all_nonblank_pages": "false",
            },
            "Stage1_Input": {"send_selected_visuals": "true"},
        },
    )

    assert len(calls) == 1
    refs = result.summaries[0]["stage1_input"]["selected_visual_refs"]
    assert len(refs) == 2
    assert {item["artifact_type"] for item in refs} == {"table_crop"}
    assert result.summaries[0]["stage1_input"]["visual_coverage"][
        "visual_extraction_strategy"
    ] == "direct_synthesis_visuals"


def test_render_all_nonblank_pages_false_is_a_supported_selective_setting(tmp_path: Path) -> None:
    pdf_path = tmp_path / "explicit-selective.pdf"
    _write_text_pdf(pdf_path, page_count=2)

    _service_instance, result, calls = _run_reader_calls(
        tmp_path,
        pdf_path,
        config_overrides={
            "Stage1_Visual": {
                "enabled": "true",
                "selection_mode": "selective",
                "render_all_nonblank_pages": "false",
            },
            "Stage1_Input": {"send_selected_visuals": "true"},
        },
    )

    assert len(calls) == 1
    assert result.summaries[0]["stage1_input"]["visual_coverage"][
        "selection_mode"
    ] == "selective"


def test_selected_visual_transport_limit_creates_object_batches_not_page_batches(tmp_path: Path) -> None:
    refs = []
    for index in range(4):
        image_path = tmp_path / f"object-{index}.png"
        image_path.write_bytes(b"x" * 100)
        refs.append(
            {
                "visual_id": f"table-{index + 1:03d}",
                "page_no": index + 1,
                "bbox": [0, 0, 100, 100],
                "artifact_type": "table_crop",
                "image_path": str(image_path),
                "image_bytes": 100,
                "image_sha256": "a" * 64,
                "selection_required": True,
                "selection_score": 1.0,
            }
        )

    built = Stage1InputBuilder().build(
        prompt_template="{{PAPER_FULL_TEXT}}\n{{VISUAL_COVERAGE_JSON}}\n{{VISUAL_OBSERVATIONS_JSON}}",
        paper_text="The normalized MinerU text is the primary evidence.",
        reader_api_config={
            "model": "deepseek-v4-flash-vision-exp",
            "api_base": "https://api.deepseek.com",
            "provider_family": "deepseek",
        },
        visual_bundle={
            "selected_visual_refs": refs,
            "selection_policy_snapshot": {
                "selection_mode": "selective",
                "selection_contract_version": "stage1_visual_selection/v1",
            },
            "coverage_report": {
                "selection_mode": "selective",
                "selection_contract_version": "stage1_visual_selection/v1",
                "required_visual_unit_count": 4,
                "required_visual_unit_ids": [item["visual_id"] for item in refs],
                "optional_visual_unit_ids": [],
                "selected_visual_unit_ids": [item["visual_id"] for item in refs],
                "visual_extraction_strategy": "direct_synthesis_visuals",
                "visual_selection_status": "complete",
                "evidence_coverage_status": "incomplete",
                "raw_reinspection_units": [],
                "required_raw_reinspection_unit_count": 0,
                "closed_raw_reinspection_unit_count": 0,
                "unresolved_raw_reinspection_unit_ids": [],
            },
        },
        stage1_input_settings={
            "send_extracted_text": "true",
            "send_selected_visuals": "true",
            "max_request_image_bytes": "1000",
            "max_single_image_bytes": "1000",
            "visual_scan_batch_size": "8",
        },
    )

    assert built.visual_coverage["visual_extraction_strategy"] == "selected_visual_batches"
    assert built.selected_visual_refs == []
    assert len(built.visual_scan_batches) == 2
    assert all(
        built.visual_coverage["planned_scan_batches"][index]["extraction_mode"]
        == "visual_extract"
        for index in range(2)
    )
    assert all(
        all(item["artifact_type"] == "table_crop" for item in batch)
        for batch in built.visual_scan_batches
    )


def test_selected_visual_batches_use_new_evidence_contract_and_one_synthesis(tmp_path: Path) -> None:
    pdf_path = tmp_path / "batched-figures.pdf"
    _write_figure_pdf(pdf_path, figure_count=4)
    calls: list[dict[str, Any]] = []

    def reader(**kwargs: Any) -> dict[str, Any]:
        calls.append(dict(kwargs))
        if kwargs.get("purpose") == "visual_extract":
            batch = kwargs["visual_scan_batch"]
            return {
                "status": "success",
                "content": {
                    "artifact_type": "stage1_visual_evidence",
                    "artifact_version": "v3",
                    "observations": [
                        {
                            "visual_id": item["visual_id"],
                            "page_no": item["page_no"],
                            "bbox": item["bbox"],
                            "artifact_type": item["artifact_type"],
                            "visible_text": ["visible"],
                            "title_or_caption": "figure",
                            "axes_or_headers": [],
                            "legend_or_notes": [],
                            "quantitative_values": [],
                            "relationships": ["treatment -> outcome"],
                            "layout_observations": [],
                            "ocr_conflicts": [],
                            "confidence": "high",
                            "needs_manual_review": False,
                            "evidence_kinds": ["relationships"],
                        }
                        for item in batch["visual_refs"]
                    ],
                },
            }
        return {"status": "success", "content": _canonical_summary()}

    service, bundle = _service(
        tmp_path,
        pdf_path,
        reader,
        config_overrides={
            "Stage1_Visual": {
                "enabled": "true",
                "selection_mode": "selective",
                "render_all_nonblank_pages": "false",
            },
            "Stage1_Input": {
                "send_selected_visuals": "true",
                "max_request_image_bytes": "30000",
                "max_single_image_bytes": "24000000",
            },
            "Primary_Reader_API": {
                "model": "deepseek-v4-flash-vision-exp",
                "api_base": "https://api.deepseek.com",
                "provider_family": "deepseek",
            },
        },
    )
    result = service.run(bundle)

    assert [call.get("purpose") for call in calls] == [
        "visual_extract",
        "visual_extract",
        "visual_extract",
        "visual_extract",
        None,
    ]
    assert len(service.receipt_ledger.list_receipts()) == 5
    assert all(
        record.artifact_type == "stage1_visual_evidence"
        and record.artifact_version == "v3"
        for record in service.registry.list_records()
        if record.artifact_type == "stage1_visual_evidence"
    )
    assert [call.call_id for call in service.expected_calls] == [
        "stage1_visual_extract:evidencebound study_author:0",
        "stage1_visual_extract:evidencebound study_author:1",
        "stage1_visual_extract:evidencebound study_author:2",
        "stage1_visual_extract:evidencebound study_author:3",
        "stage1_synthesis:evidencebound study_author",
    ]
    coverage = result.summaries[0]["stage1_input"]["visual_coverage"]
    assert coverage["visual_extraction_strategy"] == "selected_visual_batches"
    assert coverage["evidence_coverage_status"] == "complete"
    assert coverage["unresolved_visual_unit_ids"] == []


def test_selected_visual_schema_drift_fails_closed_without_semantic_retry(tmp_path: Path) -> None:
    pdf_path = tmp_path / "schema-drift-figures.pdf"
    _write_figure_pdf(pdf_path, figure_count=2)
    extract_calls: list[str] = []

    def reader(**kwargs: Any) -> dict[str, Any]:
        if kwargs.get("purpose") == "visual_extract":
            batch = kwargs["visual_scan_batch"]
            extract_calls.append(batch["visual_ids"][0])
            return {
                "status": "success",
                "content": {
                    "artifact_type": "stage1_visual_evidence",
                    "artifact_version": "v3",
                    "observations": [
                        {
                            "visual_id": item["visual_id"],
                            "page_no": item["page_no"],
                            "bbox": item["bbox"],
                            "artifact_type": item["artifact_type"],
                            "visible_text": [],
                            "title_or_caption": None,
                            "axes_or_headers": [],
                            "legend_or_notes": [],
                            "quantitative_values": [],
                            "relationships": [],
                            "layout_observations": [],
                            "ocr_conflicts": [],
                            "confidence": "low",
                            "needs_manual_review": True,
                            # Deliberate prompt/schema drift: IDs are not
                            # evidence kinds and must not trigger a retry.
                            "evidence_kinds": [item["visual_id"]],
                        }
                        for item in batch["visual_refs"]
                    ],
                },
            }
        return {"status": "success", "content": _canonical_summary()}

    service, bundle = _service(
        tmp_path,
        pdf_path,
        reader,
        config_overrides={
            "Stage1_Visual": {
                "enabled": "true",
                "selection_mode": "selective",
                "render_all_nonblank_pages": "false",
            },
            "Stage1_Input": {
                "send_selected_visuals": "true",
                "max_request_image_bytes": "30000",
                "max_single_image_bytes": "24000000",
                "stage1_semantic_retry_max_attempts": "1",
            },
            "Primary_Reader_API": {
                "model": "deepseek-v4-flash-vision-exp",
                "api_base": "https://api.deepseek.com",
                "provider_family": "deepseek",
            },
        },
    )
    result = service.run(bundle)

    assert len(extract_calls) == 2
    coverage = result.summaries[0]["stage1_input"]["visual_coverage"]
    assert coverage["evidence_coverage_status"] == "incomplete"


def test_legacy_bundle_with_only_all_visual_refs_fails_closed(tmp_path: Path) -> None:
    """A legacy all-page-like bundle must never become the selective authority."""
    image_path = tmp_path / "legacy-page.jpg"
    image_path.write_bytes(b"x" * 100)
    legacy_refs = [
        {
            "visual_id": "page-001",
            "page_no": 1,
            "bbox": [0, 0, 100, 100],
            "artifact_type": "page_snapshot",
            "image_path": str(image_path),
            "image_bytes": 100,
        }
        for index in range(2)
    ]

    with pytest.raises(ValueError, match="legacy all_visual_refs"):
        Stage1InputBuilder().build(
            prompt_template="{{PAPER_FULL_TEXT}}\n{{VISUAL_COVERAGE_JSON}}",
            paper_text="The normalized MinerU text is the primary evidence.",
            reader_api_config={
                "model": "deepseek-v4-flash-vision-exp",
                "api_base": "https://api.deepseek.com",
                "provider_family": "deepseek",
            },
            # Legacy bundles only carry all_visual_refs with no explicit
            # selection field or current selection contract.
            visual_bundle={"all_visual_refs": legacy_refs},
            stage1_input_settings={"send_selected_visuals": "true"},
        )
