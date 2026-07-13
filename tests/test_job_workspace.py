from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import re

from services.job_workspace import JobWorkspace


def test_generated_job_ids_have_random_suffix_and_do_not_collide() -> None:
    with ThreadPoolExecutor(max_workers=16) as executor:
        job_ids = list(executor.map(lambda _index: JobWorkspace.generate_job_id(), range(512)))

    assert len(set(job_ids)) == len(job_ids)
    assert all(re.fullmatch(r"\d{8}_\d{6}_[0-9a-f]{8}", job_id) for job_id in job_ids)
