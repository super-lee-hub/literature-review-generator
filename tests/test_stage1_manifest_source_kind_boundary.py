from __future__ import annotations

import json
from pathlib import Path

from runtime.provider_runtime import hash_json
from services.artifact_registry import file_sha256
from services.stage1_reuse import (
    Stage1ReusableSummaryBindingV1, Stage1ReusableSummaryManifestV1,
    build_binding_hash, verify_stage1_typed_manifest_authority,
)


def _portable_legacy_authority(tmp_path: Path, source_kind: str):
    summary = {"paper_info": {"canonical_paper_key": "paper-a"}, "ai_summary": {"summary": "Source content."}}
    summary["summary_payload_hash"] = hash_json(summary["ai_summary"])
    source = tmp_path / "summary.json"
    source.write_text(json.dumps([summary]), encoding="utf-8")
    summary_hash = hash_json(summary["ai_summary"])
    binding = Stage1ReusableSummaryBindingV1(
        canonical_paper_key="paper-a", source_authority_job_id="source-job",
        source_authority_artifact_id="summary-source", source_authority_artifact_hash=file_sha256(source),
        source_authority_artifact_path=str(source), source_authority_registry_id="artifact-registry:source-job",
        source_authority_registry_revision="1", source_kind=source_kind,
        summary_payload_hash=summary_hash, normalized_summary_payload_hash=summary_hash,
    )
    manifest = Stage1ReusableSummaryManifestV1(
        job_id="source-job", canonical_paper_key="paper-a", source_kind=source_kind,
        source_summary_artifact_id=binding.source_authority_artifact_id,
        source_summary_artifact_hash=binding.source_authority_artifact_hash,
        source_summary_artifact_path=str(source),
        source_registry_identity=binding.source_authority_registry_id,
        source_registry_revision=binding.source_authority_registry_revision,
        summary_payload_hash=summary_hash, normalized_summary_payload_hash=summary_hash,
        binding=binding.to_dict(), binding_hash=build_binding_hash(binding.to_dict()),
        paper_info=summary["paper_info"], summary_payload=summary["ai_summary"],
    ).to_dict()
    manifest["manifest_content_hash"] = hash_json({**manifest, "manifest_content_hash": ""})
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    summary["stage1_reuse"] = {
        "authority_kind": "typed_manifest", "typed_manifest_path": str(path),
        "typed_manifest_artifact_id": "manifest-source", "typed_manifest_artifact_hash": file_sha256(path),
        "binding": binding.to_dict(),
    }
    return summary, binding


def test_arbitrary_source_kind_cannot_remove_required_authority(tmp_path: Path) -> None:
    summary, binding = _portable_legacy_authority(tmp_path, "arbitrary-no-provider-authority")
    authority, reason = verify_stage1_typed_manifest_authority(summary, binding)
    assert authority is None, "unrecognized source kind must not bypass provenance authority"
    assert reason == "typed_manifest_source_kind_untrusted"


def test_genuine_legacy_manifest_remains_a_separate_supported_contract(tmp_path: Path) -> None:
    summary, binding = _portable_legacy_authority(tmp_path, "")
    authority, reason = verify_stage1_typed_manifest_authority(summary, binding)
    assert authority is not None, reason
