from __future__ import annotations

from copy import deepcopy

import pytest

from outline.v3_executor import OutlineV3ExecutionError
from tests.test_current_runtime_full_e2e import _candidate_source_bound_response
from tests.test_run_first_small_fixture_required_groups import _request


_CAPTURED_TITLES = {
    "paper-a": "paper-a",
    "paper-b": "paper-b",
    "paper-c": "paper-c",
}
_DISTINCT_TITLES = {
    "paper-a": "Observed Treatment Effects in Study A",
    "paper-b": "Treatment Outcomes Across Context B",
    "paper-c": "A Third Context for Treatment Outcomes",
}


def _request_with_titles(title_by_paper: dict[str, str]):
    executor, scope, request, source_text = _request()
    request = deepcopy(request)
    request["evidence"] = [
        {**row, "title": title_by_paper[str(row["paper_key"])]}
        for row in request["evidence"]
    ]
    return executor, scope, request, source_text


@pytest.mark.parametrize("title_by_paper", [_CAPTURED_TITLES, _DISTINCT_TITLES])
def test_candidate_fixture_emits_exact_titles_for_all_source_closed_claims_and_required_groups(
    title_by_paper: dict[str, str],
) -> None:
    executor, scope, request, source_text = _request_with_titles(title_by_paper)
    trace: list[dict] = []

    payload = _candidate_source_bound_response(
        "candidate_1_provider_generation", request, trace,
    )["content"]
    executor._validate_candidate_payload(
        "candidate_1", payload,
        allowed_paper_keys=["paper-a", "paper-b", "paper-c"],
        allowed_relation_ids=["relation-ab", "relation-bc"],
    )

    slots_by_id = {slot.claim_slot_id: slot for slot in scope.claim_slots}
    for section in payload["sections"]:
        claim_text = section["claims"][0]
        for paper in section["paper_keys"]:
            title = title_by_paper[paper]
            assert claim_text.casefold().count(title.casefold()) == 1
            assert all(text.rstrip(".") in claim_text for text in source_text[paper])
        for support in section["claim_support"]:
            slot = slots_by_id[support["claim_slot_id"]]
            assert support["source_claim_ids"] == list(slot.source_claim_ids)
            assert support["evidence_ids"] == list(slot.evidence_ids)
            assert support["source_field_ids"] == list(slot.source_field_ids)
            assert support.get("study_id", "") == slot.study_id == ""
            assert support["qualifier_source_claim_ids"] == [f"claim:{slot.paper_key}:boundary"]
            assert support["qualifier_evidence_ids"] == [f"evidence:{slot.paper_key}:boundary"]
            assert support["qualifier_source_field_ids"] == [f"field:{slot.paper_key}:boundary"]

    for group_id in ("bridge-bc", "comparison-ab"):
        group = next(group for group in scope.claim_groups if group.claim_group_id == group_id)
        group_sections = [
            section for section in payload["sections"]
            if section.get("task_ids") == [group.task_id]
        ]
        assert len(group_sections) == 1
        section = group_sections[0]
        assert {row["claim_slot_id"] for row in section["claim_support"]} == set(group.support_slot_ids)
        assert set(section["paper_keys"]) == set(group.paper_keys)
        assert section["relation_ids"] == list(group.relation_ids)


def test_candidate_fixture_keeps_required_multisource_groups_out_of_single_paper_rows() -> None:
    executor, scope, request, _source_text = _request_with_titles(_DISTINCT_TITLES)
    comparison = next(group for group in scope.claim_groups if group.claim_group_id == "comparison-ab")
    for route in request["shared_semantic_context"]["topic_routes"]:
        claim = route["fragments"][0]["provider_results"][0]["provider_output"]["claims"][0]
        if claim["paper_key"] in {"paper-a", "paper-b"}:
            # Replay the shape that previously let a comparison group's
            # member slot masquerade as a standalone per-paper assertion.
            claim["claim_id"] = comparison.synthesis_claim_id

    payload = _candidate_source_bound_response(
        "candidate_1_provider_generation", request, None,
    )["content"]
    executor._validate_candidate_payload(
        "candidate_1", payload,
        allowed_paper_keys=["paper-a", "paper-b", "paper-c"],
        allowed_relation_ids=["relation-ab", "relation-bc"],
    )

    comparison_sections = [
        section for section in payload["sections"]
        if section.get("task_ids") == [comparison.task_id]
    ]
    assert len(comparison_sections) == 1
    shared_section = comparison_sections[0]
    assert {row["claim_slot_id"] for row in shared_section["claim_support"]} == set(
        comparison.support_slot_ids
    )
    comparison_slot_ids = set(comparison.support_slot_ids)
    assert not any(
        len(section["paper_keys"]) == 1
        and comparison_slot_ids.intersection(row["claim_slot_id"] for row in section["claim_support"])
        for section in payload["sections"]
    )
    assert all(
        shared_section["claims"][0].casefold().count(_DISTINCT_TITLES[paper].casefold()) == 1
        for paper in comparison.paper_keys
    )
    bridge = next(group for group in scope.claim_groups if group.claim_group_id == "bridge-bc")
    bridge_section = next(
        section for section in payload["sections"]
        if section.get("task_ids") == [bridge.task_id]
    )
    assert {row["claim_slot_id"] for row in bridge_section["claim_support"]} == set(
        bridge.support_slot_ids
    )


def test_candidate_fixture_rejects_a_required_group_missing_one_source_member() -> None:
    executor, scope, request, _source_text = _request_with_titles(_DISTINCT_TITLES)
    payload = _candidate_source_bound_response(
        "candidate_1_provider_generation", request, None,
    )["content"]
    comparison = next(group for group in scope.claim_groups if group.claim_group_id == "comparison-ab")
    section = next(
        section for section in payload["sections"]
        if section.get("task_ids") == [comparison.task_id]
    )
    section["claim_support"].pop()

    with pytest.raises(OutlineV3ExecutionError, match="finite output contract"):
        executor._validate_candidate_payload(
            "candidate_1", payload,
            allowed_paper_keys=["paper-a", "paper-b", "paper-c"],
            allowed_relation_ids=["relation-ab", "relation-bc"],
        )


def test_candidate_fixture_requires_unambiguous_evidence_titles() -> None:
    _executor, _scope, request, _source_text = _request_with_titles(_DISTINCT_TITLES)
    request["evidence"][0].pop("title")
    with pytest.raises(AssertionError, match="source title"):
        _candidate_source_bound_response("candidate_1_provider_generation", request, None)

    _executor, _scope, request, _source_text = _request_with_titles(_DISTINCT_TITLES)
    request["evidence"].append({"paper_key": "paper-a", "title": "Conflicting title"})
    with pytest.raises(AssertionError, match="conflicting source titles"):
        _candidate_source_bound_response("candidate_1_provider_generation", request, None)
