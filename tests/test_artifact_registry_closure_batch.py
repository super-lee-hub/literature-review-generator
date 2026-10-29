from __future__ import annotations

import json
from collections import Counter
from dataclasses import replace
from pathlib import Path

import pytest

import services.artifact_registry as artifact_registry_module
from services.artifact_registry import (
    ArtifactClosureBatchError,
    ArtifactDependencyRefV2,
    ArtifactRegistry,
    RegistrySnapshotChanged,
    UnverifiedArtifact,
    UnverifiedDependency,
)


def _register(
    registry: ArtifactRegistry,
    root: Path,
    artifact_id: str,
    *,
    dependencies: tuple[ArtifactDependencyRefV2, ...] = (),
    payload: bytes | None = None,
):
    path = root / f"{artifact_id.replace(':', '_')}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload or json.dumps({"artifact_id": artifact_id}).encode("utf-8"))
    return registry.register_file(
        artifact_id=artifact_id,
        artifact_role="test",
        artifact_type="test_node",
        artifact_version="v1",
        path=path,
        producer="tests",
        depends_on=list(dependencies),
    )


def _shared_roots(tmp_path: Path):
    registry = ArtifactRegistry(tmp_path / "artifact_registry.json", "job-batch")
    leaf = _register(registry, tmp_path, "shared-leaf")
    left_child = _register(
        registry, tmp_path, "left-child", dependencies=(ArtifactDependencyRefV2.from_record(leaf),)
    )
    right_child = _register(
        registry, tmp_path, "right-child", dependencies=(ArtifactDependencyRefV2.from_record(leaf),)
    )
    left = _register(
        registry, tmp_path, "root-left", dependencies=(ArtifactDependencyRefV2.from_record(left_child),)
    )
    right = _register(
        registry, tmp_path, "root-right", dependencies=(ArtifactDependencyRefV2.from_record(right_child),)
    )
    return registry, leaf, left_child, right_child, left, right


def _external_roots(tmp_path: Path):
    remote_dir = tmp_path / "remote"
    local_dir = tmp_path / "local"
    remote_dir.mkdir()
    local_dir.mkdir()
    remote = ArtifactRegistry(remote_dir / "artifact_registry.json", "job-remote")
    remote_leaf = _register(remote, remote_dir, "remote-leaf")
    external_ref = ArtifactDependencyRefV2(
        dependency_kind="external_job",
        job_id=remote.job_id,
        artifact_id=remote_leaf.artifact_id,
        artifact_type=remote_leaf.artifact_type,
        path=remote_leaf.path,
        content_hash=remote_leaf.content_hash,
    )
    resolver = lambda job_id: remote if job_id == remote.job_id else None
    local = ArtifactRegistry(local_dir / "artifact_registry.json", "job-local")
    left = local.register_file(
        artifact_id="local-left",
        artifact_role="test",
        artifact_type="test_node",
        artifact_version="v1",
        path=_register_file(local_dir / "local-left.json", b'{"root":"left"}'),
        producer="tests",
        depends_on=[external_ref],
        external_registry_resolver=resolver,
    )
    right = local.register_file(
        artifact_id="local-right",
        artifact_role="test",
        artifact_type="test_node",
        artifact_version="v1",
        path=_register_file(local_dir / "local-right.json", b'{"root":"right"}'),
        producer="tests",
        depends_on=[external_ref],
        external_registry_resolver=resolver,
    )
    return local, remote, remote_leaf, [left, right], resolver


def _register_file(path: Path, content: bytes) -> Path:
    path.write_bytes(content)
    return path


def test_batch_verifies_union_once_and_returns_ordered_copies(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry, leaf, left_child, right_child, left, right = _shared_roots(tmp_path)
    original = ArtifactRegistry._verify_ready_artifact
    calls: Counter[str] = Counter()

    def counted(cls, record):
        calls[record.artifact_id] += 1
        return original(record)

    monkeypatch.setattr(ArtifactRegistry, "_verify_ready_artifact", classmethod(counted))

    result = registry.verify_ready_artifact_closures([left, right])

    assert [record.artifact_id for record in result] == ["root-left", "root-right"]
    assert result == [left, right]
    assert result[0] is not left
    assert result[1] is not right
    assert calls == Counter({
        leaf.artifact_id: 2,
        left_child.artifact_id: 2,
        right_child.artifact_id: 2,
        left.artifact_id: 2,
        right.artifact_id: 2,
    })


@pytest.mark.parametrize("drift", ["revision", "same_revision_records"])
def test_batch_retries_on_local_revision_or_record_mapping_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    drift: str,
) -> None:
    registry, leaf, _left_child, _right_child, left, right = _shared_roots(tmp_path)
    original_read = registry._read_registry_unlocked
    reads = 0
    injected = False

    def drifting_read():
        nonlocal reads, injected
        revision, artifacts = original_read()
        reads += 1
        if reads == 2 and not injected:
            injected = True
            if drift == "revision":
                return revision + 1, artifacts
            changed = dict(artifacts)
            changed[leaf.artifact_id] = replace(
                changed[leaf.artifact_id],
                metadata={**changed[leaf.artifact_id].metadata, "out_of_band": True},
            )
            return revision, changed
        return revision, artifacts

    monkeypatch.setattr(registry, "_read_registry_unlocked", drifting_read)

    result = registry.verify_ready_artifact_closures([left, right])

    assert [record.artifact_id for record in result] == ["root-left", "root-right"]
    assert injected is True
    assert reads == 4  # initial/final fence for the failed attempt, then a fresh attempt


@pytest.mark.parametrize("drift", ["revision", "same_revision_records"])
def test_batch_retries_all_roots_on_external_registry_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    drift: str,
) -> None:
    local, remote, remote_leaf, roots, _resolver = _external_roots(tmp_path)
    original_read = remote._read_registry_unlocked
    reads = 0
    injected = False

    def drifting_read():
        nonlocal reads, injected
        revision, artifacts = original_read()
        reads += 1
        if reads == 2 and not injected:
            injected = True
            if drift == "revision":
                return revision + 1, artifacts
            changed = dict(artifacts)
            changed[remote_leaf.artifact_id] = replace(
                changed[remote_leaf.artifact_id],
                metadata={**changed[remote_leaf.artifact_id].metadata, "out_of_band": True},
            )
            return revision, changed
        return revision, artifacts

    monkeypatch.setattr(remote, "_read_registry_unlocked", drifting_read)

    result = local.verify_ready_artifact_closures(roots, external_registry_resolver=_resolver)

    assert [record.artifact_id for record in result] == ["local-left", "local-right"]
    assert injected is True
    assert reads == 4  # the shared external snapshot and final fence are both fresh on retry


@pytest.mark.parametrize(
    ("field", "bad_value", "message"),
    [
        ("content_hash", "f" * 64, "dependency declared hash mismatch"),
        ("path", "different-root.json", "dependency path mismatch"),
        ("artifact_type", "other_type", "dependency artifact_type mismatch"),
    ],
)
def test_each_root_is_normalized_even_when_its_node_is_already_memoized(
    tmp_path: Path,
    field: str,
    bad_value: str,
    message: str,
) -> None:
    registry, _leaf, _left_child, _right_child, left, _right = _shared_roots(tmp_path)
    conflicting = replace(ArtifactDependencyRefV2.from_record(left), **{field: bad_value})

    with pytest.raises(ArtifactClosureBatchError) as caught:
        registry.verify_ready_artifact_closures([left, conflicting])

    assert caught.value.root_index == 1
    assert isinstance(caught.value.original_error, UnverifiedDependency)
    assert message in str(caught.value.original_error)


def test_shared_leaf_mutated_after_all_root_walks_fails_final_union_recheck(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry, leaf, _left_child, _right_child, left, right = _shared_roots(tmp_path)
    original_closure = registry._verify_ready_dependency_closure
    root_calls = 0

    def mutate_after_second_root(record, **kwargs):
        nonlocal root_calls
        is_root = not kwargs["dependency_stack"]
        result = original_closure(record, **kwargs)
        if is_root:
            root_calls += 1
            if root_calls == 2:
                Path(leaf.path).write_bytes(b'{"artifact_id":"late mutation"}')
        return result

    monkeypatch.setattr(registry, "_verify_ready_dependency_closure", mutate_after_second_root)

    with pytest.raises(ArtifactClosureBatchError) as caught:
        registry.verify_ready_artifact_closures([left, right])

    assert root_calls == 2
    assert caught.value.root_index == 0
    assert isinstance(caught.value.original_error, UnverifiedArtifact)
    assert "content hash changed" in str(caught.value.original_error)


def test_late_root_one_only_file_failure_maps_to_root_one(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry, _leaf, _left_child, right_child, left, right = _shared_roots(tmp_path)
    original_closure = registry._verify_ready_dependency_closure
    root_calls = 0

    def mutate_after_second_root(record, **kwargs):
        nonlocal root_calls
        is_root = not kwargs["dependency_stack"]
        result = original_closure(record, **kwargs)
        if is_root:
            root_calls += 1
            if root_calls == 2:
                Path(right_child.path).write_bytes(b'{"artifact_id":"root-one late mutation"}')
        return result

    monkeypatch.setattr(registry, "_verify_ready_dependency_closure", mutate_after_second_root)

    with pytest.raises(ArtifactClosureBatchError) as caught:
        registry.verify_ready_artifact_closures([left, right])

    assert root_calls == 2
    assert caught.value.root_index == 1
    assert isinstance(caught.value.original_error, UnverifiedArtifact)
    assert "content hash changed" in str(caught.value.original_error)


@pytest.mark.parametrize("failure_site", ["traversal", "final_fence"])
def test_batch_propagates_unexpected_runtime_errors_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_site: str,
) -> None:
    if failure_site == "traversal":
        registry, _leaf, _left_child, _right_child, left, _right = _shared_roots(tmp_path)

        def unexpected(*_args, **_kwargs):
            raise RuntimeError("unexpected traversal programming error")

        monkeypatch.setattr(registry, "_verify_ready_dependency_closure", unexpected)
        invoke = lambda: registry.verify_ready_artifact_closures([left])
    else:
        registry, _remote, _remote_leaf, roots, resolver = _external_roots(tmp_path)

        def unexpected(_snapshots):
            raise RuntimeError("unexpected final-fence programming error")

        monkeypatch.setattr(registry, "_assert_external_snapshots_unchanged", unexpected)
        invoke = lambda: registry.verify_ready_artifact_closures(
            roots, external_registry_resolver=resolver
        )

    with pytest.raises(RuntimeError, match="unexpected .* programming error") as caught:
        invoke()
    assert type(caught.value) is RuntimeError


def test_batch_local_snapshot_exhaustion_maps_global_fence_to_first_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry, _leaf, _left_child, _right_child, left, right = _shared_roots(tmp_path)
    original_read = registry._read_registry_unlocked
    reads = 0

    def always_changed():
        nonlocal reads
        revision, artifacts = original_read()
        reads += 1
        if reads % 2 == 0:
            return revision + 1, artifacts
        return revision, artifacts

    monkeypatch.setattr(registry, "_read_registry_unlocked", always_changed)

    with pytest.raises(ArtifactClosureBatchError) as caught:
        registry.verify_ready_artifact_closures([left, right])

    assert caught.value.root_index == 0
    assert isinstance(caught.value.original_error, UnverifiedDependency)
    assert str(caught.value.original_error) == "local Registry snapshot changed during closure verification"
    assert reads == artifact_registry_module.DEFAULT_EXTERNAL_SNAPSHOT_RETRY_LIMIT * 2


def test_each_batch_call_uses_fresh_registry_and_artifact_state(tmp_path: Path) -> None:
    registry, leaf, _left_child, _right_child, left, right = _shared_roots(tmp_path)

    first = registry.verify_ready_artifact_closures([left, right])
    assert [record.artifact_id for record in first] == ["root-left", "root-right"]

    Path(leaf.path).write_bytes(b'{"artifact_id":"changed between invocations"}')
    with pytest.raises(ArtifactClosureBatchError) as caught:
        registry.verify_ready_artifact_closures([left, right])
    assert isinstance(caught.value.original_error, UnverifiedDependency)
    assert "dependency content hash changed: shared-leaf" in str(caught.value.original_error)


def test_batch_cycle_is_not_hidden_by_a_prior_root_memo(tmp_path: Path) -> None:
    registry = ArtifactRegistry(tmp_path / "artifact_registry.json", "job-cycle")
    first = _register(registry, tmp_path, "first")
    second = _register(registry, tmp_path, "second")
    registry_path = Path(registry.registry_path)
    payload = json.loads(registry_path.read_text(encoding="utf-8"))
    by_id = {item["artifact_id"]: item for item in payload["artifacts"]}
    by_id["first"]["depends_on"] = [ArtifactDependencyRefV2.from_record(second).to_dict()]
    by_id["second"]["depends_on"] = [ArtifactDependencyRefV2.from_record(first).to_dict()]
    registry_path.write_text(json.dumps(payload), encoding="utf-8")
    registry.reload()

    with pytest.raises(ArtifactClosureBatchError) as caught:
        registry.verify_ready_artifact_closures([first, second])

    assert caught.value.root_index == 0
    assert isinstance(caught.value.original_error, UnverifiedDependency)
    assert "cycle detected" in str(caught.value.original_error)


def _register_lease_manifest(
    registry: ArtifactRegistry,
    tmp_path: Path,
    *,
    name: str,
    target_path: Path,
    target_bytes: bytes,
):
    target_path.write_bytes(target_bytes)
    target = registry.register_file(
        artifact_id=f"lease-target:{name}",
        artifact_role="pointer",
        artifact_type="test_artifact",
        artifact_version="v1",
        path=target_path,
        producer="tests",
    )
    manifest_payload = {
        "artifact_type": "lease_publication_manifest",
        "artifact_version": "v1",
        "job_id": registry.job_id,
        "lease_id": f"lease-{name}",
        "worker_id": f"worker-{name}",
        "lease_generation": 1,
        "fence_token": f"fence-{name}",
        "target_path": str(tmp_path / f"{name}-destination.json"),
        "final_path": target.path,
        "staging_path": str(tmp_path / f"{name}-staging.json"),
        "content_hash": target.content_hash,
        "size_bytes": target_path.stat().st_size,
        "registered_artifact_id": target.artifact_id,
        "registered_artifact_type": target.artifact_type,
        "registered_artifact_version": target.artifact_version,
        "registered_artifact_hash": target.content_hash,
        "created_at": "2026-10-23T00:00:00Z",
    }
    manifest_path = tmp_path / f"lease-manifest-{name}.json"
    manifest_path.write_text(json.dumps(manifest_payload), encoding="utf-8")
    manifest = registry.register_file(
        artifact_id=f"lease-manifest:{name}",
        artifact_role="lease_publication_manifest",
        artifact_type="lease_publication_manifest",
        artifact_version="v1",
        path=manifest_path,
        producer="tests",
        depends_on=[ArtifactDependencyRefV2.from_record(target)],
        metadata={
            "target_artifact_id": target.artifact_id,
            "target_artifact_type": target.artifact_type,
            "target_artifact_version": target.artifact_version,
            "target_artifact_hash": target.content_hash,
            "immutable": True,
        },
    )
    return manifest


def _register_lease_manifest_for_target(
    registry: ArtifactRegistry,
    tmp_path: Path,
    *,
    name: str,
    target,
):
    target_path = Path(target.path)
    manifest_payload = {
        "artifact_type": "lease_publication_manifest",
        "artifact_version": "v1",
        "job_id": registry.job_id,
        "lease_id": f"lease-{name}",
        "worker_id": f"worker-{name}",
        "lease_generation": 1,
        "fence_token": f"fence-{name}",
        "target_path": str(tmp_path / f"{name}-destination.json"),
        "final_path": target.path,
        "staging_path": str(tmp_path / f"{name}-staging.json"),
        "content_hash": target.content_hash,
        "size_bytes": target_path.stat().st_size,
        "registered_artifact_id": target.artifact_id,
        "registered_artifact_type": target.artifact_type,
        "registered_artifact_version": target.artifact_version,
        "registered_artifact_hash": target.content_hash,
        "created_at": "2026-10-23T00:00:00Z",
    }
    manifest_path = tmp_path / f"lease-manifest-{name}.json"
    manifest_path.write_text(json.dumps(manifest_payload), encoding="utf-8")
    return registry.register_file(
        artifact_id=f"lease-manifest:{name}",
        artifact_role="lease_publication_manifest",
        artifact_type="lease_publication_manifest",
        artifact_version="v1",
        path=manifest_path,
        producer="tests",
        depends_on=[ArtifactDependencyRefV2.from_record(target)],
        metadata={
            "target_artifact_id": target.artifact_id,
            "target_artifact_type": target.artifact_type,
            "target_artifact_version": target.artifact_version,
            "target_artifact_hash": target.content_hash,
            "immutable": True,
        },
    )


def _shared_lease_batch(tmp_path: Path):
    registry = ArtifactRegistry(tmp_path / "artifact_registry.json", "job-lease-shared")
    leaf_path = tmp_path / "shared-lease-leaf.json"
    leaf_path.write_bytes(b'{"node":"leaf"}')
    leaf = registry.register_file(
        artifact_id="shared-lease-leaf",
        artifact_role="test",
        artifact_type="test_leaf",
        artifact_version="v1",
        path=leaf_path,
        producer="tests",
    )
    middle = _register(
        registry,
        tmp_path,
        "shared-lease-middle",
        dependencies=(ArtifactDependencyRefV2.from_record(leaf),),
    )
    left = _register(
        registry,
        tmp_path,
        "shared-lease-left",
        dependencies=(ArtifactDependencyRefV2.from_record(middle),),
    )
    right = _register(
        registry,
        tmp_path,
        "shared-lease-right",
        dependencies=(ArtifactDependencyRefV2.from_record(middle),),
    )
    target = _register(
        registry,
        tmp_path,
        "shared-lease-target",
        dependencies=(
            ArtifactDependencyRefV2.from_record(left),
            ArtifactDependencyRefV2.from_record(right),
        ),
    )
    manifests = [
        _register_lease_manifest_for_target(
            registry,
            tmp_path,
            name=f"shared-{index}",
            target=target,
        )
        for index in range(3)
    ]
    return registry, leaf, target, manifests


def _mutate_registry_dependency(
    registry: ArtifactRegistry,
    owner_id: str,
    dependency: ArtifactDependencyRefV2,
) -> None:
    registry_path = Path(registry.registry_path)
    payload = json.loads(registry_path.read_text(encoding="utf-8"))
    owner_row = next(
        row for row in payload["artifacts"] if row["artifact_id"] == owner_id
    )
    owner_row["depends_on"].append(dependency.to_dict())
    payload["revision"] += 1
    registry_path.write_text(json.dumps(payload), encoding="utf-8")
    registry.reload()


def _mutate_registry_owner_dependency(
    registry: ArtifactRegistry,
    owner_id: str,
    update: dict[str, str],
    *,
    index: int = 0,
) -> None:
    registry_path = Path(registry.registry_path)
    payload = json.loads(registry_path.read_text(encoding="utf-8"))
    owner_row = next(
        row for row in payload["artifacts"] if row["artifact_id"] == owner_id
    )
    owner_row["depends_on"][index].update(update)
    payload["revision"] += 1
    registry_path.write_text(json.dumps(payload), encoding="utf-8")
    registry.reload()


def test_complete_ready_lease_batch_shares_verified_dependency_graph(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry, leaf, _target, manifests = _shared_lease_batch(tmp_path)
    before_revision, before_records = registry.capture_record_snapshot()
    before_by_id = {record.artifact_id: record for record in before_records}
    verified_leaf_calls = 0
    normalized_by_owner = {}
    original_verify = registry._verify_ready_artifact
    original_normalize = registry._normalize_dependencies

    def count_real_verify(record):
        nonlocal verified_leaf_calls
        if record.artifact_id == leaf.artifact_id:
            verified_leaf_calls += 1
        return original_verify(record)

    def capture_real_normalization(dependencies, artifacts, **kwargs):
        normalized = original_normalize(dependencies, artifacts, **kwargs)
        owner = kwargs.get("owner_record")
        if owner is not None and owner.artifact_type == "lease_publication_manifest":
            normalized_by_owner[owner.artifact_id] = tuple(normalized)
        return normalized

    monkeypatch.setattr(registry, "_verify_ready_artifact", count_real_verify)
    monkeypatch.setattr(registry, "_normalize_dependencies", capture_real_normalization)

    failures = registry.verify_ready_artifact_dependency_sets(
        [leaf, *manifests],
        complete_ready_set_excluding={"test_node"},
    )

    assert failures == {}
    # One direct owner verification, one recursive lease proof, and one
    # operation-final Registry byte/schema fence remain necessary.
    assert verified_leaf_calls == 3
    assert set(normalized_by_owner) == {manifest.artifact_id for manifest in manifests}
    for manifest in manifests:
        assert normalized_by_owner[manifest.artifact_id] == tuple(manifest.depends_on)
    after_revision, after_records = registry.capture_record_snapshot()
    assert after_revision == before_revision
    assert {record.artifact_id: record for record in after_records} == before_by_id


@pytest.mark.parametrize("mutation", ["shared_leaf", "early_lease_owner"])
def test_complete_ready_lease_batch_rechecks_union_after_late_byte_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    registry, leaf, _target, manifests = _shared_lease_batch(tmp_path)
    snapshot_revision, _snapshot_records = registry.capture_record_snapshot()
    late_owner_id = manifests[-1].artifact_id
    mutation_path = Path(
        leaf.path if mutation == "shared_leaf" else manifests[0].path
    )
    original_normalize = registry._normalize_dependencies
    mutated = False

    def mutate_after_late_owner(dependencies, artifacts, **kwargs):
        nonlocal mutated
        normalized = original_normalize(dependencies, artifacts, **kwargs)
        owner = kwargs.get("owner_record")
        if (
            not mutated
            and owner is not None
            and owner.artifact_id == late_owner_id
        ):
            mutation_path.write_bytes(mutation_path.read_bytes() + b"late byte mutation")
            mutated = True
        return normalized

    monkeypatch.setattr(registry, "_normalize_dependencies", mutate_after_late_owner)
    with pytest.raises(UnverifiedArtifact, match="artifact content hash changed"):
        registry.verify_ready_artifact_dependency_sets(
            [leaf, *manifests],
            complete_ready_set_excluding={"test_node"},
        )

    assert mutated is True
    final_revision, _final_records = registry.capture_record_snapshot()
    assert final_revision == snapshot_revision


def test_complete_ready_lease_batch_keeps_binding_checks_for_warmed_targets(
    tmp_path: Path,
) -> None:
    registry, leaf, target, manifests = _shared_lease_batch(tmp_path)
    malformed = _register_lease_manifest_for_target(
        registry,
        tmp_path,
        name="malformed-shared-target",
        target=target,
    )
    _mutate_registry_owner_dependency(
        registry,
        malformed.artifact_id,
        {"content_hash": "0" * 64},
    )
    malformed = registry.get(malformed.artifact_id)
    assert malformed is not None

    failures = registry.verify_ready_artifact_dependency_sets(
        [leaf, manifests[0], malformed, *manifests[1:]],
        complete_ready_set_excluding={"test_node"},
    )

    assert set(failures) == {malformed.artifact_id}
    failure = failures[malformed.artifact_id]
    # Artifact schema validation is an earlier valid rejection gate than
    # lease-target normalization; either gate must reject the warmed bad ref.
    schema_rejection = (
        isinstance(failure, UnverifiedArtifact)
        and "lease_publication_manifest target dependency is inconsistent" in str(failure)
    )
    binding_rejection = (
        isinstance(failure, UnverifiedDependency)
        and "target binding is invalid" in str(failure)
    )
    assert schema_rejection or binding_rejection, str(failure)


def test_complete_ready_lease_batch_detects_cycle_after_warming_shared_leaf(
    tmp_path: Path,
) -> None:
    registry, leaf, _target, manifests = _shared_lease_batch(tmp_path)
    cycle_target = _register(
        registry,
        tmp_path,
        "warmed-cycle-target",
        dependencies=(ArtifactDependencyRefV2.from_record(leaf),),
    )
    cycle_manifest = _register_lease_manifest_for_target(
        registry,
        tmp_path,
        name="warmed-cycle",
        target=cycle_target,
    )
    _mutate_registry_dependency(
        registry,
        cycle_target.artifact_id,
        ArtifactDependencyRefV2.from_record(cycle_manifest),
    )

    owner_ids = [leaf.artifact_id, manifests[0].artifact_id, cycle_manifest.artifact_id]
    owner_ids.extend(manifest.artifact_id for manifest in manifests[1:])
    owners = [registry.get(artifact_id) for artifact_id in owner_ids]
    assert all(owner is not None for owner in owners)
    failures = registry.verify_ready_artifact_dependency_sets(
        owners,
        complete_ready_set_excluding={"test_node"},
    )

    assert set(failures) == {cycle_manifest.artifact_id}
    assert isinstance(failures[cycle_manifest.artifact_id], UnverifiedDependency)
    assert "cycle" in str(failures[cycle_manifest.artifact_id])


def test_complete_ready_lease_batch_globally_rechecks_late_historical_target_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry, leaf, target, manifests = _shared_lease_batch(tmp_path)
    historical_path = Path(target.path)
    historical_hash = target.content_hash
    historical_bytes = historical_path.read_bytes()
    current_target_path = tmp_path / "shared-lease-current-target.json"
    current_target_bytes = b'{"node":"current target bytes"}'
    current_target_path.write_bytes(current_target_bytes)
    current_target = registry.register_file(
        artifact_id=target.artifact_id,
        artifact_role=target.artifact_role,
        artifact_type=target.artifact_type,
        artifact_version=target.artifact_version,
        path=current_target_path,
        producer=target.producer,
        depends_on=list(target.depends_on),
        metadata=target.metadata,
    )
    assert Path(current_target.path) != historical_path
    assert current_target.content_hash != historical_hash
    snapshot_revision, _snapshot_records = registry.capture_record_snapshot()
    original_normalize = registry._normalize_dependencies
    mutated = False

    def mutate_after_last_owner(dependencies, artifacts, **kwargs):
        nonlocal mutated
        normalized = original_normalize(dependencies, artifacts, **kwargs)
        owner = kwargs.get("owner_record")
        if (
            not mutated
            and owner is not None
            and owner.artifact_id == manifests[-1].artifact_id
        ):
            historical_path.write_bytes(
                historical_path.read_bytes() + b"late historical byte change"
            )
            mutated = True
        return normalized

    monkeypatch.setattr(registry, "_normalize_dependencies", mutate_after_last_owner)

    with pytest.raises(
        UnverifiedDependency,
        match="lease publication historical target content hash changed",
    ):
        registry.verify_ready_artifact_dependency_sets(
            [leaf, *manifests],
            complete_ready_set_excluding={"test_node"},
        )

    assert mutated is True
    assert current_target_path.read_bytes() == current_target_bytes
    assert historical_path.read_bytes() != historical_bytes
    assert current_target.content_hash != historical_hash
    final_revision, _final_records = registry.capture_record_snapshot()
    assert final_revision == snapshot_revision


@pytest.mark.parametrize(
    ("drift", "expected_message"),
    [
        ("revision", "external Registry revision changed during closure verification"),
        ("same_revision_records", "external Registry record mapping changed during closure verification"),
    ],
)
def test_complete_ready_lease_batch_globally_rechecks_recursive_foreign_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    drift: str,
    expected_message: str,
) -> None:
    remote_dir = tmp_path / "remote"
    local_dir = tmp_path / "local"
    remote_dir.mkdir()
    local_dir.mkdir()
    remote = ArtifactRegistry(remote_dir / "artifact_registry.json", "job-lease-remote")
    remote_leaf = _register(remote, remote_dir, "lease-remote-leaf")
    external_ref = ArtifactDependencyRefV2(
        dependency_kind="external_job",
        job_id=remote.job_id,
        artifact_id=remote_leaf.artifact_id,
        artifact_type=remote_leaf.artifact_type,
        path=remote_leaf.path,
        content_hash=remote_leaf.content_hash,
    )
    resolver = lambda job_id: remote if job_id == remote.job_id else None
    local = ArtifactRegistry(local_dir / "artifact_registry.json", "job-lease-local")
    local_target_path = _register_file(local_dir / "lease-target.json", b'{"target":true}')
    local_target = local.register_file(
        artifact_id="lease-target:foreign-closure",
        artifact_role="test",
        artifact_type="test_node",
        artifact_version="v1",
        path=local_target_path,
        producer="tests",
        depends_on=[external_ref],
        external_registry_resolver=resolver,
    )
    manifests = [
        _register_lease_manifest_for_target(
            local,
            local_dir,
            name=f"foreign-{index}",
            target=local_target,
        )
        for index in range(3)
    ]
    original_normalize = local._normalize_dependencies
    mutated = False

    def mutate_remote_after_last_owner(dependencies, artifacts, **kwargs):
        nonlocal mutated
        normalized = original_normalize(dependencies, artifacts, **kwargs)
        owner = kwargs.get("owner_record")
        if (
            not mutated
            and owner is not None
            and owner.artifact_id == manifests[-1].artifact_id
        ):
            if drift == "revision":
                _register(remote, remote_dir, "late-foreign-revision-bump")
            else:
                registry_path = Path(remote.registry_path)
                payload = json.loads(registry_path.read_text(encoding="utf-8"))
                row = next(
                    item for item in payload["artifacts"]
                    if item["artifact_id"] == remote_leaf.artifact_id
                )
                row["metadata"] = {**row["metadata"], "out_of_band": True}
                registry_path.write_text(json.dumps(payload), encoding="utf-8")
                remote.reload()
            mutated = True
        return normalized

    monkeypatch.setattr(local, "_normalize_dependencies", mutate_remote_after_last_owner)

    with pytest.raises(RegistrySnapshotChanged, match=expected_message):
        local.verify_ready_artifact_dependency_sets(
            manifests,
            external_registry_resolver=resolver,
            complete_ready_set_excluding={"test_node"},
        )

    assert mutated is True


def test_complete_ready_failed_lease_sibling_does_not_poison_later_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = ArtifactRegistry(tmp_path / "artifact_registry.json", "job-lease-partial")
    leaf_path = tmp_path / "partial-shared-leaf.json"
    leaf_path.write_bytes(b'{"node":"partial-leaf"}')
    leaf = registry.register_file(
        artifact_id="partial-shared-leaf",
        artifact_role="test",
        artifact_type="test_leaf",
        artifact_version="v1",
        path=leaf_path,
        producer="tests",
    )
    grandchild = _register(registry, tmp_path, "partial-late-grandchild")
    sibling = _register(
        registry,
        tmp_path,
        "partial-late-sibling",
        dependencies=(ArtifactDependencyRefV2.from_record(grandchild),),
    )
    bad_target = _register(
        registry,
        tmp_path,
        "partial-bad-target",
        dependencies=(
            ArtifactDependencyRefV2.from_record(leaf),
            ArtifactDependencyRefV2.from_record(sibling),
        ),
    )
    good_target = _register(
        registry,
        tmp_path,
        "partial-good-target",
        dependencies=(ArtifactDependencyRefV2.from_record(leaf),),
    )
    bad_manifest = _register_lease_manifest_for_target(
        registry,
        tmp_path,
        name="partial-bad",
        target=bad_target,
    )
    good_manifest = _register_lease_manifest_for_target(
        registry,
        tmp_path,
        name="partial-good",
        target=good_target,
    )
    _mutate_registry_owner_dependency(
        registry,
        sibling.artifact_id,
        {"content_hash": "0" * 64},
    )
    bad_manifest = registry.get(bad_manifest.artifact_id)
    good_manifest = registry.get(good_manifest.artifact_id)
    leaf = registry.get(leaf.artifact_id)
    assert bad_manifest is not None and good_manifest is not None and leaf is not None
    leaf_verifications = 0
    bad_lease_cache_states: list[bool] = []
    good_lease_cache_states: list[bool] = []
    original_verify = registry._verify_ready_artifact
    original_closure = registry._verify_ready_dependency_closure

    def count_leaf_verification(record):
        nonlocal leaf_verifications
        if record.artifact_id == leaf.artifact_id:
            leaf_verifications += 1
        return original_verify(record)

    def observe_leaf_cache(record, **kwargs):
        if record.artifact_id == leaf.artifact_id:
            memo_key = (record.job_id, record.artifact_id, id(kwargs["artifacts"]))
            was_cached = memo_key in (kwargs.get("verified_nodes") or {})
            dependency_stack = kwargs.get("dependency_stack") or set()
            if (record.job_id, bad_manifest.artifact_id) in dependency_stack:
                bad_lease_cache_states.append(was_cached)
            if (record.job_id, good_manifest.artifact_id) in dependency_stack:
                good_lease_cache_states.append(was_cached)
        return original_closure(record, **kwargs)

    monkeypatch.setattr(registry, "_verify_ready_artifact", count_leaf_verification)
    monkeypatch.setattr(registry, "_verify_ready_dependency_closure", observe_leaf_cache)

    failures = registry.verify_ready_artifact_dependency_sets(
        [leaf, bad_manifest, good_manifest],
        complete_ready_set_excluding={"test_node"},
    )

    assert set(failures) == {bad_manifest.artifact_id}
    assert isinstance(failures[bad_manifest.artifact_id], UnverifiedDependency)
    assert grandchild.artifact_id in str(failures[bad_manifest.artifact_id])
    assert leaf_verifications <= 3
    assert bad_lease_cache_states == [False]
    assert good_lease_cache_states == [True]


def test_batch_shares_historical_target_conflict_state_across_roots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = ArtifactRegistry(tmp_path / "artifact_registry.json", "job-history")
    historical_path = tmp_path / "mutable-historical-target.json"
    old_manifest = _register_lease_manifest(
        registry,
        tmp_path,
        name="old",
        target_path=historical_path,
        target_bytes=b"old historical bytes",
    )
    new_manifest = _register_lease_manifest(
        registry,
        tmp_path,
        name="new",
        target_path=historical_path,
        target_bytes=b"new historical bytes",
    )
    historical_path.write_bytes(b"old historical bytes")
    original_closure = registry._verify_ready_dependency_closure
    mutated = False

    def mutate_after_first_lease(record, **kwargs):
        nonlocal mutated
        result = original_closure(record, **kwargs)
        if record.artifact_id == old_manifest.artifact_id and not mutated:
            historical_path.write_bytes(b"new historical bytes")
            mutated = True
        return result

    monkeypatch.setattr(registry, "_verify_ready_dependency_closure", mutate_after_first_lease)

    with pytest.raises(ArtifactClosureBatchError) as caught:
        registry.verify_ready_artifact_closures([old_manifest, new_manifest])

    assert mutated is True
    assert caught.value.root_index == 1
    assert isinstance(caught.value.original_error, UnverifiedDependency)
    assert "historical target identity conflicts" in str(caught.value.original_error)


def test_late_historical_target_failure_maps_to_earliest_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = ArtifactRegistry(tmp_path / "artifact_registry.json", "job-history-late")
    historical_path = tmp_path / "mutable-historical-target.json"
    first = _register_lease_manifest(
        registry,
        tmp_path,
        name="first",
        target_path=historical_path,
        target_bytes=b"same historical bytes",
    )
    second = _register_lease_manifest(
        registry,
        tmp_path,
        name="second",
        target_path=historical_path,
        target_bytes=b"same historical bytes",
    )
    original_recheck = ArtifactRegistry._recheck_historical_targets

    def mutate_then_recheck(targets):
        historical_path.write_bytes(b"late historical mutation")
        return original_recheck(targets)

    monkeypatch.setattr(
        ArtifactRegistry,
        "_recheck_historical_targets",
        staticmethod(mutate_then_recheck),
    )

    with pytest.raises(ArtifactClosureBatchError) as caught:
        registry.verify_ready_artifact_closures([first, second])

    assert caught.value.root_index == 0
    assert isinstance(caught.value.original_error, UnverifiedDependency)
    assert "historical target content hash changed" in str(caught.value.original_error)


def test_late_root_one_only_historical_target_failure_maps_to_root_one(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = ArtifactRegistry(tmp_path / "artifact_registry.json", "job-history-root-one")
    unrelated = _register(registry, tmp_path, "unrelated-root")
    historical_path = tmp_path / "root-one-historical-target.json"
    lease_manifest = _register_lease_manifest(
        registry,
        tmp_path,
        name="root-one-only",
        target_path=historical_path,
        target_bytes=b"root one historical bytes",
    )
    original_recheck = ArtifactRegistry._recheck_historical_targets

    def mutate_then_recheck(targets):
        historical_path.write_bytes(b"root one late historical mutation")
        return original_recheck(targets)

    monkeypatch.setattr(
        ArtifactRegistry,
        "_recheck_historical_targets",
        staticmethod(mutate_then_recheck),
    )

    with pytest.raises(ArtifactClosureBatchError) as caught:
        registry.verify_ready_artifact_closures([unrelated, lease_manifest])

    assert caught.value.root_index == 1
    assert isinstance(caught.value.original_error, UnverifiedDependency)
    assert "historical target content hash changed" in str(caught.value.original_error)


def test_external_snapshot_exhaustion_maps_to_earliest_contributing_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    local, _remote, _remote_leaf, roots, resolver = _external_roots(tmp_path)
    checks = 0

    def always_changed(_snapshots):
        nonlocal checks
        checks += 1
        raise RegistrySnapshotChanged("external Registry changed in test")

    monkeypatch.setattr(local, "_assert_external_snapshots_unchanged", always_changed)

    with pytest.raises(ArtifactClosureBatchError) as caught:
        local.verify_ready_artifact_closures(roots, external_registry_resolver=resolver)

    assert caught.value.root_index == 0
    assert type(caught.value.original_error) is UnverifiedDependency
    assert str(caught.value.original_error) == "external Registry snapshot changed during dependency closure verification"
    assert checks == artifact_registry_module.DEFAULT_EXTERNAL_SNAPSHOT_RETRY_LIMIT


def test_external_snapshot_exhaustion_maps_to_root_one_when_root_zero_is_unrelated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    local, _remote, _remote_leaf, roots, resolver = _external_roots(tmp_path)
    unrelated = _register(local, tmp_path, "unrelated-local-root")
    checks = 0

    def always_changed(_snapshots):
        nonlocal checks
        checks += 1
        raise RegistrySnapshotChanged("external Registry changed in test")

    monkeypatch.setattr(local, "_assert_external_snapshots_unchanged", always_changed)

    with pytest.raises(ArtifactClosureBatchError) as caught:
        local.verify_ready_artifact_closures(
            [unrelated, roots[0]], external_registry_resolver=resolver
        )

    assert caught.value.root_index == 1
    assert type(caught.value.original_error) is UnverifiedDependency
    assert str(caught.value.original_error) == "external Registry snapshot changed during dependency closure verification"
    assert checks == artifact_registry_module.DEFAULT_EXTERNAL_SNAPSHOT_RETRY_LIMIT


def test_plural_input_validation_and_legacy_single_root_contract(tmp_path: Path) -> None:
    registry, _leaf, _left_child, _right_child, left, _right = _shared_roots(tmp_path)

    assert registry.verify_ready_artifact_closures([left]) == [registry.verify_ready_artifact_closure(left)]
    single = registry.verify_ready_artifact_closure(left)
    assert single == left
    assert single is not left

    with pytest.raises(ValueError, match="at least one"):
        registry.verify_ready_artifact_closures([])
    with pytest.raises(TypeError, match="unsupported artifact closure root"):
        registry.verify_ready_artifact_closures([object()])
    with pytest.raises(TypeError, match="unsupported artifact closure root"):
        registry.verify_ready_artifact_closure(object())

    foreign = ArtifactDependencyRefV2(
        dependency_kind="external_job",
        job_id="job-foreign",
        artifact_id="foreign-root",
        artifact_type="test_node",
        path=str(tmp_path / "foreign.json"),
        content_hash="a" * 64,
    )
    with pytest.raises(UnverifiedDependency, match="root must be local"):
        registry.verify_ready_artifact_closures([foreign])
    with pytest.raises(UnverifiedDependency, match="root must be local"):
        registry.verify_ready_artifact_closure(foreign)


def test_single_root_preserves_external_retry_exhaustion_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    local, _remote, _remote_leaf, roots, _resolver = _external_roots(tmp_path)
    checks = 0

    def always_changed(_snapshots):
        nonlocal checks
        checks += 1
        raise RegistrySnapshotChanged("external Registry changed in test")

    monkeypatch.setattr(local, "_assert_external_snapshots_unchanged", always_changed)

    with pytest.raises(UnverifiedDependency) as caught:
        local.verify_ready_artifact_closure(roots[0], external_registry_resolver=_resolver)

    assert type(caught.value) is UnverifiedDependency
    assert str(caught.value) == "external Registry snapshot changed during dependency closure verification"
    assert checks == artifact_registry_module.DEFAULT_EXTERNAL_SNAPSHOT_RETRY_LIMIT


def _expected_record_snapshot(registry: ArtifactRegistry):
    revision, records = registry.capture_record_snapshot()
    return revision, {record.artifact_id: record for record in records}


def test_pinned_closure_snapshot_unchanged_preserves_union_counts_and_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry, leaf, left_child, right_child, left, right = _shared_roots(tmp_path)
    expected = _expected_record_snapshot(registry)
    original_verify = ArtifactRegistry._verify_ready_artifact
    calls: Counter[str] = Counter()

    def counted(cls, record):
        calls[record.artifact_id] += 1
        return original_verify(record)

    monkeypatch.setattr(ArtifactRegistry, "_verify_ready_artifact", classmethod(counted))
    result = registry.verify_ready_artifact_closures(
        [left, right], expected_record_snapshot=expected,
    )

    assert result == [left, right]
    assert result[0] is not left and result[1] is not right
    assert calls == Counter({record.artifact_id: 2 for record in (leaf, left_child, right_child, left, right)})
    assert _expected_record_snapshot(registry) == expected


def test_pinned_closure_snapshot_revision_drift_rejects_without_rebasing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry, _leaf, _lc, _rc, left, right = _shared_roots(tmp_path)
    expected = _expected_record_snapshot(registry)
    _register(registry, tmp_path, "late-unrelated-artifact")
    original_read = registry._read_registry_unlocked
    reads = 0
    traversals = 0

    def counted_read():
        nonlocal reads
        reads += 1
        return original_read()

    def forbidden_traversal(*_args, **_kwargs):
        nonlocal traversals
        traversals += 1
        raise AssertionError("a changed pinned snapshot must reject before recursion")

    monkeypatch.setattr(registry, "_read_registry_unlocked", counted_read)
    monkeypatch.setattr(registry, "_verify_ready_dependency_closure", forbidden_traversal)
    with pytest.raises(ArtifactClosureBatchError) as caught:
        registry.verify_ready_artifact_closures([left, right], expected_record_snapshot=expected)

    assert caught.value.root_index == 0
    assert isinstance(caught.value.original_error, RegistrySnapshotChanged)
    assert reads == 1
    assert traversals == 0


@pytest.mark.parametrize("field", ["metadata", "depends_on"])
def test_pinned_closure_snapshot_same_revision_full_record_drift_rejects_before_traversal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
) -> None:
    registry, leaf, _lc, _rc, left, right = _shared_roots(tmp_path)
    expected = _expected_record_snapshot(registry)
    registry_path = Path(registry.registry_path)
    raw = json.loads(registry_path.read_text(encoding="utf-8"))
    row = next(item for item in raw["artifacts"] if item["artifact_id"] == leaf.artifact_id)
    if field == "metadata":
        row["metadata"] = {**row["metadata"], "late_non_hash_identity": "changed"}
    else:
        row["depends_on"] = [ArtifactDependencyRefV2.from_record(left).to_dict()]
    # Keep both the byte hash in the record and Registry revision unchanged:
    # the pin must bind metadata and dependency refs as well as content hashes.
    registry_path.write_text(json.dumps(raw), encoding="utf-8")
    registry.reload()
    original_read = registry._read_registry_unlocked
    reads = 0
    traversals = 0

    def counted_read():
        nonlocal reads
        reads += 1
        return original_read()

    def forbidden_traversal(*_args, **_kwargs):
        nonlocal traversals
        traversals += 1
        raise AssertionError("a same-revision changed map must reject before recursion")

    monkeypatch.setattr(registry, "_read_registry_unlocked", counted_read)
    monkeypatch.setattr(registry, "_verify_ready_dependency_closure", forbidden_traversal)
    with pytest.raises(ArtifactClosureBatchError) as caught:
        registry.verify_ready_artifact_closures([left, right], expected_record_snapshot=expected)

    assert caught.value.root_index == 0
    assert isinstance(caught.value.original_error, RegistrySnapshotChanged)
    assert reads == 1
    assert traversals == 0


def test_pinned_closure_snapshot_prebatch_metadata_aba_cannot_retry_back_to_original(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry, leaf, _lc, _rc, left, right = _shared_roots(tmp_path)
    expected = _expected_record_snapshot(registry)
    original_read = registry._read_registry_unlocked
    reads = 0
    traversals = 0

    def changed_first_read_then_original():
        nonlocal reads
        reads += 1
        revision, records = original_read()
        if reads == 1:
            changed = dict(records)
            changed[leaf.artifact_id] = replace(
                changed[leaf.artifact_id], metadata={**changed[leaf.artifact_id].metadata, "ABA_phase": "B"},
            )
            return revision, changed
        return revision, records  # A retry would wrongly recapture the original A.

    def forbidden_traversal(*_args, **_kwargs):
        nonlocal traversals
        traversals += 1
        raise AssertionError("B was not the captured A; do not traverse or retry")

    monkeypatch.setattr(registry, "_read_registry_unlocked", changed_first_read_then_original)
    monkeypatch.setattr(registry, "_verify_ready_dependency_closure", forbidden_traversal)
    with pytest.raises(ArtifactClosureBatchError) as caught:
        registry.verify_ready_artifact_closures([left, right], expected_record_snapshot=expected)

    assert caught.value.root_index == 0
    assert isinstance(caught.value.original_error, RegistrySnapshotChanged)
    assert reads == 1
    assert traversals == 0


def test_pinned_closure_snapshot_keeps_later_root_byte_failure_ownership(
    tmp_path: Path,
) -> None:
    registry, _leaf, _lc, _rc, left, right = _shared_roots(tmp_path)
    expected = _expected_record_snapshot(registry)
    Path(right.path).write_bytes(b'{"tampered":"right root only"}')

    with pytest.raises(ArtifactClosureBatchError) as caught:
        registry.verify_ready_artifact_closures([left, right], expected_record_snapshot=expected)

    assert caught.value.root_index == 1
    assert isinstance(caught.value.original_error, (UnverifiedArtifact, UnverifiedDependency))
    assert right.artifact_id in str(caught.value.original_error)
