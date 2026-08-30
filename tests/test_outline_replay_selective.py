"""Regression tests for selective Outline replay reuse across closure epochs (item F).

Before the fix, ``closure_epoch_id`` bound the full routing manifest, so changing
any single critic route invalidated every node's replay because the per-record
epoch check hard-failed. Reuse is now gated on the *per-node* provider config
hash (which the content-addressed replay store already keys on), so an unrelated
node whose own route is unchanged stays reusable even after the global epoch
changes. Verification still runs through every other receipt check -- reuse is
verified, never forged.
"""

import types

import pytest

from outline.v3_executor import OutlineV3Executor


def _make_receipt(closure_epoch_id: str = "EPOCH_OLD", **overrides) -> object:
    data = dict(
        receipt_id="r1",
        status="success",
        response_hash="H",
        job_id="J",
        attempt_id="outline:N",
        node_id="N",
        call_id="outline:N",
        closure_epoch_id=closure_epoch_id,
        prompt_hash="P",
        input_hash="I",
        config_hash="CFG_X",
        provider="anthropic",
        model="m",
        endpoint_type="anthropic",
        endpoint="h",
        schema_hash="S",
        finish_reason="stop",
        incomplete_reason="",
        usage_status="reported",
    )
    data.update(overrides)
    return types.SimpleNamespace(**data)


def _make_binding(provider_config_hash: str = "CFG_X", **overrides) -> dict:
    data = dict(
        node_id="N",
        semantic_node_id="N",
        receipt_ids=["r1"],
        prompt_hash="P",
        prompt_payload_hash="I",
        provider_config_hash=provider_config_hash,
        provider_family="anthropic",
        model_name="m",
        endpoint_type="anthropic",
        api_base_host="h",
        schema_hash="S",
    )
    data.update(overrides)
    return data


class _FakeExecutor:
    """Minimal harness exposing only what ``_replay_record_is_valid`` touches."""

    def __init__(self, closure_epoch_id: str, receipt_epoch: str) -> None:
        self.closure_epoch_id = closure_epoch_id
        self.job_id = "J"
        self.replay_diagnostics: list[str] = []
        self._semantic_node_id = lambda node_id: node_id  # type: ignore[assignment]
        self._receipt_ledger = types.SimpleNamespace(
            list_receipts=lambda: [_make_receipt(closure_epoch_id=receipt_epoch)]
        )

    def _replay_record_is_valid(self, record, binding):  # type: ignore[override]
        return OutlineV3Executor._replay_record_is_valid(self, record, binding)

    def _replay_receipt_index(self) -> dict:
        # Isolate the gating logic: return the current-epoch receipts. The
        # cross-epoch registry scan is exercised by the production method but is
        # out of scope for this unit test of the epoch-vs-config decision.
        return {r.receipt_id: r for r in self._receipt_ledger.list_receipts()}


def test_selective_reuse_keeps_unrelated_node_across_epoch_change() -> None:
    # Global epoch changed (EPOCH_NEW) but the node's own config hash matches the
    # verified receipt, so reuse must remain valid -- this is the bug F fixes.
    fake = _FakeExecutor(closure_epoch_id="EPOCH_NEW", receipt_epoch="EPOCH_OLD")
    record = types.SimpleNamespace(receipt_ids=["r1"], normalized_output_hash="H", output_hash="H")
    assert fake._replay_record_is_valid(record, _make_binding()) is True
    assert any("prior-epoch" in d for d in fake.replay_diagnostics)


def test_selective_reuse_invalidates_only_the_changed_node() -> None:
    # This node's route config changed (CFG_Y != receipt CFG_X): reuse must be
    # rejected, proving a route change invalidates exactly the affected node.
    fake = _FakeExecutor(closure_epoch_id="EPOCH_NEW", receipt_epoch="EPOCH_OLD")
    record = types.SimpleNamespace(receipt_ids=["r1"], normalized_output_hash="H", output_hash="H")
    assert fake._replay_record_is_valid(record, _make_binding(provider_config_hash="CFG_Y")) is False


def test_selective_reuse_respects_same_epoch_too() -> None:
    # Sanity: within the same epoch with a matching config, reuse is valid.
    fake = _FakeExecutor(closure_epoch_id="EPOCH_SAME", receipt_epoch="EPOCH_SAME")
    record = types.SimpleNamespace(receipt_ids=["r1"], normalized_output_hash="H", output_hash="H")
    assert fake._replay_record_is_valid(record, _make_binding()) is True
