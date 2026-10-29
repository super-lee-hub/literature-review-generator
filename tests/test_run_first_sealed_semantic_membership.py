from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from test_outline_v3_semantic_execution import _executor
from test_run_first_semantic_wire_audit import _register_reducer_refs

from outline.v3_executor import (
    SHARED_SYNTHESIS_CONTRACT_VERSION,
    OutlineV3ExecutionError,
)
from runtime.provider_runtime import hash_json

_TOPIC_ID = "topic:source-boundary"
_PAPER_A = "paper:a"


def _semantic_result(
    *,
    paper_key: str,
    topic_id: str = _TOPIC_ID,
    paper_keys: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "bridge_claims": [{
            "claim_id": "synthesis:cross_group_comparison:prior",
            "topic_id": topic_id,
            "topic_ids": [topic_id],
            "paper_key": paper_key,
            "paper_keys": list(paper_keys or [paper_key]),
            "text": "The prior comparison retains its source-reported boundary.",
            "source_claim_ids": ["claim:source:finding", "claim:source:limit"],
            "evidence_ids": ["evidence:source:finding", "evidence:source:limit"],
            "source_field_ids": ["field:source:sample"],
        }],
        "comparisons": [],
    }


def _bind_semantic_result(
    executor: Any,
    item: dict[str, Any],
    result: Mapping[str, Any],
) -> None:
    record = executor._persist_semantic_provider_output(
        str(item["reducer_id"]),
        result,
        dependency_hashes={"source": "4" * 64},
    )
    item["semantic_result"] = dict(result)
    item["semantic_result_ref"] = {
        "schema_version": "outline-semantic-reducer-result-ref/v1",
        "artifact_id": record.artifact_id,
        "registry_file_hash": record.content_hash,
        "artifact_content_hash": str(record.metadata["content_hash"]),
        "provider_result_hash": hash_json(dict(result)),
    }


def _set_context_owner(item: dict[str, Any], paper_key: str) -> None:
    context = item["interpretation_context"]
    for field in context["fields"]:
        field["paper_key"] = paper_key
    for dependency in context["dependencies"]:
        dependency["paper_key"] = paper_key


def _rebind_fixture_refs(
    executor: Any,
    item: dict[str, Any],
    manifest: Mapping[str, Any],
    *,
    suffix: str,
) -> None:
    reducer_id = f"{item['reducer_id']}:{suffix}"
    bound_manifest = {**dict(manifest), "node_id": reducer_id}
    manifest_record = executor._persist_semantic_reducer_input_manifest(
        reducer_id,
        bound_manifest,
        dependency_hashes={"source": "4" * 64},
    )
    membership_maps = {
        key: bound_manifest.get(key) or {}
        for key in ("topic_members_by_id", "fragment_members_by_id", "relation_members_by_id")
    }
    item["reducer_id"] = reducer_id
    item["coverage_ref"] = {
        "schema_version": "outline-semantic-reducer-coverage-ref/v1",
        "artifact_id": manifest_record.artifact_id,
        "registry_file_hash": manifest_record.content_hash,
        "artifact_content_hash": str(manifest_record.metadata["content_hash"]),
        "node_id": reducer_id,
        "manifest_hash": hash_json(bound_manifest),
        "membership_hash": hash_json(membership_maps),
        "topic_count": len(bound_manifest.get("topic_ids") or ()),
        "fragment_count": len(bound_manifest.get("fragment_ids") or ()),
        "result_count": len(bound_manifest.get("result_ids") or ()),
        "relation_count": len(bound_manifest.get("relation_ids") or ()),
        "paper_count": len(bound_manifest.get("paper_ids") or ()),
    }
    _bind_semantic_result(executor, item, item["semantic_result"])


def _sealed_request(items: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "task": "substantive_cross_group_comparison",
        "node_id": "cross_group_comparison",
        "semantic_contract_version": "semantic-evidence-graph-v2",
        "shared_synthesis_contract_version": SHARED_SYNTHESIS_CONTRACT_VERSION,
        "hierarchy": {
            "level": "bounded_semantic_final_fold",
            "stage": "cross_group_comparison",
            "reduction_level": 2,
        },
        "questions": ["Retain each source boundary."],
        "topic_synthesis": items,
        "relation_candidates": [],
        "output_contract": {},
    }


def _cross_result(
    *,
    topic_id: str,
    status_topic_id: str = _TOPIC_ID,
    paper_key: str = _PAPER_A,
) -> dict[str, Any]:
    claim_id = "synthesis:cross_group_comparison:final"
    return {
        "comparisons": [],
        "bridge_claims": [{
            "claim_id": claim_id,
            "topic_id": topic_id,
            "topic_ids": [topic_id],
            "paper_key": paper_key,
            "paper_keys": [paper_key],
            "text": "The comparison preserves the recorded sample boundary.",
            "source_claim_ids": ["claim:source:finding", "claim:source:limit"],
            "evidence_ids": ["evidence:source:finding", "evidence:source:limit"],
            "source_field_ids": ["field:source:sample"],
        }],
        "topic_dispositions": [{
            "topic_id": status_topic_id,
            "status": "integrated",
            "synthesis_claim_ids": [claim_id],
        }],
        "unresolved_questions": [],
    }


def test_sealed_membership_projection_is_per_item_and_uses_manifest_topic_keys(
    tmp_path: Path,
) -> None:
    executor = _executor(tmp_path, stability_mode="off")
    first = _register_reducer_refs(
        executor,
        "cross_group_comparison_provider:reduce:1:1",
        paper_ids=("paper:a", "paper:b"),
    )
    second = _register_reducer_refs(
        executor,
        "cross_group_comparison_provider:reduce:1:2",
        paper_ids=("paper:a", "paper:c"),
    )
    _bind_semantic_result(
        executor, first, _semantic_result(paper_key="paper:a", paper_keys=["paper:a"]),
    )
    _bind_semantic_result(
        executor,
        second,
        _semantic_result(
            paper_key="paper:c", topic_id="topic:untrusted-claim-topic", paper_keys=["paper:c"],
        ),
    )
    _set_context_owner(first, "paper:a")
    _set_context_owner(second, "paper:c")
    request = _sealed_request([first, second])
    original = deepcopy(request)

    projected = executor._project_sealed_semantic_topic_memberships(request)

    assert request == original
    first_projection, second_projection = projected["topic_synthesis"]
    assert first_projection["topic_members_by_id"] == {_TOPIC_ID: ["paper:a"]}
    assert second_projection["topic_members_by_id"] == {_TOPIC_ID: ["paper:c"]}
    assert "topic:untrusted-claim-topic" not in second_projection["topic_members_by_id"]


@pytest.mark.parametrize("foreign_owner", ["semantic_result", "interpretation_context"])
def test_sealed_membership_projection_rejects_foreign_owners(
    tmp_path: Path,
    foreign_owner: str,
) -> None:
    executor = _executor(tmp_path, stability_mode="off")
    item = _register_reducer_refs(
        executor,
        "cross_group_comparison_provider:reduce:1:1",
        paper_ids=("paper:a", "paper:b"),
    )
    if foreign_owner == "semantic_result":
        _bind_semantic_result(
            executor,
            item,
            _semantic_result(paper_key="paper:foreign"),
        )
    else:
        _set_context_owner(item, "paper:foreign")

    with pytest.raises(OutlineV3ExecutionError, match="outside its coverage manifest"):
        executor._project_sealed_semantic_topic_memberships(_sealed_request([item]))


@pytest.mark.parametrize(
    ("reference", "field"),
    [("coverage_ref", "manifest_hash"), ("semantic_result_ref", "provider_result_hash")],
)
def test_sealed_membership_projection_rejects_tampered_registry_refs(
    tmp_path: Path,
    reference: str,
    field: str,
) -> None:
    executor = _executor(tmp_path, stability_mode="off")
    item = _register_reducer_refs(
        executor,
        "cross_group_comparison_provider:reduce:1:1",
        paper_ids=("paper:a",),
    )
    item[reference][field] = "0" * 64

    with pytest.raises(OutlineV3ExecutionError, match="reference|manifest|hash differs"):
        executor._project_sealed_semantic_topic_memberships(_sealed_request([item]))


@pytest.mark.parametrize("malformation", ["missing_topic_key", "foreign_manifest_member"])
def test_sealed_membership_projection_rejects_internally_hashed_inconsistent_manifests(
    tmp_path: Path,
    malformation: str,
) -> None:
    executor = _executor(tmp_path, stability_mode="off")
    item = _register_reducer_refs(
        executor,
        "cross_group_comparison_provider:reduce:1:1",
        paper_ids=("paper:a",),
    )
    manifest = executor._load_semantic_reducer_coverage_ref(item)
    assert manifest is not None
    if malformation == "missing_topic_key":
        manifest["topic_members_by_id"] = {}
    else:
        manifest["topic_members_by_id"][_TOPIC_ID].append("paper:foreign")
    _rebind_fixture_refs(executor, item, manifest, suffix=malformation)

    # Both Registry references and their hashes are freshly rebound to this
    # malformed payload. Rejection must come from cohort/map validation.
    with pytest.raises(
        OutlineV3ExecutionError,
        match="membership is inconsistent with its coverage cohort",
    ):
        executor._project_sealed_semantic_topic_memberships(_sealed_request([item]))


def test_validator_does_not_learn_a_foreign_topic_from_a_registry_bound_nested_claim(
    tmp_path: Path,
) -> None:
    executor = _executor(tmp_path, stability_mode="off")
    item = _register_reducer_refs(
        executor,
        "cross_group_comparison_provider:reduce:1:1",
        paper_ids=("paper:a",),
    )
    # This claim is genuinely present in the referenced Registry provider
    # result, but the verified coverage manifest contains only _TOPIC_ID.
    _bind_semantic_result(
        executor,
        item,
        _semantic_result(paper_key="paper:a", topic_id="topic:untrusted-claim-topic"),
    )
    _set_context_owner(item, "paper:a")
    request = executor._project_sealed_semantic_topic_memberships(
        _sealed_request([item]),
    )

    valid = _cross_result(topic_id=_TOPIC_ID)
    executor._validate_semantic_provider_output(
        "cross_group_comparison_provider:reduce:2:1", request, valid,
    )

    foreign_topic_result = _cross_result(topic_id="topic:untrusted-claim-topic")
    foreign_topic_result["topic_dispositions"] = [{
        "topic_id": _TOPIC_ID,
        "status": "unresolved",
        "reason": "The untrusted topic association is not supported by the sealed manifest.",
    }]
    with pytest.raises(OutlineV3ExecutionError, match="outside its evidence contract"):
        executor._validate_semantic_provider_output(
            "cross_group_comparison_provider:reduce:2:1", request, foreign_topic_result,
        )


def test_validator_rejects_an_explicit_topic_map_that_expands_the_sealed_manifest(
    tmp_path: Path,
) -> None:
    executor = _executor(tmp_path, stability_mode="off")
    item = _register_reducer_refs(
        executor,
        "cross_group_comparison_provider:reduce:1:1",
        paper_ids=("paper:a",),
    )
    _set_context_owner(item, "paper:a")
    request = _sealed_request([item])
    request["topic_synthesis"][0]["topic_members_by_id"] = {
        _TOPIC_ID: ["paper:a", "paper:foreign"],
    }

    with pytest.raises(OutlineV3ExecutionError, match="exceeds its sealed manifest"):
        executor._validate_semantic_provider_output(
            "cross_group_comparison_provider:reduce:2:1",
            request,
            _cross_result(topic_id=_TOPIC_ID),
        )


@pytest.mark.parametrize("missing_ref", ["coverage_ref", "semantic_result_ref"])
@pytest.mark.parametrize("task", ["substantive_cross_group_comparison", "cross_group_comparison_bounded_reduction"])
def test_final_fold_missing_a_sealed_reference_fails_before_provider_callback(
    tmp_path: Path,
    missing_ref: str,
    task: str,
) -> None:
    calls: list[str] = []

    def provider(node_id: str, _request: Mapping[str, Any]) -> Mapping[str, Any]:
        calls.append(node_id)
        raise AssertionError("an unsealed final-fold item must not reach the provider")

    executor = _executor(
        tmp_path,
        provider=provider,
        stability_mode="off",
        max_source_prompt_tokens=8_000,
    )
    item = _register_reducer_refs(
        executor,
        "cross_group_comparison_provider:reduce:1:1",
        paper_ids=("paper:a",),
    )
    item.pop(missing_ref)
    request = _sealed_request([item])
    request["task"] = task

    with pytest.raises(OutlineV3ExecutionError):
        executor._run_bounded_semantic_provider_call(
            "cross_group_comparison_provider",
            request,
            {"topic_synthesis": "sealed-source-input"},
        )
    assert calls == []

