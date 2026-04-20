import json
from pathlib import Path
from typing import cast

import main
from config_loader import ConfigDict
from services.artifact_registry import ArtifactRegistry
from services.config_compat import CompatConfigView
from services.job_workspace import JobWorkspace
from services.progress_state import ResumeStateReport


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


def _make_bound_generator(tmp_path: Path, project_name: str = "demo", job_id: str | None = None):
    output_dir = tmp_path / "output"
    workspace = JobWorkspace.create(str(output_dir), project_name, job_id=job_id)
    registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    config = ConfigDict(
        {
            "Paths": {"output_path": str(output_dir)},
            "Writer_API": {"api_key": "writer-key", "model": "writer-model", "api_base": "https://example.com/v1"},
            "Validation": {"stage1_enabled": "false", "stage2_enabled": "false"},
            "Styling": {
                "font_name": "Times New Roman",
                "font_size_body": "12",
                "font_size_heading1": "16",
                "font_size_heading2": "14",
            },
        }
    )
    compat_view = CompatConfigView.from_config(config)

    generator = main.LiteratureReviewGenerator(project_name=project_name, pdf_folder=None)
    generator.logger = cast(main.CustomLogger, _DummyLogger())
    generator.config = config
    generator.bind_job_workspace(
        workspace=workspace,
        artifact_registry=registry,
        compat_config=compat_view,
        fingerprint_bundle={"request": "demo"},
        resume_state_report=_resume_report(workspace),
    )
    generator.summaries = [{"status": "success", "paper_info": {"title": "Paper A", "authors": ["Alice Smith"], "year": "2024", "canonical_paper_key": "paper_a"}, "ai_summary": {"paper_metadata": {"title": "Paper A", "authors": ["Alice Smith"], "year": "2024", "journal": "Journal of Tests", "doi": "10.1000/test.paper"}}}]
    generator.summary_file = workspace.artifact_path(f"{project_name}_summaries.json")
    Path(generator.summary_file).write_text(json.dumps(generator.summaries), encoding="utf-8")
    return generator, workspace, registry


def _stub_stage2_bootstrap(monkeypatch, generator) -> None:
    monkeypatch.setattr(generator, "load_configuration", lambda: True)
    monkeypatch.setattr(generator, "setup_output_directory", lambda: True)
    monkeypatch.setattr(generator, "load_existing_summaries", lambda: True)
    monkeypatch.setattr(generator, "_stage2_validation_enabled", lambda: False)
    monkeypatch.setattr(generator, "generate_word_table_of_contents", lambda _doc: True)


def test_successful_stage2_generation_creates_registered_citation_manifest(tmp_path: Path, monkeypatch) -> None:
    generator, workspace, _registry = _make_bound_generator(tmp_path, job_id="job-citation-success")
    _stub_stage2_bootstrap(monkeypatch, generator)

    outline_text = "# Demo Outline\n\n## 1. First Section\n\n## 2. Second Section"
    outline_path = Path(workspace.artifact_path("demo_literature_review_outline.md"))
    outline_path.write_text(outline_text, encoding="utf-8")

    monkeypatch.setattr(generator, "_load_outline_artifact", lambda: (str(outline_path), outline_text))
    monkeypatch.setattr(
        generator, "generate_review_section_content",
        lambda section_title, _outline: f"{section_title} generated content. [[cite:paper_a|mode=parenthetical]]",
    )
    monkeypatch.setattr(generator, "generate_apa_references", lambda: ["Author, A. (2024). Demo reference."])

    assert generator.generate_full_review_from_outline() is True

    word_path = Path(workspace.report_path("demo_literature_review.docx"))
    registry_payload = json.loads(Path(workspace.paths.registry_path).read_text(encoding="utf-8"))
    citation_records = [item for item in registry_payload["artifacts"] if item["artifact_type"] == "citation_manifest"]

    assert word_path.exists() is True
    assert len(citation_records) == 1

    artifact_path = Path(citation_records[0]["path"])
    artifact_payload = json.loads(artifact_path.read_text(encoding="utf-8"))

    assert artifact_payload["artifact_type"] == "citation_manifest"
    assert artifact_payload["artifact_version"] == "v3"
    assert artifact_payload["created_from_job_id"] == workspace.job_id
    assert artifact_payload["manifest_identity"]["manifest_id"] == "citation_manifest:v3"
    assert artifact_payload["review_reference"]["review_word_path"] == str(word_path)
    # V2 uses occurrences/clusters/bibliography instead of flat citations
    assert "occurrences" in artifact_payload
    assert "clusters" in artifact_payload
    assert "bibliography" in artifact_payload
    assert any(dep["artifact_type"] == "review_draft" for dep in citation_records[0]["depends_on"])


def test_stage2_with_failed_sections_does_not_register_citation_manifest(tmp_path: Path, monkeypatch) -> None:
    generator, workspace, _registry = _make_bound_generator(tmp_path, job_id="job-citation-failed")
    _stub_stage2_bootstrap(monkeypatch, generator)

    outline_text = "# Demo Outline\n\n## 1. First Section\n\n## 2. Second Section"
    outline_path = Path(workspace.artifact_path("demo_literature_review_outline.md"))
    outline_path.write_text(outline_text, encoding="utf-8")

    def _section_content(section_title: str, _outline: str):
        if section_title == "Second Section":
            return None
        return f"{section_title} generated content. [[cite:paper_a|mode=parenthetical]]"

    monkeypatch.setattr(generator, "_load_outline_artifact", lambda: (str(outline_path), outline_text))
    monkeypatch.setattr(generator, "generate_review_section_content", _section_content)
    monkeypatch.setattr(generator, "generate_apa_references", lambda: [])

    assert generator.generate_full_review_from_outline() is False

    word_path = Path(workspace.report_path("demo_literature_review.docx"))
    citation_path = Path(workspace.artifact_path("citation_manifests/demo_citation_manifest_v3.json"))
    failed_sections_path = Path(workspace.report_path("demo_failed_review_sections.json"))
    registry_path = Path(workspace.paths.registry_path)

    assert word_path.exists() is False
    assert citation_path.exists() is False
    assert failed_sections_path.exists() is True
    if registry_path.exists():
        registry_payload = json.loads(registry_path.read_text(encoding="utf-8"))
        assert not any(item["artifact_type"] == "citation_manifest" for item in registry_payload["artifacts"])
