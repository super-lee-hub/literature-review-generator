from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any, Callable

import pytest

from outline.v3_executor import OutlineV3ExecutionError
from services.artifact_registry import ArtifactDependencyRefV2, RegistryError
from tests import test_global_retained_frontier_reduction as global_fixtures
from tests import test_semantic_final_fold_runtime as fold_fixtures
from tests import test_semantic_identifier_runtime as identifier_fixtures
from tests.test_current_runtime_full_e2e import _source_closed_global_content


def _watch_real_batches(monkeypatch: pytest.MonkeyPatch, executor: Any) -> list[tuple[str, ...]]:
    original = executor.registry.verify_ready_artifact_closures
    calls: list[tuple[str, ...]] = []

    def verify(roots: Any, **kwargs: Any) -> Any:
        values = tuple(roots)
        result = original(values, **kwargs)
        calls.append(tuple(root.artifact_id for root in values))
        return result

    monkeypatch.setattr(executor.registry, "verify_ready_artifact_closures", verify)
    return calls


def _watch_real_semantic_validator(
    monkeypatch: pytest.MonkeyPatch,
    executor: Any,
    batches: list[tuple[str, ...]],
) -> list[tuple[int, int]]:
    original = executor._validate_semantic_provider_output
    windows: list[tuple[int, int]] = []

    def validate(node_id: str, request: Any, result: Any) -> Any:
        before = len(batches)
        validated = original(node_id, request, result)
        windows.append((before, len(batches)))
        return validated

    monkeypatch.setattr(executor, "_validate_semantic_provider_output", validate)
    return windows


def _assert_initial_triplet_and_final_union(
    expected: tuple[str, str, str],
    batches: list[tuple[str, ...]],
    windows: list[tuple[int, int]],
) -> None:
    assert len(windows) == 1
    before_validator, after_validator = windows[0]
    initial_batches = batches[:before_validator]
    final_batches = batches[after_validator:]
    assert any(set(expected).issubset(batch) for batch in final_batches), (
        "the post-semantic final union fence must still cover every restore root"
    )
    assert any(len(batch) == 3 and set(batch) == set(expected) for batch in initial_batches), (
        "the three restore roots must share one Registry closure proof before semantic parsing"
    )


def _identifier_restore_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Any, dict[str, Any], Callable[[], None], tuple[str, str, str]]:
    case = identifier_fixtures._case(tmp_path, monkeypatch)
    result = identifier_fixtures._run(case)
    state = deepcopy(case["executor"]._semantic_identifier_outputs[identifier_fixtures.NODE])
    fresh = identifier_fixtures._fresh(case, tmp_path)
    expected = tuple(state[f"{key}_artifact_id"] for key in ("physical", "expanded", "map"))

    def restore() -> None:
        fresh._restore_semantic_identifier_output(identifier_fixtures.NODE, result, state)

    return fresh, state, restore, expected


def _final_fold_restore_fixture(
    tmp_path: Path,
) -> tuple[Any, dict[str, Any], Callable[[], None], tuple[str, str, str], list[dict[str, Any]]]:
    executor, items, bases, request = fold_fixtures._case(tmp_path)
    fold_fixtures._prepare(executor, items, bases, request)
    result = executor._compile_final_fold_retention(fold_fixtures.NODE, request, {})
    state = deepcopy(executor._final_fold_results[fold_fixtures.NODE])
    parent = {
        "execution_mode": "source_closed_retention",
        "provider_output": result,
        "semantic_retention": {**state, "provider_invoked": False},
    }
    fresh = fold_fixtures._executor(tmp_path, stability_mode="off")
    expected = tuple(state[key] for key in ("artifact_id", "catalog_artifact_id", "request_artifact_id"))

    def restore() -> None:
        fresh._restore_local_final_fold(fold_fixtures.NODE, parent)

    return fresh, state, restore, expected, items


def test_identifier_restore_batches_roots_before_semantic_validation_and_keeps_final_union(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fresh, state, restore, expected = _identifier_restore_fixture(tmp_path, monkeypatch)
    batches = _watch_real_batches(monkeypatch, fresh)
    windows = _watch_real_semantic_validator(monkeypatch, fresh, batches)

    restore()

    _assert_initial_triplet_and_final_union(expected, batches, windows)
    assert fresh._semantic_identifier_outputs[identifier_fixtures.NODE] == state


def test_local_final_fold_restore_batches_roots_before_semantic_validation_and_keeps_final_union(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fresh, state, restore, expected, _items = _final_fold_restore_fixture(tmp_path)
    batches = _watch_real_batches(monkeypatch, fresh)
    windows = _watch_real_semantic_validator(monkeypatch, fresh, batches)

    restore()

    _assert_initial_triplet_and_final_union(expected, batches, windows)
    assert fresh._final_fold_results[fold_fixtures.NODE] == {**state, "provider_invoked": False}


def _arm_transient_root_read(
    monkeypatch: pytest.MonkeyPatch,
    executor: Any,
    record: Any,
) -> dict[str, Any]:
    original_verify = executor.registry.verify_ready_artifact_closures
    proof_returned = False
    triggered = False
    path = Path(record.path)
    original_bytes = path.read_bytes()
    original_read_text = Path.read_text
    original_read_bytes = Path.read_bytes

    def verify(roots: Any, **kwargs: Any) -> Any:
        nonlocal proof_returned
        values = tuple(roots)
        verified = original_verify(values, **kwargs)
        if any(root.artifact_id == record.artifact_id for root in values):
            proof_returned = True
        return verified

    def wrap_reader(original: Callable[..., Any]) -> Callable[..., Any]:
        def read(selected_path: Path, *args: Any, **kwargs: Any) -> Any:
            nonlocal triggered
            if proof_returned and not triggered and selected_path == path:
                transient_bytes = b" " + original_bytes
                selected_path.write_bytes(transient_bytes)
                try:
                    value = original(selected_path, *args, **kwargs)
                finally:
                    selected_path.write_bytes(original_bytes)
                triggered = True
                return value
            return original(selected_path, *args, **kwargs)

        return read

    monkeypatch.setattr(executor.registry, "verify_ready_artifact_closures", verify)
    monkeypatch.setattr(Path, "read_text", wrap_reader(original_read_text))
    monkeypatch.setattr(Path, "read_bytes", wrap_reader(original_read_bytes))
    return {
        "path": path,
        "original_bytes": original_bytes,
        "was_proved": lambda: proof_returned,
        "was_triggered": lambda: triggered,
    }


def test_identifier_restore_rejects_root_bytes_changed_after_real_closure_proof(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fresh, state, restore, _expected = _identifier_restore_fixture(tmp_path, monkeypatch)
    physical = fresh.registry.get(state["physical_artifact_id"])
    mutation = _arm_transient_root_read(monkeypatch, fresh, physical)

    rejected = False
    try:
        restore()
    except (OutlineV3ExecutionError, RegistryError):
        rejected = True

    assert mutation["was_proved"]()
    assert mutation["was_triggered"]()
    assert mutation["path"].read_bytes() == mutation["original_bytes"]
    assert rejected, "restore accepted root bytes that differed from the bytes it had proved"
    assert identifier_fixtures.NODE not in fresh._semantic_identifier_outputs


def test_local_final_fold_restore_rejects_root_bytes_changed_after_real_closure_proof(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fresh, state, restore, _expected, _items = _final_fold_restore_fixture(tmp_path)
    aggregate = fresh.registry.get(state["artifact_id"])
    mutation = _arm_transient_root_read(monkeypatch, fresh, aggregate)

    rejected = False
    try:
        restore()
    except (OutlineV3ExecutionError, RegistryError):
        rejected = True

    assert mutation["was_proved"]()
    assert mutation["was_triggered"]()
    assert mutation["path"].read_bytes() == mutation["original_bytes"]
    assert rejected, "restore accepted aggregate bytes that differed from the bytes it had proved"
    assert fold_fixtures.NODE not in fresh._final_fold_results


def test_local_final_fold_restore_rechecks_descendant_after_real_semantic_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fresh, state, restore, _expected, items = _final_fold_restore_fixture(tmp_path)
    descendant_id = items[0]["semantic_result_ref"]["artifact_id"]
    descendant = fresh.registry.get(descendant_id)
    catalog = fresh.registry.get(state["catalog_artifact_id"])
    assert ArtifactDependencyRefV2.from_record(descendant) in catalog.depends_on
    original_bytes = Path(descendant.path).read_bytes()
    validator = fresh._validate_semantic_provider_output
    validator_returned: list[bool] = []

    def validate(node_id: str, request: Any, result: Any) -> Any:
        validated = validator(node_id, request, result)
        validator_returned.append(True)
        Path(descendant.path).write_bytes(b" " + original_bytes)
        return validated

    monkeypatch.setattr(fresh, "_validate_semantic_provider_output", validate)

    with pytest.raises((OutlineV3ExecutionError, RegistryError)):
        restore()

    assert validator_returned == [True]
    assert Path(descendant.path).read_bytes() == b" " + original_bytes
    assert fold_fixtures.NODE not in fresh._final_fold_results


def _global_frontier_child_fixture(
    tmp_path: Path,
) -> tuple[Any, str, dict[str, Any], dict[str, Any], list[Any]]:
    executor, request, _parent, _items, observed = global_fixtures._case(
        tmp_path, large=False, count=2, deferred_from=1,
    )
    marker, original, _authority = executor._global_retention_authority(request)
    child = {
        **request,
        "cross_group_comparison": {},
        "global_retention_source": marker,
        "topic_synthesis": original["topic_synthesis"][:1],
        "hierarchy": {"level": "bounded_semantic_reduction"},
    }
    result = _source_closed_global_content(child)
    node_id = f"{global_fixtures.NODE}:reduce:1:1"
    return executor, node_id, child, result, observed


def _watch_global_member_scope(
    monkeypatch: pytest.MonkeyPatch,
    executor: Any,
) -> list[str]:
    original = executor._global_retention_member_scope
    calls: list[str] = []

    def prove(node_id: str, request: Any, *args: Any, **kwargs: Any) -> Any:
        result = original(node_id, request, *args, **kwargs)
        calls.append(node_id)
        return result

    monkeypatch.setattr(executor, "_global_retention_member_scope", prove)
    return calls


def test_direct_global_child_validator_proves_retained_scope_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executor, node_id, child, result, observed = _global_frontier_child_fixture(tmp_path)
    calls = _watch_global_member_scope(monkeypatch, executor)

    executor._validate_semantic_provider_output(node_id, child, result)

    assert calls == [node_id]
    assert observed == []


@pytest.mark.parametrize("mutation", ["bytes", "dependency"])
def test_direct_global_child_validator_rechecks_retained_scope_after_late_source_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    executor, node_id, child, result, observed = _global_frontier_child_fixture(tmp_path)
    calls = _watch_global_member_scope(monkeypatch, executor)
    target = executor.registry.get(child["topic_synthesis"][0]["coverage_ref"]["artifact_id"])
    changed = False

    class LateChange(dict):
        def items(self):
            nonlocal changed
            if not changed:
                if mutation == "bytes":
                    Path(target.path).write_text("{}", encoding="utf-8")
                else:
                    executor.registry.update_record(target.artifact_id, depends_on=[])
                changed = True
            return super().items()

    with pytest.raises((OutlineV3ExecutionError, RegistryError)):
        executor._validate_semantic_provider_output(node_id, child, LateChange(result))

    assert changed
    assert calls == [node_id]
    assert observed == []


def _global_authority_fixture(tmp_path: Path) -> tuple[Any, dict[str, Any], Any, list[Any]]:
    executor, request, _parent, _items, observed = global_fixtures._case(
        tmp_path, large=False, count=2, deferred_from=1,
    )
    state = executor._final_fold_results[fold_fixtures.NODE]
    source_record = executor.registry.get(str(state["request_artifact_id"]))
    assert source_record is not None
    return executor, request, source_record, observed


def test_global_retention_authority_batches_exact_four_roots_before_source_parse(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executor, request, source_record, observed = _global_authority_fixture(tmp_path)
    state = executor._final_fold_results[fold_fixtures.NODE]
    cross = executor._payloads["cross_group_comparison"]["provider_output"]
    sidecar = cross["fragment_aggregation"]
    expected = (
        str(state["artifact_id"]),
        str(state["request_artifact_id"]),
        str(sidecar["plan_scope_ref"]["artifact_id"]),
        str(sidecar["source_scope_ref"]["artifact_id"]),
    )
    batches = _watch_real_batches(monkeypatch, executor)
    source_reads: list[tuple[tuple[str, ...], ...]] = []
    source_path = Path(source_record.path)

    def observe_reader(original: Callable[..., Any]) -> Callable[..., Any]:
        def read(path: Path, *args: Any, **kwargs: Any) -> Any:
            # Registry validators read the same file during their proof.
            # Observe only a consumer read after a source-root proof returned.
            if path == source_path and any(source_record.artifact_id in batch for batch in batches):
                source_reads.append(tuple(batches))
            return original(path, *args, **kwargs)
        return read

    monkeypatch.setattr(Path, "read_text", observe_reader(Path.read_text))
    monkeypatch.setattr(Path, "read_bytes", observe_reader(Path.read_bytes))
    _marker, original, authority = executor._global_retention_authority(request)

    assert tuple(record.artifact_id for record in authority) == expected
    assert source_reads
    assert expected in source_reads[0]
    assert original["topic_synthesis"]
    assert observed == []


def test_global_retention_authority_rejects_source_bytes_changed_after_real_closure_proof(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executor, request, source_record, observed = _global_authority_fixture(tmp_path)
    mutation = _arm_transient_root_read(monkeypatch, executor, source_record)
    rejected = False
    try:
        executor._global_retention_authority(request)
    except (OutlineV3ExecutionError, RegistryError):
        rejected = True

    assert mutation["was_proved"]()
    assert mutation["was_triggered"]()
    assert mutation["path"].read_bytes() == mutation["original_bytes"]
    assert rejected, "GLOBAL authority parsed source bytes that differed from the bytes it had proved"
    assert observed == []
