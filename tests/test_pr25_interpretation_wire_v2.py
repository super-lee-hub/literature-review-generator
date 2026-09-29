"""Interpretation dependencies through the real Outline adapter and HTTP wire."""
from __future__ import annotations

import json
import logging
import threading
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping

import pytest

from outline.semantic_chunking import build_paper_content_layers, build_semantic_chunk_plan, build_topic_synthesis_plan
from outline.v3_evidence import build_outline_evidence_views
from outline.v3_executor import OutlineV3ExecutionError, OutlineV3Executor
from outline.v3_models import EvidenceClaim, ResearchUnit
from runtime.orchestrator import _OutlineProviderTransportAdapter
from runtime.provider_context import ProviderContextProfile
from services.artifact_registry import ArtifactRegistry
from services.job_workspace import JobWorkspace
from tests.test_outline_v3_semantic_execution import _executor, _summary

PAPER = "synthetic-interpretation"
STUDY = f"{PAPER}:study:source-S1"
BOUNDARY = "UNINDEXED_BOUNDARY: only high prior knowledge improved; low prior knowledge did not."
C1 = "The intervention improves preference."
C2 = "Improvement appears only with high prior knowledge; low prior knowledge is null."
C3 = "The proposed mechanism is correlational and was not manipulated."


def _typed_case():
    summary = _summary(PAPER, "Synthetic interpretation example", C1)
    views = build_outline_evidence_views([summary], "interpretation-http")
    layers = build_paper_content_layers([summary], views, job_id="interpretation-http")
    base = layers.dossier_by_paper[PAPER]
    claims = [
        EvidenceClaim("C1", "empirical_finding", C1, STUDY, ["E1"], "study:result", base.source_summary_hash),
        EvidenceClaim("C2", "empirical_finding", C2, STUDY, ["E2"], "study:boundary", base.source_summary_hash),
        EvidenceClaim("C3", "author_interpretation", C3, STUDY, ["E3"], "study:mechanism", base.source_summary_hash),
    ]
    unit = ResearchUnit(
        study_id=STUDY,
        source_study_id="S1",
        parent_paper_id=PAPER,
        method=["Controlled intervention"],
        findings=[C1],
        moderators_or_boundaries=[C2],
        mechanisms=[C3],
        limitations=[BOUNDARY],
        claims=claims,
        evidence_ids=["E1", "E2", "E3"],
        source_locators={"study": ["source:S1"]},
        source_summary_hash=base.source_summary_hash,
    )
    dossier = replace(
        base,
        research_units=[unit],
        claims=claims,
        findings=[C1],
        moderators_boundaries=[C2],
        mechanism_evidence=[C3],
        limitations=[BOUNDARY],
        evidence_ids_by_field={
            "findings": ["E1"],
            "moderators_boundaries": ["E2"],
            "mechanism_evidence": ["E3"],
            f"{STUDY}:findings": ["E1"],
            f"{STUDY}:moderators_or_boundaries": ["E2"],
            f"{STUDY}:mechanisms": ["E3"],
        },
        evidence_text_by_id={"E1": C1, "E2": C2, "E3": C3},
    )
    layers = replace(layers, dossiers=[dossier])
    plan = build_semantic_chunk_plan(layers, candidate_count=1, physical_call_limit=24, retry_fallback_reserve=0)
    route = next(item for item in plan.topics if "method" in item.dimensions)
    topic = next(item for item in build_topic_synthesis_plan(plan) if item.topic_id == route.topic_id)
    return summary, views, layers, plan, route, topic


def _response_for(request: Mapping[str, Any], *, complete_support: bool) -> dict[str, Any]:
    topics = [item for item in request.get("topics") or () if isinstance(item, Mapping)]
    result = {
        "topics": [
            {"topic_id": item["topic_id"], "fragment_id": item["fragment_id"], "status": "completed",
             "conclusions": [], "supporting_evidence_ids": [], "unresolved_questions": []}
            for item in topics
        ],
        "processed_fragment_ids": [item["fragment_id"] for item in topics],
        "claims": [],
        "unresolved_questions": [],
    }
    if not topics:
        return result
    claim = {
        "claim_id": "synthesis:topic_synthesis:fixture",
        "fragment_id": topics[0]["fragment_id"],
        "claim_type": "empirical_finding",
        "paper_key": PAPER,
        "study_id": STUDY,
        "text": C1 if not complete_support else "The intervention improves preference only under the stated conditions; mechanism evidence is correlational.",
        "source_claim_ids": ["C1"],
        "evidence_ids": ["E1"],
    }
    if complete_support:
        dependencies = [
            dep for unit in request.get("evidence_units") or ()
            for study in unit.get("study_units") or ()
            for dep in study.get("interpretation_dependencies") or ()
            if dep.get("primary_claim_id") == "C1"
        ]
        claim["source_claim_ids"] = sorted({"C1", *(sid for dep in dependencies for sid in dep["required_source_claim_ids"])})
        claim["evidence_ids"] = sorted({"E1", *(eid for dep in dependencies for eid in dep["required_evidence_ids"])})
        claim["source_field_ids"] = sorted({fid for dep in dependencies for fid in dep["required_source_field_ids"]})
    result["claims"] = [claim]
    return result


def _wire_call(tmp_path: Path, *, all_indexed: bool, complete_support: bool):
    summary, views, layers, plan, route, topic = _typed_case()
    if all_indexed:
        route = replace(route, required_evidence_ids=["E1", "E2", "E3"])
        topic = replace(topic, supporting_evidence_ids=["E1", "E2", "E3"])
    captures: list[dict[str, Any]] = []

    class LocalProvider(BaseHTTPRequestHandler):
        def log_message(self, *_args: Any) -> None:
            pass

        def do_POST(self) -> None:
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            wire = json.loads(raw)
            body = json.loads(next(message["content"] for message in wire["messages"] if message["role"] == "user"))
            request = body["request"]
            captures.append({"path": self.path, "node_id": body["node_id"], "request": request})
            result = _response_for(request, complete_support=complete_support)
            response = {
                "id": "local-fixture", "object": "chat.completion",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": json.dumps(result)}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }
            payload = json.dumps(response).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(("127.0.0.1", 0), LocalProvider)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        profile = ProviderContextProfile.conservative(
            provider="claude_chat_reasoning", model="claude-opus-5", endpoint_type="chat_completions",
            model_context_limit=1_000_000, max_output_tokens=65_536,
        )
        adapter = _OutlineProviderTransportAdapter(
            api_config={
                "api_key": "local-fixture", "model": "claude-opus-5", "provider_family": "claude_chat_reasoning",
                "endpoint_type": "chat_completions", "api_base": f"http://127.0.0.1:{server.server_port}/v1",
                "max_context_tokens": "1000000", "max_output_tokens": "65536", "transport_retries": "0",
            },
            profile=profile,
            logger=logging.getLogger("pr25-interpretation-fixture"),
            system_prompt="Return only valid JSON matching the supplied source contract.",
        )
        workspace = JobWorkspace.create(str(tmp_path), "interpretation-fixture", "interpretation-job")
        registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
        executor = OutlineV3Executor(
            job_id=workspace.job_id, summaries=[summary], workspace=workspace,
            artifact_registry=registry, provider=adapter, provider_profile=profile,
            candidate_count=1, stability_mode="off", max_provider_calls=24,
            max_estimated_total_tokens=1_000_000,
        )
        _expanded, batches, _rows = executor._plan_topic_provider_batches(
            [topic], topic_routes={topic.topic_id: route}, evidence_model=views,
            content_layers_model=layers, profile=profile,
        )
        assert len(batches) == 1
        request = executor._build_topic_provider_request(
            batches[0], topic_routes={topic.topic_id: route}, evidence_model=views,
            content_layers_model=layers, batch_index=1,
        )
        error = None
        result = None
        try:
            result = executor._run_semantic_provider_call(
                "topic_synthesis_provider:batch:1", request,
                {"semantic_chunk_plan": plan.content_hash},
            )
        except OutlineV3ExecutionError as exc:
            error = exc
        assert len(captures) == 1
        assert captures[0]["path"] == "/v1/chat/completions"
        return captures[0]["request"], result, error
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


@pytest.mark.parametrize("all_indexed", [False, True])
def test_method_wire_has_required_interpretation_context_and_rejects_claim_only_output(
    tmp_path: Path, all_indexed: bool,
) -> None:
    request, _result, error = _wire_call(tmp_path, all_indexed=all_indexed, complete_support=False)
    wire_text = json.dumps(request, ensure_ascii=False)
    assert "E2" in wire_text and "E3" in wire_text
    assert BOUNDARY in wire_text
    assert "interpretation_dependencies" in wire_text
    topic = request["topics"][0]
    topic_unit_ids = set(topic["planned_evidence_unit_ids"])
    dependencies = [
        dependency
        for unit in request["evidence_units"]
        if unit["evidence_unit_id"] in topic_unit_ids
        for study in unit.get("study_units") or ()
        for dependency in study.get("interpretation_dependencies") or ()
    ]
    assert dependencies
    assert all(
        dependency["required_for_synthesis_output"] is True
        for dependency in dependencies
    )
    for contract_key in ("topics", "claims"):
        contract = request["output_contract"][contract_key]
        assert "required_for_synthesis_output" in contract
        assert "source_claim_ids" in contract
        assert "source_field_ids" in contract
        assert "mark unresolved" in contract
    assert "supporting_evidence_ids" in request["output_contract"]["topics"]
    assert "evidence_ids" in request["output_contract"]["claims"]
    assert "qualifier_dependency prose alone is not provenance" in (
        request["output_contract"]["claims"]
    )
    assert request["output_contract"]["semantic_result_contract_version"] == (
        "bounded-topic-synthesis/v4"
    )
    assert error is not None and "interpretation" in str(error).lower()


def test_method_wire_accepts_scoped_conditional_claim_with_complete_source_links(tmp_path: Path) -> None:
    request, result, error = _wire_call(tmp_path, all_indexed=False, complete_support=True)
    assert error is None
    assert result is not None
    assert len(result["claims"]) == 1
    assert BOUNDARY in json.dumps(request, ensure_ascii=False)


def test_topic_summary_must_carry_required_qualifier_provenance(tmp_path: Path) -> None:
    _summary_row, views, layers, _plan, route, topic = _typed_case()
    executor = _executor(tmp_path, stability_mode="off")
    request = executor._build_topic_provider_request(
        [topic], topic_routes={route.topic_id: route}, evidence_model=views,
        content_layers_model=layers, batch_index=1,
    )
    topic_row = request["topics"][0]
    topic_unit_ids = set(topic_row["planned_evidence_unit_ids"])
    dependencies = [
        {
            **dependency,
            "paper_key": unit["paper_key"],
            "study_id": study["study_id"],
        }
        for unit in request["evidence_units"]
        if unit["evidence_unit_id"] in topic_unit_ids
        for study in unit.get("study_units") or ()
        for dependency in study.get("interpretation_dependencies") or ()
    ]
    assert dependencies and all(
        dependency["required_for_synthesis_output"] is True
        for dependency in dependencies
    )
    primary_evidence_ids = {
        str(evidence_id)
        for unit in request["evidence_units"]
        for study in unit.get("study_units") or ()
        for claim in study.get("claims") or ()
        if any(
            claim.get("claim_id") == dependency["primary_claim_id"]
            and unit["paper_key"] == dependency["paper_key"]
            and study["study_id"] == dependency["study_id"]
            for dependency in dependencies
        )
        for evidence_id in claim.get("evidence_ids") or ()
    }
    assert dependencies and primary_evidence_ids
    fragment_id = topic_row["fragment_id"]
    topic_result = {
        "topic_id": topic_row["topic_id"],
        "fragment_id": fragment_id,
        "status": "completed",
        "conclusions": ["The effect holds under the stated condition."],
        "supporting_evidence_ids": sorted(primary_evidence_ids),
        "unresolved_questions": [],
    }
    result = {
        "topics": [topic_result],
        "processed_fragment_ids": [fragment_id],
        "claims": [],
        "unresolved_questions": [],
    }
    with pytest.raises(OutlineV3ExecutionError, match="omits required qualifiers"):
        executor._validate_semantic_provider_output(
            "topic_synthesis_provider:batch:1", request, result
        )

    topic_result["source_claim_ids"] = sorted({
        dependency["primary_claim_id"]
        for dependency in dependencies
    } | {
        source_claim_id
        for dependency in dependencies
        for source_claim_id in dependency["required_source_claim_ids"]
    })
    topic_result["supporting_evidence_ids"] = sorted(
        primary_evidence_ids
        | {
            evidence_id
            for dependency in dependencies
            for evidence_id in dependency["required_evidence_ids"]
        }
    )
    topic_result["source_field_ids"] = sorted({
        field_id
        for dependency in dependencies
        for field_id in dependency["required_source_field_ids"]
    })
    executor._validate_semantic_provider_output(
        "topic_synthesis_provider:batch:1", request, result
    )


def test_paper_level_gap_cannot_inherit_an_internal_study_id(tmp_path: Path) -> None:
    paper_key = "synthetic-paper-scope"
    summary = _summary(paper_key, "Paper scope", "A paper-level result.")
    summary["core_analysis"]["research_gap"] = "The authors identify a replication gap."
    views = build_outline_evidence_views([summary], "paper-scope")
    layers = build_paper_content_layers([summary], views, job_id="paper-scope")
    gap = next(
        claim for claim in layers.dossier_by_paper[paper_key].claims
        if claim.claim_type == "author_proposed_gap" and not claim.study_id
    )
    plan = build_semantic_chunk_plan(
        layers, candidate_count=1, physical_call_limit=24, retry_fallback_reserve=0
    )
    route = next(item for item in plan.topics if "method" in item.dimensions)
    topic = next(item for item in build_topic_synthesis_plan(plan) if item.topic_id == route.topic_id)
    topic = replace(topic, supporting_evidence_ids=list(gap.evidence_ids))
    executor = _executor(tmp_path, stability_mode="off")
    request = executor._build_topic_provider_request(
        [topic], topic_routes={route.topic_id: route}, evidence_model=views,
        content_layers_model=layers, batch_index=1,
    )
    internal_study_id = next(
        str(study["study_id"])
        for unit in request["evidence_units"]
        for study in unit.get("study_units") or ()
        if any(claim.get("claim_id") == gap.claim_id for claim in study.get("claims") or ())
    )
    fragment_id = str(request["topics"][0]["fragment_id"])
    result = {
        "topics": [{"topic_id": request["topics"][0]["topic_id"],
                    "fragment_id": fragment_id, "status": "completed",
                    "conclusions": [], "supporting_evidence_ids": [],
                    "unresolved_questions": []}],
        "processed_fragment_ids": [fragment_id],
        "claims": [{"claim_id": "synthesis:topic_synthesis:gap", "fragment_id": fragment_id,
                    "claim_type": "author_proposed_gap", "paper_key": paper_key,
                    "study_id": internal_study_id, "text": "One study identifies a gap.",
                    "source_claim_ids": [gap.claim_id], "evidence_ids": list(gap.evidence_ids)}],
        "unresolved_questions": [],
    }
    with pytest.raises(OutlineV3ExecutionError, match="study_id"):
        executor._validate_semantic_provider_output("topic_synthesis_provider:batch:1", request, result)

    result["claims"][0].pop("study_id")
    result["claims"][0]["text"] = "The paper identifies a replication gap."
    executor._validate_semantic_provider_output("topic_synthesis_provider:batch:1", request, result)


def test_qualifier_text_survives_topic_result_and_scopes_cross_group_claim(tmp_path: Path) -> None:
    _summary_row, views, layers, _plan, route, topic = _typed_case()
    executor = _executor(tmp_path, stability_mode="off")
    topic_request = executor._build_topic_provider_request(
        [topic], topic_routes={topic.topic_id: route}, evidence_model=views,
        content_layers_model=layers, batch_index=1,
    )
    topic_row = topic_request["topics"][0]
    context = executor._interpretation_context_for_units(
        topic_request["evidence_units"], topic_row["planned_evidence_unit_ids"]
    )
    assert BOUNDARY in json.dumps(context, ensure_ascii=False)
    assert context["dependencies"]
    provider_results = [{
        "batch_id": "fixture-batch", "result_id": "fixture-result",
        "fragment_ids": [topic_row["fragment_id"]],
        "topic_fragments": [{
            "fragment_id": topic_row["fragment_id"],
            "planned_evidence_unit_ids": topic_row["planned_evidence_unit_ids"],
            "planned_evidence_ids": topic_row["planned_evidence_ids"],
            "interpretation_context": context,
        }],
        "provider_output": {
            "topics": [{"topic_id": topic.topic_id, "fragment_id": topic_row["fragment_id"],
                        "status": "completed", "conclusions": [],
                        "supporting_evidence_ids": [], "unresolved_questions": []}],
            "claims": [], "unresolved_questions": [],
        },
    }]
    topic_payloads = executor._build_topic_synthesis_payloads([topic], provider_results)
    carried = topic_payloads[0]["fragments"][0]["provider_results"][0]["interpretation_context"]
    assert carried == context

    cross_request = {
        "task": "substantive_cross_group_comparison",
        "semantic_contract_version": "semantic-evidence-graph-v1",
        "topic_synthesis": [{
            "topic_id": topic.topic_id, "fragment_id": topic_row["fragment_id"],
            "paper_ids": [PAPER], "result_ids": ["fixture-result"],
            "interpretation_context": carried,
        }],
        "relation_candidates": [],
    }
    claim = {
        "claim_id": "synthesis:cross_group_comparison:fixture",
        "claim_type": "empirical_finding", "paper_key": PAPER,
        "study_id": STUDY, "text": C1,
        "source_claim_ids": ["C1"], "evidence_ids": ["E1"],
    }
    result = {
        "comparisons": [], "bridge_claims": [claim],
        "processed_topic_ids": [topic.topic_id],
        "processed_fragment_ids": [topic_row["fragment_id"]],
        "processed_result_ids": ["fixture-result"],
        "processed_relation_ids": [], "unresolved_questions": [],
    }
    with pytest.raises(OutlineV3ExecutionError, match="interpretation"):
        executor._validate_semantic_provider_output(
            "cross_group_comparison_provider", cross_request, result
        )

    claim["text"] = "The intervention helps only under the stated condition."
    claim["source_claim_ids"] = ["C1", "C2", "C3"]
    claim["evidence_ids"] = ["E1", "E2", "E3"]
    claim["source_field_ids"] = sorted({
        field_id for dependency in context["dependencies"]
        for field_id in dependency["required_source_field_ids"]
    })
    executor._validate_semantic_provider_output(
        "cross_group_comparison_provider", cross_request, result
    )
