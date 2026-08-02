"""Workflow facade shared by CLI-adjacent tools and the local GUI."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from typing import Any, Optional

from services.job_runner import JobRunRequest, build_job_request_from_args


@dataclass
class WorkflowResult:
    success: bool
    exit_code: int = 0
    message: str = ""


def build_args(
    *,
    config: str = "config.ini",
    project_name: Optional[str] = None,
    pdf_folder: Optional[str] = None,
    summary_file: Optional[str] = None,
    summary_sources: Optional[list[str]] = None,
    reuse_stage1: Optional[bool] = None,
    reuse_summary_files: Optional[list[str]] = None,
    run_all: bool = False,
    analyze_only: bool = False,
    generate_outline: bool = False,
    generate_review: bool = False,
    generate_section: Optional[int] = None,
    validate_review: bool = False,
    setup: bool = False,
    prime_with_folder: Optional[str] = None,
    concept: Optional[str] = None,
    retry_failed: bool = False,
    retry_review_failed: bool = False,
    merge: Optional[str] = None,
    free_mode_profile: Optional[str] = None,
    free_mode_idea: Optional[str] = None,
    gui: bool = False,
    progress_tracker: Optional[Any] = None,
    cancel_token: Optional[Any] = None,
    zotero_report: Optional[str] = None,
    library_path: Optional[str] = None,
    queue_file: str = "output/_queue/queue.json",
) -> argparse.Namespace:
    """Create a Namespace for the shared current workflow facade."""

    namespace = argparse.Namespace(
        config=config,
        project_name=project_name,
        pdf_folder=pdf_folder,
        summary_file=summary_file,
        summary_sources=list(summary_sources or []),
        reuse_stage1=reuse_stage1,
        reuse_summary_files=list(reuse_summary_files or []),
        run_all=run_all,
        analyze_only=analyze_only,
        generate_outline=generate_outline,
        generate_review=generate_review,
        generate_section=generate_section,
        validate_review=validate_review,
        setup=setup,
        prime_with_folder=prime_with_folder,
        concept=concept,
        retry_failed=retry_failed,
        retry_review_failed=retry_review_failed,
        merge=merge,
        gui=gui,
        zotero_report=zotero_report,
        library_path=library_path,
        queue_file=queue_file,
    )
    setattr(namespace, "free_mode_profile", free_mode_profile)
    setattr(namespace, "free_mode_idea", free_mode_idea)
    setattr(namespace, "_progress_tracker", progress_tracker)
    setattr(namespace, "_cancel_token", cancel_token)
    return namespace


def run_workflow(args: argparse.Namespace, cancel_token: Optional[Any] = None) -> WorkflowResult:
    """Run a GUI request through the same current JobRunner as the CLI."""

    if cancel_token is not None and getattr(args, "_cancel_token", None) is None:
        setattr(args, "_cancel_token", cancel_token)
    progress_tracker = getattr(args, "_progress_tracker", None)
    try:
        request = build_job_request(args)
        from services.job_runner import JobRunner

        result = JobRunner().run(request, cancel_token=cancel_token)
        if progress_tracker is not None:
            progress_tracker.finish(success=result.success, message=result.message)
        return WorkflowResult(success=result.success, exit_code=result.exit_code, message=result.message)
    except Exception as exc:  # pragma: no cover - surfaced to GUI.
        if progress_tracker is not None:
            progress_tracker.finish(success=False, message=str(exc))
        return WorkflowResult(success=False, exit_code=1, message=str(exc))


def build_job_request(args: argparse.Namespace) -> JobRunRequest:
    """Translate GUI arguments into the shared typed job request."""

    return build_job_request_from_args(args)
