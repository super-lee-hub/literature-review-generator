"""Lossless final arbitration ledger wire-catalog regressions."""

from __future__ import annotations

import copy
import json

import pytest

from outline.candidate_arbitration_ledger_packing import (
    ArbitrationLedgerPackingError,
    expand_arbitration_final_ledger,
    pack_arbitration_final_ledger,
)
from outline.v3_models import compute_v3_hash


def _ledger():
    candidate_ids = [f"candidate_{index}" for index in range(1, 6)]
    candidate_hashes = {
        candidate_id: compute_v3_hash({"candidate": candidate_id})
        for candidate_id in candidate_ids
    }
    leaf_receipts = [
        {"leaf_id": f"leaf_{index}", "unit_ids": [f"unit_{index}"], "leaf_result_hash": compute_v3_hash({"leaf": index})}
        for index in range(1, 10)
    ]
    section_index = {candidate_id: [] for candidate_id in candidate_ids}
    for candidate_id in candidate_ids:
        for position in range(9):
            section_index[candidate_id].append({
                "candidate_id": candidate_id,
                "section_id": f"{candidate_id}_section_{position + 1}",
                "section_hash": compute_v3_hash({"candidate": candidate_id, "section": position}),
                "source_closure_hash": compute_v3_hash({"support": candidate_id, "section": position}),
                "title": f"Task {position + 1}: source-bounded comparison",
                "goal": "Compare the recorded claim and limitation without extending source scope.",
                "task_ids": [f"task:{position + 1}"],
                "paper_keys": [f"paper:{candidate_id}"],
                "relation_ids": [f"relation:{position + 1}"],
                "claim_count": 1,
                "support_row_count": 1,
                "position": position,
            })
    assessments = []
    for candidate_id in candidate_ids:
        for leaf_index, leaf in enumerate(leaf_receipts):
            section = section_index[candidate_id][leaf_index]
            assessments.append({
                "leaf_id": leaf["leaf_id"],
                "candidate_id": candidate_id,
                "candidate_hash": candidate_hashes[candidate_id],
                "presence": "present",
                "reviewed_section_ids": [section["section_id"]],
                "section_hashes": {section["section_id"]: section["section_hash"]},
                "coverage_assessment": f"Task {leaf_index + 1} covers the assigned source claims and retains their corpus boundary.",
                "evidence_assessment": f"The source closure supports task {leaf_index + 1} within its recorded sample limitation.",
                "structure_assessment": f"Task {leaf_index + 1} has a distinct role in the candidate's review progression.",
                "advantages": ["The claim retains the recorded source boundary."],
                "limitations": ["No inference beyond the cited source and sample."],
                "evidence_refs": [f"evidence:{candidate_id}"],
                "critic_issue_ids": ["issue:structure"] if candidate_id == "candidate_1" and leaf_index == 0 else [],
            })
    pairs = [
        (candidate_id, section["section_id"])
        for candidate_id in candidate_ids
        for section in section_index[candidate_id]
    ]
    ledger = {
        "schema_version": "outline_candidate_arbitration_ledger/v1",
        "plan_hash": "c" * 64,
        "input_hash": "d" * 64,
        "candidate_ids": candidate_ids,
        "candidate_hashes": candidate_hashes,
        "critique_hashes": {"structure_critique": "e" * 64},
        "provenance_hashes": {"review_intent": "f" * 64},
        "candidate_section_index": section_index,
        "critique_issues": [{
            "issue_id": "issue:structure",
            "scope": "section",
            "target_ids": ["candidate_1_section_1"],
            "evidence_refs": ["evidence:candidate_1"],
            "candidate_id": "candidate_1",
            "parent_candidate_hash": candidate_hashes["candidate_1"],
            "source_critic": "structure_critique",
            "message": "The section transition needs clarification.",
        }],
        "leaf_result_receipts": leaf_receipts,
        "assessments": assessments,
        "coverage": {
            "expected_candidate_section_count": len(pairs),
            "observed_candidate_section_count": len(pairs),
            "candidate_section_pairs_hash": compute_v3_hash(sorted(pairs)),
            "leaf_result_hashes": {row["leaf_id"]: row["leaf_result_hash"] for row in leaf_receipts},
        },
    }
    ledger["ledger_hash"] = compute_v3_hash(ledger)
    return ledger


def _rehash_wire(packed):
    packed.pop("wire_body_hash", None)
    packed["wire_body_hash"] = compute_v3_hash(packed)
    return packed


def _shared_closure_ledger():
    ledger = _ledger()
    for sections in ledger["candidate_section_index"].values():
        for section in sections:
            section["source_closure_hash"] = compute_v3_hash({"shared_source_closure": section["position"]})
    ledger["ledger_hash"] = compute_v3_hash({key: value for key, value in ledger.items() if key != "ledger_hash"})
    return ledger


def test_repeated_source_closure_hashes_use_explicit_catalog_refs_without_losing_rows():
    ledger = _shared_closure_ledger()
    packed = pack_arbitration_final_ledger(ledger)

    assert packed["ledger_packing"]["schema_version"] == "outline_candidate_arbitration_ledger_wire/v3"
    assert packed["ledger_packing"]["section_index_row_columns"][2] == "source_closure_hash_text_ref"
    assert packed["ledger_packing"]["section_source_closure_hash_encoding"]["schema_version"] == "outline_candidate_section_source_closure_refs/v1"
    assert all(type(row[2]) is int for rows in packed["candidate_section_index"].values() for row in rows)
    assert expand_arbitration_final_ledger(packed) == ledger
    assert sum(map(len, packed["candidate_section_index"].values())) == 45
    assert len(packed["assessments"]) == 45
    assert len(packed["leaf_result_receipt_rows"]) == 9


def test_legacy_v2_without_new_marker_still_expands_repeated_literal_hashes():
    ledger = _shared_closure_ledger()
    packed = pack_arbitration_final_ledger(ledger)
    catalog = packed["ledger_text_catalog"]
    for rows in packed["candidate_section_index"].values():
        for row in rows:
            row[2] = catalog[row[2]]
    legacy_catalog = pack_arbitration_final_ledger(_ledger())["ledger_text_catalog"]
    packed["ledger_text_catalog"] = legacy_catalog
    packing = packed["ledger_packing"]
    packing["schema_version"] = "outline_candidate_arbitration_ledger_wire/v2"
    packing.pop("section_source_closure_hash_encoding")
    packing["section_index_row_columns"][2] = "source_closure_hash"
    packing["text_catalog_sha256"] = compute_v3_hash(legacy_catalog)
    _rehash_wire(packed)

    assert expand_arbitration_final_ledger(packed) == ledger
    assert "section_source_closure_hash_encoding" not in packing


@pytest.mark.parametrize("fault", ["wire_version", "missing_encoding", "unknown_encoding", "extra_encoding_field", "columns", "v2_with_encoding"])
def test_source_hash_encoding_requires_exact_version_marker_and_columns(fault):
    packed = pack_arbitration_final_ledger(_shared_closure_ledger())
    packing = packed["ledger_packing"]
    if fault == "wire_version":
        packing["schema_version"] = "outline_candidate_arbitration_ledger_wire/v99"
    elif fault == "missing_encoding":
        packing.pop("section_source_closure_hash_encoding")
    elif fault == "unknown_encoding":
        packing["section_source_closure_hash_encoding"]["schema_version"] = "unknown/v1"
    elif fault == "extra_encoding_field":
        packing["section_source_closure_hash_encoding"]["trusted"] = True
    elif fault == "columns":
        packing["section_index_row_columns"][2] = "source_closure_hash"
    else:
        packing["schema_version"] = "outline_candidate_arbitration_ledger_wire/v2"
    _rehash_wire(packed)

    with pytest.raises(ArbitrationLedgerPackingError, match="schema|encoding|columns"):
        expand_arbitration_final_ledger(packed)


@pytest.mark.parametrize("reference", [-1, True, "0", 999999])
def test_source_hash_ref_rejects_invalid_catalog_indexes(reference):
    packed = pack_arbitration_final_ledger(_shared_closure_ledger())
    packed["candidate_section_index"]["candidate_1"][0][2] = reference
    _rehash_wire(packed)
    with pytest.raises(ArbitrationLedgerPackingError, match="outside its catalog"):
        expand_arbitration_final_ledger(packed)


def test_source_hash_ref_cannot_refer_to_scientific_prose_as_a_digest():
    packed = pack_arbitration_final_ledger(_shared_closure_ledger())
    prose_ref = packed["candidate_section_index"]["candidate_1"][0][3]
    packed["candidate_section_index"]["candidate_1"][0][2] = prose_ref
    _rehash_wire(packed)
    with pytest.raises(ArbitrationLedgerPackingError, match="not a SHA-256"):
        expand_arbitration_final_ledger(packed)


def test_rehashed_foreign_closure_hash_cannot_change_canonical_source_identity():
    packed = pack_arbitration_final_ledger(_shared_closure_ledger())
    reference = packed["candidate_section_index"]["candidate_1"][0][2]
    packed["ledger_text_catalog"][reference] = "f" * 64
    packed["ledger_packing"]["text_catalog_sha256"] = compute_v3_hash(packed["ledger_text_catalog"])
    _rehash_wire(packed)
    with pytest.raises(ArbitrationLedgerPackingError, match="canonical arbitration ledger hash"):
        expand_arbitration_final_ledger(packed)


def test_repeated_hash_catalog_reduces_actual_estimator_input_with_exact_roundtrip():
    from runtime.provider_context import ProviderContextProfile

    ledger = _shared_closure_ledger()
    packed = pack_arbitration_final_ledger(ledger)
    literal = copy.deepcopy(packed)
    catalog = literal["ledger_text_catalog"]
    for rows in literal["candidate_section_index"].values():
        for row in rows:
            row[2] = catalog[row[2]]
    legacy_catalog = pack_arbitration_final_ledger(_ledger())["ledger_text_catalog"]
    literal["ledger_text_catalog"] = legacy_catalog
    literal["ledger_packing"]["schema_version"] = "outline_candidate_arbitration_ledger_wire/v2"
    literal["ledger_packing"].pop("section_source_closure_hash_encoding")
    literal["ledger_packing"]["section_index_row_columns"][2] = "source_closure_hash"
    literal["ledger_packing"]["text_catalog_sha256"] = compute_v3_hash(legacy_catalog)
    _rehash_wire(literal)
    profile = ProviderContextProfile.conservative(provider="unit", model="unit", endpoint_type="chat_completions")

    assert profile.estimate_tokens(packed) < profile.estimate_tokens(literal)
    assert expand_arbitration_final_ledger(packed) == expand_arbitration_final_ledger(literal) == ledger


def test_pack_expand_roundtrip_preserves_full_semantic_and_source_ledger():
    original = _ledger()
    packed = pack_arbitration_final_ledger(original)
    expanded = expand_arbitration_final_ledger(packed)

    assert expanded == original
    assert packed["ledger_packing"]["schema_version"] == "outline_candidate_arbitration_ledger_wire/v2"
    assert packed["ledger_packing"]["text_catalog_sha256"] == compute_v3_hash(packed["ledger_text_catalog"])
    assert packed["ledger_packing"]["string_array_catalog_sha256"] == compute_v3_hash(
        packed["ledger_string_array_catalog"]
    )
    assert len(packed["assessments"][0]) == 11
    assert packed["ledger_packing"]["assessment_row_columns"][9] == "evidence_refs_array_ref"
    assert packed["ledger_packing"]["section_refs"].startswith("reviewed_section_refs indexes")
    original_bytes = len(json.dumps(original, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    packed_bytes = len(json.dumps(packed, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    assert packed_bytes < original_bytes


def test_pack_rejects_a_stale_canonical_ledger_hash():
    ledger = _ledger()
    ledger["assessments"][0]["coverage_assessment"] = "changed without refreshing the hash"
    with pytest.raises(ArbitrationLedgerPackingError, match="canonical arbitration ledger hash"):
        pack_arbitration_final_ledger(ledger)


def test_expand_rejects_changed_wire_body_or_catalog_digest():
    packed = pack_arbitration_final_ledger(_ledger())
    tampered = copy.deepcopy(packed)
    tampered["ledger_text_catalog"][0] += " altered"
    with pytest.raises(ArbitrationLedgerPackingError, match="wire hash"):
        expand_arbitration_final_ledger(tampered)

    tampered = copy.deepcopy(packed)
    tampered["ledger_string_array_catalog"][0].append("foreign-id")
    _rehash_wire(tampered)
    with pytest.raises(ArbitrationLedgerPackingError, match="string-array catalog digest"):
        expand_arbitration_final_ledger(tampered)


def test_expand_rejects_dangling_text_and_array_references_even_with_wire_rehash():
    packed = pack_arbitration_final_ledger(_ledger())
    tampered = copy.deepcopy(packed)
    tampered["assessments"][0][4] = 999
    _rehash_wire(tampered)
    with pytest.raises(ArbitrationLedgerPackingError, match="outside its catalog"):
        expand_arbitration_final_ledger(tampered)

    tampered = copy.deepcopy(packed)
    tampered["assessments"][0][9] = 999
    _rehash_wire(tampered)
    with pytest.raises(ArbitrationLedgerPackingError, match="outside its catalog"):
        expand_arbitration_final_ledger(tampered)


def test_expand_rejects_foreign_candidate_section_and_stale_hash_refs():
    packed = pack_arbitration_final_ledger(_ledger())
    tampered = copy.deepcopy(packed)
    tampered["assessments"][0][0] = 1
    _rehash_wire(tampered)
    with pytest.raises(ArbitrationLedgerPackingError, match="duplicate a candidate/leaf row|ledger hash"):
        expand_arbitration_final_ledger(tampered)

    tampered = copy.deepcopy(packed)
    tampered["candidate_hash_values"][0] = "0" * 64
    _rehash_wire(tampered)
    with pytest.raises(ArbitrationLedgerPackingError, match="candidate hash is stale|ledger hash"):
        expand_arbitration_final_ledger(tampered)


def test_packing_requires_every_candidate_section_and_leaf_coverage():
    ledger = _ledger()
    ledger["assessments"].pop()
    ledger["ledger_hash"] = compute_v3_hash({key: value for key, value in ledger.items() if key != "ledger_hash"})
    with pytest.raises(ArbitrationLedgerPackingError, match="every candidate for every leaf"):
        pack_arbitration_final_ledger(ledger)


@pytest.mark.parametrize(
    ("coverage_field", "replacement", "message"),
    [
        ("expected_candidate_section_count", 999, "aggregate section coverage counts"),
        ("observed_candidate_section_count", 999, "aggregate section coverage counts"),
        ("candidate_section_pairs_hash", "0" * 64, "aggregate section coverage hash"),
        ("leaf_result_hashes", {"leaf_1": "0" * 64}, "leaf result coverage hashes"),
    ],
)
def test_packing_rejects_rehashed_but_inconsistent_aggregate_coverage(coverage_field, replacement, message):
    ledger = _ledger()
    ledger["coverage"][coverage_field] = replacement
    ledger["ledger_hash"] = compute_v3_hash({key: value for key, value in ledger.items() if key != "ledger_hash"})
    with pytest.raises(ArbitrationLedgerPackingError, match=message):
        pack_arbitration_final_ledger(ledger)
