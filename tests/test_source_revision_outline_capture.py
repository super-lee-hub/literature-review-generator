"""Actual Outline HTTP capture and legacy-compatible same-job replay."""
from __future__ import annotations

import json
from pathlib import Path

from runtime.provider_runtime import ProviderCallReceiptV1, hash_json
from runtime.runner import AgentRuntimeRunner
from runtime.source_revision_response_capture import build_source_revision_response_sink
from services.artifact_registry import ArtifactRegistry
from tests.test_run_first_candidate_adversarial import _sealed_relation_scope
from tests.test_run_first_candidate_provider_replay import (
    _LocalFragmentAPI, _make_executor, _prepare_and_organize,
)


def test_actual_outline_http_preserves_original_response_and_restarts_without_post(tmp_path):
    fixture = _sealed_relation_scope(tmp_path)
    workspace, registry, *_ = fixture
    metadata = {"provider_semantic_contract_version": "source-closed-provider/v2"}
    with _LocalFragmentAPI() as server:
        executor, axis, plan = _make_executor(server, fixture)
        sink = build_source_revision_response_sink(metadata, workspace, registry, executor.publication_context)
        assert sink is not None
        executor.response_snapshot_sink = sink
        _prepare_and_organize(executor, fixture, axis, plan)
        receipts = executor._receipt_ledger.list_receipts()
        assert len(receipts) == len(server.requests) >= 2
        records = []
        for receipt in receipts:
            assert receipt.test_only is False and receipt.status == "success"
            record = sink.require(receipt)
            # Exercise the actual Runner/reconcile dispatch for the new v1
            # snapshot artifact, rather than only the store's own validator.
            AgentRuntimeRunner._schema_validators()["provider_response_snapshot"](record, Path(record.path))
            payload = json.loads(Path(record.path).read_bytes())
            assert ProviderCallReceiptV1.from_dict(payload["receipt"]).to_dict() == receipt.to_dict()
            assert payload["receipt_hash"] == hash_json(receipt.to_dict())
            assert payload["response_hash"] == hash_json(payload["content"]) == receipt.response_hash
            records.append((record.artifact_id, record.content_hash))

        sent = len(server.requests)
        fresh_registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
        restarted, next_axis, next_plan = _make_executor(server, fixture, fresh_registry)
        fresh_sink = build_source_revision_response_sink(metadata, workspace, fresh_registry, restarted.publication_context)
        assert fresh_sink is not None
        restarted.response_snapshot_sink = fresh_sink
        _prepare_and_organize(restarted, fixture, next_axis, next_plan)
        assert len(server.requests) == sent
        for receipt, expected in zip(receipts, records, strict=True):
            record = fresh_sink.require(receipt)
            assert (record.artifact_id, record.content_hash) == expected
