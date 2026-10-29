from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

import pytest

from outline.candidate_arbitration_ledger_packing import (
    expand_arbitration_final_ledger,
    pack_arbitration_final_ledger,
)
from outline.critique_coverage_wire import (
    WIRE_KEY,
    CritiqueCoverageWireError,
    expand_critique_coverage_certificates,
    pack_critique_coverage_certificates,
)
from outline.v3_models import compute_v3_hash
from runtime.provider_runtime import hash_json
from tests.test_run_first_arbitration_ledger_packing import _ledger


def _certificate(candidate_id: str, section_id: str, part_index: int) -> dict[str, Any]:
    paper_keys = [f"{candidate_id}-paper-a", f"{candidate_id}-paper-b"]
    relation_ids = [f"relation:{candidate_id}"]
    parent_source_section_hash = hash_json({
        "candidate_id": candidate_id,
        "section_id": section_id,
        "source": f"complete source section for {candidate_id}",
    })
    atom_body = {
        "schema_version": "critique-claim-support-atom-coverage/v1",
        "candidate_id": candidate_id,
        "section_id": section_id,
        "shard_index": part_index + 1,
        "parent_candidate_hash": hash_json({"candidate_id": candidate_id}),
        "parent_section_hash": hash_json({"section_id": section_id}),
        "parent_claim_count": 2,
        "parent_support_count": 2,
        "parent_paper_key_count": len(paper_keys),
        "parent_paper_keys_hash": hash_json(paper_keys),
        "parent_relation_ids_hash": hash_json(relation_ids),
        "local_to_parent_claim_indices": [part_index],
        "local_to_parent_support_indices": [part_index],
        "atomic_claim_groups": [[part_index]],
        "comparison_groups": {},
    }
    atom = {**atom_body, "content_hash": hash_json(atom_body)}
    wrapper_body = {
        "schema_version": "outline_critique_claim_support_coverage/v1",
        "parent_source_section_hash": parent_source_section_hash,
        "atom_coverage_index": atom,
    }
    return {**wrapper_body, "content_hash": hash_json(wrapper_body)}


def _request() -> dict[str, Any]:
    rows: dict[str, dict[str, Any]] = {}
    for candidate_index in range(5):
        candidate_id = f"candidate_{candidate_index + 1}"
        parent_section_id = f"source-parent-{candidate_index + 1}"
        for part_index in range(4):
            shard_index = part_index + 1
            if part_index < 2:
                section_id = parent_section_id
                certificate = _certificate(candidate_id, section_id, part_index)
            else:
                section_id = f"whole-{candidate_id}-{part_index}"
                certificate = None
            row: dict[str, Any] = {
                "candidate_id": candidate_id,
                "shard_index": shard_index,
                "reviewed_section_ids": [section_id],
                "parent_candidate_hash": hash_json({"candidate_id": candidate_id}),
                "passed": True,
                "blocking_diagnostics": [],
                "issues": [],
                "feedback": (
                    f"Keep the source boundary for {candidate_id} literal. "
                    "This sentence contains semantic critique feedback and must remain unchanged."
                ),
            }
            if certificate is not None:
                row["claim_support_coverage"] = certificate
            rows[f"{candidate_id}:shard:{shard_index}"] = row

    critiques = {
        "evidence_critique": {
            "passed": True,
            "blocking_diagnostics": [],
            "issues": [{
                "issue_id": "preserve-source-conditions",
                "scope": "candidate",
                "target_ids": ["candidate_1"],
                "severity": "non_blocking",
                "evidence_refs": ["evidence:source-condition"],
                "resolution_status": "deferred",
                "parent_candidate_hash": hash_json({"candidate_id": "candidate_1"}),
                "message": "Keep the source-level scope qualifier in the synthesis.",
            }],
            "feedback": "The complete critic feedback remains a literal source-bound payload.",
            "candidate_shard_results": rows,
        },
        "structure_critique": {
            "passed": True,
            "blocking_diagnostics": [],
            "issues": [],
            "feedback": "Retain the progression from findings to limitations.",
        },
        "coverage_critique": {
            "passed": True,
            "blocking_diagnostics": [],
            "issues": [],
            "feedback": "Keep the identified evidence gaps visible.",
        },
    }
    critic_hashes = {
        critic_id: hash_json(payload)
        for critic_id, payload in critiques.items()
    }
    ledger = _ledger()
    ledger["critique_hashes"] = critic_hashes
    ledger["ledger_hash"] = compute_v3_hash({
        key: value for key, value in ledger.items() if key != "ledger_hash"
    })
    return {
        "candidate_ids": [f"candidate_{index + 1}" for index in range(5)],
        "critiques": critiques,
        "arbitration_evaluation_ledger": pack_arbitration_final_ledger(ledger),
    }


def _rehash_wire(request: dict[str, Any]) -> None:
    wire = request[WIRE_KEY]
    wire["wire_hash"] = hash_json({key: value for key, value in wire.items() if key != "wire_hash"})


def _first_coverage_row(request: dict[str, Any]) -> dict[str, Any]:
    rows = request["critiques"]["evidence_critique"]["candidate_shard_results"]
    return next(row for row in rows.values() if "claim_support_coverage_ref" in row)


def test_twenty_part_wire_roundtrips_all_proofs_and_literal_feedback_with_savings() -> None:
    request = _request()
    original = deepcopy(request)
    original_bytes = len(json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))

    packed = pack_critique_coverage_certificates(request)

    assert request == original
    wire = packed[WIRE_KEY]
    assert wire["schema_version"] == "outline_critique_coverage_wire/v1"
    assert len(wire["parent_table"]) == 5
    assert len(wire["part_table"]) == 10
    assert wire["resolution_contract"]
    assert all(
        "claim_support_coverage" not in row
        and "claim_support_coverage_ref" in row
        for row in packed["critiques"]["evidence_critique"]["candidate_shard_results"].values()
        if "claim_support_coverage" in row or "claim_support_coverage_ref" in row
    )
    packed_bytes = len(json.dumps(packed, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    assert packed_bytes < original_bytes
    assert original_bytes - packed_bytes > 2_500

    restored = expand_critique_coverage_certificates(packed)
    assert restored == original
    assert restored["critiques"]["evidence_critique"]["feedback"] == original[
        "critiques"
    ]["evidence_critique"]["feedback"]
    assert restored["critiques"]["evidence_critique"]["issues"] == original[
        "critiques"
    ]["evidence_critique"]["issues"]
    assert restored["arbitration_evaluation_ledger"] == original["arbitration_evaluation_ledger"]


@pytest.mark.parametrize(
    "tamper",
    [
        "wire_hash",
        "columns",
        "boolean_reference",
        "missing_reference",
        "duplicate_reference",
        "tampered_parent",
        "unused_parent",
        "mixed_literal_and_reference",
        "changed_feedback",
        "changed_ledger_hash",
    ],
)
def test_malformed_or_stale_coverage_wire_fails_closed(tamper: str) -> None:
    packed = pack_critique_coverage_certificates(_request())
    wire = packed[WIRE_KEY]
    row_values = packed["critiques"]["evidence_critique"]["candidate_shard_results"]
    ref_rows = [row for row in row_values.values() if "claim_support_coverage_ref" in row]

    if tamper == "wire_hash":
        wire["wire_hash"] = "0" * 64
    elif tamper == "columns":
        wire["parent_columns"][0] = "foreign_candidate_id"
        _rehash_wire(packed)
    elif tamper == "boolean_reference":
        ref_rows[0]["claim_support_coverage_ref"] = True
    elif tamper == "missing_reference":
        ref_rows[0].pop("claim_support_coverage_ref")
    elif tamper == "duplicate_reference":
        ref_rows[1]["claim_support_coverage_ref"] = ref_rows[0]["claim_support_coverage_ref"]
    elif tamper == "tampered_parent":
        wire["parent_table"][0][2] = "f" * 64
        _rehash_wire(packed)
    elif tamper == "unused_parent":
        unused_parent = deepcopy(wire["parent_table"][0])
        unused_parent[0] = "candidate_unused"
        unused_parent[1] = "section_unused"
        for index in (2, 3, 4, 8, 9):
            unused_parent[index] = hash_json({"unused_parent_field": index})
        wire["parent_table"].append(unused_parent)
        _rehash_wire(packed)
    elif tamper == "mixed_literal_and_reference":
        ref_rows[0]["claim_support_coverage"] = _certificate("candidate_1", "source-parent-1", 0)
    elif tamper == "changed_feedback":
        packed["critiques"]["evidence_critique"]["feedback"] = "altered"
    elif tamper == "changed_ledger_hash":
        ledger = expand_arbitration_final_ledger(packed["arbitration_evaluation_ledger"])
        ledger["critique_hashes"]["evidence_critique"] = "f" * 64
        ledger["ledger_hash"] = compute_v3_hash({
            key: value for key, value in ledger.items() if key != "ledger_hash"
        })
        packed["arbitration_evaluation_ledger"] = pack_arbitration_final_ledger(ledger)

    with pytest.raises(CritiqueCoverageWireError):
        expand_critique_coverage_certificates(packed)


def test_packer_rejects_unknown_future_certificate_fields_and_no_proof_is_a_noop() -> None:
    empty_request = {"critiques": {"structure_critique": {"passed": True, "issues": []}}}
    assert pack_critique_coverage_certificates(empty_request) == empty_request

    invalid = _request()
    row = next(
        row
        for row in invalid["critiques"]["evidence_critique"]["candidate_shard_results"].values()
        if "claim_support_coverage" in row
    )
    row["claim_support_coverage"]["future_proof_field"] = "must not be dropped"
    with pytest.raises(CritiqueCoverageWireError, match="wrapper fields"):
        pack_critique_coverage_certificates(invalid)


def test_packed_final_ledger_must_bind_full_critique_hashes() -> None:
    invalid = _request()
    invalid["arbitration_evaluation_ledger"] = {
        "critique_hashes": invalid["arbitration_evaluation_ledger"]["critique_hashes"]
    }
    with pytest.raises(CritiqueCoverageWireError, match="packed canonical arbitration ledger"):
        pack_critique_coverage_certificates(invalid)
