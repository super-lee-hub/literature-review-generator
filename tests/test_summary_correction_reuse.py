from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from runtime.provider_runtime import hash_json
from services.artifact_registry import ArtifactDependencyRefV2, ArtifactRegistry, file_sha256
from services.job_workspace import JobWorkspace, publish_json_artifact
from services.queue_service import LocalPublicationContext
from services.stage1_reuse import (
    OWNER_APPROVED_SOURCE_CORRECTION_KIND,
    Stage1ReusableSummaryBindingV1,
    Stage1ReusableSummaryManifestV1,
    Stage1ReusableSummaryManifestV2,
    build_binding_hash,
)
from services.summary_correction import prepare_source_summary_correction_candidate
from services.summary_correction_adoption import adopt_source_summary_correction_candidate
from services.summary_correction_reuse import (
    OwnerCorrectedStage1ReuseExportError,
    export_owner_corrected_stage1_reuse_authority,
    verify_owner_corrected_stage1_manifest_authority,
)
from test_summary_correction import _build_source, _proposal_payload


def test_owner_corrected_manifest_requires_authorized_registry_resolver() -> None:
    authority, reason = verify_owner_corrected_stage1_manifest_authority(
        manifest=Stage1ReusableSummaryManifestV2(),
        manifest_path=Path("unused-manifest.json"),
        manifest_file_hash="",
        manifest_artifact_id="",
        source_summary_path=Path("unused-summary.json"),
        previous_summary={},
        binding=Stage1ReusableSummaryBindingV1(),
    )

    assert authority is None
    assert reason == "owner_correction_authorized_registry_resolver_missing"


def _publish_v1_prior_basis(
    *,
    workspace: JobWorkspace,
    registry: ArtifactRegistry,
    source_record: Any,
    summaries: list[dict[str, Any]],
    duplicate_key: str = "",
) -> list[Any]:
    publication = LocalPublicationContext()
    runtime_record = publish_json_artifact(
        publication,
        registry,
        workspace.artifact_path("synthetic_basis/runtime.json"),
        {"artifact_type": "runtime_job_spec", "artifact_version": "v1", "job_id": registry.job_id},
        artifact_id="synthetic:runtime",
        artifact_role="runtime_job_spec",
        artifact_type="runtime_job_spec",
        artifact_version="v1",
        producer="tests.test_summary_correction_reuse",
    )
    evidence_record = publish_json_artifact(
        publication,
        registry,
        workspace.artifact_path("synthetic_basis/evidence.json"),
        {"artifact_type": "evidence_manifest", "artifact_version": "v1", "job_id": registry.job_id},
        artifact_id="synthetic:evidence",
        artifact_role="evidence_manifest",
        artifact_type="evidence_manifest",
        artifact_version="v1",
        producer="tests.test_summary_correction_reuse",
    )
    bundle_record = publish_json_artifact(
        publication,
        registry,
        workspace.artifact_path("synthetic_basis/source_bundle.json"),
        {"artifact_type": "source_bundle", "artifact_version": "v1", "job_id": registry.job_id},
        artifact_id="synthetic:source_bundle",
        artifact_role="source_bundle",
        artifact_type="source_bundle",
        artifact_version="v1",
        producer="tests.test_summary_correction_reuse",
    )
    registry.reload()
    registry_revision = str(registry.revision)
    manifests = []
    for index, summary in enumerate(summaries):
        paper_info = summary["paper_info"]
        paper_key = str(paper_info["canonical_paper_key"])
        source_summary = copy.deepcopy(summary)
        source_summary["summary_payload_hash"] = hash_json(summary["ai_summary"])
        paper_source = publish_json_artifact(
            publication,
            registry,
            workspace.artifact_path(f"synthetic_basis/papers/{index:03d}.json"),
            [source_summary],
            artifact_id=f"synthetic:summary:{index:03d}",
            artifact_role="summary_source",
            artifact_type="summary_file",
            artifact_version="v1",
            producer="tests.test_summary_correction_reuse",
            depends_on=[ArtifactDependencyRefV2.from_record(source_record)],
        )
        summary_hash = hash_json(summary["ai_summary"])
        binding = Stage1ReusableSummaryBindingV1(
            canonical_paper_key=paper_key,
            source_paper_id=str(paper_info.get("source_paper_id") or paper_key),
            source_mode=str(summary.get("source_mode") or ""),
            summary_payload_hash=summary_hash,
            normalized_summary_payload_hash=summary_hash,
            source_kind="",
            source_authority_job_id=registry.job_id,
            source_authority_artifact_id=paper_source.artifact_id,
            source_authority_artifact_hash=paper_source.content_hash,
            source_authority_artifact_path=paper_source.path,
            source_authority_registry_id=f"artifact-registry:{registry.job_id}",
            source_authority_registry_revision=registry_revision,
        )
        manifest = Stage1ReusableSummaryManifestV1(
            job_id=registry.job_id,
            stage_name="stage1_analyze",
            canonical_paper_key=paper_key,
            source_paper_id=binding.source_paper_id,
            source_summary_artifact_id=paper_source.artifact_id,
            source_summary_artifact_hash=paper_source.content_hash,
            source_summary_artifact_path=paper_source.path,
            source_summary_artifact_version="v1",
            summary_payload_hash=summary_hash,
            normalized_summary_payload_hash=summary_hash,
            binding_hash=build_binding_hash(binding.to_dict()),
            source_registry_identity=binding.source_authority_registry_id,
            source_registry_revision=registry_revision,
            source_kind="",
            binding=binding.to_dict(),
            paper_info=dict(paper_info),
            summary_payload=dict(summary["ai_summary"]),
            runtime_spec_id=runtime_record.artifact_id,
            runtime_spec_hash=runtime_record.content_hash,
            evidence_manifest_id=evidence_record.artifact_id,
            evidence_manifest_hash=evidence_record.content_hash,
            source_bundle_id=bundle_record.artifact_id,
            source_bundle_hash=bundle_record.content_hash,
            created_at="2026-10-02T00:00:00+00:00",
            producer="tests.test_summary_correction_reuse",
        ).to_dict()
        manifest["manifest_content_hash"] = hash_json(
            {**manifest, "manifest_content_hash": ""}
        )
        manifest_dependencies = [
            ArtifactDependencyRefV2.from_record(paper_source),
            ArtifactDependencyRefV2.from_record(runtime_record),
            ArtifactDependencyRefV2.from_record(evidence_record),
            ArtifactDependencyRefV2.from_record(bundle_record),
        ]
        manifest_record = publish_json_artifact(
            publication,
            registry,
            workspace.artifact_path(f"synthetic_basis/manifests/{index:03d}.json"),
            manifest,
            artifact_id=f"synthetic:prior_manifest:{index:03d}",
            artifact_role="stage1_summary_manifest",
            artifact_type="stage1_reusable_summary_manifest",
            artifact_version="v1",
            producer="tests.test_summary_correction_reuse",
            depends_on=manifest_dependencies,
        )
        manifests.append(manifest_record)
        if paper_key == duplicate_key:
            duplicate = copy.deepcopy(manifest)
            duplicate["created_at"] = "2026-10-02T00:01:00+00:00"
            duplicate["manifest_content_hash"] = hash_json(
                {**duplicate, "manifest_content_hash": ""}
            )
            manifests.append(
                publish_json_artifact(
                    publication,
                    registry,
                    workspace.artifact_path(f"synthetic_basis/manifests/{index:03d}_duplicate.json"),
                    duplicate,
                    artifact_id=f"synthetic:prior_manifest_duplicate:{index:03d}",
                    artifact_role="stage1_summary_manifest",
                    artifact_type="stage1_reusable_summary_manifest",
                    artifact_version="v1",
                    producer="tests.test_summary_correction_reuse",
                    depends_on=manifest_dependencies,
                )
            )
    return manifests


def _prepare_approved_synthetic_source(
    tmp_path: Path,
    *,
    with_prior_manifests: bool = True,
    duplicate_key: str = "",
) -> tuple[Any, ...]:
    (
        source_workspace,
        source_registry,
        source_record,
        summaries,
        pdf_path,
        pdf_hash,
        page_renders,
    ) = _build_source(tmp_path)
    if with_prior_manifests:
        _publish_v1_prior_basis(
            workspace=source_workspace,
            registry=source_registry,
            source_record=source_record,
            summaries=summaries,
            duplicate_key=duplicate_key,
        )
    dest_workspace = JobWorkspace.create(
        str(tmp_path / "correction-output"), "correction-project", "correction-job"
    )
    dest_registry = ArtifactRegistry(dest_workspace.paths.registry_path, dest_workspace.job_id)
    proposal = _proposal_payload(source_record, summaries, pdf_path, pdf_hash, page_renders)
    correction = prepare_source_summary_correction_candidate(
        proposal_payload=proposal,
        source_registry=source_registry,
        source_artifact_id=source_record.artifact_id,
        destination_workspace=dest_workspace,
        destination_registry=dest_registry,
        publication_context=LocalPublicationContext(),
    )
    candidate = dest_registry.get(correction.candidate_artifact_id)
    assert candidate is not None
    adoption = adopt_source_summary_correction_candidate(
        source_registry=source_registry,
        destination_registry=dest_registry,
        workspace=dest_workspace,
        publication_context=LocalPublicationContext(),
        candidate_artifact_id=correction.candidate_artifact_id,
        expected_candidate_hash=candidate.content_hash,
        actor="synthetic-owner",
        reason="Review and adopt the synthetic source correction",
    )
    return (
        source_workspace,
        source_registry,
        source_record,
        summaries,
        pdf_path,
        pdf_hash,
        page_renders,
        dest_workspace,
        dest_registry,
        correction,
        adoption,
    )


def test_owner_correction_exports_v2_only_for_changed_paper(tmp_path: Path) -> None:
    prepared = _prepare_approved_synthetic_source(tmp_path)
    (
        _source_workspace,
        source_registry,
        _source_record,
        source_summaries,
        _pdf_path,
        _pdf_hash,
        _page_renders,
        correction_workspace,
        correction_registry,
        correction,
        adoption,
    ) = prepared
    source_registry_before = (file_sha256(source_registry.registry_path), source_registry.revision)

    result = export_owner_corrected_stage1_reuse_authority(
        origin_registry=source_registry,
        correction_registry=correction_registry,
        correction_workspace=correction_workspace,
        adoption_receipt_artifact_id=adoption.adoption_receipt_artifact_id,
        publication_context=LocalPublicationContext(),
    )

    assert result.status == "exported"
    assert result.paper_count == len(source_summaries) == 63
    assert result.unchanged_prior_manifest_count == 62
    assert result.usable_as_stage1_reuse is True
    assert result.canonical_pointer_advanced is False
    assert result.provider_calls == 0
    assert result.provider_receipt_ids_created == ()
    assert (file_sha256(source_registry.registry_path), source_registry.revision) == source_registry_before

    summary_file = correction_registry.get(result.summary_file_artifact_id)
    owner_manifest = correction_registry.get(result.owner_corrected_manifest_artifact_id)
    bundle = correction_registry.get(result.owner_import_bundle_artifact_id)
    assert summary_file is not None and owner_manifest is not None and bundle is not None
    assert summary_file.status == owner_manifest.status == bundle.status == "ready"
    assert summary_file.artifact_type == "summary_file"
    assert summary_file.artifact_version == "v1"
    assert owner_manifest.artifact_type == "stage1_reusable_summary_manifest"
    assert owner_manifest.artifact_version == "v2"
    manifest_payload = json.loads(Path(owner_manifest.path).read_text(encoding="utf-8"))
    assert manifest_payload["authority_kind"] == OWNER_APPROVED_SOURCE_CORRECTION_KIND
    assert manifest_payload["source_kind"] == OWNER_APPROVED_SOURCE_CORRECTION_KIND
    assert manifest_payload["provider_receipt_closure_id"] == ""
    assert manifest_payload["provider_receipt_closure_hash"] == ""
    assert manifest_payload["provider_receipt_ledger_id"] == ""
    assert manifest_payload["provider_receipt_ledger_hash"] == ""
    owner_authority = manifest_payload["owner_correction_authority"]
    assert owner_authority["schema_version"] == "owner-correction-authority-v1"
    assert owner_authority["adoption_receipt"]["artifact_id"] == adoption.adoption_receipt_artifact_id
    assert owner_authority["derived_summary_set"]["artifact_id"] == adoption.derived_summary_artifact_id
    assert owner_authority["origin_source"]["artifact_id"] == prepared[2].artifact_id
    assert owner_authority["candidate"]["status"] == "quarantined"
    assert owner_authority["proposal"]["status"] == "quarantined"
    source_file_rows = json.loads(Path(summary_file.path).read_text(encoding="utf-8"))
    assert len(source_file_rows) == 63
    assert all(
        row["summary_payload_hash"] == hash_json(row["ai_summary"])
        for row in source_file_rows
    )
    assert source_file_rows[32]["preserved_false"] is False
    assert source_file_rows[32]["preserved_zero"] == 0
    assert source_file_rows[32]["preserved_none"] is None
    bundle_payload = json.loads(Path(bundle.path).read_text(encoding="utf-8"))
    assert len(bundle_payload["manifest_refs"]) == 63
    assert sum(item["authority_version"] == "owner_corrected_v2" for item in bundle_payload["manifest_refs"]) == 1
    assert sum(item["authority_version"] == "typed_manifest_v1" for item in bundle_payload["manifest_refs"]) == 62
    assert correction_registry.get(correction.candidate_artifact_id).status == "quarantined"


def test_owner_correction_export_fails_when_prior_basis_is_missing_without_writes(
    tmp_path: Path,
) -> None:
    prepared = _prepare_approved_synthetic_source(tmp_path, with_prior_manifests=False)
    _source_workspace, source_registry, _source_record, _rows, _pdf, _pdf_hash, _renders, workspace, correction_registry, _plan, adoption = prepared
    source_state = (file_sha256(source_registry.registry_path), source_registry.revision)
    correction_state = (file_sha256(correction_registry.registry_path), correction_registry.revision)
    reuse_root = Path(workspace.artifact_path("stage1_summary_correction/reuse"))

    with pytest.raises(OwnerCorrectedStage1ReuseExportError, match="prior registered Stage 1 V1 manifest is missing"):
        export_owner_corrected_stage1_reuse_authority(
            origin_registry=source_registry,
            correction_registry=correction_registry,
            correction_workspace=workspace,
            adoption_receipt_artifact_id=adoption.adoption_receipt_artifact_id,
            publication_context=LocalPublicationContext(),
        )

    assert (file_sha256(source_registry.registry_path), source_registry.revision) == source_state
    assert (file_sha256(correction_registry.registry_path), correction_registry.revision) == correction_state
    assert not reuse_root.exists()


def test_owner_correction_export_rejects_ambiguous_prior_manifest_without_writes(
    tmp_path: Path,
) -> None:
    target_key = "10.5555/tripathi2017"
    prepared = _prepare_approved_synthetic_source(tmp_path, duplicate_key=target_key)
    _source_workspace, source_registry, _source_record, _rows, _pdf, _pdf_hash, _renders, workspace, correction_registry, _plan, adoption = prepared
    source_state = (file_sha256(source_registry.registry_path), source_registry.revision)
    correction_state = (file_sha256(correction_registry.registry_path), correction_registry.revision)
    reuse_root = Path(workspace.artifact_path("stage1_summary_correction/reuse"))

    with pytest.raises(OwnerCorrectedStage1ReuseExportError, match="prior registered Stage 1 V1 manifest is ambiguous"):
        export_owner_corrected_stage1_reuse_authority(
            origin_registry=source_registry,
            correction_registry=correction_registry,
            correction_workspace=workspace,
            adoption_receipt_artifact_id=adoption.adoption_receipt_artifact_id,
            publication_context=LocalPublicationContext(),
        )

    assert (file_sha256(source_registry.registry_path), source_registry.revision) == source_state
    assert (file_sha256(correction_registry.registry_path), correction_registry.revision) == correction_state
    assert not reuse_root.exists()
