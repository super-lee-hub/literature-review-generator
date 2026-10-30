from __future__ import annotations

from dataclasses import replace
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import Any, Mapping

import pytest

from outline.candidate_fragment_bank import (
    CandidateFragmentBankError,
    validate_candidate_fragment_bank_v1,
    validate_candidate_organization_map_for_request_v1,
)
from outline.v3_artifacts import OutlineArtifact, OutlineCandidate
from outline.v3_evidence import build_review_intent
from outline.v3_models import OrganizingAxis, OutlineCandidatePlan
from runtime.outline_v3_replay import ModelCallReplayStore
from runtime.provider_runtime import hash_json
from services.artifact_registry import ArtifactDependencyRefV2, ArtifactRegistry
from services.job_workspace import publish_json_artifact
from tests.test_run_first_candidate_adversarial import _executor, _sealed_relation_scope


_RELATIONS = (
    {
        "paper_keys": ["paper-a", "paper-b"],
        "question": "Compare both source findings while retaining their recorded boundaries.",
        "relation_type": "qualifies",
    },
    {
        "paper_keys": ["paper-c", "paper-d"],
        "question": "Compare both source findings while retaining their recorded boundaries.",
        "relation_type": "qualifies",
    },
)


class _LocalFragmentAPI:
    """Loopback-only endpoint for the actual ProviderRuntime HTTP adapter."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.responses: dict[str, dict[str, Any]] = {}
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - stdlib handler contract
                content_length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(content_length).decode("utf-8"))
                messages = body.get("messages") or []
                user_content = next(
                    (
                        item.get("content")
                        for item in messages
                        if isinstance(item, Mapping) and item.get("role") == "user"
                    ),
                    "",
                )
                envelope = json.loads(str(user_content))
                node_id = str(envelope["node_id"])
                request = envelope["request"]
                if node_id.startswith("candidate_comparison_fragments_provider:"):
                    response = {
                        "schema_version": "candidate-comparison-fragments-output/v1",
                        "parent_scope_hash": request["parent_scope_hash"],
                        "scope_hash": request["scope_hash"],
                        "fragments": [
                            {
                                "group_index": index,
                                "materialization_state": "materialized",
                                "text": (
                                    "The included source sides differ under their recorded boundaries; "
                                    "this comparison retains both qualifiers."
                                ),
                                "reason_code": None,
                            }
                            for index in range(len(request["claim_group_ids"]))
                        ],
                    }
                elif node_id.startswith("candidate_organization_provider:"):
                    sections = []
                    for section_index, comparison in enumerate(
                        request["required_comparison_sections"]
                    ):
                        group_index = int(comparison["group_index"])
                        card = request["fragment_cards"][group_index]
                        sections.append({
                            "section_index": section_index,
                            "metadata_index": int(comparison["metadata_index"]),
                            "fragment_indices": [group_index],
                            "paper_roles": [
                                [paper_index, group_index]
                                for paper_index in card[2]
                            ],
                        })
                    response = {
                        "schema_version": "candidate-organization-map/v1",
                        "candidate_id": request["candidate_id"],
                        "axis_hash": request["axis_hash"],
                        "plan_hash": request["plan_hash"],
                        "bank_hash": request["bank_hash"],
                        "map_schema_hash": request["map_schema_hash"],
                        "metadata_catalog_hash": request["metadata_catalog_hash"],
                        "metadata_catalog_contract_hash": request["metadata_catalog_contract_hash"],
                        "required_group_indices": list(request["required_group_indices"]),
                        "required_paper_indices": list(request["required_paper_indices"]),
                        "organization_contract_hash": request["organization_contract_hash"],
                        "sections": sections,
                        "deferred": [],
                    }
                else:
                    self.send_error(404, "unexpected provider node")
                    return
                owner.requests.append({"node_id": node_id, "request": request})
                owner.responses[node_id] = response
                completion = {
                    "choices": [{
                        "message": {
                            "role": "assistant",
                            "content": json.dumps(response, ensure_ascii=False),
                        },
                        "finish_reason": "stop",
                    }],
                    "usage": {"prompt_tokens": 50, "completion_tokens": 60, "total_tokens": 110},
                }
                encoded = json.dumps(completion, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def log_message(self, *_args: Any) -> None:
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = Thread(target=self.server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}/v1"

    def __enter__(self) -> "_LocalFragmentAPI":
        self.thread.start()
        return self

    def __exit__(self, *_args: Any) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def _review_intent() -> dict[str, Any]:
    return {"required_comparisons": [dict(item) for item in _RELATIONS]}


def _axis_and_plan() -> tuple[OrganizingAxis, OutlineCandidatePlan]:
    axis = OrganizingAxis(
        axis_id="axis_source_comparison",
        organizing_logic="source comparison",
        label="Source comparison",
        rationale="Group the included evidence by its source-reported comparison.",
        preferred_dimensions=["finding", "context"],
        preferred_relation_types=["qualifies"],
    )
    plan = OutlineCandidatePlan(
        candidate_id="candidate_1",
        organizing_logic=axis.organizing_logic,
        axis_id=axis.axis_id,
        shared_artifact_hashes={},
        required_node_ids=[],
        provider_generation_node_id="candidate_1_provider_generation",
    )
    return axis, plan


def _attach_source_scope_and_plan(
    executor,
    registry: ArtifactRegistry,
    inventory: Any,
    scope: Any,
    axis: OrganizingAxis,
    plan: OutlineCandidatePlan,
) -> None:
    def publish_node(node_id: str, artifact: OutlineArtifact, dependencies: tuple[str, ...]):
        record = publish_json_artifact(
            executor.publication_context,
            registry,
            executor._node_path(f"test_inputs/{node_id}"),
            artifact.to_dict(),
            artifact_id=f"outline-v3:{node_id}",
            artifact_role="outline_v3_node_output",
            artifact_type=artifact.artifact_type,
            artifact_version=artifact.artifact_version,
            producer="tests.test_run_first_candidate_provider_replay",
            depends_on=[
                ArtifactDependencyRefV2.from_record(executor.artifact_records[key])
                for key in dependencies
            ],
            metadata={
                "job_id": executor.job_id,
                "node_id": node_id,
                "content_hash": artifact.content_hash,
            },
        )
        executor.artifact_records[node_id] = record
        executor.artifact_paths[node_id] = record.path
        executor._payloads[node_id] = dict(artifact.payload)
        return record

    content_layers = registry.get(inventory.artifact_id)
    assert content_layers is not None and content_layers.status == "ready"
    executor.artifact_records["outline_content_layers"] = content_layers
    executor.artifact_paths["outline_content_layers"] = content_layers.path
    executor._candidate_output_scope = scope
    scope_record = publish_json_artifact(
        executor.publication_context,
        registry,
        executor._node_path("test_inputs/candidate_output_scope.json"),
        scope.to_dict(),
        artifact_id="outline-v3:candidate_output_scope",
        artifact_role="outline_candidate_output_scope",
        artifact_type="outline_candidate_output_scope",
        artifact_version="v1",
        producer="tests.test_run_first_candidate_provider_replay",
        depends_on=[ArtifactDependencyRefV2.from_record(content_layers)],
    )
    executor.artifact_records["candidate_output_scope"] = scope_record
    executor.artifact_paths["candidate_output_scope"] = scope_record.path
    axes_payload = {"axes": [axis.to_dict()], "candidates": [plan.to_dict()]}
    axes_artifact = executor._artifact(
        OutlineArtifact,
        axes_payload,
        {"candidate_output_scope": scope.content_hash},
    )
    publish_node(
        "organizing_axes",
        axes_artifact,
        ("candidate_output_scope",),
    )
    plan_artifact = executor._artifact(
        OutlineCandidate,
        plan.to_dict(),
        {"organizing_axes": axes_artifact.content_hash},
    )
    publish_node(
        plan.candidate_id,
        plan_artifact,
        ("organizing_axes",),
    )


def _make_executor(server: _LocalFragmentAPI, fixture, registry: ArtifactRegistry | None = None):
    workspace, original_registry, summaries, inventory, scope, context = fixture
    selected_registry = registry or original_registry
    executor = _executor(workspace, selected_registry, summaries, server, scope)
    executor._skip_exact_replay_verification = False
    executor.review_intent_input = _review_intent()
    executor._review_intent_hash = build_review_intent(executor.review_intent_input).content_hash
    executor._candidate_interpretation_tables = dict(context["interpretation_source_tables"])
    axis, plan = _axis_and_plan()
    _attach_source_scope_and_plan(
        executor, selected_registry, inventory, scope, axis, plan,
    )
    return executor, axis, plan


def _prepare_and_organize(executor, fixture, axis, plan):
    _workspace, _registry, _summaries, inventory, scope, _context = fixture
    bank, bank_record = executor._prepare_candidate_fragment_bank(
        scope, inventory, [], [],
    )
    required_papers = sorted(scope.paper_keys)
    content, dependency_hashes, _binding = executor._run_candidate_fragment_organization(
        candidate_id=plan.candidate_id,
        generation_node_id=plan.provider_generation_node_id,
        axis=axis,
        plan=plan,
        bank=bank,
        bank_record=bank_record,
        required_papers=required_papers,
    )
    return bank, bank_record, content, dependency_hashes


def _typed_result_artifact(executor, server: _LocalFragmentAPI, node_id: str, artifact_type: str):
    record = executor.artifact_records[node_id]
    assert record.artifact_type == artifact_type
    executor.registry.verify_ready_artifact_closure(record)
    envelope = json.loads(Path(record.path).read_text(encoding="utf-8"))
    http_request = next(row["request"] for row in server.requests if row["node_id"] == node_id)
    response = server.responses[node_id]
    assert envelope["artifact_type"] == artifact_type
    assert envelope["artifact_version"] == "v3"
    assert envelope["payload"] == response
    assert envelope["provider_request"] == http_request
    assert envelope["dependency_hashes"]["request_payload_hash"] == hash_json(http_request)
    receipt = next(
        row for row in executor._receipt_ledger.list_receipts()
        if row.call_id == executor._provider_call_id(node_id)
    )
    assert receipt.test_only is False
    assert receipt.status == "success"
    assert receipt.job_id == executor.job_id
    assert receipt.stage_name == "outline_v3"
    assert receipt.call_id == executor._provider_call_id(node_id)
    assert receipt.response_hash == hash_json(response)
    assert receipt.input_hash == hash_json(http_request)
    assert receipt.provider == "generic"
    assert receipt.model == "local-candidate-model"
    assert receipt.endpoint_type == "chat_completions"
    assert receipt.receipt_id in executor.receipts
    return record, envelope, http_request, response, receipt


def _comparison_receipt_snapshots(executor, bank, comparison_records):
    receipt_by_id = {
        row.receipt_id: row for row in executor._receipt_ledger.list_receipts()
    }
    actual_receipts = {
        receipt.receipt_id: receipt
        for _record, _envelope, _request, _response, receipt in comparison_records
    }
    expected_ids = {
        receipt_id
        for entry in bank["groups"]
        for receipt in entry["provider_receipt_refs"]
        for receipt_id in [str(receipt["receipt_id"])]
    }
    assert expected_ids
    assert expected_ids == set(actual_receipts)
    assert expected_ids.issubset(receipt_by_id)
    for receipt_id in expected_ids:
        receipt = receipt_by_id[receipt_id]
        assert receipt.test_only is False
        assert receipt.response_hash
        record = executor.artifact_records[f"candidate_receipt:{receipt_id}"]
        assert record.artifact_type == "provider_receipt_ledger"
        snapshot_rows = [
            json.loads(line)
            for line in Path(record.path).read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        receipt_wire = json.loads(json.dumps(receipt.to_dict(), ensure_ascii=False, sort_keys=True))
        assert snapshot_rows == [receipt_wire]
        assert hash_json(snapshot_rows[0]) == executor._receipt_record_hash(receipt)
        assert record.status == "ready"
    expected_artifacts = {
        (record.artifact_id, record.content_hash)
        for record, _envelope, _request, _response, _receipt in comparison_records
    }
    actual_artifacts = {
        (str(ref["artifact_id"]), str(ref["content_hash"]))
        for entry in bank["groups"]
        for ref in entry["comparison_artifact_refs"]
    }
    assert actual_artifacts == expected_artifacts
    expected_receipt_hashes = {
        (receipt_id, executor._receipt_record_hash(receipt))
        for receipt_id, receipt in actual_receipts.items()
    }
    bank_receipt_hashes = {
        (str(ref["receipt_id"]), str(ref["content_hash"]))
        for entry in bank["groups"]
        for ref in entry["provider_receipt_refs"]
    }
    assert bank_receipt_hashes == expected_receipt_hashes


def test_comparison_and_organization_raw_artifacts_snapshot_receipts_and_restart_replays_without_post(tmp_path: Path):
    fixture = _sealed_relation_scope(tmp_path)
    workspace, registry, _summaries, _inventory, scope, _context = fixture

    with _LocalFragmentAPI() as server:
        first, axis, plan = _make_executor(server, fixture)
        bank, bank_record, content, _compiled_dependencies = _prepare_and_organize(
            first, fixture, axis, plan,
        )
        first_request_count = len(server.requests)
        assert first_request_count >= 2
        assert first._skip_exact_replay_verification is False

        comparison_nodes = [
            row["node_id"] for row in server.requests
            if row["node_id"].startswith("candidate_comparison_fragments_provider:")
        ]
        organization_nodes = [
            row["node_id"] for row in server.requests
            if row["node_id"].startswith("candidate_organization_provider:")
        ]
        assert comparison_nodes
        assert organization_nodes == ["candidate_organization_provider:candidate_1"]
        comparison_records = [
            _typed_result_artifact(
                first, server, node_id, "candidate_comparison_fragments_result",
            )
            for node_id in comparison_nodes
        ]
        organization_record, organization_envelope, organization_request, raw_map, organization_receipt = _typed_result_artifact(
            first, server, organization_nodes[0], "candidate_organization_map_result",
        )
        assert organization_envelope["payload"] == raw_map
        assert organization_record.status == "ready"
        required_sections = organization_request["required_comparison_sections"]
        assert len(content["sections"]) == len(required_sections)
        for section, required_section in zip(content["sections"], required_sections):
            metadata = organization_request["metadata_catalog"][required_section["metadata_index"]]
            expected_papers = {
                organization_request["paper_keys"][index]
                for index in required_section["paper_indices"]
            }
            assert section["goal"] == metadata["goal"]
            assert set(section["paper_keys"]) == expected_papers
        assert {
            row["claim_slot_id"]
            for section in content["sections"] for row in section["claim_support"]
        } == {slot.claim_slot_id for slot in scope.claim_slots}
        assert organization_receipt.response_hash == hash_json(raw_map)

        _comparison_receipt_snapshots(first, bank, comparison_records)
        for node_id, _request, response in (
            (row["node_id"], row["request"], server.responses[row["node_id"]])
            for row in server.requests
        ):
            artifact_type = (
                "candidate_comparison_fragments_result"
                if node_id.startswith("candidate_comparison_fragments_provider:")
                else "candidate_organization_map_result"
            )
            record = first.artifact_records[node_id]
            replay_rows = [
                item for item in ModelCallReplayStore(workspace)._read_records()
                if item.audit_node_id == node_id
            ]
            assert replay_rows
            replay = replay_rows[-1]
            assert replay.output_hash == hash_json(response)
            assert replay.normalized_output_hash == hash_json(response)
            assert replay.registered_artifact_hash == json.loads(Path(record.path).read_text(encoding="utf-8"))["content_hash"]
            assert replay.node_output_hash == replay.registered_artifact_hash
            assert record.artifact_type == artifact_type
            assert replay.output_artifact_ids == [record.artifact_id]

        map_request_before = json.loads(json.dumps(organization_request, ensure_ascii=False))
        map_request_before.pop("_prompt_authority", None)
        map_request_before["axis_payload"]["label"] = "tampered semantic axis"
        with pytest.raises(ValueError, match="semantic payload hashes"):
            validate_candidate_organization_map_for_request_v1(raw_map, map_request_before)

        tampered_scope = replace(
            scope,
            claim_slots=(
                replace(scope.claim_slots[0], evidence_ids=("evidence:foreign",)),
                *scope.claim_slots[1:],
            ),
        )
        with pytest.raises(CandidateFragmentBankError, match="scope|support slots"):
            validate_candidate_fragment_bank_v1(bank, tampered_scope)

        restarted_registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
        restarted, restart_axis, restart_plan = _make_executor(
            server, fixture, registry=restarted_registry,
        )
        assert restart_axis == axis and restart_plan == plan
        replayed_bank, replayed_bank_record, replayed_content, _ = _prepare_and_organize(
            restarted, fixture, restart_axis, restart_plan,
        )
        assert replayed_bank["content_hash"] == bank["content_hash"]
        assert replayed_bank_record.artifact_id == bank_record.artifact_id
        assert replayed_content["sections"] == content["sections"]
        replayed_comparison_records = [
            _typed_result_artifact(
                restarted, server, node_id, "candidate_comparison_fragments_result",
            )
            for node_id in comparison_nodes
        ]
        replayed_organization_record, replayed_organization_envelope, _replayed_request, replayed_raw_map, replayed_organization_receipt = _typed_result_artifact(
            restarted, server, organization_nodes[0], "candidate_organization_map_result",
        )
        assert replayed_organization_record.artifact_id == organization_record.artifact_id
        assert replayed_organization_envelope["payload"] == raw_map == replayed_raw_map
        assert replayed_organization_receipt.receipt_id == organization_receipt.receipt_id
        assert replayed_organization_receipt.response_hash == organization_receipt.response_hash
        _comparison_receipt_snapshots(restarted, replayed_bank, replayed_comparison_records)
        assert len(server.requests) == first_request_count, "restart replay emitted a new HTTP POST"

        for node_id in [*comparison_nodes, *organization_nodes]:
            original = next(row for row in server.requests if row["node_id"] == node_id)
            assert hash_json(original["request"]) == hash_json(
                next(
                    json.loads(Path(restarted.artifact_records[node_id].path).read_text(encoding="utf-8"))["provider_request"]
                    for _ in (0,)
                )
            )
        replayed_receipt_ids = {
            receipt.receipt_id for receipt in restarted._receipt_ledger.list_receipts()
        }
        initial_receipts = {
            receipt.receipt_id: receipt for receipt in first._receipt_ledger.list_receipts()
        }
        replayed_receipts = {
            receipt.receipt_id: receipt for receipt in restarted._receipt_ledger.list_receipts()
        }
        assert set(initial_receipts).issubset(replayed_receipt_ids)
        for receipt_id, original_receipt in initial_receipts.items():
            replayed_receipt = replayed_receipts[receipt_id]
            assert replayed_receipt.test_only is False
            assert replayed_receipt.call_id == original_receipt.call_id
            assert replayed_receipt.response_hash == original_receipt.response_hash
