from __future__ import annotations

import importlib.util
import json
import shutil
import sys
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import runtime.runner as runner_module
import services

_repo_tests = Path.cwd() / "tests"
if (_repo_tests / "test_summary_correction_reuse.py").is_file():
    sys.path.insert(0, str(_repo_tests))
import tests.test_summary_correction_reuse as correction_fixture
from runtime.provider_runtime import hash_json
from services.artifact_registry import ArtifactRegistry, file_sha256
from services.job_workspace import JobWorkspace, publish_json_artifact
from services.queue_service import LocalPublicationContext
from validation.evidence_loader import PreprocessEvidenceLoader
from validation.source_binding import (
    build_validation_source_binding,
    resolve_bound_paper_artifacts,
)

PATCH_ROOT = Path(__file__).resolve().parents[1]
TARGET_KEY = "10.5555/tripathi2017"


def _load_module(monkeypatch: pytest.MonkeyPatch, module_name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, module_name, module)
    spec.loader.exec_module(module)
    return module


def _activate_patch(monkeypatch: pytest.MonkeyPatch) -> tuple[Any, Any]:
    helper = _load_module(
        monkeypatch,
        "services.automatic_stage1_reuse",
        PATCH_ROOT / "services" / "automatic_stage1_reuse.py",
    )
    monkeypatch.setattr(services, "automatic_stage1_reuse", helper, raising=False)
    patched_orchestrator = _load_module(
        monkeypatch,
        "runtime.orchestrator_automatic_stage1_reuse_v2_test",
        PATCH_ROOT / "runtime" / "orchestrator.py",
    )
    monkeypatch.setattr(
        runner_module,
        "AgentRuntimeBridge",
        patched_orchestrator.AgentRuntimeBridge,
    )
    return helper, patched_orchestrator


def _route_workspaces_to_root(
    monkeypatch: pytest.MonkeyPatch,
    output_root: Path,
) -> None:
    original_create = JobWorkspace.create.__func__
    call_counts: dict[str, int] = {}

    def create(
        cls: type[JobWorkspace],
        _base_output_dir: str,
        project_name: str,
        job_id: str | None = None,
    ) -> JobWorkspace:
        count = call_counts.get(project_name, 0) + 1
        call_counts[project_name] = count
        job_identity = f"{job_id or cls.generate_job_id()}-{count}"
        return original_create(cls, str(output_root), project_name, job_identity)

    monkeypatch.setattr(JobWorkspace, "create", classmethod(create))


def _body(path: str | Path) -> Mapping[str, Any]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    body = raw.get("payload") if isinstance(raw, Mapping) else None
    return body if isinstance(body, Mapping) else raw


def _seed_origin_validation_evidence(
    *,
    workspace: JobWorkspace,
    registry: ArtifactRegistry,
    title: str,
    summary: Mapping[str, Any],
) -> None:
    evidence_root = Path(workspace.artifact_path("automatic_reuse_validation_evidence"))
    evidence_root.mkdir(parents=True, exist_ok=True)
    normalized_path = evidence_root / "normalized.md"
    normalized_text = "# Original source\n\nOriginal verified text from the source owner.\n" * 20
    normalized_path.write_text(normalized_text, encoding="utf-8")
    chunks_path = evidence_root / "chunks.json"
    chunks_payload = [{"chunk_id": "source-c1", "page": 1, "text": normalized_text}]
    chunks_path.write_text(json.dumps(chunks_payload, ensure_ascii=False), encoding="utf-8")
    page_index_path = evidence_root / "page_index.json"
    page_index_payload = [{"page": 1, "chunk_ids": ["source-c1"]}]
    page_index_path.write_text(json.dumps(page_index_payload, ensure_ascii=False), encoding="utf-8")
    manifest_payload = {
        "artifact_type": "evidence_manifest",
        "artifact_version": "v1",
        "job_id": registry.job_id,
        "canonical_paper_key": TARGET_KEY,
        "created_at": "2026-10-20T00:00:00Z",
        "artifacts": [
            {
                "artifact_type": "normalized_text",
                "path": str(normalized_path),
                "content_hash": file_sha256(normalized_path),
            },
            {
                "artifact_type": "chunks",
                "path": str(chunks_path),
                "content_hash": file_sha256(chunks_path),
            },
            {
                "artifact_type": "page_index",
                "path": str(page_index_path),
                "content_hash": file_sha256(page_index_path),
            },
        ],
    }
    manifest_record = publish_json_artifact(
        LocalPublicationContext(),
        registry,
        evidence_root / "manifest.json",
        manifest_payload,
        artifact_id="automatic_reuse:evidence_manifest:target",
        artifact_role="evidence_manifest",
        artifact_type="evidence_manifest",
        artifact_version="v1",
        producer="tests.test_automatic_stage1_reuse_v2",
    )
    paper_payload = {
        "artifact_type": "paper_artifact",
        "artifact_version": "v1",
        "job_id": registry.job_id,
        "paper_identity": {
            "canonical_paper_key": TARGET_KEY,
            "source_paper_id": TARGET_KEY,
        },
        "paper_info": {
            "canonical_paper_key": TARGET_KEY,
            "title": title,
        },
        "analysis": {"ai_summary": dict(summary)},
        "stage1_inputs": {
            "input_mode": "text_first",
            "evidence_manifest_path": manifest_record.path,
            "evidence_manifest_hash": manifest_record.content_hash,
        },
    }
    publish_json_artifact(
        LocalPublicationContext(),
        registry,
        evidence_root / "paper.json",
        paper_payload,
        artifact_id="automatic_reuse:paper:target",
        artifact_role="paper_artifact",
        artifact_type="paper_artifact",
        artifact_version="v1",
        producer="tests.test_automatic_stage1_reuse_v2",
    )


def _export_fixture(
    *,
    tmp_path: Path,
    output_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    correction_variant: str = "",
    pdf_source_to_copy: Path | None = None,
    with_validation_evidence: bool = False,
) -> tuple[Any, ...]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    (
        source_workspace,
        source_registry,
        source_record,
        summaries,
        pdf_path,
        pdf_hash,
        page_renders,
    ) = correction_fixture._build_source(tmp_path)
    if pdf_source_to_copy is not None:
        shutil.copyfile(pdf_source_to_copy, pdf_path)
        pdf_hash = file_sha256(pdf_path)
        for render in page_renders.values():
            render["source_pdf_sha256"] = pdf_hash

    prior_records = correction_fixture._publish_v1_prior_basis(
        workspace=source_workspace,
        registry=source_registry,
        source_record=source_record,
        summaries=summaries,
    )
    target_prior_records = [
        record
        for record in prior_records
        if str(_body(record.path).get("canonical_paper_key") or "") == TARGET_KEY
    ]
    assert len(target_prior_records) == 1
    prior = target_prior_records[0]
    if with_validation_evidence:
        _seed_origin_validation_evidence(
            workspace=source_workspace,
            registry=source_registry,
            title=str(summaries[32]["paper_info"]["title"]),
            summary=summaries[32]["ai_summary"],
        )

    correction_workspace = JobWorkspace.create(
        str(tmp_path / "correction-output"),
        "correction-project",
        "correction-job",
    )
    correction_registry = ArtifactRegistry(
        correction_workspace.paths.registry_path,
        correction_workspace.job_id,
    )
    proposal = correction_fixture._proposal_payload(
        source_record,
        summaries,
        pdf_path,
        pdf_hash,
        page_renders,
    )
    if correction_variant:
        first_patch = proposal["source_field_patches"][0]
        corrected_value = str(first_patch["proposed_after_value"]) + correction_variant
        first_patch["proposed_after_value"] = corrected_value
        first_patch["proposed_after_value_hash_json"] = hash_json(corrected_value)
    source_artifact_id = str(getattr(source_record, "artifact_id", ""))
    assert source_artifact_id
    correction = correction_fixture.prepare_source_summary_correction_candidate(
        proposal_payload=proposal,
        source_registry=source_registry,
        source_artifact_id=source_artifact_id,
        destination_workspace=correction_workspace,
        destination_registry=correction_registry,
        publication_context=LocalPublicationContext(),
    )
    candidate_artifact_id = getattr(correction, "candidate_artifact_id", "")
    assert isinstance(candidate_artifact_id, str) and candidate_artifact_id
    candidate_record = correction_registry.get(candidate_artifact_id)
    assert candidate_record is not None
    adoption = correction_fixture.adopt_source_summary_correction_candidate(
        source_registry=source_registry,
        destination_registry=correction_registry,
        workspace=correction_workspace,
        publication_context=LocalPublicationContext(),
        candidate_artifact_id=correction.candidate_artifact_id,
        expected_candidate_hash=candidate_record.content_hash,
        actor="synthetic-owner",
        reason="Review and adopt the synthetic source correction",
    )
    exported = correction_fixture.export_owner_corrected_stage1_reuse_authority(
        origin_registry=source_registry,
        correction_registry=correction_registry,
        correction_workspace=correction_workspace,
        adoption_receipt_artifact_id=adoption.adoption_receipt_artifact_id,
        publication_context=LocalPublicationContext(),
        external_registry_resolver=lambda job_id: {
            source_registry.job_id: source_registry,
            correction_registry.job_id: correction_registry,
        }.get(job_id),
    )
    v2_record = correction_registry.get(exported.owner_corrected_manifest_artifact_id)
    assert v2_record is not None and v2_record.status == "ready"
    assert v2_record.artifact_version == "v2"
    return (
        source_workspace,
        source_registry,
        source_record,
        summaries,
        pdf_path,
        pdf_hash,
        correction_workspace,
        correction_registry,
        prior,
        v2_record,
        exported,
    )

def _plan(
    *,
    helper: Any,
    patched_orchestrator: Any,
    output_root: Path,
    current_workspace: JobWorkspace,
    registries: Mapping[str, ArtifactRegistry],
    pdf_path: Path,
) -> Any:
    return helper.discover_automatic_stage1_reuse(
        base_output_dir=output_root,
        current_workspace_dir=current_workspace.root_dir,
        current_job_id=current_workspace.job_id,
        paper_work_items=[
            SimpleNamespace(canonical_paper_key=TARGET_KEY, source_pdf=str(pdf_path))
        ],
        summary_payloads_from_file=(
            patched_orchestrator.InternalStageExecutorRegistry._summary_payloads_from_file
        ),
        external_registry_resolver=lambda job_id: registries.get(job_id),
    )


def test_automatic_v2_uses_exact_origin_prior_and_preserves_original_validation_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    helper, patched_orchestrator = _activate_patch(monkeypatch)
    output_root = tmp_path / "output"
    output_root.mkdir()
    _route_workspaces_to_root(monkeypatch, output_root)
    (
        _source_workspace,
        source_registry,
        _source_record,
        summaries,
        pdf_path,
        _pdf_hash,
        _correction_workspace,
        correction_registry,
        prior,
        v2_record,
        _exported,
    ) = _export_fixture(
        tmp_path=tmp_path / "owner-correction",
        output_root=output_root,
        monkeypatch=monkeypatch,
        with_validation_evidence=True,
    )
    current_workspace = JobWorkspace.create(
        str(output_root), "current-probe", "current-v2-probe"
    )
    registries = {
        source_registry.job_id: source_registry,
        correction_registry.job_id: correction_registry,
    }
    plan = _plan(
        helper=helper,
        patched_orchestrator=patched_orchestrator,
        output_root=output_root,
        current_workspace=current_workspace,
        registries=registries,
        pdf_path=pdf_path,
    )
    selected = plan.candidates_by_key[TARGET_KEY]
    assert selected.manifest_record_version == "v2"
    assert selected.manifest_record_id == v2_record.artifact_id
    assert selected.superseded_prior_v1_identity == (
        source_registry.job_id,
        prior.artifact_id,
        prior.content_hash,
        str(Path(prior.path).resolve()),
    )
    assert [item["manifest_artifact_version"] for item in selected.validation_source_manifests] == [
        "v2",
        "v1",
    ]
    assert selected.validation_source_manifests[1]["owner_job_id"] == source_registry.job_id
    corrected_summary = selected.summary["ai_summary"]
    assert corrected_summary["core_analysis"]["summary"] != summaries[32]["ai_summary"]["core_analysis"]["summary"]

    summary_paths = [
        selected.manifest_record_path,
        selected.validation_source_manifests[1]["manifest_path"],
    ]
    source_path_args = {
        "candidate": selected.summary,
        "candidate_path": selected.manifest_record_path,
        "candidate_record": v2_record,
        "owner_metadata": selected.source_item_metadata(),
        "external_registry_resolver": lambda job_id: registries.get(job_id),
    }
    assert patched_orchestrator._selected_v2_reuse_source_paths(
        **source_path_args, candidate_registry=correction_registry
    ) == tuple(summary_paths)
    assert patched_orchestrator._selected_v2_reuse_source_paths(
        **source_path_args, candidate_registry=source_registry
    ) is None
    binding = build_validation_source_binding(
        summary_sources=summary_paths,
        local_registry=None,
        job_id=current_workspace.job_id,
        include_paper_keys={TARGET_KEY},
    )
    assert binding["bound_paper_count"] == 1
    source_entry = binding["papers"][TARGET_KEY]
    assert source_entry["source_workspace_job_id"] == source_registry.job_id
    assert source_entry["stage1_paper_artifact_id"] == "automatic_reuse:paper:target"
    assert source_entry["evidence_manifest_artifact_id"] == "automatic_reuse:evidence_manifest:target"
    resolved, problems = resolve_bound_paper_artifacts(
        binding,
        external_registry_resolver=lambda job_id: registries.get(job_id),
        present_paper_keys={TARGET_KEY},
    )
    assert problems == (), problems
    assert len(resolved) == 1
    resolved_paper = resolved[0]
    assert resolved_paper["_validation_source_binding"]["source_workspace_job_id"] == source_registry.job_id
    evidence_manifest_path = resolved_paper["stage1_inputs"]["evidence_manifest_path"]
    raw_manifest = _body(evidence_manifest_path)
    evidence_by_type = {
        str(row["artifact_type"]): row
        for row in raw_manifest["artifacts"]
        if isinstance(row, Mapping)
    }
    loaded = PreprocessEvidenceLoader().load_evidence(
        normalized_text_path=evidence_by_type["normalized_text"]["path"],
        chunks_path=evidence_by_type["chunks"]["path"],
        page_index_path=evidence_by_type["page_index"]["path"],
        manifest_path=evidence_manifest_path,
        strict=True,
    )
    assert "Original verified text" in loaded.normalized_text
    assert loaded.chunks and loaded.page_index

    v2_bytes = Path(v2_record.path).read_bytes()
    Path(v2_record.path).write_bytes(v2_bytes + b" ")
    tampered_plan = _plan(
        helper=helper,
        patched_orchestrator=patched_orchestrator,
        output_root=output_root,
        current_workspace=current_workspace,
        registries=registries,
        pdf_path=pdf_path,
    )
    assert TARGET_KEY not in tampered_plan.candidates_by_key
    assert any(
        row.get("canonical_paper_key") == TARGET_KEY
        and row.get("reason") == "automatic_reuse_candidate_authority_rejected"
        for row in tampered_plan.rejected_candidates
    )


def test_automatic_v2_conflict_quarantines_only_key_and_preserves_both_correction_owners(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    helper, patched_orchestrator = _activate_patch(monkeypatch)
    output_root = tmp_path / "output"
    output_root.mkdir()
    _route_workspaces_to_root(monkeypatch, output_root)
    first = _export_fixture(
        tmp_path=tmp_path / "owner-correction-a",
        output_root=output_root,
        monkeypatch=monkeypatch,
    )
    first_pdf = first[4]
    second = _export_fixture(
        tmp_path=tmp_path / "owner-correction-b",
        output_root=output_root,
        monkeypatch=monkeypatch,
        correction_variant=" Alternative approved finding.",
        pdf_source_to_copy=first_pdf,
    )
    assert file_sha256(first[4]) == file_sha256(second[4])
    first_v2 = _body(first[9].path)
    second_v2 = _body(second[9].path)
    assert first_v2["canonical_paper_key"] == second_v2["canonical_paper_key"] == TARGET_KEY
    assert first_v2["summary_payload"] != second_v2["summary_payload"]

    current_workspace = JobWorkspace.create(
        str(output_root), "current-probe", "current-v2-conflict-probe"
    )
    registries: dict[str, ArtifactRegistry] = {}
    for prepared in (first, second):
        registries[prepared[1].job_id] = prepared[1]
        registries[prepared[7].job_id] = prepared[7]
    plan = _plan(
        helper=helper,
        patched_orchestrator=patched_orchestrator,
        output_root=output_root,
        current_workspace=current_workspace,
        registries=registries,
        pdf_path=first_pdf,
    )
    assert TARGET_KEY not in plan.candidates_by_key
    conflicts = [
        row for row in plan.rejected_candidates
        if row.get("canonical_paper_key") == TARGET_KEY
        and row.get("reason") == "automatic_reuse_conflicting_non_equivalent_owners"
    ]
    assert len(conflicts) == 1
    candidate_ids = {
        str(item["manifest_artifact_id"])
        for item in conflicts[0]["candidates"]
    }
    assert candidate_ids == {first[9].artifact_id, second[9].artifact_id}
