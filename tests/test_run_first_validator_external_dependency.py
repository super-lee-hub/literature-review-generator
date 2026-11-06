"""End-to-end regression for external Stage 1 authority in Validator preflight."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping

import pytest

import test_validation_source_binding as source_binding_tests
from test_current_validator_stage_preflight import (
    _bind_fixture_reuse_miss,
    _citation_result,
    _service,
)
from runtime.orchestrator import AgentRuntimeBridge
from runtime.provider_runtime import hash_json
from services.artifact_registry import ArtifactRegistry, UnverifiedDependency
from services.job_workspace import JobWorkspace, publish_json_artifact, utc_now_iso
from services.queue_service import (
    PersistentQueueService,
    QueueJobSpec,
    QueueState,
)
from services.repair_policy import ValidationRepairPolicy
from validation import current_validation
from validation.adjudication_reuse import _request_payload
from validation.run_result import (
    ClaimValidationResultV1,
    ValidationExecutionStatus,
    ValidationInputArtifactsV1,
    ValidationRunResultV1,
)
from validation.source_binding import (
    build_validation_source_binding,
    validation_source_binding_semantic_hash,
)

pytest_plugins = ("test_validation_source_binding",)


def _service_with_external_binding(
    tmp_path: Path,
    upstream_workspace: tuple[Path, dict[str, str]],
) -> tuple[Any, ArtifactRegistry, dict[str, Any], Any, Any]:
    upstream_ws, source_hashes = upstream_workspace
    upstream_registry = ArtifactRegistry(upstream_ws / "artifact_registry.json", "up_job")
    service = _service(tmp_path)
    resolver = source_binding_tests._upstream_resolver(upstream_registry)
    summary_path = source_binding_tests._summary_file(
        upstream_ws,
        source_hashes["paper_key"],
    )
    binding = build_validation_source_binding(
        summary_sources=[str(summary_path)],
        local_registry=service.artifact_registry,
        job_id=service.job_id,
    )
    bridge = AgentRuntimeBridge.__new__(AgentRuntimeBridge)
    session = SimpleNamespace(
        request=SimpleNamespace(
            summary_file=None,
            summary_sources=(str(summary_path),),
            reuse_summary_files=(),
        ),
        context=SimpleNamespace(
            workspace=service.workspace,
            registry=service.artifact_registry,
            publication_context=service.publication_context,
        ),
        stage_host=SimpleNamespace(),
    )
    binding_record = bridge._ensure_validation_source_binding(
        session,
        service.artifact_registry,
        external_registry_resolver=resolver,
    )
    service.validation_external_registry_resolver = resolver
    service.validation_source_binding_record = binding_record

    review_path = Path(service.review_draft_path)
    review_path.parent.mkdir(parents=True, exist_ok=True)
    review_path.write_text(json.dumps({"sections": []}), encoding="utf-8")
    citation_path = Path(service.citation_manifest_path)
    citation_path.parent.mkdir(parents=True, exist_ok=True)
    citation_path.write_text(
        json.dumps(
            {
                "citation_sets": [{"paper_ids": [source_hashes["paper_key"]]}],
                "occurrences": [],
            }
        ),
        encoding="utf-8",
    )
    return service, upstream_registry, binding, binding_record, source_hashes


def _publish_local_validation_inputs(service: Any, paper_key: str) -> tuple[Any, Any]:
    draft = publish_json_artifact(
        service.publication_context,
        service.artifact_registry,
        service.workspace.artifact_path("validation-test/review-draft.json"),
        {
            "artifact_type": "review_draft",
            "artifact_version": "v3",
            "created_from_job_id": service.job_id,
            "created_at": utc_now_iso(),
            "draft_identity": {"draft_id": "test-draft", "scope": "full_review"},
            "generation_context": {"generation_mode": "test"},
            "content": {"sections": [], "references": []},
            "projections": {},
        },
        artifact_role="review_draft",
        artifact_type="review_draft",
        artifact_version="v3",
        producer="test_run_first_validator_external_dependency",
        artifact_id="review_draft",
    )
    citation = publish_json_artifact(
        service.publication_context,
        service.artifact_registry,
        service.workspace.artifact_path("validation-test/citation-manifest.json"),
        {
            "artifact_type": "citation_manifest",
            "artifact_version": "v3",
            "created_from_job_id": service.job_id,
            "created_at": utc_now_iso(),
            "manifest_identity": {"manifest_id": "test-manifest"},
            "review_reference": {"review_draft_id": draft.artifact_id},
            "citation_sets": [{"paper_ids": [paper_key]}],
            "occurrences": [],
            "bibliography": [],
        },
        artifact_role="citation_manifest",
        artifact_type="citation_manifest",
        artifact_version="v3",
        producer="test_run_first_validator_external_dependency",
        artifact_id="citation_manifest_v3",
        depends_on=[
            {
                "dependency_kind": "local_job",
                "job_id": service.job_id,
                "artifact_id": draft.artifact_id,
                "artifact_type": draft.artifact_type,
                "path": draft.path,
                "content_hash": draft.content_hash,
            }
        ],
    )
    service.review_draft_record = draft
    service.citation_manifest_record = citation
    return draft, citation


def test_external_typed_source_flows_through_validator_inventory_and_result_closure(
    tmp_path: Path,
    upstream_workspace: tuple[Path, dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, upstream_registry, binding, binding_record, source_hashes = (
        _service_with_external_binding(tmp_path, upstream_workspace)
    )
    resolver = service.validation_external_registry_resolver
    _bind_fixture_reuse_miss(service, monkeypatch)

    _review, _citation, papers, _preprocess, _metadata = current_validation._load_inputs(
        service
    )
    assert not service._validation_source_authority_diagnostics, (
        service._validation_source_authority_diagnostics
    )
    assert [
        item["paper_identity"]["canonical_paper_key"]
        for item in papers
    ] == [source_hashes["paper_key"]]
    assert papers[0]["_validation_source_binding"]["source_workspace_job_id"] == "up_job"

    captured: list[tuple[Any, Mapping[str, Any]]] = []

    def capture_handoff(
        observed_service: Any,
        api_config: Mapping[str, Any],
        packet: Any,
    ) -> Mapping[str, Any]:
        inventory_record = observed_service._validator_stage_pretransport_inventory_record
        assert inventory_record.status == "ready"
        assert any(
            dependency.artifact_id == binding_record.artifact_id
            for dependency in inventory_record.depends_on
        )
        observed_service.artifact_registry.verify_ready_artifact_closure(
            inventory_record,
            external_registry_resolver=resolver,
        )
        persisted = json.loads(Path(inventory_record.path).read_text(encoding="utf-8"))
        assert persisted["provider_posts_emitted_at_plan"] == 0
        assert persisted["request_count"] == 1
        assert list(packet.paper_ids) == [source_hashes["paper_key"]]
        request_payload = _request_payload(packet, api_config)
        captured.append((packet, request_payload))
        return {"status": "supported", "confidence": 0.99}

    monkeypatch.setattr(current_validation, "run_adjudication_stage", capture_handoff)
    citation_result = _citation_result(
        "external-set",
        source_hashes["paper_key"],
        "A bounded claim tied to an upstream paper.",
    )
    current_validation._adjudicate(service, [citation_result])

    assert len(captured) == 1
    packet, request_payload = captured[0]
    inventory = service._validator_stage_pretransport_inventory
    assert inventory["request_count"] == 1
    assert inventory["requests"][0]["request_hash"] == hash_json(request_payload)
    assert packet.per_paper_evidence_packets[source_hashes["paper_key"]]

    # The canonical result and its projections form the next local publication
    # chain. Its closure must still reach and verify the upstream source.
    draft_record, citation_record = _publish_local_validation_inputs(
        service,
        source_hashes["paper_key"],
    )
    manifest_id = str(binding["papers"][source_hashes["paper_key"]]["evidence_manifest_artifact_id"])
    manifest_hash = str(binding["papers"][source_hashes["paper_key"]]["evidence_manifest_hash"])
    claim = ClaimValidationResultV1.from_validation_result(citation_result)
    result = ValidationRunResultV1.create(
        job_id=service.job_id,
        attempt_id=service.attempt_id,
        execution_status=ValidationExecutionStatus.SUCCEEDED,
        report_id="validation-report:external-source-test",
        validation_run_id="validation-run:external-source-test",
        claim_results=[claim],
        expected_claim_count=1,
        validated_claim_count=1,
        review_has_citations=True,
        evidence_complete=True,
        input_artifacts=ValidationInputArtifactsV1(
            review_draft_id=draft_record.artifact_id,
            review_draft_hash=draft_record.content_hash,
            citation_manifest_id=citation_record.artifact_id,
            citation_manifest_hash=citation_record.content_hash,
            evidence_manifest_ids=(manifest_id,),
            evidence_manifest_hashes=(manifest_hash,),
            validation_source_binding_id=binding_record.artifact_id,
            validation_source_binding_hash=validation_source_binding_semantic_hash(binding),
            validation_source_binding_semantic_hash=validation_source_binding_semantic_hash(binding),
            validation_source_binding_content_hash=binding_record.content_hash,
        ),
    )
    assert result.contract_satisfied
    paths = current_validation._write_reports(
        service,
        result,
        ValidationRepairPolicy.REPORT_ONLY,
    )
    completion = next(
        record
        for record in service.artifact_registry.list_records()
        if record.artifact_type == "validation_completion_projection"
        and Path(record.path).resolve() == Path(paths["completion_report_file"]).resolve()
    )
    assert completion.status == "ready"
    service.artifact_registry.verify_ready_artifact_closure(
        completion,
        external_registry_resolver=resolver,
    )
    assert upstream_registry.get(
        str(binding["papers"][source_hashes["paper_key"]]["stage1_paper_artifact_id"])
    ) is not None


@pytest.mark.parametrize("failure", ["missing_resolver", "foreign_owner", "hash_drift"])
def test_external_typed_source_rejects_bad_authority_before_validator_handoff(
    tmp_path: Path,
    upstream_workspace: tuple[Path, dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    service, upstream_registry, binding, _binding_record, source_hashes = (
        _service_with_external_binding(tmp_path, upstream_workspace)
    )
    captured: list[Any] = []
    monkeypatch.setattr(
        current_validation,
        "run_adjudication_stage",
        lambda _service, _api_config, packet: captured.append(packet),
    )
    _bind_fixture_reuse_miss(service, monkeypatch)

    external_entry = binding["papers"][source_hashes["paper_key"]]
    if failure == "missing_resolver":
        service.validation_external_registry_resolver = None
        expected_error = "external dependency cannot be verified without a resolver"
    elif failure == "foreign_owner":
        foreign_workspace = JobWorkspace.create(
            str(tmp_path / "foreign-output"),
            "foreign",
            job_id="foreign_job",
        )
        foreign_registry = ArtifactRegistry(
            foreign_workspace.paths.registry_path,
            "foreign_job",
        )
        service.validation_external_registry_resolver = lambda _job_id: foreign_registry
        expected_error = "external dependency Registry owner mismatch"
    else:
        paper_record = upstream_registry.get(str(external_entry["stage1_paper_artifact_id"]))
        assert paper_record is not None
        Path(paper_record.path).write_bytes(Path(paper_record.path).read_bytes() + b"\n")
        expected_error = "dependency content hash changed"

    with pytest.raises(UnverifiedDependency, match=expected_error):
        current_validation._adjudicate(
            service,
            [
                _citation_result(
                    "external-set",
                    source_hashes["paper_key"],
                    "A bounded claim tied to an upstream paper.",
                )
            ],
        )
    assert captured == []
    assert getattr(service, "_validator_stage_pretransport_inventory_record", None) is None


def _claimed_queue_binding_session(
    tmp_path: Path,
    summary_path: Path,
) -> tuple[Any, PersistentQueueService, Any, Any, Any]:
    service = _service(tmp_path)
    queue = PersistentQueueService(tmp_path / "queue" / "queue.json")
    queue.add_job(
        QueueJobSpec(
            job_id=service.job_id,
            job_type="validation",
            project_name=service.workspace.project_name,
        )
    )
    lease = queue.claim_job(
        service.job_id,
        worker_id="validator-source-binding-test",
        lease_seconds=60,
    )
    assert lease is not None
    publication_context = queue.publication_context(lease)
    registry = publication_context.registry(
        service.workspace.paths.registry_path,
        service.job_id,
    )
    service.artifact_registry = registry
    service.publication_context = publication_context
    session = SimpleNamespace(
        request=SimpleNamespace(
            summary_file=None,
            summary_sources=(str(summary_path),),
            reuse_summary_files=(),
        ),
        context=SimpleNamespace(
            workspace=service.workspace,
            registry=registry,
            publication_context=publication_context,
        ),
        stage_host=SimpleNamespace(),
    )
    return service, queue, lease, registry, session


@pytest.mark.parametrize(
    ("failure", "expected_error"),
    (
        (None, None),
        ("missing_resolver", "external dependency cannot be verified without a resolver"),
        ("foreign_owner", "external dependency Registry owner mismatch"),
        ("hash_drift", "dependency content hash changed"),
    ),
)
def test_claimed_queue_publication_verifies_external_binding_dependencies(
    tmp_path: Path,
    upstream_workspace: tuple[Path, dict[str, str]],
    failure: str | None,
    expected_error: str | None,
) -> None:
    upstream_ws, source_hashes = upstream_workspace
    upstream_registry = ArtifactRegistry(upstream_ws / "artifact_registry.json", "up_job")
    summary_path = source_binding_tests._summary_file(
        upstream_ws,
        source_hashes["paper_key"],
    )
    service, queue, lease, registry, session = _claimed_queue_binding_session(
        tmp_path,
        summary_path,
    )
    resolver = source_binding_tests._upstream_resolver(upstream_registry)

    if failure == "missing_resolver":
        active_resolver = None
    elif failure == "foreign_owner":
        foreign_workspace = JobWorkspace.create(
            str(tmp_path / "foreign-output"),
            "foreign",
            job_id="foreign_job",
        )
        foreign_registry = ArtifactRegistry(
            foreign_workspace.paths.registry_path,
            "foreign_job",
        )
        active_resolver = lambda _job_id: foreign_registry
    else:
        active_resolver = resolver

    bridge = AgentRuntimeBridge.__new__(AgentRuntimeBridge)
    try:
        if failure in (None, "hash_drift"):
            binding_record = bridge._ensure_validation_source_binding(
                session,
                registry,
                external_registry_resolver=active_resolver,
            )
            assert binding_record is not None
            assert {
                (dependency.job_id, dependency.artifact_type)
                for dependency in binding_record.depends_on
            } == {
                ("up_job", "paper_artifact"),
                ("up_job", "evidence_manifest"),
            }
            if failure == "hash_drift":
                paper_record = upstream_registry.get("paper:deadbeef01")
                assert paper_record is not None
                Path(paper_record.path).write_bytes(
                    Path(paper_record.path).read_bytes() + b"\nsource-bytes-drift\n"
                )
                assert expected_error is not None
                with pytest.raises(UnverifiedDependency, match=expected_error):
                    registry.verify_ready_artifact_closure(
                        binding_record,
                        external_registry_resolver=resolver,
                    )
            else:
                registry.verify_ready_artifact_closure(
                    binding_record,
                    external_registry_resolver=resolver,
                )
        else:
            assert expected_error is not None
            with pytest.raises(UnverifiedDependency, match=expected_error):
                bridge._ensure_validation_source_binding(
                    session,
                    registry,
                    external_registry_resolver=active_resolver,
                )
            assert not any(
                record.artifact_type == "validation_source_binding"
                for record in registry.list_records()
            )
    finally:
        assert queue.release_lease(
            service.job_id,
            lease_id=lease.lease_id,
            worker_id=lease.worker_id,
            lease_generation=lease.lease_generation,
            fence_token=lease.fence_token,
            state=QueueState.COMPLETED if failure is None else QueueState.FAILED,
        )
