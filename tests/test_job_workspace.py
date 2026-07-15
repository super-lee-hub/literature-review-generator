from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import multiprocessing
from pathlib import Path
import re

from services.job_workspace import JobWorkspace


def _claim_pointer_repeatedly(base: str, job_id: str) -> None:
    workspace = JobWorkspace(base, "project", job_id)
    for _ in range(25):
        workspace.write_latest_pointer(
            resume_state="running",
            fingerprint_bundle={"job": job_id},
            status="running",
        )


def test_generated_job_ids_have_random_suffix_and_do_not_collide() -> None:
    with ThreadPoolExecutor(max_workers=16) as executor:
        job_ids = list(executor.map(lambda _index: JobWorkspace.generate_job_id(), range(512)))

    assert len(set(job_ids)) == len(job_ids)
    assert all(re.fullmatch(r"\d{8}_\d{6}_[0-9a-f]{8}", job_id) for job_id in job_ids)


def test_old_job_cannot_overwrite_new_latest_pointer(tmp_path: Path) -> None:
    old = JobWorkspace.create(str(tmp_path), "project", "old-job")
    new = JobWorkspace.create(str(tmp_path), "project", "new-job")
    old.write_latest_pointer(resume_state="running", fingerprint_bundle={"job": "old"}, status="running")
    new.write_latest_pointer(resume_state="running", fingerprint_bundle={"job": "new"}, status="running")

    assert old.write_latest_pointer_if_owned(
        resume_state="complete",
        fingerprint_bundle={"job": "old"},
        status="completed",
    ) is False
    payload = json.loads(Path(new.latest_pointer_path()).read_text(encoding="utf-8"))
    assert payload["job_id"] == "new-job"
    assert payload["status"] == "running"


def test_latest_pointer_owner_can_finalize_itself(tmp_path: Path) -> None:
    workspace = JobWorkspace.create(str(tmp_path), "project", "only-job")
    workspace.write_latest_pointer(resume_state="running", fingerprint_bundle={}, status="running")

    assert workspace.write_latest_pointer_if_owned(
        resume_state="complete",
        fingerprint_bundle={},
        status="completed",
    ) is True
    payload = json.loads(Path(workspace.latest_pointer_path()).read_text(encoding="utf-8"))
    assert payload["status"] == "completed"


def test_latest_pointer_claim_is_cross_process_atomic(tmp_path: Path) -> None:
    context = multiprocessing.get_context("spawn")
    processes = [
        context.Process(target=_claim_pointer_repeatedly, args=(str(tmp_path), job_id))
        for job_id in ("process-a", "process-b")
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=20)
        assert process.exitcode == 0

    pointer = Path(tmp_path) / "project" / "_latest_job.json"
    payload = json.loads(pointer.read_text(encoding="utf-8"))
    assert payload["job_id"] in {"process-a", "process-b"}
    loser = "process-b" if payload["job_id"] == "process-a" else "process-a"
    assert JobWorkspace(str(tmp_path), "project", loser).write_latest_pointer_if_owned(
        resume_state="complete",
        fingerprint_bundle={"job": loser},
        status="completed",
    ) is False
