"""Paper-scoped source fields must survive into topic interpretation context."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from outline.semantic_chunking import (
    build_paper_content_layers,
    build_semantic_chunk_plan,
    build_topic_synthesis_plan,
)
from outline.v3_evidence import build_outline_evidence_views
from outline.v3_executor import OutlineV3Executor
from tests.test_outline_v3_semantic_execution import (
    _configured_test_provider,
    _executor,
    _summary,
)

PAPER = "paper-level-source-fields"
PRIMARY = "The intervention improves preference."
QUALIFIER = "Only consumers with high prior knowledge showed the effect."


def test_paper_level_source_fields_reach_topic_interpretation_context(
    tmp_path: Path,
) -> None:
    summary = _summary(PAPER, "Paper-level source fields", PRIMARY)
    summary["core_analysis"]["findings"] = PRIMARY
    summary["core_analysis"]["limitations"] = QUALIFIER

    views = build_outline_evidence_views([summary], "paper-level-context")
    layers = build_paper_content_layers(
        [summary], views, job_id="paper-level-context"
    )
    dossier = layers.dossier_by_paper[PAPER]
    unit = dossier.research_units[0]
    primary_claim = next(
        claim
        for claim in unit.claims
        if claim.claim_type == "empirical_finding" and claim.text == PRIMARY
    )
    dependency = next(
        dependency
        for dependency in unit.interpretation_dependencies
        if dependency.primary_claim_id == primary_claim.claim_id
    )
    ledger_by_id = {
        entry.source_field_id: entry for entry in dossier.source_field_ledger
    }
    primary_field_ids = {
        entry.source_field_id
        for entry in dossier.source_field_ledger
        if entry.canonical_field == "findings" and entry.source_value == PRIMARY
    }

    plan = build_semantic_chunk_plan(
        layers,
        candidate_count=1,
        physical_call_limit=24,
        retry_fallback_reserve=0,
    )
    route = next(item for item in plan.topics if "method" in item.dimensions)
    topic = next(
        item
        for item in build_topic_synthesis_plan(plan)
        if item.topic_id == route.topic_id
    )
    executor: OutlineV3Executor = _executor(tmp_path, stability_mode="off")
    request = executor._build_topic_provider_request(
        [topic],
        topic_routes={route.topic_id: route},
        evidence_model=views,
        content_layers_model=layers,
        batch_index=1,
    )
    topic_row = request["topics"][0]
    context = executor._interpretation_context_for_units(
        request["evidence_units"], topic_row["planned_evidence_unit_ids"]
    )
    context_dependencies = context["dependencies"]
    context_dependency = next(
        item
        for item in context_dependencies
        if item["primary_claim_id"] == primary_claim.claim_id
    )
    context_fields = {
        item["source_field_id"]: item for item in context["fields"]
    }
    primary_request_claim = next(
        claim
        for evidence_unit in request["evidence_units"]
        for study in evidence_unit.get("study_units") or ()
        if evidence_unit["paper_key"] == PAPER
        for claim in study.get("claims") or ()
        if claim["claim_id"] == primary_claim.claim_id
    )
    qualifier_field_ids = {
        field_id
        for field_id in dependency.required_source_field_ids
        if ledger_by_id[field_id].source_value == QUALIFIER
    }

    assert unit.study_id.endswith(":paper_level")
    assert unit.source_study_id == ""
    assert primary_field_ids
    assert dependency.required_source_field_ids
    assert unit.source_field_ids == []
    assert set(dependency.required_source_field_ids).issubset(context_fields)
    assert qualifier_field_ids
    assert all(
        context_fields[field_id]["paper_key"] == PAPER
        and context_fields[field_id]["owner_study_id"] == ""
        for field_id in dependency.required_source_field_ids
    )
    assert all(
        context_fields[field_id]["source_value"] == QUALIFIER
        for field_id in qualifier_field_ids
    )
    assert context_dependency["owner_study_id"] == ""
    assert context_dependency["scope"] == "paper"
    assert context_dependency["required_source_field_ids"] == (
        dependency.required_source_field_ids
    )
    assert primary_request_claim["text"] == PRIMARY
    assert primary_request_claim["evidence_ids"] == primary_claim.evidence_ids
    assert context_dependency["primary_evidence_ids"] == primary_claim.evidence_ids


def test_executor_candidate_receives_paper_qualifiers_after_topic_synthesis(
    tmp_path: Path,
) -> None:
    candidate_requests: list[Mapping[str, Any]] = []

    def provider(node_id: str, request: Mapping[str, Any]) -> Mapping[str, Any]:
        if node_id.endswith("_provider_generation"):
            candidate_requests.append(request)
        return _configured_test_provider(node_id, request)

    executor = _executor(tmp_path, provider=provider, stability_mode="off")
    executor.semantic_provider_synthesis_enabled = True
    result = executor.run()
    assert result.ok, result
    assert candidate_requests
    for request in candidate_requests:
        context = request["shared_semantic_context"]
        tables = context["interpretation_source_tables"]
        fields = {row["source_field_id"]: row for row in tables["source_fields"]}
        assert fields
        assert tables["dependencies"]
        assert all(row["owner_study_id"] == "" for row in tables["dependencies"])
        assert any(
            field["source_value"] == "The result is bounded by the tested context."
            for field in fields.values()
        )
        for dependency in tables["dependencies"]:
            assert set(dependency["required_source_field_ids"]).issubset(fields)
            assert all(
                fields[field_id]["paper_key"] == dependency["paper_key"]
                for field_id in dependency["required_source_field_ids"]
            )
        dependency_ids = {row["dependency_id"] for row in tables["dependencies"]}
        for topic in context["topic_routes"]:
            for fragment in topic["fragments"]:
                for row in fragment["provider_results"]:
                    bindings = row.get("interpretation_context", {})
                    assert set(bindings.get("source_field_ids", ())).issubset(fields)
                    assert set(bindings.get("dependency_ids", ())).issubset(dependency_ids)
