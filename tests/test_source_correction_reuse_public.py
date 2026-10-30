from __future__ import annotations

import json
from pathlib import Path

import pytest

from reviewctl import main
from services.artifact_registry import ArtifactRegistry
from tests.test_reviewctl_source_correction import _input


def test_public_exported_v2_is_consumed_by_normal_typed_input_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    from runtime.orchestrator import InternalStageExecutorRegistry
    from services.stage1_reuse import Stage1ReusableSummaryBindingV1, verify_stage1_typed_manifest_authority
    from tests.test_summary_correction_reuse import _prepare_approved_synthetic_source

    prepared = _prepare_approved_synthetic_source(tmp_path)
    source_workspace, origin_registry = prepared[:2]
    correction_workspace, _correction_registry = prepared[7:9]
    adoption = prepared[10]
    before = Path(origin_registry.registry_path).read_bytes()
    assert main([
        "source-correction-reuse", "--source-workspace", source_workspace.root_dir,
        "--workspace", correction_workspace.root_dir, "--receipt", adoption.adoption_receipt_artifact_id,
    ]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["usable_as_stage1_reuse"] is True
    assert result["provider_posts"] == 0
    summaries = InternalStageExecutorRegistry._summary_payloads_from_file(Path(result["owner_corrected_manifest_path"]))
    assert len(summaries) == 1
    binding = Stage1ReusableSummaryBindingV1.from_mapping(summaries[0]["stage1_reuse"]["binding"])
    unauthorized, unauthorized_reason = verify_stage1_typed_manifest_authority(summaries[0], binding)
    assert unauthorized is None
    assert unauthorized_reason == "owner_correction_authorized_registry_resolver_missing"
    authority, reason = verify_stage1_typed_manifest_authority(
        summaries[0], binding,
        external_registry_resolver=lambda job_id: (
            origin_registry if job_id == origin_registry.job_id
            else _correction_registry if job_id == _correction_registry.job_id else None
        ),
    )
    assert authority is not None, reason
    assert authority.manifest.artifact_version == "v2"
    assert authority.provider_closure_path == ""
    assert authority.provider_ledger_path == ""
    _assert_current_owner_reuse_closure(authority, origin_registry, _correction_registry, tmp_path)
    from runtime.artifact_validators import ArtifactSchemaError, validate_registered_artifact
    from runtime.reconcile import DEFAULT_SCHEMA_VALIDATORS, ReconcileValidationError

    manifest_record = _correction_registry.get(result["owner_corrected_manifest_artifact_id"])
    assert manifest_record is not None
    validate_registered_artifact(manifest_record, Path(manifest_record.path))
    DEFAULT_SCHEMA_VALIDATORS["stage1_reusable_summary_manifest"](manifest_record, Path(manifest_record.path))
    original_bytes = Path(manifest_record.path).read_bytes()
    tampered = json.loads(original_bytes)
    tampered["provider_receipt_closure_id"] = "forbidden-original-provider-closure"
    tampered["provider_receipt_closure_hash"] = "a" * 64
    from runtime.provider_runtime import hash_json
    tampered["manifest_content_hash"] = hash_json({**tampered, "manifest_content_hash": ""})
    Path(manifest_record.path).write_text(json.dumps(tampered), encoding="utf-8")
    try:
        with pytest.raises(ArtifactSchemaError, match="provider_receipts_forbidden"):
            validate_registered_artifact(manifest_record, Path(manifest_record.path))
        with pytest.raises(ReconcileValidationError, match="provider_receipts_forbidden"):
            DEFAULT_SCHEMA_VALIDATORS["stage1_reusable_summary_manifest"](manifest_record, Path(manifest_record.path))
    finally:
        Path(manifest_record.path).write_bytes(original_bytes)
    assert Path(origin_registry.registry_path).read_bytes() == before


def _assert_current_owner_reuse_closure(authority, origin_registry, correction_registry, tmp_path: Path) -> None:
    from dataclasses import replace
    from services.artifact_registry import ArtifactDependencyRefV2
    from services.job_workspace import JobWorkspace
    from services.queue_service import LocalPublicationContext
    from services.stage1_analysis_service import Stage1AnalysisService
    from validation.closure import _stage1_typed_manifest_authority_issues

    workspace = JobWorkspace.create(str(tmp_path / "current-reuse"), "current", "current-job")
    registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    service = object.__new__(Stage1AnalysisService)
    service.registry = registry
    service.workspace = workspace
    service.publication_context = LocalPublicationContext()
    owner_refs = authority.manifest.owner_correction_authority
    proof_records = {}
    for label, ref_name, path in (
        ("origin_source", "origin_source", authority.owner_correction_origin_source_path),
        ("prior_manifest", "origin_prior_manifest", authority.owner_correction_prior_manifest_path),
        ("derived_summary_set", "derived_summary_set", authority.owner_correction_derived_summary_path),
        ("adoption_receipt", "adoption_receipt", authority.owner_correction_receipt_path),
    ):
        reference = owner_refs[ref_name]
        proof_records[label] = service._publish_portable_authority_record(
            source_path=path, expected_hash=reference["content_hash"], portable_kind=f"owner_correction_{label}",
            source_authority_job_id=reference["job_id"], original_artifact_id=reference["artifact_id"],
            typed_manifest_artifact_id=authority.manifest_artifact_id,
            typed_manifest_artifact_hash=authority.manifest_file_hash,
        )
    source = service._publish_portable_authority_record(
        source_path=authority.source_summary_path, expected_hash=authority.manifest.source_summary_artifact_hash,
        portable_kind="summary_source", source_authority_job_id=authority.manifest.job_id,
        original_artifact_id=authority.manifest.source_summary_artifact_id,
        typed_manifest_artifact_id=authority.manifest_artifact_id, typed_manifest_artifact_hash=authority.manifest_file_hash,
    )
    manifest = service._publish_portable_authority_record(
        source_path=authority.manifest_path, expected_hash=authority.manifest_file_hash,
        portable_kind="summary_manifest", source_authority_job_id=authority.manifest.job_id,
        original_artifact_id=authority.manifest_artifact_id,
        typed_manifest_artifact_id=authority.manifest_artifact_id, typed_manifest_artifact_hash=authority.manifest_file_hash,
        dependencies=[ArtifactDependencyRefV2.from_record(item) for item in [source, *proof_records.values()]],
    )
    payload = {
        "source_authority_kind": "typed_manifest", "source_authority_job_id": authority.manifest.job_id,
        "source_authority_artifact_id": authority.manifest.source_summary_artifact_id,
        "source_authority_artifact_hash": authority.manifest.source_summary_artifact_hash,
        "typed_manifest_artifact_id": authority.manifest_artifact_id,
        "typed_manifest_artifact_hash": authority.manifest_file_hash,
        "typed_manifest_content_hash": authority.manifest.manifest_content_hash,
        "summary_payload_hash": authority.manifest.summary_payload_hash,
        "normalized_summary_payload_hash": authority.manifest.normalized_summary_payload_hash,
        "owner_correction_authority": owner_refs,
        "portable_owner_correction_records": {
            label: {"artifact_id": item.artifact_id, "content_hash": item.content_hash}
            for label, item in proof_records.items()
        },
    }
    payload.update({name: "" for name in (
        "source_provider_receipt_closure_id", "source_provider_receipt_closure_hash",
        "source_provider_receipt_ledger_id", "source_provider_receipt_ledger_hash",
    )})

    def resolve(job_id):
        return origin_registry if job_id == origin_registry.job_id else correction_registry if job_id == correction_registry.job_id else None

    def issues(record, resolver=resolve):
        return _stage1_typed_manifest_authority_issues(
            stage="analyze", paper_key=authority.manifest.canonical_paper_key,
            reuse_payload=payload, source_record=source, manifest_record=record,
            closure_record=None, ledger_record=None, registry=registry, external_registry_resolver=resolver,
        )
    assert issues(manifest) == []
    assert any("authorized_registry_resolver_missing" in item for item in issues(manifest, None))
    detached = replace(manifest, depends_on=[item for item in manifest.depends_on if item.artifact_id != source.artifact_id])
    assert any("source_dependency_invalid" in item for item in issues(detached))


def test_public_export_does_not_invent_missing_prior_reuse_basis(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    source, registry, source_record, _proposal, proposal_path = _input(tmp_path)
    assert main([
        "source-correction-plan", "--workspace", source.root_dir,
        "--proposal", str(proposal_path), "--output-root", str(tmp_path / "corrections"),
    ]) == 0
    preparation = json.loads(capsys.readouterr().out)
    destination_path = Path(preparation["destination_workspace"])
    destination = ArtifactRegistry(destination_path / "artifact_registry.json", destination_path.name.split("__", 1)[1])
    candidate = destination.get(preparation["candidate_artifact_id"])
    assert candidate is not None
    assert main([
        "source-correction-adopt", "--source-workspace", source.root_dir,
        "--workspace", str(destination_path), "--candidate", candidate.artifact_id,
        "--expected-hash", candidate.content_hash,
        "--actor", "synthetic-test-owner", "--reason", "Reviewed synthetic correction.",
    ]) == 0
    adoption = json.loads(capsys.readouterr().out)
    paths = [Path(registry.registry_path), Path(destination.registry_path), Path(source_record.path)]
    before = [path.read_bytes() for path in paths]
    assert main([
        "source-correction-reuse", "--source-workspace", source.root_dir,
        "--workspace", str(destination_path), "--receipt", adoption["adoption_receipt_artifact_id"],
    ]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "blocked", result
    assert result["usable_as_stage1_reuse"] is False
    assert result["provider_posts"] == 0
    assert "manifest" in result["reason"] or "basis" in result["reason"]
    assert [path.read_bytes() for path in paths] == before
