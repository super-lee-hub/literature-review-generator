from __future__ import annotations

import json
from pathlib import Path
import zipfile

import runtime.export_bundle as export_bundle_module
from runtime.export_bundle import ExportBundleService, ForensicAttestationService
from services.artifact_registry import ArtifactRegistry, RegistryError
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
    assert result.status == "untrusted"
    assert result.bundle_path == ""
    assert result.artifact_id == ""
    assert "caller_completion_or_closure_ignored" in result.issues


def test_export_registration_failure_is_untrusted_and_removes_bundle(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = JobWorkspace.create(str(tmp_path), "demo", "job-export-registration-failure")
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
    monkeypatch.setattr(
        export_bundle_module,
        "_derive_current_evidence",
        lambda workspace, registry: {
            "completion": {"completion_status": "complete", "canonical_ready": True},
            "closure": {"status": "clean"},
            "receipt_closure": {"status": "clean", "complete": True},
            "current_stage_closure_map": {
                "artifact_type": "current_stage_closure_map",
                "artifact_version": "v1",
                "current_set_id": "test-set",
                "stages": {},
                "requested_stages": [],
                "spec_hash": "",
                "provider_closures_by_stage": {},
                "blocking_issues": [],
            },
            "requested_stages": [],
            "spec_hash": "",
            "adoption": {"status": "not_adopted"},
            "issues": [],
        },
    )

    def fail_registration(*args, **kwargs):
        raise RegistryError("registry unavailable")

    monkeypatch.setattr(registry, "register_file", fail_registration)
    result = ExportBundleService(workspace, registry).export()

    assert result.status == "untrusted"
    assert result.bundle_path == ""
    assert result.artifact_id == ""
    assert result.manifest["status"] == "untrusted"
    assert any(issue.startswith("bundle_registration_failed:") for issue in result.issues)
    assert not list(Path(workspace.report_path("export_bundles")).glob("*.zip"))


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
        status="quarantined",
        metadata={"manual_modified": True},
    )
    result = ForensicAttestationService(workspace, registry).attest(
        completion={"completion_status": "complete"},
        closure={"status": "clean"},
    )
    assert result.status == "untrusted"
    assert result.manual_modified_artifact_ids == ("draft",)
    assert Path(result.report_path).exists()
