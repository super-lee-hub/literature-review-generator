from __future__ import annotations

import copy

import pytest

from outline.arbitration_response_row_defaults import (
    COMPACT_CONTRACT,
    COMPACT_VARIABLE_COLUMNS,
    NOT_APPLICABLE_ROW_DEFAULTS,
    RESPONSE_ROW_TABLE_SCHEMA_V1,
    RESPONSE_ROW_TABLE_SCHEMA_V2,
    ROW_TABLE_MARKER,
    ArbitrationResponseRowDefaultsError,
    compact_not_applicable_layout,
    restore_response_row_defaults,
    upgrade_reducer_response_schema,
    validate_response_row_defaults,
)


def _v1_schema() -> dict:
    full_columns = sorted({*COMPACT_VARIABLE_COLUMNS, *NOT_APPLICABLE_ROW_DEFAULTS})
    assessment_columns = sorted({"candidate_id", "candidate_hash", "unit_id", "unit_hash"})
    layouts = [
        list(layout)
        for layout in sorted({tuple(full_columns), tuple(assessment_columns)})
    ]
    assessment_layout_id = layouts.index(assessment_columns)
    full_layout_id = layouts.index(full_columns)
    return {
        "schema_version": RESPONSE_ROW_TABLE_SCHEMA_V1,
        "row_table_marker": ROW_TABLE_MARKER,
        "layouts": layouts,
        "tables": [
            {"path_template": ["assessments"], "layout_ids": [assessment_layout_id]},
            {"path_template": ["group_summaries"], "layout_ids": [full_layout_id]},
        ],
    }


def _not_applicable_row() -> dict:
    return {
        "candidate_id": "$I1",
        "candidate_hash": "$I2",
        "group_id": "$I3",
        "group_hash": "$I4",
        **copy.deepcopy(NOT_APPLICABLE_ROW_DEFAULTS),
    }


def test_v1_schema_is_unchanged_and_v2_appends_only_group_summary_layout() -> None:
    v1 = _v1_schema()
    before = copy.deepcopy(v1)

    assert validate_response_row_defaults(v1) is None
    v2 = upgrade_reducer_response_schema(v1)

    assert v1 == before
    assert v1["schema_version"] == RESPONSE_ROW_TABLE_SCHEMA_V1
    assert v2["schema_version"] == RESPONSE_ROW_TABLE_SCHEMA_V2
    assert v2["layouts"][:-1] == v1["layouts"]
    compact_layout_id = len(v1["layouts"])
    assert v2["layouts"][compact_layout_id] == sorted(COMPACT_VARIABLE_COLUMNS)
    assert v2["row_defaults"] == {str(compact_layout_id): NOT_APPLICABLE_ROW_DEFAULTS}
    assert v2["compact_contract"] == COMPACT_CONTRACT
    assert v2["tables"][0] == v1["tables"][0]
    assert v2["tables"][1]["path_template"] == ["group_summaries"]
    full_layout_id = v1["layouts"].index(sorted({*COMPACT_VARIABLE_COLUMNS, *NOT_APPLICABLE_ROW_DEFAULTS}))
    assert v2["tables"][1]["layout_ids"] == sorted([full_layout_id, compact_layout_id])
    assert validate_response_row_defaults(v2) is None


def test_only_exact_not_applicable_rows_use_compact_layout_and_restore_fully() -> None:
    schema = upgrade_reducer_response_schema(_v1_schema())
    group_layout_ids = schema["tables"][1]["layout_ids"]
    compact_layout_id = len(schema["layouts"]) - 1
    full_layout_id = next(
        index for index, layout in enumerate(schema["layouts"])
        if set(layout) == {*COMPACT_VARIABLE_COLUMNS, *NOT_APPLICABLE_ROW_DEFAULTS}
    )
    absent_row = _not_applicable_row()

    assert compact_not_applicable_layout(absent_row, group_layout_ids, schema) == compact_layout_id
    compact_values = {field: absent_row[field] for field in COMPACT_VARIABLE_COLUMNS}
    assert restore_response_row_defaults(compact_layout_id, compact_values, schema) == absent_row

    active_row = {
        **absent_row,
        "presence": "present",
        "unit_ids": ["$I5"],
        "decision_view_ids": ["$I6"],
        "assessed_direction": "supports",
        "assessed_disagreement": "consistent",
        "assessed_direction_values": ["supports"],
        "assessed_disagreement_values": ["consistent"],
        "child_lineage": [{"child_node_id": "$I7"}],
        "summary": "Active source-bound summary.",
        "evidence_refs": ["$I8"],
        "counterevidence_refs": [],
    }
    assert compact_not_applicable_layout(active_row, group_layout_ids, schema) is None
    assert restore_response_row_defaults(full_layout_id, active_row, schema) == active_row
    assert compact_not_applicable_layout(absent_row, [full_layout_id], schema) is None


@pytest.mark.parametrize(
    ("mutation", "expected_message"),
    [
        ("unknown_default", "unknown or missing fields"),
        ("overlap", "unknown or missing fields"),
        ("changed_default", "differs from its exact template"),
        ("variable_key", "compact group layout"),
        ("wrong_path", "foreign path"),
        ("unknown_version", "version is unsupported"),
        ("default_layout_id", "compact layout defaults are not bound"),
    ],
)
def test_invalid_v2_default_contract_fails_closed(mutation: str, expected_message: str) -> None:
    schema = upgrade_reducer_response_schema(_v1_schema())
    compact_layout_id = len(schema["layouts"]) - 1
    if mutation == "unknown_default":
        schema["row_defaults"][str(compact_layout_id)]["unresolved_risk"] = "not empty"
    elif mutation == "overlap":
        schema["row_defaults"][str(compact_layout_id)]["candidate_id"] = "wrong overlap"
    elif mutation == "changed_default":
        schema["row_defaults"][str(compact_layout_id)]["presence"] = "present"
    elif mutation == "variable_key":
        schema["layouts"][compact_layout_id][0] = "foreign_candidate_id"
    elif mutation == "wrong_path":
        schema["tables"][0]["layout_ids"].append(compact_layout_id)
    elif mutation == "unknown_version":
        schema["schema_version"] = "arbitration_assessment_response_row_tables/v3"
    else:
        schema["row_defaults"]["999"] = schema["row_defaults"].pop(str(compact_layout_id))

    with pytest.raises(ArbitrationResponseRowDefaultsError, match=expected_message):
        validate_response_row_defaults(schema)


def test_compaction_rejects_unknown_fields_and_malformed_allowed_layout_ids() -> None:
    schema = upgrade_reducer_response_schema(_v1_schema())
    compact_layout_id = len(schema["layouts"]) - 1
    group_layout_ids = schema["tables"][1]["layout_ids"]

    with pytest.raises(ArbitrationResponseRowDefaultsError, match="full layout"):
        compact_not_applicable_layout(
            {**_not_applicable_row(), "extra": "must not disappear"},
            group_layout_ids,
            schema,
        )
    with pytest.raises(ArbitrationResponseRowDefaultsError, match="malformed or foreign"):
        compact_not_applicable_layout(_not_applicable_row(), [compact_layout_id, 999], schema)
    with pytest.raises(ArbitrationResponseRowDefaultsError, match="unknown"):
        restore_response_row_defaults(999, {}, schema)
    with pytest.raises(ArbitrationResponseRowDefaultsError, match="unknown or missing variable"):
        restore_response_row_defaults(compact_layout_id, {"candidate_id": "$I1"}, schema)
