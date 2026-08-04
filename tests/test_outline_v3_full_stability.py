from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from tests.test_outline_v3_semantic_execution import _configured_test_provider, _executor


def test_full_stability_runs_relation_rejection_critique_and_order_sensitive_arbitration(
    tmp_path: Path,
) -> None:
    """The audit must execute the whole decision chain, not only a projection.

    This provider deliberately depends on candidate execution order.  The
    relation adjudicator still classifies every relation, while arbitration
    chooses the first candidate it receives.  The production executor should
    therefore quarantine the outline instead of allowing an unstable result
    to reach adoption.
    """

    relation_decisions: list[tuple[list[str], list[str]]] = []

    def provider(node_id: str, request: Mapping[str, Any]) -> Mapping[str, Any]:
        if node_id == "relation_adjudication":
            relation_ids = [
                str(item.get("relation_id") or "")
                for item in request.get("relation_candidates") or ()
                if isinstance(item, Mapping) and str(item.get("relation_id") or "")
            ]
            confirmed = relation_ids[::2]
            rejected = relation_ids[1::2]
            relation_decisions.append((confirmed, rejected))
            return {
                "status": "success",
                "content": {
                    "confirmed_relation_ids": confirmed,
                    "rejected_relations": [
                        {"relation_id": relation_id, "reason": "not selected by the adversarial adjudicator"}
                        for relation_id in rejected
                    ],
                },
            }
        if node_id.endswith("_provider_generation"):
            candidate_id = node_id.removesuffix("_provider_generation")
            paper_keys = [str(item) for item in request.get("paper_keys") or ()]
            organizing_logic = str(request.get("organizing_logic") or "evidence")
            first_paper = paper_keys[0] if paper_keys else "none"
            return {
                "status": "success",
                "content": {
                    "candidate_id": candidate_id,
                    "organizing_logic": organizing_logic,
                    "sections": [
                        {
                            "section_id": f"{candidate_id}_section_1",
                            "title": f"{organizing_logic} synthesis",
                            "goal": "Integrate evidence",
                            "paper_keys": paper_keys,
                            "relation_ids": list(request.get("relation_ids") or ()),
                            "claims": [f"Order-sensitive first evidence: {first_paper}"],
                        }
                    ],
                },
            }
        if node_id in {"structure_critique", "coverage_critique", "evidence_critique"}:
            return {
                "status": "success",
                "content": {"passed": True, "blocking_diagnostics": [], "recommendations": []},
            }
        if node_id == "arbitration":
            candidate_ids = [str(item) for item in request.get("candidate_ids") or ()]
            return {
                "status": "success",
                "content": {"selected_candidate_id": candidate_ids[0] if candidate_ids else ""},
            }
        return _configured_test_provider(node_id, request)

    result = _executor(tmp_path, provider=provider, stability_mode="full").run()

    assert result.ok is False
    assert result.status == "blocked"
    assert relation_decisions
    assert any(rejected for _confirmed, rejected in relation_decisions)

    stability_path = Path(result.artifacts["stability_audit"])
    stability = json.loads(stability_path.read_text(encoding="utf-8"))["payload"]
    assert stability["method"] == "metamorphic_full_decision_v2"
    assert stability["status"] == "blocked"
    assert stability["preflight"]["estimated_provider_calls"] > 0
    assert stability["exact_replay_verification"]["status"] == "verified"
    assert stability["exact_replay_verification"]["provider_invoked"] is False
    assert stability["exact_replay_verification"]["transport_call_count"] == 0
    assert any(
        not comparison["stable"]
        for comparison in stability["comparisons"].values()
        if "stable" in comparison
    )
    assert any(
        comparison.get("selected_candidate_id") is False
        for comparison in stability["comparisons"].values()
    )


def test_full_stability_quarantines_blocking_critic_before_adoption(tmp_path: Path) -> None:
    def provider(node_id: str, request: Mapping[str, Any]) -> Mapping[str, Any]:
        if node_id == "coverage_critique":
            return {
                "status": "success",
                "content": {
                    "passed": False,
                    "blocking_diagnostics": ["coverage critic requires manual adjudication"],
                    "recommendations": [],
                },
            }
        return _configured_test_provider(node_id, request)

    result = _executor(tmp_path, provider=provider, stability_mode="full").run()

    assert result.ok is False
    assert result.status == "blocked"
    stability = json.loads(
        Path(result.artifacts["stability_audit"]).read_text(encoding="utf-8")
    )["payload"]
    assert stability["status"] == "blocked"
    assert any(
        "coverage_critique" in error
        for error in stability["variant_errors"].values()
    )
