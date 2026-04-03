import pytest

from services.queue_service import CancelToken, InProcessQueueService, JobCancelledError


def test_queue_service_runs_job_and_returns_handle() -> None:
    service = InProcessQueueService()
    token = CancelToken()

    handle = service.run(lambda cancel_token: {"cancelled": cancel_token.is_cancelled()}, cancel_token=token)

    assert handle.status == "completed"
    assert handle.result == {"cancelled": False}
    assert handle.error is None


def test_queue_service_propagates_cancellation() -> None:
    service = InProcessQueueService()
    token = CancelToken()
    token.request_cancel()

    handle = service.run(lambda cancel_token: cancel_token.check_cancelled(), cancel_token=token)

    assert handle.status == "cancelled"
    assert isinstance(handle.error, JobCancelledError)
    assert handle.result is None


def test_cancel_token_raises_after_request() -> None:
    token = CancelToken()
    token.request_cancel()

    with pytest.raises(JobCancelledError):
        token.check_cancelled()
