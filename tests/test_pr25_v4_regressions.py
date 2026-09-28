"""Desired-behavior regressions for the PR25 V4 independent stability audit.

All providers are deterministic local fixtures; these tests do not authorize
or invoke an external provider.
"""

from __future__ import annotations

import copy
import json
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping

import pytest

import outline.v3_executor as executor_module
from outline.v3_executor import OutlineV3Executor
from outline.v3_evidence import (
    build_global_corpus_ledger,
    build_multi_view_matrix,
    build_outline_evidence_views,
)
from outline.v3_models import OutlineQualityGate
from outline.v3_relations import build_global_relation_map
from outline.semantic_chunking import build_paper_content_layers, build_semantic_chunk_plan
from services.artifact_registry import ArtifactRegistry
from services.job_workspace import JobWorkspace
from test_outline_v3_semantic_execution import _configured_test_provider, _executor, _summary


def _payload(path: str | Path) -> dict[str, Any]:
    envelope = json.loads(Path(path).read_text(encoding="utf-8"))
    payload = envelope.get("payload") if isinstance(envelope, Mapping) else None
    assert isinstance(payload, dict)
    return payload


def _relation_fixture(tmp_path):
    executor = _executor(tmp_path, stability_mode="off")
    summaries = [
        _summary(
            f"paper-{index}",
            f"Study {index}",
            "The treatment improves the outcome in a controlled setting.",
        )
        for index in range(5)
    ]
    evidence = build_outline_evidence_views(summaries, executor.job_id)
    ledger = build_global_corpus_ledger(evidence)
    matrix = build_multi_view_matrix(evidence)
    relation_map = build_global_relation_map(evidence, matrix, ledger)
    content_layers = build_paper_content_layers(summaries, evidence, job_id=executor.job_id)
    semantic_plan = build_semantic_chunk_plan(
        content_layers,
        relation_map,
        candidate_count=2,
        physical_call_limit=24,
    )
    candidates = [item.to_dict() for item in relation_map.relations]
    assert len(candidates) > 1
    shard_plan = executor._build_relation_shard_plan(evidence.views, candidates)
    return executor, evidence, content_layers, semantic_plan, candidates, shard_plan


@pytest.mark.parametrize("selection", ["subset", "empty", "all"])
def test_stability_relation_request_preserves_primary_selection_and_shape(
    tmp_path, selection: str,
) -> None:
    executor, evidence, content_layers, plan, candidates, shard_plan = _relation_fixture(tmp_path)
    all_ids = [str(item["relation_id"]) for item in candidates]
    if selection == "subset":
        selected_ids = [all_ids[0]]
    elif selection == "empty":
        selected_ids = []
    else:
        selected_ids = all_ids
    plan = replace(
        plan,
        coverage={**dict(plan.coverage), "selected_relation_ids": selected_ids},
    )

    primary, primary_rows, excluded = executor._relation_provider_request(
        relation_candidates=candidates,
        content_layers=content_layers,
        semantic_plan=plan,
        shard_plan=shard_plan,
    )
    variant = executor._stability_relation_compact_request(
        relation_candidates=candidates,
        content_layers=content_layers,
        semantic_plan=plan,
        shard_plan=shard_plan,
        evidence_views=evidence.views,
        variant_name="summary_order_reversed",
        shard_size=5,
        shard_order="canonical",
    )

    primary_ids = [str(item["relation_id"]) for item in primary["relation_candidates"]]
    variant_ids = [str(item["relation_id"]) for item in variant["relation_candidates"]]
    assert primary_ids == sorted(str(item["relation_id"]) for item in primary_rows)
    assert set(primary_ids) == set(selected_ids)
    assert set(variant_ids) == set(selected_ids)
    assert [item["relation_id"] for item in primary["relation_evidence_bundles"]] == sorted(selected_ids)
    assert [item["relation_id"] for item in variant["relation_evidence_bundles"]] == sorted(selected_ids)
    assert primary["relation_adjudication_contract"]["allowed_relation_ids"] == sorted(selected_ids)
    assert variant["relation_adjudication_contract"]["allowed_relation_ids"] == sorted(selected_ids)
    assert primary["excluded_relation_count"] == len(excluded)
    assert variant["excluded_relation_count"] == len(excluded)
    assert primary["excluded_relation_ids_hash"] == variant["excluded_relation_ids_hash"]

    primary_shape = dict(primary)
    variant_shape = dict(variant)
    variant_shape.pop("stability_variant")
    assert variant_shape == primary_shape


def _stability_provider(*, title_drift: bool = False):
    def provider(node_id: str, request: Mapping[str, Any]) -> Mapping[str, Any]:
        response = copy.deepcopy(_configured_test_provider(node_id, request))
        actual_node_id = str((request.get("_prompt_authority") or {}).get("node_id") or "")
        if (
            title_drift
            and "_provider_generation" in node_id
            and actual_node_id.startswith("stability:")
        ):
            for section in response.get("content", {}).get("sections") or ():
                if isinstance(section, dict):
                    section["title"] = f"{section.get('title') or ''} — alternate wording"
        return response

    return provider


def test_identical_stability_provider_is_a_passing_control_and_runs_second_executor(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    executor = _executor(
        tmp_path,
        provider=_stability_provider(),
        stability_mode="smoke",
    )
    calls: list[str] = []
    original = executor._verify_exact_replay_with_second_executor

    def record_second_executor() -> dict[str, Any]:
        calls.append("second_executor")
        return original()

    monkeypatch.setattr(executor, "_verify_exact_replay_with_second_executor", record_second_executor)
    result = executor.run()
    audit = _payload(result.artifacts["stability_audit"])

    assert result.ok is True, result.diagnostics
    assert audit["status"] == "stable"
    assert calls == ["second_executor"]
    assert audit["exact_replay_verification"]["status"] == "verified"
    assert audit["checks"]["second_executor_exact_replay"] is True


def test_title_only_stability_drift_keeps_adoption_when_facts_are_unchanged(
    tmp_path,
) -> None:
    result = _executor(
        tmp_path,
        provider=_stability_provider(title_drift=True),
        stability_mode="smoke",
    ).run()
    audit = _payload(result.artifacts["stability_audit"])
    comparison = audit["comparisons"]["summary_order_reversed"]

    assert result.ok is True, result.diagnostics
    assert audit["status"] == "stable"
    assert comparison["title_goal_similarity"] < 1.0
    assert comparison["stable"] is True
    assert comparison["semantic_review_required"] is False
    assert not audit["failed_checks"]
    assert all(
        value
        for key, value in comparison.items()
        if key not in {"title_goal_similarity", "stable", "semantic_review_required"}
    )


def test_alias_replay_never_claims_second_executor_verification_without_a_call(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    executor = _executor(
        tmp_path,
        provider=_stability_provider(),
        stability_mode="smoke",
    )
    executor.opaque_alias_enabled = True
    calls: list[str] = []
    original = executor._verify_exact_replay_with_second_executor

    def record_second_executor() -> dict[str, Any]:
        calls.append("second_executor")
        return original()

    monkeypatch.setattr(executor, "_verify_exact_replay_with_second_executor", record_second_executor)
    result = executor.run()
    audit = _payload(result.artifacts["stability_audit"])
    replay = audit["exact_replay_verification"]
    claimed_second_executor_pass = bool(audit["checks"].get("second_executor_exact_replay"))

    assert len(calls) <= 1
    if replay["status"] == "verified":
        assert calls == ["second_executor"]
        assert claimed_second_executor_pass is True
        assert result.ok is True, result.diagnostics
    else:
        # An unavailable or blocked exact replay may block stability, but it
        # must not be promoted to a successful equivalent verification.
        assert replay["status"] in {"blocked", "not_run"}
        assert claimed_second_executor_pass is False
        assert result.ok is False


def test_primary_and_stability_provider_requests_preserve_semantic_and_coordination_contracts(
    tmp_path,
) -> None:
    candidate_requests: list[tuple[str, dict[str, Any]]] = []
    arbitration_requests: list[tuple[str, dict[str, Any]]] = []

    def provider(node_id: str, request: Mapping[str, Any]) -> Mapping[str, Any]:
        concrete_id = str((request.get("_prompt_authority") or {}).get("node_id") or node_id)
        if "_provider_generation" in node_id:
            candidate_requests.append((concrete_id, dict(request)))
        if "arbitration" in node_id:
            arbitration_requests.append((concrete_id, dict(request)))
        return _configured_test_provider(node_id, request)

    result = _executor(
        tmp_path,
        provider=provider,
        stability_mode="smoke",
    ).run()

    primary_candidates = [
        request for node_id, request in candidate_requests
        if node_id.startswith("candidate_") and "_provider_generation" in node_id
    ]
    variant_candidates = [
        request for node_id, request in candidate_requests
        if node_id.startswith("stability:") and "_provider_generation" in node_id
    ]
    primary_arbitrations = [
        request for node_id, request in arbitration_requests
        if node_id == "arbitration"
    ]
    variant_arbitrations = [
        request for node_id, request in arbitration_requests
        if node_id.startswith("stability:") and node_id.endswith(":arbitration")
    ]

    assert result.ok is True, result.diagnostics
    assert primary_candidates and variant_candidates
    assert primary_arbitrations and variant_arbitrations
    primary_candidate = primary_candidates[0]
    variant_candidate = variant_candidates[0]
    assert "shared_semantic_context" in primary_candidate
    assert "shared_semantic_context" in variant_candidate
    assert variant_candidate["shared_semantic_context"] == primary_candidate["shared_semantic_context"]
    assert "output_contract" in primary_candidate
    assert "output_contract" in variant_candidate
    assert variant_candidate["output_contract"] == primary_candidate["output_contract"]

    primary_coordination = primary_arbitrations[0].get("section_coordination_contract")
    variant_coordination = variant_arbitrations[0].get("section_coordination_contract")
    assert isinstance(primary_coordination, Mapping)
    assert isinstance(variant_coordination, Mapping)
    assert variant_coordination == primary_coordination


@pytest.mark.parametrize(
    ("diagnostic", "should_block"),
    [
        (None, False),
        ("Evidence review failed; no candidate scope was supplied.", True),
        ("candidate_1 and candidate_2 have unsupported evidence.", True),
    ],
)
def test_negative_evidence_critic_is_a_hard_gate_with_passing_control(
    tmp_path, diagnostic: str | None, should_block: bool,
) -> None:
    def provider(node_id: str, request: Mapping[str, Any]) -> Mapping[str, Any]:
        if node_id == "evidence_critique" and diagnostic is not None:
            return {
                "status": "success",
                "content": {
                    "passed": False,
                    "blocking_diagnostics": [diagnostic],
                    "recommendations": [],
                },
            }
        return _configured_test_provider(node_id, request)

    result = _executor(
        tmp_path,
        provider=provider,
        stability_mode="off",
    ).run()
    assert result.ok is (not should_block), result.diagnostics
    if should_block:
        stage_health_path = result.artifacts.get("stage_health")
        if stage_health_path:
            assert _payload(stage_health_path)["adoption_eligible"] is False
        else:
            assert result.status == "blocked"
        critique_path = result.artifacts.get("evidence_critique")
        assert critique_path, "fail-closed critic rejection must retain its raw artifact"
        assert _payload(critique_path)["passed"] is False
    else:
        health = _payload(result.artifacts["stage_health"])
        critic = _payload(result.artifacts["evidence_critique"])
        assert critic["passed"] is True
        assert health["adoption_eligible"] is True


def _coverage_scope_executor(
    tmp_path,
    *,
    provider: Any,
    quality_gate: OutlineQualityGate,
) -> OutlineV3Executor:
    summary_a = _summary(
        "paper-a",
        "Study A",
        "The treatment improved the outcome in the tested population.",
    )
    summary_b = _summary(
        "paper-b",
        "Study B",
        "The excluded study reports a related outcome in a different context.",
    )
    summary_b["paper_info"]["classification"] = "support"
    summary_b["paper_info"]["must_use"] = False
    summary_b["classification"] = "support"
    summary_b["must_use"] = False
    workspace = JobWorkspace.create(str(tmp_path), "outline", job_id="v4-coverage-job")
    registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    return OutlineV3Executor(
        job_id=workspace.job_id,
        summaries=[summary_a, summary_b],
        workspace=workspace,
        artifact_registry=registry,
        provider=provider,
        candidate_count=2,
        stability_mode="off",
        quality_gate=quality_gate,
        max_estimated_total_tokens=5_000_000,
        pricing_source="tests:explicit-rates-v1",
        input_cost_per_1k_tokens=0.0,
        output_cost_per_1k_tokens=0.001,
        reasoning_cost_per_1k_tokens=0.001,
        cache_read_cost_per_1k_tokens=0.0,
        cache_write_cost_per_1k_tokens=0.0,
    )


@pytest.mark.parametrize(
    ("scope", "min_full", "expected_full"),
    [("local", 1.0, False), ("full", 0.5, True)],
)
def test_selected_coverage_scope_controls_both_audit_and_health(
    tmp_path, monkeypatch: pytest.MonkeyPatch, scope: str, min_full: float, expected_full: bool,
) -> None:
    original_ledger_builder = executor_module.build_global_corpus_ledger

    def mark_paper_b_excluded_with_reason(evidence: Any, **kwargs: Any) -> Any:
        excluded = dict(kwargs.pop("excluded_with_reasons", {}) or {})
        excluded["paper-b"] = "Fixture policy excludes paper-b from the local review scope."
        return original_ledger_builder(
            evidence,
            excluded_with_reasons=excluded,
            **kwargs,
        )

    monkeypatch.setattr(
        executor_module,
        "build_global_corpus_ledger",
        mark_paper_b_excluded_with_reason,
    )

    def provider(node_id: str, request: Mapping[str, Any]) -> Mapping[str, Any]:
        response = copy.deepcopy(_configured_test_provider(node_id, request))
        if "_provider_generation" in node_id:
            for section in response.get("content", {}).get("sections") or ():
                if isinstance(section, dict):
                    section["paper_keys"] = [
                        str(key) for key in section.get("paper_keys") or ()
                        if str(key) == "paper-a"
                    ]
                    section["relation_ids"] = []
        return response

    gate = OutlineQualityGate(
        coverage_scope=scope,
        min_canonical_coverage_full=min_full,
        min_canonical_coverage_local=1.0,
        min_effective_sections=1,
        max_duplicate_assignments=0,
    )
    result = _coverage_scope_executor(
        tmp_path,
        provider=provider,
        quality_gate=gate,
    ).run()
    coverage = _payload(result.artifacts["coverage_audit"])
    health = _payload(result.artifacts["stage_health"])

    assert coverage["passed"] is True, {
        "quality_checks": coverage.get("quality_checks"),
        "paper_coverage": coverage.get("paper_coverage"),
        "must_use_coverage": coverage.get("must_use_coverage"),
        "claim_coverage": coverage.get("claim_coverage"),
        "section_coverage": coverage.get("section_coverage"),
        "research_streams": coverage.get("research_streams"),
        "excluded_with_reason_papers": coverage.get("excluded_with_reason_papers"),
    }
    assert coverage["quality_checks"]["coverage_scope"] == scope
    assert coverage["quality_checks"]["full_threshold"] is expected_full
    assert coverage["quality_checks"]["local_threshold"] is True
    assert health["quality_gate_passed"] is True
    assert health["adoption_eligible"] is True
    assert result.ok is True, result.diagnostics
