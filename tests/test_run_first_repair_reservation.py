from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
import time
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from queue import Empty
from threading import Thread
from typing import Any

import pytest

from outline.v3_artifacts import OutlineArtifact
from runtime.outline_v3_replay import ModelCallReplayKey, ModelCallReplayStore
from runtime.primary_repair_recovery import (
    PrimaryRepairRecoveryError,
    PrimaryRepairResponseJournal,
    PrimaryRepairScopeV1,
)
from runtime.provider_run_envelope import (
    ProviderRunEnvelopeError,
    ProviderRunEnvelopeV1,
    build_provider_run_envelope_v1,
)
from runtime.provider_runtime import (
    AcceptanceExecutionContextV1,
    ProviderAggregateBudgetV1,
    ProviderAggregateBudgetV2,
    ProviderBudgetController,
    ProviderBudgetExceeded,
    ProviderBudgetV1,
    ProviderRuntime,
    ProviderRuntimeContractError,
    ProviderRuntimeLedger,
    bind_acceptance_execution_context,
    bind_provider_run_budget,
    current_acceptance_execution_context,
    hash_json,
    hash_text,
    provider_budget_controller_from_environment,
)
from services.artifact_registry import ArtifactRegistry
from services.job_workspace import JobWorkspace, publish_json_artifact
from services.queue_service import LocalPublicationContext


def _scope(
    candidate_id: str = "candidate_1",
    *,
    request_hash: str | None = None,
) -> PrimaryRepairScopeV1:
    return PrimaryRepairScopeV1(
        job_id="repair-job",
        logical_attempt_identity="attempt-1",
        candidate_id=candidate_id,
        request_hash=request_hash or hash_json({"candidate_id": candidate_id}),
        route_identity="provider/model/endpoint",
        route_config_fingerprint_sha256="a" * 64,
        source_authority_sha256="b" * 64,
        plan_contract_sha256="c" * 64,
        schema_hash="d" * 64,
    )


def _budget(calls: int = 2) -> ProviderAggregateBudgetV2:
    return ProviderAggregateBudgetV2(
        max_provider_calls_total=calls,
        max_output_tokens_total=8192,
        max_retry_attempts_total=0,
        max_wall_seconds=120,
    )


def _full_stage_projection(
    budget: ProviderAggregateBudgetV2,
    *,
    lower_calls: int | None = 2,
    lower_output: int | None = 100,
    lower_retries: int | None = 0,
    upper_calls: int | None = None,
    upper_output: int | None = None,
    upper_wall: float | None = None,
    unknown_exposures: int = 2,
    admission: str = "incomplete_unknown_exposure",
) -> dict[str, Any]:
    return {
        "schema_version": "full-stage-provider-request-plan-v1",
        "aggregate_budget": budget.to_dict(),
        "projection_identity_hash": hash_json({"plan": "bounded-run-start"}),
        "totals": {
            "physical_attempts_known_lower_bound": lower_calls,
            "aggregate_output_tokens_known_lower_bound": lower_output,
            "retry_attempts_required_reserve": lower_retries,
            "physical_attempts_upper_bound": upper_calls,
            "logical_calls_upper_bound": upper_calls,
            "estimated_output_tokens_all_attempts": upper_output,
            "wall_seconds_upper_bound": upper_wall,
            "unknown_exposure_count": unknown_exposures,
        },
        "budget_status": {"admission": admission},
        "boundary": {"projection_only": True, "no_provider_posts": True},
    }


def _run_scope_hashes() -> dict[str, str]:
    return {
        "runtime_spec_sha256": "1" * 64,
        "effective_config_sha256": "2" * 64,
        "source_snapshot_sha256": "3" * 64,
        "review_goal_sha256": "4" * 64,
        "route_plan_sha256": "5" * 64,
        "stage_work_contract_sha256": "6" * 64,
    }


def _durable_run_controller(
    path: Path,
    *,
    calls: int = 2,
    acceptance_run_id: str = "",
) -> ProviderBudgetController:
    controller = ProviderBudgetController(_budget(calls=calls))
    controller.bind_state_path(
        path,
        acceptance_run_id=acceptance_run_id,
        state_started=False,
    )
    return controller


def _scope_race_worker(
    state_path: str,
    barrier: Any,
    release_event: Any,
    results: Any,
    scope_payload: dict[str, Any],
) -> None:
    scope = PrimaryRepairScopeV1.from_dict(scope_payload)
    controller = ProviderBudgetController(_budget(calls=1))
    controller.bind_state_path(
        state_path,
        acceptance_run_id="repair-reservation-race",
        state_started=True,
    )
    barrier.wait(timeout=15)
    try:
        reservation = controller.admit(
            requested_output_tokens=128,
            requested_retry_attempts=0,
            context={"logical_call": scope.candidate_id},
            primary_repair_scope=scope,
        )
    except Exception as exc:  # the losing process reports the specific denial
        results.put(("blocked", type(exc).__name__, str(exc)))
    else:
        results.put(("granted", reservation.reservation_id, ""))
    release_event.wait(timeout=15)


def _crash_after_response_journal_worker(
    state_path: str,
    journal_root: str,
    ledger_path: str,
    scope_payload: dict[str, Any],
    request: dict[str, Any],
    prompt: str,
    api_config: dict[str, Any],
) -> None:
    scope = PrimaryRepairScopeV1.from_dict(scope_payload)
    controller = ProviderBudgetController(_budget())
    controller.bind_state_path(
        state_path,
        acceptance_run_id="",
        state_started=True,
    )
    journal = PrimaryRepairResponseJournal(journal_root)
    runtime = ProviderRuntime(
        budget=ProviderBudgetV1(max_calls=1),
        aggregate_budget=controller,
        ledger=ProviderRuntimeLedger(ledger_path),
        job_id=scope.job_id,
        attempt_id=scope.logical_attempt_identity,
        stage_name="outline_v3",
        route="candidate_1_semantic_repair",
        node_id="candidate_1_semantic_repair",
        call_id="repair-call-1",
        closure_epoch_id="repair-epoch-1",
        logical_attempt_identity=scope.logical_attempt_identity,
        endpoint_type="chat_completions",
        schema_hash=scope.schema_hash,
        primary_repair_response_journal=journal,
    )
    admission = runtime.admit(
        estimated_tokens=100,
        requested_output_tokens=256,
        requested_retry_attempts=0,
        primary_repair_scope=scope,
        request_hash=hash_json(request),
    )
    runtime.mark_transport_started(admission)
    runtime.primary_repair_raw_response_callback(
        admission,
        scope,
        prompt=prompt,
        input_payload=request,
        api_config=api_config,
        response_format="json",
        metadata={"node_id": "candidate_1_semantic_repair"},
    )
    runtime.primary_provider_attempt_started_callback(admission, scope)(1)
    # This is the hard crash point: ProviderRuntime has written and fsynced the
    # normalized response plus receipt binding, then enters aggregate budget
    # completion, before either the budget settlement or receipt append.
    controller.complete = lambda *_args, **_kwargs: os._exit(0)
    runtime.complete(
        admission=admission,
        prompt=prompt,
        input_payload=request,
        api_config=api_config,
        result={
            "status": "success",
            "content": {"sections": [{"section_id": "section-1", "claims": []}]},
            "finish_reason": "stop",
            "input_tokens": 40,
            "output_tokens": 19,
            "total_tokens": 59,
            "usage_status": "reported",
            "attempts": 1,
            "endpoint_type": "chat_completions",
        },
        metadata={"node_id": "candidate_1_semantic_repair"},
    )
    os._exit(99)  # pragma: no cover - reaching this point invalidates the fault injection


def _crash_after_raw_response_retention_worker(
    state_path: str,
    journal_root: str,
    ledger_path: str,
    scope_payload: dict[str, Any],
    request: dict[str, Any],
    prompt: str,
    api_config: dict[str, Any],
    raw_body: bytes,
) -> None:
    """Die after durable raw capture, before parsing/accounting/receipt append."""

    scope = PrimaryRepairScopeV1.from_dict(scope_payload)
    controller = ProviderBudgetController(_budget())
    controller.bind_state_path(state_path, acceptance_run_id="", state_started=True)
    journal = PrimaryRepairResponseJournal(journal_root)
    runtime = ProviderRuntime(
        budget=ProviderBudgetV1(max_calls=1),
        aggregate_budget=controller,
        ledger=ProviderRuntimeLedger(ledger_path),
        job_id=scope.job_id,
        attempt_id=scope.logical_attempt_identity,
        stage_name="outline_v3",
        route="candidate_1_semantic_repair",
        node_id="candidate_1_semantic_repair",
        call_id="repair-call-1",
        closure_epoch_id="repair-epoch-1",
        logical_attempt_identity=scope.logical_attempt_identity,
        endpoint_type="chat_completions",
        schema_hash=scope.schema_hash,
        primary_repair_response_journal=journal,
    )
    admission = runtime.admit(
        estimated_tokens=100,
        requested_output_tokens=256,
        requested_retry_attempts=0,
        primary_repair_scope=scope,
        request_hash=hash_json(request),
    )
    runtime.mark_transport_started(admission)
    attempt_started = runtime.primary_provider_attempt_started_callback(admission, scope)
    retain_raw = runtime.primary_repair_raw_response_callback(
        admission,
        scope,
        prompt=prompt,
        input_payload=request,
        api_config=api_config,
        response_format="json",
        metadata={"call_id": "repair-call-1"},
    )
    spool_path = attempt_started(1)  # idempotent with ProviderRuntime.mark_transport_started.
    attempt_started.response_headers(
        1, 200, "application/json", len(raw_body), "identity", ""
    )
    # The callback first fsyncs and journals the content-addressed response,
    # then updates aggregate scope metadata. Die at that exact boundary so
    # resume must discover the raw journal from the earlier atomic reservation.
    controller.retain_primary_repair_raw_response = (
        lambda *_args, **_kwargs: os._exit(0)
    )
    Path(spool_path).write_bytes(raw_body)
    with Path(spool_path).open("r+b") as handle:
        os.fsync(handle.fileno())
    retain_raw(
        spool_path,
        hashlib.sha256(raw_body).hexdigest(),
        len(raw_body),
        200,
        "application/json",
        "json",
        True,
        1,
    )
    os._exit(99)  # pragma: no cover - the injected crash point must run


def _crash_during_local_http_raw_capture_worker(
    state_path: str,
    journal_root: str,
    ledger_path: str,
    scope_payload: dict[str, Any],
    request: dict[str, Any],
    prompt: str,
    api_config: dict[str, Any],
    raw_body: bytes,
    crash_point: str,
) -> None:
    """Make one real localhost POST, then die in a selected raw-capture window."""

    import ai_interface

    scope = PrimaryRepairScopeV1.from_dict(scope_payload)
    controller = ProviderBudgetController(_budget())
    controller.bind_state_path(state_path, acceptance_run_id="", state_started=True)
    journal = PrimaryRepairResponseJournal(journal_root)
    runtime = ProviderRuntime(
        budget=ProviderBudgetV1(max_calls=1),
        aggregate_budget=controller,
        ledger=ProviderRuntimeLedger(ledger_path),
        job_id=scope.job_id,
        attempt_id=scope.logical_attempt_identity,
        stage_name="outline_v3",
        route="candidate_1_semantic_repair",
        node_id="candidate_1_semantic_repair",
        call_id="repair-call-local-http",
        closure_epoch_id="repair-epoch-1",
        logical_attempt_identity=scope.logical_attempt_identity,
        endpoint_type="chat_completions",
        schema_hash=scope.schema_hash,
        primary_repair_response_journal=journal,
    )
    admission = runtime.admit(
        estimated_tokens=100,
        requested_output_tokens=256,
        requested_retry_attempts=0,
        primary_repair_scope=scope,
        request_hash=hash_json(request),
    )
    runtime.mark_transport_started(admission)
    attempt_started = runtime.primary_provider_attempt_started_callback(admission, scope)
    retain_raw = runtime.primary_repair_raw_response_callback(
        admission,
        scope,
        prompt=prompt,
        input_payload=request,
        api_config=api_config,
        response_format="json",
        metadata={"call_id": "repair-call-local-http"},
    )
    if crash_point == "after_spool_fsync_before_finalizer":
        ai_interface._invoke_primary_raw_response_finalizer = (
            lambda *_args, **_kwargs: os._exit(0)
        )
    elif crash_point == "during_content_copy_before_journal_repoint":
        journal._copy_raw_spool_to_content_addressed_path = (
            lambda *_args, **_kwargs: os._exit(0)
        )
    else:
        os._exit(98)

    ai_interface._call_ai_api_detailed_uninstrumented(
        prompt,
        api_config,
        "local crash-recovery system prompt",
        max_tokens=256,
        temperature=0.0,
        response_format="json",
        retry_attempts=0,
        max_retries_per_call=0,
        attempt_limit=1,
        primary_raw_response_retention_callback=retain_raw,
        primary_provider_attempt_started_callback=attempt_started,
    )
    os._exit(99)  # pragma: no cover - a crash hook must stop before adapter return


def _crash_during_local_http_transport_worker(
    state_path: str,
    journal_root: str,
    ledger_path: str,
    scope_payload: dict[str, Any],
    request: dict[str, Any],
    prompt: str,
    api_config: dict[str, Any],
    crash_point: str,
) -> None:
    """POST once to loopback and die in the raw-spool finalization window."""

    import ai_interface

    scope = PrimaryRepairScopeV1.from_dict(scope_payload)
    controller = ProviderBudgetController(_budget())
    controller.bind_state_path(state_path, acceptance_run_id="", state_started=True)
    journal = PrimaryRepairResponseJournal(journal_root)
    runtime = ProviderRuntime(
        budget=ProviderBudgetV1(max_calls=1),
        aggregate_budget=controller,
        ledger=ProviderRuntimeLedger(ledger_path),
        job_id=scope.job_id,
        attempt_id=scope.logical_attempt_identity,
        stage_name="outline_v3",
        route="candidate_1_semantic_repair",
        node_id="candidate_1_semantic_repair",
        call_id="repair-call-local-http",
        closure_epoch_id="repair-epoch-1",
        logical_attempt_identity=scope.logical_attempt_identity,
        endpoint_type="chat_completions",
        schema_hash=scope.schema_hash,
        primary_repair_response_journal=journal,
    )
    admission = runtime.admit(
        estimated_tokens=100,
        requested_output_tokens=256,
        requested_retry_attempts=0,
        primary_repair_scope=scope,
        request_hash=hash_json(request),
    )
    runtime.mark_transport_started(admission)
    attempt_started = runtime.primary_provider_attempt_started_callback(admission, scope)
    retain_raw = runtime.primary_repair_raw_response_callback(
        admission,
        scope,
        prompt=prompt,
        input_payload=request,
        api_config=api_config,
        response_format="json",
        metadata={"call_id": "repair-call-local-http"},
    )
    if crash_point == "after_spool_fsync_before_finalizer":
        ai_interface._invoke_primary_raw_response_finalizer = (
            lambda *_args, **_kwargs: os._exit(0)
        )
    elif crash_point == "during_content_copy_before_journal_repoint":
        journal._copy_raw_spool_to_content_addressed_path = (
            lambda *_args, **_kwargs: os._exit(0)
        )
    else:
        os._exit(98)
    ai_interface._call_ai_api_detailed_uninstrumented(
        prompt,
        api_config,
        "local crash-recovery system prompt",
        max_tokens=256,
        temperature=0.0,
        response_format="json",
        retry_attempts=0,
        max_retries_per_call=0,
        attempt_limit=1,
        primary_raw_response_retention_callback=retain_raw,
        primary_provider_attempt_started_callback=attempt_started,
    )
    os._exit(99)  # pragma: no cover - the fault injection must interrupt the call


def test_primary_scope_and_budget_are_reserved_atomically_across_processes(
    tmp_path: Path,
) -> None:
    context = multiprocessing.get_context("spawn")
    state_path = tmp_path / "provider_budget_state.json"
    controller = ProviderBudgetController(_budget(calls=1))
    controller.bind_state_path(
        state_path,
        acceptance_run_id="repair-reservation-race",
        state_started=False,
    )
    scope = _scope()
    barrier = context.Barrier(2)
    release_event = context.Event()
    results = context.Queue()
    processes = [
        context.Process(
            target=_scope_race_worker,
            args=(str(state_path), barrier, release_event, results, scope.to_dict()),
        )
        for _ in range(2)
    ]
    for process in processes:
        process.start()
    try:
        outcomes = [results.get(timeout=20) for _ in processes]
        snapshot = controller.snapshot()
        scope_rows = controller.lookup_primary_repair_scopes()
    except Empty:
        pytest.fail("scope reservation workers did not report their outcomes")
    finally:
        release_event.set()
        for process in processes:
            process.join(timeout=20)
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)

    assert [process.exitcode for process in processes] == [0, 0]
    assert sum(item[0] == "granted" for item in outcomes) == 1
    denied = next(item for item in outcomes if item[0] == "blocked")
    assert denied[1] == "ProviderBudgetExceeded"
    assert "scope" in denied[2].lower()
    assert snapshot["calls_reserved"] == 1
    assert len(scope_rows) == 1


def test_spent_primary_scope_blocks_duplicate_but_allows_a_distinct_bounded_scope(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "provider_budget_state.json"
    journal_root = tmp_path / "repair-responses"
    controller = ProviderBudgetController(_budget(calls=2))
    controller.bind_state_path(
        state_path,
        acceptance_run_id="repair-positive-continuation",
        state_started=False,
    )
    journal = PrimaryRepairResponseJournal(journal_root)
    first_scope = _scope("candidate_1")
    first = controller.admit(
        requested_output_tokens=128,
        requested_retry_attempts=0,
        context={"primary_repair_response_journal_root": str(journal_root)},
        primary_repair_scope=first_scope,
    )
    controller.mark_transport_started(first)
    response = {
        "status": "success",
        "content": '{"sections": []}',
        "finish_reason": "stop",
        "output_tokens": 11,
        "attempts": 1,
    }
    reference = journal.persist_response(first_scope, first.reservation_id, response)
    controller.complete(
        first,
        response,
        primary_repair_response_ref=reference.to_dict(),
    )

    with pytest.raises(ProviderBudgetExceeded, match="scope"):
        controller.admit(
            requested_output_tokens=128,
            requested_retry_attempts=0,
            primary_repair_scope=first_scope,
        )
    with pytest.raises(ProviderRuntimeContractError, match="materialization changed"):
        controller.admit(
            requested_output_tokens=128,
            requested_retry_attempts=0,
            primary_repair_scope=replace(
                first_scope,
                request_hash=hash_json({"candidate_id": "candidate_1", "changed": True}),
            ),
        )

    second = controller.admit(
        requested_output_tokens=128,
        requested_retry_attempts=0,
        primary_repair_scope=_scope("candidate_2"),
    )
    snapshot = controller.snapshot()
    assert snapshot["calls_used"] == 1
    assert snapshot["calls_reserved"] == 1
    assert second.reservation_id != first.reservation_id


def test_only_proven_unstarted_primary_reservations_can_release_scope(tmp_path: Path) -> None:
    controller = ProviderBudgetController(_budget(calls=1))
    controller.bind_state_path(
        tmp_path / "provider_budget_state.json",
        acceptance_run_id="repair-unstarted-release",
        state_started=False,
    )
    scope = _scope()
    unstarted = controller.admit(primary_repair_scope=scope)
    assert controller.release_unstarted(unstarted) is True
    retry = controller.admit(primary_repair_scope=scope)
    controller.mark_transport_started(retry)
    with pytest.raises(ProviderRuntimeContractError, match="after transport started"):
        controller.release_unstarted(retry)


def test_finite_run_envelope_allows_unknown_future_payloads_with_jit_admission() -> None:
    budget = ProviderAggregateBudgetV2(
        max_provider_calls_total=24,
        max_output_tokens_total=96_000,
        max_retry_attempts_total=24,
        max_wall_seconds=3_600,
    )
    projection = _full_stage_projection(budget)
    projection["exact_requests_materialized"] = False
    projection["envelope_complete"] = False
    envelope = build_provider_run_envelope_v1(
        job_id="review-job",
        run_id="run-1",
        budget_authority="documented_default",
        provider_budget=budget,
        scope_hashes=_run_scope_hashes(),
        full_stage_request_plan=projection,
    )
    payload = envelope.to_dict()
    assert payload["admission_status"] == "ready_with_jit"
    assert payload["jit_admission_required"] is True
    assert payload["future_request_materialization"] == "at_ready_stage_before_first_post"
    assert payload["conditional_work_upper_bounds"]["unknown_exposure_count"] == 2
    assert payload["provider_budget"] == budget.to_dict()
    assert payload["projection_is_provider_authority"] is False
    assert ProviderRunEnvelopeV1.from_dict(payload) == envelope
    tampered = dict(payload)
    tampered["run_id"] = "different-run"
    with pytest.raises(ProviderRunEnvelopeError, match="hash"):
        ProviderRunEnvelopeV1.from_dict(tampered)


def test_finite_run_envelope_blocks_known_required_work_over_cap() -> None:
    budget = ProviderAggregateBudgetV2(
        max_provider_calls_total=1,
        max_output_tokens_total=99,
        max_retry_attempts_total=0,
        max_wall_seconds=30,
    )
    projection = _full_stage_projection(
        budget,
        lower_calls=2,
        lower_output=100,
        lower_retries=0,
        admission="conditional_within_budget",
    )
    envelope = build_provider_run_envelope_v1(
        job_id="review-job",
        run_id="run-too-small",
        budget_authority="explicit_runtime_spec",
        provider_budget=budget,
        scope_hashes=_run_scope_hashes(),
        full_stage_request_plan=projection,
    )
    assert envelope.admission_status == "blocked_budget"


def test_run_envelope_keeps_total_wall_projection_jit_bounded() -> None:
    budget = ProviderAggregateBudgetV2(
        max_provider_calls_total=24,
        max_output_tokens_total=96_000,
        max_retry_attempts_total=24,
        max_wall_seconds=120,
    )
    projection = _full_stage_projection(
        budget,
        lower_calls=3,
        lower_output=1_000,
        lower_retries=1,
        upper_calls=20,
        upper_output=20_000,
        upper_wall=3_600,
        admission="blocked_budget",
    )
    envelope = build_provider_run_envelope_v1(
        job_id="review-job",
        run_id="wall-jit",
        budget_authority="explicit_runtime_spec",
        provider_budget=budget,
        scope_hashes=_run_scope_hashes(),
        full_stage_request_plan=projection,
    )
    assert envelope.admission_status == "ready_with_jit"
    assert envelope.conditional_work_upper_bounds["wall_seconds_upper_bound"] == 3_600


def test_strict_zero_call_budget_is_not_projected_as_unbounded() -> None:
    budget = ProviderAggregateBudgetV2(
        max_provider_calls_total=0,
        max_output_tokens_total=0,
        max_retry_attempts_total=0,
        max_wall_seconds=0,
    )
    projection = _full_stage_projection(
        budget,
        lower_calls=1,
        lower_output=1,
        lower_retries=0,
        admission="unknown",
    )
    envelope = build_provider_run_envelope_v1(
        job_id="review-job",
        run_id="run-zero",
        budget_authority="explicit_runtime_spec",
        provider_budget=budget,
        scope_hashes=_run_scope_hashes(),
        full_stage_request_plan=projection,
    )
    assert envelope.admission_status == "blocked_budget"
    assert envelope.provider_budget.max_provider_calls_total == 0


def _acceptance_context_for_controller(
    controller: ProviderBudgetController,
    *,
    run_id: str,
) -> AcceptanceExecutionContextV1:
    assert controller.state_path is not None
    return AcceptanceExecutionContextV1(
        acceptance_run_id=run_id,
        final_executable_sha="e" * 40,
        absolute_deadline_epoch=4_102_444_800.0,
        provider_budget=controller.budget,
        provider_budget_state_path=str(controller.state_path),
        evidence_root=str(controller.state_path.parent / "evidence"),
        process_event_log=str(controller.state_path.parent / "events.jsonl"),
        scenario_state_path=str(controller.state_path.parent / "scenario.json"),
        owner_authorized=True,
        provider_budget_state_started=True,
    )


def test_explicit_acceptance_controller_and_serialized_authority_precede_run_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    acceptance_run_id = "accepted-provider-run"
    acceptance_controller = _durable_run_controller(
        tmp_path / "accepted-budget.json",
        calls=5,
        acceptance_run_id=acceptance_run_id,
    )
    acceptance_context = _acceptance_context_for_controller(
        acceptance_controller,
        run_id=acceptance_run_id,
    )
    active_run_controller = _durable_run_controller(
        tmp_path / "ordinary-run-budget.json",
        calls=3,
    )

    for variable in (
        "AUTO_GENERATE_ACCEPTANCE_BUDGET_JSON",
        "AUTO_GENERATE_ACCEPTANCE_BUDGET_STATE_PATH",
        "AUTO_GENERATE_ACCEPTANCE_CONTEXT_JSON",
        "AUTO_GENERATE_ACCEPTANCE_RUN_ID",
    ):
        monkeypatch.delenv(variable, raising=False)

    with bind_acceptance_execution_context(
        acceptance_context,
        acceptance_controller,
    ):
        with bind_provider_run_budget(active_run_controller):
            assert provider_budget_controller_from_environment() is acceptance_controller
            assert current_acceptance_execution_context() == acceptance_context

    # A serialized acceptance context also outranks the ordinary run binding
    # in a child-style context where the in-process accepted controller is absent.
    monkeypatch.setenv(
        "AUTO_GENERATE_ACCEPTANCE_BUDGET_JSON",
        json.dumps(acceptance_context.provider_budget.to_dict(), sort_keys=True),
    )
    monkeypatch.setenv(
        "AUTO_GENERATE_ACCEPTANCE_BUDGET_STATE_PATH",
        acceptance_context.provider_budget_state_path,
    )
    monkeypatch.setenv("AUTO_GENERATE_ACCEPTANCE_RUN_ID", acceptance_run_id)
    monkeypatch.setenv(
        "AUTO_GENERATE_ACCEPTANCE_CONTEXT_JSON",
        json.dumps(acceptance_context.to_dict(), sort_keys=True),
    )
    with bind_provider_run_budget(active_run_controller):
        resolved = provider_budget_controller_from_environment()
        assert resolved is not None
        assert resolved is not active_run_controller
        assert resolved.budget == acceptance_context.provider_budget
        assert resolved.state_path == acceptance_controller.state_path
        assert resolved.acceptance_run_id == acceptance_run_id
        assert current_acceptance_execution_context() is None


def test_run_budget_precedes_unbound_environment_fallback_and_restores_nested_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outer = _durable_run_controller(tmp_path / "outer-run-budget.json", calls=3)
    inner = _durable_run_controller(tmp_path / "inner-run-budget.json", calls=4)
    for variable in (
        "AUTO_GENERATE_ACCEPTANCE_BUDGET_STATE_PATH",
        "AUTO_GENERATE_ACCEPTANCE_CONTEXT_JSON",
        "AUTO_GENERATE_ACCEPTANCE_RUN_ID",
    ):
        monkeypatch.delenv(variable, raising=False)
    # Even invalid legacy fallback data must not override a bound public run.
    monkeypatch.setenv("AUTO_GENERATE_ACCEPTANCE_BUDGET_JSON", "{invalid")
    with bind_provider_run_budget(outer):
        assert provider_budget_controller_from_environment() is outer
        with bind_provider_run_budget(inner):
            assert provider_budget_controller_from_environment() is inner
            assert current_acceptance_execution_context() is None
        assert provider_budget_controller_from_environment() is outer
        with pytest.raises(RuntimeError, match="test unwind"):
            with bind_provider_run_budget(inner):
                assert provider_budget_controller_from_environment() is inner
                raise RuntimeError("test unwind")
        assert provider_budget_controller_from_environment() is outer
    monkeypatch.delenv("AUTO_GENERATE_ACCEPTANCE_BUDGET_JSON", raising=False)
    assert provider_budget_controller_from_environment() is None


def test_run_budget_binding_rejects_unbounded_or_unpersisted_controllers(
    tmp_path: Path,
) -> None:
    unbound = ProviderBudgetController(_budget())
    with pytest.raises(ProviderRuntimeContractError, match="durably bound"):
        with bind_provider_run_budget(unbound):
            pytest.fail("an unpersisted run budget must not bind")

    legacy = ProviderBudgetController(
        ProviderAggregateBudgetV1(
            max_provider_calls_total=2,
            max_output_tokens_total=1_000,
            max_retry_attempts_total=1,
            max_wall_seconds=120,
        )
    )
    legacy.bind_state_path(
        tmp_path / "legacy-run-budget.json",
        acceptance_run_id="",
        state_started=False,
    )
    with pytest.raises(ProviderRuntimeContractError, match="strict finite V2"):
        with bind_provider_run_budget(legacy):
            pytest.fail("a legacy run budget must not bind")


def test_journaled_success_recovers_accounting_after_process_crash_without_repost(
    tmp_path: Path,
) -> None:
    context = multiprocessing.get_context("spawn")
    state_path = tmp_path / "provider_budget_state.json"
    journal_root = tmp_path / "repair-responses"
    ledger_path = tmp_path / "provider_receipts.jsonl"
    request = {"candidate_id": "candidate_1", "task": "bounded repair"}
    prompt = json.dumps(request, ensure_ascii=False, sort_keys=True)
    api_config = {
        "provider_family": "fixture",
        "model": "fixture-model",
        "api_base": "https://fixture.invalid/v1",
    }
    scope = _scope(request_hash=hash_json(request))
    parent = ProviderBudgetController(_budget())
    parent.bind_state_path(
        state_path,
        acceptance_run_id="",
        state_started=False,
    )
    process = context.Process(
        target=_crash_after_response_journal_worker,
        args=(
            str(state_path),
            str(journal_root),
            str(ledger_path),
            scope.to_dict(),
            request,
            prompt,
            api_config,
        ),
    )
    process.start()
    process.join(timeout=20)
    if process.is_alive():
        process.terminate()
        process.join(timeout=5)
        pytest.fail("fault-injection worker did not terminate")
    assert process.exitcode == 0

    resumed = ProviderBudgetController(_budget())
    resumed.bind_state_path(
        state_path,
        acceptance_run_id="",
        state_started=True,
    )
    scope_state = resumed.lookup_primary_repair_scope(scope)
    assert scope_state is not None
    assert scope_state["status"] == "response_retained"
    journal = PrimaryRepairResponseJournal(journal_root)
    recovered = journal.load_entry_for_scope(scope)
    assert recovered is not None
    reference = recovered.reference
    response = dict(recovered.response)
    resumed_runtime = ProviderRuntime(
        budget=ProviderBudgetV1(max_calls=1),
        aggregate_budget=resumed,
        ledger=ProviderRuntimeLedger(ledger_path),
        job_id=scope.job_id,
        attempt_id=scope.logical_attempt_identity,
        stage_name="outline_v3",
        route="candidate_1_semantic_repair",
        node_id="candidate_1_semantic_repair",
        call_id="repair-call-1",
        closure_epoch_id="repair-epoch-1",
        logical_attempt_identity=scope.logical_attempt_identity,
        endpoint_type="chat_completions",
        schema_hash=scope.schema_hash,
        primary_repair_response_journal=journal,
    )
    recovered_response = resumed_runtime.recover_primary_repair_response(
        scope,
        prompt=prompt,
        input_payload=request,
        api_config=api_config,
    )
    assert recovered_response == (reference, response)
    snapshot = resumed.snapshot()
    assert snapshot["calls_used"] == 1
    assert snapshot["calls_reserved"] == 0
    assert resumed.lookup_primary_repair_scope(scope)["status"] == "completed"
    receipts = resumed_runtime.ledger.list_receipts()
    assert len(receipts) == 1
    receipt = receipts[0]
    assert receipt.test_only is False
    assert receipt.response_hash == hash_json(response["content"])
    assert receipt.prompt_hash == hash_text(prompt)
    assert receipt.input_hash == hash_json(request)
    assert receipt.metadata["accounting_recovered_from_response_journal"] is True
    assert receipt.metadata["primary_repair_response_ref"] == reference.to_dict()

    # Exercise the production publication and replay stores with the recovered
    # response and genuine receipt. This is the work that must proceed locally
    # after the crash; no replacement provider admission is required.
    output_workspace = JobWorkspace.create(
        str(tmp_path / "output-workspaces"), "review", scope.job_id
    )
    registry = ArtifactRegistry(
        output_workspace.paths.registry_path,
        output_workspace.job_id,
    )
    artifact = OutlineArtifact(
        job_id=scope.job_id,
        dependency_hashes={"source": scope.source_authority_sha256},
        payload=dict(response["content"]),
    )
    artifact_id = f"outline-v3:repair:{hash_text('candidate_1_semantic_repair')[:24]}"
    output_record = publish_json_artifact(
        LocalPublicationContext(),
        registry,
        output_workspace.artifact_path("outline_v3/primary_repair_output.json"),
        artifact.to_dict(),
        artifact_role="outline_v3_repair_output",
        artifact_type="outline_candidate_repair",
        artifact_version="v1",
        producer="outline.v3_executor.OutlineV3Executor",
        artifact_id=artifact_id,
    )
    registry.verify_ready_artifact_closure(output_record)
    replay_key = ModelCallReplayKey(
        node_id="candidate_1_semantic_repair",
        node_version="v3",
        schema_version="outline-v3",
        model_route="candidate_repair",
        model_name=str(api_config["model"]),
        provider=str(api_config["provider_family"]),
        prompt_template_hash=hash_text("repair-template"),
        prompt_payload_hash=scope.request_hash,
        input_artifact_hashes=[scope.source_authority_sha256],
        config_hash=hash_json(api_config),
        execution_binding_hash=scope.fingerprint_sha256,
    )
    replay_store = ModelCallReplayStore(output_workspace)
    replay_store.append(
        replay_key,
        output_hash=hash_json(response["content"]),
        normalized_output_hash=hash_json(response["content"]),
        registered_artifact_hash=artifact.content_hash,
        node_output_hash=artifact.content_hash,
        output_artifact_ids=(artifact_id,),
        receipt_ids=(receipt.receipt_id,),
        audit_node_id="candidate_1_semantic_repair",
        closure_epoch_id="repair-epoch-1",
    )
    replay_lookup = replay_store.lookup(replay_key)
    assert replay_lookup.reusable is True
    assert replay_lookup.record is not None
    assert replay_lookup.record.receipt_ids == [receipt.receipt_id]
    published_payload = json.loads(Path(output_record.path).read_text(encoding="utf-8"))
    assert registry.get(artifact_id).content_hash == output_record.content_hash
    assert published_payload["content_hash"] == replay_lookup.record.registered_artifact_hash
    assert replay_lookup.record.node_output_hash == published_payload["content_hash"]
    with pytest.raises(ProviderBudgetExceeded, match="scope"):
        resumed.admit(primary_repair_scope=scope)

    continuation = resumed.admit(primary_repair_scope=_scope("candidate_2"))
    assert continuation.provider_calls == 1


def _raw_chat_completion(content: str, *, include_usage: bool = True) -> bytes:
    payload: dict[str, Any] = {
        "id": "response-1",
        "model": "fixture-model",
        "choices": [
            {
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
    }
    if include_usage:
        payload["usage"] = {
            "prompt_tokens": 40,
            "completion_tokens": 19,
            "total_tokens": 59,
        }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")


class _LocalChunkedProvider:
    def __init__(
        self,
        body: bytes,
        *,
        content_length_delta: int | None = None,
        content_type: str = "application/json",
    ) -> None:
        self.body = body
        self.content_length_delta = content_length_delta
        self.content_type = content_type
        self.requests: list[bytes] = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self) -> None:
                request_length = int(self.headers.get("Content-Length", "0"))
                owner.requests.append(self.rfile.read(request_length))
                self.send_response(200)
                self.send_header("Content-Type", owner.content_type)
                if owner.content_length_delta is None:
                    self.send_header("Transfer-Encoding", "chunked")
                else:
                    self.send_header(
                        "Content-Length",
                        str(len(owner.body) + owner.content_length_delta),
                    )
                self.end_headers()
                try:
                    if owner.content_length_delta is None:
                        midpoint = max(1, len(owner.body) // 2)
                        for chunk in (owner.body[:midpoint], owner.body[midpoint:]):
                            self.wfile.write(f"{len(chunk):X}\r\n".encode("ascii"))
                            self.wfile.write(chunk + b"\r\n")
                            self.wfile.flush()
                        self.wfile.write(b"0\r\n\r\n")
                        self.wfile.flush()
                    else:
                        self.wfile.write(owner.body)
                        self.wfile.flush()
                        if owner.content_length_delta > 0:
                            # Keep the connection open: the client must observe
                            # a missing declared byte rather than a clean EOF.
                            time.sleep(1.0)
                except OSError:
                    return

            def log_message(self, *_args: Any) -> None:
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = Thread(target=self.server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}/v1"

    def __enter__(self) -> "_LocalChunkedProvider":
        self.thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def test_raw_fsynced_spool_crash_recovers_receipt_registry_and_replay_without_post(
    tmp_path: Path,
) -> None:
    context = multiprocessing.get_context("spawn")
    state_path = tmp_path / "provider_budget_state.json"
    journal_root = tmp_path / "repair-responses"
    ledger_path = tmp_path / "provider_receipts.jsonl"
    request = {"candidate_id": "candidate_1", "task": "bounded repair"}
    prompt = json.dumps(request, ensure_ascii=False, sort_keys=True)
    api_config = {
        "provider_family": "fixture",
        "model": "fixture-model",
        "api_base": "https://fixture.invalid/v1",
    }
    raw_body = _raw_chat_completion(
        '{"sections":[{"section_id":"section-1","claims":[]}]}'
    )
    scope = _scope(request_hash=hash_json(request))
    initial = ProviderBudgetController(_budget())
    initial.bind_state_path(state_path, acceptance_run_id="", state_started=False)
    process = context.Process(
        target=_crash_after_raw_response_retention_worker,
        args=(
            str(state_path),
            str(journal_root),
            str(ledger_path),
            scope.to_dict(),
            request,
            prompt,
            api_config,
            raw_body,
        ),
    )
    process.start()
    process.join(timeout=20)
    if process.is_alive():
        process.terminate()
        process.join(timeout=5)
        pytest.fail("raw-spool fault-injection worker did not terminate")
    assert process.exitcode == 0
    resumed = ProviderBudgetController(_budget())
    resumed.bind_state_path(state_path, acceptance_run_id="", state_started=True)
    scope_state = resumed.lookup_primary_repair_scope(scope)
    assert scope_state is not None
    assert scope_state["status"] == "raw_retained"
    journal = PrimaryRepairResponseJournal(journal_root)
    assert journal.attempt_spool_path(scope, 1).is_file()
    raw_entry = journal.load_entry_for_scope(scope)
    assert raw_entry is not None and raw_entry.entry_kind == "raw_response"
    assert raw_entry.raw_spool is not None
    assert raw_entry.raw_spool.complete is True
    assert raw_entry.raw_spool.attempt_number == 1
    assert Path(raw_entry.raw_spool.path).read_bytes() == raw_body

    resumed_runtime = ProviderRuntime(
        budget=ProviderBudgetV1(max_calls=1),
        aggregate_budget=resumed,
        ledger=ProviderRuntimeLedger(ledger_path),
        job_id=scope.job_id,
        attempt_id=scope.logical_attempt_identity,
        stage_name="outline_v3",
        route="candidate_1_semantic_repair",
        node_id="candidate_1_semantic_repair",
        call_id="repair-call-1",
        closure_epoch_id="repair-epoch-1",
        logical_attempt_identity=scope.logical_attempt_identity,
        endpoint_type="chat_completions",
        schema_hash=scope.schema_hash,
        primary_repair_response_journal=journal,
    )
    recovered = resumed_runtime.recover_primary_repair_response(
        scope,
        prompt=prompt,
        input_payload=request,
        api_config=api_config,
    )
    assert recovered is not None
    response_ref, response = recovered
    assert response["status"] == "success"
    assert response["content"] == {
        "sections": [{"section_id": "section-1", "claims": []}]
    }
    assert response["input_tokens"] == 40
    assert response["output_tokens"] == 19
    assert response["attempts"] == 1
    assert response["raw_response_sha256"] == hashlib.sha256(raw_body).hexdigest()

    snapshot = resumed.snapshot()
    assert snapshot["calls_used"] == 1
    assert snapshot["calls_reserved"] == 0
    assert snapshot["output_tokens_used"] == 19
    assert resumed.lookup_primary_repair_scope(scope)["status"] == "completed"
    receipt = resumed_runtime.ledger.list_receipts()[0]
    assert receipt.test_only is False
    assert receipt.response_hash == hash_json(response["content"])
    assert receipt.metadata["primary_repair_response_ref"] == response_ref.to_dict()
    assert receipt.metadata["accounting_recovered_from_response_journal"] is True

    workspace = JobWorkspace.create(str(tmp_path / "output-workspaces"), "review", scope.job_id)
    registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    artifact = OutlineArtifact(
        job_id=scope.job_id,
        dependency_hashes={"source": scope.source_authority_sha256},
        payload=dict(response["content"]),
    )
    artifact_id = f"outline-v3:repair:{hash_text('candidate_1_semantic_repair')[:24]}"
    output_record = publish_json_artifact(
        LocalPublicationContext(),
        registry,
        workspace.artifact_path("outline_v3/primary_repair_output.json"),
        artifact.to_dict(),
        artifact_role="outline_v3_repair_output",
        artifact_type="outline_candidate_repair",
        artifact_version="v1",
        producer="outline.v3_executor.OutlineV3Executor",
        artifact_id=artifact_id,
    )
    registry.verify_ready_artifact_closure(output_record)
    replay_key = ModelCallReplayKey(
        node_id="candidate_1_semantic_repair",
        node_version="v3",
        schema_version="outline-v3",
        model_route="candidate_repair",
        model_name=str(api_config["model"]),
        provider=str(api_config["provider_family"]),
        prompt_template_hash=hash_text("repair-template"),
        prompt_payload_hash=scope.request_hash,
        input_artifact_hashes=[scope.source_authority_sha256],
        config_hash=hash_json(api_config),
        execution_binding_hash=scope.fingerprint_sha256,
    )
    replay_store = ModelCallReplayStore(workspace)
    replay_store.append(
        replay_key,
        output_hash=hash_json(response["content"]),
        normalized_output_hash=hash_json(response["content"]),
        registered_artifact_hash=artifact.content_hash,
        node_output_hash=artifact.content_hash,
        output_artifact_ids=(artifact_id,),
        receipt_ids=(receipt.receipt_id,),
        audit_node_id="candidate_1_semantic_repair",
        closure_epoch_id="repair-epoch-1",
    )
    lookup = replay_store.lookup(replay_key)
    assert lookup.reusable is True
    assert lookup.record is not None
    assert lookup.record.receipt_ids == [receipt.receipt_id]
    with pytest.raises(ProviderBudgetExceeded, match="scope"):
        resumed.admit(primary_repair_scope=scope)
    assert resumed.admit(primary_repair_scope=_scope("candidate_2")).provider_calls == 1


@pytest.mark.parametrize(
    "crash_point",
    [
        "after_spool_fsync_before_finalizer",
        "during_content_copy_before_journal_repoint",
    ],
)
def test_local_http_orphan_spool_recovers_across_finalization_windows_without_repost(
    tmp_path: Path,
    crash_point: str,
) -> None:
    from tests.test_run_first_timeouts import _LocalBodyServer

    context = multiprocessing.get_context("spawn")
    state_path = tmp_path / "provider_budget_state.json"
    journal_root = tmp_path / "repair-responses"
    ledger_path = tmp_path / "provider_receipts.jsonl"
    request = {"candidate_id": "candidate_1", "task": "bounded repair"}
    prompt = json.dumps(request, ensure_ascii=False, sort_keys=True)
    raw_body = _raw_chat_completion('{"sections":[]}')
    scope = _scope(request_hash=hash_json(request))
    budget = _budget(calls=2)
    initial = ProviderBudgetController(budget)
    initial.bind_state_path(state_path, acceptance_run_id="", state_started=False)

    with _LocalBodyServer([(0, raw_body)]) as provider:
        api_config = {
            "api_key": "local-only-test-key",
            "model": "fixture-model",
            "api_base": provider.base_url,
            "provider_family": "generic",
            "endpoint_type": "chat_completions",
            "proxy_mode": "direct",
            "connect_timeout_seconds": "1",
            "read_timeout_seconds": "1",
            "total_timeout_seconds": "5",
            "first_body_timeout_seconds": "2",
            "transport_retries": "0",
            "raw_response_dir": str(journal_root / "raw_spools"),
        }
        process = context.Process(
            target=_crash_during_local_http_transport_worker,
            args=(
                str(state_path),
                str(journal_root),
                str(ledger_path),
                scope.to_dict(),
                request,
                prompt,
                api_config,
                crash_point,
            ),
        )
        process.start()
        process.join(timeout=20)
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
            pytest.fail("localhost crash-injection worker did not terminate")
        assert process.exitcode == 0
        assert len(provider.requests) == 1

        resumed = ProviderBudgetController(budget)
        resumed.bind_state_path(state_path, acceptance_run_id="", state_started=True)
        scope_state = resumed.lookup_primary_repair_scope(scope)
        assert scope_state is not None
        assert scope_state["status"] == "raw_retained"
        journal = PrimaryRepairResponseJournal(journal_root)
        entry = journal.load_entry_for_scope(scope)
        assert entry is not None and entry.entry_kind == "raw_response"
        assert entry.raw_spool is not None
        assert entry.raw_spool.complete is True
        assert entry.raw_spool.http_status == 200
        assert entry.raw_spool.byte_count == len(raw_body)
        if crash_point == "after_spool_fsync_before_finalizer":
            assert entry.raw_spool.completion_proof == "content_length_exact"
        else:
            assert entry.raw_spool.completion_proof == "reader_eof"

        resumed_runtime = ProviderRuntime(
            budget=ProviderBudgetV1(max_calls=1),
            aggregate_budget=resumed,
            ledger=ProviderRuntimeLedger(ledger_path),
            job_id=scope.job_id,
            attempt_id=scope.logical_attempt_identity,
            stage_name="outline_v3",
            route="candidate_1_semantic_repair",
            node_id="candidate_1_semantic_repair",
            call_id="repair-call-local-http",
            closure_epoch_id="repair-epoch-1",
            logical_attempt_identity=scope.logical_attempt_identity,
            endpoint_type="chat_completions",
            schema_hash=scope.schema_hash,
            primary_repair_response_journal=journal,
        )
        recovered = resumed_runtime.recover_primary_repair_response(
            scope,
            prompt=prompt,
            input_payload=request,
            api_config=api_config,
        )
        assert recovered is not None
        _response_ref, response = recovered
        assert response["status"] == "success"
        assert response["content"] == {"sections": []}
        assert response["attempts"] == 1
        assert len(resumed_runtime.ledger.list_receipts()) == 1
        snapshot = resumed.snapshot()
        assert snapshot["calls_used"] == 1
        assert snapshot["calls_reserved"] == 0
        with pytest.raises(ProviderBudgetExceeded, match="scope"):
            resumed.admit(primary_repair_scope=scope)
        assert resumed.admit(primary_repair_scope=_scope("candidate_2")).provider_calls == 1
        assert len(provider.requests) == 1


@pytest.mark.parametrize(
    "tamper",
    ["content_length", "content_type"],
)
def test_orphan_transport_metadata_drift_stays_ambiguous_without_repost(
    tmp_path: Path,
    tamper: str,
) -> None:
    from tests.test_run_first_timeouts import _LocalBodyServer

    context = multiprocessing.get_context("spawn")
    state_path = tmp_path / "provider_budget_state.json"
    journal_root = tmp_path / "repair-responses"
    ledger_path = tmp_path / "provider_receipts.jsonl"
    request = {"candidate_id": "candidate_1", "task": "bounded repair"}
    prompt = json.dumps(request, ensure_ascii=False, sort_keys=True)
    raw_body = _raw_chat_completion('{"sections":[]}')
    scope = _scope(request_hash=hash_json(request))
    budget = _budget(calls=2)
    initial = ProviderBudgetController(budget)
    initial.bind_state_path(state_path, acceptance_run_id="", state_started=False)

    with _LocalBodyServer([(0, raw_body)]) as provider:
        api_config = {
            "api_key": "local-only-test-key",
            "model": "fixture-model",
            "api_base": provider.base_url,
            "provider_family": "generic",
            "endpoint_type": "chat_completions",
            "proxy_mode": "direct",
            "connect_timeout_seconds": "1",
            "read_timeout_seconds": "1",
            "total_timeout_seconds": "5",
            "first_body_timeout_seconds": "2",
            "transport_retries": "0",
            "raw_response_dir": str(journal_root / "raw_spools"),
        }
        process = context.Process(
            target=_crash_during_local_http_transport_worker,
            args=(
                str(state_path),
                str(journal_root),
                str(ledger_path),
                scope.to_dict(),
                request,
                prompt,
                api_config,
                "after_spool_fsync_before_finalizer",
            ),
        )
        process.start()
        process.join(timeout=20)
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
            pytest.fail("localhost crash-injection worker did not terminate")
        assert process.exitcode == 0
        assert len(provider.requests) == 1

        journal = PrimaryRepairResponseJournal(journal_root)
        intent_path = journal._attempt_metadata_path(scope, 1)
        intent = json.loads(intent_path.read_text(encoding="utf-8"))
        if tamper == "content_length":
            intent["content_length"] = int(intent["content_length"]) + 1
        else:
            intent["content_type"] = "text/event-stream"
        record = dict(intent)
        record.pop("record_sha256", None)
        canonical = json.dumps(
            record,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        intent["record_sha256"] = hashlib.sha256(
            canonical.encode("utf-8")
        ).hexdigest()
        intent_path.write_text(
            json.dumps(intent, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )

        resumed = ProviderBudgetController(budget)
        with pytest.raises(ProviderBudgetExceeded, match="ambiguous"):
            resumed.bind_state_path(
                state_path,
                acceptance_run_id="",
                state_started=True,
            )
        snapshot = resumed.snapshot()
        assert snapshot["calls_used"] == 0
        assert snapshot["calls_reserved"] == 1
        with pytest.raises(ProviderBudgetExceeded, match="ambiguous"):
            resumed.admit(primary_repair_scope=_scope("candidate_2"))
        assert len(provider.requests) == 1


@pytest.mark.parametrize("protocol", ["json", "sse"])
def test_chunked_semantic_completion_recovers_content_but_keeps_http_and_usage_unknown(
    tmp_path: Path,
    protocol: str,
) -> None:
    context = multiprocessing.get_context("spawn")
    state_path = tmp_path / "provider_budget_state.json"
    journal_root = tmp_path / "repair-responses"
    ledger_path = tmp_path / "provider_receipts.jsonl"
    request = {"candidate_id": "candidate_1", "task": "bounded repair"}
    prompt = json.dumps(request, ensure_ascii=False, sort_keys=True)
    if protocol == "json":
        raw_body = _raw_chat_completion('{"sections":[]}')
        content_type = "application/json"
    else:
        message = json.dumps(
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": '{"sections":[]}',
                        },
                        "finish_reason": "stop",
                    }
                ]
            },
            separators=(",", ":"),
        )
        raw_body = f"data: {message}\n\ndata: [DONE]\n\n".encode("utf-8")
        content_type = "text/event-stream"
    scope = _scope(request_hash=hash_json(request))
    budget = _budget(calls=2)
    ProviderBudgetController(budget).bind_state_path(
        state_path,
        acceptance_run_id="",
        state_started=False,
    )

    with _LocalChunkedProvider(
        raw_body,
        content_type=content_type,
    ) as provider:
        api_config = {
            "api_key": "local-chunked-test-key",
            "model": "fixture-model",
            "api_base": provider.base_url,
            "provider_family": "generic",
            "endpoint_type": "chat_completions",
            "proxy_mode": "direct",
            "connect_timeout_seconds": "1",
            "read_timeout_seconds": "1",
            "total_timeout_seconds": "5",
            "first_body_timeout_seconds": "2",
            "transport_retries": "0",
            "raw_response_dir": str(journal_root / "raw_spools"),
        }
        process = context.Process(
            target=_crash_during_local_http_transport_worker,
            args=(
                str(state_path),
                str(journal_root),
                str(ledger_path),
                scope.to_dict(),
                request,
                prompt,
                api_config,
                "after_spool_fsync_before_finalizer",
            ),
        )
        process.start()
        process.join(timeout=20)
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
            pytest.fail("chunked localhost crash-injection worker did not terminate")
        assert process.exitcode == 0
        assert len(provider.requests) == 1

        resumed = ProviderBudgetController(budget)
        resumed.bind_state_path(state_path, acceptance_run_id="", state_started=True)
        assert resumed.lookup_primary_repair_scope(scope)["status"] == (
            "raw_semantically_complete"
        )
        journal = PrimaryRepairResponseJournal(journal_root)
        entry = journal.load_entry_for_scope(scope)
        assert entry is not None and entry.entry_kind == "semantic_raw_response"
        assert entry.raw_spool is not None
        assert entry.raw_spool.complete is False
        assert entry.raw_spool.completion_proof == "semantic_complete_only"

        resumed_runtime = ProviderRuntime(
            budget=ProviderBudgetV1(max_calls=1),
            aggregate_budget=resumed,
            ledger=ProviderRuntimeLedger(ledger_path),
            job_id=scope.job_id,
            attempt_id=scope.logical_attempt_identity,
            stage_name="outline_v3",
            route="candidate_1_semantic_repair",
            node_id="candidate_1_semantic_repair",
            call_id="repair-call-local-http",
            closure_epoch_id="repair-epoch-1",
            logical_attempt_identity=scope.logical_attempt_identity,
            endpoint_type="chat_completions",
            schema_hash=scope.schema_hash,
            primary_repair_response_journal=journal,
        )
        recovered = resumed_runtime.recover_primary_repair_response(
            scope,
            prompt=prompt,
            input_payload=request,
            api_config=api_config,
        )
        assert recovered is not None
        _response_ref, response = recovered
        assert response["status"] == "success"
        assert response["content"] == {"sections": []}
        assert response["response_body_complete"] is False
        assert response["transport_completion_proof"] == "semantic_complete_only"
        receipt = resumed_runtime.ledger.list_receipts()[0]
        assert receipt.output_tokens is None
        assert receipt.total_tokens is None
        assert receipt.usage_status == "unknown"
        snapshot = resumed.snapshot()
        assert snapshot["calls_used"] == 1
        assert snapshot["output_tokens_used"] == 256
        assert snapshot["calls_reserved"] == 0
        assert len(provider.requests) == 1


def test_partial_http_body_with_valid_json_prefix_is_not_recovered(
    tmp_path: Path,
) -> None:
    context = multiprocessing.get_context("spawn")
    state_path = tmp_path / "provider_budget_state.json"
    journal_root = tmp_path / "repair-responses"
    ledger_path = tmp_path / "provider_receipts.jsonl"
    request = {"candidate_id": "candidate_1", "task": "bounded repair"}
    prompt = json.dumps(request, ensure_ascii=False, sort_keys=True)
    valid_body = _raw_chat_completion('{"sections":[]}').ljust(8192, b" ")
    scope = _scope(request_hash=hash_json(request))
    budget = _budget(calls=2)
    ProviderBudgetController(budget).bind_state_path(
        state_path,
        acceptance_run_id="",
        state_started=False,
    )

    # The declared HTTP body is one byte longer than the valid JSON prefix.
    # Syntax alone must never upgrade this read to a complete response.
    with _LocalChunkedProvider(
        valid_body,
        content_length_delta=1,
    ) as provider:
        api_config = {
            "api_key": "local-partial-test-key",
            "model": "fixture-model",
            "api_base": provider.base_url,
            "provider_family": "generic",
            "endpoint_type": "chat_completions",
            "proxy_mode": "direct",
            "connect_timeout_seconds": "1",
            "read_timeout_seconds": "0.3",
            "total_timeout_seconds": "5",
            "first_body_timeout_seconds": "1",
            "transport_retries": "0",
            "raw_response_dir": str(journal_root / "raw_spools"),
        }
        process = context.Process(
            target=_crash_during_local_http_transport_worker,
            args=(
                str(state_path),
                str(journal_root),
                str(ledger_path),
                scope.to_dict(),
                request,
                prompt,
                api_config,
                "after_spool_fsync_before_finalizer",
            ),
        )
        process.start()
        process.join(timeout=20)
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
            pytest.fail("partial-body localhost crash-injection worker did not terminate")
        assert process.exitcode == 0
        assert len(provider.requests) == 1

        resumed = ProviderBudgetController(budget)
        with pytest.raises(ProviderBudgetExceeded, match="ambiguous"):
            resumed.bind_state_path(
                state_path,
                acceptance_run_id="",
                state_started=True,
            )
        snapshot = resumed.snapshot()
        assert snapshot["calls_used"] == 0
        assert snapshot["calls_reserved"] == 1
        assert len(provider.requests) == 1


def test_durable_run_wall_clamps_active_local_http_read_and_charges_unknown_spend(
    tmp_path: Path,
) -> None:
    import ai_interface
    from tests.test_run_first_timeouts import _LocalBodyServer

    request = {"candidate_id": "candidate_1", "task": "bounded repair"}
    prompt = json.dumps(request, ensure_ascii=False, sort_keys=True)
    scope = _scope(request_hash=hash_json(request))
    body = _raw_chat_completion('{"sections":[]}')
    padded = body + (b" " * max(0, 8192 - len(body))) + (b" " * 200)
    controller = ProviderBudgetController(
        ProviderAggregateBudgetV2(
            max_provider_calls_total=2,
            max_output_tokens_total=2_000,
            max_retry_attempts_total=1,
            max_wall_seconds=4,
        )
    )
    controller.bind_state_path(
        tmp_path / "provider_budget_state.json",
        acceptance_run_id="",
        state_started=False,
    )
    journal = PrimaryRepairResponseJournal(tmp_path / "repair-responses")
    ledger = ProviderRuntimeLedger(tmp_path / "provider_receipts.jsonl")
    runtime = ProviderRuntime(
        budget=ProviderBudgetV1(max_calls=1),
        aggregate_budget=controller,
        ledger=ledger,
        job_id=scope.job_id,
        attempt_id=scope.logical_attempt_identity,
        stage_name="outline_v3",
        route="candidate_1_semantic_repair",
        node_id="candidate_1_semantic_repair",
        call_id="repair-call-wall-deadline",
        closure_epoch_id="repair-epoch-1",
        logical_attempt_identity=scope.logical_attempt_identity,
        endpoint_type="chat_completions",
        schema_hash=scope.schema_hash,
        primary_repair_response_journal=journal,
    )
    admission = runtime.admit(
        estimated_tokens=100,
        requested_output_tokens=512,
        requested_retry_attempts=1,
        primary_repair_scope=scope,
        request_hash=hash_json(request),
    )
    runtime.mark_transport_started(admission)
    metadata = {"call_id": "repair-call-wall-deadline"}

    with _LocalBodyServer(
        [(0.05, padded[:8192]), (2.0, padded[8192:])]
    ) as provider:
        api_config = {
            "api_key": "local-wall-budget-test-key",
            "model": "fixture-model",
            "api_base": provider.base_url,
            "provider_family": "generic",
            "endpoint_type": "chat_completions",
            "proxy_mode": "direct",
            "connect_timeout_seconds": "1",
            "read_timeout_seconds": "5",
            "total_timeout_seconds": "10",
            "first_body_timeout_seconds": "5",
            "transport_retries": "1",
            "raw_response_dir": str(journal.raw_spool_root),
            "provider_aggregate_wall_deadline_epoch": str(
                runtime.aggregate_wall_deadline_epoch
            ),
        }
        attempt_started = runtime.primary_provider_attempt_started_callback(
            admission, scope
        )
        retain_raw = runtime.primary_repair_raw_response_callback(
            admission,
            scope,
            prompt=prompt,
            input_payload=request,
            api_config=api_config,
            response_format="json",
            metadata=metadata,
        )
        # Spend most of the persisted run envelope before the POST. The HTTP
        # call is then allowed less time than the configured request timeout.
        time.sleep(2.7)
        remaining_before_call = (
            float(runtime.aggregate_wall_deadline_epoch) - time.time()
        )
        started = time.monotonic()
        result = ai_interface._call_ai_api_detailed_uninstrumented(
            prompt,
            api_config,
            "local wall-budget system prompt",
            max_tokens=512,
            temperature=0.0,
            response_format="json",
            retry_attempts=1,
            max_retries_per_call=1,
            attempt_limit=2,
            primary_raw_response_retention_callback=retain_raw,
            primary_provider_attempt_started_callback=attempt_started,
        )
        elapsed = time.monotonic() - started
        assert len(provider.requests) == 1

    assert remaining_before_call > 0
    assert elapsed <= remaining_before_call + 0.5
    assert result["status"] == "failed"
    assert result["error_kind"] == "outcome_unknown"
    assert result["timeout_phase"] == "total_deadline"
    assert result["provider_aggregate_wall_deadline_enforced"] is True
    assert result["provider_http_attempts_started"] == 1
    assert result["response_body_complete"] is False
    receipt = runtime.complete(
        admission=admission,
        prompt=prompt,
        input_payload=request,
        api_config=api_config,
        metadata=metadata,
        result=result,
    )
    snapshot = controller.snapshot()
    assert receipt.status == "failed"
    assert receipt.attempts == 1
    assert snapshot["calls_used"] == 1
    assert snapshot["retry_attempts_used"] == 0
    assert snapshot["output_tokens_used"] == 512
    assert snapshot["calls_reserved"] == 0
    assert snapshot["output_tokens_reserved"] == 0
    assert len(ledger.list_receipts()) == 1


@pytest.mark.parametrize("tamper", ["bytes", "protocol"])
def test_raw_spool_tamper_or_protocol_drift_fails_closed_without_repost(
    tmp_path: Path,
    tamper: str,
) -> None:
    scope = _scope()
    raw_body = _raw_chat_completion('{"sections":[]}')
    controller = ProviderBudgetController(_budget())
    state_path = tmp_path / "provider_budget_state.json"
    journal = PrimaryRepairResponseJournal(tmp_path / "repair-responses")
    controller.bind_state_path(state_path, acceptance_run_id="", state_started=False)
    reservation = controller.admit(
        requested_output_tokens=256,
        requested_retry_attempts=0,
        context={"primary_repair_response_journal_root": str(journal.root)},
        primary_repair_scope=scope,
    )
    controller.mark_transport_started(reservation)
    spool = tmp_path / "adapter-response.spool"
    spool.write_bytes(raw_body)
    reference = journal.persist_raw_response(
        scope,
        reservation.reservation_id,
        spool_path=spool,
        sha256=hashlib.sha256(raw_body).hexdigest(),
        byte_count=len(raw_body),
        http_status=200,
        content_type="application/json",
        response_protocol="json",
        complete=True,
        attempt_number=1,
        receipt_binding={"schema_version": "test-binding"},
    )
    controller.retain_primary_repair_raw_response(
        reservation, scope, reference, journal=journal
    )
    entry = journal.load_entry_for_scope(scope)
    assert entry is not None and entry.raw_spool is not None
    if tamper == "bytes":
        Path(entry.raw_spool.path).write_bytes(b'{"tampered":true}')
    else:
        target = Path(reference.path)
        payload = json.loads(target.read_text(encoding="utf-8"))
        payload["raw_spool"]["response_protocol"] = "sse"
        payload["raw_attempts"][0]["response_protocol"] = "sse"
        body = dict(payload)
        body.pop("record_sha256", None)
        canonical = json.dumps(
            body,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        payload["record_sha256"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        target.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8")

    if tamper == "bytes":
        with pytest.raises(PrimaryRepairRecoveryError):
            journal.load_entry_for_scope(scope)
    else:
        drifted = journal.load_entry_for_scope(scope)
        assert drifted is not None and drifted.raw_spool is not None
        from ai_interface import reparse_provider_response_spool

        with pytest.raises(ValueError, match="protocol"):
            reparse_provider_response_spool(
                drifted.raw_spool.path,
                drifted.raw_spool.sha256,
                drifted.raw_spool.byte_count,
                drifted.raw_spool.content_type,
                drifted.raw_spool.response_protocol,
                "chat_completions",
                "fixture-model",
                "json",
                200,
                True,
            )
    with pytest.raises(ProviderBudgetExceeded, match="scope|ambiguous"):
        controller.admit(primary_repair_scope=scope)
    snapshot = controller.snapshot()
    assert snapshot["calls_used"] == 0
    assert snapshot["calls_reserved"] == 1


def test_incomplete_raw_spool_remains_ambiguous_and_cannot_start_a_retry(
    tmp_path: Path,
) -> None:
    scope = _scope()
    controller = ProviderBudgetController(
        ProviderAggregateBudgetV2(
            max_provider_calls_total=2,
            max_output_tokens_total=1_000,
            max_retry_attempts_total=1,
            max_wall_seconds=120,
        )
    )
    state_path = tmp_path / "provider_budget_state.json"
    journal = PrimaryRepairResponseJournal(tmp_path / "repair-responses")
    controller.bind_state_path(state_path, acceptance_run_id="", state_started=False)
    reservation = controller.admit(
        requested_output_tokens=256,
        requested_retry_attempts=1,
        primary_repair_scope=scope,
    )
    controller.mark_transport_started(reservation)
    spool = tmp_path / "partial-response.spool"
    raw_body = b'{"choices":[{"message":'
    spool.write_bytes(raw_body)
    reference = journal.persist_raw_response(
        scope,
        reservation.reservation_id,
        spool_path=spool,
        sha256=hashlib.sha256(raw_body).hexdigest(),
        byte_count=len(raw_body),
        http_status=200,
        content_type="application/json",
        response_protocol="json",
        complete=False,
        attempt_number=1,
        receipt_binding={"schema_version": "test-binding"},
    )
    controller.retain_primary_repair_raw_response(
        reservation, scope, reference, journal=journal
    )
    assert controller.lookup_primary_repair_scope(scope)["status"] == "ambiguous_raw_response"
    with pytest.raises(ProviderBudgetExceeded, match="ambiguous"):
        controller.admit(primary_repair_scope=_scope("candidate_2"))
    with pytest.raises(ProviderRuntimeContractError, match="incomplete"):
        controller.mark_primary_repair_attempt_started(reservation, scope, 2)
    snapshot = controller.snapshot()
    assert snapshot["calls_used"] == 0
    assert snapshot["calls_reserved"] == 2


def test_raw_retention_callback_failure_recovers_already_journaled_200_in_process(
    tmp_path: Path,
) -> None:
    request = {"candidate_id": "candidate_1", "task": "bounded repair"}
    prompt = json.dumps(request, ensure_ascii=False, sort_keys=True)
    api_config = {
        "provider_family": "fixture",
        "model": "fixture-model",
        "api_base": "https://fixture.invalid/v1",
    }
    scope = _scope(request_hash=hash_json(request))
    controller = ProviderBudgetController(_budget())
    controller.bind_state_path(
        tmp_path / "provider_budget_state.json",
        acceptance_run_id="",
        state_started=False,
    )
    journal = PrimaryRepairResponseJournal(tmp_path / "repair-responses")
    ledger = ProviderRuntimeLedger(tmp_path / "provider_receipts.jsonl")
    runtime = ProviderRuntime(
        budget=ProviderBudgetV1(max_calls=1),
        aggregate_budget=controller,
        ledger=ledger,
        job_id=scope.job_id,
        attempt_id=scope.logical_attempt_identity,
        stage_name="outline_v3",
        route="candidate_1_semantic_repair",
        node_id="candidate_1_semantic_repair",
        call_id="repair-call-retention-failure",
        closure_epoch_id="repair-epoch-1",
        logical_attempt_identity=scope.logical_attempt_identity,
        endpoint_type="chat_completions",
        schema_hash=scope.schema_hash,
        primary_repair_response_journal=journal,
    )
    metadata = {"call_id": "repair-call-retention-failure"}
    admission = runtime.admit(
        estimated_tokens=100,
        requested_output_tokens=256,
        requested_retry_attempts=0,
        primary_repair_scope=scope,
        request_hash=hash_json(request),
    )
    runtime.mark_transport_started(admission)
    attempt_started = runtime.primary_provider_attempt_started_callback(admission, scope)
    retain_raw = runtime.primary_repair_raw_response_callback(
        admission,
        scope,
        prompt=prompt,
        input_payload=request,
        api_config=api_config,
        response_format="json",
        metadata=metadata,
    )
    attempt_spool_path = attempt_started(1)
    raw_body = _raw_chat_completion('{"sections":[]}')
    attempt_started.response_headers(
        1, 200, "application/json", len(raw_body), "identity", ""
    )
    spool = Path(attempt_spool_path)
    spool.write_bytes(raw_body)

    def fail_after_journaled_raw_response(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("state update failed")

    controller.retain_primary_repair_raw_response = fail_after_journaled_raw_response
    with pytest.raises(OSError, match="state update failed"):
        retain_raw(
            str(spool),
            hashlib.sha256(raw_body).hexdigest(),
            len(raw_body),
            200,
            "application/json",
            "json",
            True,
            1,
        )
    assert controller.lookup_primary_repair_scope(scope)["status"] == "transport_started"

    receipt = runtime.complete(
        admission=admission,
        prompt=prompt,
        input_payload=request,
        api_config=api_config,
        metadata=metadata,
        result={
            "status": "failed",
            "error_kind": "outcome_unknown",
            "message": "raw response retention callback failed",
            "attempts": 1,
            "primary_raw_response_retention_callback_failed": True,
        },
    )
    assert receipt.status == "success"
    assert receipt.attempts == 1
    assert receipt.output_tokens == 19
    assert receipt.metadata["primary_repair_response_ref"]
    assert controller.snapshot()["calls_used"] == 1
    assert controller.snapshot()["calls_reserved"] == 0
    recovered = journal.load_entry_for_scope(scope)
    assert recovered is not None and recovered.entry_kind == "normalized_response"
    assert recovered.response["content"] == {"sections": []}


def test_retry_response_history_is_contiguous_and_charges_unknown_prior_usage(
    tmp_path: Path,
) -> None:
    request = {"candidate_id": "candidate_1", "task": "bounded repair"}
    scope = _scope(request_hash=hash_json(request))
    budget = ProviderAggregateBudgetV2(
        max_provider_calls_total=2,
        max_output_tokens_total=1_000,
        max_retry_attempts_total=1,
        max_wall_seconds=120,
    )
    controller = ProviderBudgetController(budget)
    controller.bind_state_path(
        tmp_path / "provider_budget_state.json",
        acceptance_run_id="",
        state_started=False,
    )
    journal = PrimaryRepairResponseJournal(tmp_path / "repair-responses")
    ledger = ProviderRuntimeLedger(tmp_path / "provider_receipts.jsonl")
    runtime = ProviderRuntime(
        budget=ProviderBudgetV1(max_calls=1),
        aggregate_budget=controller,
        ledger=ledger,
        job_id=scope.job_id,
        attempt_id=scope.logical_attempt_identity,
        stage_name="outline_v3",
        route="candidate_1_semantic_repair",
        node_id="candidate_1_semantic_repair",
        call_id="repair-call-retry",
        closure_epoch_id="repair-epoch-1",
        logical_attempt_identity=scope.logical_attempt_identity,
        endpoint_type="chat_completions",
        schema_hash=scope.schema_hash,
        primary_repair_response_journal=journal,
    )
    admission = runtime.admit(
        estimated_tokens=100,
        requested_output_tokens=200,
        requested_retry_attempts=1,
        primary_repair_scope=scope,
        request_hash=hash_json(request),
    )
    runtime.mark_transport_started(admission)
    attempt_started = runtime.primary_provider_attempt_started_callback(admission, scope)
    retain_raw = runtime.primary_repair_raw_response_callback(
        admission,
        scope,
        prompt="repair prompt",
        input_payload=request,
        api_config={
            "provider_family": "fixture",
            "model": "fixture-model",
            "api_base": "https://fixture.invalid/v1",
        },
        response_format="json",
        metadata={"call_id": "repair-call-retry"},
    )
    first_spool_path = attempt_started(1)
    first_body = b'{"error":{"message":"rate limited"}}'
    attempt_started.response_headers(
        1, 429, "application/json", len(first_body), "identity", ""
    )
    first_spool = Path(first_spool_path)
    first_spool.write_bytes(first_body)
    retain_raw(
        str(first_spool),
        hashlib.sha256(first_body).hexdigest(),
        len(first_body),
        429,
        "application/json",
        "json",
        True,
        1,
    )

    second_spool_path = attempt_started(2)
    second_body = _raw_chat_completion('{"sections":[]}')
    attempt_started.response_headers(
        2, 200, "application/json", len(second_body), "identity", ""
    )
    second_spool = Path(second_spool_path)
    second_spool.write_bytes(second_body)
    retain_raw(
        str(second_spool),
        hashlib.sha256(second_body).hexdigest(),
        len(second_body),
        200,
        "application/json",
        "json",
        True,
        2,
    )
    with pytest.raises(ProviderRuntimeContractError, match="already marked"):
        attempt_started(2)
    entry = journal.load_entry_for_scope(scope)
    assert entry is not None and entry.entry_kind == "raw_response"
    assert [item.attempt_number for item in entry.raw_attempts] == [1, 2]
    assert entry.raw_spool is not None and entry.raw_spool.attempt_number == 2

    receipt = runtime.complete(
        admission=admission,
        prompt="repair prompt",
        input_payload=request,
        api_config={
            "provider_family": "fixture",
            "model": "fixture-model",
            "api_base": "https://fixture.invalid/v1",
        },
        metadata={"call_id": "repair-call-retry"},
        result={
            "status": "success",
            "content": {"sections": []},
            "finish_reason": "stop",
            "input_tokens": 40,
            "output_tokens": 19,
            "total_tokens": 59,
            "usage_status": "reported",
            "attempts": 2,
            "endpoint_type": "chat_completions",
        },
    )
    snapshot = controller.snapshot()
    assert snapshot["calls_used"] == 2
    assert snapshot["retry_attempts_used"] == 1
    assert snapshot["output_tokens_used"] == 200
    assert receipt.attempts == 2
    assert receipt.input_tokens is None
    assert receipt.output_tokens is None
    assert receipt.total_tokens is None
    assert receipt.usage_status == "unknown"
    assert journal.load_entry_for_scope(scope).entry_kind == "normalized_response"


def test_provider_runtime_carries_journal_reference_into_durable_receipt(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "provider_budget_state.json"
    journal = PrimaryRepairResponseJournal(tmp_path / "repair-responses")
    controller = ProviderBudgetController(_budget())
    controller.bind_state_path(
        state_path,
        acceptance_run_id="",
        state_started=False,
    )
    ledger = ProviderRuntimeLedger(tmp_path / "provider_receipts.jsonl")
    request = {"candidate_id": "candidate_1", "task": "bounded repair"}
    scope = _scope(request_hash=hash_json(request))
    runtime = ProviderRuntime(
        budget=ProviderBudgetV1(max_calls=1),
        aggregate_budget=controller,
        ledger=ledger,
        job_id=scope.job_id,
        attempt_id=scope.logical_attempt_identity,
        stage_name="outline_v3",
        route="candidate_1_semantic_repair",
        node_id="candidate_1_semantic_repair",
        call_id="repair-call-1",
        logical_attempt_identity=scope.logical_attempt_identity,
        endpoint_type="chat_completions",
        schema_hash=scope.schema_hash,
        primary_repair_response_journal=journal,
    )
    admission = runtime.admit(
        estimated_tokens=100,
        requested_output_tokens=128,
        requested_retry_attempts=0,
        primary_repair_scope=scope,
        request_hash=hash_json(request),
    )
    runtime.mark_transport_started(admission)
    runtime.primary_repair_raw_response_callback(
        admission,
        scope,
        prompt="repair prompt",
        input_payload=request,
        api_config={"provider_family": "fixture", "model": "fixture-model"},
        response_format="json",
    )
    runtime.primary_provider_attempt_started_callback(admission, scope)(1)
    result = {
        "status": "success",
        "content": {"sections": []},
        "finish_reason": "stop",
        "output_tokens": 11,
        "total_tokens": 111,
        "attempts": 1,
    }
    receipt = runtime.complete(
        admission=admission,
        prompt="repair prompt",
        input_payload=request,
        api_config={"provider_family": "fixture", "model": "fixture-model"},
        result=result,
    )

    reference = receipt.metadata["primary_repair_response_ref"]
    assert reference["scope_id"] == scope.scope_id
    assert reference["reservation_id"] == admission.aggregate_reservation_id
    stored = ledger.list_receipts()
    assert len(stored) == 1
    assert stored[0].metadata["primary_repair_response_ref"] == reference
    scope_state = controller.lookup_primary_repair_scope(scope)
    assert scope_state is not None
    assert scope_state["response_ref"] == reference
    assert journal.load(reference, scope=scope) == result | {
        "raw_response_path": "",
        "raw_response_sha256": "",
        "response_bytes": 0,
    }
