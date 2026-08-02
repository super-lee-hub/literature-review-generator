from __future__ import annotations

import json

import ai_interface
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
    runtime = ProviderRuntime(budget=ProviderBudgetV1(max_total_tokens=1))
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
