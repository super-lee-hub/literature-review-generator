from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
from typing import Any

from outline.v3_revision import apply_selected_revision


def _support(claim: str, claim_id: str) -> dict[str, Any]:
    return {
        "claim": claim,
        "claim_id": claim_id,
        "paper_key": "paper-a",
        "source_claim_ids": [f"source:{claim_id}"],
        "evidence_ids": [f"evidence:{claim_id}"],
    }


def _section(
    section_id: str = "candidate_1_section_1",
    *,
    claims: list[str] | None = None,
    claim_support: list[dict[str, Any]] | None = None,
    paper_keys: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "section_id": section_id,
        "title": "Initial title",
        "goal": "Initial goal",
        "paper_keys": list(paper_keys or ["paper-a"]),
        "claims": list(claims or []),
        "claim_support": deepcopy(claim_support or []),
    }


def _apply(
    sections: list[dict[str, Any]],
    recommendations: list[Any],
    *,
    view_by_key: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return apply_selected_revision(
        candidate_id="candidate_1",
        sections=sections,
        recommendations=recommendations,
        view_by_key=view_by_key or {},
        parent_candidate_hash="parent-hash-candidate-1",
    )


def test_remove_claim_removes_orphan_support_and_retains_surviving_support() -> None:
    surviving = "Supported claim remains."
    removed = "Claim selected for removal."
    orphan = "Claim already absent."
    source = _section(
        claims=[surviving, removed],
        claim_support=[
            _support(surviving, "keep"),
            _support(removed, "remove"),
            _support(orphan, "orphan"),
        ],
    )
    original = deepcopy(source)

    result = _apply(
        [source],
        [{
            "issue_id": "issue:remove-claim",
            "target_section_ids": [source["section_id"]],
            "operation": "remove_claim",
            "replacement": removed,
        }],
    )

    revised = result["revised_sections"][0]
    assert revised["claims"] == [surviving]
    assert revised["claim_support"] == [_support(surviving, "keep")]
    assert result["unresolved_revisions"] == []
    assert result["revision_records"][0]["candidate_id"] == "candidate_1"
    assert revised["revision_lineage"]["candidate_id"] == "candidate_1"
    assert source == original


def test_aggregate_replacement_cleans_support_for_removed_claim_and_keeps_survivor() -> None:
    aggregate = "Aggregate gap claim about the whole sample."
    survivor = "A supported finding remains."
    source = _section(
        claims=[aggregate, survivor],
        claim_support=[
            _support(aggregate, "aggregate"),
            _support(survivor, "survivor"),
        ],
        paper_keys=["paper-a", "paper-b"],
    )
    views = {
        "paper-a": SimpleNamespace(
            limitations=["The authors report a narrow sample."],
            research_gaps=[],
            future_directions=[],
        ),
        "paper-b": SimpleNamespace(
            limitations=[],
            research_gaps=["The authors call for another context."],
            future_directions=[],
        ),
    }

    result = _apply(
        [source],
        [{
            "issue_id": "issue:aggregate-boundary",
            "target_section_ids": [source["section_id"]],
            "operation": "replace_aggregate_claim_with_per_paper_boundaries",
        }],
        view_by_key=views,
    )

    revised = result["revised_sections"][0]
    assert aggregate not in revised["claims"]
    assert survivor in revised["claims"]
    assert any(claim.startswith("paper-a 的作者自陈边界：") for claim in revised["claims"])
    assert any(claim.startswith("paper-b 的作者自陈边界：") for claim in revised["claims"])
    assert revised["claim_support"] == [_support(survivor, "survivor")]
    assert result["unresolved_revisions"] == []


def test_remove_last_claim_is_unresolved_and_leaves_claim_and_support_intact() -> None:
    claim = "Only supported claim."
    support = _support(claim, "only")
    source = _section(claims=[claim], claim_support=[support])

    result = _apply(
        [source],
        [{
            "issue_id": "issue:last-claim",
            "target_section_ids": [source["section_id"]],
            "operation": "remove_claim",
            "replacement": claim,
        }],
    )

    assert result["revised_sections"][0]["claims"] == [claim]
    assert result["revised_sections"][0]["claim_support"] == [support]
    assert result["revision_records"] == []
    assert result["unresolved_revisions"][0]["target_results"] == [{
        "section_id": source["section_id"],
        "status": "unresolved",
        "reason": "cannot_delete_last_supported_claim",
    }]


def test_explicit_target_uses_exact_section_identity() -> None:
    section_one = _section("S1")
    section_ten = _section("S10")

    result = _apply(
        [section_one, section_ten],
        [{
            "issue_id": "issue:s10-title",
            "target_section_ids": ["S10"],
            "operation": "replace_title",
            "replacement": "Only section ten changes.",
        }],
    )

    revised = {section["section_id"]: section for section in result["revised_sections"]}
    assert revised["S1"]["title"] == "Initial title"
    assert revised["S10"]["title"] == "Only section ten changes."
    assert result["unresolved_revisions"] == []


def test_missing_target_is_reported_not_silently_skipped() -> None:
    source = _section("S1")
    result = _apply(
        [source],
        [{
            "issue_id": "issue:missing-target",
            "target_section_ids": ["S12"],
            "operation": "replace_title",
            "replacement": "Must not be rebound.",
        }],
    )

    assert result["revised_sections"][0]["title"] == "Initial title"
    assert result["revision_records"] == []
    unresolved = result["unresolved_revisions"][0]
    assert unresolved["issue_id"] == "issue:missing-target"
    assert unresolved["target_results"][0]["reason"] == "target_section_not_found"


def test_unsupported_operation_is_reported_as_unresolved() -> None:
    source = _section("S1")
    result = _apply(
        [source],
        [{
            "issue_id": "issue:unsupported",
            "target_section_ids": ["S1"],
            "operation": "rewrite_without_contract",
        }],
    )

    assert result["revision_records"] == []
    assert result["unresolved_revisions"][0]["target_results"][0]["reason"] == "unsupported_or_missing_operation"


def test_malformed_claim_support_makes_claim_revision_unresolved_atomically() -> None:
    claim = "Claim to remove."
    survivor = "Another claim remains supported."
    source = _section(
        claims=[claim, survivor],
        claim_support=[_support(claim, "claim"), "malformed support row"],
    )

    result = _apply(
        [source],
        [{
            "issue_id": "issue:malformed-support",
            "target_section_ids": [source["section_id"]],
            "operation": "remove_claim",
            "replacement": claim,
        }],
    )

    revised = result["revised_sections"][0]
    assert revised["claims"] == [claim, survivor]
    assert revised["claim_support"] == source["claim_support"]
    assert result["revision_records"] == []
    assert result["unresolved_revisions"][0]["target_results"][0]["reason"] == "claim_support_malformed"
