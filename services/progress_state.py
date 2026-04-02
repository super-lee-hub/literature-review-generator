from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Literal, Mapping, Optional

from services.job_workspace import atomic_write_json, utc_now_iso

ResumeStateKind = Literal["strong_resumable", "weak_resumable", "non_resumable"]


@dataclass(frozen=True)
class Stage1ProgressSnapshot:
    artifact_type: str
    artifact_version: str
    created_from_job_id: str
    created_at: str
    project_name: str
    job_id: str
    summary_file: str
    summary_count: int
    processed_papers: List[str]
    failed_papers: List[str]
    fingerprint_bundle: Dict[str, Any]
    checkpoint_file: str | None = None


@dataclass(frozen=True)
class ResumeStateReport:
    artifact_type: str
    artifact_version: str
    created_from_job_id: str
    created_at: str
    project_name: str
    job_id: str
    state: ResumeStateKind
    reason: str
    summary_file: str
    progress_snapshot_file: str | None
    checkpoint_file: str | None
    fingerprint_bundle: Dict[str, Any]


def write_stage1_progress_snapshot(path: str, snapshot: Stage1ProgressSnapshot) -> None:
    atomic_write_json(path, asdict(snapshot))


def load_stage1_progress_snapshot(path: str | None) -> Optional[Stage1ProgressSnapshot]:
    if not path or not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    return Stage1ProgressSnapshot(**payload)


def _summary_file_is_readable(path: str) -> bool:
    if not path or not os.path.exists(path):
        return False
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        return isinstance(payload, list)
    except Exception:
        return False


def determine_resume_state(
    *,
    project_name: str,
    job_id: str,
    summary_file: str,
    progress_snapshot_file: str | None,
    checkpoint_file: str | None,
    expected_fingerprint_bundle: Mapping[str, Any],
) -> ResumeStateReport:
    snapshot = load_stage1_progress_snapshot(progress_snapshot_file)
    summary_readable = _summary_file_is_readable(summary_file)

    state: ResumeStateKind
    reason: str
    if snapshot:
        if snapshot.fingerprint_bundle == dict(expected_fingerprint_bundle) and summary_readable:
            state = "strong_resumable"
            reason = "summary and progress snapshot match current fingerprint bundle"
        elif snapshot.fingerprint_bundle != dict(expected_fingerprint_bundle):
            state = "non_resumable"
            reason = "existing progress snapshot fingerprint does not match current request/config/source bundle"
        else:
            state = "non_resumable"
            reason = "progress snapshot exists but required summary artifact is missing or unreadable"
    elif summary_readable:
        state = "weak_resumable"
        reason = "summary exists without a matching progress snapshot"
    else:
        state = "non_resumable"
        reason = "required summary/progress artifacts are missing or unreadable"

    return ResumeStateReport(
        artifact_type="resume_state_report",
        artifact_version="v1",
        created_from_job_id=job_id,
        created_at=utc_now_iso(),
        project_name=project_name,
        job_id=job_id,
        state=state,
        reason=reason,
        summary_file=summary_file,
        progress_snapshot_file=progress_snapshot_file,
        checkpoint_file=checkpoint_file,
        fingerprint_bundle=dict(expected_fingerprint_bundle),
    )
