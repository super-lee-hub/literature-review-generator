"""Source-bound scientific view of a locally retained cross-topic aggregate.

Scientific rows remain complete. Large technical ownership ledgers stay in the
Registry aggregate, with exact references checked again before model admission.
"""
from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any

from runtime.provider_runtime import hash_json

_REFERENCE_FIELDS = {"dependency_kind", "job_id", "artifact_id", "artifact_type", "path", "content_hash"}
_TECHNICAL_FIELDS = {"base_item_hashes", "manifest_hashes", "intermediate_item_hashes", "occurrences"}
_FRAGMENT_FIELDS = ("integrated_source_item_hashes", "nonintegrated_source_item_hashes")


def project_global_retention_request(
    request: Mapping[str, Any], *, source_reference: Mapping[str, str],
) -> dict[str, Any]:
    """Replace technical provenance with its exact retained artifact authority."""
    source = request.get("cross_group_comparison")
    if not isinstance(source, Mapping) or not isinstance(source.get("fragment_aggregation"), Mapping):
        raise ValueError("global retention view requires a full fragment aggregate")
    if (set(source_reference) != _REFERENCE_FIELDS
            or any(not isinstance(value, str) or not value for value in source_reference.values())
            or source_reference["dependency_kind"] != "local_job"
            or source_reference["artifact_type"] != "outline_artifact"
            or len(source_reference["content_hash"]) != 64):
        raise ValueError("global retention view requires an exact local Registry reference")
    sidecar = source["fragment_aggregation"]
    if sidecar.get("schema_version") != "outline-semantic-final-fold-fragment-aggregation/v2":
        raise ValueError("unsupported retained fragment authority")
    result = copy.deepcopy(dict(request))
    cross = result["cross_group_comparison"]
    projected = {key: copy.deepcopy(value) for key, value in sidecar.items() if key not in _TECHNICAL_FIELDS}
    projected.update({
        "schema_version": "outline-semantic-final-fold-scientific-view/v1",
        "source_reference": dict(source_reference),
        "expanded_cross_result_hash": hash_json(source),
        "technical_ownership_hash": hash_json(sidecar),
        "source_dispositions": [],
    })
    for occurrence in sidecar.get("occurrences") or []:
        if not isinstance(occurrence, Mapping) or not isinstance(occurrence.get("row"), Mapping):
            raise ValueError("source fragment disposition is malformed")
        projected["source_dispositions"].append({
            "disposition": copy.deepcopy(occurrence["row"]),
            "assigned_paper_ids": copy.deepcopy(occurrence.get("assigned_paper_ids") or []),
            "assigned_fragment_count": len(occurrence.get("assigned_source_item_hashes") or []),
        })
    cross["fragment_aggregation"] = projected
    for row in cross.get("topic_dispositions") or []:
        if not isinstance(row, dict) or "retained_fragment_counts" in row:
            raise ValueError("logical topic disposition is malformed or already projected")
        counts = {}
        for key in _FRAGMENT_FIELDS:
            values = row.pop(key, None)
            if not isinstance(values, list):
                raise ValueError("logical topic disposition lost its exact fragment inventory")
            counts[key] = len(values)
        row["retained_fragment_counts"] = counts
    return result


def expand_global_retention_request(
    request: Mapping[str, Any], *, source: Mapping[str, Any],
) -> dict[str, Any]:
    """Restore full authority, rejecting changes to any transmitted science."""
    cross = request.get("cross_group_comparison")
    sidecar = cross.get("fragment_aggregation") if isinstance(cross, Mapping) else None
    if not isinstance(sidecar, Mapping) or sidecar.get("expanded_cross_result_hash") != hash_json(source):
        raise ValueError("global retention source changed")
    result = copy.deepcopy(dict(request))
    result["cross_group_comparison"] = copy.deepcopy(dict(source))
    if hash_json(project_global_retention_request(result, source_reference=sidecar.get("source_reference") or {})) != hash_json(dict(request)):
        raise ValueError("global retention scientific view changed")
    return result
