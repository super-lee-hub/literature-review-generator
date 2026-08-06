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
