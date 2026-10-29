from __future__ import annotations

import copy
import json

import pytest
from outline.semantic_final_fold_response import (
    build_final_fold_catalog,
    compact_final_fold_response,
    expand_final_fold_response,
)

from runtime.provider_runtime import hash_json


def _items(count: int = 3):
    result = []
    for index in range(count):
        body = {
            "comparisons": [{"comparison_id": f"cmp-{index}", "qualifier": False}],
            "bridge_claims": [{"claim_id": f"claim-{index}", "topic_ids": [f"topic-{index}"], "zero": 0}],
            "unresolved_questions": [f"q-{index}"],
        }
        result.append({
            "reducer_id": f"reducer-{index}",
            "coverage_ref": {
                "schema_version": "outline-semantic-reducer-coverage-ref/v1",
                "registry_file_hash": "a" * 64,
            },
            "semantic_result": body,
            "semantic_result_ref": {
                "schema_version": "outline-semantic-reducer-result-ref/v1",
                "artifact_id": f"result-artifact-{index}",
                "registry_file_hash": "b" * 64,
                "provider_result_hash": hash_json(body),
            },
            "topic_disposition_groups": [{
                "status": "integrated",
                "reason": "",
                "topic_ids": [f"topic-{index}"],
                "synthesis_claim_ids_by_topic": {f"topic-{index}": [f"claim-{index}"]},
            }],
        })
    return result


def _source_results(items):
    result = {}
    for item in items:
        full = copy.deepcopy(item["semantic_result"])
        dispositions = []
        for group in item.get("topic_disposition_groups") or []:
            for topic_id in group.get("topic_ids") or []:
                dispositions.append({
                    "topic_id": topic_id,
                    "status": group.get("status"),
                    **({"reason": group["reason"]} if group.get("reason") else {}),
                    **({"synthesis_claim_ids": group["synthesis_claim_ids_by_topic"][topic_id]}
                       if topic_id in group.get("synthesis_claim_ids_by_topic", {}) else {}),
                })
        full["topic_dispositions"] = dispositions
        item["semantic_result_ref"]["provider_result_hash"] = hash_json(full)
        result[item["semantic_result_ref"]["artifact_id"]] = full
    return result


def test_roundtrip_preserves_sealed_rows_and_provider_order() -> None:
    items = _items(); catalog = build_final_fold_catalog(items, node_id="cross_group_comparison_provider:final", source_results=_source_results(items))
    response = compact_final_fold_response(catalog, {
        "comparisons": [2, 0, 1],
        "bridge_claims": [1, 2, 0],
        "unresolved_questions": [0, 2, 1],
        "topic_dispositions": [2, 1, 0],
    })
    expanded = expand_final_fold_response(response, catalog)
    assert expanded["comparisons"][0]["comparison_id"] == "cmp-2"
    assert expanded["bridge_claims"][0]["claim_id"] == "claim-1"
    assert expanded["topic_dispositions"][0]["topic_id"] == "topic-2"
    assert expanded["bridge_claims"][1]["zero"] == 0


def test_catalog_compacts_large_rows() -> None:
    items = _items(200)
    catalog = build_final_fold_catalog(items, node_id="cross_group_comparison_provider:final", source_results=_source_results(items))
    response = compact_final_fold_response(catalog)
    assert len(json.dumps(response, separators=(",", ":"))) < len(json.dumps(items, separators=(",", ":")))
    assert len(expand_final_fold_response(response, catalog)["comparisons"]) == 200


@pytest.mark.parametrize("mutate", [
    lambda response: response["selected_indices"]["comparisons"].__setitem__(0, True),
    lambda response: response["selected_indices"]["comparisons"].append(0),
    lambda response: response.__setitem__("catalog_hash", "f" * 64),
    lambda response: response.__setitem__("unknown", 1),
])
def test_tampered_or_non_permutation_response_rejected(mutate) -> None:
    items = _items(); catalog = build_final_fold_catalog(items, node_id="cross_group_comparison_provider:final", source_results=_source_results(items))
    response = compact_final_fold_response(catalog)
    mutate(response)
    with pytest.raises(ValueError):
        expand_final_fold_response(response, catalog)


def test_source_schema_and_hash_are_required() -> None:
    item = _items(1)[0]
    bad = copy.deepcopy(item)
    bad["semantic_result_ref"]["provider_result_hash"] = "0" * 64
    with pytest.raises(ValueError):
        build_final_fold_catalog([bad], node_id="cross_group_comparison_provider:final", source_results={})


def test_pruned_semantic_body_uses_trusted_full_source_result() -> None:
    item = _items(1)[0]
    full = {
        **copy.deepcopy(item["semantic_result"]),
        "processed_topic_ids": ["topic-0"],
        "processed_result_ids": ["result-0"],
        "topic_dispositions": [{"topic_id": "topic-0", "status": "integrated", "synthesis_claim_ids": ["claim-0"]}],
    }
    item["semantic_result_ref"]["artifact_id"] = "full-result-1"
    item["semantic_result_ref"]["provider_result_hash"] = hash_json(full)
    catalog = build_final_fold_catalog(
        [item], node_id="cross_group_comparison_provider:final",
        source_results={"full-result-1": full},
    )
    assert catalog["categories"]["comparisons"][0]["row"]["comparison_id"] == "cmp-0"


def test_catalog_source_ref_mutation_is_rejected() -> None:
    items = _items(1); catalog = build_final_fold_catalog(items, node_id="cross_group_comparison_provider:final", source_results=_source_results(items))
    response = compact_final_fold_response(catalog)
    catalog["source_refs"][0]["coverage_registry_file_hash"] = "f" * 64
    with pytest.raises(ValueError):
        expand_final_fold_response(response, catalog)


def test_duplicate_topic_or_claim_rows_are_rejected() -> None:
    items = _items(2)
    items[1]["topic_disposition_groups"][0]["topic_ids"] = ["topic-0"]
    with pytest.raises(ValueError):
        build_final_fold_catalog(items, node_id="cross_group_comparison_provider:final", source_results=_source_results(items))


def _v2_case(states=("integrated", "deferred")):
    items = _items(len(states))
    bases, manifests = [], {}
    for index, (item, status) in enumerate(zip(items, states, strict=True)):
        base = {"topic_id": "topic-0", "fragment_id": f"fragment-{index}", "paper_ids": [f"paper-{index}"], "bridge_paper_ids": ["context-paper"]}
        bases.append(base)
        groups = [{"topic_ids": ["topic-0"], "status": status}]
        if status == "integrated":
            item["semantic_result"]["bridge_claims"][0]["topic_ids"] = ["topic-0"]
            groups[0]["synthesis_claim_ids_by_topic"] = {"topic-0": [f"claim-{index}"]}
        else:
            groups[0]["reason"] = "This fragment has no complete source-closed comparison."
            item["semantic_result"]["bridge_claims"] = []
        item["topic_disposition_groups"] = groups
        manifest = {"source_item_hashes": [hash_json(base)], "source_input_hash": hash_json([base]), "topic_ids": ["topic-0"], "child_input_manifest_hashes": []}
        manifest_id = f"manifest-{index}"
        manifests[manifest_id] = manifest
        item["coverage_ref"].update(artifact_id=manifest_id, manifest_hash=hash_json(manifest), registry_file_hash=hash_json({"file": index}))
    full = _source_results(items)
    for item in items:
        payload = full[item["semantic_result_ref"]["artifact_id"]]
        payload["topic_dispositions"][0].update(source_field_ids=["sample-boundary"], study_id="", zero=0, uncertain=False)
        item["semantic_result_ref"]["provider_result_hash"] = hash_json(payload)
    return items, full, {"base_items": bases, "input_manifests": manifests, "source_scope_ref": {"artifact_id": "topic-synthesis", "registry_file_hash": "c" * 64}}


def _build_v2(items, full, ownership):
    return build_final_fold_catalog(items, node_id="cross_group_comparison_provider", codec_version="v2-fragment-aggregation", source_results=full, ownership_proof=ownership)


def test_v2_retains_every_source_array_and_partial_fragment_disposition():
    items, full, proof = _v2_case()
    catalog = _build_v2(items, full, proof)
    expanded = expand_final_fold_response(compact_final_fold_response(catalog), catalog)
    for category in ("comparisons", "bridge_claims", "unresolved_questions"):
        assert expanded[category] == [row for payload in full.values() for row in payload[category]]
    topic = expanded["topic_dispositions"][0]
    assert topic["status"] == "integrated"
    assert topic["integration_scope"] == "partial_source_fragments"
    assert topic["synthesis_claim_ids"] == ["claim-0"]
    assert len(topic["nonintegrated_source_item_hashes"]) == 1
    source_rows = [row for payload in full.values() for row in payload["topic_dispositions"]]
    assert [row["row"] for row in expanded["fragment_aggregation"]["occurrences"]] == source_rows
    assert expanded["fragment_aggregation"]["occurrence_count"] == 2
    assert expanded["fragment_aggregation"]["logical_topic_count"] == 1


def test_v2_all_integrated_unions_only_bound_claims_and_canonical_topics():
    items, full, proof = _v2_case(("integrated", "integrated"))
    catalog = _build_v2(items, full, proof)
    selected = {name: list(reversed(range(len(entries)))) for name, entries in catalog["categories"].items()}
    expanded = expand_final_fold_response(compact_final_fold_response(catalog, selected), catalog)
    assert expanded["topic_dispositions"][0]["synthesis_claim_ids"] == ["claim-0", "claim-1"]
    assert expanded["topic_dispositions"][0]["integration_scope"] == "all_assigned_fragments_have_integrations"
    assert expanded["bridge_claims"] == list(reversed([payload["bridge_claims"][0] for payload in full.values()]))


def test_v2_all_deferred_stays_unresolved_with_exact_reasons():
    items, full, proof = _v2_case(("deferred", "deferred"))
    catalog = _build_v2(items, full, proof)
    expanded = expand_final_fold_response(compact_final_fold_response(catalog), catalog)
    topic = expanded["topic_dispositions"][0]
    assert topic["status"] == "unresolved" and "synthesis_claim_ids" not in topic
    assert "no complete source-closed comparison" in topic["reason"]
    assert {entry["row"]["status"] for entry in expanded["fragment_aggregation"]["occurrences"]} == {"deferred"}


@pytest.mark.parametrize("mutation", ["missing_base", "wrong_input_hash", "context_topic", "missing_manifest", "sidecar"])
def test_v2_rejects_incomplete_or_changed_ownership(mutation):
    items, full, proof = _v2_case()
    if mutation == "missing_base":
        proof["base_items"].append({"topic_id": "topic-0", "fragment_id": "missing"})
    elif mutation == "wrong_input_hash":
        manifest = proof["input_manifests"]["manifest-0"]
        manifest["source_input_hash"] = hash_json(manifest["source_item_hashes"])
        items[0]["coverage_ref"]["manifest_hash"] = hash_json(manifest)
    elif mutation == "context_topic":
        source = full[items[0]["semantic_result_ref"]["artifact_id"]]
        source["topic_dispositions"][0]["topic_id"] = "context-only-topic"
        items[0]["semantic_result_ref"]["provider_result_hash"] = hash_json(source)
        items[0]["topic_disposition_groups"][0]["topic_ids"] = ["context-only-topic"]
    elif mutation == "missing_manifest":
        del proof["input_manifests"]["manifest-0"]
    else:
        catalog = _build_v2(items, full, proof)
        response = compact_final_fold_response(catalog)
        catalog["fragment_aggregation"]["occurrences"][0]["row"]["status"] = "deferred"
        response["fragment_aggregation_hash"] = hash_json(catalog["fragment_aggregation"])
        with pytest.raises(ValueError):
            expand_final_fold_response(response, catalog)
        return
    with pytest.raises((ValueError, TypeError)):
        _build_v2(items, full, proof)


def test_v2_two_level_coverage_resolves_exact_original_fragments():
    leaves, full, proof = _v2_case(("integrated", "integrated"))
    parent = _items(1)[0]
    parent["reducer_id"] = "parent-reducer"
    parent["semantic_result_ref"]["artifact_id"] = "parent-result"
    parent["semantic_result"] = {category: [row for payload in full.values() for row in payload[category]] for category in ("comparisons", "bridge_claims", "unresolved_questions")}
    parent["topic_disposition_groups"] = [{"status": "integrated", "topic_ids": ["topic-0"], "synthesis_claim_ids_by_topic": {"topic-0": ["claim-0", "claim-1"]}}]
    full.update(_source_results([parent]))
    manifest = {"topic_ids": ["topic-0"], "source_item_hashes": [hash_json(item) for item in leaves], "source_input_hash": hash_json(leaves), "child_input_manifest_hashes": [item["coverage_ref"]["registry_file_hash"] for item in leaves]}
    proof["input_manifests"]["parent-manifest"] = manifest
    parent["coverage_ref"].update(artifact_id="parent-manifest", manifest_hash=hash_json(manifest))
    proof["intermediate_items"] = {hash_json(item): item for item in leaves}
    catalog = _build_v2([parent], full, proof)
    assert len(catalog["fragment_aggregation"]["base_item_hashes"]) == 2
    assert len(catalog["fragment_aggregation"]["manifest_hashes"]) == 3
    proof["intermediate_items"].pop(hash_json(leaves[0]))
    with pytest.raises(ValueError):
        _build_v2([parent], full, proof)

