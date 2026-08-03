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
            "common_core": {
                "title": title,
                "authors": ["Author"],
                "year": "2025",
                "summary": finding,
                "key_points": [finding],
                "methodology": "Controlled empirical study",
                "findings": finding,
                "conclusions": finding,
                "limitations": "The result is bounded by the tested context.",
                "research_gap": "Further replication is needed.",
            },
            "type_specific_details": {
                "paper_type": "empirical",
                "data_source_and_size": "Two controlled samples",
                "analysis_technique": "Regression analysis",
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


def _executor(tmp_path: Path, *, adopt: bool = True, provider: Any = None) -> OutlineV3Executor:
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
        provider=provider,
        candidate_count=2,
        adopt=adopt,
    )


def test_outline_v3_fixture_executes_evidence_bound_adoption(tmp_path: Path) -> None:
    executor = _executor(tmp_path)
    result = executor.run()

    assert result.ok is True
    assert result.status == "complete"
    assert result.adopted is True

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
    result = _executor(tmp_path, adopt=False).run()

    assert result.ok is True
    assert result.status == "ready_for_adoption"
    assert result.adopted is False
    assert "adoption" not in result.artifacts


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
