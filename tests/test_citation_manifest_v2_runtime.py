"""Runtime tests for canonical citation manifest persistence."""

import json
import os
import tempfile
from typing import cast

from main import LiteratureReviewGenerator
from models import SummariesList
from services.artifact_registry import ArtifactRegistry
from services.config_compat import CompatConfigView
from services.job_workspace import JobWorkspace
from services.progress_state import ResumeStateReport


class MockJobWorkspace:
    def __init__(self, root_dir: str):
        self.root_dir = root_dir
        self.job_id = "test_job"
        self.project_name = "test_project"

    def ensure_exists(self) -> None:
        os.makedirs(self.root_dir, exist_ok=True)

    def artifact_path(self, filename: str) -> str:
        return os.path.join(self.root_dir, filename)

    def report_path(self, filename: str) -> str:
        return os.path.join(self.root_dir, "reports", filename)

    def checkpoint_path(self, filename: str) -> str:
        return os.path.join(self.root_dir, "checkpoints", filename)


class MockArtifactRegistry:
    def __init__(self) -> None:
        self.registered_files = []

    def register_file(self, **kwargs):
        self.registered_files.append(kwargs)


class MockCompatConfigView:
    pass


class MockResumeStateReport:
    pass


def _build_generator(temp_dir: str) -> LiteratureReviewGenerator:
    workspace = MockJobWorkspace(temp_dir)
    workspace.ensure_exists()
    registry = MockArtifactRegistry()

    generator = LiteratureReviewGenerator(project_name="test_project")
    generator.job_workspace = cast(JobWorkspace, workspace)
    generator.artifact_registry = cast(ArtifactRegistry, registry)
    generator.compat_config = cast(CompatConfigView, MockCompatConfigView())
    generator.resume_state_report = cast(ResumeStateReport, MockResumeStateReport())
    generator.project_name = "test_project"
    generator.output_dir = temp_dir
    generator.summary_file = os.path.join(temp_dir, "test_summaries.json")
    generator.summaries = cast(
        SummariesList,
        [
            {
                "status": "success",
                "paper_info": {
                    "title": "Test Paper 1",
                    "authors": ["Author A", "Author B"],
                    "year": "2023",
                    "canonical_paper_key": "test_paper_1",
                },
                "ai_summary": {
                    "paper_metadata": {
                        "title": "Test Paper 1",
                        "authors": ["Author A", "Author B"],
                        "year": "2023",
                        "journal": "Journal of Testing",
                        "doi": "10.1000/test.paper",
                    }
                },
            }
        ],
    )
    return generator


def test_citation_manifest_v3_generation() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        generator = _build_generator(temp_dir)
        review_draft_v2 = {
            "artifact_type": "review_draft",
            "artifact_version": "v2",
            "content": {
                "sections": [
                    {
                        "section_number": 1,
                        "section_title": "Introduction",
                        "blocks": [
                            {
                                "block_id": "s1_b1",
                                "block_order": 1,
                                "text": "This is a test paragraph with [[cite:test_paper_1]].",
                                "citations": [
                                    {
                                        "local_ref_id": "cit1",
                                        "citation_token": "[[cite:test_paper_1]]",
                                        "paper_id": "test_paper_1",
                                        "paper_key": "test_paper_1",
                                    }
                                ],
                            }
                        ],
                    }
                ],
                "references": ["Author A, B. (2023). Test Paper 1. Journal of Testing."],
            },
        }

        review_draft_path = os.path.join(temp_dir, "review_drafts", "test_project_review_draft_v2.json")
        os.makedirs(os.path.dirname(review_draft_path), exist_ok=True)
        with open(review_draft_path, "w", encoding="utf-8") as handle:
            json.dump(review_draft_v2, handle)

        result = generator._persist_citation_manifest(
            review_draft_path=review_draft_path,
            review_word_path=os.path.join(temp_dir, "test_review.docx"),
        )

        assert result is True

        manifest_path = os.path.join(temp_dir, "citation_manifests", "test_project_citation_manifest_v3.json")
        migration_report_path = os.path.join(temp_dir, "citation_manifests", "test_project_citation_migration_report.json")
        compatibility_v1_path = os.path.join(temp_dir, "citation_manifests", "test_project_citation_manifest_v1.json")
        assert os.path.exists(manifest_path)
        assert os.path.exists(migration_report_path)
        assert os.path.exists(compatibility_v1_path)

        payload = json.loads(open(manifest_path, "r", encoding="utf-8").read())
        assert payload["artifact_version"] == "v3"
        assert payload["paper_entries"][0]["paper_id"] == "test_paper_1"
        assert payload["migration_report"]["contract_version"] == "v3"

        registry = cast(MockArtifactRegistry, generator.artifact_registry)
        registrations = [item for item in registry.registered_files if item.get("artifact_type") == "citation_manifest"]
        assert any(item.get("artifact_version") == "v3" for item in registrations)


def test_citation_manifest_requires_review_draft_v2_on_normal_path() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        generator = _build_generator(temp_dir)
        review_draft_path = os.path.join(temp_dir, "non_existent_draft.json")

        result = generator._persist_citation_manifest(
            review_draft_path=review_draft_path,
            review_word_path=os.path.join(temp_dir, "test_review.docx"),
        )

        assert result is False
