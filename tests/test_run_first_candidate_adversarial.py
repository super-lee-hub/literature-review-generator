"""Adversarial replay tests for compact, source-closed candidate shards.

These tests use the production writer-source sealer, Registry, ProviderRuntime,
replay store, and the runtime-owned HTTP transport against loopback only.
"""

from __future__ import annotations

from dataclasses import asdict
import json
import logging
from pathlib import Path
from threading import Thread
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Mapping

import pytest

from outline.candidate_output_scope import (
    CandidateOutputScopeError,
    build_candidate_output_scope_v1,
)
from outline.candidate_request_packing import project_semantic_context
from outline.provider_router import OutlineProviderRouter, OutlineRoleRoute
from outline.v3_executor import OutlineV3Executor, OutlineV3ExecutionError
from runtime.orchestrator import _OutlineProviderTransportAdapter
from runtime.outline_v3_replay import ModelCallReplayStore
from runtime.provider_context import ProviderContextProfile
from runtime.provider_runtime import hash_json
from services.artifact_registry import ArtifactRegistry
from services.job_workspace import JobWorkspace
from services.writer_source_inventory import load_writer_source_inventory_v1
from tests.test_outline_v3_semantic_execution import _summary
from tests.writer_source_fixture import bind_production_writer_sources


class _LocalCandidateAPI:
    """OpenAI-compatible loopback server that only returns compact candidate JSON."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - stdlib handler contract
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length).decode("utf-8"))
                messages = body.get("messages") or []
                user_message = next(
                    (item.get("content") for item in messages
                     if isinstance(item, Mapping) and item.get("role") == "user"),
                    "",
                )
                envelope = json.loads(str(user_message))
                request = envelope["request"]
                owner.requests.append({"path": self.path, "node_id": envelope["node_id"], "request": request})
                scope = request["candidate_output_scope"]
                group_ids = list(scope["claim_group_ids"])
                relation_ids = sorted({
                    str(relation_id)
                    for slot in scope.get("claim_slots") or ()
                    if isinstance(slot, Mapping)
                    for relation_id in slot.get("relation_ids") or ()
                    if str(relation_id)
                })
                compact = {
                    "candidate_id": str(request.get("candidate_id") or "candidate_1"),
                    "candidate_wire_version": "candidate-slot-output/v1",
                    "sections": [{
                        "section_id": f"section:{envelope['node_id'].rsplit(':', 1)[-1]}",
                        "title": "Assigned source comparison",
                        "goal": "Preserve the complete assigned source boundaries.",
                        "rationale": "The provider authored the comparison; the compiler restores only assigned support.",
                        "relation_ids": relation_ids,
                        "claims": [{
                            "text": "The assigned source records differ within their recorded boundaries.",
                            "claim_group_indices": [index],
                        } for index, _group_id in enumerate(group_ids)],
                    }],
                }
                payload = {
                    "choices": [{
                        "message": {"role": "assistant", "content": json.dumps(compact, ensure_ascii=False)},
                        "finish_reason": "stop",
                    }],
                    "usage": {"prompt_tokens": 20, "completion_tokens": 20, "total_tokens": 40},
                }
                encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
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

    def __enter__(self) -> "_LocalCandidateAPI":
        self.thread.start()
        return self

    def __exit__(self, *_args: Any) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def _sealed_relation_scope(tmp_path: Path):
    workspace = JobWorkspace.create(str(tmp_path), "candidate-replay", job_id="candidate-replay-job")
    registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    summaries = [
        _summary("paper-a", "Paper A", "The treatment improved trust under the first bounded context."),
        _summary("paper-b", "Paper B", "The treatment changed trust under the second bounded context."),
        _summary("paper-c", "Paper C", "The intervention raised retention in the third bounded sample."),
        _summary("paper-d", "Paper D", "The intervention reduced retention in the fourth bounded sample."),
    ]
    service = type("FixtureService", (), {})()
    service.registry = registry
    service.workspace = workspace
    service.summaries = summaries
    bind_production_writer_sources(service, [])
    inventory = load_writer_source_inventory_v1(registry)
    papers = inventory.paper_by_key()

    bundles: list[dict[str, Any]] = []
    relation_ids: list[str] = []
    for left, right, suffix in (("paper-a", "paper-b", "ab"), ("paper-c", "paper-d", "cd")):
        paper_keys = [left, right]
        claim_ids_by_paper: dict[str, list[str]] = {}
        finding_by_paper: dict[str, str] = {}
        required_evidence_ids: set[str] = set()
        for paper_key in paper_keys:
            paper = papers[paper_key]
            finding = next(
                claim for claim in paper.claims
                if claim.claim_type == "empirical_finding" and claim.evidence_ids
            )
            boundary = next(
                claim for claim in paper.claims
                if claim.claim_type == "author_interpretation"
                and "bounded" in claim.text.casefold()
                and claim.evidence_ids
            )
            claim_ids_by_paper[paper_key] = [finding.claim_id, boundary.claim_id]
            finding_by_paper[paper_key] = finding.text
            required_evidence_ids.update((*finding.evidence_ids, *boundary.evidence_ids))
        relation_id = f"relation:confirmed:{suffix}"
        relation_ids.append(relation_id)
        evidence_ids = sorted(required_evidence_ids)
        bundles.append({
            "relation_id": relation_id,
            "relation_type": "qualifies",
            "comparison_question": "Compare both source findings while retaining their recorded boundaries.",
            "paper_ids": paper_keys,
            "claim_ids_left": claim_ids_by_paper[left],
            "claim_ids_right": claim_ids_by_paper[right],
            "findings_left": [finding_by_paper[left]],
            "findings_right": [finding_by_paper[right]],
            "required_evidence_ids": evidence_ids,
            "provided_evidence_ids": evidence_ids,
            "missing_evidence_ids": [],
            "evidence_completeness": "complete",
            "decision": "confirmed",
        })
    routes = [
        {"topic_id": f"topic:{paper_key}", "logical_node_id": f"topic_synthesis:{paper_key}",
         "paper_ids": [paper_key], "fragments": []}
        for paper_key in sorted(papers)
    ]
    scope = build_candidate_output_scope_v1(
        inventory, routes, selected_relation_ids=relation_ids, relation_bundles=bundles,
    )
    source_tables = {
        "source_fields": [asdict(row) for paper in inventory.papers for row in paper.source_fields],
        "dependencies": [asdict(row) for paper in inventory.papers for row in paper.interpretation_dependencies],
        "source_claims": [asdict(row) for paper in inventory.papers for row in paper.claims],
    }
    context = {"interpretation_source_tables": source_tables, "topic_routes": []}
    return workspace, registry, summaries, inventory, scope, context


def _executor(workspace, registry, summaries, server: _LocalCandidateAPI, scope) -> OutlineV3Executor:
    api_config = {
        "api_key": "local-candidate-fixture-key",
        "provider_family": "generic",
        "model": "local-candidate-model",
        "api_base": server.base_url,
        "endpoint_type": "chat_completions",
        "transport_retries": "0",
        "total_timeout_seconds": "3",
        "connect_timeout_seconds": "2",
        "read_timeout_seconds": "2",
        "max_context_tokens": "128000",
    }
    profile = ProviderContextProfile.from_api_config(
        api_config, max_output_tokens=800, default_provider="generic",
        default_model="local-candidate-model", default_endpoint_type="chat_completions",
    )
    transport = _OutlineProviderTransportAdapter(
        api_config=api_config, profile=profile, logger=logging.getLogger("test.candidate.replay"),
        system_prompt="Return a compact source-supported candidate object.",
    )
    route = OutlineRoleRoute(
        role="candidate_provider_generation", config_section="Outline_API",
        provider_name="generic", model="local-candidate-model", endpoint_type="chat_completions",
        profile=profile, transport=transport, api_base=server.base_url, config_identity=api_config,
    )
    executor = OutlineV3Executor(
        job_id=workspace.job_id, summaries=summaries, workspace=workspace,
        artifact_registry=registry, provider_profile=profile,
        provider_router=OutlineProviderRouter(routes={"candidate_provider_generation": route}),
        enabled_semantic_roles=("candidate_provider_generation",), candidate_count=1,
        stability_mode="off", max_provider_calls=10, max_source_prompt_tokens=32_000,
        _skip_exact_replay_verification=True,
    )
    executor._candidate_output_scope = scope
    return executor


def _group_request(executor, scope, context, group):
    group_id = group.claim_group_id
    local_scope = scope.for_groups([group_id], allowed_paper_keys=group.paper_keys)
    shard_id = "candidate_group_" + hash_json([group_id])[:16]
    node_id = f"candidate_1_provider_generation:local:{shard_id}"
    projected = project_semantic_context(context, local_scope)
    request = {
        "candidate_id": "candidate_1",
        "organizing_logic": "source comparison",
        "paper_keys": list(local_scope.paper_keys),
        "relation_ids": list(group.relation_ids),
        "candidate_output_scope": executor._candidate_output_scope_wire(
            group.paper_keys, [group_id],
        ),
        "shared_semantic_context": projected,
        "hierarchy": {
            "level": "candidate_local_shard", "shard_id": shard_id,
            "claim_group_ids": [group_id], "paper_keys": list(local_scope.paper_keys),
            "group_assignment": {group_id: node_id},
        },
        "output_contract": {
            "preferred_candidate_wire_version": "candidate-slot-output/v1",
            "must_include": ["candidate_wire_version", "sections"],
        },
    }
    input_hashes = (local_scope.content_hash, hash_json(projected))
    return node_id, request, local_scope, input_hashes


def _publish_expanded(executor, node_id, raw, group, scope):
    group_id = group.claim_group_id
    expanded = executor._expand_candidate_wire_payload(raw, group.paper_keys, [group_id])
    executor._validate_candidate_payload(
        "candidate_1", expanded, allowed_paper_keys=group.paper_keys,
        allowed_relation_ids=group.relation_ids, allowed_claim_group_ids=[group_id],
    )
    dependency_hashes = {
        "input_0": scope.for_groups([group_id], allowed_paper_keys=group.paper_keys).content_hash,
        "relation_shard": hash_json({"claim_group_ids": [group_id], "paper_keys": list(group.paper_keys)}),
    }
    executor._persist_candidate_shard_output(
        node_id, expanded, dependency_hashes=dependency_hashes,
    )
    return expanded


def _call_group(executor, scope, context, group):
    node_id, request, _local, input_hashes = _group_request(executor, scope, context, group)
    raw = executor._provider_call(
        node_id, request, expect_json=True, input_artifact_hashes=input_hashes,
        transport_node_id="candidate_1_provider_generation", output_tokens=512,
    )
    expanded = _publish_expanded(executor, node_id, raw, group, scope)
    return node_id, request, raw, expanded


def _planned_group_requests(executor, scope, context, inventory):
    evidence = [
        {"paper_key": paper.paper_key, "projection": "registry_complete_evidence_ref_v1",
         "dossier_id": paper.dossier_id, "source_summary_hash": paper.source_summary_hash}
        for paper in inventory.papers
    ]
    relations = [
        {"relation_id": relation_id, "paper_keys": list(group.paper_keys), "relation_type": "qualifies"}
        for group in scope.claim_groups
        for relation_id in group.relation_ids
    ]
    parent = {
        "candidate_id": "candidate_1", "organizing_logic": "source comparison",
        "paper_keys": list(scope.paper_keys), "relation_ids": list(scope.selected_relation_ids),
        "relations": relations, "evidence": evidence,
        "shared_semantic_context": context,
        "semantic_chunk_plan": {"content_hash": "plan-placeholder"},
        "content_layer_refs": {},
        "output_contract": {"preferred_candidate_wire_version": "candidate-slot-output/v1"},
    }
    planned = executor._candidate_group_requests(
        generation_node_id="candidate_1_provider_generation", provider_request=parent,
    )
    return {
        tuple(str(value) for value in request["hierarchy"]["claim_group_ids"]): request
        for _node_id, _shard, _papers, _relations, request in planned
    }


def test_dynamic_compact_candidate_replay_returns_published_canonical_shard_without_post(tmp_path: Path):
    workspace, registry, summaries, _inventory, scope, context = _sealed_relation_scope(tmp_path)
    group = next(item for item in scope.claim_groups if item.relation_ids == ("relation:confirmed:ab",))

    with _LocalCandidateAPI() as server:
        first = _executor(workspace, registry, summaries, server, scope)
        node_id, request, raw, expanded = _call_group(first, scope, context, group)
        first_posts = len(server.requests)
        assert first_posts == 1

        artifact = first.artifact_records[node_id]
        envelope = json.loads(Path(artifact.path).read_text(encoding="utf-8"))
        assert envelope["payload"] == expanded
        raw_response_hash = hash_json(raw)
        canonical_artifact_hash = str(envelope["content_hash"])
        assert raw_response_hash != hash_json(expanded)
        assert canonical_artifact_hash != raw_response_hash
        receipt = next(row for row in first._receipt_ledger.list_receipts()
                       if row.receipt_id == first.receipts[-1])
        assert receipt.response_hash == raw_response_hash
        assert set(expanded["sections"][0]["paper_keys"]) == set(group.paper_keys)
        assert {row["claim_slot_id"] for row in expanded["sections"][0]["claim_support"]} == set(group.support_slot_ids)

        records = [row for row in ModelCallReplayStore(workspace)._read_records()
                   if row.audit_node_id == node_id]
        assert records, "validated canonical candidate shard was not recorded for replay"
        replay = records[-1]
        assert replay.output_hash == raw_response_hash
        assert replay.normalized_output_hash == raw_response_hash
        assert replay.registered_artifact_hash == canonical_artifact_hash
        assert replay.node_output_hash == canonical_artifact_hash
        assert replay.output_artifact_ids == [artifact.artifact_id]
        assert replay.receipt_ids == [first.receipts[-1]]

        restarted_registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
        restarted = _executor(workspace, restarted_registry, summaries, server, scope)
        replay_node, replay_request, replay_raw, _ = _call_group(restarted, scope, context, group)
        assert replay_node == node_id
        assert hash_json(replay_request) == hash_json(request)
        assert replay_raw == expanded, "replay must return the validated source-closed artifact, not compact raw bytes"
        assert len(server.requests) == first_posts, "exact dynamic replay emitted an extra HTTP POST"


def test_grouped_candidate_requests_localize_qualifier_changes(tmp_path: Path):
    workspace, registry, summaries, inventory, scope, context = _sealed_relation_scope(tmp_path)
    ab_group = next(item for item in scope.claim_groups if item.relation_ids == ("relation:confirmed:ab",))
    cd_group = next(item for item in scope.claim_groups if item.relation_ids == ("relation:confirmed:cd",))

    with _LocalCandidateAPI() as server:
        executor = _executor(workspace, registry, summaries, server, scope)
        before = _planned_group_requests(executor, scope, context, inventory)
        ab_key = (ab_group.claim_group_id,)
        cd_key = (cd_group.claim_group_id,)
        assert ab_key in before, f"candidate packer did not split comparison groups: {list(before)}"
        assert cd_key in before, f"candidate packer did not split comparison groups: {list(before)}"
        ab_request = before[ab_key]
        cd_request = before[cd_key]

        changed_context = json.loads(json.dumps(context, ensure_ascii=False))
        ab_field_ids = {
            value for slot in scope.claim_slots if slot.claim_group_id == ab_group.claim_group_id
            for value in slot.source_field_ids
        }
        changed_field = next(
            row for row in changed_context["interpretation_source_tables"]["source_fields"]
            if row.get("source_field_id") in ab_field_ids
        )
        changed_field["source_value"] = str(changed_field["source_value"]) + " [qualifier revision]"
        after = _planned_group_requests(executor, scope, changed_context, inventory)
        changed_ab_request = after[ab_key]
        unchanged_cd_request = after[cd_key]
        assert changed_ab_request != ab_request
        assert unchanged_cd_request == cd_request, (
            "a source qualifier outside the assigned group must not invalidate this group's request identity"
        )


def test_compact_candidate_compiler_rejects_foreign_group_and_cannot_import_its_support(tmp_path: Path):
    workspace, registry, summaries, _inventory, scope, _context = _sealed_relation_scope(tmp_path)
    ab_group = next(item for item in scope.claim_groups if item.relation_ids == ("relation:confirmed:ab",))
    cd_group = next(item for item in scope.claim_groups if item.relation_ids == ("relation:confirmed:cd",))
    with _LocalCandidateAPI() as server:
        executor = _executor(workspace, registry, summaries, server, scope)
        foreign_response = {
            "candidate_id": "candidate_1",
            "candidate_wire_version": "candidate-slot-output/v1",
            "sections": [{
                "section_id": "foreign-group", "title": "Foreign", "goal": "Wrong group.",
                "relation_ids": list(cd_group.relation_ids),
                "claims": [{"text": "A foreign source claim.", "claim_group_indices": [0]}],
            }],
        }
        with pytest.raises(CandidateOutputScopeError, match="missing paper support"):
            scope.for_groups([cd_group.claim_group_id], allowed_paper_keys=ab_group.paper_keys)
        with pytest.raises(OutlineV3ExecutionError, match="missing paper support|outside|unknown|scope"):
            executor._expand_candidate_wire_payload(
                foreign_response, ab_group.paper_keys, [cd_group.claim_group_id],
            )

