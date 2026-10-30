from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from outline.v3_executor import OutlineV3ExecutionError, OutlineV3Executor
from tests.test_current_runtime_full_e2e import (
    _source_bound_topic_response,
    _source_closed_cross_group_content,
)


def _source_claim(paper_key: str, topic_ids: list[str], suffix: str) -> dict[str, Any]:
    return {
        "claim_id": f"synthesis:topic_synthesis:fixture-{suffix}",
        "topic_ids": list(topic_ids),
        "paper_key": paper_key,
        "text": f"Source claim for {paper_key} with its recorded boundary.",
        "source_claim_ids": [f"claim:{paper_key}:{suffix}"],
        "evidence_ids": [f"evidence:{paper_key}:{suffix}"],
        "source_field_ids": [f"field:{paper_key}:finding", f"field:{paper_key}:boundary"],
    }


def _topic_fragment(
    topic_id: str,
    fragment_id: str,
    paper_keys: list[str],
    claims: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "topic_id": topic_id,
        "fragment_id": fragment_id,
        "paper_ids": list(paper_keys),
        "interpretation_context": {
            "fields": [
                {"paper_key": paper_key, "source_field_id": f"field:{paper_key}:{kind}"}
                for paper_key in paper_keys
                for kind in ("finding", "boundary")
            ],
            "dependencies": [],
        },
        "claims": claims,
    }


def _topic_statuses(result: dict[str, Any]) -> dict[str, str]:
    return {
        str(row.get("topic_id") or ""): str(row.get("status") or "")
        for row in result.get("topic_dispositions") or ()
        if isinstance(row, dict)
    }


def _compact_prior_bridge_request(topic_id: str, paper_key: str) -> dict[str, Any]:
    claim = {
        "claim_id": "synthesis:cross_group_comparison:fixture-prior-paper9",
        "topic_ids": [topic_id],
        "paper_keys": [paper_key],
        "text": "The treatment increased the primary outcome by 2 points in the observed controlled sample.",
        "source_claim_ids": ["claim:paper9:finding"],
        "evidence_ids": ["evidence:paper9:finding"],
        "source_field_ids": ["field:paper9:finding", "field:paper9:boundary"],
    }
    fields = [
        {
            "paper_key": paper_key,
            "source_field_id": "field:paper9:finding",
            "source_path": "findings",
            "source_value": "The treatment increased the primary outcome by 2 points.",
            "disposition": "exact",
            "canonical_field": "findings",
            "scope": "paper",
        },
        {
            "paper_key": paper_key,
            "source_field_id": "field:paper9:boundary",
            "source_path": "limitations",
            "source_value": "Observed controlled sample; 30-day follow-up.",
            "disposition": "exact",
            "canonical_field": "limitations",
            "scope": "paper",
        },
    ]
    return {
        "semantic_contract_version": "semantic-evidence-graph-v1",
        "topic_synthesis": [{
            "reducer_id": "cross_group_comparison_provider:reduce:1:1",
            "topic_ids": [topic_id],
            "paper_ids": [paper_key],
            "coverage_ref": {
                "schema_version": "outline-semantic-reducer-coverage-ref/v1",
                "artifact_id": "fixture-only-coverage-ref",
            },
            "semantic_result_ref": {
                "schema_version": "outline-semantic-reducer-result-ref/v1",
                "artifact_id": "fixture-only-result-ref",
            },
            # The enclosing compact item carries the source owner and its
            # source closure. `paper_keys` on the nested bridge are not a
            # substitute for the sealed per-topic membership map.
            "source_claim_ids": ["claim:paper9:finding"],
            "evidence_ids": ["evidence:paper9:finding"],
            "interpretation_context": {"fields": fields, "dependencies": []},
            "semantic_result": {"bridge_claims": [claim], "comparisons": []},
        }],
        "relation_candidates": [],
    }


def test_bridge_fixture_uses_valid_multi_topic_and_multi_fragment_ownership():
    topic_x = "topic:method:x"
    topic_y = "topic:context:y"
    topic_z = "topic:construct:z"
    claim_a = _source_claim("paper-a", [topic_x, topic_z], "a")
    claim_b = _source_claim("paper-b", [topic_x, topic_y], "b")
    claim_c = _source_claim("paper-c", [topic_y, topic_z], "c")
    request = {
        "topic_synthesis": [
            _topic_fragment(topic_x, "fragment-x-a", ["paper-a"], [claim_a]),
            _topic_fragment(topic_x, "fragment-x-b", ["paper-b"], [claim_b]),
            _topic_fragment(topic_y, "fragment-y-bc", ["paper-b", "paper-c"], [claim_c]),
            _topic_fragment(topic_z, "fragment-z-d", ["paper-d"], []),
        ],
        "relation_candidates": [{
            "relation_id": "relation:paper-a-paper-c",
            "paper_keys": ["paper-a", "paper-c"],
        }],
    }

    result = _source_closed_cross_group_content(request)

    assert len(result["bridge_claims"]) == 1
    bridge = result["bridge_claims"][0]
    assert bridge["paper_keys"] == ["paper-a", "paper-c"]
    # Claims mention an extra requested topic, but each source paper owns only
    # one of the two selected topic memberships. The fixture must not attach Z.
    assert bridge["topic_ids"] == sorted([topic_x, topic_y])
    assert bridge["relation_ids"] == ["relation:paper-a-paper-c"]
    assert bridge["source_claim_ids"] == ["claim:paper-a:a", "claim:paper-c:c"]
    assert bridge["evidence_ids"] == ["evidence:paper-a:a", "evidence:paper-c:c"]
    assert bridge["source_field_ids"] == [
        "field:paper-a:boundary", "field:paper-a:finding",
        "field:paper-c:boundary", "field:paper-c:finding",
    ]
    statuses = _topic_statuses(result)
    assert statuses == {topic_x: "integrated", topic_y: "integrated", topic_z: "deferred"}
    assert set(result["processed_topic_ids"]) == {topic_x, topic_y, topic_z}


def test_foreign_topic_claim_is_rejected_and_does_not_mark_its_topic_integrated():
    topic_a = "topic:method:a"
    topic_b = "topic:method:b"
    foreign_claim = _source_claim("paper-a", [topic_b], "foreign-a")
    valid_claim = _source_claim("paper-b", [topic_b], "valid-b")
    request = {
        "topic_synthesis": [
            _topic_fragment(topic_a, "fragment-a", ["paper-a"], [foreign_claim]),
            _topic_fragment(topic_b, "fragment-b", ["paper-b"], [valid_claim]),
        ],
        "relation_candidates": [{
            "relation_id": "relation:paper-a-paper-b",
            "paper_keys": ["paper-a", "paper-b"],
        }],
    }
    fixture_trace: list[dict[str, Any]] = []

    result = _source_closed_cross_group_content(request, fixture_trace=fixture_trace)

    assert result["bridge_claims"]
    assert all("paper-a" not in (claim.get("paper_keys") or ()) for claim in result["bridge_claims"])
    assert all(claim.get("topic_ids") == [topic_b] for claim in result["bridge_claims"])
    statuses = _topic_statuses(result)
    assert statuses == {topic_a: "deferred", topic_b: "integrated"}
    rejected = next(row for row in fixture_trace if row.get("kind") == "cross_group_source_membership")
    assert any(
        row.get("claim_id") == foreign_claim["claim_id"]
        and row.get("reason") == "source paper is not a member of the claim's declared topic set"
        for row in rejected.get("rejected_claim_diagnostics") or ()
    )


def test_final_reducer_preserves_multiple_valid_prior_bridge_owners():
    topic_a = "topic:construct:a"
    topic_b = "topic:method:b"
    topic_c = "topic:context:c"
    prior_ab = {
        "claim_id": "synthesis:cross_group_comparison:fixture-ab",
        "topic_ids": [topic_a, topic_b],
        "paper_keys": ["paper-a", "paper-b"],
        "text": "A bounded source comparison from the child reducer.",
        "source_claim_ids": ["claim:paper-a:ab", "claim:paper-b:ab"],
        "evidence_ids": ["evidence:paper-a:ab", "evidence:paper-b:ab"],
        "source_field_ids": ["field:paper-a:ab", "field:paper-b:ab"],
    }
    prior_bc = {
        "claim_id": "synthesis:cross_group_comparison:fixture-bc",
        "topic_ids": [topic_b, topic_c],
        "paper_keys": ["paper-b", "paper-c"],
        "text": "A second bounded source comparison from the child reducer.",
        "source_claim_ids": ["claim:paper-b:bc", "claim:paper-c:bc"],
        "evidence_ids": ["evidence:paper-b:bc", "evidence:paper-c:bc"],
        "source_field_ids": ["field:paper-b:bc", "field:paper-c:bc"],
    }
    members = {
        topic_a: ["paper-a"],
        topic_b: ["paper-a", "paper-b"],
        topic_c: ["paper-c"],
    }
    fields = [
        {"paper_key": paper, "source_field_id": field_id}
        for paper, field_id in (
            ("paper-a", "field:paper-a:ab"), ("paper-b", "field:paper-b:ab"),
            ("paper-b", "field:paper-b:bc"), ("paper-c", "field:paper-c:bc"),
        )
    ]
    request = {
        "topic_synthesis": [{
            "reducer_id": "cross_group_comparison_provider:reduce:1:4",
            "topic_ids": [topic_a, topic_b, topic_c],
            "paper_ids": ["paper-a", "paper-b", "paper-c"],
            "topic_members_by_id": members,
            "coverage_ref": {"schema_version": "outline-semantic-reducer-coverage-ref/v1"},
            "semantic_result_ref": {"schema_version": "outline-semantic-reducer-result-ref/v1"},
            "interpretation_context": {"fields": fields, "dependencies": []},
            "semantic_result": {
                "bridge_claims": [prior_ab, prior_bc],
                "comparisons": [],
            },
        }],
        "relation_candidates": [],
    }

    result = _source_closed_cross_group_content(
        request, node_id="cross_group_comparison_provider",
    )

    claims = {item["claim_id"]: item for item in result["bridge_claims"]}
    assert set(claims) == {prior_ab["claim_id"], prior_bc["claim_id"]}
    assert claims[prior_ab["claim_id"]]["topic_ids"] == sorted([topic_a, topic_b])
    assert claims[prior_bc["claim_id"]]["topic_ids"] == sorted([topic_b, topic_c])
    assert result["comparisons"]
    statuses = _topic_statuses(result)
    assert statuses == {topic_a: "integrated", topic_b: "integrated", topic_c: "integrated"}
    assert set(result["processed_topic_ids"]) == {topic_a, topic_b, topic_c}


def test_compact_prior_refs_do_not_author_unmapped_topic_membership():
    topic_id = "topic:construct:2771f8cda486"
    paper_key = "rfspaper0009_unknown_author"
    request = _compact_prior_bridge_request(topic_id, paper_key)
    fixture_trace: list[dict[str, Any]] = []

    result = _source_closed_cross_group_content(
        request,
        node_id="cross_group_comparison_provider:reduce:1:2",
        fixture_trace=fixture_trace,
    )

    assert result["bridge_claims"] == []
    assert _topic_statuses(result) == {topic_id: "deferred"}
    membership = next(
        row for row in fixture_trace if row.get("kind") == "cross_group_source_membership"
    )
    assert membership["topic_members_by_id"] == {}
    assert any(
        row.get("claim_id") == "synthesis:cross_group_comparison:fixture-prior-paper9"
        and row.get("reason") == "source paper is not a member of the claim's declared topic set"
        for row in membership["rejected_claim_diagnostics"]
    )


def test_exact_topic_request_trace_preserves_core_and_bridge_membership_and_validator_binding():
    topic_id = "topic:construct:2771f8cda486"
    paper_key = "rfspaper0009_unknown_author"
    members = [
        f"rfspaper{index:04d}_unknown_author"
        for index in (2, 3, 5, 6, 7, 8, 9)
    ]
    trace: list[dict[str, Any]] = []
    _source_bound_topic_response(
        "topic_synthesis_provider:batch:10",
        {
            "topics": [
                {
                    "topic_id": topic_id,
                    "fragment_id": f"{topic_id}:papers:e12f",
                    "paper_ids": members[:5],
                    "bridge_paper_ids": members,
                },
                {
                    "topic_id": topic_id,
                    "fragment_id": f"{topic_id}:papers:c06c",
                    "paper_ids": members[-2:],
                    "bridge_paper_ids": members,
                },
            ],
            "evidence_units": [],
        },
        trace,
    )
    source_topic_trace = next(row for row in trace if row.get("kind") == "topic_source_bound")
    assert source_topic_trace["topic_members_by_id"] == {topic_id: members}

    request = _compact_prior_bridge_request(topic_id, paper_key)
    result = _source_closed_cross_group_content(request, fixture_trace=trace)
    assert len(result["bridge_claims"]) == 1
    assert result["bridge_claims"][0]["paper_keys"] == [paper_key]
    assert result["bridge_claims"][0]["topic_ids"] == [topic_id]
    assert _topic_statuses(result) == {topic_id: "integrated"}

    # Exercise the production validator on the compact result with the exact
    # membership map resolved from the topic request trace. The same response
    # must fail when the map models the incomplete reducer manifest from the
    # captured current07 run (papers 2,3,5,6,7 only).
    validation_request = deepcopy(request)
    compact = validation_request["topic_synthesis"][0]
    compact["topic_members_by_id"] = source_topic_trace["topic_members_by_id"]
    compact.pop("coverage_ref")
    compact.pop("semantic_result_ref")
    executor = object.__new__(OutlineV3Executor)
    executor._validate_semantic_provider_output(
        "cross_group_comparison_provider", validation_request, result,
    )

    compact["topic_members_by_id"] = {topic_id: members[:-2]}
    with pytest.raises(OutlineV3ExecutionError, match=r"bridge_claims\[0\].*outside topic membership"):
        executor._validate_semantic_provider_output(
            "cross_group_comparison_provider", validation_request, result,
        )
