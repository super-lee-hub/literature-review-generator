from __future__ import annotations

import json
from pathlib import Path
import zipfile

from runtime.export_bundle import ExportBundleService, ForensicAttestationService
from services.artifact_registry import ArtifactRegistry
from services.job_workspace import JobWorkspace, atomic_write_json


def test_export_bundle_contains_verified_artifact_and_provenance(tmp_path: Path) -> None:
    workspace = JobWorkspace.create(str(tmp_path), "demo", "job-export")
    registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    source = Path(workspace.artifact_path("summaries/source.json"))
    atomic_write_json(str(source), {"paper_key": "p1", "summary": "text"})
    registry.register_file(
        artifact_id="summary:p1",
        artifact_role="summary",
        artifact_type="summary",
        artifact_version="v1",
        path=source,
        producer="tests",
    )
    result = ExportBundleService(workspace, registry).export(
        completion={"completion_status": "complete"},
        closure={"status": "clean"},
    )
    assert result.status == "canonical_verified"
    assert Path(result.bundle_path).exists()
    with zipfile.ZipFile(result.bundle_path) as archive:
        names = set(archive.namelist())
        assert "provenance_manifest.json" in names
        assert "checksums.json" in names
        assert any(name.startswith("artifacts/summary_p1") for name in names)
        manifest = json.loads(archive.read("provenance_manifest.json"))
    assert manifest["status"] == "canonical_verified"
    assert registry.get(result.artifact_id) is not None


def test_forensic_attestation_does_not_call_untrusted_workspace_canonical(tmp_path: Path) -> None:
    workspace = JobWorkspace.create(str(tmp_path), "demo", "job-attest")
    registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    source = Path(workspace.artifact_path("draft.json"))
    atomic_write_json(str(source), {"draft": True})
    registry.register_file(
        artifact_id="draft",
        artifact_role="review_draft",
        artifact_type="review_draft",
        artifact_version="v2",
        path=source,
        producer="tests",
        metadata={"manual_modified": True},
    )
    result = ForensicAttestationService(workspace, registry).attest(
        completion={"completion_status": "complete"},
        closure={"status": "clean"},
    )
    assert result.status == "manual_repaired_legacy"
    assert result.manual_modified_artifact_ids == ("draft",)
    assert Path(result.report_path).exists()
