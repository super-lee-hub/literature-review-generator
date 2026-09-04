"""Red-first tests: selective crop render failures must be durable omissions.

A required crop that fails local rendering must stay visible in the coverage
contract (unresolved/omissions/materialization_failed_unit_ids) so Stage 1
cannot silently report complete or not_required coverage for evidence it
never received.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from preprocess.visual_artifacts import Stage1VisualArtifactBuilder
from tests.test_current_stage1_generation import _canonical_summary, _service, _write_visual_pdf


def _manifest_payload(service: Any) -> dict[str, Any]:
    manifest_record = next(
        record for record in service.registry.list_records() if record.artifact_type == "visual_manifest"
    )
    payload = json.loads(Path(manifest_record.path).read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def test_crop_render_failure_is_durable_unresolved_omission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pdf_path = tmp_path / "crop-failure.pdf"
    _write_visual_pdf(pdf_path)

    def fail_all_renders(self: Any, **kwargs: Any) -> bool:
        return False

    monkeypatch.setattr(
        Stage1VisualArtifactBuilder,
        "_render_pixmap_if_safe",
        fail_all_renders,
    )

    def reader(**kwargs: Any) -> dict[str, Any]:
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
            "Stage1_Input": {"send_selected_visuals": "true"},
            "Primary_Reader_API": {
                "model": "deepseek-v4-flash-vision-exp",
                "api_base": "https://api.deepseek.com",
                "provider_family": "deepseek",
            },
        },
    )
    result = service.run(bundle)
    assert result.generated_count == 1

    manifest = _manifest_payload(service)
    coverage = manifest["coverage_report"]
    # The figure pages were selected as required crops; rendering failed for
    # every one of them.  The coverage contract must expose that fact.
    failed_ids = coverage["materialization_failed_unit_ids"]
    assert failed_ids, "render failures must be recorded durably"
    assert all(item.startswith("figure-") for item in failed_ids)
    assert set(failed_ids).issubset(set(coverage["unresolved_visual_unit_ids"]))
    assert set(failed_ids).issubset(set(coverage["required_visual_unit_ids"]))
    assert coverage["visual_selection_status"] == "incomplete"
    assert coverage["evidence_coverage_status"] == "incomplete"
    failure_omissions = [
        omission
        for omission in coverage["omissions"]
        if omission.get("scope") == "selected_visual_extraction"
    ]
    assert {item["visual_id"] for item in failure_omissions} == set(failed_ids)
    assert all(item["authority_blocking"] is True for item in failure_omissions)
    assert all(
        item["reason"] == "render_failed_or_safety_limit" for item in failure_omissions
    )
