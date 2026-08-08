from __future__ import annotations

from dataclasses import asdict, replace
import json
import multiprocessing
from pathlib import Path
from typing import Any, cast

import pytest

from runtime.lifecycle import (
    bootstrap_job_runtime,
    finalize_job_runtime,
    publish_running_job_runtime,
)
from services.job_runner import JobRunRequest, JobRunner
from services.job_workspace import JobWorkspace, atomic_write_json
from services.source_inventory import build_source_inventory


class _DummyGenerator:
    def __init__(self, output_root: Path) -> None:
        self.config: dict[str, dict[str, str]] = {"Paths": {"output_path": str(output_root)}}
        self.bound: dict[str, Any] = {}

    def bind_job_workspace(self, **kwargs: Any) -> None:
        self.bound = dict(kwargs)


def _source_inventory(root: Path) -> Any:
    pdf = root / "papers" / "fixture.pdf"
    pdf.parent.mkdir(parents=True, exist_ok=True)
    pdf.write_bytes(b"%PDF-1.4\nfixture")
    return build_source_inventory(
        source_mode="direct",
        project_name="demo",
        pdf_root=pdf.parent,
        pdf_paths=[pdf],
    )


def _write_resume_report(workspace: JobWorkspace, report: Any) -> str:
    path = workspace.artifact_path("resume_state_report.json")
    atomic_write_json(path, asdict(report))
    return path


def _concurrent_explicit_bootstrap_worker(
    output_root: str,
    start_barrier: Any,
    workspace_claimed: Any,
    release_winner: Any,
    results: Any,
) -> None:
    root = Path(output_root)
    generator = _DummyGenerator(root)
    runner = JobRunner()
    request = JobRunRequest(
        config="config.ini",
        project_name="demo",
        pdf_folder=str(root / "papers"),
        action="run_all",
        job_id="fixed-job",
    )

    def blocking_build_workspace(**kwargs: Any) -> JobWorkspace:
        workspace_claimed.set()
        if not release_winner.wait(timeout=30):
            raise RuntimeError("timed out waiting to release workspace winner")
        return runner._build_workspace(**kwargs)

    try:
        start_barrier.wait(timeout=30)
        context = bootstrap_job_runtime(
            request=request,
            generator=generator,
            project_name="demo",
            source_snapshot={"pdf_folder": str(root / "papers")},
            request_snapshot={"action": "run_all"},
            source_inventory=_source_inventory(root),
            build_workspace=blocking_build_workspace,
            write_resume_report=_write_resume_report,
            publish_running_state=False,
        )
        results.put(("completed", context.workspace.root_dir))
    except BaseException as exc:
        results.put(("error", type(exc).__name__, str(exc)))


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
        source_inventory=_source_inventory(tmp_path),
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


def test_concurrent_explicit_job_claim_rejects_loser_before_workspace_mutation(
    tmp_path: Path,
) -> None:
    process_context = multiprocessing.get_context("spawn")
    start_barrier = process_context.Barrier(2)
    workspace_claimed = process_context.Event()
    release_winner = process_context.Event()
    results = process_context.Queue()
    processes = [
        process_context.Process(
            target=_concurrent_explicit_bootstrap_worker,
            args=(
                str(tmp_path),
                start_barrier,
                workspace_claimed,
                release_winner,
                results,
            ),
        )
        for _index in range(2)
    ]
    for process in processes:
        process.start()

    try:
        assert workspace_claimed.wait(timeout=30)
        loser = results.get(timeout=30)
        assert loser[0] == "error"
        assert "workspace already exists" in loser[2]

        workspace_path = tmp_path / "demo__fixed-job"
        assert workspace_path.is_dir()
        assert [path for path in workspace_path.rglob("*") if path.is_file()] == []

        release_winner.set()
        winner = results.get(timeout=30)
        assert winner == ("completed", str(workspace_path))
    finally:
        release_winner.set()
        for process in processes:
            process.join(timeout=30)
            if process.is_alive():
                process.terminate()
                process.join(timeout=10)

    assert all(process.exitcode is not None for process in processes)
    assert sorted(cast(int, process.exitcode) for process in processes) == [0, 0]


def test_resume_does_not_steal_latest_pointer_from_newer_job(tmp_path: Path) -> None:
    latest = JobWorkspace.create(str(tmp_path), "demo", job_id="newer-job")
    latest.write_latest_pointer(
        resume_state="strong_resumable",
        fingerprint_bundle={"job_fingerprint": "newer"},
        status="running",
    )
    generator = _DummyGenerator(tmp_path)
    runner = JobRunner()
    request = JobRunRequest(
        config="config.ini",
        project_name="demo",
        pdf_folder=str(tmp_path / "papers"),
        action="run_all",
        job_id="older-job",
    )

    context = bootstrap_job_runtime(
        request=request,
        generator=generator,
        project_name="demo",
        source_snapshot={"pdf_folder": str(tmp_path / "papers")},
        request_snapshot={"action": "run_all", "attempt": "resume"},
        source_inventory=_source_inventory(tmp_path),
        build_workspace=runner._build_workspace,
        write_resume_report=_write_resume_report,
        claim_latest_pointer=False,
    )
    finalize_job_runtime(
        context=context,
        write_resume_report=_write_resume_report,
        status="completed",
    )

    pointer = json.loads(Path(context.pointer_path).read_text(encoding="utf-8"))
    assert pointer["job_id"] == "newer-job"
    assert pointer["status"] == "running"


def test_running_outcome_is_durable_before_latest_pointer_update(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
        source_inventory=_source_inventory(tmp_path),
        build_workspace=runner._build_workspace,
        write_resume_report=_write_resume_report,
        publish_running_state=False,
    )
    context = replace(context, attempt_number=2, resumed_from_attempt=1)

    def fail_pointer_write(_self: JobWorkspace, **_kwargs: Any) -> str:
        raise RuntimeError("pointer write failed")

    monkeypatch.setattr(JobWorkspace, "write_latest_pointer", fail_pointer_write)

    with pytest.raises(RuntimeError, match="pointer write failed"):
        publish_running_job_runtime(context, claim_latest_pointer=True)

    outcome = json.loads(Path(context.job_outcome_path).read_text(encoding="utf-8"))
    assert outcome["job_status"] == "running"
    assert outcome["attempt_number"] == 2
    assert outcome["resumed_from_attempt"] == 1
    context.registry.reload()
    registered = context.registry.get("job_outcome")
    assert registered is not None
    assert registered.metadata["job_status"] == "running"


def test_bootstrap_rejects_non_boolean_readiness_policy(tmp_path: Path) -> None:
    generator = _DummyGenerator(tmp_path)
    runner = JobRunner()
    request = JobRunRequest(
        config="config.ini",
        project_name="demo",
        pdf_folder=str(tmp_path / "papers"),
        action="run_all",
    )
    object.__setattr__(request, "validation_required", "false")

    with pytest.raises(ValueError, match="validation_required must be a boolean"):
        bootstrap_job_runtime(
            request=request,
            generator=generator,
            project_name="demo",
            source_snapshot={"pdf_folder": str(tmp_path / "papers")},
            request_snapshot={"action": "run_all"},
            source_inventory=_source_inventory(tmp_path),
            build_workspace=runner._build_workspace,
            write_resume_report=_write_resume_report,
        )
