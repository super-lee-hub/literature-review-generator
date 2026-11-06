from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from outline.critique_claim_support_packing import pack_critique_claim_support_shards_v1
from outline.v3_critique import derive_critique_disposition
from runtime.provider_runtime import hash_json

_CANDIDATE_ID = "candidate_1"
_CANDIDATE_HASH = "parent-candidate-hash"
_PARENT_SECTION_ID = "source-parent"


def _parent_section(
    section_id: str = _PARENT_SECTION_ID,
    *,
    include_multi_support: bool = True,
) -> dict[str, Any]:
    claims = ["Finding AB.", "Finding C.", "Finding D."]
    support_specs = [
        (0, "paper-a", "slot:a", ["relation:ab"]),
        (0, "paper-b", "slot:b", ["relation:ab"]),
        (1, "paper-c", "slot:c", []),
        (2, "paper-d", "slot:d", []),
    ]
    if not include_multi_support:
        support_specs = [
            (0, "paper-a", "slot:a", []),
            (1, "paper-c", "slot:c", []),
            (2, "paper-d", "slot:d", []),
        ]
    return {
        "section_id": section_id,
        "title": "Source findings",
        "goal": "Preserve each source finding with its complete support.",
        "paper_keys": ["paper-a", "paper-b", "paper-c", "paper-d"],
        "relation_ids": ["relation:ab"],
        "paper_roles": {},
        "claims": claims,
        "claim_count": len(claims),
        "claim_support": [
            {
                "claim": claims[claim_index],
                "claim_slot_id": slot_id,
                "paper_key": paper_key,
                "primary_claim_id": f"{paper_key}:primary",
                "source_claim_ids": [f"{paper_key}:primary"],
                "evidence_ids": [f"{paper_key}:evidence"],
                "source_field_ids": [f"{paper_key}:field"],
                "relation_ids": relation_ids,
            }
            for claim_index, paper_key, slot_id, relation_ids in support_specs
        ],
    }


def _packed_parts(
    section: dict[str, Any],
    *,
    input_cap: int = 3,
) -> tuple[dict[str, Any], ...]:
    claims = list(section["claims"])
    candidate_section = {
        key: section[key]
        for key in (
            "section_id",
            "title",
            "goal",
            "paper_keys",
            "paper_roles",
            "relation_ids",
            "claims",
            "claim_count",
        )
    }
    support_rows = []
    for support in section["claim_support"]:
        claim_index = claims.index(support["claim"])
        support_rows.append({
            **{key: value for key, value in support.items() if key != "claim"},
            "claim_index": claim_index,
        })
    section_evidence = {
        **{
            key: value for key, value in section.items()
            if key not in {"claims", "claim_support"}
        },
        "candidate_id": _CANDIDATE_ID,
        "claim_array_ref": {
            "candidate_id": _CANDIDATE_ID,
            "section_id": section["section_id"],
            "field": "candidate_contents.sections.claims",
        },
        "claim_support": support_rows,
    }
    return pack_critique_claim_support_shards_v1(
        _CANDIDATE_ID,
        _CANDIDATE_HASH,
        candidate_section,
        section_evidence,
        input_cap_tokens=input_cap,
        measure_request=lambda projection: (
            len(projection["candidate_section"]["claims"])
            + len(projection["section_evidence"]["claim_support"])
        ),
    )


def _certificate(section: dict[str, Any], part: dict[str, Any]) -> dict[str, Any]:
    body = {
        "schema_version": "outline_critique_claim_support_coverage/v1",
        "parent_source_section_hash": hash_json(section),
        "atom_coverage_index": deepcopy(part["atom_coverage_index"]),
    }
    return {**body, "content_hash": hash_json(body)}


def _row(
    *,
    shard_index: int,
    reviewed_section_ids: list[str],
    certificate: dict[str, Any] | None = None,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "candidate_id": _CANDIDATE_ID,
        "shard_index": shard_index,
        "reviewed_section_ids": reviewed_section_ids,
        "parent_candidate_hash": _CANDIDATE_HASH,
        "passed": True,
        "blocking_diagnostics": [],
        "issues": [],
    }
    if certificate is not None:
        row["claim_support_coverage"] = certificate
    return row


def _inputs(
    *,
    include_whole_section: bool = True,
) -> tuple[dict[str, str], dict[str, dict[str, Any]], dict[str, Any], tuple[dict[str, Any], ...]]:
    section = _parent_section()
    parts = _packed_parts(section)
    sections = [section]
    rows: dict[str, dict[str, Any]] = {}
    for index, part in enumerate(parts, start=1):
        rows[f"{_CANDIDATE_ID}:shard:{index}"] = _row(
            shard_index=index,
            reviewed_section_ids=[_PARENT_SECTION_ID],
            certificate=_certificate(section, part),
        )
    if include_whole_section:
        whole_section = {
            "section_id": "whole-other-section",
            "title": "Another source section",
            "claims": ["The separate source claim."],
        }
        sections.append(whole_section)
        index = len(parts) + 1
        rows[f"{_CANDIDATE_ID}:shard:{index}"] = _row(
            shard_index=index,
            reviewed_section_ids=[whole_section["section_id"]],
        )
    hashes = {_CANDIDATE_ID: _CANDIDATE_HASH}
    contents = {_CANDIDATE_ID: {"candidate_id": _CANDIDATE_ID, "sections": sections}}
    critique = {"coverage_critique": {"passed": True, "candidate_shard_results": rows}}
    return hashes, contents, critique, parts


def _derive(
    hashes: dict[str, str],
    contents: dict[str, dict[str, Any]],
    critique: dict[str, Any],
    *,
    trusted: bool = True,
) -> dict[str, Any]:
    return derive_critique_disposition(
        critique,
        candidate_hashes=hashes,
        candidate_contents=contents,
        trusted_shard_critic_ids=("coverage_critique",) if trusted else (),
    )


def _rehash_certificate(row: dict[str, Any]) -> None:
    certificate = row["claim_support_coverage"]
    atom = certificate["atom_coverage_index"]
    atom["content_hash"] = hash_json({key: value for key, value in atom.items() if key != "content_hash"})
    body = {key: value for key, value in certificate.items() if key != "content_hash"}
    certificate["content_hash"] = hash_json(body)


def test_three_claim_support_parts_cover_parent_with_other_whole_sections() -> None:
    hashes, contents, critique, parts = _inputs()

    result = _derive(hashes, contents, critique)

    assert len(parts) == 3
    assert result["global_blocker"] is False
    assert result["blocked_candidate_ids"] == []
    assert result["eligible_candidate_ids"] == [_CANDIDATE_ID]


def test_all_support_rows_for_multisource_ab_claim_stay_in_one_atom() -> None:
    section = _parent_section(include_multi_support=True)
    parts = _packed_parts(section)
    ab_holders = [
        part for part in parts
        if 0 in part["atom_coverage_index"]["local_to_parent_claim_indices"]
    ]
    assert len(ab_holders) == 1
    assert ab_holders[0]["atom_coverage_index"]["local_to_parent_support_indices"] == [0, 1]

    hashes, contents, critique, _parts = _inputs(include_whole_section=False)
    result = _derive(hashes, contents, critique)
    assert result["global_blocker"] is False
    assert result["eligible_candidate_ids"] == [_CANDIDATE_ID]


@pytest.mark.parametrize(
    "mutation",
    [
        "missing",
        "duplicate",
        "invalid_index_type",
        "foreign",
        "rehashed_wrong_parent",
        "partial_support",
        "mixed",
    ],
)
def test_invalid_claim_support_certificates_fail_closed(mutation: str) -> None:
    hashes, contents, critique, _parts = _inputs(include_whole_section=False)
    rows = critique["coverage_critique"]["candidate_shard_results"]
    target = rows[f"{_CANDIDATE_ID}:shard:2"]

    if mutation == "missing":
        target.pop("claim_support_coverage")
    elif mutation == "duplicate":
        target["claim_support_coverage"]["atom_coverage_index"][
            "local_to_parent_claim_indices"
        ] = [1, 1]
        _rehash_certificate(target)
    elif mutation == "invalid_index_type":
        target["claim_support_coverage"]["atom_coverage_index"][
            "local_to_parent_claim_indices"
        ] = [{"not": "an integer"}]
        _rehash_certificate(target)
    elif mutation == "foreign":
        target["claim_support_coverage"]["atom_coverage_index"]["candidate_id"] = "candidate_foreign"
        _rehash_certificate(target)
    elif mutation == "rehashed_wrong_parent":
        wrong_parent = deepcopy(contents[_CANDIDATE_ID]["sections"][0])
        wrong_parent["goal"] = "A different source parent."
        target["claim_support_coverage"]["parent_source_section_hash"] = hash_json(wrong_parent)
        _rehash_certificate(target)
    elif mutation == "partial_support":
        target["claim_support_coverage"]["atom_coverage_index"][
            "local_to_parent_claim_indices"
        ] = [0]
        target["claim_support_coverage"]["atom_coverage_index"][
            "local_to_parent_support_indices"
        ] = [0]
        target["claim_support_coverage"]["atom_coverage_index"]["atomic_claim_groups"] = [[0]]
        _rehash_certificate(target)
    elif mutation == "mixed":
        rows[f"{_CANDIDATE_ID}:shard:4"] = _row(
            shard_index=4,
            reviewed_section_ids=[_PARENT_SECTION_ID],
        )

    result = _derive(hashes, contents, critique)
    assert result["global_blocker"] is True
    assert result["eligible_candidate_ids"] == []


def test_untrusted_claim_support_certificate_remains_unread_and_blocking() -> None:
    hashes, contents, critique, _parts = _inputs(include_whole_section=False)

    result = _derive(hashes, contents, critique, trusted=False)

    assert result["global_blocker"] is True
    assert result["eligible_candidate_ids"] == []
    assert not any(issue["source"] == "trusted_candidate_shard" for issue in result["issues"])
