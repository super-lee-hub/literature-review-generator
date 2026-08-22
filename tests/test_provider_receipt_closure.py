from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from runtime.provider_receipt_closure import ExpectedProviderCall, ProviderReceiptClosure
from runtime.provider_runtime import (
    ProviderBudgetV1,
    ProviderRuntime,
    ProviderRuntimeLedger,
    hash_json,
)
from services.artifact_registry import file_sha256
from services.job_workspace import atomic_write_json


def _bound_receipt(tmp_path: Path, *, call_id: str, epoch: str = "epoch-1"):
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
        closure_epoch_id=epoch,
        endpoint_type="chat_completions",
    )
    admission = runtime.admit(estimated_tokens=1)
    payload = {"supported": True}
    receipt = runtime.complete(
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
            "content": payload,
            "input_tokens": 1,
            "output_tokens": 1,
            "total_tokens": 2,
            "usage_status": "reported",
        },
    )
    content_hash = hash_json(payload)
    artifact_path = tmp_path / f"{call_id}.artifact.json"
    atomic_write_json(
        str(artifact_path),
        {
            "artifact_type": "provider_test_output",
            "artifact_version": "v1",
            "job_id": receipt.job_id,
            "content_hash": content_hash,
            "payload": payload,
        },
    )
    registry_hash = file_sha256(artifact_path)
    receipt = replace(
        receipt,
        metadata={
            "registry_file_hash": registry_hash,
            "registry_file_path": str(artifact_path),
            "replay_output_hash": str(receipt.response_hash),
        },
    )
    expected = ExpectedProviderCall(
        call_id=receipt.call_id,
        job_id=receipt.job_id,
        attempt_id=receipt.attempt_id,
        stage_name=receipt.stage_name,
        node_id=receipt.node_id,
        closure_epoch_id=receipt.closure_epoch_id,
        logical_attempt_identity=receipt.logical_attempt_identity,
        expected_call_graph_hash=hash_json({"node_id": call_id, "call_id": call_id}),
        prompt_hash=receipt.prompt_hash,
        input_hash=receipt.input_hash,
        config_hash=receipt.config_hash,
        schema_hash=receipt.schema_hash,
        output_hash=str(receipt.response_hash),
        provider_response_hash=str(receipt.response_hash),
        normalized_output_hash=str(receipt.response_hash),
        artifact_payload_hash=hash_json(payload),
        artifact_content_hash=content_hash,
        registry_file_hash=registry_hash,
        artifact_path=str(artifact_path),
        registered_artifact_hash=content_hash,
        replay_output_hash=str(receipt.response_hash),
        node_output_hash=content_hash,
        max_attempts=1,
        usage_required=True,
    )
    return receipt, expected


def test_fully_bound_receipt_closure_passes(tmp_path: Path) -> None:
    receipt, expected = _bound_receipt(tmp_path, call_id="call-expected")

    closure = ProviderReceiptClosure.evaluate([expected], [receipt])

    assert closure.complete is True
    assert closure.missing_call_ids == ()
    assert closure.hash_mismatches == {}


def test_historical_receipts_are_isolated_from_current_epoch(tmp_path: Path) -> None:
    receipt, expected = _bound_receipt(tmp_path, call_id="call-expected")
    historical, _ = _bound_receipt(tmp_path, call_id="call-historical", epoch="epoch-old")

    closure = ProviderReceiptClosure.evaluate([expected], [receipt, historical])

    assert closure.complete is True
    assert closure.out_of_epoch_receipts == (historical.receipt_id,)
    assert historical.receipt_id in closure.historical_receipts


def test_same_epoch_unexpected_receipt_blocks_with_explicit_reason(tmp_path: Path) -> None:
    receipt, expected = _bound_receipt(tmp_path, call_id="call-expected")
    unexpected, _ = _bound_receipt(tmp_path, call_id="call-unexpected")

    closure = ProviderReceiptClosure.evaluate([expected], [receipt, unexpected])

    assert closure.complete is False
    assert closure.unexpected_receipts == (unexpected.call_id,)


def test_hash_mismatch_blocks_with_explicit_reason(tmp_path: Path) -> None:
    receipt, expected = _bound_receipt(tmp_path, call_id="call-hash")
    mismatched = replace(expected, provider_response_hash="f" * 64)

    closure = ProviderReceiptClosure.evaluate([mismatched], [receipt])

    assert closure.complete is False
    assert "provider_response_hash" in closure.hash_mismatches[receipt.call_id]


def test_request_variant_accepts_exact_backup_identity_without_wildcarding(tmp_path: Path) -> None:
    receipt, expected = _bound_receipt(tmp_path, call_id="call-request-variant")
    variant = replace(
        expected,
        input_hash="a" * 64,
        config_hash="b" * 64,
        request_variants=(
            {"input_hash": receipt.input_hash, "config_hash": receipt.config_hash},
        ),
    )

    closure = ProviderReceiptClosure.evaluate([variant], [receipt])

    assert closure.complete is True
    assert closure.hash_mismatches == {}


@pytest.mark.parametrize(
    "field_name",
    ("job_id", "attempt_id", "stage_name", "node_id", "prompt_hash", "input_hash", "config_hash", "schema_hash"),
)
def test_receipt_binding_identity_mismatch_is_stale_and_blocked(
    tmp_path: Path,
    field_name: str,
) -> None:
    receipt, expected = _bound_receipt(tmp_path, call_id=f"call-{field_name}")
    value = "other-stage" if field_name == "stage_name" else "f" * 64
    mismatched = replace(expected, **{field_name: value})

    closure = ProviderReceiptClosure.evaluate([mismatched], [receipt])

    assert closure.complete is False
    assert closure.stale_call_ids == (receipt.call_id,)
    assert field_name in closure.hash_mismatches[receipt.call_id]


def test_receipt_closure_rejects_a_changed_logical_attempt_identity(tmp_path: Path) -> None:
    receipt, expected = _bound_receipt(tmp_path, call_id="call-attempt-identity")
    mismatched = replace(expected, logical_attempt_identity="other-attempt")

    closure = ProviderReceiptClosure.evaluate([mismatched], [receipt])

    assert closure.complete is False
    assert closure.stale_call_ids == (receipt.call_id,)
    assert "logical_attempt_identity" in closure.hash_mismatches[receipt.call_id]


def test_missing_expected_call_blocks_with_explicit_reason(tmp_path: Path) -> None:
    _receipt, expected = _bound_receipt(tmp_path, call_id="call-missing")

    closure = ProviderReceiptClosure.evaluate([expected], [])

    assert closure.complete is False
    assert closure.missing_call_ids == (expected.call_id,)
