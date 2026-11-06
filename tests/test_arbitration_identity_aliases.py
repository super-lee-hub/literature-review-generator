from __future__ import annotations

from copy import deepcopy

import pytest

from outline.arbitration_identity_aliases import (
    WIRE_KEY,
    ArbitrationIdentityAliasError,
    canonicalize_arbitration_identity_response,
    expand_arbitration_identity_request,
    project_arbitration_identity_request,
)
from outline.candidate_arbitration_final_projection import (
    project_arbitration_final_request,
)
from outline.candidate_arbitration_ledger_packing import pack_arbitration_final_ledger
from outline.v3_models import compute_v3_hash
from runtime.provider_context import ProviderContextProfile
from runtime.provider_runtime import hash_json
from tests import test_run_first_arbitration_final_projection as scale_fixture
from tests import test_run_first_arbitration_ledger_packing as ledger_fixture


def _final_request(scale: int = 500) -> dict:
    return project_arbitration_final_request(
        scale_fixture._request(scale_fixture._ledger(scale))
    )


def _alias(catalog: dict, kind: str, canonical: str) -> str:
    return next(
        row["alias"]
        for row in catalog["entries"]
        if row["kind"] == kind and row["canonical_value"] == canonical
    )


def _request_from_ledger(ledger: dict) -> dict:
    candidate_ids = list(ledger["candidate_ids"])
    section_hashes = {
        candidate: {
            row["section_id"]: row["section_hash"]
            for row in ledger["candidate_section_index"][candidate]
        }
        for candidate in candidate_ids
    }
    return project_arbitration_final_request({
        "candidate_ids": candidate_ids,
        "candidate_hashes": deepcopy(ledger["candidate_hashes"]),
        "critique_disposition": {
            "eligible_candidate_ids": candidate_ids,
            "issues": deepcopy(ledger["critique_issues"]),
        },
        "section_coordination_contract": {
            "candidate_section_hashes": section_hashes,
            "output_fields": "Preserve source section identities and boundaries.",
        },
        "candidate_contents": {
            candidate: {"candidate_id": candidate, "organizing_logic": "Preserve all source distinctions."}
            for candidate in candidate_ids
        },
        "arbitration_evaluation_ledger": pack_arbitration_final_ledger(ledger),
        "hierarchy": {
            "level": "all_candidate_final_arbitration",
            "ledger_hash": ledger["ledger_hash"],
            "coverage": deepcopy(ledger["coverage"]),
        },
        "evaluation_contract": "Choose globally while retaining every source boundary.",
    })


@pytest.mark.parametrize("fault", ["selected_candidate_id", "ledger_hash", "source_section_ids"])
def test_empty_typed_response_identity_is_rejected(fault):
    request = _final_request()
    catalog, wire = project_arbitration_identity_request(request)
    selected = _alias(catalog, "candidate_id", request["candidate_ids"][0])
    response = {"selected_candidate_id": selected}
    if fault == "source_section_ids":
        response["section_coordination"] = {
            "candidate_id": selected, "merge_groups": [{"source_section_ids": [""]}],
        }
    else:
        response[fault] = ""
    with pytest.raises(ArbitrationIdentityAliasError):
        canonicalize_arbitration_identity_response(response, wire, catalog)


def test_claim_support_partition_uses_the_same_typed_aliases_and_roundtrips():
    from outline.candidate_arbitration_hierarchy import build_arbitration_final_ledger
    from tests.test_run_first_arbitration_claim_support_parts import _plan, _response_for

    plan, _contents = _plan()
    ledger = build_arbitration_final_ledger(plan, {
        leaf["leaf_id"]: _response_for(plan, leaf) for leaf in plan["leaves"]
    })
    request = _request_from_ledger(ledger)
    catalog, wire = project_arbitration_identity_request(request)
    partition = wire["arbitration_evaluation_ledger"]["claim_support_partition"]
    claim_alias = _alias(catalog, "claim_id", "claim-target:010")
    assert claim_alias in partition["string_catalog"]
    assert "claim-target:010" not in partition["string_catalog"]
    assert "claim" in partition["string_catalog"]  # Issue scope remains literal.
    assert "A" in partition["string_catalog"] and "B" in partition["string_catalog"]
    assert expand_arbitration_identity_request(wire, catalog) == request


@pytest.mark.parametrize("scale", [500, 600])
def test_scale_fixture_request_aliases_expand_exactly_and_reduce_wire(scale: int) -> None:
    request = _final_request(scale)
    original = deepcopy(request)
    profile = ProviderContextProfile.conservative(
        provider="diagnostic", model="offline-only", endpoint_type="chat_completions"
    )
    before = profile.estimate_request(request)["estimated_input_tokens"]

    catalog, wire = project_arbitration_identity_request(request)
    after = profile.estimate_request(wire)["estimated_input_tokens"]

    assert request == original
    assert after < before
    assert expand_arbitration_identity_request(wire, catalog) == original
    assert hash_json(expand_arbitration_identity_request(wire, catalog)) == hash_json(original)
    assert WIRE_KEY in wire
    assert "entries" not in wire[WIRE_KEY]
    assert wire[WIRE_KEY]["catalog_hash"] == catalog["catalog_hash"]
    candidate_alias = _alias(catalog, "candidate_id", "candidate_1")
    assert candidate_alias in wire["candidate_ids"]
    assert wire["critique_disposition"]["eligible_candidate_ids"] == wire["candidate_ids"]
    assert candidate_alias in wire["candidate_contents"]
    assert candidate_alias in wire["arbitration_evaluation_ledger"]["candidate_section_index"]


def test_v3_closure_text_refs_are_aliased_without_rewriting_text_catalog_prose() -> None:
    ledger = ledger_fixture._shared_closure_ledger()
    shared_hash = ledger["candidate_section_index"]["candidate_1"][0]["source_closure_hash"]
    ledger["candidate_section_index"]["candidate_1"][0]["title"] = shared_hash
    ledger["ledger_hash"] = compute_v3_hash(
        {key: value for key, value in ledger.items() if key != "ledger_hash"}
    )
    packed = pack_arbitration_final_ledger(ledger)
    assert packed["ledger_packing"]["schema_version"] == "outline_candidate_arbitration_ledger_wire/v3"
    request = _request_from_ledger(ledger)
    catalog, wire = project_arbitration_identity_request(request)
    encoded_index = wire["arbitration_evaluation_ledger"]["candidate_section_index"]
    assert all(
        type(row[2]) is int
        for rows in encoded_index.values()
        for row in rows
    )
    title_row = encoded_index[_alias(catalog, "candidate_id", "candidate_1")][0]
    title_hash_text_ref = title_row[3]
    assert title_hash_text_ref == title_row[2]
    assert wire["arbitration_evaluation_ledger"]["ledger_text_catalog"][title_hash_text_ref] == shared_hash
    assert expand_arbitration_identity_request(wire, catalog) == request


def test_shared_array_values_keep_one_alias_and_each_checked_identity_role() -> None:
    ledger = ledger_fixture._ledger()
    ledger["candidate_section_index"]["candidate_1"][0]["paper_keys"] = ["shared-typed-id"]
    assessment = next(
        row for row in ledger["assessments"]
        if row["candidate_id"] == "candidate_1" and row["leaf_id"] == "leaf_1"
    )
    assessment["evidence_refs"] = ["shared-typed-id"]
    ledger["ledger_hash"] = compute_v3_hash(
        {key: value for key, value in ledger.items() if key != "ledger_hash"}
    )
    request = _request_from_ledger(ledger)
    catalog, wire = project_arbitration_identity_request(request)
    # This is one literal value in a shared table row, not two competing
    # canonical identities. Preserve both schema roles without inventing a gate.
    assert _alias(catalog, "paper_id", "shared-typed-id") == _alias(catalog, "evidence_id", "shared-typed-id")
    assert expand_arbitration_identity_request(wire, catalog) == request


def test_resigned_catalog_cannot_invent_a_role_missing_from_the_typed_request():
    from outline.arbitration_identity_aliases import _mapping_hash, _verify_catalog

    request = _final_request()
    catalog, wire = project_arbitration_identity_request(request)
    changed, changed_wire = deepcopy(catalog), deepcopy(wire)
    value = request["candidate_ids"][0]
    changed["entries"].append({"kind": "section_id", "canonical_value": value,
                               "alias": _alias(catalog, "candidate_id", value)})
    changed["entries"].sort(key=lambda row: (row["kind"], row["canonical_value"]))
    changed["allowed_echo_values"]["section_id"] = sorted({
        *changed["allowed_echo_values"]["section_id"], value,
    })
    changed["mapping_hash"] = _mapping_hash(changed["prefix"], changed["entries"],
                                           changed["array_catalog_aliases"], changed["text_catalog_aliases"])
    changed["catalog_hash"] = hash_json({key: item for key, item in changed.items() if key != "catalog_hash"})
    changed_wire[WIRE_KEY].update(mapping_hash=changed["mapping_hash"], catalog_hash=changed["catalog_hash"])
    assert _verify_catalog(changed) == changed  # Self-consistency alone is insufficient.
    with pytest.raises(ArbitrationIdentityAliasError, match="actual typed request scope"):
        expand_arbitration_identity_request(changed_wire, changed)


def test_shared_empty_array_catalog_row_stays_literal_and_roundtrips() -> None:
    ledger = ledger_fixture._ledger()
    section = ledger["candidate_section_index"]["candidate_1"][0]
    section["paper_keys"] = []
    section["task_ids"] = []
    section["relation_ids"] = []
    ledger["ledger_hash"] = compute_v3_hash(
        {key: value for key, value in ledger.items() if key != "ledger_hash"}
    )
    request = _request_from_ledger(ledger)
    catalog, wire = project_arbitration_identity_request(request)
    assert [] in wire["arbitration_evaluation_ledger"]["ledger_string_array_catalog"]
    assert expand_arbitration_identity_request(wire, catalog) == request


def test_claim_scope_issue_targets_use_claim_aliases_and_decode_strictly() -> None:
    ledger = ledger_fixture._ledger()
    issue = ledger["critique_issues"][0]
    issue["scope"] = "claim"
    issue["target_ids"] = ["source-claim:primary-1"]
    ledger["ledger_hash"] = compute_v3_hash(
        {key: value for key, value in ledger.items() if key != "ledger_hash"}
    )
    request = _request_from_ledger(ledger)
    catalog, wire = project_arbitration_identity_request(request)
    claim_alias = _alias(catalog, "claim_id", "source-claim:primary-1")
    issue_wire = wire["arbitration_evaluation_ledger"]["critique_issues"][0]
    array_index = issue_wire["target_ids_array_ref"]
    assert wire["arbitration_evaluation_ledger"]["ledger_string_array_catalog"][array_index] == [claim_alias]
    assert expand_arbitration_identity_request(wire, catalog) == request

    response = {
        "selected_candidate_id": _alias(catalog, "candidate_id", "candidate_1"),
        "selection_reasons": ["Retain the complete claim and source closure."],
        "accepted_recommendations": [
            {
                "issue_id": _alias(catalog, "issue_id", issue["issue_id"]),
                "target_claim_ids": [claim_alias],
            }
        ],
    }
    canonical = canonicalize_arbitration_identity_response(response, wire, catalog)
    assert canonical["accepted_recommendations"][0]["target_claim_ids"] == [
        "source-claim:primary-1"
    ]


def test_prose_that_looks_like_a_hash_or_alias_is_never_rewritten() -> None:
    request = _final_request(500)
    request["candidate_contents"]["candidate_1"]["organizing_logic"] = "f" * 64
    request["diagnostic_prose"] = "$A0 is literal prose and names no identity."
    candidate_hash_like = request["candidate_contents"]["candidate_1"]["organizing_logic"]
    catalog, wire = project_arbitration_identity_request(request)
    candidate_alias = _alias(catalog, "candidate_id", "candidate_1")

    assert catalog["prefix"] != "$A"
    assert wire["candidate_contents"][candidate_alias]["organizing_logic"] == candidate_hash_like
    assert wire["diagnostic_prose"] == "$A0 is literal prose and names no identity."
    assert expand_arbitration_identity_request(wire, catalog) == request


def test_all_candidate_lists_and_critic_shard_keys_share_typed_aliases() -> None:
    request = _final_request(500)
    request["critique_disposition"]["blocked_candidate_ids"] = ["candidate_2"]
    request["critiques"]["coverage_critique"] = {
        "candidate_shard_results": {
            "candidate_1": {"candidate_id": "candidate_1", "summary": "Literal critic prose."}
        }
    }
    catalog, wire = project_arbitration_identity_request(request)
    candidate_1 = _alias(catalog, "candidate_id", "candidate_1")
    candidate_2 = _alias(catalog, "candidate_id", "candidate_2")

    assert wire["candidate_ids"][0] == candidate_1
    assert wire["critique_disposition"]["eligible_candidate_ids"] == wire["candidate_ids"]
    assert wire["critique_disposition"]["blocked_candidate_ids"] == [candidate_2]
    shard_map = wire["critiques"]["coverage_critique"]["candidate_shard_results"]
    assert list(shard_map) == [candidate_1]
    assert shard_map[candidate_1]["candidate_id"] == candidate_1
    assert expand_arbitration_identity_request(wire, catalog) == request


def test_response_decodes_echoed_ids_hashes_and_map_keys_but_not_prose() -> None:
    ledger = scale_fixture._ledger(500)
    catalog, wire = project_arbitration_identity_request(
        project_arbitration_final_request(scale_fixture._request(ledger))
    )
    candidate_id = ledger["candidate_ids"][0]
    issue = ledger["critique_issues"][0]
    section_id = issue["target_ids"][0]
    evidence_id = issue["evidence_refs"][0]
    token_in_prose = _alias(catalog, "candidate_id", candidate_id)
    response = {
        "selected_candidate_id": token_in_prose,
        "selection_reasons": [f"The literal {token_in_prose} remains prose here."],
        "candidate_scores": {token_in_prose: 0.9},
        "accepted_recommendations": [
            {
                "issue_id": _alias(catalog, "issue_id", issue["issue_id"]),
                "target_section_ids": [_alias(catalog, "section_id", section_id)],
                "evidence_ids": [_alias(catalog, "evidence_id", evidence_id)],
                "replacement": f"Do not rewrite this {token_in_prose} phrase.",
            }
        ],
        "rejected_recommendations": [],
        "unresolved_risks": [],
        "section_coordination": {
            "candidate_id": token_in_prose,
            "merge_groups": [
                {
                    "source_section_ids": [_alias(catalog, "section_id", section_id)],
                    "source_section_hashes": {
                        _alias(catalog, "section_id", section_id): _alias(
                            catalog,
                            "section_hash",
                            ledger["candidate_section_index"][candidate_id][0]["section_hash"],
                        )
                    },
                    "new_section_id": "merged:new-section-1",
                    "title": "Preserve the authored section title.",
                    "goal": "Preserve the authored section goal.",
                    "integration_reason": "The source scopes are compatible.",
                    "canonical_intent": {
                        "title": "Preserve the authored section title.",
                        "goal": "Preserve the authored section goal.",
                        "source_intents": {
                            _alias(catalog, "section_id", section_id): {
                                "title": "Original title.",
                                "goal": "Original goal.",
                                "contribution": "This source section contributes a distinct boundary.",
                            }
                        },
                    },
                }
            ],
            "section_order": [
                _alias(catalog, "section_id", section_id),
                "merged:new-section-1",
            ],
        },
    }

    canonical = canonicalize_arbitration_identity_response(response, wire, catalog)
    assert canonical["selected_candidate_id"] == candidate_id
    assert canonical["candidate_scores"] == {candidate_id: 0.9}
    assert canonical["accepted_recommendations"][0]["issue_id"] == issue["issue_id"]
    assert canonical["accepted_recommendations"][0]["target_section_ids"] == [section_id]
    assert canonical["accepted_recommendations"][0]["evidence_ids"] == [evidence_id]
    assert canonical["selection_reasons"] == response["selection_reasons"]
    assert canonical["accepted_recommendations"][0]["replacement"] == response["accepted_recommendations"][0]["replacement"]
    coordination = canonical["section_coordination"]
    assert coordination["candidate_id"] == candidate_id
    assert coordination["merge_groups"][0]["source_section_ids"] == [section_id]
    assert section_id in coordination["merge_groups"][0]["source_section_hashes"]
    assert coordination["merge_groups"][0]["new_section_id"] == "merged:new-section-1"
    assert coordination["section_order"] == [section_id, "merged:new-section-1"]
    assert section_id in coordination["merge_groups"][0]["canonical_intent"]["source_intents"]


@pytest.mark.parametrize("fault", ["missing", "tampered", "foreign", "duplicate_alias", "wire_body"])
def test_catalog_and_wire_corruption_fail_closed(fault: str) -> None:
    catalog, wire = project_arbitration_identity_request(_final_request(500))
    bad_catalog = deepcopy(catalog)
    bad_wire = deepcopy(wire)
    if fault == "missing":
        bad_catalog = {}
    elif fault == "tampered":
        bad_catalog["entries"][0]["canonical_value"] += ":foreign"
    elif fault == "foreign":
        foreign_catalog, _ = project_arbitration_identity_request(_final_request(600))
        bad_catalog = foreign_catalog
    elif fault == "duplicate_alias":
        bad_catalog["entries"][1]["alias"] = bad_catalog["entries"][0]["alias"]
    else:
        bad_wire["evaluation_contract"] += " Changed after aliasing."
    with pytest.raises(ArbitrationIdentityAliasError):
        expand_arbitration_identity_request(bad_wire, bad_catalog)


@pytest.mark.parametrize("fault", ["unknown", "wrong_kind", "raw_echo"])
def test_response_rejects_unknown_wrong_kind_or_unaliased_echo(fault: str) -> None:
    ledger = scale_fixture._ledger(500)
    catalog, wire = project_arbitration_identity_request(
        project_arbitration_final_request(scale_fixture._request(ledger))
    )
    candidate = ledger["candidate_ids"][0]
    candidate_alias = _alias(catalog, "candidate_id", candidate)
    if fault == "unknown":
        echoed = catalog["prefix"] + "999999"
    elif fault == "wrong_kind":
        echoed = _alias(catalog, "section_id", ledger["candidate_section_index"][candidate][0]["section_id"])
    else:
        echoed = candidate
    with pytest.raises(ArbitrationIdentityAliasError):
        canonicalize_arbitration_identity_response(
            {"selected_candidate_id": echoed}, wire, catalog
        )
    assert candidate_alias != echoed or fault == "raw_echo"


def test_response_rejects_empty_required_identity_and_hash_echoes() -> None:
    ledger = scale_fixture._ledger(500)
    catalog, wire = project_arbitration_identity_request(
        project_arbitration_final_request(scale_fixture._request(ledger))
    )
    selected = _alias(catalog, "candidate_id", ledger["candidate_ids"][0])
    with pytest.raises(ArbitrationIdentityAliasError, match="nonempty"):
        canonicalize_arbitration_identity_response(
            {"selected_candidate_id": ""}, wire, catalog
        )
    with pytest.raises(ArbitrationIdentityAliasError, match="nonempty"):
        canonicalize_arbitration_identity_response(
            {"selected_candidate_id": selected, "section_hash": ""}, wire, catalog
        )


def test_unknown_fields_remain_literal_and_unsupported_packed_columns_fail() -> None:
    request = _final_request(500)
    request["opaque_unlisted_identity"] = "a" * 64
    catalog, wire = project_arbitration_identity_request(request)
    assert wire["opaque_unlisted_identity"] == "a" * 64
    assert expand_arbitration_identity_request(wire, catalog) == request

    unsupported = _final_request(500)
    unsupported["arbitration_evaluation_ledger"]["ledger_packing"]["section_index_row_columns"] = ["unknown"]
    with pytest.raises(ArbitrationIdentityAliasError, match="section columns"):
        project_arbitration_identity_request(unsupported)
