from __future__ import annotations

import copy
import hashlib
from typing import Any

import pytest

from tests.test_current_runtime_full_e2e import (
    _assert_source_bound_fixture_trace,
    _candidate_fragment_trace,
    _ORGANIZATION_TRACE_HEADERS,
    _source_bound_topic_response,
)


def _canonical_topic_request() -> tuple[dict[str, Any], dict[str, Any]]:
    dependency = {
        "primary_claim_id": "source-claim-primary",
        "scope": "paper",
        "required_source_claim_ids": ["source-claim-qualifier"],
        "required_evidence_ids": ["evidence-qualifier"],
        "required_source_field_ids": ["source-field-context"],
    }
    request = {
        "topics": [
            {
                "topic_id": "topic-1",
                "fragment_id": "fragment-1",
                "paper_ids": ["paper-1"],
                "planned_evidence_ids": ["evidence-primary"],
            }
        ],
        "evidence_units": [
            {
                "paper_key": "paper-1",
                "source_summary_hash": "summary-hash-1",
                "study_units": [
                    {
                        "study_id": "study-1",
                        "source_study_id": "study-1",
                        "claims": [
                            {
                                "claim_id": "source-claim-primary",
                                "claim_type": "empirical_finding",
                                "text": "The intervention improved the measured outcome.",
                                "source_locator": "p. 4",
                                "evidence_ids": ["evidence-primary"],
                            },
                            {
                                "claim_id": "source-claim-qualifier",
                                "claim_type": "qualifier",
                                "text": "The result was observed in a controlled context.",
                                "source_locator": "p. 5",
                                "evidence_ids": ["evidence-qualifier"],
                            },
                        ],
                        "interpretation_source_fields": [
                            {
                                "source_field_id": "source-field-context",
                                "canonical_field": "sample_context",
                                "source_value": "a controlled context",
                            }
                        ],
                        # These rows intentionally have no dependency_id: the
                        # canonical interpretation contract binds them by the
                        # primary claim and the exact required source IDs.
                        "interpretation_dependencies": [dependency],
                    }
                ],
            }
        ],
    }
    return request, dependency


def _minimal_candidate_trace() -> dict[str, Any]:
    return {
        "kind": "candidate_source_bound",
        "sections": [
            {
                "source_claim_ids": ["source-claim-primary"],
                "evidence_ids": ["evidence-primary"],
                "source_field_ids": [],
                "qualifier_source_claim_ids": [],
                "qualifier_evidence_ids": [],
                "qualifier_source_field_ids": [],
                "dependency_ids": [],
            }
        ],
    }


def _minimal_fieldless_writer_trace() -> dict[str, Any]:
    task = {
        "writer_task_id": "writer-task-1",
        "source_claim_ids": ["source-claim-primary"],
        "evidence_ids": ["evidence-primary"],
        "source_field_ids": [],
        "qualifier_source_claim_ids": [],
        "qualifier_evidence_ids": [],
        "qualifier_source_field_ids": [],
        "required_qualifier_ids": {
            "source_claim_ids": [],
            "evidence_ids": [],
            "source_field_ids": [],
        },
        "planned_claim": "The supported result improves the outcome.",
        "allowed_ref_ids": ["R001"],
        "required_output_unit_id": "writer-output-1",
    }
    return {
        "kind": "writer_source_bound",
        "schema_version": "writer_task_scope_wire/v1",
        "writer_task_basis_hash": hashlib.sha256(b"writer-task-basis").hexdigest(),
        "task_count": 1,
        "tasks": [task],
        "source_store_kinds": ["source_claim", "evidence"],
        "returned_output_unit_ids": ["writer-output-1"],
        "returned_blocks": [
            {
                "writer_task_id": "writer-task-1",
                "writer_output_unit_id": "writer-output-1",
                "text": "The supported result improves the outcome [[cite_ref:R001]].",
            }
        ],
        "dispositions": [{"disposition": "covered"}],
    }


def _trace_with_valid_topic_closure() -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    request, dependency = _canonical_topic_request()
    fixture_trace: list[dict[str, Any]] = []
    response = _source_bound_topic_response(
        "topic_synthesis_provider_topic-1",
        request,
        fixture_trace,
    )
    fixture_trace.extend((_minimal_candidate_trace(), _minimal_fieldless_writer_trace()))
    return fixture_trace, response, dependency


def test_source_bound_topic_trace_closes_qualifiers_without_fabricated_dependency_ids() -> None:
    fixture_trace, response, dependency = _trace_with_valid_topic_closure()

    topic_row = next(row for row in fixture_trace if row["kind"] == "topic_source_bound")
    closure = topic_row["claim_closures"][0]
    assert "dependency_id" not in closure["dependency_rows"][0]
    assert closure["dependency_rows"] == [dependency]
    assert closure["source_claim_ids"] == [
        "source-claim-primary",
        "source-claim-qualifier",
    ]
    assert [row["claim_id"] for row in closure["source_claim_records"]] == closure[
        "source_claim_ids"
    ]
    assert response["content"]["claims"][0]["source_claim_ids"] == closure[
        "source_claim_ids"
    ]

    writer_row = next(row for row in fixture_trace if row["kind"] == "writer_source_bound")
    assert "source_field" not in writer_row["source_store_kinds"]
    assert writer_row["tasks"][0]["source_field_ids"] == []
    _assert_source_bound_fixture_trace(fixture_trace)


def test_source_bound_trace_rejects_a_missing_required_qualifier() -> None:
    fixture_trace, _response, _dependency = _trace_with_valid_topic_closure()
    topic_row = next(row for row in fixture_trace if row["kind"] == "topic_source_bound")
    topic_row["claim_closures"][0]["qualifier_source_claim_ids"] = []

    with pytest.raises(AssertionError):
        _assert_source_bound_fixture_trace(fixture_trace)


def test_source_bound_trace_requires_field_store_when_writer_task_uses_source_fields() -> None:
    fixture_trace, _response, _dependency = _trace_with_valid_topic_closure()
    writer_row = next(row for row in fixture_trace if row["kind"] == "writer_source_bound")
    writer_row["tasks"][0]["source_field_ids"] = ["source-field-context"]

    with pytest.raises(AssertionError):
        _assert_source_bound_fixture_trace(fixture_trace)


def _valid_fragment_trace() -> list[dict[str, Any]]:
    trace, _, _ = _trace_with_valid_topic_closure()
    trace = [row for row in trace if row["kind"] != "candidate_source_bound"]
    comparison = {
        "schema_version": "candidate-comparison-fragments-request/v1",
        "parent_scope_hash": "parent-hash", "scope_hash": "scope-hash",
        "comparison_scope": {"claim_groups": [{}, {}]},
    }
    output = {
        "schema_version": "candidate-comparison-fragments-output/v1",
        "parent_scope_hash": "parent-hash", "scope_hash": "scope-hash",
        "fragments": [{"group_index": i, "materialization_state": "materialized"} for i in range(2)],
    }
    organization = {
        "schema_version": "candidate-organization-request/v1",
        **{key: key + "-bound" for key in _ORGANIZATION_TRACE_HEADERS},
        "required_group_indices": [0, 1], "required_paper_indices": [0, 1],
    }
    organized = {
        **organization, "schema_version": "candidate-organization-map/v1", "sections": [{}],
    }
    trace.extend((
        _candidate_fragment_trace("comparison-node", comparison, output),
        _candidate_fragment_trace("organization-node", organization, organized),
    ))
    return trace


def test_fragment_trace_requires_exact_schema_bound_candidate_exchange() -> None:
    _assert_source_bound_fixture_trace(_valid_fragment_trace(), require_candidate_source_bound=False)


@pytest.mark.parametrize("corruption", [
    "scope", "candidate", "required_groups", "missing_group", "duplicate_group",
    "missing_organization", "missing_comparison", "empty_sections", "writer_qualifier",
])
def test_fragment_trace_rejects_identity_coverage_or_source_corruption(corruption: str) -> None:
    trace = copy.deepcopy(_valid_fragment_trace())
    comparison = next(row for row in trace if row.get("branch") == "comparison")
    organization = next(row for row in trace if row.get("branch") == "organization")
    if corruption == "scope":
        comparison["response_binding"]["scope_hash"] = "foreign-scope"
    elif corruption == "candidate":
        organization["response_binding"]["candidate_id"] = "foreign-candidate"
    elif corruption == "required_groups":
        organization["response_binding"]["required_group_indices"] = []
    elif corruption == "missing_group":
        comparison["materialized_group_indices"] = [0]
        comparison["fragment_count"] = 1
    elif corruption == "duplicate_group":
        comparison["materialized_group_indices"] = [0, 0]
    elif corruption == "missing_organization":
        trace.remove(organization)
    elif corruption == "missing_comparison":
        trace.remove(comparison)
    elif corruption == "empty_sections":
        organization["section_count"] = 0
    else:
        writer = next(row for row in trace if row["kind"] == "writer_source_bound")
        writer["tasks"][0]["required_qualifier_ids"]["source_claim_ids"] = ["missing-qualifier"]
    with pytest.raises(AssertionError):
        _assert_source_bound_fixture_trace(trace, require_candidate_source_bound=False)


def test_fragment_option_still_checks_every_existing_candidate_row() -> None:
    trace = _valid_fragment_trace()
    candidate = _minimal_candidate_trace()
    candidate["sections"][0]["evidence_ids"] = []
    trace.append(candidate)
    with pytest.raises(AssertionError):
        _assert_source_bound_fixture_trace(trace, require_candidate_source_bound=False)
