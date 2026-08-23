from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import fitz  # type: ignore
import pytest

from tests.test_current_stage1_generation import _canonical_summary, _service


def _write_long_text_pdf(path: Path, *, page_count: int = 13) -> None:
    document = fitz.open()
    for page_no in range(page_count):
        page = document.new_page()
        page.insert_text(
            (72, 72),
            f"Page {page_no + 1} Figure {page_no + 1} evidence\n"
            "Results: treatment improved the outcome by 17.3 percent.",
        )
    document.save(path)
    document.close()


def _long_visual_config(*, require_complete: bool = True) -> dict[str, dict[str, str]]:
    return {
        "Stage1_Visual": {"enabled": "true"},
        "Stage1_Input": {
            "send_selected_visuals": "true",
            "single_call_max_pages": "12",
            "visual_scan_batch_size": "4",
            "require_complete_visual_coverage": "true" if require_complete else "false",
        },
        "Primary_Reader_API": {
            "model": "deepseek-v4-flash-vision-exp",
            "api_base": "https://api.deepseek.com",
            "provider_family": "deepseek",
        },
    }


def _long_scan_content(batch: dict[str, Any], *, omit_last: bool = False) -> dict[str, Any]:
    pairs = list(zip(batch["visual_ids"], batch["page_nos"]))
    if omit_last:
        pairs = pairs[:-1]
    return {
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
                "quantitative_values": ["17.3%"],
                "relationships": ["treatment -> outcome"],
                "layout_observations": [],
                "ocr_conflicts": ["OCR says 13.7%"],
                "confidence": "high",
                "needs_manual_review": False,
                "candidate_attribution_status": "no_matching_candidate",
                "raw_reinspection_candidates": [],
            }
            for visual_id, page_no in pairs
        ],
    }


def _complete_long_run(
    tmp_path: Path,
    *,
    require_complete: bool = True,
    engine_type: str = "primary",
) -> tuple[Any, Any, Any, list[str]]:
    pdf_path = tmp_path / "qualification-long.pdf"
    _write_long_text_pdf(pdf_path)
    calls: list[str] = []

    def reader(**kwargs: Any) -> dict[str, Any]:
        if kwargs.get("purpose") == "visual_scan":
            calls.append("visual_scan")
            return {
                "status": "success",
                "content": _long_scan_content(kwargs["visual_scan_batch"]),
            }
        calls.append("synthesis")
        result: dict[str, Any] = {
            "status": "success",
            "content": _canonical_summary(),
        }
        if engine_type == "backup":
            result.update({"engine_type": "backup", "fallback_reason": "quota"})
        return result

    service, bundle = _service(
        tmp_path,
        pdf_path,
        reader,
        config_overrides=_long_visual_config(require_complete=require_complete),
    )
    first = service.run(bundle)
    return service, bundle, first, calls


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
                            "quantitative_values": ["17.3%"],
                            "relationships": ["X positively predicts Y"],
                            "layout_observations": [],
                            "ocr_conflicts": ["OCR says 13.7%, visual says 17.3%"],
                            "confidence": "high",
                            "needs_manual_review": False,
                            "candidate_attribution_status": "no_matching_candidate",
                            "raw_reinspection_candidates": [],
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
                            "quantitative_values": ["17.3%"],
                            "relationships": ["X positively predicts Y"],
                            "layout_observations": [],
                            "ocr_conflicts": ["OCR says 13.7%, visual says 17.3%"],
                            "confidence": "high",
                            "needs_manual_review": False,
                            "candidate_attribution_status": "no_matching_candidate",
                            "raw_reinspection_candidates": [],
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


def test_long_paper_final_synthesis_receives_attributed_child_crop(tmp_path: Path) -> None:
    pdf_path = tmp_path / "long-with-crop.pdf"
    document = fitz.open()
    for page_no in range(15):
        page = document.new_page()
        page.insert_text(
            (72, 72),
            f"Page {page_no + 1} Figure {page_no + 1} framework evidence\n"
            "The diagram reports a quantitative relationship.",
        )
        if page_no == 6:
            pixmap = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 240, 160), False)
            pixmap.clear_with(150)
            page.insert_image(fitz.Rect(72, 110, 312, 270), pixmap=pixmap)
    document.save(pdf_path)
    document.close()
    synthesis_contents: list[object] = []

    def reader(**kwargs):
        if kwargs.get("purpose") == "visual_scan":
            batch = kwargs["visual_scan_batch"]
            child_candidates = [
                dict(item)
                for item in (batch.get("child_candidates") or [])
                if isinstance(item, dict)
            ]
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
                            "visible_text": ["framework", "diagram"],
                            "title_or_caption": "framework diagram",
                            "axes_or_headers": ["Outcome"],
                            "legend_or_notes": [],
                            "quantitative_values": ["17.3%"],
                            "relationships": ["mechanism -> outcome"],
                            "layout_observations": [],
                            "ocr_conflicts": ["OCR says 13.7%, visual says 17.3%"],
                            "confidence": "high",
                            "needs_manual_review": False,
                            "candidate_attribution_status": (
                                "resolved"
                                if page_no == 7 and any(
                                    int(item.get("page_no") or 0) == page_no
                                    for item in child_candidates
                                )
                                else "no_matching_candidate"
                            ),
                            "raw_reinspection_candidates": [
                                {
                                    "candidate_visual_id": str(candidate["candidate_visual_id"]),
                                    "evidence_kinds": ["relationships", "quantitative_values"],
                                    "reason": "the page observation identifies the visible framework crop",
                                    "confidence": "high",
                                    "requires_raw_reinspection": True,
                                }
                                for candidate in child_candidates
                                if page_no == 7
                                and int(candidate.get("page_no") or 0) == page_no
                                and str(candidate.get("artifact_type") or "") == "figure_crop"
                            ][:1],
                        }
                        for visual_id, page_no in zip(batch["visual_ids"], batch["page_nos"])
                    ],
                },
            }
        synthesis_contents.append(kwargs.get("user_content"))
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
                "final_image_refs_max": "16",
            },
            "Primary_Reader_API": {
                "model": "deepseek-v4-flash-vision-exp",
                "api_base": "https://api.deepseek.com",
                "provider_family": "deepseek",
            },
        },
    )
    result = service.run(bundle)

    assert len(synthesis_contents) == 1
    final_content = synthesis_contents[0]
    assert isinstance(final_content, list)
    child_items = [
        item
        for item in final_content
        if isinstance(item, dict)
        and item.get("type") == "local_image_path"
        and item.get("artifact_type") == "figure_crop"
    ]
    assert child_items, "the final request must contain a page-attributed child crop"
    child = child_items[0]
    selected = result.summaries[0]["stage1_input"]["selected_visual_refs"]
    selected_child = next(item for item in selected if item["visual_id"] == child["visual_id"])
    assert selected_child["source_page_visual_id"] == "page-007"
    assert selected_child["source_observation_visual_id"] == "page-007"
    assert child["path"] == selected_child["image_path"]


def test_long_paper_final_request_keeps_ambiguous_children_atomic(tmp_path: Path) -> None:
    pdf_path = tmp_path / "ambiguous-atomic-long.pdf"
    document = fitz.open()
    for page_no in range(13):
        page = document.new_page()
        page.insert_text(
            (72, 72),
            f"Page {page_no + 1} framework evidence\n"
            "Results: treatment improved the outcome by 17.3 percent.",
        )
    document.save(pdf_path)
    document.close()
    synthesis_contents: list[object] = []

    def reader(**kwargs: Any) -> dict[str, Any]:
        if kwargs.get("purpose") == "visual_scan":
            batch = kwargs["visual_scan_batch"]
            observations = _long_scan_content(batch)["observations"]
            for observation in observations:
                if int(observation.get("page_no") or 0) != 7:
                    continue
                candidates = [
                    dict(item)
                    for item in (batch.get("child_candidates") or [])
                    if isinstance(item, dict)
                    and int(item.get("page_no") or 0) == 7
                ]
                observation["candidate_attribution_status"] = "ambiguous"
                observation["raw_reinspection_candidates"] = [
                    {
                        "candidate_visual_id": str(item["candidate_visual_id"]),
                        "evidence_kinds": ["visible_text"],
                        "reason": "page resolution cannot distinguish the two overlapping objects",
                        "confidence": "low",
                        "requires_raw_reinspection": True,
                    }
                    for item in candidates
                ]
            return {
                "status": "success",
                "content": {
                    "artifact_type": "stage1_visual_observations",
                    "artifact_version": "v2",
                    "observations": observations,
                },
            }
        synthesis_contents.append(kwargs.get("user_content"))
        return {"status": "success", "content": _canonical_summary()}

    service, bundle = _service(
        tmp_path,
        pdf_path,
        reader,
        config_overrides=_long_visual_config(),
    )
    original_build_visual_bundle = service._build_visual_bundle

    def build_bundle_with_ambiguous_pair(item: Any, preprocess_metadata: dict[str, Any]) -> dict[str, Any]:
        visual_bundle = original_build_visual_bundle(item, preprocess_metadata)
        refs = [
            dict(ref)
            for ref in (visual_bundle.get("all_visual_refs") or [])
            if isinstance(ref, dict)
        ]
        source = next(
            ref
            for ref in refs
            if int(ref.get("page_no") or 0) == 7
            and str(ref.get("artifact_type") or "") == "page_snapshot"
        )
        child_a = {
            **source,
            "visual_id": "figure-007-ambiguous-a",
            "artifact_type": "figure_crop",
            "bbox": [0, 0, 100, 100],
            "selection_score": 1.0,
            "dedupe_group_id": "ambiguous-pair",
        }
        child_b = {
            **child_a,
            "visual_id": "figure-007-ambiguous-b",
            "bbox": [5, 5, 95, 95],
            "selection_score": 0.9,
        }
        refs.extend([child_a, child_b])
        visual_bundle["all_visual_refs"] = refs
        visual_bundle["selected_visual_refs"] = refs
        return visual_bundle

    service._build_visual_bundle = build_bundle_with_ambiguous_pair  # type: ignore[method-assign]
    result = service.run(bundle)

    assert len(synthesis_contents) == 1
    final_content = synthesis_contents[0]
    assert isinstance(final_content, list)
    final_image_ids = {
        str(item.get("visual_id") or "")
        for item in final_content
        if isinstance(item, dict) and item.get("type") == "local_image_path"
    }
    assert {"figure-007-ambiguous-a", "figure-007-ambiguous-b"}.issubset(final_image_ids)
    assert "page-007" not in final_image_ids
    coverage = result.summaries[0]["stage1_input"]["visual_coverage"]
    group = next(
        item
        for item in coverage["raw_reinspection_groups"]
        if item["page_no"] == 7
    )
    assert group["raw_reinspection_resolution"] == "all_children"
    assert group["ambiguous_candidate_ids"] == [
        "figure-007-ambiguous-a",
        "figure-007-ambiguous-b",
    ]
    assert group["raw_reinspection_selected_ids"] == [
        "figure-007-ambiguous-a",
        "figure-007-ambiguous-b",
    ]
    assert group["child_reinspection_complete"] is True


def test_current_stage1_rejects_v1_visual_provider_output(tmp_path: Path) -> None:
    pdf_path = tmp_path / "legacy-visual-output-long.pdf"
    _write_long_text_pdf(pdf_path)

    def reader(**kwargs: Any) -> dict[str, Any]:
        if kwargs.get("purpose") == "visual_scan":
            legacy = _long_scan_content(kwargs["visual_scan_batch"])
            legacy["artifact_version"] = "v1"
            for observation in legacy["observations"]:
                observation.pop("candidate_attribution_status", None)
                observation.pop("raw_reinspection_candidates", None)
            return {"status": "success", "content": legacy}
        return {"status": "success", "content": _canonical_summary()}

    service, bundle = _service(
        tmp_path,
        pdf_path,
        reader,
        config_overrides=_long_visual_config(),
    )
    result = service.run(bundle)
    summary = result.summaries[0]
    coverage = summary["stage1_input"]["visual_coverage"]

    assert coverage["scan_coverage_status"] in {"failed", "partial"}
    assert coverage["evidence_coverage_status"] == "incomplete"
    assert coverage["observed_visual_ids"] == []
    assert all(
        str(item.get("status") or "") == "scan_failed"
        for item in coverage["scan_batches"]
    )


def test_partial_long_scan_blocks_exact_reuse_and_requires_new_provider_work(tmp_path: Path) -> None:
    pdf_path = tmp_path / "partial-long.pdf"
    _write_long_text_pdf(pdf_path)
    calls: list[str] = []

    def reader(**kwargs: Any) -> dict[str, Any]:
        if kwargs.get("purpose") == "visual_scan":
            calls.append("visual_scan")
            return {
                "status": "success",
                "content": _long_scan_content(kwargs["visual_scan_batch"], omit_last=True),
            }
        calls.append("synthesis")
        return {"status": "success", "content": _canonical_summary()}

    service, bundle = _service(
        tmp_path,
        pdf_path,
        reader,
        config_overrides=_long_visual_config(),
    )
    first = service.run(bundle)
    first_call_count = len(calls)
    coverage = first.summaries[0]["stage1_input"]["visual_coverage"]
    assert coverage["scan_coverage_status"] in {"partial", "failed"}
    assert coverage["evidence_coverage_status"] == "incomplete"
    assert "visual_coverage_incomplete" in first.summaries[0]["ai_summary"]["quality_audit"]["conflict_flags"]

    second = service.run(bundle, existing_summaries=first.summaries)

    assert second.reused_count == 0
    assert second.generated_count == 1
    assert len(calls) > first_call_count
    assert second.summaries[0]["stage1_reuse"]["decision"] != "exact_summary_reuse"


@pytest.mark.parametrize("tampered_artifact", ["coverage", "observation"])
def test_tampered_visual_qualification_artifact_blocks_exact_reuse(
    tmp_path: Path,
    tampered_artifact: str,
) -> None:
    service, bundle, first, calls = _complete_long_run(tmp_path)
    qualification = first.summaries[0]["stage1_reuse"]["binding"]["visual_evidence_qualification"]
    target_key = (
        "coverage_artifact_path"
        if tampered_artifact == "coverage"
        else "observation_artifact_paths"
    )
    target_value = qualification[target_key]
    target_path = Path(target_value if isinstance(target_value, str) else target_value[0])
    assert target_path.is_file()
    target_path.write_bytes(b"tampered visual evidence artifact")
    first_call_count = len(calls)

    second = service.run(bundle, existing_summaries=first.summaries)

    assert second.reused_count == 0
    assert second.generated_count == 1
    assert len(calls) > first_call_count
    assert second.summaries[0]["stage1_reuse"]["decision"] != "exact_summary_reuse"


def test_explicit_degraded_visual_policy_allows_reuse_with_status_preserved(tmp_path: Path) -> None:
    service, bundle, first, calls = _complete_long_run(
        tmp_path,
        require_complete=False,
        engine_type="backup",
    )
    summary = first.summaries[0]
    qualification = summary["stage1_reuse"]["binding"]["visual_evidence_qualification"]
    assert qualification["require_complete_visual_coverage"] is False
    assert qualification["scan_coverage_status"] == "complete"
    assert qualification["final_synthesis_modality"] == "text_only"
    assert qualification["final_raw_visual_recheck_status"] == "not_run_fallback"
    assert qualification["evidence_coverage_status"] == "degraded"
    first_call_count = len(calls)

    second = service.run(bundle, existing_summaries=first.summaries)

    assert second.reused_count == 1
    assert second.generated_count == 0
    assert len(calls) == first_call_count
    assert second.actual_provider_transport_count == 0
    assert second.summaries[0]["stage1_reuse"]["decision"] == "exact_summary_reuse"
