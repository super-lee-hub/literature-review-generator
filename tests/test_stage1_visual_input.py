import base64
import json
from pathlib import Path
from typing import cast
from unittest.mock import Mock, patch

import fitz  # type: ignore

import main
from ai_interface import _call_ai_api
from config_loader import ConfigDict
from models import PaperInfo
from preprocess.service import PreprocessManager
from preprocess.visual_artifacts import Stage1VisualArtifactBuilder
from services.artifact_registry import ArtifactRegistry
from services.config_compat import CompatConfigView
from services.job_workspace import JobWorkspace
from services.progress_state import ResumeStateReport
from services.stage1_input_builder import Stage1InputBuilder


PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+kvX8AAAAASUVORK5CYII="
)


class _DummyLogger:
    def info(self, *_args, **_kwargs):
        pass

    def warning(self, *_args, **_kwargs):
        pass

    def error(self, *_args, **_kwargs):
        pass

    def success(self, *_args, **_kwargs):
        pass

    def debug(self, *_args, **_kwargs):
        pass


def _quality_ready_ai_summary():
    return {
        "routing": {
            "paper_type": "empirical",
            "classification_status": "resolved",
            "route_confidence": "high",
            "secondary_candidates": [],
        },
        "core_analysis": {
            "summary": "A detailed summary with enough content to support downstream checks.",
            "key_points": ["Point A"],
            "methodology": "Mixed methods.",
            "findings": "Important findings.",
            "conclusions": "Meaningful conclusions.",
            "relevance": "Relevant to the review.",
            "limitations": "Limited by scope and sampling frame.",
            "theoretical_framework": None,
            "research_gap": None,
            "future_research_directions": [],
        },
        "paper_metadata": {
            "title": None,
            "authors": [],
            "year": None,
            "journal": None,
            "doi": None,
        },
        "specialized_details": {
            "empirical": {
                "research_questions_or_hypotheses": [],
                "data_source_and_size": None,
                "analysis_technique": None,
                "core_variables": {
                    "independent": [],
                    "dependent": [],
                    "mediators": [],
                    "moderators": [],
                    "controls": [],
                    "other_core_constructs": [],
                },
                "sample_characteristics_or_context": None,
            },
            "review": None,
            "conceptual": None,
        },
    }


def _resume_report(workspace: JobWorkspace) -> ResumeStateReport:
    return ResumeStateReport(
        artifact_type="resume_state_report",
        artifact_version="v1",
        created_from_job_id=workspace.job_id,
        created_at="2026-04-03T00:00:00Z",
        project_name=workspace.project_name,
        job_id=workspace.job_id,
        state="non_resumable",
        reason="test bootstrap",
        summary_file=workspace.artifact_path(f"{workspace.project_name}_summaries.json"),
        progress_snapshot_file=None,
        checkpoint_file=workspace.checkpoint_path(f"{workspace.project_name}_checkpoint.json"),
        fingerprint_bundle={"request": "demo"},
    )


def _make_bound_generator(tmp_path: Path, *, api_base: str, model: str, job_id: str):
    output_dir = tmp_path / "output"
    workspace = JobWorkspace.create(str(output_dir), "demo", job_id=job_id)
    registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    config = ConfigDict(
        {
            "Paths": {"output_path": str(output_dir)},
            "Primary_Reader_API": {"api_key": "primary", "model": model, "api_base": api_base},
            "Backup_Reader_API": {"api_key": "", "model": "backup", "api_base": api_base},
            "Validation": {"stage1_enabled": "false", "stage2_enabled": "false"},
        }
    )
    compat_view = CompatConfigView.from_config(config)

    generator = main.LiteratureReviewGenerator(project_name="demo", pdf_folder=str(tmp_path))
    generator.logger = cast(main.CustomLogger, _DummyLogger())
    generator.config = config
    generator.bind_job_workspace(
        workspace=workspace,
        artifact_registry=registry,
        compat_config=compat_view,
        fingerprint_bundle={"request": "demo"},
        resume_state_report=_resume_report(workspace),
    )
    return generator, workspace, registry


def _create_visual_pdf(path: Path) -> None:
    doc = fitz.open()
    try:
        page = doc.new_page(width=595, height=842)
        page.insert_text((72, 72), ("This page has only text.\n" * 20))

        page = doc.new_page(width=595, height=842)
        page.insert_text(
            (72, 72),
            (
                "As shown in Figure 1, the proposed framework model explains the process and mechanism in detail.\n"
                * 6
            ),
        )
        page.insert_image(fitz.Rect(90, 170, 430, 430), stream=PNG_BYTES)
        page.insert_text((90, 460), "Figure 1. Proposed framework model.")

        page = doc.new_page(width=595, height=842)
        page.insert_text(
            (72, 72),
            (
                "Figure 2 illustrates the architecture diagram and process workflow used in the study.\n"
                * 6
            ),
        )
        page.insert_image(fitz.Rect(110, 180, 470, 500), stream=PNG_BYTES)
        page.insert_text((110, 530), "Figure 2. Process architecture diagram.")

        doc.save(str(path))
    finally:
        doc.close()


def _sample_visual_bundle(tmp_path: Path) -> dict:
    image_path = tmp_path / "sample.png"
    image_path.write_bytes(PNG_BYTES)
    return {
        "bundle_path": str(tmp_path / "visual_bundle.json"),
        "visual_manifest_path": str(tmp_path / "visual_manifest.json"),
        "selection_policy_snapshot": {"budgets": {"total_visuals_max": 10}},
        "selected_visual_refs": [
            {
                "visual_id": "figure-001",
                "artifact_id": "figure_crop:test",
                "paper_key": "paper-key",
                "source_pdf": str(tmp_path / "paper.pdf"),
                "page_no": 2,
                "bbox": [90.0, 170.0, 430.0, 430.0],
                "artifact_type": "figure_crop",
                "source_type": "image_block",
                "image_path": str(image_path),
                "caption_excerpt": "Figure 1. Proposed framework model.",
                "nearby_text_excerpt": "As shown in Figure 1, the proposed framework model explains the process.",
                "selection_reason": "large_image_block:0.18, caption_or_context_cues:4",
                "selection_score": 7.5,
                "dedupe_group_id": "abc123",
            }
        ],
    }


def test_visual_bundle_writes_manifest_and_registers_artifacts(tmp_path: Path) -> None:
    pdf_path = tmp_path / "paper.pdf"
    _create_visual_pdf(pdf_path)

    workspace = JobWorkspace.create(str(tmp_path / "output"), "demo", job_id="job-visuals")
    registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    manager = PreprocessManager(
        config={
            "Paths": {"output_path": str(tmp_path)},
            "Preprocess": {
                "enabled": "true",
                "cache_dir": str(tmp_path / "cache"),
                "extractor_profile": "fitz",
                "ocr_mode": "off",
                "force_rebuild": "true",
            },
        },
        logger=None,
    )
    preprocess_result = manager.prepare_pdf(str(pdf_path))
    assert preprocess_result is not None

    bundle = Stage1VisualArtifactBuilder().build_bundle(
        job_id=workspace.job_id,
        paper_key="paper-key",
        paper_info={"title": "Demo Paper"},
        source_pdf=str(pdf_path),
        output_dir=workspace.artifact_path("stage1_visuals/demo"),
        artifact_registry=registry,
        preprocess_metadata={
            "manifest_path": preprocess_result.manifest_path,
            "page_index_path": preprocess_result.page_index_path,
            "structured_json_path": preprocess_result.structured_json_path,
        },
    )

    assert bundle is not None
    manifest_payload = json.loads(Path(bundle.visual_manifest_path).read_text(encoding="utf-8"))
    registry_payload = json.loads(Path(workspace.paths.registry_path).read_text(encoding="utf-8"))

    assert manifest_payload["artifact_type"] == "visual_manifest"
    assert manifest_payload["artifact_version"] == "v1"
    assert manifest_payload["created_from_job_id"] == workspace.job_id
    assert manifest_payload["selection_policy"]["deferred_artifact_types"] == ["table_crop"]
    assert manifest_payload["budget_decisions"]["selected_counts"]["total"] >= 2
    assert any(item["artifact_type"] == "page_snapshot" for item in manifest_payload["visuals"])
    assert any(item["artifact_type"] == "figure_crop" for item in manifest_payload["visuals"])
    first_visual = manifest_payload["visuals"][0]
    assert first_visual["page_no"] >= 1
    assert len(first_visual["bbox"]) == 4
    assert first_visual["image_path"]
    assert first_visual["selection_reason"]
    assert first_visual["dedupe_group_id"]
    assert Path(first_visual["image_path"]).exists() is True

    artifact_types = {item["artifact_type"] for item in registry_payload["artifacts"]}
    assert "stage1_visual_bundle" in artifact_types
    assert "visual_manifest" in artifact_types
    assert "page_snapshot" in artifact_types
    assert "figure_crop" in artifact_types
    assert "table_crop" not in artifact_types


def test_stage1_input_builder_returns_multimodal_payload_when_supported(tmp_path: Path) -> None:
    built = Stage1InputBuilder().build(
        prompt_template="Analyze carefully:\n{{PAPER_FULL_TEXT}}",
        paper_text="Main text body.",
        reader_api_config={"api_key": "key", "model": "gpt-4o", "api_base": "https://api.openai.com/v1"},
        visual_bundle=_sample_visual_bundle(tmp_path),
    )

    assert built.input_mode == "multimodal"
    assert built.user_message_content is not None
    assert built.user_message_content[0]["type"] == "text"
    assert built.user_message_content[1]["type"] == "local_image_path"
    assert "Treat the paper text as the primary evidence" in built.prompt_text
    assert "Figure 1. Proposed framework model." in built.prompt_text


def test_stage1_input_builder_falls_back_to_text_only_when_backend_is_unclear(tmp_path: Path) -> None:
    built = Stage1InputBuilder().build(
        prompt_template="Analyze carefully:\n{{PAPER_FULL_TEXT}}",
        paper_text="Main text body.",
        reader_api_config={"api_key": "key", "model": "custom-reader", "api_base": "https://example.com/v1"},
        visual_bundle=_sample_visual_bundle(tmp_path),
    )

    assert built.input_mode == "text_only"
    assert built.user_message_content is None
    assert built.fallback_reason == "conservative_fallback_for_unsupported_or_unclear_backend"
    assert len(built.selected_visual_refs) == 1


def test_call_ai_api_serializes_local_image_path_content(tmp_path: Path) -> None:
    image_path = tmp_path / "sample.png"
    image_path.write_bytes(PNG_BYTES)

    mock_response = Mock()
    mock_response.json.return_value = {
        "choices": [{"message": {"content": '{"summary": "ok"}'}}],
    }
    mock_response.raise_for_status.return_value = None

    with patch("ai_interface.requests.post", return_value=mock_response) as mock_post:
        result = _call_ai_api(
            "fallback prompt",
            {"api_key": "key", "model": "gpt-4o", "api_base": "https://api.openai.com/v1"},
            "system",
            user_content=[
                {"type": "text", "text": "hello"},
                {"type": "local_image_path", "path": str(image_path)},
            ],
        )

    assert result == {"summary": "ok"}
    payload = mock_post.call_args.kwargs["json"]
    user_content = payload["messages"][1]["content"]
    assert user_content[0] == {"type": "text", "text": "hello"}
    assert user_content[1]["type"] == "image_url"
    assert user_content[1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_process_paper_links_visual_bundle_into_paper_artifact_with_text_only_fallback(
    tmp_path: Path,
    monkeypatch,
) -> None:
    pdf_path = tmp_path / "paper.pdf"
    _create_visual_pdf(pdf_path)

    generator, workspace, _registry = _make_bound_generator(
        tmp_path,
        api_base="https://example.com/v1",
        model="custom-reader",
        job_id="job-stage1-visual-fallback",
    )

    monkeypatch.setattr(
        generator,
        "_prepare_stage1_input",
        lambda _path: ("A" * 1400, {"analysis_input_kind": "text", "extractor_used": "mock"}),
    )
    monkeypatch.setattr(generator, "_load_stage1_prompt_template", lambda: "{{PAPER_FULL_TEXT}}")
    monkeypatch.setattr(generator, "_inject_free_mode_context", lambda prompt: prompt)
    monkeypatch.setattr(main, "get_summary_from_ai_with_fallback", lambda *args, **kwargs: _quality_ready_ai_summary())
    monkeypatch.setattr(main, "validate_summary_quality", lambda _summary_data: (True, "ok"))

    paper: PaperInfo = {
        "title": "Visual Paper",
        "authors": ["Alice Example"],
        "year": "2024",
        "journal": "Journal of Tests",
        "doi": "10.1000/demo",
        "pdf_path": str(pdf_path),
    }

    result = generator.process_paper(paper, 0, None, 1)

    assert result is not None
    assert result["status"] == "success"
    assert "stage1_input" in result
    stage1_input = result["stage1_input"]
    assert stage1_input["input_mode"] == "text_only"
    assert stage1_input["fallback_reason"] == "conservative_fallback_for_unsupported_or_unclear_backend"
    assert stage1_input["selected_visual_refs"]
    assert "prompt_text" not in stage1_input

    registry_payload = json.loads(Path(workspace.paths.registry_path).read_text(encoding="utf-8"))
    artifact_types = {item["artifact_type"] for item in registry_payload["artifacts"]}
    assert "page_snapshot" in artifact_types
    assert "figure_crop" in artifact_types
    assert "visual_manifest" in artifact_types
    assert "stage1_visual_bundle" in artifact_types
    assert "paper_artifact" in artifact_types

    paper_record = next(item for item in registry_payload["artifacts"] if item["artifact_type"] == "paper_artifact")
    assert any(dep["artifact_type"] == "visual_manifest" for dep in paper_record["depends_on"])

    paper_artifact = json.loads(Path(paper_record["path"]).read_text(encoding="utf-8"))
    assert paper_artifact["stage1_inputs"]["visual_artifact_manifest_path"]
    assert paper_artifact["stage1_inputs"]["selected_visual_refs"]
    assert paper_artifact["stage1_inputs"]["visual_selection_policy_snapshot"]["budgets"]["total_visuals_max"] == 10


def test_build_stage1_model_input_restores_manifest_file_path_from_registry(tmp_path: Path, monkeypatch) -> None:
    generator, workspace, registry = _make_bound_generator(
        tmp_path,
        api_base="https://example.com/v1",
        model="custom-reader",
        job_id="job-stage1-visual-restore",
    )
    monkeypatch.setattr(generator, "_load_stage1_prompt_template", lambda: "Analyze:\n{{PAPER_FULL_TEXT}}")
    monkeypatch.setattr(generator, "_inject_free_mode_context", lambda prompt: prompt)

    image_path = tmp_path / "restored-sample.png"
    image_path.write_bytes(PNG_BYTES)

    paper: PaperInfo = {
        "title": "Visual Paper",
        "authors": ["Alice Example"],
        "year": "2024",
        "journal": "Journal of Tests",
        "doi": "10.1000/demo",
        "pdf_path": str(tmp_path / "paper.pdf"),
        "canonical_paper_key": "test-paper",
        "paper_key_aliases": ["test-paper"],
    }

    manifest_path = Path(workspace.artifact_path("stage1_visuals/test/visual_manifest.json"))
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(
            {
                "artifact_type": "visual_manifest",
                "artifact_version": "v1",
                "created_from_job_id": workspace.job_id,
                "created_at": "2024-01-01T00:00:00Z",
                "paper_key": "test-paper",
                "paper_title": "Visual Paper",
                "source_pdf": "paper.pdf",
                "bundle_dir": str(manifest_path.parent),
                "selection_policy": {"budgets": {"total_visuals_max": 10}},
                "budget_decisions": {},
                "visuals": [
                    {
                        "visual_id": "figure-001",
                        "artifact_id": "figure_crop:test",
                        "paper_key": "test-paper",
                        "source_pdf": "paper.pdf",
                        "page_no": 2,
                        "bbox": [90.0, 170.0, 430.0, 430.0],
                        "artifact_type": "figure_crop",
                        "source_type": "image_block",
                        "image_path": str(image_path),
                        "caption_excerpt": "Figure 1. Proposed framework model.",
                        "nearby_text_excerpt": "As shown in Figure 1, the proposed framework model explains the process.",
                        "selection_reason": "large_image_block:0.18, caption_or_context_cues:4",
                        "selection_score": 7.5,
                        "dedupe_group_id": "abc123",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    registry.register_file(
        artifact_role="visual_manifest",
        artifact_type="visual_manifest",
        artifact_version="v1",
        path=str(manifest_path),
        producer="test",
    )

    paper_key = generator._paper_artifact_key(paper)
    artifact_hash = generator._paper_artifact_hash(paper_key)
    paper_artifact_path = Path(workspace.artifact_path(f"paper_artifacts/{artifact_hash}.json"))
    paper_artifact_path.parent.mkdir(parents=True, exist_ok=True)
    paper_artifact_path.write_text(
        json.dumps(
            {
                "artifact_type": "paper_artifact",
                "artifact_version": "v1",
                "created_from_job_id": workspace.job_id,
                "created_at": "2024-01-01T00:00:00Z",
                "paper_identity": {
                    "source_paper_id": "test-paper",
                    "canonical_paper_key": "test-paper",
                    "paper_key_aliases": ["test-paper"],
                },
                "source": {},
                "paper_info": dict(paper),
                "analysis": {},
                "stage1_inputs": {
                    "visual_artifact_manifest_path": str(manifest_path.parent),
                    "selected_visual_refs": [],
                },
            }
        ),
        encoding="utf-8",
    )

    built = generator._build_stage1_model_input(
        pdf_text="Main text body.",
        reader_api_config={"api_key": "key", "model": "custom-reader", "api_base": "https://example.com/v1"},
        visual_bundle=None,
        paper=paper,
    )

    assert built["visual_manifest_path"] == str(manifest_path)
    assert built["visual_manifest_path"] != str(manifest_path.parent)
    assert built["selected_visual_refs"][0]["paper_key"] == "test-paper"
