from __future__ import annotations

from pathlib import Path

from runtime.provider_receipt_closure import ExpectedProviderCall, ProviderReceiptClosure
from runtime.provider_runtime import ProviderBudgetV1, ProviderRuntime, ProviderRuntimeLedger


def _receipt(tmp_path: Path, *, call_id: str):
    ledger = ProviderRuntimeLedger(tmp_path / f"{call_id}.jsonl")
    runtime = ProviderRuntime(
        budget=ProviderBudgetV1(max_calls=1),
        ledger=ledger,
        job_id="job-closure",
        attempt_id="attempt-1",
        stage_name="stage-test",
        route="validator",
        schema_hash="a" * 64,
        node_id=call_id,
        call_id=call_id,
        endpoint_type="chat_completions",
    )
    admission = runtime.admit(estimated_tokens=1)
    return runtime.complete(
        admission=admission,
        prompt="prompt",
        input_payload={"claim": call_id},
        api_config={
            "provider_family": "test",
            "model": "test-model",
            "api_base": "https://example.invalid",
            "endpoint_type": "chat_completions",
        },
        result={
            "status": "success",
            "content": {"supported": True},
            "input_tokens": 1,
            "output_tokens": 1,
            "total_tokens": 2,
            "usage_status": "reported",
        },
    )


def test_out_of_scope_receipts_block_closure(tmp_path: Path) -> None:
    expected_receipt = _receipt(tmp_path, call_id="call-expected")
    out_of_scope_receipt = _receipt(tmp_path, call_id="call-other")
    expected = ExpectedProviderCall(
        call_id=expected_receipt.call_id,
        job_id=expected_receipt.job_id,
        attempt_id=expected_receipt.attempt_id,
        stage_name=expected_receipt.stage_name,
        node_id=expected_receipt.node_id,
        prompt_hash=expected_receipt.prompt_hash,
        input_hash=expected_receipt.input_hash,
        config_hash=expected_receipt.config_hash,
        schema_hash=expected_receipt.schema_hash,
        provider_response_hash=str(expected_receipt.response_hash),
        normalized_output_hash=str(expected_receipt.response_hash),
        max_attempts=1,
        usage_required=True,
    )

    closure = ProviderReceiptClosure.evaluate(
        [expected],
        [expected_receipt],
        out_of_scope=[out_of_scope_receipt],
    )

    assert closure.out_of_scope_receipts == ("call-other",)
    assert closure.complete is False
