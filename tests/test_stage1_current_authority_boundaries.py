from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from runtime.artifact_validators import ArtifactSchemaError, validate_registered_artifact
from runtime.provider_runtime import hash_json
from services.artifact_registry import ArtifactRegistry, UnverifiedArtifact, file_sha256
from services.stage1_reuse import (
    Stage1ReusableSummaryBindingV1,
    Stage1ReusableSummaryManifestV1,
    Stage1VisualEvidenceQualificationV1,
    _validate_manifest_self_binding,
    build_binding_hash,
)


def _qualification() -> dict[str, Any]:
    return Stage1VisualEvidenceQualificationV1(
        require_complete_visual_coverage=False,
        required_nonblank_page_count=1,
        required_page_ids=("page-004",),
        sent_page_ids=("page-004",),
        observed_page_ids=("page-004",),
        scan_coverage_status="complete",
        final_raw_visual_recheck_status="partial",
        evidence_coverage_status="degraded",
        visual_observation_artifact_version="v2",
        visual_scan_prompt_id="stage1.visual_scan.system.v2",
        visual_scan_prompt_version="v2",
        visual_scan_prompt_sha256="a" * 64,
        visual_scan_schema_hash="b" * 64,
        required_raw_reinspection_unit_count=1,
        closed_raw_reinspection_unit_count=0,
        unresolved_raw_reinspection_unit_ids=("ambiguous-page-4",),
        raw_reinspection_units=(
            {"unit_id": "ambiguous-page-4", "closed": False},
        ),
    ).to_dict()


def _typed_manifest_fixture() -> tuple[dict[str, Any], Stage1ReusableSummaryBindingV1, dict[str, Any]]:
    summary_payload = {"summary": "authoritative"}
    summary_hash = hash_json(summary_payload)
    binding = Stage1ReusableSummaryBindingV1(
        canonical_paper_key="paper-a",
        source_paper_id="paper-a",
        source_mode="direct",
        source_pdf_content_sha256="1" * 64,
        stage1_extracted_text_hash="2" * 64,
        stage1_semantic_input_hash="3" * 64,
        preprocess_contract_hash="4" * 64,
        prompt_id="stage1.analysis.user.v3",
        prompt_version="v3",
        prompt_sha256="5" * 64,
        prompt_template_hash="6" * 64,
        input_builder_policy_hash="7" * 64,
        summary_schema_hash="8" * 64,
        visual_input_manifest_hash="9" * 64,
        visual_coverage_hash="a" * 64,
        visual_scan_schema_hash="b" * 64,
        visual_evidence_qualification=_qualification(),
        source_authority_job_id="parent-job",
        source_authority_registry_id="artifact-registry:parent-job",
        source_authority_registry_revision="1",
        source_authority_artifact_id="summary:parent",
        source_authority_artifact_hash="c" * 64,
        normalized_summary_payload_hash=summary_hash,
        summary_payload_hash=summary_hash,
    )
    binding_payload = binding.to_dict()
    manifest = Stage1ReusableSummaryManifestV1(
        job_id="parent-job",
        stage_name="stage1_analyze",
        canonical_paper_key="paper-a",
        source_paper_id="paper-a",
        source_summary_artifact_id=binding.source_authority_artifact_id,
        source_summary_artifact_hash=binding.source_authority_artifact_hash,
        summary_payload_hash=summary_hash,
        normalized_summary_payload_hash=summary_hash,
        binding_hash=build_binding_hash(binding_payload),
        source_pdf_content_sha256=binding.source_pdf_content_sha256,
        stage1_extracted_text_hash=binding.stage1_extracted_text_hash,
        stage1_semantic_input_hash=binding.stage1_semantic_input_hash,
        preprocess_contract_hash=binding.preprocess_contract_hash,
        prompt_id=binding.prompt_id,
        prompt_version=binding.prompt_version,
        prompt_sha256=binding.prompt_sha256,
        prompt_template_hash=binding.prompt_template_hash,
        input_builder_policy_hash=binding.input_builder_policy_hash,
        summary_schema_hash=binding.summary_schema_hash,
        visual_input_manifest_hash=binding.visual_input_manifest_hash,
        visual_coverage_hash=binding.visual_coverage_hash,
        visual_scan_schema_hash=binding.visual_scan_schema_hash,
        visual_evidence_qualification=binding.visual_evidence_qualification,
        source_registry_identity=binding.source_authority_registry_id,
        source_registry_revision=binding.source_authority_registry_revision,
        source_kind="stage1_provider_generated",
        binding=binding_payload,
        paper_info={"canonical_paper_key": "paper-a"},
        summary_payload=summary_payload,
    )
    payload = manifest.to_dict()
    payload["manifest_content_hash"] = hash_json(
        {**payload, "manifest_content_hash": ""}
    )
    previous_summary = {
        "paper_info": {"canonical_paper_key": "paper-a"},
        "ai_summary": summary_payload,
    }
    return payload, binding, previous_summary


def _recompute_manifest_hashes(payload: dict[str, Any]) -> None:
    binding = payload.get("binding")
    assert isinstance(binding, dict)
    payload["binding_hash"] = build_binding_hash(binding)
    payload["manifest_content_hash"] = hash_json(
        {**payload, "manifest_content_hash": ""}
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "top_missing_nested_empty",
        "top_empty_nested_empty",
        "top_valid_nested_missing",
        "top_missing_nested_valid",
        "top_nested_mismatch",
    ],
)
def test_current_typed_manifest_requires_self_bound_visual_qualification(
    mutation: str,
) -> None:
    payload, binding, previous_summary = _typed_manifest_fixture()
    mutated = deepcopy(payload)
    nested = mutated["binding"]
    assert isinstance(nested, dict)
    if mutation == "top_missing_nested_empty":
        mutated.pop("visual_evidence_qualification", None)
        nested["visual_evidence_qualification"] = {}
    elif mutation == "top_empty_nested_empty":
        mutated["visual_evidence_qualification"] = {}
        nested["visual_evidence_qualification"] = {}
    elif mutation == "top_valid_nested_missing":
        nested.pop("visual_evidence_qualification", None)
    elif mutation == "top_missing_nested_valid":
        mutated.pop("visual_evidence_qualification", None)
    else:
        top = dict(mutated["visual_evidence_qualification"])
        top["visual_scan_schema_hash"] = "d" * 64
        mutated["visual_evidence_qualification"] = top
    _recompute_manifest_hashes(mutated)

    if mutation == "top_nested_mismatch":
        expected_binding = Stage1ReusableSummaryBindingV1.from_mapping(nested)
    else:
        expected_binding = binding
    manifest, reason = _validate_manifest_self_binding(
        mutated,
        binding=expected_binding,
        previous_summary=previous_summary,
    )

    assert manifest is None
    assert reason in {
        "typed_manifest_visual_evidence_qualification_missing",
        "typed_manifest_visual_evidence_qualification_mismatch",
    }


_MISSING = object()


def _registry_manifest_payload(qualification: object = _MISSING) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "artifact_type": "stage1_reusable_summary_manifest",
        "artifact_version": "v1",
        "job_id": "job-1",
        "stage_name": "stage1_analyze",
        "canonical_paper_key": "paper-a",
        "source_paper_id": "paper-a",
        "source_summary_artifact_id": "summary:1",
        "source_summary_artifact_hash": "1" * 64,
        "summary_payload_hash": "2" * 64,
        "binding_hash": "3" * 64,
        "runtime_spec_id": "runtime:1",
        "runtime_spec_hash": "4" * 64,
        "evidence_manifest_id": "evidence:1",
        "evidence_manifest_hash": "5" * 64,
        "source_bundle_id": "bundle:1",
        "source_bundle_hash": "6" * 64,
        "created_at": "2026-08-24T00:00:00Z",
        "producer": "tests",
    }
    if qualification is not _MISSING:
        payload["visual_evidence_qualification"] = qualification
    return payload


@pytest.mark.parametrize("qualification", [_MISSING, {}])
def test_registry_ready_rejects_current_manifest_without_visual_qualification(
    tmp_path: Path,
    qualification: object,
) -> None:
    payload = _registry_manifest_payload(qualification)
    payload.update(
        {
            "prompt_id": "stage1.analysis.user.v3",
            "prompt_version": "v3",
            "prompt_sha256": "7" * 64,
            "visual_coverage_hash": "8" * 64,
            "visual_scan_schema_hash": "9" * 64,
            "binding": {"visual_scan_schema_hash": "9" * 64},
        }
    )
    path = tmp_path / "current-manifest.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    registry = ArtifactRegistry(tmp_path / "registry.json", "job-1")

    with pytest.raises(UnverifiedArtifact, match="visual_evidence_qualification"):
        registry.register_file(
            artifact_id="manifest:current",
            artifact_role="summary_manifest",
            artifact_type="stage1_reusable_summary_manifest",
            artifact_version="v1",
            path=path,
            producer="tests",
        )


def test_registry_ready_preserves_genuine_legacy_manifest_without_current_markers(
    tmp_path: Path,
) -> None:
    path = tmp_path / "legacy-manifest.json"
    path.write_text(json.dumps(_registry_manifest_payload()), encoding="utf-8")
    registry = ArtifactRegistry(tmp_path / "registry.json", "job-1")

    record = registry.register_file(
        artifact_id="manifest:legacy",
        artifact_role="summary_manifest",
        artifact_type="stage1_reusable_summary_manifest",
        artifact_version="v1",
        path=path,
        producer="tests",
    )

    assert record.status == "ready"


def _portable_manifest_payload(*, current: bool) -> dict[str, Any]:
    binding: dict[str, Any] = {"canonical_paper_key": "paper-a"}
    if current:
        binding["visual_coverage_hash"] = "a" * 64
    summary_payload: dict[str, Any] = {}
    payload: dict[str, Any] = {
        "artifact_type": "stage1_reusable_summary_manifest",
        "artifact_version": "v1",
        "job_id": "job-1",
        "stage_name": "stage1_analyze",
        "canonical_paper_key": "paper-a",
        "source_summary_artifact_id": "summary:1",
        "source_summary_artifact_hash": "b" * 64,
        "summary_payload": summary_payload,
        "summary_payload_hash": hash_json(summary_payload),
        "normalized_summary_payload_hash": hash_json(summary_payload),
        "binding": binding,
        "binding_hash": hash_json(binding),
        "manifest_content_hash": "",
        "provider_receipt_closure_id": "",
        "provider_receipt_closure_hash": "",
        "provider_receipt_ledger_id": "",
        "provider_receipt_ledger_hash": "",
    }
    payload["manifest_content_hash"] = hash_json(payload)
    return payload


@pytest.mark.parametrize("current", [True, False])
def test_portable_manifest_current_proof_removal_fails_but_legacy_remains_supported(
    tmp_path: Path,
    current: bool,
) -> None:
    payload = _portable_manifest_payload(current=current)
    path = tmp_path / ("current-portable.json" if current else "legacy-portable.json")
    path.write_text(json.dumps(payload), encoding="utf-8")
    record = SimpleNamespace(
        artifact_type="stage1_portable_summary_manifest",
        artifact_version="v1",
        job_id="job-1",
        content_hash=file_sha256(path),
        metadata={
            "authority_kind": "typed_manifest",
            "stage_name": "stage1_analyze",
            "source_authority_job_id": "job-1",
            "original_artifact_id": "manifest:1",
            "original_artifact_hash": file_sha256(path),
            "typed_manifest_artifact_id": "manifest:1",
            "typed_manifest_artifact_hash": file_sha256(path),
        },
    )

    if current:
        with pytest.raises(ArtifactSchemaError, match="visual_evidence_qualification"):
            validate_registered_artifact(record, path)
    else:
        validate_registered_artifact(record, path)
