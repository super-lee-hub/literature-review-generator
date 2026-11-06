"""Operation-local reducer closure proofs on real Registry-owned fold inputs."""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import socket
from typing import Any

import pytest

from outline.v3_executor import OutlineV3ExecutionError
from services.artifact_registry import ArtifactDependencyRefV2, RegistryError
from tests.test_semantic_final_fold_runtime import NODE, _case, _prepare


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def denied(*_args: Any, **_kwargs: Any) -> None:
        pytest.fail("semantic validation units must not use a transport")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket, "getaddrinfo", denied)


def _rewrite_record(executor: Any, artifact_id: str, change: Any, *, advance: bool = False) -> None:
    path = Path(executor.registry.registry_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    row = next(row for row in payload["artifacts"] if row["artifact_id"] == artifact_id)
    change(row)
    payload["revision"] += int(advance)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _roots(items: list[dict[str, Any]]) -> set[str]:
    return {
        item[key]["artifact_id"]
        for item in items
        for key in ("coverage_ref", "semantic_result_ref")
    }


def _ready(
    tmp_path: Path, *, marker: Any = None, marker_on_shared_node: bool = False,
    ignored_provenance: bool = False,
) -> tuple[Any, list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    executor, items, bases, request = _case(tmp_path)
    if marker is not None:
        artifact_id = (
            "outline-v3:topic_synthesis" if marker_on_shared_node
            else items[0]["coverage_ref"]["artifact_id"]
        )
        executor.registry.update_record(artifact_id, metadata_updates={"typed_marker": marker})
    if ignored_provenance:
        # These are existing, source-bound claim rows. Their extra provenance
        # is in an input claims branch, outside the sealed semantic_result.
        # The original add_prior_synthesis handles claim fields, then skips
        # recursively treating their arbitrary provenance as reducer roots.
        claims = deepcopy(items[0]["semantic_result"]["bridge_claims"])
        assert claims
        for claim in claims:
            claim["diagnostic_provenance"] = {
                "coverage_ref": {"artifact_id": "missing:ignored-coverage", "schema_version": "bad"},
                "semantic_result_ref": {"artifact_id": "missing:ignored-result", "schema_version": "bad"},
            }
        items[0]["claims"] = claims
    _prepare(executor, items, bases, request)
    output = executor._compile_final_fold_retention(NODE, request, {})
    assert executor._receipt_ledger.list_receipts() == ()
    return executor, items, request, output


def _state(executor: Any) -> tuple[Any, Any, Any]:
    return (
        executor._receipt_ledger.list_receipts(),
        dict(executor._pending_replays),
        deepcopy(executor._expected_provider_calls),
    )


def _watch_union(executor: Any, items: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, ...]]:
    expected = _roots(items)
    calls: list[tuple[str, ...]] = []
    real = executor.registry.verify_ready_artifact_closures

    def verified(roots: Any, **kwargs: Any) -> Any:
        roots = tuple(roots)
        ids = tuple(record.artifact_id for record in roots)
        if set(ids) == expected and len(ids) == len(expected):
            calls.append(ids)
        # Singleton entrypoints legitimately delegate here too. They are not
        # counted as the complete operation union and are never bypassed.
        return real(roots, **kwargs)

    monkeypatch.setattr(executor.registry, "verify_ready_artifact_closures", verified)
    return calls


def _after_checked(executor: Any, monkeypatch: pytest.MonkeyPatch, effect: Any) -> Any:
    real = executor._validate_semantic_provider_output_checked

    def checked(node_id: str, request: Any, result: Any, *, verify_closure: Any = None) -> None:
        real(node_id, request, result, verify_closure=verify_closure)
        effect(verify_closure)

    monkeypatch.setattr(executor, "_validate_semantic_provider_output_checked", checked)
    return real


def test_validation_preserves_output_and_uses_two_complete_union_proofs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    executor, items, request, output = _ready(tmp_path)
    calls = _watch_union(executor, items, monkeypatch)
    before = (deepcopy(request), deepcopy(output), _state(executor))
    callbacks = []
    loaded: set[str] = set()
    real_checked = executor._validate_semantic_provider_output_checked

    def checked(node: str, req: Any, result: Any, *, verify_closure: Any = None) -> None:
        assert callable(verify_closure)
        callbacks.append(verify_closure)
        real_checked(node, req, result, verify_closure=verify_closure)

    monkeypatch.setattr(executor, "_validate_semantic_provider_output_checked", checked)
    for name, key in (
        ("_load_semantic_reducer_coverage_ref", "coverage_ref"),
        ("_load_semantic_reducer_result_ref", "semantic_result_ref"),
    ):
        original = getattr(executor, name)

        def loader(value: Any, *, verify_closure: Any = None, original: Any = original, key: str = key) -> Any:
            reference = value.get(key) if isinstance(value, Mapping) else None
            if verify_closure is not None and isinstance(reference, Mapping):
                assert callbacks and verify_closure is callbacks[-1]
                loaded.add(reference["artifact_id"])
            return original(value, verify_closure=verify_closure)

        monkeypatch.setattr(executor, name, loader)
    assert executor._validate_semantic_provider_output(NODE, request, output) is None
    assert len(calls) == 2 and all(set(ids) == _roots(items) for ids in calls)
    assert len(callbacks) == 1 and loaded == _roots(items)
    assert (request, output, _state(executor)) == before


@pytest.mark.parametrize("key,loader_name", [
    ("coverage_ref", "_load_semantic_reducer_coverage_ref"),
    ("semantic_result_ref", "_load_semantic_reducer_result_ref"),
])
def test_independent_loader_keeps_singleton_proof_and_rechecks_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, key: str, loader_name: str,
) -> None:
    executor, items, _request, _output = _ready(tmp_path)
    singleton = executor.registry.verify_ready_artifact_closure
    calls = []

    def verified(record: Any, **kwargs: Any) -> Any:
        calls.append(record.artifact_id)
        return singleton(record, **kwargs)

    monkeypatch.setattr(executor.registry, "verify_ready_artifact_closure", verified)
    loader = getattr(executor, loader_name)
    assert loader(items[0]) is not None
    record = executor.registry.get(items[0][key]["artifact_id"])
    Path(record.path).write_bytes(Path(record.path).read_bytes() + b" ")
    with pytest.raises((OutlineV3ExecutionError, RegistryError)):
        loader(items[0])
    assert calls == [record.artifact_id, record.artifact_id]
    assert executor._receipt_ledger.list_receipts() == ()


@pytest.mark.parametrize("key", ["coverage_ref", "semantic_result_ref"])
@pytest.mark.parametrize("change", ["missing", "foreign_owner", "stale_record"])
def test_initial_root_authority_failure_is_not_hidden(
    tmp_path: Path, key: str, change: str,
) -> None:
    executor, items, request, output = _ready(tmp_path)
    ref = items[0][key]
    if change == "missing":
        ref["artifact_id"] = "missing:semantic-root"
    elif change == "foreign_owner":
        _rewrite_record(executor, ref["artifact_id"], lambda row: row.update(job_id="foreign-owner"))
    else:
        _rewrite_record(executor, ref["artifact_id"], lambda row: row.update(content_hash="f" * 64))
    before = _state(executor)
    with pytest.raises((OutlineV3ExecutionError, RegistryError)):
        executor._validate_semantic_provider_output(NODE, request, output)
    assert _state(executor) == before


@pytest.mark.parametrize("key", ["coverage_ref", "semantic_result_ref"])
def test_initial_root_bytes_are_verified(tmp_path: Path, key: str) -> None:
    executor, items, request, output = _ready(tmp_path)
    record = executor.registry.get(items[0][key]["artifact_id"])
    Path(record.path).write_bytes(Path(record.path).read_bytes() + b" ")
    with pytest.raises((OutlineV3ExecutionError, RegistryError)):
        executor._validate_semantic_provider_output(NODE, request, output)
    assert executor._receipt_ledger.list_receipts() == ()


@pytest.mark.parametrize("change", ["coverage_schema", "membership_hash", "result_hash", "compact_body"])
def test_batch_proof_does_not_replace_loader_reference_or_compact_body_checks(tmp_path: Path, change: str) -> None:
    executor, items, request, output = _ready(tmp_path)
    if change == "coverage_schema":
        items[0]["coverage_ref"]["schema_version"] = "foreign-schema"
    elif change == "membership_hash":
        items[0]["coverage_ref"]["membership_hash"] = "0" * 64
    elif change == "result_hash":
        items[0]["semantic_result_ref"]["provider_result_hash"] = "0" * 64
    else:
        items[0]["semantic_result"]["bridge_claims"][0]["text"] += " altered scientific body"
    with pytest.raises((OutlineV3ExecutionError, RegistryError)):
        executor._validate_semantic_provider_output(NODE, request, output)
    assert executor._receipt_ledger.list_receipts() == ()


def test_conflicting_duplicate_registry_id_is_rejected(tmp_path: Path) -> None:
    executor, items, request, output = _ready(tmp_path)
    path = Path(executor.registry.registry_path)
    registry = json.loads(path.read_text(encoding="utf-8"))
    row = deepcopy(next(row for row in registry["artifacts"] if row["artifact_id"] == items[0]["coverage_ref"]["artifact_id"]))
    row["metadata"]["conflicting_copy"] = "different-authority"
    registry["artifacts"].append(row)
    path.write_text(json.dumps(registry), encoding="utf-8")
    with pytest.raises((OutlineV3ExecutionError, RegistryError)):
        executor._validate_semantic_provider_output(NODE, request, output)


def test_callback_rejects_a_conflicting_same_id_record(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    executor, items, request, output = _ready(tmp_path, marker=False)
    root = executor.registry.get(items[0]["coverage_ref"]["artifact_id"])
    forged = replace(root, metadata={**root.metadata, "typed_marker": 0})
    assert forged.artifact_id == root.artifact_id

    def effect(callback: Any) -> None:
        callback(forged)

    _after_checked(executor, monkeypatch, effect)
    with pytest.raises(OutlineV3ExecutionError, match="scope|authority"):
        executor._validate_semantic_provider_output(NODE, request, output)


@pytest.mark.parametrize("change", [
    "coverage_bytes", "result_bytes", "root_metadata", "root_edges", "root_removed",
    "root_false_zero", "root_int_float", "shared_false_zero", "revision_only", "unrelated_append",
])
def test_success_tail_rejects_late_bytes_or_registry_snapshot_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str,
) -> None:
    shared = change == "shared_false_zero"
    marker = 1 if change == "root_int_float" else False
    executor, items, request, output = _ready(tmp_path, marker=marker, marker_on_shared_node=shared)
    root_id = items[0]["coverage_ref"]["artifact_id"]
    before = _state(executor)

    def mutate(_callback: Any) -> None:
        if change in {"coverage_bytes", "result_bytes"}:
            key = "coverage_ref" if change == "coverage_bytes" else "semantic_result_ref"
            record = executor.registry.get(items[0][key]["artifact_id"])
            Path(record.path).write_bytes(Path(record.path).read_bytes() + b" ")
        elif change == "unrelated_append":
            path = tmp_path / "unrelated.json"
            path.write_text('{"diagnostic": true}', encoding="utf-8")
            executor.registry.register_file(
                path=path, artifact_id="unrelated:diagnostic", artifact_role="diagnostic",
                artifact_type="semantic_validation_diagnostic", artifact_version="v1", producer="unit",
            )
        else:
            path = Path(executor.registry.registry_path)
            payload = json.loads(path.read_text(encoding="utf-8"))
            identity = "outline-v3:topic_synthesis" if shared else root_id
            row = next(row for row in payload["artifacts"] if row["artifact_id"] == identity)
            if change == "root_removed":
                payload["artifacts"].remove(row)
            elif change == "root_edges":
                target = executor.registry.get("outline-v3:topic_synthesis")
                assert target is not None
                row["depends_on"].append(ArtifactDependencyRefV2.from_record(target).to_dict())
            elif change in {"root_false_zero", "shared_false_zero"}:
                row["metadata"]["typed_marker"] = 0
            elif change == "root_int_float":
                row["metadata"]["typed_marker"] = 1.0
            elif change == "root_metadata":
                row["metadata"]["changed"] = True
            else:
                payload["revision"] += 1
            path.write_text(json.dumps(payload), encoding="utf-8")

    _after_checked(executor, monkeypatch, mutate)
    with pytest.raises((OutlineV3ExecutionError, RegistryError)):
        executor._validate_semantic_provider_output(NODE, request, output)
    assert _state(executor) == before


def test_next_validation_cannot_reuse_old_byte_proof(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    executor, items, request, output = _ready(tmp_path)
    calls = _watch_union(executor, items, monkeypatch)
    executor._validate_semantic_provider_output(NODE, request, output)
    record = executor.registry.get(items[0]["semantic_result_ref"]["artifact_id"])
    path = Path(record.path)
    original = path.read_bytes()
    path.write_bytes(original + b" ")
    with pytest.raises((OutlineV3ExecutionError, RegistryError)):
        executor._validate_semantic_provider_output(NODE, request, output)
    path.write_bytes(original)
    executor._validate_semantic_provider_output(NODE, request, output)
    assert len(calls) == 5
    assert executor._receipt_ledger.list_receipts() == ()


def test_exception_does_not_leave_a_validation_proof_for_next_operation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    executor, items, request, output = _ready(tmp_path)
    calls = _watch_union(executor, items, monkeypatch)

    def fail_after_real_validation(_callback: Any) -> None:
        raise RuntimeError("independent validation interruption")

    real = _after_checked(executor, monkeypatch, fail_after_real_validation)
    with pytest.raises(RuntimeError, match="independent validation interruption"):
        executor._validate_semantic_provider_output(NODE, request, output)
    monkeypatch.setattr(executor, "_validate_semantic_provider_output_checked", real)
    record = executor.registry.get(items[0]["coverage_ref"]["artifact_id"])
    path = Path(record.path)
    original = path.read_bytes()
    path.write_bytes(original + b" ")
    with pytest.raises((OutlineV3ExecutionError, RegistryError)):
        executor._validate_semantic_provider_output(NODE, request, output)
    path.write_bytes(original)
    executor._validate_semantic_provider_output(NODE, request, output)
    assert len(calls) == 4
    assert executor._receipt_ledger.list_receipts() == ()


def test_ignored_claim_provenance_does_not_add_reducer_authority(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    executor, items, request, output = _ready(tmp_path, ignored_provenance=True)
    calls = _watch_union(executor, items, monkeypatch)
    before = (deepcopy(request), deepcopy(output), _state(executor))
    executor._validate_semantic_provider_output(NODE, request, output)
    assert len(calls) == 2 and all(set(ids) == _roots(items) for ids in calls)
    assert (request, output, _state(executor)) == before
