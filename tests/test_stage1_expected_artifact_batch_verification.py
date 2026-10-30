"""Regression-test draft for the Stage 1 expected-owner batch patch.

Copy this file into the pinned checkout's tests/ directory after integrating
stage1_expected_artifact_batch_verification.patch, then run it from the repo
root. The existing _run_stage1 helper replaces the Reader call with a local
stub; this file does not contact model providers or reuse active workspaces.
"""
from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from services.artifact_registry import (
    ArtifactDependencyRefV2,
    ArtifactRecord,
    ArtifactRegistry,
)
from tests.test_stage1_closure_adversarial import _run_stage1
from validation.closure import (
    _payload_for_record,
    _provider_closure_entry,
    _provider_record_for_stage,
    _terminal_for_stage,
    resolve_current_stage_closure_map,
)


_PAPERS = [
    ("batch-a", "Study A", "A treatment improved the first outcome."),
    ("batch-b", "Study B", "A treatment improved the second outcome."),
    ("batch-c", "Study C", "A treatment improved the third outcome."),
]
_EXPECTED_OWNER_UNTRUSTED = "provider_closure_expected_artifact_dependency_untrusted:analyze:"


def _stage1_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ArtifactRegistry:
    result, _workspace, registry = _run_stage1(
        tmp_path,
        monkeypatch,
        job_id="stage1-expected-owner-batch-test",
        papers=_PAPERS,
    )
    assert result.job_status == "completed", result
    return registry


def _stage1_context(
    registry: ArtifactRegistry,
) -> tuple[int, Any, ArtifactRecord, ArtifactRecord, Mapping[str, Any]]:
    revision, records = registry.capture_record_snapshot()
    current_set = registry.resolve_current_artifact_set()
    # Analyze-only jobs have no Review/Validate CurrentArtifactSet. The
    # production closure API resolves their Stage 1 records without one.
    closure_record = _provider_record_for_stage("analyze", records, current_set)
    terminal_record, terminal_payload = _terminal_for_stage("analyze", records)
    assert closure_record is not None
    assert terminal_record is not None
    assert terminal_payload is not None
    return revision, current_set, closure_record, terminal_record, terminal_payload


def _selected_expected_owners(
    registry: ArtifactRegistry,
    closure_record: ArtifactRecord,
) -> tuple[dict[str, ArtifactRecord], dict[str, list[str]]]:
    payload, _receipt_rows = _payload_for_record(closure_record)
    raw = json.loads(Path(closure_record.path).read_text(encoding="utf-8"))
    closure = dict(payload) if isinstance(payload, Mapping) else {}
    if "expected_calls" not in closure and isinstance(raw, Mapping) and "expected_calls" in raw:
        closure["expected_calls"] = raw["expected_calls"]
    expected_calls = closure.get("expected_calls")
    assert isinstance(expected_calls, list) and expected_calls

    dependency_owners_by_path: dict[str, ArtifactRecord] = {}
    for dependency in closure_record.depends_on:
        owner = registry.get(dependency.artifact_id)
        if owner is not None and owner.status == "ready":
            dependency_owners_by_path[str(Path(owner.path).resolve())] = owner

    selected_owners: dict[str, ArtifactRecord] = {}
    call_ids_by_owner: dict[str, list[str]] = {}
    for item in expected_calls:
        assert isinstance(item, Mapping)
        call_id = str(item.get("call_id") or "")
        artifact_path = str(item.get("artifact_path") or "")
        assert call_id and artifact_path
        owner = dependency_owners_by_path.get(str(Path(artifact_path).resolve()))
        assert owner is not None, f"expected-call owner is not a direct closure dependency: {call_id}"
        selected_owners[owner.artifact_id] = owner
        call_ids_by_owner.setdefault(owner.artifact_id, []).append(call_id)
    return selected_owners, call_ids_by_owner


def _invoke_stage1_entry(
    registry: ArtifactRegistry,
    context: tuple[int, Any, ArtifactRecord, ArtifactRecord, Mapping[str, Any]],
    *,
    external_registry_resolver: Any = None,
) -> tuple[dict[str, Any], list[str]]:
    revision, current_set, closure_record, terminal_record, terminal_payload = context
    return _provider_closure_entry(
        "analyze",
        closure_record,
        terminal_record,
        terminal_payload,
        registry,
        current_set=current_set,
        external_registry_resolver=external_registry_resolver,
        expected_registry_revision=revision,
    )


def _rewrite_dependency_list(
    registry: ArtifactRegistry,
    artifact_id: str,
    dependency_ref: Mapping[str, Any],
) -> ArtifactRegistry:
    registry_path = Path(registry.registry_path)
    document = json.loads(registry_path.read_text(encoding="utf-8"))
    row = next(
        item
        for item in document["artifacts"]
        if isinstance(item, dict) and item.get("artifact_id") == artifact_id
    )
    row.setdefault("depends_on", []).append(dict(dependency_ref))
    registry_path.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
    return ArtifactRegistry(registry.registry_path, registry.job_id)


def _publish_quarantined_marker(
    registry: ArtifactRegistry,
    path: Path,
    artifact_id: str,
) -> None:
    path.write_text("{}\n", encoding="utf-8")
    writer = ArtifactRegistry(registry.registry_path, registry.job_id)
    writer.register_file(
        artifact_id=artifact_id,
        artifact_role="test_marker",
        artifact_type="test_marker",
        artifact_version="v1",
        path=path,
        producer="stage1-closure-batch-test",
        status="quarantined",
    )


def _issues_for_call(blocking: Sequence[str], call_id: str) -> list[str]:
    prefix = _EXPECTED_OWNER_UNTRUSTED + call_id + ":"
    return [issue for issue in blocking if issue.startswith(prefix)]


def test_stage1_expected_owners_use_one_exact_batch_and_keep_closure_complete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _stage1_registry(tmp_path, monkeypatch)
    context = _stage1_context(registry)
    revision, _current_set, closure_record, _terminal_record, _terminal_payload = context
    expected_owners, _call_ids_by_owner = _selected_expected_owners(registry, closure_record)
    assert len(expected_owners) >= 2

    original_batch = registry.verify_ready_artifact_dependency_sets
    original_direct = registry.verify_ready_dependencies
    batch_calls: list[tuple[tuple[ArtifactRecord, ...], dict[str, Any]]] = []
    direct_calls: list[dict[str, Any]] = []

    def batch_spy(
        owners: Sequence[ArtifactRecord],
        **kwargs: Any,
    ) -> dict[str, Exception]:
        owner_tuple = tuple(owners)
        batch_calls.append((owner_tuple, dict(kwargs)))
        return original_batch(owner_tuple, **kwargs)

    def direct_spy(dependencies: Any, **kwargs: Any) -> Any:
        direct_calls.append(dict(kwargs))
        return original_direct(dependencies, **kwargs)

    monkeypatch.setattr(registry, "verify_ready_artifact_dependency_sets", batch_spy)
    monkeypatch.setattr(registry, "verify_ready_dependencies", direct_spy)
    entry, blocking = _invoke_stage1_entry(registry, context)

    assert blocking == []
    assert entry["complete"] is True
    assert len(batch_calls) == 1
    batched_owners, batch_kwargs = batch_calls[0]
    assert {owner.artifact_id for owner in batched_owners} == set(expected_owners)
    assert batch_kwargs["expected_snapshot_revision"] == revision
    assert batch_kwargs["external_registry_resolver"] is None
    # One enclosing provider-closure dependency check remains; the former
    # per-expected-call direct checks are the ones removed by this patch.
    assert len(direct_calls) == 1


def test_stage1_batch_maps_one_bad_owner_to_only_its_expected_call_ids(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _stage1_registry(tmp_path, monkeypatch)
    initial_context = _stage1_context(registry)
    _revision, _current_set, closure_record, _terminal_record, _terminal_payload = initial_context
    expected_owners, calls_by_owner = _selected_expected_owners(registry, closure_record)
    bad_owner_id = sorted(expected_owners)[0]
    missing_dependency = {
        "dependency_kind": "local_job",
        "job_id": registry.job_id,
        "artifact_id": "missing-stage1-batch-dependency",
        "artifact_type": "test_dependency",
        "path": str(tmp_path / "missing-stage1-batch-dependency.json"),
        "content_hash": "f" * 64,
    }
    registry = _rewrite_dependency_list(registry, bad_owner_id, missing_dependency)
    context = _stage1_context(registry)
    _entry, blocking = _invoke_stage1_entry(registry, context)

    affected_call_ids = set(calls_by_owner[bad_owner_id])
    untrusted_issues = [issue for issue in blocking if issue.startswith(_EXPECTED_OWNER_UNTRUSTED)]
    assert {call_id for call_id in affected_call_ids if _issues_for_call(untrusted_issues, call_id)} == affected_call_ids
    unaffected_call_ids = {
        call_id
        for owner_id, owner_call_ids in calls_by_owner.items()
        if owner_id != bad_owner_id
        for call_id in owner_call_ids
    }
    assert all(not _issues_for_call(untrusted_issues, call_id) for call_id in unaffected_call_ids)
    assert len(untrusted_issues) == len(affected_call_ids)


def test_stage1_batch_revision_mismatch_blocks_all_selected_call_ids(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _stage1_registry(tmp_path, monkeypatch)
    stale_context = _stage1_context(registry)
    stale_revision, _current_set, closure_record, _terminal_record, _terminal_payload = stale_context
    expected_owners, calls_by_owner = _selected_expected_owners(registry, closure_record)
    expected_call_ids = {call_id for call_ids in calls_by_owner.values() for call_id in call_ids}

    _publish_quarantined_marker(
        registry,
        tmp_path / "registry-raced-before-batch.json",
        "test-marker:before-batch",
    )
    original_batch = registry.verify_ready_artifact_dependency_sets
    batch_calls: list[tuple[tuple[ArtifactRecord, ...], dict[str, Any]]] = []

    def batch_spy(owners: Sequence[ArtifactRecord], **kwargs: Any) -> dict[str, Exception]:
        owner_tuple = tuple(owners)
        batch_calls.append((owner_tuple, dict(kwargs)))
        return original_batch(owner_tuple, **kwargs)

    monkeypatch.setattr(registry, "verify_ready_artifact_dependency_sets", batch_spy)
    _entry, blocking = _invoke_stage1_entry(registry, stale_context)

    assert len(batch_calls) == 1
    batched_owners, batch_kwargs = batch_calls[0]
    assert {owner.artifact_id for owner in batched_owners} == set(expected_owners)
    assert batch_kwargs["expected_snapshot_revision"] == stale_revision
    untrusted_issues = [issue for issue in blocking if issue.startswith(_EXPECTED_OWNER_UNTRUSTED)]
    assert all(_issues_for_call(untrusted_issues, call_id) for call_id in expected_call_ids)
    assert len(untrusted_issues) == len(expected_call_ids)


def test_stage1_closure_map_final_snapshot_fence_rejects_post_batch_registry_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _stage1_registry(tmp_path, monkeypatch)
    original_batch = registry.verify_ready_artifact_dependency_sets
    batch_count = 0

    def publish_after_batch(owners: Sequence[ArtifactRecord], **kwargs: Any) -> dict[str, Exception]:
        nonlocal batch_count
        result = original_batch(tuple(owners), **kwargs)
        batch_count += 1
        if batch_count == 1:
            _publish_quarantined_marker(
                registry,
                tmp_path / "registry-raced-after-batch.json",
                "test-marker:after-batch",
            )
        return result

    monkeypatch.setattr(registry, "verify_ready_artifact_dependency_sets", publish_after_batch)
    stage_map = resolve_current_stage_closure_map(registry)

    assert batch_count == 1
    assert "current_stage_closure_registry_snapshot_changed" in stage_map.blocking_issues


def test_stage1_expected_owner_keeps_foreign_dependency_on_no_resolver_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _stage1_registry(tmp_path, monkeypatch)
    initial_context = _stage1_context(registry)
    _revision, _current_set, closure_record, _terminal_record, _terminal_payload = initial_context
    expected_owners, calls_by_owner = _selected_expected_owners(registry, closure_record)
    bad_owner_id = sorted(expected_owners)[0]

    external_dir = tmp_path / "foreign-registry"
    external_dir.mkdir()
    external_registry = ArtifactRegistry(str(external_dir / "artifact_registry.json"), "foreign-test-job")
    foreign_path = external_dir / "foreign-dependency.json"
    foreign_path.write_text('{"kind":"test-dependency"}\n', encoding="utf-8")
    foreign_record = external_registry.register_file(
        artifact_id="foreign-test-dependency",
        artifact_role="test_dependency",
        artifact_type="test_dependency",
        artifact_version="v1",
        path=foreign_path,
        producer="stage1-closure-batch-test",
    )
    foreign_ref = ArtifactDependencyRefV2.from_record(
        foreign_record,
        dependency_kind="external_job",
    ).to_dict()
    registry = _rewrite_dependency_list(registry, bad_owner_id, foreign_ref)
    context = _stage1_context(registry)

    resolver_calls: list[str] = []

    def external_resolver(job_id: str) -> ArtifactRegistry | None:
        resolver_calls.append(job_id)
        return external_registry if job_id == external_registry.job_id else None

    original_batch = registry.verify_ready_artifact_dependency_sets
    original_direct = registry.verify_ready_dependencies
    batch_calls: list[dict[str, Any]] = []
    direct_calls: list[dict[str, Any]] = []

    def batch_spy(owners: Sequence[ArtifactRecord], **kwargs: Any) -> dict[str, Exception]:
        batch_calls.append(dict(kwargs))
        return original_batch(tuple(owners), **kwargs)

    def direct_spy(dependencies: Any, **kwargs: Any) -> Any:
        direct_calls.append(dict(kwargs))
        return original_direct(dependencies, **kwargs)

    monkeypatch.setattr(registry, "verify_ready_artifact_dependency_sets", batch_spy)
    monkeypatch.setattr(registry, "verify_ready_dependencies", direct_spy)
    _entry, blocking = _invoke_stage1_entry(
        registry,
        context,
        external_registry_resolver=external_resolver,
    )

    assert len(batch_calls) == 1
    assert batch_calls[0]["external_registry_resolver"] is None
    assert direct_calls and direct_calls[0]["external_registry_resolver"] is external_resolver
    bad_call_ids = calls_by_owner[bad_owner_id]
    untrusted_issues = [issue for issue in blocking if issue.startswith(_EXPECTED_OWNER_UNTRUSTED)]
    assert all(_issues_for_call(untrusted_issues, call_id) for call_id in bad_call_ids)
    assert len(untrusted_issues) == len(bad_call_ids)
    # The resolver is supplied to the enclosing closure check, but the Stage 1
    # expected-owner batch must not ask it to authorize foreign owner inputs.
    assert resolver_calls == []
