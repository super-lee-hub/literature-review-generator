"""Exact candidate request identity and bounded local packing work."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping

import pytest

import outline.v3_executor as executor_module  # noqa: E402
from outline.candidate_output_scope import CandidateOutputScopeV1  # noqa: E402
from outline.v3_executor import OutlineV3Executor  # noqa: E402
from runtime.provider_context import ProviderContextProfile  # noqa: E402
from runtime.provider_runtime import hash_json  # noqa: E402
from tests.test_run_first_candidate_adversarial import (  # noqa: E402
    _executor,
    _sealed_relation_scope,
)


TEST_ROOT = Path(__file__).resolve().parent
BASELINE_PATH = TEST_ROOT / "fixtures" / "run_first_candidate_request_identity.json"
STATIC_PLAN = {
    "schema_version": "candidate-packing-static-parent-plan/v1",
    "payload": "static-semantic-plan-sentinel-" + "p" * (64 * 1024),
}


def _parent_request(scope: Any, inventory: Any, context: Mapping[str, Any]) -> dict[str, Any]:
    evidence = [
        {
            "paper_key": paper.paper_key,
            "projection": "registry_complete_evidence_ref_v1",
            "dossier_id": paper.dossier_id,
            "source_summary_hash": paper.source_summary_hash,
        }
        for paper in inventory.papers
    ]
    relations = [
        {
            "relation_id": relation_id,
            "paper_keys": list(group.paper_keys),
            "relation_type": "qualifies",
        }
        for group in scope.claim_groups
        for relation_id in group.relation_ids
    ]
    return {
        "candidate_id": "candidate_1",
        "organizing_logic": "source comparison",
        "paper_keys": list(scope.paper_keys),
        "relation_ids": list(scope.selected_relation_ids),
        "relations": relations,
        "evidence": evidence,
        "shared_semantic_context": context,
        "semantic_chunk_plan": STATIC_PLAN,
        "content_layer_refs": {},
        "output_contract": {
            "preferred_candidate_wire_version": "candidate-slot-output/v1",
            "claim_prose_reserve_tokens": 256,
            "section_prose_reserve_tokens": 64,
        },
    }


def _case(tmp_path: Path) -> tuple[Any, Any, Any, Any, Any, OutlineV3Executor, dict[str, Any]]:
    workspace, registry, summaries, inventory, scope, context = _sealed_relation_scope(tmp_path)
    parent = _parent_request(scope, inventory, context)
    executor = _executor(
        workspace,
        registry,
        summaries,
        SimpleNamespace(base_url="http://127.0.0.1:9/v1"),
        scope,
    )
    return workspace, registry, summaries, inventory, scope, executor, parent


def _run_group_requests(
    executor: OutlineV3Executor,
    parent: Mapping[str, Any],
) -> list[tuple[str, dict[str, Any], list[str], list[str], dict[str, Any]]]:
    return executor._candidate_group_requests(
        generation_node_id="candidate_1_provider_generation",
        provider_request=parent,
    )


def _identity_rows(planned: list[tuple[str, dict[str, Any], list[str], list[str], dict[str, Any]]]) -> list[dict[str, Any]]:
    rows = []
    for node_id, shard, paper_keys, relation_ids, request in planned:
        rows.append({
            "node_id": node_id,
            "paper_keys": list(paper_keys),
            "relation_ids": list(relation_ids),
            "claim_group_ids": list(request["hierarchy"]["claim_group_ids"]),
            "group_assignment": dict(shard["group_assignment"]),
            "assignment_coverage_hash": str(shard["assignment_coverage_hash"]),
            "scope_hash": str(request["candidate_output_scope"]["content_hash"]),
            "semantic_context_hash": hash_json(request["shared_semantic_context"]),
            "request_hash": hash_json(request),
        })
    return rows


def test_real_sealed_scope_keeps_exact_request_and_assignment_identities(tmp_path: Path) -> None:
    _workspace, _registry, _summaries, _inventory, scope, executor, parent = _case(tmp_path)
    expected = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))

    planned = _run_group_requests(executor, parent)

    assert scope.content_hash == expected["source_scope_hash"]
    assert hash_json(parent["semantic_chunk_plan"]) == expected["semantic_plan_hash"]
    assert _identity_rows(planned) == expected["rows"]
    assert len(planned) == 2


def test_required_ab_group_keeps_complete_qualifier_and_empty_study_closure(
    tmp_path: Path,
) -> None:
    _workspace, _registry, _summaries, _inventory, scope, executor, parent = _case(tmp_path)
    ab_group = next(
        group
        for group in scope.claim_groups
        if group.relation_ids == ("relation:confirmed:ab",)
    )

    planned = _run_group_requests(executor, parent)
    ab_rows = [
        row for row in planned
        if ab_group.claim_group_id in row[4]["hierarchy"]["claim_group_ids"]
    ]

    assert len(ab_rows) == 1
    _node_id, shard, paper_keys, relation_ids, request = ab_rows[0]
    assert ab_group.claim_seed is True
    assert set(paper_keys) == {"paper-a", "paper-b"}
    assert relation_ids == ["relation:confirmed:ab"]
    assert shard["group_assignment"][ab_group.claim_group_id]
    assert {row["paper_key"] for row in shard["required_corpus_witnesses"]} >= {
        "paper-a",
        "paper-b",
    }

    expected_slots = {
        slot.claim_slot_id: slot
        for slot in scope.claim_slots
        if slot.claim_group_id == ab_group.claim_group_id
    }
    wire_slots = request["candidate_output_scope"]["claim_slots"]
    assert {row["claim_slot_id"] for row in wire_slots} == set(expected_slots)
    for wire_slot in wire_slots:
        slot = expected_slots[wire_slot["claim_slot_id"]]
        assert wire_slot["study_id"] == slot.study_id == ""
        assert wire_slot["primary_claim_id"] == slot.primary_claim_id
        assert wire_slot["source_claim_ids"] == list(slot.source_claim_ids)
        assert wire_slot["evidence_ids"] == list(slot.evidence_ids)
        assert wire_slot["source_field_ids"] == list(slot.source_field_ids)
        assert wire_slot["relation_ids"] == ["relation:confirmed:ab"]

    local_tables = request["shared_semantic_context"]["interpretation_source_tables"]
    expected_field_ids = {
        field_id
        for slot in expected_slots.values()
        for field_id in slot.source_field_ids
    }
    fields = {
        str(field["source_field_id"]): field
        for field in local_tables["source_fields"]
        if isinstance(field, Mapping)
    }
    assert expected_field_ids.issubset(fields)
    assert all(fields[field_id].get("study_id", "") == "" for field_id in expected_field_ids)
    for paper_key in ab_group.paper_keys:
        assert any(
            str(fields[field_id].get("paper_key") or "") == paper_key
            and "bounded" in str(fields[field_id].get("source_value") or "").casefold()
            for field_id in expected_field_ids
        ), f"{paper_key} qualifier field was lost"
    dependencies = [
        row for row in local_tables["dependencies"]
        if isinstance(row, Mapping)
        and set(row.get("required_source_field_ids") or ()).intersection(expected_field_ids)
    ]
    assert dependencies
    assert all(row.get("study_id", "") == "" for row in dependencies)
    assert all(
        set(row.get("required_source_field_ids") or ()).issubset(expected_field_ids)
        for row in dependencies
    )


def test_static_parent_hashes_are_bounded_even_when_packer_measures_trials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _workspace, _registry, _summaries, _inventory, scope, executor, parent = _case(tmp_path)
    original_hash_json = executor_module.hash_json
    target_plan = parent["semantic_chunk_plan"]
    target_context = parent["shared_semantic_context"]
    plan_hash_calls = 0
    context_hash_calls = 0
    parent_scope_hash_calls = 0
    estimate_calls = 0

    def counted_hash_json(value: Any) -> str:
        nonlocal plan_hash_calls, context_hash_calls
        if isinstance(value, Mapping) and value == target_plan:
            plan_hash_calls += 1
        if isinstance(value, Mapping) and value == target_context:
            context_hash_calls += 1
        return original_hash_json(value)

    monkeypatch.setattr(executor_module, "hash_json", counted_hash_json)
    content_hash_property = CandidateOutputScopeV1.__dict__["content_hash"]
    original_content_hash = content_hash_property.fget
    assert original_content_hash is not None

    def counted_scope_hash(candidate_scope: CandidateOutputScopeV1) -> str:
        nonlocal parent_scope_hash_calls
        if candidate_scope is scope:
            parent_scope_hash_calls += 1
        return original_content_hash(candidate_scope)

    monkeypatch.setattr(
        CandidateOutputScopeV1,
        "content_hash",
        property(counted_scope_hash),
    )

    original_estimate = ProviderContextProfile.estimate_request_v1

    def counted_estimate(
        profile: ProviderContextProfile,
        request: Mapping[str, Any],
        **kwargs: Any,
    ) -> Any:
        nonlocal estimate_calls
        if request.get("candidate_id") == "candidate_1":
            estimate_calls += 1
        return original_estimate(profile, request, **kwargs)

    monkeypatch.setattr(ProviderContextProfile, "estimate_request_v1", counted_estimate)
    planned = _run_group_requests(executor, parent)

    assert len(planned) >= 2
    assert estimate_calls > len(planned), "fixture should measure multiple packing trials"
    assert context_hash_calls <= 1
    assert plan_hash_calls <= 1
    assert parent_scope_hash_calls <= 1
