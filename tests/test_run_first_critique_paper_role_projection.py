from __future__ import annotations

from copy import deepcopy

import pytest

from outline.critique_paper_role_projection import (
    CritiquePaperRoleProjectionError,
    expand_critique_paper_roles_v1,
    project_critique_paper_roles_v1,
)
from runtime.provider_context import ProviderContextProfile


def _section() -> dict[str, object]:
    claims = [
        f"Source claim {index}: " + (f"condition {index} remains attached; " * 35)
        for index in range(52)
    ]
    return {
        "section_id": "section:candidate_1:1",
        "title": "Source findings",
        "paper_keys": [f"paper-{index}" for index in range(52)],
        "claims": claims,
        "paper_roles": {
            f"paper-{index}": claim for index, claim in enumerate(claims)
        },
    }


def test_projection_round_trips_exact_local_role_text_without_mutating_input() -> None:
    section = _section()
    original = deepcopy(section)

    projected = project_critique_paper_roles_v1("candidate_1", section)
    expanded = expand_critique_paper_roles_v1("candidate_1", projected)

    assert projected["paper_keys"] == original["paper_keys"]
    assert projected["claims"] == original["claims"]
    assert all(type(value) is int for value in projected["paper_roles"].values())
    assert projected["paper_role_claim_array_ref"] == {
        "candidate_id": "candidate_1",
        "section_id": "section:candidate_1:1",
        "field": "candidate_contents.sections.claims",
    }
    assert expanded == original
    assert section == original


def test_only_exact_local_claim_matches_are_encoded() -> None:
    section = {
        "section_id": "section:candidate_2:0",
        "claims": ["Exact source claim."],
        "paper_roles": {
            "paper-a": "Exact source claim.",
            "paper-b": "Distinct literal role explanation.",
        },
    }

    projected = project_critique_paper_roles_v1("candidate_2", section)
    expanded = expand_critique_paper_roles_v1("candidate_2", projected)

    assert projected["paper_roles"] == {
        "paper-a": 0,
        "paper-b": "Distinct literal role explanation.",
    }
    assert expanded["paper_roles"] == section["paper_roles"]


def test_duplicate_claim_text_uses_deterministic_first_index_and_expands_losslessly() -> None:
    section = {
        "section_id": "section:candidate_3:0",
        "claims": ["Same exact text.", "Same exact text."],
        "paper_roles": {"paper-a": "Same exact text."},
    }

    projected = project_critique_paper_roles_v1("candidate_3", section)

    assert projected["paper_roles"] == {"paper-a": 0}
    assert expand_critique_paper_roles_v1("candidate_3", projected)["paper_roles"] == {
        "paper-a": "Same exact text."
    }


def test_projected_role_indices_are_bound_to_the_enclosing_candidate_and_section() -> None:
    projected = project_critique_paper_roles_v1("candidate_1", _section())

    with pytest.raises(CritiquePaperRoleProjectionError, match="reference"):
        expand_critique_paper_roles_v1("candidate_2", projected)

    foreign_section = deepcopy(projected)
    foreign_section["section_id"] = "section:candidate_1:2"
    with pytest.raises(CritiquePaperRoleProjectionError, match="reference"):
        expand_critique_paper_roles_v1("candidate_1", foreign_section)

    foreign_field = deepcopy(projected)
    foreign_field["paper_role_claim_array_ref"]["field"] = "candidate_contents.planned_claims"
    with pytest.raises(CritiquePaperRoleProjectionError, match="reference"):
        expand_critique_paper_roles_v1("candidate_1", foreign_field)


@pytest.mark.parametrize("bad_index", [True, -1, 52])
def test_invalid_or_boolean_claim_indices_fail_closed(bad_index: object) -> None:
    projected = project_critique_paper_roles_v1("candidate_1", _section())
    projected["paper_roles"]["paper-0"] = bad_index

    with pytest.raises(CritiquePaperRoleProjectionError):
        expand_critique_paper_roles_v1("candidate_1", projected)


def test_unmatched_literal_roles_remain_unencoded() -> None:
    section = {
        "section_id": "section:candidate_4:0",
        "claims": ["A source claim."],
        "paper_roles": {"paper-a": "A custom role."},
    }

    projected = project_critique_paper_roles_v1("candidate_4", section)

    assert projected == section


def test_exact_local_indices_reduce_the_serialized_profile_estimate() -> None:
    section = _section()
    request = {
        "candidate_contents": {
            "candidate_1": {"sections": [section]},
        }
    }
    projected = {
        "candidate_contents": {
            "candidate_1": {
                "sections": [
                    project_critique_paper_roles_v1("candidate_1", section)
                ]
            }
        },
        "paper_role_claim_index_contract": (
            "Resolve integer role values through each enclosing section's exact claims array."
        ),
    }
    profile = ProviderContextProfile.conservative(
        provider="test",
        model="test",
        endpoint_type="responses",
        model_context_limit=200_000,
        max_output_tokens=16_000,
        reasoning_reserve=4_096,
        safety_margin=2_048,
    )

    original_estimate = profile.estimate_request(request)["estimated_input_tokens"]
    projected_estimate = profile.estimate_request(projected)["estimated_input_tokens"]

    assert projected_estimate < original_estimate
    assert original_estimate - projected_estimate > 10_000
    assert projected["candidate_contents"]["candidate_1"]["sections"][0]["claims"] == section["claims"]
