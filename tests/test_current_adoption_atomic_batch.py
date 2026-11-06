"""Pure temporary-Registry coverage for one atomic current-adoption batch."""

from __future__ import annotations

import hashlib
import inspect
import json
import socket
from collections import Counter
from dataclasses import asdict, replace
from pathlib import Path

import pytest

import services.artifact_registry as registry_module
from outline.adoption_transaction import (
    ADOPTION_POINTER_ARTIFACT_ID,
    current_adoption_record,
)
from outline.v3_artifacts import AdoptedOutline
from services.artifact_registry import ArtifactDependencyRefV2, ArtifactRegistry
from tests.test_artifact_registry_closure_batch import (
    _external_roots,
    _register,
    _register_lease_manifest,
)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def denied(*_args, **_kwargs):
        pytest.fail("pure Registry case attempted a socket connection")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)


def _case(tmp_path: Path, *, registry=None, dependencies=(), resolver=None):
    registry = registry or ArtifactRegistry(tmp_path / "artifact_registry.json", "adoption-batch-job")
    leaf = _register(registry, tmp_path, "source")
    target_payload = AdoptedOutline(
        job_id=registry.job_id,
        payload={
            "status": "succeeded", "adoption_id": "adopted", "adoption_identity": "adopted",
            "current_pointer_artifact_id": ADOPTION_POINTER_ARTIFACT_ID, "current_pointer_role": "current",
            "actor": "tests", "reason": "bounded pure Registry case", "expected_hash": "0" * 64,
            "final_outline_hash": "0" * 64, "coverage_audit_hash": "0" * 64,
            "stability_audit_hash": "0" * 64, "stage_health_hash": "0" * 64,
            "provider_receipt_closure_hash": "0" * 64, "final_outline": {"sections": []},
        },
    ).to_dict()
    target_path = tmp_path / "adopted.json"
    target_path.write_text(json.dumps(target_payload), encoding="utf-8")
    target = registry.register_file(
        artifact_id="adopted", artifact_role="adopted_outline", artifact_type="adopted_outline",
        artifact_version="v3", path=target_path, producer="tests",
        depends_on=[ArtifactDependencyRefV2.from_record(leaf), *dependencies],
        external_registry_resolver=resolver,
    )
    pointer_path = tmp_path / "pointer.json"
    pointer_path.write_text(json.dumps({
        "artifact_type": "outline_adoption_pointer", "artifact_version": "v1", "job_id": registry.job_id,
        "role": "current", "current_adoption_artifact_id": target.artifact_id,
        "current_adoption_hash": target.content_hash, "adoption_identity": target.artifact_id,
        "updated_at": "2026-10-25T00:00:00Z",
    }), encoding="utf-8")
    pointer = registry.register_file(
        artifact_id=ADOPTION_POINTER_ARTIFACT_ID, artifact_role="outline_v3_adoption_current",
        artifact_type="outline_adoption_pointer", artifact_version="v1", path=pointer_path,
        producer="tests", depends_on=[ArtifactDependencyRefV2.from_record(target)],
        external_registry_resolver=resolver,
    )
    return registry, leaf, target, pointer


def _rewrite(registry, change):
    path = Path(registry.registry_path)
    doc = json.loads(path.read_text(encoding="utf-8"))
    rows = {row["artifact_id"]: row for row in doc["artifacts"]}
    change(rows)
    doc["artifacts"] = list(rows.values())
    path.write_text(json.dumps(doc), encoding="utf-8")
    registry.reload()


def test_current_adoption_uses_one_batch_and_shared_nodes(tmp_path, monkeypatch):
    registry, leaf, target, pointer = _case(tmp_path)
    batches = []
    checks = Counter()
    hash_checks = Counter()
    original_batch = registry.verify_ready_artifact_closures
    original_check = ArtifactRegistry._verify_ready_artifact
    original_hash = registry_module.file_sha256
    path_ids = {str(Path(row.path).resolve()): row.artifact_id for row in (leaf, target, pointer)}

    def batch(roots, **kwargs):
        roots = tuple(roots)
        batches.append(tuple(root.artifact_id for root in roots))
        return original_batch(roots, **kwargs)

    def check(cls, record):
        checks[record.artifact_id] += 1
        return original_check(record)

    def file_hash(path):
        hash_checks[path_ids[str(Path(path).resolve())]] += 1
        return original_hash(path)

    monkeypatch.setattr(registry, "verify_ready_artifact_closures", batch)
    monkeypatch.setattr(ArtifactRegistry, "_verify_ready_artifact", classmethod(check))
    monkeypatch.setattr(registry_module, "file_sha256", file_hash)
    result = current_adoption_record(registry)
    assert result is not None and result.artifact_id == target.artifact_id
    print(json.dumps({"batches": batches, "artifact_checks": dict(checks), "file_hashes": dict(hash_checks)}))
    assert batches == [(pointer.artifact_id, target.artifact_id)], (batches, dict(checks))
    # One successful walk plus the existing independent late-byte fence.
    assert checks == Counter({leaf.artifact_id: 2, target.artifact_id: 2, pointer.artifact_id: 2})
    assert 2 <= hash_checks[leaf.artifact_id] <= 3


def test_current_adoption_does_not_select_foreign_target_from_second_read_aba(tmp_path, monkeypatch):
    registry, leaf, target, pointer = _case(tmp_path)
    foreign_path = tmp_path / "foreign-adopted.json"
    foreign_path.write_bytes(Path(target.path).read_bytes())
    foreign = registry.register_file(
        artifact_id="foreign-adopted", artifact_role="adopted_outline", artifact_type="adopted_outline",
        artifact_version="v3", path=foreign_path, producer="tests",
        depends_on=[ArtifactDependencyRefV2.from_record(leaf)],
    )
    original_bytes = Path(pointer.path).read_bytes()
    switched = json.loads(original_bytes.decode("utf-8"))
    switched.update(current_adoption_artifact_id=foreign.artifact_id, current_adoption_hash=foreign.content_hash, adoption_identity=foreign.artifact_id)
    foreign_bytes = json.dumps(switched).encode("utf-8")
    read_text = Path.read_text

    def read(path, *args, **kwargs):
        frame = inspect.currentframe()
        caller = frame.f_back.f_code.co_name if frame and frame.f_back else ""
        if path == Path(pointer.path) and caller == "_load_json":
            path.write_bytes(foreign_bytes)
            try:
                return read_text(path, *args, **kwargs)
            finally:
                path.write_bytes(original_bytes)
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    result = current_adoption_record(registry)
    assert result is not None and result.artifact_id == target.artifact_id
    assert Path(pointer.path).read_bytes() == original_bytes


@pytest.mark.parametrize("defect", [
    "pointer-missing", "target-missing", "pointer-status", "target-status",
    "pointer-empty-hash", "target-empty-hash", "pointer-bytes", "target-bytes",
    "pointer-schema", "pointer-type", "target-type", "target-version", "target-schema",
    "selected-hash", "selected-identity", "pointer-edge", "target-edge",
])
def test_current_adoption_preserves_failure_semantics(tmp_path, defect):
    registry, _leaf, target, pointer = _case(tmp_path)

    def mutate(rows):
        if defect.endswith("-missing"):
            rows.pop(pointer.artifact_id if defect.startswith("pointer") else target.artifact_id)
        elif defect.endswith("-status"):
            rows[pointer.artifact_id if defect.startswith("pointer") else target.artifact_id]["status"] = "failed"
        elif defect.endswith("-empty-hash"):
            rows[pointer.artifact_id if defect.startswith("pointer") else target.artifact_id]["content_hash"] = ""
        elif defect.endswith("-bytes"):
            Path(pointer.path if defect.startswith("pointer") else target.path).write_bytes(b"changed")
        elif defect in {"target-type", "target-version"}:
            rows[target.artifact_id]["artifact_type" if defect == "target-type" else "artifact_version"] = "test_node" if defect == "target-type" else "v1"
        elif defect in {"pointer-edge", "target-edge"}:
            rows[pointer.artifact_id if defect == "pointer-edge" else target.artifact_id]["depends_on"][0]["content_hash"] = "f" * 64
        else:
            payload = json.loads(Path(pointer.path).read_text(encoding="utf-8"))
            if defect == "pointer-schema":
                payload.pop("role")
            elif defect == "pointer-type":
                payload["artifact_type"] = "wrong-type"
            elif defect == "selected-hash":
                payload["current_adoption_hash"] = "f" * 64
            elif defect == "selected-identity":
                payload["adoption_identity"] = "foreign"
            elif defect == "target-schema":
                outer = json.loads(Path(target.path).read_text(encoding="utf-8"))
                outer["payload"].pop("status")
                invalid = AdoptedOutline.from_dict(outer).to_dict()
                Path(target.path).write_text(json.dumps(invalid), encoding="utf-8")
                digest = hashlib.sha256(Path(target.path).read_bytes()).hexdigest()
                rows[target.artifact_id]["content_hash"] = digest
                rows[pointer.artifact_id]["depends_on"][0]["content_hash"] = digest
                payload["current_adoption_hash"] = digest
            Path(pointer.path).write_text(json.dumps(payload), encoding="utf-8")
            rows[pointer.artifact_id]["content_hash"] = hashlib.sha256(Path(pointer.path).read_bytes()).hexdigest()

    _rewrite(registry, mutate)
    assert current_adoption_record(registry) is None


def test_current_adoption_rejects_unverified_buffer_before_target_lookup(tmp_path, monkeypatch):
    registry, _leaf, target, pointer = _case(tmp_path)
    foreign = json.loads(Path(pointer.path).read_text(encoding="utf-8"))
    foreign["current_adoption_artifact_id"] = "foreign"
    foreign_buffer = json.dumps(foreign).encode("utf-8")
    read_bytes = Path.read_bytes
    get = registry.get
    looked_up = []

    def read(path):
        return foreign_buffer if path == Path(pointer.path) else read_bytes(path)

    def lookup(artifact_id):
        looked_up.append(artifact_id)
        return get(artifact_id)

    monkeypatch.setattr(Path, "read_bytes", read)
    monkeypatch.setattr(registry, "get", lookup)
    assert current_adoption_record(registry) is None
    assert looked_up == [pointer.artifact_id]
    assert target.artifact_id not in looked_up


@pytest.mark.parametrize("mutated", ["pointer", "target", "source"])
def test_current_adoption_rejects_late_bytes(tmp_path, monkeypatch, mutated):
    registry, leaf, target, pointer = _case(tmp_path)
    original = registry._verify_ready_dependency_closure
    changed = False

    def verify(record, **kwargs):
        nonlocal changed
        result = original(record, **kwargs)
        if record.artifact_id == pointer.artifact_id and not changed:
            chosen = {"pointer": pointer, "target": target, "source": leaf}[mutated]
            Path(chosen.path).write_bytes(b"late source mutation")
            changed = True
        return result

    monkeypatch.setattr(registry, "_verify_ready_dependency_closure", verify)
    assert current_adoption_record(registry) is None
    assert changed


def test_current_adoption_allows_unrelated_ready_append_and_refreshes_next_call(tmp_path, monkeypatch):
    registry, leaf, target, _pointer = _case(tmp_path)
    original = registry._read_registry_unlocked
    reads = 0

    def read():
        nonlocal reads
        reads += 1
        if reads == 2:
            path = tmp_path / "unrelated.json"
            path.write_bytes(b'{"unrelated":true}')
            unrelated = replace(leaf, artifact_id="unrelated", path=str(path), content_hash=hashlib.sha256(path.read_bytes()).hexdigest(), depends_on=[])
            doc_path = Path(registry.registry_path)
            doc = json.loads(doc_path.read_text(encoding="utf-8"))
            doc["artifacts"].append(asdict(unrelated))
            doc["revision"] += 1
            doc_path.write_text(json.dumps(doc), encoding="utf-8")
        return original()

    monkeypatch.setattr(registry, "_read_registry_unlocked", read)
    assert current_adoption_record(registry).artifact_id == target.artifact_id
    assert reads >= 4
    Path(leaf.path).write_bytes(b"changed between calls")
    assert current_adoption_record(registry) is None


def test_current_adoption_preserves_external_resolution_and_bytes_fences(tmp_path, monkeypatch):
    local, _remote, remote_leaf, roots, resolver = _external_roots(tmp_path)
    registry, _leaf, target, _pointer = _case(tmp_path, registry=local, dependencies=(ArtifactDependencyRefV2.from_record(roots[0]),), resolver=resolver)
    # Public adoption resolution has no external resolver parameter: preserve
    # its existing fail-closed result rather than implicitly finding one.
    assert current_adoption_record(registry) is None
    batch = registry.verify_ready_artifact_closures
    def resolved_batch(values, **kwargs):
        kwargs["external_registry_resolver"] = resolver
        return batch(values, **kwargs)

    monkeypatch.setattr(registry, "verify_ready_artifact_closures", resolved_batch)
    assert current_adoption_record(registry).artifact_id == target.artifact_id
    Path(remote_leaf.path).write_bytes(b"changed external source")
    assert current_adoption_record(registry) is None


def test_current_adoption_preserves_historical_lease_fence(tmp_path, monkeypatch):
    registry = ArtifactRegistry(tmp_path / "artifact_registry.json", "historical-adoption-job")
    lease_path = tmp_path / "leased.json"
    manifest = _register_lease_manifest(registry, tmp_path, name="old", target_path=lease_path, target_bytes=b"historical source")
    registry, _leaf, target, pointer = _case(tmp_path, registry=registry, dependencies=(ArtifactDependencyRefV2.from_record(manifest),))
    assert current_adoption_record(registry).artifact_id == target.artifact_id
    original = registry._verify_ready_dependency_closure

    def verify(record, **kwargs):
        result = original(record, **kwargs)
        if record.artifact_id == pointer.artifact_id:
            lease_path.write_bytes(b"late historical bytes")
        return result

    monkeypatch.setattr(registry, "_verify_ready_dependency_closure", verify)
    assert current_adoption_record(registry) is None
