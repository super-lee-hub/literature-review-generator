from services.progress_service import ProgressTracker


def test_finish_success_clears_indeterminate_and_completes_counts() -> None:
    tracker = ProgressTracker()
    tracker.reset(task_type="demo", stage="stage-1", indeterminate=True)
    tracker.emit(current=2, total=5)

    tracker.finish(success=True, message="done")

    snapshot = tracker.snapshot()
    assert snapshot["status"] == "completed"
    assert snapshot["indeterminate"] is False
    assert snapshot["current"] == 5
    assert snapshot["remaining_count"] == 0
    assert snapshot["message"] == "done"


def test_finish_failure_clears_indeterminate_without_overwriting_progress() -> None:
    tracker = ProgressTracker()
    tracker.reset(task_type="demo", stage="stage-1", indeterminate=True)
    tracker.emit(current=2, total=5)

    tracker.finish(success=False, message="failed")

    snapshot = tracker.snapshot()
    assert snapshot["status"] == "failed"
    assert snapshot["indeterminate"] is False
    assert snapshot["current"] == 2
    assert snapshot["remaining_count"] == 3
    assert snapshot["message"] == "failed"
