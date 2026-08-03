"""Negative coverage for the current artifact validation gates."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from runtime.artifact_validators import ArtifactSchemaError, validate_current_outline_artifact
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


@pytest.mark.parametrize("payload", [{}, {"hello": "world"}])
def test_validation_run_result_rejects_placeholder_json(tmp_path: Path, payload: dict[str, object]) -> None:
    path = tmp_path / "validation_run_result.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    record = SimpleNamespace(artifact_type="validation_run_result", artifact_version="v1", job_id="job-1")

    with pytest.raises(ValidationRunResultError):
        _validate_validation_run_result(record, path)
