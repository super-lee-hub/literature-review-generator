from __future__ import annotations

from pathlib import Path
from typing import Any
import json

import pytest

from outline.v3_executor import OutlineV3ExecutionError
from outline.evidence_alias import build_alias_map
from runtime.pause_state import PauseRequestedError
from tests.test_outline_candidate_contract_robustness import _executor, _sections


def _repair(executor: Any, content: dict[str, Any], *, aliases: bool = False) -> dict[str, Any]:
    return executor._semantic_repair_candidate(
        "candidate_1", content, ValueError("outside-corpus paper key"),
        allowed_paper_keys=["paper-a"], allowed_relation_ids=[],
        alias_map=build_alias_map(["paper-a", "paper-b"], []) if aliases else None,
    )


@pytest.mark.parametrize("restart", [False, True])
@pytest.mark.parametrize("aliases", [False, True])
def test_validated_primary_repair_replays_without_another_provider_call(
    tmp_path: Path, restart: bool, aliases: bool,
) -> None:
    calls: list[str] = []

    def provider(node_id: str, request: Any) -> dict[str, Any]:
        calls.append(node_id)
        return _sections("candidate_1", request["allowed_paper_ids"])

    executor = _executor(tmp_path, provider=provider, semantic_repair_enabled=True, opaque_alias_enabled=aliases)
    content = _sections("candidate_1", ["outside-corpus"])["content"]
    first = _repair(executor, content, aliases=aliases)
    if restart:
        executor = _executor(tmp_path, provider=provider, semantic_repair_enabled=True, opaque_alias_enabled=aliases)
    second = _repair(executor, content, aliases=aliases)
    assert second == first
    assert calls == ["candidate_1_semantic_repair"]


@pytest.mark.parametrize("restart", [False, True])
def test_failed_primary_repair_is_not_reissued_in_the_same_intent(tmp_path: Path, restart: bool) -> None:
    calls: list[str] = []

    def provider(node_id: str, _request: Any) -> dict[str, Any]:
        calls.append(node_id)
        raise TimeoutError("synthetic started request lost its result")

    executor = _executor(tmp_path, provider=provider, semantic_repair_enabled=True)
    content = _sections("candidate_1", ["outside-corpus"])["content"]
    with pytest.raises(TimeoutError, match="synthetic started request"):
        _repair(executor, content)
    if restart:
        executor = _executor(tmp_path, provider=provider, semantic_repair_enabled=True)
    with pytest.raises(OutlineV3ExecutionError, match="already attempted"):
        _repair(executor, content)
    assert calls == ["candidate_1_semantic_repair"]


def test_success_receipt_without_a_committed_output_blocks_new_repair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def provider(node_id: str, _request: Any) -> dict[str, Any]:
        calls.append(node_id)
        return _sections("candidate_1", ["paper-a"])

    executor = _executor(tmp_path, provider=provider, semantic_repair_enabled=True)
    content = _sections("candidate_1", ["outside-corpus"])["content"]

    def interrupted_publication(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("synthetic crash after completed receipt before artifact publication")

    monkeypatch.setattr(executor, "_persist_repair_output", interrupted_publication)
    with pytest.raises(RuntimeError, match="synthetic crash"):
        _repair(executor, content)
    recovered = _executor(tmp_path, provider=provider, semantic_repair_enabled=True)
    with pytest.raises(OutlineV3ExecutionError, match="already attempted"):
        _repair(recovered, content)
    assert calls == ["candidate_1_semantic_repair"]


@pytest.mark.parametrize("status", ["quarantined", "invalid"])
def test_unrelated_nonready_failure_does_not_block_fresh_repair(tmp_path: Path, status: str) -> None:
    calls: list[str] = []

    def provider(node_id: str, _request: Any) -> dict[str, Any]:
        calls.append(node_id)
        return _sections("candidate_1", ["paper-a"])

    executor = _executor(tmp_path, provider=provider, semantic_repair_enabled=True)
    failure = tmp_path / "unrelated-failure.json"
    failure.write_text(json.dumps({
        "candidate_id": "candidate_2", "repair_node_id": "candidate_2_semantic_repair",
        "attempt_identity": "unrelated-intent",
    }), encoding="utf-8")
    executor.registry.register_file(
        artifact_role="outline_repair", artifact_type="outline_candidate_repair_failure",
        artifact_version="v1", producer="test.fixture", path=failure, status=status,
    )
    _repair(executor, _sections("candidate_1", ["outside-corpus"])["content"])
    assert calls == ["candidate_1_semantic_repair"]


def test_unstarted_budget_rejection_can_resume_without_consuming_a_repair(tmp_path: Path) -> None:
    calls: list[str] = []

    def provider(node_id: str, _request: Any) -> dict[str, Any]:
        calls.append(node_id)
        return _sections("candidate_1", ["paper-a"])

    executor = _executor(tmp_path, provider=provider, semantic_repair_enabled=True)
    executor.max_provider_calls = 0
    content = _sections("candidate_1", ["outside-corpus"])["content"]
    with pytest.raises(OutlineV3ExecutionError, match="provider call budget exhausted"):
        _repair(executor, content)
    assert calls == []
    assert not executor._receipt_ledger.list_receipts()
    # The first invocation was never admitted. This is a local fixture cap,
    # not a reset of any durable aggregate authorization or usage.
    executor.max_provider_calls = 1
    _repair(executor, content)
    assert calls == ["candidate_1_semantic_repair"]


def test_paused_unstarted_repair_runs_after_explicit_resume(tmp_path: Path) -> None:
    calls: list[str] = []

    def provider(node_id: str, _request: Any) -> dict[str, Any]:
        calls.append(node_id)
        return _sections("candidate_1", ["paper-a"])

    executor = _executor(tmp_path, provider=provider, semantic_repair_enabled=True)
    content = _sections("candidate_1", ["outside-corpus"])["content"]
    executor._pause_state.request(reason="pause before the first repair")
    with pytest.raises(PauseRequestedError):
        _repair(executor, content)
    assert calls == []
    assert not executor._receipt_ledger.list_receipts()
    executor._pause_state.clear(reason="explicit synthetic resume")
    _repair(executor, content)
    assert calls == ["candidate_1_semantic_repair"]
