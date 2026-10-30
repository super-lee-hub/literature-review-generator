from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from services.job_workspace import JobWorkspace
from services.review_generation_service import ReviewGenerationService
from services.settings import ApplicationSettings
from tests.test_writer_source_inventory import JOB_ID, PLANNED_CLAIM, SOURCE_SUMMARY, _registry_with_inventory


@pytest.mark.parametrize("outcome", ["complete", "missing_qualifier", "needs_review"])
def test_writer_joins_canonical_sources_before_dispatch_and_preserves_task_scope(
    tmp_path: Path, outcome: str,
) -> None:
    workspace = JobWorkspace.create(str(tmp_path / "output"), "source-scope", JOB_ID)
    registry, _inventory, packet, _layer, _views = _registry_with_inventory(Path(workspace.root_dir))
    if outcome == "missing_qualifier":
        for name in ("source_claim_ids", "evidence_ids", "source_field_ids"):
            packet["claim_support"][0][name] = packet["claim_support"][0][name][:1]
    calls: list[dict[str, Any]] = []

    def writer(**kwargs: Any):
        payload = json.loads(kwargs["prompt_text"])
        scope = payload["writer_task_scope"]
        assert scope["usable_for_provider_admission"] is True
        calls.append(scope)
        if outcome == "needs_review":
            return {"status": "success", "content": {
                "blocks": [], "task_dispositions": [{
                    "writer_task_id": task["writer_task_id"],
                    "writer_task_basis_hash": scope["writer_task_basis_hash"],
                    "disposition": "needs_review", "reason_code": "source_uncertainty",
                } for task in scope["tasks"]],
            }, "usage_status": "provider_not_supported"}
        blocks = []
        for task in scope["tasks"]:
            unit = next(item for item in task["output_units"] if item["required"])
            blocks.append({
                "writer_task_id": task["writer_task_id"],
                "writer_output_unit_id": unit["writer_output_unit_id"],
                "writer_task_basis_hash": scope["writer_task_basis_hash"],
                "text": f"{PLANNED_CLAIM} [[cite_ref:{unit['allowed_ref_ids'][0]}]]",
            })
        return {"status": "success", "content": {
            "blocks": blocks,
            "task_dispositions": [{
                "writer_task_id": task["writer_task_id"],
                "writer_task_basis_hash": scope["writer_task_basis_hash"],
                "disposition": "covered",
            } for task in scope["tasks"]],
        }, "usage_status": "provider_not_supported"}

    service = ReviewGenerationService(
        job_id=JOB_ID, attempt_id="source-scope-attempt", workspace=workspace,
        artifact_registry=registry, settings=ApplicationSettings.from_config({
            "Writer_API": {"api_key": "synthetic", "model": "writer", "api_base": "https://writer.test/v1"},
        }), summaries=[deepcopy(SOURCE_SUMMARY)], writer=writer,
    )
    outline = {"title": "Bounded source review", "sections": [{"section_id": packet["section_id"], "title": "Evidence"}]}
    if outcome == "missing_qualifier":
        with pytest.raises(RuntimeError, match="requires review"):
            service.run(outline_payload=outline, evidence_packets=[packet])
        assert calls == []
        return
    if outcome == "needs_review":
        with pytest.raises(RuntimeError, match="disposition saved"):
            service.run(outline_payload=outline, evidence_packets=[packet])
        records = [item for item in registry.list_records() if item.artifact_type == "review_writer_review_disposition"]
        assert len(records) == 1 and records[0].status == "quarantined"
        payload = json.loads(Path(records[0].path).read_text(encoding="utf-8"))
        assert payload["canonical_ready"] is False
        assert payload["validated_output"]["task_dispositions"][0]["disposition"] == "needs_review"
        assert payload["provider_receipt_ids"]
        assert registry.get(f"review-section:{packet['section_id']}") is None
        return
    result = service.run(outline_payload=outline, evidence_packets=[packet])
    assert len(calls) == 1
    section = result.sections[0]
    assert section["writer_task_scope"]["source_inventory_binding"]["artifact_id"] == "outline-v3:outline_content_layers"
    assert section["writer_task_dispositions"][0]["disposition"] == "covered"
    assert section["blocks"][0]["writer_task_basis_hash"] == calls[0]["writer_task_basis_hash"]
