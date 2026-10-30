from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from outline.topic_source_scope import stable_topic_batch_identity_v2
from outline.v3_artifacts import OutlineArtifact
from runtime.job_spec import RuntimeJobSpec, RuntimeSourceSpec
from runtime.provider_receipt_closure import (
    ExpectedProviderCall,
    ProviderReceiptClosure,
)
from runtime.provider_response_snapshot import (
    ProviderResponseSnapshotStore,
)
from runtime.provider_routes import _safe_host
from runtime.provider_runtime import (
    ProviderAggregateBudgetV2,
    ProviderBudgetController,
    ProviderBudgetV1,
    ProviderRuntime,
    ProviderRuntimeLedger,
    hash_json,
    hash_text,
)
from runtime.source_revision import (
    PROVIDER_SEMANTIC_CONTRACT_V2,
    SOURCE_REVISION_PLAN_REF_VERSION,
    SourceRevisionPlanRefV1,
    child_core_spec_hash,
)
from runtime.source_revision_replay import (
    SOURCE_REVISION_REPLAY_SCOPE_VERSION,
    SourceRevisionReplayContext,
    SourceRevisionReplayError,
    build_outline_source_revision_scope_v1,
    build_writer_source_revision_scope_v1,
)
from services.artifact_registry import (
    ArtifactDependencyRefV2,
    ArtifactRecord,
    ArtifactRegistry,
    UnverifiedArtifact,
    file_sha256,
)
from services.job_workspace import (
    JobWorkspace,
    publish_bytes_artifact,
    publish_json_artifact,
)
from services.queue_service import LocalPublicationContext
from services.writer_task_scope import _selected_source_binding, _selected_task_basis
from tests.test_topic_source_scope import (
    _request as _topic_request,
)
from tests.test_topic_source_scope import (
    _successor_wire as _topic_successor_wire,
)
from tests.test_topic_source_scope import (
    source_case as _topic_source_case,
)
from tests.test_writer_selected_source_contract import (
    _case as _writer_case,
)
from tests.test_writer_selected_source_contract import (
    _scope as _writer_scope,
)
from tests.test_writer_selected_source_contract import (
    _wire as _writer_wire,
)

_API = {
    "provider_family": "openai_chat",
    "model": "source-revision-unit-model",
    "api_base": "http://127.0.0.1:1/v1",
    "api_key": "local-test-key",
    "endpoint_type": "chat_completions",
}


def _scope(keys: list[str], *, content: str = "selected-source", non_source: str = "non-source") -> dict[str, Any]:
    return {
        "schema_version": SOURCE_REVISION_REPLAY_SCOPE_VERSION,
        "canonical_paper_keys": sorted(keys),
        "selected_source_content_hash": hash_json({"selected": content}),
        "non_source_scope_hash": hash_json({"non_source": non_source}),
    }


def _publish_json(
    context: LocalPublicationContext,
    registry: ArtifactRegistry,
    workspace: JobWorkspace,
    relative: str,
    payload: Any,
    *,
    artifact_id: str,
    artifact_type: str,
    artifact_role: str | None = None,
    artifact_version: str = "v1",
    depends_on: tuple[ArtifactDependencyRefV2, ...] = (),
    external_registry_resolver=None,
    metadata: dict[str, Any] | None = None,
) -> ArtifactRecord:
    return publish_json_artifact(
        context,
        registry,
        workspace.artifact_path(relative),
        payload,
        artifact_id=artifact_id,
        artifact_role=artifact_role or artifact_type,
        artifact_type=artifact_type,
        artifact_version=artifact_version,
        producer=__name__,
        depends_on=list(depends_on),
        external_registry_resolver=external_registry_resolver,
        metadata=dict(metadata or {}),
    )


def _record_ref(record: ArtifactRecord) -> dict[str, str]:
    return {
        "job_id": record.job_id,
        "artifact_id": record.artifact_id,
        "artifact_type": record.artifact_type,
        "artifact_version": record.artifact_version,
        "artifact_role": record.artifact_role,
        "path": record.path,
        "content_hash": record.content_hash,
    }


def _outline_artifact(job_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    return OutlineArtifact(
        job_id=job_id,
        dependency_hashes={},
        payload=dict(payload),
    ).to_dict()


def _provider_receipt(
    workspace: JobWorkspace,
    registry: ArtifactRegistry,
    context: LocalPublicationContext,
    *,
    stage_name: str,
    route: str,
    config_section: str,
    node_id: str,
    call_id: str,
    request_payload: Mapping[str, Any],
    response: Any,
) -> tuple[Any, ArtifactRecord, ProviderBudgetController]:
    ledger = ProviderRuntimeLedger(workspace.log_path("source_revision_provider_receipts.jsonl"))
    budget = ProviderBudgetController(ProviderAggregateBudgetV2(
        max_provider_calls_total=2,
        max_output_tokens_total=1_000,
        max_retry_attempts_total=0,
        max_wall_seconds=60,
    ))
    schema_hash = hash_text("source-revision-test-schema")
    runtime = ProviderRuntime(
        budget=ProviderBudgetV1(max_calls=2, max_total_tokens=1_000),
        aggregate_budget=budget,
        ledger=ledger,
        job_id=workspace.job_id,
        attempt_id="parent-attempt",
        stage_name=stage_name,
        route=route,
        schema_hash=schema_hash,
        node_id=node_id,
        call_id=call_id,
        closure_epoch_id="parent-closure-epoch",
        logical_attempt_identity="parent-logical-attempt",
        endpoint_type=_API["endpoint_type"],
    )
    admission = runtime.admit(estimated_tokens=5, requested_output_tokens=20)
    receipt = runtime.complete(
        admission=admission,
        prompt="source-revision attached prompt",
        input_payload=request_payload,
        api_config=dict(_API),
        result={
            "status": "success",
            "content": response,
            "finish_reason": "stop",
            "input_tokens": 5,
            "output_tokens": 4,
            "total_tokens": 9,
        },
        metadata={
            "config_section": config_section,
            "transport_config": dict(_API),
        },
    )
    ledger_record = publish_bytes_artifact(
        context,
        registry,
        workspace.artifact_path("provider_receipts/source_revision_parent.jsonl"),
        Path(ledger.path).read_bytes(),
        artifact_id="review_provider_receipts" if stage_name == "stage3_review" else "outline_v3_provider_receipts",
        artifact_role="provider_receipts",
        artifact_type="provider_receipt_ledger",
        artifact_version="v1",
        producer=__name__,
        metadata={"stage_name": stage_name, "closure_epoch_id": receipt.closure_epoch_id},
    )
    return receipt, ledger_record, budget


def _make_case(
    tmp_path: Path,
    *,
    stage: str = "outline_v3",
    current_scope: Mapping[str, Any] | None = None,
    request_payload: Mapping[str, Any] | None = None,
    missing_parent_scope: bool = False,
    include_outline_scope_ref: bool = True,
    omit_parent_plan_dependency: bool = False,
    create_replay_context: bool = True,
    outline_receipt_route_override: str | None = None,
    outline_receipt_config_section_override: str | None = None,
) -> dict[str, Any]:
    output_root = tmp_path / "output"
    parent_ws = JobWorkspace.create(str(output_root), "revision-parent", "parent-job")
    child_ws = JobWorkspace.create(str(output_root), "revision-child", "child-job")
    parent_publication = LocalPublicationContext()
    child_publication = LocalPublicationContext()
    parent_registry = parent_publication.registry(parent_ws.paths.registry_path, parent_ws.job_id)
    child_registry = child_publication.registry(child_ws.paths.registry_path, child_ws.job_id)

    if stage == "stage3_review":
        if current_scope is None:
            parent_source_case = _writer_case(tmp_path / "writer-parent")
            child_source_case = _writer_case(tmp_path / "writer-child", changed=True)
            parent_writer_scope = _writer_scope(parent_source_case)
            child_writer_scope = _writer_scope(child_source_case)
            parent_binding = {
                "binding_version": "review-section-binding-v1",
                "stage_name": stage,
                "section_id": "section-U45",
                "final_outline_hash": hash_json({"outline": "parent"}),
                "adoption_artifact_id": "outline-v3:adoption:parent",
                "adoption_artifact_hash": hash_json({"adoption": "parent"}),
                "outline_section_hash": hash_json({"section": "U45"}),
                "evidence_packet_hash": hash_json({"packet": "U45"}),
                "citation_catalog_hash": parent_source_case[1]["catalog_hash"],
                "writer_provider": _API["provider_family"],
                "writer_model": _API["model"],
                "writer_endpoint": _API["endpoint_type"],
                "writer_config_hash": hash_json(_API),
                "system_prompt_hash": hash_text("writer-system"),
                "prompt_id": "writer.v3",
                "prompt_version": "3",
                "prompt_sha256": hash_text("writer-template"),
                "prompt_template_hash": hash_text("writer-template"),
                "prompt_hash": hash_text("source-revision attached prompt"),
                "prompt_payload_hash": "",
                "output_schema_hash": hash_text("writer-schema"),
                "context_profile_hash": hash_json({"profile": "writer"}),
                "application_schema_version": "test-schema",
                "provider_semantic_contract_version": "source-closed-provider/v2",
                # Deliberately job-local and not part of the selected-source wire.
                "source_inventory_authority": {"job_id": "parent-only"},
            }
            child_binding = dict(parent_binding)
            child_binding["source_inventory_authority"] = {"job_id": "child-only"}
            child_binding["final_outline_hash"] = hash_json({"outline": "child"})
            child_binding["adoption_artifact_id"] = "outline-v3:adoption:child"
            child_binding["adoption_artifact_hash"] = hash_json({"adoption": "child"})
            child_binding["citation_catalog_hash"] = hash_json({"unselected": "child catalog"})
            parent_request = {
                "system": "writer system",
                "user": json.dumps(_writer_wire(parent_writer_scope), ensure_ascii=False, sort_keys=True),
            }
            child_request = {
                "system": "writer system",
                "user": json.dumps(_writer_wire(child_writer_scope), ensure_ascii=False, sort_keys=True),
            }
            parent_request_hash = hash_json(parent_request)
            assert hash_json(child_request) == parent_request_hash
            parent_binding["prompt_payload_hash"] = parent_request_hash
            child_binding["prompt_payload_hash"] = parent_request_hash
            parent_scope_data = build_writer_source_revision_scope_v1(parent_writer_scope, parent_binding)
            child_scope_data = build_writer_source_revision_scope_v1(child_writer_scope, child_binding)
            # The selected task scope must be the same across the unrelated A9 change.
            assert parent_scope_data == child_scope_data
            current_scope = child_scope_data
            request_payload = child_request
            selected_scope_record_payload = None
            writer_scope_for_output = parent_writer_scope
            writer_binding_for_output = parent_binding
        else:
            parent_source_case = _writer_case(tmp_path / "writer-parent")
            child_source_case = _writer_case(tmp_path / "writer-child", changed=True)
            parent_writer_scope = _writer_scope(parent_source_case)
            child_writer_scope = _writer_scope(child_source_case)
            parent_binding = {
                "binding_version": "review-section-binding-v1",
                "stage_name": stage,
                "section_id": "section-U45",
                "outline_section_hash": hash_json({"section": "U45"}),
                "evidence_packet_hash": hash_json({"packet": "U45"}),
                "citation_catalog_hash": parent_source_case[1]["catalog_hash"],
                "writer_provider": _API["provider_family"],
                "writer_model": _API["model"],
                "writer_endpoint": _API["endpoint_type"],
                "writer_config_hash": hash_json(_API),
                "system_prompt_hash": hash_text("writer-system"),
                "prompt_id": "writer.v3",
                "prompt_version": "3",
                "prompt_sha256": hash_text("writer-template"),
                "prompt_template_hash": hash_text("writer-template"),
                "prompt_hash": hash_text("writer-user"),
                "prompt_payload_hash": "0" * 64,
                "output_schema_hash": hash_text("writer-schema"),
                "context_profile_hash": hash_json({"profile": "writer"}),
                "application_schema_version": "test-schema",
                "provider_semantic_contract_version": "source-closed-provider/v2",
            }
            current_scope = build_writer_source_revision_scope_v1(child_writer_scope, parent_binding)
            request_payload = {"system": "writer system", "user": "U45 canonical selected source wire"}
            selected_scope_record_payload = None
            writer_scope_for_output = parent_writer_scope
            writer_binding_for_output = parent_binding
    else:
        source_versions = _topic_source_case.__wrapped__(tmp_path / "outline-scope")
        parent_request = _topic_request(source_versions[0])
        child_request = _topic_request(source_versions[1], ordinal=99)
        parent_wire = _topic_successor_wire(source_versions[0], parent_request)
        child_wire = _topic_successor_wire(source_versions[1], child_request)
        assert parent_wire == child_wire
        request_payload = dict(request_payload or child_wire)
        current_scope = dict(current_scope or build_outline_source_revision_scope_v1(request_payload))
        selected_keys = list(current_scope["canonical_paper_keys"])
        selected_scope_record_payload = {
            "schema_version": "outline_request_payload_audit/v1",
            "job_id": parent_ws.job_id,
            "stage_name": "outline_v3",
            "operation_id": "parent-logical-attempt",
            "attempt_id": "",
            "physical_attempt_id": "parent-physical-attempt",
            "closure_epoch_id": "parent-closure-epoch",
            "node_id": str(request_payload["node_id"]),
            "semantic_node_id": str(request_payload["node_id"]),
            "payload_hash": "",
            "route": {
                "config_section": "Outline_API",
                "provider": _API["provider_family"],
                "model": _API["model"],
                "endpoint_type": _API["endpoint_type"],
                "api_base_host": _API["api_base"],
            },
            "route_fingerprint": hash_json({"route": "Outline_API"}),
            "config_hash": "",
            "schema_hash": "",
            "serialized_bytes": len(json.dumps(request_payload).encode("utf-8")),
            "estimated_input_tokens": 10,
            "input_cap": 10_000,
            "output_cap": 1_000,
            "mock_live": "live",
            "status": "success",
            "shard_ids": [],
            "paper_keys": selected_keys,
            "evidence_view_hashes": [],
            "relation_candidate_ids": [],
            "receipt_ids": [],
            "artifact_refs": [],
            "source_revision_scope": dict(current_scope),
        }
        if missing_parent_scope:
            selected_scope_record_payload.pop("source_revision_scope")
        writer_scope_for_output = None
        writer_binding_for_output = None

    if stage == "stage3_review":
        route = "Writer_API"
        receipt_route = route
        config_section = route
        node_id = "section-U45"
        call_id = "review:section-U45"
        output_response = {
            "blocks": [{"text": "The reported effect retains its stated condition."}],
            "task_dispositions": [{"disposition": "covered"}],
        }
    else:
        route = "Outline_API"
        node_id = str(request_payload["node_id"])
        provider_node_id = str(
            stable_topic_batch_identity_v2(request_payload)["provider_node_id"]
        )
        call_id = f"outline:{provider_node_id}"
        receipt_route = outline_receipt_route_override or provider_node_id
        config_section = outline_receipt_config_section_override or route
        assert selected_scope_record_payload is not None
        selected_scope_record_payload["node_id"] = provider_node_id
        selected_scope_record_payload["semantic_node_id"] = provider_node_id
        output_response = {
            "topics": [{"paper_keys": list(current_scope["canonical_paper_keys"]), "status": "completed"}],
        }

    receipt, ledger_record, parent_budget = _provider_receipt(
        parent_ws,
        parent_registry,
        parent_publication,
        stage_name=stage,
        route=receipt_route,
        config_section=config_section,
        node_id=node_id,
        call_id=call_id,
        request_payload=request_payload,
        response=output_response,
    )
    response_store = ProviderResponseSnapshotStore(parent_ws, parent_registry, parent_publication)
    response_snapshot_record = response_store.capture(receipt, output_response)
    logical_output_hash = hash_json(output_response)

    if stage == "stage3_review":
        assert writer_scope_for_output is not None and writer_binding_for_output is not None
        writer_binding_for_output["prompt_payload_hash"] = receipt.input_hash
        writer_binding_for_output["prompt_hash"] = receipt.prompt_hash
        writer_binding_for_output["writer_config_hash"] = hash_json(_API)
        writer_section = {
            **output_response,
            "provider_receipt_ids": [receipt.receipt_id],
            "writer_task_scope": writer_scope_for_output,
            "provider_semantic_contract_version": "source-closed-provider/v2",
        }
        writer_section_hash = hash_json(writer_section)
        section_payload = {
            "artifact_type": "review_section",
            "artifact_version": "v3",
            "status": "ready",
            "job_id": parent_ws.job_id,
            "section_id": node_id,
            "binding_hash": hash_json(writer_binding_for_output),
            "binding": dict(writer_binding_for_output),
            "content_hash": writer_section_hash,
            "section": writer_section,
        }
        output_record = _publish_json(
            parent_publication, parent_registry, parent_ws, "review/section.json",
            section_payload, artifact_id="review-section:section-U45:test",
            artifact_type="review_section", artifact_role="review_section", artifact_version="v3",
        )
        scope_record = output_record
        receipt_snapshot_payload = {
            "artifact_type": "review_provider_receipt_snapshot",
            "artifact_version": "v1",
            "status": "partial",
            "job_id": parent_ws.job_id,
            "stage_name": stage,
            "section_id": node_id,
            "binding_hash": section_payload["binding_hash"],
            "closure_epoch_id": receipt.closure_epoch_id,
            "logical_attempt_identity": receipt.logical_attempt_identity,
            "call_id": receipt.call_id,
            "expected_call_graph_hash": hash_json({"call": call_id}),
            "receipt_id": receipt.receipt_id,
            "receipt_hash": hash_json(receipt.to_dict()),
            "receipt": receipt.to_dict(),
        }
        review_receipt_snapshot = _publish_json(
            parent_publication, parent_registry, parent_ws,
            "review/receipt_snapshot.json", receipt_snapshot_payload,
            artifact_id=(
                "review:receipt-snapshot:section-U45:"
                f"{section_payload['binding_hash'][:24]}:{hash_json(receipt.to_dict())[:24]}"
            ),
            artifact_type="review_provider_receipt_snapshot",
            artifact_role="provider_receipt_snapshot",
        )
        replay_row = {
            "replay_version": "review-section-replay-v1",
            "job_id": parent_ws.job_id,
            "stage_name": stage,
            "closure_epoch_id": receipt.closure_epoch_id,
            "logical_attempt_identity": receipt.logical_attempt_identity,
            "expected_call_graph_hash": receipt_snapshot_payload["expected_call_graph_hash"],
            "section_id": node_id,
            "binding_hash": section_payload["binding_hash"],
            "artifact_id": output_record.artifact_id,
            "artifact_path": output_record.path,
            "artifact_content_hash": writer_section_hash,
            "registry_file_hash": output_record.content_hash,
            "receipt_id": receipt.receipt_id,
            "receipt_hash": hash_json(receipt.to_dict()),
            "normalized_output_hash": receipt.response_hash,
            "receipt_snapshot_artifact_id": review_receipt_snapshot.artifact_id,
            "receipt_snapshot_path": review_receipt_snapshot.path,
            "receipt_snapshot_content_hash": review_receipt_snapshot.content_hash,
        }
        replay_record = publish_bytes_artifact(
            parent_publication,
            parent_registry,
            parent_ws.artifact_path("review/review_replay.jsonl"),
            (json.dumps(replay_row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8"),
            artifact_id="review_replay",
            artifact_role="review_replay",
            artifact_type="review_replay_ledger",
            artifact_version="v1",
            producer=__name__,
            depends_on=[
                ArtifactDependencyRefV2.from_record(output_record),
                ArtifactDependencyRefV2.from_record(review_receipt_snapshot),
            ],
        )
        lineage_records = (replay_record, review_receipt_snapshot)
    else:
        assert selected_scope_record_payload is not None
        selected_scope_record_payload.update({
            "attempt_id": call_id,
            "payload_hash": receipt.input_hash,
            "config_hash": receipt.config_hash,
            "schema_hash": receipt.schema_hash,
            "closure_epoch_id": receipt.closure_epoch_id,
            "operation_id": receipt.logical_attempt_identity,
            "receipt_ids": [receipt.receipt_id],
        })
        scope_record = publish_bytes_artifact(
            parent_publication,
            parent_registry,
            parent_ws.artifact_path("outline/request_payload_audit.jsonl"),
            (json.dumps(selected_scope_record_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8"),
            artifact_id="outline-v3:request-payload-audit:test",
            artifact_role="outline_request_payload_audit",
            artifact_type="outline_request_payload_audit",
            artifact_version="v1",
            producer=__name__,
        )
        lineage_records = ()
        outline_document = _outline_artifact(parent_ws.job_id, output_response)
        output_record = _publish_json(
            parent_publication, parent_registry, parent_ws, "outline/output.json",
            outline_document,
            artifact_id="outline-v3:topic-output:test",
            artifact_type="outline_artifact",
            artifact_role="outline_artifact",
            artifact_version="v3",
        )

    output_content_hash = writer_section_hash if stage == "stage3_review" else str(outline_document["content_hash"])
    output_payload_hash = writer_section_hash if stage == "stage3_review" else logical_output_hash
    expected = ExpectedProviderCall(
        call_id=call_id,
        job_id=parent_ws.job_id,
        attempt_id=receipt.attempt_id,
        stage_name=stage,
        node_id=node_id,
        closure_epoch_id=receipt.closure_epoch_id,
        logical_attempt_identity=receipt.logical_attempt_identity,
        expected_call_graph_hash=hash_json({"call": call_id}),
        prompt_hash=receipt.prompt_hash,
        input_hash=receipt.input_hash,
        config_hash=receipt.config_hash,
        schema_hash=receipt.schema_hash,
        output_hash=receipt.response_hash or "",
        provider_response_hash=receipt.response_hash or "",
        normalized_output_hash=receipt.response_hash or "",
        artifact_payload_hash=output_payload_hash,
        artifact_content_hash=output_content_hash,
        registry_file_hash=output_record.content_hash,
        artifact_path=output_record.path,
        registered_artifact_hash=output_content_hash,
        replay_output_hash=receipt.response_hash or "",
        node_output_hash=output_content_hash,
        max_attempts=1,
        usage_required=False,
        verified_reuse=False,
        provider=receipt.provider,
        model=receipt.model,
        endpoint=receipt.endpoint,
        endpoint_type=receipt.endpoint_type,
        prompt_id=receipt.prompt_id,
        prompt_version=receipt.prompt_version,
        prompt_sha256=receipt.prompt_sha256,
    )
    closure_payload = {
        **ProviderReceiptClosure.evaluate([expected], [receipt]).to_dict(),
        "job_id": parent_ws.job_id,
        "stage_name": "stage2_outline" if stage == "outline_v3" else stage,
        "status": "ready",
        "attempt_id": receipt.attempt_id,
        "logical_attempt_identity": receipt.logical_attempt_identity,
        "expected_call_graph_hash": expected.expected_call_graph_hash,
        "expected_calls": [asdict(expected)],
        "observed_receipt_ids": [receipt.receipt_id],
    }
    closure_stage = "stage2_outline" if stage == "outline_v3" else stage
    closure_record = _publish_json(
        parent_publication,
        parent_registry,
        parent_ws,
        "provider_receipt_closure.json",
        {
            "artifact_type": "provider_receipt_closure",
            "artifact_version": "v1",
            "job_id": parent_ws.job_id,
            "stage_name": closure_stage,
            "status": "ready",
            "payload": closure_payload,
        },
        artifact_id=f"provider-receipt-closure:{stage}:test",
        artifact_type="provider_receipt_closure",
        depends_on=tuple(
            ArtifactDependencyRefV2.from_record(record)
            for record in (ledger_record, output_record, *lineage_records)
        ),
    )
    current_set = _publish_json(
        parent_publication,
        parent_registry,
        parent_ws,
        "current_artifact_set.json",
        {
            "artifact_type": "current_artifact_set",
            "artifact_version": "v1",
            "set_id": "parent-current-set",
            "job_id": parent_ws.job_id,
            "promotion_transaction_id": "parent-promotion",
            "promotion_transaction_hash": hash_json({"promotion": "parent"}),
            "review_draft_artifact_id": "review-draft",
            "review_draft_artifact_hash": hash_json({"review-draft": "parent"}),
            "citation_manifest_artifact_id": "citation-manifest",
            "citation_manifest_artifact_hash": hash_json({"citation-manifest": "parent"}),
            "review_docx_artifact_id": "review-docx",
            "review_docx_artifact_hash": hash_json({"review-docx": "parent"}),
            "validation_receipt_closure_artifact_id": "validation-closure",
            "validation_receipt_closure_artifact_hash": hash_json({"validation-closure": "parent"}),
            "validation_status": "clean",
            "validation_run_result_artifact_id": "validation-run-result",
            "validation_run_result_artifact_hash": hash_json({"validation-run-result": "parent"}),
            "actor": "test-fixture",
            "reason": "bounded source revision replay contract fixture",
            "created_at": "2026-10-27T00:00:00Z",
        },
        artifact_id="current_artifact_set:test",
        artifact_type="current_artifact_set",
        depends_on=(
            ArtifactDependencyRefV2.from_record(closure_record),
            ArtifactDependencyRefV2.from_record(output_record),
        ),
    )

    keys = [f"paper-{index}" for index in range(10)] if stage == "stage3_review" else sorted(
        set(current_scope["canonical_paper_keys"]) | {"paper-9"}
    )
    old_source_dir = tmp_path / "old-pdfs"
    new_source_dir = tmp_path / "new-pdfs"
    old_source_dir.mkdir(exist_ok=True)
    new_source_dir.mkdir(exist_ok=True)
    old_rows, new_rows = [], []
    for key in keys:
        old_path = old_source_dir / f"{key}.pdf"
        new_path = new_source_dir / f"{key}.pdf"
        old_path.write_bytes(f"old:{key}".encode())
        new_path.write_bytes(
            (f"new:{key}" if key == keys[-1] else f"old:{key}").encode("utf-8")
        )
        old_hash = file_sha256(old_path)
        new_hash = file_sha256(new_path)
        identity_hash = hash_json({"canonical_paper_key": key, "identity": "stable"})
        old_rows.append({
            "canonical_paper_key": key, "source_pdf": str(old_path.resolve()),
            "pdf_sha256": old_hash, "identity_hash": identity_hash,
        })
        new_rows.append({
            "canonical_paper_key": key, "source_pdf": str(new_path.resolve()),
            "pdf_sha256": new_hash, "identity_hash": identity_hash,
        })
    config_path = tmp_path / "config.ini"
    config_path.write_text(
        f"[Application]\nconfig_schema = 1\n[Paths]\noutput_path = {output_root}\n",
        encoding="utf-8",
    )
    review_intent = {"prompt": "bounded source revision test"}
    provider_budget = ProviderAggregateBudgetV2(
        max_provider_calls_total=2,
        max_output_tokens_total=1_000,
        max_retry_attempts_total=0,
        max_wall_seconds=60,
    ).to_dict()
    child_spec = RuntimeJobSpec(
        project_name=child_ws.project_name,
        source=RuntimeSourceSpec(mode="direct", pdf_folder=str(new_source_dir.resolve())),
        job_id=child_ws.job_id,
        config=str(config_path.resolve()),
        action="run_all",
        reuse_stage1=True,
        queue_file=str((output_root / "_queue" / "child.queue.json").resolve()),
        workspace_path=child_ws.root_dir,
        metadata={
            "provider_semantic_contract_version": PROVIDER_SEMANTIC_CONTRACT_V2,
            "review_intent": review_intent,
            "provider_budget": provider_budget,
        },
    )
    child_spec.validate()
    authority_records = [
        ledger_record, closure_record, current_set, output_record,
        *lineage_records,
    ]
    if stage == "stage3_review" or include_outline_scope_ref:
        authority_records.append(scope_record)
    unique_authority = {record.artifact_id: record for record in authority_records}
    body = {
        "schema_version": "source-revision-plan/v1",
        "mode": "replace-existing-pdf/v1",
        "provider_semantic_contract_version": PROVIDER_SEMANTIC_CONTRACT_V2,
        "child_core_spec_hash": child_core_spec_hash(child_spec.to_dict()),
        "child_job_id": child_ws.job_id,
        "child_project_name": child_ws.project_name,
        "child_workspace_path": child_ws.root_dir,
        "parent": {
            "workspace_path": parent_ws.root_dir,
            "job_id": parent_ws.job_id,
            "registry_path": parent_registry.registry_path,
            "authority_refs": [_record_ref(record) for record in unique_authority.values()],
            "response_snapshot_refs": [_record_ref(response_snapshot_record)],
        },
        "old_sources": old_rows,
        "new_sources": new_rows,
        "changed_paper_key": keys[-1],
        "unchanged_paper_keys": keys[:-1],
        "review_intent_hash": hash_json(review_intent),
        "config_binding": {
            "parent_path": str(config_path.resolve()),
            "child_path": str(config_path.resolve()),
            "file_sha256": file_sha256(config_path),
        },
        "provider_budget": provider_budget,
    }
    plan_file = tmp_path / "source-revision-plan.json"
    plan_bytes = (
        json.dumps(body, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n"
    ).encode("utf-8")
    plan_file.write_bytes(plan_bytes)
    plan_reference = SourceRevisionPlanRefV1(
        schema_version=SOURCE_REVISION_PLAN_REF_VERSION,
        path=str(plan_file.resolve()),
        file_sha256=hashlib.sha256(plan_bytes).hexdigest(),
        content_hash=hash_json(body),
    )
    child_spec = replace(
        child_spec,
        metadata={**child_spec.metadata, "source_revision_plan": plan_reference.to_dict()},
    )
    child_runtime_spec = _publish_json(
        child_publication,
        child_registry,
        child_ws,
        "runtime_job_spec_v1.json",
        child_spec.to_dict(),
        artifact_id="runtime_job_spec",
        artifact_type="runtime_job_spec",
        metadata={"job_id": child_ws.job_id},
    )
    plan_ref_deps = [
        ArtifactDependencyRefV2.from_record(record, dependency_kind="external_job")
        for record in (*unique_authority.values(), response_snapshot_record)
    ]
    if omit_parent_plan_dependency:
        plan_ref_deps.pop(0)
    plan_ref_deps.append(ArtifactDependencyRefV2.from_record(child_runtime_spec))
    resolver = lambda job_id: parent_registry if job_id == parent_registry.job_id else None
    plan_record = _publish_json(
        child_publication,
        child_registry,
        child_ws,
        "source_revision_plan.json",
        body,
        artifact_id="source_revision_plan",
        artifact_type="source_revision_plan",
        metadata={"plan_content_hash": hash_json(body)},
        depends_on=tuple(plan_ref_deps),
        external_registry_resolver=resolver,
    )
    if create_replay_context:
        # This intentionally synthetic parent does not satisfy the whole-run
        # SourceRevision admission contract. Admission itself has dedicated
        # tests; replay tests pin the sealed-body/context behavior here.
        with patch(
            "runtime.source_revision_admission.verify_source_revision_plan_v1",
            return_value=body,
        ):
            replay = SourceRevisionReplayContext(
                child_registry=child_registry,
                plan_record=plan_record,
                plan_body=body,
                parent_registry=parent_registry,
                external_registry_resolver=resolver,
            )
    else:
        replay = None
    return {
        "parent_workspace": parent_ws,
        "child_workspace": child_ws,
        "parent_registry": parent_registry,
        "child_registry": child_registry,
        "parent_publication": parent_publication,
        "child_publication": child_publication,
        "parent_budget": parent_budget,
        "parent_receipt": receipt,
        "parent_ledger_record": ledger_record,
        "parent_closure_record": closure_record,
        "parent_scope_record": scope_record,
        "parent_output_record": output_record,
        "parent_snapshot_record": response_snapshot_record,
        "lineage_records": lineage_records,
        "parent_expected_call": expected,
        "writer_binding_for_output": writer_binding_for_output if stage == "stage3_review" else None,
        "writer_task_scope": child_writer_scope if stage == "stage3_review" else None,
        "current_scope": dict(current_scope),
        "request_payload": dict(request_payload),
        "route_identity": {
            "route": route,
            "provider": receipt.provider,
            "model": receipt.model,
            "endpoint": receipt.endpoint,
            "endpoint_type": receipt.endpoint_type,
            "config_hash": receipt.config_hash,
            "schema_hash": receipt.schema_hash,
            **(
                {"route_fingerprint": selected_scope_record_payload["route_fingerprint"]}
                if stage == "outline_v3" else {}
            ),
        },
        "replay": replay,
        "plan_record": plan_record,
        "plan_body": body,
        "stage": stage,
        "external_registry_resolver": resolver,
    }


def test_replay_context_rejects_plan_when_admission_returns_changed_parent_closure(tmp_path, monkeypatch):
    case = _make_case(tmp_path, create_replay_context=False)
    changed_body = json.loads(json.dumps(case["plan_body"]))
    changed_body["parent"]["authority_refs"][0]["content_hash"] = "0" * 64
    monkeypatch.setattr(
        "runtime.source_revision_admission.verify_source_revision_plan_v1",
        lambda _child_spec: changed_body,
    )

    with pytest.raises(SourceRevisionReplayError) as exc:
        SourceRevisionReplayContext(
            child_registry=case["child_registry"],
            plan_record=case["plan_record"],
            plan_body=case["plan_body"],
            parent_registry=case["parent_registry"],
            external_registry_resolver=case["external_registry_resolver"],
        )

    assert exc.value.code == "source_revision_plan_fresh_verification_mismatch"


def _parent_bytes(case: Mapping[str, Any]) -> dict[str, bytes]:
    root = Path(case["parent_workspace"].root_dir)
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


def _child_call(case: Mapping[str, Any], *, mutation: str = "") -> ExpectedProviderCall:
    call = case["parent_expected_call"]
    values = {
        **asdict(call),
        "job_id": case["child_workspace"].job_id,
        "attempt_id": "child-attempt",
        "closure_epoch_id": "child-closure-epoch",
        "logical_attempt_identity": "child-logical-attempt",
        "expected_call_graph_hash": hash_json({"child": call.call_id}),
        "verified_reuse": False,
        "reuse_evidence_artifact_id": "",
        "reuse_evidence_artifact_hash": "",
        "reuse_evidence_record_hash": "",
    }
    if mutation == "prompt":
        values["prompt_hash"] = hash_text("different prompt")
    elif mutation == "schema":
        values["schema_hash"] = hash_text("different schema")
    return ExpectedProviderCall.from_mapping(values)


def _child_call_for_output(
    case: Mapping[str, Any],
    output_record: ArtifactRecord,
    response: Any,
    *,
    call: ExpectedProviderCall | None = None,
) -> ExpectedProviderCall:
    document = json.loads(Path(output_record.path).read_bytes())
    logical = document.get("payload") if isinstance(document, Mapping) else None
    if logical is None and isinstance(document, Mapping):
        logical = document.get("section") or document.get("analysis")
    content_hash = str(document.get("content_hash") or "") if isinstance(document, Mapping) else ""
    if not content_hash:
        content_hash = hash_json(logical)
    base = call or _child_call(case)
    receipt_hash = case["parent_receipt"].response_hash or ""
    return replace(
        base,
        output_hash=receipt_hash,
        provider_response_hash=receipt_hash,
        normalized_output_hash=receipt_hash,
        artifact_payload_hash=hash_json(logical),
        artifact_content_hash=content_hash,
        registry_file_hash=output_record.content_hash,
        artifact_path=output_record.path,
        registered_artifact_hash=content_hash,
        replay_output_hash=receipt_hash,
        node_output_hash=content_hash,
    )


def _child_output_for_match(case: Mapping[str, Any], match) -> ArtifactRecord:
    child_ws = case["child_workspace"]
    if case["stage"] == "stage3_review":
        section = {
            "section_number": 1,
            "section_title": "Effects",
            "blocks": [{"type": "paragraph", "text": "Supported result"}],
            "evidence_packet_id": "section-U45",
            "provider_receipt_ids": [],
        }
        content_hash = hash_json(section)
        payload = {
            "artifact_type": "review_section",
            "artifact_version": "v3",
            "status": "ready",
            "job_id": child_ws.job_id,
            "section_id": "section-U45",
            "binding_hash": hash_json({"child": "writer"}),
            "binding": {"section_id": "section-U45"},
            "content_hash": content_hash,
            "section": section,
        }
        artifact_type = "review_section"
        artifact_version = "v3"
    else:
        payload = _outline_artifact(child_ws.job_id, match.response)
        artifact_type = "outline_artifact"
        artifact_version = "v3"
    return _publish_json(
        case["child_publication"],
        case["child_registry"],
        child_ws,
        "source_revision/child_output.json",
        payload,
        artifact_id="source-revision-child-output",
        artifact_type=artifact_type,
        artifact_version=artifact_version,
    )


def _physical_route_identity(case: Mapping[str, Any]) -> tuple[str, str, str, str]:
    route = case["route_identity"]
    endpoint = str(route["endpoint"])
    return (
        str(route["provider"]),
        str(route["model"]),
        str(route["endpoint_type"]),
        _safe_host(endpoint) or endpoint.strip().casefold(),
    )


def _lookup(case: Mapping[str, Any], *, scope=None, route=None, mutation: str = ""):
    call = _child_call(case, mutation=mutation)
    current_scope = dict(scope or case["current_scope"])
    current_route = dict(route or case["route_identity"])
    request = dict(case["request_payload"])
    if mutation == "schema":
        current_route["schema_hash"] = call.schema_hash
    if mutation in {"prompt", "schema"}:
        # Keep the current call's input body authoritative. Prompt/schema are
        # independent ExpectedProviderCall identity fields.
        call = replace(call, input_hash=hash_json(request))
    return case["replay"].lookup_response(
        expected_call=call,
        request_payload=request,
        current_scope=current_scope,
        parent_scope_record=None,
        route_identity=current_route,
    )


def test_outline_unchanged_scope_returns_detached_response_and_only_reads_parent(tmp_path):
    case = _make_case(tmp_path)
    parent_receipt = case["parent_receipt"]
    assert parent_receipt.call_id.startswith("outline:topic_synthesis_provider:batch:")
    assert parent_receipt.node_id == "topic_synthesis"
    assert parent_receipt.route == parent_receipt.call_id.removeprefix("outline:")
    assert parent_receipt.metadata["config_section"] == "Outline_API"
    parent_before = _parent_bytes(case)
    registry_before = Path(case["parent_registry"].registry_path).read_bytes()
    budget_before = case["parent_budget"].snapshot()

    result = _lookup(case)

    assert result.status == "match"
    assert result.match is not None
    assert result.match.response == {
        "topics": [{"paper_keys": case["current_scope"]["canonical_paper_keys"], "status": "completed"}],
    }
    result.match.response["topics"][0]["status"] = "mutated detached copy"
    again = _lookup(case)
    assert again.status == "match" and again.match is not None
    assert again.match.response["topics"][0]["status"] == "completed"
    assert _parent_bytes(case) == parent_before
    assert Path(case["parent_registry"].registry_path).read_bytes() == registry_before
    budget_after = case["parent_budget"].snapshot()
    for field in (
        "calls_used", "calls_reserved", "output_tokens_used", "output_tokens_reserved",
        "retry_attempts_used", "retry_attempts_reserved",
    ):
        assert budget_after[field] == budget_before[field]


def test_outline_wrong_semantic_receipt_route_is_not_reused(tmp_path):
    case = _make_case(
        tmp_path,
        outline_receipt_route_override="topic_synthesis_provider:batch:wrong",
    )

    result = _lookup(case)

    assert result.status == "no_match"
    assert result.reason == "route_identity_changed"


def test_outline_wrong_receipt_config_section_is_not_reused(tmp_path):
    case = _make_case(
        tmp_path,
        outline_receipt_config_section_override="Writer_API",
    )

    result = _lookup(case)

    assert result.status == "no_match"
    assert result.reason == "route_identity_changed"


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ("changed_key", "changed_source_key"),
        ("selected_hash", "selected_source_scope_changed"),
        ("non_source_hash", "non_source_scope_changed"),
        ("route", "route_identity_changed"),
    ],
)
def test_changed_selected_or_non_source_scope_route_is_no_match(tmp_path, change, reason):
    case = _make_case(tmp_path)
    scope = dict(case["current_scope"])
    route = dict(case["route_identity"])
    if change == "changed_key":
        scope["canonical_paper_keys"] = ["paper-9"]
    elif change == "selected_hash":
        scope["selected_source_content_hash"] = hash_json({"selected": "changed value"})
    elif change == "non_source_hash":
        scope["non_source_scope_hash"] = hash_json({"question": "different"})
    elif change == "route":
        route["route"] = "Other_API"
    result = _lookup(case, scope=scope, route=route)
    assert result.status == "no_match"
    assert result.reason == reason


def test_opaque_wire_equality_cannot_hide_canonical_changed_source(tmp_path):
    case = _make_case(tmp_path)
    # Keep the provider payload byte-identical while reporting the canonical
    # selected key that the SourceRevision plan marks changed.
    result = _lookup(case, scope={
        **case["current_scope"],
        "canonical_paper_keys": ["paper-9"],
    })
    assert hash_json(case["request_payload"]) == case["parent_expected_call"].input_hash
    assert result.status == "no_match"
    assert result.reason == "changed_source_key"


@pytest.mark.parametrize("mutation", ["prompt", "schema"])
def test_prompt_and_schema_identity_changes_are_no_match(tmp_path, mutation):
    case = _make_case(tmp_path)
    result = _lookup(case, mutation=mutation)
    assert result.status == "no_match"
    assert result.reason == "provider_call_identity_changed"


def test_parent_and_child_provider_call_ids_may_differ_when_request_and_node_match(tmp_path):
    case = _make_case(tmp_path)
    child_call = replace(_child_call(case), call_id="child-outline-call:stable-topic")
    result = case["replay"].lookup_response(
        expected_call=child_call,
        request_payload=case["request_payload"],
        current_scope=case["current_scope"],
        parent_scope_record=None,
        route_identity=case["route_identity"],
    )
    assert result.status == "match" and result.match is not None
    assert result.match.call_id == child_call.call_id
    assert result.match.parent_expected_call["call_id"] == case["parent_expected_call"].call_id


def test_outline_missing_parent_selected_scope_contract_fails_closed(tmp_path):
    case = _make_case(tmp_path, missing_parent_scope=True)
    with pytest.raises(SourceRevisionReplayError) as exc:
        _lookup(case)
    assert exc.value.code == "missing_parent_scope_binding"


def test_outline_scope_record_not_reachable_from_plan_authority_is_not_scanned(tmp_path):
    case = _make_case(tmp_path, include_outline_scope_ref=False)
    with pytest.raises(SourceRevisionReplayError) as exc:
        _lookup(case)
    assert exc.value.code == "parent_scope_record_missing_or_ambiguous"


def test_child_plan_artifact_must_carry_every_exact_external_parent_ref(tmp_path):
    with pytest.raises(UnverifiedArtifact, match="SourceRevision proof lacks an exact Registry dependency"):
        _make_case(
            tmp_path,
            omit_parent_plan_dependency=True,
            create_replay_context=False,
        )


def _publish_source_pdf_dependency_root(
    case: Mapping[str, Any],
    source_path: Path,
    *,
    artifact_type: str = "source_pdf",
    owner: str = "parent",
) -> ArtifactRecord:
    if owner == "parent":
        source_registry = case["parent_registry"]
    else:
        source_registry = case["child_registry"]
    source_record = source_registry.register_file(
        artifact_role=artifact_type,
        artifact_type=artifact_type,
        artifact_version="v1",
        path=source_path,
        producer=__name__,
        artifact_id=f"{artifact_type}:reachability:{owner}:{source_path.name}",
    )
    dependency = ArtifactDependencyRefV2.from_record(
        source_record,
        dependency_kind="local_job" if owner == "parent" else "external_job",
    )
    resolver = case["external_registry_resolver"]
    if owner != "parent":
        resolver = lambda job_id: (
            case["child_registry"]
            if job_id == case["child_workspace"].job_id
            else case["parent_registry"]
            if job_id == case["parent_workspace"].job_id
            else None
        )
    return _publish_json(
        case["parent_publication"],
        case["parent_registry"],
        case["parent_workspace"],
        f"test_source_pdf_roots/{owner}_{artifact_type}_{source_path.name}.json",
        {"artifact_type": "test_source_pdf_dependency_root", "status": "ready"},
        artifact_id=f"test-source-pdf-root:{owner}:{artifact_type}:{source_path.name}",
        artifact_type="test_source_pdf_dependency_root",
        depends_on=(dependency,),
        external_registry_resolver=resolver,
    )


def test_reachability_allows_only_exact_parent_source_pdfs_from_the_admitted_plan(tmp_path):
    case = _make_case(tmp_path)
    planned_path = Path(case["plan_body"]["old_sources"][0]["source_pdf"])
    record = _publish_source_pdf_dependency_root(case, planned_path)

    reachable = case["replay"]._reachable_from_registry_roots((record,))

    assert f"source_pdf:reachability:parent:{planned_path.name}" in reachable

    unplanned_path = tmp_path / "unplanned.pdf"
    unplanned_path.write_bytes(b"not in the admitted source map")
    unplanned_root = _publish_source_pdf_dependency_root(case, unplanned_path)
    with pytest.raises(SourceRevisionReplayError) as unplanned_error:
        case["replay"]._reachable_from_registry_roots((unplanned_root,))
    assert unplanned_error.value.code == "parent_source_pdf_not_plan_bound"

    other_artifact_path = tmp_path / "external-index.json"
    other_artifact_path.write_text("{}", encoding="utf-8")
    other_root = _publish_source_pdf_dependency_root(
        case, other_artifact_path, artifact_type="test_external_index",
    )
    with pytest.raises(SourceRevisionReplayError) as other_error:
        case["replay"]._reachable_from_registry_roots((other_root,))
    assert other_error.value.code == "parent_dependency_outside_owner_workspace"


def test_reachability_rejects_planned_pdf_when_live_bytes_differ_from_record_and_plan(tmp_path):
    case = _make_case(tmp_path)
    planned_path = Path(case["plan_body"]["old_sources"][0]["source_pdf"])
    root = _publish_source_pdf_dependency_root(case, planned_path)
    planned_path.write_bytes(b"changed after source revision admission")

    with pytest.raises(SourceRevisionReplayError) as exc:
        case["replay"]._reachable_from_registry_roots((root,))

    assert exc.value.code == "parent_source_pdf_not_plan_bound"


def test_reachability_rejects_plan_matching_pdf_from_the_wrong_source_owner(tmp_path):
    case = _make_case(tmp_path)
    planned_path = Path(case["plan_body"]["old_sources"][0]["source_pdf"])
    root = _publish_source_pdf_dependency_root(case, planned_path, owner="child")
    parent_resolver = case["replay"].external_registry_resolver
    case["replay"].external_registry_resolver = lambda job_id: (
        case["child_registry"]
        if job_id == case["child_workspace"].job_id
        else parent_resolver(job_id)
    )

    with pytest.raises(SourceRevisionReplayError) as exc:
        case["replay"]._reachable_from_registry_roots((root,))

    assert exc.value.code == "parent_source_pdf_not_plan_bound"


def test_parent_verified_reuse_from_ancestor_is_not_downgraded_to_no_match(tmp_path, monkeypatch):
    case = _make_case(tmp_path)
    parent_call = replace(case["parent_expected_call"], verified_reuse=True)
    monkeypatch.setattr(
        case["replay"],
        "_closure_calls_for_request",
        lambda _expected, _request_hash: [(
            case["parent_closure_record"],
            {"complete": True, "expected_calls": [asdict(parent_call)]},
            parent_call,
        )],
    )
    with pytest.raises(SourceRevisionReplayError) as exc:
        _lookup(case, scope={**case["current_scope"], "canonical_paper_keys": ["paper-9"]})
    assert exc.value.code == "unsupported_parent_reuse_authority"


@pytest.mark.parametrize(
    ("field", "value", "error_code"),
    [
        ("provider_response_hash", hash_json({"different": "raw response"}),
         "parent_receipt_provider_response_hash_mismatch"),
        ("provider_response_hash", "", "parent_receipt_provider_response_hash_mismatch"),
        ("normalized_output_hash", hash_json({"different": "normalized output"}),
         "parent_receipt_normalized_output_hash_mismatch"),
    ],
)
def test_parent_receipt_binds_response_hash_domains_without_output_hash_fallback(
    tmp_path, field, value, error_code,
):
    case = _make_case(tmp_path)
    replay = case["replay"]
    child_call = _child_call(case)
    closure_record, closure_payload, parent_call = replay._closure_calls_for_request(
        child_call,
        hash_json(case["request_payload"]),
    )[0]
    reachable = replay._reachable_from_registry_roots((closure_record,))

    with pytest.raises(SourceRevisionReplayError) as exc:
        replay._parent_receipt(
            replace(parent_call, **{field: value}),
            closure_payload,
            reachable,
        )

    assert exc.value.code == error_code


def test_parent_receipt_requires_the_expected_logical_attempt_identity(tmp_path):
    case = _make_case(tmp_path)
    replay = case["replay"]
    child_call = _child_call(case)
    closure_record, closure_payload, parent_call = replay._closure_calls_for_request(
        child_call,
        hash_json(case["request_payload"]),
    )[0]
    reachable = replay._reachable_from_registry_roots((closure_record,))

    with pytest.raises(SourceRevisionReplayError) as exc:
        replay._parent_receipt(
            replace(parent_call, logical_attempt_identity="different-logical-attempt"),
            closure_payload,
            reachable,
        )

    assert exc.value.code == "parent_original_receipt_missing"


def test_provider_closure_selection_skips_unrelated_stage1_shape_but_keeps_outline_strict(tmp_path):
    case = _make_case(tmp_path)
    stage1_closure = _publish_json(
        case["parent_publication"],
        case["parent_registry"],
        case["parent_workspace"],
        "stage1/mixed_authority_closure.json",
        {
            "artifact_type": "provider_receipt_closure",
            "artifact_version": "v1",
            "job_id": case["parent_workspace"].job_id,
            "stage_name": "stage1_analyze",
            "expected_calls": [{"call_id": "stage1:source", "stage_name": "stage1_analyze"}],
            "payload": ProviderReceiptClosure.evaluate([], []).to_dict(),
        },
        artifact_id="stage1:provider_receipt_closure:mixed-authority-test",
        artifact_type="provider_receipt_closure",
    )
    replay = case["replay"]
    replay._authority_records.append(stage1_closure)
    current = _child_call(case)
    matches = replay._closure_calls_for_request(current, hash_json(case["request_payload"]))

    assert len(matches) == 1
    assert matches[0][0] == case["parent_closure_record"]
    assert matches[0][1]["stage_name"] == "stage2_outline"

    malformed_outline_closure = _publish_json(
        case["parent_publication"],
        case["parent_registry"],
        case["parent_workspace"],
        "outline/malformed_selected_closure.json",
        {
            "artifact_type": "provider_receipt_closure",
            "artifact_version": "v1",
            "job_id": case["parent_workspace"].job_id,
            "stage_name": "stage2_outline",
            "payload": ProviderReceiptClosure.evaluate([], []).to_dict(),
        },
        artifact_id="outline:provider_receipt_closure:malformed-selected-test",
        artifact_type="provider_receipt_closure",
    )
    replay._authority_records.append(malformed_outline_closure)
    with pytest.raises(SourceRevisionReplayError) as exc:
        replay._provider_closures(current.stage_name)
    assert exc.value.code == "parent_provider_closure_incomplete"


def test_source_revision_replay_error_string_retains_machine_code():
    with_detail = SourceRevisionReplayError("parent_closure_invalid", "malformed stage closure")
    without_detail = SourceRevisionReplayError("parent_closure_invalid")

    assert with_detail.code == without_detail.code == "parent_closure_invalid"
    assert str(with_detail) == "parent_closure_invalid: malformed stage closure"
    assert str(without_detail) == "parent_closure_invalid"


@pytest.mark.parametrize("authority", ["closure", "ledger", "snapshot", "output"])
def test_eligible_parent_authority_byte_corruption_fails_closed(tmp_path, authority):
    case = _make_case(tmp_path)
    record = {
        "closure": case["parent_closure_record"],
        "ledger": case["parent_ledger_record"],
        "snapshot": case["parent_snapshot_record"],
        "output": case["parent_output_record"],
    }[authority]
    Path(record.path).write_bytes(b"{}\n")
    with pytest.raises(SourceRevisionReplayError):
        _lookup(case)


def test_writer_v2_selected_scope_replays_when_only_global_inventory_changes(tmp_path):
    case = _make_case(tmp_path, stage="stage3_review")
    parent_scope, _parent_route = case["replay"]._parent_scope_from_record(
        case["parent_scope_record"],
        case["parent_expected_call"],
        case["replay"]._reachable_from_plan_authority(),
    )
    assert {key: value for key, value in parent_scope.items() if key != "eligible"} == case["current_scope"]
    parent_before = _parent_bytes(case)
    result = _lookup(case)
    assert result.status == "match", result.reason
    assert result.match is not None
    assert result.match.parent_scope_record.artifact_type == "review_section"
    assert result.match.parent_lineage_records
    assert _parent_bytes(case) == parent_before


def test_writer_v2_replay_ignores_global_outline_adoption_and_catalog_hashes(tmp_path):
    case = _make_case(tmp_path, stage="stage3_review")
    binding = dict(case["writer_binding_for_output"])
    binding.update({
        "final_outline_hash": hash_json({"outline": "a newer unrelated section"}),
        "adoption_artifact_id": "outline-v3:adoption:new-global-id",
        "adoption_artifact_hash": hash_json({"adoption": "new-global-hash"}),
        "citation_catalog_hash": hash_json({"unselected": "citation changed"}),
    })
    current_scope = build_writer_source_revision_scope_v1(
        case["writer_task_scope"], binding,
    )
    assert current_scope == case["current_scope"]
    assert hash_json(case["request_payload"]) == case["parent_expected_call"].input_hash

    result = _lookup(case, scope=current_scope)

    assert result.status == "match", result.reason


@pytest.mark.parametrize("binding_field", [
    "section_id", "outline_section_hash", "evidence_packet_hash", "context_profile_hash",
])
def test_writer_v2_replay_still_rejects_section_packet_or_context_changes(tmp_path, binding_field):
    case = _make_case(tmp_path, stage="stage3_review")
    binding = dict(case["writer_binding_for_output"])
    binding[binding_field] = hash_json({"changed": binding_field})
    if binding_field == "section_id":
        binding[binding_field] = "section:changed"
    current_scope = build_writer_source_revision_scope_v1(
        case["writer_task_scope"], binding,
    )

    result = _lookup(case, scope=current_scope)

    assert result.status == "no_match"
    assert result.reason == "non_source_scope_changed"


def test_writer_v2_replay_still_rejects_selected_catalog_changes(tmp_path):
    case = _make_case(tmp_path, stage="stage3_review")
    writer_scope = json.loads(json.dumps(case["writer_task_scope"]))
    writer_scope["selected_catalog_entries"][0]["title"] += " (changed)"
    writer_scope["selected_source_binding"] = _selected_source_binding(
        writer_scope["tasks"],
        writer_scope["selected_catalog_entries"],
        writer_scope["selected_context_sources"],
    )
    writer_scope["writer_task_basis_hash"] = _selected_task_basis(writer_scope)
    current_scope = build_writer_source_revision_scope_v1(
        writer_scope, case["writer_binding_for_output"],
    )

    result = _lookup(case, scope=current_scope)

    assert result.status == "no_match"
    assert result.reason == "selected_source_scope_changed"


@pytest.mark.parametrize("stage", ["outline_v3", "stage3_review"])
def test_child_proof_is_external_parent_evidence_and_has_no_child_receipt(tmp_path, stage, monkeypatch):
    case = _make_case(tmp_path, stage=stage)
    child_call = replace(_child_call(case), call_id="child:outline:stable-topic")
    result = case["replay"].lookup_response(
        expected_call=child_call,
        request_payload=case["request_payload"],
        current_scope=case["current_scope"],
        parent_scope_record=None,
        route_identity=case["route_identity"],
    )
    assert result.status == "match" and result.match is not None
    child_registry = case["child_registry"]
    child_ws = case["child_workspace"]
    child_output = _child_output_for_match(case, result.match)
    child_call = _child_call_for_output(case, child_output, result.match.response, call=child_call)

    wrong_hash_field = "artifact_payload_hash" if stage == "outline_v3" else "artifact_content_hash"
    with pytest.raises(SourceRevisionReplayError) as wrong_output_hash:
        case["replay"].publish_child_reuse_proof(
            match=result.match,
            child_output_record=child_output,
            child_plan_record=case["plan_record"],
            child_expected_call=replace(
                child_call,
                **{wrong_hash_field: hash_json({"wrong": wrong_hash_field})},
            ),
            publication_context=case["child_publication"],
            path=child_ws.artifact_path("source_revision/wrong-output-hash.json"),
        )
    assert wrong_output_hash.value.code == "child_reuse_proof_output_hash_mismatch"

    proof = case["replay"].publish_child_reuse_proof(
        match=result.match,
        child_output_record=child_output,
        child_plan_record=case["plan_record"],
        child_expected_call=child_call,
        publication_context=case["child_publication"],
        path=child_ws.artifact_path("source_revision/reuse-proof.json"),
    )
    proof_payload = json.loads(Path(proof.path).read_bytes())
    assert proof_payload["source_authority"]["parent_call_id"] == case["parent_expected_call"].call_id
    assert proof_payload["child_call"]["call_id"] == child_call.call_id
    bound = replace(
        child_call,
        verified_reuse=True,
        reuse_evidence_artifact_id=proof.artifact_id,
        reuse_evidence_artifact_hash=proof.content_hash,
        reuse_evidence_record_hash=hash_json(asdict(proof)),
    )
    provider_authority = case["replay"].verified_provider_reuse_authority_v1(
        match=result.match,
        proof_record=proof,
        child_expected_call=bound,
        child_output_record=child_output,
        route_identity=_physical_route_identity(case),
    )
    assert provider_authority.request_hash == result.match.request_hash
    assert provider_authority.receipt_hash == hash_json(result.match.receipt.to_dict())
    assert provider_authority.output_hash == result.match.receipt.response_hash
    bad_host_identity = (*_physical_route_identity(case)[:3], "other-host.example")
    with pytest.raises(SourceRevisionReplayError) as route_error:
        case["replay"].verified_provider_reuse_authority_v1(
            match=result.match,
            proof_record=proof,
            child_expected_call=bound,
            child_output_record=child_output,
            route_identity=bad_host_identity,
        )
    assert route_error.value.code == "verified_reuse_route_identity_mismatch"
    case["replay"].verify_child_reuse_proof(
        proof,
        child_expected_call=bound,
        child_output_record=child_output,
        current_scope=case["current_scope"],
        route_identity=case["route_identity"],
    )

    # Simulate a weak/legacy Registry closure returning a self-signed proof
    # row without its recorded dependency union. The adapter must still call
    # the complete proof verifier and refuse to mint authority.
    forged_proof = replace(proof, depends_on=())
    original_get = child_registry.get
    original_verify_closure = child_registry.verify_ready_artifact_closure
    monkeypatch.setattr(
        child_registry,
        "get",
        lambda artifact_id: forged_proof if artifact_id == proof.artifact_id else original_get(artifact_id),
    )
    monkeypatch.setattr(
        child_registry,
        "verify_ready_artifact_closure",
        lambda record, **kwargs: (
            None if record == forged_proof else original_verify_closure(record, **kwargs)
        ),
    )
    forged_bound = replace(
        child_call,
        verified_reuse=True,
        reuse_evidence_artifact_id=forged_proof.artifact_id,
        reuse_evidence_artifact_hash=forged_proof.content_hash,
        reuse_evidence_record_hash=hash_json(asdict(forged_proof)),
    )
    with pytest.raises(SourceRevisionReplayError) as forged_error:
        case["replay"].verified_provider_reuse_authority_v1(
            match=result.match,
            proof_record=forged_proof,
            child_expected_call=forged_bound,
            child_output_record=child_output,
            route_identity=_physical_route_identity(case),
        )
    assert forged_error.value.code == "child_reuse_proof_dependency_missing"

    dependencies = {(dep.dependency_kind, dep.job_id, dep.artifact_id) for dep in proof.depends_on}
    assert ("local_job", child_ws.job_id, child_output.artifact_id) in dependencies
    assert ("local_job", child_ws.job_id, case["plan_record"].artifact_id) in dependencies
    assert (
        "external_job", case["parent_workspace"].job_id,
        case["parent_closure_record"].artifact_id,
    ) in dependencies
    assert not [
        record for record in child_registry.list_records()
        if record.artifact_type == "provider_receipt_ledger"
    ]


def test_child_proof_tamper_and_wrong_parent_resolver_fail_closed(tmp_path):
    case = _make_case(tmp_path)
    result = _lookup(case)
    assert result.status == "match" and result.match is not None
    child_output = _publish_json(
        case["child_publication"],
        case["child_registry"],
        case["child_workspace"],
        "outline/output.json",
        _outline_artifact(case["child_workspace"].job_id, result.match.response),
        artifact_id="child-output",
        artifact_type="outline_artifact",
        artifact_version="v3",
    )
    child_call = _child_call_for_output(case, child_output, result.match.response)
    proof = case["replay"].publish_child_reuse_proof(
        match=result.match,
        child_output_record=child_output,
        child_plan_record=case["plan_record"],
        child_expected_call=child_call,
        publication_context=case["child_publication"],
        path=case["child_workspace"].artifact_path("source_revision/proof.json"),
    )
    bound = replace(
        child_call,
        verified_reuse=True,
        reuse_evidence_artifact_id=proof.artifact_id,
        reuse_evidence_artifact_hash=proof.content_hash,
        reuse_evidence_record_hash=hash_json(asdict(proof)),
    )
    with pytest.raises(SourceRevisionReplayError) as scope_error:
        case["replay"].verify_child_reuse_proof(
            proof,
            child_expected_call=bound,
            child_output_record=child_output,
            current_scope={
                **case["current_scope"],
                "selected_source_content_hash": hash_json({"changed": "current source"}),
            },
            route_identity=case["route_identity"],
        )
    assert scope_error.value.code == "child_reuse_proof_current_scope_mismatch"
    proof_path = Path(proof.path)
    proof_path.write_text("{}", encoding="utf-8")
    with pytest.raises(SourceRevisionReplayError, match="child_reuse_proof_dependency_closure_invalid|artifact_hash_mismatch"):
        case["replay"].verify_child_reuse_proof(
            proof,
            child_expected_call=bound,
            child_output_record=child_output,
            current_scope=case["current_scope"],
            route_identity=case["route_identity"],
        )

    foreign = ArtifactRegistry(
        tmp_path / "foreign" / "artifact_registry.json",
        case["parent_registry"].job_id,
    )
    with patch(
        "runtime.source_revision_admission.verify_source_revision_plan_v1",
        return_value=case["replay"].plan_body,
    ), pytest.raises(SourceRevisionReplayError) as exc:
        SourceRevisionReplayContext(
            child_registry=case["child_registry"],
            plan_record=case["plan_record"],
            plan_body=case["replay"].plan_body,
            parent_registry=case["parent_registry"],
            external_registry_resolver=lambda job_id: foreign if job_id == "parent-job" else None,
        )
    assert exc.value.code == "designated_parent_registry_mismatch"


@pytest.mark.parametrize("content", [
    {"nested": [{"value": "original"}]},
    ["original", 3, False],
    "original response café",
    0,
    False,
])
def test_snapshot_registered_content_is_detached_and_preserves_original_json_shape(tmp_path, content):
    workspace = JobWorkspace.create(str(tmp_path / "output"), "snapshot-owner", "snapshot-job")
    publication = LocalPublicationContext()
    registry = publication.registry(workspace.paths.registry_path, workspace.job_id)
    ledger = ProviderRuntimeLedger(workspace.log_path("provider_receipts.jsonl"))
    runtime = ProviderRuntime(
        budget=ProviderBudgetV1(max_calls=1, max_total_tokens=100),
        ledger=ledger,
        job_id=workspace.job_id,
        attempt_id="attempt",
        stage_name="stage3_review",
        route="Writer_API",
        node_id="section",
        call_id="review:section",
        endpoint_type=_API["endpoint_type"],
    )
    admission = runtime.admit(estimated_tokens=1, requested_output_tokens=5)
    receipt = runtime.complete(
        admission=admission,
        prompt="prompt",
        input_payload={"scope": "source"},
        api_config=dict(_API),
        result={"status": "success", "content": content, "finish_reason": "stop"},
        metadata={"transport_config": dict(_API)},
    )
    store = ProviderResponseSnapshotStore(workspace, registry, publication)
    record = store.capture(receipt, content)
    returned = store.read_registered_content(receipt)
    assert type(returned) is type(content)
    assert returned == content
    assert store.read_registered(receipt) == record
    if isinstance(returned, dict):
        returned["nested"][0]["value"] = "mutated"
        assert store.read_registered_content(receipt) == content
    elif isinstance(returned, list):
        returned.append("mutated")
        assert store.read_registered_content(receipt) == content
