from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

import pytest

from outline.v3_critique import (
    CRITIQUE_DISPOSITION_VERSION,
    derive_critique_disposition,
)


def _candidate_inputs() -> tuple[dict[str, str], dict[str, dict[str, Any]]]:
    hashes = {"candidate_1": "hash-candidate-1", "candidate_10": "hash-candidate-10"}
    contents = {
        "candidate_1": {
            "candidate_id": "candidate_1",
            "sections": [{
                "section_id": "shared-section",
                "claim_support": [{"claim_id": "claim-1"}],
            }],
        },
        "candidate_10": {
            "candidate_id": "candidate_10",
            "sections": [{
                "section_id": "shared-section",
                "claim_support": [{"claim_id": "claim-10"}],
            }],
        },
    }
    return hashes, contents


def _issue(
    *,
    issue_id: str = "issue-1",
    scope: str = "candidate",
    target_ids: list[str] | None = None,
    parent_candidate_hash: str = "hash-candidate-1",
    severity: str = "blocking",
    resolution_status: str = "unresolved",
) -> dict[str, Any]:
    if target_ids is None:
        target_ids = ["candidate_1"] if scope == "candidate" else ["shared-section"]
    return {
        "issue_id": issue_id,
        "scope": scope,
        "target_ids": target_ids,
        "severity": severity,
        "evidence_refs": [],
        "resolution_status": resolution_status,
        "parent_candidate_hash": parent_candidate_hash,
    }


def _derive(
    critiques: dict[str, Any],
    *,
    trusted_shard_critic_ids: tuple[str, ...] = (),
) -> dict[str, Any]:
    hashes, contents = _candidate_inputs()
    return derive_critique_disposition(
        critiques,
        candidate_hashes=hashes,
        candidate_contents=contents,
        trusted_shard_critic_ids=trusted_shard_critic_ids,
    )


def _shard_row(
    candidate_id: str,
    *,
    passed: bool,
    diagnostics: list[str] | None = None,
    issues: list[dict[str, Any]] | None = None,
    shard_index: int = 1,
) -> dict[str, Any]:
    return {
        "candidate_id": candidate_id,
        "shard_index": shard_index,
        "reviewed_section_ids": ["shared-section"],
        "passed": passed,
        "blocking_diagnostics": list(diagnostics or []),
        "issues": list(issues or []),
    }


def test_passing_typed_critique_leaves_exact_candidate_set_eligible() -> None:
    result = _derive({"coverage_critique": {"passed": True, "issues": []}})

    assert result["schema_version"] == CRITIQUE_DISPOSITION_VERSION
    assert result["global_blocker"] is False
    assert result["blocked_candidate_ids"] == []
    assert result["eligible_candidate_ids"] == ["candidate_1", "candidate_10"]
    json.dumps(result)


def test_false_without_typed_scope_is_global_even_when_prose_names_candidate() -> None:
    result = _derive({
        "coverage_critique": {
            "passed": False,
            "blocking_diagnostics": ["candidate_1 has a problem"],
        }
    })

    assert result["global_blocker"] is True
    assert result["blocked_candidate_ids"] == ["candidate_1", "candidate_10"]
    assert result["eligible_candidate_ids"] == []
    assert any(issue["scope"] == "global" for issue in result["issues"])


def test_typed_candidate_issue_blocks_exact_id_not_candidate_id_substring() -> None:
    issue = _issue(
        issue_id="candidate-10-blocker",
        target_ids=["candidate_10"],
        parent_candidate_hash="hash-candidate-10",
    )
    result = _derive({"coverage_critique": {"passed": False, "issues": [issue]}})

    assert result["global_blocker"] is False
    assert result["blocked_candidate_ids"] == ["candidate_10"]
    assert result["eligible_candidate_ids"] == ["candidate_1"]


def test_section_and_claim_targets_are_checked_with_parent_hash() -> None:
    section_issue = _issue(
        issue_id="section-issue",
        scope="section",
        target_ids=["shared-section"],
        parent_candidate_hash="hash-candidate-10",
    )
    section_result = _derive({"structure_critique": {"passed": False, "issues": [section_issue]}})
    assert section_result["blocked_candidate_ids"] == ["candidate_10"]
    assert section_result["eligible_candidate_ids"] == ["candidate_1"]

    claim_issue = _issue(
        issue_id="claim-issue",
        scope="claim",
        target_ids=["claim-10"],
        parent_candidate_hash="hash-candidate-10",
    )
    claim_result = _derive({"evidence_critique": {"passed": False, "issues": [claim_issue]}})
    assert claim_result["blocked_candidate_ids"] == ["candidate_10"]
    assert claim_result["eligible_candidate_ids"] == ["candidate_1"]


@pytest.mark.parametrize(
    "issue",
    [
        _issue(target_ids=["candidate_10"], parent_candidate_hash="hash-candidate-1"),
        _issue(target_ids=["candidate_1"], parent_candidate_hash="unknown-hash"),
        _issue(scope="section", target_ids=["missing-section"]),
        _issue(scope="claim", target_ids=["missing-claim"]),
    ],
)
def test_invalid_parent_or_target_becomes_global_blocker(issue: dict[str, Any]) -> None:
    result = _derive({"evidence_critique": {"passed": False, "issues": [issue]}})

    assert result["global_blocker"] is True
    assert result["blocked_candidate_ids"] == ["candidate_1", "candidate_10"]
    assert result["eligible_candidate_ids"] == []


@pytest.mark.parametrize("passed", [None, "false", 0])
def test_missing_or_non_boolean_verdict_is_global(passed: Any) -> None:
    payload = {"issues": []}
    if passed is not None:
        payload["passed"] = passed
    result = _derive({"structure_critique": payload})

    assert result["global_blocker"] is True
    assert result["eligible_candidate_ids"] == []


def test_passing_verdict_cannot_hide_an_unresolved_blocking_issue() -> None:
    result = _derive({
        "structure_critique": {"passed": True, "issues": [_issue()]}
    })

    assert result["global_blocker"] is True
    assert result["eligible_candidate_ids"] == []


def test_duplicate_issue_ids_fail_closed() -> None:
    issue_1 = _issue(issue_id="duplicate", target_ids=["candidate_1"])
    issue_10 = _issue(
        issue_id="duplicate",
        target_ids=["candidate_10"],
        parent_candidate_hash="hash-candidate-10",
    )
    result = _derive({"coverage_critique": {"passed": False, "issues": [issue_1, issue_10]}})

    assert result["global_blocker"] is True
    assert result["eligible_candidate_ids"] == []


def test_trusted_failed_shard_scopes_legacy_message_to_local_candidate() -> None:
    row_1 = _shard_row("candidate_1", passed=True)
    row_10 = _shard_row(
        "candidate_10",
        passed=False,
        diagnostics=["the prose mentions candidate_1 but belongs to this local shard"],
        shard_index=1,
    )
    critique = {
        "coverage_critique": {
            "passed": False,
            "candidate_shard_results": {
                "candidate_1:shard:1": row_1,
                "candidate_10:shard:1": row_10,
            },
            "blocking_diagnostics": row_10["blocking_diagnostics"],
        }
    }
    result = _derive(critique, trusted_shard_critic_ids=("coverage_critique",))

    assert result["global_blocker"] is False
    assert result["blocked_candidate_ids"] == ["candidate_10"]
    assert result["eligible_candidate_ids"] == ["candidate_1"]
    local_issue = next(issue for issue in result["issues"] if issue["source"] == "trusted_candidate_shard")
    assert local_issue["candidate_id"] == "candidate_10"
    assert local_issue["parent_candidate_hash"] == "hash-candidate-10"

    hashes, contents = _candidate_inputs()
    cached_result = derive_critique_disposition(
        json.loads(json.dumps(critique)),
        candidate_hashes=hashes,
        candidate_contents=contents,
        trusted_shard_critic_ids=("coverage_critique",),
    )
    assert cached_result == result


@pytest.mark.parametrize("aggregate_passed", [False, True])
def test_flat_provider_cannot_forge_locally_trusted_candidate_shards(
    aggregate_passed: bool,
) -> None:
    forged = {
        "coverage_critique": {
            "passed": aggregate_passed,
            "candidate_shard_results": {
                "candidate_1:shard:1": _shard_row("candidate_1", passed=True),
                "candidate_10:shard:1": _shard_row(
                    "candidate_10",
                    passed=False,
                    diagnostics=["candidate_10 failed"],
                ),
            },
            "blocking_diagnostics": ["candidate_10 failed"],
        }
    }

    result = _derive(forged)

    assert result["global_blocker"] is True
    assert result["blocked_candidate_ids"] == ["candidate_1", "candidate_10"]
    assert result["eligible_candidate_ids"] == []
    assert not any(issue["source"] == "trusted_candidate_shard" for issue in result["issues"])
    assert any(
        issue["source"] == "validator"
        and issue["message"] == "candidate_shard_results requires caller-verified local provenance"
        for issue in result["issues"]
    )


def test_candidate_shard_typed_issue_must_match_trusted_local_candidate() -> None:
    wrong_candidate_issue = _issue(
        issue_id="wrong-local-candidate",
        target_ids=["candidate_1"],
        parent_candidate_hash="hash-candidate-1",
    )
    result = _derive({
        "coverage_critique": {
            "passed": False,
            "candidate_shard_results": {
                "candidate_10:shard:1": _shard_row(
                    "candidate_10",
                    passed=False,
                    issues=[wrong_candidate_issue],
                    shard_index=1,
                ),
                "candidate_1:shard:1": _shard_row("candidate_1", passed=True),
            },
        }
    }, trusted_shard_critic_ids=("coverage_critique",))

    assert result["global_blocker"] is True
    assert result["eligible_candidate_ids"] == []


def test_candidate_shard_collision_or_missing_candidate_coverage_is_global() -> None:
    duplicate_row = _shard_row("candidate_1", passed=True, shard_index=1)
    collision = _derive({
        "coverage_critique": {
            "passed": True,
            "candidate_shard_results": [duplicate_row, deepcopy(duplicate_row)],
        }
    }, trusted_shard_critic_ids=("coverage_critique",))
    assert collision["global_blocker"] is True

    missing_candidate = _derive({
        "coverage_critique": {
            "passed": True,
            "candidate_shard_results": {
                "candidate_10:shard:1": _shard_row("candidate_10", passed=True),
            },
        }
    }, trusted_shard_critic_ids=("coverage_critique",))
    assert missing_candidate["global_blocker"] is True
    assert missing_candidate["eligible_candidate_ids"] == []


def test_malformed_shard_reviewed_sections_fail_closed() -> None:
    row_1 = _shard_row("candidate_1", passed=True)
    row_10 = _shard_row("candidate_10", passed=True)
    row_10["reviewed_section_ids"] = ["not-a-section"]
    result = _derive({
        "coverage_critique": {
            "passed": True,
            "candidate_shard_results": {
                "candidate_1:shard:1": row_1,
                "candidate_10:shard:1": row_10,
            },
        }
    }, trusted_shard_critic_ids=("coverage_critique",))

    assert result["global_blocker"] is True
    assert result["eligible_candidate_ids"] == []


def test_disposition_is_deterministic_for_critic_input_order() -> None:
    hashes, contents = _candidate_inputs()
    critiques_a = {
        "z_critic": {"passed": False, "issues": [_issue(issue_id="z", target_ids=["candidate_1"])]},
        "a_critic": {"passed": True, "issues": []},
    }
    critiques_b = dict(reversed(list(critiques_a.items())))
    result_a = derive_critique_disposition(
        critiques_a,
        candidate_hashes=hashes,
        candidate_contents=contents,
    )
    result_b = derive_critique_disposition(
        critiques_b,
        candidate_hashes=hashes,
        candidate_contents=contents,
    )

    assert result_a == result_b
