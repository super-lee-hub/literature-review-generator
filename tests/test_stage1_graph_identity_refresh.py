"""Red-first tests for expected-call-graph identity consistency after refresh.

The synthesis expected call is only known after visual scans produce their
observations. ``_refresh_synthesis_expected_call`` updates that identity and
the whole graph must be rebound (graph hash + closure epoch) so the published
graph JSON, every expected call and every receipt share one final identity.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import fitz  # type: ignore

from runtime.provider_runtime import hash_json
from tests.test_current_stage1_generation import _canonical_summary, _service


def _write_four_figure_pdf(path: Path) -> None:
    document = fitz.open()
    for figure_no in range(1, 5):
        page = document.new_page()
        page.insert_text(
            (72, 72),
            f"Figure {figure_no}. Research framework and treatment-to-outcome pathway. "
            "The surrounding paragraph explains the construct definitions, study design, and observed relationship.",
        )
        pixmap = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 320, 180), False)
        pixmap.clear_with(50 + figure_no * 40)
        page.insert_image(fitz.Rect(72, 110, 392, 290), pixmap=pixmap)
    document.save(path)
    document.close()


def _batched_reader(calls: list[dict[str, Any]]):
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

    return reader


def _published_graph_payload(service: Any) -> dict[str, Any]:
    path = Path(service.expected_call_graph_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def _recompute_graph_hash(payload: dict[str, Any]) -> str:
    expected_calls = payload.get("expected_calls") or []
    return hash_json(
        {
            "identity_version": "stage1_expected_call_graph/v2",
            "job_id": payload.get("job_id") or "",
            "stage_name": payload.get("stage_name") or "",
            "attempt_id": payload.get("attempt_id") or "",
            "source_bundle_hash": payload.get("source_bundle_hash") or "",
            "runtime_spec_hash": payload.get("runtime_spec_hash") or "",
            "call_shapes": [
                {
                    "call_id": item["call_id"],
                    "node_id": item["node_id"],
                    "prompt_id": item["prompt_id"],
                    "schema_hash": item["schema_hash"],
                    "artifact_path": item["artifact_path"],
                    "max_attempts": item["max_attempts"],
                    "request_variants": list(item.get("request_variants") or ()),
                }
                for item in expected_calls
            ],
        }
    )


def test_published_graph_hash_matches_recomputed_final_call_shapes(tmp_path: Path) -> None:
    """The refreshed graph JSON hash must be recomputed from its own call shapes."""
    pdf_path = tmp_path / "four-figures.pdf"
    _write_four_figure_pdf(pdf_path)
    calls: list[dict[str, Any]] = []

    service, bundle = _service(
        tmp_path,
        pdf_path,
        _batched_reader(calls),
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

    assert len(calls) == 5  # 4 visual extracts + 1 synthesis
    graph = _published_graph_payload(service)
    assert str(graph.get("expected_call_graph_hash") or "") == _recompute_graph_hash(graph)
    assert str(graph.get("expected_call_graph_hash") or "") == service.expected_call_graph_hash
    assert str(graph.get("closure_epoch_id") or "") == service.closure_epoch_id


def test_all_receipts_share_final_closure_epoch_after_refresh(tmp_path: Path) -> None:
    """Visual and synthesis receipts must land under the final closure epoch."""
    pdf_path = tmp_path / "four-figures-epoch.pdf"
    _write_four_figure_pdf(pdf_path)
    calls: list[dict[str, Any]] = []

    service, bundle = _service(
        tmp_path,
        pdf_path,
        _batched_reader(calls),
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

    graph = _published_graph_payload(service)
    final_epoch = str(graph.get("closure_epoch_id") or "")
    assert final_epoch
    receipts = service.receipt_ledger.list_receipts()
    assert len(receipts) == 5
    assert {str(receipt.closure_epoch_id or "") for receipt in receipts} == {final_epoch}
    assert set(result.receipt_ids) == {receipt.receipt_id for receipt in receipts}
    # The bound closure record must exist and be consistent with the graph.
    assert service.receipt_closure_path
    assert Path(service.receipt_closure_path).is_file()
