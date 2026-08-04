from __future__ import annotations

import json

import ai_interface
from runtime.provider_context import ProviderContextProfile
from runtime.provider_runtime import (
    ProviderBudgetExceeded,
    ProviderBudgetV1,
    ProviderRuntime,
    ProviderRuntimeLedger,
)


def test_provider_runtime_enforces_budget_and_emits_redacted_append_only_receipt(tmp_path) -> None:
    ledger = ProviderRuntimeLedger(tmp_path / "provider_receipts.jsonl")
    runtime = ProviderRuntime(
        budget=ProviderBudgetV1(max_calls=1, max_total_tokens=20),
        ledger=ledger,
        job_id="job-1",
        attempt_id="attempt-1",
        stage_name="analyze",
        route="Primary_Reader_API",
        node_id="paper-1",
        call_id="call-1",
        endpoint_type="chat_completions",
    )

    admission = runtime.admit(estimated_tokens=10)
    receipt = runtime.complete(
        admission=admission,
        prompt="private prompt",
        input_payload={"text": "private input"},
        api_config={
            "api_key": "super-secret",
            "model": "test-model",
            "api_base": "https://provider.example/v1",
        },
        result={"status": "success", "content": {"answer": "ok"}},
        metadata={"authorization": "Bearer super-secret", "safe": "value"},
    )

    assert receipt.status == "success"
    assert receipt.prompt_hash != "private prompt"
    assert receipt.metadata["authorization"] == "[REDACTED_SECRET]"
    persisted = ledger.list_receipts()
    assert persisted == (receipt,)
    assert "super-secret" not in (tmp_path / "provider_receipts.jsonl").read_text(encoding="utf-8")

    try:
        runtime.admit(estimated_tokens=1)
    except ProviderBudgetExceeded as exc:
        assert "call budget" in str(exc)
    else:
        raise AssertionError("second admission should be rejected by max_calls")


def test_provider_runtime_budget_mapping_is_fail_closed_for_invalid_limits() -> None:
    budget = ProviderBudgetV1.from_mapping(
        {"max_calls": "not-a-number", "max_total_tokens": "-4", "max_elapsed_seconds": "bad"}
    )
    assert budget == ProviderBudgetV1()


def test_ai_detailed_call_attaches_durable_provider_receipt(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        ai_interface,
        "_call_ai_api_detailed_uninstrumented",
        lambda *args, **kwargs: {"status": "success", "content": {"answer": "ok"}},
    )
    ledger = ProviderRuntimeLedger(tmp_path / "receipts.jsonl")
    runtime = ProviderRuntime(
        ledger=ledger,
        job_id="job-1",
        attempt_id="attempt-1",
        stage_name="analyze",
        test_only=True,
    )
    result = ai_interface._call_ai_api_detailed(
        "prompt",
        {
            "api_key": "secret",
            "model": "test-model",
            "api_base": "https://provider.example/v1",
        },
        "system",
        provider_runtime=runtime,
    )

    assert result["status"] == "success"
    assert result["provider_receipt"]["job_id"] == "job-1"
    assert len(ledger.list_receipts()) == 1
    assert json.loads((tmp_path / "receipts.jsonl").read_text(encoding="utf-8"))["status"] == "success"


def test_ai_detailed_call_blocks_before_transport_when_budget_is_exhausted(monkeypatch) -> None:
    called = False

    def fail_if_called(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("transport should not run after budget admission failure")

    monkeypatch.setattr(ai_interface, "_call_ai_api_detailed_uninstrumented", fail_if_called)
    runtime = ProviderRuntime(budget=ProviderBudgetV1(max_total_tokens=1), test_only=True)
    result = ai_interface._call_ai_api_detailed(
        "a long prompt",
        {"api_key": "secret", "model": "test-model", "api_base": "https://provider.example/v1"},
        "system",
        provider_runtime=runtime,
    )

    assert called is False
    assert result["status"] == "failed"
    assert result["error_kind"] == "budget_exhausted"
    assert result["provider_receipt"]["status"] == "failed"


def test_ai_detailed_call_enforces_retry_budget_and_records_attempts(monkeypatch) -> None:
    calls = 0

    def fail_transport(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise ai_interface.requests.exceptions.ConnectionError("temporary network failure")

    monkeypatch.setattr(ai_interface, "_post_with_proxy_mode", fail_transport)
    monkeypatch.setattr(ai_interface, "load_config", lambda: {"Performance": {"api_retry_attempts": "5"}})
    runtime = ProviderRuntime(budget=ProviderBudgetV1(max_retries_per_call=1), test_only=True)

    result = ai_interface._call_ai_api_detailed(
        "prompt",
        {"api_key": "secret", "model": "test-model", "api_base": "https://provider.example/v1"},
        "system",
        provider_runtime=runtime,
    )

    assert result["status"] == "failed"
    assert result["error_kind"] == "transient_network"
    assert calls == 2
    assert result["attempts"] == 2
    assert result["provider_receipt"]["attempts"] == 2


def test_provider_context_admission_counts_nested_evidence_and_reserves() -> None:
    profile = ProviderContextProfile(
        provider="test",
        model="test-model",
        endpoint_type="responses",
        model_context_limit=2_048,
        verified_context_limit=1_600,
        input_budget=500,
        max_output_tokens=400,
        reasoning_reserve=100,
        safety_margin=100,
    )
    base = profile.estimate_request(
        {
            "evidence_views": [{"paper_key": "paper-1", "text": "short"}],
            "relation_candidates": [{"relation_id": "r1"}],
            "review_intent": {"question": "compare"},
        }
    )
    expanded = profile.estimate_request(
        {
            "evidence_views": [{"paper_key": "paper-1", "text": "x" * 900}],
            "relation_candidates": [{"relation_id": "r1", "evidence": "y" * 300}],
            "review_intent": {"question": "compare", "coverage_contract": "z" * 300},
        }
    )

    assert expanded["estimated_input_tokens"] > base["estimated_input_tokens"]
    assert expanded["within_budget"] is False
    assert expanded["estimated_total_tokens"] == (
        expanded["estimated_input_tokens"]
        + profile.max_output_tokens
        + profile.reasoning_reserve
        + profile.safety_margin
    )
