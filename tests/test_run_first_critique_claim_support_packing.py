from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from outline import critique_claim_support_packing as claim_support_packing
from outline.critique_claim_support_packing import (
    CRITIQUE_CLAIM_SUPPORT_ATOM_CONTRACT_V1,
    CritiqueClaimSupportPackingError,
    CritiqueComparisonClaimGroupV1,
    pack_critique_claim_support_shards_v1,
)
from outline.critique_paper_role_projection import (
    PAPER_ROLE_CLAIM_ARRAY_FIELD_V1,
    expand_critique_paper_roles_v1,
)
from outline.v3_critique import derive_critique_disposition
from runtime.provider_context import ProviderContextProfile
from tests.test_run_first_critique_packing import _critique_executor


def _parent_pair(
    claims: list[str],
    support_rows: list[dict[str, Any]],
    paper_keys: list[str],
    *,
    relation_ids: list[str] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    candidate_id = "candidate_1"
    section_id = "section:candidate_1:comparison"
    roles: dict[str, int] = {}
    for row in support_rows:
        roles.setdefault(str(row["paper_key"]), int(row["claim_index"]))
    role_ref = {
        "candidate_id": candidate_id,
        "section_id": section_id,
        "field": PAPER_ROLE_CLAIM_ARRAY_FIELD_V1,
    }
    candidate_section = {
        "section_id": section_id,
        "title": "Source comparison",
        "goal": "Review the complete selected source statements.",
        "paper_keys": list(paper_keys),
        "relation_ids": list(relation_ids or []),
        "claims": list(claims),
        "claim_count": len(claims),
        "paper_roles": roles,
        "paper_role_claim_array_ref": role_ref if roles else None,
    }
    if not roles:
        candidate_section.pop("paper_role_claim_array_ref")
    evidence_section = {
        "candidate_id": candidate_id,
        "section_id": section_id,
        "paper_keys": list(paper_keys),
        "relation_ids": list(relation_ids or []),
        "claim_array_ref": {
            "candidate_id": candidate_id,
            "section_id": section_id,
            "field": PAPER_ROLE_CLAIM_ARRAY_FIELD_V1,
        },
        "claim_support": deepcopy(support_rows),
        "paper_roles": dict(roles),
    }
    if roles:
        evidence_section["paper_role_claim_array_ref"] = dict(role_ref)
    return candidate_section, evidence_section


def _support(claim_index: int, slot: str, paper_key: str) -> dict[str, Any]:
    return {
        "claim_index": claim_index,
        "claim_slot_id": f"slot:{slot}",
        "paper_key": paper_key,
        "primary_claim_id": f"claim:{slot}",
        "source_claim_ids": [f"claim:{slot}"],
        "evidence_ids": [f"evidence:{slot}"],
        "source_field_ids": [f"field:{slot}"],
        "study_id": "",
        "relation_ids": [],
    }


def _measure_by_atom_size(projection: dict[str, Any]) -> int:
    return (
        len(projection["candidate_section"]["claims"]) * 100
        + len(projection["section_evidence"]["claim_support"]) * 50
    )


def _executor_evidence_request(
    claims: list[str],
    papers: list[str],
    *,
    relation_ids: list[str] | None = None,
) -> dict[str, Any]:
    section_id = "section:candidate_1:source-findings"
    relation_values = list(relation_ids or [])
    support_rows = [
        {
            "claim": claim,
            "claim_slot_id": f"slot:{index}",
            "task_id": f"source-task:{index}",
            "paper_key": papers[index],
            "primary_claim_id": f"claim:{index}",
            "source_claim_ids": [f"claim:{index}", f"qualifier:{index}"],
            "evidence_ids": [f"evidence:{index}:0", f"evidence:{index}:1"],
            "source_field_ids": [f"field:{index}"],
            "relation_ids": relation_values,
            "study_id": "",
        }
        for index, claim in enumerate(claims)
    ]
    section = {
        "section_id": section_id,
        "title": "Source findings",
        "goal": "Preserve every supported source finding and its complete conditions.",
        "paper_keys": list(papers),
        "relation_ids": relation_values,
        "claims": list(claims),
        "claim_count": len(claims),
        "paper_roles": {paper: claim for paper, claim in zip(papers, claims, strict=True)},
        "claim_support": support_rows,
    }
    candidate = {
        "candidate_id": "candidate_1",
        "organizing_logic": "source-grounded findings",
        "sections": [section],
        "planned_claims": list(claims),
    }
    return {
        "node_id": "evidence_critique",
        "candidate_contents": {"candidate_1": candidate},
        "candidate_hashes": {"candidate_1": "candidate-generation-hash"},
        "paper_key_aliases": {},
        "paper_reuse_policy": "Repeated use requires distinct source support.",
        "candidate_claims": {"candidate_1": list(claims)},
        "section_evidence": {"candidate_1": [section]},
        "paper_keys": list(papers),
        "source_summary_hashes": [f"summary:{index}" for index in range(len(papers))],
        "relation_evidence": [],
        "contradictions": [],
        "boundaries": [],
        "gaps": [],
    }


def test_packer_keeps_each_claim_and_all_support_rows_together_and_covers_parent() -> None:
    claims = ["Finding A.", "Finding B.", "Finding C."]
    supports = [
        _support(0, "a-left", "paper-a"),
        _support(0, "a-right", "paper-b"),
        _support(1, "b", "paper-b"),
        _support(2, "c", "paper-c"),
    ]
    candidate, evidence = _parent_pair(claims, supports, ["paper-a", "paper-b", "paper-c"])
    source_candidate = deepcopy(candidate)
    source_evidence = deepcopy(evidence)

    shards = pack_critique_claim_support_shards_v1(
        "candidate_1",
        "candidate-generation-hash",
        candidate,
        evidence,
        input_cap_tokens=300,
        measure_request=_measure_by_atom_size,
    )

    assert len(shards) == 2
    assert [shard["candidate_section"]["claims"] for shard in shards] == [
        ["Finding A."],
        ["Finding B.", "Finding C."],
    ]
    assert [
        row["claim_slot_id"]
        for shard in shards
        for row in shard["section_evidence"]["claim_support"]
    ] == ["slot:a-left", "slot:a-right", "slot:b", "slot:c"]
    assert [
        index
        for shard in shards
        for index in shard["atom_coverage_index"]["local_to_parent_claim_indices"]
    ] == [0, 1, 2]
    assert [
        index
        for shard in shards
        for index in shard["atom_coverage_index"]["local_to_parent_support_indices"]
    ] == [0, 1, 2, 3]
    assert all(shard["parent_section_hash"] == shards[0]["parent_section_hash"] for shard in shards)
    assert [shard["candidate_section"]["paper_keys"] for shard in shards] == [
        ["paper-a", "paper-b"],
        ["paper-b", "paper-c"],
    ]
    assert all(shard["atom_coverage_index"]["parent_paper_key_count"] == 3 for shard in shards)
    assert candidate == source_candidate
    assert evidence == source_evidence


def test_explicit_cross_paper_comparison_group_is_indivisible_and_keeps_both_papers() -> None:
    candidate, evidence = _parent_pair(
        ["The left-side report.", "The right-side report.", "A separate finding."],
        [
            _support(0, "left", "paper-a"),
            _support(1, "right", "paper-b"),
            _support(2, "other", "paper-c"),
        ],
        ["paper-a", "paper-b", "paper-c"],
        relation_ids=["relation:ab"],
    )
    group = CritiqueComparisonClaimGroupV1(
        group_id="relation:ab",
        claim_indices=(0, 1),
        required_paper_keys=("paper-a", "paper-b"),
        relation_ids=("relation:ab",),
    )

    shards = pack_critique_claim_support_shards_v1(
        "candidate_1",
        "candidate-generation-hash",
        candidate,
        evidence,
        input_cap_tokens=400,
        measure_request=_measure_by_atom_size,
        comparison_groups=(group,),
    )

    comparison_shard = next(
        shard for shard in shards
        if "relation:ab"
        in shard["atom_coverage_index"]["comparison_groups"]
    )
    assert comparison_shard["candidate_section"]["claims"] == [
        "The left-side report.",
        "The right-side report.",
    ]
    assert {
        row["paper_key"] for row in comparison_shard["section_evidence"]["claim_support"]
    } == {"paper-a", "paper-b"}
    assert comparison_shard["candidate_section"]["paper_keys"] == ["paper-a", "paper-b"]
    assert comparison_shard["atom_coverage_index"]["comparison_groups"]["relation:ab"][
        "required_paper_keys"
    ] == ["paper-a", "paper-b"]


def test_oversized_comparison_atom_fails_instead_of_splitting_its_paper_support() -> None:
    candidate, evidence = _parent_pair(
        ["A." * 20, "B." * 20],
        [_support(0, "left", "paper-a"), _support(1, "right", "paper-b")],
        ["paper-a", "paper-b"],
        relation_ids=["relation:ab"],
    )
    group = CritiqueComparisonClaimGroupV1(
        group_id="relation:ab",
        claim_indices=(0, 1),
        required_paper_keys=("paper-a", "paper-b"),
        relation_ids=("relation:ab",),
    )

    with pytest.raises(CritiqueClaimSupportPackingError, match="complete claim/support atom"):
        pack_critique_claim_support_shards_v1(
            "candidate_1",
            "candidate-generation-hash",
            candidate,
            evidence,
            input_cap_tokens=1,
            measure_request=lambda _projection: 2,
            comparison_groups=(group,),
        )


def test_duplicate_claim_text_remains_two_distinct_parent_occurrences() -> None:
    candidate, evidence = _parent_pair(
        ["Repeated exact text.", "Repeated exact text."],
        [_support(0, "first", "paper-a"), _support(1, "second", "paper-b")],
        ["paper-a", "paper-b"],
    )

    shards = pack_critique_claim_support_shards_v1(
        "candidate_1",
        "candidate-generation-hash",
        candidate,
        evidence,
        input_cap_tokens=150,
        measure_request=_measure_by_atom_size,
    )

    assert len(shards) == 2
    assert [s["candidate_section"]["claims"] for s in shards] == [
        ["Repeated exact text."],
        ["Repeated exact text."],
    ]
    assert [s["atom_coverage_index"]["local_to_parent_claim_indices"] for s in shards] == [
        [0],
        [1],
    ]
    assert [s["atom_coverage_index"]["local_to_parent_support_indices"] for s in shards] == [
        [0],
        [1],
    ]


def test_literal_paper_roles_follow_only_their_supported_paper_and_remain_exact() -> None:
    candidate, evidence = _parent_pair(
        ["Finding A.", "Finding B."],
        [_support(0, "a", "paper-a"), _support(1, "b", "paper-b")],
        ["paper-a", "paper-b"],
    )
    candidate["paper_roles"] = {
        "paper-a": "Exact unmatched role label for A.",
        "paper-b": "Exact unmatched role label for B.",
    }
    evidence["paper_roles"] = dict(candidate["paper_roles"])
    candidate.pop("paper_role_claim_array_ref", None)
    evidence.pop("paper_role_claim_array_ref", None)

    shards = pack_critique_claim_support_shards_v1(
        "candidate_1",
        "candidate-generation-hash",
        candidate,
        evidence,
        input_cap_tokens=150,
        measure_request=_measure_by_atom_size,
    )

    assert len(shards) == 2
    assert [shard["candidate_section"]["paper_keys"] for shard in shards] == [
        ["paper-a"],
        ["paper-b"],
    ]
    assert [shard["candidate_section"]["paper_roles"] for shard in shards] == [
        {"paper-a": "Exact unmatched role label for A."},
        {"paper-b": "Exact unmatched role label for B."},
    ]
    observed_roles = {
        paper_key: value
        for shard in shards
        for paper_key, value in expand_critique_paper_roles_v1(
            "candidate_1", shard["candidate_section"]
        )["paper_roles"].items()
    }
    assert observed_roles == candidate["paper_roles"]


def test_literal_paper_role_without_a_support_atom_fails_closed() -> None:
    candidate, evidence = _parent_pair(
        ["Finding A.", "Finding B."],
        [_support(0, "a", "paper-a"), _support(1, "b", "paper-a")],
        ["paper-a", "paper-b"],
    )
    candidate["paper_roles"] = {
        "paper-a": "Supported literal role.",
        "paper-b": "Unassignable literal role.",
    }
    evidence["paper_roles"] = dict(candidate["paper_roles"])
    candidate.pop("paper_role_claim_array_ref", None)
    evidence.pop("paper_role_claim_array_ref", None)

    with pytest.raises(CritiqueClaimSupportPackingError, match="cannot be assigned"):
        pack_critique_claim_support_shards_v1(
            "candidate_1",
            "candidate-generation-hash",
            candidate,
            evidence,
            input_cap_tokens=1000,
            measure_request=_measure_by_atom_size,
        )


def test_executor_uses_claim_support_packer_for_oversized_source_section(monkeypatch) -> None:
    claims = [
        f"Finding {index}: "
        + (f"complete source qualifier and boundary {index} remains attached; " * 45)
        for index in range(52)
    ]
    papers = [f"paper-{index}" for index in range(52)]
    request = _executor_evidence_request(claims, papers)
    calls: list[tuple[str, dict[str, Any]]] = []
    executor, profile = _critique_executor(calls)
    original_packer = claim_support_packing.pack_critique_claim_support_shards_v1
    packer_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def recording_packer(*args, **kwargs):
        packer_calls.append((args, dict(kwargs)))
        return original_packer(*args, **kwargs)

    monkeypatch.setattr(
        claim_support_packing,
        "pack_critique_claim_support_shards_v1",
        recording_packer,
    )
    merged = executor._run_hierarchical_critique(
        node_id="evidence_critique",
        request=request,
        dependency_hashes={"candidate": "candidate-generation-hash"},
    )

    assert len(packer_calls) == 1
    assert len(calls) > 1
    observed_claim_indices: list[int] = []
    observed_support_indices: list[int] = []
    for _node_id, local_request in calls:
        assert profile.estimate_request(local_request)["estimated_input_tokens"] <= 32_000
        local_candidate = local_request["candidate_contents"]["candidate_1"]
        local_section = local_candidate["sections"][0]
        atom = local_request["atom_coverage_index"]
        claim_indices = atom["local_to_parent_claim_indices"]
        observed_claim_indices.extend(claim_indices)
        observed_support_indices.extend(atom["local_to_parent_support_indices"])
        assert local_section["claims"] == [claims[index] for index in claim_indices]
        assert [
            row["claim_slot_id"] for row in local_request["section_evidence"]["candidate_1"][0]["claim_support"]
        ] == [f"slot:{index}" for index in atom["local_to_parent_support_indices"]]
    assert sorted(observed_claim_indices) == list(range(52))
    assert sorted(observed_support_indices) == list(range(52))
    disposition = derive_critique_disposition(
        {"evidence_critique": merged},
        candidate_hashes=request["candidate_hashes"],
        candidate_contents=request["candidate_contents"],
        trusted_shard_critic_ids=("evidence_critique",),
    )
    assert disposition["global_blocker"] is False
    assert disposition["eligible_candidate_ids"] == ["candidate_1"]
    assert disposition["issues"] == []

    missing_proof = deepcopy(merged)
    first_shard = next(iter(missing_proof["candidate_shard_results"].values()))
    first_shard.pop("claim_support_coverage")
    rejected = derive_critique_disposition(
        {"evidence_critique": missing_proof},
        candidate_hashes=request["candidate_hashes"],
        candidate_contents=request["candidate_contents"],
        trusted_shard_critic_ids=("evidence_critique",),
    )
    assert rejected["global_blocker"] is True
    assert rejected["eligible_candidate_ids"] == []


def test_executor_partial_coverage_uses_parent_local_indices_after_whole_section() -> None:
    claims = [f"Finding {index}: " + ("complete source qualifier remains attached; " * 60)
              for index in range(52)]
    request = _executor_evidence_request(claims, [f"paper-{index}" for index in range(52)])
    prelude = deepcopy(_executor_evidence_request(["Prelude finding."], ["paper-prelude"])
                       ["candidate_contents"]["candidate_1"]["sections"][0])
    prelude["section_id"] = "section:prelude"
    request["candidate_contents"]["candidate_1"]["sections"].insert(0, prelude)
    request["section_evidence"]["candidate_1"].insert(0, prelude)
    request["candidate_claims"]["candidate_1"].insert(0, "Prelude finding.")
    calls: list[tuple[str, dict[str, Any]]] = []
    executor, _profile = _critique_executor(calls)
    merged = executor._run_hierarchical_critique(
        node_id="evidence_critique", request=request,
        dependency_hashes={"candidate": "candidate-generation-hash"},
    )
    partial_rows = [row for row in merged["candidate_shard_results"].values()
                    if "claim_support_coverage" in row]
    assert len(partial_rows) > 1
    assert any(row["shard_index"] != row["claim_support_coverage"]["atom_coverage_index"]["shard_index"]
               for row in partial_rows)
    result = derive_critique_disposition(
        {"evidence_critique": merged}, candidate_hashes=request["candidate_hashes"],
        candidate_contents=request["candidate_contents"],
        trusted_shard_critic_ids=("evidence_critique",),
    )
    assert result["global_blocker"] is False, result["issues"]
    assert result["eligible_candidate_ids"] == ["candidate_1"]


def test_executor_does_not_split_relation_marked_section_without_group_binding(monkeypatch) -> None:
    claims = ["Finding A: " + ("complete source qualifier; " * 12),
              "Finding B: " + ("complete source qualifier; " * 12)]
    request = _executor_evidence_request(
        claims,
        ["paper-a", "paper-b"],
        relation_ids=["relation:ab"],
    )
    calls: list[tuple[str, dict[str, Any]]] = []
    executor, _profile = _critique_executor(calls)
    executor.max_source_prompt_tokens = 100
    original_packer = claim_support_packing.pack_critique_claim_support_shards_v1
    packer_calls: list[dict[str, Any]] = []

    def recording_packer(*args, **kwargs):
        packer_calls.append(dict(kwargs))
        return original_packer(*args, **kwargs)

    monkeypatch.setattr(
        claim_support_packing,
        "pack_critique_claim_support_shards_v1",
        recording_packer,
    )
    with pytest.raises(CritiqueClaimSupportPackingError, match="cannot be split"):
        executor._run_hierarchical_critique(
            node_id="evidence_critique",
            request=request,
            dependency_hashes={"candidate": "candidate-generation-hash"},
        )

    assert calls == []
    assert len(packer_calls) == 1
    assert packer_calls[0]["must_preserve_parent_section"] is True


def test_exact_profile_callback_packs_full_claims_and_support_without_text_loss() -> None:
    claims = [
        f"Source claim {index}: " + (f"full qualifier {index} remains attached; " * 55)
        for index in range(52)
    ]
    supports = [_support(index, f"source-{index}", f"paper-{index}") for index in range(52)]
    papers = [f"paper-{index}" for index in range(52)]
    candidate, evidence = _parent_pair(claims, supports, papers)
    profile = ProviderContextProfile.conservative(
        provider="local",
        model="rfs-outline-local",
        endpoint_type="chat_completions",
        model_context_limit=128_000,
        max_output_tokens=8_192,
    )

    def exact_measure(projection: dict[str, Any]) -> int:
        selected_papers = set(projection["candidate_section"]["paper_keys"])
        boundaries = [
            {"paper_key": f"paper-{index}", "boundary": f"Boundary {index} " + ("context " * 90)}
            for index in range(52)
            if f"paper-{index}" in selected_papers
        ]
        request = {
            "candidate_contents": {
                "candidate_1": {"sections": [projection["candidate_section"]]}
            },
            "section_evidence": {"candidate_1": [projection["section_evidence"]]},
            "atom_coverage_index": projection["atom_coverage_index"],
            "claim_support_atom_contract": projection["claim_support_atom_contract"],
            "boundaries": boundaries,
        }
        return int(profile.estimate_request(request)["estimated_input_tokens"])

    shards = pack_critique_claim_support_shards_v1(
        "candidate_1",
        "candidate-generation-hash",
        candidate,
        evidence,
        input_cap_tokens=12_000,
        measure_request=exact_measure,
    )

    assert len(shards) > 1
    assert all(shard["estimated_input_tokens"] <= 12_000 for shard in shards)
    observed_claim_indices = [
        index
        for shard in shards
        for index in shard["atom_coverage_index"]["local_to_parent_claim_indices"]
    ]
    observed_support_indices = [
        index
        for shard in shards
        for index in shard["atom_coverage_index"]["local_to_parent_support_indices"]
    ]
    assert observed_claim_indices == list(range(52))
    assert observed_support_indices == list(range(52))
    for shard in shards:
        coverage = shard["atom_coverage_index"]
        assert coverage["parent_claim_count"] == 52
        assert coverage["parent_support_count"] == 52
        for local_index, parent_index in enumerate(coverage["local_to_parent_claim_indices"]):
            assert shard["candidate_section"]["claims"][local_index] == claims[parent_index]
        for local_index, parent_support_index in enumerate(
            coverage["local_to_parent_support_indices"]
        ):
            support = shard["section_evidence"]["claim_support"][local_index]
            assert support["claim_slot_id"] == supports[parent_support_index]["claim_slot_id"]


def test_hierarchical_executor_projects_exact_role_text_without_dropping_claims() -> None:
    claims = [
        f"Finding {index}: " + (f"complete source condition {index} remains attached; " * 30)
        for index in range(52)
    ]
    paper_keys = [f"paper-{index}" for index in range(52)]
    roles = {paper_key: claim for paper_key, claim in zip(paper_keys, claims, strict=True)}
    section = {
        "section_id": "section:candidate_1:1",
        "title": "Source findings",
        "goal": "Review the complete source claims and their conditions.",
        "paper_keys": paper_keys,
        "paper_roles": roles,
        "relation_ids": [],
        "claims": claims,
    }
    request = {
        "node_id": "structure_critique",
        "candidate_contents": {
            "candidate_1": {
                "candidate_id": "candidate_1",
                "organizing_logic": "source boundaries",
                "sections": [section],
            }
        },
        "candidate_hashes": {"candidate_1": "candidate-generation-hash"},
        "paper_key_aliases": {},
        "review_intent": {"review_question": "Preserve all source claims."},
        "checks": ["section_progression", "goal_claim_alignment"],
    }
    calls: list[tuple[str, dict[str, Any]]] = []
    executor, profile = _critique_executor(calls)
    full_compact = executor._compact_candidate_for_critique(
        "candidate_1",
        request["candidate_contents"]["candidate_1"],
    )
    baseline = {
        **request,
        "candidate_contents": {"candidate_1": full_compact},
        "hierarchy": {
            "level": "critique_candidate_shard",
            "shard_id": "candidate_1:shard:1",
            "target_tokens": 28_000,
            "candidate_id": "candidate_1",
            "section_ids": [section["section_id"]],
        },
    }
    baseline = executor._attach_prompt_authority(
        "structure_critique:local:candidate_1:shard:1",
        baseline,
    )
    baseline = executor._attach_critique_output_contract(
        baseline,
        "structure_critique",
    )
    assert profile.estimate_request(baseline)["estimated_input_tokens"] > 32_000

    executor._run_hierarchical_critique(
        node_id="structure_critique",
        request=request,
        dependency_hashes={"candidate": "candidate-generation-hash"},
    )

    assert len(calls) == 1
    local_request = calls[0][1]
    assert profile.estimate_request(local_request)["estimated_input_tokens"] <= 32_000
    local_section = local_request["candidate_contents"]["candidate_1"]["sections"][0]
    assert local_section["claims"] == claims
    assert local_section["paper_keys"] == paper_keys
    assert all(type(value) is int for value in local_section["paper_roles"].values())
    expanded = expand_critique_paper_roles_v1("candidate_1", local_section)
    assert expanded["paper_roles"] == roles
    assert local_request["candidate_hashes"] == {
        "candidate_1": "candidate-generation-hash"
    }


@pytest.mark.parametrize(
    "mutation",
    [
        lambda _candidate, evidence: evidence["claim_support"][0].update(claim_index=True),
        lambda _candidate, evidence: evidence["claim_support"][0].update(claim_index=99),
        lambda _candidate, evidence: evidence["claim_support"][0].update(paper_key="paper-foreign"),
        lambda _candidate, evidence: evidence["claim_support"].append(
            dict(evidence["claim_support"][0], claim_slot_id="slot:foreign", claim_index=99)
        ),
    ],
)
def test_malformed_or_unmatched_support_fails_closed(mutation) -> None:
    candidate, evidence = _parent_pair(
        ["Finding A."],
        [_support(0, "a", "paper-a")],
        ["paper-a"],
    )
    mutation(candidate, evidence)

    with pytest.raises(CritiqueClaimSupportPackingError):
        pack_critique_claim_support_shards_v1(
            "candidate_1",
            "candidate-generation-hash",
            candidate,
            evidence,
            input_cap_tokens=1000,
            measure_request=_measure_by_atom_size,
        )


def test_comparison_group_missing_a_required_paper_is_rejected() -> None:
    candidate, evidence = _parent_pair(
        ["The comparison statement."],
        [_support(0, "left", "paper-a")],
        ["paper-a", "paper-b"],
        relation_ids=["relation:ab"],
    )
    group = CritiqueComparisonClaimGroupV1(
        group_id="relation:ab",
        claim_indices=(0,),
        required_paper_keys=("paper-a", "paper-b"),
    )

    with pytest.raises(CritiqueClaimSupportPackingError, match="required paper support"):
        pack_critique_claim_support_shards_v1(
            "candidate_1",
            "candidate-generation-hash",
            candidate,
            evidence,
            input_cap_tokens=1000,
            measure_request=_measure_by_atom_size,
            comparison_groups=(group,),
        )


def test_comparison_marked_section_without_proven_grouping_is_never_split() -> None:
    candidate, evidence = _parent_pair(
        ["The left-side report.", "The right-side report."],
        [_support(0, "left", "paper-a"), _support(1, "right", "paper-b")],
        ["paper-a", "paper-b"],
        relation_ids=["relation:ab"],
    )

    with pytest.raises(CritiqueClaimSupportPackingError, match="cannot be split"):
        pack_critique_claim_support_shards_v1(
            "candidate_1",
            "candidate-generation-hash",
            candidate,
            evidence,
            input_cap_tokens=150,
            measure_request=_measure_by_atom_size,
            must_preserve_parent_section=True,
        )


def test_comparison_marked_section_that_fits_remains_one_parent_section() -> None:
    candidate, evidence = _parent_pair(
        ["The left-side report.", "The right-side report."],
        [_support(0, "left", "paper-a"), _support(1, "right", "paper-b")],
        ["paper-a", "paper-b"],
        relation_ids=["relation:ab"],
    )

    shards = pack_critique_claim_support_shards_v1(
        "candidate_1",
        "candidate-generation-hash",
        candidate,
        evidence,
        input_cap_tokens=1000,
        measure_request=_measure_by_atom_size,
        must_preserve_parent_section=True,
    )

    assert len(shards) == 1
    assert shards[0]["candidate_section"]["section_id"] == candidate["section_id"]
    assert shards[0]["atom_coverage_index"]["local_to_parent_claim_indices"] == [0, 1]


def test_contract_explicitly_defines_local_claim_and_parent_coverage_indices() -> None:
    assert "section's claims array" in CRITIQUE_CLAIM_SUPPORT_ATOM_CONTRACT_V1
    assert "atom_coverage_index" in CRITIQUE_CLAIM_SUPPORT_ATOM_CONTRACT_V1
    assert "parent" in CRITIQUE_CLAIM_SUPPORT_ATOM_CONTRACT_V1
