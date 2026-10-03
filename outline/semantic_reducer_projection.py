"""Preserve complete nested topic fragments when physical packing needs them."""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import Any


def split_nested_topic_for_reduction_v1(topic: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Split a pure aggregate; retain unscoped facts as an indivisible input."""
    original = deepcopy(dict(topic))
    fragments = original.get("fragments")
    if not isinstance(fragments, list) or len(fragments) <= 1:
        return [original]
    # These facts can depend on more than one fragment. Their source closure
    # cannot be inferred from a navigation-only aggregate.
    if any(original.get(key) for key in (
        "conclusions", "claims", "conflicts", "comparability_notes",
        "unresolved_questions", "interpretation_context", "bridge_paper_ids", "relation_ids",
    )):
        return [original]
    ids: list[str] = []
    for fragment in fragments:
        if not isinstance(fragment, Mapping):
            raise ValueError("nested topic fragment must be an object")
        fragment_id = str(fragment.get("fragment_id") or "")
        if not fragment_id or fragment_id in ids:
            raise ValueError("nested topic fragment identity is missing or duplicated")
        ids.append(fragment_id)
    declared_ids = original.get("fragment_ids")
    if declared_ids is not None and set(declared_ids) != set(ids):
        raise ValueError("nested topic fragment identity set is detached")
    refs = original.get("provider_output_refs") or []
    if any(not isinstance(ref, Mapping) or ref.get("fragment_id") not in ids for ref in refs):
        raise ValueError("nested topic provider reference is detached")
    rows = []
    all_fields: dict[str, dict[str, Any]] = {}
    all_papers: set[str] = set()
    all_evidence: set[str] = set()
    all_batches: set[str] = set()
    all_results: set[str] = set()
    fragment_results_seen: set[str] = set()
    for fragment in fragments:
        fragment_id = fragment["fragment_id"]
        paper_ids = list(fragment.get("paper_ids") or [])
        if not paper_ids or not set(paper_ids).issubset(original.get("paper_ids") or []):
            raise ValueError("nested topic fragment paper ownership is detached")
        results = fragment.get("provider_results") or []
        if not isinstance(results, list) or len(results) != 1 or not isinstance(results[0], Mapping):
            raise ValueError("nested topic fragment requires exactly one complete provider result")
        field_rows: dict[str, dict[str, Any]] = {}
        dependencies: list[dict[str, Any]] = []
        result_ids: set[str] = set()
        batch_ids: set[str] = set()
        evidence_ids = set(fragment.get("supporting_evidence_ids") or [])

        def collect_output_support(value: Any) -> None:
            if isinstance(value, Mapping):
                owners = set(value.get("paper_ids") or value.get("paper_keys") or [])
                if value.get("paper_key"):
                    owners.add(value["paper_key"])
                if not owners.issubset(paper_ids):
                    raise ValueError("nested topic output belongs to a different paper")
                if value.get("fragment_id") and value["fragment_id"] != fragment_id:
                    raise ValueError("nested topic output belongs to a different fragment")
                if value.get("topic_id") and value["topic_id"] != original.get("topic_id"):
                    raise ValueError("nested topic output belongs to a different topic")
                for key in ("evidence_ids", "supporting_evidence_ids"):
                    evidence_ids.update(value.get(key) or [])
                for child in value.values():
                    collect_output_support(child)
            elif isinstance(value, list):
                for child in value:
                    collect_output_support(child)
        for result in results:
            if result.get("fragment_id") != fragment_id:
                raise ValueError("nested topic result belongs to a different fragment")
            if result.get("topic_id") and result["topic_id"] != original.get("topic_id"):
                raise ValueError("nested topic result belongs to a different topic")
            result_id = str(result.get("result_id") or "")
            if not result_id or result_id in fragment_results_seen:
                raise ValueError("nested topic result identity is missing or duplicated")
            fragment_results_seen.add(result_id)
            if result.get("paper_ids") and not set(result["paper_ids"]).issubset(paper_ids):
                raise ValueError("nested topic result belongs to a different paper")
            for key in ("result_id", "batch_result_id"):
                if result.get(key):
                    result_ids.add(str(result[key]))
            if result.get("batch_id"):
                batch_ids.add(str(result["batch_id"]))
            collect_output_support(result.get("provider_output") or {})
            context = result.get("interpretation_context") or {}
            if not isinstance(context, Mapping):
                raise ValueError("nested topic interpretation context must be an object")
            for field in context.get("fields") or []:
                if not isinstance(field, Mapping) or not field.get("source_field_id"):
                    raise ValueError("nested topic interpretation field identity is missing")
                field_id = str(field["source_field_id"])
                if not isinstance(field.get("source_value"), str) or not field["source_value"].strip():
                    raise ValueError("nested topic interpretation field has no source text")
                if field.get("paper_key") not in paper_ids:
                    raise ValueError("nested topic interpretation field belongs to a different paper")
                if field_id in field_rows and field_rows[field_id] != dict(field):
                    raise ValueError("nested topic interpretation field has conflicting contents")
                if field_id in all_fields and all_fields[field_id] != dict(field):
                    raise ValueError("nested topic interpretation field has conflicting owners or contents")
                field_rows[field_id] = dict(field)
                all_fields[field_id] = dict(field)
            for dependency in context.get("dependencies") or []:
                if not isinstance(dependency, Mapping):
                    raise ValueError("nested topic interpretation dependency must be an object")
                if dependency.get("paper_key") not in paper_ids:
                    raise ValueError("nested topic interpretation dependency belongs to a different paper")
                if dict(dependency) not in dependencies:
                    dependencies.append(dict(dependency))
        if any(not set(dep.get("required_source_field_ids") or []).issubset(field_rows) for dep in dependencies):
            raise ValueError("nested topic interpretation dependency lost its source text")
        for dependency in dependencies:
            for field_id in dependency.get("required_source_field_ids") or []:
                field = field_rows[field_id]
                if field["paper_key"] != dependency["paper_key"]:
                    raise ValueError("nested topic interpretation dependency has a foreign field")
                if field.get("scope") == "explicit_study" and (
                    not field.get("owner_study_id")
                    or field["owner_study_id"] != dependency.get("owner_study_id")
                ):
                    raise ValueError("nested topic interpretation dependency has a foreign study field")
        fragment_refs = [dict(ref) for ref in refs if ref["fragment_id"] == fragment_id]
        if any(
            str(ref.get(key) or "") not in result_ids
            for ref in fragment_refs for key in ("result_id", "batch_result_id")
            if ref.get(key)
        ):
            raise ValueError("nested topic provider reference has an unknown result")
        if any(ref.get("batch_id") and ref["batch_id"] not in batch_ids for ref in fragment_refs):
            raise ValueError("nested topic provider reference has an unknown batch")
        all_papers.update(paper_ids)
        all_evidence.update(evidence_ids)
        all_batches.update(batch_ids)
        all_results.update(result_ids)
        projected_fragment = deepcopy(dict(fragment))
        for result in projected_fragment.get("provider_results") or []:
            result.pop("interpretation_context", None)
        row = deepcopy(original)
        row.update({
            "fragment_id": fragment_id,
            "fragment_ids": [fragment_id],
            "paper_ids": paper_ids,
            "fragments": [projected_fragment],
            "evidence_unit_indexes": deepcopy(fragment.get("evidence_unit_indexes") or {}),
            "supporting_evidence_ids": sorted(evidence_ids),
            "provider_output_refs": fragment_refs,
            "provider_batch_ids": sorted(batch_ids),
            "provider_calls": len(batch_ids),
            "result_ids": sorted(result_ids),
        })
        if "supporting_evidence_count" in row:
            row["supporting_evidence_count"] = len(row["supporting_evidence_ids"])
        if field_rows or dependencies:
            row["interpretation_context"] = {
                "fields": [field_rows[key] for key in sorted(field_rows)],
                "dependencies": dependencies,
            }
        rows.append(row)
    for key, available in (
        ("paper_ids", all_papers), ("supporting_evidence_ids", all_evidence),
        ("provider_batch_ids", all_batches), ("result_ids", all_results),
    ):
        if not set(original.get(key) or []).issubset(available):
            raise ValueError(f"nested topic aggregate {key} contains a detached reference")
    return rows
