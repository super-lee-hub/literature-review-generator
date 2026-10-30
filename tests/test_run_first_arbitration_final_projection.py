from __future__ import annotations

import copy
import math

import pytest

from outline.candidate_arbitration_final_projection import (
    ArbitrationFinalProjectionError,
    expand_arbitration_final_projection,
    project_arbitration_final_request,
)
from outline.candidate_arbitration_ledger_packing import (
    ArbitrationLedgerPackingError,
    pack_arbitration_final_ledger,
)
from outline.v3_models import compute_v3_hash


def _ledger(fragment_count: int = 64) -> dict:
    candidate_ids = [f"candidate_{index}" for index in range(1, 6)]
    candidate_hashes = {
        candidate_id: compute_v3_hash({"candidate": candidate_id, "fragments": fragment_count})
        for candidate_id in candidate_ids
    }
    total_rows = math.ceil(fragment_count * 55 / 64)
    base, remainder = divmod(total_rows, len(candidate_ids))
    counts = [base + (1 if index < remainder else 0) for index in range(len(candidate_ids))]
    section_index = {candidate_id: [] for candidate_id in candidate_ids}
    groups = {}
    issues = []
    issue_by_section = {}
    leaf_width = 5
    retained_64_leaf_sizes = [2, 1, 1, 1, 1, 1, 1, 1, 2]

    for candidate_position, candidate_id in enumerate(candidate_ids):
        for position in range(counts[candidate_position]):
            fragment = position + 1
            section_id = f"fragment-{fragment_count}:{candidate_id}:{fragment:04d}"
            body = {
                "candidate_id": candidate_id,
                "section_id": section_id,
                "source_closure_hash": compute_v3_hash({
                    "fixture": fragment_count,
                    "candidate": candidate_id,
                    "fragment": fragment,
                    "qualifier": position % 7,
                }),
                "title": f"Fixture {fragment_count} {candidate_id} section {fragment:04d}",
                "goal": f"Assess distinct fixture fragment {fragment:04d} and qualifier {position % 7}.",
                "task_ids": [f"task:{position % 17:02d}"],
                "paper_keys": [f"paper:{fragment:04d}"],
                "relation_ids": [f"relation:{position % 31:02d}"],
                "claim_count": 1 + position % 3,
                "support_row_count": 2 + position % 4,
                "position": position,
            }
            section_index[candidate_id].append({**body, "section_hash": compute_v3_hash(body)})
        if fragment_count == 64:
            start = 0
            candidate_groups = []
            for group_size in retained_64_leaf_sizes:
                candidate_groups.append(section_index[candidate_id][start:start + group_size])
                start += group_size
            groups[candidate_id] = candidate_groups
        else:
            groups[candidate_id] = [
                section_index[candidate_id][start:start + leaf_width]
                for start in range(0, len(section_index[candidate_id]), leaf_width)
            ]

    for candidate_id in candidate_ids:
        for row in section_index[candidate_id]:
            position = int(row["position"])
            if fragment_count < 500 or position % 19:
                continue
            issue_id = f"issue:{fragment_count}:{candidate_id}:{position + 1:04d}"
            issue = {
                "issue_id": issue_id,
                "scope": "section",
                "target_ids": [row["section_id"]],
                "candidate_id": candidate_id,
                "parent_candidate_hash": candidate_hashes[candidate_id],
                "source_critic": "evidence_critique",
                "severity": "critical",
                "status": "unresolved",
                "evidence_refs": [f"evidence:{candidate_id}:{position + 1:04d}"],
                "message": f"Distinct critical issue note {issue_id} preserves qualifier closure.",
            }
            issues.append(issue)
            issue_by_section[(candidate_id, row["section_id"])] = issue_id

    leaf_count = max(len(value) for value in groups.values())
    leaf_receipts = []
    assessments = []
    for leaf_index in range(leaf_count):
        leaf_id = f"leaf:{fragment_count}:{leaf_index + 1:03d}"
        leaf_rows = []
        for candidate_id in candidate_ids:
            assigned = groups[candidate_id][leaf_index] if leaf_index < len(groups[candidate_id]) else []
            section_ids = [row["section_id"] for row in assigned]
            issue_ids = sorted(
                issue_by_section[(candidate_id, section_id)]
                for section_id in section_ids
                if (candidate_id, section_id) in issue_by_section
            )
            position = leaf_index + 1
            item = {
                "leaf_id": leaf_id,
                "candidate_id": candidate_id,
                "candidate_hash": candidate_hashes[candidate_id],
                "presence": "present" if assigned else "absent",
                "reviewed_section_ids": section_ids,
                "section_hashes": {row["section_id"]: row["section_hash"] for row in assigned},
                "coverage_assessment": f"Unique coverage note {fragment_count}/{candidate_id}/{position:03d}.",
                "evidence_assessment": f"Unique evidence note {fragment_count}/{candidate_id}/{position:03d}.",
                "structure_assessment": f"Unique structure note {fragment_count}/{candidate_id}/{position:03d}.",
                "advantages": [f"Unique advantage note {fragment_count}/{candidate_id}/{position:03d}."] if assigned else [],
                "limitations": [f"Unique limitation note {fragment_count}/{candidate_id}/{position:03d}."] if assigned else [],
                "evidence_refs": [f"evidence:{candidate_id}:{row['position'] + 1:04d}" for row in assigned],
                "critic_issue_ids": issue_ids,
            }
            if fragment_count == 64:
                repeated_notes = [f"Retained 64 semantic note {index}" for index in range(4)]
                note_position = len(assessments) * 3
                item["coverage_assessment"] = repeated_notes[note_position % 4]
                item["evidence_assessment"] = repeated_notes[(note_position + 1) % 4]
                item["structure_assessment"] = repeated_notes[(note_position + 2) % 4]
            assessments.append(item)
            leaf_rows.append(item)
        leaf_receipts.append({
            "leaf_id": leaf_id,
            "unit_ids": [f"unit:{fragment_count}:{leaf_index + 1:03d}"],
            "leaf_result_hash": compute_v3_hash(leaf_rows),
        })

    pairs = sorted(
        (candidate_id, row["section_id"])
        for candidate_id, rows in section_index.items()
        for row in rows
    )
    ledger = {
        "schema_version": "outline_candidate_arbitration_ledger/v1",
        "plan_hash": compute_v3_hash({"plan": fragment_count, "pairs": pairs}),
        "input_hash": compute_v3_hash({"input": fragment_count, "pairs": pairs}),
        "candidate_ids": candidate_ids,
        "candidate_hashes": candidate_hashes,
        "critique_hashes": {"evidence_critique": compute_v3_hash({"critic": fragment_count})},
        "provenance_hashes": {"request": compute_v3_hash({"source": fragment_count})},
        "candidate_section_index": section_index,
        "critique_issues": issues,
        "leaf_result_receipts": leaf_receipts,
        "assessments": assessments,
        "coverage": {
            "expected_candidate_section_count": len(pairs),
            "observed_candidate_section_count": len(pairs),
            "candidate_section_pairs_hash": compute_v3_hash(pairs),
            "leaf_result_hashes": {
                row["leaf_id"]: row["leaf_result_hash"] for row in leaf_receipts
            },
        },
    }
    ledger["ledger_hash"] = compute_v3_hash(ledger)
    return ledger


def _request(ledger: dict) -> dict:
    section_hashes = {
        candidate_id: {
            row["section_id"]: row["section_hash"]
            for row in ledger["candidate_section_index"][candidate_id]
        }
        for candidate_id in ledger["candidate_ids"]
    }
    recommendations = [
        {
            "recommendation_id": f"recommendation:{index}",
            "note": f"Unique recommendation note for issue {issue['issue_id']}.",
            "issue_id": issue["issue_id"],
        }
        for index, issue in enumerate(ledger["critique_issues"], start=1)
    ]
    return {
        "candidate_ids": copy.deepcopy(ledger["candidate_ids"]),
        "candidate_hashes": copy.deepcopy(ledger["candidate_hashes"]),
        "critiques": {"recommendations": recommendations},
        "critique_disposition": {
            "eligible_candidate_ids": copy.deepcopy(ledger["candidate_ids"]),
            "issues": copy.deepcopy(ledger["critique_issues"]),
        },
        "section_coordination_contract": {
            "candidate_section_hashes": section_hashes,
            "output_fields": "Preserve every source section hash during coordination.",
        },
        "candidate_contents": {
            candidate_id: {
                "candidate_id": candidate_id,
                "organizing_logic": "Compare complete source-bound sections.",
                "section_index_ref": {"ledger_field": "candidate_section_index", "candidate_id": candidate_id},
            }
            for candidate_id in ledger["candidate_ids"]
        },
        "arbitration_evaluation_ledger": pack_arbitration_final_ledger(ledger),
        "hierarchy": {
            "level": "all_candidate_final_arbitration",
            "ledger_hash": ledger["ledger_hash"],
            "coverage": copy.deepcopy(ledger["coverage"]),
        },
        "evaluation_contract": "Select globally among all candidates using every assessment and issue.",
    }


def _rehash_wire(packed: dict) -> dict:
    packed.pop("wire_body_hash", None)
    packed["wire_body_hash"] = compute_v3_hash(packed)
    return packed


def test_exact_aliases_expand_to_the_original_request_and_leave_input_unchanged():
    canonical = _ledger()
    original = _request(canonical)
    before = copy.deepcopy(original)

    projected = project_arbitration_final_request(original)

    assert original == before
    assert "candidate_hashes" not in projected
    assert "candidate_hashes_ref" in projected
    assert "issues" not in projected["critique_disposition"]
    assert "issues_ref" in projected["critique_disposition"]
    assert "candidate_section_hashes" not in projected["section_coordination_contract"]
    assert "candidate_section_hashes_ref" in projected["section_coordination_contract"]
    assert projected["arbitration_final_projection"]["schema_version"] == (
        "outline_candidate_arbitration_final_projection/v1"
    )
    assert expand_arbitration_final_projection(projected) == original
    assert project_arbitration_final_request(projected) == projected




def test_retained_64_shape_has_nine_leaf_rows_and_roundtrips():
    canonical = _ledger(64)
    original = _request(canonical)
    note_values = [
        row[field]
        for row in canonical["assessments"]
        for field in ("coverage_assessment", "evidence_assessment", "structure_assessment")
    ]

    assert sum(map(len, canonical["candidate_section_index"].values())) == 55
    assert len(canonical["assessments"]) == 45
    assert len(canonical["leaf_result_receipts"]) == 9
    assert len(set(note_values)) == 4
    assert canonical["critique_issues"] == []
    assert expand_arbitration_final_projection(project_arbitration_final_request(original)) == original


@pytest.mark.parametrize(("fragment_count", "expected_rows", "expected_assessments"), [(500, 430, 90), (600, 516, 105)])
def test_distinct_note_scale_fixtures_preserve_every_candidate_assessment_issue_and_recommendation(
    fragment_count: int,
    expected_rows: int,
    expected_assessments: int,
):
    canonical = _ledger(fragment_count)
    original = _request(canonical)
    note_values = [
        row[field]
        for row in canonical["assessments"]
        for field in ("coverage_assessment", "evidence_assessment", "structure_assessment")
    ]

    assert sum(map(len, canonical["candidate_section_index"].values())) == expected_rows
    assert len(canonical["assessments"]) == expected_assessments
    assert len(set(note_values)) == len(note_values)

    projected = project_arbitration_final_request(original)
    round_trip = expand_arbitration_final_projection(projected)

    assert round_trip == original
    assert round_trip["candidate_ids"] == canonical["candidate_ids"]
    assert round_trip["candidate_hashes"] == canonical["candidate_hashes"]
    assert round_trip["hierarchy"]["coverage"] == canonical["coverage"]
    assert round_trip["critique_disposition"]["issues"] == canonical["critique_issues"]
    assert round_trip["section_coordination_contract"]["candidate_section_hashes"] == {
        candidate_id: {
            row["section_id"]: row["section_hash"]
            for row in canonical["candidate_section_index"][candidate_id]
        }
        for candidate_id in canonical["candidate_ids"]
    }
    assert len(round_trip["critiques"]["recommendations"]) == len(canonical["critique_issues"])


def test_unproved_literal_fields_fall_back_independently():
    canonical = _ledger()
    original = _request(canonical)
    original["candidate_hashes"]["candidate_1"] = "foreign-hash"
    original["critique_disposition"]["issues"].append({"issue_id": "foreign"})
    original["section_coordination_contract"]["candidate_section_hashes"]["candidate_1"]["foreign"] = "foreign-hash"

    projected = project_arbitration_final_request(original)

    assert projected["candidate_hashes"] == original["candidate_hashes"]
    assert "candidate_hashes_ref" not in projected
    assert projected["critique_disposition"]["issues"] == original["critique_disposition"]["issues"]
    assert "issues_ref" not in projected["critique_disposition"]
    assert projected["section_coordination_contract"]["candidate_section_hashes"] == (
        original["section_coordination_contract"]["candidate_section_hashes"]
    )
    assert "candidate_section_hashes_ref" not in projected["section_coordination_contract"]
    assert _rehash_wire(copy.deepcopy(projected["arbitration_evaluation_ledger"])) == (
        projected["arbitration_evaluation_ledger"]
    )


def test_missing_literal_field_falls_back_without_inventing_reference():
    canonical = _ledger()
    original = _request(canonical)
    original["section_coordination_contract"].pop("candidate_section_hashes")

    projected = project_arbitration_final_request(original)

    assert "candidate_section_hashes" not in projected["section_coordination_contract"]
    assert "candidate_section_hashes_ref" not in projected["section_coordination_contract"]
    assert expand_arbitration_final_projection(projected) == original


def test_stale_hierarchy_binding_falls_back_to_all_literal_fields():
    canonical = _ledger()
    original = _request(canonical)
    original["hierarchy"]["ledger_hash"] = "0" * 64

    projected = project_arbitration_final_request(original)

    assert projected == original
    assert "arbitration_final_projection" not in projected


@pytest.mark.parametrize(
    ("tamper", "match"),
    [
        ("foreign_path", "candidate hash reference is foreign or stale"),
        ("missing_reference", "candidate hash reference is foreign or stale"),
        ("stale_section_digest", "section hash reference is foreign or stale"),
        ("foreign_section_path", "section hash reference is foreign or stale"),
    ],
)
def test_expansion_rejects_foreign_missing_or_stale_aliases(tamper: str, match: str):
    projected = project_arbitration_final_request(_request(_ledger()))
    if tamper == "foreign_path":
        projected["candidate_hashes_ref"]["ledger_field"] = "foreign.candidate_hash_values"
    elif tamper == "missing_reference":
        projected.pop("candidate_hashes_ref")
    elif tamper == "stale_section_digest":
        projected["section_coordination_contract"]["candidate_section_hashes_ref"][
            "expected_candidate_section_pairs_sha256"
        ] = "0" * 64
    elif tamper == "foreign_section_path":
        projected["section_coordination_contract"]["candidate_section_hashes_ref"][
            "ledger_field"
        ] = "foreign.candidate_section_index"
    with pytest.raises(ArbitrationFinalProjectionError, match=match):
        expand_arbitration_final_projection(projected)


def test_expansion_rejects_projection_digest_and_ledger_wire_tampering():
    projected = project_arbitration_final_request(_request(_ledger()))
    projected["critique_disposition"]["issues_ref"]["expected_sha256"] = "0" * 64
    with pytest.raises(ArbitrationFinalProjectionError, match="issue reference is foreign or stale"):
        expand_arbitration_final_projection(projected)

    projected = project_arbitration_final_request(_request(_ledger()))
    wire = projected["arbitration_evaluation_ledger"]
    wire["ledger_text_catalog"][0] += " foreign"
    with pytest.raises(ArbitrationLedgerPackingError, match="wire hash"):
        expand_arbitration_final_projection(projected)

    projected = project_arbitration_final_request(_request(_ledger()))
    wire = projected["arbitration_evaluation_ledger"]
    wire["ledger_text_catalog"][0] += " stale digest"
    _rehash_wire(wire)
    with pytest.raises(ArbitrationLedgerPackingError, match="text catalog digest"):
        expand_arbitration_final_projection(projected)
