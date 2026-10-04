"""Compact wire projection for a locally verified Writer task scope.

Canonical source prose is content-addressed once in ``evidence_store``.
Per-task rows reference those entries while retaining every claim, qualifier,
evidence, field, study, ref, and output-limit identity. Candidate packet copies
and other unverified projections remain local and are never put on the wire.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from outline.v3_models import compute_v3_hash
from services.writer_source_inventory import SOURCE_INVENTORY_ARTIFACT_ID


WRITER_WIRE_SCOPE_VERSION = "writer_task_scope_wire/v1"
_SOURCE_COLLECTIONS = {
    "source_claims": "source_claim",
    "evidence": "evidence",
    "source_fields": "source_field",
    "interpretation_dependencies": "interpretation_dependency",
    "research_units": "research_unit",
}
_TASK_FIELDS = (
    "writer_task_id",
    "task_kind",
    "planned_claim",
    "duplicate_index",
    "status",
    "reason_codes",
    "paper_keys",
    "source_claim_ids",
    "evidence_ids",
    "source_field_ids",
    "qualifier_source_claim_ids",
    "qualifier_evidence_ids",
    "qualifier_source_field_ids",
    "allowed_ref_ids",
    "source_membership_status",
    "output_units",
)


class WriterWireProjectionError(ValueError):
    """A provider-facing scope projection is incomplete or corrupt."""


def _store_entity(store: dict[str, dict[str, Any]], kind: str, value: Any) -> str:
    if not isinstance(value, Mapping):
        raise WriterWireProjectionError(f"canonical source {kind} must be an object")
    normalized = dict(value)
    try:
        digest = compute_v3_hash({"kind": kind, "value": normalized})
    except (TypeError, ValueError) as exc:
        raise WriterWireProjectionError(f"canonical source {kind} is not hashable JSON") from exc
    key = f"source:{digest}"
    entry = {"kind": kind, "value": normalized}
    existing = store.get(key)
    if existing is not None and existing != entry:
        raise WriterWireProjectionError(f"canonical source hash collision at {key}")
    store[key] = entry
    return key


def project_writer_scope_for_provider_v1(scope: Mapping[str, Any]) -> dict[str, Any]:
    """Project a fully verified paragraph scope without duplicating source prose."""
    if not isinstance(scope, Mapping):
        raise WriterWireProjectionError("Writer task scope must be an object")
    basis = str(scope.get("writer_task_basis_hash") or "")
    binding = scope.get("source_inventory_binding")
    output_contract = scope.get("output_contract")
    source_identity_validation = scope.get("source_identity_validation")
    if (
        scope.get("scope_status") != "ready"
        or scope.get("source_authority_status") != "canonical_claim_and_evidence_inventory_verified"
        or scope.get("usable_for_provider_admission") is not True
        or not isinstance(binding, Mapping)
        or binding.get("artifact_id") != SOURCE_INVENTORY_ARTIFACT_ID
        or any(not isinstance(binding.get(key), str) or not binding.get(key) for key in ("artifact_hash", "content_hash"))
        or not re.fullmatch(r"[0-9a-f]{64}", basis)
        or not isinstance(output_contract, Mapping)
        or not isinstance(source_identity_validation, Mapping)
    ):
        raise WriterWireProjectionError("Writer scope is not verified and ready for provider admission")

    raw_tasks = scope.get("tasks")
    if not isinstance(raw_tasks, list) or not raw_tasks:
        raise WriterWireProjectionError("verified Writer scope has no task rows")
    declared_task_ids = scope.get("required_task_ids")
    if not isinstance(declared_task_ids, list) or any(not isinstance(value, str) for value in declared_task_ids):
        raise WriterWireProjectionError("Writer scope required_task_ids must be a string array")
    task_ids = [str(task.get("writer_task_id") or "") for task in raw_tasks if isinstance(task, Mapping)]
    if len(task_ids) != len(raw_tasks) or any(not task_id for task_id in task_ids):
        raise WriterWireProjectionError("Writer scope contains a malformed task row")
    if len(set(task_ids)) != len(task_ids) or declared_task_ids != task_ids:
        raise WriterWireProjectionError("Writer scope task identities are missing, duplicated, or reordered")
    if int(scope.get("task_count") or -1) != len(raw_tasks):
        raise WriterWireProjectionError("Writer scope task_count is inconsistent")

    evidence_store: dict[str, dict[str, Any]] = {}
    tasks: list[dict[str, Any]] = []
    for task in raw_tasks:
        if any(key not in task for key in _TASK_FIELDS):
            raise WriterWireProjectionError("verified Writer task is missing required wire fields")
        if task.get("status") != "ready" or task.get("source_membership_status") != "verified":
            raise WriterWireProjectionError(f"task {task['writer_task_id']} is not source verified")
        bundle = task.get("canonical_source_bundle")
        if not isinstance(bundle, list) or not bundle:
            raise WriterWireProjectionError(f"task {task['writer_task_id']} has no canonical source bundle")
        if not isinstance(task.get("output_units"), list) or not task.get("output_units"):
            raise WriterWireProjectionError(f"task {task['writer_task_id']} has no output units")
        bundle_refs: list[dict[str, Any]] = []
        for source_row in bundle:
            if not isinstance(source_row, Mapping):
                raise WriterWireProjectionError("canonical source bundle contains a non-object row")
            compact_row = {key: value for key, value in source_row.items() if key not in _SOURCE_COLLECTIONS}
            for collection, kind in _SOURCE_COLLECTIONS.items():
                values = source_row.get(collection)
                if not isinstance(values, list):
                    raise WriterWireProjectionError(f"canonical source row {collection} must be an array")
                compact_row[f"{collection}_refs"] = [_store_entity(evidence_store, kind, item) for item in values]
            bundle_refs.append(compact_row)

        task_wire = {key: task.get(key) for key in _TASK_FIELDS}
        task_wire["canonical_source_bundle_refs"] = bundle_refs
        tasks.append(task_wire)

    projected = {
        "schema_version": WRITER_WIRE_SCOPE_VERSION,
        "section_id": scope.get("section_id"),
        "scope_status": scope.get("scope_status"),
        "source_authority_status": scope.get("source_authority_status"),
        "usable_for_provider_admission": scope.get("usable_for_provider_admission"),
        "writer_task_basis_hash": basis,
        "source_inventory_binding": dict(binding),
        "task_count": len(tasks),
        "required_task_ids": list(declared_task_ids),
        "max_output_units": scope.get("max_output_units"),
        "min_required_output_units": scope.get("min_required_output_units"),
        "max_serialized_output_bytes_upper_bound": scope.get("max_serialized_output_bytes_upper_bound"),
        "output_contract": dict(output_contract),
        "source_identity_validation": dict(source_identity_validation),
        "tasks": tasks,
        "evidence_store": {key: evidence_store[key] for key in sorted(evidence_store)},
    }
    if scope.get("schema_version") == "writer_task_scope/v2":
        from services.writer_table_layout import _verified_bound_scope

        _verified_bound_scope(scope)
        projected["schema_version"] = "writer_task_scope_wire/v2"
        projected["table_layouts"] = scope["table_layouts"]
        projected["table_layout_binding"] = scope["table_layout_binding"]
    return projected


def expand_writer_source_bundles_for_test_v1(projection: Mapping[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Validate content-addressed references and reconstruct exact source bundles."""
    if not isinstance(projection, Mapping) or projection.get("schema_version") != WRITER_WIRE_SCOPE_VERSION:
        raise WriterWireProjectionError("Writer wire projection schema is invalid")
    store = projection.get("evidence_store")
    tasks = projection.get("tasks")
    if not isinstance(store, Mapping) or not isinstance(tasks, list):
        raise WriterWireProjectionError("Writer wire projection has no evidence_store or task array")
    task_ids = [str(task.get("writer_task_id") or "") for task in tasks if isinstance(task, Mapping)]
    if (
        len(task_ids) != len(tasks)
        or not task_ids
        or len(task_ids) != len(set(task_ids))
        or projection.get("required_task_ids") != task_ids
        or projection.get("task_count") != len(tasks)
    ):
        raise WriterWireProjectionError("Writer wire task identities are incomplete or inconsistent")
    for key, entry in store.items():
        if not isinstance(key, str) or not isinstance(entry, Mapping) or set(entry) != {"kind", "value"}:
            raise WriterWireProjectionError("Writer evidence_store entry is malformed")
        expected = f"source:{compute_v3_hash({'kind': entry.get('kind'), 'value': entry.get('value')})}"
        if key != expected or entry.get("kind") not in set(_SOURCE_COLLECTIONS.values()) or not isinstance(entry.get("value"), Mapping):
            raise WriterWireProjectionError(f"Writer evidence_store key is invalid: {key}")

    used: set[str] = set()
    result: dict[str, list[dict[str, Any]]] = {}
    for task in tasks:
        if not isinstance(task, Mapping):
            raise WriterWireProjectionError("Writer wire task row is malformed")
        task_id = str(task.get("writer_task_id") or "")
        if not task_id or task_id in result:
            raise WriterWireProjectionError("Writer wire task identity is missing or repeated")
        refs = task.get("canonical_source_bundle_refs")
        if not isinstance(refs, list):
            raise WriterWireProjectionError(f"task {task_id} has no canonical bundle refs")
        restored: list[dict[str, Any]] = []
        for row in refs:
            if not isinstance(row, Mapping):
                raise WriterWireProjectionError(f"task {task_id} has a malformed source row ref")
            restored_row = {key: value for key, value in row.items() if key not in {f"{name}_refs" for name in _SOURCE_COLLECTIONS}}
            for collection, kind in _SOURCE_COLLECTIONS.items():
                entity_refs = row.get(f"{collection}_refs")
                if not isinstance(entity_refs, list):
                    raise WriterWireProjectionError(f"task {task_id} source row has no {collection} refs")
                values: list[dict[str, Any]] = []
                for ref in entity_refs:
                    entry = store.get(ref) if isinstance(ref, str) else None
                    if not isinstance(entry, Mapping) or entry.get("kind") != kind or not isinstance(entry.get("value"), Mapping):
                        raise WriterWireProjectionError(f"task {task_id} contains an invalid {collection} ref")
                    used.add(ref)
                    values.append(dict(entry["value"]))
                restored_row[collection] = values
            restored.append(restored_row)
        result[task_id] = restored
    if used != set(store):
        raise WriterWireProjectionError("Writer evidence_store contains unreferenced source rows")
    return result
