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
