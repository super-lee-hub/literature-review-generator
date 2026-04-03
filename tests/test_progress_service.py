import services.progress_service as progress_service
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


def test_running_snapshot_updates_elapsed_without_mutating_stored_state(monkeypatch) -> None:
    current_time = 100.0

    def _fake_time() -> float:
        return current_time

    monkeypatch.setattr(progress_service.time, "time", _fake_time)

    tracker = ProgressTracker()
    tracker.reset(task_type="demo", stage="stage-1")

    current_time = 105.5
    first_snapshot = tracker.snapshot()
    assert first_snapshot["elapsed_seconds"] == 5.5
    assert tracker._snapshot["elapsed_seconds"] == 0.0
    assert tracker._snapshot["updated_at"] == 100.0

    current_time = 107.0
    second_snapshot = tracker.snapshot()
    assert second_snapshot["elapsed_seconds"] == 7.0
    assert tracker._snapshot["elapsed_seconds"] == 0.0
    assert tracker._snapshot["updated_at"] == 100.0
