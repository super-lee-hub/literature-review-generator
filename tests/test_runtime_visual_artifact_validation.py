"""Regression coverage for Stage 1 visual artifacts at the reconciliation gate."""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

from runtime.reconcile import ReconcileValidationError, RuntimeReconciler
from services.artifact_registry import ArtifactRegistry
from services.job_workspace import JobWorkspace


_ONE_PIXEL_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "YAAAAAYAAjCB0C8AAAAASUVORK5CYII="
)


@pytest.mark.parametrize(
    "artifact_type",
    ("page_snapshot", "figure_crop", "table_crop", "formula_crop"),
)
def test_reconciler_validates_stage1_visual_binary_artifacts(
    tmp_path: Path,
    artifact_type: str,
) -> None:
    job_id = "job-visual-validation"
    workspace = JobWorkspace.create(str(tmp_path), "visual-validation", job_id=job_id)
    registry = ArtifactRegistry(workspace.paths.registry_path, job_id)
    image_path = Path(workspace.artifact_path(f"{artifact_type}.png"))
    image_path.write_bytes(_ONE_PIXEL_PNG)

    record = registry.register_file(
        artifact_id=f"{artifact_type}:test",
        artifact_role=artifact_type,
        artifact_type=artifact_type,
        artifact_version="v1",
        path=image_path,
        producer="tests",
    )

    RuntimeReconciler(workspace, registry).validate_record(record)


def test_reconciler_rejects_invalid_stage1_visual_binary_artifact(tmp_path: Path) -> None:
    job_id = "job-invalid-visual-validation"
    workspace = JobWorkspace.create(str(tmp_path), "invalid-visual-validation", job_id=job_id)
    registry = ArtifactRegistry(workspace.paths.registry_path, job_id)
    image_path = Path(workspace.artifact_path("page_snapshot.bin"))
    image_path.write_bytes(b"not an image")

    record = registry.register_file(
        artifact_id="page_snapshot:invalid",
        artifact_role="page_snapshot",
        artifact_type="page_snapshot",
        artifact_version="v1",
        path=image_path,
        producer="tests",
    )

    with pytest.raises(ReconcileValidationError, match="visual artifact"):
        RuntimeReconciler(workspace, registry).validate_record(record)


def test_reconciler_reuses_successful_record_validation_within_one_pass(
    tmp_path: Path,
) -> None:
    job_id = "job-visual-validation-cache"
    workspace = JobWorkspace.create(str(tmp_path), "visual-validation-cache", job_id=job_id)
    registry = ArtifactRegistry(workspace.paths.registry_path, job_id)
    image_path = Path(workspace.artifact_path("page_snapshot.png"))
    image_path.write_bytes(_ONE_PIXEL_PNG)
    record = registry.register_file(
        artifact_id="page_snapshot:cache",
        artifact_role="page_snapshot",
        artifact_type="page_snapshot",
        artifact_version="v1",
        path=image_path,
        producer="tests",
    )
    validation_calls = 0

    def count_validation(_record, _path) -> None:
        nonlocal validation_calls
        validation_calls += 1

    reconciler = RuntimeReconciler(
        workspace,
        registry,
        schema_validators={"page_snapshot": count_validation},
    )
    reconciler.validate_record(record)
    reconciler.validate_record(record)

    assert validation_calls == 1
