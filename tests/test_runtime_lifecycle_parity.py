from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from runtime.lifecycle import bootstrap_job_runtime, finalize_job_runtime
from services.job_runner import JobRunRequest, JobRunner
from services.job_workspace import JobWorkspace, atomic_write_json


class _DummyGenerator:
    def __init__(self, output_root: Path) -> None:
        self.config: dict[str, dict[str, str]] = {"Paths": {"output_path": str(output_root)}}
        self.bound: dict[str, Any] = {}

    def bind_job_workspace(self, **kwargs: Any) -> None:
        self.bound = dict(kwargs)


def _write_resume_report(workspace: JobWorkspace, report: Any) -> str:
    path = workspace.artifact_path("resume_state_report.json")
    atomic_write_json(path, asdict(report))
    return path


def test_bootstrap_and_finalize_runtime_match_pointer_contract(tmp_path: Path) -> None:
    generator = _DummyGenerator(tmp_path)
    runner = JobRunner()
    request = JobRunRequest(
        config="config.ini",
        project_name="demo",
        pdf_folder=str(tmp_path / "papers"),
        action="run_all",
    )

    context = bootstrap_job_runtime(
        request=request,
        generator=generator,
        project_name="demo",
        source_snapshot={"pdf_folder": str(tmp_path / "papers")},
        request_snapshot={"action": "run_all"},
        build_workspace=runner._build_workspace,
        write_resume_report=_write_resume_report,
    )

    assert context.workspace.root_dir
    assert generator.bound["workspace"].root_dir == context.workspace.root_dir
    pointer_payload = json.loads(Path(context.pointer_path).read_text(encoding="utf-8"))
    assert pointer_payload["status"] == "running"

    resume_state = finalize_job_runtime(
        context=context,
        write_resume_report=_write_resume_report,
        status="completed",
    )

    assert isinstance(resume_state, str)
    final_pointer_payload = json.loads(Path(context.pointer_path).read_text(encoding="utf-8"))
    assert final_pointer_payload["status"] == "completed"
