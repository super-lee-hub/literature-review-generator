from __future__ import annotations

import pytest

from runtime.provider_runtime import (
    ProviderBudgetV1, ProviderRuntime, ProviderRuntimeContractError, ProviderRuntimeLedger, hash_json,
)


class _ObservedSink:
    def __init__(self, ledger):
        self.ledger = ledger
        self.values = {}
        self.captures = []
        self.requires = []
        self.fail_capture = False

    def capture(self, receipt, content):
        # An actual durable receipt must already exist, not an invented callback.
        assert any(row.to_dict() == receipt.to_dict() for row in self.ledger.list_receipts())
        self.captures.append(receipt.receipt_id)
        if self.fail_capture:
            raise OSError("snapshot filesystem unavailable")
        assert hash_json(content) == receipt.response_hash
        self.values[receipt.receipt_id] = hash_json(content)

    def require(self, receipt):
        self.requires.append(receipt.receipt_id)
        if self.values.get(receipt.receipt_id) != receipt.response_hash:
            raise ValueError("original response snapshot missing")


def _runtime(ledger, sink):
    return ProviderRuntime(
        budget=ProviderBudgetV1(max_calls=3, max_total_tokens=1000), ledger=ledger,
        job_id="snapshot-job", attempt_id="snapshot-attempt", stage_name="stage3_review",
        route="Writer_API", node_id="unchanged-U", call_id="review:unchanged-U",
        endpoint_type="chat_completions",
        response_snapshot_sink=sink,
    )


def _complete(runtime, admission, *, content=None):
    return runtime.complete(
        admission=admission, prompt="source-bound task", input_payload={"source": "selected-U"},
        api_config={"model": "local-snapshot-model", "api_base": "http://127.0.0.1:1/v1"},
        result={"status": "success", "content": content or {"answer": "grounded"},
                "finish_reason": "stop", "input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
    )


def test_capture_precedes_success_return_and_follows_durable_receipt(tmp_path):
    ledger = ProviderRuntimeLedger(tmp_path / "receipts.jsonl")
    sink = _ObservedSink(ledger)
    runtime = _runtime(ledger, sink)
    admission = runtime.admit(estimated_tokens=10, requested_output_tokens=20)
    receipt = _complete(runtime, admission)
    assert sink.captures == [receipt.receipt_id]
    assert sink.requires == []
    ledger_bytes = ledger.path.read_bytes()
    # The cached outer return must require the existing snapshot, not capture
    # caller-provided replacement content under an old successful receipt.
    assert _complete(runtime, admission, content={"different": "content"}) == receipt
    assert sink.captures == [receipt.receipt_id]
    assert sink.requires == [receipt.receipt_id]
    assert ledger.path.read_bytes() == ledger_bytes


def test_outer_cached_success_cannot_bypass_missing_snapshot(tmp_path):
    ledger = ProviderRuntimeLedger(tmp_path / "receipts.jsonl")
    sink = _ObservedSink(ledger)
    runtime = _runtime(ledger, sink)
    admission = runtime.admit(estimated_tokens=10, requested_output_tokens=20)
    receipt = _complete(runtime, admission)
    before = ledger.path.read_bytes()
    sink.values.clear()
    with pytest.raises(ProviderRuntimeContractError, match="snapshot"):
        _complete(runtime, admission)
    assert sink.requires == [receipt.receipt_id]
    assert len(sink.captures) == 1
    assert ledger.path.read_bytes() == before


def test_restarted_existing_receipt_requires_original_snapshot(tmp_path):
    ledger = ProviderRuntimeLedger(tmp_path / "receipts.jsonl")
    sink = _ObservedSink(ledger)
    runtime = _runtime(ledger, sink)
    admission = runtime.admit(estimated_tokens=10, requested_output_tokens=20)
    receipt = _complete(runtime, admission)
    before = ledger.path.read_bytes()
    restarted = _runtime(ledger, sink)
    assert _complete(restarted, admission) == receipt
    assert sink.requires == [receipt.receipt_id]
    sink.values.clear()
    with pytest.raises(ProviderRuntimeContractError, match="snapshot"):
        _complete(_runtime(ledger, sink), admission)
    assert ledger.path.read_bytes() == before
    assert len(sink.captures) == 1


def test_failed_capture_keeps_known_success_without_recapture_or_ledger_reset(tmp_path):
    ledger = ProviderRuntimeLedger(tmp_path / "receipts.jsonl")
    sink = _ObservedSink(ledger)
    sink.fail_capture = True
    runtime = _runtime(ledger, sink)
    admission = runtime.admit(estimated_tokens=10, requested_output_tokens=20)
    with pytest.raises(ProviderRuntimeContractError, match="snapshot"):
        _complete(runtime, admission)
    rows = ledger.list_receipts()
    assert len(rows) == 1 and rows[0].status == "success"
    before = ledger.path.read_bytes()
    sink.fail_capture = False
    # A new result argument is not durable recovery authority for the old call.
    with pytest.raises(ProviderRuntimeContractError, match="snapshot"):
        _complete(runtime, admission)
    assert len(sink.captures) == 1
    assert ledger.path.read_bytes() == before


def test_only_opted_in_receipts_gain_admitted_transport_identity(tmp_path):
    legacy_ledger = ProviderRuntimeLedger(tmp_path / "legacy.jsonl")
    legacy = _runtime(legacy_ledger, None)
    legacy_receipt = _complete(legacy, legacy.admit(estimated_tokens=10, requested_output_tokens=20))
    assert "transport_config" not in legacy_receipt.metadata

    ledger = ProviderRuntimeLedger(tmp_path / "successor.jsonl")
    sink = _ObservedSink(ledger)
    successor = _runtime(ledger, sink)
    receipt = _complete(successor, successor.admit(estimated_tokens=10, requested_output_tokens=20))
    assert receipt.metadata["transport_config"] == {
        "provider_family": "generic", "endpoint_type": "chat_completions",
        "api_base": "http://127.0.0.1:1/v1", "model": "local-snapshot-model",
    }
    assert ledger.list_receipts()[0].to_dict() == receipt.to_dict()


def test_existing_instrumented_transport_metadata_is_preserved(tmp_path):
    runtime = _runtime(ProviderRuntimeLedger(tmp_path / "receipts.jsonl"), _ObservedSink(None))
    rich = {"transport_config": {"provider_family": "generic", "endpoint_type": "chat_completions",
            "api_base": "http://127.0.0.1:1/v1", "model": "local-snapshot-model",
            "request_route": "actual-route", "timeout_seconds": 7}}
    config = {"model": "local-snapshot-model", "api_base": "http://127.0.0.1:1/v1", "api_key": "do-not-copy-key"}
    projected = runtime._snapshot_receipt_metadata(rich, config, "chat_completions")
    assert projected == rich and projected is not rich
    assert "do-not-copy-key" not in str(projected)
    minimal = runtime._snapshot_receipt_metadata(None, config, "chat_completions")
    assert "do-not-copy-key" not in str(minimal)
