from __future__ import annotations

import json
from typing import Any

import pytest

from outline.semantic_chunking import (
    build_paper_content_layers,
    build_semantic_chunk_plan,
    build_topic_synthesis_plan,
)
from outline.v3_evidence import build_outline_evidence_views
from outline.v3_executor import OutlineV3Executor
from outline.v3_models import PaperContentLayers, PaperEvidenceDossier, PaperIndexCard
from runtime.provider_context import ProviderContextProfile
from runtime.provider_routes import build_reachable_provider_route_plan
from runtime.provider_runtime import ProviderAggregateBudgetV1, hash_json
from runtime.stage_planning import (
    ProviderStageRequestInventoryV1,
    StagePlanError,
    UnplannedProviderExposureV1,
    VerifiedProviderReuseAuthorityV1,
    build_full_stage_request_plan_v1,
    build_provider_request_plan_row_v1,
    build_stage_plan,
)
from services.artifact_registry import ArtifactRegistry
from services.job_workspace import JobWorkspace
from summary_schema import normalize_ai_summary


def _config() -> dict[str, dict[str, str]]:
    roles = {
        "outline_model": "Outline_API",
        "relation_adjudicator_model": "Relation_API",
        "structure_critic_model": "Structure_API",
        "coverage_critic_model": "Coverage_API",
        "evidence_critic_model": "Evidence_API",
        "arbitrator_model": "Arbitrator_API",
    }
    sections = {name for name in roles.values()}
    sections.update({"Writer_API", "Validator_API"})
    config: dict[str, dict[str, str]] = {
        "OutlineModels": roles,
        "Outline": {
            "relation_adjudication_enabled": "true",
            "structure_critique_enabled": "true",
            "coverage_critique_enabled": "true",
            "evidence_critique_enabled": "true",
        },
        "Validation": {"review_enabled": "true"},
    }
    for section in sections:
        config[section] = {
            "api_key": "synthetic-test-only",
            "provider_family": "test_provider",
            "model": f"model-{section.casefold()}",
            "endpoint_type": "chat_completions",
            "api_base": f"https://{section.casefold()}.example.test/v1",
        }
    return config


def _plans(*, requested_stages: tuple[str, ...]) -> tuple[Any, Any]:
    stage_plan = build_stage_plan(
        action="run_all",
        requested_stages=requested_stages,
        validation_enabled="validate" in requested_stages,
        validation_required="validate" in requested_stages,
    )
    route_plan = build_reachable_provider_route_plan(
        _config(),
        action="run_all",
        requested_stages=requested_stages,
        stage_plan=stage_plan,
    )
    return stage_plan, route_plan


def _profile(route: Any) -> ProviderContextProfile:
    return ProviderContextProfile.conservative(
        provider=route.provider_family,
        model=route.model,
        endpoint_type=route.endpoint_type,
        model_context_limit=80_000,
        max_output_tokens=4_000,
        reasoning_reserve=512,
        safety_margin=256,
    )


def _request_payload(call_id: str, qualifier: str = "scope-bound synthetic qualifier") -> dict[str, Any]:
    return {
        "system": "Return only the requested evidence-bound JSON object.",
        "task": "Interpret the selected evidence and preserve its scope.",
        "call_id": call_id,
        "study_unit": {
            "interpretation_dependencies": [
                {
                    "primary_claim_id": f"claim:{call_id}",
                    "required_source_field_ids": [f"field:{call_id}"],
                    "source_field_text": [qualifier],
                    "scope": "explicit_study",
                    "study_id": f"study:{call_id}",
                }
            ]
        },
        "output_schema": {"type": "object", "required": ["conclusion", "scope"]},
    }


def _request_row(
    route_plan: Any,
    *,
    stage: str,
    role: str,
    call_id: str,
    conditional_on: str = "",
    reuse: VerifiedProviderReuseAuthorityV1 | None = None,
    wall_seconds: float | None = None,
) -> Any:
    route = route_plan.route_for_role(role)
    payload = _request_payload(call_id)
    return build_provider_request_plan_row_v1(
        stage_name=stage,
        request_id=call_id,
        source_builder="synthetic actual-schema builder for contract test",
        route=route,
        request_payload=payload,
        profile=_profile(route),
        retry_attempts=1,
        requested_output_tokens=2_000,
        conditional_on=conditional_on,
        verified_reuse=reuse,
        wall_seconds_upper_bound=wall_seconds,
    )


def _source_summary(
    paper_id: str,
    *,
    method: str,
    theory: str,
) -> dict[str, Any]:
    finding = f"{paper_id} finding remains bounded by its own assigned study."
    raw = normalize_ai_summary(
        {
            "routing": {
                "paper_type": "empirical",
                "paper_subtype_raw": "quantitative",
                "paper_subtype_normalized": "quantitative",
                "classification_status": "resolved",
                "route_confidence": "high",
                "classification_rationale": "synthetic family-grouping fixture",
                "secondary_candidates": [],
            },
            "paper_metadata": {
                "title": f"Synthetic {paper_id}",
                "authors": ["Synthetic Author"],
                "year": "2025",
                "journal": "Synthetic Journal",
                "doi": f"10.5555/{paper_id}",
            },
            "core_analysis": {
                "summary": finding,
                "key_points": [finding],
                "methodology": method,
                "findings": finding,
                "conclusions": finding,
                "relevance": "Synthetic planning fixture.",
                "limitations": "This result applies only under its stated condition.",
                "research_gap": "No additional gap is asserted by this fixture.",
                "theoretical_framework": theory,
                "future_research_directions": [],
            },
            "specialized_details": {
                "empirical": {
                    "research_questions_or_hypotheses": [f"RQ for {paper_id}"],
                    "data_source_and_size": "Synthetic consumer sample, size 100.",
                    "analysis_technique": method,
                    "core_variables": {"independent": ["treatment"], "dependent": ["outcome"]},
                    "sample_characteristics_or_context": "Synthetic retail context.",
                    "studies": [
                        {
                            "study_id": f"{paper_id}:study-1",
                            "method": method,
                            "findings": finding,
                            "conditions": f"Only under the condition reported for {paper_id}.",
                        }
                    ],
                },
                "review": None,
                "conceptual": None,
            },
        }
    )
    raw["status"] = "success"
    raw["paper_info"] = {
        "canonical_paper_key": paper_id,
        "source_paper_id": paper_id,
        "title": f"Synthetic {paper_id}",
        "authors": ["Synthetic Author"],
        "year": 2025,
        "classification": "core",
        "must_use": True,
    }
    # The normalizer preserves the canonical fields and the ledger scans this
    # source collection for explicit study IDs and condition ownership.
    raw.setdefault("specialized_details", {}).setdefault("empirical", {})["studies"] = [
        {
            "study_id": f"{paper_id}:study-1",
            "method": method,
            "findings": finding,
            "conditions": f"Only under the condition reported for {paper_id}.",
        }
    ]
    return raw


def _family_plan(summaries: list[dict[str, Any]]):
    evidence = build_outline_evidence_views(summaries, job_id="topic-family-test")
    layers = build_paper_content_layers(
        summaries,
        evidence,
        job_id="topic-family-test",
    )
    plan = build_semantic_chunk_plan(layers, candidate_count=1, physical_call_limit=24)
    return evidence, layers, plan


def test_method_theory_pairing_shares_per_paper_reads_and_keeps_family_metadata(tmp_path) -> None:
    summaries = [
        _source_summary(
            "paper-a",
            method="Three laboratory scenario-based experiments with randomized assignment.",
            theory="Dual entitlement principle and price fairness theory.",
        ),
        _source_summary(
            "paper-b",
            method="Online between-subjects randomized price experiments.",
            theory="Price fairness judgments using the dual entitlement principle.",
        ),
    ]
    evidence, layers, plan = _family_plan(summaries)
    paired_topics = [
        item for item in plan.topics
        if item.topic_id.startswith("topic:method_theory:paper:")
    ]
    assert len(paired_topics) == 2
    assert {item.paper_ids[0] for item in paired_topics} == {"paper-a", "paper-b"}
    for topic in paired_topics:
        assert len(topic.paper_ids) == 1
        assert topic.dimensions == ["method", "theory"]
        assert len(topic.comparison_questions) == 2
        assert topic.comparison_questions[0].startswith("METHOD QUESTION")
        assert topic.comparison_questions[1].startswith("THEORY QUESTION")
        assert "answer the two dimensions separately" in topic.question
    family_groups = plan.coverage["topic_family_grouping"]["groups"]
    assert plan.coverage["topic_family_grouping"]["version"] == "method-theory-paper-pairs-v3"
    assert plan.coverage["topic_family_grouping"]["method_family_group_count"] == 1
    assert plan.coverage["topic_family_grouping"]["theory_family_group_count"] == 1
    assert plan.coverage["topic_family_grouping"]["source_label_occurrence_count"] == 4
    method_family = next(item for item in family_groups if item["dimension"] == "method")
    theory_family = next(item for item in family_groups if item["dimension"] == "theory")
    assert method_family["paper_ids"] == theory_family["paper_ids"] == ["paper-a", "paper-b"]
    assert "not evidence of equivalence" in " ".join(plan.cross_group_questions).casefold()
    assert "paper-a" in " ".join(plan.cross_group_questions)
    assert "paper-b" in " ".join(plan.cross_group_questions)

    for topic in paired_topics:
        paper_id = topic.paper_ids[0]
        expected_evidence = {
            evidence_id
            for field_name in (
                "research_questions", "operationalizations", "findings", "zero_results",
                "theoretical_derivation", "concept_definitions",
            )
            for evidence_id in layers.dossier_by_paper[paper_id].evidence_ids_by_field.get(field_name, [])
        }
        assert set(topic.required_evidence_ids) == expected_evidence

    for paper_id in ("paper-a", "paper-b"):
        dossier = layers.dossier_by_paper[paper_id]
        unit = next(
            item
            for item in dossier.research_units
            if item.source_study_id == f"{paper_id}:study-1"
        )
        ledger_by_id = {entry.source_field_id: entry for entry in dossier.source_field_ledger}
        explicit_method = [
            entry for entry in dossier.source_field_ledger
            if entry.canonical_field == "method" and entry.scope == "explicit_study"
        ]
        explicit_condition = [
            entry for entry in dossier.source_field_ledger
            if entry.source_path.endswith("conditions") and entry.scope == "explicit_study"
        ]
        assert explicit_method and explicit_condition
        assert all(entry.interpretation_required for entry in (*explicit_method, *explicit_condition))
        assert all(entry.study_id == unit.source_study_id for entry in (*explicit_method, *explicit_condition))
        assert {entry.source_field_id for entry in (*explicit_method, *explicit_condition)}.issubset(
            set(unit.source_field_ids)
        )
        for dependency in unit.interpretation_dependencies:
            assert dependency.scope in {"paper", "explicit_study", "unresolved"}
            assert all(field_id in ledger_by_id for field_id in dependency.required_source_field_ids)

    workspace = JobWorkspace.create(str(tmp_path), "topic-family", job_id="topic-family-test")
    registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    profile = ProviderContextProfile.conservative(
        provider="test_provider",
        model="test-model",
        endpoint_type="chat_completions",
        model_context_limit=80_000,
        max_output_tokens=4_000,
        reasoning_reserve=512,
        safety_margin=256,
    )
    executor = OutlineV3Executor(
        job_id=workspace.job_id,
        summaries=summaries,
        workspace=workspace,
        artifact_registry=registry,
        provider_profile=profile,
        candidate_count=1,
        stability_mode="off",
        max_provider_calls=24,
        max_source_prompt_tokens=32_000,
    )
    topic_plan = build_topic_synthesis_plan(plan)
    topic_routes = {item.topic_id: item for item in plan.topics}
    _expanded, batches, _rows = executor._plan_topic_provider_batches(
        topic_plan,
        topic_routes=topic_routes,
        evidence_model=evidence,
        content_layers_model=layers,
        profile=profile,
    )
    for topic in paired_topics:
        batch_number, batch = next(
            (index, batch)
            for index, batch in enumerate(batches, start=1)
            if topic.topic_id in {item.topic_id for item in batch}
        )
        request = executor._build_topic_provider_request(
            batch,
            topic_routes=topic_routes,
            evidence_model=evidence,
            content_layers_model=layers,
            batch_index=batch_number,
        )
        topic_row = next(
            item for item in request.get("topics") or ()
            if isinstance(item, dict) and item.get("topic_id") == topic.topic_id
        )
        assert topic_row["dimensions"] == ["method", "theory"]
        assert topic_row["comparison_questions"][0].startswith("METHOD QUESTION")
        assert topic_row["comparison_questions"][1].startswith("THEORY QUESTION")
        paper_id = topic.paper_ids[0]
        evidence_units = [
            item for item in request.get("evidence_units") or () if isinstance(item, dict)
        ]
        paper_units = [item for item in evidence_units if item.get("paper_key") == paper_id]
        unit_ids = [str(item.get("evidence_unit_id") or "") for item in paper_units]
        assert paper_units
        assert len(unit_ids) == len(set(unit_ids))
        assert all(unit_ids)
        serialized = json.dumps(request, ensure_ascii=False)
        summary = next(
            item for item in summaries
            if item["paper_metadata"]["doi"].endswith(paper_id)
        )
        assert summary["core_analysis"]["methodology"] in serialized
        assert summary["core_analysis"]["theoretical_framework"] in serialized
        assert "Only under the condition reported for" in serialized


def test_method_theory_grouping_keeps_all_497_residual_ids_in_outlier_route() -> None:
    cards = [
        PaperIndexCard(paper_id="paper-a", method_category="three scenario experiments", theories=["dual entitlement principle"]),
        PaperIndexCard(paper_id="paper-b", method_category="online randomized experiment", theories=["price fairness and dual entitlement"]),
        PaperIndexCard(paper_id="paper-outlier"),
    ]
    dossiers = [
        PaperEvidenceDossier(
            dossier_id=f"dossier:{paper_id}",
            paper_id=paper_id,
            evidence_ids_by_field={
                "research_questions": [f"{paper_id}:rq"],
                "operationalizations": [f"{paper_id}:method"],
                "findings": [f"{paper_id}:finding"],
                "theoretical_derivation": [f"{paper_id}:theory"],
                "concept_definitions": [f"{paper_id}:concept"],
            },
        )
        for paper_id in ("paper-a", "paper-b")
    ]
    residual_ids = [f"OUTLIER_REQUIRED_{index:03d}" for index in range(497)]
    dossiers.append(
        PaperEvidenceDossier(
            dossier_id="dossier:paper-outlier",
            paper_id="paper-outlier",
            evidence_ids_by_field={"unmapped_source_evidence": residual_ids},
        )
    )
    layers = PaperContentLayers(index_cards=cards, dossiers=dossiers)

    plan = build_semantic_chunk_plan(layers, candidate_count=1, physical_call_limit=24)

    paired = [
        item for item in plan.topics
        if item.topic_id.startswith("topic:method_theory:paper:")
    ]
    outlier = next(item for item in plan.topics if item.topic_id == "topic:outlier_pool")
    assert {item.paper_ids[0] for item in paired} == {"paper-a", "paper-b"}
    assert all(item.dimensions == ["method", "theory"] for item in paired)
    assert outlier.paper_ids == ["paper-outlier"]
    assert len(outlier.required_evidence_ids) == 497
    assert set(outlier.required_evidence_ids) == set(residual_ids)


def test_request_estimate_hashes_the_provider_visible_payload_and_counts_qualifier_text() -> None:
    route_plan = _plans(requested_stages=("outline",))[1]
    route = route_plan.route_for_role("candidate_provider_generation")
    profile = _profile(route)
    short = _request_payload("call-1", "scope qualifier")
    long = _request_payload("call-1", "scope qualifier " * 40)

    short_estimate = profile.estimate_request_v1(short)
    long_estimate = profile.estimate_request_v1(long)

    assert short_estimate.request_hash == hash_json(short)
    assert long_estimate.request_hash == hash_json(long)
    assert long_estimate.estimated_input_tokens > short_estimate.estimated_input_tokens
    assert long_estimate.within_input_budget is True


def test_full_stage_projection_keeps_writer_validator_and_repair_exposure_separate() -> None:
    stage_plan, route_plan = _plans(
        requested_stages=("outline", "review", "validate")
    )
    outline_rows = []
    for route in route_plan.routes:
        if route.stage != "outline":
            continue
        call_id = f"outline:{route.semantic_role}"
        reuse = None
        if route.semantic_role == "candidate_provider_generation":
            estimate = _profile(route).estimate_request_v1(_request_payload(call_id))
            reuse = VerifiedProviderReuseAuthorityV1(
                request_hash=estimate.request_hash,
                route_identity=route.identity,
                receipt_hash="a" * 64,
                output_hash="b" * 64,
                authority_hash="c" * 64,
            )
        outline_rows.append(
            _request_row(
                route_plan,
                stage="outline",
                role=route.semantic_role,
                call_id=call_id,
                reuse=reuse,
            )
        )

    writer = route_plan.route_for_role("writer")
    validator = route_plan.route_for_role("validator")
    inventories = (
        ProviderStageRequestInventoryV1(
            stage_name="outline",
            source_builder="outline.v3_executor request builders",
            requests=tuple(outline_rows),
        ),
        ProviderStageRequestInventoryV1(
            stage_name="review",
            source_builder="services.review_generation_service writer request builder",
            requests=(
                _request_row(
                    route_plan,
                    stage="review",
                    role="writer",
                    call_id="writer:section-1",
                    wall_seconds=45,
                ),
            ),
        ),
        ProviderStageRequestInventoryV1(
            stage_name="validate",
            source_builder="validation.llm_adjudicator request builder",
            requests=(
                _request_row(
                    route_plan,
                    stage="validate",
                    role="validator",
                    call_id="validator:claim-1",
                    wall_seconds=30,
                ),
            ),
            unknown_exposures=(
                UnplannedProviderExposureV1(
                    stage_name="validate",
                    semantic_role="validator",
                    route_identity=validator.identity,
                    reason="repair_request_waits_for_validation_finding_and_is_not_materialized",
                    conditional_on="validation_finding_requires_repair",
                    logical_calls_upper_bound=None,
                    output_tokens_per_call_upper_bound=2_000,
                    reasoning_tokens_per_call_upper_bound=512,
                    retry_attempts_per_call_upper_bound=1,
                ),
            ),
        ),
    )
    budget = ProviderAggregateBudgetV1(
        max_provider_calls_total=24,
        max_output_tokens_total=50_000,
        max_retry_attempts_total=20,
        max_wall_seconds=3_600,
    )

    plan = build_full_stage_request_plan_v1(
        stage_plan=stage_plan,
        reachable_route_plan=route_plan,
        stage_inventories=inventories,
        aggregate_budget=budget,
        local_steps=("source_intake", "adoption", "citation_assembly", "docx", "export"),
    )

    assert plan["limits"]["effective_provider_call_limit"] == 24
    assert plan["totals"]["verified_reuse_calls"] == 1
    assert plan["totals"]["logical_calls_known"] == len(outline_rows) - 1 + 2
    assert plan["totals"]["logical_calls_upper_bound"] is None
    assert plan["totals"]["physical_attempts_upper_bound"] is None
    assert plan["totals"]["estimated_input_tokens_all_attempts"] is None
    assert plan["totals"]["aggregate_output_tokens_reserved"] is None
    assert plan["totals"]["wall_seconds_upper_bound"] is None
    assert plan["totals"]["price_status"] == "unknown_no_complete_route_pricing"
    assert plan["budget_status"]["wall_time"] == "unknown"
    assert plan["budget_status"]["admission"] == "incomplete_unknown_exposure"
    assert plan["local_steps_provider_calls"] == 0
    assert "validation_finding_requires_repair" in json.dumps(plan)
    assert "scope-bound synthetic qualifier" not in json.dumps(plan)
    assert route_plan.route_for_role("writer").identity == writer.identity


def test_known_outline_lower_bound_blocks_at_24_even_when_other_roles_are_unknown() -> None:
    stage_plan, route_plan = _plans(requested_stages=("outline",))
    route = route_plan.route_for_role("candidate_provider_generation")
    profile = _profile(route)
    request_rows = tuple(
        build_provider_request_plan_row_v1(
            stage_name="outline",
            request_id=f"topic-batch-{index}",
            source_builder="outline.v3_executor actual topic request builder",
            route=route,
            request_payload=_request_payload(f"topic-batch-{index}"),
            profile=profile,
            retry_attempts=2,
            requested_output_tokens=2_000,
        )
        for index in range(119)
    )
    inventory = ProviderStageRequestInventoryV1(
        stage_name="outline",
        source_builder="outline.v3_executor request plan",
        requests=request_rows,
    )

    plan = build_full_stage_request_plan_v1(
        stage_plan=stage_plan,
        reachable_route_plan=route_plan,
        stage_inventories=(inventory,),
        aggregate_budget=ProviderAggregateBudgetV1(max_provider_calls_total=24),
    )

    assert plan["totals"]["logical_calls_known"] == 119
    assert plan["totals"]["physical_attempts_known_lower_bound"] == 119
    assert plan["totals"]["physical_attempts_upper_bound"] is None
    assert plan["budget_status"]["provider_calls"] == "exceeded"
    assert plan["budget_status"]["admission"] == "blocked_budget"
    assert plan["totals"]["verified_reuse_calls"] == 0


def test_verified_reuse_must_match_request_and_route_identity() -> None:
    route_plan = _plans(requested_stages=("outline",))[1]
    route = route_plan.route_for_role("candidate_provider_generation")
    profile = _profile(route)
    request = _request_payload("cache-check")
    estimate = profile.estimate_request_v1(request)
    authority = VerifiedProviderReuseAuthorityV1(
        request_hash=estimate.request_hash,
        route_identity=route.identity,
        receipt_hash="a" * 64,
        output_hash="b" * 64,
        authority_hash="c" * 64,
    )

    with pytest.raises(StagePlanError, match="request hash"):
        build_provider_request_plan_row_v1(
            stage_name="outline",
            request_id="cache-check",
            source_builder="outline test builder",
            route=route,
            request_payload=_request_payload("cache-check", "changed source qualifier"),
            profile=profile,
            retry_attempts=0,
            verified_reuse=authority,
        )


def test_local_only_projection_has_zero_provider_calls() -> None:
    stage_plan, route_plan = _plans(requested_stages=())
    plan = build_full_stage_request_plan_v1(
        stage_plan=stage_plan,
        reachable_route_plan=route_plan,
        stage_inventories=(),
        aggregate_budget=ProviderAggregateBudgetV1(max_provider_calls_total=24),
        local_steps=("source_intake", "citation_assembly", "docx", "export"),
    )

    assert plan["provider_requests"] == []
    assert plan["totals"]["physical_attempts_upper_bound"] == 0
    assert plan["totals"]["price_status"] == "zero_provider_posts"
    assert plan["budget_status"]["admission"] == "within_budget"
    assert plan["local_steps_provider_calls"] == 0
