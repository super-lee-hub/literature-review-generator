from __future__ import annotations

import pytest

from runtime.provider_runtime import (
    ProviderAggregateBudgetV1,
    ProviderAggregateBudgetV2,
    ProviderBudgetController,
    ProviderBudgetExceeded,
    ProviderBudgetV1,
    ProviderRuntime,
)


@pytest.mark.parametrize(
    ("shared_retry_limit", "expected_attempts"),
    [(0, 1), (1, 2), (6, 7)],
)
def test_v2_max_attempts_clamps_to_remaining_shared_retry_allowance(
    shared_retry_limit: int,
    expected_attempts: int,
) -> None:
    controller = ProviderBudgetController(
        ProviderAggregateBudgetV2(
            max_provider_calls_total=20,
            max_output_tokens_total=10_000,
            max_retry_attempts_total=shared_retry_limit,
            max_wall_seconds=300.0,
        )
    )
    runtime = ProviderRuntime(aggregate_budget=controller, test_only=True)

    assert runtime.max_attempts_for_call(12) == expected_attempts


def test_shared_provider_call_slots_clamp_attempts_and_zero_slots_still_reject_admission() -> None:
    available_controller = ProviderBudgetController(
        ProviderAggregateBudgetV2(
            max_provider_calls_total=3,
            max_output_tokens_total=10_000,
            max_retry_attempts_total=6,
            max_wall_seconds=300.0,
        )
    )
    available_runtime = ProviderRuntime(
        aggregate_budget=available_controller,
        test_only=True,
    )
    assert available_runtime.max_attempts_for_call(12) == 3

    exhausted_controller = ProviderBudgetController(
        ProviderAggregateBudgetV2(
            max_provider_calls_total=0,
            max_output_tokens_total=10_000,
            max_retry_attempts_total=6,
            max_wall_seconds=300.0,
        )
    )
    exhausted_runtime = ProviderRuntime(
        aggregate_budget=exhausted_controller,
        test_only=True,
    )
    # A transport still has an initial attempt; aggregate admission rejects it.
    assert exhausted_runtime.max_attempts_for_call(12) == 1
    with pytest.raises(ProviderBudgetExceeded, match="provider call"):
        exhausted_runtime.admit(requested_retry_attempts=0)


def test_v1_zero_shared_limits_remain_unbounded_but_positive_limits_apply() -> None:
    unbounded = ProviderRuntime(
        aggregate_budget=ProviderBudgetController(
            ProviderAggregateBudgetV1(
                max_provider_calls_total=0,
                max_output_tokens_total=10_000,
                max_retry_attempts_total=0,
                max_wall_seconds=300.0,
            )
        ),
        test_only=True,
    )
    assert unbounded.max_attempts_for_call(12) == 12

    call_limited = ProviderRuntime(
        aggregate_budget=ProviderBudgetController(
            ProviderAggregateBudgetV1(
                max_provider_calls_total=5,
                max_output_tokens_total=10_000,
                max_retry_attempts_total=0,
                max_wall_seconds=300.0,
            )
        ),
        test_only=True,
    )
    assert call_limited.max_attempts_for_call(12) == 5

    retry_limited = ProviderRuntime(
        aggregate_budget=ProviderBudgetController(
            ProviderAggregateBudgetV1(
                max_provider_calls_total=0,
                max_output_tokens_total=10_000,
                max_retry_attempts_total=2,
                max_wall_seconds=300.0,
            )
        ),
        test_only=True,
    )
    assert retry_limited.max_attempts_for_call(12) == 3


def test_local_per_call_retry_cap_still_applies_with_shared_budget() -> None:
    runtime = ProviderRuntime(
        budget=ProviderBudgetV1(max_retries_per_call=2),
        aggregate_budget=ProviderBudgetController(
            ProviderAggregateBudgetV2(
                max_provider_calls_total=20,
                max_output_tokens_total=10_000,
                max_retry_attempts_total=6,
                max_wall_seconds=300.0,
            )
        ),
        test_only=True,
    )

    assert runtime.max_attempts_for_call(12) == 3


def test_max_attempts_uses_resumed_shared_used_and_reserved_totals(tmp_path) -> None:
    budget = ProviderAggregateBudgetV2(
        max_provider_calls_total=20,
        max_output_tokens_total=10_000,
        max_retry_attempts_total=6,
        max_wall_seconds=300.0,
    )
    state_path = tmp_path / "shared-budget.json"
    first = ProviderBudgetController(budget)
    first.bind_state_path(state_path, acceptance_run_id="shared-retry-runtime-test")

    used_reservation = first.admit(requested_retry_attempts=2)
    first.complete(used_reservation, {"attempts": 2, "output_tokens": 0})
    first.admit(requested_retry_attempts=1)

    resumed = ProviderBudgetController(budget)
    resumed.bind_state_path(
        state_path,
        acceptance_run_id="shared-retry-runtime-test",
        state_started=True,
    )
    snapshot = resumed.snapshot()
    assert snapshot["retry_attempts_used"] == 1
    assert snapshot["retry_attempts_reserved"] == 1
    assert snapshot["calls_used"] == 2
    assert snapshot["calls_reserved"] == 2

    runtime = ProviderRuntime(aggregate_budget=resumed, test_only=True)
    # Six retries less one used and one still reserved leave four; four retries
    # permit five total attempts. The call cap has more room.
    assert runtime.max_attempts_for_call(12) == 5


def test_ambiguous_inflight_reservation_stays_blocked_at_atomic_admission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller = ProviderBudgetController(
        ProviderAggregateBudgetV2(
            max_provider_calls_total=20,
            max_output_tokens_total=10_000,
            max_retry_attempts_total=6,
            max_wall_seconds=300.0,
        )
    )
    reservation = controller.admit(requested_retry_attempts=2)
    controller.mark_transport_started(reservation)
    monkeypatch.setattr(controller, "_reservation_owner_liveness", lambda _reservation: "dead")

    with pytest.raises(ProviderBudgetExceeded, match="ambiguous orphaned"):
        controller.reconcile_orphaned_reservations()

    runtime = ProviderRuntime(aggregate_budget=controller, test_only=True)
    # The snapshot still accounts for the reservation, but admission remains
    # fail-closed because its transport result is ambiguous.
    assert runtime.max_attempts_for_call(12) == 5
    with pytest.raises(ProviderBudgetExceeded, match="ambiguous reservations"):
        runtime.admit(requested_retry_attempts=0)


def test_atomic_admission_rejects_a_stale_attempt_limit_after_another_call_uses_retries() -> None:
    controller = ProviderBudgetController(
        ProviderAggregateBudgetV2(
            max_provider_calls_total=10,
            max_output_tokens_total=10_000,
            max_retry_attempts_total=1,
            max_wall_seconds=300.0,
        )
    )
    runtime = ProviderRuntime(aggregate_budget=controller, test_only=True)

    stale_attempt_limit = runtime.max_attempts_for_call(8)
    assert stale_attempt_limit == 2

    competing_reservation = controller.admit(requested_retry_attempts=1)
    controller.complete(competing_reservation, {"attempts": 2, "output_tokens": 0})

    with pytest.raises(ProviderBudgetExceeded, match="retry budget"):
        runtime.admit(requested_retry_attempts=stale_attempt_limit - 1)
