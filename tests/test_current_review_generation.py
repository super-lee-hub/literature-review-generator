from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

import pytest

from runtime.stage_contracts import build_source_bundle
from services.artifact_registry import ArtifactRegistry
from services.job_workspace import JobWorkspace
from services.review_generation_service import ReviewGenerationService
from services.settings import ApplicationSettings
from services.stage1_analysis_service import Stage1AnalysisService

from test_current_stage1_generation import _canonical_summary, _write_pdf


def _stage1_summary(tmp_path: Path) -> tuple[dict[str, Any], Path]:
    pdf_path = tmp_path / "review-paper.pdf"
    _write_pdf(pdf_path)
    config = {
        "Preprocess": {"enabled": "true", "cache_dir": str(tmp_path / "cache")},
        "Stage1_Visual": {"enabled": "false"},
        "Stage1_Input": {"send_extracted_text": "true", "send_selected_visuals": "false", "send_original_pdf": "never"},
        "Primary_Reader_API": {"api_key": "reader", "model": "reader", "api_base": "https://reader.test/v1"},
        "Backup_Reader_API": {"api_key": "backup", "model": "backup", "api_base": "https://backup.test/v1"},
        "Writer_API": {"api_key": "writer", "model": "writer", "api_base": "https://writer.test/v1"},
    }
    workspace = JobWorkspace.create(str(tmp_path / "output"), "current", job_id="review-job")
    registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    registry.register_file(
        artifact_role="source_pdf", artifact_type="source_pdf", artifact_version="v1",
        path=pdf_path, producer="test", artifact_id=f"source_pdf:{pdf_path.name}",
    )
    service = Stage1AnalysisService(
        job_id=workspace.job_id,
        attempt_id="attempt-1",
        workspace=workspace,
        artifact_registry=registry,
        config=config,
        settings=ApplicationSettings.from_config(config),
        reader=lambda **kwargs: {"status": "success", "content": _canonical_summary()},
    )
    bundle = build_source_bundle(
        source_mode="direct", project_name="current",
        papers=[{"title": "Evidence-bound study", "pdf_path": str(pdf_path)}],
    )
    result = service.run(bundle)
    return result.summaries[0], pdf_path


def test_current_review_writer_consumes_packet_and_emits_structured_citation(tmp_path: Path) -> None:
    summary, _pdf_path = _stage1_summary(tmp_path)
    config = {
        "Writer_API": {"api_key": "writer", "model": "writer", "api_base": "https://writer.test/v1"},
    }
    workspace = JobWorkspace.create(str(tmp_path / "review-output"), "current", job_id="writer-job")
    registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    seen: list[Mapping[str, Any]] = []

    def writer(**kwargs: Any) -> Mapping[str, Any]:
        seen.append(kwargs)
        return {
            "status": "success",
            "content": {
                "blocks": [
                    {"text": "The controlled result supports the mechanism [[cite_ref:R001]]."}
                ]
            },
        }

    service = ReviewGenerationService(
        job_id=workspace.job_id,
        attempt_id="attempt-1",
        workspace=workspace,
        artifact_registry=registry,
        settings=ApplicationSettings.from_config(config),
        summaries=[summary],
        writer=writer,
    )
    paper_key = summary["paper_info"]["canonical_paper_key"]
    result = service.run(
        outline_payload={
            "title": "Evidence-led review",
            "sections": [{"section_id": "section_1", "title": "Results", "goal": "Synthesize the result"}],
        },
        evidence_packets=[
            {
                "section_id": "section_1",
                "section_goal": "Synthesize the result",
                "planned_claims": ["The treatment improves the outcome."],
                "paper_keys": [paper_key],
                "source_summary_hashes": ["summary-hash"],
                "retrieval_provenance": {"source": "stage1_summary", "paper_keys": [paper_key]},
            }
        ],
    )

    assert len(seen) == 1
    assert seen[0]["evidence_packet"]["paper_keys"] == [paper_key]
    block = result.sections[0]["blocks"][0]
    assert "[[cite_ref:R001]]" in block["text"]
    assert block["citations"][0]["ref_id"] == "R001"
    assert result.citation_ref_catalog["entries"][0]["ref_id"] == "R001"
    assert registry.get("citation_ref_catalog") is not None
    assert registry.get("review_provider_receipts") is not None


def test_current_review_writer_rejects_unresolved_citation(tmp_path: Path) -> None:
    summary, _pdf_path = _stage1_summary(tmp_path)
    config = {"Writer_API": {"api_key": "writer", "model": "writer", "api_base": "https://writer.test/v1"}}
    workspace = JobWorkspace.create(str(tmp_path / "review-output"), "current", job_id="writer-job")
    registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    service = ReviewGenerationService(
        job_id=workspace.job_id,
        attempt_id="attempt-1",
        workspace=workspace,
        artifact_registry=registry,
        settings=ApplicationSettings.from_config(config),
        summaries=[summary],
        writer=lambda **kwargs: {
            "status": "success",
            "content": {"blocks": [{"text": "Unsupported source [[cite_ref:R999]]."}]},
        },
    )
    paper_key = summary["paper_info"]["canonical_paper_key"]
    try:
        service.run(
            outline_payload={"sections": [{"section_id": "section_1", "title": "Results"}]},
            evidence_packets=[
                {
                    "section_id": "section_1",
                    "planned_claims": ["Claim"],
                    "paper_keys": [paper_key],
                    "source_summary_hashes": ["hash"],
                    "retrieval_provenance": {"source": "test"},
                }
            ],
        )
    except RuntimeError as exc:
        assert "outside its evidence packet" in str(exc) or "unresolved" in str(exc)
    else:
        raise AssertionError("unresolved Writer citation was accepted")


def test_current_review_resume_after_section_two_crash_reuses_section_one(tmp_path: Path) -> None:
    summary, _pdf_path = _stage1_summary(tmp_path)
    config = {
        "Writer_API": {"api_key": "writer", "model": "writer", "api_base": "https://writer.test/v1"},
    }
    workspace = JobWorkspace.create(str(tmp_path / "review-output"), "current", job_id="writer-resume-job")
    registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    packets = [
        {
            "section_id": section_id,
            "section_goal": f"Synthesize {section_id}",
            "planned_claims": ["The treatment improves the outcome."],
            "paper_keys": [summary["paper_info"]["canonical_paper_key"]],
            "source_summary_hashes": ["summary-hash"],
            "retrieval_provenance": {"source": "test", "section_id": section_id},
        }
        for section_id in ("section_1", "section_2")
    ]
    first_calls: list[int] = []

    def crash_on_section_two(**kwargs: Any) -> Mapping[str, Any]:
        section_number = int(kwargs["section_number"])
        first_calls.append(section_number)
        if section_number == 2:
            raise RuntimeError("simulated section 2 process crash")
        return {
            "status": "success",
            "content": {"blocks": [{"text": "The result supports the claim [[cite_ref:R001]]."}]},
            "usage_status": "provider_not_supported",
        }

    first_service = ReviewGenerationService(
        job_id=workspace.job_id,
        attempt_id="review-resume-attempt",
        workspace=workspace,
        artifact_registry=registry,
        settings=ApplicationSettings.from_config(config),
        summaries=[summary],
        writer=crash_on_section_two,
    )
    with pytest.raises(RuntimeError, match="simulated section 2 process crash"):
        first_service.run(
            outline_payload={
                "title": "Evidence-led review",
                "sections": [
                    {"section_id": "section_1", "title": "Results 1", "goal": "First"},
                    {"section_id": "section_2", "title": "Results 2", "goal": "Second"},
                ],
            },
            evidence_packets=packets,
        )
    assert first_calls == [1, 2]
    assert registry.get("review-section:section_1") is not None
    assert registry.get("review-section:section_2") is None

    retry_calls: list[int] = []

    def retry_writer(**kwargs: Any) -> Mapping[str, Any]:
        retry_calls.append(int(kwargs["section_number"]))
        return {
            "status": "success",
            "content": {"blocks": [{"text": "The result supports the claim [[cite_ref:R001]]."}]},
            "usage_status": "provider_not_supported",
        }

    retry_service = ReviewGenerationService(
        job_id=workspace.job_id,
        attempt_id="review-resume-attempt",
        workspace=workspace,
        artifact_registry=registry,
        settings=ApplicationSettings.from_config(config),
        summaries=[summary],
        writer=retry_writer,
    )
    resumed = retry_service.run(
        outline_payload={
            "title": "Evidence-led review",
            "sections": [
                {"section_id": "section_1", "title": "Results 1", "goal": "First"},
                {"section_id": "section_2", "title": "Results 2", "goal": "Second"},
            ],
        },
        evidence_packets=packets,
    )

    assert retry_calls == [2]
    assert [item["section_number"] for item in resumed.sections] == [1, 2]
    assert registry.get("review-section:section_1") is not None
    assert registry.get("review-section:section_2") is not None
