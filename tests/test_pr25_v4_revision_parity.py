"""Offline regressions for post-arbitration stability revision parity."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from outline.v3_executor import OutlineV3Executor
from test_outline_v3_semantic_execution import _configured_test_provider, _executor


_EXPLICIT_CLAIM = "P001 Study 1 reports a bounded treatment effect."
_REMOVED_CLAIM = "This removable claim is in the initial plan."
_REVISED_GOAL = "Revised evidence-bound goal."


def _typed_tables() -> dict[str, list[dict[str, Any]]]:
    field_id = "field:paper-a:study:1:boundary"
    owner_study = "paper-a:study:1"
    primary_claim_id = "claim:paper-a:study:1:effect"
    evidence_id = "evidence:paper-a:study:1:effect"
    return {
        "source_fields": [{
            "source_field_id": field_id,
            "source_value": "The effect is scoped to Study 1.",
            "paper_key": "paper-a",
            "owner_study_id": owner_study,
            "study_id": "1",
            "scope": "explicit_study",
        }],
        "dependencies": [{
            "dependency_id": "interpretation-dependency:paper-a:study:1",
            "primary_claim_id": primary_claim_id,
            "primary_evidence_ids": [evidence_id],
            "required_source_claim_ids": [],
            "required_evidence_ids": [],
            "required_source_field_ids": [field_id],
            "scope": "explicit_study",
            "paper_key": "paper-a",
            "study_id": owner_study,
            "owner_study_id": owner_study,
        }],
    }


def _explicit_support() -> dict[str, Any]:
    return {
        "claim": _EXPLICIT_CLAIM,
        "paper_key": "paper-a",
        "study_id": "paper-a:study:1",
        "source_claim_ids": ["claim:paper-a:study:1:effect"],
        "evidence_ids": ["evidence:paper-a:study:1:effect"],
        "source_field_ids": ["field:paper-a:study:1:boundary"],
        "direction": "positive",
        "claim_kind": "empirical_finding",
    }


def _candidate_response(request: Mapping[str, Any]) -> dict[str, Any]:
    candidate_id = str(request.get("candidate_id") or "candidate_1")
    return {
        "candidate_id": candidate_id,
        "organizing_logic": str(request.get("organizing_logic") or "evidence"),
        "sections": [{
            "section_id": f"{candidate_id}_section_1",
            "title": "Evidence synthesis",
            "goal": "Original evidence-bound goal.",
            "paper_keys": [str(item) for item in request.get("paper_keys") or ()],
            "relation_ids": [str(item) for item in request.get("relation_ids") or ()],
            "claims": [_EXPLICIT_CLAIM, _REMOVED_CLAIM],
            "claim_support": [
                _explicit_support(),
                {
                    "claim": _REMOVED_CLAIM,
                    "paper_key": "paper-b",
                    "study_id": "paper-b:study:2",
                    "source_claim_ids": ["claim:paper-b:study:2:removable"],
                    "evidence_ids": ["evidence:paper-b:study:2:removable"],
                    "direction": "positive",
                    "claim_kind": "empirical_finding",
                },
            ],
        }],
    }


def _install_capture(executor: OutlineV3Executor, monkeypatch: Any) -> list[list[dict[str, Any]]]:
    snapshots: list[list[dict[str, Any]]] = []
    original = OutlineV3Executor._stability_fact_inventory

    def capture(sections: list[Mapping[str, Any]]) -> dict[str, Any]:
        snapshots.append([dict(section) for section in sections])
        return original(sections)

    monkeypatch.setattr(executor, "_stability_fact_inventory", capture)
    return snapshots


def _install_typed_tables(monkeypatch: Any) -> None:
    # The smoke audit creates a fresh executor for exact-replay verification;
    # patch the static builder at class scope so both executors bind identical
    # deterministic fixture authorities.
    monkeypatch.setattr(
        OutlineV3Executor,
        "_candidate_semantic_source_tables",
        staticmethod(lambda _values: _typed_tables()),
    )


def _accepted_recommendations(section_id: str) -> list[dict[str, str]]:
    return [
        {
            "issue_id": "issue:replace-goal",
            "target_section_ids": [section_id],
            "operation": "replace_goal",
            "replacement": _REVISED_GOAL,
        },
        {
            "issue_id": "issue:remove-claim",
            "target_section_ids": [section_id],
            "operation": "remove_claim",
            "replacement": _REMOVED_CLAIM,
        },
    ]


def _read_payload(path: str) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))["payload"]


def _section_projection(section: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "section_id": section.get("section_id"),
        "title": section.get("title"),
        "goal": section.get("goal"),
        "claims": list(section.get("claims") or ()),
        "claim_support": [dict(row) for row in section.get("claim_support") or ()],
    }


def test_primary_and_variant_apply_same_revisions_without_orphan_support(
    tmp_path: Path, monkeypatch: Any,
) -> None:
    def provider(node_id: str, request: Mapping[str, Any]) -> Mapping[str, Any]:
        if "_provider_generation" in node_id:
            return {"status": "success", "content": _candidate_response(request)}
        response = dict(_configured_test_provider(node_id, request))
        if node_id == "arbitration" or node_id.endswith(":arbitration"):
            content = dict(response.get("content") or {})
            selected_id = str(content.get("selected_candidate_id") or "candidate_1")
            content["selected_candidate_id"] = selected_id
            content["accepted_recommendations"] = _accepted_recommendations(
                f"{selected_id}_section_1"
            )
            content["rejected_recommendations"] = []
            response["content"] = content
        return response

    executor = _executor(
        tmp_path,
        provider=provider,
        stability_mode="smoke",
        candidate_count=1,
    )
    _install_typed_tables(monkeypatch)
    snapshots = _install_capture(executor, monkeypatch)

    result = executor.run()

    assert result.ok is True, result.diagnostics
    revision = _read_payload(result.artifacts["selected_candidate_revision"])
    primary_sections = revision["sections"]
    assert len(primary_sections) == 1
    primary_section = primary_sections[0]
    assert primary_section["goal"] == _REVISED_GOAL
    assert primary_section["claims"] == [_EXPLICIT_CLAIM]
    assert all(row["claim"] in primary_section["claims"] for row in primary_section["claim_support"])
    assert _explicit_support() in primary_section["claim_support"]

    variant_sections = next(
        snapshot for snapshot in snapshots
        if snapshot and snapshot[0].get("goal") == _REVISED_GOAL
        and snapshot[0].get("claims") == [_EXPLICIT_CLAIM]
    )
    assert [_section_projection(item) for item in variant_sections] == [
        _section_projection(item) for item in primary_sections
    ]

    stability = _read_payload(result.artifacts["stability_audit"])
    comparison = stability["comparisons"]["summary_order_reversed"]
    assert comparison["claims_text_exact"] is True
    assert comparison["title_goal_similarity"] == 1.0
    assert comparison["semantic_fact_hashes"] is True


def test_variant_only_unresolved_recommendation_blocks_stability(
    tmp_path: Path, monkeypatch: Any,
) -> None:
    def provider(node_id: str, request: Mapping[str, Any]) -> Mapping[str, Any]:
        if "_provider_generation" in node_id:
            return {"status": "success", "content": _candidate_response(request)}
        response = dict(_configured_test_provider(node_id, request))
        if node_id == "arbitration" or node_id.endswith(":arbitration"):
            content = dict(response.get("content") or {})
            selected_id = str(content.get("selected_candidate_id") or "candidate_1")
            content["selected_candidate_id"] = selected_id
            if not request.get("stability_variant"):
                content["accepted_recommendations"] = [{
                    "issue_id": "issue:canonical-goal",
                    "target_section_ids": [f"{selected_id}_section_1"],
                    "operation": "replace_goal",
                    "replacement": "Canonical revised goal.",
                }]
            else:
                content["accepted_recommendations"] = [{
                    "issue_id": "issue:variant-missing-target",
                    "target_section_ids": ["section:does-not-exist"],
                    "operation": "replace_goal",
                    "replacement": "Variant goal.",
                }]
            content["rejected_recommendations"] = []
            response["content"] = content
        return response

    executor = _executor(
        tmp_path,
        provider=provider,
        stability_mode="smoke",
        candidate_count=1,
    )
    _install_typed_tables(monkeypatch)

    result = executor.run()

    assert result.ok is False
    stability = _read_payload(result.artifacts["stability_audit"])
    assert stability["status"] == "blocked"
    assert "summary_order_reversed" in stability["variant_errors"]
    assert "issue:variant-missing-target" in stability["variant_errors"]["summary_order_reversed"]
    primary_revision = _read_payload(result.artifacts["selected_candidate_revision"])
    assert primary_revision["sections"][0]["goal"] == "Canonical revised goal."


def test_explicit_study_support_survives_primary_and_variant_finalization(
    tmp_path: Path, monkeypatch: Any,
) -> None:
    def provider(node_id: str, request: Mapping[str, Any]) -> Mapping[str, Any]:
        if "_provider_generation" in node_id:
            candidate = _candidate_response(request)
            candidate["sections"][0]["claims"] = [_EXPLICIT_CLAIM]
            candidate["sections"][0]["claim_support"] = [_explicit_support()]
            return {"status": "success", "content": candidate}
        return _configured_test_provider(node_id, request)

    executor = _executor(
        tmp_path,
        provider=provider,
        stability_mode="smoke",
        candidate_count=1,
    )
    _install_typed_tables(monkeypatch)
    snapshots = _install_capture(executor, monkeypatch)

    result = executor.run()

    assert result.ok is True, result.diagnostics
    revision = _read_payload(result.artifacts["selected_candidate_revision"])
    primary_section = revision["sections"][0]
    assert primary_section["claims"] == [_EXPLICIT_CLAIM]
    assert primary_section["claim_support"] == [_explicit_support()]
    variant_sections = next(
        snapshot for snapshot in snapshots
        if snapshot and snapshot[0].get("claims") == [_EXPLICIT_CLAIM]
    )
    assert variant_sections[0]["claim_support"] == [_explicit_support()]
