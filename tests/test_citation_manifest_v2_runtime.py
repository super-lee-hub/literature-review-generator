"""Tests for citation_manifest_v2 runtime persistence and validation integration.

This module tests:
- citation_manifest_v2 runtime persistence
- registry registration
- occurrence/cluster generation from review_draft_v2 blocks
- validator consuming v2 primary path
- failure paths not falsely registering ready artifacts
"""

import json
from pathlib import Path
from typing import cast

import main
from config_loader import ConfigDict
from services.artifact_registry import ArtifactRegistry
from services.config_compat import CompatConfigView
from services.job_workspace import JobWorkspace
from services.progress_state import ResumeStateReport
from services.citation_manifest import (
    CitationManifestV2,
    build_citation_manifest_v2_from_review_draft,
)
from services.review_draft import build_review_draft_v2


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
    generator.summaries = [
        {
            "status": "success",
            "paper_info": {
                "title": "Test Paper About AI",
                "authors": ["Smith", "Johnson"],
                "year": "2024",
            }
        }
    ]
    generator.summary_file = workspace.artifact_path(f"{project_name}_summaries.json")
    Path(generator.summary_file).write_text(json.dumps(generator.summaries), encoding="utf-8")
    return generator, workspace, registry


def _stub_stage2_bootstrap(monkeypatch, generator) -> None:
    monkeypatch.setattr(generator, "load_configuration", lambda: True)
    monkeypatch.setattr(generator, "setup_output_directory", lambda: True)
    monkeypatch.setattr(generator, "load_existing_summaries", lambda: True)
    monkeypatch.setattr(generator, "_stage2_validation_enabled", lambda: False)
    monkeypatch.setattr(generator, "generate_word_table_of_contents", lambda _doc: True)


def test_citation_manifest_v2_persisted_as_primary_artifact(tmp_path: Path, monkeypatch) -> None:
    """Test that citation_manifest_v2 is persisted as the primary durable artifact."""
    generator, workspace, _registry = _make_bound_generator(tmp_path, job_id="job-v2-primary")
    _stub_stage2_bootstrap(monkeypatch, generator)

    outline_text = "# Demo Outline\n\n## 1. First Section\n\n## 2. Second Section"
    outline_path = Path(workspace.artifact_path("demo_literature_review_outline.md"))
    outline_path.write_text(outline_text, encoding="utf-8")

    monkeypatch.setattr(generator, "_load_outline_artifact", lambda: (str(outline_path), outline_text))
    monkeypatch.setattr(
        generator, "generate_review_section_content",
        lambda section_title, _outline: f"{section_title} generated content with citation (Smith, 2024).",
    )
    monkeypatch.setattr(generator, "generate_apa_references", lambda: ["Smith, J. (2024). Test paper about AI."])

    assert generator.generate_full_review_from_outline() is True

    # Check v2 is the primary artifact
    v2_path = Path(workspace.artifact_path("citation_manifests/demo_citation_manifest_v2.json"))
    assert v2_path.exists(), "Citation manifest v2 should exist as primary artifact"

    # Check v1 exists as compatibility projection
    v1_path = Path(workspace.artifact_path("citation_manifests/demo_citation_manifest_v1.json"))
    assert v1_path.exists(), "Citation manifest v1 should exist as compatibility projection"

    # Verify v2 structure
    v2_data = json.loads(v2_path.read_text(encoding="utf-8"))
    assert v2_data["artifact_version"] == "v2"
    assert "occurrences" in v2_data
    assert "clusters" in v2_data
    assert "bibliography" in v2_data

    # Verify v1 is projection from v2
    v1_data = json.loads(v1_path.read_text(encoding="utf-8"))
    assert v1_data["artifact_version"] == "v1"
    assert v1_data["manifest_identity"].get("projection_from") == "v2"


def test_citation_manifest_v2_registry_registration(tmp_path: Path, monkeypatch) -> None:
    """Test that citation_manifest_v2 is properly registered in artifact registry."""
    generator, workspace, _registry = _make_bound_generator(tmp_path, job_id="job-v2-registry")
    _stub_stage2_bootstrap(monkeypatch, generator)

    outline_text = "# Demo Outline\n\n## 1. Introduction"
    outline_path = Path(workspace.artifact_path("demo_literature_review_outline.md"))
    outline_path.write_text(outline_text, encoding="utf-8")

    monkeypatch.setattr(generator, "_load_outline_artifact", lambda: (str(outline_path), outline_text))
    monkeypatch.setattr(
        generator, "generate_review_section_content",
        lambda section_title, _outline: f"Introduction with citation (Smith, 2024).",
    )
    monkeypatch.setattr(generator, "generate_apa_references", lambda: ["Smith, J. (2024). Test paper."])

    assert generator.generate_full_review_from_outline() is True

    # Check registry
    registry_payload = json.loads(Path(workspace.paths.registry_path).read_text(encoding="utf-8"))
    citation_records = [item for item in registry_payload["artifacts"] if item["artifact_type"] == "citation_manifest"]

    assert len(citation_records) == 1
    assert citation_records[0]["artifact_version"] == "v2"
    assert citation_records[0]["path"].endswith("_citation_manifest_v2.json")

    # Verify dependency on review_draft
    depends_on = citation_records[0].get("depends_on", [])
    assert any(dep.get("artifact_type") == "review_draft" for dep in depends_on)


def test_occurrence_cluster_generation_from_review_draft_v2(tmp_path: Path) -> None:
    """Test occurrence/cluster/bibliography generation from review_draft_v2 blocks."""
    review_draft = build_review_draft_v2(
        job_id="test-job",
        project_name="test",
        draft_id="draft-1",
        outline_artifact_id="outline-1",
        outline_source_path="/path/outline.md",
        summary_file="/path/summaries.json",
        review_word_path="/path/review.docx",
        sections=[
            {
                "section_number": 1,
                "section_title": "Introduction",
                "content": "This is the introduction with citation (Smith, 2024). Another sentence.",
            },
            {
                "section_number": 2,
                "section_title": "Methods",
                "content": "Methods section cites (Johnson, 2023) and (Smith, 2024) again.",
            },
        ],
        references=["Smith, J. (2024). AI Paper.", "Johnson, A. (2023). Methods Paper."],
        generation_mode="test",
    )

    paper_summaries = [
        {
            "paper_info": {
                "title": "AI Paper",
                "authors": ["Smith"],
                "year": "2024",
            }
        },
        {
            "paper_info": {
                "title": "Methods Paper",
                "authors": ["Johnson"],
                "year": "2023",
            }
        },
    ]

    manifest = build_citation_manifest_v2_from_review_draft(
        job_id="test-job",
        project_name="test",
        manifest_id="manifest-1",
        review_draft_path="/path/draft.json",
        review_word_path="/path/review.docx",
        review_draft_v2=review_draft.to_dict(),
        paper_summaries=paper_summaries,
    )

    # Verify structure
    assert manifest.artifact_version == "v2"
    assert len(manifest.occurrences) > 0
    assert len(manifest.clusters) > 0
    assert len(manifest.bibliography) == 2

    # Check occurrences have proper structure
    for occ in manifest.occurrences:
        assert occ.occurrence_id
        assert occ.citation_token
        assert occ.section_number > 0
        assert occ.block_id

    # Check clusters group occurrences by paper
    for cluster in manifest.clusters:
        assert cluster.cluster_id
        assert cluster.paper_id
        assert len(cluster.occurrence_ids) > 0
        assert cluster.total_occurrences == len(cluster.occurrence_ids)

    # Check bibliography marks cited papers
    cited_entries = [e for e in manifest.bibliography if e.is_cited]
    assert len(cited_entries) > 0


def test_validator_consumes_v2_occurrences(tmp_path: Path) -> None:
    """Test that validator properly consumes v2 occurrences as primary input."""
    from validation.review_validator import ReviewValidator

    # Create a v2-style citation manifest
    citation_manifest = {
        "artifact_type": "citation_manifest",
        "artifact_version": "v2",
        "created_from_job_id": "test-job",
        "created_at": "2026-04-04T00:00:00Z",
        "manifest_identity": {"manifest_id": "test-manifest"},
        "review_reference": {"review_draft_path": "/path/draft.json"},
        "occurrences": [
            {
                "occurrence_id": "occ_1",
                "citation_token": "(Smith, 2024)",
                "paper_id": "paper_1",
                "paper_key": "paper_1",
                "section_number": 1,
                "section_title": "Introduction",
                "block_id": "s1_b1",
                "block_order": 1,
                "spans": [],
                "context_before": "This is context",
                "context_after": "",
            }
        ],
        "clusters": [
            {
                "cluster_id": "cluster_1",
                "paper_id": "paper_1",
                "paper_key": "paper_1",
                "occurrence_ids": ["occ_1"],
                "first_occurrence_section": 1,
                "total_occurrences": 1,
            }
        ],
        "bibliography": [
            {
                "entry_id": "bib_1",
                "paper_id": "paper_1",
                "paper_key": "paper_1",
                "citation_text": "Smith, J. (2024). Test.",
                "is_cited": True,
                "cluster_id": "cluster_1",
            }
        ],
    }

    review_draft = {
        "content": {
            "sections": [
                {
                    "section_number": 1,
                    "section_title": "Introduction",
                    "blocks": [
                        {"block_id": "s1_b1", "text": "Test content (Smith, 2024)."}
                    ],
                }
            ],
            "references": ["Smith, J. (2024). Test."],
        }
    }

    paper_artifacts = [
        {
            "paper_identity": {
                "canonical_paper_key": "paper_1",
                "source_paper_id": "paper_1",
            },
            "analysis": {
                "ai_summary": {"findings": ["Finding 1"]}
            },
        }
    ]

    validator = ReviewValidator(review_draft, citation_manifest, paper_artifacts)
    report = validator.validate()

    # Verify validator used occurrences
    assert report.total_citations == 1
    assert len(report.citation_results) == 1


def test_validator_fallback_to_v1_citations(tmp_path: Path) -> None:
    """Test that validator falls back to v1 citations when v2 occurrences not present."""
    from validation.review_validator import ReviewValidator

    # Create a v1-style citation manifest (no occurrences)
    citation_manifest = {
        "artifact_type": "citation_manifest",
        "artifact_version": "v1",
        "created_from_job_id": "test-job",
        "created_at": "2026-04-04T00:00:00Z",
        "manifest_identity": {"manifest_id": "test-manifest"},
        "review_reference": {"review_draft_path": "/path/draft.json"},
        "citations": [
            {
                "citation_id": "cite_1",
                "paper_id": "paper_1",
                "text": "(Smith, 2024)",
                "context": "Context",
                "section_number": 1,
                "section_title": "Introduction",
            }
        ],
    }

    review_draft = {"content": {"sections": [], "references": []}}
    paper_artifacts = [
        {
            "paper_identity": {"canonical_paper_key": "paper_1"},
            "analysis": {"ai_summary": {}},
        }
    ]

    validator = ReviewValidator(review_draft, citation_manifest, paper_artifacts)
    report = validator.validate()

    # Verify validator fell back to v1 citations
    assert report.total_citations == 1


def test_failed_sections_do_not_register_citation_manifest(tmp_path: Path, monkeypatch) -> None:
    """Test that citation manifest is not registered when sections fail."""
    generator, workspace, _registry = _make_bound_generator(tmp_path, job_id="job-failed-sections")
    _stub_stage2_bootstrap(monkeypatch, generator)

    outline_text = "# Demo Outline\n\n## 1. First Section\n\n## 2. Second Section"
    outline_path = Path(workspace.artifact_path("demo_literature_review_outline.md"))
    outline_path.write_text(outline_text, encoding="utf-8")

    def _section_content(section_title: str, _outline: str):
        if section_title == "Second Section":
            return None  # Simulate failure
        return f"{section_title} generated content."

    monkeypatch.setattr(generator, "_load_outline_artifact", lambda: (str(outline_path), outline_text))
    monkeypatch.setattr(generator, "generate_review_section_content", _section_content)
    monkeypatch.setattr(generator, "generate_apa_references", lambda: [])

    # Generate should succeed even with failed sections
    assert generator.generate_full_review_from_outline() is True

    # But citation manifest should not be registered
    v2_path = Path(workspace.artifact_path("citation_manifests/demo_citation_manifest_v2.json"))
    assert not v2_path.exists(), "Citation manifest should not exist when sections fail"

    registry_path = Path(workspace.paths.registry_path)
    if registry_path.exists():
        registry_payload = json.loads(registry_path.read_text(encoding="utf-8"))
        citation_records = [item for item in registry_payload["artifacts"] if item["artifact_type"] == "citation_manifest"]
        assert len(citation_records) == 0, "No citation manifest should be registered on failure"


def test_citation_manifest_v2_roundtrip(tmp_path: Path) -> None:
    """Test that CitationManifestV2 can be serialized and deserialized correctly."""
    from services.citation_manifest import CitationManifestV2, CitationOccurrence, CitationCluster, BibliographyEntry

    manifest = CitationManifestV2(
        artifact_type="citation_manifest",
        artifact_version="v2",
        created_from_job_id="test-job",
        created_at="2026-04-04T00:00:00Z",
        manifest_identity={"manifest_id": "test"},
        review_reference={"review_draft_path": "/path/draft.json"},
        occurrences=[
            CitationOccurrence(
                occurrence_id="occ_1",
                citation_token="(Smith, 2024)",
                paper_id="paper_1",
                paper_key="paper_1",
                section_number=1,
                section_title="Intro",
                block_id="b1",
                block_order=1,
            )
        ],
        clusters=[
            CitationCluster(
                cluster_id="cluster_1",
                paper_id="paper_1",
                paper_key="paper_1",
                occurrence_ids=["occ_1"],
                first_occurrence_section=1,
                total_occurrences=1,
            )
        ],
        bibliography=[
            BibliographyEntry(
                entry_id="bib_1",
                paper_id="paper_1",
                paper_key="paper_1",
                citation_text="Smith, J. (2024). Test.",
                is_cited=True,
            )
        ],
    )

    # Serialize
    data = manifest.to_dict()

    # Deserialize
    restored = CitationManifestV2.from_dict(data)

    # Verify
    assert restored.artifact_version == "v2"
    assert len(restored.occurrences) == 1
    assert restored.occurrences[0].occurrence_id == "occ_1"
    assert len(restored.clusters) == 1
    assert restored.clusters[0].cluster_id == "cluster_1"
    assert len(restored.bibliography) == 1
    assert restored.bibliography[0].entry_id == "bib_1"
