from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

import fitz

from runtime.job_spec import RuntimeJobSpec, RuntimeSourceSpec
from runtime.stage_planning import build_stage_plan
from services.stage1_analysis_service import Stage1AnalysisService
from services.artifact_registry import ArtifactRegistry
from services.job_workspace import JobWorkspace
from services.settings import ApplicationSettings
from services.stage1_reuse import Stage1ReusableSummaryBindingV1
from runtime.stage_contracts import build_source_bundle


def _config(tmp_path: Path) -> dict[str, Any]:
    return {
        "Paths": {"output_path": str(tmp_path / "output")},
        "Preprocess": {"enabled": "true", "cache_dir": str(tmp_path / "cache")},
        "Stage1_Input": {
            "send_extracted_text": "true",
            "send_selected_visuals": "false",
            "send_original_pdf": "never",
        },
        "Stage1_Visual": {"enabled": "false"},
        "Multimodal": {"enabled": "false"},
        "Primary_Reader_API": {"model": "test-model", "endpoint_type": "chat_completions"},
        "Backup_Reader_API": {"model": "backup-model", "endpoint_type": "chat_completions"},
    }


def _pdf(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 72), text)
    document.save(path)
    document.close()


def _summary() -> Mapping[str, Any]:
    return {
        "schema_version": "summary_v2_lite",
        "paper_metadata": {"title": "Verified paper", "doi": "10.1000/verified"},
        "core_analysis": {
            "summary": "A substantive summary.",
            "methodology": "A substantive method.",
            "findings": "A substantive finding.",
            "conclusions": "A substantive conclusion.",
        },
    }


def _service(tmp_path: Path, pdf_path: Path, reader_calls: list[int]) -> tuple[Stage1AnalysisService, Any]:
    workspace = JobWorkspace.create(str(tmp_path / "output"), "reuse", "reuse-job")
    registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)

    def reader(**_kwargs: Any) -> Mapping[str, Any]:
        reader_calls.append(1)
        return {"status": "success", "content": _summary()}

    service = Stage1AnalysisService(
        job_id=workspace.job_id,
        attempt_id="attempt-1",
        workspace=workspace,
        artifact_registry=registry,
        config=_config(tmp_path),
        settings=ApplicationSettings.from_config(_config(tmp_path)),
        reader=reader,
    )
    bundle = build_source_bundle(
        source_mode="direct",
        project_name="reuse",
        papers=[
            {
                "title": "Verified paper",
                "doi": "10.1000/verified",
                "pdf_path": str(pdf_path),
            }
        ],
    )
    return service, bundle


def test_same_identity_but_changed_pdf_is_regenerated(tmp_path: Path) -> None:
    pdf_path = tmp_path / "paper.pdf"
    _pdf(pdf_path, "original bytes")
    calls: list[int] = []
    service, bundle = _service(tmp_path, pdf_path, calls)
    first = service.run(bundle)

    _pdf(pdf_path, "changed bytes")
    second = service.run(bundle, existing_summaries=first.summaries)

    assert len(calls) == 2
    assert second.reused_count == 0
    assert second.generated_count == 1
    assert second.summaries[0]["stage1_reuse"]["decision"] in {
        "identity_match_but_stale",
        "regenerate_required",
    }


def test_bare_matching_summary_is_unverified_and_regenerated_with_pdf(tmp_path: Path) -> None:
    pdf_path = tmp_path / "paper.pdf"
    _pdf(pdf_path, "available source")
    calls: list[int] = []
    service, bundle = _service(tmp_path, pdf_path, calls)
    bare = {
        "status": "success",
        "paper_info": {
            "canonical_paper_key": "10.1000/verified",
            "source_paper_id": "10.1000/verified",
            "title": "Verified paper",
        },
        "ai_summary": dict(_summary()),
    }

    result = service.run(bundle, existing_summaries=(bare,))

    assert len(calls) == 1
    assert result.reused_count == 0
    assert result.generated_count == 1
    assert result.summaries[0]["stage1_reuse"]["decision"] == "identity_match_unverified"


def test_runtime_job_spec_preserves_omitted_validation_policy_as_none() -> None:
    spec = RuntimeJobSpec(
        project_name="demo",
        source=RuntimeSourceSpec(mode="direct", pdf_folder="C:/papers"),
        action="run_all",
        metadata={},
    )

    request = spec.to_job_request()

    assert request.validation_required is None
    assert request.require_clean_validation is None
    assert request.allow_unvalidated_when_validation_optional is None
    plan = build_stage_plan(
        action="run_all",
        requested_stages=None,
        validation_enabled=True,
        validation_required=request.validation_required,
        require_clean_validation=request.require_clean_validation,
        allow_unvalidated_when_validation_optional=request.allow_unvalidated_when_validation_optional,
    )
    assert plan.requested_stages == ("analyze", "outline", "review", "validate")
    assert plan.validation_required is True
    assert plan.require_clean_validation is True
    assert plan.allow_unvalidated_when_validation_optional is False


def test_reuse_contract_does_not_accept_a_current_run_snapshot_as_authority() -> None:
    from services.stage1_reuse import Stage1ReuseEligibilityV1

    decision = Stage1ReuseEligibilityV1(
        decision="identity_match_unverified",
        canonical_paper_key="10.1000/verified",
        reason="current_run_snapshot_is_not_prior_authority",
        original_source_binding={},
        current_source_binding={},
        reuse_comparison={},
    )

    assert decision.reusable is False
    assert decision.to_dict()["decision"] == "identity_match_unverified"


def test_reuse_binding_allows_provider_to_be_omitted_on_both_runs() -> None:
    from services.stage1_reuse import Stage1ReusableSummaryBindingV1

    original = Stage1ReusableSummaryBindingV1(
        canonical_paper_key="10.1000/verified",
        source_paper_id="paper-1",
        source_mode="direct",
        source_pdf_hash="pdf-hash",
        source_pdf_fingerprint="pdf-fingerprint",
        preprocess_hash="preprocess-hash",
        stage1_input_hash="input-hash",
        prompt_hash="prompt-hash",
        builder_version="Stage1InputBuilder:v1",
        model="model-1",
        endpoint_type="chat_completions",
        provider_config_hash="config-hash",
        schema_hash="schema-hash",
        visual_provenance_hash="visual-hash",
    )

    comparison = original.compare(original)

    assert comparison["equal"] is True
    assert comparison["missing_fields"] == []


def test_reuse_binding_separates_pdf_bytes_from_semantic_input_and_ignores_direct_path_move() -> None:
    common = {
        "canonical_paper_key": "10.1000/verified",
        "source_mode": "direct",
        "source_pdf_content_sha256": "a" * 64,
        "stage1_extracted_text_hash": "b" * 64,
        "stage1_semantic_input_hash": "c" * 64,
        "preprocess_contract_hash": "d" * 64,
        "prompt_template_hash": "e" * 64,
        "input_builder_policy_hash": "f" * 64,
        "provider_config_hash": "1" * 64,
        "summary_schema_hash": "2" * 64,
        "visual_input_manifest_hash": "3" * 64,
    }
    original = Stage1ReusableSummaryBindingV1(
        **common,
        source_paper_id=r"D:\papers\a.pdf",
        source_pdf=r"D:\papers\a.pdf",
        original_source_location=r"D:\papers\a.pdf",
        current_source_location=r"D:\papers\a.pdf",
    )
    moved = Stage1ReusableSummaryBindingV1(
        **common,
        source_paper_id=r"E:\library\a.pdf",
        source_pdf=r"E:\library\a.pdf",
        original_source_location=r"D:\papers\a.pdf",
        current_source_location=r"E:\library\a.pdf",
        location_changed=True,
    )

    comparison = original.compare(moved)

    assert comparison["equal"] is True
    assert original.source_pdf_content_sha256 != original.stage1_semantic_input_hash
    assert comparison["current"]["source_pdf_content_sha256"] == "a" * 64


def test_reuse_binding_invalidates_different_pdf_bytes_with_same_semantic_input() -> None:
    common = {
        "canonical_paper_key": "10.1000/verified",
        "source_mode": "direct",
        "source_pdf_content_sha256": "a" * 64,
        "stage1_extracted_text_hash": "b" * 64,
        "stage1_semantic_input_hash": "c" * 64,
        "preprocess_contract_hash": "d" * 64,
        "prompt_template_hash": "e" * 64,
        "input_builder_policy_hash": "f" * 64,
        "provider_config_hash": "1" * 64,
        "summary_schema_hash": "2" * 64,
        "visual_input_manifest_hash": "3" * 64,
    }
    original = Stage1ReusableSummaryBindingV1(**common)
    changed = Stage1ReusableSummaryBindingV1(**{**common, "source_pdf_content_sha256": "9" * 64})

    comparison = original.compare(changed)

    assert comparison["equal"] is False
    assert "source_pdf_content_sha256" in comparison["mismatches"]


def test_reuse_binding_compares_authority_payload_and_provider_closure_facts() -> None:
    common = {
        "canonical_paper_key": "10.1000/verified",
        "source_mode": "direct",
        "source_pdf_content_sha256": "a" * 64,
        "stage1_extracted_text_hash": "b" * 64,
        "stage1_semantic_input_hash": "c" * 64,
        "preprocess_contract_hash": "d" * 64,
        "prompt_template_hash": "e" * 64,
        "input_builder_policy_hash": "f" * 64,
        "provider_config_hash": "1" * 64,
        "summary_schema_hash": "2" * 64,
        "visual_input_manifest_hash": "3" * 64,
        "normalized_summary_payload_hash": "4" * 64,
        "summary_payload_hash": "4" * 64,
        "source_authority_job_id": "parent-job",
        "source_authority_registry_id": "parent-registry",
        "source_authority_registry_revision": "7",
        "source_authority_artifact_id": "parent:summary",
        "source_authority_artifact_hash": "5" * 64,
        "source_summary_manifest_id": "parent:manifest",
        "source_summary_manifest_hash": "6" * 64,
        "source_provider_receipt_closure_id": "parent:closure",
        "source_provider_receipt_closure_hash": "7" * 64,
        "source_provider_receipt_ledger_id": "parent:ledger",
        "source_provider_receipt_ledger_hash": "8" * 64,
    }
    original = Stage1ReusableSummaryBindingV1(
        **common,
        extra={"source_kind": "stage1_provider_generated", "provider_transport_count": 1},
    )
    changed = Stage1ReusableSummaryBindingV1(
        **{**common, "source_provider_receipt_closure_hash": "9" * 64}
    )

    comparison = original.compare(changed)

    assert comparison["equal"] is False
    assert "source_provider_receipt_closure_hash" in comparison["mismatches"]


def test_provider_generated_authority_without_original_closure_is_not_exact_reuse(
    tmp_path: Path,
) -> None:
    from runtime.provider_runtime import hash_json
    from services.stage1_reuse import evaluate_stage1_reuse

    ai_summary = {"summary": "authoritative"}
    authority_path = tmp_path / "authority.json"
    authority_path.write_text(
        json.dumps(
            [
                {
                    "artifact_type": "summary_file",
                    "artifact_version": "v1",
                    "source_kind": "stage1_provider_generated",
                    "job_id": "parent-job",
                    "paper_info": {"canonical_paper_key": "10.1000/verified"},
                    "ai_summary": ai_summary,
                    "summary_payload_hash": hash_json(ai_summary),
                }
            ]
        ),
        encoding="utf-8",
    )
    parent_registry = ArtifactRegistry(str(tmp_path / "parent-registry.json"), "parent-job")
    authority_record = parent_registry.register_file(
        artifact_role="summary_source",
        artifact_type="summary_file",
        artifact_version="v1",
        path=authority_path,
        producer="test",
        artifact_id="parent:summary",
    )
    binding = Stage1ReusableSummaryBindingV1(
        canonical_paper_key="10.1000/verified",
        source_mode="direct",
        source_pdf_content_sha256="a" * 64,
        stage1_extracted_text_hash="b" * 64,
        stage1_semantic_input_hash="c" * 64,
        preprocess_contract_hash="d" * 64,
        prompt_template_hash="e" * 64,
        input_builder_policy_hash="f" * 64,
        provider_config_hash="1" * 64,
        summary_schema_hash="2" * 64,
        visual_input_manifest_hash="3" * 64,
        normalized_summary_payload_hash=hash_json(ai_summary),
        summary_payload_hash=hash_json(ai_summary),
        source_authority_job_id="parent-job",
        source_authority_registry_id="parent-registry",
        source_authority_registry_revision=str(parent_registry.revision),
        source_authority_artifact_id=authority_record.artifact_id,
        source_authority_artifact_hash=authority_record.content_hash,
        source_authority_artifact_path=str(authority_path),
        source_authority_registry_path=str(parent_registry.registry_path),
        extra={"source_kind": "stage1_provider_generated", "provider_transport_count": 1},
    )
    imported = {
        "status": "success",
        "paper_info": {"canonical_paper_key": "10.1000/verified"},
        "ai_summary": ai_summary,
        "provider": {"transport_count": 1},
        "stage1_reuse": {"binding": binding.to_dict()},
    }

    result = evaluate_stage1_reuse(
        imported,
        binding,
        registry=ArtifactRegistry(str(tmp_path / "child-registry.json"), "child-job"),
        external_registry_resolver=lambda job_id: (
            parent_registry if job_id == "parent-job" else None
        ),
    )

    assert result.reusable is False
    assert result.decision == "identity_match_unverified"
    assert "closure" in result.reason or "provider" in result.reason


def test_reuse_requires_authority_payload_to_match_imported_summary(tmp_path: Path) -> None:
    from services.stage1_reuse import evaluate_stage1_reuse

    authority_path = tmp_path / "authority.json"
    authority_payload = {
        "artifact_type": "summary_file",
        "artifact_version": "v1",
        "source_kind": "stage1_provider_generated",
        "job_id": "parent-job",
        "paper_info": {"canonical_paper_key": "10.1000/verified"},
        "ai_summary": {"summary": "authoritative"},
    }
    authority_path.write_text(json.dumps([authority_payload]), encoding="utf-8")
    registry = ArtifactRegistry(str(tmp_path / "registry.json"), "child-job")
    # The authority is intentionally represented as a parent-owned record;
    # evaluate_stage1_reuse must inspect its payload rather than trust a path.
    parent_registry = ArtifactRegistry(str(tmp_path / "parent-registry.json"), "parent-job")
    authority_record = parent_registry.register_file(
        artifact_role="summary_source",
        artifact_type="summary_file",
        artifact_version="v1",
        path=authority_path,
        producer="test",
        artifact_id="parent:summary",
    )
    binding = Stage1ReusableSummaryBindingV1(
        canonical_paper_key="10.1000/verified",
        source_mode="direct",
        source_pdf_content_sha256="a" * 64,
        stage1_extracted_text_hash="b" * 64,
        stage1_semantic_input_hash="c" * 64,
        preprocess_contract_hash="d" * 64,
        prompt_template_hash="e" * 64,
        input_builder_policy_hash="f" * 64,
        provider_config_hash="1" * 64,
        summary_schema_hash="2" * 64,
        visual_input_manifest_hash="3" * 64,
        source_authority_job_id="parent-job",
        source_authority_artifact_id=authority_record.artifact_id,
        source_authority_artifact_hash=authority_record.content_hash,
        source_authority_artifact_path=str(authority_path),
        source_authority_registry_path=str(parent_registry.registry_path),
    )
    imported = {
        "status": "success",
        "paper_info": {"canonical_paper_key": "10.1000/verified"},
        "ai_summary": {"summary": "tampered import"},
        "stage1_reuse": {"binding": binding.to_dict()},
    }
    current = binding

    result = evaluate_stage1_reuse(
        imported,
        current,
        registry=registry,
        external_registry_resolver=lambda job_id: parent_registry if job_id == "parent-job" else None,
    )

    assert result.reusable is False
    assert result.decision == "identity_match_unverified"
    assert "payload" in result.reason
