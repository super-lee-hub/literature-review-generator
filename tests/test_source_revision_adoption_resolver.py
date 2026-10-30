from __future__ import annotations

import hashlib
import json
from pathlib import Path

from outline.adoption_transaction import (
    ADOPTION_POINTER_ARTIFACT_ID,
    OutlineAdoptionTransaction,
    current_adoption_record,
)
from runtime.runner import AgentRuntimeRunner
from services.artifact_registry import ArtifactRecord, UnverifiedDependency
from services.job_workspace import JobWorkspace


class _ResolverAwareRegistry:
    def __init__(self, job_id: str, registry_path: str, parent_registry):
        self.job_id = job_id
        self.registry_path = registry_path
        self.publication_context = None
        self.parent_registry = parent_registry
        self.records: dict[str, ArtifactRecord] = {}
        self.closure_checks: list[tuple[str, object]] = []
        self.closure_batches: list[object] = []

    def get(self, artifact_id: str):
        return self.records.get(artifact_id)

    def verify_ready_artifact_closure(self, record, *, external_registry_resolver=None):
        self.closure_checks.append((record.artifact_id, external_registry_resolver))
        if record.artifact_id == "outline-v3:provider_receipt_closure":
            if external_registry_resolver is None:
                raise UnverifiedDependency("external dependency Registry is unavailable: parent-http")
            if external_registry_resolver("parent-http") is not self.parent_registry:
                raise UnverifiedDependency("external dependency Registry owner mismatch: parent-http")
        return record

    def verify_ready_artifact_closures(self, records, *, external_registry_resolver=None):
        self.closure_batches.append(external_registry_resolver)
        verified = [
            self.verify_ready_artifact_closure(
                record, external_registry_resolver=external_registry_resolver,
            )
            for record in records
        ]
        return tuple(verified)


def _registered_json_record(
    registry: _ResolverAwareRegistry,
    root: Path,
    artifact_id: str,
    artifact_type: str,
    document: dict,
    *,
    artifact_version: str = "v1",
    depends_on=(),
) -> ArtifactRecord:
    path = root / f"{artifact_id.replace(':', '-')}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    path.write_bytes(raw)
    record = ArtifactRecord(
        artifact_id=artifact_id,
        artifact_role=artifact_type,
        artifact_type=artifact_type,
        artifact_version=artifact_version,
        path=str(path),
        producer=__name__,
        job_id=registry.job_id,
        status="ready",
        content_hash=hashlib.sha256(raw).hexdigest(),
        depends_on=list(depends_on),
    )
    registry.records[artifact_id] = record
    return record


def test_outline_adoption_verifies_external_closure_and_current_pointer_readback(tmp_path, monkeypatch):
    workspace = JobWorkspace.create(str(tmp_path), "source-revision", "child-http")
    parent_registry = object()
    registry = _ResolverAwareRegistry(
        workspace.job_id, workspace.paths.registry_path, parent_registry,
    )
    resolver = lambda job_id: parent_registry if job_id == "parent-http" else None
    root = Path(workspace.root_dir)
    source = _registered_json_record(
        registry, root, "outline-v3:final_outline", "final_outline",
        {"artifact_type": "final_outline", "artifact_version": "v3", "job_id": registry.job_id,
         "status": "ready", "payload": {"title": "Verified S2 outline"}},
        artifact_version="v3",
    )
    coverage = _registered_json_record(
        registry, root, "outline-v3:coverage_audit", "coverage_audit",
        {"payload": {"passed": True}},
    )
    stability = _registered_json_record(
        registry, root, "outline-v3:stability_audit", "stability_audit",
        {"payload": {"status": "stable"}},
    )
    receipt_closure = _registered_json_record(
        registry, root, "outline-v3:provider_receipt_closure", "provider_receipt_closure",
        {"payload": {"complete": True}},
    )
    _registered_json_record(
        registry, root, "outline-v3:stage_health", "outline_stage_health",
        {"payload": {
            "adoption_eligible": True,
            "coverage_audit_hash": coverage.content_hash,
            "stability_audit_hash": stability.content_hash,
            "provider_receipt_closure_hash": receipt_closure.content_hash,
        }},
    )
    # The child plan is the switch which allows resume/export to reconstruct a
    # SourceRevision parent resolver. The resolver implementation itself is
    # supplied by Runner and is tested through this monkeypatched boundary.
    _registered_json_record(
        registry, root, "source_revision_plan", "source_revision_plan",
        {"artifact_type": "source_revision_plan", "job_id": registry.job_id},
    )
    monkeypatch.setattr(
        AgentRuntimeRunner,
        "_open_workspace",
        staticmethod(lambda _path: (workspace, registry)),
    )
    monkeypatch.setattr(
        AgentRuntimeRunner,
        "_external_registry_resolver",
        staticmethod(lambda _workspace: resolver),
    )

    published = []

    def publish_json_artifact(
        _context, target_registry, path, payload, *, artifact_id, artifact_role,
        artifact_type, artifact_version, producer, depends_on=(),
        external_registry_resolver=None, metadata=None,
    ):
        assert target_registry is registry
        record = _registered_json_record(
            registry, root, artifact_id, artifact_type, payload,
            artifact_version=artifact_version, depends_on=depends_on,
        )
        published.append((artifact_id, external_registry_resolver))
        return record

    monkeypatch.setattr("outline.adoption_transaction.publish_json_artifact", publish_json_artifact)

    result = OutlineAdoptionTransaction(workspace, registry).adopt(
        source_artifact_id=source.artifact_id,
        actor="source-revision-test",
        reason="verify external parent closure readback",
        expected_hash=source.content_hash,
    )

    assert result.status == "succeeded"
    assert any(
        artifact_id == "outline-v3:provider_receipt_closure" and captured is resolver
        for artifact_id, captured in registry.closure_checks
    )
    assert any(
        artifact_id == "outline-v3:adoption:" + source.content_hash[:16] and captured is resolver
        for artifact_id, captured in published
    )
    assert any(artifact_id == ADOPTION_POINTER_ARTIFACT_ID and captured is resolver
               for artifact_id, captured in published)
    assert any(artifact_id == result.audit_artifact_id and captured is resolver
               for artifact_id, captured in published)

    selected = current_adoption_record(registry)
    assert selected is not None and selected.artifact_id == result.adopted_artifact_id
    assert registry.closure_batches and registry.closure_batches[-1] is resolver


def test_adoption_external_resolver_can_find_the_declared_parent_only(tmp_path, monkeypatch):
    workspace = JobWorkspace.create(str(tmp_path), "source-revision", "child-http")
    parent_registry = object()
    registry = _ResolverAwareRegistry(
        workspace.job_id, workspace.paths.registry_path, parent_registry,
    )
    resolver = lambda job_id: parent_registry if job_id == "parent-http" else None
    _registered_json_record(
        registry, Path(workspace.root_dir), "source_revision_plan", "source_revision_plan",
        {"artifact_type": "source_revision_plan", "job_id": registry.job_id},
    )
    calls = []
    monkeypatch.setattr(
        AgentRuntimeRunner,
        "_open_workspace",
        staticmethod(lambda path: (calls.append(str(path)) or workspace, registry)),
    )
    monkeypatch.setattr(
        AgentRuntimeRunner,
        "_external_registry_resolver",
        staticmethod(lambda opened_workspace: resolver if opened_workspace is workspace else None),
    )

    from outline.adoption_transaction import _source_revision_resolver

    actual = _source_revision_resolver(registry)

    assert calls == [str(Path(registry.registry_path).parent)]
    assert actual is resolver
    assert actual("parent-http") is parent_registry
    assert actual("unrelated-job") is None
