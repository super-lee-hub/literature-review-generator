from __future__ import annotations

import copy
import hashlib
import json

import pytest

from outline.candidate_arbitration_packing import (
    expand_arbitration_candidate_support,
    pack_arbitration_candidate_support,
)
from runtime.provider_context import ProviderContextProfile


def _support_row(candidate_index: int, claim_index: int, row_index: int) -> dict:
    # Each candidate authors different prose, while the complete source
    # closure is the same. Metadata remains source-bound and explicit.
    paper_number = claim_index % 10
    claim = (
        f"Candidate {candidate_index} interprets outcome {claim_index} with a "
        f"distinct but source-bounded statement, retaining its qualifier and "
        f"time window; candidate-specific wording {candidate_index}."
    )
    return {
        "claim": claim,
        "claim_slot_id": f"candidate-claim-slot:v1:{claim_index:02d}{row_index:02d}" + ("a" * 24),
        "task_id": f"topic_synthesis:context:{claim_index:02d}{row_index:02d}",
        "paper_key": f"rfspaper{paper_number:04d}_unknown_author",
        "primary_claim_id": f"claim:{paper_number}:findings:{claim_index:02d}:" + ("c" * 16),
        "source_claim_ids": [
            f"claim:{paper_number}:findings:{claim_index:02d}:" + ("c" * 16),
            f"claim:{paper_number}:limitations:{claim_index:02d}:" + ("d" * 16),
            f"claim:{paper_number}:qualifiers:{claim_index:02d}:" + ("e" * 16),
        ],
        "evidence_ids": [
            f"evidence:{paper_number}:findings:{claim_index:02d}:" + ("a" * 16),
            f"evidence:{paper_number}:limitations:{claim_index:02d}:" + ("b" * 16),
        ],
        "source_field_ids": [
            f"source-field:{paper_number:04d}:{claim_index:02d}:" + ("f" * 64),
            f"source-field:{paper_number:04d}:{claim_index:02d}:" + ("1" * 64),
            f"source-field:{paper_number:04d}:{claim_index:02d}:" + ("2" * 64),
        ],
        "qualifier": f"Observed sample; follow-up {30 + claim_index} days; no broader inference.",
    }


def _request() -> dict:
    candidate_ids = [f"candidate_{index}" for index in range(1, 6)]
    candidate_contents = {}
    for candidate_index, candidate_id in enumerate(candidate_ids, 1):
        sections = []
        start = 0
        for section_number, claim_count in enumerate((3, 5, 3), 1):
            claims = [
                (
                    f"Candidate {candidate_index}, section {section_number}: "
                    f"source records report outcome {claim_index} in the stated "
                    f"sample; limitations and follow-up remain attached. "
                    + ("Careful scope language. " * 9)
                )
                for claim_index in range(start, start + claim_count)
            ]
            claim_support = []
            for local_index, claim in enumerate(claims):
                global_claim_index = start + local_index
                for row_index in range(2):
                    row = _support_row(candidate_index, global_claim_index, row_index)
                    row["claim"] = claim
                    claim_support.append(row)
            sections.append(
                {
                    "section_id": f"{candidate_id}-section-{section_number}",
                    "title": f"Candidate {candidate_index} section {section_number}",
                    "goal": "Organize assigned findings and retain their boundaries.",
                    "rationale": f"candidate {candidate_index} rationale {section_number}",
                    "paper_keys": ["rfspaper0000_unknown_author", "rfspaper0001_unknown_author"],
                    "relation_ids": [f"relation-{section_number}"],
                    "task_ids": [f"topic_synthesis:context:{section_number:02d}"],
                    "claims": claims,
                    "claim_support": claim_support,
                }
            )
            start += claim_count
        candidate_contents[candidate_id] = {
            "candidate_id": candidate_id,
            "organizing_logic": f"Candidate {candidate_index} distinct organizing logic.",
            "claims": [claim for section in sections for claim in section["claims"]],
            "planned_claims": [claim for section in sections for claim in section["claims"]],
            "sections": sections,
            "paper_assignments": [
                {"section_id": section["section_id"], "paper_keys": list(section["paper_keys"])}
                for section in sections
            ],
            "shard_plan": {
                "shard_count": 3,
                "paper_keys": [f"rfspaper{paper:04d}_unknown_author" for paper in range(10)],
                "relation_ids": [f"relation-{relation:02d}" for relation in range(26)],
                "source_view_hashes": [f"view-hash-{paper:02d}-" + ("a" * 32) for paper in range(10)],
                "group_assignment": {
                    f"candidate-group-{group:02d}-" + ("b" * 24): f"node-{candidate_index}-{group % 3}"
                    for group in range(17)
                },
                "deferred_optional_groups": [
                    {"group_id": f"optional-group-{group:02d}", "reason": "optional follow-up coverage"}
                    for group in range(25)
                ],
                "assignment_coverage_hash": f"coverage-hash-{candidate_index}-" + ("c" * 32),
                "status": "complete",
            },
        }

    # Keep these peer results explicit. Packing support must preserve every
    # critique identity and payload exactly as received.
    critiques = {
        f"{kind}_critique": {
            "node_id": f"{kind}_critique",
            "passed": True,
            "candidate_shard_results": {
                candidate_id: {
                    "candidate_id": candidate_id,
                    "coverage": {"assigned_groups": 3, "resolved_groups": 3},
                    "observed_claim_slots": [f"slot-{slot}" for slot in range(11)],
                }
                for candidate_id in candidate_ids
            },
            "recommendations": [f"Preserve {kind} boundary for all candidates."],
        }
        for kind in ("structure", "coverage", "evidence")
    }
    return {
        "candidate_ids": candidate_ids,
        "candidate_hashes": {candidate_id: f"sha256:{index:064x}" for index, candidate_id in enumerate(candidate_ids, 1)},
        "candidate_contents": candidate_contents,
        "critiques": critiques,
        "coverage_metrics": {key: {"score": index} for index, key in enumerate(critiques)},
        "evidence_metrics": {key: {"score": index + 1} for index, key in enumerate(critiques)},
        "structure_metrics": {key: {"score": index + 2} for index, key in enumerate(critiques)},
        "blocking_diagnostics": [],
        "review_intent": {"required_comparison": "compare reported outcomes without inheriting a conclusion"},
        "selection_rule": "coverage_then_evidence_then_structure",
        "section_coordination_contract": {
            "candidate_section_hashes": {
                candidate_id: {
                    section["section_id"]: f"section-hash-{candidate_id}-{section['section_id']}"
                    for section in candidate_contents[candidate_id]["sections"]
                }
                for candidate_id in candidate_ids
            }
        },
    }


def _estimate(value) -> int:
    profile = ProviderContextProfile(
        provider="test",
        model="test",
        endpoint_type="chat_completions",
        model_context_limit=128_000,
        verified_context_limit=100_000,
        input_budget=32_000,
        max_output_tokens=8_000,
        reasoning_reserve=1_000,
        safety_margin=1_000,
    )
    return profile.estimate_tokens(value)


def test_arbitration_support_catalog_roundtrips_all_five_candidates_and_peer_results():
    request = _request()
    original = copy.deepcopy(request)
    packed = pack_arbitration_candidate_support(request)

    assert request == original
    assert packed["candidate_ids"] == original["candidate_ids"]
    assert packed["candidate_hashes"] == original["candidate_hashes"]
    assert packed["critiques"] == original["critiques"]
    assert packed["coverage_metrics"] == original["coverage_metrics"]
    assert packed["evidence_metrics"] == original["evidence_metrics"]
    assert packed["structure_metrics"] == original["structure_metrics"]
    assert packed["section_coordination_contract"] == original["section_coordination_contract"]
    assert packed["candidate_support_packing"]["schema_version"] == "outline_candidate_support_catalog/v1"
    assert "original row order" in packed["candidate_support_packing"]["claim_support_refs"]
    assert set(packed["candidate_contents"]) == set(original["candidate_contents"])

    for candidate_id in original["candidate_ids"]:
        before = original["candidate_contents"][candidate_id]
        after = packed["candidate_contents"][candidate_id]
        assert after["candidate_id"] == before["candidate_id"]
        assert after["organizing_logic"] == before["organizing_logic"]
        assert "claims" not in after  # exactly derivable from section claims
        assert "planned_claims" not in after  # exactly derivable from section claims
        assert "paper_assignments" not in after  # exactly derivable from section scope
        assert [section["section_id"] for section in after["sections"]] == [
            section["section_id"] for section in before["sections"]
        ]
        assert [packed["candidate_text_catalog"][index]
                for section in after["sections"] for index in section["claim_text_refs"]] == before["claims"]
        assert "shard_plan" not in after
        receipt = after["shard_plan_receipt"]
        assert receipt["plan_hash"] == hashlib.sha256(
            json.dumps(before["shard_plan"], ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        assert receipt["shard_count"] == before["shard_plan"]["shard_count"]
        assert receipt["group_assignment_count"] == len(before["shard_plan"]["group_assignment"])
        assert receipt["group_assignment_hash"] == hashlib.sha256(
            json.dumps(
                before["shard_plan"]["group_assignment"],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        assert receipt["assignment_coverage_hash"] == before["shard_plan"]["assignment_coverage_hash"]
        assert receipt["deferred_optional_group_count"] == len(before["shard_plan"]["deferred_optional_groups"])
        assert receipt["status"] == before["shard_plan"]["status"]
        assert [len(section["claim_support"]) for section in before["sections"]] == [6, 10, 6]
        assert [len(section["claim_support_refs"]) for section in after["sections"]] == [6, 10, 6]
        assert all(
            type(claim_index) is int and type(catalog_index) is int
            for section in after["sections"]
            for claim_index, catalog_index in section["claim_support_refs"]
        )

    expanded = expand_arbitration_candidate_support(packed)
    assert set(expanded["candidate_contents"]) == set(original["candidate_contents"])
    assert expanded["candidate_hashes"] == original["candidate_hashes"]
    for candidate_id in original["candidate_ids"]:
        before = original["candidate_contents"][candidate_id]
        after = expanded["candidate_contents"][candidate_id]
        assert after["organizing_logic"] == before["organizing_logic"]
        assert [section["claims"] for section in after["sections"]] == [
            section["claims"] for section in before["sections"]
        ]
        assert [section["claim_support"] for section in after["sections"]] == [
            section["claim_support"] for section in before["sections"]
        ]
    # The source catalog is shared across all five candidates, yet includes all
    # 22 distinct claim-support rows and their exact source/qualifier closure.
    assert len(packed["candidate_support_catalog"]) == 22
    assert sum(
        len(section["claim_support_refs"])
        for candidate in packed["candidate_contents"].values()
        for section in candidate["sections"]
    ) == 110
    assert len(packed["candidate_source_id_catalog"]) == 33


def test_arbitration_support_catalog_keys_full_support_not_claim_slot_id_only():
    request = _request()
    candidate_two_row = request["candidate_contents"]["candidate_2"]["sections"][0]["claim_support"][0]
    different_closure = copy.deepcopy(candidate_two_row)
    different_closure["qualifier"] = "The reported follow-up differs; preserve this exact limit."
    request["candidate_contents"]["candidate_2"]["sections"][0]["claim_support"][0] = different_closure

    packed = pack_arbitration_candidate_support(request)
    assert len(packed["candidate_support_catalog"]) == 23
    restored = expand_arbitration_candidate_support(packed)
    assert restored["candidate_contents"]["candidate_2"]["sections"][0]["claim_support"][0] == different_closure


@pytest.mark.parametrize(
    "mutate",
    [
        lambda request: request["candidate_contents"]["candidate_1"]["sections"][0]["claim_support_refs"].__setitem__(0, [0, 999]),
        lambda request: request["candidate_contents"]["candidate_1"]["sections"][0]["claim_support_refs"].__setitem__(0, [-1, 0]),
        lambda request: request["candidate_contents"]["candidate_1"]["sections"][0]["claim_support_refs"].__setitem__(0, [999, 0]),
    ],
)
def test_arbitration_support_catalog_rejects_invalid_refs(mutate):
    packed = pack_arbitration_candidate_support(_request())
    mutate(packed)
    with pytest.raises(ValueError, match="invalid support reference"):
        expand_arbitration_candidate_support(packed)


def test_arbitration_support_catalog_rejects_duplicate_or_foreign_candidate_identity():
    request = _request()
    request["candidate_ids"].append("candidate_1")
    with pytest.raises(ValueError, match="must be unique"):
        pack_arbitration_candidate_support(request)

    request = _request()
    request["candidate_contents"]["candidate_foreign"] = copy.deepcopy(
        request["candidate_contents"]["candidate_1"]
    )
    with pytest.raises(ValueError, match="exactly match candidate IDs"):
        pack_arbitration_candidate_support(request)


def test_arbitration_support_catalog_preserves_nonduplicate_authored_claims():
    request = _request()
    request["candidate_contents"]["candidate_1"]["claims"][0] = "separate authored claim"
    packed = pack_arbitration_candidate_support(request)
    assert packed["candidate_contents"]["candidate_1"]["claims"] == [
        "separate authored claim",
        *request["candidate_contents"]["candidate_1"]["claims"][1:],
    ]


def test_arbitration_support_catalog_preserves_nonduplicate_planned_claims_and_assignments():
    request = _request()
    request["candidate_contents"]["candidate_1"]["planned_claims"][0] = "separately authored planned claim"
    request["candidate_contents"]["candidate_2"]["paper_assignments"][0]["paper_keys"] = [
        "rfspaper0009_unknown_author"
    ]
    packed = pack_arbitration_candidate_support(request)
    assert packed["candidate_contents"]["candidate_1"]["planned_claims"][0] == "separately authored planned claim"
    assert packed["candidate_contents"]["candidate_2"]["paper_assignments"] == request[
        "candidate_contents"
    ]["candidate_2"]["paper_assignments"]


def test_arbitration_support_catalog_rejects_unknown_source_id_array_reference():
    packed = pack_arbitration_candidate_support(_request())
    packed["candidate_support_catalog"][0]["source_field_ids_ref"] = 999
    with pytest.raises(ValueError, match="invalid source ID array reference"):
        expand_arbitration_candidate_support(packed)


def test_arbitration_support_catalog_rejects_unknown_packing_schema():
    packed = pack_arbitration_candidate_support(_request())
    packed["candidate_support_packing"] = {"schema_version": "future/unsupported"}
    with pytest.raises(ValueError, match="unsupported.*schema"):
        expand_arbitration_candidate_support(packed)


def test_arbitration_catalog_indices_are_stable_under_candidate_and_mapping_permutation():
    request = _request()
    packed = pack_arbitration_candidate_support(request)

    reordered = copy.deepcopy(request)
    reordered["candidate_ids"].reverse()
    reordered["candidate_contents"] = {
        candidate_id: reordered["candidate_contents"][candidate_id]
        for candidate_id in reversed(list(reordered["candidate_contents"]))
    }
    reordered["candidate_hashes"] = {
        candidate_id: reordered["candidate_hashes"][candidate_id]
        for candidate_id in reversed(list(reordered["candidate_hashes"]))
    }
    for candidate in reordered["candidate_contents"].values():
        for section in candidate["sections"]:
            section["claim_support"] = [dict(reversed(list(row.items()))) for row in section["claim_support"]]
    repacked = pack_arbitration_candidate_support(reordered)

    assert json.dumps(packed["candidate_support_catalog"], ensure_ascii=False, separators=(",", ":")) == json.dumps(
        repacked["candidate_support_catalog"], ensure_ascii=False, separators=(",", ":")
    )
    assert packed["candidate_source_id_catalog"] == repacked["candidate_source_id_catalog"]
    for candidate_id in request["candidate_ids"]:
        assert [section["claim_support_refs"] for section in packed["candidate_contents"][candidate_id]["sections"]] == [
            section["claim_support_refs"] for section in repacked["candidate_contents"][candidate_id]["sections"]
        ]


def test_arbitration_full_request_estimate_fits_32k_without_dropping_candidates_or_critiques():
    request = _request()
    packed = pack_arbitration_candidate_support(request)

    assert len(packed["candidate_ids"]) == 5
    assert len(packed["critiques"]) == 3
    assert set(packed["candidate_contents"]) == set(request["candidate_contents"])
    assert _estimate(request) > 32_000
    assert _estimate(packed) <= 32_000
    assert json.loads(json.dumps(packed, ensure_ascii=False)) == packed


def test_exact_repeated_claim_and_role_text_is_shared_without_merging_source_closures():
    request = _request()
    claim = "The zero effect is bounded by the observed sample and 30-day follow-up. " * 20
    role = "Compare the observed finding with the distinct source's reported finding."
    for candidate in request["candidate_contents"].values():
        for section in candidate["sections"]:
            section["claims"] = [claim]
            for row in section["claim_support"]:
                row["claim"] = claim
            section["paper_roles"] = {paper: role for paper in section["paper_keys"]}
        candidate["claims"] = [claim for _section in candidate["sections"]]
        candidate["planned_claims"] = list(candidate["claims"])
    divergent = request["candidate_contents"]["candidate_2"]["sections"][0]["claim_support"][0]
    divergent["qualifier"] = "Only the separately observed sample; no pooled effect."
    packed = pack_arbitration_candidate_support(request)
    assert packed["candidate_text_catalog"] == sorted([claim, role])
    assert len(packed["candidate_support_catalog"]) == 23
    assert _estimate(packed) < _estimate(request)
    restored = expand_arbitration_candidate_support(packed)
    for candidate_id in request["candidate_ids"]:
        assert restored["candidate_contents"][candidate_id]["sections"] == request["candidate_contents"][candidate_id]["sections"]
    assert restored["candidate_hashes"] == request["candidate_hashes"]


@pytest.mark.parametrize("index", [True, -1, 999])
def test_arbitration_text_catalog_rejects_invalid_reference(index):
    packed = pack_arbitration_candidate_support(_request())
    packed["candidate_contents"]["candidate_1"]["sections"][0]["claim_text_refs"][0] = index
    with pytest.raises(ValueError, match="invalid text reference"):
        expand_arbitration_candidate_support(packed)


def test_arbitration_text_catalog_rejects_changed_qualifier_text():
    packed = pack_arbitration_candidate_support(_request())
    packed["candidate_text_catalog"][0] += "Claimed long-term generalization."
    with pytest.raises(ValueError, match="text catalog identity"):
        expand_arbitration_candidate_support(packed)
