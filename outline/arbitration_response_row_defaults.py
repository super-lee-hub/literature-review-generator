"""Lossless reducer response-row defaults for not-applicable groups.

This helper is deliberately separate from wire encoding.  It upgrades only a
response row-table schema and provides strict per-row compact/restore
operations; callers still run the ordinary semantic response validators after
restoration.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping, Sequence
from typing import Any

RESPONSE_ROW_TABLE_SCHEMA_V1 = "arbitration_assessment_response_row_tables/v1"
RESPONSE_ROW_TABLE_SCHEMA_V2 = "arbitration_assessment_response_row_tables/v2"
ROW_TABLE_MARKER = "arbitration_row_table_v1"
REDUCER_GROUP_SUMMARIES_PATH = ("group_summaries",)

COMPACT_VARIABLE_COLUMNS = tuple(sorted({
    "candidate_id",
    "candidate_hash",
    "group_id",
    "group_hash",
}))
NOT_APPLICABLE_ROW_DEFAULTS: dict[str, Any] = {
    "presence": "not_applicable",
    "unit_ids": [],
    "decision_view_ids": [],
    "assessed_direction": "not_comparable",
    "assessed_disagreement": "not_applicable",
    "assessed_direction_values": [],
    "assessed_disagreement_values": [],
    "child_lineage": [],
    "summary": "",
    "evidence_refs": [],
    "counterevidence_refs": [],
}
COMPACT_CONTRACT = (
    "Use the compact group_summaries layout only when every fixed field exactly "
    "matches row_defaults. Restore every default before typed ID expansion and "
    "semantic response validation. Otherwise use the full row layout."
)

_BASE_SCHEMA_KEYS = frozenset({
    "schema_version", "row_table_marker", "layouts", "tables",
})
_V2_SCHEMA_KEYS = _BASE_SCHEMA_KEYS | frozenset({"row_defaults", "compact_contract"})
_FULL_ROW_FIELDS = frozenset({*COMPACT_VARIABLE_COLUMNS, *NOT_APPLICABLE_ROW_DEFAULTS})


class ArbitrationResponseRowDefaultsError(ValueError):
    """A reducer response row-default schema or row is invalid."""


def _schema_copy(schema: Any) -> dict[str, Any]:
    if not isinstance(schema, Mapping):
        raise ArbitrationResponseRowDefaultsError("response row-table schema must be an object")
    try:
        copied = copy.deepcopy(dict(schema))
    except (TypeError, ValueError) as exc:
        raise ArbitrationResponseRowDefaultsError("response row-table schema is not JSON-like") from exc
    if any(not isinstance(key, str) for key in copied):
        raise ArbitrationResponseRowDefaultsError("response row-table schema keys must be strings")
    return copied


def _validate_base_schema(schema: Mapping[str, Any], *, version: str) -> None:
    expected_keys = _BASE_SCHEMA_KEYS if version == RESPONSE_ROW_TABLE_SCHEMA_V1 else _V2_SCHEMA_KEYS
    if set(schema) != expected_keys:
        raise ArbitrationResponseRowDefaultsError("response row-table schema has unknown or missing fields")
    if schema.get("schema_version") != version or schema.get("row_table_marker") != ROW_TABLE_MARKER:
        raise ArbitrationResponseRowDefaultsError("response row-table schema version or marker is invalid")

    layouts = schema.get("layouts")
    tables = schema.get("tables")
    if not isinstance(layouts, list) or not isinstance(tables, list):
        raise ArbitrationResponseRowDefaultsError("response row-table layouts and tables must be arrays")
    for layout in layouts:
        if (
            not isinstance(layout, list)
            or not layout
            or any(not isinstance(column, str) or not column for column in layout)
            or len(layout) != len(set(layout))
        ):
            raise ArbitrationResponseRowDefaultsError("response row-table layout columns are invalid")
    layout_tuples = [tuple(layout) for layout in layouts]
    old_layouts = layout_tuples if version == RESPONSE_ROW_TABLE_SCHEMA_V1 else layout_tuples[:-1]
    if old_layouts != sorted(set(old_layouts)):
        raise ArbitrationResponseRowDefaultsError("original response row-table layout IDs/order are not canonical")

    seen_paths: set[tuple[Any, ...]] = set()
    for table in tables:
        if (
            not isinstance(table, Mapping)
            or set(table) != {"path_template", "layout_ids"}
            or not isinstance(table.get("path_template"), list)
            or not isinstance(table.get("layout_ids"), list)
        ):
            raise ArbitrationResponseRowDefaultsError("response row-table path entry is malformed")
        path = tuple(
            tuple(sorted(part.items())) if isinstance(part, Mapping) else part
            for part in table["path_template"]
        )
        if path in seen_paths:
            raise ArbitrationResponseRowDefaultsError("response row-table path is duplicated")
        seen_paths.add(path)
        layout_ids = table["layout_ids"]
        if (
            not layout_ids
            or any(type(layout_id) is not int or not 0 <= layout_id < len(layouts) for layout_id in layout_ids)
            or layout_ids != sorted(set(layout_ids))
        ):
            raise ArbitrationResponseRowDefaultsError("response row-table path layout IDs are invalid")
    if tables != sorted(
        tables,
        key=lambda table: json.dumps(
            table["path_template"], ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ),
    ):
        raise ArbitrationResponseRowDefaultsError("response row-table paths are not canonical-order")


def _group_summary_table(schema: Mapping[str, Any]) -> dict[str, Any]:
    matches = [
        table for table in schema["tables"]
        if tuple(table["path_template"]) == REDUCER_GROUP_SUMMARIES_PATH
    ]
    if len(matches) != 1:
        raise ArbitrationResponseRowDefaultsError(
            "reducer response schema must declare exactly one group_summaries table"
        )
    if not isinstance(matches[0], dict):
        raise ArbitrationResponseRowDefaultsError("group_summaries table must be a JSON object")
    return matches[0]


def _full_group_layout_ids(schema: Mapping[str, Any], group_table: Mapping[str, Any]) -> list[int]:
    layouts = schema["layouts"]
    full = [
        layout_id for layout_id in group_table["layout_ids"]
        if set(layouts[layout_id]) == _FULL_ROW_FIELDS
        and len(layouts[layout_id]) == len(_FULL_ROW_FIELDS)
    ]
    if len(full) != 1:
        raise ArbitrationResponseRowDefaultsError(
            "group_summaries must retain exactly one full 15-column row layout"
        )
    return full


def upgrade_reducer_response_schema(v1_schema: Mapping[str, Any]) -> dict[str, Any]:
    """Return a v2 copy with one compact not-applicable group row layout."""

    source = _schema_copy(v1_schema)
    _validate_base_schema(source, version=RESPONSE_ROW_TABLE_SCHEMA_V1)
    group_table = _group_summary_table(source)
    _full_group_layout_ids(source, group_table)
    compact_columns = list(COMPACT_VARIABLE_COLUMNS)
    if compact_columns in source["layouts"]:
        raise ArbitrationResponseRowDefaultsError(
            "v1 response schema already contains the reserved compact group layout"
        )

    upgraded = copy.deepcopy(source)
    compact_layout_id = len(upgraded["layouts"])
    upgraded["layouts"].append(compact_columns)
    upgraded_group_table = _group_summary_table(upgraded)
    upgraded_group_table["layout_ids"] = sorted(
        {*upgraded_group_table["layout_ids"], compact_layout_id},
    )
    upgraded["schema_version"] = RESPONSE_ROW_TABLE_SCHEMA_V2
    upgraded["row_defaults"] = {
        str(compact_layout_id): copy.deepcopy(NOT_APPLICABLE_ROW_DEFAULTS),
    }
    upgraded["compact_contract"] = COMPACT_CONTRACT
    validate_response_row_defaults(upgraded)
    return upgraded


def validate_response_row_defaults(schema: Mapping[str, Any]) -> None:
    """Validate v1 unchanged or the exact closed v2 defaults extension."""

    copied = _schema_copy(schema)
    version = copied.get("schema_version")
    if version == RESPONSE_ROW_TABLE_SCHEMA_V1:
        _validate_base_schema(copied, version=RESPONSE_ROW_TABLE_SCHEMA_V1)
        return
    if version != RESPONSE_ROW_TABLE_SCHEMA_V2:
        raise ArbitrationResponseRowDefaultsError("response row-table schema version is unsupported")
    _validate_base_schema(copied, version=RESPONSE_ROW_TABLE_SCHEMA_V2)
    if copied.get("compact_contract") != COMPACT_CONTRACT:
        raise ArbitrationResponseRowDefaultsError("response row-default contract is invalid")
    group_table = _group_summary_table(copied)
    full_layout_ids = _full_group_layout_ids(copied, group_table)
    compact_columns = list(COMPACT_VARIABLE_COLUMNS)
    if not copied["layouts"] or copied["layouts"][-1] != compact_columns:
        raise ArbitrationResponseRowDefaultsError("compact group layout must be appended without renumbering v1 layouts")
    compact_layout_id = len(copied["layouts"]) - 1
    default_rows = copied.get("row_defaults")
    if (
        not isinstance(default_rows, Mapping)
        or set(default_rows) != {str(compact_layout_id)}
        or compact_layout_id not in group_table["layout_ids"]
        or not set(full_layout_ids).issubset(set(group_table["layout_ids"]))
    ):
        raise ArbitrationResponseRowDefaultsError("compact layout defaults are not bound to group_summaries")
    if any(
        compact_layout_id in table["layout_ids"]
        for table in copied["tables"]
        if table is not group_table
    ):
        raise ArbitrationResponseRowDefaultsError("compact not-applicable layout is allowed at a foreign path")
    defaults = default_rows[str(compact_layout_id)]
    if not isinstance(defaults, Mapping) or set(defaults) != set(NOT_APPLICABLE_ROW_DEFAULTS):
        raise ArbitrationResponseRowDefaultsError("row defaults contain unknown or missing fields")
    if set(defaults).intersection(compact_columns):
        raise ArbitrationResponseRowDefaultsError("row defaults overlap variable compact columns")
    for field, expected in NOT_APPLICABLE_ROW_DEFAULTS.items():
        value = defaults[field]
        if type(value) is not type(expected) or value != expected:
            raise ArbitrationResponseRowDefaultsError(f"row default {field} differs from its exact template")
    if set(defaults).union(compact_columns) != _FULL_ROW_FIELDS:
        raise ArbitrationResponseRowDefaultsError("compact defaults and variable columns do not cover the full row")


def compact_not_applicable_layout(
    row_values: Mapping[str, Any],
    allowed_layout_ids: Sequence[int],
    schema: Mapping[str, Any],
) -> int | None:
    """Choose the compact layout only for a complete exact default-template row."""

    validate_response_row_defaults(schema)
    if schema.get("schema_version") == RESPONSE_ROW_TABLE_SCHEMA_V1:
        return None
    if not isinstance(row_values, Mapping):
        raise ArbitrationResponseRowDefaultsError("group_summaries row must be an object")
    if set(row_values) != _FULL_ROW_FIELDS:
        raise ArbitrationResponseRowDefaultsError("group_summaries row fields differ from the exact full layout")
    if any(not isinstance(row_values.get(field), str) or not row_values[field] for field in COMPACT_VARIABLE_COLUMNS):
        raise ArbitrationResponseRowDefaultsError("compact group identity fields must be nonempty strings")
    if not isinstance(allowed_layout_ids, Sequence) or isinstance(allowed_layout_ids, (str, bytes)):
        raise ArbitrationResponseRowDefaultsError("allowed response layout IDs must be a sequence")
    if (
        any(type(layout_id) is not int or not 0 <= layout_id < len(schema["layouts"])
            for layout_id in allowed_layout_ids)
        or len(allowed_layout_ids) != len(set(allowed_layout_ids))
    ):
        raise ArbitrationResponseRowDefaultsError("allowed response layout IDs are malformed or foreign")
    compact_layout_id = len(schema["layouts"]) - 1
    if compact_layout_id not in allowed_layout_ids:
        return None
    defaults = schema["row_defaults"][str(compact_layout_id)]
    if any(
        type(row_values[field]) is not type(expected) or row_values[field] != expected
        for field, expected in defaults.items()
    ):
        return None
    return compact_layout_id


def restore_response_row_defaults(
    layout_id: int,
    row_values: Mapping[str, Any],
    schema: Mapping[str, Any],
) -> dict[str, Any]:
    """Expand a v2 compact row before typed alias decoding and semantic validation."""

    validate_response_row_defaults(schema)
    if type(layout_id) is not int or not 0 <= layout_id < len(schema["layouts"]):
        raise ArbitrationResponseRowDefaultsError("response row layout ID is unknown")
    if not isinstance(row_values, Mapping):
        raise ArbitrationResponseRowDefaultsError("response row values must be an object")
    observed = copy.deepcopy(dict(row_values))
    if schema.get("schema_version") == RESPONSE_ROW_TABLE_SCHEMA_V2 and layout_id == len(schema["layouts"]) - 1:
        if set(observed) != set(COMPACT_VARIABLE_COLUMNS):
            raise ArbitrationResponseRowDefaultsError("compact response row has unknown or missing variable fields")
        if any(not isinstance(observed[field], str) or not observed[field] for field in COMPACT_VARIABLE_COLUMNS):
            raise ArbitrationResponseRowDefaultsError("compact response row identity fields are invalid")
        return {
            **copy.deepcopy(schema["row_defaults"][str(layout_id)]),
            **observed,
        }

    if set(observed) != set(schema["layouts"][layout_id]):
        raise ArbitrationResponseRowDefaultsError("full response row columns differ from its declared layout")
    return observed


__all__ = [
    "COMPACT_CONTRACT",
    "COMPACT_VARIABLE_COLUMNS",
    "NOT_APPLICABLE_ROW_DEFAULTS",
    "RESPONSE_ROW_TABLE_SCHEMA_V1",
    "RESPONSE_ROW_TABLE_SCHEMA_V2",
    "ArbitrationResponseRowDefaultsError",
    "compact_not_applicable_layout",
    "restore_response_row_defaults",
    "upgrade_reducer_response_schema",
    "validate_response_row_defaults",
]
