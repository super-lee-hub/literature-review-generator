from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

import fitz  # type: ignore

from runtime.stage_contracts import build_source_bundle
from services.artifact_registry import ArtifactRegistry
from services.job_workspace import JobWorkspace
from services.settings import ApplicationSettings
from services.stage1_analysis_service import Stage1AnalysisService
from summary_schema import normalize_ai_summary


def _summary() -> dict[str, Any]:
    return normalize_ai_summary(
        {
            "routing": {
                "paper_type": "empirical",
                "paper_subtype_raw": "experiment",
                "paper_subtype_normalized": "experiment",
                "classification_status": "resolved",
                "route_confidence": "high",
                "classification_rationale": "visual empirical design",
                "secondary_candidates": [],
            },
            "paper_metadata": {
                "title": "Visual evidence study",
                "authors": ["Example Author"],
                "year": "2025",
                "journal": "Example Journal",
                "doi": "10.1000/visual",
            },
            "core_analysis": {
                "summary": "A study with a figure-supported empirical result.",
                "methodology": "Experiment with N=80 observations.",
                "findings": "The figure reports a 20 percent improvement (p < 0.05).",
                "conclusions": "The result is consistent with the proposed mechanism.",
                "key_points": ["The figure reports a 20 percent improvement."],
                "relevance": "The result informs visual evidence interpretation.",
                "limitations": "The result is bounded by the tested context.",
                "theoretical_framework": None,
                "research_gap": "Further replication is needed.",
                "future_research_directions": [],
            },
            "specialized_details": {
                "empirical": {
                    "research_questions_or_hypotheses": [],
                    "data_source_and_size": "Experiment, N=80.",
                    "analysis_technique": "Regression analysis.",
                    "core_variables": {"independent": ["treatment"], "dependent": ["outcome"]},
                    "sample_characteristics_or_context": "Experimental context.",
                },
                "review": None,
                "conceptual": None,
            },
        }
    )


def test_current_stage1_visual_bundle_is_traceable_and_passed_to_reader(tmp_path: Path) -> None:
    pdf_path = tmp_path / "visual-paper.pdf"
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 72), "Figure 1. Treatment effect\nResults: 20 percent improvement.")
    page.draw_rect(fitz.Rect(72, 100, 260, 220), color=(0, 0, 1), fill=(0.8, 0.8, 1))
    document.save(pdf_path)
    document.close()

    config = {
        "Preprocess": {"enabled": "true", "cache_dir": str(tmp_path / "cache")},
        "Stage1_Visual": {"enabled": "true"},
        "Stage1_Input": {"send_extracted_text": "true", "send_selected_visuals": "true", "send_original_pdf": "never"},
        "Multimodal": {"enabled": "true"},
        "Primary_Reader_API": {
            "api_key": "reader",
            "model": "vision-reader",
            "api_base": "https://reader.test/v1",
            "endpoint_type": "responses",
            "supports_image_input": "true",
        },
        "Backup_Reader_API": {"api_key": "backup", "model": "backup-reader", "api_base": "https://backup.test/v1"},
    }
    workspace = JobWorkspace.create(str(tmp_path / "output"), "current", job_id="visual-job")
    registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    registry.register_file(
        artifact_role="source_pdf", artifact_type="source_pdf", artifact_version="v1",
        path=pdf_path, producer="test", artifact_id=f"source_pdf:{pdf_path.name}",
    )
    settings = ApplicationSettings.from_config(config)
    observed: list[Mapping[str, Any]] = []

    def reader(**kwargs: Any) -> Mapping[str, Any]:
        observed.append(kwargs)
        return {"status": "success", "content": _summary()}

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
        source_mode="direct", project_name="current",
        papers=[{"title": "Visual evidence study", "pdf_path": str(pdf_path), "source_paper_id": "visual-1"}],
    )
    result = service.run(bundle)

    assert observed
    assert result.summaries[0]["stage1_input"]["visual_manifest_path"]
    assert result.summaries[0]["stage1_input"]["visual_bundle_path"]
    assert result.summaries[0]["stage1_input"]["selected_visual_refs"]
    assert any(record.artifact_type == "visual_manifest" for record in registry.list_records())
    assert any(record.artifact_type == "stage1_visual_bundle" for record in registry.list_records())


def test_scanned_primary_with_empty_text_reaches_visual_stage1_path(tmp_path: Path) -> None:
    from io import BytesIO
    from PIL import Image, ImageDraw  # type: ignore

    page_image = Image.new("RGB", (800, 1100), "white")
    draw = ImageDraw.Draw(page_image)
    draw.rectangle((60, 60, 740, 1040), outline="black", width=8)
    draw.text((100, 120), "Scanned article page", fill="black")
    image_stream = BytesIO()
    page_image.save(image_stream, format="PNG")

    pdf_path = tmp_path / "scanned-primary.pdf"
    document = fitz.open()
    for _ in range(5):
        page = document.new_page(width=595, height=842)
        page.insert_image(page.rect, stream=image_stream.getvalue())
    document.save(pdf_path)
    document.close()

    config = {
        "Preprocess": {
            "enabled": "true",
            "cache_dir": str(tmp_path / "cache"),
            "extractor_profile": "fitz",
            "ocr_mode": "off",
            "force_rebuild": "true",
        },
        "Stage1_Visual": {"enabled": "true"},
        "Stage1_Input": {
            "send_extracted_text": "true",
            "send_selected_visuals": "true",
            "send_original_pdf": "never",
        },
        "Multimodal": {"enabled": "true"},
        "Primary_Reader_API": {
            "api_key": "reader",
            "model": "vision-reader",
            "api_base": "https://reader.test/v1",
            "endpoint_type": "responses",
            "supports_image_input": "true",
        },
        "Backup_Reader_API": {
            "api_key": "backup",
            "model": "backup-reader",
            "api_base": "https://backup.test/v1",
        },
    }
    workspace = JobWorkspace.create(str(tmp_path / "output"), "scanned", job_id="scanned-job")
    registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    registry.register_file(
        artifact_role="source_pdf",
        artifact_type="source_pdf",
        artifact_version="v1",
        path=pdf_path,
        producer="test",
        artifact_id=f"source_pdf:{pdf_path.name}",
    )
    observed: list[Mapping[str, Any]] = []

    def reader(**kwargs: Any) -> Mapping[str, Any]:
        observed.append(kwargs)
        return {"status": "success", "content": _summary()}

    service = Stage1AnalysisService(
        job_id=workspace.job_id,
        attempt_id="attempt-1",
        workspace=workspace,
        artifact_registry=registry,
        config=config,
        settings=ApplicationSettings.from_config(config),
        reader=reader,
    )
    bundle = build_source_bundle(
        source_mode="direct",
        project_name="scanned",
        papers=[
            {
                "title": "Visual evidence study",
                "authors": ["Example Author"],
                "year": "2025",
                "doi": "10.1000/visual",
                "pdf_path": str(pdf_path),
                "source_paper_id": "scanned-1",
                "source_attachment_role": "SCANNED_PRIMARY",
            }
        ],
    )

    result = service.run(bundle)

    assert observed
    summary = result.summaries[0]
    assert summary["preprocess"]["scanned_like"] is True
    assert summary["preprocess"]["stage1_input_text"] == ""
    assert sum(
        ref.get("artifact_type") == "page_snapshot"
        for ref in summary["stage1_input"]["selected_visual_refs"]
    ) == 5
    assert summary["stage1_input"]["visual_coverage"]["rendered_pages"] == 5
