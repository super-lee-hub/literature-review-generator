"""Compatibility-free application entrypoint.

The executable control plane lives in :mod:`reviewctl`.  This module keeps a
small import surface for the GUI and library callers while all work is routed
through the typed runtime job specification.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from config_loader import load_config
from services.settings import ApplicationSettings


class LiteratureReviewGenerator:
    """Current stage host used by artifact-oriented library integrations."""

    REVIEW_DRAFT_ARTIFACT_VERSION = "v3"
    CITATION_MANIFEST_ARTIFACT_VERSION = "v3"
    REVIEW_DRAFT_ARTIFACT_TYPE = "review_draft"
    CITATION_MANIFEST_ARTIFACT_TYPE = "citation_manifest"

    def __init__(
        self,
        config: str | Mapping[str, Any] | None = None,
        project_name: str = "literature_review",
        pdf_folder: str | None = None,
        queue_file: str = "output/_queue/queue.json",
        zotero_report: str | None = None,
        library_path: str | None = None,
        **_: Any,
    ) -> None:
        self.config = (
            dict(config)
            if isinstance(config, Mapping)
            else load_config(str(config or "config.ini"))
        )
        self.settings = ApplicationSettings.from_config(self.config)
        self.project_name = str(project_name or "literature_review")
        self.pdf_folder = pdf_folder
        self.queue_file = queue_file
        self.zotero_report = zotero_report
        self.library_path = library_path
        self.summaries: list[dict[str, Any]] = []
        self.papers: list[dict[str, Any]] = []
        self.workspace: Any = None
        self.job_workspace: Any = None

    @staticmethod
    def get_paper_key(paper: Mapping[str, Any]) -> str:
        return str(
            paper.get("canonical_paper_key")
            or paper.get("source_paper_id")
            or paper.get("title")
            or "unknown-paper"
        ).strip()

    @staticmethod
    def format_review_content(value: Any) -> str:
        return str(value or "").strip()

    def bind_job_workspace(self, *, workspace: Any, artifact_registry: Any, settings: ApplicationSettings, **_: Any) -> None:
        self.workspace = workspace
        self.job_workspace = workspace
        self.artifact_registry = artifact_registry
        self.settings = settings


def build_parser():
    from reviewctl import build_parser as build_control_parser

    return build_control_parser()


def main(argv: list[str] | None = None) -> int:
    from reviewctl import main as run_control_plane

    return run_control_plane(argv)


if __name__ == "__main__":
    raise SystemExit(main())
