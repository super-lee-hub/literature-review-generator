from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import validation.closure as closure_module
import validation.required_comparison_binding as required_binding
from runtime import export_bundle
from runtime.provider_runtime import ProviderAggregateBudgetV2, hash_json
from runtime.runner import AgentRuntimeRunner
from runtime.source_revision_admission import PLAN_MODE, PLAN_VERSION
from services.artifact_registry import ArtifactDependencyRefV2, ArtifactRegistry
from services.job_workspace import JobWorkspace
from tests.test_run_first_required_comparison_binding import _build_case
from tests.test_validation_closure import _bundle


def _register_parent_receipt_closure(tmp_path: Path) -> tuple[JobWorkspace, ArtifactRegistry, Any]:
    workspace = JobWorkspace.create(
        str(tmp_path / "parent-output"), "required-comparison-binding", "parent-http",
    )
    registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    path = Path(workspace.artifact_path("outline-provider-receipt-closure.json"))
    path.write_text(json.dumps({
        "artifact_type": "provider_receipt_closure",
        "artifact_version": "v1",
        "job_id": workspace.job_id,
        "status": "ready",
        "payload": {
            "closure_epoch_id": "parent-epoch",
            "expected_call_ids": [],
            "observed_call_ids": [],
            "missing_call_ids": [],
            "hash_mismatches": {},
            "complete": True,
            "closure_hash": "a" * 64,
        },
    }, sort_keys=True), encoding="utf-8")
    record = registry.register_file(
        artifact_id="outline-v3:provider_receipt_closure",
        artifact_role="provider_receipt_closure",
        artifact_type="provider_receipt_closure",
        artifact_version="v1",
        path=path,
        producer=__name__,
    )
    return workspace, registry, record


def _register_source_revision_plan(fixture: dict[str, Any], parent_workspace: JobWorkspace) -> None:
    registry = fixture["registry"]
    workspace = fixture["workspace"]
    source_path = str((Path(workspace.root_dir) / "source.pdf").resolve())
    body = {
        "schema_version": PLAN_VERSION,
        "mode": PLAN_MODE,
        "provider_semantic_contract_version": "source-closed-provider/v2",
        "child_core_spec_hash": "b" * 64,
        "child_job_id": registry.job_id,
        "child_project_name": workspace.project_name,
        "child_workspace_path": str(Path(workspace.root_dir).resolve()),
        "parent": {
            "workspace_path": str(Path(parent_workspace.root_dir).resolve()),
            "job_id": parent_workspace.job_id,
            "registry_path": str(Path(parent_workspace.paths.registry_path).resolve()),
            "authority_refs": [],
            "response_snapshot_refs": [],
        },
        "old_sources": [{
            "canonical_paper_key": "paper-a",
            "source_pdf": source_path,
            "pdf_sha256": "c" * 64,
            "identity_hash": "d" * 64,
        }],
        "new_sources": [{
            "canonical_paper_key": "paper-a",
            "source_pdf": source_path,
            "pdf_sha256": "e" * 64,
            "identity_hash": "d" * 64,
        }],
        "changed_paper_key": "paper-a",
        "unchanged_paper_keys": [],
        "review_intent_hash": "f" * 64,
        "config_binding": {
            "parent_path": str(Path(fixture["spec"].config).resolve()),
            "child_path": str(Path(fixture["spec"].config).resolve()),
            "file_sha256": "1" * 64,
        },
        "provider_budget": ProviderAggregateBudgetV2(10, 1000, 0, 60).to_dict(),
    }
    path = Path(workspace.artifact_path("source_revision_plan_v1.json"))
    path.write_text(json.dumps(body, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    runtime_spec = registry.get("runtime_job_spec")
    assert runtime_spec is not None
    registry.register_file(
        artifact_id="source_revision_plan",
        artifact_role="source_revision_plan",
        artifact_type="source_revision_plan",
        artifact_version="v1",
        path=path,
        producer=__name__,
        depends_on=[ArtifactDependencyRefV2.from_record(runtime_spec)],
        metadata={"plan_content_hash": hash_json(body)},
    )


def _add_external_outline_dependency(
    fixture: dict[str, Any],
    receipt_record: Any,
    *,
    mismatch: str = "",
) -> None:
    dependency = ArtifactDependencyRefV2.from_record(
        receipt_record, dependency_kind="external_job",
    )
    if mismatch == "parent":
        dependency = replace(dependency, job_id="other-parent")
    elif mismatch == "source":
        wrong_path = Path(fixture["workspace"].root_dir) / "wrong-provider-receipt.json"
        wrong_path.write_text("{}", encoding="utf-8")
        dependency = replace(dependency, path=str(wrong_path.resolve()))

    registry = fixture["registry"]
    registry_path = Path(registry.registry_path)
    payload = json.loads(registry_path.read_text(encoding="utf-8"))
    row = next(
        artifact for artifact in payload["artifacts"]
        if artifact["artifact_id"] == fixture["outline_record"].artifact_id
    )
    row["depends_on"].append(dependency.to_dict())
    registry_path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8",
    )
    registry.reload()


def _source_revision_case(tmp_path: Path, *, mismatch: str = ""):
    fixture = _build_case(tmp_path)
    parent_workspace, parent_registry, receipt_record = _register_parent_receipt_closure(tmp_path)
    _register_source_revision_plan(fixture, parent_workspace)
    _add_external_outline_dependency(fixture, receipt_record, mismatch=mismatch)
    if mismatch == "hash":
        Path(receipt_record.path).write_bytes(Path(receipt_record.path).read_bytes() + b"drift")
    return fixture, parent_registry


def test_source_revision_required_comparison_uses_discovered_parent_resolver(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture, parent_registry = _source_revision_case(tmp_path)
    created_resolvers: list[Any] = []
    observed_resolvers: list[Any] = []
    original_factory = AgentRuntimeRunner._external_registry_resolver
    original_closure = ArtifactRegistry.verify_ready_artifact_closure
    original_batch = ArtifactRegistry.verify_ready_artifact_closures

    def capture_factory(workspace, **kwargs):
        resolver = original_factory(workspace, **kwargs)
        created_resolvers.append(resolver)
        return resolver

    def capture_closure(self, record, *, external_registry_resolver=None):
        observed_resolvers.append(external_registry_resolver)
        return original_closure(
            self, record, external_registry_resolver=external_registry_resolver,
        )

    def capture_batch(self, records, *, external_registry_resolver=None):
        observed_resolvers.append(external_registry_resolver)
        return original_batch(
            self, records, external_registry_resolver=external_registry_resolver,
        )

    monkeypatch.setattr(AgentRuntimeRunner, "_external_registry_resolver", staticmethod(capture_factory))
    monkeypatch.setattr(ArtifactRegistry, "verify_ready_artifact_closure", capture_closure)
    monkeypatch.setattr(ArtifactRegistry, "verify_ready_artifact_closures", capture_batch)

    bindings, issues = required_binding.resolve_required_comparison_task_bindings(
        fixture["registry"], fixture["draft"],
    )

    assert issues == []
    assert bindings
    assert len(created_resolvers) == 1
    resolver = created_resolvers[0]
    assert resolver("parent-http") is not None
    assert resolver("parent-http").job_id == parent_registry.job_id
    assert observed_resolvers
    assert all(observed is resolver for observed in observed_resolvers)


@pytest.mark.parametrize(
    ("mismatch", "expected_issue"),
    [
        ("parent", "external dependency Registry is unavailable: other-parent/"),
        ("source", "dependency path mismatch: outline-v3:provider_receipt_closure"),
        ("hash", "dependency content hash changed: outline-v3:provider_receipt_closure"),
    ],
)
def test_source_revision_external_parent_or_dependency_mismatch_fails_closed(
    tmp_path: Path,
    mismatch: str,
    expected_issue: str,
) -> None:
    fixture, _parent_registry = _source_revision_case(tmp_path, mismatch=mismatch)

    bindings, issues = required_binding.resolve_required_comparison_task_bindings(
        fixture["registry"], fixture["draft"],
    )

    assert bindings == {}
    assert len(issues) == 1
    assert issues[0].startswith("required_comparison_outline_binding_untrusted:")
    assert expected_issue in issues[0]


def test_legacy_comparison_without_source_revision_plan_needs_no_external_resolver(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _build_case(tmp_path)

    def unexpected_resolver(*_args, **_kwargs):
        raise AssertionError("legacy comparison must not require external registry discovery")

    monkeypatch.setattr(
        AgentRuntimeRunner, "_external_registry_resolver", staticmethod(unexpected_resolver),
    )
    bindings, issues = required_binding.resolve_required_comparison_task_bindings(
        fixture["registry"], fixture["draft"],
    )

    assert issues == []
    assert bindings


def test_validation_closure_passes_the_supplied_resolver_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, registry = _bundle(tmp_path)
    resolver = lambda _job_id: None
    captured: list[Any] = []
    original = closure_module.required_review_comparison_issues

    def observe(registry_arg, draft, manifest, *, external_registry_resolver=None):
        captured.append(external_registry_resolver)
        return original(
            registry_arg, draft, manifest,
            external_registry_resolver=external_registry_resolver,
        )

    monkeypatch.setattr(closure_module, "required_review_comparison_issues", observe)
    ValidationClosureService = closure_module.ValidationClosureService
    ValidationClosureService(workspace, registry, external_registry_resolver=resolver).inspect()

    assert captured == [resolver]
    assert captured[0] is resolver


def test_export_derivation_passes_discovered_resolver_to_validation_service(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, registry = _bundle(tmp_path)
    resolver = lambda _job_id: None
    captured: list[Any] = []

    class ObservedValidationService:
        def __init__(self, _workspace, _registry, *, external_registry_resolver=None):
            captured.append(external_registry_resolver)

        def inspect(self):
            return SimpleNamespace(
                status="clean",
                semantic={},
                to_dict=lambda: {"status": "clean", "semantic": {}},
            )

    monkeypatch.setattr(
        AgentRuntimeRunner,
        "_external_registry_resolver",
        staticmethod(lambda *_args, **_kwargs: resolver),
    )
    monkeypatch.setattr(AgentRuntimeRunner, "status", staticmethod(lambda _workspace: SimpleNamespace(
        job_status="completed",
        completion_status="complete",
        canonical_ready=True,
        completion_reasons=(),
        completion_evidence_hash="",
    )))
    monkeypatch.setattr(closure_module, "ValidationClosureService", ObservedValidationService)

    export_bundle._derive_current_evidence(workspace, registry)

    assert captured == [resolver]
    assert captured[0] is resolver
