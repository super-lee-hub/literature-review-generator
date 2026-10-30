"""Finite, source-bound Writer table layouts and local projection helpers."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from copy import deepcopy
from typing import Any

from outline.v3_models import compute_v3_hash
from services.citation_ref_catalog import extract_ref_ids_from_token
from services.sentence_segmenter import segment_sentences
from services.writer_source_inventory import SOURCE_INVENTORY_ARTIFACT_ID


WRITER_TABLE_LAYOUT_VERSION = "writer_table_layout/v1"
WRITER_TASK_SCOPE_WITH_TABLES_VERSION = "writer_task_scope/v2"
WRITER_TABLE_PROJECTION_VERSION = "writer_table_projection/v1"

_SCOPE_V1 = "writer_task_scope/v1"
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}\Z")
_CITATION_TOKEN = re.compile(r"\[\[cite_ref:[^\]]+\]\]")
_STATIC_LABEL_FORBIDDEN = re.compile(r"[0-9０-９.!?。！？\r\n]")
_SOURCE_CONTEXT_FIELDS = (
    "source_claim_ids",
    "evidence_ids",
    "source_field_ids",
    "qualifier_source_claim_ids",
    "qualifier_evidence_ids",
    "qualifier_source_field_ids",
)
_MAX_STATIC_LABEL_CHARS = 80
_MAX_STATIC_LABEL_COUNT = 64
_MAX_TABLE_COUNT = 32
_MAX_TABLE_COLUMNS = 16
_MAX_TABLE_ROWS = 512
_MAX_TABLE_CELLS = 4096
_MAX_LAYOUT_BYTES = 64 * 1024


class WriterTableLayoutError(ValueError):
    """A Writer table layout is unbounded or detached from verified scope."""


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _require_digest(value: Any, label: str) -> str:
    digest = _text(value)
    if not _SHA256.fullmatch(digest):
        raise WriterTableLayoutError(f"{label} must be a lowercase SHA-256 digest")
    return digest


def _static_label(value: Any, *, allowed: set[str], label: str) -> str:
    if not isinstance(value, str):
        raise WriterTableLayoutError(f"{label} must be text")
    normalized = value.strip()
    if (
        not normalized
        or normalized != value
        or len(normalized) > _MAX_STATIC_LABEL_CHARS
        or _STATIC_LABEL_FORBIDDEN.search(normalized)
        or _CITATION_TOKEN.search(normalized)
        or normalized not in allowed
    ):
        raise WriterTableLayoutError(f"{label} is not an approved nonfactual table label")
    return normalized


def _allowed_labels(values: Sequence[Any]) -> list[str]:
    if isinstance(values, (str, bytes, bytearray)) or not isinstance(values, Sequence):
        raise WriterTableLayoutError("allowed_static_labels must be a finite string array")
    labels = [_static_label(value, allowed={value} if isinstance(value, str) else set(), label="static label") for value in values]
    if len(labels) > _MAX_STATIC_LABEL_COUNT or len(labels) != len(set(labels)):
        raise WriterTableLayoutError("approved static labels are duplicated or exceed the fixed limit")
    return sorted(labels)


def _verified_scope_indexes(
    scope: Mapping[str, Any],
    *,
    accepted_versions: set[str],
) -> tuple[dict[str, dict[str, Any]], dict[tuple[str, str], dict[str, Any]]]:
    if not isinstance(scope, Mapping) or scope.get("schema_version") not in accepted_versions:
        raise WriterTableLayoutError("Writer scope schema is not supported")
    if (
        scope.get("scope_status") != "ready"
        or scope.get("source_authority_status") != "canonical_claim_and_evidence_inventory_verified"
        or scope.get("usable_for_provider_admission") is not True
    ):
        raise WriterTableLayoutError("Writer scope is not source-verified and ready")
    _require_digest(scope.get("writer_task_basis_hash"), "writer_task_basis_hash")
    binding = scope.get("source_inventory_binding")
    if (
        not isinstance(binding, Mapping)
        or binding.get("artifact_id") != SOURCE_INVENTORY_ARTIFACT_ID
    ):
        raise WriterTableLayoutError("Writer scope has no canonical source inventory binding")
    _require_digest(binding.get("artifact_hash"), "source inventory artifact_hash")
    _require_digest(binding.get("content_hash"), "source inventory content_hash")

    raw_tasks = scope.get("tasks")
    if not isinstance(raw_tasks, list) or not raw_tasks:
        raise WriterTableLayoutError("Writer scope has no task array")
    task_count = scope.get("task_count")
    if isinstance(task_count, bool) or task_count != len(raw_tasks):
        raise WriterTableLayoutError("Writer scope task_count is inconsistent")
    task_ids = scope.get("required_task_ids")
    if (
        not isinstance(task_ids, list)
        or task_ids != [_text(task.get("writer_task_id")) for task in raw_tasks if isinstance(task, Mapping)]
        or len(task_ids) != len(raw_tasks)
        or len(task_ids) != len(set(task_ids))
    ):
        raise WriterTableLayoutError("Writer scope task identities are missing, duplicated, or reordered")

    tasks: dict[str, dict[str, Any]] = {}
    units: dict[tuple[str, str], dict[str, Any]] = {}
    global_unit_ids: set[str] = set()
    for raw_task in raw_tasks:
        if not isinstance(raw_task, Mapping):
            raise WriterTableLayoutError("Writer scope contains a malformed task")
        task = dict(raw_task)
        task_id = _text(task.get("writer_task_id"))
        if (
            not task_id
            or not _IDENTIFIER.fullmatch(task_id)
            or task_id in tasks
            or task.get("status") != "ready"
            or task.get("source_membership_status") != "verified"
        ):
            raise WriterTableLayoutError("table layouts require unique, source-verified ready tasks")
        output_units = task.get("output_units")
        if not isinstance(output_units, list) or not output_units:
            raise WriterTableLayoutError(f"task {task_id} has no output units")
        tasks[task_id] = task
        for raw_unit in output_units:
            if not isinstance(raw_unit, Mapping):
                raise WriterTableLayoutError(f"task {task_id} contains a malformed output unit")
            unit = dict(raw_unit)
            unit_id = _text(unit.get("writer_output_unit_id"))
            key = (task_id, unit_id)
            allowed_refs = unit.get("allowed_ref_ids")
            source_context = unit.get("required_source_context")
            if (
                not unit_id
                or not _IDENTIFIER.fullmatch(unit_id)
                or key in units
                or unit_id in global_unit_ids
                or unit.get("max_sentences") != 1
                or isinstance(unit.get("max_text_chars"), bool)
                or not isinstance(unit.get("max_text_chars"), int)
                or not 1 <= unit["max_text_chars"] <= 4096
                or isinstance(unit.get("max_text_utf8_bytes"), bool)
                or not isinstance(unit.get("max_text_utf8_bytes"), int)
                or not 1 <= unit["max_text_utf8_bytes"] <= 16_384
                or not isinstance(unit.get("required"), bool)
                or not isinstance(allowed_refs, list)
                or not allowed_refs
                or any(not isinstance(ref, str) or not ref.strip() for ref in allowed_refs)
                or len(allowed_refs) != len(set(allowed_refs))
                or not isinstance(source_context, Mapping)
                or any(not isinstance(source_context.get(name), list) for name in _SOURCE_CONTEXT_FIELDS)
            ):
                raise WriterTableLayoutError(f"task {task_id} output unit {unit_id!r} is incomplete")
            global_unit_ids.add(unit_id)
            units[key] = unit
    max_output_units = scope.get("max_output_units")
    min_required_output_units = scope.get("min_required_output_units")
    required_unit_count = sum(unit.get("required") is True for unit in units.values())
    if (
        isinstance(max_output_units, bool)
        or not isinstance(max_output_units, int)
        or max_output_units != len(units)
        or isinstance(min_required_output_units, bool)
        or not isinstance(min_required_output_units, int)
        or min_required_output_units != required_unit_count
    ):
        raise WriterTableLayoutError("Writer scope output-unit cardinality is inconsistent")
    return tasks, units


def _normalize_layouts(
    layouts: Sequence[Any],
    *,
    tasks: Mapping[str, Mapping[str, Any]],
    units: Mapping[tuple[str, str], Mapping[str, Any]],
    allowed_static_labels: Sequence[str],
) -> tuple[list[dict[str, Any]], list[str], dict[str, int]]:
    if isinstance(layouts, (str, bytes, bytearray)) or not isinstance(layouts, Sequence):
        raise WriterTableLayoutError("layouts must be a finite array")
    if len(layouts) > _MAX_TABLE_COUNT:
        raise WriterTableLayoutError("table layout count exceeds the fixed limit")
    approved_labels = set(allowed_static_labels)
    normalized: list[dict[str, Any]] = []
    table_ids: set[str] = set()
    selected_units: list[str] = []
    selected_keys: set[tuple[str, str]] = set()
    total_rows = 0
    total_cells = 0

    for layout_index, raw_layout in enumerate(layouts):
        if not isinstance(raw_layout, Mapping) or set(raw_layout) != {
            "schema_version", "table_id", "headers", "rows"
        }:
            raise WriterTableLayoutError(f"table layout {layout_index} has an invalid schema")
        table_id = raw_layout.get("table_id")
        if (
            raw_layout.get("schema_version") != WRITER_TABLE_LAYOUT_VERSION
            or not isinstance(table_id, str)
            or not _IDENTIFIER.fullmatch(table_id)
            or table_id in table_ids
        ):
            raise WriterTableLayoutError(f"table layout {layout_index} has an invalid or duplicate identity")
        table_ids.add(table_id)

        headers_raw = raw_layout.get("headers")
        if (
            not isinstance(headers_raw, list)
            or not headers_raw
            or len(headers_raw) > min(_MAX_TABLE_COLUMNS, len(approved_labels))
        ):
            raise WriterTableLayoutError(f"table {table_id} has missing or unbounded headers")
        headers = [
            _static_label(value, allowed=approved_labels, label=f"table {table_id} header")
            for value in headers_raw
        ]
        if len(headers) != len(set(headers)):
            raise WriterTableLayoutError(f"table {table_id} has duplicate headers")

        rows_raw = raw_layout.get("rows")
        if not isinstance(rows_raw, list) or not rows_raw:
            raise WriterTableLayoutError(f"table {table_id} has no rows")
        if len(rows_raw) > len(units):
            raise WriterTableLayoutError(f"table {table_id} row count exceeds finite source-unit cardinality")
        rows: list[dict[str, Any]] = []
        row_ids: set[str] = set()
        for row_index, raw_row in enumerate(rows_raw):
            if not isinstance(raw_row, Mapping) or set(raw_row) != {"row_id", "cells"}:
                raise WriterTableLayoutError(f"table {table_id} row {row_index} has an invalid schema")
            row_id = raw_row.get("row_id")
            cells_raw = raw_row.get("cells")
            if (
                not isinstance(row_id, str)
                or not _IDENTIFIER.fullmatch(row_id)
                or row_id in row_ids
                or not isinstance(cells_raw, list)
                or len(cells_raw) != len(headers)
            ):
                raise WriterTableLayoutError(f"table {table_id} row {row_index} has missing, extra, or duplicate cells")
            row_ids.add(row_id)
            cells: list[dict[str, Any]] = []
            row_has_fact = False
            for column_index, raw_cell in enumerate(cells_raw):
                if not isinstance(raw_cell, Mapping):
                    raise WriterTableLayoutError(f"table {table_id} row {row_id} has a malformed cell")
                if set(raw_cell) == {"static_text"}:
                    cells.append({
                        "static_text": _static_label(
                            raw_cell.get("static_text"),
                            allowed=approved_labels,
                            label=f"table {table_id} static cell",
                        ),
                    })
                    continue
                if set(raw_cell) != {"writer_task_id", "writer_output_unit_id"}:
                    raise WriterTableLayoutError(f"table {table_id} row {row_id} has an unsupported cell schema")
                task_id = raw_cell.get("writer_task_id")
                unit_id = raw_cell.get("writer_output_unit_id")
                if not isinstance(task_id, str) or not isinstance(unit_id, str):
                    raise WriterTableLayoutError(f"table {table_id} row {row_id} has a non-string unit reference")
                key = (task_id, unit_id)
                unit = units.get(key)
                if task_id not in tasks or unit is None:
                    raise WriterTableLayoutError(f"table {table_id} references a foreign task or output unit")
                if key in selected_keys:
                    raise WriterTableLayoutError("a factual output unit cannot fill more than one table cell")
                selected_keys.add(key)
                selected_units.append(unit_id)
                row_has_fact = True
                cells.append({
                    "writer_task_id": task_id,
                    "writer_output_unit_id": unit_id,
                })
            if not row_has_fact:
                raise WriterTableLayoutError(f"table {table_id} row {row_id} has no source-bound factual cell")
            rows.append({"row_id": row_id, "cells": cells})
        total_rows += len(rows)
        total_cells += len(headers) + sum(len(row["cells"]) for row in rows)
        normalized.append({
            "schema_version": WRITER_TABLE_LAYOUT_VERSION,
            "table_id": table_id,
            "headers": headers,
            "rows": rows,
        })

    if total_rows > min(_MAX_TABLE_ROWS, len(units)):
        raise WriterTableLayoutError("total table row count exceeds finite source-unit cardinality")
    if total_cells > _MAX_TABLE_CELLS:
        raise WriterTableLayoutError("table cell count exceeds the fixed finite limit")
    try:
        serialized = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise WriterTableLayoutError("table layouts are not bounded JSON") from exc
    if len(serialized) > _MAX_LAYOUT_BYTES:
        raise WriterTableLayoutError("serialized table layouts exceed the byte limit")
    return normalized, selected_units, {
        "table_count": len(normalized),
        "row_count": total_rows,
        "max_table_cells": total_cells,
        "factual_cell_count": len(selected_units),
    }


def _layout_hash(
    *,
    base_basis_hash: str,
    layouts: Sequence[Mapping[str, Any]],
    allowed_static_labels: Sequence[str],
) -> str:
    try:
        return compute_v3_hash({
            "schema_version": WRITER_TABLE_LAYOUT_VERSION,
            "base_writer_task_basis_hash": base_basis_hash,
            "layouts": list(layouts),
            "allowed_static_labels": list(allowed_static_labels),
        })
    except (TypeError, ValueError) as exc:
        raise WriterTableLayoutError("table layout binding is not hashable JSON") from exc


def bind_writer_table_layouts_v1(
    scope: Mapping[str, Any],
    layouts: Sequence[Mapping[str, Any]],
    *,
    allowed_static_labels: Sequence[str],
) -> dict[str, Any]:
    """Bind fixed table layout containers to verified sentence output units.

    Factual cell content remains a normal one-sentence Writer unit. A layout
    can only place those existing units into cells; it cannot create claims or
    add arbitrary Markdown. Optional expansion units selected by a cell become
    required before Writer admission.
    """

    if not isinstance(scope, Mapping) or scope.get("schema_version") != _SCOPE_V1:
        raise WriterTableLayoutError("table binding requires writer_task_scope/v1")
    base_basis = _require_digest(scope.get("writer_task_basis_hash"), "base writer task basis hash")
    labels = _allowed_labels(allowed_static_labels)
    if layouts and not labels:
        raise WriterTableLayoutError("table layouts require caller-approved static labels")
    tasks, units = _verified_scope_indexes(scope, accepted_versions={_SCOPE_V1})
    normalized_layouts, selected_unit_ids, counts = _normalize_layouts(
        layouts,
        tasks=tasks,
        units=units,
        allowed_static_labels=labels,
    )
    layout_hash = _layout_hash(
        base_basis_hash=base_basis,
        layouts=normalized_layouts,
        allowed_static_labels=labels,
    )
    bound = deepcopy(dict(scope))
    bound["schema_version"] = WRITER_TASK_SCOPE_WITH_TABLES_VERSION
    bound["base_writer_task_basis_hash"] = base_basis
    bound["table_layouts"] = normalized_layouts
    bound["table_layout_binding"] = {
        "schema_version": WRITER_TABLE_LAYOUT_VERSION,
        "base_writer_task_basis_hash": base_basis,
        "layout_hash": layout_hash,
        "allowed_static_labels": labels,
        **counts,
        "required_table_unit_ids": selected_unit_ids,
    }
    selected = set(selected_unit_ids)
    promoted_count = 0
    for task in bound["tasks"]:
        for unit in task["output_units"]:
            if str(unit.get("writer_output_unit_id") or "") in selected and not unit["required"]:
                unit["required"] = True
                promoted_count += 1
    bound["min_required_output_units"] = int(scope.get("min_required_output_units") or 0) + promoted_count
    output_contract = deepcopy(dict(scope.get("output_contract") or {}))
    output_contract.update({
        "table_layout_schema_version": WRITER_TABLE_LAYOUT_VERSION,
        "table_bindings": "fixed source-task layout; factual cells reference one validated sentence output unit",
        "max_table_cells": counts["max_table_cells"],
        "static_label_policy": "exact caller allowlist; no citation tokens, digits, or prose",
    })
    bound["output_contract"] = output_contract
    bound["max_table_cells"] = counts["max_table_cells"]
    bound["writer_task_basis_hash"] = compute_v3_hash({
        "schema_version": WRITER_TASK_SCOPE_WITH_TABLES_VERSION,
        "base_writer_task_basis_hash": base_basis,
        "table_layout_hash": layout_hash,
        "table_layouts": normalized_layouts,
        "max_table_cells": counts["max_table_cells"],
    })
    return bound


def _verified_bound_scope(
    scope: Mapping[str, Any],
) -> tuple[str, list[dict[str, Any]], dict[tuple[str, str], dict[str, Any]], dict[str, int]]:
    if not isinstance(scope, Mapping) or scope.get("schema_version") != WRITER_TASK_SCOPE_WITH_TABLES_VERSION:
        raise WriterTableLayoutError("table projection requires writer_task_scope/v2")
    basis = _require_digest(scope.get("writer_task_basis_hash"), "writer_task_basis_hash")
    base_basis = _require_digest(scope.get("base_writer_task_basis_hash"), "base_writer_task_basis_hash")
    binding = scope.get("table_layout_binding")
    if not isinstance(binding, Mapping):
        raise WriterTableLayoutError("bound table scope has no approved static-label list")
    labels = binding.get("allowed_static_labels")
    if not isinstance(labels, list):
        raise WriterTableLayoutError("bound table scope has no approved static-label list")
    labels = _allowed_labels(labels)
    tasks, units = _verified_scope_indexes(scope, accepted_versions={WRITER_TASK_SCOPE_WITH_TABLES_VERSION})
    raw_layouts = scope.get("table_layouts")
    if not isinstance(raw_layouts, list):
        raise WriterTableLayoutError("bound table scope has no layout array")
    normalized_layouts, selected_unit_ids, counts = _normalize_layouts(
        raw_layouts,
        tasks=tasks,
        units=units,
        allowed_static_labels=labels,
    )
    layout_hash = _layout_hash(
        base_basis_hash=base_basis,
        layouts=normalized_layouts,
        allowed_static_labels=labels,
    )
    expected_basis = compute_v3_hash({
        "schema_version": WRITER_TASK_SCOPE_WITH_TABLES_VERSION,
        "base_writer_task_basis_hash": base_basis,
        "table_layout_hash": layout_hash,
        "table_layouts": normalized_layouts,
        "max_table_cells": counts["max_table_cells"],
    })
    if (
        basis != expected_basis
        or binding.get("base_writer_task_basis_hash") != base_basis
        or binding.get("layout_hash") != layout_hash
        or binding.get("required_table_unit_ids") != selected_unit_ids
        or any(binding.get(key) != value for key, value in counts.items())
        or scope.get("max_table_cells") != counts["max_table_cells"]
        or not set(selected_unit_ids).issubset({unit_id for _task_id, unit_id in units})
    ):
        raise WriterTableLayoutError("bound table layout or basis hash changed")
    for task in tasks.values():
        for unit in task["output_units"]:
            if str(unit.get("writer_output_unit_id") or "") in set(selected_unit_ids) and unit.get("required") is not True:
                raise WriterTableLayoutError("table-bound optional output unit was not promoted to required")
    return basis, normalized_layouts, units, counts


def _is_complete_pipe_table(text: str) -> bool:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) < 3 or any("|" not in line for line in lines):
        return False
    rows = [_split_pipe_row(line) for line in lines]
    header, separator = rows[0], rows[1]
    if not header or len(header) != len(separator) or any(not cell for cell in header):
        return False
    if not all(re.fullmatch(r":?-{3,}:?", cell) for cell in separator):
        return False
    return all(len(row) == len(header) for row in rows[2:])


def _split_pipe_row(line: str) -> list[str]:
    """Split a Markdown table row while honoring escaped literal pipes."""
    raw = line.strip()
    cells: list[str] = []
    current: list[str] = []
    escaped = False
    for character in raw:
        if escaped:
            if character == "|":
                current.append("|")
            else:
                current.extend(("\\", character))
            escaped = False
        elif character == "\\":
            escaped = True
        elif character == "|":
            cells.append("".join(current).strip())
            current.clear()
        else:
            current.append(character)
    if escaped:
        current.append("\\")
    cells.append("".join(current).strip())
    if raw.startswith("|") and cells and not cells[0]:
        cells.pop(0)
    if raw.endswith("|") and cells and not cells[-1]:
        cells.pop()
    return cells


def project_writer_table_layouts_v1(
    scope: Mapping[str, Any],
    validated_output: Mapping[str, Any],
) -> dict[str, Any]:
    """Project validated sentence units into native table and paragraph blocks."""

    basis, layouts, units, counts = _verified_bound_scope(scope)
    if (
        not isinstance(validated_output, Mapping)
        or validated_output.get("scope_status") != "ready"
        or validated_output.get("usable_for_provider_admission") is not True
        or validated_output.get("writer_task_basis_hash") != basis
    ):
        raise WriterTableLayoutError("Writer output is not validated against the bound table basis")
    raw_blocks = validated_output.get("blocks")
    if not isinstance(raw_blocks, list) or len(raw_blocks) > int(scope.get("max_output_units") or 0):
        raise WriterTableLayoutError("validated Writer output exceeds the finite unit envelope")

    block_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for raw_block in raw_blocks:
        if not isinstance(raw_block, Mapping) or set(raw_block) != {
            "writer_task_id", "writer_output_unit_id", "writer_task_basis_hash", "text"
        }:
            raise WriterTableLayoutError("validated Writer output contains a malformed unit block")
        task_id = _text(raw_block.get("writer_task_id"))
        unit_id = _text(raw_block.get("writer_output_unit_id"))
        key = (task_id, unit_id)
        unit = units.get(key)
        text = raw_block.get("text")
        if (
            unit is None
            or key in block_by_key
            or raw_block.get("writer_task_basis_hash") != basis
            or not isinstance(text, str)
            or not text.strip()
            or len(text) > int(unit["max_text_chars"])
            or len(text.encode("utf-8")) > int(unit["max_text_utf8_bytes"])
            or len(segment_sentences(text)) != 1
            or _is_complete_pipe_table(text)
        ):
            raise WriterTableLayoutError(f"Writer unit {unit_id!r} is missing, oversized, duplicated, or not atomic")
        ref_ids: list[str] = []
        for match in _CITATION_TOKEN.finditer(text):
            extracted = extract_ref_ids_from_token(match.group(0))
            if not extracted:
                raise WriterTableLayoutError(f"Writer unit {unit_id} has a malformed citation token")
            ref_ids.extend(extracted)
        if not ref_ids or set(ref_ids) - set(unit["allowed_ref_ids"]):
            raise WriterTableLayoutError(f"Writer unit {unit_id} has missing or foreign citations")
        block_by_key[key] = dict(raw_block)

    table_by_unit: dict[tuple[str, str], str] = {}
    for layout in layouts:
        for row in layout["rows"]:
            for cell in row["cells"]:
                if "writer_task_id" in cell:
                    key = (cell["writer_task_id"], cell["writer_output_unit_id"])
                    if key in table_by_unit:
                        raise WriterTableLayoutError("a validated Writer unit is bound to more than one table cell")
                    table_by_unit[key] = layout["table_id"]
                    if key not in block_by_key:
                        raise WriterTableLayoutError(f"table cell is missing validated Writer unit {key[1]}")

    table_by_id = {layout["table_id"]: layout for layout in layouts}
    emitted_tables: set[str] = set()
    output_blocks: list[dict[str, Any]] = []
    ordered_keys = [
        (str(task["writer_task_id"]), str(unit["writer_output_unit_id"]))
        for task in scope["tasks"]
        for unit in task["output_units"]
        if (str(task["writer_task_id"]), str(unit["writer_output_unit_id"])) in block_by_key
    ]

    def table_block(layout: Mapping[str, Any]) -> dict[str, Any]:
        table_id = str(layout["table_id"])
        rows: list[dict[str, Any]] = []
        for row in layout["rows"]:
            cells: list[dict[str, Any]] = []
            for column_index, cell in enumerate(row["cells"]):
                cell_id = f"{table_id}:{row['row_id']}:{column_index}"
                if "static_text" in cell:
                    cells.append({
                        "cell_id": cell_id,
                        "block_id": f"writer_static_cell_{compute_v3_hash(cell_id)[:24]}",
                        "cell_kind": "static_label",
                        "text": cell["static_text"],
                        "source_validation_status": "caller_allowlisted_nonfactual_label",
                    })
                    continue
                task_id = str(cell["writer_task_id"])
                unit_id = str(cell["writer_output_unit_id"])
                unit = units[(task_id, unit_id)]
                output = block_by_key[(task_id, unit_id)]
                cells.append({
                    "cell_id": cell_id,
                    "block_id": f"writer_cell_{unit_id}",
                    "cell_kind": "factual_output_unit",
                    "writer_task_id": task_id,
                    "writer_output_unit_id": unit_id,
                    "writer_task_basis_hash": basis,
                    "text": output["text"],
                    "allowed_ref_ids": list(unit["allowed_ref_ids"]),
                    "required_source_context": deepcopy(dict(unit["required_source_context"])),
                    "source_validation_status": "canonical_source_inventory_verified",
                })
            rows.append({"row_id": row["row_id"], "cells": cells})
        return {
            "block_id": f"writer_table_{table_id}",
            "block_kind": "table",
            "table_layout_schema_version": WRITER_TABLE_LAYOUT_VERSION,
            "table_id": table_id,
            "writer_task_basis_hash": basis,
            "headers": list(layout["headers"]),
            "rows": rows,
        }

    for key in ordered_keys:
        table_id = table_by_unit.get(key)
        if table_id:
            if table_id not in emitted_tables:
                output_blocks.append(table_block(table_by_id[table_id]))
                emitted_tables.add(table_id)
            continue
        unit = units[key]
        output = block_by_key[key]
        output_blocks.append({
            "block_id": f"writer_unit_{key[1]}",
            "block_kind": "paragraph",
            "writer_task_id": key[0],
            "writer_output_unit_id": key[1],
            "writer_task_basis_hash": basis,
            "text": output["text"],
            "allowed_ref_ids": list(unit["allowed_ref_ids"]),
            "required_source_context": deepcopy(dict(unit["required_source_context"])),
            "source_validation_status": "canonical_source_inventory_verified",
        })
    if emitted_tables != set(table_by_id):
        raise WriterTableLayoutError("bound table layout has no validated factual unit to anchor its position")

    return {
        "schema_version": WRITER_TABLE_PROJECTION_VERSION,
        "writer_task_basis_hash": basis,
        "source_inventory_binding": deepcopy(dict(scope["source_inventory_binding"])),
        "table_layout_hash": str(scope["table_layout_binding"]["layout_hash"]),
        "max_table_cells": counts["max_table_cells"],
        "blocks": output_blocks,
    }


__all__ = [
    "WRITER_TABLE_LAYOUT_VERSION",
    "WRITER_TASK_SCOPE_WITH_TABLES_VERSION",
    "WRITER_TABLE_PROJECTION_VERSION",
    "WriterTableLayoutError",
    "bind_writer_table_layouts_v1",
    "project_writer_table_layouts_v1",
]
