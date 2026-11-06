from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path
from typing import Any

import runtime.export_bundle as export_bundle_module
from runtime.export_bundle import ExportBundleService, ExportBundleSpecV1
from services.artifact_registry import ArtifactRegistry
from services.job_workspace import JobWorkspace, atomic_write_json


def _canonical_evidence() -> dict[str, Any]:
    return {
        "completion": {
            "job_status": "completed",
            "completion_status": "complete",
            "canonical_ready": True,
            "completion_reasons": [],
            "completion_evidence_hash": "a" * 64,
        },
        "closure": {
            "status": "clean",
            "semantic": {"validation_disposition": "clean", "contract_satisfied": True},
        },
        "validation_policy": {
            "projection_version": "export-validation-policy-v1",
            "source": "runtime_stage_plan",
            "runtime_spec_hash": "b" * 64,
            "stage_plan_hash": "d" * 64,
            "validation_enabled": True,
            "validation_required": True,
            "require_clean_validation": True,
            "allow_unvalidated_when_validation_optional": False,
        },
        "receipt_closure": {
            "status": "clean",
            "complete": True,
            "artifact_ids": ["provider_receipt_closure:current"],
            "closures": [{"complete": True}],
        },
        "current_stage_closure_map": {
            "artifact_type": "current_stage_closure_map",
            "artifact_version": "v1",
            "job_id": "job-export-reuse",
            "current_set_id": "current-set:one",
            "stages": {"validate": {"status": "clean"}},
            "requested_stages": ["validate"],
            "spec_hash": "b" * 64,
            "provider_closures_by_stage": {},
            "blocking_issues": [],
        },
        "requested_stages": ["validate"],
        "spec_hash": "b" * 64,
        "current_artifact_set": {"set_id": "current-set:one"},
        "adoption": {"status": "not_adopted"},
        "issues": [],
    }


def _canonical_evidence_for_snapshot(
    _workspace,
    registry,
    *,
    records_snapshot,
    expected_snapshot_revision,
    records_snapshot_error,
):
    assert records_snapshot_error is None
    assert expected_snapshot_revision == registry._revision
    assert records_snapshot == registry.list_records()
    return _canonical_evidence()


def _workspace_and_registry(tmp_path: Path) -> tuple[JobWorkspace, ArtifactRegistry, Path]:
    workspace = JobWorkspace.create(str(tmp_path), "demo", "job-export-reuse")
    registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    source = Path(workspace.artifact_path("summaries/source.json"))
    atomic_write_json(str(source), {"paper_key": "p1", "summary": "canonical text"})
    registry.register_file(
        artifact_id="summary:p1",
        artifact_role="summary",
        artifact_type="summary",
        artifact_version="v1",
        path=source,
        producer="tests",
    )
    outcome = Path(workspace.artifact_path("job_outcome_v1.json"))
    atomic_write_json(str(outcome), {"job_status": "completed", "revision": 1})
    registry.register_file(
        artifact_id="job_outcome",
        artifact_role="job_outcome",
        artifact_type="job_outcome",
        artifact_version="v1",
        path=outcome,
        producer="tests",
    )
    return workspace, registry, source


def test_repeat_canonical_export_reuses_registered_bundle_identity_and_bytes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace, registry, _source = _workspace_and_registry(tmp_path)
    outcome = Path(workspace.artifact_path("job_outcome_v1.json"))
    evidence_calls = 0

    def derive_evidence(_workspace, _registry, **snapshot):
        nonlocal evidence_calls
        evidence_calls += 1
        evidence = _canonical_evidence_for_snapshot(_workspace, _registry, **snapshot)
        if evidence_calls > 1:
            evidence["completion"]["completion_evidence_hash"] = "c" * 64
            evidence["closure"]["checked_at"] = "2026-10-05T00:00:02Z"
            evidence["current_stage_closure_map"]["resolved_at"] = "2026-10-05T00:00:03Z"
        return evidence

    monkeypatch.setattr(
        export_bundle_module,
        "_derive_current_evidence",
        derive_evidence,
    )
    service = ExportBundleService(workspace, registry)

    first = service.export()
    first_record = registry.get(first.artifact_id)
    assert first.status == "canonical_verified"
    assert first.manifest["validation_enabled"] is True
    assert first.manifest["validation_required"] is True
    assert first.manifest["stage_plan_hash"] == "d" * 64
    assert first.manifest["operational_snapshot_policy"] == {
        "job_outcome": "excluded_from_canonical_bundle_identity"
    }
    assert all(item["artifact_type"] != "job_outcome" for item in first.manifest["records"])
    assert first_record is not None
    assert first.bundle_path == first_record.path

    # A same-CurrentArtifactSet resume may refresh this mutable operational
    # singleton without changing the canonical export identity.
    atomic_write_json(str(outcome), {"job_status": "completed", "revision": 2})
    registry.register_file(
        artifact_id="job_outcome",
        artifact_role="job_outcome",
        artifact_type="job_outcome",
        artifact_version="v1",
        path=outcome,
        producer="tests",
    )

    second = service.export()
    second_record = registry.get(second.artifact_id)

    assert second.status == first.status
    assert second.bundle_id == first.bundle_id
    assert second.artifact_id == first.artifact_id
    assert second.bundle_path == first.bundle_path
    assert second.manifest == first.manifest
    assert second_record is not None
    assert second_record.content_hash == first_record.content_hash
    assert hashlib.sha256(Path(second.bundle_path).read_bytes()).hexdigest() == first_record.content_hash
    with zipfile.ZipFile(second.bundle_path) as archive:
        stored_manifest = json.loads(archive.read("provenance_manifest.json"))
        status_text = archive.read("EXPORT_STATUS.txt").decode("utf-8")
    assert "validation_required=true\n" in status_text
    assert "validation_enabled=true\n" in status_text
    assert stored_manifest == second.manifest


def test_canonical_export_refuses_explicit_job_outcome_payload_request(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace, registry, _source = _workspace_and_registry(tmp_path)
    monkeypatch.setattr(
        export_bundle_module,
        "_derive_current_evidence",
        _canonical_evidence_for_snapshot,
    )

    result = ExportBundleService(workspace, registry).export(
        spec=ExportBundleSpecV1(include_artifact_ids=("job_outcome",))
    )

    assert result.status == "untrusted"
    assert result.artifact_id == ""
    assert "job_outcome_is_operational_snapshot_use_forensic_export" in result.issues


def test_forensic_export_labels_job_outcome_as_export_time_snapshot(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace, registry, _source = _workspace_and_registry(tmp_path)
    monkeypatch.setattr(
        export_bundle_module,
        "_derive_current_evidence",
        _canonical_evidence_for_snapshot,
    )

    result = ExportBundleService(workspace, registry).export(
        spec=ExportBundleSpecV1(export_mode="forensic")
    )

    outcome_entry = next(
        item for item in result.manifest["records"] if item["artifact_type"] == "job_outcome"
    )
    assert result.manifest["operational_snapshot_policy"] == {
        "job_outcome": "included_as_forensic_snapshot_at_export"
    }
    assert outcome_entry["temporal_scope"] == "forensic_snapshot_at_export"
    assert outcome_entry["integrity"] == "verified"
    with zipfile.ZipFile(result.bundle_path) as archive:
        payload = json.loads(archive.read("artifacts/job_outcome"))
    assert payload["revision"] == 1


def test_repeat_export_rejects_tampered_cached_bundle_without_republication(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace, registry, _source = _workspace_and_registry(tmp_path)
    monkeypatch.setattr(
        export_bundle_module,
        "_derive_current_evidence",
        _canonical_evidence_for_snapshot,
    )
    service = ExportBundleService(workspace, registry)

    first = service.export()
    first_record = registry.get(first.artifact_id)
    assert first_record is not None
    Path(first_record.path).write_bytes(b"tampered cached export")

    repeated = service.export()

    assert repeated.status == "untrusted"
    assert repeated.artifact_id == ""
    assert repeated.bundle_path == ""
    assert any(issue.startswith("cached_export_bundle_rejected:") for issue in repeated.issues)
    assert registry.get(first.artifact_id).content_hash == first_record.content_hash


def test_repeat_export_does_not_reuse_bundle_after_source_dependency_tamper(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace, registry, source = _workspace_and_registry(tmp_path)
    monkeypatch.setattr(
        export_bundle_module,
        "_derive_current_evidence",
        _canonical_evidence_for_snapshot,
    )
    service = ExportBundleService(workspace, registry)

    first = service.export()
    source.write_text('{"paper_key":"p1","summary":"tampered"}', encoding="utf-8")

    repeated = service.export()

    assert repeated.status == "untrusted"
    assert repeated.artifact_id == ""
    assert repeated.bundle_path == ""
    assert "summary:p1: artifact content hash changed: summary:p1" in "\n".join(repeated.issues)
    assert registry.get(first.artifact_id) is not None


def test_new_registered_source_hash_creates_a_distinct_valid_export_version(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace, registry, source = _workspace_and_registry(tmp_path)
    monkeypatch.setattr(
        export_bundle_module,
        "_derive_current_evidence",
        _canonical_evidence_for_snapshot,
    )
    service = ExportBundleService(workspace, registry)

    first = service.export()
    atomic_write_json(str(source), {"paper_key": "p1", "summary": "new verified text"})
    registry.register_file(
        artifact_id="summary:p1",
        artifact_role="summary",
        artifact_type="summary",
        artifact_version="v1",
        path=source,
        producer="tests",
    )

    updated = service.export()

    assert updated.status == "canonical_verified"
    assert updated.artifact_id
    assert updated.bundle_id != first.bundle_id
    assert updated.bundle_path != first.bundle_path
    updated_record = registry.get(updated.artifact_id)
    assert updated_record is not None
    assert updated_record.content_hash == hashlib.sha256(Path(updated.bundle_path).read_bytes()).hexdigest()


def test_corrected_policy_creates_new_bundle_and_preserves_prior_ready_zip(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace, registry, _source = _workspace_and_registry(tmp_path)
    service = ExportBundleService(workspace, registry)
    current_identity = export_bundle_module._export_identity_payload

    def prior_identity(**fields):
        payload = current_identity(**fields)
        payload.pop("validation_policy")
        return payload

    def prior_evidence(_workspace, _registry, **snapshot):
        evidence = _canonical_evidence_for_snapshot(_workspace, _registry, **snapshot)
        evidence["validation_policy"] = {}
        return evidence

    # Materialize the old projection and identity without changing any source
    # artifact. The corrected exporter must leave that immutable ZIP intact.
    with monkeypatch.context() as old:
        old.setattr(export_bundle_module, "_export_identity_payload", prior_identity)
        old.setattr(export_bundle_module, "_derive_current_evidence", prior_evidence)
        prior = service.export()
    prior_record = registry.get(prior.artifact_id)
    assert prior_record is not None
    prior_bytes = Path(prior.bundle_path).read_bytes()
    assert prior.manifest["validation_required"] is False

    monkeypatch.setattr(export_bundle_module, "_derive_current_evidence", _canonical_evidence_for_snapshot)
    corrected = service.export()
    repeated = service.export()

    assert corrected.status == repeated.status == "canonical_verified"
    assert corrected.artifact_id != prior.artifact_id
    assert corrected.bundle_path != prior.bundle_path
    assert corrected.manifest["validation_required"] is True
    assert corrected.manifest["validation_enabled"] is True
    assert corrected.artifact_id == repeated.artifact_id
    assert corrected.bundle_path == repeated.bundle_path
    assert registry.get(prior.artifact_id) == prior_record
    assert Path(prior.bundle_path).read_bytes() == prior_bytes
    assert hashlib.sha256(prior_bytes).hexdigest() == prior_record.content_hash
    assert Path(corrected.bundle_path).read_bytes() == Path(repeated.bundle_path).read_bytes()
