"""Lossless table and exact-string encoding of bounded semantic request data.

The provider receives every source value. Only repeated JSON keys and complete
strings share representations. Responses still use ordinary objects and the
original canonical identities, so physical response/replay hashes are unchanged.
"""

from __future__ import annotations

import copy
import re
from collections import Counter
from collections.abc import Mapping
from typing import Any

from runtime.provider_runtime import hash_json

WIRE_KEY = "semantic_source_wire"
WIRE_VERSION = "semantic-source-tables-and-strings/v1"
_MULTI_SOURCE_VERSION = "semantic-source-tables-and-strings/v2"
_TABLE_KEYS = {"fields", "dependencies", "bridge_claims"}
_TABLE_MARKER = "semantic_row_table_v1"
_CONTRACT = (
    "semantic-source-tables-and-strings/v1: in topic_synthesis only, an object "
    "with semantic_row_table_v1 has columns (ordered key layouts) and rows "
    "[layout_index,values]; zip each layout with its values to reconstruct the "
    "original object array in row order. Exact string tokens prefix+decimal "
    "index refer to strings[index], including source IDs and source prose. "
    "Read the complete strings and tables; preserve missing fields, empty "
    "values, zero and false. Return ordinary output objects and ORIGINAL "
    "canonical IDs/string values, never input table/string reference tokens."
)
_MULTI_SOURCE_CONTRACT = _CONTRACT.replace("v1: in topic_synthesis only", "v2: in each named source_keys field")


class SemanticRequestWireError(ValueError):
    """A semantic request encoding cannot be restored exactly."""


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, Mapping):
        return [string for key, item in value.items() for string in [str(key), *_strings(item)]]
    if isinstance(value, list):
        return [string for item in value for string in _strings(item)]
    return []


def _pack_rows(value: Any) -> Any:
    if isinstance(value, Mapping):
        if _TABLE_MARKER in value:
            raise SemanticRequestWireError("source data collides with the row-table marker")
        result = {}
        for key, item in value.items():
            if key in _TABLE_KEYS and isinstance(item, list) and item and all(isinstance(row, Mapping) for row in item):
                layouts: list[list[str]] = []
                rows = []
                for row in item:
                    if any(not isinstance(name, str) for name in row):
                        raise SemanticRequestWireError("source row keys must be strings")
                    columns = sorted(row)
                    if columns not in layouts:
                        layouts.append(columns)
                    rows.append([layouts.index(columns), [_pack_rows(row[name]) for name in columns]])
                result[key] = {_TABLE_MARKER: {"columns": layouts, "rows": rows}}
            else:
                result[key] = _pack_rows(item)
        return result
    if isinstance(value, list):
        return [_pack_rows(item) for item in value]
    return copy.deepcopy(value)


def _expand_rows(value: Any, *, table_allowed: bool = False) -> Any:
    if isinstance(value, Mapping):
        if _TABLE_MARKER in value:
            if not table_allowed or set(value) != {_TABLE_MARKER}:
                raise SemanticRequestWireError("row-table marker is misplaced")
            table = value[_TABLE_MARKER]
            if not isinstance(table, Mapping) or set(table) != {"columns", "rows"}:
                raise SemanticRequestWireError("row table has invalid fields")
            layouts, rows = table["columns"], table["rows"]
            if not isinstance(layouts, list) or not layouts or not isinstance(rows, list) or not rows:
                raise SemanticRequestWireError("row table has no layouts or rows")
            normalized = []
            for layout in layouts:
                if (not isinstance(layout, list) or any(not isinstance(key, str) for key in layout)
                        or layout != sorted(set(layout)) or layout in normalized):
                    raise SemanticRequestWireError("row-table columns are malformed or duplicated")
                normalized.append(layout)
            used = set()
            result = []
            for row in rows:
                if not isinstance(row, list) or len(row) != 2 or type(row[0]) is not int:
                    raise SemanticRequestWireError("row-table row is malformed")
                index, values = row
                if not 0 <= index < len(layouts) or not isinstance(values, list) or len(values) != len(layouts[index]):
                    raise SemanticRequestWireError("row-table row has invalid index or width")
                used.add(index)
                result.append({key: _expand_rows(item) for key, item in zip(layouts[index], values, strict=True)})
            if used != set(range(len(layouts))):
                raise SemanticRequestWireError("row table contains an unused column layout")
            return result
        return {key: _expand_rows(item, table_allowed=key in _TABLE_KEYS) for key, item in value.items()}
    if isinstance(value, list):
        return [_expand_rows(item) for item in value]
    return copy.deepcopy(value)


def pack_semantic_request_wire(
    request: Mapping[str, Any], *, source_keys: tuple[str, ...] = ("topic_synthesis",),
) -> dict[str, Any]:
    """Encode source row arrays and repeated exact string values once."""
    if WIRE_KEY in request:
        raise SemanticRequestWireError("request already has semantic source wire metadata")
    if (not source_keys or len(set(source_keys)) != len(source_keys)
            or any(key not in {"topic_synthesis", "cross_group_comparison"} for key in source_keys)):
        raise SemanticRequestWireError("semantic source fields are invalid")
    if not any(request.get(key) for key in source_keys):
        return copy.deepcopy(dict(request))
    for key in source_keys:
        if (key not in request or not isinstance(request[key], list if key == "topic_synthesis" else Mapping)):
            raise SemanticRequestWireError("semantic source field is missing or malformed")
    packed = copy.deepcopy(dict(request))
    for key in source_keys:
        packed[key] = _pack_rows(request[key])
    present_strings = {value for key in source_keys for value in _strings(packed[key])}
    # Keys stay literal. Count only value occurrences before choosing strings.
    repeated: Counter[str] = Counter()

    def count_values(value: Any) -> None:
        if isinstance(value, str):
            repeated[value] += 1
        elif isinstance(value, Mapping):
            for item in value.values():
                count_values(item)
        elif isinstance(value, list):
            for item in value:
                count_values(item)

    for key in source_keys:
        count_values(packed[key])
    strings = sorted(value for value, count in repeated.items() if count > 1 and len(value.encode("utf-8")) >= 32)
    prefix = "$S"
    while any(value.startswith(prefix) for value in present_strings):
        prefix += "_"
    indices = {value: index for index, value in enumerate(strings)}

    def replace(value: Any) -> Any:
        if isinstance(value, str):
            return prefix + str(indices[value]) if value in indices else value
        if isinstance(value, Mapping):
            return {key: replace(item) for key, item in value.items()}
        if isinstance(value, list):
            return [replace(item) for item in value]
        return value

    for key in source_keys:
        packed[key] = replace(packed[key])
    multi = source_keys != ("topic_synthesis",)
    packed[WIRE_KEY] = {
        "schema_version": _MULTI_SOURCE_VERSION if multi else WIRE_VERSION,
        "contract": _MULTI_SOURCE_CONTRACT if multi else _CONTRACT,
        **({"source_keys": list(source_keys)} if multi else {}),
        "prefix": prefix,
        "strings": strings,
        "expanded_request_hash": hash_json(dict(request)),
    }
    if expand_semantic_request_wire(packed) != dict(request):
        raise SemanticRequestWireError("semantic source wire is not lossless")
    return packed


def expand_semantic_request_wire(request: Mapping[str, Any]) -> dict[str, Any]:
    """Restore every row/value and reject corrupt or unused wire entries."""
    if WIRE_KEY not in request:
        return copy.deepcopy(dict(request))
    metadata = request[WIRE_KEY]
    multi = isinstance(metadata, Mapping) and metadata.get("schema_version") == _MULTI_SOURCE_VERSION
    if (not isinstance(metadata, Mapping) or set(metadata) != {
        "schema_version", "contract", "prefix", "strings", "expanded_request_hash",
    } | ({"source_keys"} if multi else set())
            or metadata.get("schema_version") != (_MULTI_SOURCE_VERSION if multi else WIRE_VERSION)
            or metadata.get("contract") != (_MULTI_SOURCE_CONTRACT if multi else _CONTRACT)):
        raise SemanticRequestWireError("semantic source wire metadata is invalid")
    source_keys = metadata.get("source_keys", ["topic_synthesis"])
    if (not isinstance(source_keys, list) or not source_keys
            or any(key not in {"topic_synthesis", "cross_group_comparison"} for key in source_keys)
            or len(source_keys) != len(set(source_keys))):
        raise SemanticRequestWireError("semantic source fields are invalid")
    prefix, strings = metadata["prefix"], metadata["strings"]
    if not isinstance(prefix, str) or re.fullmatch(r"\$S_*", prefix) is None:
        raise SemanticRequestWireError("semantic string prefix is invalid")
    if (not isinstance(strings, list) or any(not isinstance(value, str) for value in strings)
            or len(strings) != len(set(strings)) or strings != sorted(strings)
            or any(value.startswith(prefix) for value in strings)):
        raise SemanticRequestWireError("semantic string catalog is invalid")
    used: set[int] = set()

    def restore(value: Any) -> Any:
        if isinstance(value, str) and value.startswith(prefix):
            suffix = value[len(prefix):]
            if not suffix.isascii() or not suffix.isdecimal() or str(int(suffix)) != suffix or not 0 <= int(suffix) < len(strings):
                raise SemanticRequestWireError("semantic string reference is invalid")
            index = int(suffix)
            used.add(index)
            return strings[index]
        if isinstance(value, Mapping):
            return {key: restore(item) for key, item in value.items()}
        if isinstance(value, list):
            return [restore(item) for item in value]
        return copy.deepcopy(value)

    result = {key: copy.deepcopy(value) for key, value in request.items() if key != WIRE_KEY}
    for key in source_keys:
        source = result.get(key)
        if not isinstance(source, list if key == "topic_synthesis" else Mapping):
            raise SemanticRequestWireError(f"semantic source wire lost {key}")
        result[key] = _expand_rows(restore(source))
    if used != set(range(len(strings))):
        raise SemanticRequestWireError("semantic string catalog contains unused entries")
    if hash_json(result) != metadata["expanded_request_hash"]:
        raise SemanticRequestWireError("semantic source wire expanded hash differs")
    return result
