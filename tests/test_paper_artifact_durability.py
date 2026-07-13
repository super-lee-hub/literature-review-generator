import json
from pathlib import Path
from typing import cast

import pytest

import main
from config_loader import ConfigDict
from models import PaperInfo
from services.artifact_registry import ArtifactRegistry
from services.config_compat import CompatConfigView
from services.job_workspace import JobWorkspace
from services.progress_state import ResumeStateReport
from services.source_normalizer import normalize_source_papers, project_descriptors_to_legacy_papers
from services.source_identity import evaluate_source_identity


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


def _quality_ready_ai_summary(
    *,
    paper_metadata: dict | None = None,
):
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
        "paper_metadata": paper_metadata
        or {
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


def _make_bound_generator(tmp_path: Path, *, source_mode: str, job_id: str):
    output_dir = tmp_path / "output"
    workspace = JobWorkspace.create(str(output_dir), "demo", job_id=job_id)
    registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    config = ConfigDict(
        {
            "Paths": {"output_path": str(output_dir)},
            "Primary_Reader_API": {"api_key": "primary", "model": "m1", "api_base": "https://example.com/v1"},
            "Backup_Reader_API": {"api_key": "", "model": "m2", "api_base": "https://example.com/v1"},
            "Validation": {"stage1_enabled": "false", "stage2_enabled": "false"},
        }
    )
    compat_view = CompatConfigView.from_config(config)

    generator = main.LiteratureReviewGenerator(
        project_name="demo",
        pdf_folder=str(tmp_path) if source_mode == "direct" else None,
    )
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


def _normalized_paper(tmp_path: Path, *, source_mode: str) -> PaperInfo:
    pdf_path = tmp_path / f"{source_mode}-paper.pdf"
    pdf_path.write_text("dummy pdf placeholder", encoding="utf-8")

    base_paper = {
        "title": f"{source_mode.title()} Paper",
        "authors": ["Alice Example"],
        "year": "2024",
        "journal": "Journal of Tests",
        "doi": "10.1000/demo-zotero" if source_mode == "zotero" else "",
        "pdf_path": str(pdf_path),
    }
    descriptors = normalize_source_papers(source_mode, [base_paper])
    return project_descriptors_to_legacy_papers([base_paper], descriptors)[0]


def _stub_stage1_success(monkeypatch, generator) -> None:
    monkeypatch.setattr(
        generator,
        "_prepare_stage1_input",
        lambda *_args, **_kwargs: (
            "x" * 1200,
            {
                "analysis_input_kind": "plain_text",
                "extractor_used": "mock",
                "selected_text_source": "plain_text",
                "stage1_quality_level": "FALLBACK",
                "stage1_input_path": str(Path("cache") / "stage1_input.md"),
                "stage1_input_manifest_path": str(Path("cache") / "stage1_input_manifest.json"),
                "stage1_quality_report_path": str(Path("cache") / "stage1_text_quality_report.json"),
            },
        ),
    )
    monkeypatch.setattr(generator, "_load_stage1_prompt_template", lambda: "{{PAPER_FULL_TEXT}}")
    monkeypatch.setattr(generator, "_inject_free_mode_context", lambda prompt: prompt)
    monkeypatch.setattr(
        main,
        "inspect_text_identity",
        lambda expected, _text, **_kwargs: evaluate_source_identity(expected, expected),
    )
    monkeypatch.setattr(main, "get_summary_from_ai_with_fallback", lambda *args, **kwargs: _quality_ready_ai_summary())
    monkeypatch.setattr(main, "validate_summary_quality", lambda _summary_data: (True, "ok"))


@pytest.mark.parametrize("source_mode", ["direct", "zotero"])
def test_successful_process_paper_creates_registered_paper_artifact(
    tmp_path: Path,
    monkeypatch,
    source_mode: str,
) -> None:
    generator, workspace, _registry = _make_bound_generator(
        tmp_path,
        source_mode=source_mode,
        job_id=f"job-{source_mode}",
    )
    paper = _normalized_paper(tmp_path, source_mode=source_mode)
    _stub_stage1_success(monkeypatch, generator)

    result = generator.process_paper(paper, 0, None, 1)

    assert result is not None
    assert result["status"] == "success"

    registry_payload = json.loads(Path(workspace.paths.registry_path).read_text(encoding="utf-8"))
    paper_records = [item for item in registry_payload["artifacts"] if item["artifact_type"] == "paper_artifact"]

    assert len(paper_records) == 1
    artifact_path = Path(paper_records[0]["path"])
    artifact_payload = json.loads(artifact_path.read_text(encoding="utf-8"))

    assert artifact_payload["artifact_type"] == "paper_artifact"
    assert artifact_payload["artifact_version"] == "v1"
    assert artifact_payload["created_from_job_id"] == workspace.job_id
    assert artifact_payload["paper_identity"]["source_paper_id"] == paper.get("source_paper_id")
    assert artifact_payload["paper_identity"]["canonical_paper_key"] == paper.get("canonical_paper_key")
    assert artifact_payload["source"]["source_mode"] == source_mode
    assert artifact_payload["source"]["source_pdf"] == paper.get("source_pdf")
    assert artifact_payload["analysis"]["status"] == "success"
    assert artifact_payload["analysis"]["text_length"] == 1200
    assert artifact_payload["analysis"]["ai_summary"]["routing"]["paper_type"] == "empirical"
    assert artifact_payload["analysis"]["preprocess"]["selected_text_source"] == "plain_text"
    assert artifact_payload["analysis"]["preprocess"]["stage1_quality_level"] == "FALLBACK"
    assert artifact_payload["stage1_inputs"]["selected_text_source"] == "plain_text"
    assert artifact_payload["stage1_inputs"]["stage1_quality_level"] == "FALLBACK"
    assert artifact_payload["stage1_inputs"]["stage1_input_manifest_path"].endswith("stage1_input_manifest.json")


def test_failed_process_paper_does_not_create_or_register_paper_artifact(tmp_path: Path, monkeypatch) -> None:
    generator, workspace, _registry = _make_bound_generator(
        tmp_path,
        source_mode="direct",
        job_id="job-failed",
    )
    paper = _normalized_paper(tmp_path, source_mode="direct")

    monkeypatch.setattr(
        generator,
        "_prepare_stage1_input",
        lambda *_args, **_kwargs: ("too short", {"analysis_input_kind": "text", "extractor_used": "mock"}),
    )

    result = generator.process_paper(paper, 0, None, 1)

    assert result is not None
    assert result["status"] == "failed"

    registry_path = Path(workspace.paths.registry_path)
    if registry_path.exists():
        registry_payload = json.loads(registry_path.read_text(encoding="utf-8"))
        assert not any(item["artifact_type"] == "paper_artifact" for item in registry_payload["artifacts"])
    assert not list((Path(workspace.paths.artifacts_dir) / "paper_artifacts").glob("*.json"))


def test_paper_artifact_creation_does_not_break_summary_durability(tmp_path: Path, monkeypatch) -> None:
    generator, workspace, _registry = _make_bound_generator(
        tmp_path,
        source_mode="direct",
        job_id="job-summary",
    )
    paper = _normalized_paper(tmp_path, source_mode="direct")
    _stub_stage1_success(monkeypatch, generator)

    result = generator.process_paper(paper, 0, None, 1)

    assert result is not None
    assert result["status"] == "success"

    generator.summaries = [result]
    generator._checkpoint_processed_papers.add(main.LiteratureReviewGenerator.get_paper_key(result["paper_info"]))

    assert generator.save_summaries() is True

    summary_path = Path(workspace.artifact_path("demo_summaries.json"))
    progress_path = Path(workspace.artifact_path("stage1_progress_snapshot.json"))
    registry_payload = json.loads(Path(workspace.paths.registry_path).read_text(encoding="utf-8"))

    assert json.loads(summary_path.read_text(encoding="utf-8"))[0]["status"] == "success"
    assert json.loads(progress_path.read_text(encoding="utf-8"))["artifact_type"] == "stage1_progress_snapshot"
    assert any(item["artifact_type"] == "summary_file" for item in registry_payload["artifacts"])
    assert any(item["artifact_type"] == "stage1_progress_snapshot" for item in registry_payload["artifacts"])
    assert any(item["artifact_type"] == "paper_artifact" for item in registry_payload["artifacts"])
