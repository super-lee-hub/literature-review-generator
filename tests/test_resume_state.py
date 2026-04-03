import json

from services.progress_state import Stage1ProgressSnapshot, determine_resume_state, write_stage1_progress_snapshot


def test_resume_state_is_weak_for_summaries_only(tmp_path) -> None:
    summary_file = tmp_path / "artifacts" / "demo_summaries.json"
    summary_file.parent.mkdir(parents=True)
    summary_file.write_text(json.dumps([{"status": "success"}]), encoding="utf-8")

    report = determine_resume_state(
        project_name="demo",
        job_id="job-1",
        summary_file=str(summary_file),
        progress_snapshot_file=None,
        checkpoint_file=None,
        expected_fingerprint_bundle={"request": "a"},
    )

    assert report.state == "weak_resumable"


def test_resume_state_is_strong_when_snapshot_matches(tmp_path) -> None:
    summary_file = tmp_path / "artifacts" / "demo_summaries.json"
    progress_file = tmp_path / "artifacts" / "stage1_progress_snapshot.json"
    summary_file.parent.mkdir(parents=True)
    summary_file.write_text(json.dumps([{"status": "success"}]), encoding="utf-8")

    snapshot = Stage1ProgressSnapshot(
        artifact_type="stage1_progress_snapshot",
        artifact_version="v1",
        created_from_job_id="job-1",
        created_at="2026-04-02T00:00:00Z",
        project_name="demo",
        job_id="job-1",
        summary_file=str(summary_file),
        summary_count=1,
        processed_papers=["paper-1"],
        failed_papers=[],
        fingerprint_bundle={"request": "a"},
        checkpoint_file=None,
    )
    write_stage1_progress_snapshot(str(progress_file), snapshot)

    report = determine_resume_state(
        project_name="demo",
        job_id="job-1",
        summary_file=str(summary_file),
        progress_snapshot_file=str(progress_file),
        checkpoint_file=None,
        expected_fingerprint_bundle={"request": "a"},
    )

    assert report.state == "strong_resumable"


def test_resume_state_is_non_resumable_when_fingerprint_mismatches(tmp_path) -> None:
    summary_file = tmp_path / "artifacts" / "demo_summaries.json"
    progress_file = tmp_path / "artifacts" / "stage1_progress_snapshot.json"
    summary_file.parent.mkdir(parents=True)
    summary_file.write_text(json.dumps([{"status": "success"}]), encoding="utf-8")

    snapshot = Stage1ProgressSnapshot(
        artifact_type="stage1_progress_snapshot",
        artifact_version="v1",
        created_from_job_id="job-1",
        created_at="2026-04-02T00:00:00Z",
        project_name="demo",
        job_id="job-1",
        summary_file=str(summary_file),
        summary_count=1,
        processed_papers=["paper-1"],
        failed_papers=[],
        fingerprint_bundle={"request": "old"},
        checkpoint_file=None,
    )
    write_stage1_progress_snapshot(str(progress_file), snapshot)

    report = determine_resume_state(
        project_name="demo",
        job_id="job-2",
        summary_file=str(summary_file),
        progress_snapshot_file=str(progress_file),
        checkpoint_file=None,
        expected_fingerprint_bundle={"request": "new"},
    )

    assert report.state == "non_resumable"
