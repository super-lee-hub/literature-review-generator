"""Negative coverage for the current artifact validation gates."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from runtime.artifact_validators import (
    ArtifactSchemaError,
    validate_current_outline_artifact,
    validate_registered_artifact,
)
from services.citation_ref_catalog import build_document_ref_catalog
from runtime.reconcile import _validate_validation_run_result
from validation.run_result import ValidationRunResultError


def _stage1_visual_coverage_payload() -> dict[str, object]:
    return {
        "artifact_type": "stage1_visual_coverage",
        "artifact_version": "v1",
        "job_id": "job-1",
        "paper_key": "paper-a",
        "total_pdf_pages": 1,
        "nonblank_pages": [1],
        "rendered_pages": [1],
        "visually_scanned_pages": [1],
        "page_status": [],
        "scan_batches": [],
        "coverage_status": "complete",
        "scan_coverage_status": "complete",
        "final_synthesis_modality": "text_only",
        "final_raw_visual_recheck_status": "partial",
        "evidence_coverage_status": "degraded",
        "raw_reinspection_units": [{"unit_id": "unit-1", "closed": False}],
        "required_raw_reinspection_unit_count": 1,
        "closed_raw_reinspection_unit_count": 0,
        "unresolved_raw_reinspection_unit_ids": ["unit-1"],
        "omissions": [],
        "transport_omissions": [],
    }


@pytest.mark.parametrize("artifact_type", ["final_outline", "coverage_audit"])
@pytest.mark.parametrize("payload", [{}, {"hello": "world"}])
def test_current_outline_artifacts_reject_placeholder_json(
    tmp_path: Path,
    artifact_type: str,
    payload: dict[str, object],
) -> None:
    path = tmp_path / f"{artifact_type}.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    record = SimpleNamespace(artifact_type=artifact_type, artifact_version="v3", job_id="job-1")

    with pytest.raises(ArtifactSchemaError):
        validate_current_outline_artifact(record, path)


def test_current_outline_artifact_unknown_version_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "final_outline.json"
    path.write_text("{}", encoding="utf-8")
    record = SimpleNamespace(artifact_type="final_outline", artifact_version="v2", job_id="job-1")

    with pytest.raises(ArtifactSchemaError, match="version-aware"):
        validate_registered_artifact(record, path)


@pytest.mark.parametrize("payload", [{}, {"hello": "world"}])
def test_validation_run_result_rejects_placeholder_json(tmp_path: Path, payload: dict[str, object]) -> None:
    path = tmp_path / "validation_run_result.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    record = SimpleNamespace(artifact_type="validation_run_result", artifact_version="v1", job_id="job-1")

    with pytest.raises(ValidationRunResultError):
        _validate_validation_run_result(record, path)


def test_review_replay_ledger_validates_jsonl_records(tmp_path: Path) -> None:
    path = tmp_path / "review_replay.jsonl"
    path.write_text(
        json.dumps(
            {
                "replay_version": "review-section-replay-v1",
                "job_id": "job-1",
                "stage_name": "stage3_review",
                "closure_epoch_id": "review-epoch",
                "section_id": "candidate_1_section_1",
                "binding_hash": "b" * 64,
                "artifact_id": "review-section:candidate_1_section_1",
                "artifact_path": str(tmp_path / "section.json"),
                "artifact_content_hash": "c" * 64,
                "registry_file_hash": "d" * 64,
                "receipt_id": "receipt-1",
                "normalized_output_hash": "e" * 64,
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    record = SimpleNamespace(
        artifact_type="review_replay_ledger",
        artifact_version="v1",
        job_id="job-1",
    )

    validate_registered_artifact(record, path)


def test_review_replay_ledger_rejects_json_object_and_malformed_lines(tmp_path: Path) -> None:
    record = SimpleNamespace(
        artifact_type="review_replay_ledger",
        artifact_version="v1",
        job_id="job-1",
    )
    object_path = tmp_path / "object.jsonl"
    object_path.write_text(json.dumps({"records": []}), encoding="utf-8")
    malformed_path = tmp_path / "malformed.jsonl"
    malformed_path.write_text("not-json\n", encoding="utf-8")

    with pytest.raises(ArtifactSchemaError):
        validate_registered_artifact(record, object_path)
    with pytest.raises(ArtifactSchemaError):
        validate_registered_artifact(record, malformed_path)


def test_citation_ref_catalog_uses_its_canonical_v1_contract(tmp_path: Path) -> None:
    payload = build_document_ref_catalog(
        [
            {
                "paper_info": {
                    "canonical_paper_key": "paper-a",
                    "title": "Study A",
                    "authors": ["Author A"],
                    "year": 2025,
                }
            }
        ],
        project_name="validator-test",
        job_id="job-1",
    )
    path = tmp_path / "citation_ref_catalog.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    record = SimpleNamespace(
        artifact_type="citation_ref_catalog",
        artifact_version="v1",
        job_id="job-1",
    )

    validate_registered_artifact(record, path)

    malformed = dict(payload)
    malformed["catalog_hash"] = "0" * 64
    path.write_text(json.dumps(malformed), encoding="utf-8")
    with pytest.raises(ArtifactSchemaError):
        validate_registered_artifact(record, path)


def test_stage1_visual_observations_v2_binds_child_ids_to_persisted_refs(tmp_path: Path) -> None:
    payload = {
        "artifact_type": "stage1_visual_observations",
        "artifact_version": "v2",
        "job_id": "job-1",
        "paper_key": "paper-a",
        "batch_index": 0,
        "call_id": "stage1_visual_scan:paper-a:0",
        "prompt_id": "stage1.visual_scan.system.v2",
        "prompt_version": "v2",
        "prompt_sha256": "a" * 64,
        "visual_ids": ["page-001"],
        "child_candidate_ids": ["table-001-01"],
        "child_candidate_refs": [],
        "schema_hash": "b" * 64,
        "status": "failed",
        "observations": [],
        "error": "provider failure",
    }
    path = tmp_path / "visual-observations.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    record = SimpleNamespace(
        artifact_type="stage1_visual_observations",
        artifact_version="v2",
        job_id="job-1",
    )

    with pytest.raises(ArtifactSchemaError, match="child_candidate_ids do not match"):
        validate_registered_artifact(record, path)

    payload["child_candidate_ids"] = []
    path.write_text(json.dumps(payload), encoding="utf-8")
    validate_registered_artifact(record, path)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda payload: payload.update(
            final_raw_visual_recheck_status="complete"
        ),
        lambda payload: payload.update(evidence_coverage_status="complete"),
        lambda payload: payload.update(
            transport_omissions=[
                {
                    "visual_id": "figure-1",
                    "page_no": 1,
                    "reason": "raw_reinspection_group_not_represented",
                    "scope": "raw_reinspection",
                    "authority_blocking": False,
                    "raw_reinspection_group_id": "unknown-unit",
                    "raw_reinspection_resolution": "not_represented",
                }
            ]
        ),
        lambda payload: (
            payload.update(
                raw_reinspection_units=[{"unit_id": "unit-1", "closed": True}],
                closed_raw_reinspection_unit_count=1,
                unresolved_raw_reinspection_unit_ids=[],
                transport_omissions=[
                    {
                        "visual_id": "figure-1",
                        "page_no": 1,
                        "reason": "raw_reinspection_group_not_represented",
                        "scope": "raw_reinspection",
                        "authority_blocking": False,
                        "raw_reinspection_group_id": "unit-1",
                        "raw_reinspection_resolution": "not_represented",
                    }
                ],
            )
        ),
    ],
)
def test_stage1_visual_coverage_validator_rejects_semantic_contradictions(
    tmp_path: Path,
    mutation,
) -> None:
    payload = _stage1_visual_coverage_payload()
    mutation(payload)
    path = tmp_path / "stage1_visual_coverage.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    record = SimpleNamespace(
        artifact_type="stage1_visual_coverage",
        artifact_version="v1",
        job_id="job-1",
    )

    with pytest.raises(ArtifactSchemaError, match="semantic"):
        validate_registered_artifact(record, path)


def test_stage1_visual_coverage_validator_rejects_malformed_omission_array(
    tmp_path: Path,
) -> None:
    payload = _stage1_visual_coverage_payload()
    payload["transport_omissions"] = {"scope": "final_transport"}
    path = tmp_path / "stage1_visual_coverage-malformed-omission.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    record = SimpleNamespace(
        artifact_type="stage1_visual_coverage",
        artifact_version="v1",
        job_id="job-1",
    )

    with pytest.raises(ArtifactSchemaError, match="semantic"):
        validate_registered_artifact(record, path)
