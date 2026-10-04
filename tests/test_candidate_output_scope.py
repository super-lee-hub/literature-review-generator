from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from outline.candidate_output_scope import (
    CandidateOutputScopeError,
    build_candidate_output_scope_v1,
)
from services.artifact_registry import ArtifactRegistry
from services.job_workspace import JobWorkspace
from services.writer_source_inventory import load_writer_source_inventory_v1
from tests.test_outline_v3_semantic_execution import _summary
from tests.writer_source_fixture import bind_production_writer_sources


@pytest.fixture
def source_scope(tmp_path):
    workspace = JobWorkspace.create(str(tmp_path), "candidate-output-scope", job_id="scope-job")
    registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    source = _summary(
        "scope-paper",
        "Bounded source paper",
        "The treatment improved the outcome in the tested context.",
    )
    fixture_service = type("FixtureService", (), {})()
    fixture_service.registry = registry
    fixture_service.workspace = workspace
    fixture_service.summaries = [source]
    bind_production_writer_sources(fixture_service, [])
    inventory = load_writer_source_inventory_v1(registry)
    paper = inventory.paper_by_key()["scope-paper"]
    primary = next(
        claim for claim in paper.claims
        if claim.claim_type == "empirical_finding" and claim.evidence_ids
    )
    dependencies = [
        item for item in paper.interpretation_dependencies
        if item.primary_claim_id == primary.claim_id
    ]
    source_claim_ids = {primary.claim_id}
    evidence_ids = set(primary.evidence_ids)
    source_field_ids: set[str] = set()
    for dependency in dependencies:
        source_claim_ids.update(dependency.required_source_claim_ids)
        evidence_ids.update(dependency.required_evidence_ids)
        source_field_ids.update(dependency.required_source_field_ids)
    claim_map = {claim.claim_id: claim for claim in paper.claims}
    for claim_id in source_claim_ids:
        evidence_ids.update(claim_map[claim_id].evidence_ids)
    route = {
        "topic_id": "topic:scope",
        "logical_node_id": "topic_synthesis:scope",
        "paper_ids": [paper.paper_key],
        "fragments": [{
            "fragment_id": "topic:scope:fragment:1",
            "paper_ids": [paper.paper_key],
            "provider_results": [{
                "result_id": "topic-result:scope:1",
                "provider_output": {
                    "claims": [{
                        "claim_id": "synthesis:topic_synthesis:scope-claim-1",
                        "fragment_id": "topic:scope:fragment:1",
                        "claim_type": "empirical_finding",
                        "paper_key": paper.paper_key,
                        "text": "The result improved under the tested conditions.",
                        "source_claim_ids": sorted(source_claim_ids),
                        "evidence_ids": sorted(evidence_ids),
                        "source_field_ids": sorted(source_field_ids),
                        "relation_ids": ["relation:confirmed:1"],
                    }],
                },
            }],
        }],
    }
    scope = build_candidate_output_scope_v1(
        inventory,
        [route],
        selected_relation_ids=["relation:confirmed:1", "relation:confirmed:unused"],
    )
    return inventory, paper, primary, route, scope


def _valid_payload(scope, *, claim_text: str = "The review preserves the tested boundary.") -> dict[str, Any]:
    slot = scope.claim_slots[0]
    support = {
        "claim_slot_id": slot.claim_slot_id,
        "task_id": slot.task_id,
        "claim": claim_text,
        "paper_key": slot.paper_key,
        "primary_claim_id": slot.primary_claim_id,
        "source_claim_ids": list(slot.source_claim_ids),
        "evidence_ids": list(slot.evidence_ids),
        "source_field_ids": list(slot.source_field_ids),
    }
    if slot.study_id:
        support["study_id"] = slot.study_id
    return {
        "sections": [{
            "section_id": "section:scope:1",
            "task_ids": [slot.task_id],
            "title": "A source-bounded result",
            "goal": "Present one supported result.",
            "paper_keys": [slot.paper_key],
            "relation_ids": ["relation:confirmed:1"],
            "claims": [claim_text],
            "claim_support": [support],
        }],
    }


def test_scope_uses_only_source_closed_claims_from_actual_task_results(source_scope):
    inventory, paper, _primary, _route, scope = source_scope

    assert scope.task_ids == ("topic_synthesis:scope",)
    assert len(scope.claim_slots) == 1
    assert scope.claim_slots[0].paper_key == paper.paper_key
    assert scope.claim_slots[0].source_claim_ids
    assert scope.source_inventory_artifact_id == inventory.artifact_id
    assert scope.content_hash == scope.to_dict()["content_hash"]
    assert scope.max_sections == len(scope.task_ids) + len(scope.claim_slots)
    assert scope.max_claims == len(scope.claim_slots)
    assert scope.max_support_rows == len(scope.claim_slots)


def test_for_papers_filters_slots_without_rebuilding_or_expanding_source_scope(source_scope):
    _inventory, paper, _primary, _route, scope = source_scope

    filtered = scope.for_papers([paper.paper_key])
    absent = scope.for_papers(["unrelated-paper"])

    assert filtered.content_hash == scope.content_hash
    assert [item.claim_slot_id for item in filtered.claim_slots] == [
        scope.claim_slots[0].claim_slot_id
    ]
    assert absent.claim_slots == ()
    assert absent.task_ids == ()


def test_candidate_payload_must_bind_sections_and_support_rows_to_scope(source_scope):
    _inventory, _paper, _primary, _route, scope = source_scope

    scope.validate(_valid_payload(scope))
    unknown = _valid_payload(scope)
    unknown["sections"][0]["task_ids"] = ["topic_synthesis:invented"]
    with pytest.raises(CandidateOutputScopeError, match="task"):
        scope.validate(unknown)
    unrelated_relation = _valid_payload(scope)
    unrelated_relation["sections"][0]["relation_ids"] = ["relation:confirmed:unused"]
    with pytest.raises(CandidateOutputScopeError, match="no consumed source claim group"):
        scope.validate(unrelated_relation)


def test_candidate_support_must_include_the_complete_verified_qualifier_closure(source_scope):
    _inventory, _paper, _primary, _route, scope = source_scope
    payload = _valid_payload(scope)
    support = payload["sections"][0]["claim_support"][0]
    if len(scope.claim_slots[0].source_claim_ids) == 1:
        pytest.skip("canonical fixture has no qualifier claim for this primary")
    support["source_claim_ids"] = [scope.claim_slots[0].primary_claim_id]

    with pytest.raises(CandidateOutputScopeError, match="source_claim_ids"):
        scope.validate(payload)


def test_scope_rejects_unverified_inventory_instead_of_silently_dropping_claims(source_scope):
    inventory, _paper, _primary, route, _scope = source_scope
    unsealed = replace(inventory, _seal=None)

    with pytest.raises(CandidateOutputScopeError, match="verified"):
        build_candidate_output_scope_v1(unsealed, [route])


def test_scope_rejects_topic_claims_without_canonical_source_membership(source_scope):
    inventory, _paper, _primary, route, _scope = source_scope
    route = {
        **route,
        "fragments": [{
            **route["fragments"][0],
            "provider_results": [{
                **route["fragments"][0]["provider_results"][0],
                "provider_output": {
                    "claims": [{
                        **route["fragments"][0]["provider_results"][0]["provider_output"]["claims"][0],
                        "source_claim_ids": ["source-claim:not-in-registry"],
                    }],
                },
            }],
        }],
    }

    with pytest.raises(CandidateOutputScopeError, match="source claim"):
        build_candidate_output_scope_v1(inventory, [route])


def test_scope_rejects_unknown_and_cyclic_synthesis_lineage(source_scope):
    inventory, paper, _primary, route, scope = source_scope
    slot = scope.claim_slots[0]
    unknown_global = {
        "claim_id": "synthesis:global_synthesis:unknown-source",
        "paper_key": paper.paper_key,
        "text": "Unsupported derived statement.",
        "source_claim_ids": ["synthesis:cross_group_comparison:not-a-ready-result"],
        "evidence_ids": list(slot.evidence_ids),
        "source_field_ids": list(slot.source_field_ids),
    }
    with pytest.raises(CandidateOutputScopeError, match="absent from both task lineage and verified inventory"):
        build_candidate_output_scope_v1(
            inventory,
            [route],
            selected_relation_ids=["relation:confirmed:1"],
            bridge_claims=[{
                "task_id": "global_synthesis",
                "result_id": "global-result:unknown",
                "provider_output": {"synthesis_claims": [unknown_global]},
            }],
        )

    cross_id = "synthesis:cross_group_comparison:cycle-cross"
    global_id = "synthesis:global_synthesis:cycle-global"
    cross_claim = {
        "claim_id": cross_id,
        "topic_ids": [route["topic_id"]],
        "paper_key": paper.paper_key,
        "text": "Cyclic cross statement.",
        "source_claim_ids": [global_id],
        "evidence_ids": list(slot.evidence_ids),
        "source_field_ids": list(slot.source_field_ids),
    }
    global_claim = {
        "claim_id": global_id,
        "paper_key": paper.paper_key,
        "text": "Cyclic global statement.",
        "source_claim_ids": [cross_id],
    }
    with pytest.raises(CandidateOutputScopeError, match="contains a cycle"):
        build_candidate_output_scope_v1(
            inventory,
            [route],
            selected_relation_ids=["relation:confirmed:1"],
            bridge_claims=[
                {
                    "task_id": "cross_group_comparison",
                    "result_id": "cross-result:cycle",
                    "provider_output": {"bridge_claims": [cross_claim]},
                },
                {
                    "task_id": "global_synthesis",
                    "result_id": "global-result:cycle",
                    "provider_output": {"synthesis_claims": [global_claim]},
                },
            ],
        )


def test_actual_cross_topic_bridge_claim_gets_one_group_with_each_paper_closure(tmp_path):
    workspace = JobWorkspace.create(str(tmp_path), "candidate-bridge-scope", job_id="bridge-scope-job")
    registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    summaries = [
        _summary("bridge-left", "Left source", "The effect appeared in the first context."),
        _summary("bridge-right", "Right source", "The effect changed in a second context."),
    ]
    fixture_service = type("FixtureService", (), {})()
    fixture_service.registry = registry
    fixture_service.workspace = workspace
    fixture_service.summaries = summaries
    bind_production_writer_sources(fixture_service, [])
    inventory = load_writer_source_inventory_v1(registry)
    papers = inventory.paper_by_key()

    source_ids: set[str] = set()
    evidence_ids: set[str] = set()
    field_ids: set[str] = set()
    primary_by_paper: dict[str, list[str]] = {}
    for paper_key in ("bridge-left", "bridge-right"):
        paper = papers[paper_key]
        primary = next(
            claim for claim in paper.claims
            if claim.claim_type == "empirical_finding" and claim.evidence_ids
        )
        primary_by_paper[paper_key] = [primary.claim_id]
        source_ids.add(primary.claim_id)
        evidence_ids.update(primary.evidence_ids)
        for dependency in paper.interpretation_dependencies:
            if dependency.primary_claim_id != primary.claim_id:
                continue
            source_ids.update(dependency.required_source_claim_ids)
            evidence_ids.update(dependency.required_evidence_ids)
            field_ids.update(dependency.required_source_field_ids)
        claims_by_id = {claim.claim_id: claim for claim in paper.claims}
        for claim_id in list(source_ids):
            if claim_id in claims_by_id:
                evidence_ids.update(claims_by_id[claim_id].evidence_ids)

    topic_routes = [
        {
            "topic_id": "topic:left",
            "logical_node_id": "topic_synthesis:left",
            "paper_ids": ["bridge-left"],
            "fragments": [],
        },
        {
            "topic_id": "topic:right",
            "logical_node_id": "topic_synthesis:right",
            "paper_ids": ["bridge-right"],
            "fragments": [],
        },
    ]
    claim = {
        "claim_id": "synthesis:cross_group_comparison:bridge-1",
        "topic_ids": ["topic:left", "topic:right"],
        "paper_keys": ["bridge-left", "bridge-right"],
        "text": "The effect differs across the two source contexts.",
        "source_claim_ids": sorted(source_ids),
        "primary_claim_ids_by_paper": primary_by_paper,
        "evidence_ids": sorted(evidence_ids),
        "source_field_ids": sorted(field_ids),
        "relation_ids": ["relation:confirmed:bridge"],
    }
    global_claim = {
        "claim_id": "synthesis:global_synthesis:global-1",
        "paper_keys": ["bridge-left", "bridge-right"],
        "text": "The cross-context difference remains the shared conclusion.",
        # This is the real derived lineage form: the ready global task points
        # to the prior cross-group assertion, whose source closure points to
        # the canonical claims above. Evidence and qualifier fields are
        # inherited only from that actual task result.
        "source_claim_ids": [claim["claim_id"]],
    }
    scope = build_candidate_output_scope_v1(
        inventory,
        topic_routes,
        selected_relation_ids=["relation:confirmed:bridge"],
        bridge_claims=[
            {
                "task_id": "cross_group_comparison",
                "result_id": "bridge-result:actual-1",
                "provider_output": {"bridge_claims": [claim]},
            },
            {
                "task_id": "global_synthesis",
                "result_id": "global-result:actual-1",
                "provider_output": {"synthesis_claims": [global_claim]},
            },
        ],
    )

    assert len(scope.claim_groups) == 2
    assert scope.max_claims == 2
    assert {slot.paper_key for slot in scope.claim_slots} == {"bridge-left", "bridge-right"}
    assert scope.max_support_rows == len(scope.claim_slots) == 4
    assert scope.for_papers(["bridge-left"]).claim_slots == ()
    slots = tuple(
        slot for slot in scope.claim_slots
        if slot.task_id == "global_synthesis"
    )
    payload = {
        "sections": [{
            "section_id": "section:cross-context",
            "task_ids": ["global_synthesis"],
            "title": "Cross-context result",
            "goal": "Compare the two verified contexts.",
            "paper_keys": ["bridge-left", "bridge-right"],
            "relation_ids": [],
            "claims": [global_claim["text"]],
            "claim_support": [{
                "claim_slot_id": slot.claim_slot_id,
                "task_id": slot.task_id,
                "claim": global_claim["text"],
                "paper_key": slot.paper_key,
                "primary_claim_id": slot.primary_claim_id,
                "source_claim_ids": list(slot.source_claim_ids),
                "evidence_ids": list(slot.evidence_ids),
                "source_field_ids": list(slot.source_field_ids),
            } for slot in slots],
        }],
    }
    scope.validate(payload)

    split_group = {
        "sections": [{
            "section_id": f"section:split:{index}",
            "task_ids": ["global_synthesis"],
            "title": f"Section {index}",
            "goal": "Present the verified cross-topic result.",
            "paper_keys": [slot.paper_key],
            "relation_ids": [],
            "claims": [global_claim["text"]],
            "claim_support": [{
                "claim_slot_id": slot.claim_slot_id,
                "task_id": slot.task_id,
                "claim": global_claim["text"],
                "paper_key": slot.paper_key,
                "primary_claim_id": slot.primary_claim_id,
                "source_claim_ids": list(slot.source_claim_ids),
                "evidence_ids": list(slot.evidence_ids),
                "source_field_ids": list(slot.source_field_ids),
            }],
        } for index, slot in enumerate(slots, start=1)],
    }
    with pytest.raises(CandidateOutputScopeError, match="cannot be split"):
        scope.validate(split_group)

