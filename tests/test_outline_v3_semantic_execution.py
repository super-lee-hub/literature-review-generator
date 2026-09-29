from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping

import pytest

from outline.v3_executor import OutlineV3ExecutionError, OutlineV3Executor
from outline.v3_artifacts import OutlineArtifact
from outline.v3_evidence import (
    build_global_corpus_ledger,
    build_multi_view_matrix,
    build_outline_evidence_views,
)
from outline.v3_relations import build_global_relation_map
from outline.semantic_chunking import (
    TopicRoute,
    build_paper_content_layers,
    build_semantic_chunk_plan,
    build_topic_synthesis_plan,
)
from outline.v3_models import EvidenceClaim, PaperEvidenceDossier, ResearchUnit, TopicSynthesis
from runtime.provider_runtime import ProviderRuntimeLedger, hash_json, hash_text
from runtime.provider_context import ProviderContextProfile
from services.artifact_registry import ArtifactRegistry
from services.job_workspace import JobWorkspace
from summary_schema import normalize_ai_summary


def _summary(paper_key: str, title: str, finding: str) -> dict[str, Any]:
    summary = normalize_ai_summary(
        {
            "routing": {
                "paper_type": "empirical",
                "paper_subtype_raw": "quantitative",
                "paper_subtype_normalized": "quantitative",
                "classification_status": "resolved",
                "route_confidence": "high",
                "classification_rationale": "controlled empirical design",
                "secondary_candidates": [],
            },
            "paper_metadata": {
                "title": title,
                "authors": ["Author"],
                "year": "2025",
                "journal": "Example Journal",
                "doi": "10.1000/example",
            },
            "core_analysis": {
                "summary": finding,
                "key_points": [finding],
                "methodology": "Controlled empirical study",
                "findings": finding,
                "conclusions": finding,
                "relevance": "The result informs the research question.",
                "limitations": "The result is bounded by the tested context.",
                "research_gap": "Further replication is needed.",
                "theoretical_framework": None,
                "future_research_directions": [],
            },
            "specialized_details": {
                "empirical": {
                    "research_questions_or_hypotheses": [],
                    "data_source_and_size": "Two controlled samples",
                    "analysis_technique": "Regression analysis",
                    "core_variables": {"independent": ["treatment"], "dependent": ["outcome"]},
                    "sample_characteristics_or_context": "Controlled context.",
                },
                "review": None,
                "conceptual": None,
            },
        }
    )
    summary["status"] = "success"
    summary["paper_info"] = {
        "canonical_paper_key": paper_key,
        "source_paper_id": paper_key,
        "title": title,
        "authors": ["Author"],
        "year": 2025,
        "classification": "core",
        "must_use": True,
    }
    return summary


def _configured_test_provider(node_id: str, request: Mapping[str, Any]) -> Mapping[str, Any]:
    if "relation_adjudication" in node_id:
        candidates = [
            dict(item) for item in request.get("relation_candidates") or ()
            if isinstance(item, Mapping)
        ]
        confirmed = [
            str(item.get("relation_id") or "")
            for item in candidates
            if item.get("relation_id") and item.get("evidence_fields")
        ]
        rejected = [
            {"relation_id": str(item.get("relation_id") or ""), "reason": "insufficient evidence fields"}
            for item in candidates
            if str(item.get("relation_id") or "") not in confirmed
        ]
        return {"status": "success", "content": {"confirmed_relation_ids": confirmed, "rejected_relations": rejected}}
    if node_id.startswith("topic_synthesis_provider"):
        topics = [
            dict(item) for item in request.get("topics") or ()
            if isinstance(item, Mapping)
        ]
        return {
            "status": "success",
            "content": {
                "topics": [
                    {
                        "topic_id": str(item.get("topic_id") or ""),
                        "fragment_id": str(item.get("fragment_id") or item.get("topic_id") or ""),
                        "status": "completed",
                        "conclusions": [],
                        "unresolved_questions": [],
                        "supporting_evidence_ids": list(item.get("planned_evidence_ids") or ()),
                    }
                    for item in topics
                ],
                "processed_fragment_ids": [
                    str(item.get("fragment_id") or item.get("topic_id") or "")
                    for item in topics
                ],
                "claims": [],
                "unresolved_questions": [],
            },
        }
    if (
        node_id.startswith(("cross_group_comparison_provider", "global_synthesis_provider"))
        and request.get("shared_synthesis_contract_version") == "v1"
    ):
        # The routed integration tests need the same substantive, qualified
        # cross/global fixture as the production-path HTTP tests.
        from tests.test_current_runtime_full_e2e import _outline_provider_response

        return _outline_provider_response(node_id, request)
    if node_id.startswith(("cross_group_comparison_provider", "global_synthesis_provider")):
        topic_ids: set[str] = set()
        fragment_ids: set[str] = set()
        result_ids: set[str] = set()
        relation_ids: set[str] = set()

        def collect_ids(value: Any) -> None:
            if isinstance(value, Mapping):
                topic_id = str(value.get("topic_id") or "")
                relation_id = str(value.get("relation_id") or "")
                if topic_id:
                    topic_ids.add(topic_id)
                raw_fragments = [
                    value.get("fragment_id"),
                    *(value.get("fragment_ids") or ()),
                    *(value.get("processed_fragment_ids") or ()),
                ]
                fragment_ids.update(str(item) for item in raw_fragments if str(item or ""))
                raw_results = [
                    value.get("result_id"),
                    value.get("batch_result_id"),
                    *(value.get("result_ids") or ()),
                    *(value.get("batch_result_ids") or ()),
                    *(value.get("processed_result_ids") or ()),
                ]
                result_ids.update(str(item) for item in raw_results if str(item or ""))
                if relation_id:
                    relation_ids.add(relation_id)
                for key in ("topic_ids", "processed_topic_ids"):
                    topic_ids.update(str(item) for item in value.get(key) or () if str(item))
                for key in ("relation_ids", "processed_relation_ids"):
                    relation_ids.update(str(item) for item in value.get(key) or () if str(item))
                for child in value.values():
                    collect_ids(child)
            elif isinstance(value, list):
                for child in value:
                    collect_ids(child)

        collect_ids(request.get("topic_synthesis"))
        collect_ids(request.get("cross_group_comparison"))
        collect_ids(request.get("relation_candidates"))
        if node_id.startswith("cross_group_comparison_provider"):
            content = {
                "comparisons": [],
                "bridge_claims": [],
                "processed_topic_ids": sorted(topic_ids),
                "processed_fragment_ids": sorted(fragment_ids),
                "processed_result_ids": sorted(result_ids),
                "processed_relation_ids": sorted(relation_ids),
                "topic_dispositions": [
                    {
                        "topic_id": topic_id,
                        "status": "unresolved",
                        "reason": "The local fixture supplies no substantive cross-topic claim.",
                    }
                    for topic_id in sorted(topic_ids)
                ],
                "unresolved_questions": [],
            }
        else:
            content = {
                "synthesis_claims": [],
                "organizing_principles": [],
                "processed_topic_ids": sorted(topic_ids),
                "processed_fragment_ids": sorted(fragment_ids),
                "processed_result_ids": sorted(result_ids),
                "unresolved_questions": [],
            }
        return {"status": "success", "content": content}
    if "_provider_generation" in node_id:
        candidate_id = str(request.get("candidate_id") or "")
        if not candidate_id:
            candidate_id = next(
                (value for value in ("candidate_1", "candidate_2") if value in node_id),
                node_id.split("_provider_generation", 1)[0],
            )
        paper_keys = [str(item) for item in request.get("paper_keys") or ()]
        logic = str(request.get("organizing_logic") or "evidence")
        return {"status": "success", "content": {"candidate_id": candidate_id, "organizing_logic": logic, "sections": [{
            "section_id": f"{candidate_id}_section_1",
            "title": f"{logic} synthesis",
            "goal": "Integrate evidence",
            "paper_keys": paper_keys,
            "relation_ids": list(request.get("relation_ids") or ()),
            "claims": ["The provider-bound evidence supports this synthesis."],
        }]}}
    if any(role in node_id for role in ("structure_critique", "coverage_critique", "evidence_critique")):
        return {"status": "success", "content": {"passed": True, "blocking_diagnostics": [], "recommendations": []}}
    if node_id == "arbitration" or node_id.endswith(":arbitration"):
        candidate_ids = [str(item) for item in request.get("candidate_ids") or ()]
        selected = sorted(candidate_ids)[0] if candidate_ids else ""
        content: dict[str, Any] = {"selected_candidate_id": selected}
        contract = request.get("section_coordination_contract") or {}
        if selected in (contract.get("required_if_selected_candidate_sharded") or ()):
            candidate = (request.get("candidate_contents") or {}).get(selected) or {}
            content["section_coordination"] = {
                "candidate_id": selected,
                "merge_groups": [],
                "section_order": [
                    str(section.get("section_id") or "")
                    for section in candidate.get("sections") or ()
                    if isinstance(section, Mapping)
                ],
            }
        return {"status": "success", "content": content}
    return {"status": "success", "content": {"node_id": node_id, "accepted": True}}


def _executor(
    tmp_path: Path,
    *,
    provider: Any = None,
    stability_mode: str = "smoke",
    max_provider_calls: int | None = None,
    max_estimated_cost: float | None = None,
    max_estimated_total_tokens: int | None = 5_000_000,
    pricing_source: str | None = "tests:explicit-rates-v1",
    candidate_count: int = 2,
    technical_shard_target_tokens: int = 0,
    max_source_prompt_tokens: int | None = None,
    enabled_semantic_roles: tuple[str, ...] | None = None,
) -> OutlineV3Executor:
    workspace = JobWorkspace.create(str(tmp_path), "outline", job_id="outline-job")
    registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    return OutlineV3Executor(
        job_id=workspace.job_id,
        summaries=[
            _summary("paper-a", "Study A", "The treatment improved the outcome."),
            _summary("paper-b", "Study B", "The treatment improved the outcome under a different context."),
        ],
        workspace=workspace,
        artifact_registry=registry,
        provider=provider or _configured_test_provider,
        enabled_semantic_roles=enabled_semantic_roles,
        candidate_count=candidate_count,
        stability_mode=stability_mode,
        max_provider_calls=max_provider_calls,
        max_estimated_cost=max_estimated_cost,
        max_estimated_total_tokens=max_estimated_total_tokens,
        pricing_source=pricing_source,
        input_cost_per_1k_tokens=0.0,
        output_cost_per_1k_tokens=0.001,
        reasoning_cost_per_1k_tokens=0.001,
        technical_shard_target_tokens=technical_shard_target_tokens,
        max_source_prompt_tokens=max_source_prompt_tokens,
        cache_read_cost_per_1k_tokens=0.0,
        cache_write_cost_per_1k_tokens=0.0,
    )


def _legacy_semantic_request(request: Mapping[str, Any]) -> dict[str, Any]:
    """Scope old ID-echo validator fixtures to the explicit v1 adapter."""

    return {
        **request,
        "semantic_contract_version": "semantic-evidence-graph-v1",
    }


def _dossier_with_claims(dossier: PaperEvidenceDossier, count: int = 16) -> PaperEvidenceDossier:
    claims = [
        EvidenceClaim(
            claim_id=f"C{index}",
            claim_type="empirical_finding",
            text=(
                f"FINDING_{index} Chinese boundary: 该结果仅在动态定价时成立. "
                "English condition: only when participants know the reference price. " * 8
            ),
            study_id="paper-a:study:1",
            evidence_ids=[f"E{index}"],
            source_locator=f"results:{index}",
            source_summary_hash=dossier.source_summary_hash,
        )
        for index in range(1, count + 1)
    ]
    root_claim = EvidenceClaim(
        claim_id="ROOT_GAP",
        claim_type="author_proposed_gap",
        text="PAPER_LEVEL_GAP_SENTINEL",
        evidence_ids=["E_ROOT"],
        source_locator="discussion:gap",
        source_summary_hash=dossier.source_summary_hash,
    )
    unit = ResearchUnit(
        study_id="paper-a:study:1",
        parent_paper_id="paper-a",
        research_questions=["Does the treatment change perceived fairness?"],
        definitions_and_operationalizations={"construct": ["perceived fairness"]},
        method=["random assignment"],
        sample_or_context=["adult consumers"],
        findings=[claim.text for claim in claims],
        zero_results=["NULL_RESULT_SENTINEL"],
        claims=claims,
        source_locators={"results": [claim.source_locator for claim in claims]},
        evidence_ids=[*[f"E{index}" for index in range(1, count + 1)], "E_ZERO"],
        source_summary_hash=dossier.source_summary_hash,
    )
    return replace(
        dossier,
        research_questions=["Does the treatment change perceived fairness?"],
        findings=[claim.text for claim in claims],
        research_units=[unit],
        claims=[*claims, root_claim],
        source_locators={"discussion": ["discussion:gap"], "findings": ["results"]},
        evidence_ids_by_field={
            "findings": [f"E{index}" for index in range(1, count + 1)],
            "zero_results": ["E_ZERO"],
            "research_gaps": ["E_ROOT"],
        },
        evidence_text_by_id={
            **{f"E{index}": claim.text for index, claim in enumerate(claims, start=1)},
            "E_ZERO": "NULL_RESULT_SENTINEL",
            "E_ROOT": "PAPER_LEVEL_GAP_SENTINEL",
        },
        status="ready",
    )


def _materialized_claims(unit: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    claims = [
        item for item in unit.get("claims") or () if isinstance(item, Mapping)
    ]
    claims.extend(
        claim
        for study in unit.get("study_units") or ()
        if isinstance(study, Mapping)
        for claim in study.get("claims") or ()
        if isinstance(claim, Mapping)
    )
    return claims


def test_outline_v3_fixture_executes_evidence_bound_adoption(tmp_path: Path) -> None:
    executor = _executor(tmp_path)
    result = executor.run()

    assert result.ok is True
    assert result.status == "ready_for_adoption"
    assert result.adopted is False

    packet_path = Path(result.artifacts["section_evidence_packets"])
    packet = json.loads(packet_path.read_text(encoding="utf-8"))["payload"]
    first = packet["packets"][0]
    assert first["paper_keys"] == ["paper-a", "paper-b"]
    assert first["evidence_items"]
    assert first["findings"]
    assert first["source_summary_hashes"]
    assert first["retrieval_provenance"]["source_artifacts"]

    ledger = ProviderRuntimeLedger(result.artifacts["provider_receipts"])
    assert ledger.list_receipts()

    audit_path = Path(result.artifacts["request_payload_audit"])
    audit_rows = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert audit_rows
    assert all(row["schema_version"] == "outline_request_payload_audit/v1" for row in audit_rows)
    assert all(row["serialized_bytes"] > 0 for row in audit_rows)
    assert all(row["mock_live"] == "mock" for row in audit_rows)
    assert {"paper-a", "paper-b"}.issubset({key for row in audit_rows for key in row["paper_keys"]})

    graph = json.loads(Path(result.artifacts["hierarchical_call_graph"]).read_text(encoding="utf-8"))
    assert graph["schema_version"] == "outline_hierarchical_call_graph/v1"
    assert graph["provider_calls"]
    assert graph["edges"]
    assert {"paper-a", "paper-b"}.issubset(set(graph["coverage"]["paper_keys"]))


def test_selected_revision_requires_every_target_to_resolve(tmp_path: Path) -> None:
    def provider(node_id: str, request: Mapping[str, Any]) -> Mapping[str, Any]:
        response = dict(_configured_test_provider(node_id, request))
        if node_id == "arbitration":
            response["content"] = {
                "selected_candidate_id": "candidate_1",
                "selection_reasons": ["fixture"],
                "accepted_recommendations": [
                    {
                        "issue_id": "issue:multi-target",
                        "target_section_ids": [
                            "candidate_1_section_1",
                            "candidate_1_section_missing",
                        ],
                        "operation": "replace_title",
                        "replacement": "Revised title",
                    }
                ],
                "rejected_recommendations": [],
                "unresolved_risks": [],
            }
        return response

    result = _executor(
        tmp_path,
        provider=provider,
        stability_mode="off",
    ).run()

    assert result.ok is False
    assert result.status == "blocked"
    assert any("issue:multi-target" in item for item in result.diagnostics)


def test_outline_v3_without_explicit_adoption_stops_at_ready_for_adoption(tmp_path: Path) -> None:
    result = _executor(tmp_path).run()

    assert result.ok is True
    assert result.status == "ready_for_adoption"
    assert result.adopted is False
    assert "adoption" not in result.artifacts


def test_outline_v3_stability_provider_call_budget_rejects_before_transport(tmp_path: Path) -> None:
    transport_calls: list[str] = []

    def provider(node_id: str, request: Mapping[str, Any]) -> Mapping[str, Any]:
        transport_calls.append(node_id)
        return _configured_test_provider(node_id, request)

    result = _executor(
        tmp_path,
        provider=provider,
        stability_mode="smoke",
        max_provider_calls=1,
    ).run()

    assert result.ok is False
    assert result.status == "blocked"
    assert transport_calls == []
    assert any("max_provider_calls_exceeded" in item for item in result.diagnostics)
    preflight_paths = list(tmp_path.rglob("stability_preflight_*.json"))
    assert preflight_paths
    preflight = json.loads(preflight_paths[0].read_text(encoding="utf-8"))
    assert preflight["preflight_status"] == "rejected"
    assert preflight["rejection_reason"] == "max_provider_calls_exceeded"


def test_outline_v3_actual_request_cap_blocks_before_provider_post(tmp_path: Path) -> None:
    transport_calls: list[str] = []

    def provider(node_id: str, request: Mapping[str, Any]) -> Mapping[str, Any]:
        transport_calls.append(node_id)
        return _configured_test_provider(node_id, request)

    result = _executor(
        tmp_path,
        provider=provider,
        stability_mode="off",
        max_source_prompt_tokens=1,
    ).run()

    assert result.ok is False
    assert result.status == "blocked"
    assert transport_calls == []


def test_outline_v3_stability_cost_budget_rejects_before_transport(tmp_path: Path) -> None:
    transport_calls: list[str] = []

    def provider(node_id: str, request: Mapping[str, Any]) -> Mapping[str, Any]:
        transport_calls.append(node_id)
        return _configured_test_provider(node_id, request)

    result = _executor(
        tmp_path,
        provider=provider,
        stability_mode="smoke",
        max_estimated_cost=0.0,
    ).run()

    assert result.ok is False
    assert result.status == "blocked"
    assert transport_calls == []
    assert any("max_estimated_cost_exceeded" in item for item in result.diagnostics)
    preflight_paths = list(tmp_path.rglob("stability_preflight_*.json"))
    assert preflight_paths
    preflight = json.loads(preflight_paths[0].read_text(encoding="utf-8"))
    assert preflight["preflight_status"] == "rejected"
    assert preflight["rejection_reason"] == "max_estimated_cost_exceeded"


def test_outline_v3_unknown_pricing_does_not_claim_a_monetary_ceiling(tmp_path: Path) -> None:
    result = _executor(
        tmp_path,
        pricing_source=None,
        max_estimated_cost=0.0,
    ).run()

    assert result.ok is True
    preflight_paths = list(tmp_path.rglob("stability_preflight_*.json"))
    assert preflight_paths
    preflight = json.loads(preflight_paths[0].read_text(encoding="utf-8"))
    assert preflight["cost_status"] == "unknown"
    assert preflight["estimated_cost"] is None
    assert preflight["monetary_ceiling_enforced"] is False
    assert "monetary ceiling was not enforced" in preflight["cost_ceiling_note"]


def test_outline_v3_generic_pricing_source_without_provider_binding_is_unknown(tmp_path: Path) -> None:
    result = _executor(
        tmp_path,
        pricing_source="config:OutlineStability-v1",
    ).run()

    assert result.ok is True
    preflight = json.loads(
        next(tmp_path.rglob("stability_preflight_*.json")).read_text(encoding="utf-8")
    )
    assert preflight["cost_status"] == "unknown"
    assert preflight["estimated_cost"] is None
    assert preflight["monetary_ceiling_enforced"] is False


def test_outline_v3_total_token_ceiling_is_enforced_even_when_pricing_is_unknown(
    tmp_path: Path,
) -> None:
    transport_calls: list[str] = []

    def provider(node_id: str, request: Mapping[str, Any]) -> Mapping[str, Any]:
        transport_calls.append(node_id)
        return _configured_test_provider(node_id, request)

    result = _executor(
        tmp_path,
        provider=provider,
        pricing_source=None,
        max_estimated_cost=0.0,
        max_estimated_total_tokens=1,
    ).run()

    assert result.ok is False
    assert transport_calls == []
    assert any("max_estimated_total_tokens_exceeded" in item for item in result.diagnostics)
    preflight = json.loads(
        next(tmp_path.rglob("stability_preflight_*.json")).read_text(encoding="utf-8")
    )
    assert preflight["preflight_status"] == "rejected"
    assert preflight["rejection_reason"] == "max_estimated_total_tokens_exceeded"


def test_outline_v3_explicit_pricing_estimate_changes_with_input_volume(tmp_path: Path) -> None:
    short = _executor(tmp_path / "short")
    short.input_cost_per_1k_tokens = 0.001
    short._preflight_stability_budget()
    short_estimate = short.stability_preflight["estimated_cost"]

    long = _executor(tmp_path / "long")
    long.input_cost_per_1k_tokens = 0.001
    # Stay below the exact flat-relation input cap while still increasing the
    # provider-visible source volume used by the pricing estimate.
    long.summaries[0]["core_analysis"]["summary"] += " long-evidence " * 2000
    long._preflight_stability_budget()
    long_estimate = long.stability_preflight["estimated_cost"]

    assert short_estimate is not None
    assert long_estimate is not None
    assert long_estimate > short_estimate


def test_relation_shard_plan_is_lossless_and_estimate_is_bounded(tmp_path: Path) -> None:
    executor = _executor(
        tmp_path,
        stability_mode="off",
        technical_shard_target_tokens=1,
    )
    evidence = build_outline_evidence_views(executor.summaries, executor.job_id)
    plan = executor._build_relation_shard_plan(evidence.views, [])

    assert plan["shard_count"] == len(evidence.views)
    assert plan["coverage"]["input_view_count"] == len(evidence.views)
    assert plan["coverage"]["planned_view_count"] == len(evidence.views)
    assert plan["coverage"]["missing_view_hashes"] == []
    assert all(item["estimated_input_tokens"] >= 1 for item in plan["shards"])


def test_shard_target_changes_real_relation_subrequest_membership(tmp_path: Path) -> None:
    def run_with_target(target_tokens: int) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        calls: list[dict[str, Any]] = []

        def provider(node_id: str, request: Mapping[str, Any]) -> Mapping[str, Any]:
            calls.append({"node_id": node_id, "request": dict(request)})
            ids = [
                str(item.get("relation_id") or "")
                for item in request.get("relation_candidates") or ()
                if isinstance(item, Mapping) and str(item.get("relation_id") or "")
            ]
            rejected = [
                {"relation_id": relation_id, "reason": "not confirmed in fixture"}
                for relation_id in ids
                if not relation_id.endswith("0")
            ]
            confirmed = [relation_id for relation_id in ids if relation_id not in {item["relation_id"] for item in rejected}]
            return {
                "status": "success",
                "content": {
                    "confirmed_relation_ids": confirmed,
                    "rejected_relations": rejected,
                },
            }

        executor = _executor(
            tmp_path / str(target_tokens),
            provider=provider,
            stability_mode="off",
            technical_shard_target_tokens=target_tokens,
        )
        evidence = build_outline_evidence_views(executor.summaries, executor.job_id)
        ledger = build_global_corpus_ledger(evidence)
        matrix = build_multi_view_matrix(evidence)
        relation_map = build_global_relation_map(evidence, matrix, ledger)
        relations = [item.to_dict() for item in relation_map.relations]
        plan = executor._build_relation_shard_plan(evidence.views, relations)
        executor._run_hierarchical_relation_adjudication(
            evidence_views=evidence.views,
            relation_candidates=relations,
            shard_plan=plan,
            relation_contract={
                "must_return_confirmed_relation_ids": True,
                "must_reject_without_recorded_evidence": True,
                "allowed_relation_ids": [item["relation_id"] for item in relations],
            },
            relation_dependencies={"evidence": hash_json(evidence.to_dict())},
        )
        return plan, calls

    small_plan, small_calls = run_with_target(500)
    large_plan, large_calls = run_with_target(5_000)

    assert small_plan["shard_count"] > large_plan["shard_count"]
    assert len(small_calls) > len(large_calls)
    assert {
        str((request.get("hierarchy") or {}).get("shard_id") or "")
        for item in small_calls
        for request in [item["request"]]
    } != {
        str((request.get("hierarchy") or {}).get("shard_id") or "")
        for item in large_calls
        for request in [item["request"]]
    }
    assert all(
        (request.get("hierarchy") or {}).get("target_tokens") in {500, 5_000}
        for item in [*small_calls, *large_calls]
        for request in [item["request"]]
    )


def test_oversized_cross_relation_blocks_before_any_local_provider_call(tmp_path: Path) -> None:
    calls: list[str] = []

    def provider(node_id: str, _request: Mapping[str, Any]) -> Mapping[str, Any]:
        calls.append(node_id)
        raise AssertionError("no relation transport is admissible")

    executor = _executor(
        tmp_path,
        provider=provider,
        stability_mode="off",
        technical_shard_target_tokens=500,
    )
    evidence = build_outline_evidence_views(executor.summaries, executor.job_id)
    paper_a, paper_b = [view.paper_key for view in evidence.views]
    relation_candidates = [
        {"relation_id": "local", "paper_keys": [paper_a]},
        {"relation_id": "cross", "paper_keys": [paper_a, paper_b]},
    ]
    shard_plan = {
        "shards": [
            {
                "shard_id": "s1", "paper_keys": [paper_a],
                "relation_candidate_ids": ["local"],
                "evidence_chunks": [
                    {"paper_key": paper_a, "evidence_chunk_id": "a1", "findings": ["local source"]}
                ],
            },
            {
                "shard_id": "s2", "paper_keys": [paper_b],
                "relation_candidate_ids": [],
                "evidence_chunks": [
                    {"paper_key": paper_b, "evidence_chunk_id": "b1", "findings": ["X" * 150_000]}
                ],
            },
        ]
    }
    with pytest.raises(OutlineV3ExecutionError, match="complete relation request"):
        executor._run_hierarchical_relation_adjudication(
            evidence_views=evidence.views,
            relation_candidates=relation_candidates,
            shard_plan=shard_plan,
            relation_contract={"allowed_relation_ids": ["local", "cross"]},
            relation_dependencies={"evidence": "fixture-evidence"},
        )
    assert calls == []


def test_evidence_projection_preserves_tail_and_chunks_long_view(tmp_path: Path) -> None:
    executor = _executor(
        tmp_path,
        stability_mode="off",
        technical_shard_target_tokens=1,
    )
    evidence = build_outline_evidence_views(executor.summaries, executor.job_id)
    view = replace(
        evidence.views[0],
        findings=[f"finding-{index}" for index in range(1, 12)],
        theories=["theory-tail-" + ("x" * 1400)],
    )

    projected = executor._prompt_evidence_views([view])[0]
    assert len(projected["findings"]) == 11
    assert projected["findings"][-1] == "finding-11"
    chunks = executor._prompt_evidence_chunks(view)
    assert len(chunks) > 1
    assert any("theory-tail-" in value for chunk in chunks for value in chunk["theories"])
    assert any("finding-11" in value for chunk in chunks for value in chunk["findings"])

    plan = executor._build_relation_shard_plan(
        [view],
        [{"relation_id": "r-long", "paper_keys": [view.paper_key]}],
    )
    assert plan["coverage"]["missing_chunk_ids"] == []
    assert plan["coverage"]["input_chunk_count"] == len(chunks)


def test_topic_provider_request_materializes_complete_dossier_unit(
    tmp_path: Path,
) -> None:
    """Semantic topic requests carry actual study/claim evidence, not hashes only."""

    executor = _executor(
        tmp_path,
        stability_mode="off",
        technical_shard_target_tokens=32_000,
    )
    evidence = build_outline_evidence_views(executor.summaries, executor.job_id)
    ledger = build_global_corpus_ledger(evidence)
    matrix = build_multi_view_matrix(evidence)
    relation_map = build_global_relation_map(evidence, matrix, ledger)
    content_layers = build_paper_content_layers(
        executor.summaries,
        evidence,
        job_id=executor.job_id,
    )
    semantic_plan = build_semantic_chunk_plan(
        content_layers,
        relation_map,
        candidate_count=executor.candidate_count,
        physical_call_limit=24,
    )
    topic_plan = build_topic_synthesis_plan(semantic_plan)
    routes = {topic.topic_id: topic for topic in semantic_plan.topics}
    request = executor._build_topic_provider_request(
        [topic_plan[0]],
        topic_routes=routes,
        evidence_model=evidence,
        content_layers_model=content_layers,
        batch_index=1,
    )
    assert request["output_contract"]["semantic_result_contract_version"] == "bounded-topic-synthesis/v3"
    assert request["output_contract"]["response_root_type"].startswith("single JSON object")
    assert "Do not echo source_locators" in request["output_contract"]["source_locator_policy"]
    assert "null/zero findings" in request["output_contract"]["conciseness_policy"]
    assert request["output_contract"]["required_top_level_keys"] == [
        "topics", "processed_fragment_ids", "claims", "unresolved_questions"
    ]

    unit = request["evidence_units"][0]
    assert unit["projection"].startswith("scoped_")
    assert "study_units" in unit
    assert "claims" not in unit
    assert "evidence_ids_by_field" in unit
    assert "source_locators" in unit
    assert unit["evidence_unit_id"]
    assert unit["evidence_unit_hash"]
    assert "semantic_fields" not in unit
    assert _materialized_claims(unit)
    assert unit["study_units"][0]["claims"]
    if unit["study_units"]:
        assert unit["study_units"][0]["shared_context"]["method"]


def test_complete_topic_request_preserves_long_bilingual_qualifier_tail(
    tmp_path: Path,
) -> None:
    """A long bilingual finding keeps its terminal boundary in the wire request."""

    executor = _executor(
        tmp_path,
        stability_mode="off",
        technical_shard_target_tokens=32_000,
        max_source_prompt_tokens=32_000,
    )
    bilingual = (
        "中文条件说明：该结果只在动态定价且消费者知道参照价格时成立。 "
        "English boundary: the effect is conditional on dynamic pricing and a known reference price. "
    ) * 12
    tail = bilingual + "TAIL_QUALIFIER_MUST_SURVIVE"
    executor.summaries[0]["core_analysis"]["findings"] = tail
    executor.summaries[0]["core_analysis"]["limitations"] = tail

    evidence = build_outline_evidence_views(executor.summaries, executor.job_id)
    ledger = build_global_corpus_ledger(evidence)
    matrix = build_multi_view_matrix(evidence)
    relation_map = build_global_relation_map(evidence, matrix, ledger)
    content_layers = build_paper_content_layers(
        executor.summaries,
        evidence,
        job_id=executor.job_id,
    )
    semantic_plan = build_semantic_chunk_plan(
        content_layers,
        relation_map,
        candidate_count=executor.candidate_count,
        physical_call_limit=24,
    )
    topic_plan = build_topic_synthesis_plan(semantic_plan)
    routes = {topic.topic_id: topic for topic in semantic_plan.topics}
    topic = next(
        topic
        for topic in topic_plan
        if "paper-a" in topic.paper_ids
        and "context" in routes[topic.topic_id].dimensions
    )
    request = executor._build_topic_provider_request(
        [topic],
        topic_routes=routes,
        evidence_model=evidence,
        content_layers_model=content_layers,
        batch_index=1,
    )
    serialized = json.dumps(request, ensure_ascii=False, sort_keys=True)
    budget = executor.profile.estimate_request(
        executor._attach_prompt_authority("topic_synthesis:long-bilingual", request)
    )

    assert "中文条件说明" in serialized
    assert "English boundary" in serialized
    assert "TAIL_QUALIFIER_MUST_SURVIVE" in serialized
    assert budget["estimated_input_tokens"] <= 32_000


def test_complete_topic_units_keep_scope_and_conserve_claim_evidence(tmp_path: Path) -> None:
    executor = _executor(tmp_path, stability_mode="off", technical_shard_target_tokens=32_000)
    evidence = build_outline_evidence_views(executor.summaries, executor.job_id)
    layers = build_paper_content_layers(executor.summaries, evidence, job_id=executor.job_id)
    base = layers.dossier_by_paper["paper-a"]
    dossier = _dossier_with_claims(base)
    view = next(item for item in evidence.views if item.paper_key == "paper-a")

    chunks = executor._complete_topic_evidence_units(
        view,
        dossier,
        fields=["findings", "limitations"],
        chunk_target_tokens=700,
    )

    expected_claim_ids = {*(f"C{index}" for index in range(1, 17)), "ROOT_GAP"}
    actual_claim_ids = {
        str(claim.get("claim_id") or "")
        for chunk in chunks
        for claim in _materialized_claims(chunk)
    }
    assert len(chunks) > 2
    assert actual_claim_ids == expected_claim_ids
    assert all(chunk.get("chunk_complete_for_claim") is True for chunk in chunks)
    study_chunks = [chunk for chunk in chunks if chunk.get("unit_scope") == "study"]
    assert all(chunk.get("chunk_complete_for_study") is False for chunk in study_chunks)
    assert all("semantic_fields" not in chunk for chunk in study_chunks)
    assert sum(
        "FINDING_16" in json.dumps(chunk, ensure_ascii=False)
        for chunk in study_chunks
    ) == 1
    assert sum(
        json.dumps(chunk, ensure_ascii=False).count("NULL_RESULT_SENTINEL")
        for chunk in chunks
    ) == 1
    assert sum(
        json.dumps(chunk, ensure_ascii=False).count("PAPER_LEVEL_GAP_SENTINEL")
        for chunk in chunks
    ) == 1
    actual_evidence_ids: set[str] = set()
    for chunk in chunks:
        actual_evidence_ids.update(str(value) for value in chunk.get("evidence_text_by_id", {}))
        for values in (chunk.get("evidence_ids_by_field") or {}).values():
            actual_evidence_ids.update(str(value) for value in values)
        for claim in _materialized_claims(chunk):
            actual_evidence_ids.update(
                str(value) for value in claim.get("evidence_ids") or ()
            )
    assert actual_evidence_ids == {
        *(f"E{index}" for index in range(1, 17)),
        "E_ZERO",
        "E_ROOT",
    }


def test_study_scope_flags_are_partial_for_filtered_claims_and_complete_only_for_full_study(
    tmp_path: Path,
) -> None:
    executor = _executor(tmp_path, stability_mode="off")
    evidence = build_outline_evidence_views(executor.summaries, executor.job_id)
    layers = build_paper_content_layers(
        executor.summaries,
        evidence,
        job_id=executor.job_id,
    )
    dossier = _dossier_with_claims(layers.dossier_by_paper["paper-a"], count=3)
    view = next(item for item in evidence.views if item.paper_key == "paper-a")

    filtered = executor._complete_topic_evidence_units(
        view,
        dossier,
        fields=["findings"],
        required_evidence_ids=["E1"],
        chunk_target_tokens=30_000,
    )
    filtered_study = next(item for item in filtered if item.get("unit_scope") == "study")
    filtered_claims = _materialized_claims(filtered_study)
    assert {str(item["claim_id"]) for item in filtered_claims} == {"C1"}
    assert filtered_study["chunk_complete_for_claim"] is True
    assert filtered_study["chunk_complete_for_study"] is False
    assert filtered_study["coverage_status"] == "complete_for_claim"
    assert "claims" not in filtered_study
    assert filtered_claims[0]["text"] in json.dumps(filtered_study, ensure_ascii=False)
    serialized_filtered_study = json.dumps(filtered_study, ensure_ascii=False)
    assert serialized_filtered_study.count(filtered_claims[0]["text"]) == 1
    assert "FINDING_2" not in json.dumps(filtered_study, ensure_ascii=False)

    full = executor._complete_topic_evidence_units(
        view,
        dossier,
        fields=["findings", "limitations"],
        chunk_target_tokens=30_000,
    )
    full_study = next(item for item in full if item.get("unit_scope") == "study")
    assert {str(item["claim_id"]) for item in _materialized_claims(full_study)} == {
        "C1", "C2", "C3"
    }
    assert full_study["chunk_complete_for_study"] is True
    assert full_study["coverage_status"] == "complete_for_study"


def test_topic_request_keeps_multi_study_claims_and_paper_gap_in_their_scopes(
    tmp_path: Path,
) -> None:
    executor = _executor(tmp_path, stability_mode="off")
    evidence = build_outline_evidence_views(executor.summaries, executor.job_id)
    layers = build_paper_content_layers(
        executor.summaries,
        evidence,
        job_id=executor.job_id,
    )
    base = layers.dossier_by_paper["paper-a"]
    study_one = EvidenceClaim(
        claim_id="S1_CLAIM",
        claim_type="empirical_finding",
        text="Study one result",
        study_id="paper-a:study:1",
        evidence_ids=["E_S1"],
        source_locator="results:study-1",
        source_summary_hash=base.source_summary_hash,
    )
    study_two = EvidenceClaim(
        claim_id="S2_CLAIM",
        claim_type="empirical_finding",
        text="Study two result",
        study_id="paper-a:study:2",
        evidence_ids=["E_S2"],
        source_locator="results:study-2",
        source_summary_hash=base.source_summary_hash,
    )
    paper_gap = EvidenceClaim(
        claim_id="PAPER_GAP",
        claim_type="author_proposed_gap",
        text="Paper-level gap",
        evidence_ids=["E_GAP"],
        source_locator="discussion:gap",
        source_summary_hash=base.source_summary_hash,
    )
    dossier = replace(
        base,
        research_units=[
            ResearchUnit(
                study_id="paper-a:study:1",
                parent_paper_id="paper-a",
                findings=[study_one.text],
                claims=[study_one],
                evidence_ids=["E_S1"],
                source_locators={"results": [study_one.source_locator]},
                source_summary_hash=base.source_summary_hash,
            ),
            ResearchUnit(
                study_id="paper-a:study:2",
                parent_paper_id="paper-a",
                findings=[study_two.text],
                claims=[study_two],
                evidence_ids=["E_S2"],
                source_locators={"results": [study_two.source_locator]},
                source_summary_hash=base.source_summary_hash,
            ),
        ],
        claims=[study_one, study_two, paper_gap],
        evidence_ids_by_field={
            "findings": ["E_S1", "E_S2"],
            "research_gaps": ["E_GAP"],
        },
        evidence_text_by_id={
            "E_S1": study_one.text,
            "E_S2": study_two.text,
            "E_GAP": paper_gap.text,
        },
    )
    layers = replace(
        layers,
        dossiers=[dossier if item.paper_id == "paper-a" else item for item in layers.dossiers],
    )
    view = next(item for item in evidence.views if item.paper_key == "paper-a")
    units = executor._complete_topic_evidence_units(
        view,
        dossier,
        fields=["findings"],
    )
    indexes = [int(item["chunk_index"]) for item in units]
    topic = TopicSynthesis(
        topic_id="topic:multi-study",
        fragment_id="topic:multi-study:fragment:1",
        paper_ids=["paper-a"],
        supporting_evidence_ids=["E_S1", "E_S2", "E_GAP"],
        evidence_unit_indexes={"paper-a": indexes},
    )
    request = executor._build_topic_provider_request(
        [topic],
        topic_routes={
            topic.topic_id: TopicRoute(
                topic_id=topic.topic_id,
                question="Compare the studies while retaining paper-level gaps",
                paper_ids=["paper-a"],
                dimensions=["finding", "gap"],
            )
        },
        evidence_model=evidence,
        content_layers_model=layers,
        batch_index=1,
    )

    materialized = request["evidence_units"]
    study_claims = {
        str(claim["claim_id"]): str(study["study_id"])
        for unit in materialized
        for study in unit.get("study_units") or ()
        for claim in study.get("claims") or ()
    }
    paper_claims = {
        str(claim["claim_id"])
        for unit in materialized
        for claim in unit.get("claims") or ()
    }
    assert study_claims == {
        "S1_CLAIM": "paper-a:study:1",
        "S2_CLAIM": "paper-a:study:2",
    }
    assert paper_claims == {"PAPER_GAP"}
    assert {"E_S1", "E_S2", "E_GAP"}.issubset(
        set(request["topics"][0]["planned_evidence_ids"])
    )


def test_topic_builder_unions_same_paper_fragment_filters_and_checks_wire_ids(
    tmp_path: Path,
) -> None:
    executor = _executor(tmp_path, stability_mode="off")
    evidence = build_outline_evidence_views(executor.summaries, executor.job_id)
    layers = build_paper_content_layers(executor.summaries, evidence, job_id=executor.job_id)
    dossier = _dossier_with_claims(layers.dossier_by_paper["paper-a"])
    layers = replace(
        layers,
        dossiers=[dossier if item.paper_id == "paper-a" else item for item in layers.dossiers],
    )
    units = executor._complete_topic_evidence_units(
        next(item for item in evidence.views if item.paper_key == "paper-a"),
        dossier,
        fields=["findings"],
        chunk_target_tokens=700,
    )
    indexes = [int(item["chunk_index"]) for item in units]
    first_indexes = indexes[: len(indexes) // 2]
    second_indexes = indexes[len(indexes) // 2 :]
    route = TopicRoute(
        topic_id="topic:test",
        question="Compare the evidence",
        paper_ids=["paper-a"],
        dimensions=["context"],
    )
    first = TopicSynthesis(
        topic_id=route.topic_id,
        fragment_id="topic:test:fragment:one",
        paper_ids=["paper-a"],
        supporting_evidence_ids=dossier.evidence_ids,
        evidence_unit_indexes={"paper-a": first_indexes},
        evidence_chunk_target_tokens=700,
    )
    second = TopicSynthesis(
        topic_id=route.topic_id,
        fragment_id="topic:test:fragment:two",
        paper_ids=["paper-a"],
        supporting_evidence_ids=dossier.evidence_ids,
        evidence_unit_indexes={"paper-a": second_indexes},
        evidence_chunk_target_tokens=700,
    )
    restored = TopicSynthesis.from_dict(first.to_dict())
    assert restored.evidence_unit_indexes == first.evidence_unit_indexes
    assert restored.fragment_id == first.fragment_id

    request = executor._build_topic_provider_request(
        [first, second],
        topic_routes={route.topic_id: route},
        evidence_model=evidence,
        content_layers_model=layers,
        batch_index=1,
    )
    actual_claim_ids = {
        str(claim.get("claim_id") or "")
        for unit in request["evidence_units"]
        for claim in _materialized_claims(unit)
    }
    assert actual_claim_ids == {*(f"C{index}" for index in range(1, 17)), "ROOT_GAP"}
    assert request["planned_evidence_unit_ids"] == sorted(
        unit["evidence_unit_id"] for unit in request["evidence_units"]
    )
    assert {item["fragment_id"] for item in request["topics"]} == {
        first.fragment_id,
        second.fragment_id,
    }


@pytest.mark.parametrize(
    ("invalid_indexes", "include_unfiltered_sibling"),
    [
        ([1, 1], False),
        ([999], True),
        ([True], False),
        ([1.0], False),
        (["1"], False),
        ([], True),
    ],
)
def test_topic_builder_rejects_each_invalid_fragment_before_union(
    tmp_path: Path,
    invalid_indexes: list[Any],
    include_unfiltered_sibling: bool,
) -> None:
    executor = _executor(tmp_path, stability_mode="off")
    evidence = build_outline_evidence_views(executor.summaries, executor.job_id)
    layers = build_paper_content_layers(executor.summaries, evidence, job_id=executor.job_id)
    route = TopicRoute(
        topic_id="topic:selection-contract",
        question="Assess the paper's evidence",
        paper_ids=["paper-a"],
        dimensions=["method"],
    )
    bad_fragment = TopicSynthesis(
        topic_id=route.topic_id,
        fragment_id="topic:selection-contract:bad",
        paper_ids=["paper-a"],
        evidence_unit_indexes={"paper-a": invalid_indexes},
    )
    batch = [bad_fragment]
    if include_unfiltered_sibling:
        batch.append(
            TopicSynthesis(
                topic_id=route.topic_id,
                fragment_id="topic:selection-contract:all",
                paper_ids=["paper-a"],
            )
        )
    with pytest.raises(OutlineV3ExecutionError, match="evidence-unit selection"):
        executor._build_topic_provider_request(
            batch,
            topic_routes={route.topic_id: route},
            evidence_model=evidence,
            content_layers_model=layers,
            batch_index=1,
        )


def test_topic_batch_planning_groups_same_paper_tasks_before_other_papers(
    tmp_path: Path,
) -> None:
    executor = _executor(
        tmp_path,
        stability_mode="off",
        technical_shard_target_tokens=32_000,
        max_source_prompt_tokens=32_000,
    )
    evidence = build_outline_evidence_views(executor.summaries, executor.job_id)
    layers = build_paper_content_layers(executor.summaries, evidence, job_id=executor.job_id)
    dossier_by_paper = layers.dossier_by_paper
    topic_plan = [
        TopicSynthesis(
            topic_id="topic:z-paper-a",
            fragment_id="fragment:z-paper-a",
            paper_ids=["paper-a"],
            supporting_evidence_ids=list(dossier_by_paper["paper-a"].evidence_ids),
        ),
        TopicSynthesis(
            topic_id="topic:m-paper-b",
            fragment_id="fragment:m-paper-b",
            paper_ids=["paper-b"],
            supporting_evidence_ids=list(dossier_by_paper["paper-b"].evidence_ids),
        ),
        TopicSynthesis(
            topic_id="topic:a-paper-a",
            fragment_id="fragment:a-paper-a",
            paper_ids=["paper-a"],
            supporting_evidence_ids=list(dossier_by_paper["paper-a"].evidence_ids),
        ),
    ]
    topic_routes = {
        item.topic_id: TopicRoute(
            topic_id=item.topic_id,
            question=f"Assess {item.topic_id}",
            paper_ids=list(item.paper_ids),
            dimensions=["finding"],
        )
        for item in topic_plan
    }
    expanded, batches, request_plan = executor._plan_topic_provider_batches(
        topic_plan,
        topic_routes=topic_routes,
        evidence_model=evidence,
        content_layers_model=layers,
        profile=executor.profile,
    )

    assert [tuple(item.paper_ids) for item in expanded] == [
        ("paper-a",),
        ("paper-a",),
        ("paper-b",),
    ]
    assert len(batches) == 1
    assert request_plan[0]["planned_wire_ids_equal_materialized_ids"] is True
    individual_a_units = sum(
        len(executor._build_topic_provider_request(
            [item],
            topic_routes=topic_routes,
            evidence_model=evidence,
            content_layers_model=layers,
            batch_index=index,
        )["evidence_units"])
        for index, item in enumerate(expanded[:2], start=1)
    )
    combined_a_request = executor._build_topic_provider_request(
        expanded[:2],
        topic_routes=topic_routes,
        evidence_model=evidence,
        content_layers_model=layers,
        batch_index=1,
    )
    assert len(combined_a_request["evidence_units"]) < individual_a_units


def test_topic_builder_materializes_only_the_required_evidence_for_a_dimension(
    tmp_path: Path,
) -> None:
    executor = _executor(tmp_path, stability_mode="off")
    evidence = build_outline_evidence_views(executor.summaries, executor.job_id)
    layers = build_paper_content_layers(executor.summaries, evidence, job_id=executor.job_id)
    dossier = _dossier_with_claims(layers.dossier_by_paper["paper-a"])
    layers = replace(
        layers,
        dossiers=[dossier if item.paper_id == "paper-a" else item for item in layers.dossiers],
    )
    topic = TopicSynthesis(
        topic_id="topic:focused",
        fragment_id="topic:focused:paper-a",
        paper_ids=["paper-a"],
        supporting_evidence_ids=["E1"],
    )
    route = TopicRoute(
        topic_id=topic.topic_id,
        question="Review the one required claim",
        paper_ids=["paper-a"],
        dimensions=["mechanism"],
    )

    request = executor._build_topic_provider_request(
        [topic],
        topic_routes={route.topic_id: route},
        evidence_model=evidence,
        content_layers_model=layers,
        batch_index=1,
    )
    actual_claim_ids = {
        str(claim.get("claim_id") or "")
        for unit in request["evidence_units"]
        for claim in _materialized_claims(unit)
    }
    serialized = json.dumps(request, ensure_ascii=False)

    assert actual_claim_ids == {"C1"}
    assert request["topics"][0]["planned_evidence_ids"] == ["E1"]
    assert "FINDING_2" not in serialized
    assert "PAPER_LEVEL_GAP_SENTINEL" not in serialized
    assert "NULL_RESULT_SENTINEL" not in serialized


def test_complete_topic_units_reject_invalid_explicit_filter(tmp_path: Path) -> None:
    executor = _executor(tmp_path, stability_mode="off")
    evidence = build_outline_evidence_views(executor.summaries, executor.job_id)
    layers = build_paper_content_layers(executor.summaries, evidence, job_id=executor.job_id)
    dossier = _dossier_with_claims(layers.dossier_by_paper["paper-a"])
    view = next(item for item in evidence.views if item.paper_key == "paper-a")
    with pytest.raises(Exception, match="empty or out of range"):
        executor._complete_topic_evidence_units(
            view,
            dossier,
            fields=["findings"],
            chunk_indexes=[999],
            chunk_target_tokens=700,
        )


def test_semantic_transport_receives_concrete_node_identity(tmp_path: Path) -> None:
    received_node_ids: list[str] = []

    def provider(node_id: str, _request: Mapping[str, Any]) -> Mapping[str, Any]:
        received_node_ids.append(node_id)
        return {
            "status": "success",
            "content": {
                "topics": [{"topic_id": "topic:one", "fragment_id": "fragment:one"}],
                "processed_fragment_ids": ["fragment:one"],
                "claims": [],
                "unresolved_questions": [],
            },
        }

    executor = _executor(tmp_path, provider=provider, stability_mode="off")
    from runtime.outline_v3_dag import OutlineNodeRecord

    topic_node_id = "topic_synthesis_provider:batch:1"
    executor._dag = replace(
        executor._dag,
        nodes=[*executor._dag.nodes, OutlineNodeRecord(node_id=topic_node_id)],
    )
    executor._run_semantic_provider_call(
        topic_node_id,
        {
            "topics": [
                {
                    "topic_id": "topic:one",
                    "fragment_id": "fragment:one",
                    "paper_ids": ["paper-a"],
                }
            ],
            "evidence_units": [],
        },
        {},
    )

    assert received_node_ids == [topic_node_id]


def test_semantic_provider_output_rejects_unknown_evidence_identity(
    tmp_path: Path,
) -> None:
    executor = _executor(tmp_path, stability_mode="off")
    request = {
        "topics": [{
            "topic_id": "topic:one",
            "fragment_id": "fragment:one",
            "paper_ids": ["paper-a"],
        }],
        "evidence_units": [
            {
                "paper_key": "paper-a",
                "unit_scope": "study",
                "study_units": [{"study_id": "study-a", "claims": []}],
                "claims": [],
                "evidence_ids_by_field": {"findings": ["evidence-a"]},
                "evidence_text_by_id": {"evidence-a": "finding"},
            }
        ],
    }

    with pytest.raises(Exception, match="outside its evidence contract"):
        executor._validate_semantic_provider_output(
            "topic_synthesis_provider:batch:1",
            request,
            {
                "topics": [{
                    "topic_id": "topic:one",
                    "fragment_id": "fragment:one",
                    "status": "processed",
                    "supporting_evidence_ids": ["evidence-a"],
                }],
                "processed_fragment_ids": ["fragment:one"],
                "claims": [
                    {
                        "claim_id": "synthesis:topic_synthesis:bad-evidence",
                        "fragment_id": "fragment:one",
                        "paper_key": "paper-a",
                        "study_id": "study-a",
                        "evidence_ids": ["evidence-not-supplied"],
                    }
                ],
                "unresolved_questions": [],
            },
        )


def test_invalid_semantic_output_is_not_persisted_as_reusable_success(tmp_path: Path) -> None:
    node_id = "topic_synthesis_provider:batch:invalid"

    def invalid_provider(_provider_node: str, request: Mapping[str, Any]) -> Mapping[str, Any]:
        fragment = request["topics"][0]["fragment_id"]
        topic_id = request["topics"][0]["topic_id"]
        return {
            "status": "success",
            "content": {
                "topics": [{
                    "topic_id": topic_id,
                    "fragment_id": fragment,
                    "status": "processed",
                    "supporting_evidence_ids": ["evidence-a"],
                }],
                "processed_fragment_ids": [fragment],
                "claims": [{
                    "claim_id": "synthesis:topic_synthesis:invalid",
                    "fragment_id": fragment,
                    "paper_key": "paper-a",
                    "evidence_ids": ["unknown-evidence"],
                }],
                "unresolved_questions": [],
            },
        }

    executor = _executor(tmp_path, provider=invalid_provider, stability_mode="off")
    request = {
        "topics": [{
            "topic_id": "topic:one",
            "fragment_id": "fragment:one",
            "paper_ids": ["paper-a"],
        }],
        "evidence_units": [{
            "paper_key": "paper-a",
            "unit_scope": "study",
            "study_units": [{"study_id": "study-a", "claims": []}],
            "claims": [],
            "evidence_ids_by_field": {"findings": ["evidence-a"]},
            "evidence_text_by_id": {"evidence-a": "finding"},
        }],
        "output_contract": {},
    }

    with pytest.raises(Exception, match="outside its evidence contract"):
        executor._run_semantic_provider_call(node_id, request, {"input": "hash"})

    artifact_id = f"outline-v3:semantic-provider:{hash_text(node_id)[:24]}"
    assert executor.registry.get(artifact_id) is None


def test_semantic_validator_rejects_unknown_ids_for_empty_cross_allowlist(
    tmp_path: Path,
) -> None:
    executor = _executor(tmp_path, stability_mode="off")
    request = {"topic_synthesis": [{"topic_id": "topic:A", "paper_ids": ["paper-A"]}]}
    result = {
        "comparisons": [{"paper_key": "NO_SUCH_PAPER", "evidence_ids": ["NO_SUCH_EVIDENCE"]}],
        "bridge_claims": [],
        "processed_topic_ids": ["topic:A"],
        "processed_fragment_ids": [],
        "processed_result_ids": [],
        "processed_relation_ids": [],
        "unresolved_questions": [],
    }
    with pytest.raises(Exception, match="outside its evidence contract"):
        executor._validate_semantic_provider_output(
            "cross_group_comparison_provider",
            _legacy_semantic_request(request),
            result,
        )


def test_topic_results_are_consumed_once_per_fragment_not_fragment_times_batch(
    tmp_path: Path,
) -> None:
    executor = _executor(tmp_path, stability_mode="off")
    topic_plan = [
        TopicSynthesis(
            topic_id="topic:shared",
            fragment_id=f"topic:shared:fragment:{index}",
            paper_ids=["paper-a"],
            supporting_evidence_ids=[f"E{index}"],
        )
        for index in range(1, 4)
    ]
    provider_results = []
    for index, topic in enumerate(topic_plan, start=1):
        fragment_id = str(topic.fragment_id)
        provider_results.append(
            {
                "batch_id": f"batch-{index}",
                "result_id": f"result-{index}",
                "topic_ids": [topic.topic_id],
                "fragment_ids": [fragment_id],
                "topic_fragments": [{
                    "fragment_id": fragment_id,
                    "planned_evidence_unit_ids": [f"unit-{index}"],
                    "planned_evidence_ids": [f"E{index}"],
                }],
                "provider_output": {
                    "processed_fragment_ids": [fragment_id],
                    "topics": [{
                        "topic_id": topic.topic_id,
                        "fragment_id": fragment_id,
                        "status": "processed",
                        "supporting_evidence_ids": [f"E{index}"],
                    }],
                    "claims": [],
                    "unresolved_questions": [],
                },
            }
        )

    grouped = executor._build_topic_synthesis_payloads(topic_plan, provider_results)

    assert len(grouped) == 1
    assert len(grouped[0]["fragments"]) == 3
    consumed = [
        row
        for fragment in grouped[0]["fragments"]
        for row in fragment["provider_results"]
    ]
    assert len(consumed) == 3
    assert all(str(row["result_id"]).startswith("fragment-result:") for row in consumed)
    assert {row["batch_result_id"] for row in consumed} == {
        "result-1",
        "result-2",
        "result-3",
    }
    assert "provider_outputs" not in grouped[0]
    assert {row["result_id"] for row in grouped[0]["provider_output_refs"]} == {
        row["result_id"] for row in consumed
    }


def test_cross_group_reducer_uses_bounded_requests_and_preserves_topic_ids(
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, Mapping[str, Any]]] = []

    def provider(node_id: str, request: Mapping[str, Any]) -> Mapping[str, Any]:
        calls.append((node_id, request))
        rows = [item for item in request.get("topic_synthesis") or () if isinstance(item, Mapping)]
        comparisons = []
        processed_topics: set[str] = set()
        processed_fragments: set[str] = set()
        processed_results: set[str] = set()
        for item in rows:
            item_topic_ids = {
                str(value)
                for value in (
                    *list(item.get("topic_ids") or ()),
                    *list(item.get("processed_topic_ids") or ()),
                )
                if str(value)
            }
            if item.get("topic_id"):
                item_topic_ids.add(str(item["topic_id"]))
            processed_topics.update(item_topic_ids)
            for field_name in ("fragment_id", "fragment_ids", "processed_fragment_ids"):
                values = item.get(field_name) or ()
                if isinstance(values, str):
                    values = [values]
                processed_fragments.update(str(value) for value in values if str(value))
            for field_name in (
                "result_id",
                "batch_result_id",
                "result_ids",
                "batch_result_ids",
                "processed_result_ids",
            ):
                values = item.get(field_name) or ()
                if isinstance(values, str):
                    values = [values]
                processed_results.update(str(value) for value in values if str(value))
            topic_id = sorted(item_topic_ids)[0] if item_topic_ids else ""
            papers = [str(value) for value in item.get("paper_ids") or () if str(value)]
            nested_outputs = [
                output
                for output in item.get("provider_outputs") or ()
                if isinstance(output, Mapping)
            ]
            source_claims = [
                claim
                for output in nested_outputs
                for claim in output.get("claims") or ()
                if isinstance(claim, Mapping)
            ]
            source_claims.extend(
                claim
                for claim in item.get("claims") or ()
                if isinstance(claim, Mapping)
            )
            evidence_ids = sorted({
                str(evidence_id)
                for claim in source_claims
                for evidence_id in claim.get("evidence_ids") or ()
                if str(evidence_id)
            })
            if papers and evidence_ids:
                comparisons.append({
                    "topic_id": topic_id,
                    "paper_key": papers[0],
                    "evidence_ids": evidence_ids,
                    "summary": f"bounded comparison for {topic_id}",
                })
        return {
            "status": "success",
            "content": {
                "comparisons": comparisons,
                "bridge_claims": [],
                "processed_topic_ids": sorted(processed_topics),
                "processed_fragment_ids": sorted(processed_fragments),
                "processed_result_ids": sorted(processed_results),
                "processed_relation_ids": [],
                "unresolved_questions": [],
            },
        }

    executor = _executor(
        tmp_path,
        provider=provider,
        stability_mode="off",
        max_source_prompt_tokens=7_000,
    )
    topic_context = [
        {
            "topic_id": f"topic:{index}",
            "paper_ids": [f"paper-{index}"],
            "provider_outputs": [{
                "claims": [{
                    "claim_id": f"synthesis:topic_synthesis:{index}",
                    "paper_key": f"paper-{index}",
                    "evidence_ids": [f"E{index}"],
                }],
                "large_context": "full supported topic synthesis " * 450,
            }],
        }
        for index in range(1, 5)
    ]

    result = executor._run_bounded_semantic_provider_call(
        "cross_group_comparison_provider",
        {
            "task": "substantive_cross_group_comparison",
            "node_id": "cross_group_comparison",
            "semantic_contract_version": "semantic-evidence-graph-v1",
            "questions": ["Compare the supplied topics"],
            "topic_synthesis": topic_context,
            "relation_candidates": [],
            "output_contract": {
                "comparisons": "evidence-bound comparisons",
                "bridge_claims": "supported claims",
                "processed_topic_ids": "all input topic IDs",
                "processed_fragment_ids": "all input fragment IDs",
                "processed_result_ids": "all input result IDs",
                "processed_relation_ids": "all input relation IDs",
                "unresolved_questions": "array",
            },
        },
        {"topic_synthesis": "test-input-hash"},
    )

    assert len(calls) > 1
    assert any(
        isinstance(request.get("hierarchy"), Mapping)
        and request["hierarchy"].get("level") == "bounded_semantic_reduction"
        for _node_id, request in calls
    )
    assert result["processed_topic_ids"] == [
        "topic:1",
        "topic:2",
        "topic:3",
        "topic:4",
    ]
    semantic_audit = [
        row
        for row in executor._request_payload_audit
        if str(row.get("semantic_node_id") or "").startswith("cross_group_comparison_provider")
    ]
    assert semantic_audit
    assert all(int(row.get("estimated_input_tokens") or 0) <= 7_000 for row in semantic_audit)


def test_cross_reducer_repartitions_assigned_relations_without_losing_questions(
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, Mapping[str, Any]]] = []

    def provider(node_id: str, request: Mapping[str, Any]) -> Mapping[str, Any]:
        calls.append((node_id, request))
        identities: dict[str, set[str]] = {
            key: set() for key in ("topic", "fragment", "result", "relation")
        }

        def collect(value: Any) -> None:
            if isinstance(value, Mapping):
                for kind, keys in {
                    "topic": ("topic_id", "topic_ids", "processed_topic_ids"),
                    "fragment": ("fragment_id", "fragment_ids", "processed_fragment_ids"),
                    "result": ("result_id", "result_ids", "processed_result_ids"),
                    "relation": ("relation_id", "relation_ids", "processed_relation_ids"),
                }.items():
                    for key in keys:
                        raw = value.get(key)
                        if isinstance(raw, str):
                            identities[kind].add(raw)
                        elif isinstance(raw, list):
                            identities[kind].update(str(item) for item in raw if str(item))
                for child in value.values():
                    collect(child)
            elif isinstance(value, list):
                for child in value:
                    collect(child)

        collect(request.get("topic_synthesis"))
        collect(request.get("relation_candidates"))
        return {"status": "success", "content": {
            "comparisons": [], "bridge_claims": [],
            "processed_topic_ids": sorted(identities["topic"]),
            "processed_fragment_ids": sorted(identities["fragment"]),
            "processed_result_ids": sorted(identities["result"]),
            "processed_relation_ids": sorted(identities["relation"]),
            "unresolved_questions": [],
        }}

    executor = _executor(
        tmp_path, provider=provider, stability_mode="off",
        max_source_prompt_tokens=5_500,
    )
    topic_context = [
        {
            "topic_id": f"topic:{index}", "fragment_id": f"fragment:{index}",
            "result_ids": [f"result:{index}"], "paper_ids": [f"paper-{index}"],
            "provider_outputs": [{"detail": "full source detail " * 140}],
        }
        for index in range(1, 9)
    ]
    relations = [
        {
            "relation_id": f"relation:{index}",
            "paper_keys": ["paper-1", "paper-2"],
            "relation_text": "comparison condition " * 18,
        }
        for index in range(1, 19)
    ]
    questions = ["Compare methods across all papers", "Compare boundary conditions"]
    result = executor._run_bounded_semantic_provider_call(
        "cross_group_comparison_provider",
        {
            "task": "substantive_cross_group_comparison",
            "node_id": "cross_group_comparison",
            "semantic_contract_version": "semantic-evidence-graph-v1",
            "questions": questions,
            "topic_synthesis": topic_context,
            "relation_candidates": relations,
            "output_contract": {},
        },
        {"topic_synthesis": "post-relation-repartition-fixture"},
    )
    assert len(calls) > 1
    assert all(request.get("questions") == questions for _node, request in calls)
    assert set(result["processed_relation_ids"]) == {
        item["relation_id"] for item in relations
    }
    assert set(result["processed_fragment_ids"]) == {
        item["fragment_id"] for item in topic_context
    }
    semantic_audit = [
        row for row in executor._request_payload_audit
        if str(row.get("semantic_node_id") or "").startswith("cross_group_comparison_provider")
    ]
    assert semantic_audit
    assert all(int(row.get("estimated_input_tokens") or 0) <= 5_500 for row in semantic_audit)


def test_interpretation_contract_changes_source_and_provider_replay_binding(
    tmp_path: Path,
) -> None:
    executor = _executor(tmp_path, stability_mode="off")
    for node_id in (
        "outline_evidence_views", "outline_content_layers", "semantic_chunk_plan",
        "topic_synthesis", "topic_synthesis_provider:batch:1",
        "cross_group_comparison_provider", "global_synthesis_provider",
    ):
        binding = executor.build_current_node_binding(
            node_id, dependency_hashes={"fixture": "frozen"}
        )
        assert binding["node_version"] == "v3-interpretation-v1"
    assert executor.build_current_node_binding("global_navigation")["node_version"] == "v3"


def test_candidate_shared_interpretation_tables_reconstruct_repeated_source_context() -> None:
    field = {
        "source_field_id": "field:boundary", "source_value": "Only high-context samples improve. " * 30,
        "paper_key": "paper-a", "owner_study_id": "paper-a:study:1", "scope": "explicit_study",
    }
    dependency = {
        "primary_claim_id": "claim:effect", "required_source_claim_ids": ["claim:boundary"],
        "required_evidence_ids": ["E_BOUNDARY"],
        "required_source_field_ids": [field["source_field_id"]],
        "paper_key": "paper-a", "owner_study_id": "paper-a:study:1",
    }
    routes = [{
        "topic_id": f"topic:{index}",
        "supporting_evidence_ids": ["E_BOUNDARY"],
        "fragments": [{"fragment_id": f"fragment:{index}",
                       "supporting_evidence_ids": ["E_BOUNDARY"], "provider_results": [{
            "result_id": f"result:{index}",
            "planned_evidence_ids": ["E_BOUNDARY"],
            "provider_output": {"claims": [{"source_claim_ids": ["claim:effect"]}]},
            "interpretation_context": {"fields": [field], "dependencies": [dependency]},
        }]}],
    } for index in range(6)]

    tables = OutlineV3Executor._candidate_semantic_source_tables(routes)
    compact = OutlineV3Executor._compact_semantic_topic_routes_for_candidate(routes)
    assert tables["source_fields"] == [field]
    assert len(tables["dependencies"]) == 1
    assert tables["dependencies"][0]["required_source_field_ids"] == [field["source_field_id"]]
    table_by_id = {item["source_field_id"]: item for item in tables["source_fields"]}
    dependency_by_id = {item["dependency_id"]: item for item in tables["dependencies"]}
    for route in compact:
        assert "supporting_evidence_ids" not in route
        assert route["fragments"][0]["planned_evidence_ids"] == ["E_BOUNDARY"]
        result = route["fragments"][0]["provider_results"][0]
        assert "planned_evidence_ids" not in result
        context = result["interpretation_context"]
        assert [table_by_id[item] for item in context["source_field_ids"]] == [field]
        assert [dependency_by_id[item]["primary_claim_id"] for item in context["dependency_ids"]] == [
            "claim:effect"
        ]
        assert result["provider_output"]["claims"] == [
            {"source_claim_ids": ["claim:effect"]}
        ]
    original_bytes = len(json.dumps(routes, ensure_ascii=False).encode("utf-8"))
    shared_bytes = len(json.dumps({"tables": tables, "routes": compact}, ensure_ascii=False).encode("utf-8"))
    assert shared_bytes < original_bytes


def test_candidate_explicit_study_claim_requires_complete_same_study_support(
    tmp_path: Path,
) -> None:
    executor = _executor(tmp_path, stability_mode="off")
    field = {
        "source_field_id": "field:S1:boundary",
        "source_value": "The effect holds only in Study S1.",
        "paper_key": "paper-a", "owner_study_id": "paper-a:study:S1",
        "study_id": "S1", "scope": "explicit_study",
    }
    dependency = {
        "dependency_id": "interpretation-dependency:fixture",
        "primary_claim_id": "claim:S1:effect",
        "primary_evidence_ids": ["E_S1_EFFECT"],
        "required_source_claim_ids": ["claim:S1:boundary"],
        "required_evidence_ids": ["E_S1_BOUNDARY"],
        "required_source_field_ids": [field["source_field_id"]],
        "scope": "explicit_study", "paper_key": "paper-a",
        "study_id": "paper-a:study:S1", "owner_study_id": "paper-a:study:S1",
    }
    executor._candidate_interpretation_tables = {
        "source_fields": [field], "dependencies": [dependency],
    }
    section = {
        "section_id": "section:1", "paper_keys": ["paper-a"],
        "relation_ids": [], "claims": ["Study S2 demonstrated the effect without the S1 boundary."],
    }
    with pytest.raises(OutlineV3ExecutionError, match="study S2"):
        executor._validate_candidate_payload(
            "candidate:test", {"sections": [section]},
            allowed_paper_keys=["paper-a"], allowed_relation_ids=[],
        )

    section["claims"] = ["Study S1 showed the bounded effect."]
    with pytest.raises(OutlineV3ExecutionError, match="complete scoped interpretation support"):
        executor._validate_candidate_payload(
            "candidate:test", {"sections": [section]},
            allowed_paper_keys=["paper-a"], allowed_relation_ids=[],
        )

    section["claim_support"] = [{
        "claim": section["claims"][0], "paper_key": "paper-a",
        "study_id": "paper-a:study:S1",
        "source_claim_ids": ["claim:S1:effect", "claim:S1:boundary"],
        "evidence_ids": ["E_S1_EFFECT", "E_S1_BOUNDARY"],
        "source_field_ids": [field["source_field_id"]],
    }]
    executor._validate_candidate_payload(
        "candidate:test", {"sections": [section]},
        allowed_paper_keys=["paper-a"], allowed_relation_ids=[],
    )


def test_old_evidence_contract_cache_invalidates_descendants_before_reuse(
    tmp_path: Path,
) -> None:
    executor = _executor(tmp_path, stability_mode="off")
    executor._run_node(
        "outline_evidence_views",
        lambda: (
            executor._artifact(OutlineArtifact, {"source_marker": "old-complete-for-claim"}),
            (), "deterministic", "local",
        ),
    )
    current = executor._dag.get("outline_evidence_views")
    old_binding = dict(current.execution_binding)
    old_binding["node_version"] = "v3"
    executor._dag = executor._node_store.record_node(
        "outline_evidence_views", status="succeeded",
        output_hash=current.output_hash,
        output_artifact_ids=current.output_artifact_ids,
        execution_binding=old_binding,
    )

    assert executor._load_node("outline_evidence_views") is None
    assert executor._dag.get("outline_evidence_views").status == "stale"
    assert executor._dag.get("outline_content_layers").status == "pending"
    assert executor._dag.get("semantic_chunk_plan").status == "pending"
    assert executor._dag.get("topic_synthesis").status == "pending"


def test_multi_fragment_topic_and_result_identities_survive_cross_global_reduction_and_persist(
    tmp_path: Path,
) -> None:
    calls: list[str] = []

    def provider(node_id: str, request: Mapping[str, Any]) -> Mapping[str, Any]:
        calls.append(node_id)
        topic_ids: set[str] = set()
        fragment_ids: set[str] = set()
        result_ids: set[str] = set()
        relation_ids: set[str] = set()

        def collect(value: Any) -> None:
            if isinstance(value, Mapping):
                if value.get("topic_id"):
                    topic_ids.add(str(value["topic_id"]))
                if value.get("fragment_id"):
                    fragment_ids.add(str(value["fragment_id"]))
                if value.get("relation_id"):
                    relation_ids.add(str(value["relation_id"]))
                for key in ("topic_ids", "processed_topic_ids"):
                    topic_ids.update(str(item) for item in value.get(key) or () if str(item))
                for key in ("fragment_ids", "processed_fragment_ids"):
                    fragment_ids.update(str(item) for item in value.get(key) or () if str(item))
                for key in ("result_id", "batch_result_id", "result_ids", "batch_result_ids", "processed_result_ids"):
                    raw = value.get(key) or ()
                    if isinstance(raw, str):
                        raw = [raw]
                    result_ids.update(str(item) for item in raw if str(item))
                for key in ("relation_ids", "processed_relation_ids"):
                    relation_ids.update(str(item) for item in value.get(key) or () if str(item))
                for child in value.values():
                    collect(child)
            elif isinstance(value, list):
                for child in value:
                    collect(child)

        collect(request.get("topic_synthesis"))
        collect(request.get("cross_group_comparison"))
        collect(request.get("relation_candidates"))
        if node_id.startswith("cross_group_comparison_provider"):
            content = {
                "comparisons": [],
                "bridge_claims": [],
                "processed_topic_ids": sorted(topic_ids),
                "processed_fragment_ids": sorted(fragment_ids),
                "processed_result_ids": sorted(result_ids),
                "processed_relation_ids": sorted(relation_ids),
                "unresolved_questions": [],
            }
        else:
            content = {
                "synthesis_claims": [],
                "organizing_principles": [],
                "processed_topic_ids": sorted(topic_ids),
                "processed_fragment_ids": sorted(fragment_ids),
                "processed_result_ids": sorted(result_ids),
                "unresolved_questions": [],
            }
        return {"status": "success", "content": content}

    executor = _executor(
        tmp_path,
        provider=provider,
        stability_mode="off",
        max_source_prompt_tokens=4_500,
    )
    topic_plan: list[TopicSynthesis] = []
    provider_results: list[dict[str, Any]] = []
    for index in range(1, 11):
        topic_id = "topic:shared" if index <= 2 else f"topic:{index}"
        fragment_id = f"{topic_id}:fragment:{index}"
        topic = TopicSynthesis(
            topic_id=topic_id,
            fragment_id=fragment_id,
            paper_ids=[f"paper-{index}"],
            supporting_evidence_ids=[f"E{index}"],
        )
        topic_plan.append(topic)
        provider_results.append({
            "batch_id": f"batch-{index}",
            "result_id": f"batch-result-{index}",
            "fragment_ids": [fragment_id],
            "topic_fragments": [{
                "fragment_id": fragment_id,
                "planned_evidence_unit_ids": [f"unit-{index}"],
                "planned_evidence_ids": [f"E{index}"],
            }],
            "provider_output": {
                "topics": [{
                    "topic_id": topic_id,
                    "fragment_id": fragment_id,
                    "status": "processed",
                    "supporting_evidence_ids": [f"E{index}"],
                    "bounded_context": f"Fragment {index} " + "source detail " * 220,
                }],
                "processed_fragment_ids": [fragment_id],
                "claims": [],
                "unresolved_questions": [],
            },
        })
    topic_context = executor._build_topic_synthesis_payloads(topic_plan, provider_results)
    planned_fragments = {str(item.fragment_id) for item in topic_plan}
    assert sum(len(item.get("fragments") or ()) for item in topic_context) == 10
    shared = next(item for item in topic_context if item.get("topic_id") == "topic:shared")
    assert len(shared["fragments"]) == 2
    planned_results = {
        str(result_id)
        for topic in topic_context
        for fragment in topic.get("fragments") or ()
        for result in fragment.get("provider_results") or ()
        for result_id in (result.get("result_id"), result.get("batch_result_id"))
        if str(result_id or "")
    }

    cross_result = executor._run_bounded_semantic_provider_call(
        "cross_group_comparison_provider",
        {
            "task": "substantive_cross_group_comparison",
            "node_id": "cross_group_comparison",
            "semantic_contract_version": "semantic-evidence-graph-v1",
            "topic_synthesis": topic_context,
            "relation_candidates": [],
            "output_contract": {},
        },
        {"topic_synthesis": "multi-fragment-input"},
    )
    cross_record = executor._persist_semantic_provider_output(
        "cross_group_comparison_provider",
        cross_result,
        dependency_hashes={"topic_synthesis": "multi-fragment-input"},
    )
    assert cross_record.artifact_id == f"outline-v3:semantic-provider:{hash_text('cross_group_comparison_provider')[:24]}"
    assert planned_fragments.issubset(set(cross_result["processed_fragment_ids"]))
    assert planned_results.issubset(set(cross_result["processed_result_ids"]))
    assert any(str(value).startswith("reduction-result:") for value in cross_result["processed_result_ids"])
    assert len(cross_result["processed_fragment_ids"]) == len(set(cross_result["processed_fragment_ids"]))
    assert len(cross_result["processed_result_ids"]) == len(set(cross_result["processed_result_ids"]))

    global_result = executor._run_bounded_semantic_provider_call(
        "global_synthesis_provider",
        {
            "task": "substantive_global_synthesis",
            "node_id": "global_synthesis",
            "semantic_contract_version": "semantic-evidence-graph-v1",
            "topic_synthesis": topic_context,
            "cross_group_comparison": cross_result,
            "relation_candidates": [],
            "output_contract": {},
        },
        {"cross_group_comparison": "persisted-cross-result"},
    )
    global_record = executor._persist_semantic_provider_output(
        "global_synthesis_provider",
        global_result,
        dependency_hashes={"cross_group_comparison": "persisted-cross-result"},
    )
    assert global_record.artifact_id == f"outline-v3:semantic-provider:{hash_text('global_synthesis_provider')[:24]}"
    assert planned_fragments.issubset(set(global_result["processed_fragment_ids"]))
    assert set(cross_result["processed_result_ids"]).issubset(set(global_result["processed_result_ids"]))
    assert len(global_result["processed_fragment_ids"]) == len(set(global_result["processed_fragment_ids"]))
    assert len(global_result["processed_result_ids"]) == len(set(global_result["processed_result_ids"]))
    assert any(":reduce:" in node_id for node_id in calls)


def test_candidate_semantic_context_keeps_claims_and_compacts_processing_id_arrays(
    tmp_path: Path,
) -> None:
    executor = _executor(tmp_path, stability_mode="off")
    result = {
        "claims": [{
            "claim_id": "synthesis:global_synthesis:claim-1",
            "paper_key": "paper-a",
            "evidence_ids": ["E1"],
        }],
        "processed_topic_ids": ["topic:a", "topic:b"],
        "processed_fragment_ids": ["fragment:a", "fragment:b", "fragment:c"],
        "processed_result_ids": ["result:a", "result:b"],
        "processed_relation_ids": ["relation:a"],
    }

    compact = executor._compact_semantic_provider_result_for_candidate(result)

    assert compact["claims"] == result["claims"]
    for key in (
        "processed_topic_ids",
        "processed_fragment_ids",
        "processed_result_ids",
        "processed_relation_ids",
    ):
        assert key not in compact
        assert compact[f"{key}_coverage"]["count"] == len(result[key])
        assert compact[f"{key}_coverage"]["identity_set_hash"] == hash_json(sorted(result[key]))

    topic_routes = executor._compact_semantic_topic_routes_for_candidate([{
        "topic_id": "topic:a",
        "fragment_ids": ["fragment:a"],
        "provider_batch_ids": ["batch:a"],
        "provider_output_refs": [{"result_id": "result:a"}],
        "fragments": [{
            "fragment_id": "fragment:a",
            "provider_results": [{"result_id": "result:a", "provider_output": {"claims": []}}],
        }],
    }])
    assert topic_routes[0]["topic_id"] == "topic:a"
    assert topic_routes[0]["fragments"][0]["provider_results"][0]["result_id"] == "result:a"
    assert "provider_batch_ids" not in topic_routes[0]
    assert "provider_output_refs" not in topic_routes[0]


def test_semantic_preflight_accounts_for_input_output_and_retry_reserves(
    tmp_path: Path,
) -> None:
    executor = _executor(tmp_path, stability_mode="off")
    executor.profile = ProviderContextProfile.conservative(
        provider="openai_responses",
        model="test-output-cap",
        endpoint_type="responses",
        model_context_limit=128_000,
        max_output_tokens=32_000,
        reasoning_reserve=256,
        safety_margin=512,
    )
    executor.semantic_provider_synthesis_enabled = True
    executor.semantic_transport_retries = 1
    executor.max_provider_calls = None
    executor.input_cost_per_1k_tokens = 0.01
    executor.output_cost_per_1k_tokens = 0.02
    executor.reasoning_cost_per_1k_tokens = 0.03
    executor._pricing_is_explicit = True

    executor._preflight_stability_budget()

    plan = executor.semantic_request_plan
    assert plan
    assert all(row["physical_attempt_upper_bound"] == 2 for row in plan)
    assert all(row["estimated_output_tokens"] <= 16_384 for row in plan)
    semantic_input = sum(int(row["estimated_input_tokens"]) for row in plan)
    semantic_output = sum(int(row["estimated_output_tokens"]) for row in plan)
    semantic_reasoning = sum(int(row["estimated_reasoning_tokens"]) for row in plan)
    base_cost = sum(
        float(row.estimated_cost or 0.0)
        * int(row.physical_attempt_upper_bound or 1)
        for row in executor.provider_call_plans
        if row.transport_expected
    )
    expected_cost = base_cost + 2 * (
        semantic_input / 1000 * 0.01
        + semantic_output / 1000 * 0.02
        + semantic_reasoning / 1000 * 0.03
    )
    assert executor.stability_preflight["estimated_output_tokens"] >= 2 * semantic_output
    assert executor.stability_preflight["estimated_input_tokens"] >= 2 * semantic_input
    assert executor.stability_preflight["estimated_cost"] == pytest.approx(expected_cost)


def test_semantic_validator_enforces_paper_study_evidence_ownership_and_namespace(
    tmp_path: Path,
) -> None:
    executor = _executor(tmp_path, stability_mode="off")
    request = {
        "topic_synthesis": [
            {
                "topic_id": "topic:A",
                "paper_ids": ["A"],
                "provider_outputs": [{
                    "claims": [{
                        "claim_id": "synthesis:topic_synthesis:a1",
                        "paper_key": "A",
                        "study_id": "A:S1",
                        "evidence_ids": ["E_A"],
                    }]
                }],
            },
            {
                "topic_id": "topic:B",
                "paper_ids": ["B"],
                "provider_outputs": [{
                    "claims": [{
                        "claim_id": "synthesis:topic_synthesis:b1",
                        "paper_key": "B",
                        "study_id": "B:S1",
                        "evidence_ids": ["E_B"],
                    }]
                }],
            },
        ]
    }
    base = {
        "comparisons": [],
        "bridge_claims": [],
        "processed_topic_ids": ["topic:A", "topic:B"],
        "processed_fragment_ids": [],
        "processed_result_ids": [],
        "processed_relation_ids": [],
        "unresolved_questions": [],
    }
    with pytest.raises(Exception, match="outside its evidence contract"):
        executor._validate_semantic_provider_output(
            "cross_group_comparison_provider",
            _legacy_semantic_request(request),
            {
                **base,
                "comparisons": [{
                    "paper_key": "A",
                    "study_id": "B:S1",
                    "evidence_ids": ["E_B"],
                }],
            },
        )
    with pytest.raises(Exception, match="must use the synthesis:"):
        executor._validate_semantic_provider_output(
            "cross_group_comparison_provider",
            _legacy_semantic_request(request),
            {
                **base,
                "bridge_claims": [{
                    "claim_id": "claim:unknown-source",
                    "paper_key": "A",
                    "evidence_ids": ["E_A"],
                }],
            },
        )
    with pytest.raises(Exception, match="empty semantic result"):
        executor._validate_semantic_provider_output(
            "cross_group_comparison_provider",
            _legacy_semantic_request(request),
            {},
        )


def test_semantic_validator_rejects_source_claim_evidence_edge_mismatch(
    tmp_path: Path,
) -> None:
    executor = _executor(tmp_path, stability_mode="off")
    request = {
        "evidence_units": [{
            "paper_key": "paper-A",
            "study_units": [{
                "study_id": "paper-A:study:1",
                "source_study_id": "1",
                "claims": [
                    {"claim_id": "C1", "evidence_ids": ["E1"]},
                    {"claim_id": "C2", "evidence_ids": ["E2"]},
                ],
            }],
            "evidence_ids_by_field": {"findings": ["E1", "E2"]},
            "evidence_text_by_id": {"E1": "First result", "E2": "Second result"},
        }],
    }
    result = {
        "comparisons": [],
        "bridge_claims": [{
            "claim_id": "synthesis:cross_group_comparison:bad-edge",
            "paper_key": "paper-A",
            "study_id": "paper-A:study:1",
            "source_claim_ids": ["C1"],
            "evidence_ids": ["E2"],
        }],
        "processed_topic_ids": [],
        "processed_fragment_ids": [],
        "processed_result_ids": [],
        "processed_relation_ids": [],
        "unresolved_questions": [],
    }

    with pytest.raises(Exception, match="not bound to source claims"):
        executor._validate_semantic_provider_output(
            "cross_group_comparison_provider",
            _legacy_semantic_request(request),
            result,
        )


@pytest.mark.parametrize(
    ("identity_field", "identity_value"),
    [
        ("paper_key", "unknown-paper"),
        ("topic_id", "unknown-topic"),
        ("fragment_id", "unknown-fragment"),
        ("relation_id", "unknown-relation"),
        ("evidence_ids", ["unknown-evidence"]),
        ("source_claim_ids", ["unknown-source-claim"]),
        ("claim_id", "C1"),
    ],
)
def test_semantic_validator_rejects_each_unknown_identity_kind_from_empty_allowlist(
    tmp_path: Path,
    identity_field: str,
    identity_value: Any,
) -> None:
    executor = _executor(tmp_path, stability_mode="off")
    result = {
        "comparisons": [{identity_field: identity_value}],
        "bridge_claims": [],
        "processed_topic_ids": [],
        "processed_fragment_ids": [],
        "processed_result_ids": [],
        "processed_relation_ids": [],
        "unresolved_questions": [],
    }
    with pytest.raises(Exception, match="outside its evidence contract"):
        executor._validate_semantic_provider_output(
            "cross_group_comparison_provider",
            _legacy_semantic_request({}),
            result,
        )


def test_semantic_validator_rejects_topic_and_relation_membership_mismatch(
    tmp_path: Path,
) -> None:
    executor = _executor(tmp_path, stability_mode="off")
    topic_request = {
        "topic_synthesis": [
            {
                "topic_id": "topic:A",
                "paper_ids": ["paper-A"],
                "provider_outputs": [{"claims": [{
                    "claim_id": "synthesis:topic_synthesis:a",
                    "paper_key": "paper-A",
                    "evidence_ids": ["E_A"],
                }]}],
            },
            {
                "topic_id": "topic:B",
                "paper_ids": ["paper-B"],
                "provider_outputs": [{"claims": [{
                    "claim_id": "synthesis:topic_synthesis:b",
                    "paper_key": "paper-B",
                    "evidence_ids": ["E_B"],
                }]}],
            },
        ]
    }
    topic_result = {
        "comparisons": [{
            "topic_id": "topic:A",
            "paper_key": "paper-B",
            "evidence_ids": ["E_B"],
            "summary": "This paper is outside topic A.",
        }],
        "bridge_claims": [],
        "processed_topic_ids": ["topic:A", "topic:B"],
        "processed_fragment_ids": [],
        "processed_result_ids": [],
        "processed_relation_ids": [],
        "unresolved_questions": [],
    }
    with pytest.raises(Exception, match="outside topic membership"):
        executor._validate_semantic_provider_output(
            "cross_group_comparison_provider",
            _legacy_semantic_request(topic_request),
            topic_result,
        )

    relation_request = {
        "topic_synthesis": [{
            "topic_id": "topic:B",
            "paper_ids": ["paper-B"],
            "provider_outputs": [{"claims": [{
                "claim_id": "synthesis:topic_synthesis:b",
                "paper_key": "paper-B",
                "evidence_ids": ["E_B"],
            }]}],
        }],
        "relation_candidates": [{"relation_id": "relation:A", "paper_keys": ["paper-A"]}],
    }
    relation_result = {
        "comparisons": [{
            "relation_id": "relation:A",
            "paper_key": "paper-B",
            "evidence_ids": ["E_B"],
            "summary": "This paper is outside relation A.",
        }],
        "bridge_claims": [],
        "processed_topic_ids": ["topic:B"],
        "processed_fragment_ids": [],
        "processed_result_ids": [],
        "processed_relation_ids": ["relation:A"],
        "unresolved_questions": [],
    }
    with pytest.raises(Exception, match="outside relation membership"):
        executor._validate_semantic_provider_output(
            "cross_group_comparison_provider",
            _legacy_semantic_request(relation_request),
            relation_result,
        )


def test_topic_conclusions_require_bound_support_and_rejections_are_not_persisted(
    tmp_path: Path,
) -> None:
    calls: list[str] = []

    def unsupported_provider(node_id: str, request: Mapping[str, Any]) -> Mapping[str, Any]:
        calls.append(node_id)
        topic_ids: set[str] = set()
        fragment_ids: set[str] = set()
        result_ids: set[str] = set()
        relation_ids: set[str] = set()

        def collect(value: Any) -> None:
            if isinstance(value, Mapping):
                if value.get("topic_id"):
                    topic_ids.add(str(value["topic_id"]))
                if value.get("fragment_id"):
                    fragment_ids.add(str(value["fragment_id"]))
                if value.get("relation_id"):
                    relation_ids.add(str(value["relation_id"]))
                for key in ("topic_ids", "processed_topic_ids"):
                    topic_ids.update(str(item) for item in value.get(key) or () if str(item))
                for key in ("fragment_ids", "processed_fragment_ids"):
                    fragment_ids.update(str(item) for item in value.get(key) or () if str(item))
                for key in ("result_id", "batch_result_id"):
                    if value.get(key):
                        result_ids.add(str(value[key]))
                for key in ("result_ids", "batch_result_ids", "processed_result_ids"):
                    result_ids.update(str(item) for item in value.get(key) or () if str(item))
                for key in ("relation_ids", "processed_relation_ids"):
                    relation_ids.update(str(item) for item in value.get(key) or () if str(item))
                for child in value.values():
                    collect(child)
            elif isinstance(value, list):
                for child in value:
                    collect(child)

        collect(request.get("topic_synthesis"))
        collect(request.get("cross_group_comparison"))
        collect(request.get("relation_candidates"))
        topic_row = next(
            (
                item
                for item in request.get("topic_synthesis") or ()
                if isinstance(item, Mapping) and item.get("topic_id") and item.get("paper_ids")
            ),
            {},
        )
        topic_id = str(topic_row.get("topic_id") or next(iter(sorted(topic_ids)), ""))
        paper_id = str((topic_row.get("paper_ids") or [""])[0] or "")
        return {
            "status": "success",
            "content": {
                "comparisons": ([{
                    "topic_id": topic_id,
                    "paper_key": paper_id,
                    "summary": "Unsupported factual comparison.",
                }] if topic_id and paper_id else []),
                "bridge_claims": [],
                "processed_topic_ids": sorted(topic_ids),
                "processed_fragment_ids": sorted(fragment_ids),
                "processed_result_ids": sorted(result_ids),
                "processed_relation_ids": sorted(relation_ids),
                "unresolved_questions": [],
            },
        }

    executor = _executor(
        tmp_path,
        provider=unsupported_provider,
        stability_mode="off",
        max_source_prompt_tokens=4_000,
    )
    topic_context = [
        {
            "topic_id": f"topic:{index}",
            "fragment_id": f"fragment:{index}",
            "result_ids": [f"result:{index}"],
            "paper_ids": [f"paper-{index}"],
                "provider_outputs": [{"context": "bounded prior result " * 300}],
        }
        for index in range(1, 5)
    ]
    with pytest.raises(Exception, match="factual without evidence or source-claim support"):
        executor._run_bounded_semantic_provider_call(
            "cross_group_comparison_provider",
            {
                "task": "substantive_cross_group_comparison",
                "node_id": "cross_group_comparison",
                "semantic_contract_version": "semantic-evidence-graph-v1",
                "topic_synthesis": topic_context,
                "relation_candidates": [],
                "output_contract": {},
            },
            {"topic_synthesis": "unsupported-conclusion-test"},
        )
    assert calls and ":reduce:" in calls[0]
    artifact_id = f"outline-v3:semantic-provider:{hash_text(calls[0])[:24]}"
    assert executor.registry.get(artifact_id) is None

    topic_executor = _executor(tmp_path / "topic", stability_mode="off")
    topic_request = {
        "topics": [{"topic_id": "topic:A", "fragment_id": "fragment:A", "paper_ids": ["paper-A"]}],
        "evidence_units": [{
            "paper_key": "paper-A",
            "evidence_ids_by_field": {"findings": ["E_A"]},
            "evidence_text_by_id": {"E_A": "Supported finding"},
        }],
    }
    with pytest.raises(Exception, match=r"topics\[0\]\.conclusions\[0\]"):
        topic_executor._validate_semantic_provider_output(
            "topic_synthesis_provider:batch:unsupported",
            topic_request,
            {
                "topics": [{
                    "topic_id": "topic:A",
                    "fragment_id": "fragment:A",
                    "paper_key": "paper-A",
                    "conclusions": ["Unsupported topic finding"],
                }],
                "processed_fragment_ids": ["fragment:A"],
                "claims": [],
                "unresolved_questions": [],
            },
        )


def test_outline_preflight_counts_hierarchical_relation_calls(tmp_path: Path) -> None:
    executor = _executor(
        tmp_path,
        stability_mode="off",
        technical_shard_target_tokens=1,
    )
    executor._preflight_stability_budget()

    assert executor.stability_preflight["hierarchical_relation_shard_calls"] > 0
    assert executor.stability_preflight["estimated_provider_calls"] > len(
        executor._provider_node_ids()
    )


@pytest.mark.parametrize("target_tokens", [0, 24_000, 32_000, 50_000])
def test_outline_preflight_positive_targets_use_effective_cap(
    tmp_path: Path,
    target_tokens: int,
) -> None:
    executor = _executor(
        tmp_path / str(target_tokens),
        stability_mode="off",
        technical_shard_target_tokens=target_tokens,
        max_source_prompt_tokens=32_000,
    )
    executor._preflight_stability_budget()

    assert executor.stability_preflight["preflight_status"] == "accepted"
    transport_plans = [
        item for item in executor.provider_call_plans if item.transport_expected
    ]
    assert transport_plans
    assert max(item.estimated_input_tokens for item in transport_plans) <= 32_000


def test_hierarchical_relation_adjudication_emits_local_and_cross_shard_calls(
    tmp_path: Path,
) -> None:
    calls: list[str] = []

    def provider(node_id: str, request: Mapping[str, Any]) -> Mapping[str, Any]:
        calls.append(node_id)
        relation_ids = [
            str(item.get("relation_id") or "")
            for item in request.get("relation_candidates") or ()
            if isinstance(item, Mapping)
        ]
        return {
            "status": "success",
            "content": {
                "confirmed_relation_ids": relation_ids,
                "rejected_relations": [],
            },
        }

    executor = _executor(
        tmp_path,
        provider=provider,
        stability_mode="off",
        technical_shard_target_tokens=1,
    )
    evidence = build_outline_evidence_views(executor.summaries, executor.job_id)
    relations = [
        {"relation_id": "r_local_a", "paper_keys": ["paper-a"]},
        {"relation_id": "r_local_b", "paper_keys": ["paper-b"]},
        {"relation_id": "r_cross", "paper_keys": ["paper-a", "paper-b"]},
    ]
    plan = executor._build_relation_shard_plan(evidence.views, relations)
    content, digests = executor._run_hierarchical_relation_adjudication(
        evidence_views=evidence.views,
        relation_candidates=relations,
        shard_plan=plan,
        relation_contract={"output_fields": {}, "allowed_relation_ids": []},
        relation_dependencies={"evidence": "evidence-hash", "plan": "plan-hash"},
    )

    assert len(plan["shards"]) == 2
    assert len(calls) == 3
    assert sorted(content["confirmed_relation_ids"]) == ["r_cross", "r_local_a", "r_local_b"]
    assert len(digests) == 3
    assert {item["level"] for item in digests} == {"local_shard", "cross_shard"}


@pytest.mark.parametrize(
    ("candidate_count", "stability_mode", "expected_transport_calls"),
    [
        (1, "off", 6),
        (2, "off", 7),
        (5, "off", 10),
        (1, "smoke", 12),
        (2, "smoke", 14),
        (5, "smoke", 20),
        (1, "full", 36),
        (2, "full", 42),
        (5, "full", 60),
    ],
)
def test_outline_v3_call_plan_has_exact_transport_count(
    tmp_path: Path,
    candidate_count: int,
    stability_mode: str,
    expected_transport_calls: int,
) -> None:
    executor = _executor(
        tmp_path,
        candidate_count=candidate_count,
        stability_mode=stability_mode,
    )

    executor._preflight_stability_budget()

    transport_plans = [item for item in executor.provider_call_plans if item.transport_expected]
    replay_plans = [item for item in executor.provider_call_plans if not item.transport_expected]
    assert len(transport_plans) == expected_transport_calls
    assert len(replay_plans) == (0 if stability_mode == "off" else candidate_count + 5)
    assert executor.stability_preflight["estimated_provider_calls"] == expected_transport_calls
    assert all(item.cost_status == "estimate" for item in transport_plans)


@pytest.mark.parametrize(
    ("stability_mode", "expected_transport_calls"),
    [("off", 7), ("smoke", 14), ("full", 42)],
)
def test_outline_v3_transport_trace_matches_call_plan(
    tmp_path: Path,
    stability_mode: str,
    expected_transport_calls: int,
) -> None:
    transport_calls: list[str] = []

    def provider(node_id: str, request: Mapping[str, Any]) -> Mapping[str, Any]:
        transport_calls.append(node_id)
        return _configured_test_provider(node_id, request)

    result = _executor(
        tmp_path,
        provider=provider,
        stability_mode=stability_mode,
        candidate_count=2,
    ).run()

    assert result.ok is True
    assert len(transport_calls) == expected_transport_calls
    stability = json.loads(
        Path(result.artifacts["stability_audit"]).read_text(encoding="utf-8")
    )["payload"]
    assert stability["preflight"]["estimated_provider_calls"] == expected_transport_calls
    assert stability["provider_call_count_total"] == expected_transport_calls
    assert stability["transport_call_count_after_stability"] == expected_transport_calls


def test_stability_dynamic_candidate_and_critique_shards_are_registry_bound(
    tmp_path: Path,
) -> None:
    executor = _executor(
        tmp_path,
        stability_mode="off",
        technical_shard_target_tokens=1,
    )
    evidence = build_outline_evidence_views(executor.summaries, executor.job_id)
    ledger = build_global_corpus_ledger(evidence)
    matrix = build_multi_view_matrix(evidence)
    relation_map = build_global_relation_map(evidence, matrix, ledger)
    relations = [item.to_dict() for item in relation_map.relations]
    paper_keys = [view.paper_key for view in evidence.views]
    relation_ids = [item["relation_id"] for item in relations]
    candidate_request = {
        "candidate_id": "candidate_1",
        "organizing_logic": "evidence",
        "paper_keys": paper_keys,
        "relation_ids": relation_ids,
        "relations": relations,
        "evidence": executor._prompt_evidence_views(evidence.views),
    }
    candidate = executor._run_hierarchical_candidate_generation(
        candidate_id="candidate_1",
        generation_node_id="candidate_1_provider_generation",
        provider_request=candidate_request,
        evidence_views=evidence.views,
        relation_candidates=relations,
        allowed_paper_keys=paper_keys,
        allowed_relation_ids=relation_ids,
        generation_deps={"candidate": "candidate-hash"},
        alias_map=None,
        node_prefix="stability:test-candidate",
    )
    critique = executor._run_hierarchical_critique(
        node_id="coverage_critique",
        request={
            "node_id": "coverage_critique",
            "candidate_contents": {"candidate_1": candidate},
            "candidate_hashes": {"candidate_1": hash_json(candidate)},
            "corpus_ledger": ledger.to_dict(),
            "relations": relations,
        },
        dependency_hashes={"candidate": hash_json(candidate)},
        node_prefix="stability:test-critique",
    )

    assert candidate["sections"]
    assert critique["passed"] is True
    assert any(
        node_id.startswith("stability:test-candidate:candidate_1_provider_generation:local:")
        for node_id in executor.artifact_records
    )
    assert any(
        node_id.startswith("stability:test-critique:coverage_critique:local:")
        for node_id in executor.artifact_records
    )


def test_outline_v3_actual_usage_and_cost_are_reported_without_billing_claim(tmp_path: Path) -> None:
    def provider(node_id: str, request: Mapping[str, Any]) -> Mapping[str, Any]:
        response = dict(_configured_test_provider(node_id, request))
        response.update(
            {
                "input_tokens": 100,
                "output_tokens": 20,
                "reasoning_tokens": 5,
                "cached_input_tokens": 3,
                "usage_status": "reported",
            }
        )
        return response

    result = _executor(tmp_path, provider=provider, stability_mode="smoke").run()

    assert result.ok is True
    stability = json.loads(
        Path(result.artifacts["stability_audit"]).read_text(encoding="utf-8")
    )["payload"]
    usage = stability["actual_usage_totals"]
    assert usage["provider_calls"] == 14
    assert usage["usage_status"] == "reported"
    assert usage["input_tokens"] == 14 * 100
    assert usage["output_tokens"] == 14 * 20
    assert usage["reasoning_tokens"] == 14 * 5
    assert usage["actual_cost"] is not None
    assert usage["actual_cost"] > 0
    assert usage["cost_status"] == "calculated"
    assert usage["pricing_source"] == "tests:explicit-rates-v1"
    assert usage["pricing_policy"] == "estimate_only_not_billing_v1"


def test_outline_v3_relation_adjudication_unknown_id_is_fail_closed(tmp_path: Path) -> None:
    def provider(node_id: str, request: Mapping[str, Any]) -> Mapping[str, Any]:
        if node_id == "relation_adjudication":
            return {
                "status": "success",
                "content": {
                    "confirmed_relation_ids": ["relation-not-in-candidates"],
                    "rejected_relations": [],
                    "method": "invalid-test-provider",
                },
            }
        return {"status": "success", "content": {"node_id": node_id, "accepted": True}}

    result = _executor(tmp_path, provider=provider).run()

    assert result.ok is False
    assert result.status == "blocked"
    assert any("unknown relation" in item for item in result.diagnostics)


def test_outline_v3_relation_adjudication_rejected_unknown_id_is_fail_closed(tmp_path: Path) -> None:
    def provider(node_id: str, request: Mapping[str, Any]) -> Mapping[str, Any]:
        if node_id == "relation_adjudication":
            return {
                "status": "success",
                "content": {
                    "confirmed_relation_ids": [],
                    "rejected_relations": [{"relation_id": "relation-not-in-candidates", "reason": "invalid test"}],
                    "method": "invalid-test-provider",
                },
            }
        return {"status": "success", "content": {"node_id": node_id, "accepted": True}}

    result = _executor(tmp_path, provider=provider).run()

    assert result.ok is False
    assert result.status == "blocked"
    assert any("rejected an unknown relation" in item for item in result.diagnostics)


def test_outline_v3_invalid_arbitration_selection_is_fail_closed(tmp_path: Path) -> None:
    def provider(node_id: str, request: Mapping[str, Any]) -> Mapping[str, Any]:
        if node_id == "relation_adjudication":
            candidates = [dict(item) for item in request["relation_candidates"]]
            return {
                "status": "success",
                "content": {
                    "confirmed_relation_ids": [str(item["relation_id"]) for item in candidates],
                    "rejected_relations": [],
                    "method": "valid-test-provider",
                },
            }
        if node_id.endswith("_provider_generation"):
            candidate_id = node_id.removesuffix("_provider_generation")
            papers = list(request["paper_keys"])
            return {
                "status": "success",
                "content": {
                    "candidate_id": candidate_id,
                    "organizing_logic": str(request["organizing_logic"]),
                    "sections": [{
                        "section_id": f"{candidate_id}_section_1",
                        "goal": "Integrate evidence",
                        "paper_keys": papers,
                        "relation_ids": list(request["relation_ids"]),
                        "claims": ["The provider-bound evidence supports this synthesis."],
                    }],
                },
            }
        if node_id in {"structure_critique", "coverage_critique", "evidence_critique"}:
            return {
                "status": "success",
                "content": {"passed": True, "blocking_diagnostics": [], "recommendations": []},
            }
        if node_id == "arbitration":
            return {
                "status": "success",
                "content": {
                    "selected_candidate_id": "candidate-not-in-request",
                    "accepted_recommendations": [],
                    "rejected_recommendations": [],
                },
            }
        return {"status": "success", "content": {"node_id": node_id, "accepted": True}}

    result = _executor(tmp_path, provider=provider).run()

    assert result.ok is False
    assert result.status == "blocked"
    assert any("selected an unknown candidate" in item for item in result.diagnostics)
