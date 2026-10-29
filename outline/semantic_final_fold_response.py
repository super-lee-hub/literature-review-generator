"""Pure compact codec for a sealed semantic final fold response."""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping, Sequence
from typing import Any

from runtime.provider_runtime import hash_json

_CATEGORIES = ("comparisons", "bridge_claims", "unresolved_questions", "topic_dispositions")
_COVERAGE_SCHEMA = "outline-semantic-reducer-coverage-ref/v1"
_RESULT_SCHEMA = "outline-semantic-reducer-result-ref/v1"


def topic_fragment_inputs(topics: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Reconstruct the existing topic-fragment request view from its source."""
    rows = []
    for topic in topics:
        for fragment in topic.get("fragments") or ():
            sources = fragment.get("provider_results") or []
            if len(sources) != 1 or not isinstance(sources[0], Mapping):
                raise ValueError("topic fragment has no unique provider-result owner")
            source = sources[0]
            provider = source.get("provider_output") or {}
            evidence = {
                str(value) for value in (provider.get("topic") or {}).get("supporting_evidence_ids") or () if str(value)
            } | {
                str(value) for claim in provider.get("claims") or ()
                for value in claim.get("evidence_ids") or () if str(value)
            }
            row = {
                "topic_id": str(topic.get("topic_id") or ""),
                "fragment_id": str(fragment.get("fragment_id") or ""),
                "question": str(topic.get("question") or ""),
                "paper_ids": [str(value) for value in fragment.get("paper_ids") or () if str(value)],
                "bridge_paper_ids": [str(value) for value in topic.get("bridge_paper_ids") or () if str(value)],
                "supporting_evidence_ids": sorted(evidence),
                "provider_batch_ids": [str(source.get("batch_id") or "")],
                "provider_outputs": [dict(provider)],
                "result_ids": [str(source.get("result_id") or "")],
            }
            context = source.get("interpretation_context") or {}
            fields = [dict(value) for value in context.get("fields") or ()]
            dependencies = [dict(value) for value in context.get("dependencies") or ()]
            if fields or dependencies:
                row["interpretation_context"] = {"fields": fields, "dependencies": dependencies}
            rows.append(row)
    return rows


def _sha(value: Any) -> str:
    return hash_json(value)


def _disposition_groups(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            raise TypeError("topic disposition must be an object")
        topic_id = str(row.get("topic_id") or "")
        if not topic_id:
            raise ValueError("topic disposition requires topic_id")
        key = (str(row.get("status") or ""), str(row.get("reason") or ""))
        group = grouped.setdefault(key, {"status": key[0], "reason": key[1], "topic_ids": [], "synthesis_claim_ids_by_topic": {}})
        group["topic_ids"].append(topic_id)
        claim_ids = list(row.get("synthesis_claim_ids") or [])
        if claim_ids:
            group["synthesis_claim_ids_by_topic"][topic_id] = claim_ids
    result = []
    for group in grouped.values():
        result.append({
            "status": group["status"],
            **({"reason": group["reason"]} if group["reason"] else {}),
            "topic_ids": list(group["topic_ids"]),
            **({"synthesis_claim_ids_by_topic": group["synthesis_claim_ids_by_topic"]}
               if group["synthesis_claim_ids_by_topic"] else {}),
        })
    return result


def _sealed_rows(items: Sequence[Mapping[str, Any]], source_results: Mapping[str, Mapping[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    rows = {category: [] for category in _CATEGORIES}
    for item in items:
        if not isinstance(item, Mapping):
            raise TypeError("sealed reducer item must be an object")
        coverage = item.get("coverage_ref")
        result_ref = item.get("semantic_result_ref")
        result = item.get("semantic_result")
        source_id = str(result_ref.get("artifact_id") or "") if isinstance(result_ref, Mapping) else ""
        full_result = source_results.get(source_id)
        if isinstance(full_result, Mapping):
            projected_full = {
                key: copy.deepcopy(value)
                for key, value in full_result.items()
                if key not in {"processed_topic_ids", "processed_fragment_ids", "processed_result_ids", "processed_relation_ids", "topic_dispositions"}
            }
            if not isinstance(result, Mapping) or hash_json(projected_full) != hash_json(result):
                raise ValueError("pruned semantic body differs from its trusted full source result")
        if (
            not isinstance(coverage, Mapping)
            or coverage.get("schema_version") != _COVERAGE_SCHEMA
            or not isinstance(result_ref, Mapping)
            or result_ref.get("schema_version") != _RESULT_SCHEMA
            or not isinstance(result, Mapping)
            or not isinstance(full_result, Mapping)
            or len(str(coverage.get("registry_file_hash") or "")) != 64
            or len(str(result_ref.get("registry_file_hash") or "")) != 64
            or str(result_ref.get("provider_result_hash") or "") != _sha(full_result)
        ):
            raise ValueError("sealed reducer item lacks a valid source-bound reference")
        for category in ("comparisons", "bridge_claims", "unresolved_questions"):
            values = full_result.get(category) or []
            if not isinstance(values, list):
                raise TypeError(f"sealed reducer {category} must be an array")
            rows[category].extend(copy.deepcopy(value) for value in values)
        supplied_groups = item.get("topic_disposition_groups") or []
        source_dispositions = full_result.get("topic_dispositions") if isinstance(full_result, Mapping) else []
        expected_groups = _disposition_groups(source_dispositions or [])
        def canonical_group(group: Mapping[str, Any]) -> dict[str, Any]:
            return {
                "status": str(group.get("status") or ""),
                **({"reason": str(group.get("reason") or "")} if str(group.get("reason") or "") else {}),
                "topic_ids": sorted(str(value) for value in group.get("topic_ids") or []),
                **({"synthesis_claim_ids_by_topic": {
                    str(key): sorted({str(value) for value in values})
                    for key, values in group["synthesis_claim_ids_by_topic"].items()
                }}
                   if group.get("synthesis_claim_ids_by_topic") else {}),
            }
        def group_projection(groups: Sequence[Any]) -> list[dict[str, Any]]:
            if any(not isinstance(group, Mapping) for group in groups):
                raise TypeError("topic disposition group must be an object")
            return sorted(
                (canonical_group(group) for group in groups),
                key=lambda group: (group["status"], group.get("reason", "")),
            )
        if hash_json(group_projection(expected_groups)) != hash_json(
            group_projection(supplied_groups)
        ):
            raise ValueError("topic disposition groups differ from trusted full source result")
        rows["topic_dispositions"].extend(copy.deepcopy(row) for row in source_dispositions or [])
    return rows


def _catalog_digest(catalog: Mapping[str, Any]) -> str:
    return _sha({
        "schema_version": catalog.get("schema_version"),
        "identity": catalog.get("identity"),
        "source_refs": catalog.get("source_refs"),
        "categories": catalog.get("categories"),
        **({"fragment_aggregation": catalog["fragment_aggregation"]}
           if "fragment_aggregation" in catalog else {}),
    })


def _build_fragment_aggregation(
    items: Sequence[Mapping[str, Any]],
    source_results: Mapping[str, Mapping[str, Any]],
    ownership: Mapping[str, Any],
) -> tuple[dict[str, list[Any]], dict[str, Any]]:
    # Retain the complete verified source rows before deriving a logical view.
    rows = _sealed_rows(items, source_results)
    base_items = ownership.get("base_items")
    manifests = ownership.get("input_manifests")
    scope = ownership.get("source_scope_ref")
    intermediate = ownership.get("intermediate_items", {})
    if (not isinstance(base_items, list) or not base_items
            or not isinstance(manifests, Mapping) or not isinstance(scope, Mapping)
            or not isinstance(intermediate, Mapping)):
        raise TypeError("v2 ownership proof is incomplete")
    if any(not isinstance(item, Mapping) or not str(item.get("topic_id") or "") for item in base_items):
        raise ValueError("base fragments require their assigned topic identity")
    base_by_hash = {hash_json(item): item for item in base_items}
    if len(base_by_hash) != len(base_items):
        raise ValueError("v2 base item hashes must be unique")
    if not str(scope.get("artifact_id") or "") or len(str(scope.get("registry_file_hash") or "")) != 64:
        raise ValueError("v2 source scope reference is incomplete")
    used_manifests: dict[str, str] = {}
    used_intermediate: set[str] = set()

    def input_item(item_hash: str) -> Mapping[str, Any]:
        value = base_by_hash.get(item_hash, intermediate.get(item_hash))
        if not isinstance(value, Mapping) or hash_json(value) != item_hash:
            raise ValueError("missing or changed source fragment/intermediate item")
        return value

    def assigned_leaves(item: Mapping[str, Any], stack: frozenset[str]) -> list[str]:
        coverage = item.get("coverage_ref")
        if not isinstance(coverage, Mapping):
            raise TypeError("v2 item lacks coverage reference")
        artifact_id = str(coverage.get("artifact_id") or "")
        if artifact_id in stack:
            raise ValueError("cyclic reducer ownership")
        manifest = manifests.get(artifact_id)
        if not isinstance(manifest, Mapping) or hash_json(manifest) != str(coverage.get("manifest_hash") or ""):
            raise ValueError("v2 coverage manifest hash mismatch")
        hashes = manifest.get("source_item_hashes")
        if not isinstance(hashes, list) or not hashes or any(not isinstance(value, str) for value in hashes):
            raise ValueError("coverage manifest has no exact source items")
        ordered_items = [input_item(value) for value in hashes]
        if hash_json(ordered_items) != str(manifest.get("source_input_hash") or ""):
            raise ValueError("v2 source input hash differs")
        used_manifests[artifact_id] = hash_json(manifest)
        leaves: list[str] = []
        child_hashes: set[str] = set()
        for item_hash, child in zip(hashes, ordered_items, strict=True):
            if item_hash in base_by_hash:
                leaves.append(item_hash)
            else:
                _sealed_rows([child], source_results)
                child_ref = child.get("coverage_ref")
                if not isinstance(child_ref, Mapping):
                    raise ValueError("intermediate result has no coverage reference")
                child_hashes.add(str(child_ref.get("registry_file_hash") or ""))
                used_intermediate.add(item_hash)
                leaves.extend(assigned_leaves(child, stack | {artifact_id}))
        if set(manifest.get("child_input_manifest_hashes") or ()) != child_hashes:
            raise ValueError("reducer child manifest lineage differs")
        assigned_topics = {str(base_by_hash[value]["topic_id"]) for value in leaves}
        if set(manifest.get("topic_ids") or ()) != assigned_topics:
            raise ValueError("manifest topics differ from assigned fragment ownership")
        return leaves

    frontier_leaves = [assigned_leaves(item, frozenset()) for item in items]
    union_hashes = [value for group in frontier_leaves for value in group]
    if sorted(union_hashes) != sorted(base_by_hash) or len(union_hashes) != len(set(union_hashes)):
        raise ValueError("v2 source item ownership is incomplete, unknown, or duplicated")
    occurrences: list[dict[str, Any]] = []
    logical: dict[str, list[dict[str, Any]]] = {}
    claims = {str(row.get("claim_id") or ""): row for row in rows["bridge_claims"]}
    if len(claims) != len(rows["bridge_claims"]) or "" in claims:
        raise ValueError("duplicate or missing bridge claim identity")
    for item, leaves in zip(items, frontier_leaves, strict=True):
        ref = item.get("semantic_result_ref")
        source_id = str(ref.get("artifact_id") or "") if isinstance(ref, Mapping) else ""
        full = source_results.get(source_id)
        if not isinstance(ref, Mapping) or not isinstance(full, Mapping) or str(ref.get("provider_result_hash") or "") != hash_json(full):
            raise ValueError("v2 full provider result authority is invalid")
        dispositions = full.get("topic_dispositions") or []
        source_claim_ids = {str(value.get("claim_id") or "") for value in full.get("bridge_claims") or ()}
        assigned_topics = {str(base_by_hash[value]["topic_id"]) for value in leaves}
        actual_topics = [str(row.get("topic_id") or "") for row in dispositions]
        if len(actual_topics) != len(set(actual_topics)) or set(actual_topics) != assigned_topics:
            raise ValueError("source dispositions do not cover their assigned fragments")
        for index, row in enumerate(dispositions):
            if not isinstance(row, Mapping):
                raise TypeError("v2 disposition occurrence is not an object")
            topic_id = str(row.get("topic_id") or "")
            assigned = [value for value in leaves if str(base_by_hash[value]["topic_id"]) == topic_id]
            occurrence = {
                "result_artifact_id": source_id,
                "result_registry_file_hash": str(ref.get("registry_file_hash") or ""),
                "coverage_ref": copy.deepcopy(item["coverage_ref"]),
                "row_index": index, "row_hash": hash_json(row), "topic_id": topic_id,
                "assigned_source_item_hashes": sorted(assigned), "row": copy.deepcopy(row),
                "assigned_paper_ids": sorted({str(paper) for value in assigned for paper in base_by_hash[value].get("paper_ids") or ()}),
                "assigned_fragment_ids": sorted({str(base_by_hash[value].get("fragment_id") or "") for value in assigned}),
            }
            status = str(row.get("status") or "")
            claim_ids = [str(value) for value in row.get("synthesis_claim_ids") or ()]
            if status == "integrated":
                if not claim_ids or any(
                    claim_id not in source_claim_ids or claim_id not in claims or topic_id not in {
                        *[str(value) for value in claims[claim_id].get("topic_ids") or ()],
                        str(claims[claim_id].get("topic_id") or ""),
                    } for claim_id in claim_ids
                ):
                    raise ValueError("integrated source occurrence has no exact topic-owned claim")
            elif status not in {"unresolved", "deferred", "insufficient_evidence", "not_comparable"} or claim_ids or not str(row.get("reason") or "").strip():
                raise ValueError("nonintegrated source occurrence has no bounded reason")
            occurrences.append(occurrence)
            logical.setdefault(topic_id, []).append(occurrence)
    logical_rows = []
    for topic_id in sorted(logical):
        topic_occurrences = logical[topic_id]
        integrated = [value for value in topic_occurrences if value["row"]["status"] == "integrated"]
        remaining = [value for value in topic_occurrences if value["row"]["status"] != "integrated"]
        row = {
            "topic_id": topic_id,
            "status": "integrated" if integrated else "unresolved",
            # Integration is bounded to the referenced claims, never all facts.
            "integration_scope": "partial_source_fragments" if integrated and remaining else "all_assigned_fragments_have_integrations" if integrated else "no_integrated_fragments",
            "integrated_source_item_hashes": sorted(value for occurrence in integrated for value in occurrence["assigned_source_item_hashes"]),
            "nonintegrated_source_item_hashes": sorted(value for occurrence in remaining for value in occurrence["assigned_source_item_hashes"]),
            "integrated_paper_ids": sorted({value for occurrence in integrated for value in occurrence["assigned_paper_ids"]}),
            "nonintegrated_paper_ids": sorted({value for occurrence in remaining for value in occurrence["assigned_paper_ids"]}),
        }
        if integrated:
            row["synthesis_claim_ids"] = sorted({str(value) for occurrence in integrated for value in occurrence["row"].get("synthesis_claim_ids") or ()})
        if remaining:
            row["reason"] = "Source-fragment review items remain: " + json.dumps(
                [value["row"] for value in remaining], ensure_ascii=False, sort_keys=True,
            )
        logical_rows.append(row)
    rows["topic_dispositions"] = logical_rows
    return rows, {
        "schema_version": "outline-semantic-final-fold-fragment-aggregation/v2",
        "policy": "retain-each-source-disposition-and-scope-positive-claims/v1",
        "source_scope_ref": copy.deepcopy(scope),
        "plan_scope_ref": copy.deepcopy(ownership.get("plan_scope_ref")),
        "original_questions": copy.deepcopy(ownership.get("questions") or []),
        "question_disposition": "not_adjudicated_by_local_aggregation",
        "required_comparison_obligations": copy.deepcopy(ownership.get("required_comparisons") or []),
        "base_item_hashes": sorted(base_by_hash),
        "manifest_hashes": dict(sorted(used_manifests.items())),
        "intermediate_item_hashes": sorted(used_intermediate),
        "occurrences": occurrences, "logical_topic_count": len(logical_rows), "occurrence_count": len(occurrences),
    }


def build_final_fold_catalog(
    items: Sequence[Mapping[str, Any]],
    *,
    node_id: str,
    stage_name: str = "outline_v3",
    codec_version: str = "v1",
    source_results: Mapping[str, Mapping[str, Any]],
    ownership_proof: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if not node_id or not stage_name or not codec_version:
        raise ValueError("final fold catalog identity is required")
    if codec_version not in {"v1", "v2-fragment-aggregation"}:
        raise ValueError("unsupported final fold codec version")
    if codec_version == "v2-fragment-aggregation":
        if not isinstance(ownership_proof, Mapping):
            raise ValueError("v2 fragment aggregation requires ownership proof")
        rows, sidecar = _build_fragment_aggregation(items, source_results, ownership_proof)
    else:
        rows = _sealed_rows(items, source_results)
        sidecar = None
    topic_ids: set[str] = set()
    claim_ids: set[str] = set()
    for row in rows["topic_dispositions"]:
        topic_id = str(row.get("topic_id") or "")
        if topic_id in topic_ids:
            raise ValueError("duplicate or conflicting topic disposition")
        topic_ids.add(topic_id)
    for row in rows["bridge_claims"]:
        claim_id = str(row.get("claim_id") or "")
        if claim_id and claim_id in claim_ids:
            raise ValueError("duplicate or conflicting bridge claim")
        if claim_id:
            claim_ids.add(claim_id)
    referenced_items = list(items)
    if codec_version == "v2-fragment-aggregation":
        referenced_items.extend((ownership_proof or {}).get("intermediate_items", {}).values())
    unique_items = {
        (str(item["coverage_ref"].get("artifact_id") or ""),
         str(item["semantic_result_ref"].get("artifact_id") or "")): item
        for item in referenced_items
    }
    source_refs = [
        {
            "reducer_id": str(item.get("reducer_id") or ""),
            "coverage_artifact_id": str(item["coverage_ref"].get("artifact_id") or ""),
            "coverage_registry_file_hash": str(item["coverage_ref"].get("registry_file_hash") or ""),
            "result_artifact_id": str(item["semantic_result_ref"].get("artifact_id") or ""),
            "result_registry_file_hash": str(item["semantic_result_ref"].get("registry_file_hash") or ""),
        }
        for item in unique_items.values()
    ]
    catalog = {
        "schema_version": "outline-semantic-final-fold-catalog/v1",
        "identity": {
            "node_id": node_id,
            "stage_name": stage_name,
            "codec_version": codec_version,
        },
        "source_refs": source_refs,
        "categories": {
            category: [
                {"index": index, "row_hash": _sha(row), "row": row}
                for index, row in enumerate(values)
            ]
            for category, values in rows.items()
        },
    }
    if sidecar is not None:
        catalog["fragment_aggregation"] = sidecar
    catalog["catalog_hash"] = _catalog_digest(catalog)
    return catalog


def compact_final_fold_response(catalog: Mapping[str, Any], selected: Mapping[str, Sequence[int]] | None = None) -> dict[str, Any]:
    categories_value = catalog.get("categories")
    categories: Mapping[str, Any] = categories_value if isinstance(categories_value, Mapping) else {}
    chosen = selected or {
        category: list(range(len(categories.get(category) or [])))
        for category in _CATEGORIES
    }
    identity_value = catalog.get("identity")
    identity: Mapping[str, Any] = identity_value if isinstance(identity_value, Mapping) else {}
    return {
        "schema_version": "outline-semantic-final-fold-response/v1",
        "catalog_hash": str(catalog.get("catalog_hash") or ""),
        "node_id": str(identity.get("node_id") or ""),
        "stage_name": str(identity.get("stage_name") or ""),
        "codec_version": str(identity.get("codec_version") or ""),
        "selected_indices": {category: list(indices) for category, indices in chosen.items()},
        **({"fragment_aggregation_hash": _sha(catalog.get("fragment_aggregation"))}
           if isinstance(catalog.get("fragment_aggregation"), Mapping) else {}),
    }


def expand_final_fold_response(response: Mapping[str, Any], catalog: Mapping[str, Any]) -> dict[str, Any]:
    required_fields = {"schema_version", "catalog_hash", "node_id", "stage_name", "codec_version", "selected_indices"}
    if isinstance(catalog.get("fragment_aggregation"), Mapping):
        required_fields.add("fragment_aggregation_hash")
    if set(response) != required_fields:
        raise ValueError("final fold response contains unknown or missing fields")
    if response.get("schema_version") != "outline-semantic-final-fold-response/v1":
        raise ValueError("final fold response schema is invalid")
    if response.get("catalog_hash") != catalog.get("catalog_hash"):
        raise ValueError("final fold response catalog hash differs")
    if _catalog_digest(catalog) != str(catalog.get("catalog_hash") or ""):
        raise ValueError("final fold catalog hash is stale or mutated")
    identity_value = catalog.get("identity")
    identity: Mapping[str, Any] = identity_value if isinstance(identity_value, Mapping) else {}
    if any(response.get(key) != identity.get(key) for key in ("node_id", "stage_name", "codec_version")):
        raise ValueError("final fold response identity differs")
    if isinstance(catalog.get("fragment_aggregation"), Mapping) and response.get("fragment_aggregation_hash") != _sha(catalog["fragment_aggregation"]):
        raise ValueError("fragment aggregation sidecar hash differs")
    selected = response.get("selected_indices")
    categories = catalog.get("categories")
    if not isinstance(selected, Mapping) or not isinstance(categories, Mapping) or set(selected) != set(_CATEGORIES):
        raise ValueError("final fold response category set is invalid")
    expanded: dict[str, list[dict[str, Any]]] = {}
    for category in _CATEGORIES:
        source = categories.get(category)
        indices = selected.get(category)
        if not isinstance(source, list) or not isinstance(indices, list):
            raise TypeError("final fold category is not an array")
        if any(isinstance(index, bool) or not isinstance(index, int) for index in indices):
            raise ValueError("final fold indices must be integers")
        expected = set(range(len(source)))
        if set(indices) != expected or len(indices) != len(expected):
            raise ValueError("final fold indices must be an exact permutation")
        values: list[dict[str, Any]] = []
        for index in indices:
            entry = source[index]
            if not isinstance(entry, Mapping) or entry.get("row_hash") != _sha(entry.get("row")):
                raise ValueError("final fold catalog row hash is invalid")
            values.append(copy.deepcopy(entry["row"]))
        expanded[category] = values
    if isinstance(catalog.get("fragment_aggregation"), Mapping):
        expanded["topic_dispositions"].sort(key=lambda value: str(value["topic_id"]))
        expanded["fragment_aggregation"] = copy.deepcopy(catalog["fragment_aggregation"])
    return expanded


__all__ = ["build_final_fold_catalog", "compact_final_fold_response", "expand_final_fold_response", "topic_fragment_inputs"]
