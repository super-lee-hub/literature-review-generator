from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from tests.test_outline_v3_semantic_execution import (
    _configured_test_provider,
    _executor,
    _purposeful_relation_summaries,
)


def test_full_stability_keeps_equivalent_candidates_eligible_across_execution_order(
    tmp_path: Path,
) -> None:
    """The audit must execute the whole decision chain, not only a projection.

    Arbitration chooses the first candidate it receives, so the selected
    candidate ID may change when candidate order is permuted. Both candidates
    carry the same scientific facts. The relation adjudicator still classifies
    selected relations, and the audit must distinguish an equivalent choice
    from a factual instability.
    """

    relation_decisions: list[tuple[list[str], list[str]]] = []

    def provider(node_id: str, request: Mapping[str, Any]) -> Mapping[str, Any]:
        if "relation_adjudication" in node_id:
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

    # Reuse two existing source-bound records with an explicit contrast in
    # findings and study context. The relation task is therefore supported by
    # the summaries' own fields rather than shared labels alone.
    source_rows = _purposeful_relation_summaries()
    source_contrast_summaries = [source_rows[0], source_rows[3]]
    executor = _executor(
        tmp_path,
        provider=provider,
        stability_mode="full",
        summaries=source_contrast_summaries,
    )
    result = executor.run()

    assert result.ok is True
    assert result.status == "ready_for_adoption"
    assert relation_decisions
    assert any(rejected for _confirmed, rejected in relation_decisions)
    relation_record = executor.registry.get("outline-v3:relation_candidates")
    assert relation_record is not None and relation_record.status == "ready"
    relation_map = json.loads(Path(relation_record.path).read_text(encoding="utf-8"))["payload"]
    source_relation_rows = relation_map.get("relations") or []
    assert source_relation_rows
    assert all(
        row.get("evidence_fields") and row.get("source_fields")
        for row in source_relation_rows
    )
    adjudicated_relation_ids = {
        relation_id
        for confirmed, rejected in relation_decisions
        for relation_id in (*confirmed, *rejected)
    }
    assert {str(row["relation_id"]) for row in source_relation_rows}.issubset(
        adjudicated_relation_ids
    )

    stability_path = Path(result.artifacts["stability_audit"])
    stability = json.loads(stability_path.read_text(encoding="utf-8"))["payload"]
    assert stability["method"] == "metamorphic_full_decision_v2"
    assert stability["status"] == "stable"
    assert stability["preflight"]["estimated_provider_calls"] > 0
    assert stability["exact_replay_verification"]["status"] == "verified"
    assert stability["exact_replay_verification"]["provider_invoked"] is False
    assert stability["exact_replay_verification"]["transport_call_count"] == 0
    order_comparison = stability["comparisons"]["candidate_execution_order_permuted"]
    assert order_comparison["semantic_fact_hashes"] is True
    assert order_comparison["organization_equivalent"] is True
    assert order_comparison["stable"] is True


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

    executor = _executor(tmp_path, provider=provider, stability_mode="full")
    result = executor.run()

    assert result.ok is False
    assert result.status == "blocked"
    # A global/unscoped failed critique now stops before arbitration and
    # stability variants, rather than publishing a later blocked audit.
    assert "final_outline" not in result.artifacts
    assert "stability_audit" not in result.artifacts
    critique_record = executor.registry.get("outline-v3:coverage_critique")
    assert critique_record is not None and critique_record.status == "ready"
    critique = json.loads(Path(critique_record.path).read_text(encoding="utf-8"))["payload"]
    assert critique["passed"] is False
    assert executor.registry.get("outline-v3:request_payload_audit:" + executor.closure_epoch_id) is not None
