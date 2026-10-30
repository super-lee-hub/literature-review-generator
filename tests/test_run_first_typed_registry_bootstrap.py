from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from runtime.control_plane import ReviewControlPlane
from runtime.orchestrator import AgentRuntimeBridge
from runtime.provider_runtime import hash_json
from runtime.runner import AgentRuntimeRunner
from services.artifact_registry import RegistryError, file_sha256
from services.queue_service import LocalPublicationContext
from services.settings import ApplicationSettings
from services.stage1_reuse import (
    build_binding_hash,
    verified_typed_reuse_registry_paths,
)
from tests.test_stage1_closure_adversarial import (
    _artifact_entry,
    _registry_json,
    _run_stage1,
    _stage1_papers,
    _write_registry,
)
from validation.source_binding import resolve_bound_paper_artifacts


@pytest.fixture()
def typed_parent_child(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    parent_root = tmp_path / "parent-root"
    child_root = tmp_path / "child-root"
    papers = _stage1_papers(parent_root / "source-papers", prefix="bootstrap")
    parent_result, parent_workspace, parent_registry = _run_stage1(
        parent_root,
        monkeypatch,
        job_id="bootstrap-parent",
        papers=papers,
    )
    assert parent_result.completion_status == "complete", parent_result
    manifest_paths = tuple(
        record.path
        for record in parent_registry.list_records()
        if record.status == "ready"
        and record.artifact_type == "stage1_reusable_summary_manifest"
        and record.artifact_version == "v1"
    )
    assert len(manifest_paths) == 3
    child_result, child_workspace, child_registry = _run_stage1(
        child_root,
        monkeypatch,
        job_id="bootstrap-child",
        papers=[],
        reuse_summary_files=manifest_paths,
        reuse_stage1=True,
    )
    assert child_result.completion_status == "complete", child_result
    assert Path(parent_workspace.base_output_dir) != Path(child_workspace.base_output_dir)
    assert not Path(parent_workspace.root_dir).is_relative_to(
        Path(child_workspace.base_output_dir)
    )
    return SimpleNamespace(
        parent_workspace=parent_workspace,
        parent_registry=parent_registry,
        child_workspace=child_workspace,
        child_registry=child_registry,
        manifest_paths=manifest_paths,
    )


def _binding_session(case: Any) -> Any:
    return SimpleNamespace(
        request=SimpleNamespace(
            summary_file=None,
            summary_sources=(),
            reuse_summary_files=case.manifest_paths,
        ),
        context=SimpleNamespace(
            workspace=case.child_workspace,
            registry=case.child_registry,
            publication_context=LocalPublicationContext(),
        ),
        stage_host=SimpleNamespace(),
    )


def _rewrite_current_summary(case: Any, rows: list[dict[str, Any]]) -> None:
    record = case.child_registry.get("summary_file")
    assert record is not None
    Path(record.path).write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    payload = _registry_json(case.child_registry)
    pointer = _artifact_entry(payload, "summary_file")
    versioned_id = str(pointer["metadata"]["current_version_artifact_id"])
    for entry in (pointer, _artifact_entry(payload, versioned_id)):
        entry["content_hash"] = file_sha256(record.path)
        entry["metadata"]["summary_set_hash"] = hash_json(rows)
    _write_registry(case.child_registry, payload)


def test_typed_reuse_discovers_outside_sibling_owner_and_original_validation_text(
    typed_parent_child: Any,
) -> None:
    case = typed_parent_child
    expected_path = str(Path(case.parent_registry.registry_path).resolve())
    assert verified_typed_reuse_registry_paths(case.child_registry) == (expected_path,)
    bridge = AgentRuntimeBridge.__new__(AgentRuntimeBridge)
    bridge.job_spec = SimpleNamespace(metadata={})
    session = _binding_session(case)
    resolver = AgentRuntimeRunner._external_registry_resolver(case.child_workspace)
    resolved = resolver(case.parent_workspace.job_id)
    assert resolved is not None and resolved.registry_path == expected_path
    binding_record = bridge._ensure_validation_source_binding(
        session, case.child_registry, external_registry_resolver=resolver
    )
    assert binding_record is not None
    case.child_registry.verify_ready_artifact_closure(
        binding_record, external_registry_resolver=resolver
    )
    binding = json.loads(Path(binding_record.path).read_text(encoding="utf-8"))
    assert binding["bound_paper_count"] == 3
    assert len(binding_record.depends_on) == 6
    assert all(ref.job_id == case.parent_workspace.job_id for ref in binding_record.depends_on)
    assert {ref.artifact_type for ref in binding_record.depends_on} == {
        "paper_artifact", "evidence_manifest"
    }
    for _replay in range(2):
        fresh_resolver = AgentRuntimeRunner._external_registry_resolver(case.child_workspace)
        recovered, problems = resolve_bound_paper_artifacts(
            binding, external_registry_resolver=fresh_resolver
        )
        assert problems == (), problems
        assert len(recovered) == 3
        for paper in recovered:
            metadata = paper["_validation_source_binding"]
            assert metadata["source_workspace_job_id"] == case.parent_workspace.job_id
            inputs = paper["stage1_inputs"]["preprocess_evidence"]
            assert set(inputs) == {"markdown_path", "chunks_path", "page_index_path"}
            assert "treatment improved the outcome" in Path(inputs["markdown_path"]).read_text(
                encoding="utf-8"
            ).casefold()
    session.stage_host = SimpleNamespace(
        settings=ApplicationSettings(),
        summaries=[],
        check_cancelled=lambda: None,
        logger=None,
        config={},
    )
    validation_service = bridge.build_validation_service(session)
    assert validation_service.validation_source_binding_record == binding_record
    assert callable(validation_service.validation_external_registry_resolver)
    recovered, problems = resolve_bound_paper_artifacts(
        binding,
        external_registry_resolver=validation_service.validation_external_registry_resolver,
    )
    assert len(recovered) == 3 and problems == ()
    status = AgentRuntimeRunner.status(case.child_workspace.root_dir)
    assert status.completion_status == "complete", status
    reconciled = AgentRuntimeRunner.reconcile(case.child_workspace.root_dir)
    assert reconciled.clean, reconciled.issues
    assert set(reconciled.completed_stages) == {"analyze", "source_intake"}
    exported = ReviewControlPlane(repo_root=Path(__file__).resolve().parents[1]).export(
        workspace=case.child_workspace.root_dir
    )
    assert exported["status"] == "untrusted", exported
    stage_map = exported["manifest"]["current_stage_closure_map"]
    assert stage_map["blocking_issues"] == [], stage_map
    assert stage_map["provider_closures_by_stage"]["analyze"]["complete"] is True


@pytest.mark.parametrize("source_kind", ("paper_artifact", "normalized_text"))
def test_used_resolver_still_rejects_later_source_bytes_drift(
    typed_parent_child: Any, source_kind: str
) -> None:
    case = typed_parent_child
    resolver = AgentRuntimeRunner._external_registry_resolver(case.child_workspace)
    assert resolver(case.parent_workspace.job_id) is not None
    bridge = AgentRuntimeBridge.__new__(AgentRuntimeBridge)
    bridge.job_spec = SimpleNamespace(metadata={})
    record = bridge._ensure_validation_source_binding(
        _binding_session(case), case.child_registry, external_registry_resolver=resolver
    )
    assert record is not None
    case.child_registry.verify_ready_artifact_closure(record, external_registry_resolver=resolver)
    binding = json.loads(Path(record.path).read_text(encoding="utf-8"))
    if source_kind == "paper_artifact":
        source_ref = next(ref for ref in record.depends_on if ref.artifact_type == "paper_artifact")
        path = Path(source_ref.path)
    else:
        entry = next(iter(binding["papers"].values()))
        path = Path(entry["evidence"]["markdown_path"]["path"])
    with path.open("ab") as handle:
        handle.write(b"\nsource-bytes-drift\n")
    if source_kind == "paper_artifact":
        with pytest.raises(RegistryError):
            case.child_registry.verify_ready_artifact_closure(
                record, external_registry_resolver=resolver
            )
    recovered, problems = resolve_bound_paper_artifacts(binding, external_registry_resolver=resolver)
    assert len(recovered) == 2
    expected_issue = (
        "artifact_hash_mismatch" if source_kind == "paper_artifact" else "normalized_text_hash_mismatch"
    )
    assert any(expected_issue in problem for problem in problems), problems


@pytest.mark.parametrize("mutation", ("missing", "wrong_job"))
def test_typed_reuse_rejects_missing_or_foreign_owner(
    typed_parent_child: Any, mutation: str
) -> None:
    case = typed_parent_child
    path = Path(case.parent_registry.registry_path)
    if mutation == "missing":
        path.unlink()
    else:
        payload = _registry_json(case.parent_registry)
        payload["job_id"] = "foreign-owner"
        _write_registry(case.parent_registry, payload)
    with pytest.raises(RegistryError):
        verified_typed_reuse_registry_paths(case.child_registry)


@pytest.mark.parametrize("target", ("manifest", "source_summary"))
def test_typed_reuse_rejects_changed_authority_bytes(
    typed_parent_child: Any, target: str
) -> None:
    case = typed_parent_child
    manifest_path = Path(case.manifest_paths[0])
    if target == "manifest":
        path = manifest_path
    else:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        path = Path(manifest["source_summary_artifact_path"])
    with path.open("ab") as handle:
        handle.write(b"\nchanged-authority-bytes\n")
    with pytest.raises(RegistryError):
        verified_typed_reuse_registry_paths(case.child_registry)


@pytest.mark.parametrize(
    "mutation",
    ("missing_version", "version_hash", "version_path", "mutable_version", "set_hash", "pointer_role"),
)
def test_typed_reuse_rejects_stale_current_summary_pointer(
    typed_parent_child: Any, mutation: str
) -> None:
    case = typed_parent_child
    payload = _registry_json(case.child_registry)
    pointer = _artifact_entry(payload, "summary_file")
    metadata = pointer["metadata"]
    assert metadata["pointer_role"] == "current"
    versioned = _artifact_entry(payload, metadata["current_version_artifact_id"])
    if mutation == "missing_version":
        metadata["current_version_artifact_id"] = "missing-version"
    elif mutation == "version_hash":
        versioned["content_hash"] = "f" * 64
    elif mutation == "version_path":
        versioned["path"] = str(Path(versioned["path"]).with_name("foreign.json"))
    elif mutation == "mutable_version":
        versioned["metadata"]["immutable"] = False
    elif mutation == "pointer_role":
        metadata.pop("pointer_role")
    else:
        metadata["summary_set_hash"] = "f" * 64
    _write_registry(case.child_registry, payload)
    with pytest.raises(RegistryError, match="stale current summary pointer"):
        verified_typed_reuse_registry_paths(case.child_registry)


def test_typed_reuse_rejects_conflicting_verified_owner_locators(
    typed_parent_child: Any, tmp_path: Path
) -> None:
    case = typed_parent_child
    summary_record = case.child_registry.get("summary_file")
    assert summary_record is not None
    rows = json.loads(Path(summary_record.path).read_text(encoding="utf-8"))
    row = rows[0]
    reuse = row["stage1_reuse"]
    original_path = Path(reuse["typed_manifest_path"])
    manifest = json.loads(original_path.read_text(encoding="utf-8"))
    duplicate_registry = tmp_path / "duplicate-owner" / "artifact_registry.json"
    duplicate_registry.parent.mkdir()
    duplicate_registry.write_bytes(Path(case.parent_registry.registry_path).read_bytes())
    manifest["binding"]["source_authority_registry_path"] = str(duplicate_registry)
    manifest["binding_hash"] = build_binding_hash(manifest["binding"])
    manifest["manifest_content_hash"] = ""
    manifest["manifest_content_hash"] = hash_json(manifest)
    duplicate_manifest = tmp_path / "conflicting-manifest.json"
    duplicate_manifest.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    reuse["binding"]["source_authority_registry_path"] = str(duplicate_registry)
    reuse["typed_manifest_path"] = str(duplicate_manifest)
    reuse["typed_manifest_artifact_hash"] = file_sha256(duplicate_manifest)
    _rewrite_current_summary(case, rows)
    with pytest.raises(RegistryError, match="locators conflict"):
        verified_typed_reuse_registry_paths(case.child_registry)


def test_resolver_rejects_conflicting_explicit_and_typed_owner_locators(
    typed_parent_child: Any, tmp_path: Path
) -> None:
    case = typed_parent_child
    duplicate = tmp_path / "second-owner" / "artifact_registry.json"
    duplicate.parent.mkdir()
    duplicate.write_bytes(Path(case.parent_registry.registry_path).read_bytes())
    resolver = AgentRuntimeRunner._external_registry_resolver(
        case.child_workspace, registry_paths=(duplicate,)
    )
    assert resolver(case.parent_workspace.job_id) is None
