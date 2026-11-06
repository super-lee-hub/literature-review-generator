"""Focused regressions for local reducer audit metadata on the provider wire."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from test_outline_v3_semantic_execution import _executor

from outline.v3_executor import (
    SHARED_SYNTHESIS_CONTRACT_VERSION,
    OutlineV3ExecutionError,
    _project_semantic_dependency_templates_request_v1,
)
from runtime.provider_context import ProviderContextProfile
from runtime.provider_runtime import hash_json


def _audit_item(reducer_id: str) -> dict[str, Any]:
    return {
        "reducer_id": reducer_id,
        "topic_ids": ["topic:source-boundary"],
        "paper_ids": ["paper:source"],
        "topic_members_by_id": {"topic:source-boundary": ["paper:source"]},
        "coverage_ref": {
            "schema_version": "outline-semantic-reducer-coverage-ref/v1",
            "artifact_id": f"coverage:{reducer_id}",
            "registry_file_hash": "b" * 64,
            "artifact_content_hash": "c" * 64,
            "node_id": reducer_id,
            "manifest_hash": "d" * 64,
            "membership_hash": "e" * 64,
            "topic_count": 1,
        },
        "semantic_result_ref": {
            "schema_version": "outline-semantic-reducer-result-ref/v1",
            "artifact_id": f"result:{reducer_id}",
            "registry_file_hash": "f" * 64,
            "artifact_content_hash": "1" * 64,
            "provider_result_hash": "2" * 64,
        },
        "semantic_result": {
            "bridge_claims": [{
                "claim_id": "synthesis:cross_group_comparison:prior",
                "topic_ids": ["topic:source-boundary"],
                "paper_key": "paper:source",
                "text": "The observed result remains bounded by the reported sample.",
                "source_claim_ids": ["claim:source:finding", "claim:source:limit"],
                "evidence_ids": ["evidence:source:finding", "evidence:source:limit"],
                "source_field_ids": ["field:source:sample"],
            }],
            "comparisons": [],
        },
        "interpretation_context": {
            "fields": [{
                "source_field_id": "field:source:sample",
                "source_path": "methods.sample",
                "source_value": "The result applies only to the recorded sample.",
                "disposition": "exact",
                "canonical_field": "sample boundary",
                "scope": "paper",
                "study_id": "",
                "paper_key": "paper:source",
                "owner_study_id": "",
            }],
            "dependencies": [{
                "dependency_id": "dependency:source:finding",
                "primary_claim_id": "claim:source:finding",
                "primary_evidence_ids": ["evidence:source:finding"],
                "required_source_claim_ids": ["claim:source:limit"],
                "required_evidence_ids": ["evidence:source:limit"],
                "required_source_field_ids": ["field:source:sample"],
                "scope": "paper",
                "study_id": "",
                "paper_key": "paper:source",
                "owner_study_id": "",
                "reason": "retain the source-reported sample boundary",
            }],
        },
        "reduction_level": 2,
        "local_lineage_hash": "a" * 64,
    }


def _sealed_group_item() -> dict[str, Any]:
    item = _audit_item("cross_group_comparison_provider:reduce:2:1")
    paper_ids = ["paper:a", "paper:b", "paper:c"]
    item.pop("topic_members_by_id")
    item["paper_ids"] = paper_ids
    item["coverage_ref"]["paper_count"] = len(paper_ids)
    prior_claim = item["semantic_result"]["bridge_claims"][0]
    prior_claim["paper_key"] = paper_ids[0]
    prior_claim["paper_keys"] = [paper_ids[0], paper_ids[-1]]
    return item


def test_matching_reducer_projection_omits_only_audit_hash_and_saves_64_profile_tokens(
    tmp_path: Path,
) -> None:
    items = [_audit_item(f"cross_group_comparison_provider:reduce:2:{index}") for index in (1, 2)]
    canonical = {
        "task": "cross_group_comparison_bounded_reduction",
        "topic_synthesis": items,
        "relation_candidates": [],
        "questions": ["Retain the source-supported comparison and its qualifier."],
    }
    original = deepcopy(canonical)

    wire = _project_semantic_dependency_templates_request_v1(canonical)

    expected_wire = deepcopy(original)
    for item in expected_wire["topic_synthesis"]:
        item.pop("local_lineage_hash")
    assert wire == expected_wire
    assert canonical == original
    assert all(
        item["interpretation_context"] == expected["interpretation_context"]
        and item["semantic_result"] == expected["semantic_result"]
        and item["coverage_ref"] == expected["coverage_ref"]
        and item["semantic_result_ref"] == expected["semantic_result_ref"]
        for item, expected in zip(wire["topic_synthesis"], original["topic_synthesis"], strict=True)
    )

    executor = _executor(tmp_path / "profile", stability_mode="off")
    profile = executor._node_route("candidate_1_provider_generation").profile
    assert isinstance(profile, ProviderContextProfile)
    before = int(profile.estimate_request(canonical)["estimated_input_tokens"])
    after = int(profile.estimate_request(wire)["estimated_input_tokens"])
    assert before - after == 64
    assert before - after >= 53


def test_sealed_reducer_group_projection_omits_only_broad_paper_ids() -> None:
    item = _sealed_group_item()
    canonical = {"topic_synthesis": [item]}
    original = deepcopy(canonical)

    wire = _project_semantic_dependency_templates_request_v1(canonical)

    expected_wire = deepcopy(original)
    wire_item = expected_wire["topic_synthesis"][0]
    wire_item.pop("local_lineage_hash")
    wire_item.pop("paper_ids")
    assert wire == expected_wire
    assert canonical == original
    projected_item = wire["topic_synthesis"][0]
    canonical_item = original["topic_synthesis"][0]
    assert projected_item["coverage_ref"] == canonical_item["coverage_ref"]
    assert projected_item["semantic_result_ref"] == canonical_item["semantic_result_ref"]
    assert projected_item["semantic_result"] == canonical_item["semantic_result"]
    assert projected_item["semantic_result"]["bridge_claims"][0]["paper_key"] == "paper:a"
    assert projected_item["semantic_result"]["bridge_claims"][0]["paper_keys"] == [
        "paper:a",
        "paper:c",
    ]
    assert projected_item["interpretation_context"] == canonical_item["interpretation_context"]


@pytest.mark.parametrize(
    "invalid_binding",
    [
        "paper_count_missing",
        "paper_count_boolean",
        "paper_count_mismatch",
        "paper_ids_empty",
        "paper_ids_duplicate",
        "paper_ids_not_array",
        "coverage_unknown_schema",
        "coverage_nonmatching_node",
    ],
)
def test_unsealed_or_malformed_group_authority_keeps_broad_paper_ids(
    invalid_binding: str,
) -> None:
    item = _sealed_group_item()
    if invalid_binding == "paper_count_missing":
        item["coverage_ref"].pop("paper_count")
    elif invalid_binding == "paper_count_boolean":
        item["coverage_ref"]["paper_count"] = True
    elif invalid_binding == "paper_count_mismatch":
        item["coverage_ref"]["paper_count"] = 2
    elif invalid_binding == "paper_ids_empty":
        item["paper_ids"] = []
        item["coverage_ref"]["paper_count"] = 0
    elif invalid_binding == "paper_ids_duplicate":
        item["paper_ids"] = ["paper:a", "paper:a", "paper:c"]
        item["coverage_ref"]["paper_count"] = 3
    elif invalid_binding == "paper_ids_not_array":
        item["paper_ids"] = "paper:a"
        item["coverage_ref"]["paper_count"] = 1
    elif invalid_binding == "coverage_unknown_schema":
        item["coverage_ref"]["schema_version"] = "outline-semantic-reducer-coverage-ref/v2"
    elif invalid_binding == "coverage_nonmatching_node":
        item["coverage_ref"]["node_id"] = "cross_group_comparison_provider:reduce:2:other"

    canonical = {"topic_synthesis": [item]}
    expected = deepcopy(canonical)
    expected_item = expected["topic_synthesis"][0]
    refs_match = invalid_binding not in {
        "coverage_unknown_schema",
        "coverage_nonmatching_node",
    }
    if refs_match:
        expected_item.pop("local_lineage_hash")

    assert _project_semantic_dependency_templates_request_v1(canonical) == expected
    assert "paper_ids" in canonical["topic_synthesis"][0]


@pytest.mark.parametrize(
    "invalid_ref",
    [
        "coverage_not_mapping",
        "coverage_unknown_schema",
        "coverage_nonmatching_node",
        "coverage_missing_both_node_ids",
        "coverage_empty_node_ids",
        "coverage_blank_node_ids",
        "coverage_nonstring_node_ids",
        "result_not_mapping",
        "result_unknown_schema",
    ],
)
def test_invalid_or_unmatched_refs_keep_local_lineage_hash(invalid_ref: str) -> None:
    item = _audit_item("cross_group_comparison_provider:reduce:2:1")
    if invalid_ref == "coverage_not_mapping":
        item["coverage_ref"] = []
    elif invalid_ref == "coverage_unknown_schema":
        item["coverage_ref"]["schema_version"] = "outline-semantic-reducer-coverage-ref/v2"
    elif invalid_ref == "coverage_nonmatching_node":
        item["coverage_ref"]["node_id"] = "cross_group_comparison_provider:reduce:2:other"
    elif invalid_ref == "coverage_missing_both_node_ids":
        item.pop("reducer_id")
        item["coverage_ref"].pop("node_id")
    elif invalid_ref == "coverage_empty_node_ids":
        item["reducer_id"] = ""
        item["coverage_ref"]["node_id"] = ""
    elif invalid_ref == "coverage_blank_node_ids":
        item["reducer_id"] = "  "
        item["coverage_ref"]["node_id"] = "  "
    elif invalid_ref == "coverage_nonstring_node_ids":
        item["reducer_id"] = 123
        item["coverage_ref"]["node_id"] = 123
    elif invalid_ref == "result_not_mapping":
        item["semantic_result_ref"] = None
    elif invalid_ref == "result_unknown_schema":
        item["semantic_result_ref"]["schema_version"] = "outline-semantic-reducer-result-ref/v2"

    canonical = {"topic_synthesis": [item]}

    assert _project_semantic_dependency_templates_request_v1(canonical) == canonical
    assert canonical["topic_synthesis"][0]["local_lineage_hash"] == "a" * 64


def _register_reducer_refs(
    executor: Any,
    reducer_id: str,
    *,
    paper_ids: tuple[str, ...] = ("paper:source",),
) -> dict[str, Any]:
    topic_id = "topic:source-boundary"
    papers = list(paper_ids)
    assert papers and all(isinstance(value, str) and value.strip() for value in papers)
    primary_paper_id = papers[0]
    manifest = {
        "schema_version": "outline-semantic-reducer-input/v1",
        "node_id": reducer_id,
        "reduction_level": 1,
        "topic_ids": [topic_id],
        "fragment_ids": ["fragment:source-boundary"],
        "result_ids": ["result:source-boundary"],
        "relation_ids": [],
        "paper_ids": papers,
        "topic_members_by_id": {topic_id: papers},
        "fragment_members_by_id": {"fragment:source-boundary": papers},
        "relation_members_by_id": {},
        "source_field_ids": ["field:source:sample"],
        "source_input_hash": "3" * 64,
    }
    coverage_record = executor._persist_semantic_reducer_input_manifest(
        reducer_id, manifest, dependency_hashes={"source": "4" * 64}
    )
    membership_maps = {
        key: manifest.get(key) or {}
        for key in ("topic_members_by_id", "fragment_members_by_id", "relation_members_by_id")
    }
    coverage_ref = {
        "schema_version": "outline-semantic-reducer-coverage-ref/v1",
        "artifact_id": coverage_record.artifact_id,
        "registry_file_hash": coverage_record.content_hash,
        "artifact_content_hash": coverage_record.metadata["content_hash"],
        "node_id": reducer_id,
        "manifest_hash": hash_json(manifest),
        "membership_hash": hash_json(membership_maps),
        "topic_count": len(manifest["topic_ids"]),
        "fragment_count": len(manifest["fragment_ids"]),
        "result_count": len(manifest["result_ids"]),
        "relation_count": len(manifest["relation_ids"]),
        "paper_count": len(manifest["paper_ids"]),
    }
    prior_claim = {
        "claim_id": "synthesis:cross_group_comparison:prior",
        "topic_ids": [topic_id],
        "paper_key": primary_paper_id,
        "paper_keys": [primary_paper_id, papers[-1]],
        "text": "A prior source-bound synthesis statement retains its exact sample limit.",
        "source_claim_ids": ["claim:source:finding", "claim:source:limit"],
        "evidence_ids": ["evidence:source:finding", "evidence:source:limit"],
        "source_field_ids": ["field:source:sample"],
    }
    semantic_result = {"bridge_claims": [prior_claim], "comparisons": []}
    result_record = executor._persist_semantic_provider_output(
        reducer_id, semantic_result, dependency_hashes={"source": "4" * 64}
    )
    return {
        "reducer_id": reducer_id,
        "topic_ids": [topic_id],
        "paper_ids": papers,
        "coverage_ref": coverage_ref,
        "semantic_result_ref": {
            "schema_version": "outline-semantic-reducer-result-ref/v1",
            "artifact_id": result_record.artifact_id,
            "registry_file_hash": result_record.content_hash,
            "artifact_content_hash": result_record.metadata["content_hash"],
            "provider_result_hash": hash_json(semantic_result),
        },
        "semantic_result": semantic_result,
        "source_claim_ids": ["claim:source:finding", "claim:source:limit"],
        "evidence_ids": ["evidence:source:finding", "evidence:source:limit"],
        "interpretation_context": {
            "fields": [{
                "source_field_id": "field:source:sample",
                "source_path": "methods.sample",
                "source_value": "The result applies only to the recorded sample.",
                "disposition": "exact",
                "canonical_field": "sample boundary",
                "scope": "paper",
                "study_id": "",
                "paper_key": primary_paper_id,
                "owner_study_id": "",
            }],
            "dependencies": [{
                "dependency_id": "dependency:source:finding",
                "primary_claim_id": "claim:source:finding",
                "primary_evidence_ids": ["evidence:source:finding"],
                "required_source_claim_ids": ["claim:source:limit"],
                "required_evidence_ids": ["evidence:source:limit"],
                "required_source_field_ids": ["field:source:sample"],
                "scope": "paper",
                "study_id": "",
                "paper_key": primary_paper_id,
                "owner_study_id": "",
                "reason": "retain the source-reported sample boundary",
            }],
        },
        "reduction_level": 1,
        "local_lineage_hash": "a" * 64,
    }


@pytest.mark.parametrize("compact_source_wire", [False, True])
def test_bounded_provider_validates_expanded_item_with_audit_hash_but_sends_wire_projection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    compact_source_wire: bool,
) -> None:
    from outline.semantic_request_wire import (
        WIRE_KEY,
        expand_semantic_request_wire,
        pack_semantic_request_wire,
    )

    wire_requests: list[dict[str, Any]] = []

    def provider(_node_id: str, request: Mapping[str, Any]) -> Mapping[str, Any]:
        wire_requests.append(deepcopy(dict(request)))
        return {
            "status": "success",
            "content": {
                "comparisons": [],
                "bridge_claims": [],
                "topic_dispositions": [{
                    "topic_id": "topic:source-boundary",
                    "status": "unresolved",
                    "reason": "The supplied source scope does not establish a final comparison.",
                }],
                "unresolved_questions": ["Which source comparison is supported?"],
            },
        }

    executor = _executor(
        tmp_path / "bounded",
        provider=provider,
        stability_mode="off",
        max_source_prompt_tokens=8_000,
    )
    paper_ids = ("paper:a", "paper:b", "paper:c")
    item = _register_reducer_refs(
        executor,
        "cross_group_comparison_provider:reduce:1:1",
        paper_ids=paper_ids,
    )
    request = {
        "task": "cross_group_comparison_bounded_reduction",
        "node_id": "cross_group_comparison",
        "semantic_contract_version": "semantic-evidence-graph-v2",
        "shared_synthesis_contract_version": SHARED_SYNTHESIS_CONTRACT_VERSION,
        "hierarchy": {
            "level": "bounded_semantic_reduction",
            "stage": "cross_group_comparison",
            "reduction_level": 2,
            "group_index": 1,
        },
        "questions": ["Retain the exact source boundary."],
        "topic_synthesis": [item],
        "relation_candidates": [],
        "output_contract": {},
    }
    if compact_source_wire:
        request["task"] = "substantive_cross_group_comparison"
        request["hierarchy"]["level"] = "bounded_semantic_final_fold"
        request["topic_synthesis"].extend(
            _register_reducer_refs(
                executor, f"cross_group_comparison_provider:reduce:1:{index}",
                paper_ids=paper_ids,
            ) for index in range(2, 5)
        )
    original_request = deepcopy(request)
    if compact_source_wire:
        projected = _project_semantic_dependency_templates_request_v1(request)
        projected = executor._project_sealed_semantic_topic_memberships(projected)
        attached = executor._attach_prompt_authority(
            "cross_group_comparison_provider:reduce:2:1", projected,
        )
        profile = executor._node_route("candidate_1_provider_generation").profile
        original_tokens = profile.estimate_tokens(attached)
        packed_tokens = profile.estimate_tokens(pack_semantic_request_wire(attached))
        assert packed_tokens < original_tokens
        executor.max_source_prompt_tokens = (original_tokens + packed_tokens) // 2
    original_validator = executor._validate_semantic_provider_output
    expanded_requests: list[dict[str, Any]] = []

    def capture_expanded_validation(
        node_id: str,
        expanded_request: Mapping[str, Any],
        result: Mapping[str, Any],
    ) -> None:
        expanded_requests.append(deepcopy(dict(expanded_request)))
        expanded_item = expanded_request["topic_synthesis"][0]
        assert expanded_item["local_lineage_hash"] == "a" * 64
        assert expanded_item["paper_ids"] == list(paper_ids)
        coverage_manifest = executor._load_semantic_reducer_coverage_ref(expanded_item)
        assert coverage_manifest is not None
        assert coverage_manifest["paper_ids"] == list(paper_ids)
        original_validator(node_id, expanded_request, result)

    monkeypatch.setattr(
        executor,
        "_validate_semantic_provider_output",
        capture_expanded_validation,
    )

    result = executor._run_bounded_semantic_provider_call(
        "cross_group_comparison_provider:reduce:2:1",
        request,
        {"topic_synthesis": "sealed-source-input"},
    )

    assert request == original_request
    assert len(wire_requests) == len(expanded_requests) == 1
    if compact_source_wire:
        assert WIRE_KEY in wire_requests[0]
        assert profile.estimate_tokens(wire_requests[0]) <= executor.max_source_prompt_tokens
    else:
        assert WIRE_KEY not in wire_requests[0]
    decoded_wire = expand_semantic_request_wire(wire_requests[0])
    wire_item = decoded_wire["topic_synthesis"][0]
    expanded_item = expanded_requests[0]["topic_synthesis"][0]
    assert "local_lineage_hash" not in wire_item
    assert "paper_ids" not in wire_item
    assert wire_item["coverage_ref"] == expanded_item["coverage_ref"]
    assert wire_item["semantic_result_ref"] == expanded_item["semantic_result_ref"]
    assert wire_item["semantic_result"] == expanded_item["semantic_result"]
    assert wire_item["semantic_result"]["bridge_claims"][0]["paper_key"] == "paper:a"
    assert wire_item["semantic_result"]["bridge_claims"][0]["paper_keys"] == [
        "paper:a",
        "paper:c",
    ]
    assert wire_item["source_claim_ids"] == expanded_item["source_claim_ids"]
    assert wire_item["evidence_ids"] == expanded_item["evidence_ids"]
    assert wire_item["interpretation_context"] == expanded_item["interpretation_context"]
    if compact_source_wire:
        assert wire_item["topic_members_by_id"] == {
            "topic:source-boundary": ["paper:a", "paper:c"],
        }
    assert expanded_item["local_lineage_hash"] == "a" * 64
    assert result["topic_dispositions"][0]["status"] == "unresolved"
    receipts = executor._receipt_ledger.list_receipts()
    assert len(receipts) == 1
    assert receipts[0].input_hash == hash_json(wire_requests[0])


def test_source_wire_that_still_exceeds_cap_never_reaches_provider(tmp_path: Path) -> None:
    calls = []

    def provider(node_id: str, _request: Mapping[str, Any]) -> Mapping[str, Any]:
        calls.append(node_id)
        raise AssertionError("an over-cap semantic request must not reach transport")

    executor = _executor(tmp_path, provider=provider, stability_mode="off", max_source_prompt_tokens=100)
    items = [
        _register_reducer_refs(executor, f"cross_group_comparison_provider:reduce:1:{index}")
        for index in (1, 2)
    ]
    with pytest.raises(OutlineV3ExecutionError, match="BLOCKED_BUDGET"):
        executor._run_bounded_semantic_provider_call(
            "cross_group_comparison_provider",
            {
                "task": "substantive_cross_group_comparison",
                "node_id": "cross_group_comparison",
                "semantic_contract_version": "semantic-evidence-graph-v2",
                "shared_synthesis_contract_version": SHARED_SYNTHESIS_CONTRACT_VERSION,
                "topic_synthesis": items,
                "questions": ["Preserve the complete source closure."],
                "relation_candidates": [], "output_contract": {},
            },
            {"topic_synthesis": "sealed-source-input"},
        )
    assert calls == []


def test_bounded_group_projection_still_rejects_foreign_provider_claim_owner(
    tmp_path: Path,
) -> None:
    wire_requests: list[dict[str, Any]] = []
    foreign_claim_id = "synthesis:cross_group_comparison:foreign-owner"

    def provider(_node_id: str, request: Mapping[str, Any]) -> Mapping[str, Any]:
        wire_requests.append(deepcopy(dict(request)))
        return {
            "status": "success",
            "content": {
                "comparisons": [],
                "bridge_claims": [{
                    "claim_id": foreign_claim_id,
                    "topic_ids": ["topic:source-boundary"],
                    "paper_key": "paper:foreign",
                    "paper_keys": ["paper:foreign"],
                    "text": "A foreign paper claim must remain outside this source scope.",
                    "source_claim_ids": ["claim:source:finding"],
                    "evidence_ids": ["evidence:source:finding"],
                    "source_field_ids": ["field:source:sample"],
                }],
                "topic_dispositions": [{
                    "topic_id": "topic:source-boundary",
                    "status": "integrated",
                    "synthesis_claim_ids": [foreign_claim_id],
                }],
                "unresolved_questions": [],
            },
        }

    executor = _executor(
        tmp_path / "foreign-owner",
        provider=provider,
        stability_mode="off",
        max_source_prompt_tokens=8_000,
    )
    item = _register_reducer_refs(
        executor,
        "cross_group_comparison_provider:reduce:1:1",
        paper_ids=("paper:a", "paper:b", "paper:c"),
    )
    request = {
        "task": "cross_group_comparison_bounded_reduction",
        "node_id": "cross_group_comparison",
        "semantic_contract_version": "semantic-evidence-graph-v2",
        "shared_synthesis_contract_version": SHARED_SYNTHESIS_CONTRACT_VERSION,
        "hierarchy": {
            "level": "bounded_semantic_reduction",
            "stage": "cross_group_comparison",
            "reduction_level": 2,
            "group_index": 1,
        },
        "questions": ["Retain the exact source boundary."],
        "topic_synthesis": [item],
        "relation_candidates": [],
        "output_contract": {},
    }

    with pytest.raises(
        OutlineV3ExecutionError,
        match="identities outside its evidence contract",
    ):
        executor._run_bounded_semantic_provider_call(
            "cross_group_comparison_provider:reduce:2:1",
            request,
            {"topic_synthesis": "sealed-source-input"},
        )

    assert len(wire_requests) == 1
    wire_item = wire_requests[0]["topic_synthesis"][0]
    assert "paper_ids" not in wire_item
    assert wire_item["coverage_ref"]["paper_count"] == 3
    assert wire_requests[0]["topic_synthesis"][0]["interpretation_context"] == item[
        "interpretation_context"
    ]
