"""Imported typed summaries must retain their original full-text authority."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
from types import SimpleNamespace
from typing import Any

import fitz
import pytest

from runtime.orchestrator import AgentRuntimeBridge
from runtime.job_spec import RuntimeJobSpec, RuntimeSourceSpec
from services.artifact_registry import ArtifactDependencyRefV2, RegistryError
from services.job_workspace import publish_json_artifact
from services.queue_service import LocalPublicationContext
from services.settings import ApplicationSettings
from tests.test_stage1_closure_adversarial import (
    _registry_json,
    _rewrite_json_artifact,
    _run_stage1,
    _stage1_papers,
    _write_registry,
)
from validation.closure import (
    _provider_closure_entry,
    resolve_current_stage_closure_map,
)
from validation.current_validation import _load_inputs
from validation.evidence_loader import PreprocessEvidenceLoader
from validation.source_binding import (
    build_validation_source_authority_fingerprint,
    validation_source_binding_semantic_hash,
)


@pytest.fixture()
def typed_parent_child(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    parent_root = tmp_path / "parent-root"
    child_root = tmp_path / "child-root"
    papers = _stage1_papers(parent_root / "source-papers", prefix="authority")
    parent_result, parent_workspace, parent_registry = _run_stage1(
        parent_root, monkeypatch, job_id="authority-parent", papers=papers
    )
    assert parent_result.completion_status == "complete", parent_result.completion_reasons
    manifest_paths = tuple(
        record.path for record in parent_registry.list_records()
        if record.status == "ready"
        and record.artifact_type == "stage1_reusable_summary_manifest"
        and record.artifact_version == "v1"
    )
    assert len(manifest_paths) == 3
    child_result, child_workspace, child_registry = _run_stage1(
        child_root,
        monkeypatch,
        job_id="authority-child",
        papers=[],
        reuse_summary_files=manifest_paths,
        reuse_stage1=True,
    )
    assert child_result.completion_status == "complete", child_result.completion_reasons
    assert Path(parent_workspace.base_output_dir) != Path(child_workspace.base_output_dir)
    return SimpleNamespace(
        parent_workspace=parent_workspace,
        parent_registry=parent_registry,
        child_workspace=child_workspace,
        child_registry=child_registry,
        manifest_paths=manifest_paths,
    )


def _body(record: Any) -> dict[str, Any]:
    payload = json.loads(Path(record.path).read_text(encoding="utf-8"))
    return dict(payload.get("payload", payload))


def _parent_papers(case: Any) -> dict[str, Any]:
    return {
        _body(record)["paper_identity"]["canonical_paper_key"]: record
        for record in case.parent_registry.list_records()
        if record.artifact_type == "paper_artifact" and record.status == "ready"
    }


def _validation_service(case: Any) -> Any:
    context = LocalPublicationContext()
    registry = case.child_registry
    workspace = case.child_workspace
    keys = sorted(_parent_papers(case))
    for artifact_type, payload in (
        (
            "review_draft",
            {
                "draft_identity": {"draft_id": "source-authority-loader"},
                "generation_context": {},
                "content": {"sections": []},
                "projections": {},
            },
        ),
        (
            "citation_manifest",
            {
                "manifest_identity": {"manifest_id": "source-authority-loader"},
                "review_reference": {},
                "citation_sets": [{"paper_ids": keys}],
                "occurrences": [],
                "bibliography": [],
            },
        ),
    ):
        payload.update(
            artifact_type=artifact_type,
            artifact_version="v3",
            job_id=workspace.job_id,
            created_at="2026-10-07T00:00:00Z",
        )
        publish_json_artifact(
            context,
            registry,
            workspace.artifact_path(f"{artifact_type}.json"),
            payload,
            artifact_id=artifact_type,
            artifact_role=artifact_type,
            artifact_type=artifact_type,
            artifact_version="v3",
            producer=__name__,
        )
    summary_record = registry.get("summary_file")
    assert summary_record is not None
    session = SimpleNamespace(
        request=SimpleNamespace(
            summary_file=None,
            summary_sources=(),
            reuse_summary_files=case.manifest_paths,
        ),
        context=SimpleNamespace(
            workspace=workspace,
            registry=registry,
            publication_context=context,
        ),
        stage_host=SimpleNamespace(
            settings=ApplicationSettings(),
            summaries=json.loads(Path(summary_record.path).read_text(encoding="utf-8")),
            check_cancelled=lambda: None,
            logger=None,
            config={},
        ),
    )
    job_spec = RuntimeJobSpec(
        project_name=workspace.job_id,
        source=RuntimeSourceSpec(mode="direct", pdf_folder=str(workspace.root_dir)),
        job_id=workspace.job_id,
        action="validate_review",
        workspace_path=str(workspace.root_dir),
    )
    job_spec.validate()
    bridge = AgentRuntimeBridge.__new__(AgentRuntimeBridge)
    bridge.job_spec = job_spec
    return bridge.build_validation_service(session)


def _fingerprint(service: Any, papers: list[dict[str, Any]], keys: list[str]) -> Any:
    return build_validation_source_authority_fingerprint(
        paper_artifacts=papers,
        registry=service.artifact_registry,
        cited_paper_keys=keys,
        current_binding_artifact_id=service.current_validation_source_binding_id,
        current_binding_content_hash=service.current_validation_source_binding_content_hash,
        current_binding_semantic_hash=service.current_validation_source_binding_semantic_hash,
    )


def test_imported_typed_reuse_keeps_original_owner_fingerprint(
    typed_parent_child: Any,
) -> None:
    case = typed_parent_child
    assert not [
        record for record in case.child_registry.list_records()
        if record.artifact_type == "paper_artifact"
    ]
    service = _validation_service(case)
    assert service.paper_artifact_records == ()
    _review, _manifest, papers, preprocess, _metadata = _load_inputs(service)
    assert service._validation_source_authority_diagnostics == ()
    expected = _parent_papers(case)
    assert len(papers) == len(expected) == 3
    fingerprint, _identity, diagnostics = _fingerprint(service, papers, sorted(expected))
    assert diagnostics == (), diagnostics
    for entry in fingerprint["papers"]:
        key = entry["canonical_paper_key"]
        owner = expected[key]
        assert entry["source_job_id"] == case.parent_workspace.job_id
        assert entry["paper_artifact_id"] == owner.artifact_id
        assert entry["paper_artifact_hash"] == owner.content_hash
        assert entry["paper_artifact_version"] == "v1"
        assert entry["evidence_manifest_id"]
        assert entry["evidence_manifest_version"] == "v1"
        for leaf in ("normalized_text_hash", "chunks_hash", "page_index_hash"):
            assert len(entry[leaf]) == 64
        original_text = Path(preprocess[key]["markdown_path"]).read_text(encoding="utf-8")
        assert "treatment improved the outcome" in original_text.casefold()
        selected = next(
            paper for paper in papers
            if paper["paper_identity"]["canonical_paper_key"] == key
        )
        evidence = PreprocessEvidenceLoader().load_evidence(
            normalized_text_path=preprocess[key]["markdown_path"],
            chunks_path=preprocess[key]["chunks_path"],
            page_index_path=preprocess[key]["page_index_path"],
            manifest_path=selected["stage1_inputs"]["evidence_manifest_path"],
            strict=True,
        )
        assert evidence.normalized_text == original_text
        assert evidence.chunks and evidence.page_index


def test_direct_pdf_typed_reuse_preserves_verified_manifest_owner(
    typed_parent_child: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = typed_parent_child
    direct_root = tmp_path / "direct-child"
    pdf_dir = direct_root / "source-papers"
    pdf_dir.mkdir(parents=True)
    scope = _body(case.parent_registry.get("stage1_source_scope"))
    for row in scope["papers"]:
        original = Path(row["source_pdf"])
        shutil.copyfile(original, pdf_dir / original.name)
    papers = _stage1_papers(pdf_dir, prefix="authority")
    result, workspace, registry = _run_stage1(
        direct_root,
        monkeypatch,
        job_id="authority-direct-child",
        papers=papers,
        reuse_summary_files=case.manifest_paths,
        reuse_stage1=True,
        output_root=Path(case.parent_workspace.base_output_dir),
    )
    assert result.completion_status == "complete", result.completion_reasons
    summary_record = registry.get("summary_file")
    assert summary_record is not None
    summaries = json.loads(Path(summary_record.path).read_text(encoding="utf-8"))
    assert len(summaries) == 3
    for summary in summaries:
        reuse = summary["stage1_reuse"]
        assert reuse["source_authority_kind"] == "typed_manifest"
        # This row binds the current reuse execution; the original typed
        # manifest retains a different, independently verified source binding.
        assert "authority_kind" not in reuse
        assert summary["provider"]["transport_count"] == 0
    carriers = [record for record in registry.list_records()
                if record.artifact_type == "paper_artifact"]
    assert len(carriers) == 3
    assert all(not _body(record)["stage1_inputs"]["evidence_manifest_path"]
               for record in carriers)
    direct_case = SimpleNamespace(
        parent_workspace=case.parent_workspace,
        parent_registry=case.parent_registry,
        child_workspace=workspace,
        child_registry=registry,
        manifest_paths=case.manifest_paths,
    )
    service = _validation_service(direct_case)
    _review, _manifest, selected, _preprocess, _metadata = _load_inputs(service)
    assert service._validation_source_authority_diagnostics == ()
    owners = _parent_papers(case)
    fingerprint, _identity, problems = _fingerprint(service, selected, sorted(owners))
    assert problems == (), problems
    assert len(selected) == len(fingerprint["papers"]) == 3
    for entry in fingerprint["papers"]:
        owner = owners[entry["canonical_paper_key"]]
        assert entry["source_job_id"] == case.parent_workspace.job_id
        assert entry["paper_artifact_hash"] == owner.content_hash
        assert entry["evidence_manifest_id"]
        assert len(entry["normalized_text_hash"]) == 64


def test_mixed_direct_pdf_reuse_keeps_local_and_typed_authorities(
    typed_parent_child: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = typed_parent_child
    direct_root = tmp_path / "mixed-child"
    pdf_dir = direct_root / "source-papers"
    pdf_dir.mkdir(parents=True)
    scope = _body(case.parent_registry.get("stage1_source_scope"))
    for row in scope["papers"]:
        original = Path(row["source_pdf"])
        shutil.copyfile(original, pdf_dir / original.name)

    manifest_by_key: dict[str, str] = {}
    for path in case.manifest_paths:
        raw_manifest = json.loads(Path(path).read_text(encoding="utf-8"))
        manifest = raw_manifest.get("payload", raw_manifest)
        manifest_by_key[str(manifest.get("canonical_paper_key") or "")] = path
    assert len(manifest_by_key) == 3
    # Change one valid PDF while preserving the other two byte-for-byte. The
    # changed key must be generated locally; the other two retain typed owners.
    generated_key = sorted(manifest_by_key)[-1]
    generated_pdf = next(
        Path(row["source_pdf"])
        for row in scope["papers"]
        if str(row.get("paper_key") or "") == generated_key
    )
    changed_pdf = pdf_dir / generated_pdf.name
    document = fitz.open(changed_pdf)
    document[0].insert_text((72, 700), "Additional source context for mixed authority regression")
    changed_path = changed_pdf.with_name(changed_pdf.stem + ".changed.pdf")
    document.save(changed_path)
    document.close()
    changed_pdf.unlink()
    changed_path.replace(changed_pdf)

    papers = _stage1_papers(pdf_dir, prefix="authority")
    reused_paths = case.manifest_paths
    result, workspace, registry = _run_stage1(
        direct_root,
        monkeypatch,
        job_id="authority-mixed-child",
        papers=papers,
        reuse_summary_files=reused_paths,
        reuse_stage1=True,
        output_root=Path(case.parent_workspace.base_output_dir),
    )
    assert result.completion_status == "complete", result.completion_reasons
    summary_record = registry.get("summary_file")
    assert summary_record is not None
    summaries = json.loads(Path(summary_record.path).read_text(encoding="utf-8"))
    assert len(summaries) == 3
    typed_rows = {
        str(row["paper_info"]["canonical_paper_key"]): row
        for row in summaries
        if row.get("stage1_reuse", {}).get("source_authority_kind") == "typed_manifest"
    }
    assert set(typed_rows) == set(manifest_by_key) - {generated_key}
    generated_row = next(
        row for row in summaries
        if str(row["paper_info"]["canonical_paper_key"]) == generated_key
    )
    assert generated_row.get("stage1_reuse", {}).get("source_authority_kind") != "typed_manifest"
    local_papers = [
        record for record in registry.list_records()
        if record.artifact_type == "paper_artifact" and record.status == "ready"
    ]
    assert len(local_papers) == 3
    assert sum(bool(_body(record)["stage1_inputs"]["evidence_manifest_path"])
               for record in local_papers) == 1
    mixed_case = SimpleNamespace(
        parent_workspace=case.parent_workspace,
        parent_registry=case.parent_registry,
        child_workspace=workspace,
        child_registry=registry,
        manifest_paths=case.manifest_paths,
    )
    import validation.closure as closure_module

    original_closure_map = closure_module.resolve_current_stage_closure_map
    original_provider_entry = closure_module._provider_closure_entry
    proof_calls: list[tuple[str, Any]] = []

    def reject_full_run_closure(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("mixed Stage 1 binding must not require a pre-CAS full-run closure")

    def observe_analyze_proof(
        stage: str,
        record: Any,
        terminal_record: Any,
        terminal_payload: Any,
        registry: Any,
        **kwargs: Any,
    ) -> Any:
        proof_calls.append((stage, kwargs.get("current_set")))
        return original_provider_entry(
            stage, record, terminal_record, terminal_payload, registry, **kwargs
        )

    monkeypatch.setattr(closure_module, "resolve_current_stage_closure_map", reject_full_run_closure)
    monkeypatch.setattr(closure_module, "_provider_closure_entry", observe_analyze_proof)
    service = _validation_service(mixed_case)
    monkeypatch.setattr(closure_module, "resolve_current_stage_closure_map", original_closure_map)
    assert proof_calls == [("analyze", None)]
    binding_record = service.validation_source_binding_record
    assert binding_record is not None
    binding_payload = _body(binding_record)
    expected_reused_keys = set(manifest_by_key) - {generated_key}
    assert set(binding_payload["papers"]) == expected_reused_keys
    assert binding_payload["bound_paper_count"] == len(expected_reused_keys)

    _review, _manifest, selected, _preprocess, _metadata = _load_inputs(service)
    assert service._validation_source_authority_diagnostics == ()
    fingerprint, _identity, problems = _fingerprint(
        service, selected, sorted(manifest_by_key)
    )
    assert problems == (), problems
    assert len(fingerprint["papers"]) == 3
    for entry in fingerprint["papers"]:
        expected_owner = (workspace.job_id if entry["canonical_paper_key"] == generated_key
                          else case.parent_workspace.job_id)
        assert entry["source_job_id"] == expected_owner


@pytest.mark.parametrize("declared_field", ("evidence_manifest_path", "evidence_manifest_hash"))
def test_partial_local_manifest_declaration_still_conflicts_with_bound_owner(
    typed_parent_child: Any,
    declared_field: str,
) -> None:
    case = typed_parent_child
    service = _validation_service(case)
    key, owner = next(iter(_parent_papers(case).items()))
    paper = _body(owner)
    paper["created_from_job_id"] = case.child_workspace.job_id
    paper["stage1_inputs"] = {
        "evidence_manifest_path": "",
        "evidence_manifest_hash": "",
        declared_field: owner.path if declared_field.endswith("path") else "0" * 64,
    }
    local = publish_json_artifact(
        LocalPublicationContext(), case.child_registry,
        case.child_workspace.artifact_path("partial-local-paper.json"), paper,
        artifact_id="paper:partial-local-declaration", artifact_role="paper_artifact",
        artifact_type="paper_artifact", artifact_version="v1", producer=__name__,
    )
    _review, _manifest, selected, _preprocess, _metadata = _load_inputs(service)
    assert f"validation_source_authority_ambiguous:{key}" in (
        service._validation_source_authority_diagnostics
    )
    assert next(row for row in selected
                if row["paper_identity"]["canonical_paper_key"] == key)["_registry_artifact_id"] == local.artifact_id


@pytest.mark.parametrize(
    ("target", "diagnostic"),
    (
        ("paper", "artifact_hash_mismatch"),
        ("manifest", "evidence_manifest_hash_mismatch"),
        ("normalized_text", "normalized_text_hash_mismatch"),
    ),
)
def test_imported_typed_reuse_rejects_original_source_tampering(
    typed_parent_child: Any,
    target: str,
    diagnostic: str,
) -> None:
    case = typed_parent_child
    service = _validation_service(case)
    binding = _body(service.validation_source_binding_record)
    key, entry = next(iter(binding["papers"].items()))
    path = Path(
        entry["stage1_paper_artifact_path"] if target == "paper"
        else entry["evidence_manifest_path"] if target == "manifest"
        else entry["evidence"]["markdown_path"]["path"]
    )
    with path.open("ab") as handle:
        handle.write(b"\nchanged-original-source\n")
    _review, _manifest, papers, _preprocess, _metadata = _load_inputs(service)
    assert len(papers) == 2
    assert any(
        problem.startswith(f"VALIDATION_SOURCE_AUTHORITY_INVALID:{key}:")
        and diagnostic in problem
        for problem in service._validation_source_authority_diagnostics
    ), service._validation_source_authority_diagnostics
    _fingerprint_payload, _identity, problems = _fingerprint(
        service, papers, sorted(binding["papers"])
    )
    assert f"source_authority_missing:{key}" in problems
    assert not [
        record for record in case.child_registry.list_records()
        if record.artifact_type == "paper_artifact"
    ]


def test_imported_typed_reuse_rejects_foreign_owner_before_validation(
    typed_parent_child: Any,
) -> None:
    case = typed_parent_child
    payload = _registry_json(case.parent_registry)
    payload["job_id"] = "foreign-owner"
    _write_registry(case.parent_registry, payload)
    with pytest.raises(RegistryError):
        _validation_service(case)


@pytest.mark.parametrize(
    "mutation",
    ("count", "output_ref", "foreign_job_ref", "foreign_artifact_ref", "typed_manifest"),
)
def test_imported_stage1_coverage_rejects_incomplete_or_changed_authority(
    typed_parent_child: Any,
    mutation: str,
) -> None:
    case = typed_parent_child
    registry = case.child_registry
    before = resolve_current_stage_closure_map(registry)
    assert not before.blocking_issues, before.blocking_issues
    terminal = registry.get(before.provider_closures_by_stage["analyze"]["terminal_artifact_id"])
    assert terminal is not None
    terminal_payload = _body(terminal)
    if mutation == "count":
        _rewrite_json_artifact(
            registry,
            "summary_source_manifest",
            lambda payload: payload.update(summary_count=payload["summary_count"] + 1),
        )
        registry.reload()
    elif mutation == "output_ref":
        terminal_payload["output_artifact_refs"] = [
            ref for ref in terminal_payload["output_artifact_refs"]
            if ref["artifact_type"] != "summary_file"
        ]
    elif mutation in {"foreign_job_ref", "foreign_artifact_ref"}:
        for ref in terminal_payload["output_artifact_refs"]:
            if ref["artifact_type"] == "summary_file":
                ref["job_id" if mutation == "foreign_job_ref" else "artifact_id"] = "foreign-owner"
    else:
        with Path(case.manifest_paths[0]).open("ab") as handle:
            handle.write(b"\nchanged-typed-manifest\n")
    closure = registry.get("stage1:provider_receipt_closure")
    assert closure is not None
    _entry, problems = _provider_closure_entry(
        "analyze",
        closure,
        terminal,
        terminal_payload,
        registry,
        external_registry_resolver=lambda job_id: (
            case.parent_registry if job_id == case.parent_workspace.job_id else None
        ),
    )
    assert "provider_closure_paper_artifact_identity_coverage_incomplete:analyze" in problems
    if mutation == "typed_manifest":
        assert any(
            problem.startswith("provider_closure_imported_source_authority_untrusted:analyze:")
            for problem in problems
        ), problems


def test_complete_local_authority_still_conflicts_with_external_owner(
    typed_parent_child: Any,
) -> None:
    case = typed_parent_child
    service = _validation_service(case)
    key, owner = next(iter(_parent_papers(case).items()))
    paper = _body(owner)
    manifest_path = Path(paper["stage1_inputs"]["evidence_manifest_path"])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest = dict(manifest.get("payload", manifest))
    manifest["job_id"] = case.child_workspace.job_id
    context = LocalPublicationContext()
    local_manifest = publish_json_artifact(
        context,
        case.child_registry,
        case.child_workspace.artifact_path("local-evidence-manifest.json"),
        manifest,
        artifact_id="evidence_manifest:local-authority",
        artifact_role="evidence_manifest",
        artifact_type="evidence_manifest",
        artifact_version="v1",
        producer=__name__,
    )
    paper["created_from_job_id"] = case.child_workspace.job_id
    paper["stage1_inputs"]["evidence_manifest_path"] = local_manifest.path
    paper["stage1_inputs"]["evidence_manifest_hash"] = local_manifest.content_hash
    local_paper = publish_json_artifact(
        context,
        case.child_registry,
        case.child_workspace.artifact_path("local-paper-authority.json"),
        paper,
        artifact_id="paper:local-authority",
        artifact_role="paper_artifact",
        artifact_type="paper_artifact",
        artifact_version="v1",
        producer=__name__,
        depends_on=[ArtifactDependencyRefV2.from_record(local_manifest)],
    )
    _review, _manifest, papers, _preprocess, _metadata = _load_inputs(service)
    assert f"validation_source_authority_ambiguous:{key}" in (
        service._validation_source_authority_diagnostics
    )
    chosen = next(item for item in papers if item["paper_identity"]["canonical_paper_key"] == key)
    assert chosen["_registry_artifact_id"] == local_paper.artifact_id
    assert "_validation_source_binding" not in chosen
    fingerprint, _identity, problems = _fingerprint(service, [chosen], [key])
    assert problems == (), problems
    assert fingerprint["papers"][0]["source_job_id"] == case.child_workspace.job_id
    assert fingerprint["papers"][0]["paper_artifact_hash"] == local_paper.content_hash

    matching_binding = _body(service.validation_source_binding_record)
    matching_binding["papers"][key].update(
        source_workspace_job_id=case.child_workspace.job_id,
        source_workspace=case.child_workspace.root_dir,
        stage1_paper_artifact_id=local_paper.artifact_id,
        stage1_paper_artifact_path=local_paper.path,
        stage1_paper_artifact_hash=local_paper.content_hash,
        evidence_manifest_artifact_id=local_manifest.artifact_id,
        evidence_manifest_job_id=case.child_workspace.job_id,
        evidence_manifest_path=local_manifest.path,
        evidence_manifest_hash=local_manifest.content_hash,
    )
    matching_record = publish_json_artifact(
        context,
        case.child_registry,
        case.child_workspace.artifact_path("matching-local-source-binding.json"),
        matching_binding,
        artifact_id="validation_source_binding:matching-local",
        artifact_role="runtime_stage_evidence",
        artifact_type="validation_source_binding",
        artifact_version="v1",
        producer=__name__,
        depends_on=[
            ArtifactDependencyRefV2.from_record(local_paper),
            ArtifactDependencyRefV2.from_record(local_manifest),
        ],
        metadata={"semantic_payload_hash": validation_source_binding_semantic_hash(matching_binding)},
    )
    service.validation_source_binding_record = matching_record
    _review, _manifest, papers, _preprocess, _metadata = _load_inputs(service)
    assert service._validation_source_authority_diagnostics == ()
    chosen = next(item for item in papers if item["paper_identity"]["canonical_paper_key"] == key)
    assert chosen["_registry_artifact_id"] == local_paper.artifact_id
    assert "_validation_source_binding" not in chosen


def test_pure_typed_reuse_skips_mixed_source_projection(
    typed_parent_child: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = typed_parent_child
    import validation.closure as closure_module

    def unexpected_analyze_proof(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("pure reuse must retain the original source-binding path")

    monkeypatch.setattr(closure_module, "_provider_closure_entry", unexpected_analyze_proof)
    service = _validation_service(case)
    binding_record = service.validation_source_binding_record
    assert binding_record is not None
    binding_payload = _body(binding_record)
    assert set(binding_payload["papers"]) == set(_parent_papers(case))
