"""Bind declarative Writer table plans to verified task output units."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any, NoReturn, TypeGuard

from services.writer_table_layout import (
    WRITER_TABLE_LAYOUT_VERSION,
    WriterTableLayoutError,
    bind_writer_table_layouts_v1,
)


WRITER_TABLE_PLAN_VERSION = "writer_table_plan/v1"
WRITER_TABLE_PLAN_STATIC_LABELS = (
    "Measure",
    "Finding",
    "Evidence",
    "Context",
    "Result",
    "Sample",
    "Condition",
    "指标",
    "研究发现",
    "证据",
    "条件",
    "结果",
    "样本量",
)

_SCOPE_V1 = "writer_task_scope/v1"
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}\Z")
_MAX_TABLE_COUNT = 32
_MAX_TABLE_COLUMNS = 16
_MAX_TABLE_ROWS = 512
_MAX_TABLE_CELLS = 4096
_MAX_LAYOUT_BYTES = 64 * 1024
_STATIC_CELL_FIELDS = {"static_text"}
_SELECTOR_CELL_FIELDS = {"planned_claim_index", "source_claim_id"}


def _invalid(message: str) -> NoReturn:
    raise WriterTableLayoutError(message)


def _exact_mapping(value: Any, fields: set[str], label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != fields:
        _invalid(f"{label} has unknown, missing, or extra fields")
    return value


def _valid_identifier(value: Any) -> TypeGuard[str]:
    return isinstance(value, str) and _IDENTIFIER.fullmatch(value) is not None


def _scope_tasks_and_unit_count(scope: Mapping[str, Any]) -> tuple[list[Mapping[str, Any]], int]:
    if scope.get("schema_version") != _SCOPE_V1:
        _invalid("table planning requires writer_task_scope/v1")
    raw_tasks = scope.get("tasks")
    if not isinstance(raw_tasks, list) or not raw_tasks:
        _invalid("Writer scope has no task array")

    tasks: list[Mapping[str, Any]] = []
    unit_count = 0
    for raw_task in raw_tasks:
        if not isinstance(raw_task, Mapping):
            _invalid("Writer scope contains a malformed task")
        raw_units = raw_task.get("output_units")
        if not isinstance(raw_units, list):
            _invalid("Writer task has no output-unit array")
        tasks.append(raw_task)
        unit_count += len(raw_units)
    if unit_count < 1:
        _invalid("Writer scope has no finite source output units")
    return tasks, unit_count


def _ready_task_for_claim(
    *,
    tasks: list[Mapping[str, Any]],
    claim: str,
) -> Mapping[str, Any]:
    matches = [
        task
        for task in tasks
        if task.get("task_kind") == "planned_claim" and task.get("planned_claim") == claim
    ]
    if len(matches) != 1 or matches[0].get("status") != "ready":
        _invalid("planned claim does not map to exactly one ready Writer task")
    task_id = matches[0].get("writer_task_id")
    if not _valid_identifier(task_id):
        _invalid("planned claim task has an invalid identity")
    return matches[0]


def _selected_unit(
    *,
    task: Mapping[str, Any],
    source_claim_id: Any,
) -> Mapping[str, Any]:
    raw_units = task.get("output_units")
    if not isinstance(raw_units, list):
        _invalid("planned claim task has no output-unit array")

    if source_claim_id is None:
        candidates = [
            unit
            for unit in raw_units
            if isinstance(unit, Mapping) and unit.get("unit_kind") == "planned_claim"
        ]
        if len(candidates) != 1 or candidates[0].get("source_claim_id") is not None:
            _invalid("planned claim must resolve to exactly one primary output unit")
        if candidates[0].get("required") is not True:
            _invalid("planned claim primary output unit is not required")
    else:
        if not isinstance(source_claim_id, str) or not source_claim_id.strip():
            _invalid("source_claim_id must be a non-empty string or null")
        candidates = [
            unit
            for unit in raw_units
            if isinstance(unit, Mapping)
            and unit.get("unit_kind") == "source_claim_expansion"
            and unit.get("source_claim_id") == source_claim_id
        ]
        if len(candidates) != 1:
            _invalid("source claim does not map to exactly one Writer expansion unit")

    unit_id = candidates[0].get("writer_output_unit_id")
    if not _valid_identifier(unit_id):
        _invalid("selected Writer output unit has an invalid identity")
    return candidates[0]


def bind_writer_table_plan_v1(
    scope: Mapping[str, Any],
    packet: Mapping[str, Any],
    plan: Mapping[str, Any],
) -> dict[str, Any]:
    """Bind fixed table structure to existing source-backed Writer units.

    The plan can choose only packet claim indexes and source-claim IDs already
    represented by a ready task. The adapter derives Writer task and unit IDs
    from that verified scope, then delegates basis binding and optional-unit
    promotion to ``bind_writer_table_layouts_v1``.
    """

    if not isinstance(scope, Mapping) or not isinstance(packet, Mapping):
        _invalid("table planning requires a Writer scope and section packet")
    plan_fields = _exact_mapping(plan, {"schema_version", "tables"}, "table plan")
    if plan_fields.get("schema_version") != WRITER_TABLE_PLAN_VERSION:
        _invalid("table plan schema version is not supported")
    raw_tables = plan_fields.get("tables")
    if not isinstance(raw_tables, list) or not raw_tables:
        _invalid("table plan must contain a non-empty finite table array")
    if len(raw_tables) > _MAX_TABLE_COUNT:
        _invalid("table plan count exceeds the fixed limit")

    raw_claims = packet.get("planned_claims")
    if not isinstance(raw_claims, list):
        _invalid("section packet has no ordered planned-claims array")
    tasks, unit_count = _scope_tasks_and_unit_count(scope)

    layouts: list[dict[str, Any]] = []
    table_ids: set[str] = set()
    selected_units: set[tuple[str, str]] = set()
    total_rows = 0
    total_cells = 0
    approved_labels = set(WRITER_TABLE_PLAN_STATIC_LABELS)

    for table_index, raw_table in enumerate(raw_tables):
        table = _exact_mapping(
            raw_table,
            {"table_id", "headers", "rows"},
            f"table {table_index}",
        )
        table_id = table.get("table_id")
        if not _valid_identifier(table_id) or table_id in table_ids:
            _invalid(f"table {table_index} has an invalid or duplicate identity")
        table_ids.add(table_id)

        raw_headers = table.get("headers")
        if (
            not isinstance(raw_headers, list)
            or not raw_headers
            or len(raw_headers) > min(_MAX_TABLE_COLUMNS, len(approved_labels))
        ):
            _invalid(f"table {table_id} has missing or unbounded headers")
        headers: list[str] = []
        for header in raw_headers:
            if not isinstance(header, str) or header not in approved_labels:
                _invalid(f"table {table_id} has a non-product header label")
            headers.append(header)
        if len(headers) != len(set(headers)):
            _invalid(f"table {table_id} has duplicate headers")

        raw_rows = table.get("rows")
        if (
            not isinstance(raw_rows, list)
            or not raw_rows
            or len(raw_rows) > min(_MAX_TABLE_ROWS, unit_count)
        ):
            _invalid(f"table {table_id} has missing or unbounded rows")
        total_rows += len(raw_rows)
        if total_rows > min(_MAX_TABLE_ROWS, unit_count):
            _invalid("table plan row count exceeds finite source-unit cardinality")
        total_cells += len(headers) + len(raw_rows) * len(headers)
        if total_cells > _MAX_TABLE_CELLS:
            _invalid("table plan cell count exceeds the fixed finite limit")

        rows: list[dict[str, Any]] = []
        row_ids: set[str] = set()
        for row_index, raw_row in enumerate(raw_rows):
            row = _exact_mapping(raw_row, {"row_id", "cells"}, f"table {table_id} row {row_index}")
            row_id = row.get("row_id")
            raw_cells = row.get("cells")
            if (
                not _valid_identifier(row_id)
                or row_id in row_ids
                or not isinstance(raw_cells, list)
                or len(raw_cells) != len(headers)
            ):
                _invalid(f"table {table_id} row {row_index} has invalid or duplicate cells")
            row_ids.add(row_id)

            cells: list[dict[str, Any]] = []
            row_has_source_unit = False
            for cell_index, raw_cell in enumerate(raw_cells):
                if not isinstance(raw_cell, Mapping):
                    _invalid(f"table {table_id} row {row_id} cell {cell_index} is malformed")
                fields = set(raw_cell)
                if fields == _STATIC_CELL_FIELDS:
                    static_text = raw_cell.get("static_text")
                    if not isinstance(static_text, str) or static_text not in approved_labels:
                        _invalid(f"table {table_id} row {row_id} has an unapproved static label")
                    cells.append({"static_text": static_text})
                    continue
                if fields != _SELECTOR_CELL_FIELDS:
                    _invalid(f"table {table_id} row {row_id} has an unsupported cell schema")

                claim_index = raw_cell.get("planned_claim_index")
                source_claim_id = raw_cell.get("source_claim_id")
                if isinstance(claim_index, bool) or not isinstance(claim_index, int):
                    _invalid("planned_claim_index must be an integer, not a boolean")
                if not 0 <= claim_index < len(raw_claims):
                    _invalid("planned_claim_index is outside the packet claims array")
                claim = raw_claims[claim_index]
                if not isinstance(claim, str) or not claim.strip():
                    _invalid("selected packet claim must be non-empty text")
                if sum(item == claim for item in raw_claims) != 1:
                    _invalid("selected packet claim text is duplicated")

                task = _ready_task_for_claim(tasks=tasks, claim=claim)
                unit = _selected_unit(task=task, source_claim_id=source_claim_id)
                task_id = task["writer_task_id"]
                unit_id = unit["writer_output_unit_id"]
                selected_key = (task_id, unit_id)
                if selected_key in selected_units:
                    _invalid("a claim output unit cannot fill more than one table cell")
                selected_units.add(selected_key)
                row_has_source_unit = True
                cells.append({
                    "writer_task_id": task_id,
                    "writer_output_unit_id": unit_id,
                })
            if not row_has_source_unit:
                _invalid(f"table {table_id} row {row_id} has no source-bound factual cell")
            rows.append({"row_id": row_id, "cells": cells})

        layouts.append({
            "schema_version": WRITER_TABLE_LAYOUT_VERSION,
            "table_id": table_id,
            "headers": headers,
            "rows": rows,
        })

    try:
        layout_bytes = json.dumps(
            layouts,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise WriterTableLayoutError("table plan is not bounded JSON") from exc
    if len(layout_bytes) > _MAX_LAYOUT_BYTES:
        _invalid("serialized table plan exceeds the fixed byte limit")

    return bind_writer_table_layouts_v1(
        scope,
        layouts,
        allowed_static_labels=WRITER_TABLE_PLAN_STATIC_LABELS,
    )
