from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from outline.v3_executor import OutlineV3Executor
from runtime.provider_runtime import ProviderRuntimeLedger
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
    if node_id == "relation_adjudication":
        candidates = [
            dict(item) for item in request.get("relation_candidates") or ()
            if isinstance(item, Mapping)
        ]
        confirmed = [
            str(item.get("relation_id") or "")
            for item in candidates
            if item.get("relation_id") and item.get("evidence_fields")
        ]
        return {"status": "success", "content": {"confirmed_relation_ids": confirmed, "rejected_relations": []}}
    if node_id.endswith("_provider_generation"):
        candidate_id = node_id.removesuffix("_provider_generation")
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
    if node_id in {"structure_critique", "coverage_critique", "evidence_critique"}:
        return {"status": "success", "content": {"passed": True, "blocking_diagnostics": [], "recommendations": []}}
    if node_id == "arbitration":
        candidate_ids = [str(item) for item in request.get("candidate_ids") or ()]
        return {"status": "success", "content": {"selected_candidate_id": sorted(candidate_ids)[0] if candidate_ids else ""}}
    return {"status": "success", "content": {"node_id": node_id, "accepted": True}}


def _executor(
    tmp_path: Path,
    *,
    provider: Any = None,
    stability_mode: str = "smoke",
    max_provider_calls: int | None = None,
    max_estimated_cost: float | None = None,
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
        candidate_count=2,
        stability_mode=stability_mode,
        max_provider_calls=max_provider_calls,
        max_estimated_cost=max_estimated_cost,
    )


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

    ledger = ProviderRuntimeLedger(executor._receipt_ledger.path)
    assert ledger.list_receipts()


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
