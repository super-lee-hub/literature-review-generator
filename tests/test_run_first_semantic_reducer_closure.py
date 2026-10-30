"""Source-closure continuity across bounded shared-semantic reduction levels."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import Any

import pytest

from outline.v3_executor import OutlineV3ExecutionError
from outline.semantic_request_wire import expand_semantic_request_wire
from runtime.provider_runtime import hash_json, hash_text
from tests.test_outline_v3_semantic_execution import _executor


def _source_comparison() -> tuple[dict[str, Any], set[str], set[str], dict[str, str]]:
    row: dict[str, Any] = {
        "topic_id": "topic:b-c",
        "fragment_id": "fragment:b-c",
        "paper_ids": ["paper:b", "paper:c"],
        "result_ids": ["result:b-c"],
        "bounded_context": (
            "The source reports a two-paper comparison, retaining each study condition. "
            "来源报告双论文比较，并保留各自研究条件。 " * 18
        ),
    }
    claims: list[dict[str, Any]] = []
    fields: list[dict[str, Any]] = []
    dependencies: list[dict[str, Any]] = []
    source_claim_ids: set[str] = set()
    evidence_ids: set[str] = set()
    source_field_values: dict[str, str] = {}
    for side, paper_key in (("b", "paper:b"), ("c", "paper:c")):
        for index in range(3):
            primary_id = f"source:{side}:finding:{index}"
            qualifier_id = f"source:{side}:qualifier:{index}"
            primary_evidence = f"evidence:{side}:finding:{index}"
            qualifier_evidence = f"evidence:{side}:qualifier:{index}"
            field_id = f"field:{side}:boundary:{index}"
            field_value = (
                f"Paper {side.upper()} reports finding {index + 1} only within its recorded "
                f"{side.upper()}-specific sample and observation window."
            )
            source_claim_ids.update((primary_id, qualifier_id))
            evidence_ids.update((primary_evidence, qualifier_evidence))
            source_field_values[field_id] = field_value
            claims.extend((
                {
                    "claim_id": primary_id, "claim_type": "empirical_finding",
                    "paper_key": paper_key,
                    "text": f"Paper {side.upper()} reports finding {index + 1}.",
                    "evidence_ids": [primary_evidence],
                    "source_locator": f"results:{side}:{index}",
                },
                {
                    "claim_id": qualifier_id, "claim_type": "author_interpretation",
                    "paper_key": paper_key, "text": field_value,
                    "evidence_ids": [qualifier_evidence],
                    "source_locator": f"discussion:{side}:{index}",
                },
            ))
            fields.append({
                "source_field_id": field_id,
                "source_path": f"results.{side}.boundary.{index}",
                "source_value": field_value,
                "disposition": "exact",
                "canonical_field": "study boundary",
                "scope": "paper",
                "study_id": "",
                "paper_key": paper_key,
                "owner_study_id": "",
            })
            dependencies.append({
                "dependency_id": f"dependency:{side}:{index}",
                "primary_claim_id": primary_id,
                "primary_evidence_ids": [primary_evidence],
                "required_source_claim_ids": [qualifier_id],
                "required_evidence_ids": [qualifier_evidence],
                "required_source_field_ids": [field_id],
                "scope": "paper",
                "study_id": "",
                "paper_key": paper_key,
                "owner_study_id": "",
                "reason": "preserve the source-reported sample and observation boundary",
            })

    # An unreferenced input field proves the reducer carries only the validated
    # output closure, not every field from every child request.
    fields.append({
        "source_field_id": "field:b:unused",
        "source_path": "methods.unused_field",
        "source_value": "This unused field must not be copied into the bridge request.",
        "disposition": "exact",
        "canonical_field": "unused",
        "scope": "paper",
        "study_id": "",
        "paper_key": "paper:b",
        "owner_study_id": "",
    })
    row["claims"] = claims
    row["supporting_evidence_ids"] = sorted(evidence_ids)
    row["interpretation_context"] = {"fields": fields, "dependencies": dependencies}
    return row, source_claim_ids, evidence_ids, source_field_values


def _topic_ids(value: Any) -> set[str]:
    found: set[str] = set()

    def visit(item: Any) -> None:
        if isinstance(item, Mapping):
            for key in ("topic_id", "topic_ids", "processed_topic_ids"):
                raw = item.get(key)
                values = [raw] if isinstance(raw, str) else raw or ()
                found.update(str(value) for value in values if str(value))
            members = item.get("topic_members_by_id")
            if isinstance(members, Mapping):
                found.update(str(key) for key in members if str(key))
            for child in item.values():
                visit(child)
        elif isinstance(item, (list, tuple)):
            for child in item:
                visit(child)

    visit(value)
    return found


def _bridge_claims(value: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []

    def visit(item: Any) -> None:
        if isinstance(item, Mapping):
            for key in ("bridge_claims", "claims", "synthesis_claims"):
                raw = item.get(key)
                if isinstance(raw, list):
                    found.extend(
                        dict(row) for row in raw
                        if isinstance(row, Mapping)
                        and str(row.get("claim_id") or "").startswith("synthesis:cross_group_comparison:")
                    )
            for child in item.values():
                visit(child)
        elif isinstance(item, (list, tuple)):
            for child in item:
                visit(child)

    visit(value)
    return found


def _context_fields(items: Any) -> list[dict[str, Any]]:
    fields: dict[str, dict[str, Any]] = {}

    def visit(item: Any) -> None:
        if isinstance(item, Mapping):
            context = item.get("interpretation_context")
            if isinstance(context, Mapping):
                for raw in context.get("fields") or ():
                    if isinstance(raw, Mapping) and str(raw.get("source_field_id") or ""):
                        fields[str(raw["source_field_id"])] = dict(raw)
            for child in item.values():
                visit(child)
        elif isinstance(item, (list, tuple)):
            for child in item:
                visit(child)

    visit(items)
    return [fields[key] for key in sorted(fields)]


def _bounded_noise_topics(count: int = 64) -> list[dict[str, Any]]:
    rows = [_source_comparison()[0]]
    for index in range(1, count):
        suffix = f"{index:02d}"
        rows.append({
            "topic_id": f"topic:{suffix}",
            "fragment_id": f"fragment:{suffix}",
            "paper_ids": [f"paper:{suffix}"],
            "result_ids": [f"result:{suffix}"],
            "supporting_evidence_ids": [f"evidence:{suffix}"],
            "claims": [{
                "claim_id": f"source:{suffix}",
                "paper_key": f"paper:{suffix}",
                "text": f"The bounded supported finding for source {suffix}.",
                "evidence_ids": [f"evidence:{suffix}"],
            }],
            "bounded_context": (
                f"Topic {suffix}: bilingual source describes the measured finding and its boundary. "
                "该来源报告了有条件的研究发现，并保留适用范围。 " * 18
            ),
        })
    return rows


@pytest.mark.parametrize("reverse", [False, True])
def test_reducer_unions_disjoint_fragments_of_the_same_topic(tmp_path, reverse):
    topics = _bounded_noise_topics(64)[1:]
    for topic in topics:
        topic["topic_id"] = "topic:shared"
    topics[-1]["bridge_paper_ids"] = ["paper:bridge"]
    if reverse:
        topics.reverse()
    final_requests = []

    def provider(node_id, request):
        request = expand_semantic_request_wire(request)
        if ":reduce:" not in node_id:
            final_requests.append(deepcopy(request))
        return {"status": "success", "content": {
            "comparisons": [], "bridge_claims": [],
            "topic_dispositions": [{
                "topic_id": "topic:shared", "status": "deferred",
                "reason": "Membership projection test authors no scientific statement.",
            }],
            "unresolved_questions": [],
        }}

    executor = _executor(tmp_path, provider=provider, stability_mode="off", max_source_prompt_tokens=8_000)
    # Membership survives reduction even when the final scientific gate
    # correctly refuses a provider that authored no supported claim.
    with pytest.raises(OutlineV3ExecutionError, match="no supported integrated topic"):
        executor._run_bounded_semantic_provider_call("cross_group_comparison_provider", {
            "task": "substantive_cross_group_comparison", "node_id": "cross_group_comparison",
            "semantic_contract_version": "semantic-evidence-graph-v2",
            "shared_synthesis_contract_version": "v1", "topic_synthesis": topics,
            "relation_candidates": [], "questions": ["Retain each supplied source slice."],
            "output_contract": {},
        }, {"topic_synthesis": hash_json(topics)})
    assert final_requests and final_requests[-1]["hierarchy"]["level"] == "bounded_semantic_final_fold"
    expected_members = {f"paper:{index:02d}" for index in range(1, 64)} | {"paper:bridge"}
    recovered_members = set()
    recovered_fragments = {}
    for item in final_requests[-1]["topic_synthesis"]:
        manifest = executor._load_semantic_reducer_coverage_ref(item)
        assert manifest is not None
        recovered_members.update(manifest["topic_members_by_id"]["topic:shared"])
        for fragment_id, members in manifest["fragment_members_by_id"].items():
            recovered_fragments.setdefault(fragment_id, set()).update(members)
    assert recovered_members == expected_members
    for topic in topics:
        assert recovered_fragments[topic["fragment_id"]] == set(topic["paper_ids"]) | set(topic.get("bridge_paper_ids", []))


def test_multilevel_cross_reducer_preserves_exact_bridge_source_context_and_ancestry(tmp_path):
    comparison, expected_claim_ids, expected_evidence_ids, expected_field_values = _source_comparison()
    topics = _bounded_noise_topics()
    topics[0] = comparison
    final_requests: list[dict[str, Any]] = []
    reducer_requests: list[tuple[str, dict[str, Any]]] = []
    reducer_wire_requests: list[dict[str, Any]] = []

    def provider(node_id: str, request: Mapping[str, Any]) -> Mapping[str, Any]:
        wire_request = deepcopy(dict(request))
        request = expand_semantic_request_wire(request)
        hierarchy = request.get("hierarchy")
        final_fold = isinstance(hierarchy, Mapping) and hierarchy.get("level") == "bounded_semantic_final_fold"
        if final_fold:
            final_requests.append(request)
        elif ":reduce:" in node_id:
            reducer_requests.append((node_id, request))
            reducer_wire_requests.append(wire_request)

        requested_topics = sorted(_topic_ids(request.get("topic_synthesis") or ()))
        fields = _context_fields(request.get("topic_synthesis") or ())
        prior_bridges = _bridge_claims(request.get("topic_synthesis") or ())
        has_bc = "topic:b-c" in set(requested_topics)
        fields_by_id = {
            str(field.get("source_field_id") or ""): field for field in fields
        }
        bridge_claims: list[dict[str, Any]] = []
        if has_bc:
            field_ids = sorted(set(expected_field_values).intersection(fields_by_id))
            if set(field_ids) == set(expected_field_values):
                source_ids = sorted(expected_claim_ids)
                source_evidence = sorted(expected_evidence_ids)
                text = "The B/C comparison retains these exact reported boundaries: " + "; ".join(
                    str(fields_by_id[field_id]["source_value"]) for field_id in field_ids
                )
            elif prior_bridges:
                # Without the original field closure, a reducer can only refer
                # to the validated earlier synthesis. It must not recreate a
                # source-level conclusion from IDs alone.
                prior = next((claim for claim in prior_bridges if "topic:b-c" in claim.get("topic_ids", ())), prior_bridges[0])
                source_ids = [str(prior["claim_id"])]
                source_evidence = sorted(expected_evidence_ids)
                text = "The earlier B/C comparison remains qualified by its recorded source boundary."
                field_ids = []
            else:
                source_ids = []
                source_evidence = []
                text = ""
                field_ids = []
            if source_ids and source_evidence:
                bridge_claims.append({
                    "claim_id": f"synthesis:cross_group_comparison:{hash_text(node_id)[:12]}",
                    "topic_ids": ["topic:b-c"],
                    "fragment_id": "fragment:b-c",
                    "paper_keys": ["paper:b", "paper:c"],
                    "text": text,
                    "source_claim_ids": source_ids,
                    "evidence_ids": source_evidence,
                    **({"source_field_ids": field_ids} if field_ids else {}),
                })

        bridge_ids = {str(claim["claim_id"]) for claim in bridge_claims}
        integrated = bool(bridge_claims)
        result = {
            "comparisons": [],
            "bridge_claims": bridge_claims,
            "topic_dispositions": [
                ({
                    "topic_id": topic_id,
                    "status": "integrated",
                    "synthesis_claim_ids": sorted(bridge_ids),
                } if topic_id == "topic:b-c" and integrated else {
                    "topic_id": topic_id,
                    "status": "unresolved",
                    "reason": "No source-complete comparison claim was returned for this topic.",
                })
                for topic_id in requested_topics
            ],
            "unresolved_questions": [],
        }
        return {"status": "success", "content": result}

    executor = _executor(
        tmp_path, provider=provider, stability_mode="off", max_source_prompt_tokens=8_000,
    )
    request = {
        "task": "substantive_cross_group_comparison",
        "node_id": "cross_group_comparison",
        "semantic_contract_version": "semantic-evidence-graph-v2",
        "shared_synthesis_contract_version": "v1",
        "topic_synthesis": topics,
        "relation_candidates": [],
        "questions": ["Compare the B/C findings while preserving their different study boundaries."],
        "output_contract": {},
    }
    result = executor._run_bounded_semantic_provider_call(
        "cross_group_comparison_provider", request, {"topic_synthesis": "source-input-hash"},
    )

    assert any(
        isinstance((provider_request.get("hierarchy") or {}), Mapping)
        and int(provider_request["hierarchy"].get("reduction_level") or 0) >= 2
        for provider_request in reducer_wire_requests
    ), "the fixture must cross more than one reduction boundary"
    assert final_requests, "the bounded reduction must reach the original final comparison task"
    profile = executor._node_route("candidate_1_provider_generation").profile
    assert all(
        profile.estimate_tokens(provider_request) <= 8_000
        for provider_request in reducer_wire_requests
    ), "every child reducer request must fit the unchanged 8,000-token test cap"
    assert len(result["topic_dispositions"]) == len(topics)
    assert {row["topic_id"] for row in result["topic_dispositions"]} == {
        row["topic_id"] for row in topics
    }
    final_request = final_requests[-1]
    assert final_request["hierarchy"]["level"] == "bounded_semantic_final_fold"
    assert final_request["questions"] == request["questions"]
    assert all(provider_request.get("questions") == request["questions"]
               for _node_id, provider_request in reducer_requests)
    final_topic = next(
        item for item in final_request["topic_synthesis"]
        if isinstance(item, Mapping) and "topic:b-c" in _topic_ids(item)
    )
    context = final_topic.get("interpretation_context")
    assert isinstance(context, Mapping), "the final reducer lost the verified B/C interpretation context"
    field_rows = context.get("fields") or ()
    assert {
        str(field.get("source_field_id") or ""): str(field.get("source_value") or "")
        for field in field_rows if isinstance(field, Mapping)
    } == expected_field_values, "carry exact required field text, and no unrelated source fields"
    dependency_rows = context.get("dependencies") or ()
    assert {str(row.get("primary_claim_id") or "") for row in dependency_rows if isinstance(row, Mapping)} == {
        f"source:{side}:finding:{index}"
        for side in ("b", "c") for index in range(3)
    }

    previous_bridge = next(
        claim for claim in _bridge_claims(final_topic.get("semantic_result"))
        if "topic:b-c" in claim.get("topic_ids", ())
    )
    assert set(previous_bridge["source_claim_ids"]) == expected_claim_ids
    assert set(previous_bridge["evidence_ids"]) == expected_evidence_ids
    assert set(previous_bridge["source_field_ids"]) == set(expected_field_values)
    assert result["topic_dispositions"] and any(
        row.get("topic_id") == "topic:b-c" and row.get("status") == "integrated"
        for row in result["topic_dispositions"] if isinstance(row, Mapping)
    ), "the source-complete comparison must remain usable, not collapse to unresolved work"
    assert {row["topic_id"] for row in result["topic_dispositions"]} == {
        str(row["topic_id"]) for row in topics
    }

    coverage_ref = final_topic["coverage_ref"]
    coverage_manifest = executor._load_semantic_reducer_coverage_ref(final_topic)
    assert coverage_manifest is not None
    assert coverage_manifest["node_id"] == coverage_ref["node_id"] == final_topic["reducer_id"]
    assert coverage_manifest["topic_ids"] == final_topic["topic_ids"]
    assert coverage_manifest["topic_members_by_id"]["topic:b-c"] == ["paper:b", "paper:c"]
    assert coverage_ref["relation_count"] == 0, "zero relations is an explicit sealed count, not a missing count"
    assert set(coverage_manifest["source_field_ids"]) == {
        *expected_field_values, "field:b:unused",
    }, "the Registry manifest must retain the complete input field inventory"

    result_ref = final_topic["semantic_result_ref"]
    assert result_ref["schema_version"] == "outline-semantic-reducer-result-ref/v1"
    full_result = executor._load_semantic_reducer_result_ref(final_topic)
    assert isinstance(full_result, Mapping)

    expanded_dispositions: dict[str, dict[str, Any]] = {}
    for group in final_topic["topic_disposition_groups"]:
        for topic_id in group["topic_ids"]:
            disposition = {"topic_id": topic_id, "status": group["status"]}
            if group.get("reason"):
                disposition["reason"] = group["reason"]
            claim_ids = (group.get("synthesis_claim_ids_by_topic") or {}).get(topic_id)
            if claim_ids:
                disposition["synthesis_claim_ids"] = list(claim_ids)
            expanded_dispositions[topic_id] = disposition
    assert [expanded_dispositions[key] for key in sorted(expanded_dispositions)] == sorted(
        full_result["topic_dispositions"], key=lambda row: str(row["topic_id"]),
    ), "grouped dispositions must round-trip exact topic IDs, statuses, reasons and claim refs"

    bad_coverage_ref = deepcopy(final_topic)
    bad_coverage_ref["coverage_ref"]["membership_hash"] = "0" * 64
    with pytest.raises(OutlineV3ExecutionError, match="coverage manifest|Registry authority"):
        executor._load_semantic_reducer_coverage_ref(bad_coverage_ref)
    bad_result_ref = deepcopy(final_topic)
    bad_result_ref["semantic_result_ref"]["registry_file_hash"] = "0" * 64
    with pytest.raises(OutlineV3ExecutionError, match="semantic result reference|Registry authority"):
        executor._load_semantic_reducer_result_ref(bad_result_ref)
    bad_result_hash = deepcopy(final_topic)
    bad_result_hash["semantic_result_ref"]["provider_result_hash"] = "0" * 64
    with pytest.raises(OutlineV3ExecutionError, match="provider-result hash"):
        executor._load_semantic_reducer_result_ref(bad_result_hash)
    bad_compact_result = deepcopy(final_topic)
    bad_compact_result["topic_disposition_groups"][0]["reason"] = "tampered disposition"
    with pytest.raises(OutlineV3ExecutionError, match="compact semantic result|disposition"):
        executor._load_semantic_reducer_result_ref(bad_compact_result)
    bad_result_request = deepcopy(final_request)
    bad_result_topic = next(
        item for item in bad_result_request["topic_synthesis"]
        if isinstance(item, Mapping) and "topic:b-c" in _topic_ids(item)
    )
    bad_result_topic["semantic_result_ref"]["registry_file_hash"] = "0" * 64
    with pytest.raises(OutlineV3ExecutionError, match="semantic reducer result reference"):
        executor._validate_semantic_provider_output(
            "cross_group_comparison_provider", bad_result_request, result,
        )

    unsupported = deepcopy(result)
    unsupported["bridge_claims"][0]["source_field_ids"].append("field:foreign:unsupported")
    with pytest.raises(OutlineV3ExecutionError, match="outside its evidence contract.*source_field_ids"):
        executor._validate_semantic_provider_output(
            "cross_group_comparison_provider", final_request, unsupported,
        )

    incomplete_request = deepcopy(final_request)
    incomplete_topic = next(
        item for item in incomplete_request["topic_synthesis"]
        if isinstance(item, Mapping) and "topic:b-c" in _topic_ids(item)
    )
    incomplete_topic["interpretation_context"]["fields"] = [
        field for field in incomplete_topic["interpretation_context"]["fields"]
        if field.get("source_field_id") != min(expected_field_values)
    ]
    with pytest.raises(OutlineV3ExecutionError, match="no visible source text"):
        executor._validate_semantic_provider_output(
            "cross_group_comparison_provider", incomplete_request, result,
        )
