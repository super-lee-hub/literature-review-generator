from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

import fitz  # type: ignore

from runtime.provider_runtime import ProviderRuntimeLedger
from runtime.stage_contracts import build_source_bundle
from services.artifact_registry import ArtifactRegistry
from services.job_workspace import JobWorkspace
from services.settings import ApplicationSettings
from services.stage1_analysis_service import Stage1AnalysisService
from summary_schema import normalize_ai_summary


def _write_pdf(path: Path) -> None:
    document = fitz.open()
    page = document.new_page()
    page.insert_text(
        (72, 72),
        "Title: Evidence-bound study\n"
        "Methodology: A controlled experiment with N=120 observations.\n"
        "Results: The treatment improved the outcome by 15 percent (p < 0.01).\n"
        "Conclusion: The result supports the proposed mechanism under the tested context.",
    )
    document.save(path)
    document.close()


def _canonical_summary() -> dict[str, Any]:
    return normalize_ai_summary(
        {
            "common_core": {
                "title": "Evidence-bound study",
                "authors": ["Example Author"],
                "year": "2025",
                "summary": "The study tests a treatment with a controlled experiment and reports a measurable improvement.",
                "key_points": ["The treatment improved the outcome by 15 percent."],
                "methodology": "A controlled experiment with N=120 observations.",
                "findings": "The treatment improved the outcome by 15 percent (p < 0.01).",
                "conclusions": "The result supports the proposed mechanism under the tested context.",
            },
            "type_specific_details": {
                "paper_type": "empirical",
                "data_source_and_size": "Controlled experiment, N=120.",
                "analysis_technique": "Group comparison with significance testing.",
            },
        }
    )


def _service(tmp_path: Path, pdf_path: Path, reader: Any) -> tuple[Stage1AnalysisService, Any]:
    config = {
        "Paths": {"output_path": str(tmp_path / "output")},
        "Preprocess": {"enabled": "true", "cache_dir": str(tmp_path / "cache")},
        "Stage1_Input": {
            "send_extracted_text": "true",
            "send_selected_visuals": "false",
            "send_original_pdf": "never",
        },
        "Stage1_Visual": {"enabled": "false"},
        "Primary_Reader_API": {"api_key": "reader", "model": "reader-test", "api_base": "https://reader.test/v1"},
        "Backup_Reader_API": {"api_key": "backup", "model": "backup-test", "api_base": "https://backup.test/v1"},
        "Runtime": {"node_retry_limit": "1"},
    }
    workspace = JobWorkspace.create(str(tmp_path / "output"), "current", job_id="stage1-job")
    registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    registry.register_file(
        artifact_role="source_pdf",
        artifact_type="source_pdf",
        artifact_version="v1",
        path=pdf_path,
        producer="test",
        artifact_id=f"source_pdf:{pdf_path.name}",
    )
    settings = ApplicationSettings.from_config(config)
    service = Stage1AnalysisService(
        job_id=workspace.job_id,
        attempt_id="attempt-1",
        workspace=workspace,
        artifact_registry=registry,
        config=config,
        settings=settings,
        reader=reader,
    )
    bundle = build_source_bundle(
        source_mode="direct",
        project_name="current",
        papers=[
            {
                "title": "Evidence-bound study",
                "authors": ["Example Author"],
                "year": "2025",
                "pdf_path": str(pdf_path),
                "source_paper_id": "paper-1",
            }
        ],
    )
    return service, bundle


def test_current_stage1_generates_canonical_summary_and_receipt(tmp_path: Path) -> None:
    pdf_path = tmp_path / "paper.pdf"
    _write_pdf(pdf_path)
    calls: list[Mapping[str, Any]] = []

    def reader(**kwargs: Any) -> Mapping[str, Any]:
        calls.append(kwargs)
        return {"status": "success", "content": _canonical_summary()}

    service, bundle = _service(tmp_path, pdf_path, reader)
    result = service.run(bundle)

    assert result.generated_count == 1
    assert result.reused_count == 0
    assert len(calls) == 1
    summary = result.summaries[0]
    assert summary["status"] == "success"
    assert summary["ai_summary"]["schema_version"] == "summary_v2_lite"
    assert summary["stage1_input"]["input_mode"] == "text_only"
    assert result.receipt_ids
    ledger = ProviderRuntimeLedger(result.receipt_ledger_path)
    assert len(ledger.list_receipts()) == 1


def test_current_stage1_resume_reuses_matching_summary_without_provider_call(tmp_path: Path) -> None:
    pdf_path = tmp_path / "paper.pdf"
    _write_pdf(pdf_path)
    calls: list[int] = []

    def reader(**kwargs: Any) -> Mapping[str, Any]:
        calls.append(1)
        return {"status": "success", "content": _canonical_summary()}

    service, bundle = _service(tmp_path, pdf_path, reader)
    first = service.run(bundle)
    second = service.run(bundle, existing_summaries=first.summaries)

    assert len(calls) == 1
    assert second.generated_count == 0
    assert second.reused_count == 1
    assert second.summaries[0]["ai_summary"] == first.summaries[0]["ai_summary"]


def test_current_stage1_rejects_placeholder_provider_output(tmp_path: Path) -> None:
    pdf_path = tmp_path / "paper.pdf"
    _write_pdf(pdf_path)

    def reader(**kwargs: Any) -> Mapping[str, Any]:
        summary = _canonical_summary()
        summary["core_analysis"]["findings"] = "Dummy findings."
        return {"status": "success", "content": summary}

    service, bundle = _service(tmp_path, pdf_path, reader)
    try:
        service.run(bundle)
    except RuntimeError as exc:
        assert "placeholder" in str(exc)
    else:
        raise AssertionError("placeholder Stage 1 output was accepted")
