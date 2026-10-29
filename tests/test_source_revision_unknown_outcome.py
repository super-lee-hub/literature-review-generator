"""Verified parent responses cannot settle an unknown child physical attempt."""
import json

import pytest

from runtime.provider_runtime import (
    ProviderAggregateBudgetV2, ProviderBudgetController, ProviderRuntimeContractError,
)


def test_parent_reuse_does_not_erase_started_child_reservation_on_restart(tmp_path):
    budget = ProviderAggregateBudgetV2(10, 10000, 0, 600)
    controller = ProviderBudgetController(budget)
    state = tmp_path / "finite-budget.json"
    controller.bind_state_path(state)
    identity = {"job_id": "child", "stage_name": "outline_v3", "call_id": "outline:topic"}
    reservation = controller.admit(requested_output_tokens=100, context=identity)
    controller.mark_transport_started(reservation)
    original = json.loads(state.read_bytes())
    restarted = ProviderBudgetController(budget)
    restarted.bind_state_path(state)
    before = state.read_bytes()
    rebound = json.loads(before)
    for field in ("budget", "absolute_deadline_epoch", "calls_used", "reservations"):
        assert rebound[field] == original[field]
    with pytest.raises(ProviderRuntimeContractError, match="requires reconciliation"):
        restarted.assert_response_reuse_allowed(**identity)
    restarted.assert_response_reuse_allowed(**{**identity, "call_id": "outline:independent-topic"})
    assert state.read_bytes() == before
    assert restarted.snapshot()["calls_reserved"] == 1
    assert restarted.snapshot()["output_tokens_reserved"] == 100


def test_unstarted_child_reservation_does_not_claim_an_unknown_provider_outcome():
    controller = ProviderBudgetController(ProviderAggregateBudgetV2(10, 10000, 0, 600))
    identity = {"job_id": "child", "stage_name": "stage3_review", "call_id": "review:section"}
    controller.admit(requested_output_tokens=100, context=identity)
    before = controller.snapshot()
    controller.assert_response_reuse_allowed(**identity)
    after = controller.snapshot()
    assert {k: v for k, v in before.items() if k != "elapsed_seconds"} == {
        k: v for k, v in after.items() if k != "elapsed_seconds"
    }
