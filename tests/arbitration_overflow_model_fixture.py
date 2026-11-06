"""Pure wire-visible model responses for the current overflow contracts.

This module is a provider-edge fixture, not an executor, validator, or runtime
adapter. It derives responses from each physically visible request and keeps
only optional same-server request/response history in ``state``.
"""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Mapping, MutableMapping, Sequence
from typing import Any

from outline.arbitration_assessment_wire import (
    ROW_TABLE_FIELDS,
    ROW_TABLE_MARKER,
    WIRE_KEY,
    WIRE_SCHEMA,
)
from outline.arbitration_response_row_defaults import (
    compact_not_applicable_layout,
    validate_response_row_defaults,
)
from outline.candidate_arbitration_aggregation import (
    ASSESSMENT_INPUT_SCHEMA,
    ASSESSMENT_RESPONSE_SCHEMA,
    REDUCTION_INPUT_SCHEMA,
    REDUCTION_RESPONSE_SCHEMA,
    SELECTION_DETAIL_KIND,
    SELECTION_DETAIL_REQUEST_SCHEMA,
    SELECTION_DETAIL_RESPONSE_SCHEMA,
    SELECTION_FINAL_INPUT_SCHEMA,
    SELECTION_FINAL_KIND,
    SELECTION_INPUT_SCHEMA,
    SELECTION_RESPONSE_SCHEMA,
    _aggregate_direction_values,
    _aggregate_disagreement_values,
)
from outline.candidate_arbitration_navigation import (
    DETAIL_FINAL_REQUEST_SCHEMA,
    DETAIL_REDUCE_REQUEST_SCHEMA,
    DETAIL_REDUCE_RESPONSE_SCHEMA,
    DETAIL_REQUEST_SCHEMA,
    DETAIL_RESPONSE_SCHEMA,
    DETAIL_SLICE_RESPONSE_SCHEMA,
    EXTRACT_KIND,
    EXTRACT_REQUEST_SCHEMA,
    EXTRACT_RESPONSE_SCHEMA,
    NAV_KIND,
    NAV_REQUEST_SCHEMA,
    NAV_RESPONSE_SCHEMA,
    REDUCE_KIND,
    REDUCE_REQUEST_SCHEMA,
    REDUCE_RESPONSE_SCHEMA,
)
from outline.candidate_arbitration_views import expand_arbitration_critical_view_wire
from outline.semantic_request_wire import expand_semantic_request_wire
from outline.v3_models import compute_v3_hash

_DIRECTIONS = {
    "supports", "contradicts", "mixed", "null", "reverse", "not_comparable", "insufficient",
}
_DISAGREEMENT = {"consistent", "disagrees", "mixed", "uncertain", "not_applicable"}
_DISPOSITIONS = {"compatible", "conflict", "uncertain"}
_ADVERSE_WORDS = re.compile(r"\b(null|reverse|minority|counterevidence|contradict(?:s|ed|ion)?)\b", re.IGNORECASE)


def _enum(value: Any, allowed: set[str], label: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise ValueError(f"{label} is outside its typed wire enum")
    return value


def build_arbitration_overflow_response(
    node_id: str,
    request: Mapping[str, Any],
    *,
    trace: list[dict[str, Any]] | None = None,
    state: MutableMapping[str, Any] | Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Build one current overflow response from the actual request on the wire.

    ``state`` may select ``selector_mode='detail_once'`` and/or
    ``navigation_mode='coherent_merge'``. The default selector chooses one
    eligible candidate directly; default navigation preserves exact section
    order and proposes no merge. No receipts or executor artifacts are made.
    Unknown and legacy v1 request schemas return ``None`` for the existing
    provider fixture to handle.
    """

    if not isinstance(request, Mapping):
        return None
    request_copy = copy.deepcopy(dict(request))
    schema = request_copy.get("schema_version")
    kind = str(request_copy.get("request_kind") or "")
    config = state if isinstance(state, Mapping) else {}
    fixture_state = _mutable_fixture_state(state)
    request_identity = _request_identity(request_copy)

    if WIRE_KEY in request_copy:
        response = _wire_assessment_response(request_copy, config)
        if response is None:
            return None
    elif schema == ASSESSMENT_INPUT_SCHEMA or kind == "arbitration_leaf_assessment_v2":
        response = _assessment_response(request_copy, config)
    elif schema == REDUCTION_INPUT_SCHEMA or kind == "arbitration_reduce_new":
        response = _reduction_response(request_copy)
    elif schema == SELECTION_INPUT_SCHEMA or kind == "arbitration_decision":
        response = _selection_response(request_copy, config, fixture_state)
    elif schema == SELECTION_FINAL_INPUT_SCHEMA or kind == SELECTION_FINAL_KIND:
        response = _selection_response(request_copy, config, fixture_state, final=True)
    elif schema == SELECTION_DETAIL_REQUEST_SCHEMA or kind == SELECTION_DETAIL_KIND:
        response = _selector_detail_response(request_copy, config)
    elif schema == EXTRACT_REQUEST_SCHEMA or kind == EXTRACT_KIND:
        response = _extract_response(request_copy)
    elif schema == REDUCE_REQUEST_SCHEMA or kind == REDUCE_KIND:
        response = _layout_reduction_response(request_copy)
    elif schema == NAV_REQUEST_SCHEMA or kind == NAV_KIND:
        response = _navigation_response(request_copy, config)
    elif schema == DETAIL_REQUEST_SCHEMA:
        response = _merge_detail_slice_response(request_copy, config)
    elif schema == DETAIL_REDUCE_REQUEST_SCHEMA:
        response = _merge_detail_reduction_response(request_copy)
    elif schema == DETAIL_FINAL_REQUEST_SCHEMA:
        response = _merge_group_finalizer_response(request_copy, config)
    else:
        return None

    if fixture_state is not None and request_identity:
        history = fixture_state.setdefault("wire_response_history", {})
        request_hash = compute_v3_hash(request_copy)
        previous = history.get(request_identity)
        if isinstance(previous, Mapping) and isinstance(previous.get("response"), Mapping):
            # Keep a stable model response for a repeated node ID. If the wire
            # body changed while the identity did not, replaying the earlier
            # response lets the production validator expose the mismatch.
            response = copy.deepcopy(dict(previous["response"]))
        else:
            history[request_identity] = {
                "request_hash": request_hash,
                "response": copy.deepcopy(response),
            }
    if trace is not None:
        trace.append({
            "kind": "arbitration_overflow_model_fixture",
            "transport_node_id": str(node_id),
            "request_id": request_identity,
            "request_schema": str(schema or ""),
            "request_hash": compute_v3_hash(request_copy),
            "response_hash": compute_v3_hash(response),
        })
    return response


def _wire_assessment_response(request: Mapping[str, Any], state: Mapping[str, Any]) -> dict[str, Any] | None:
    metadata = request.get(WIRE_KEY)
    schema_version = request.get("schema_version")
    node_id = request.get("node_id")
    if (
        not isinstance(metadata, Mapping)
        or metadata.get("schema_version") != WIRE_SCHEMA
        or metadata.get("canonical_schema_version") != schema_version
        or metadata.get("node_id") != node_id
        or not isinstance(node_id, str)
        or not node_id
    ):
        raise ValueError("assessment wire marker or canonical node identity is malformed")
    try:
        semantic_wrapper = expand_semantic_request_wire({
            "topic_synthesis": copy.deepcopy(request.get("topic_synthesis")),
            "semantic_source_wire": copy.deepcopy(request.get("semantic_source_wire")),
        })
        topics = semantic_wrapper.get("topic_synthesis")
        if (
            not isinstance(topics, list)
            or len(topics) != 1
            or not isinstance(topics[0], Mapping)
            or set(topics[0]) != {"assessment_request"}
        ):
            raise ValueError("assessment topic synthesis wrapper is malformed")
        body = _expand_model_row_tables(
            topics[0]["assessment_request"], reducer_groups=schema_version == REDUCTION_INPUT_SCHEMA,
        )
        if not isinstance(body, Mapping):
            raise TypeError("assessment request body is not an object")
        body = {**body, "schema_version": schema_version, "node_id": node_id}
        echoed_group_hashes = _wire_echoed_group_hashes(request)
        if schema_version == ASSESSMENT_INPUT_SCHEMA:
            view_by_id = _wire_critical_view_map(body)
            response = _assessment_response(
                body,
                state,
                view_by_id=view_by_id,
                echoed_group_hashes=echoed_group_hashes,
            )
        elif schema_version == REDUCTION_INPUT_SCHEMA:
            response = _reduction_response(body, echoed_group_hashes=echoed_group_hashes)
        else:
            return None
        if any(not row.get("group_hash") for row in response.get("group_contributions", response.get("group_summaries", []))):
            raise ValueError("wire response is missing a compiler-provided group hash alias")
        return _pack_wire_response_tables(response, request.get("response_row_table_schemas"))
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("assessment model fixture could not read the supplied wire") from exc


def _wire_echoed_group_hashes(request: Mapping[str, Any]) -> dict[tuple[str, str], str]:
    rows = _expand_model_row_tables(request.get("response_echo_bindings"), "response_echo_bindings")
    if not isinstance(rows, list):
        raise TypeError("response echo bindings are not an array")
    result: dict[tuple[str, str], str] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            raise TypeError("response echo binding row is malformed")
        candidate_id = row.get("candidate_id")
        group_id = row.get("group_id")
        group_hash = row.get("group_hash")
        if candidate_id is None and request.get("schema_version") == REDUCTION_INPUT_SCHEMA:
            if set(row) != {"group_id", "group_hash"}:
                raise ValueError("reducer group hash binding has unknown fields")
            candidate_id = ""
        elif not isinstance(candidate_id, str) or not candidate_id:
            raise ValueError("response echo binding omits a candidate alias")
        if not all(isinstance(value, str) and value for value in (group_id, group_hash)):
            raise ValueError("response echo binding omits a typed alias")
        key = (candidate_id, group_id)
        if key in result:
            raise ValueError("response echo bindings repeat a candidate/group pair")
        result[key] = group_hash
    return result


def _wire_critical_view_map(request: Mapping[str, Any]) -> dict[tuple[str, str], Mapping[str, Any]]:
    wire = request.get("critical_view_wire")
    if not isinstance(wire, Mapping):
        raise TypeError("assessment wire has no physically visible critical source facts")
    view_ids = wire.get("decision_view_ids")
    raw_views = wire.get("views")
    literal_catalog = wire.get("source_literal_bundle_catalog")
    if (
        not isinstance(view_ids, list)
        or not isinstance(raw_views, list)
        or len(view_ids) != len(raw_views)
        or not isinstance(literal_catalog, list)
    ):
        raise ValueError("critical view wire rows or source literal catalog are malformed")
    support_by_slot = {
        str(row.get("claim_slot_id") or ""): row
        for row in request.get("source_support_catalog") or ()
        if isinstance(row, Mapping) and row.get("claim_slot_id")
    }
    result: dict[tuple[str, str], Mapping[str, Any]] = {}
    for index, raw in enumerate(raw_views):
        if not isinstance(raw, Mapping):
            raise TypeError("critical decision view row is malformed")
        view_id = raw.get("decision_view_id")
        reference = raw.get("source_literal_bundle_ref")
        if (
            not isinstance(view_id, str)
            or not view_id
            or view_ids[index] != view_id
            or view_id in result
            or type(reference) is not int
            or reference < 0
            or reference >= len(literal_catalog)
        ):
            raise ValueError("critical decision view identity or source bundle reference is malformed")
        candidate_id = raw.get("candidate_id")
        if not isinstance(candidate_id, str) or not candidate_id:
            raise ValueError("critical decision view omits its candidate identity")
        view_key = (candidate_id, view_id)
        if view_key in result:
            raise ValueError("critical decision view candidate/ID pair is duplicated")
        literal_bundle = literal_catalog[reference]
        if not isinstance(literal_bundle, Mapping):
            raise TypeError("critical source literal bundle is malformed")
        source_bindings = literal_bundle.get("source_bindings")
        literal_details = literal_bundle.get("literal_critical_detail")
        qualifier_values = literal_bundle.get("literal_qualifier_values")
        if (
            not isinstance(source_bindings, Mapping)
            or not isinstance(literal_details, Mapping)
            or not isinstance(qualifier_values, list)
        ):
            raise TypeError("critical source bindings or literal details are missing")
        slot_id = raw.get("claim_slot_id")
        support = support_by_slot.get(str(slot_id or ""))
        if not isinstance(slot_id, str) or not isinstance(support, Mapping):
            raise TypeError("critical view has no matching visible source support row")
        if source_bindings.get("claim_slot_id") != slot_id:
            raise ValueError("critical source bindings differ from the decision view claim slot")

        source_claim_ids = _string_list(source_bindings.get("source_claim_ids"))
        evidence_ids = _string_list(source_bindings.get("evidence_ids"))
        source_field_ids = _string_list(source_bindings.get("source_field_ids"))
        details = {
            "source_claims": ("claim_id", source_claim_ids, "source_claim_ids"),
            "evidence": ("evidence_id", evidence_ids, "evidence_ids"),
            "source_fields": ("source_field_id", source_field_ids, "source_field_ids"),
        }
        detail_ids: dict[str, list[str]] = {}
        for field, (id_field, binding_ids, support_field) in details.items():
            rows = literal_details.get(field)
            if not isinstance(rows, list):
                raise TypeError("critical source literal detail list is missing")
            ids = [str(row.get(id_field) or "") for row in rows if isinstance(row, Mapping)]
            if len(ids) != len(rows) or len(ids) != len(set(ids)) or any(not value for value in ids):
                raise ValueError("critical source literal detail identities are malformed")
            if field != "evidence" and set(ids) != set(binding_ids):
                raise ValueError("critical source literal detail omits a bound source fact")
            if field == "evidence" and not set(ids).issubset(binding_ids):
                raise ValueError("critical evidence detail contains an unbound source fact")
            expected_ids = set(_string_list(support.get(support_field)))
            if not set(binding_ids).issubset(expected_ids):
                raise ValueError("critical source bindings contain a fact outside visible source support")
            detail_ids[id_field] = ids
        if not detail_ids["claim_id"] and not detail_ids["evidence_id"] and not detail_ids["source_field_id"]:
            raise ValueError("critical decision view contains no source facts")
        expanded = {
            **copy.deepcopy(dict(raw)),
            "source_bindings": copy.deepcopy(dict(source_bindings)),
            "literal_qualifier_values": copy.deepcopy(qualifier_values),
            "literal_critical_detail": copy.deepcopy(dict(literal_details)),
        }
        result[view_key] = expanded

    referenced: set[tuple[str, str]] = set()
    for candidate in request.get("candidate_units") or ():
        if not isinstance(candidate, Mapping):
            raise TypeError("candidate unit row is malformed")
        candidate_id = candidate.get("candidate_id")
        if not isinstance(candidate_id, str) or not candidate_id:
            raise ValueError("candidate unit omits its aliased candidate identity")
        for unit in candidate.get("unit_rows") or ():
            if not isinstance(unit, Mapping):
                raise TypeError("candidate assessment unit row is malformed")
            if "decision_views" in unit or not isinstance(unit.get("decision_view_ids"), list):
                raise ValueError("wire candidate unit must use ordered decision_view_ids references")
            view_ids = unit["decision_view_ids"]
            if any(not isinstance(view_id, str) or not view_id for view_id in view_ids):
                raise ValueError("wire decision_view_ids contains an invalid alias")
            for view_id in view_ids:
                view_key = (candidate_id, view_id)
                if view_key not in result:
                    raise ValueError("assessment decision view reference is missing or cross-candidate")
                if view_key in referenced:
                    raise ValueError("assessment decision view reference is duplicated")
                referenced.add(view_key)
    if referenced != set(result):
        raise ValueError("critical view wire contains a missing or unreferenced candidate view")
    return result


def _expand_model_row_tables(value: Any, field: str | None = None, *, reducer_groups: bool = False) -> Any:
    if isinstance(value, Mapping):
        if ROW_TABLE_MARKER in value:
            if (field not in ROW_TABLE_FIELDS and not (reducer_groups and field == "groups")) or set(value) != {ROW_TABLE_MARKER}:
                raise ValueError("arbitration row table marker is misplaced")
            table = value[ROW_TABLE_MARKER]
            if not isinstance(table, Mapping) or set(table) != {"columns", "rows"}:
                raise ValueError("arbitration request row table has invalid fields")
            layouts, rows = table.get("columns"), table.get("rows")
            if not isinstance(layouts, list) or not layouts or not isinstance(rows, list) or len(rows) < 2:
                raise ValueError("arbitration request row table has no layouts or rows")
            normalized_layouts: list[list[str]] = []
            for layout in layouts:
                if (
                    not isinstance(layout, list)
                    or any(not isinstance(key, str) for key in layout)
                    or layout != sorted(set(layout))
                    or layout in normalized_layouts
                ):
                    raise ValueError("arbitration request row-table layout is malformed")
                normalized_layouts.append(layout)
            used_layouts: set[int] = set()
            expanded_rows = []
            for row in rows:
                if (
                    not isinstance(row, list)
                    or len(row) != 2
                    or type(row[0]) is not int
                    or not 0 <= row[0] < len(layouts)
                    or not isinstance(row[1], list)
                    or len(row[1]) != len(layouts[row[0]])
                ):
                    raise ValueError("arbitration request row-table row is malformed")
                layout_id, cells = row
                used_layouts.add(layout_id)
                expanded_rows.append({
                    key: _expand_model_row_tables(cell, key, reducer_groups=reducer_groups)
                    for key, cell in zip(layouts[layout_id], cells, strict=True)
                })
            if used_layouts != set(range(len(layouts))):
                raise ValueError("arbitration request row table has an unused layout")
            return expanded_rows
        return {key: _expand_model_row_tables(item, key, reducer_groups=reducer_groups) for key, item in value.items()}
    if isinstance(value, list):
        return [_expand_model_row_tables(item, field, reducer_groups=reducer_groups) for item in value]
    return copy.deepcopy(value)


def _pack_wire_response_tables(response: Mapping[str, Any], schema: Any) -> dict[str, Any]:
    if not isinstance(schema, Mapping) or schema.get("row_table_marker") != ROW_TABLE_MARKER:
        raise ValueError("response row-table schema is missing or unsupported")
    validate_response_row_defaults(schema)
    layouts = schema.get("layouts")
    tables = schema.get("tables")
    if not isinstance(layouts, list) or not isinstance(tables, list):
        raise TypeError("response row-table schema layouts or tables are malformed")
    normalized_layouts: list[list[str]] = []
    for layout in layouts:
        if (
            not isinstance(layout, list)
            or any(not isinstance(column, str) for column in layout)
            or layout != sorted(set(layout))
            or layout in normalized_layouts
        ):
            raise ValueError("response row-table schema layout is malformed")
        normalized_layouts.append(layout)
    tables_by_path: dict[str, tuple[list[Any], set[int]]] = {}
    for table in tables:
        if (
            not isinstance(table, Mapping)
            or set(table) != {"path_template", "layout_ids"}
            or not isinstance(table.get("path_template"), list)
            or not isinstance(table.get("layout_ids"), list)
        ):
            raise ValueError("response row-table schema table is malformed")
        path = copy.deepcopy(table["path_template"])
        ids = table["layout_ids"]
        if (
            any(type(layout_id) is not int or not 0 <= layout_id < len(layouts) for layout_id in ids)
            or ids != sorted(set(ids))
        ):
            raise ValueError("response row-table schema layout IDs are malformed")
        path_key = json.dumps(path, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if path_key in tables_by_path:
            raise ValueError("response row-table schema repeats a path")
        tables_by_path[path_key] = (path, set(ids))

    packed = copy.deepcopy(dict(response))
    for path, allowed_ids in sorted(tables_by_path.values(), key=lambda item: len(item[0]), reverse=True):
        packed = _replace_response_table_path(packed, path, allowed_ids, normalized_layouts, schema)
    return packed


def _replace_response_table_path(
    value: Any,
    path: Sequence[Any],
    allowed_layout_ids: set[int],
    layouts: Sequence[Sequence[str]],
    schema: Mapping[str, Any],
) -> Any:
    if not path:
        if not isinstance(value, list):
            raise ValueError("declared response row-table path is not an array")
        if len(value) < 2:
            return value
        if any(not isinstance(row, Mapping) or not row for row in value):
            raise ValueError("declared response row table contains a non-object row")
        encoded_rows = []
        used_layout_ids: set[int] = set()
        for row in value:
            default_layout_ids = {int(layout_id) for layout_id in schema.get("row_defaults", {})}
            compact_layout = (
                compact_not_applicable_layout(row, sorted(allowed_layout_ids), schema)
                if allowed_layout_ids.intersection(default_layout_ids) else None
            )
            row_keys = sorted(row)
            matching = [
                layout_id for layout_id in allowed_layout_ids
                if list(layouts[layout_id]) == row_keys
            ]
            if compact_layout is not None:
                layout_id = compact_layout
            elif len(matching) == 1:
                layout_id = matching[0]
            else:
                raise ValueError("response row columns have no unique wire-schema layout")
            used_layout_ids.add(layout_id)
            encoded_rows.append([layout_id, [copy.deepcopy(row[column]) for column in layouts[layout_id]]])
        return {
            ROW_TABLE_MARKER: {
                "layout_ids": sorted(used_layout_ids),
                "rows": encoded_rows,
            },
        }

    part, rest = path[0], path[1:]
    if isinstance(part, str):
        if not isinstance(value, Mapping) or part not in value:
            raise ValueError("response row-table path is absent from generated response")
        updated = copy.deepcopy(dict(value))
        updated[part] = _replace_response_table_path(updated[part], rest, allowed_layout_ids, layouts, schema)
        return updated
    if part == "*":
        if not isinstance(value, list):
            raise ValueError("response row-table wildcard path is not an array")
        return [
            _replace_response_table_path(item, rest, allowed_layout_ids, layouts, schema)
            for item in value
        ]
    if isinstance(part, Mapping) and part.get("row") == "*" and isinstance(part.get("column"), str):
        if not isinstance(value, list):
            raise ValueError("response row-table row-column path is not an array")
        column = str(part["column"])
        updated_rows = []
        for row in value:
            if not isinstance(row, Mapping) or column not in row:
                raise ValueError("response row-table row-column path is absent")
            updated = copy.deepcopy(dict(row))
            updated[column] = _replace_response_table_path(updated[column], rest, allowed_layout_ids, layouts, schema)
            updated_rows.append(updated)
        return updated_rows
    raise ValueError("response row-table path contains an unsupported selector")


def _mutable_fixture_state(
    state: MutableMapping[str, Any] | Mapping[str, Any] | None,
) -> MutableMapping[str, Any] | None:
    if isinstance(state, MutableMapping):
        return state
    return None


def _request_identity(request: Mapping[str, Any]) -> str:
    prompt_authority = request.get("_prompt_authority")
    if isinstance(prompt_authority, Mapping) and prompt_authority.get("node_id"):
        return str(prompt_authority["node_id"])
    for key in ("node_id", "request_id", "span_id", "slice_id"):
        if request.get(key):
            return str(request[key])
    return ""


def _assessment_response(
    request: Mapping[str, Any],
    state: Mapping[str, Any],
    *,
    view_by_id: Mapping[Any, Mapping[str, Any]] | None = None,
    echoed_group_hashes: Mapping[tuple[str, str], str] | None = None,
) -> dict[str, Any]:
    candidate_ids = _string_list(request.get("candidate_ids"))
    candidate_hashes = _mapping(request.get("candidate_hashes"))
    node_id = str(request.get("node_id") or "")
    required_units = _string_list(request.get("required_unit_ids"))
    support_by_slot = {
        str(row.get("claim_slot_id") or ""): row
        for row in request.get("source_support_catalog") or ()
        if isinstance(row, Mapping) and row.get("claim_slot_id")
    }
    view_by_id = view_by_id if view_by_id is not None else _expanded_leaf_views(request)
    candidate_rows = {
        str(row.get("candidate_id") or ""): row
        for row in request.get("candidate_units") or ()
        if isinstance(row, Mapping)
    }
    unit_assessments: dict[tuple[str, str], dict[str, Any]] = {}
    rows: list[dict[str, Any]] = []
    group_accumulator: dict[tuple[str, str], dict[str, Any]] = {}
    for candidate_id in candidate_ids:
        candidate = candidate_rows.get(candidate_id, {})
        for unit in candidate.get("unit_rows") or ():
            if not isinstance(unit, Mapping):
                continue
            unit_id = str(unit.get("unit_id") or "")
            if unit_id not in required_units:
                continue
            slot_ids = {
                str(slot.get("claim_slot_id") or "")
                for section in unit.get("sections") or ()
                if isinstance(section, Mapping)
                for slot in section.get("claim_slots") or ()
                if isinstance(slot, Mapping) and slot.get("claim_slot_id")
            }
            evidence_refs = sorted({
                str(ref)
                for slot_id in slot_ids
                for ref in _string_list(support_by_slot.get(slot_id, {}).get("evidence_ids"))
            })
            raw_view_assessments = []
            view_decisions: dict[str, tuple[str, str, list[str], list[str]]] = {}
            raw_views = unit.get("decision_views")
            if raw_views is None:
                raw_views = unit.get("decision_view_ids") or []
            if not isinstance(raw_views, list):
                raise TypeError("unit decision-view references must be an array")
            tuple_indexed_views = any(isinstance(key, tuple) for key in view_by_id)
            for view_reference in raw_views:
                if isinstance(view_reference, Mapping):
                    view_wire = view_reference
                    view_id = str(view_wire.get("decision_view_id") or "")
                elif isinstance(view_reference, str) and view_reference:
                    view_id = view_reference
                    view_wire = {"decision_view_id": view_id}
                else:
                    raise ValueError("unit decision-view reference is malformed")
                if tuple_indexed_views:
                    view = view_by_id.get((candidate_id, view_id))
                    if not isinstance(view, Mapping):
                        raise ValueError("unit decision-view reference has no candidate-matched wire view")
                else:
                    view = view_by_id.get(view_id, view_wire)
                literal_refs = _literal_view_refs(view)
                direction, disagreement = _source_direction(view)
                direction, disagreement = _apply_view_override(state, view_id, direction, disagreement, view)
                counter_refs = literal_refs if _has_adverse_literal(view) or direction not in {"supports", "not_comparable"} else []
                raw_view_assessments.append({
                    "decision_view_id": view_id,
                    "decision_view_hash": str(view.get("decision_view_hash") or view_wire.get("decision_view_hash") or ""),
                    "assessed_direction": direction,
                    "assessed_disagreement": disagreement,
                    "reason": _direction_reason(direction),
                    "evidence_refs": literal_refs,
                    "counterevidence_refs": counter_refs,
                })
                view_decisions[view_id] = (direction, disagreement, literal_refs, counter_refs)
            all_directions = {row["assessed_direction"] for row in raw_view_assessments}
            all_disagreements = {row["assessed_disagreement"] for row in raw_view_assessments}
            unit_key = (candidate_id, unit_id)
            unit_override = state.get("unit_overrides", {}) if isinstance(state, Mapping) else {}
            override = unit_override.get(f"{candidate_id}:{unit_id}") if isinstance(unit_override, Mapping) else None
            if isinstance(override, Mapping):
                unit_direction = _enum(override.get("assessed_direction"), _DIRECTIONS, "unit override direction")
                unit_disagreement = _enum(override.get("assessed_disagreement"), _DISAGREEMENT, "unit override disagreement")
            else:
                unit_direction = _aggregate_direction_values(all_directions) if all_directions else "not_comparable"
                unit_disagreement = _aggregate_disagreement_values(all_disagreements) if all_disagreements else "not_applicable"
            expected_presence = str(unit.get("presence") or "absent")
            unit_evidence = evidence_refs if expected_presence == "present" else []
            unit_counterevidence: list[str] = []
            if unit_direction not in {"supports", "not_comparable"} or unit_disagreement in {"disagrees", "mixed", "uncertain"}:
                unit_counterevidence = list(unit_evidence)
            assessment = {
                "candidate_id": candidate_id,
                "candidate_hash": str(candidate_hashes.get(candidate_id) or ""),
                "unit_id": unit_id,
                "unit_hash": str(unit.get("unit_hash") or ""),
                "presence": expected_presence,
                "section_bindings": copy.deepcopy(unit.get("section_bindings") or []),
                "assessed_direction": unit_direction,
                "assessed_disagreement": unit_disagreement,
                "reason": _direction_reason(unit_direction),
                "evidence_refs": unit_evidence,
                "counterevidence_refs": unit_counterevidence,
                "view_assessments": raw_view_assessments,
            }
            rows.append(assessment)
            unit_assessments[unit_key] = assessment
            group_views = unit.get("group_views") if isinstance(unit.get("group_views"), Mapping) else {}
            for group_id in _string_list(unit.get("group_ids")):
                key = (candidate_id, group_id)
                aggregate = group_accumulator.setdefault(key, {
                    "unit_ids": [], "view_ids": set(), "directions": set(), "disagreements": set(),
                    "evidence_refs": set(), "counterevidence_refs": set(),
                })
                aggregate["unit_ids"].append(unit_id)
                aggregate["directions"].add(unit_direction)
                aggregate["disagreements"].add(unit_disagreement)
                aggregate["evidence_refs"].update(unit_evidence)
                aggregate["counterevidence_refs"].update(unit_counterevidence)
                for view_id in _string_list(group_views.get(group_id)):
                    aggregate["view_ids"].add(view_id)
                    direction, disagreement, view_refs, counter_refs = view_decisions.get(
                        view_id, ("mixed", "uncertain", [], []),
                    )
                    aggregate["directions"].add(direction)
                    aggregate["disagreements"].add(disagreement)
                    aggregate["evidence_refs"].update(view_refs)
                    aggregate["counterevidence_refs"].update(counter_refs)
    group_hashes = {
        str(group.get("group_id") or ""): compute_v3_hash(group)
        for group in request.get("review_group_map") or ()
        if isinstance(group, Mapping) and group.get("group_id")
    }
    contributions = []
    for (candidate_id, group_id), aggregate in sorted(
        group_accumulator.items(), key=lambda item: (candidate_ids.index(item[0][0]), item[0][1]),
    ):
        contributions.append({
            "candidate_id": candidate_id,
            "candidate_hash": str(candidate_hashes.get(candidate_id) or ""),
            "group_id": group_id,
            "group_hash": (
                echoed_group_hashes.get((candidate_id, group_id), echoed_group_hashes.get(("", group_id), ""))
                if echoed_group_hashes is not None else group_hashes.get(group_id, "")
            ),
            "unit_ids": aggregate["unit_ids"],
            "decision_view_ids": sorted(aggregate["view_ids"]),
            "assessed_direction": _aggregate_direction_values(aggregate["directions"]),
            "assessed_disagreement": _aggregate_disagreement_values(aggregate["disagreements"]),
            "summary": _direction_reason(_aggregate_direction_values(aggregate["directions"])),
            "evidence_refs": sorted(aggregate["evidence_refs"]),
            "counterevidence_refs": sorted(aggregate["counterevidence_refs"]),
        })
    return {
        "schema_version": ASSESSMENT_RESPONSE_SCHEMA,
        "node_id": node_id,
        "candidate_ids": candidate_ids,
        "candidate_hashes": copy.deepcopy(dict(candidate_hashes)),
        "unit_ids": required_units,
        "assessments": rows,
        "group_contributions": contributions,
    }


def _expanded_leaf_views(request: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    wire = request.get("critical_view_wire")
    if not isinstance(wire, Mapping):
        return {}
    try:
        views = expand_arbitration_critical_view_wire(
            wire, expected_view_set_hash=str(wire.get("source_view_set_hash") or ""),
        )
    except (TypeError, ValueError, KeyError):
        return {}
    return {str(row.get("decision_view_id") or ""): row for row in views if row.get("decision_view_id")}


def _literal_view_refs(view: Mapping[str, Any]) -> list[str]:
    literal = view.get("literal_critical_detail")
    if not isinstance(literal, Mapping):
        return _string_list(view.get("literal_view_ref_ids"))
    refs = {
        str(row["claim_id"])
        for row in literal.get("source_claims") or ()
        if isinstance(row, Mapping) and row.get("claim_id")
    }
    refs.update(
        str(row["evidence_id"])
        for row in literal.get("evidence") or ()
        if isinstance(row, Mapping) and row.get("evidence_id")
    )
    refs.update(
        str(row["source_field_id"])
        for row in literal.get("source_fields") or ()
        if isinstance(row, Mapping) and row.get("source_field_id")
    )
    return sorted(refs)


def _source_direction(view: Mapping[str, Any]) -> tuple[str, str]:
    bindings = view.get("source_bindings") if isinstance(view.get("source_bindings"), Mapping) else {}
    relation = bindings.get("source_relation_direction") if isinstance(bindings, Mapping) else None
    disagreement = bindings.get("source_disagreement_status") if isinstance(bindings, Mapping) else None
    explicit_direction = None
    if isinstance(relation, Mapping) and relation.get("status") == "known":
        candidate = str(relation.get("value") or "").casefold()
        explicit_direction = candidate if candidate in _DIRECTIONS else None
    explicit_disagreement = None
    if isinstance(disagreement, Mapping) and disagreement.get("status") == "known":
        candidate = str(disagreement.get("value") or "").casefold()
        explicit_disagreement = candidate if candidate in _DISAGREEMENT else None
    literal_json = str(view.get("literal_critical_detail") or "").casefold()
    directions: set[str] = set()
    if explicit_direction:
        directions.add(explicit_direction)
    if re.search(r"\bnull\b", literal_json):
        directions.add("null")
    if re.search(r"\breverse\b", literal_json):
        directions.add("reverse")
    if re.search(r"\bcontradict(?:s|ed|ion)?\b", literal_json):
        directions.add("contradicts")
    if re.search(r"\bminority\b", literal_json):
        directions.add("mixed")
    direction = _aggregate_direction_values(directions) if directions else "supports"
    if explicit_disagreement:
        disagreement_value = explicit_disagreement
    elif re.search(r"\b(counterevidence|minority|disagree|conflict)\b", literal_json):
        disagreement_value = "disagrees"
    elif direction in {"null", "reverse", "contradicts", "mixed"}:
        disagreement_value = "uncertain"
    else:
        disagreement_value = "consistent"
    return direction, disagreement_value


def _has_adverse_literal(view: Mapping[str, Any]) -> bool:
    return _ADVERSE_WORDS.search(str(view.get("literal_critical_detail") or "")) is not None


def _direction_reason(direction: str) -> str:
    return {
        "supports": "Support.",
        "contradicts": "Opposes.",
        "mixed": "Mixed.",
        "null": "Null.",
        "reverse": "Reverse.",
        "not_comparable": "Scope.",
        "insufficient": "Unclear.",
    }.get(direction, "Uncertain.")


def _apply_view_override(
    state: Mapping[str, Any], view_id: str, direction: str, disagreement: str, view: Mapping[str, Any],
) -> tuple[str, str]:
    overrides = state.get("view_overrides") if isinstance(state, Mapping) else None
    override = overrides.get(view_id) if isinstance(overrides, Mapping) else None
    if not isinstance(override, Mapping):
        return direction, disagreement
    requested_direction = str(override.get("assessed_direction") or direction)
    requested_disagreement = str(override.get("assessed_disagreement") or disagreement)
    if requested_direction not in _DIRECTIONS or requested_disagreement not in _DISAGREEMENT:
        return direction, disagreement
    # Explicit null/reverse/minority/counterevidence literals cannot be
    # overwritten by an optimistic fixture override.
    if _has_adverse_literal(view):
        return direction, disagreement
    return requested_direction, requested_disagreement


def _reduction_response(
    request: Mapping[str, Any],
    *,
    echoed_group_hashes: Mapping[tuple[str, str], str] | None = None,
) -> dict[str, Any]:
    decision_scope = request.get("decision_scope") if isinstance(request.get("decision_scope"), Mapping) else {}
    candidate_ids = _string_list(request.get("required_candidate_ids") or decision_scope.get("candidate_ids"))
    candidate_hashes = _mapping(decision_scope.get("candidate_hashes"))
    group_map = request.get("group_map") or request.get("review_group_map") or []
    group_table = group_map.get("groups") if isinstance(group_map, Mapping) else group_map
    if not isinstance(group_table, list):
        group_table = []
    group_hashes = {
        str(group.get("group_id") or ""): compute_v3_hash(group)
        for group in group_table if isinstance(group, Mapping) and group.get("group_id")
    }
    summaries = [row for row in request.get("candidate_group_summaries") or () if isinstance(row, Mapping)]
    unit_order = _string_list(request.get("unit_ids"))
    child_groups: dict[tuple[str, str, str], Mapping[str, Any]] = {}
    child_units: dict[str, set[str]] = {}
    child_hashes: dict[str, str] = {}
    for row in summaries:
        child_id = str(row.get("child_node_id") or "")
        if not child_id:
            continue
        child_hashes[child_id] = str(row.get("child_result_hash") or "")
        child_units.setdefault(child_id, set()).update(_string_list(row.get("unit_ids")))
        key = (child_id, str(row.get("candidate_id") or ""), str(row.get("group_id") or ""))
        child_groups[key] = row
    if "child_node_ids" in request or "child_result_hashes" in request:
        child_ids = _string_list(request.get("child_node_ids"))
        child_result_hashes = _string_list(request.get("child_result_hashes"))
        if (
            len(child_ids) < 2 or len(set(child_ids)) != len(child_ids)
            or len(child_result_hashes) != len(child_ids)
            or child_ids != request.get("child_node_ids")
            or child_result_hashes != request.get("child_result_hashes")
            or any(child_hashes.get(child_id, child_hash) != child_hash
                   for child_id, child_hash in zip(child_ids, child_result_hashes, strict=True))
        ):
            raise ValueError("reducer child order/hash declaration is malformed or inconsistent")
    else:
        child_ids = sorted(child_units, key=lambda child: min((unit_order.index(unit) for unit in child_units[child] if unit in unit_order), default=len(unit_order)))
        if len(child_ids) < 2:
            child_ids = _string_list(next((row.get("child_node_ids") for row in summaries if isinstance(row.get("child_node_ids"), list)), []))
        child_result_hashes = [child_hashes.get(child_id, "") for child_id in child_ids]
    group_rows = []
    for candidate_id in candidate_ids:
        for group in group_table:
            if not isinstance(group, Mapping):
                continue
            group_id = str(group.get("group_id") or "")
            child_rows = [
                child_groups[(child_id, candidate_id, group_id)]
                for child_id in child_ids
                if (child_id, candidate_id, group_id) in child_groups
            ]
            units = []
            view_ids: set[str] = set()
            directions: set[str] = set()
            disagreements: set[str] = set()
            evidence: set[str] = set()
            counterevidence: set[str] = set()
            lineage = []
            for child in child_rows:
                child_units_for_group = _string_list(child.get("unit_ids"))
                units.extend(child_units_for_group)
                view_ids.update(_string_list(child.get("decision_view_ids")))
                directions.update(_string_list(child.get("assessed_direction_values")))
                disagreements.update(_string_list(child.get("assessed_disagreement_values")))
                evidence.update(_string_list(child.get("evidence_refs")))
                counterevidence.update(_string_list(child.get("counterevidence_refs")))
                lineage.append({
                    "child_node_id": str(child.get("child_node_id") or ""),
                    "child_result_hash": str(child.get("child_result_hash") or ""),
                    "unit_ids": child_units_for_group,
                    "assessed_direction_values": _string_list(child.get("assessed_direction_values")),
                    "assessed_disagreement_values": _string_list(child.get("assessed_disagreement_values")),
                    "evidence_refs": _string_list(child.get("evidence_refs")),
                    "counterevidence_refs": _string_list(child.get("counterevidence_refs")),
                })
            units = [unit for unit in unit_order if unit in set(units)]
            group_rows.append({
                "candidate_id": candidate_id,
                "candidate_hash": str(candidate_hashes.get(candidate_id) or ""),
                "group_id": group_id,
                "group_hash": (
                    echoed_group_hashes.get((candidate_id, group_id), echoed_group_hashes.get(("", group_id), ""))
                    if echoed_group_hashes is not None else group_hashes.get(group_id, "")
                ),
                "presence": "present" if units else "not_applicable",
                "unit_ids": units,
                "decision_view_ids": sorted(view_ids),
                "assessed_direction_values": sorted(directions),
                "assessed_disagreement_values": sorted(disagreements),
                "child_lineage": lineage,
                "assessed_direction": _aggregate_direction_values(directions) if directions else "not_comparable",
                "assessed_disagreement": _aggregate_disagreement_values(disagreements) if disagreements else "not_applicable",
                "summary": _direction_reason(_aggregate_direction_values(directions)) if units else "",
                "evidence_refs": sorted(evidence),
                "counterevidence_refs": sorted(counterevidence),
            })
    return {
        "schema_version": REDUCTION_RESPONSE_SCHEMA,
        "node_id": str(request.get("node_id") or ""),
        "tree_node_id": str(request.get("tree_node_id") or ""),
        "child_node_ids": child_ids,
        "child_result_hashes": child_result_hashes,
        "candidate_ids": candidate_ids,
        "candidate_hashes": copy.deepcopy(dict(candidate_hashes)),
        "unit_ids": unit_order,
        "group_summaries": group_rows,
    }


def _selection_response(
    request: Mapping[str, Any],
    state: Mapping[str, Any],
    fixture_state: MutableMapping[str, Any] | None,
    *,
    final: bool = False,
) -> dict[str, Any]:
    candidate_ids = _string_list(request.get("candidate_ids"))
    candidate_hashes = _mapping(request.get("candidate_hashes"))
    selected = str(state.get("selected_candidate_id") or "") if isinstance(state, Mapping) else ""
    if selected not in candidate_ids:
        selected = min(candidate_ids) if candidate_ids else ""
    requested_views: list[str] = []
    selector_mode = str(state.get("selector_mode") or "") if isinstance(state, Mapping) else ""
    detail_started = bool(fixture_state and fixture_state.get("selector_detail_started"))
    if not final and selector_mode == "detail_once" and not detail_started:
        index = request.get("noncritical_fact_index") or []
        if index and isinstance(index[0], Mapping) and index[0].get("decision_view_id"):
            requested_views = [str(index[0]["decision_view_id"])]
            if fixture_state is not None:
                fixture_state["selector_detail_started"] = True
    if final and fixture_state is not None:
        fixture_state["selector_detail_finished"] = True
    return {
        "schema_version": SELECTION_RESPONSE_SCHEMA,
        "node_id": str(request.get("node_id") or ""),
        "candidate_ids": candidate_ids,
        "candidate_hashes": copy.deepcopy(dict(candidate_hashes)),
        "outcome": "detail_required" if requested_views else "selected",
        "selected_candidate_id": None if requested_views else selected,
        "detail_required_view_ids": requested_views,
        "rationale": _short_text(
            "Exact source scope retained.",
            (_mapping(request.get("output_contract"))).get("max_rationale_tokens"),
        ),
    }


def _selector_detail_response(request: Mapping[str, Any], state: Mapping[str, Any]) -> dict[str, Any]:
    body = request
    view = body.get("detail_view") if isinstance(body.get("detail_view"), Mapping) else {}
    view_id = str(body.get("detail_view_id") or view.get("decision_view_id") or "")
    literal = body.get("source_literal_fact")
    direction, disagreement = _literal_payload_direction(literal)
    overrides = state.get("detail_overrides") if isinstance(state, Mapping) else None
    override = overrides.get(view_id) if isinstance(overrides, Mapping) else None
    if isinstance(override, Mapping):
        direction, disagreement = _safe_detail_override(literal, override, direction, disagreement)
    return {
        "schema_version": SELECTION_DETAIL_RESPONSE_SCHEMA,
        "node_id": str(body.get("node_id") or ""),
        "prior_selector_node_id": str(body.get("prior_selector_node_id") or ""),
        "prior_selector_request_hash": str(body.get("prior_selector_request_hash") or ""),
        "prior_selection_result_hash": str(body.get("prior_selection_result_hash") or ""),
        "candidate_ids": _string_list(body.get("candidate_ids")),
        "candidate_hashes": copy.deepcopy(dict(_mapping(body.get("candidate_hashes")))),
        "detail_view_id": view_id,
        "detail_view_hash": str(body.get("detail_view_hash") or view.get("view_hash") or ""),
        "source_fact_hash": str(body.get("source_fact_hash") or view.get("source_fact_hash") or ""),
        "assessment_status": "resolved",
        "assessed_direction": direction,
        "assessed_disagreement": disagreement,
        "cited_detail_view_ids": [view_id] if view_id else [],
        "reason": "OK.",
    }


def _extract_response(request: Mapping[str, Any]) -> dict[str, Any]:
    span = request.get("span") if isinstance(request.get("span"), Mapping) else {}
    return {
        "schema_version": EXTRACT_RESPONSE_SCHEMA,
        "candidate_id": str(request.get("candidate_id") or ""),
        "candidate_hash": str(request.get("candidate_hash") or ""),
        "span_id": str(span.get("span_id") or ""),
        "source_hash": str(span.get("source_hash") or ""),
        "utf8_start": span.get("utf8_start"),
        "utf8_end": span.get("utf8_end"),
        "text_hash": str(span.get("text_hash") or ""),
        "expression": _short_text(span.get("text"), request.get("max_expression_tokens")),
    }


def _layout_reduction_response(request: Mapping[str, Any]) -> dict[str, Any]:
    candidate_id = str(request.get("candidate_id") or "")
    candidate_hash = str(request.get("candidate_hash") or "")
    child_nodes = [row for row in request.get("child_nodes") or () if isinstance(row, Mapping)]
    section_rows: dict[str, list[Mapping[str, Any]]] = {}
    intent_rows: dict[str, list[Mapping[str, Any]]] = {}
    for child in child_nodes:
        for row in child.get("layout_by_section") or ():
            if isinstance(row, Mapping):
                section_rows.setdefault(str(row.get("section_id") or ""), []).append(row)
        for row in child.get("intent_layout") or ():
            if isinstance(row, Mapping):
                intent_rows.setdefault(str(row.get("source_field") or ""), []).append(row)
    section_scope: dict[str, Mapping[str, Any]] = {}
    for child in child_nodes:
        for row in child.get("layout_by_section") or ():
            if isinstance(row, Mapping) and row.get("section_id"):
                section_scope.setdefault(str(row["section_id"]), row)
    layout = []
    for section_id, rows in section_rows.items():
        first = rows[0]
        layout.append({
            "section_id": section_id,
            "section_hash": first.get("section_hash"),
            "position": first.get("position"),
            "task_ids": copy.deepcopy(first.get("task_ids") or []),
            "hash_aliases": copy.deepcopy(first.get("hash_aliases") or []),
            "span_ids": [span for row in rows for span in _string_list(row.get("span_ids"))],
            "source_fields": [field for row in rows for field in _string_list(row.get("source_fields"))],
            "expression": _short_text(
                " ".join(str(row.get("expression") or "") for row in rows),
                request.get("expression_token_bound"),
            ),
        })
    layout.sort(key=lambda row: (int(row.get("position") or 0), str(row["section_id"])))
    intent = []
    for field, rows in intent_rows.items():
        intent.append({
            "source_field": field,
            "source_hash": rows[0].get("source_hash"),
            "span_ids": [span for row in rows for span in _string_list(row.get("span_ids"))],
            "expression": _short_text(
                " ".join(str(row.get("expression") or "") for row in rows),
                request.get("expression_token_bound"),
            ),
        })
    intent.sort(key=lambda row: str(row["source_field"]))
    return {
        "schema_version": REDUCE_RESPONSE_SCHEMA,
        "candidate_id": candidate_id,
        "candidate_hash": candidate_hash,
        "node_id": str(request.get("node_id") or ""),
        "child_node_ids": copy.deepcopy(list(request.get("child_node_ids") or [])),
        "child_hashes": copy.deepcopy(list(request.get("child_hashes") or [])),
        "span_ids": copy.deepcopy(list(request.get("expected_span_ids") or [])),
        "layout_by_section": layout,
        "intent_layout": intent,
    }


def _navigation_response(request: Mapping[str, Any], state: Mapping[str, Any]) -> dict[str, Any]:
    scopes = [row for row in request.get("section_scope") or () if isinstance(row, Mapping)]
    mode = str(state.get("navigation_mode") or "order_only") if isinstance(state, Mapping) else "order_only"
    merge_groups = []
    if mode == "coherent_merge":
        for left_index, left in enumerate(scopes):
            for right in scopes[left_index + 1:]:
                common_tasks = sorted(set(_string_list(left.get("task_ids"))).intersection(_string_list(right.get("task_ids"))))
                common_aliases = sorted(set(_string_list(left.get("hash_aliases"))).intersection(_string_list(right.get("hash_aliases"))))
                if not common_tasks and not common_aliases:
                    continue
                source_ids = [str(left.get("section_id") or ""), str(right.get("section_id") or "")]
                source_hashes = {
                    str(left.get("section_id") or ""): str(left.get("section_hash") or ""),
                    str(right.get("section_id") or ""): str(right.get("section_hash") or ""),
                }
                merge_groups = [{
                    "source_section_ids": source_ids,
                    "source_section_hashes": source_hashes,
                    "title": _short_text("Shared.", request.get("merge_proposal_text_budget_tokens")),
                    "goal": _short_text("Preserve.", request.get("merge_proposal_text_budget_tokens")),
                    "integration_reason": _short_text("Overlap.", request.get("merge_proposal_text_budget_tokens")),
                }]
                break
            if merge_groups:
                break
    return {
        "schema_version": NAV_RESPONSE_SCHEMA,
        "candidate_id": str(request.get("candidate_id") or ""),
        "candidate_hash": str(request.get("candidate_hash") or ""),
        "root_node_id": str(request.get("root_node_id") or ""),
        "root_hash": str(request.get("root_hash") or ""),
        "plan_hash": str(request.get("plan_hash") or ""),
        "span_manifest_hash": str(request.get("span_manifest_hash") or ""),
        "section_order": [str(row.get("section_id") or "") for row in scopes],
        "merge_groups": merge_groups,
    }


def _merge_detail_slice_response(request: Mapping[str, Any], state: Mapping[str, Any]) -> dict[str, Any]:
    item_ids = _string_list(request.get("detail_item_ids"))
    item_hashes = _mapping(request.get("detail_item_hashes"))
    mode = str(state.get("navigation_mode") or "order_only") if isinstance(state, Mapping) else "order_only"
    assessments = []
    for detail_id in item_ids:
        detail_item = _detail_item_by_id(request, detail_id)
        disposition = _detail_disposition(detail_item, mode, state, detail_id)
        assessments.append({
            "detail_id": detail_id,
            "value_hash": str(item_hashes.get(detail_id) or ""),
            "disposition": disposition,
        })
    return {
        "schema_version": DETAIL_SLICE_RESPONSE_SCHEMA,
        "candidate_id": str(request.get("candidate_id") or ""),
        "candidate_hash": str(request.get("candidate_hash") or ""),
        "proposal_hash": str(request.get("proposal_hash") or ""),
        "group_id": str(request.get("group_id") or ""),
        "slice_id": str(request.get("slice_id") or ""),
        "detail_item_ids": item_ids,
        "detail_item_hashes": copy.deepcopy(dict(item_hashes)),
        "critical_literal_bindings_hash": str(request.get("source_critical_literal_binding_refs_hash") or ""),
        "intent_context_hash": compute_v3_hash({
            key: copy.deepcopy(request.get(key))
            for key in ("intent_detail_item_ids", "intent_detail_item_hashes")
        }),
        "assessments": assessments,
        "summary": _short_text(
            " ".join(
                _flatten_text(item.get("value"))
                for item in request.get("exact_detail_items") or () if isinstance(item, Mapping)
            ),
            request.get("max_summary_tokens"),
        ),
    }


def _detail_item_by_id(request: Mapping[str, Any], detail_id: str) -> Mapping[str, Any]:
    for field in ("exact_detail_items", "exact_intent_items"):
        for item in request.get(field) or ():
            if isinstance(item, Mapping) and item.get("detail_id") == detail_id:
                return item
    return {}


def _detail_disposition(
    item: Mapping[str, Any], mode: str, state: Mapping[str, Any], detail_id: str,
) -> str:
    value = item.get("value")
    if _ADVERSE_WORDS.search(_flatten_text(value)):
        return "uncertain"
    overrides = state.get("detail_dispositions") if isinstance(state, Mapping) else None
    override = overrides.get(detail_id) if isinstance(overrides, Mapping) else None
    if override in _DISPOSITIONS:
        return str(override)
    return "compatible" if mode == "coherent_merge" else "uncertain"


def _merge_detail_reduction_response(request: Mapping[str, Any]) -> dict[str, Any]:
    body = request.get("body") if isinstance(request.get("body"), Mapping) else request
    children = [row for row in body.get("child_nodes") or () if isinstance(row, Mapping)]
    dispositions = {str(row.get("disposition") or "uncertain") for row in children}
    disposition = "conflict" if "conflict" in dispositions else (
        "uncertain" if "uncertain" in dispositions else "compatible"
    )
    return {
        "schema_version": DETAIL_REDUCE_RESPONSE_SCHEMA,
        "candidate_id": str(body.get("candidate_id") or ""),
        "candidate_hash": str(body.get("candidate_hash") or ""),
        "proposal_hash": str(body.get("proposal_hash") or ""),
        "group_id": str(body.get("group_id") or ""),
        "node_id": str(body.get("node_id") or ""),
        "level": body.get("level"),
        "child_node_ids": copy.deepcopy(list(body.get("child_node_ids") or [])),
        "child_hashes": copy.deepcopy(list(body.get("child_hashes") or [])),
        "detail_item_ids": copy.deepcopy(list(body.get("detail_item_ids") or [])),
        "detail_item_hashes": copy.deepcopy(dict(_mapping(body.get("detail_item_hashes")))),
        "critical_literal_bindings_hash": str(body.get("critical_literal_bindings_hash") or ""),
        "disposition": disposition,
        "summary": _short_text(
            " ".join(str(row.get("summary") or "") for row in children),
            body.get("max_summary_tokens"),
        ),
    }


def _merge_group_finalizer_response(request: Mapping[str, Any], state: Mapping[str, Any]) -> dict[str, Any]:
    root = request.get("detail_root") if isinstance(request.get("detail_root"), Mapping) else {}
    mode = str(state.get("navigation_mode") or "order_only") if isinstance(state, Mapping) else "order_only"
    decision = "accept" if mode == "coherent_merge" and root.get("disposition") == "compatible" else "reject"
    section_ids = _string_list(request.get("source_section_ids"))
    return {
        "schema_version": DETAIL_RESPONSE_SCHEMA,
        "candidate_id": str(request.get("candidate_id") or ""),
        "candidate_hash": str(request.get("candidate_hash") or ""),
        "proposal_hash": str(request.get("proposal_hash") or ""),
        "group_id": str(request.get("group_id") or ""),
        "detail_root_hash": str(root.get("node_hash") or ""),
        "detail_item_ids": copy.deepcopy(list(root.get("detail_item_ids") or [])),
        "detail_item_hashes": copy.deepcopy(dict(_mapping(root.get("detail_item_hashes")))),
        "critical_literal_bindings_hash": str(root.get("critical_literal_bindings_hash") or ""),
        "decision": decision,
        "source_contributions": {
            section_id: _short_text(root.get("summary"), request.get("max_contribution_tokens"))
            for section_id in section_ids
        },
    }


def _literal_payload_direction(value: Any) -> tuple[str, str]:
    text = _flatten_text(value)
    markers = set()
    if re.search(r"\bnull\b", text):
        markers.add("null")
    if re.search(r"\breverse\b", text):
        markers.add("reverse")
    if re.search(r"\bcontradict(?:s|ed|ion)?\b", text):
        markers.add("contradicts")
    if re.search(r"\bminority\b", text):
        markers.add("mixed")
    direction = _aggregate_direction_values(markers) if markers else "supports"
    disagreement = "disagrees" if re.search(r"\b(counterevidence|minority|disagree|conflict)\b", text) else (
        "uncertain" if direction in {"null", "reverse", "contradicts", "mixed"} else "consistent"
    )
    return direction, disagreement


def _safe_detail_override(
    literal: Any, override: Mapping[str, Any], direction: str, disagreement: str,
) -> tuple[str, str]:
    requested_direction = str(override.get("assessed_direction") or direction)
    requested_disagreement = str(override.get("assessed_disagreement") or disagreement)
    if requested_direction not in _DIRECTIONS or requested_disagreement not in _DISAGREEMENT:
        return direction, disagreement
    literal_direction, literal_disagreement = _literal_payload_direction(literal)
    if _ADVERSE_WORDS.search(_flatten_text(literal)):
        return literal_direction, literal_disagreement
    return requested_direction, requested_disagreement


def _flatten_text(value: Any) -> str:
    if isinstance(value, Mapping):
        return " ".join([str(key) for key in value] + [_flatten_text(item) for item in value.values()])
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return " ".join(_flatten_text(item) for item in value)
    return str(value or "").casefold()


def _short_text(value: Any, bound: Any) -> str:
    if not isinstance(value, str) or type(bound) is not int or bound < 1:
        return ""
    words = " ".join(value.split()[:bound])
    return words[: max(1, bound * 2)]


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if isinstance(item, str) and item]


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}
