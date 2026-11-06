"""A JIT proof may share graph work, never source bytes or late authority."""

from __future__ import annotations

import json
import socket
from dataclasses import replace
from itertools import pairwise
from pathlib import Path
from time import perf_counter

import fitz
import pytest

from runtime.cancellation import CancellationRequestStore
from runtime.job_spec import RuntimeJobSpec, RuntimeSourceSpec
from services.artifact_registry import (
    ArtifactDependencyRefV2,
    ArtifactRegistry,
    RegistryError,
)
from services.job_workspace import (
    JobWorkspace,
    publish_bytes_artifact,
    publish_json_artifact,
)
from services.queue_service import LocalPublicationContext
from services.stage1_source_scope_verification import Stage1SourceScopeVerification


@pytest.fixture(autouse=True)
def no_transport(monkeypatch):
    def denied(*_args, **_kwargs):
        pytest.fail("source verification must not use transport")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket, "getaddrinfo", denied)


def _json(registry, name, payload, dependencies=(), *, artifact_type=None, resolver=None):
    path = Path(registry.registry_path).parent / f"{name}.json"
    return publish_json_artifact(
        LocalPublicationContext(), registry, path, payload,
        artifact_id=name, artifact_role=artifact_type or name,
        artifact_type=artifact_type or name, artifact_version="v1",
        producer="source-scope-test", depends_on=list(dependencies),
        external_registry_resolver=resolver,
    )


def _case(tmp_path, *, extra_kind=None, extra_dependency=None, resolver=None, paper_count=3):
    tmp_path.mkdir(parents=True, exist_ok=True)
    registry = ArtifactRegistry(tmp_path / "artifact_registry.json", "scope-job")
    pdfs = []
    for index in range(paper_count):
        path = tmp_path / f"paper-{index}.pdf"
        with fitz.open() as document:
            document.new_page().insert_text((40, 40), f"Controlled source {index}")
            document.save(path)
        pdfs.append(publish_bytes_artifact(
            LocalPublicationContext(), registry, path, path.read_bytes(),
            artifact_id=f"source_pdf:paper-{index}.pdf", artifact_role="source_pdf",
            artifact_type="source_pdf", artifact_version="v1", producer="source-scope-test",
        ))
    spec = _json(registry, "runtime_job_spec", RuntimeJobSpec(
        project_name="scope", job_id=registry.job_id, action="analyze",
        source=RuntimeSourceSpec(mode="direct", pdf_folder=str(tmp_path)),
    ).to_dict())
    dependencies = [ArtifactDependencyRefV2.from_record(row) for row in pdfs]
    if extra_kind is not None:
        extra = _json(registry, "future-source", {"version": "future"}, artifact_type=extra_kind)
        dependencies.append(ArtifactDependencyRefV2.from_record(extra))
    if extra_dependency is not None:
        dependencies.append(extra_dependency)
    bundle = _json(registry, "source_bundle", {"paper_keys": [f"paper-{i}" for i in range(paper_count)]},
                   tuple(dependencies), resolver=resolver)
    root = _json(registry, "stage1_source_scope", {
        "schema_version": "stage1-source-scope/v1", "job_id": registry.job_id,
        "papers": [{"paper_key": f"paper-{i}", "source_pdf": row.path,
                    "source_pdf_sha256": row.content_hash} for i, row in enumerate(pdfs)],
    }, tuple(ArtifactDependencyRefV2.from_record(row) for row in (bundle, spec)), resolver=resolver)
    return registry, root, (root, bundle, spec, *pdfs)


class _StoreCancelled(RuntimeError):
    pass


def _actual_store_checker(registry, tmp_path):
    workspace = JobWorkspace(str(tmp_path), "source-control", registry.job_id)
    store = CancellationRequestStore(workspace, registry)

    def check():
        if store.is_requested():
            raise _StoreCancelled("actual cancellation marker requested")

    return store, check


def test_real_missing_marker_store_has_bounded_control_reload_count(tmp_path, monkeypatch):
    import services.stage1_source_scope_verification as module

    registry, root, records = _case(tmp_path, paper_count=40)
    store, checker = _actual_store_checker(registry, tmp_path / "control")
    assert not store.path.exists()
    monkeypatch.setattr(module, "monotonic", lambda: 0.0, raising=False)
    reloads = []
    original_reload = registry.reload

    def reload_registry():
        reloads.append(True)
        return original_reload()

    monkeypatch.setattr(registry, "reload", reload_registry)
    with Stage1SourceScopeVerification(registry, root, is_local_route=lambda: True,
                                       check_cancelled=checker) as proof:
        construction_reloads = len(reloads)
        before = len(reloads)
        for _ in range(3):
            with proof.guard(root):
                proof.verify(root)
        guarded_reloads = len(reloads) - before
        print({"actual_store": "CancellationRequestStore", "marker_present": False,
               "participating_records": len(records), "guard_uses": 3,
               "construction_control_reloads": construction_reloads,
               "guard_control_reloads": guarded_reloads})
        assert guarded_reloads <= 8
    assert not store.path.exists()


def test_cancel_created_after_last_periodic_poll_rejects_at_guard_exit(tmp_path, monkeypatch):
    import services.stage1_source_scope_verification as module

    registry, root, _records = _case(tmp_path)
    store, checker = _actual_store_checker(registry, tmp_path / "control")
    monkeypatch.setattr(module, "monotonic", lambda: 0.0, raising=False)
    posts = []
    with (
        pytest.raises(_StoreCancelled),
        Stage1SourceScopeVerification(registry, root, is_local_route=lambda: True,
                                     check_cancelled=checker) as proof,
    ):
        with proof.guard(root):
            proof.verify(root)
            store.request(requested_by="test", reason="cancel immediately before guard exit")
        posts.append("provider")
    assert posts == []
    with pytest.raises(RuntimeError, match="not active"):
        proof.verify(root)


def test_real_cancel_during_hash_wins_before_provider_even_when_poll_interval_not_elapsed(tmp_path, monkeypatch):
    import services.stage1_source_scope_verification as module

    registry, root, _records = _case(tmp_path)
    store, checker = _actual_store_checker(registry, tmp_path / "control")
    monkeypatch.setattr(module, "monotonic", lambda: 0.0, raising=False)
    original_hash = module.file_sha256
    requested = []
    entered = []

    def hashed(path):
        value = original_hash(path)
        if not requested:
            requested.append(True)
            store.request(requested_by="test", reason="cancel during source hash")
        return value

    with (
        pytest.raises(_StoreCancelled),
        Stage1SourceScopeVerification(registry, root, is_local_route=lambda: True,
                                     check_cancelled=checker) as proof,
    ):
        monkeypatch.setattr(module, "file_sha256", hashed)
        with proof.guard(root):
            entered.append("preparation")
    assert requested == [True]
    assert entered == []


def test_real_cancel_does_not_mask_original_preparation_exception_or_leave_permission(tmp_path):
    registry, root, _records = _case(tmp_path)
    store, checker = _actual_store_checker(registry, tmp_path / "control")
    with (
        pytest.raises(ValueError, match="original preparation"),
        Stage1SourceScopeVerification(registry, root, is_local_route=lambda: True,
                                     check_cancelled=checker) as proof,
        proof.guard(root),
    ):
        store.request(requested_by="test", reason="cancel beside preparation error")
        raise ValueError("original preparation")
    with pytest.raises(RuntimeError, match="not active"):
        proof.verify(root)


def test_slow_control_reads_are_time_polled_and_guard_boundaries_are_immediate(tmp_path, monkeypatch):
    import services.stage1_source_scope_verification as module

    registry, root, records = _case(tmp_path)
    clock = [0.0]
    calls = []

    def checker():
        start = clock[0]
        clock[0] += 0.4
        calls.append((start, clock[0]))

    monkeypatch.setattr(module, "monotonic", lambda: clock[0], raising=False)
    original_hash = module.file_sha256

    def hashed(path):
        value = original_hash(path)
        clock[0] += 0.1
        return value

    with Stage1SourceScopeVerification(registry, root, is_local_route=lambda: True,
                                       check_cancelled=checker) as proof:
        monkeypatch.setattr(module, "file_sha256", hashed)
        before = len(calls)
        guard_started = clock[0]
        with proof.guard(root):
            proof.verify(root)
        guarded = calls[before:]
        assert guarded[0][0] == guard_started
        assert guarded[-1][1] == clock[0]
        assert 3 <= len(guarded) <= 8
        assert all(current[0] - previous[1] <= 0.36 for previous, current in pairwise(guarded))
        assert len(guarded) < len(records) * 2


def _rewrite_record(registry, artifact_id, *, advance_revision=False, **changes):
    path = Path(registry.registry_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    row = next(row for row in payload["artifacts"] if row["artifact_id"] == artifact_id)
    row.update(changes)
    if advance_revision:
        payload["revision"] += 1
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_repeated_jit_uses_rehash_every_file_but_recurse_only_at_construction(tmp_path, monkeypatch):
    import services.stage1_source_scope_verification as module

    registry, root, records = _case(tmp_path)
    recursive, hashes, registry_reads = [], [], []
    original_recursive = registry._verify_ready_dependency_closure
    original_read = registry._read_registry_unlocked
    original_hash = module.file_sha256

    def recursive_call(*args, **kwargs):
        recursive.append(args[0].artifact_id)
        return original_recursive(*args, **kwargs)

    def hashed(path):
        hashes.append(str(path))
        return original_hash(path)

    def read_registry():
        registry_reads.append(True)
        return original_read()

    monkeypatch.setattr(registry, "_verify_ready_dependency_closure", recursive_call)
    monkeypatch.setattr(module, "file_sha256", hashed)
    monkeypatch.setattr(registry, "_read_registry_unlocked", read_registry)
    before_full = len(recursive)
    started = perf_counter()
    for _ in range(3):
        for row in records:
            assert module.file_sha256(row.path) == row.content_hash
        registry.verify_ready_artifact_closure(root)
        for row in records:
            assert module.file_sha256(row.path) == row.content_hash
    legacy_seconds = perf_counter() - started
    legacy_recursive = len(recursive) - before_full
    legacy_registry_reads = len(registry_reads)
    assert legacy_recursive >= 3 * len(records)
    assert sorted(hashes) == sorted([row.path for row in records] * 6)
    started = perf_counter()
    with Stage1SourceScopeVerification(registry, root, is_local_route=lambda: True) as proof:
        initial_count = len(recursive)
        construction_recursive = initial_count - legacy_recursive
        construction_registry_reads = len(registry_reads) - legacy_registry_reads
        assert construction_recursive >= len(records)
        hashes.clear()
        initial_reads = len(registry_reads)
        for _ in range(3):
            with proof.guard(root):
                assert proof.verify(root) == root
        scoped_recursive = len(recursive) - initial_count
        print({"legacy_uses": 3, "participating_records": len(records),
               "legacy_recursive": legacy_recursive, "legacy_seconds": round(legacy_seconds, 6),
               "construction_recursive": construction_recursive,
               "scoped_recursive": scoped_recursive, "scoped_fresh_hashes": len(hashes),
               "legacy_registry_reads": legacy_registry_reads,
               "construction_registry_reads": construction_registry_reads,
               "scoped_registry_reads": len(registry_reads) - initial_reads,
               "construction_and_scoped_seconds": round(perf_counter() - started, 6)})
        assert scoped_recursive == 0
        assert len(registry_reads) - initial_reads == 12
        assert sorted(hashes) == sorted([row.path for row in records] * 6)
    assert registry.verify_ready_artifact_closure(root) == root


def test_schema_dispatcher_replacement_falls_back_to_actual_full_gate(tmp_path, monkeypatch):
    from runtime import artifact_validators

    registry, root, _records = _case(tmp_path)
    original = artifact_validators.validate_registered_artifact
    rejected = []

    def rejecting(record, path):
        if record.artifact_id == root.artifact_id:
            rejected.append(record.artifact_id)
            raise ValueError("new source schema rejects")
        return original(record, path)

    with Stage1SourceScopeVerification(registry, root, is_local_route=lambda: True) as proof:
        monkeypatch.setattr(artifact_validators, "validate_registered_artifact", rejecting)
        with pytest.raises(RegistryError, match="new source schema rejects"), proof.guard(root):
            proof.verify(root)
    assert rejected == [root.artifact_id]


def test_instance_ready_verifier_replacement_cannot_borrow_old_schema_proof(tmp_path, monkeypatch):
    registry, root, _records = _case(tmp_path)
    original = registry._verify_ready_artifact
    rejected = []

    def rejecting(record):
        if record.artifact_id == root.artifact_id:
            rejected.append(record.artifact_id)
            raise RegistryError("current source ready verifier rejects")
        return original(record)

    with Stage1SourceScopeVerification(registry, root, is_local_route=lambda: True) as proof:
        monkeypatch.setattr(registry, "_verify_ready_artifact", rejecting)
        with pytest.raises(RegistryError, match="current source ready verifier rejects"), proof.guard(root):
            proof.verify(root)
    assert rejected == [root.artifact_id]


def test_related_type_dispatch_replacement_does_not_reuse_old_schema_proof(tmp_path, monkeypatch):
    from runtime import artifact_validators

    registry, root, _records = _case(tmp_path)
    with Stage1SourceScopeVerification(registry, root, is_local_route=lambda: True) as proof:
        monkeypatch.setattr(artifact_validators, "CURRENT_PRODUCTION_ARTIFACT_TYPES",
                            artifact_validators.CURRENT_PRODUCTION_ARTIFACT_TYPES | {"stage1_source_scope"})
        with pytest.raises(RegistryError), proof.guard(root):
            proof.verify(root)


def test_schema_dispatcher_change_during_guard_rejects_before_provider(tmp_path, monkeypatch):
    from runtime import artifact_validators

    registry, root, _records = _case(tmp_path)
    original = artifact_validators.validate_registered_artifact
    posts = []
    with (
        Stage1SourceScopeVerification(registry, root, is_local_route=lambda: True) as proof,
        pytest.raises(RegistryError, match="schema verifier"),
    ):
        with proof.guard(root):
            proof.verify(root)
            monkeypatch.setattr(artifact_validators, "validate_registered_artifact",
                                lambda *args: original(*args))
        posts.append("provider")
    assert posts == []


@pytest.mark.parametrize("field", ["metadata", "depends_on", "path", "content_hash", "job_id", "status"])
def test_selected_record_drift_rejects_before_provider(tmp_path, field):
    registry, root, records = _case(tmp_path)
    target = records[1]
    changes = {"metadata": {"changed": True}, "depends_on": [], "path": target.path + ".moved",
               "content_hash": "0" * 64, "job_id": "foreign", "status": "dirty"}
    posts = []
    with Stage1SourceScopeVerification(registry, root, is_local_route=lambda: True) as proof:
        _rewrite_record(registry, target.artifact_id, **{field: changes[field]})
        with pytest.raises(RegistryError):
            with proof.guard(root):
                proof.verify(root)
            posts.append("provider")
    assert posts == []


@pytest.mark.parametrize("index", [0, 1, 2, 3, 4, 5])
def test_every_participating_file_is_fresh_at_successful_guard_exit(tmp_path, index):
    registry, root, records = _case(tmp_path)
    posts = []
    with (
        Stage1SourceScopeVerification(registry, root, is_local_route=lambda: True) as proof,
        pytest.raises(RegistryError),
    ):
        with proof.guard(root):
            proof.verify(root)
            path = Path(records[index].path)
            path.write_bytes(path.read_bytes() + b"\nchanged")
        posts.append("provider")
    assert posts == []


def test_same_revision_metadata_change_inside_guard_rejects(tmp_path):
    registry, root, records = _case(tmp_path)
    with (
        Stage1SourceScopeVerification(registry, root, is_local_route=lambda: True) as proof,
        pytest.raises(RegistryError), proof.guard(root),
    ):
        proof.verify(root)
        _rewrite_record(registry, records[2].artifact_id, metadata={"late": "changed"})


def test_ordinary_unrelated_output_append_does_not_invalidate_source_proof(tmp_path):
    registry, root, _records = _case(tmp_path)
    with Stage1SourceScopeVerification(registry, root, is_local_route=lambda: True) as proof:
        with proof.guard(root):
            proof.verify(root)
            _json(registry, "unrelated-output", {"generation": 1}, artifact_type="test_output")
        with proof.guard(root):
            assert proof.verify(root) == root


@pytest.mark.parametrize("boundary", ["entry", "exit"])
@pytest.mark.parametrize("mutation", ["unrelated_append", "metadata", "aba", "same_revision_aba"])
def test_registry_drift_during_each_source_hash_operation_rejects_before_provider(
    tmp_path, monkeypatch, boundary, mutation,
):
    import services.stage1_source_scope_verification as module

    registry, root, records = _case(tmp_path)
    target = records[1]
    original_hash = module.file_sha256
    phase = ["entry"]
    changed = []
    posts = []

    def hashed(path):
        value = original_hash(path)
        if str(path) != root.path or phase[0] != boundary or changed:
            return value
        changed.append(True)
        if mutation == "unrelated_append":
            _json(registry, "during-hash-output", {"generation": 1}, artifact_type="test_output")
        else:
            _rewrite_record(registry, target.artifact_id, metadata={"late": True},
                            advance_revision=mutation == "aba")
            if mutation in {"aba", "same_revision_aba"}:
                _rewrite_record(registry, target.artifact_id, metadata=target.metadata,
                                advance_revision=mutation == "aba")
        return value

    with Stage1SourceScopeVerification(registry, root, is_local_route=lambda: True) as proof:
        monkeypatch.setattr(module, "file_sha256", hashed)
        with pytest.raises(RegistryError):
            with proof.guard(root):
                proof.verify(root)
                phase[0] = "exit"
            posts.append("provider")
    assert changed == [True]
    assert posts == []


def test_default_verify_outside_guard_keeps_original_full_gate(tmp_path, monkeypatch):
    registry, root, _records = _case(tmp_path)
    calls = []
    original = registry.verify_ready_artifact_closure

    def full(*args, **kwargs):
        calls.append(args[0])
        return original(*args, **kwargs)

    monkeypatch.setattr(registry, "verify_ready_artifact_closure", full)
    with Stage1SourceScopeVerification(registry, root, is_local_route=lambda: True) as proof:
        assert proof.verify(root) == root
    assert calls == [root]


@pytest.mark.parametrize("local", [False, "unknown"])
def test_nonlocal_or_unknown_route_uses_original_full_gate(tmp_path, monkeypatch, local):
    registry, root, _records = _case(tmp_path)
    calls = []
    original = registry.verify_ready_artifact_closure

    def full(*args, **kwargs):
        calls.append(args[0])
        return original(*args, **kwargs)

    monkeypatch.setattr(registry, "verify_ready_artifact_closure", full)
    with Stage1SourceScopeVerification(registry, root, is_local_route=lambda: local) as proof:
        for _ in range(2):
            with proof.guard(root):
                proof.verify(root)
    assert calls == [root, root]


def test_route_change_during_optimized_guard_rejects(tmp_path):
    registry, root, _records = _case(tmp_path)
    route = [True]
    with (
        Stage1SourceScopeVerification(registry, root, is_local_route=lambda: route[0]) as proof,
        pytest.raises(RegistryError, match="route"), proof.guard(root),
    ):
        proof.verify(root)
        route[0] = False


def test_initial_full_proof_is_pinned_and_source_failure_never_becomes_cached(tmp_path, monkeypatch):
    registry, root, records = _case(tmp_path)
    original = registry.verify_ready_artifact_closures

    def changed(roots, **kwargs):
        assert "expected_record_snapshot" in kwargs
        _rewrite_record(registry, records[1].artifact_id, metadata={"late": True})
        return original(roots, **kwargs)

    monkeypatch.setattr(registry, "verify_ready_artifact_closures", changed)
    proof = Stage1SourceScopeVerification(registry, root, is_local_route=lambda: True)
    with pytest.raises(RegistryError), proof:
        pytest.fail("stale construction must not enter")
    with pytest.raises(RuntimeError):
        proof.verify(root)


def test_exception_and_cancellation_revoke_guard_without_masking_original(tmp_path):
    registry, root, records = _case(tmp_path)
    cancelled = [False]

    class Cancelled(RuntimeError):
        pass

    def check():
        if cancelled[0]:
            raise Cancelled("cancelled before provider")

    with Stage1SourceScopeVerification(registry, root, is_local_route=lambda: True,
                                       check_cancelled=check) as proof:
        with pytest.raises(ValueError, match="original preparation"), proof.guard(root):
            proof.verify(root)
            raise ValueError("original preparation")
        cancelled[0] = True
        with pytest.raises(Cancelled), proof.guard(root):
            pytest.fail("cancelled operation must not enter")
        cancelled[0] = False
        with proof.guard(root):
            proof.verify(root)
        path = Path(records[-1].path)
        path.write_bytes(path.read_bytes() + b"changed after prior success")
        with pytest.raises(RegistryError), proof.guard(root):
            pytest.fail("previous success must not cache bytes")
    with pytest.raises(RuntimeError):
        proof.verify(root)
    with pytest.raises(RuntimeError):
        proof.__enter__()


def test_wrong_root_or_foreign_root_cannot_borrow_guard(tmp_path):
    registry, root, _records = _case(tmp_path)
    with Stage1SourceScopeVerification(registry, root, is_local_route=lambda: True) as proof:
        with proof.guard(root), pytest.raises(RegistryError):
            proof.verify(replace(root, job_id="foreign"))
        with pytest.raises(RegistryError), proof.guard(replace(root, metadata={"forged": True})):
            pytest.fail("a different root cannot borrow the proof")


def test_external_ancestry_falls_back_and_still_requires_resolver(tmp_path, monkeypatch):
    remote = ArtifactRegistry(tmp_path / "remote" / "artifact_registry.json", "remote")
    remote_leaf = _json(remote, "remote_source", {"source": "remote"}, artifact_type="test_source")
    resolver = lambda job_id: remote if job_id == remote.job_id else None
    registry, external_root, _records = _case(
        tmp_path / "local",
        extra_dependency=ArtifactDependencyRefV2.from_record(remote_leaf, dependency_kind="external_job"),
        resolver=resolver,
    )
    with (
        pytest.raises(RegistryError),
        Stage1SourceScopeVerification(registry, external_root, is_local_route=lambda: True),
    ):
        pytest.fail("external authority without resolver cannot pass")
    calls = []
    original = registry.verify_ready_artifact_closure

    def full(*args, **kwargs):
        calls.append(kwargs.get("external_registry_resolver"))
        return original(*args, **kwargs)

    monkeypatch.setattr(registry, "verify_ready_artifact_closure", full)
    with Stage1SourceScopeVerification(registry, external_root, is_local_route=lambda: True,
                                       external_registry_resolver=resolver) as proof:
        with proof.guard(external_root):
            proof.verify(external_root)
        Path(remote_leaf.path).write_bytes(b"foreign bytes changed")
        with pytest.raises(RegistryError), proof.guard(external_root):
            proof.verify(external_root)
    assert calls == [resolver, resolver]


def test_unknown_local_ancestor_shape_falls_back_to_full_verification(tmp_path, monkeypatch):
    registry, root, _records = _case(tmp_path, extra_kind="future_source_authority")
    calls = []
    original = registry.verify_ready_artifact_closure

    def full(*args, **kwargs):
        calls.append(args[0])
        return original(*args, **kwargs)

    monkeypatch.setattr(registry, "verify_ready_artifact_closure", full)
    with (
        Stage1SourceScopeVerification(registry, root, is_local_route=lambda: True) as proof,
        proof.guard(root),
    ):
        proof.verify(root)
    assert calls == [root]
