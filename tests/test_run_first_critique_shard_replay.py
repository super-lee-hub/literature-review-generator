from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from outline.v3_executor import OutlineV3ExecutionError
from runtime.outline_v3_replay import ModelCallReplayStore
from runtime.provider_runtime import hash_text
from tests.test_outline_v3_semantic_execution import (
    _configured_test_provider,
)
from tests.test_outline_v3_semantic_execution import (
    _executor as _base_executor,
)


def _request() -> dict[str, Any]:
    section = {
        "section_id": "source-section-1",
        "title": "Source findings",
        "goal": "Preserve the source finding and its boundary.",
        "paper_keys": ["paper-a"],
        "relation_ids": [],
        "claims": ["The source reports a bounded finding."],
        "paper_roles": {},
    }
    return {
        "node_id": "coverage_critique",
        "candidate_contents": {
            "candidate_1": {
                "candidate_id": "candidate_1",
                "organizing_logic": "source findings",
                "sections": [section],
            }
        },
        "candidate_hashes": {"candidate_1": "candidate-generation-hash"},
    }


def _make_executor(tmp_path: Path, provider_calls: list[str]):
    def provider(node_id: str, request: Mapping[str, Any]) -> Mapping[str, Any]:
        provider_calls.append(node_id)
        return _configured_test_provider(node_id, request)

    return _base_executor(
        tmp_path,
        provider=provider,
        stability_mode="off",
        max_provider_calls=1,
        candidate_count=1,
    )


def _run_critique(executor, request: Mapping[str, Any]) -> dict[str, Any]:
    return executor._run_hierarchical_critique(
        node_id="coverage_critique",
        request=request,
        dependency_hashes={"candidate_generations": "candidate-generation-hash"},
    )


def _capture_provider_call(executor):
    captured: dict[str, Any] = {}
    original = executor._provider_call

    def capture(node_id: str, request: Mapping[str, Any], **kwargs: Any) -> dict[str, Any]:
        result = original(node_id, request, **kwargs)
        if not captured:
            captured.update({
                "node_id": node_id,
                "request": deepcopy(dict(request)),
                "kwargs": deepcopy(kwargs),
                "result": deepcopy(result),
            })
        return result

    executor._provider_call = capture
    return captured, original


def test_local_critique_shard_persists_receipt_registry_output_and_exact_replay(
    tmp_path: Path,
) -> None:
    provider_calls: list[str] = []
    executor = _make_executor(tmp_path, provider_calls)
    request = _request()
    local_node_id = "coverage_critique:local:candidate_1:shard:1"
    captured, original_provider_call = _capture_provider_call(executor)

    first = _run_critique(executor, request)

    assert provider_calls == [local_node_id]
    assert executor._provider_call_count == 1
    replay_store = ModelCallReplayStore(executor.workspace)
    records = replay_store._read_records()
    assert len(records) == 1
    replay = records[0]
    assert replay.audit_node_id == local_node_id
    assert replay.closure_epoch_id == executor.closure_epoch_id
    assert len(replay.output_artifact_ids) == len(replay.receipt_ids) == 1
    assert replay.output_artifact_ids == [
        f"outline-v3:critique-shard:{executor.closure_epoch_id}:{hash_text(local_node_id)[:16]}"
    ]
    artifact = executor.registry.get(replay.output_artifact_ids[0])
    assert artifact is not None and artifact.status == "ready"
    executor.registry.verify_ready_artifact_closure(artifact)
    receipt_by_id = {
        receipt.receipt_id: receipt
        for receipt in executor._receipt_ledger.list_receipts()
    }
    receipt = receipt_by_id[replay.receipt_ids[0]]
    assert receipt.status == "success"
    assert receipt.call_id == executor._provider_call_id(local_node_id)
    assert receipt.node_id == local_node_id

    executor.artifact_records.pop(local_node_id)
    executor.artifact_paths.pop(local_node_id)
    second = original_provider_call(
        captured["node_id"],
        deepcopy(captured["request"]),
        **deepcopy(captured["kwargs"]),
    )

    assert second == captured["result"]
    assert first["passed"] is True
    assert provider_calls == [local_node_id]
    assert executor._provider_call_count == 1
    assert len(replay_store._read_records()) == 1
    assert executor._replay_evidence[-1]["lookup_status"] == "hit"
    assert executor._replay_evidence[-1]["provider_invoked"] is False
    refs = executor._dependency_refs((local_node_id,))
    assert len(refs) == 1 and refs[0].artifact_id == artifact.artifact_id
    assert refs[0].content_hash == artifact.content_hash and refs[0].path == artifact.path


@pytest.mark.parametrize("invalid_evidence", ["missing_output", "missing_receipt"])
def test_critique_shard_replay_rejects_missing_authority_without_another_call(
    tmp_path: Path,
    invalid_evidence: str,
) -> None:
    provider_calls: list[str] = []
    executor = _make_executor(tmp_path, provider_calls)
    request = _request()
    captured, original_provider_call = _capture_provider_call(executor)
    _run_critique(executor, request)
    replay_store = ModelCallReplayStore(executor.workspace)
    record = replay_store._read_records()[0]
    assert provider_calls == ["coverage_critique:local:candidate_1:shard:1"]

    if invalid_evidence == "missing_output":
        artifact = executor.registry.get(record.output_artifact_ids[0])
        assert artifact is not None
        Path(artifact.path).unlink()
    else:
        replay_store.append(
            record.key,
            output_hash=record.output_hash,
            normalized_output_hash=record.normalized_output_hash,
            output_artifact_ids=record.output_artifact_ids,
            receipt_ids=(),
            registered_artifact_hash=record.registered_artifact_hash,
            node_output_hash=record.node_output_hash,
            audit_node_id=record.audit_node_id,
            closure_epoch_id=record.closure_epoch_id,
        )

    with pytest.raises(OutlineV3ExecutionError, match="provider call budget exhausted"):
        original_provider_call(
            captured["node_id"],
            deepcopy(captured["request"]),
            **deepcopy(captured["kwargs"]),
        )

    assert provider_calls == ["coverage_critique:local:candidate_1:shard:1"]
    assert executor._provider_call_count == 1
