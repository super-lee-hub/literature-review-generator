from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from services.writer_table_layout import (
    WRITER_TASK_SCOPE_WITH_TABLES_VERSION,
    WriterTableLayoutError,
)
from services.writer_table_plan import (
    WRITER_TABLE_PLAN_STATIC_LABELS,
    WRITER_TABLE_PLAN_VERSION,
    bind_writer_table_plan_v1,
)
from test_writer_table_layout import OPTIONAL_UNIT, PRIMARY_UNIT, TASK_ID, UNUSED_UNIT, _scope as _layout_scope


def _scope() -> dict[str, Any]:
    scope = _layout_scope()
    scope["tasks"][0]["output_units"][1]["source_claim_id"] = "claim:qualifier"
    scope["tasks"][0]["output_units"][2]["source_claim_id"] = "claim:unused"
    return scope


def _packet(claim: str | None = None) -> dict[str, Any]:
    return {
        "planned_claims": [claim or "The outcome varies under the measured condition."],
    }


def _plan(*, source_claim_id: str | None = "claim:qualifier") -> dict[str, Any]:
    return {
        "schema_version": WRITER_TABLE_PLAN_VERSION,
        "tables": [{
            "table_id": "study-results",
            "headers": ["Measure", "Finding"],
            "rows": [
                {
                    "row_id": "sample",
                    "cells": [
                        {"static_text": "Sample"},
                        {"planned_claim_index": 0, "source_claim_id": None},
                    ],
                },
                {
                    "row_id": "condition",
                    "cells": [
                        {"static_text": "Condition"},
                        {"planned_claim_index": 0, "source_claim_id": source_claim_id},
                    ],
                },
            ],
        }],
    }


def test_plan_binds_primary_and_qualifier_units_and_promotes_optional_unit() -> None:
    scope = _scope()
    packet = _packet()
    plan = _plan()

    bound = bind_writer_table_plan_v1(scope, packet, plan)
    repeated = bind_writer_table_plan_v1(scope, packet, plan)

    assert bound["schema_version"] == WRITER_TASK_SCOPE_WITH_TABLES_VERSION
    assert bound["base_writer_task_basis_hash"] == scope["writer_task_basis_hash"]
    assert bound["writer_task_basis_hash"] != scope["writer_task_basis_hash"]
    assert bound["writer_task_basis_hash"] == repeated["writer_task_basis_hash"]
    assert bound["table_layout_binding"]["allowed_static_labels"] == sorted(WRITER_TABLE_PLAN_STATIC_LABELS)
    assert bound["table_layout_binding"]["required_table_unit_ids"] == [PRIMARY_UNIT, OPTIONAL_UNIT]
    assert bound["table_layouts"][0]["rows"][0]["cells"][1] == {
        "writer_task_id": TASK_ID,
        "writer_output_unit_id": PRIMARY_UNIT,
    }
    assert bound["table_layouts"][0]["rows"][1]["cells"][1] == {
        "writer_task_id": TASK_ID,
        "writer_output_unit_id": OPTIONAL_UNIT,
    }
    units = {unit["writer_output_unit_id"]: unit for unit in bound["tasks"][0]["output_units"]}
    assert units[PRIMARY_UNIT]["required"] is True
    assert units[OPTIONAL_UNIT]["required"] is True
    assert units[UNUSED_UNIT]["required"] is False
    assert units[OPTIONAL_UNIT]["required_source_context"]["qualifier_source_claim_ids"] == ["claim:qualifier"]


def test_claim_index_resolves_by_exact_text_when_scope_task_order_differs() -> None:
    scope = _scope()
    original_task = scope["tasks"][0]
    other_task = deepcopy(original_task)
    other_task["writer_task_id"] = "wtv1_other0001"
    other_task["planned_claim"] = "The comparison group remains stable."
    other_task["output_units"][0]["writer_output_unit_id"] = "wou1_other0001"
    other_task["output_units"][1]["writer_output_unit_id"] = "wou1_other0002"
    other_task["output_units"][1]["source_claim_id"] = "claim:other-qualifier"
    other_task["output_units"][2]["writer_output_unit_id"] = "wou1_other0003"
    other_task["output_units"][2]["source_claim_id"] = "claim:other-unused"
    scope["tasks"] = [other_task, original_task]
    scope["required_task_ids"] = [other_task["writer_task_id"], original_task["writer_task_id"]]
    scope["task_count"] = 2
    scope["max_output_units"] = 6
    scope["min_required_output_units"] = 2
    packet = {
        "planned_claims": [original_task["planned_claim"], other_task["planned_claim"]],
    }

    bound = bind_writer_table_plan_v1(scope, packet, _plan())

    assert bound["table_layouts"][0]["rows"][0]["cells"][1]["writer_task_id"] == TASK_ID
    assert bound["table_layouts"][0]["rows"][1]["cells"][1]["writer_task_id"] == TASK_ID
    assert bound["tasks"][0]["writer_task_id"] != TASK_ID


@pytest.mark.parametrize(
    ("packet", "plan", "message"),
    [
        (_packet("A claim absent from scope."), _plan(source_claim_id=None), "exactly one ready Writer task"),
        (_packet(), _plan(source_claim_id="claim:invented"), "exactly one Writer expansion unit"),
        (_packet(), {
            **_plan(),
            "tables": [{
                **_plan()["tables"][0],
                "rows": [{
                    "row_id": "sample",
                    "cells": [
                        {"static_text": "Sample"},
                        {"planned_claim_index": True, "source_claim_id": None},
                    ],
                }],
            }],
        }, "integer, not a boolean"),
        (_packet(), {**_plan(), "extra": "injected"}, "unknown, missing, or extra fields"),
        (_packet(), {
            **_plan(),
            "tables": [{**_plan()["tables"][0], "model_layout": "freeform"}],
        }, "unknown, missing, or extra fields"),
        (_packet(), {
            **_plan(),
            "tables": [{
                **_plan()["tables"][0],
                "rows": [{
                    "row_id": "sample",
                    "cells": [
                        {"static_text": "Sample"},
                        {"planned_claim_index": 0, "source_claim_id": None, "text": "invented fact"},
                    ],
                }],
            }],
        }, "unsupported cell schema"),
        (_packet(), {
            **_plan(),
            "tables": [{
                **_plan()["tables"][0],
                "headers": ["Invented result"],
            }],
        }, "non-product header label"),
        (_packet(), {
            **_plan(),
            "tables": [{
                **_plan()["tables"][0],
                "rows": [{
                    "row_id": "sample",
                    "cells": [
                        {"static_text": "42 participants"},
                        {"planned_claim_index": 0, "source_claim_id": None},
                    ],
                }],
            }],
        }, "unapproved static label"),
    ],
)
def test_wrong_claim_source_selector_boolean_and_injected_plan_fields_fail_closed(
    packet: dict[str, Any],
    plan: dict[str, Any],
    message: str,
) -> None:
    with pytest.raises(WriterTableLayoutError, match=message):
        bind_writer_table_plan_v1(_scope(), packet, plan)


def test_empty_duplicate_and_unbounded_plans_fail_closed() -> None:
    scope = _scope()
    packet = _packet()
    empty = {"schema_version": WRITER_TABLE_PLAN_VERSION, "tables": []}
    with pytest.raises(WriterTableLayoutError, match="non-empty finite"):
        bind_writer_table_plan_v1(scope, packet, empty)

    duplicate_claim_packet = {
        "planned_claims": [packet["planned_claims"][0], packet["planned_claims"][0]],
    }
    with pytest.raises(WriterTableLayoutError, match="claim text is duplicated"):
        bind_writer_table_plan_v1(scope, duplicate_claim_packet, _plan(source_claim_id=None))

    oversized = {
        "schema_version": WRITER_TABLE_PLAN_VERSION,
        "tables": [
            {"table_id": f"table-{index}", "headers": ["Measure"], "rows": []}
            for index in range(33)
        ],
    }
    with pytest.raises(WriterTableLayoutError, match="count exceeds the fixed limit"):
        bind_writer_table_plan_v1(scope, packet, oversized)


def test_duplicate_unit_selector_is_rejected_before_layout_binder(monkeypatch: pytest.MonkeyPatch) -> None:
    plan = _plan(source_claim_id=None)
    plan["tables"][0]["rows"][1]["cells"][1] = {
        "planned_claim_index": 0,
        "source_claim_id": None,
    }

    def unexpected_binder(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise AssertionError("layout binder ran before selector validation completed")

    monkeypatch.setattr("services.writer_table_plan.bind_writer_table_layouts_v1", unexpected_binder)
    with pytest.raises(WriterTableLayoutError, match="cannot fill more than one"):
        bind_writer_table_plan_v1(_scope(), _packet(), plan)
