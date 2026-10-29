"""Focused repair paper-authority boundary tests.

These controls exercise ``RepairTransactionService._paper_authority_inputs``
with the real source-binding fixture and Registry closure.  They keep the
current workspace's paper records authoritative, recover only the exact
foreign records named by the validation run's binding, and fail closed on
identity, owner, byte, or leaf drift.
"""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import shutil
from types import SimpleNamespace
from typing import Any, Callable

import pytest

import test_validation_source_binding as source_binding_tests
from services.artifact_registry import ArtifactDependencyRefV2, ArtifactRegistry, RegistryError
from services.job_workspace import JobWorkspace
from validation.repair_transaction import RepairTransactionService
from validation.run_result import (
    ClaimValidationResultV1,
    ClaimVerdict,
    ValidationInputArtifactsV1,
    ValidationRunResultV1,
)
from validation.source_binding import validation_source_binding_semantic_hash


pytest_plugins = ("test_validation_source_binding",)


EXTERNAL_KEY = "10.1234/upstream.paper.2026"
LOCAL_KEY = "10.1234/local.paper.2026"


def _registry_snapshot(registry: ArtifactRegistry) -> tuple[int, dict[str, Any]]:
    return (
        registry.revision,
        {
            record.artifact_id: {
                "artifact_type": record.artifact_type,
                "status": record.status,
                "path": record.path,
                "content_hash": record.content_hash,
                "depends_on": tuple(
                    (
                        dependency.dependency_kind,
                        dependency.job_id,
                        dependency.artifact_id,
                        dependency.artifact_type,
                        dependency.path,
                        dependency.content_hash,
                    )
                    for dependency in record.depends_on
                ),
            }
            for record in registry.list_records()
        },
    )


def _write_local_paper(
    workspace: JobWorkspace,
    registry: ArtifactRegistry,
    paper_key: str = LOCAL_KEY,
) -> Any:
    root = Path(workspace.artifact_path("local-authority"))
    root.mkdir(parents=True, exist_ok=True)
    leaf_paths = {
        "normalized_text": root / "normalized.md",
        "chunks": root / "chunks.json",
        "page_index": root / "page_index.json",
    }
    leaf_paths["normalized_text"].write_text("local source text\n", encoding="utf-8")
    leaf_paths["chunks"].write_text("{\"chunks\": []}", encoding="utf-8")
    leaf_paths["page_index"].write_text("{\"pages\": []}", encoding="utf-8")
    manifest_path = root / "evidence_manifest.json"
    manifest_payload = {
        "artifact_type": "evidence_manifest",
        "artifact_version": "v1",
        "job_id": workspace.job_id,
        "canonical_paper_key": paper_key,
        "created_at": "2026-10-25T00:00:00Z",
        "artifacts": [
            {
                "artifact_type": artifact_type,
                "path": str(path),
                "content_hash": source_binding_tests._sha256_bytes(path.read_bytes()),
            }
            for artifact_type, path in (
                ("normalized_text", leaf_paths["normalized_text"]),
                ("chunks", leaf_paths["chunks"]),
                ("page_index", leaf_paths["page_index"]),
            )
        ],
    }
    manifest_path.write_text(json.dumps(manifest_payload), encoding="utf-8")
    manifest_record = registry.register_file(
        artifact_id=f"evidence_manifest:{paper_key}",
        artifact_role="evidence_manifest",
        artifact_type="evidence_manifest",
        artifact_version="v1",
        path=manifest_path,
        producer="tests.test_repair_bound_paper_inputs",
    )
    paper_path = root / "paper.json"
    paper_path.write_text(
        json.dumps(
            {
                "paper_identity": {
                    "canonical_paper_key": paper_key,
                    "source_paper_id": paper_key,
                },
                "paper_info": {
                    "canonical_paper_key": paper_key,
                    "title": "Local paper",
                },
                "analysis": {"ai_summary": {"main_findings": ["local finding"]}},
                "stage1_inputs": {
                    "evidence_manifest_path": str(manifest_path),
                    "evidence_manifest_hash": manifest_record.content_hash,
                },
            }
        ),
        encoding="utf-8",
    )
    return registry.register_file(
        artifact_id=f"paper:{paper_key}",
        artifact_role="paper_summary",
        artifact_type="paper_artifact",
        artifact_version="v1",
        path=paper_path,
        producer="tests.test_repair_bound_paper_inputs",
        depends_on=[ArtifactDependencyRefV2.from_record(manifest_record)],
    )


def _register_local_summary_projection(
    case: SimpleNamespace,
    paper_key: str = EXTERNAL_KEY,
) -> Any:
    """Install a local summary-only projection with no paper authority."""

    root = Path(case.downstream_workspace.artifact_path("local-summary-projection"))
    root.mkdir(parents=True, exist_ok=True)
    local_paper = root / "paper.json"
    local_paper.write_text(
        json.dumps(
            {
                "paper_identity": {
                    "canonical_paper_key": paper_key,
                    "source_paper_id": paper_key,
                },
                "paper_info": {
                    "canonical_paper_key": paper_key,
                    "title": "Summary projection",
                },
                "analysis": {"ai_summary": {"main_findings": ["summary-only"]}},
            }
        ),
        encoding="utf-8",
    )
    return case.registry.register_file(
        artifact_id="paper:local-summary-projection",
        artifact_role="paper_summary",
        artifact_type="paper_artifact",
        artifact_version="v1",
        path=local_paper,
        producer="tests.test_repair_bound_paper_inputs",
    )


def _register_local_marker_paper(case: SimpleNamespace, paper_key: str = EXTERNAL_KEY) -> Any:
    """Install a same-key paper carrying a conflicting authority marker."""

    root = Path(case.downstream_workspace.artifact_path("local-marker-authority"))
    root.mkdir(parents=True, exist_ok=True)
    paper_path = root / "paper.json"
    paper_path.write_text(
        json.dumps(
            {
                "paper_identity": {
                    "canonical_paper_key": paper_key,
                    "source_paper_id": paper_key,
                },
                "paper_info": {"canonical_paper_key": paper_key, "title": "Marker"},
                "analysis": {"ai_summary": {"main_findings": ["marker"]}},
                "_validation_source_binding": {
                    "source_workspace_job_id": "wrong-marker-owner",
                },
            }
        ),
        encoding="utf-8",
    )
    return case.registry.register_file(
        artifact_id="paper:local-marker-authority",
        artifact_role="paper_summary",
        artifact_type="paper_artifact",
        artifact_version="v1",
        path=paper_path,
        producer="tests.test_repair_bound_paper_inputs",
    )


def _binding_case(
    tmp_path: Path,
    upstream_workspace: tuple[Path, dict[str, str]],
    *,
    include_local: bool = False,
) -> SimpleNamespace:
    upstream_source, upstream_hashes = upstream_workspace
    upstream_ws = tmp_path / "upstream__up_job"
    shutil.copytree(upstream_source, upstream_ws)
    summary_path = source_binding_tests._summary_file(upstream_ws, EXTERNAL_KEY)
    bridge, session, registry, upstream_registry = source_binding_tests._binding_context(
        tmp_path, [summary_path], upstream_ws
    )
    local_record = None
    if include_local:
        local_record = _write_local_paper(session.context.workspace, registry)
    binding = source_binding_tests.build_validation_source_binding(
        summary_sources=[str(summary_path)],
        local_registry=registry,
        job_id=registry.job_id,
    )
    binding_record = source_binding_tests._publish_manual_binding(
        session,
        registry,
        binding,
        upstream_registry,
    )
    assert binding_record is not None
    binding_payload = json.loads(Path(binding_record.path).read_text(encoding="utf-8"))
    assert set(binding_payload["papers"]) == {EXTERNAL_KEY}
    assert upstream_hashes["paper_key"] == EXTERNAL_KEY
    return SimpleNamespace(
        upstream_workspace=upstream_ws,
        upstream_registry=upstream_registry,
        downstream_workspace=session.context.workspace,
        registry=registry,
        session=session,
        bridge=bridge,
        binding_record=binding_record,
        binding_payload=binding_payload,
        local_record=local_record,
        external_entry=binding_payload["papers"][EXTERNAL_KEY],
    )


def _local_self_binding_case(
    tmp_path: Path,
    upstream_workspace: tuple[Path, dict[str, str]],
) -> SimpleNamespace:
    upstream_ws, _hashes = upstream_workspace
    _bridge, session, registry, upstream_registry = source_binding_tests._binding_context(
        tmp_path, [], upstream_ws
    )
    local_record = _write_local_paper(session.context.workspace, registry, EXTERNAL_KEY)
    manifest_record = registry.get(f"evidence_manifest:{EXTERNAL_KEY}")
    assert manifest_record is not None
    import validation.source_binding as source_binding

    binding_payload = {
        "artifact_type": "validation_source_binding",
        "artifact_version": "v1",
        "binding_contract_version": "v1",
        "job_id": registry.job_id,
        "upstream_workspaces": [],
        "summary_source_identities": [],
        "diagnostics": [],
        "bound_paper_count": 1,
        "papers": {
            EXTERNAL_KEY: {
                "canonical_paper_key": EXTERNAL_KEY,
                "source_workspace": session.context.workspace.paths.root_dir,
                "source_workspace_job_id": registry.job_id,
                "stage1_paper_artifact_id": local_record.artifact_id,
                "stage1_paper_artifact_type": "paper_artifact",
                "stage1_paper_artifact_version": "v1",
                "stage1_paper_artifact_path": local_record.path,
                "stage1_paper_artifact_hash": local_record.content_hash,
                "evidence_manifest_artifact_id": manifest_record.artifact_id,
                "evidence_manifest_artifact_type": "evidence_manifest",
                "evidence_manifest_artifact_version": "v1",
                "evidence_manifest_job_id": registry.job_id,
                "evidence_manifest_path": manifest_record.path,
                "evidence_manifest_hash": manifest_record.content_hash,
                "evidence": source_binding._manifest_evidence(manifest_record.path),
            }
        },
    }
    binding_path = Path(session.context.workspace.artifact_path("local-self-binding.json"))
    binding_path.write_text(json.dumps(binding_payload), encoding="utf-8")
    binding_record = registry.register_file(
        artifact_id="validation_source_binding:local-self",
        artifact_role="runtime_stage_evidence",
        artifact_type="validation_source_binding",
        artifact_version="v1",
        path=binding_path,
        producer="tests.test_repair_bound_paper_inputs",
        depends_on=[
            ArtifactDependencyRefV2.from_record(local_record),
            ArtifactDependencyRefV2.from_record(manifest_record),
        ],
    )
    return SimpleNamespace(
        upstream_workspace=upstream_ws,
        upstream_registry=upstream_registry,
        downstream_workspace=session.context.workspace,
        registry=registry,
        session=session,
        bridge=_bridge,
        binding_record=binding_record,
        binding_payload=binding_payload,
        local_record=local_record,
        external_entry=binding_payload["papers"][EXTERNAL_KEY],
    )


def _validation_record(
    case: SimpleNamespace,
    *,
    binding_record: Any | None = None,
    binding_payload: dict[str, Any] | None = None,
    binding_id: str | None = None,
    semantic_hash: str | None = None,
    content_hash: str | None = None,
    legacy_hash: str | None = None,
    job_id: str | None = None,
    paper_key: str = EXTERNAL_KEY,
) -> Any:
    selected_record = binding_record or case.binding_record
    selected_payload = binding_payload or case.binding_payload
    if selected_record is None:
        inputs = ValidationInputArtifactsV1()
    else:
        selected_semantic_hash = semantic_hash or validation_source_binding_semantic_hash(
            selected_payload
        )
        inputs = ValidationInputArtifactsV1(
            validation_source_binding_id=binding_id or selected_record.artifact_id,
            validation_source_binding_hash=selected_semantic_hash,
            validation_source_binding_semantic_hash=selected_semantic_hash,
            validation_source_binding_content_hash=content_hash or selected_record.content_hash,
        )
    claim = ClaimValidationResultV1(
        claim_result_id="claim:paper-authority",
        claim_unit_ids=(),
        citation_set_key="R001",
        paper_ids=(paper_key,),
        block_ids=("block-1",),
        claim_text="The source supports the recorded finding.",
        claim_context="",
        verdict=ClaimVerdict.NEEDS_REVIEW,
        reasoning_summary="fixture authority boundary",
        repair_hint="manual review",
        root_causes=("citation_mapping_error",),
        span_start=None,
        span_end=None,
        alignment_status="ambiguous",
        alignment_confidence=0.0,
        low_confidence=True,
        details={},
        evidence_candidates=(),
    )
    validation = ValidationRunResultV1.create(
        job_id=job_id or case.registry.job_id,
        execution_status="succeeded",
        claim_results=(claim,),
        attempt_id="paper-authority-boundary",
        input_artifacts=inputs,
        expected_claim_count=1,
        review_has_citations=True,
        evidence_complete=True,
    )
    validation_path = Path(
        case.downstream_workspace.artifact_path("validation/paper-authority.json")
    )
    validation_path.parent.mkdir(parents=True, exist_ok=True)
    validation_payload = validation.to_dict()
    if legacy_hash is not None:
        validation_payload["input_artifacts"]["validation_source_binding_hash"] = legacy_hash
    validation_path.write_text(json.dumps(validation_payload), encoding="utf-8")
    dependencies = []
    if selected_record is not None:
        dependencies.append(ArtifactDependencyRefV2.from_record(selected_record))
    return case.registry.register_file(
        artifact_id="validation_run_result:paper-authority",
        artifact_role="validation_run_result",
        artifact_type="validation_run_result",
        artifact_version="v1",
        path=validation_path,
        producer="tests.test_repair_bound_paper_inputs",
        depends_on=dependencies,
        external_registry_resolver=source_binding_tests._upstream_resolver(
            case.upstream_registry
        ),
    )


def _service(case: SimpleNamespace) -> RepairTransactionService:
    return RepairTransactionService(case.downstream_workspace, case.registry)


def _assert_blocked(
    action: Callable[[], Any],
    registry: ArtifactRegistry,
) -> None:
    before = _registry_snapshot(registry)
    with pytest.raises((ValueError, RegistryError)) as caught:
        action()
    message = str(caught.value).lower()
    assert any(
        token in message
        for token in ("authority", "binding", "paper", "manifest", "evidence")
    ), message
    assert _registry_snapshot(registry) == before


def test_valid_foreign_paper_authority_is_readonly(
    tmp_path: Path,
    upstream_workspace: tuple[Path, dict[str, str]],
) -> None:
    case = _binding_case(tmp_path, upstream_workspace)
    validation_record = _validation_record(case)
    before = _registry_snapshot(case.registry)

    paper_payloads, local_records, selected_binding = _service(case)._paper_authority_inputs(
        validation_record
    )

    assert set(paper_payloads) == {EXTERNAL_KEY}
    assert local_records == {}
    assert selected_binding is not None
    assert selected_binding.artifact_id == case.binding_record.artifact_id
    assert paper_payloads[EXTERNAL_KEY]["paper_identity"]["canonical_paper_key"] == EXTERNAL_KEY
    assert paper_payloads[EXTERNAL_KEY]["stage1_inputs"]
    assert _registry_snapshot(case.registry) == before


@pytest.mark.parametrize(
    "invalid_field", ("binding_id", "semantic_hash", "content_hash", "legacy_hash")
)
def test_binding_identity_mismatch_is_blocked_without_registry_write(
    tmp_path: Path,
    upstream_workspace: tuple[Path, dict[str, str]],
    invalid_field: str,
) -> None:
    case = _binding_case(tmp_path, upstream_workspace)
    kwargs: dict[str, Any] = {}
    if invalid_field == "binding_id":
        kwargs["binding_id"] = "validation_source_binding:missing"
    elif invalid_field == "semantic_hash":
        kwargs["semantic_hash"] = "f" * 64
    elif invalid_field == "content_hash":
        kwargs["content_hash"] = "e" * 64
    else:
        kwargs["legacy_hash"] = "d" * 64
    validation_record = _validation_record(case, **kwargs)

    _assert_blocked(
        lambda: _service(case)._paper_authority_inputs(validation_record),
        case.registry,
    )


def test_binding_owner_mismatch_is_blocked(
    tmp_path: Path,
    upstream_workspace: tuple[Path, dict[str, str]],
) -> None:
    case = _binding_case(tmp_path, upstream_workspace)
    tampered_binding = deepcopy(case.binding_payload)
    tampered_binding["papers"][EXTERNAL_KEY]["source_workspace_job_id"] = "wrong-owner"
    payload_path = Path(case.downstream_workspace.artifact_path("bad-owner-binding.json"))
    payload_path.write_text(json.dumps(tampered_binding), encoding="utf-8")
    original_entry = case.external_entry
    dependencies = [
        ArtifactDependencyRefV2(
            dependency_kind="external_job",
            job_id=str(original_entry["source_workspace_job_id"]),
            artifact_id=str(original_entry["stage1_paper_artifact_id"]),
            artifact_type="paper_artifact",
            path=str(original_entry["stage1_paper_artifact_path"]),
            content_hash=str(original_entry["stage1_paper_artifact_hash"]),
        ),
        ArtifactDependencyRefV2(
            dependency_kind="external_job",
            job_id=str(original_entry["evidence_manifest_job_id"]),
            artifact_id=str(original_entry["evidence_manifest_artifact_id"]),
            artifact_type="evidence_manifest",
            path=str(original_entry["evidence_manifest_path"]),
            content_hash=str(original_entry["evidence_manifest_hash"]),
        ),
    ]
    bad_binding_record = case.registry.register_file(
        artifact_id="validation_source_binding:wrong-owner",
        artifact_role="runtime_stage_evidence",
        artifact_type="validation_source_binding",
        artifact_version="v1",
        path=payload_path,
        producer="tests.test_repair_bound_paper_inputs",
        depends_on=dependencies,
        external_registry_resolver=source_binding_tests._upstream_resolver(
            case.upstream_registry
        ),
    )
    validation_record = _validation_record(
        case,
        binding_record=bad_binding_record,
        binding_payload=tampered_binding,
    )

    _assert_blocked(
        lambda: _service(case)._paper_authority_inputs(validation_record),
        case.registry,
    )


@pytest.mark.parametrize("drift", ("paper", "manifest", "leaf_tamper", "leaf_missing"))
def test_historical_external_authority_drift_is_blocked(
    tmp_path: Path,
    upstream_workspace: tuple[Path, dict[str, str]],
    drift: str,
) -> None:
    case = _binding_case(tmp_path, upstream_workspace)
    validation_record = _validation_record(case)
    entry = case.external_entry
    if drift == "paper":
        source_binding_tests._rewrite_upstream_paper(
            case.upstream_workspace,
            mutate=lambda payload: payload.setdefault("metadata", {}).update(
                {"tampered": True}
            ),
        )
        case.upstream_registry.reload()
    elif drift == "manifest":
        manifest_path = Path(entry["evidence_manifest_path"])
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["payload"]["created_at"] = "2099-01-01T00:00:00Z"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        case.upstream_registry.reload()
    else:
        leaf = entry["evidence"]["markdown_path"]
        leaf_path = Path(leaf["path"])
        if drift == "leaf_missing":
            leaf_path.unlink()
        else:
            leaf_path.write_text("tampered leaf\n", encoding="utf-8")

    _assert_blocked(
        lambda: _service(case)._paper_authority_inputs(validation_record),
        case.registry,
    )


def test_hybrid_local_and_foreign_authority_has_exact_key_coverage(
    tmp_path: Path,
    upstream_workspace: tuple[Path, dict[str, str]],
) -> None:
    case = _binding_case(tmp_path, upstream_workspace, include_local=True)
    validation_record = _validation_record(case)

    paper_payloads, local_records, selected_binding = _service(case)._paper_authority_inputs(
        validation_record
    )

    assert set(paper_payloads) == {EXTERNAL_KEY, LOCAL_KEY}
    assert set(local_records) == {LOCAL_KEY}
    assert local_records[LOCAL_KEY].artifact_id == case.local_record.artifact_id
    assert paper_payloads[EXTERNAL_KEY]["paper_identity"]["canonical_paper_key"] == EXTERNAL_KEY
    assert paper_payloads[LOCAL_KEY]["paper_identity"]["canonical_paper_key"] == LOCAL_KEY
    assert selected_binding is not None


def test_same_key_local_foreign_authority_conflict_is_rejected(
    tmp_path: Path,
    upstream_workspace: tuple[Path, dict[str, str]],
) -> None:
    case = _binding_case(tmp_path, upstream_workspace)
    _write_local_paper(case.downstream_workspace, case.registry, EXTERNAL_KEY)
    validation_record = _validation_record(case)

    _assert_blocked(
        lambda: _service(case)._paper_authority_inputs(validation_record),
        case.registry,
    )


def test_same_key_local_marker_authority_conflict_is_rejected(
    tmp_path: Path,
    upstream_workspace: tuple[Path, dict[str, str]],
) -> None:
    case = _binding_case(tmp_path, upstream_workspace)
    _register_local_marker_paper(case)
    validation_record = _validation_record(case)

    _assert_blocked(
        lambda: _service(case)._paper_authority_inputs(validation_record),
        case.registry,
    )


def test_same_key_local_summary_projection_merges_foreign_authority(
    tmp_path: Path,
    upstream_workspace: tuple[Path, dict[str, str]],
) -> None:
    case = _binding_case(tmp_path, upstream_workspace)
    local_record = _register_local_summary_projection(case)
    validation_record = _validation_record(case)

    paper_payloads, local_records, selected_binding = _service(case)._paper_authority_inputs(
        validation_record
    )

    assert set(paper_payloads) == {EXTERNAL_KEY}
    assert EXTERNAL_KEY not in local_records
    assert local_record.artifact_id not in {
        record.artifact_id for record in local_records.values()
    }
    assert selected_binding is not None


def test_local_current_authority_binding_matches_and_is_accepted(
    tmp_path: Path,
    upstream_workspace: tuple[Path, dict[str, str]],
) -> None:
    case = _local_self_binding_case(tmp_path, upstream_workspace)
    validation_record = _validation_record(case)

    paper_payloads, local_records, selected_binding = _service(case)._paper_authority_inputs(
        validation_record
    )

    assert set(paper_payloads) == {EXTERNAL_KEY}
    assert local_records[EXTERNAL_KEY].artifact_id == case.local_record.artifact_id
    assert selected_binding is not None
    assert selected_binding.artifact_id == case.binding_record.artifact_id


def test_local_only_authority_preserves_default_without_binding(
    tmp_path: Path,
    upstream_workspace: tuple[Path, dict[str, str]],
) -> None:
    upstream_ws, _hashes = upstream_workspace
    _bridge, session, registry, upstream_registry = source_binding_tests._binding_context(
        tmp_path, [], upstream_ws
    )
    local_record = _write_local_paper(session.context.workspace, registry)
    validation_record = _validation_record(
        SimpleNamespace(
            downstream_workspace=session.context.workspace,
            registry=registry,
            upstream_registry=upstream_registry,
            binding_record=None,
            binding_payload=None,
        ),
        paper_key=LOCAL_KEY,
    )
    before = _registry_snapshot(registry)
    paper_payloads, local_records, selected_binding = RepairTransactionService(
        session.context.workspace, registry
    )._paper_authority_inputs(validation_record)

    assert set(paper_payloads) == {LOCAL_KEY}
    assert set(local_records) == {LOCAL_KEY}
    assert local_records[LOCAL_KEY].artifact_id == local_record.artifact_id
    assert selected_binding is None
    assert _registry_snapshot(registry) == before


def test_promotion_paper_authority_uses_registered_revalidation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The promotion fixture must pass its durable revalidation to authority loading."""

    from tests import test_repair_promotion as promotion_tests

    observations: list[tuple[str | None, tuple[str, ...]]] = []
    original = RepairTransactionService._paper_authority_inputs

    def observe(service: RepairTransactionService, validation_record: Any | None) -> Any:
        registered = tuple(
            sorted(
                record.artifact_id
                for record in service.registry.list_records()
                if record.status == "quarantined"
                and record.artifact_type == "validation_run_result_repaired"
            )
        )
        if registered:
            observations.append(
                (
                    validation_record.artifact_id if validation_record is not None else None,
                    registered,
                )
            )
        return original(service, validation_record)

    monkeypatch.setattr(RepairTransactionService, "_paper_authority_inputs", observe)
    promotion_tests.test_repair_promotion_creates_versioned_outputs_without_replacing_canonical(
        tmp_path
    )

    assert observations
    assert all(
        selected_id is not None and selected_id in repaired_ids
        for selected_id, repaired_ids in observations
    )


def test_promoted_repair_resume_verifies_external_dependency_without_provider_call(
    tmp_path: Path,
    upstream_workspace: tuple[Path, dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recovery reuses promoted validation and verifies its foreign closure."""

    from runtime.runner import AgentRuntimeRunner
    from tests import test_repair_promotion as promotion_tests

    captured: dict[str, Any] = {}
    original_promote = RepairTransactionService.promote

    def capture_promote(service: RepairTransactionService, *args: Any, **kwargs: Any) -> Any:
        captured["service"] = service
        return original_promote(service, *args, **kwargs)

    monkeypatch.setattr(RepairTransactionService, "promote", capture_promote)
    promotion_tests.test_repair_promotion_creates_versioned_outputs_without_replacing_canonical(
        tmp_path
    )

    service = captured["service"]
    workspace = service.workspace
    registry = service.registry
    foreign_source, _hashes = upstream_workspace
    foreign_root = tmp_path / "foreign__up_job"
    shutil.copytree(foreign_source, foreign_root)
    foreign_registry = ArtifactRegistry(
        foreign_root / "artifact_registry.json",
        "up_job",
    )
    foreign_record = foreign_registry.get("paper:deadbeef01")
    assert foreign_record is not None
    current_set = registry.resolve_current_artifact_set()
    assert current_set is not None
    closure_record = registry.get(current_set.validation_receipt_closure_artifact_id)
    assert closure_record is not None
    closure_payload = json.loads(Path(closure_record.path).read_text(encoding="utf-8"))
    assert closure_payload["payload"]["complete"] is True
    if closure_record.metadata.get("complete") is not True:
        registry.update_record(
            closure_record.artifact_id,
            metadata_updates={"complete": True},
        )
    promoted_validation = registry.get(current_set.validation_run_result_artifact_id)
    assert promoted_validation is not None
    external_ref = ArtifactDependencyRefV2(
        dependency_kind="external_job",
        job_id=foreign_registry.job_id,
        artifact_id=foreign_record.artifact_id,
        artifact_type=foreign_record.artifact_type,
        path=foreign_record.path,
        content_hash=foreign_record.content_hash,
    )
    resolver = AgentRuntimeRunner._external_registry_resolver(
        workspace,
        registry_paths=[foreign_registry.registry_path],
    )
    registry.update_record(
        promoted_validation.artifact_id,
        depends_on=[*promoted_validation.depends_on, external_ref],
        external_registry_resolver=resolver,
    )
    seen_jobs: list[str] = []
    base_resolver = resolver

    def observed_resolver(job_id: str) -> ArtifactRegistry | None:
        seen_jobs.append(job_id)
        return base_resolver(job_id)

    session = SimpleNamespace(
        context=SimpleNamespace(
            workspace=workspace,
            registry=registry,
            publication_context=service.publication_context,
        )
    )
    recovered = AgentRuntimeRunner._recover_promoted_repair_validation(
        session,
        external_registry_resolver=observed_resolver,
    )
    assert recovered is not None and recovered.success is True
    assert recovered.metadata["model_call_count_this_attempt"] == 0
    assert "up_job" in seen_jobs

    foreign_record_path = Path(foreign_record.path)
    foreign_record_path.write_bytes(foreign_record_path.read_bytes() + b"\n")
    with pytest.raises(RegistryError):
        AgentRuntimeRunner._recover_promoted_repair_validation(
            session,
            external_registry_resolver=observed_resolver,
        )
