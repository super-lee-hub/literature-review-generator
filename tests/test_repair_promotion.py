from __future__ import annotations

import json
from pathlib import Path

from services.artifact_registry import ArtifactDependencyRefV2, ArtifactRegistry
from services.job_workspace import JobWorkspace, atomic_write_json
from validation.repair_transaction import RepairTransactionService


def _write(path: Path, payload: dict) -> Path:
    atomic_write_json(str(path), payload)
    return path


def test_repair_promotion_creates_versioned_outputs_without_replacing_canonical(
    tmp_path: Path,
) -> None:
    workspace = JobWorkspace.create(str(tmp_path), "repair", job_id="repair-promotion-job")
    registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    draft_payload = {
        "artifact_type": "review_draft",
        "artifact_version": "v3",
        "created_from_job_id": workspace.job_id,
        "created_at": "2026-01-01T00:00:00Z",
        "draft_identity": {"draft_id": "canonical-draft", "project_name": "repair"},
        "generation_context": {"section_count": 1},
        "content": {
            "sections": [
                {
                    "section_number": 1,
                    "section_title": "Evidence",
                    "title": "Evidence",
                    "blocks": [{"block_id": "b1", "text": "A grounded claim."}],
                }
            ],
            "references": [],
        },
        "projections": {},
    }
    manifest_payload = {
        "artifact_type": "citation_manifest",
        "artifact_version": "v3",
        "created_from_job_id": workspace.job_id,
        "created_at": "2026-01-01T00:00:00Z",
        "manifest_identity": {"manifest_id": "canonical-manifest", "project_name": "repair"},
        "review_reference": {
            "review_draft_path": "canonical.json",
            "review_word_path": "canonical.docx",
        },
        "paper_entries": [],
        "occurrences": [],
        "clusters": [],
        "citation_sets": [],
        "bibliography": [],
        "review_draft_version": "v3",
        "dependencies": {},
        "render_policy": {"citation_style": "APA7"},
    }
    draft_path = _write(Path(workspace.artifact_path("canonical_draft.json")), draft_payload)
    draft_record = registry.register_file(
        artifact_id="review_draft",
        artifact_role="review_draft",
        artifact_type="review_draft",
        artifact_version="v3",
        path=draft_path,
        producer="tests",
    )
    manifest_path = _write(Path(workspace.artifact_path("canonical_manifest.json")), manifest_payload)
    registry.register_file(
        artifact_id="citation_manifest:v3",
        artifact_role="citation_manifest",
        artifact_type="citation_manifest",
        artifact_version="v3",
        path=manifest_path,
        producer="tests",
        depends_on=[ArtifactDependencyRefV2.from_record(draft_record)],
    )

    derived_draft_path = _write(
        Path(workspace.artifact_path("derived_draft.json")),
        draft_payload,
    )
    derived_draft = registry.register_file(
        artifact_id="review_draft_repaired:repair-tx",
        artifact_role="review_draft_repaired",
        artifact_type="review_draft_repaired",
        artifact_version="v1",
        path=derived_draft_path,
        producer="tests",
        status="quarantined",
    )
    derived_manifest_path = _write(
        Path(workspace.artifact_path("derived_manifest.json")),
        manifest_payload,
    )
    derived_manifest = registry.register_file(
        artifact_id="citation_manifest_repaired:repair-tx",
        artifact_role="citation_manifest_repaired",
        artifact_type="citation_manifest_repaired",
        artifact_version="v1",
        path=derived_manifest_path,
        producer="tests",
        status="quarantined",
    )
    source_payload = {
        "transaction_id": "repair-tx:source",
        "job_id": workspace.job_id,
        "status": "quarantined",
        "applied_artifact_ids": [derived_draft.artifact_id, derived_manifest.artifact_id],
    }
    source_path = _write(Path(workspace.artifact_path("repair_transaction.json")), source_payload)
    source = registry.register_file(
        artifact_id="repair-tx:source",
        artifact_role="repair_transaction",
        artifact_type="repair_transaction",
        artifact_version="v1",
        path=source_path,
        producer="tests",
        status="quarantined",
    )

    result = RepairTransactionService(workspace, registry).promote(
        source.artifact_id,
        actor="researcher",
        reason="explicit human promotion test",
    )

    assert result["status"] == "promoted", result
    assert result["canonical_replacement"] is False
    assert result["canonical_paths_unchanged"] is True
    assert registry.get("review_draft").path == str(draft_path.resolve())  # type: ignore[union-attr]
    assert registry.get("citation_manifest:v3").path == str(manifest_path.resolve())  # type: ignore[union-attr]
    for artifact_id in result["versioned_artifact_ids"]:
        record = registry.get(artifact_id)
        assert record is not None and record.status == "ready"
    promotion = registry.get(result["promotion_transaction_id"])
    assert promotion is not None and promotion.status == "ready"
    assert json.loads(Path(promotion.path).read_text(encoding="utf-8"))["status"] == "promoted"
