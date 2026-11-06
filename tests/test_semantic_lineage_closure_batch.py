"""Scoped GLOBAL lineage discovery and operation-local closure controls."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import socket
from typing import Any

import pytest

from outline.v3_executor import OutlineV3ExecutionError
from runtime.provider_runtime import hash_json
from services.artifact_registry import ArtifactDependencyRefV2, ArtifactRegistry, RegistryError
from tests import test_global_retained_frontier_reduction as global_case
from tests.test_current_runtime_full_e2e import _source_closed_global_content


NODE = global_case.NODE


@pytest.fixture(autouse=True)
def _no_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    def denied(*_args: Any, **_kwargs: Any) -> None:
        pytest.fail("lineage closure units must not use network or providers")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket, "getaddrinfo", denied)


def _scoped(base: dict[str, Any], marker: Any, members: Any, *, final: bool = True) -> dict[str, Any]:
    return {
        **deepcopy(base), "cross_group_comparison": {}, "global_retention_source": deepcopy(marker),
        "topic_synthesis": deepcopy(members),
        "hierarchy": {"level": "bounded_semantic_final_fold" if final else "bounded_semantic_reduction"},
    }


def _fixture(tmp_path: Path, *, count: int = 3) -> tuple[Any, Any, Any, Any, Any, Any]:
    executor, request, _parent, _items, observed = global_case._case(
        tmp_path, large=False, count=count, deferred_from=count - 1,
    )
    marker, original, authority = executor._global_retention_authority(request)
    scoped = _scoped(request, marker, original["topic_synthesis"])
    return executor, request, scoped, original["topic_synthesis"], authority, observed


def _oracle(executor: Any, request: Any, node: str = NODE) -> Any:
    marker, original, authority = executor._global_retention_authority(request)
    frontier = {hash_json(item): item for item in original["topic_synthesis"]}
    # Stable-input semantic walk oracle only. None retains real singleton
    # loader checks; wrapper end fences are tested through the public method.
    return executor._global_retention_member_scope_checked(
        node, request, frontier=frontier, marker=marker, authority=authority, verify_closure=None,
    )


def _nest(executor: Any, base: Any, marker: Any, source: Any, *, level: int, index: int,
          shared_claim: bool = False) -> tuple[dict[str, Any], Any, Any]:
    node = f"{NODE}:reduce:{level}:{index}"
    child = _scoped(base, marker, [source], final=False)
    child["hierarchy"].update(reduction_level=level, group_index=index)
    records, _reasons = executor._global_retention_member_scope(node, child)
    manifest = deepcopy(executor._load_semantic_reducer_coverage_ref(source))
    manifest.update(
        node_id=node, reduction_level=level, group_index=index,
        global_retention_source=deepcopy(marker), global_source_items=[deepcopy(source)],
        source_item_hashes=[hash_json(source)], source_input_hash=hash_json([source]),
        child_input_manifest_hashes=[source["coverage_ref"]["registry_file_hash"]],
        provider_request_hash=hash_json(child),
    )
    coverage = executor._persist_semantic_reducer_input_manifest(
        node, manifest, dependency_hashes={}, source_records=records,
    )
    result = _source_closed_global_content(child)
    for offset, claim in enumerate(result["synthesis_claims"]):
        claim["claim_id"] = (
            "synthesis:global_synthesis:shared-lineage-claim" if shared_claim
            else f"synthesis:global_synthesis:lineage:{level}:{index}:{offset}"
        )
        claim["source_claim_ids"] = [key for key in claim["source_claim_ids"] if not key.startswith("synthesis:")]
    executor._validate_semantic_provider_output(node, child, result)
    output = executor._persist_semantic_provider_output(
        node, result, dependency_hashes={}, recovery_sources=(coverage,),
    )
    member = deepcopy(source)
    member.pop("topic_disposition_groups", None)
    member.update(reducer_id=node, semantic_result={key: value for key, value in result.items() if key not in {
        "processed_topic_ids", "processed_fragment_ids", "processed_result_ids", "processed_relation_ids",
    }})
    maps = {key: manifest[key] for key in ("topic_members_by_id", "fragment_members_by_id", "relation_members_by_id")}
    member["coverage_ref"].update(
        artifact_id=coverage.artifact_id, registry_file_hash=coverage.content_hash,
        artifact_content_hash=coverage.metadata["content_hash"], node_id=node,
        manifest_hash=hash_json(manifest), membership_hash=hash_json(maps),
    )
    member["semantic_result_ref"].update(
        artifact_id=output.artifact_id, registry_file_hash=output.content_hash,
        artifact_content_hash=output.metadata["content_hash"], provider_result_hash=hash_json(result),
    )
    return member, coverage, output


def _watch(executor: Any, expected: Any, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    ids = {record.artifact_id for record in expected}
    calls: list[tuple[str, ...]] = []
    callbacks = []
    state = {"phase": "authority", "unions": calls, "callbacks": callbacks, "discovery_coverage": set()}
    authority = executor._global_retention_authority
    batch = executor.registry.verify_ready_artifact_closures
    checked = executor._global_retention_member_scope_checked
    coverage = executor._load_semantic_reducer_coverage_ref
    result = executor._load_semantic_reducer_result_ref

    def read_authority(request: Any) -> Any:
        state["phase"] = "authority"
        try:
            return authority(request)
        finally:
            state["phase"] = "discovery"

    def union(roots: Any, **kwargs: Any) -> Any:
        roots = tuple(roots)
        found = tuple(row.artifact_id for row in roots)
        if set(found) == ids and len(found) == len(ids):
            calls.append(found)
        return batch(roots, **kwargs)

    def read_coverage(value: Any, *, verify_closure: Any = None) -> Any:
        if state["phase"] == "discovery" and not calls and isinstance(value.get("coverage_ref"), dict):
            assert callable(verify_closure)
            state["discovery_coverage"].add(value["coverage_ref"]["artifact_id"])
        return coverage(value, verify_closure=verify_closure)

    def read_result(value: Any, *, verify_closure: Any = None) -> Any:
        assert state["phase"] != "discovery" or calls, "discovery must not restore results before the complete union"
        return result(value, verify_closure=verify_closure)

    def semantic(node: str, request: Any, *, frontier: Any, marker: Any, authority: Any, verify_closure: Any) -> Any:
        state["phase"] = "checked"
        assert callable(verify_closure) and len(calls) == 1
        callbacks.append(verify_closure)
        return checked(node, request, frontier=frontier, marker=marker, authority=authority, verify_closure=verify_closure)

    monkeypatch.setattr(executor, "_global_retention_authority", read_authority)
    monkeypatch.setattr(executor.registry, "verify_ready_artifact_closures", union)
    monkeypatch.setattr(executor, "_load_semantic_reducer_coverage_ref", read_coverage)
    monkeypatch.setattr(executor, "_load_semantic_reducer_result_ref", read_result)
    monkeypatch.setattr(executor, "_global_retention_member_scope_checked", semantic)
    return state


def _after_checked(executor: Any, monkeypatch: pytest.MonkeyPatch, effect: Any) -> tuple[Any, list[Any]]:
    real = executor._global_retention_member_scope_checked
    callbacks = []

    def checked(node: str, request: Any, *, frontier: Any, marker: Any, authority: Any, verify_closure: Any) -> Any:
        callbacks.append(verify_closure)
        output = real(node, request, frontier=frontier, marker=marker, authority=authority, verify_closure=verify_closure)
        effect(verify_closure)
        return output

    monkeypatch.setattr(executor, "_global_retention_member_scope_checked", checked)
    return real, callbacks


def _rewrite(executor: Any, artifact_id: str, change: Any, *, revision: bool = False) -> None:
    path = Path(executor.registry.registry_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    row = next(row for row in payload["artifacts"] if row["artifact_id"] == artifact_id)
    change(row)
    payload["revision"] += int(revision)
    path.write_text(json.dumps(payload), encoding="utf-8")


@pytest.mark.parametrize("count", [2, 3])
def test_complete_lineage_union_preserves_source_order_reasons_and_readonly_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, count: int,
) -> None:
    executor, _base, request, leaves, authority, observed = _fixture(tmp_path, count=count)
    expected, reasons = _oracle(executor, request)
    before = deepcopy(request)
    watched = _watch(executor, expected, monkeypatch)
    records, actual_reasons = executor._global_retention_member_scope(NODE, request)
    assert records == expected and actual_reasons == reasons == []
    assert [record.artifact_id for record in records[:len(authority)]] == [record.artifact_id for record in authority]
    assert len(watched["unions"]) == 2
    assert watched["discovery_coverage"] == {item["coverage_ref"]["artifact_id"] for item in leaves}
    assert request == before and observed == [] and executor._receipt_ledger.list_receipts() == ()
    for callback in watched["callbacks"]:
        with pytest.raises(OutlineV3ExecutionError):
            callback(records[0])


def test_nested_grandchild_roots_and_typed_leaf_dependencies_are_all_proved(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    executor, base, request, leaves, _authority, observed = _fixture(tmp_path)
    marker = request["global_retention_source"]
    child, child_input, child_output = _nest(executor, base, marker, leaves[0], level=1, index=1)
    grandchild, grand_input, grand_output = _nest(executor, base, marker, child, level=2, index=1)
    request["topic_synthesis"] = [grandchild, *leaves[1:]]
    expected, reasons = _oracle(executor, request)
    expected_ids = {record.artifact_id for record in expected}
    for item in [*leaves, child, grandchild]:
        assert {item[key]["artifact_id"] for key in ("coverage_ref", "semantic_result_ref")} <= expected_ids
    assert ArtifactDependencyRefV2.from_record(child_input) in child_output.depends_on
    assert ArtifactDependencyRefV2.from_record(grand_input) in grand_output.depends_on
    assert {child_input.artifact_id, child_output.artifact_id} <= {ref.artifact_id for ref in grand_input.depends_on}
    watched = _watch(executor, expected, monkeypatch)
    actual = executor._global_retention_member_scope(NODE, request)
    assert actual == (expected, reasons) and len(watched["unions"]) == 2
    assert observed == [] and executor._receipt_ledger.list_receipts() == ()


def test_deferred_only_subset_keeps_original_sorted_reasons(tmp_path: Path) -> None:
    executor, _base, request, leaves, _authority, observed = _fixture(tmp_path)
    request["topic_synthesis"] = [leaves[-1]]
    node = f"{NODE}:reduce:1:99"
    expected, reasons = _oracle(executor, request, node)
    assert reasons == sorted({leaves[-1]["topic_disposition_groups"][0]["reason"], *leaves[-1]["semantic_result"]["unresolved_questions"]})
    assert executor._global_retention_member_scope(node, request) == (expected, reasons)
    assert observed == []


@pytest.mark.parametrize("change", ["missing_ref", "ref_hash", "initial_bytes"])
def test_initial_missing_stale_reference_or_leaf_bytes_reject(tmp_path: Path, change: str) -> None:
    executor, _base, request, leaves, _authority, observed = _fixture(tmp_path)
    if change == "missing_ref":
        request["topic_synthesis"][0]["coverage_ref"]["artifact_id"] = "missing:lineage"
    elif change == "ref_hash":
        request["topic_synthesis"][0]["semantic_result_ref"]["provider_result_hash"] = "0" * 64
    else:
        record = executor.registry.get(leaves[0]["semantic_result_ref"]["artifact_id"])
        Path(record.path).write_bytes(Path(record.path).read_bytes() + b" ")
    with pytest.raises((OutlineV3ExecutionError, RegistryError)):
        executor._global_retention_member_scope(NODE, request)
    assert observed == [] and executor._receipt_ledger.list_receipts() == ()


def test_nested_manifest_cannot_omit_a_typed_leaf_dependency(tmp_path: Path) -> None:
    executor, base, request, leaves, _authority, observed = _fixture(tmp_path)
    child, coverage, _result = _nest(executor, base, request["global_retention_source"], leaves[0], level=1, index=1)
    request["topic_synthesis"] = [child, *leaves[1:]]
    missing_id = leaves[0]["semantic_result_ref"]["artifact_id"]
    _rewrite(executor, coverage.artifact_id, lambda row: row.update(depends_on=[ref for ref in row["depends_on"] if ref["artifact_id"] != missing_id]))
    with pytest.raises(OutlineV3ExecutionError, match="typed|dependency"):
        executor._global_retention_member_scope(NODE, request)
    assert observed == []


@pytest.mark.parametrize("change", ["missing_leaf", "repeated_leaf", "claim_conflict"])
def test_existing_partition_and_claim_conflict_oracles_still_reject(tmp_path: Path, change: str) -> None:
    executor, base, request, leaves, _authority, observed = _fixture(tmp_path)
    if change == "missing_leaf":
        request["topic_synthesis"] = leaves[:-1]
    elif change == "repeated_leaf":
        request["topic_synthesis"].append(deepcopy(leaves[0]))
    else:
        marker = request["global_retention_source"]
        first, *_ = _nest(executor, base, marker, leaves[0], level=1, index=1, shared_claim=True)
        second, *_ = _nest(executor, base, marker, leaves[1], level=1, index=2, shared_claim=True)
        request["topic_synthesis"] = [first, second, *leaves[2:]]
    with pytest.raises(OutlineV3ExecutionError, match="member|frontier|conflict"):
        executor._global_retention_member_scope(NODE, request)
    assert observed == [] and executor._receipt_ledger.list_receipts() == ()


def test_real_typed_dependency_cycle_cannot_enter_lineage_union(tmp_path: Path) -> None:
    executor, base, request, leaves, _authority, observed = _fixture(tmp_path)
    child, coverage, _result = _nest(executor, base, request["global_retention_source"], leaves[0], level=1, index=1)
    request["topic_synthesis"] = [child, *leaves[1:]]
    ref = ArtifactDependencyRefV2.from_record(coverage).to_dict()
    _rewrite(executor, coverage.artifact_id, lambda row: row["depends_on"].append(ref))
    with pytest.raises((OutlineV3ExecutionError, RegistryError)):
        executor._global_retention_member_scope(NODE, request)
    assert observed == []


@pytest.mark.parametrize("change", ["bytes", "metadata", "edge", "false_zero", "int_float", "revision"])
def test_late_change_after_real_semantic_walk_is_rejected_and_callback_revoked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str,
) -> None:
    executor, _base, request, leaves, _authority, observed = _fixture(tmp_path)
    target = executor.registry.get(leaves[0]["coverage_ref"]["artifact_id"])
    executor.registry.update_record(target.artifact_id, metadata_updates={"typed_marker": 1 if change == "int_float" else False})
    target = executor.registry.get(target.artifact_id)

    def mutate(_callback: Any) -> None:
        if change == "bytes":
            Path(target.path).write_bytes(Path(target.path).read_bytes() + b" ")
            return
        def alter(row: Any) -> None:
            if change == "metadata":
                row["metadata"]["changed"] = True
            elif change == "edge":
                source = executor.artifact_records["topic_synthesis"]
                row["depends_on"].append(ArtifactDependencyRefV2.from_record(source).to_dict())
            elif change == "false_zero":
                row["metadata"]["typed_marker"] = 0
            elif change == "int_float":
                row["metadata"]["typed_marker"] = 1.0
        _rewrite(executor, target.artifact_id, alter, revision=change == "revision")

    _real, callbacks = _after_checked(executor, monkeypatch, mutate)
    with pytest.raises((OutlineV3ExecutionError, RegistryError)):
        executor._global_retention_member_scope(NODE, request)
    assert len(callbacks) == 1
    with pytest.raises(OutlineV3ExecutionError):
        callbacks[0](target)
    assert observed == [] and executor._receipt_ledger.list_receipts() == ()


def test_callback_cannot_grant_a_record_outside_the_complete_union(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    executor, _base, request, leaves, _authority, observed = _fixture(tmp_path)
    target = executor.registry.get(leaves[0]["coverage_ref"]["artifact_id"])
    outside = replace(target, artifact_id="outside:lineage-proof")
    _real, callbacks = _after_checked(executor, monkeypatch, lambda callback: callback(outside))
    with pytest.raises(OutlineV3ExecutionError, match="scope|verified|authority"):
        executor._global_retention_member_scope(NODE, request)
    with pytest.raises(OutlineV3ExecutionError):
        callbacks[0](target)
    assert observed == []


def test_exception_revokes_proof_and_next_operation_reverifies_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    executor, _base, request, leaves, _authority, observed = _fixture(tmp_path)
    target = executor.registry.get(leaves[0]["semantic_result_ref"]["artifact_id"])
    path = Path(target.path)
    original = path.read_bytes()

    def interrupt(_callback: Any) -> None:
        raise RuntimeError("lineage interpretation interrupted")

    real, callbacks = _after_checked(executor, monkeypatch, interrupt)
    with pytest.raises(RuntimeError, match="lineage interpretation interrupted"):
        executor._global_retention_member_scope(NODE, request)
    with pytest.raises(OutlineV3ExecutionError):
        callbacks[0](target)
    monkeypatch.setattr(executor, "_global_retention_member_scope_checked", real)
    path.write_bytes(original + b" ")
    with pytest.raises((OutlineV3ExecutionError, RegistryError)):
        executor._global_retention_member_scope(NODE, request)
    path.write_bytes(original)
    assert executor._global_retention_member_scope(NODE, request) == _oracle(executor, request)
    assert observed == [] and executor._receipt_ledger.list_receipts() == ()


def test_nested_external_dependency_without_resolver_still_rejects(tmp_path: Path) -> None:
    executor, base, request, leaves, _authority, observed = _fixture(tmp_path)
    child, coverage, _result = _nest(executor, base, request["global_retention_source"], leaves[0], level=1, index=1)
    request["topic_synthesis"] = [child, *leaves[1:]]
    path = tmp_path / "foreign.json"
    path.write_text('{"source": true}', encoding="utf-8")
    foreign = ArtifactRegistry(tmp_path / "foreign-registry.json", "foreign-owner")
    record = foreign.register_file(path=path, artifact_id="foreign:source", artifact_role="source",
                                   artifact_type="lineage_foreign_source", artifact_version="v1", producer="unit")
    ref = ArtifactDependencyRefV2.from_record(record, dependency_kind="external_job").to_dict()
    _rewrite(executor, coverage.artifact_id, lambda row: row["depends_on"].append(ref))
    with pytest.raises((OutlineV3ExecutionError, RegistryError)):
        executor._global_retention_member_scope(NODE, request)
    assert observed == [] and executor._receipt_ledger.list_receipts() == ()
