"""Lossless wire projection for overflow arbitration assessment/reducer calls.

Only typed identifiers and hashes are aliased. The semantic request wire
codec interns repeated exact strings into a complete on-wire table; it does
not remove or summarize source literals. The canonical request, response
shape, alias catalog, and provider request remain separate hash domains.
The same exact canonical value can reuse one token across typed roles; the
catalog records each allowed role, and different canonical values never share
a token. Existing semantic validators still determine field ownership.
"""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

from outline.arbitration_response_row_defaults import (
    RESPONSE_ROW_TABLE_SCHEMA_V1,
    RESPONSE_ROW_TABLE_SCHEMA_V2,
    ArbitrationResponseRowDefaultsError,
    compact_not_applicable_layout,
    restore_response_row_defaults,
    upgrade_reducer_response_schema,
    validate_response_row_defaults,
)
from outline.semantic_identifier_aliases import build_identifier_alias_plan
from outline.semantic_request_wire import (
    expand_semantic_request_wire,
    pack_semantic_request_wire,
)
from outline.v3_models import compute_v3_hash

WIRE_SCHEMA = "arbitration_assessment_wire/v1"
WIRE_POLICY = "lossless-typed-assessment-identities-and-exact-strings/v1"
WIRE_KEY = "arbitration_assessment_wire"
ROW_TABLE_MARKER = "arbitration_row_table_v1"
DECISION_VIEW_REFERENCE_SCHEMA = "candidate-units-reference-critical-view-wire/v1"
_REDUCTION_INPUT_SCHEMA = "arbitration_reduction_input/v2"
ROW_TABLE_FIELDS = {
    "review_group_map", "source_support_catalog", "candidate_units", "unit_rows", "sections",
    "claim_slots", "decision_views", "views", "source_literal_bundle_catalog",
    "literal_qualifier_values", "source_claims", "evidence", "source_fields", "section_bindings",
    "assessments", "view_assessments", "group_contributions", "group_summaries",
    "candidate_group_summaries", "child_lineage", "typed_lineage", "response_echo_bindings",
}
WIRE_CONTRACT = (
    "Read source data at topic_synthesis[0].assessment_request. Expand semantic_source_wire exact strings and request row tables "
    "before assessment. In candidate_units, decision_view_ids are ordered references to full rows in critical_view_wire.views, "
    "matched by candidate_id and decision_view_id. Opaque typed aliases must be copied exactly into their same typed response "
    "fields; never put aliases in prose. response_row_table_schemas.layouts holds complete ordered columns once; encode each "
    "declared multirow response array as {arbitration_row_table_v1:{layout_ids:[global ids],rows:[[global_id,[cells]],...]}}. "
    "Request row tables use {arbitration_row_table_v1:{columns:[layouts],rows:[[local_id,[cells]],...]}}. Echo response_echo_bindings "
    "hash aliases exactly. Preserve every fact, literal, qualifier, null/reverse finding, minority view and unknown field."
)
_REDUCER_GROUP_BINDINGS_MARKER = "arbitration_claim_group_bindings_v1"
_REDUCER_GROUP_BINDINGS_SCHEMA = "arbitration_claim_group_bindings/v1"
_REDUCER_GROUP_BINDING_FIELDS = (
    "claim_group_task_bindings",
    "claim_group_assertion_bindings",
    "claim_group_slot_bindings",
)
_REDUCER_GROUP_BINDING_COLUMNS = [
    "claim_group_ids", "task_ids", "assertion_ids", "slot_ids",
]
_REDUCER_WIRE_CONTRACT = (
    "Read topic_synthesis[0].assessment_request. Expand exact strings in semantic_source_wire and request row tables; "
    "unpack the aligned group_map binding table. Resolve candidate_units.decision_view_ids in order to "
    "critical_view_wire.views by candidate_id and decision_view_id. Encode declared response tables with "
    "response_row_table_schemas, row_defaults and compact_contract as "
    "{arbitration_row_table_v1:{layout_ids:[ids],rows:[[id,[cells]],...]}}. Echo typed aliases and "
    "response_echo_bindings group_id/group_hash pairs exactly in typed fields; never put aliases in prose. "
    "Preserve every source fact, literal, qualifier, null/reverse/minority finding and unknown field."
)
_HASH_RE = re.compile(r"[0-9a-f]{64}")
_CATALOG_KEYS = {
    "schema_version", "policy", "wire_contract", "canonical_schema_version",
    "node_id", "canonical_request_hash", "canonical_response_shape_hash",
    "wire_request_body_hash", "wire_response_shape_hash", "alias_prefix",
    "alias_entries", "source_reference_kinds", "alias_mapping_hash", "response_echo_bindings",
    "response_echo_bindings_hash", "response_row_table_schemas", "response_row_table_schema_hash",
    "literal_string_table",
    "literal_table_hash", "semantic_source_wire_hash", "catalog_hash",
    "decision_view_reference_schema", "decision_view_reference_hash",
}
_WIRE_METADATA_KEYS = {
    "schema_version", "policy", "contract", "canonical_schema_version", "node_id",
    "alias_prefix", "catalog_hash",
}

_ID_FIELDS = {
    "candidate_id": "candidate_id", "unit_id": "unit_id", "section_id": "section_id",
    "section_ids": "section_id", "claim_id": "claim_id", "claim_slot_id": "claim_slot_id",
    "claim_group_id": "claim_group_id", "group_id": "group_id", "decision_view_id": "decision_view_id",
    "view_id": "decision_view_id", "issue_id": "issue_id", "task_id": "task_id",
    "topic_id": "topic_id", "fragment_id": "fragment_id", "provider_result_id": "result_id",
    "synthesis_claim_id": "claim_id", "primary_claim_id": "claim_id", "paper_key": "paper_id",
    "paper_id": "paper_id", "study_id": "study_id", "source_relation_group_id": "claim_group_id",
    "comparison_id": "comparison_id", "tree_node_id": "tree_node_id", "child_node_id": "child_node_id",
    "source_fact_id": "source_fact_id", "recommendation_id": "recommendation_id",
    "source_field_id": "source_field_id", "evidence_id": "evidence_id", "relation_id": "relation_id",
    "artifact_id": "artifact_id", "primary_evidence_id": "evidence_id",
}
_ID_ARRAY_FIELDS = {
    "candidate_ids": "candidate_id", "required_candidate_ids": "candidate_id",
    "unit_ids": "unit_id", "required_unit_ids": "unit_id", "section_ids": "section_id",
    "source_section_ids": "section_id", "claim_ids": "claim_id", "source_claim_ids": "claim_id",
    "required_source_claim_ids": "claim_id", "qualifier_source_claim_ids": "claim_id",
    "target_claim_ids": "claim_id", "claim_slot_ids": "claim_slot_id", "claim_group_ids": "claim_group_id",
    "group_ids": "group_id", "decision_view_ids": "decision_view_id", "view_ids": "decision_view_id",
    "issue_ids": "issue_id", "typed_issue_ids": "issue_id", "critic_issue_ids": "issue_id",
    "required_comparison_ids": "comparison_id", "comparison_ids": "comparison_id",
    "task_ids": "task_id", "topic_ids": "topic_id", "fragment_ids": "fragment_id",
    "paper_keys": "paper_id", "paper_ids": "paper_id", "relation_ids": "relation_id",
    "evidence_ids": "evidence_id", "evidence_refs": "source_ref_id",
    "counterevidence_refs": "source_ref_id", "literal_view_ref_ids": "source_ref_id",
    "source_field_ids": "source_field_id",
    "artifact_ids": "artifact_id", "result_ids": "result_id", "candidate_ids_in_order": "candidate_id",
    "source_scope_ids": "source_scope_id", "target_ids": "target_id",
    "child_node_ids": "child_node_id",
}
_REDUCTION_ID_ARRAY_FIELDS = {
    "used_assertion_ids": "claim_id",
}
_HASH_FIELDS = {
    "candidate_hash", "unit_hash", "section_hash", "decision_view_hash", "view_hash",
    "group_hash", "source_fact_hash", "source_binding_hash", "source_closure_hash",
    "artifact_hash",
    "source_inventory_artifact_hash", "source_inventory_content_hash", "candidate_output_scope_hash",
    "review_group_map_hash", "view_set_hash", "work_plan_hash", "plan_input_hash",
    "legacy_plan_hash", "legacy_ledger_hash", "legacy_ledger_wire_hash", "claim_hash",
    "support_row_hash", "part_hash", "leaf_result_hash", "child_result_hash",
    "source_support_catalog_hash", "source_view_set_hash", "source_literal_bundle_catalog_hash",
    "literal_bundle_catalog_hash",
    "result_hash", "wire_body_hash", "literal_bundle_hash", "bundle_hash",
    "canonical_partition_hash", "coverage_hash", "content_hash", "input_hash",
}
_HASH_ARRAY_FIELDS = {
    "unit_hashes", "section_hashes", "decision_view_hashes", "child_result_hashes",
    "leaf_result_hashes", "candidate_hashes_in_order",
}
_MAP_KEY_FIELDS = {
    "candidate_hashes": "candidate_id",
    "claim_group_task_bindings": "claim_group_id",
    "claim_group_assertion_bindings": "claim_group_id",
    "comparison_relation_bindings": "comparison_id",
    "issue_rows": "issue_id",
    "candidate_section_index": "candidate_id",
    "candidate_section_hashes": "candidate_id",
    "candidate_rows_by_id": "candidate_id",
    "candidate_scores": "candidate_id",
    "candidate_group_summaries": "candidate_id",
    "group_views": "group_id",
    "group_evidence": "group_id",
    "group_hashes": "group_id",
    "view_hashes": "decision_view_id",
    "candidate_view_bindings": "candidate_id",
}
_REDUCTION_MAP_KEY_FIELDS = {
    "used_assertion_ids_by_candidate": "candidate_id",
    "claim_group_slot_bindings": "claim_group_id",
}
_REDUCTION_MAP_VALUE_ARRAY_FIELDS = {
    "used_assertion_ids_by_candidate": "claim_id",
    "claim_group_slot_bindings": "claim_slot_id",
}
_MAP_VALUE_FIELDS = {
    "candidate_hashes": "candidate_hash",
    "claim_group_task_bindings": "task_id",
    "claim_group_assertion_bindings": "claim_id",
    "comparison_relation_bindings": "claim_group_id",
    "group_hashes": "group_hash",
    "view_hashes": "decision_view_hash",
}


class ArbitrationAssessmentWireError(ValueError):
    """The assessment wire or its local lossless projection catalog is invalid."""


def _wire_contract_for_schema(canonical_schema_version: str) -> str:
    if canonical_schema_version == _REDUCTION_INPUT_SCHEMA:
        return _REDUCER_WIRE_CONTRACT
    return WIRE_CONTRACT


def _json_copy(value: Any) -> Any:
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise ArbitrationAssessmentWireError("assessment wire values must be finite JSON data") from exc


def _pack_row_tables(
    value: Any,
    field: str | None = None,
    *,
    reducer_input: bool = False,
) -> Any:
    if isinstance(value, Mapping):
        if ROW_TABLE_MARKER in value:
            raise ArbitrationAssessmentWireError("assessment source collides with the row-table marker")
        return {
            key: _pack_row_tables(item, key, reducer_input=reducer_input)
            for key, item in value.items()
        }
    if isinstance(value, list):
        if (
            (field in ROW_TABLE_FIELDS or (reducer_input and field == "groups"))
            and len(value) >= 2
            and all(isinstance(row, Mapping) and row for row in value)
        ):
            layouts: list[list[str]] = []
            layout_indexes: dict[tuple[str, ...], int] = {}
            rows = []
            for row in value:
                if any(not isinstance(key, str) for key in row):
                    raise ArbitrationAssessmentWireError("row-table object keys must be strings")
                columns = tuple(sorted(row))
                index = layout_indexes.get(columns)
                if index is None:
                    index = len(layouts)
                    layout_indexes[columns] = index
                    layouts.append(list(columns))
                rows.append([
                    index,
                    [_pack_row_tables(row[key], key, reducer_input=reducer_input) for key in columns],
                ])
            return {ROW_TABLE_MARKER: {"columns": layouts, "rows": rows}}
        return [_pack_row_tables(item, field, reducer_input=reducer_input) for item in value]
    return copy.deepcopy(value)


def _expand_row_tables(
    value: Any,
    field: str | None = None,
    *,
    reducer_input: bool = False,
) -> Any:
    if isinstance(value, Mapping):
        if ROW_TABLE_MARKER in value:
            allowed = field in ROW_TABLE_FIELDS or (reducer_input and field == "groups")
            if not allowed or set(value) != {ROW_TABLE_MARKER}:
                raise ArbitrationAssessmentWireError("assessment row-table marker is misplaced")
            table = value[ROW_TABLE_MARKER]
            if not isinstance(table, Mapping) or set(table) != {"columns", "rows"}:
                raise ArbitrationAssessmentWireError("assessment row table has invalid fields")
            layouts, rows = table["columns"], table["rows"]
            if not isinstance(layouts, list) or not layouts or not isinstance(rows, list) or len(rows) < 2:
                raise ArbitrationAssessmentWireError("assessment row table has no layouts or rows")
            normalized_layouts: list[list[str]] = []
            for layout in layouts:
                if (
                    not isinstance(layout, list)
                    or any(not isinstance(key, str) for key in layout)
                    or layout != sorted(set(layout))
                    or layout in normalized_layouts
                ):
                    raise ArbitrationAssessmentWireError("assessment row-table layout is malformed or duplicated")
                normalized_layouts.append(layout)
            used_layouts: set[int] = set()
            restored_rows = []
            for row in rows:
                if (
                    not isinstance(row, list)
                    or len(row) != 2
                    or type(row[0]) is not int
                    or not 0 <= row[0] < len(layouts)
                    or not isinstance(row[1], list)
                    or len(row[1]) != len(layouts[row[0]])
                ):
                    raise ArbitrationAssessmentWireError("assessment row-table row is malformed")
                index, values = row
                used_layouts.add(index)
                restored_rows.append({
                    key: _expand_row_tables(item, key, reducer_input=reducer_input)
                    for key, item in zip(layouts[index], values, strict=True)
                })
            if used_layouts != set(range(len(layouts))):
                raise ArbitrationAssessmentWireError("assessment row table contains an unused layout")
            return restored_rows
        return {
            key: _expand_row_tables(item, key, reducer_input=reducer_input)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_expand_row_tables(item, field, reducer_input=reducer_input) for item in value]
    return copy.deepcopy(value)


def _all_strings(value: Any):
    if isinstance(value, Mapping):
        for key, item in value.items():
            if isinstance(key, str):
                yield key
            yield from _all_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _all_strings(item)
    elif isinstance(value, str):
        yield value


def _issue_target_kind(scope: Any) -> str:
    return {
        "candidate": "candidate_id", "section": "section_id", "claim": "claim_id",
    }.get(str(scope or "").casefold(), "target_id")


def _field_kind(field: str, parent: Any = None) -> str | None:
    if field == "node_id":
        return None
    if field == "source_scope_ids" and isinstance(parent, Mapping):
        if parent.get("group_kind") == "typed_issue":
            return _issue_target_kind(parent.get("issue_scope"))
        return "claim_group_id"
    if field == "target_ids" and isinstance(parent, Mapping):
        return _issue_target_kind(parent.get("scope") or parent.get("issue_scope"))
    if field in _ID_FIELDS:
        return _ID_FIELDS[field]
    if field in _HASH_FIELDS:
        return field
    return None


def _typed_occurrences(
    value: Any,
    domain: str,
    *,
    reducer_input: bool = False,
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []

    def add(path: list[Any], kind: str, canonical: str) -> None:
        if canonical:
            result.append({"domain": domain, "path": list(path), "kind": kind, "canonical_value": canonical})

    def walk(item: Any, path: list[Any], field: str | None = None, parent: Any = None) -> None:
        if isinstance(item, Mapping):
            if field == "group_views_by_candidate":
                for candidate, groups in item.items():
                    if isinstance(candidate, str):
                        add([*path, {"map_key": candidate}], "candidate_id", candidate)
                    if not isinstance(groups, Mapping):
                        walk(groups, [*path, candidate], None, item)
                        continue
                    for group_id, view_ids in groups.items():
                        if isinstance(group_id, str):
                            add([*path, candidate, {"map_key": group_id}], "group_id", group_id)
                        walk(view_ids, [*path, candidate, group_id], "decision_view_ids", groups)
                return
            if field == "group_evidence_by_candidate":
                for candidate, groups in item.items():
                    if isinstance(candidate, str):
                        add([*path, {"map_key": candidate}], "candidate_id", candidate)
                    if not isinstance(groups, Mapping):
                        walk(groups, [*path, candidate], None, item)
                        continue
                    for group_id, evidence_ids in groups.items():
                        if isinstance(group_id, str):
                            add([*path, candidate, {"map_key": group_id}], "group_id", group_id)
                        walk(evidence_ids, [*path, candidate, group_id], "evidence_refs", groups)
                return
            if field in {"group_views", "group_evidence"}:
                value_field = "decision_view_ids" if field == "group_views" else "evidence_refs"
                for group_id, values in item.items():
                    if isinstance(group_id, str):
                        add([*path, {"map_key": group_id}], "group_id", group_id)
                    walk(values, [*path, group_id], value_field, item)
                return
            # A row inside an array has schema column names, not ID map keys.
            # The same field name can otherwise describe a keyed mapping.
            row_object = isinstance(parent, list)
            key_kind = None if row_object else _MAP_KEY_FIELDS.get(field or "")
            value_kind = None if row_object else _MAP_VALUE_FIELDS.get(field or "")
            value_array_kind = None
            if reducer_input and not row_object:
                key_kind = _REDUCTION_MAP_KEY_FIELDS.get(field or "", key_kind)
                value_array_kind = _REDUCTION_MAP_VALUE_ARRAY_FIELDS.get(field or "")
            for key, child in item.items():
                if not isinstance(key, str):
                    continue
                if key_kind is not None:
                    add([*path, {"map_key": key}], key_kind, key)
                child_path = [*path, key]
                if value_kind is not None and isinstance(child, str):
                    add(child_path, value_kind, child)
                elif value_array_kind is not None and isinstance(child, list):
                    for index, list_item in enumerate(child):
                        if isinstance(list_item, str):
                            add([*child_path, index], value_array_kind, list_item)
                        else:
                            walk(list_item, [*child_path, index], None, child)
                else:
                    walk(child, child_path, key, item)
            return
        if isinstance(item, list):
            array_kind = (
                _field_kind(field or "", parent)
                or _ID_ARRAY_FIELDS.get(field or "")
                or (_REDUCTION_ID_ARRAY_FIELDS.get(field or "") if reducer_input else None)
            )
            hash_array = field in _HASH_ARRAY_FIELDS if field is not None else False
            for index, child in enumerate(item):
                child_path = [*path, index]
                if array_kind is not None and isinstance(child, str):
                    add(child_path, array_kind, child)
                elif hash_array and isinstance(child, str) and _HASH_RE.fullmatch(child):
                    add(child_path, str(field), child)
                else:
                    walk(child, child_path, field, item)
            return
        if isinstance(item, str):
            kind = _field_kind(field or "", parent)
            if kind is not None:
                if kind not in _ID_FIELDS.values() and kind not in _ID_ARRAY_FIELDS.values() and not _HASH_RE.fullmatch(item):
                    raise ArbitrationAssessmentWireError(f"typed hash field {field} is not a SHA-256")
                add(path, kind, item)

    walk(value, [])
    result.sort(key=_entry_sort_key)
    return result


def _entry_sort_key(row: Mapping[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(row.get("domain") or ""),
        json.dumps(row.get("path"), ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        str(row.get("kind") or ""),
        str(row.get("canonical_value") or ""),
    )


def _source_reference_kinds(request: Mapping[str, Any]) -> dict[str, list[str]]:
    """Record real source ID kinds for the response's union-valued ref fields."""
    observed: dict[str, set[str]] = {}
    field_kinds = {
        "source_claim_id": "source_claim_id",
        "source_claim_ids": "source_claim_id",
        "required_source_claim_ids": "source_claim_id",
        "qualifier_source_claim_ids": "source_claim_id",
        "primary_claim_id": "source_claim_id",
        "evidence_id": "evidence_id",
        "evidence_ids": "evidence_id",
        "primary_evidence_id": "evidence_id",
        "required_evidence_ids": "evidence_id",
        "qualifier_evidence_ids": "evidence_id",
        "source_field_id": "source_field_id",
        "source_field_ids": "source_field_id",
        "required_source_field_ids": "source_field_id",
        "qualifier_source_field_ids": "source_field_id",
    }

    def collect(value: Any, kind: str) -> None:
        if isinstance(value, str) and value:
            observed.setdefault(value, set()).add(kind)
        elif isinstance(value, list):
            for row in value:
                collect(row, kind)

    def walk(value: Any, path: list[Any]) -> None:
        if isinstance(value, Mapping):
            for key, item in value.items():
                if not isinstance(key, str):
                    continue
                kind = field_kinds.get(key)
                if key == "claim_id" and "source_claims" in path:
                    kind = "source_claim_id"
                if kind is not None:
                    collect(item, kind)
                walk(item, [*path, key])
        elif isinstance(value, list):
            for index, row in enumerate(value):
                walk(row, [*path, index])

    walk(request, [])
    return {value: sorted(kinds) for value, kinds in sorted(observed.items())}


def _path_key(path: Sequence[Any]) -> str:
    return json.dumps(list(path), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _alias_seed(
    request: Mapping[str, Any], response_shape: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, str], dict[str, list[str]]]:
    reducer_input = request.get("schema_version") == _REDUCTION_INPUT_SCHEMA
    request_entries = _typed_occurrences(request, "request", reducer_input=reducer_input)
    response_entries = _typed_occurrences(response_shape, "response")
    request_pairs = {(row["kind"], row["canonical_value"]) for row in request_entries}
    source_reference_kinds = _source_reference_kinds(request)
    request_pairs.update(("source_ref_id", value) for value in source_reference_kinds)
    response_echo_entries = [row for row in response_entries if row["kind"] == "group_hash"]
    alias_pairs = request_pairs | {
        (row["kind"], row["canonical_value"]) for row in response_echo_entries
    }
    aliases_to_use = sorted({value for _kind, value in alias_pairs})
    seed = {
        "claim_ids": aliases_to_use,
        # The generic identifier codec uses this only to select a collision-safe
        # prefix. It does not alias values in this field.
        "prefix_context": sorted(set(_all_strings(request)) | set(_all_strings(response_shape))),
    }
    try:
        generic_catalog, _generic_wire = build_identifier_alias_plan(seed)
    except (TypeError, ValueError, KeyError) as exc:
        raise ArbitrationAssessmentWireError("structural alias seed could not be built") from exc
    forward = dict(generic_catalog["forward"])
    # Reuse the generic value->token map: identical canonical strings share a
    # compact token, while path/kind entries below constrain every echo.
    entries = list(request_entries)
    entries.extend(
        row for row in response_entries
        if (row["kind"], row["canonical_value"]) in request_pairs
        or row["kind"] == "group_hash"
    )
    for row in entries:
        row["alias"] = forward[row["canonical_value"]]
    entries.sort(key=_entry_sort_key)
    return {"generic_catalog": generic_catalog, "entries": entries}, forward, source_reference_kinds


def _response_echo_bindings(
    response_shape: Mapping[str, Any],
    alias_by_value: Mapping[str, str],
    *,
    reducer_input: bool = False,
) -> list[dict[str, str]]:
    rows = []
    for field in ("group_contributions", "group_summaries"):
        for row in response_shape.get(field) or ():
            if not isinstance(row, Mapping):
                continue
            candidate_id = row.get("candidate_id")
            group_id = row.get("group_id")
            group_hash = row.get("group_hash")
            if (
                not isinstance(candidate_id, str) or not candidate_id
                or not isinstance(group_id, str) or not group_id
                or not isinstance(group_hash, str) or not group_hash
            ):
                continue
            if group_hash not in alias_by_value:
                raise ArbitrationAssessmentWireError("response-only group hash has no deterministic echo alias")
            rows.append({
                "candidate_id": alias_by_value.get(candidate_id, candidate_id),
                "group_id": alias_by_value.get(group_id, group_id),
                "group_hash": alias_by_value[group_hash],
            })
    rows.sort(key=lambda row: (row["candidate_id"], row["group_id"], row["group_hash"]))
    if len({(row["candidate_id"], row["group_id"]) for row in rows}) != len(rows):
        raise ArbitrationAssessmentWireError("response group hash echo bindings repeat a candidate/group pair")
    if reducer_input:
        hashes_by_group: dict[str, str] = {}
        for row in rows:
            group_id = row["group_id"]
            group_hash = row["group_hash"]
            previous = hashes_by_group.setdefault(group_id, group_hash)
            if previous != group_hash:
                raise ArbitrationAssessmentWireError("reducer group hash echo differs across candidates")
        return [
            {"group_id": group_id, "group_hash": hashes_by_group[group_id]}
            for group_id in sorted(hashes_by_group)
        ]
    return rows


def _compact_reducer_group_bindings(value: Any, *, reducer_input: bool) -> Any:
    """Losslessly align reducer-only group binding maps after typed aliasing."""
    result = copy.deepcopy(value)
    if not reducer_input or not isinstance(result, dict):
        return result
    group_map = result.get("group_map")
    if not isinstance(group_map, dict):
        return result
    if _REDUCER_GROUP_BINDINGS_MARKER in group_map:
        raise ArbitrationAssessmentWireError("reducer group binding marker collides with source data")
    maps = [group_map.get(field) for field in _REDUCER_GROUP_BINDING_FIELDS]
    if not all(isinstance(item, Mapping) for item in maps):
        return result
    task_bindings, assertion_bindings, slot_bindings = maps
    assert isinstance(task_bindings, Mapping)
    assert isinstance(assertion_bindings, Mapping)
    assert isinstance(slot_bindings, Mapping)
    if any(
        not isinstance(group_id, str) or not group_id
        for bindings in (task_bindings, assertion_bindings, slot_bindings)
        for group_id in bindings
    ):
        return result
    group_ids = sorted(task_bindings)
    if (
        set(task_bindings) != set(assertion_bindings)
        or set(task_bindings) != set(slot_bindings)
        or any(not isinstance(task_bindings[group_id], str) or not task_bindings[group_id] for group_id in group_ids)
        or any(not isinstance(assertion_bindings[group_id], str) or not assertion_bindings[group_id] for group_id in group_ids)
        or any(
            not isinstance(slot_bindings[group_id], list)
            or any(not isinstance(slot_id, str) or not slot_id for slot_id in slot_bindings[group_id])
            for group_id in group_ids
        )
    ):
        return result
    compact = {
        "schema_version": _REDUCER_GROUP_BINDINGS_SCHEMA,
        "columns": copy.deepcopy(_REDUCER_GROUP_BINDING_COLUMNS),
        "claim_group_ids": group_ids,
        "task_ids": [task_bindings[group_id] for group_id in group_ids],
        "assertion_ids": [assertion_bindings[group_id] for group_id in group_ids],
        "slot_ids": [copy.deepcopy(slot_bindings[group_id]) for group_id in group_ids],
    }
    for field in _REDUCER_GROUP_BINDING_FIELDS:
        group_map.pop(field)
    group_map[_REDUCER_GROUP_BINDINGS_MARKER] = compact
    return result


def _expand_reducer_group_bindings(value: Any, *, reducer_input: bool) -> Any:
    """Restore reducer-only aligned group bindings before typed alias decoding."""
    result = copy.deepcopy(value)
    if not isinstance(result, dict):
        return result
    group_map = result.get("group_map")
    if not isinstance(group_map, dict):
        return result
    has_marker = _REDUCER_GROUP_BINDINGS_MARKER in group_map
    has_shadow_maps = any(field in group_map for field in _REDUCER_GROUP_BINDING_FIELDS)
    if not has_marker:
        return result
    if not reducer_input:
        raise ArbitrationAssessmentWireError("reducer group binding marker is not allowed in leaf requests")
    if has_shadow_maps:
        raise ArbitrationAssessmentWireError("reducer group binding marker coexists with shadow binding maps")
    compact = group_map[_REDUCER_GROUP_BINDINGS_MARKER]
    if (
        not isinstance(compact, Mapping)
        or set(compact) != {
            "schema_version", "columns", "claim_group_ids", "task_ids", "assertion_ids", "slot_ids",
        }
        or compact.get("schema_version") != _REDUCER_GROUP_BINDINGS_SCHEMA
        or compact.get("columns") != _REDUCER_GROUP_BINDING_COLUMNS
    ):
        raise ArbitrationAssessmentWireError("reducer group binding table has an unknown schema or columns")
    group_ids = compact.get("claim_group_ids")
    task_ids = compact.get("task_ids")
    assertion_ids = compact.get("assertion_ids")
    slot_ids = compact.get("slot_ids")
    if (
        not isinstance(group_ids, list)
        or not isinstance(task_ids, list)
        or not isinstance(assertion_ids, list)
        or not isinstance(slot_ids, list)
        or len({len(group_ids), len(task_ids), len(assertion_ids), len(slot_ids)}) != 1
        or any(not isinstance(group_id, str) or not group_id for group_id in group_ids)
        or len(set(group_ids)) != len(group_ids)
        or any(not isinstance(task_id, str) or not task_id for task_id in task_ids)
        or any(not isinstance(assertion_id, str) or not assertion_id for assertion_id in assertion_ids)
        or any(
            not isinstance(group_slots, list)
            or any(not isinstance(slot_id, str) or not slot_id for slot_id in group_slots)
            for group_slots in slot_ids
        )
    ):
        raise ArbitrationAssessmentWireError("reducer group binding table has malformed or misaligned rows")
    group_map.pop(_REDUCER_GROUP_BINDINGS_MARKER)
    group_map["claim_group_task_bindings"] = dict(zip(group_ids, task_ids, strict=True))
    group_map["claim_group_assertion_bindings"] = dict(zip(group_ids, assertion_ids, strict=True))
    group_map["claim_group_slot_bindings"] = dict(zip(group_ids, slot_ids, strict=True))
    return result


def _response_row_table_schemas(value: Any) -> list[dict[str, Any]]:
    schemas: list[dict[str, Any]] = []

    def template(path: Sequence[Any]) -> list[Any]:
        result = []
        for part in path:
            if type(part) is int:
                result.append("*")
            elif isinstance(part, Mapping) and "row" in part and "column" in part:
                result.append({"row": "*", "column": part["column"]})
            else:
                result.append(copy.deepcopy(part))
        return result

    def walk(item: Any, path: list[Any], parent_identity: Mapping[str, Any] | None = None) -> None:
        if isinstance(item, Mapping) and ROW_TABLE_MARKER in item:
            table = item[ROW_TABLE_MARKER]
            layouts = table["columns"]
            rows = table["rows"]
            descriptor = {
                "path_template": template(path),
                "columns": copy.deepcopy(layouts),
                "row_count": len(rows),
                "row_layout_indices": [int(row[0]) for row in rows],
            }
            if parent_identity:
                descriptor["parent_identity"] = copy.deepcopy(dict(parent_identity))
            schemas.append(descriptor)
            for row_index, (layout_index, cells) in enumerate(rows):
                row_values = dict(zip(layouts[layout_index], cells, strict=True))
                identity = {
                    key: row_values[key]
                    for key in ("candidate_id", "unit_id", "group_id", "decision_view_id")
                    if isinstance(row_values.get(key), str)
                }
                for column, cell in zip(layouts[layout_index], cells, strict=True):
                    walk(cell, [*path, {"row": row_index, "column": column}], identity or parent_identity)
            return
        if isinstance(item, Mapping):
            for key, child in item.items():
                walk(child, [*path, key], parent_identity)
        elif isinstance(item, list):
            for index, child in enumerate(item):
                walk(child, [*path, index], parent_identity)

    walk(value, [])
    schemas.sort(key=lambda row: (
        json.dumps(row["path_template"], ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        json.dumps(row.get("parent_identity") or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
    ))
    return schemas


def _compact_response_row_table_schemas(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    layout_keys = sorted({
        tuple(layout)
        for row in rows
        for layout in row.get("columns") or ()
    })
    layout_id = {layout: index for index, layout in enumerate(layout_keys)}
    by_path: dict[str, dict[str, Any]] = {}
    for row in rows:
        path = list(row["path_template"])
        path_key = _path_key(path)
        table = by_path.setdefault(path_key, {
            "path_template": path,
            "layout_ids": set(),
        })
        local_to_global = [layout_id[tuple(layout)] for layout in row["columns"]]
        table["layout_ids"].update(local_to_global)
    tables = []
    for table in sorted(by_path.values(), key=lambda row: _path_key(row["path_template"])):
        tables.append({
            "path_template": table["path_template"],
            "layout_ids": sorted(table["layout_ids"]),
        })
    return {
        "schema_version": "arbitration_assessment_response_row_tables/v1",
        "row_table_marker": ROW_TABLE_MARKER,
        "layouts": [list(layout) for layout in layout_keys],
        "tables": tables,
    }


def _path_template_key(path: Sequence[Any]) -> str:
    normalized = []
    for part in path:
        if type(part) is int:
            normalized.append("*")
        elif isinstance(part, Mapping) and "row" in part and "column" in part:
            normalized.append({"row": "*", "column": part["column"]})
        else:
            normalized.append(copy.deepcopy(part))
    return _path_key(normalized)


def _response_table_path_map(schema_catalog: Mapping[str, Any]) -> dict[str, set[int]]:
    return {
        _path_key(table["path_template"]): set(table["layout_ids"])
        for table in schema_catalog["tables"]
    }


def _response_tables_use_global_layouts(value: Any, schema_catalog: Mapping[str, Any]) -> Any:
    """Replace embedded response columns with references to the request's global layout catalog."""
    try:
        validate_response_row_defaults(schema_catalog)
    except ArbitrationResponseRowDefaultsError as exc:
        raise ArbitrationAssessmentWireError("response row-default schema is invalid") from exc
    layouts = schema_catalog["layouts"]
    global_id = {tuple(layout): index for index, layout in enumerate(layouts)}
    path_layouts = _response_table_path_map(schema_catalog)

    def walk(item: Any, path: list[Any], field: str | None = None) -> Any:
        if isinstance(item, Mapping) and ROW_TABLE_MARKER in item:
            table = item[ROW_TABLE_MARKER]
            allowed = path_layouts.get(_path_template_key(path))
            if allowed is None:
                raise ArbitrationAssessmentWireError("response row table is absent from the declared schema")
            rows = []
            used: set[int] = set()
            for row_index, (local_index, cells) in enumerate(table["rows"]):
                columns = table["columns"][local_index]
                layout_index = global_id.get(tuple(columns))
                if layout_index is None or layout_index not in allowed:
                    raise ArbitrationAssessmentWireError("response row uses an undeclared column layout")
                row_values = dict(zip(columns, cells, strict=True))
                if (
                    schema_catalog.get("schema_version") == RESPONSE_ROW_TABLE_SCHEMA_V2
                    and path == ["group_summaries"]
                ):
                    try:
                        compact_layout_id = compact_not_applicable_layout(
                            row_values, sorted(allowed), schema_catalog,
                        )
                    except ArbitrationResponseRowDefaultsError as exc:
                        raise ArbitrationAssessmentWireError("response group row-default projection failed") from exc
                    if compact_layout_id is not None:
                        layout_index = compact_layout_id
                        columns = layouts[layout_index]
                        cells = [row_values[column] for column in columns]
                used.add(layout_index)
                rows.append([
                    layout_index,
                    [
                        walk(cell, [*path, {"row": row_index, "column": column}])
                        for column, cell in zip(columns, cells, strict=True)
                    ],
                ])
            return {ROW_TABLE_MARKER: {"layout_ids": sorted(used), "rows": rows}}
        if isinstance(item, Mapping):
            return {key: walk(child, [*path, key]) for key, child in item.items()}
        if isinstance(item, list):
            return [walk(child, [*path, index]) for index, child in enumerate(item)]
        return copy.deepcopy(item)

    return walk(value, [])


def _expand_response_global_layout_tables(value: Any, schema_catalog: Mapping[str, Any]) -> Any:
    """Expand response tables using the full column layouts supplied in the wire request."""
    try:
        validate_response_row_defaults(schema_catalog)
    except ArbitrationResponseRowDefaultsError as exc:
        raise ArbitrationAssessmentWireError("response row-default schema is invalid") from exc
    layouts = schema_catalog["layouts"]
    path_layouts = _response_table_path_map(schema_catalog)

    def walk(item: Any, path: list[Any], field: str | None = None) -> Any:
        if isinstance(item, Mapping) and ROW_TABLE_MARKER in item:
            if set(item) != {ROW_TABLE_MARKER}:
                raise ArbitrationAssessmentWireError("response row-table marker has unknown fields")
            table = item[ROW_TABLE_MARKER]
            if not isinstance(table, Mapping) or set(table) != {"layout_ids", "rows"}:
                raise ArbitrationAssessmentWireError("response global row table has invalid fields")
            allowed = path_layouts.get(_path_template_key(path))
            layout_ids, rows = table["layout_ids"], table["rows"]
            if (
                allowed is None
                or not isinstance(layout_ids, list)
                or layout_ids != sorted(set(layout_ids))
                or any(type(index) is not int or index not in allowed for index in layout_ids)
                or not isinstance(rows, list)
                or len(rows) < 2
            ):
                raise ArbitrationAssessmentWireError("response global row table layout declaration is invalid")
            if any(
                not isinstance(row, list)
                or len(row) != 2
                or type(row[0]) is not int
                or row[0] not in layout_ids
                or not isinstance(row[1], list)
                or len(row[1]) != len(layouts[row[0]])
                for row in rows
            ):
                raise ArbitrationAssessmentWireError("response global row table row shape is invalid")
            if sorted({row[0] for row in rows}) != layout_ids:
                raise ArbitrationAssessmentWireError("response global row table layout references are not exact")
            restored = []
            for row_index, (layout_index, cells) in enumerate(rows):
                columns = layouts[layout_index]
                row_values = dict(zip(columns, cells, strict=True))
                try:
                    full_row = restore_response_row_defaults(layout_index, row_values, schema_catalog)
                except ArbitrationResponseRowDefaultsError as exc:
                    raise ArbitrationAssessmentWireError("response row defaults failed expansion") from exc
                restored.append({
                    column: walk(cell, [*path, {"row": row_index, "column": column}], column)
                    for column, cell in full_row.items()
                })
            return restored
        if isinstance(item, Mapping):
            return {key: walk(child, [*path, key], key) for key, child in item.items()}
        if isinstance(item, list):
            allowed = path_layouts.get(_path_template_key(path))
            if (
                field in ROW_TABLE_FIELDS
                and len(item) >= 2
                and all(isinstance(row, Mapping) and row for row in item)
                and allowed is not None
            ):
                raise ArbitrationAssessmentWireError("response object rows must use the declared row-table encoding")
            return [walk(child, [*path, index], field) for index, child in enumerate(item)]
        return copy.deepcopy(item)

    return walk(value, [])


def _compact_candidate_decision_view_rows(
    request: Mapping[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Replace exact duplicate candidate-unit view rows with ordered typed IDs."""
    compacted = _json_copy(request)
    candidate_rows = compacted.get("candidate_units")
    critical_wire = compacted.get("critical_view_wire")
    if not isinstance(candidate_rows, list) or not isinstance(critical_wire, Mapping):
        return compacted, []
    raw_views = critical_wire.get("views")
    if not isinstance(raw_views, list):
        raise ArbitrationAssessmentWireError("critical view wire rows are required for exact view references")
    views_by_identity: dict[tuple[str, str], Mapping[str, Any]] = {}
    for view in raw_views:
        if not isinstance(view, Mapping):
            raise ArbitrationAssessmentWireError("critical view wire contains a malformed view")
        candidate_id = view.get("candidate_id")
        view_id = view.get("decision_view_id")
        if not isinstance(candidate_id, str) or not isinstance(view_id, str):
            raise ArbitrationAssessmentWireError("critical view wire row lacks its exact candidate/view identity")
        identity = (candidate_id, view_id)
        if identity in views_by_identity:
            raise ArbitrationAssessmentWireError("critical view wire repeats a candidate/view identity")
        views_by_identity[identity] = view

    reference_rows: list[dict[str, Any]] = []
    for candidate_index, candidate in enumerate(candidate_rows):
        if not isinstance(candidate, dict) or not isinstance(candidate.get("candidate_id"), str):
            raise ArbitrationAssessmentWireError("candidate units row lacks its candidate identity")
        candidate_id = candidate["candidate_id"]
        unit_rows = candidate.get("unit_rows")
        if not isinstance(unit_rows, list):
            raise ArbitrationAssessmentWireError("candidate units row lacks its unit row list")
        for unit_index, unit in enumerate(unit_rows):
            if not isinstance(unit, dict):
                raise ArbitrationAssessmentWireError("candidate unit row is malformed")
            if "decision_view_ids" in unit:
                raise ArbitrationAssessmentWireError("candidate unit already has decision-view references")
            if "decision_views" not in unit:
                continue
            views = unit["decision_views"]
            if not isinstance(views, list):
                raise ArbitrationAssessmentWireError("candidate unit decision_views must be an array")
            view_ids: list[str] = []
            for view in views:
                if not isinstance(view, Mapping):
                    raise ArbitrationAssessmentWireError("candidate unit contains a malformed decision view")
                view_id = view.get("decision_view_id")
                if not isinstance(view_id, str):
                    raise ArbitrationAssessmentWireError("candidate unit decision view lacks its exact identity")
                global_view = views_by_identity.get((candidate_id, view_id))
                if global_view is None or dict(global_view) != dict(view):
                    raise ArbitrationAssessmentWireError(
                        "candidate unit decision view is not exactly present in its candidate's critical view table"
                    )
                view_ids.append(view_id)
            if len(view_ids) != len(set(view_ids)):
                raise ArbitrationAssessmentWireError("candidate unit repeats a decision-view identity")
            unit_id = unit.get("unit_id")
            if not isinstance(unit_id, str):
                raise ArbitrationAssessmentWireError("candidate unit row lacks its exact unit identity")
            unit.pop("decision_views")
            unit["decision_view_ids"] = view_ids
            reference_rows.append({
                "candidate_index": candidate_index,
                "candidate_id": candidate_id,
                "unit_index": unit_index,
                "unit_id": unit_id,
                "decision_view_ids": view_ids,
            })
    return compacted, reference_rows


def _expand_candidate_decision_view_rows(
    request: Mapping[str, Any],
    *,
    reference_schema: Any,
    reference_hash: Any,
) -> dict[str, Any]:
    if reference_schema != DECISION_VIEW_REFERENCE_SCHEMA or not isinstance(reference_hash, str):
        raise ArbitrationAssessmentWireError("candidate decision-view reference contract is missing")
    expanded = _json_copy(request)
    candidate_rows = expanded.get("candidate_units")
    critical_wire = expanded.get("critical_view_wire")
    if not isinstance(candidate_rows, list) or not isinstance(critical_wire, Mapping):
        if compute_v3_hash([]) != reference_hash:
            raise ArbitrationAssessmentWireError("candidate decision-view reference table is missing")
        return expanded
    raw_views = critical_wire.get("views")
    if not isinstance(raw_views, list):
        raise ArbitrationAssessmentWireError("critical view wire rows are required for view-reference expansion")
    views_by_identity: dict[tuple[str, str], Mapping[str, Any]] = {}
    for view in raw_views:
        if not isinstance(view, Mapping):
            raise ArbitrationAssessmentWireError("critical view wire contains a malformed view")
        candidate_id = view.get("candidate_id")
        view_id = view.get("decision_view_id")
        if not isinstance(candidate_id, str) or not isinstance(view_id, str):
            raise ArbitrationAssessmentWireError("critical view wire row lacks its exact candidate/view identity")
        identity = (candidate_id, view_id)
        if identity in views_by_identity:
            raise ArbitrationAssessmentWireError("critical view wire repeats a candidate/view identity")
        views_by_identity[identity] = view

    reference_rows: list[dict[str, Any]] = []
    for candidate_index, candidate in enumerate(candidate_rows):
        if not isinstance(candidate, dict) or not isinstance(candidate.get("candidate_id"), str):
            raise ArbitrationAssessmentWireError("candidate units row lacks its candidate identity")
        candidate_id = candidate["candidate_id"]
        unit_rows = candidate.get("unit_rows")
        if not isinstance(unit_rows, list):
            raise ArbitrationAssessmentWireError("candidate units row lacks its unit row list")
        for unit_index, unit in enumerate(unit_rows):
            if not isinstance(unit, dict):
                raise ArbitrationAssessmentWireError("candidate unit row is malformed")
            if "decision_views" in unit:
                raise ArbitrationAssessmentWireError("candidate unit mixes literal decision views with references")
            if "decision_view_ids" not in unit:
                continue
            view_ids = unit["decision_view_ids"]
            unit_id = unit.get("unit_id")
            if (
                not isinstance(view_ids, list)
                or any(not isinstance(view_id, str) for view_id in view_ids)
                or len(view_ids) != len(set(view_ids))
                or not isinstance(unit_id, str)
            ):
                raise ArbitrationAssessmentWireError("candidate decision-view references are malformed")
            views = []
            for view_id in view_ids:
                global_view = views_by_identity.get((candidate_id, view_id))
                if global_view is None or global_view.get("candidate_id") != candidate_id:
                    raise ArbitrationAssessmentWireError("candidate decision-view reference is missing or foreign")
                views.append(copy.deepcopy(dict(global_view)))
            unit.pop("decision_view_ids")
            unit["decision_views"] = views
            reference_rows.append({
                "candidate_index": candidate_index,
                "candidate_id": candidate_id,
                "unit_index": unit_index,
                "unit_id": unit_id,
                "decision_view_ids": list(view_ids),
            })
    if compute_v3_hash(reference_rows) != reference_hash:
        raise ArbitrationAssessmentWireError("candidate decision-view reference rows differ from their bound table")
    return expanded


def _mapping_for(catalog: Mapping[str, Any], domain: str) -> dict[str, Mapping[str, Any]]:
    return {
        _path_key(row["path"]): row
        for row in catalog["alias_entries"]
        if row["domain"] == domain
    }


def _transform_typed(
    value: Any,
    *,
    domain: str,
    entries: Sequence[Mapping[str, Any]],
    encode: bool,
    prefix: str,
    reducer_input: bool = False,
) -> Any:
    domain_entries = [entry for entry in entries if entry["domain"] == domain]
    expected = _mapping_for({"alias_entries": domain_entries}, domain)
    map_key_reverse: dict[tuple[str, str, str], str] = {}
    map_key_expected: dict[tuple[str, str], set[str]] = {}
    for entry in domain_entries:
        path = entry["path"]
        if not isinstance(path, list) or not path or not isinstance(path[-1], Mapping):
            continue
        canonical_key = path[-1].get("map_key")
        if not isinstance(canonical_key, str):
            continue
        parent_path = _path_key(path[:-1])
        kind = str(entry["kind"])
        map_key_reverse[(parent_path, kind, str(entry["alias"]))] = canonical_key
        map_key_expected.setdefault((parent_path, kind), set()).add(canonical_key)
    alias_reverse: dict[str, tuple[str, set[str]]] = {}
    canonical_to_alias: dict[tuple[str, str], str] = {}
    for entry in domain_entries:
        alias = str(entry["alias"])
        canonical = str(entry["canonical_value"])
        kind = str(entry["kind"])
        canonical_to_alias[(kind, canonical)] = alias
        prior = alias_reverse.get(alias)
        if prior is not None and prior[0] != canonical:
            raise ArbitrationAssessmentWireError("structural alias collides across canonical values")
        if prior is None:
            alias_reverse[alias] = (canonical, {kind})
        else:
            prior[1].add(kind)

    def transform_scalar(item: Any, path: list[Any], kind: str | None) -> Any:
        if not isinstance(item, str):
            return copy.deepcopy(item)
        row = expected.get(_path_key(path))
        if encode:
            if domain == "response" and kind is not None:
                alias = canonical_to_alias.get((kind, item))
                if alias is not None:
                    return alias
                if item.startswith(prefix):
                    raise ArbitrationAssessmentWireError("canonical response value collides with alias prefix")
                return item
            if row is None:
                if item.startswith(prefix):
                    raise ArbitrationAssessmentWireError("canonical text collides with structural alias prefix")
                return item
            if row["canonical_value"] != item or row["kind"] != kind:
                raise ArbitrationAssessmentWireError("typed structural alias path differs from its catalog")
            return str(row["alias"])
        if domain == "response":
            if item.startswith(prefix):
                found = alias_reverse.get(item)
                if found is None or kind is None or kind not in found[1]:
                    raise ArbitrationAssessmentWireError("unknown or wrong-kind response alias")
                return found[0]
            if kind is not None and (kind, item) in canonical_to_alias:
                raise ArbitrationAssessmentWireError("response echoed a canonical value instead of its alias")
            return item
        if row is not None:
            alias = str(row["alias"])
            canonical = str(row["canonical_value"])
            if item != alias:
                raise ArbitrationAssessmentWireError("typed structural field omitted or changed its expected alias")
            found = alias_reverse.get(item)
            if found is None or kind not in found[1] or found[0] != canonical:
                raise ArbitrationAssessmentWireError(
                    f"typed structural alias has a foreign kind or value at {_path_key(path)} "
                    f"(expected kind={kind!r}, canonical={canonical!r}, found={found!r})"
                )
            return canonical
        if item.startswith(prefix):
            found = alias_reverse.get(item)
            if found is None or kind is None or kind not in found[1]:
                raise ArbitrationAssessmentWireError("unknown or wrong-kind structural alias")
            return found[0]
        if kind is not None and (kind, item) in canonical_to_alias:
            raise ArbitrationAssessmentWireError(
                f"wire contains a canonical value where its alias is required at {_path_key(path)} "
                f"(kind={kind!r})"
            )
        return item

    def transform_map_key(item: Any, parent_path: list[Any], kind: str | None) -> Any:
        if not isinstance(item, str):
            raise ArbitrationAssessmentWireError("JSON mapping keys must be strings")
        if kind is None:
            if item.startswith(prefix):
                raise ArbitrationAssessmentWireError("unknown structural alias appears as an object key")
            return item
        if encode:
            return transform_scalar(item, [*parent_path, {"map_key": item}], kind)
        if domain == "response":
            if item.startswith(prefix):
                found = alias_reverse.get(item)
                if found is None or kind not in found[1]:
                    raise ArbitrationAssessmentWireError("unknown or wrong-kind response map-key alias")
                return found[0]
            if (kind, item) in canonical_to_alias:
                raise ArbitrationAssessmentWireError("response map key used a canonical value instead of its alias")
            return item
        location = _path_key(parent_path)
        canonical = map_key_reverse.get((location, kind, item))
        if canonical is not None:
            return canonical
        if item.startswith(prefix):
            raise ArbitrationAssessmentWireError("unknown or wrong-kind structural alias object key")
        if item in map_key_expected.get((location, kind), set()):
            raise ArbitrationAssessmentWireError("wire object key omitted its expected structural alias")
        return item

    def walk(item: Any, path: list[Any], field: str | None = None, parent: Any = None) -> Any:
        if isinstance(item, Mapping):
            if field in {"group_views_by_candidate", "group_evidence_by_candidate"}:
                candidate_kind = "candidate_id"
                nested_kind = "decision_view_ids" if field == "group_views_by_candidate" else "evidence_refs"
                result: dict[str, Any] = {}
                for raw_candidate, groups in item.items():
                    wire_candidate = transform_map_key(raw_candidate, path, candidate_kind)
                    canonical_candidate = raw_candidate if encode else wire_candidate
                    if not isinstance(groups, Mapping):
                        result[wire_candidate] = walk(groups, [*path, canonical_candidate], None, item)
                        continue
                    transformed_groups = {}
                    for raw_group_id, rows in groups.items():
                        wire_group = transform_map_key(raw_group_id, [*path, canonical_candidate], "group_id")
                        canonical_group_id = raw_group_id if encode else wire_group
                        transformed_groups[wire_group] = walk(
                            rows, [*path, canonical_candidate, canonical_group_id], nested_kind, groups,
                        )
                    result[wire_candidate] = transformed_groups
                return result
            if field in {"group_views", "group_evidence"}:
                nested_kind = "decision_view_ids" if field == "group_views" else "evidence_refs"
                result = {}
                for raw_group_id, rows in item.items():
                    wire_group = transform_map_key(raw_group_id, path, "group_id")
                    canonical_group_id = raw_group_id if encode else wire_group
                    result[wire_group] = walk(rows, [*path, canonical_group_id], nested_kind, item)
                return result
            row_object = isinstance(parent, list)
            key_kind = None if row_object else _MAP_KEY_FIELDS.get(field or "")
            value_kind = None if row_object else _MAP_VALUE_FIELDS.get(field or "")
            value_array_kind = None
            if reducer_input and not row_object:
                key_kind = _REDUCTION_MAP_KEY_FIELDS.get(field or "", key_kind)
                value_array_kind = _REDUCTION_MAP_VALUE_ARRAY_FIELDS.get(field or "")
            result = {}
            for raw_key, child in item.items():
                transformed_key = transform_map_key(raw_key, path, key_kind)
                canonical_key = raw_key if encode else transformed_key
                if value_kind is not None and isinstance(child, str):
                    transformed_value = transform_scalar(child, [*path, canonical_key], value_kind)
                elif value_array_kind is not None and isinstance(child, list):
                    transformed_value = [
                        transform_scalar(value, [*path, canonical_key, index], value_array_kind)
                        if isinstance(value, str)
                        else walk(value, [*path, canonical_key, index], None, child)
                        for index, value in enumerate(child)
                    ]
                else:
                    transformed_value = walk(child, [*path, canonical_key], raw_key, item)
                if transformed_key in result:
                    raise ArbitrationAssessmentWireError("structural aliases collide as object keys")
                result[transformed_key] = transformed_value
            return result
        if isinstance(item, list):
            array_kind = (
                _field_kind(field or "", parent)
                or _ID_ARRAY_FIELDS.get(field or "")
                or (_REDUCTION_ID_ARRAY_FIELDS.get(field or "") if reducer_input else None)
            )
            hash_array = field in _HASH_ARRAY_FIELDS if field is not None else False
            list_result = []
            for index, child in enumerate(item):
                child_path = [*path, index]
                if array_kind is not None and isinstance(child, str):
                    list_result.append(transform_scalar(child, child_path, array_kind))
                elif hash_array and isinstance(child, str):
                    list_result.append(transform_scalar(child, child_path, str(field)))
                else:
                    list_result.append(walk(child, child_path, field, item))
            return list_result
        kind = _field_kind(field or "", parent)
        return transform_scalar(item, path, kind)

    return walk(value, [])


def _wire_body_hash_body(wire_request: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: copy.deepcopy(value)
        for key, value in wire_request.items()
        if key not in {WIRE_KEY, "_prompt_authority"}
    }


def project_assessment_wire(
    canonical_request: Mapping[str, Any],
    canonical_response_shape: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Project a new assessment/reducer body without changing its semantics.

    The input is the pre-prompt-authority payload. The provider receives
    opaque aliases in typed identity/hash positions and a lossless semantic
    string table. The returned catalog is local state and must never be added
    to the provider request; the caller attaches top-level prompt authority
    afterwards and binds that complete request with its actual request hash.
    """
    request = _json_copy(canonical_request)
    response_shape = _json_copy(canonical_response_shape)
    if not isinstance(request, dict) or not isinstance(response_shape, dict):
        raise ArbitrationAssessmentWireError("canonical request and response shape must be objects")
    if WIRE_KEY in request or WIRE_KEY in response_shape:
        raise ArbitrationAssessmentWireError("assessment data is already projected")
    if "_prompt_authority" in request:
        raise ArbitrationAssessmentWireError("project assessment wire before attaching prompt authority")
    if not isinstance(request.get("node_id"), str) or not request.get("node_id"):
        raise ArbitrationAssessmentWireError("canonical assessment node_id is required")
    reducer_input = request.get("schema_version") == _REDUCTION_INPUT_SCHEMA

    request_body = {key: value for key, value in request.items() if key != "node_id"}
    compacted_request_body, decision_view_reference_rows = _compact_candidate_decision_view_rows(request_body)
    alias_plan, _forward, source_reference_kinds = _alias_seed(
        {**compacted_request_body, "node_id": request["node_id"]}, response_shape,
    )
    entries = alias_plan["entries"]
    alias_prefix = str(alias_plan["generic_catalog"]["prefix"])
    response_echo_bindings = _response_echo_bindings(
        response_shape, _forward, reducer_input=reducer_input,
    )
    aliased_request = _transform_typed(
        compacted_request_body, domain="request", entries=entries, encode=True, prefix=alias_prefix,
        reducer_input=reducer_input,
    )
    aliased_response_shape = _transform_typed(
        response_shape, domain="response", entries=entries, encode=True, prefix=alias_prefix,
    )
    aliased_request = _compact_reducer_group_bindings(
        aliased_request, reducer_input=reducer_input,
    )
    packed_response_shape = _pack_row_tables(aliased_response_shape)
    row_packed_request = _pack_row_tables(aliased_request, reducer_input=reducer_input)
    response_row_table_schemas_v1 = _compact_response_row_table_schemas(
        _response_row_table_schemas(packed_response_shape),
    )
    response_row_table_schemas = (
        upgrade_reducer_response_schema(response_row_table_schemas_v1)
        if reducer_input else response_row_table_schemas_v1
    )
    response_row_table_schema_hash = compute_v3_hash(response_row_table_schemas)
    wire_response_shape = _response_tables_use_global_layouts(
        packed_response_shape, response_row_table_schemas,
    )
    wire_response_echo_bindings = _pack_row_tables(response_echo_bindings, "response_echo_bindings")

    semantic_wrapper = {"topic_synthesis": [{"assessment_request": row_packed_request}]}
    packed_wrapper = pack_semantic_request_wire(semantic_wrapper)
    semantic_source_wire = copy.deepcopy(packed_wrapper["semantic_source_wire"])
    wire_body = {
        "schema_version": request["schema_version"],
        "node_id": request["node_id"],
        "topic_synthesis": copy.deepcopy(packed_wrapper["topic_synthesis"]),
        "semantic_source_wire": semantic_source_wire,
        "response_echo_bindings": wire_response_echo_bindings,
        "response_row_table_schemas": response_row_table_schemas,
    }
    canonical_request_hash = compute_v3_hash(request)
    canonical_response_shape_hash = compute_v3_hash(response_shape)
    wire_request_body_hash = compute_v3_hash(wire_body)
    wire_response_shape_hash = compute_v3_hash(wire_response_shape)
    source_reference_rows = [
        {"canonical_value": value, "source_kinds": kinds, "alias": _forward[value]}
        for value, kinds in source_reference_kinds.items()
    ]
    alias_mapping_hash = compute_v3_hash({"entries": entries, "source_reference_kinds": source_reference_rows})
    literal_string_table = copy.deepcopy(list(semantic_source_wire.get("strings") or []))
    literal_table_hash = compute_v3_hash(literal_string_table)
    semantic_source_wire_hash = compute_v3_hash(semantic_source_wire)
    wire_contract = _wire_contract_for_schema(str(request["schema_version"]))
    catalog = {
        "schema_version": WIRE_SCHEMA,
        "policy": WIRE_POLICY,
        "wire_contract": wire_contract,
        "canonical_schema_version": str(request["schema_version"]),
        "node_id": str(request["node_id"]),
        "canonical_request_hash": canonical_request_hash,
        "canonical_response_shape_hash": canonical_response_shape_hash,
        "wire_request_body_hash": wire_request_body_hash,
        "wire_response_shape_hash": wire_response_shape_hash,
        "alias_prefix": alias_prefix,
        "alias_entries": entries,
        "source_reference_kinds": source_reference_rows,
        "alias_mapping_hash": alias_mapping_hash,
        "response_echo_bindings": response_echo_bindings,
        "response_echo_bindings_hash": compute_v3_hash(response_echo_bindings),
        "response_row_table_schemas": response_row_table_schemas,
        "response_row_table_schema_hash": response_row_table_schema_hash,
        "literal_string_table": literal_string_table,
        "literal_table_hash": literal_table_hash,
        "semantic_source_wire_hash": semantic_source_wire_hash,
        "decision_view_reference_schema": DECISION_VIEW_REFERENCE_SCHEMA,
        "decision_view_reference_hash": compute_v3_hash(decision_view_reference_rows),
    }
    catalog["catalog_hash"] = compute_v3_hash(catalog)
    wire = {
        **wire_body,
        WIRE_KEY: {
            "schema_version": WIRE_SCHEMA,
            "policy": WIRE_POLICY,
            "contract": wire_contract,
            **{key: catalog[key] for key in (
                "canonical_schema_version", "node_id", "alias_prefix", "catalog_hash",
            )},
        },
    }
    restored = expand_assessment_wire_request(wire, catalog)
    if compute_v3_hash(restored) != canonical_request_hash:
        raise ArbitrationAssessmentWireError("assessment request projection is not lossless")
    return wire, wire_response_shape, catalog


def _verify_catalog(raw: Mapping[str, Any]) -> dict[str, Any]:
    catalog = _json_copy(raw)
    if not isinstance(catalog, dict) or set(catalog) != _CATALOG_KEYS:
        raise ArbitrationAssessmentWireError("assessment wire catalog schema is invalid")
    expected_wire_contract = _wire_contract_for_schema(str(catalog.get("canonical_schema_version") or ""))
    if (
        catalog["schema_version"] != WIRE_SCHEMA
        or catalog["policy"] != WIRE_POLICY
        or catalog["wire_contract"] != expected_wire_contract
    ):
        raise ArbitrationAssessmentWireError("assessment wire catalog policy is invalid")
    if not isinstance(catalog["alias_prefix"], str) or not re.fullmatch(r"\$I_*", catalog["alias_prefix"]):
        raise ArbitrationAssessmentWireError("assessment identity alias prefix is invalid")
    if catalog["decision_view_reference_schema"] != DECISION_VIEW_REFERENCE_SCHEMA:
        raise ArbitrationAssessmentWireError("candidate decision-view reference schema is invalid")
    if not isinstance(catalog["alias_entries"], list):
        raise ArbitrationAssessmentWireError("assessment identity alias entries are malformed")
    if any(
        not isinstance(row, Mapping)
        or set(row) != {"domain", "path", "kind", "canonical_value", "alias"}
        for row in catalog["alias_entries"]
    ):
        raise ArbitrationAssessmentWireError("assessment alias entry is malformed")
    for field in (
        "canonical_request_hash", "canonical_response_shape_hash", "wire_request_body_hash",
        "wire_response_shape_hash", "alias_mapping_hash", "response_echo_bindings_hash", "literal_table_hash",
        "response_row_table_schema_hash", "semantic_source_wire_hash", "decision_view_reference_hash", "catalog_hash",
    ):
        if not isinstance(catalog[field], str) or not re.fullmatch(r"[0-9a-f]{64}", catalog[field]):
            raise ArbitrationAssessmentWireError(f"assessment catalog {field} is invalid")
    entries = catalog["alias_entries"]
    if entries != sorted(entries, key=_entry_sort_key):
        raise ArbitrationAssessmentWireError("assessment alias entries are not canonical-order")
    canonical_values = sorted({str(row.get("canonical_value") or "") for row in entries})
    if any(not value for value in canonical_values):
        raise ArbitrationAssessmentWireError("assessment alias has an empty canonical value")
    expected_alias = {value: catalog["alias_prefix"] + str(index) for index, value in enumerate(canonical_values)}
    for row in entries:
        if not isinstance(row, Mapping) or set(row) != {"domain", "path", "kind", "canonical_value", "alias"}:
            raise ArbitrationAssessmentWireError("assessment alias entry is malformed")
        if row["domain"] not in {"request", "response"} or not isinstance(row["path"], list):
            raise ArbitrationAssessmentWireError("assessment alias entry location is invalid")
        if not isinstance(row["kind"], str) or not row["kind"]:
            raise ArbitrationAssessmentWireError("assessment alias entry kind is invalid")
        if row["alias"] != expected_alias[row["canonical_value"]]:
            raise ArbitrationAssessmentWireError("assessment aliases are not deterministic")
    source_reference_rows = catalog["source_reference_kinds"]
    if not isinstance(source_reference_rows, list):
        raise ArbitrationAssessmentWireError("source reference kind catalog is malformed")
    if any(
        not isinstance(row, Mapping)
        or set(row) != {"canonical_value", "source_kinds", "alias"}
        for row in source_reference_rows
    ):
        raise ArbitrationAssessmentWireError("source reference kind row is malformed")
    alias_by_value = {
        str(row["canonical_value"]): str(row["alias"])
        for row in entries
    }
    if source_reference_rows != sorted(source_reference_rows, key=lambda row: str(row["canonical_value"])):
        raise ArbitrationAssessmentWireError("source reference kind rows are not canonical-order")
    for row in source_reference_rows:
        kinds = row["source_kinds"]
        if (
            not isinstance(row["canonical_value"], str)
            or row["alias"] != alias_by_value.get(row["canonical_value"])
            or not isinstance(kinds, list)
            or not kinds
            or kinds != sorted(set(kinds))
            or not set(kinds).issubset({"source_claim_id", "evidence_id", "source_field_id"})
        ):
            raise ArbitrationAssessmentWireError("source reference kind bindings are invalid")
    if catalog["alias_mapping_hash"] != compute_v3_hash({
        "entries": entries,
        "source_reference_kinds": source_reference_rows,
    }):
        raise ArbitrationAssessmentWireError("source-reference alias mapping hash differs")
    echo_rows = catalog["response_echo_bindings"]
    reducer_catalog = catalog["canonical_schema_version"] == _REDUCTION_INPUT_SCHEMA
    if reducer_catalog:
        echo_order = lambda row: (str(row.get("group_id") or ""), str(row.get("group_hash") or ""))
        expected_echo_fields = {"group_id", "group_hash"}
    else:
        echo_order = lambda row: (
            str(row.get("candidate_id") or ""),
            str(row.get("group_id") or ""),
            str(row.get("group_hash") or ""),
        )
        expected_echo_fields = {"candidate_id", "group_id", "group_hash"}
    if not isinstance(echo_rows, list) or echo_rows != sorted(echo_rows, key=echo_order):
        raise ArbitrationAssessmentWireError("response echo binding table is malformed or unordered")
    response_alias_roles = {
        (str(row["alias"]), str(row["kind"]))
        for row in entries if row["domain"] == "response"
    }
    seen_echo_pairs = set()
    for row in echo_rows:
        if not isinstance(row, Mapping) or set(row) != expected_echo_fields:
            raise ArbitrationAssessmentWireError("response echo binding row is malformed")
        pair = row["group_id"] if reducer_catalog else (row["candidate_id"], row["group_id"])
        if pair in seen_echo_pairs:
            raise ArbitrationAssessmentWireError("response echo binding table repeats a group or candidate/group")
        seen_echo_pairs.add(pair)
        if (
            (not reducer_catalog and (row["candidate_id"], "candidate_id") not in response_alias_roles)
            or (row["group_id"], "group_id") not in response_alias_roles
            or (row["group_hash"], "group_hash") not in response_alias_roles
        ):
            raise ArbitrationAssessmentWireError("response echo binding references a missing or wrong-kind alias")
    if catalog["response_echo_bindings_hash"] != compute_v3_hash(echo_rows):
        raise ArbitrationAssessmentWireError("response echo binding hash differs")
    row_schema_catalog = catalog["response_row_table_schemas"]
    expected_row_schema_version = (
        RESPONSE_ROW_TABLE_SCHEMA_V2
        if catalog["canonical_schema_version"] == _REDUCTION_INPUT_SCHEMA
        else RESPONSE_ROW_TABLE_SCHEMA_V1
    )
    if (
        not isinstance(row_schema_catalog, Mapping)
        or row_schema_catalog.get("schema_version") != expected_row_schema_version
        or row_schema_catalog.get("row_table_marker") != ROW_TABLE_MARKER
        or not isinstance(row_schema_catalog.get("layouts"), list)
        or not isinstance(row_schema_catalog.get("tables"), list)
    ):
        raise ArbitrationAssessmentWireError("response row-table schema catalog is malformed")
    try:
        validate_response_row_defaults(row_schema_catalog)
    except ArbitrationResponseRowDefaultsError as exc:
        raise ArbitrationAssessmentWireError("response row-default schema catalog is malformed") from exc
    layouts = row_schema_catalog["layouts"]
    if any(
        not isinstance(layout, list)
        or any(not isinstance(column, str) for column in layout)
        or layout != sorted(set(layout))
        for layout in layouts
    ) or (
        expected_row_schema_version == RESPONSE_ROW_TABLE_SCHEMA_V1
        and layouts != [list(layout) for layout in sorted({tuple(layout) for layout in layouts})]
    ):
        raise ArbitrationAssessmentWireError("response row-table column layouts are malformed or duplicated")
    tables = row_schema_catalog["tables"]
    for table in tables:
        if (
            not isinstance(table, Mapping)
            or set(table) != {"path_template", "layout_ids"}
            or not isinstance(table.get("path_template"), list)
            or not isinstance(table.get("layout_ids"), list)
            or table["layout_ids"] != sorted(set(table["layout_ids"]))
            or any(type(index) is not int or not 0 <= index < len(layouts) for index in table["layout_ids"])
        ):
            raise ArbitrationAssessmentWireError("response row-table path schema is malformed")
    if len({_path_key(table["path_template"]) for table in tables}) != len(tables):
        raise ArbitrationAssessmentWireError("response row-table paths are duplicated")
    if tables != sorted(tables, key=lambda row: _path_key(row["path_template"])):
        raise ArbitrationAssessmentWireError("response row-table paths are not canonical-order")
    if catalog["response_row_table_schema_hash"] != compute_v3_hash(row_schema_catalog):
        raise ArbitrationAssessmentWireError("response row-table schema hash differs")
    strings = catalog["literal_string_table"]
    if not isinstance(strings, list) or any(not isinstance(value, str) for value in strings):
        raise ArbitrationAssessmentWireError("assessment literal string table is malformed")
    if strings != sorted(set(strings)) or catalog["literal_table_hash"] != compute_v3_hash(strings):
        raise ArbitrationAssessmentWireError("assessment literal string table hash or order differs")
    if catalog["catalog_hash"] != compute_v3_hash({key: value for key, value in catalog.items() if key != "catalog_hash"}):
        raise ArbitrationAssessmentWireError("assessment wire catalog hash differs")
    return catalog


def _wire_metadata(catalog: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": WIRE_SCHEMA,
        "policy": WIRE_POLICY,
        "contract": catalog["wire_contract"],
        **{key: catalog[key] for key in (
            "canonical_schema_version", "node_id", "alias_prefix", "catalog_hash",
        )},
    }


def expand_assessment_wire_request(
    wire_request: Mapping[str, Any],
    local_catalog: Mapping[str, Any],
) -> dict[str, Any]:
    """Restore the pre-prompt-authority canonical body and verify its local hashes.

    When called on the final provider request, the top-level prompt authority
    is intentionally excluded from this local representation catalog; the
    caller's full formal request hash and route/prompt receipt bind it.
    """
    catalog = _verify_catalog(local_catalog)
    wire = _json_copy(wire_request)
    if not isinstance(wire, dict):
        raise ArbitrationAssessmentWireError("assessment wire request must be an object")
    metadata = wire.pop(WIRE_KEY, None)
    expected_metadata = _wire_metadata(catalog)
    if not isinstance(metadata, Mapping) or dict(metadata) != expected_metadata:
        raise ArbitrationAssessmentWireError("assessment wire authority differs from local catalog")
    prompt_authority = wire.pop("_prompt_authority", None)
    if prompt_authority is not None and (
        not isinstance(prompt_authority, Mapping) or prompt_authority.get("node_id") != catalog["node_id"]
    ):
        raise ArbitrationAssessmentWireError("assessment prompt authority node_id changed")
    if set(wire) != {
        "schema_version", "node_id", "topic_synthesis", "semantic_source_wire", "response_echo_bindings",
        "response_row_table_schemas",
    }:
        raise ArbitrationAssessmentWireError("assessment wire body has unknown or missing fields")
    if wire.get("schema_version") != catalog["canonical_schema_version"] or wire.get("node_id") != catalog["node_id"]:
        raise ArbitrationAssessmentWireError("assessment wire top-level schema or node_id changed")
    if compute_v3_hash(wire) != catalog["wire_request_body_hash"]:
        raise ArbitrationAssessmentWireError("assessment wire body hash differs from local catalog")
    if compute_v3_hash(wire["semantic_source_wire"]) != catalog["semantic_source_wire_hash"]:
        raise ArbitrationAssessmentWireError("assessment semantic string/table metadata changed")
    if list(wire["semantic_source_wire"].get("strings") or []) != catalog["literal_string_table"]:
        raise ArbitrationAssessmentWireError("assessment literal strings differ from the exact local table")
    response_echo_bindings = _expand_row_tables(
        wire.get("response_echo_bindings"), "response_echo_bindings",
    )
    if (
        response_echo_bindings != catalog["response_echo_bindings"]
        or compute_v3_hash(response_echo_bindings) != catalog["response_echo_bindings_hash"]
    ):
        raise ArbitrationAssessmentWireError("response echo binding table differs from the local catalog")
    if (
        wire.get("response_row_table_schemas") != catalog["response_row_table_schemas"]
        or compute_v3_hash(wire.get("response_row_table_schemas")) != catalog["response_row_table_schema_hash"]
    ):
        raise ArbitrationAssessmentWireError("response row-table schemas differ from the local catalog")
    try:
        expanded_wrapper = expand_semantic_request_wire({
            "topic_synthesis": wire["topic_synthesis"],
            "semantic_source_wire": wire["semantic_source_wire"],
        })
    except (TypeError, ValueError, KeyError) as exc:
        raise ArbitrationAssessmentWireError("assessment exact-string/table expansion failed") from exc
    rows = expanded_wrapper.get("topic_synthesis")
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], Mapping) or set(rows[0]) != {"assessment_request"}:
        raise ArbitrationAssessmentWireError("assessment topic_synthesis wire wrapper is malformed")
    aliased_request = rows[0]["assessment_request"]
    reducer_input = catalog["canonical_schema_version"] == _REDUCTION_INPUT_SCHEMA
    aliased_request = _expand_row_tables(aliased_request, reducer_input=reducer_input)
    aliased_request = _expand_reducer_group_bindings(
        aliased_request, reducer_input=reducer_input,
    )
    canonical_body = _transform_typed(
        aliased_request, domain="request", entries=catalog["alias_entries"],
        encode=False, prefix=catalog["alias_prefix"], reducer_input=reducer_input,
    )
    if not isinstance(canonical_body, dict):
        raise ArbitrationAssessmentWireError("expanded assessment request is not an object")
    canonical_body["schema_version"] = catalog["canonical_schema_version"]
    canonical_body["node_id"] = catalog["node_id"]
    canonical_body = _expand_candidate_decision_view_rows(
        canonical_body,
        reference_schema=catalog["decision_view_reference_schema"],
        reference_hash=catalog["decision_view_reference_hash"],
    )
    if compute_v3_hash(canonical_body) != catalog["canonical_request_hash"]:
        raise ArbitrationAssessmentWireError("expanded assessment request hash differs")
    if _source_reference_kinds(canonical_body) != {
        row["canonical_value"]: list(row["source_kinds"])
        for row in catalog["source_reference_kinds"]
    }:
        raise ArbitrationAssessmentWireError("source reference kinds differ from the expanded request")
    return canonical_body


def project_assessment_wire_response(
    canonical_response: Mapping[str, Any],
    local_catalog: Mapping[str, Any],
) -> dict[str, Any]:
    """Encode typed response echoes; intended for fixtures and deterministic peers."""
    catalog = _verify_catalog(local_catalog)
    response = _json_copy(canonical_response)
    if not isinstance(response, dict):
        raise ArbitrationAssessmentWireError("canonical assessment response must be an object")
    result = _transform_typed(
        _expand_row_tables(response), domain="response", entries=catalog["alias_entries"],
        encode=True, prefix=catalog["alias_prefix"],
    )
    if not isinstance(result, dict):
        raise ArbitrationAssessmentWireError("wire assessment response must be an object")
    return _response_tables_use_global_layouts(
        _pack_row_tables(result), catalog["response_row_table_schemas"],
    )


def expand_assessment_wire_response(
    wire_response: Mapping[str, Any],
    local_catalog: Mapping[str, Any],
    wire_request: Mapping[str, Any],
) -> dict[str, Any]:
    """Restore typed identity/hash echoes; prose and unknown fields are untouched."""
    catalog = _verify_catalog(local_catalog)
    canonical_request = expand_assessment_wire_request(wire_request, catalog)
    response = _json_copy(wire_response)
    if not isinstance(response, dict):
        raise ArbitrationAssessmentWireError("wire assessment response must be an object")
    if response.get("node_id") != canonical_request.get("node_id"):
        raise ArbitrationAssessmentWireError("wire response node_id is not the canonical transport identity")
    expanded_rows = _expand_response_global_layout_tables(
        response, wire_request["response_row_table_schemas"],
    )
    canonical = _transform_typed(
        expanded_rows, domain="response", entries=catalog["alias_entries"],
        encode=False, prefix=catalog["alias_prefix"],
    )
    if not isinstance(canonical, dict):
        raise ArbitrationAssessmentWireError("expanded assessment response is not an object")
    # These contribution fields are set-valued source unions. Opaque alias
    # order cannot encode canonical identifier order; normalize only ordering
    # after expansion. Keep duplicates and every member for the semantic
    # validator to reject omissions, additions, or repeated references.
    for collection in ("group_contributions", "group_summaries"):
        rows = canonical.get(collection)
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            for field in ("evidence_refs", "counterevidence_refs"):
                refs = row.get(field)
                if isinstance(refs, list) and all(isinstance(ref, str) for ref in refs):
                    row[field] = sorted(refs)
            if collection == "group_summaries":
                view_ids = row.get("decision_view_ids")
                if isinstance(view_ids, list) and all(isinstance(view_id, str) for view_id in view_ids):
                    row["decision_view_ids"] = sorted(view_ids)
    return canonical


__all__ = [
    "WIRE_CONTRACT", "WIRE_KEY", "WIRE_POLICY", "WIRE_SCHEMA",
    "ArbitrationAssessmentWireError", "expand_assessment_wire_request",
    "expand_assessment_wire_response", "project_assessment_wire",
    "project_assessment_wire_response",
]
