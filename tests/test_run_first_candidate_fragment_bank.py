from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from outline.candidate_fragment_bank import (
    CandidateFragmentBankError,
    build_candidate_fragment_bank_v1,
    build_candidate_organization_request_v1,
    expand_candidate_organization_map_v1,
    resolve_candidate_fragment_claims,
    validate_candidate_fragment_bank_intrinsic_v1,
    validate_candidate_fragment_bank_v1,
    validate_candidate_organization_map_for_request_v1,
    validate_candidate_organization_map_v1,
)
from outline.candidate_output_scope import (
    _inventory_indexes,
    _source_closure,
    build_candidate_output_scope_v1,
)
from outline.v3_models import OrganizingAxis, OutlineCandidatePlan
from runtime.provider_runtime import hash_json
from runtime.provider_context import ProviderContextProfile
from services.artifact_registry import ArtifactRegistry
from services.job_workspace import JobWorkspace
from services.writer_source_inventory import load_writer_source_inventory_v1
from tests.test_outline_v3_semantic_execution import _summary
from tests.writer_source_fixture import bind_production_writer_sources


PAPER_KEYS = ("paper-a", "paper-b")
CANONICAL_TEXT = "The same source-bounded wording is retained for both papers."
REQUIRED_COMPARISON_TEXT = (
    "The papers differ in their reported conditions; both source-side qualifiers remain attached."
)


def _sealed_scope(
    tmp_path,
) -> tuple[Any, list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], Any]:
    workspace = JobWorkspace.create(str(tmp_path), "candidate-fragment-bank", job_id="fragment-bank-job")
    registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    summaries = [
        _summary(paper_key, f"Source paper {paper_key}", f"Evidence for {paper_key} under its recorded conditions.")
        for paper_key in PAPER_KEYS
    ]
    fixture_service = type("FixtureService", (), {})()
    fixture_service.registry = registry
    fixture_service.workspace = workspace
    fixture_service.summaries = summaries
    bind_production_writer_sources(fixture_service, [])
    inventory = load_writer_source_inventory_v1(registry)
    indexes = _inventory_indexes(inventory)

    canonical_claims: list[dict[str, Any]] = []
    evidence_by_paper: dict[str, tuple[str, ...]] = {}
    primary_by_paper: dict[str, str] = {}
    for paper_key in PAPER_KEYS:
        paper = inventory.paper_by_key()[paper_key]
        primary = next(
            claim for claim in paper.claims
            if claim.claim_type == "empirical_finding" and claim.evidence_ids
        )
        source_ids, evidence_ids, source_field_ids, _study_id = _source_closure(
            paper, primary.claim_id, indexes
        )
        evidence_by_paper[paper_key] = evidence_ids
        primary_by_paper[paper_key] = primary.claim_id
        canonical_claims.append({
            "claim_id": f"synthesis:topic_synthesis:{paper_key}",
            "fragment_id": "fragment:shared",
            "claim_type": "empirical_finding",
            "paper_key": paper_key,
            "text": CANONICAL_TEXT,
            "source_claim_ids": list(source_ids),
            "evidence_ids": list(evidence_ids),
            "source_field_ids": list(source_field_ids),
            "primary_claim_id": primary.claim_id,
            "relation_ids": [],
        })

    topic_id = "topic:shared"
    fragment_id = "fragment:shared"
    result_id = "topic-result:shared"
    route = {
        "topic_id": topic_id,
        "logical_node_id": "topic_synthesis:shared",
        "paper_ids": list(PAPER_KEYS),
        "fragments": [{
            "fragment_id": fragment_id,
            "paper_ids": list(PAPER_KEYS),
            "provider_results": [{
                "result_id": result_id,
                "provider_output": {"claims": canonical_claims},
            }],
        }],
    }

    cross_claim = {
        "claim_id": "synthesis:cross_group_comparison:bridge",
        "claim_type": "author_interpretation",
        "fragment_id": "fragment:cross",
        "topic_ids": [topic_id],
        "paper_keys": list(PAPER_KEYS),
        "text": "The two source-bounded findings are compared together.",
        "source_claim_ids": [claim["claim_id"] for claim in canonical_claims],
        "evidence_ids": sorted({value for values in evidence_by_paper.values() for value in values}),
        "source_field_ids": sorted({
            field_id
            for claim in canonical_claims
            for field_id in claim["source_field_ids"]
        }),
        "primary_claim_ids_by_paper": {
            paper_key: [primary_by_paper[paper_key]] for paper_key in PAPER_KEYS
        },
        "relation_ids": [],
    }
    global_claim = {
        "claim_id": "synthesis:global_synthesis:summary",
        "claim_type": "author_interpretation",
        "paper_keys": list(PAPER_KEYS),
        "text": "The global synthesis preserves both paper-level closures.",
        "source_claim_ids": [cross_claim["claim_id"]],
        "evidence_ids": cross_claim["evidence_ids"],
        "source_field_ids": cross_claim["source_field_ids"],
        "primary_claim_ids_by_paper": {
            paper_key: [primary_by_paper[paper_key]] for paper_key in PAPER_KEYS
        },
        "relation_ids": [],
    }
    bridge_claims = [
        {
            "task_id": "cross_group_comparison",
            "result_id": "cross-result:shared",
            "provider_output": {"bridge_claims": [cross_claim]},
        },
        {
            "task_id": "global_synthesis",
            "result_id": "global-result:shared",
            "provider_output": {"synthesis_claims": [global_claim]},
        },
    ]

    required_evidence = sorted({value for values in evidence_by_paper.values() for value in values})
    primary_ids = [primary_by_paper[paper_key] for paper_key in PAPER_KEYS]

    def comparison_bundle(relation_id: str) -> dict[str, Any]:
        return {
            "relation_id": relation_id,
            "relation_type": "contrasts",
            "comparison_question": "How do the two source-bounded findings differ?",
            "paper_ids": list(PAPER_KEYS),
            "evidence_completeness": "complete",
            "missing_evidence_ids": [],
            "required_evidence_ids": required_evidence,
            "provided_evidence_ids": required_evidence,
            "claim_ids_left": [primary_ids[0]],
            "claim_ids_right": [primary_ids[1]],
        }

    relation_bundles = [comparison_bundle("relation:required-ab"), comparison_bundle("relation:optional-ab")]
    scope = build_candidate_output_scope_v1(
        inventory,
        [route],
        selected_relation_ids=[item["relation_id"] for item in relation_bundles],
        bridge_claims=bridge_claims,
        relation_bundles=relation_bundles,
    )
    return inventory, [route], bridge_claims, relation_bundles, scope


def _artifact_ref(artifact_id: str, content_hash: str) -> dict[str, str]:
    return {"artifact_id": artifact_id, "content_hash": content_hash}


def _receipt_ref(receipt_id: str, content_hash: str) -> dict[str, str]:
    return {"receipt_id": receipt_id, "content_hash": content_hash}


def _comparison_row(group_id: str, label: str) -> dict[str, Any]:
    # These are fixture references for intrinsic checks; they confer no Registry
    # or live-provider authority.
    return {
        "claim_group_id": group_id,
        "text": REQUIRED_COMPARISON_TEXT if label == "required" else f"Materialized comparison fixture for {label}.",
        "artifact_refs": [_artifact_ref(f"fixture-artifact:{label}", "a" * 64)],
        "provider_receipt_refs": [_receipt_ref(f"fixture-receipt:{label}", "b" * 64)],
    }


def _build_bank(fixture, *, comparison_rows=None, required_group_ids=()):
    inventory, topic_routes, bridge_claims, _bundles, scope = fixture
    canonical = resolve_candidate_fragment_claims(
        scope, inventory, topic_routes, bridge_claims
    )
    source_ref = {
        "artifact_id": inventory.artifact_id,
        "content_hash": inventory.artifact_hash,
        "inventory_content_hash": inventory.content_hash,
    }
    canonical_before = deepcopy(canonical)
    routes_before = deepcopy(topic_routes)
    comparisons_before = deepcopy(list(comparison_rows or ()))
    bank = build_candidate_fragment_bank_v1(
        scope,
        canonical,
        comparison_rows or (),
        required_group_ids,
        [source_ref],
    )
    assert canonical == canonical_before
    assert topic_routes == routes_before
    assert list(comparison_rows or ()) == comparisons_before
    return canonical, bank


def _seed_group(scope, relation_id: str) -> str:
    return next(
        group.claim_group_id for group in scope.claim_groups
        if group.claim_seed and dict(group.task_contract).get("relation_id") == relation_id
    )


def _organization_context(
    bank: dict[str, Any],
    scope: Any,
    *,
    candidate_id: str,
    required_group_ids: list[str],
):
    axis = OrganizingAxis(
        axis_id=f"axis:{candidate_id}",
        organizing_logic="organize source contributions by supported relation",
        label="Source contribution",
        rationale="Use the existing axis semantics.",
    )
    plan = OutlineCandidatePlan(
        candidate_id=candidate_id,
        organizing_logic=axis.organizing_logic,
        axis_id=axis.axis_id,
        shared_artifact_hashes={},
        required_node_ids=[],
        provider_generation_node_id=f"generation:{candidate_id}",
    )
    metadata_catalog = [{
        "title": "Source-bounded findings and comparison",
        "goal": next((dict(group.task_contract)["comparison_question"] for group in scope.claim_groups
                      if group.claim_seed and group.claim_group_id in required_group_ids),
                     "Explain the complete source closures and the required comparison."),
        "rationale": "Keep the two source sides and their qualifiers together.",
    }]
    request = build_candidate_organization_request_v1(
        bank,
        scope,
        axis=axis,
        plan=plan,
        required_group_ids=required_group_ids,
        required_paper_keys=list(PAPER_KEYS),
        metadata_catalog=metadata_catalog,
    )
    return request, axis, plan, metadata_catalog


def _expand_from_request(
    organization_map: dict[str, Any],
    request: dict[str, Any],
    bank: dict[str, Any],
    scope: Any,
    required_group_ids: list[str],
) -> dict[str, Any]:
    return expand_candidate_organization_map_v1(
        organization_map,
        bank,
        scope,
        required_group_ids,
        list(PAPER_KEYS),
        axis=request["axis_payload"],
        plan=request["plan_payload"],
        metadata_catalog=request["metadata_catalog"],
    )


def _complete_map(request: dict[str, Any], *, deferred: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    deferred = deferred or []
    deferred_indices = {
        index for item in deferred for index in item["fragment_indices"]
    }
    selected_indices = [
        row[0] for row in request["fragment_cards"]
        if row[3] == "materialized" and row[0] not in deferred_indices
    ]
    selected_papers = sorted({
        paper_index
        for fragment_index in selected_indices
        for paper_index in request["fragment_cards"][fragment_index][2]
    })
    paper_roles = []
    for paper_index in selected_papers:
        role_fragment_index = next(
            fragment_index for fragment_index in selected_indices
            if paper_index in request["fragment_cards"][fragment_index][2]
        )
        paper_roles.append([paper_index, role_fragment_index])
    return {
        "schema_version": "candidate-organization-map/v1",
        "candidate_id": request["candidate_id"],
        "axis_hash": request["axis_hash"],
        "plan_hash": request["plan_hash"],
        "bank_hash": request["bank_hash"],
        "map_schema_hash": request["map_schema_hash"],
        "metadata_catalog_hash": request["metadata_catalog_hash"],
        "metadata_catalog_contract_hash": request["metadata_catalog_contract_hash"],
        "required_group_indices": request["required_group_indices"],
        "required_paper_indices": request["required_paper_indices"],
        "organization_contract_hash": request["organization_contract_hash"],
        "sections": [{
            "section_index": 0,
            "metadata_index": 0,
            "paper_roles": paper_roles,
            "fragment_indices": selected_indices,
        }],
        "deferred": deferred,
    }


def test_explicit_empty_comparison_obligations_do_not_invent_tasks(source_fixture):
    _inventory, _routes, _bridges, _bundles, scope = source_fixture
    _canonical, bank = _build_bank(source_fixture)
    request, _axis, _plan, _catalog = _organization_context(
        bank, scope, candidate_id="no-required-comparison", required_group_ids=[],
    )
    assert request["required_group_indices"] == []
    unresolved = [row[0] for row in request["fragment_cards"] if row[3] == "unresolved"]
    response = _complete_map(request, deferred=[{
        "reason_code": "optional_unresolved", "fragment_indices": unresolved,
    }])
    compiled = _expand_from_request(response, request, bank, scope, [])
    assert compiled["candidate_id"] == "no-required-comparison"
    assert {key for section in compiled["sections"] for key in section["paper_keys"]} == set(PAPER_KEYS)
    assert all(
        not bank["groups"][index]["claim_group"].get("claim_seed", False)
        for section in response["sections"] for index in section["fragment_indices"]
    )


@pytest.fixture
def source_fixture(tmp_path):
    return _sealed_scope(tmp_path)


def test_resolution_binds_full_task_topic_fragment_result_and_claim_identity(source_fixture):
    inventory, routes, bridges, _bundles, scope = source_fixture
    canonical = resolve_candidate_fragment_claims(scope, inventory, routes, bridges)
    expected = {group.claim_group_id for group in scope.claim_groups if not group.claim_seed}
    assert set(canonical) == expected
    assert all(canonical[group_id]["text"] in {CANONICAL_TEXT,
        "The two source-bounded findings are compared together.",
        "The global synthesis preserves both paper-level closures."}
        for group_id in expected)

    foreign_result = deepcopy(routes)
    foreign_result[0]["fragments"][0]["provider_results"][0]["result_id"] = "foreign-result"
    with pytest.raises(CandidateFragmentBankError, match="identity"):
        resolve_candidate_fragment_claims(scope, inventory, foreign_result, bridges)

    foreign_owner = deepcopy(routes)
    foreign_owner[0]["fragments"][0]["provider_results"][0]["provider_output"]["claims"][0]["paper_key"] = "paper-b"
    with pytest.raises(CandidateFragmentBankError, match="source claim|closure|hash"):
        resolve_candidate_fragment_claims(scope, inventory, foreign_owner, bridges)


def test_source_closure_and_empty_study_values_expand_losslessly(source_fixture):
    inventory, _routes, _bridges, _bundles, scope = source_fixture
    required = _seed_group(scope, "relation:required-ab")
    _canonical, bank = _build_bank(
        source_fixture,
        comparison_rows=[_comparison_row(required, "required")],
        required_group_ids=[required],
    )
    request, axis, plan, metadata_catalog = _organization_context(
        bank, scope, candidate_id="candidate-a", required_group_ids=[required]
    )
    assert request["axis_payload"] == axis.to_dict()
    assert request["plan_payload"] == plan.to_dict()
    assert request["axis_hash"] == hash_json(axis.to_dict())
    assert request["plan_hash"] == hash_json(plan.to_dict())
    assert request["metadata_catalog"] == metadata_catalog
    assert request["metadata_catalog_hash"] == hash_json(metadata_catalog)
    assert request["metadata_catalog_contract_hash"]
    assert request["required_group_indices"] == [next(
        entry["index"] for entry in bank["groups"]
        if entry["claim_group"]["claim_group_id"] == required
    )]
    assert request["required_paper_indices"] == [0, 1]
    assert request["organization_contract_hash"]
    original_catalog_title = request["metadata_catalog"][0]["title"]
    metadata_catalog[0]["title"] = "Mutated caller-owned catalog"
    assert request["metadata_catalog"][0]["title"] == original_catalog_title
    canonical_text_index = request["text_pool"].index(CANONICAL_TEXT)
    canonical_cards = [
        card for card in request["fragment_cards"]
        if card[1] == canonical_text_index
    ]
    assert len(canonical_cards) == 2
    assert request["bank_group_count"] == len(bank["groups"])
    assert request["bank_schema_version"] == "outline_candidate_fragment_bank/v1"
    assert "support_slots" not in request
    assert "source_claim_ids" not in request
    assert "evidence_ids" not in request
    assert "source_field_ids" not in request
    assert "claim_slot_id" not in request
    for card in request["fragment_cards"]:
        fragment_index, text_index, paper_indices, state, task_ref, topic_ref = card
        entry = bank["groups"][fragment_index]
        group = entry["claim_group"]
        assert entry["index"] == fragment_index
        assert state == entry["materialization_state"]
        assert request["task_refs"][task_ref] == group["task_id"]
        if topic_ref is not None:
            assert request["topic_refs"][topic_ref] == group["topic_id"]
        assert [request["paper_keys"][index] for index in paper_indices] == group["paper_keys"]
        if text_index is None:
            assert entry["text"] is None
        else:
            assert request["text_pool"][text_index] == entry["text"]
    optional = _seed_group(scope, "relation:optional-ab")
    optional_index = next(
        row["index"] for row in bank["groups"] if row["claim_group"]["claim_group_id"] == optional
    )
    organization_map = _complete_map(
        request,
        deferred=[{"fragment_indices": [optional_index], "reason_code": "optional_unresolved"}],
    )
    payload = _expand_from_request(organization_map, request, bank, scope, [required])
    scope.validate(payload)

    section = payload["sections"][0]
    assert set(section["paper_keys"]) == set(PAPER_KEYS)
    assert section["claims"].count(CANONICAL_TEXT) == 1
    assert len([row for row in section["claim_support"] if row["claim"] == CANONICAL_TEXT]) == 2
    supports_by_slot = {row["claim_slot_id"]: row for row in section["claim_support"]}
    for slot in scope.claim_slots:
        if slot.claim_group_id == optional:
            continue
        support = supports_by_slot[slot.claim_slot_id]
        assert support["claim"] == (
            REQUIRED_COMPARISON_TEXT if slot.claim_group_id == required
            else next(row["text"] for row in bank["groups"]
                      if row["claim_group"]["claim_group_id"] == slot.claim_group_id)
        )
        assert support["source_claim_ids"] == list(slot.source_claim_ids)
        assert support["evidence_ids"] == list(slot.evidence_ids)
        assert support["source_field_ids"] == list(slot.source_field_ids)
        assert support["study_id"] == slot.study_id
    assert all(not row["study_id"] for row in supports_by_slot.values())
    required_slots = [slot for slot in scope.claim_slots if slot.claim_group_id == required]
    assert {slot.paper_key for slot in required_slots} == set(PAPER_KEYS)
    assert all(len(slot.source_claim_ids) > 1 for slot in required_slots)
    assert all(slot.evidence_ids and slot.source_field_ids for slot in required_slots)
    assert all(supports_by_slot[slot.claim_slot_id]["paper_key"] == slot.paper_key for slot in required_slots)
    assert inventory.is_verified


def test_required_seed_must_have_real_text_and_artifact_receipt_references(source_fixture):
    _inventory, _routes, _bridges, _bundles, scope = source_fixture
    required = _seed_group(scope, "relation:required-ab")
    with pytest.raises(CandidateFragmentBankError, match="required comparison"):
        _build_bank(source_fixture, required_group_ids=[required])


@pytest.mark.parametrize('mutation', ['wrong_goal', 'removed_constraint', 'foreign_pair', 'bool_group'])
def test_required_comparison_organization_binds_exact_question_and_pair(source_fixture, mutation):
    scope = source_fixture[-1]
    required = _seed_group(scope, 'relation:required-ab')
    _canonical, bank = _build_bank(source_fixture,
                                  comparison_rows=[_comparison_row(required, 'required')],
                                  required_group_ids=[required])
    _request, axis, plan, catalog = _organization_context(
        bank, scope, candidate_id='candidate_1', required_group_ids=[required],
    )
    catalog.append({'title': 'Other evidence', 'goal': 'A different source question.', 'rationale': 'Other purpose.'})
    request = build_candidate_organization_request_v1(
        bank, scope, axis=axis, plan=plan, required_group_ids=[required],
        required_paper_keys=list(PAPER_KEYS), metadata_catalog=catalog,
    )
    unresolved = [row[0] for row in request['fragment_cards'] if row[3] == 'unresolved']
    response = _complete_map(request, deferred=[{
        'reason_code': 'optional_unresolved', 'fragment_indices': unresolved,
    }])
    assert request['required_comparison_sections']
    if mutation == 'wrong_goal':
        response['sections'][0]['metadata_index'] = 1
    elif mutation == 'removed_constraint':
        request['required_comparison_sections'] = []
    elif mutation == 'foreign_pair':
        request['required_comparison_sections'][0]['paper_indices'] = [0]
    else:
        request['required_comparison_sections'][0]['group_index'] = True
    with pytest.raises(CandidateFragmentBankError):
        validate_candidate_organization_map_for_request_v1(response, request)


def test_request_rejects_unresolved_required_group_and_paper_without_materialized_witness(source_fixture):
    inventory, routes, _bridges, relation_bundles, _scope = source_fixture
    required_seed = _seed_group(_scope, "relation:required-ab")
    _canonical, unresolved_bank = _build_bank(
        source_fixture,
        comparison_rows=(),
        required_group_ids=(),
    )
    unresolved_axis = OrganizingAxis(
        axis_id="axis:required-unresolved",
        organizing_logic="organize by supported relation",
        label="Required comparison",
        rationale="Existing axis metadata.",
    )
    unresolved_plan = OutlineCandidatePlan(
        candidate_id="candidate-required-unresolved",
        organizing_logic=unresolved_axis.organizing_logic,
        axis_id=unresolved_axis.axis_id,
    )
    metadata_catalog = [{"title": "Title", "goal": "Goal", "rationale": "Rationale"}]
    with pytest.raises(CandidateFragmentBankError, match="required group .* unresolved before organization"):
        build_candidate_organization_request_v1(
            unresolved_bank,
            _scope,
            axis=unresolved_axis,
            plan=unresolved_plan,
            required_group_ids=[required_seed],
            required_paper_keys=list(PAPER_KEYS),
            metadata_catalog=metadata_catalog,
        )

    route_a = deepcopy(routes[0])
    route_a["paper_ids"] = [PAPER_KEYS[0]]
    route_a["fragments"][0]["paper_ids"] = [PAPER_KEYS[0]]
    result = route_a["fragments"][0]["provider_results"][0]
    result["provider_output"]["claims"] = [
        claim for claim in result["provider_output"]["claims"]
        if claim["paper_key"] == PAPER_KEYS[0]
    ]
    scope_a = build_candidate_output_scope_v1(
        inventory,
        [route_a],
        selected_relation_ids=[item["relation_id"] for item in relation_bundles],
        relation_bundles=relation_bundles,
    )
    canonical_a = resolve_candidate_fragment_claims(scope_a, inventory, [route_a], [])
    bank_a = build_candidate_fragment_bank_v1(
        scope_a,
        canonical_a,
        (),
        (),
        [{
            "artifact_id": inventory.artifact_id,
            "content_hash": inventory.artifact_hash,
            "inventory_content_hash": inventory.content_hash,
        }],
    )
    only_a_group = next(iter(canonical_a))
    axis_a = OrganizingAxis(
        axis_id="axis:required-paper-witness",
        organizing_logic="organize by supported relation",
        label="Paper witness",
        rationale="Existing axis metadata.",
    )
    plan_a = OutlineCandidatePlan(
        candidate_id="candidate-required-paper-witness",
        organizing_logic=axis_a.organizing_logic,
        axis_id=axis_a.axis_id,
    )
    with pytest.raises(CandidateFragmentBankError, match="required paper paper-b has no materialized complete group"):
        build_candidate_organization_request_v1(
            bank_a,
            scope_a,
            axis=axis_a,
            plan=plan_a,
            required_group_ids=[only_a_group],
            required_paper_keys=list(PAPER_KEYS),
            metadata_catalog=metadata_catalog,
        )


def test_canonical_claim_hash_is_rechecked_before_bank_materialization(source_fixture):
    inventory, routes, bridges, _bundles, scope = source_fixture
    canonical = resolve_candidate_fragment_claims(scope, inventory, routes, bridges)
    group_id = next(iter(canonical))
    altered = deepcopy(canonical)
    altered[group_id]["text"] = "A different sentence cannot replace the stored claim."
    source_ref = {
        "artifact_id": inventory.artifact_id,
        "content_hash": inventory.artifact_hash,
        "inventory_content_hash": inventory.content_hash,
    }
    with pytest.raises(CandidateFragmentBankError, match="hash differs from scope"):
        build_candidate_fragment_bank_v1(scope, altered, (), (), [source_ref])


def test_optional_seed_may_be_deferred_or_materialized_and_selected(source_fixture):
    _inventory, _routes, _bridges, _bundles, scope = source_fixture
    required = _seed_group(scope, "relation:required-ab")
    optional = _seed_group(scope, "relation:optional-ab")
    rows = [_comparison_row(required, "required"), _comparison_row(optional, "optional")]
    _canonical, bank = _build_bank(
        source_fixture,
        comparison_rows=rows,
        required_group_ids=[required],
    )
    request, _axis, _plan, _catalog = _organization_context(
        bank, scope, candidate_id="candidate-b", required_group_ids=[required]
    )
    organization_map = _complete_map(request)
    payload = _expand_from_request(organization_map, request, bank, scope, [required])
    selected = {
        row["claim_slot_id"] for row in payload["sections"][0]["claim_support"]
    }
    optional_slots = {
        slot.claim_slot_id for slot in scope.claim_slots if slot.claim_group_id == optional
    }
    assert optional_slots.issubset(selected)
    optional_entry = next(
        row for row in bank["groups"] if row["claim_group"]["claim_group_id"] == optional
    )
    assert optional_entry["materialization_state"] == "materialized"


def test_bank_hash_and_exact_scope_bindings_reject_tampering(source_fixture):
    _inventory, _routes, _bridges, _bundles, scope = source_fixture
    required = _seed_group(scope, "relation:required-ab")
    _canonical, bank = _build_bank(
        source_fixture,
        comparison_rows=[_comparison_row(required, "required")],
        required_group_ids=[required],
    )
    validate_candidate_fragment_bank_intrinsic_v1(bank)
    validate_candidate_fragment_bank_v1(bank, scope)

    changed_text = deepcopy(bank)
    changed_text["groups"][0]["text"] = "Changed text without a new hash."
    with pytest.raises(CandidateFragmentBankError, match="content hash|text hash|canonical"):
        validate_candidate_fragment_bank_intrinsic_v1(changed_text)

    changed_slots = deepcopy(bank)
    changed_slots["groups"][0]["support_slots"].pop()
    # Recompute the outer hash to prove that scope binding catches a coherent-looking forgery.
    from runtime.provider_runtime import hash_json

    changed_slots["content_hash"] = hash_json({key: value for key, value in changed_slots.items() if key != "content_hash"})
    with pytest.raises(CandidateFragmentBankError, match="support slot|scope"):
        validate_candidate_fragment_bank_v1(changed_slots, scope)


@pytest.mark.parametrize(
    "mutation, message",
    [
        (lambda value: value["sections"][0]["fragment_indices"].append(True), "index"),
        (lambda value: value["sections"][0].update({"claims": ["invented claim"]}), "field"),
        (lambda value: value["sections"][0].update({"title": "Unsupported effect assertion."}), "unknown or missing fields"),
        (lambda value: value["sections"][0].update({"goal": "Unsupported effect assertion."}), "unknown or missing fields"),
        (lambda value: value["sections"][0].update({"rationale": "Unsupported effect assertion."}), "unknown or missing fields"),
        (lambda value: value["sections"][0]["paper_roles"].__setitem__(0, {"paper_index": 0, "role": "Unsupported effect assertion."}), "two-index pair"),
        (lambda value: value["sections"][0]["paper_roles"].__setitem__(0, {"paper_index": 0, "role_fragment_index": 0}), "two-index pair"),
        (lambda value: value["sections"][0]["fragment_indices"].append(999), "index"),
    ],
)
def test_composition_rejects_boolean_foreign_and_freeform_claim_fields(source_fixture, mutation, message):
    _inventory, _routes, _bridges, _bundles, scope = source_fixture
    required = _seed_group(scope, "relation:required-ab")
    _canonical, bank = _build_bank(
        source_fixture,
        comparison_rows=[_comparison_row(required, "required")],
        required_group_ids=[required],
    )
    request, _axis, _plan, _catalog = _organization_context(
        bank, scope, candidate_id="candidate-a", required_group_ids=[required]
    )
    payload = _complete_map(
        request,
        deferred=[{
            "fragment_indices": [_seed_group_index(bank, "relation:optional-ab")],
            "reason_code": "optional_unresolved",
        }],
    )
    mutation(payload)
    with pytest.raises(CandidateFragmentBankError, match=message):
        _expand_from_request(payload, request, bank, scope, [required])


def _seed_group_index(bank: dict[str, Any], relation_id: str) -> int:
    return next(
        row["index"] for row in bank["groups"]
        if dict(row["claim_group"].get("task_contract") or {}).get("relation_id") == relation_id
    )


def test_positional_role_pairs_reduce_600_role_wire_cost_under_production_profile():
    """A deterministic format-unit comparison, not a 600-paper acceptance test."""
    profile = ProviderContextProfile.conservative(
        provider="fixture",
        model="outline-v3",
        endpoint_type="internal",
        model_context_limit=128_000,
        max_output_tokens=4_096,
    )
    old_dictionary_roles = [
        {"paper_index": index, "role_fragment_index": index}
        for index in range(600)
    ]
    positional_roles = [[index, index] for index in range(600)]
    old_tokens = profile.estimate_tokens(old_dictionary_roles)
    positional_tokens = profile.estimate_tokens(positional_roles)
    assert old_tokens > 16_000
    assert positional_tokens < old_tokens


def test_organization_map_is_axis_plan_and_bank_bound(source_fixture):
    _inventory, _routes, _bridges, _bundles, scope = source_fixture
    required = _seed_group(scope, "relation:required-ab")
    _canonical, bank = _build_bank(
        source_fixture,
        comparison_rows=[_comparison_row(required, "required")],
        required_group_ids=[required],
    )
    request, _axis, _plan, _catalog = _organization_context(
        bank, scope, candidate_id="candidate-a", required_group_ids=[required]
    )
    payload = _complete_map(
        request,
        deferred=[{
            "fragment_indices": [_seed_group_index(bank, "relation:optional-ab")],
            "reason_code": "optional_unresolved",
        }],
    )
    missing_required_binding = deepcopy(payload)
    missing_required_binding["required_group_indices"] = []
    with pytest.raises(CandidateFragmentBankError, match="required_group_indices"):
        _expand_from_request(missing_required_binding, request, bank, scope, [required])
    payload["axis_hash"] = "0" * 64
    with pytest.raises(CandidateFragmentBankError, match="axis"):
        _expand_from_request(payload, request, bank, scope, [required])


def test_request_bound_and_bank_full_validators_match_and_reject_tampered_context(source_fixture):
    _inventory, _routes, _bridges, _bundles, scope = source_fixture
    required = _seed_group(scope, "relation:required-ab")
    _canonical, bank = _build_bank(
        source_fixture,
        comparison_rows=[_comparison_row(required, "required")],
        required_group_ids=[required],
    )
    request, axis, plan, catalog = _organization_context(
        bank, scope, candidate_id="candidate-validator-parity", required_group_ids=[required]
    )
    organization_map = _complete_map(
        request,
        deferred=[{
            "fragment_indices": [_seed_group_index(bank, "relation:optional-ab")],
            "reason_code": "optional_unresolved",
        }],
    )
    map_before = deepcopy(organization_map)
    request_before = deepcopy(request)
    validate_candidate_organization_map_for_request_v1(organization_map, request)
    validate_candidate_organization_map_v1(
        organization_map,
        bank,
        axis=axis,
        plan=plan,
        required_group_ids=[required],
        required_paper_keys=list(PAPER_KEYS),
        metadata_catalog=catalog,
    )
    assert organization_map == map_before
    assert request == request_before
    reordered = deepcopy(organization_map)
    reordered["sections"][0]["fragment_indices"].reverse()
    validate_candidate_organization_map_for_request_v1(reordered, request)
    reordered_payload = _expand_from_request(reordered, request, bank, scope, [required])
    expected_claim_order = list(dict.fromkeys(
        bank["groups"][index]["text"]
        for index in reordered["sections"][0]["fragment_indices"]
    ))
    assert reordered_payload["sections"][0]["claims"] == expected_claim_order

    altered_axis_request = deepcopy(request)
    altered_axis_request["axis_payload"]["label"] = "A different semantic axis"
    with pytest.raises(CandidateFragmentBankError, match="semantic payload hashes"):
        validate_candidate_organization_map_for_request_v1(organization_map, altered_axis_request)
    altered_coverage_request = deepcopy(request)
    altered_coverage_request["required_paper_indices"] = [0]
    with pytest.raises(CandidateFragmentBankError, match="contract hash"):
        validate_candidate_organization_map_for_request_v1(organization_map, altered_coverage_request)


def test_organization_map_requires_an_exact_used_deferred_partition(source_fixture):
    _inventory, _routes, _bridges, _bundles, scope = source_fixture
    required = _seed_group(scope, "relation:required-ab")
    _canonical, bank = _build_bank(
        source_fixture,
        comparison_rows=[_comparison_row(required, "required")],
        required_group_ids=[required],
    )
    request, _axis, _plan, _catalog = _organization_context(
        bank, scope, candidate_id="candidate-partition", required_group_ids=[required]
    )
    optional_index = _seed_group_index(bank, "relation:optional-ab")
    base = _complete_map(
        request,
        deferred=[{"fragment_indices": [optional_index], "reason_code": "optional_unresolved"}],
    )
    missing = deepcopy(base)
    missing["sections"][0]["fragment_indices"].pop()
    with pytest.raises(CandidateFragmentBankError, match="partition"):
        _expand_from_request(missing, request, bank, scope, [required])
    duplicated = deepcopy(base)
    duplicated["sections"][0]["fragment_indices"].append(
        duplicated["sections"][0]["fragment_indices"][0]
    )
    with pytest.raises(CandidateFragmentBankError, match="unique indices"):
        _expand_from_request(duplicated, request, bank, scope, [required])


def test_unresolved_fragment_cannot_be_selected_and_required_group_cannot_be_deferred(source_fixture):
    _inventory, _routes, _bridges, _bundles, scope = source_fixture
    required = _seed_group(scope, "relation:required-ab")
    _canonical, bank = _build_bank(
        source_fixture,
        comparison_rows=[_comparison_row(required, "required")],
        required_group_ids=[required],
    )
    request, _axis, _plan, _catalog = _organization_context(
        bank, scope, candidate_id="candidate-required", required_group_ids=[required]
    )
    optional_index = _seed_group_index(bank, "relation:optional-ab")
    required_index = _seed_group_index(bank, "relation:required-ab")
    unresolved_selected = _complete_map(
        request,
        deferred=[{"fragment_indices": [optional_index], "reason_code": "optional_unresolved"}],
    )
    unresolved_selected["sections"][0]["fragment_indices"].append(optional_index)
    unresolved_selected["deferred"] = []
    with pytest.raises(CandidateFragmentBankError, match="unresolved fragment"):
        _expand_from_request(unresolved_selected, request, bank, scope, [required])

    required_deferred = _complete_map(
        request,
        deferred=[{"fragment_indices": [optional_index], "reason_code": "optional_unresolved"}],
    )
    required_deferred["sections"][0]["fragment_indices"].remove(required_index)
    required_deferred["deferred"].append({
        "reason_code": "not_selected_for_axis",
        "fragment_indices": [required_index],
    })
    with pytest.raises(CandidateFragmentBankError, match="omits a required group index"):
        _expand_from_request(required_deferred, request, bank, scope, [required])
