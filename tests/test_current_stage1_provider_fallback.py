from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping
from unittest.mock import Mock
import json

import fitz  # type: ignore
import pytest

import ai_interface
from runtime.provider_runtime import ProviderRuntimeLedger
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


def test_stage1_length_retry_escalates_same_primary_budget_before_backup(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    pdf_path = tmp_path / "length-retry-paper.pdf"
    _write_pdf(pdf_path)
    first_response = Mock()
    first_response.status_code = 200
    first_response.raise_for_status.return_value = None
    first_response.json.return_value = {
        "choices": [
            {
                "message": {"content": "{\"partial\": true}"},
                "finish_reason": "length",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 12000, "total_tokens": 12010},
    }
    second_response = Mock()
    second_response.status_code = 200
    second_response.raise_for_status.return_value = None
    second_response.json.return_value = {
        "choices": [
            {
                "message": {"content": json.dumps(_canonical_summary())},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 80, "total_tokens": 90},
    }
    requests = [first_response, second_response]
    monkeypatch.setattr("ai_interface.requests.post", Mock(side_effect=requests))

    service, bundle = _service(
        tmp_path,
        pdf_path,
        reader=None,
        config_overrides={
            "Stage1_Input": {
                "stage1_synthesis_max_output_tokens": "12000",
                "stage1_length_retry_max_attempts": "1",
                "stage1_length_retry_ceiling_tokens": "24000",
                "stage1_request_timeout_seconds": "240",
            }
        },
    )
    result = service.run(bundle)

    summary = result.summaries[0]
    assert result.generated_count == 1
    assert summary["provider"]["requested_output_budgets"] == [12000, 24000]
    assert summary["provider"]["length_retries"] == 1
    assert summary["provider"]["terminal_output_tokens"] == 24000
    assert summary["provider"]["request_timeout_seconds"] == 240
    ledger = ProviderRuntimeLedger(result.receipt_ledger_path)
    receipts = ledger.list_receipts()
    assert len(receipts) == 2
    assert [receipt.metadata["requested_output_tokens"] for receipt in receipts] == [12000, 24000]
    assert all(receipt.route == "Primary_Reader_API" for receipt in receipts)


def test_stage1_length_budget_exhaustion_does_not_fallback_to_backup(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    pdf_path = tmp_path / "length-exhausted-paper.pdf"
    _write_pdf(pdf_path)
    engines: list[str] = []

    def always_length(
        prompt_text: str,
        primary_api_config: Mapping[str, Any],
        backup_api_config: Mapping[str, Any],
        *,
        engine_type: str = "primary",
        **kwargs: Any,
    ) -> Mapping[str, Any]:
        engines.append(engine_type)
        return {
            "status": "failed",
            "error_kind": "invalid_response",
            "finish_reason": "length",
            "message": "provider output reached max_tokens",
        }

    monkeypatch.setattr("ai_interface.get_summary_from_ai_detailed", always_length)
    service, bundle = _service(
        tmp_path,
        pdf_path,
        reader=None,
        config_overrides={
            "Stage1_Input": {
                "stage1_synthesis_max_output_tokens": "12000",
                "stage1_length_retry_max_attempts": "1",
                "stage1_length_retry_ceiling_tokens": "24000",
            }
        },
    )

    with pytest.raises(RuntimeError):
        service.run(bundle)

    assert engines == ["primary", "primary"]


def test_stage1_visual_length_retry_is_recorded_before_scan_can_close(
    tmp_path: Path,
) -> None:
    pdf_path = tmp_path / "visual-length-retry-paper.pdf"
    document = fitz.open()
    for page_number in range(1, 14):
        page = document.new_page()
        page.insert_text((72, 72), f"Page {page_number}. Figure and results.")
    document.save(pdf_path)
    document.close()
    scan_calls: list[tuple[int, int]] = []

    def reader(**kwargs: Any) -> Mapping[str, Any]:
        if kwargs.get("purpose") == "visual_scan":
            batch = kwargs["visual_scan_batch"]
            scan_calls.append(
                (
                    int(batch["batch_index"]),
                    int(kwargs["primary_api_config"]["max_output_tokens"]),
                )
            )
            if len(scan_calls) == 1:
                return {
                    "status": "failed",
                    "error_kind": "invalid_response",
                    "finish_reason": "length",
                    "message": "structured visual response was truncated",
                }
            return {
                "status": "success",
                "content": {
                    "artifact_type": "stage1_visual_observations",
                    "artifact_version": "v2",
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
                            "candidate_attribution_status": "no_matching_candidate",
                            "raw_reinspection_candidates": [],
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
            **_visual_config_overrides(),
            "Stage1_Visual": {
                **_visual_config_overrides()["Stage1_Visual"],
                "selection_mode": "adaptive_page_scan",
                "render_all_nonblank_pages": "true",
            },
            "Stage1_Input": {
                **_visual_config_overrides()["Stage1_Input"],
                "single_call_max_pages": "12",
                "stage1_synthesis_max_output_tokens": "12000",
                "stage1_length_retry_max_attempts": "1",
                "stage1_length_retry_ceiling_tokens": "24000",
            },
        },
    )
    result = service.run(bundle)

    assert result.generated_count == 1
    assert scan_calls[0][1] == 16000
    assert scan_calls[1][1] == 24000
    scan_batches = result.summaries[0]["stage1_input"]["visual_coverage"]["scan_batches"]
    assert scan_batches[0]["length_retries"] == 1
    assert scan_batches[0]["requested_output_budgets"] == [16000, 24000]


def _semantic_retry_visual_content(
    batch: Mapping[str, Any],
    *,
    invalid: bool,
) -> dict[str, Any]:
    observations: list[dict[str, Any]] = []
    candidates = list(batch.get("child_candidates") or [])
    for visual_id, page_no in zip(batch["visual_ids"], batch["page_nos"]):
        raw_candidates: list[dict[str, Any]] = []
        attribution_status = "no_matching_candidate"
        if invalid and candidates:
            candidate = candidates[0]
            candidate_id = str(candidate.get("visual_id") or "")
            raw_candidates = [
                {
                    "candidate_visual_id": candidate_id,
                    "evidence_kinds": ["relationships"],
                    "reason": "The page metadata identifies the candidate.",
                    "confidence": "certain" if invalid else "high",
                    "requires_raw_reinspection": True,
                }
            ]
            attribution_status = "resolved"
        elif invalid:
            attribution_status = "invalid"
        observations.append(
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
                "candidate_attribution_status": attribution_status,
                "raw_reinspection_candidates": raw_candidates,
            }
        )
    return {
        "artifact_type": "stage1_visual_observations",
        "artifact_version": "v2",
        "observations": observations,
    }


def _write_semantic_retry_pdf(path: Path, *, page_count: int = 13) -> None:
    document = fitz.open()
    for page_number in range(1, page_count + 1):
        page = document.new_page()
        page.insert_text(
            (72, 72),
            f"Page {page_number}. Evidence-bound visual scan test.\n"
            "The treatment improved the outcome by 17.3 percent.",
        )
    document.save(str(path))
    document.close()


def test_stage1_semantic_retry_accepts_only_schema_valid_visual_observation(
    tmp_path: Path,
) -> None:
    pdf_path = tmp_path / "semantic-retry-paper.pdf"
    _write_semantic_retry_pdf(pdf_path)
    calls: list[tuple[int, int]] = []
    invalidated = False

    def reader(**kwargs: Any) -> Mapping[str, Any]:
        nonlocal invalidated
        if kwargs.get("purpose") == "visual_scan":
            batch = kwargs["visual_scan_batch"]
            candidates = list(batch.get("child_candidates") or [])
            is_invalid_attempt = not invalidated
            if is_invalid_attempt:
                invalidated = True
            calls.append((int(batch["batch_index"]), int(kwargs["primary_api_config"]["max_output_tokens"])))
            return {
                "status": "success",
                "finish_reason": "stop",
                "content": _semantic_retry_visual_content(
                    batch,
                    invalid=is_invalid_attempt,
                ),
            }
        return {"status": "success", "content": _canonical_summary()}

    service, bundle = _service(
        tmp_path,
        pdf_path,
        reader,
        config_overrides={
            **_visual_config_overrides(),
            "Stage1_Visual": {
                **_visual_config_overrides()["Stage1_Visual"],
                "selection_mode": "adaptive_page_scan",
                "render_all_nonblank_pages": "true",
            },
            "Stage1_Input": {
                **_visual_config_overrides()["Stage1_Input"],
                "single_call_max_pages": "12",
                "visual_scan_batch_size": "1",
                "stage1_semantic_retry_max_attempts": "1",
            },
        },
    )
    result = service.run(bundle)
    summary = result.summaries[0]
    coverage = summary["stage1_input"]["visual_coverage"]

    assert invalidated is True
    assert len(calls) == 14
    assert summary["provider"]["semantic_retries"] == 1
    assert coverage["evidence_coverage_status"] == "complete"
    assert coverage["failed_pages"] == 0
    receipts = ProviderRuntimeLedger(result.receipt_ledger_path).list_receipts()
    semantic_statuses = [receipt.metadata.get("semantic_validation_status") for receipt in receipts]
    assert "failed" in semantic_statuses
    assert "passed" in semantic_statuses
    assert max(int(receipt.metadata.get("semantic_retry_count") or 0) for receipt in receipts) == 1


def test_repeated_invalid_visual_observation_stays_incomplete_and_not_reusable(
    tmp_path: Path,
) -> None:
    pdf_path = tmp_path / "repeated-semantic-retry-paper.pdf"
    _write_semantic_retry_pdf(pdf_path)

    def reader(**kwargs: Any) -> Mapping[str, Any]:
        if kwargs.get("purpose") == "visual_scan":
            batch = kwargs["visual_scan_batch"]
            return {
                "status": "success",
                "finish_reason": "stop",
                "content": _semantic_retry_visual_content(batch, invalid=True),
            }
        return {"status": "success", "content": _canonical_summary()}

    service, bundle = _service(
        tmp_path,
        pdf_path,
        reader,
        config_overrides={
            **_visual_config_overrides(),
            "Stage1_Visual": {
                **_visual_config_overrides()["Stage1_Visual"],
                "selection_mode": "adaptive_page_scan",
                "render_all_nonblank_pages": "true",
            },
            "Stage1_Input": {
                **_visual_config_overrides()["Stage1_Input"],
                "single_call_max_pages": "12",
                "visual_scan_batch_size": "1",
                "stage1_semantic_retry_max_attempts": "1",
            },
        },
    )
    result = service.run(bundle)
    summary = result.summaries[0]
    coverage = summary["stage1_input"]["visual_coverage"]

    assert summary["provider"]["semantic_retries"] == 13
    assert coverage["evidence_coverage_status"] == "incomplete"
    assert coverage["failed_pages"] > 0
    assert summary["stage1_reuse"]["binding"]["visual_evidence_qualification"][
        "evidence_coverage_status"
    ] == "incomplete"


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
                    "artifact_version": "v2",
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
                            "candidate_attribution_status": "no_matching_candidate",
                            "raw_reinspection_candidates": [],
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
        config_overrides={
            **_visual_config_overrides(),
            "Stage1_Visual": {
                **_visual_config_overrides()["Stage1_Visual"],
                "selection_mode": "adaptive_page_scan",
                "render_all_nonblank_pages": "true",
            },
        },
    )
    result = service.run(bundle)
    summary = result.summaries[0]

    assert result.generated_count == 1
    assert summary["provider"]["route"] == "Backup_Reader_API"
    assert summary["provider"]["successful_engine"] == "backup"
    assert summary["provider"]["successful_input_mode"] == "text_only"
    assert summary["provider"]["images_actually_sent_count"] == 0
    assert summary["provider"]["scan_coverage_status"] == "complete"
    assert summary["provider"]["final_synthesis_modality"] == "text_only"
    assert summary["provider"]["final_raw_visual_recheck_status"] == "not_run_fallback"
    assert summary["provider"]["evidence_coverage_status"] == "degraded"
    # ``visual_coverage_status`` is the Registry v1 scan-domain alias;
    # final synthesis degradation is carried separately by
    # ``evidence_coverage_status``.
    assert summary["provider"]["visual_coverage_status"] == "complete"
    assert "final_raw_visual_recheck_missing" in summary["ai_summary"]["quality_audit"]["conflict_flags"]
    assert summary["ai_summary"]["quality_audit"]["needs_manual_review"] is True
