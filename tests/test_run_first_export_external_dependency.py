"""Export trust must resolve the same external Registry owners as validation."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import test_validation_source_binding as source_binding_tests
from runtime.export_bundle import _derive_current_evidence
from runtime.orchestrator import AgentRuntimeBridge
from runtime.runner import AgentRuntimeRunner
from services.artifact_registry import ArtifactRegistry
from services.job_workspace import JobWorkspace
from services.queue_service import LocalPublicationContext

pytest_plugins = ("test_validation_source_binding",)


def _register_external_validation_binding(
    tmp_path: Path,
    upstream_workspace: tuple[Path, dict[str, str]],
) -> tuple[JobWorkspace, ArtifactRegistry, ArtifactRegistry, Any]:
    upstream_root, hashes = upstream_workspace
    upstream_registry = ArtifactRegistry(
        upstream_root / "artifact_registry.json",
        "up_job",
    )
    resolver = source_binding_tests._upstream_resolver(upstream_registry)
    summary_path = source_binding_tests._summary_file(
        upstream_root,
        hashes["paper_key"],
    )

    workspace = JobWorkspace.create(
        str(tmp_path / "export-output"),
        "export-dependency",
        job_id="export-dependency-job",
    )
    registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    publication_context = LocalPublicationContext()
    session = SimpleNamespace(
        request=SimpleNamespace(
            summary_file=None,
            summary_sources=(str(summary_path),),
            reuse_summary_files=(),
        ),
        context=SimpleNamespace(
            workspace=workspace,
            registry=registry,
            publication_context=publication_context,
        ),
        stage_host=SimpleNamespace(),
    )
    bridge = AgentRuntimeBridge.__new__(AgentRuntimeBridge)
    binding_record = bridge._ensure_validation_source_binding(
        session,
        registry,
        external_registry_resolver=resolver,
    )
    assert binding_record is not None
    external_dependencies = [
        dependency
        for dependency in binding_record.depends_on
        if dependency.dependency_kind == "external_job"
    ]
    assert {(item.job_id, item.artifact_type) for item in external_dependencies} == {
        ("up_job", "paper_artifact"),
        ("up_job", "evidence_manifest"),
    }
    return workspace, registry, upstream_registry, binding_record


@pytest.mark.parametrize(
    ("resolver_case", "expected_error"),
    (
        ("valid", ""),
        ("missing", "external dependency Registry is unavailable"),
        ("foreign", "external dependency Registry owner mismatch"),
        ("hash_drift", "dependency content hash changed"),
    ),
)
def test_export_current_evidence_uses_external_registry_resolver_for_ready_dependencies(
    tmp_path: Path,
    upstream_workspace: tuple[Path, dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
    resolver_case: str,
    expected_error: str,
) -> None:
    workspace, registry, upstream_registry, binding_record = (
        _register_external_validation_binding(tmp_path, upstream_workspace)
    )
    valid_resolver = source_binding_tests._upstream_resolver(upstream_registry)

    if resolver_case == "valid" or resolver_case == "hash_drift":
        active_resolver = valid_resolver
    elif resolver_case == "missing":
        active_resolver = lambda _job_id: None
    else:
        foreign_workspace = JobWorkspace.create(
            str(tmp_path / "foreign-output"),
            "foreign-owner",
            job_id="foreign_job",
        )
        foreign_registry = ArtifactRegistry(
            foreign_workspace.paths.registry_path,
            "foreign_job",
        )
        active_resolver = lambda _job_id: foreign_registry

    if resolver_case == "hash_drift":
        paper_record = upstream_registry.get("paper:deadbeef01")
        assert paper_record is not None
        Path(paper_record.path).write_bytes(
            Path(paper_record.path).read_bytes() + b"\nsource-bytes-drift\n"
        )

    # Keep _derive_current_evidence and its status, validation, and stage-map
    # gates real; only control the external Registry locator result.
    monkeypatch.setattr(
        AgentRuntimeRunner,
        "_external_registry_resolver",
        staticmethod(lambda *_args, **_kwargs: active_resolver),
    )
    evidence = _derive_current_evidence(workspace, registry)
    dependency_issue_prefix = f"dependency_closure:{binding_record.artifact_id}:"
    dependency_issues = [
        issue
        for issue in evidence["issues"]
        if issue.startswith(dependency_issue_prefix)
    ]

    if resolver_case == "valid":
        assert dependency_issues == []
        return

    assert dependency_issues, evidence["issues"]
    assert any(expected_error in issue for issue in dependency_issues), dependency_issues
