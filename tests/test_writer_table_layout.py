from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from services.writer_source_inventory import SOURCE_INVENTORY_ARTIFACT_ID
from services.writer_table_layout import (
    WRITER_TABLE_LAYOUT_VERSION,
    WRITER_TASK_SCOPE_WITH_TABLES_VERSION,
    WriterTableLayoutError,
    bind_writer_table_layouts_v1,
    project_writer_table_layouts_v1,
)


TASK_ID = "wtv1_task0001"
PRIMARY_UNIT = "wou1_unit0001"
OPTIONAL_UNIT = "wou1_unit0002"
UNUSED_UNIT = "wou1_unit0003"


def _unit(unit_id: str, *, required: bool) -> dict[str, Any]:
    return {
        "writer_output_unit_id": unit_id,
        "unit_kind": "planned_claim" if required else "source_claim_expansion",
        "source_claim_id": None if required else "claim:qualifier",
        "required": required,
        "max_sentences": 1,
        "max_text_chars": 180,
        "max_text_utf8_bytes": 720,
        "source_closure_text_chars": 50,
        "allowed_ref_ids": ["R001", "R002"],
        "required_source_context": {
            "source_claim_ids": ["claim:1"],
            "evidence_ids": ["evidence:1"],
            "source_field_ids": ["field:sample"],
            "qualifier_source_claim_ids": ["claim:qualifier"],
            "qualifier_evidence_ids": ["evidence:qualifier"],
            "qualifier_source_field_ids": ["field:condition"],
        },
    }


def _scope(*, base_basis: str = "a" * 64) -> dict[str, Any]:
    units = [
        _unit(PRIMARY_UNIT, required=True),
        _unit(OPTIONAL_UNIT, required=False),
        _unit(UNUSED_UNIT, required=False),
    ]
    return {
        "schema_version": "writer_task_scope/v1",
        "scope_status": "ready",
        "source_authority_status": "canonical_claim_and_evidence_inventory_verified",
        "usable_for_provider_admission": True,
        "section_id": "section:results",
        "writer_task_basis_hash": base_basis,
        "source_inventory_binding": {
            "artifact_id": SOURCE_INVENTORY_ARTIFACT_ID,
            "artifact_hash": "b" * 64,
            "content_hash": "c" * 64,
        },
        "task_count": 1,
        "required_task_ids": [TASK_ID],
        "tasks": [{
            "writer_task_id": TASK_ID,
            "task_kind": "planned_claim",
            "planned_claim": "The outcome varies under the measured condition.",
            "duplicate_index": None,
            "status": "ready",
            "reason_codes": [],
            "paper_keys": ["paper:1"],
            "source_claim_ids": ["claim:1"],
            "evidence_ids": ["evidence:1"],
            "source_field_ids": ["field:sample"],
            "qualifier_source_claim_ids": ["claim:qualifier"],
            "qualifier_evidence_ids": ["evidence:qualifier"],
            "qualifier_source_field_ids": ["field:condition"],
            "allowed_ref_ids": ["R001", "R002"],
            "support_rows": [],
            "source_evidence": [],
            "canonical_source_bundle": [{
                "source_claims": [{"source_claim_id": "claim:1"}],
                "evidence": [{"evidence_id": "evidence:1"}],
                "source_fields": [{"source_field_id": "field:sample"}],
                "interpretation_dependencies": [{"source_claim_id": "claim:qualifier"}],
                "research_units": [],
            }],
            "source_membership_status": "verified",
            "output_units": units,
        }],
        "source_bundle": {},
        "active_ref_mapping": {"paper:1": ["R001", "R002"]},
        "source_identity_validation": {"verified": ["canonical id membership"]},
        "max_output_units": 3,
        "min_required_output_units": 1,
        "max_serialized_output_bytes_upper_bound": 10_000,
        "output_contract": {
            "block_fields": ["writer_task_id", "writer_output_unit_id", "writer_task_basis_hash", "text"],
            "max_sentences_per_unit": 1,
            "table_bindings": "outside_v1",
        },
    }


def _layout(*, second_unit: str = OPTIONAL_UNIT) -> dict[str, Any]:
    return {
        "schema_version": WRITER_TABLE_LAYOUT_VERSION,
        "table_id": "study-results",
        "headers": ["Measure", "Finding"],
        "rows": [
            {
                "row_id": "sample",
                "cells": [
                    {"static_text": "Sample"},
                    {"writer_task_id": TASK_ID, "writer_output_unit_id": PRIMARY_UNIT},
                ],
            },
            {
                "row_id": "condition",
                "cells": [
                    {"static_text": "Condition"},
                    {"writer_task_id": TASK_ID, "writer_output_unit_id": second_unit},
                ],
            },
        ],
    }


STATIC_LABELS = ["Measure", "Finding", "Sample", "Condition"]


def _validated_output(scope: dict[str, Any]) -> dict[str, Any]:
    basis = scope["writer_task_basis_hash"]
    return {
        "schema_version": "writer_task_scope/v1",
        "writer_task_basis_hash": basis,
        "scope_status": "ready",
        "source_authority_status": "canonical_claim_and_evidence_inventory_verified",
        "source_inventory_binding": deepcopy(scope["source_inventory_binding"]),
        "usable_for_provider_admission": True,
        "blocks": [
            {
                "writer_task_id": TASK_ID,
                "writer_output_unit_id": unit_id,
                "writer_task_basis_hash": basis,
                "text": text,
            }
            for unit_id, text in (
                (PRIMARY_UNIT, "The sample included 24 participants [[cite_ref:R001]]."),
                (OPTIONAL_UNIT, "The outcome varied under the measured condition [[cite_ref:R002]]."),
                (UNUSED_UNIT, "The estimate remained stable in the comparison group [[cite_ref:R001]]."),
            )
        ],
        "task_dispositions": [],
        "block_count": 3,
        "max_output_units": 3,
    }


def test_layout_binds_existing_factual_units_and_promotes_selected_optional_unit() -> None:
    base = _scope()

    bound = bind_writer_table_layouts_v1(
        base,
        [_layout()],
        allowed_static_labels=STATIC_LABELS,
    )

    assert bound["schema_version"] == WRITER_TASK_SCOPE_WITH_TABLES_VERSION
    assert bound["base_writer_task_basis_hash"] == base["writer_task_basis_hash"]
    assert bound["writer_task_basis_hash"] != base["writer_task_basis_hash"]
    assert bound["table_layout_binding"]["table_count"] == 1
    assert bound["table_layout_binding"]["row_count"] == 2
    assert bound["max_table_cells"] == 6  # 2 headers plus 2 rows with 2 cells each
    assert bound["table_layout_binding"]["factual_cell_count"] == 2
    assert bound["table_layout_binding"]["required_table_unit_ids"] == [PRIMARY_UNIT, OPTIONAL_UNIT]
    units = {unit["writer_output_unit_id"]: unit for unit in bound["tasks"][0]["output_units"]}
    assert units[OPTIONAL_UNIT]["required"] is True
    assert units[UNUSED_UNIT]["required"] is False
    assert units[OPTIONAL_UNIT]["required_source_context"]["qualifier_source_claim_ids"] == ["claim:qualifier"]
    assert bound["min_required_output_units"] == 2


def test_basis_binds_original_scope_layout_and_static_label_allowlist() -> None:
    first = bind_writer_table_layouts_v1(_scope(), [_layout()], allowed_static_labels=STATIC_LABELS)
    changed_layout = bind_writer_table_layouts_v1(
        _scope(),
        [_layout(second_unit=UNUSED_UNIT)],
        allowed_static_labels=STATIC_LABELS,
    )
    changed_base = bind_writer_table_layouts_v1(
        _scope(base_basis="d" * 64),
        [_layout()],
        allowed_static_labels=STATIC_LABELS,
    )
    changed_allowlist = bind_writer_table_layouts_v1(
        _scope(),
        [_layout()],
        allowed_static_labels=[*STATIC_LABELS, "Outcome"],
    )

    assert len({
        first["writer_task_basis_hash"],
        changed_layout["writer_task_basis_hash"],
        changed_base["writer_task_basis_hash"],
        changed_allowlist["writer_task_basis_hash"],
    }) == 4


@pytest.mark.parametrize(
    ("layout_mutation", "allowed_labels"),
    [
        (lambda layout: layout["rows"][1]["cells"].__setitem__(1, layout["rows"][0]["cells"][1]), STATIC_LABELS),
        (lambda layout: layout["rows"][1]["cells"][1].update(writer_task_id="foreign-task"), STATIC_LABELS),
        (lambda layout: layout["rows"][1]["cells"].pop(), STATIC_LABELS),
        (lambda layout: layout["rows"][1]["cells"][0].update(static_text="N = 24"), [*STATIC_LABELS, "N = 24"]),
        (lambda layout: layout["rows"][1]["cells"][0].update(static_text="Unsupported label"), STATIC_LABELS),
    ],
)
def test_invalid_duplicate_foreign_missing_or_factual_cells_fail_closed(
    layout_mutation,
    allowed_labels: list[str],
) -> None:
    layout = deepcopy(_layout())
    layout_mutation(layout)

    with pytest.raises(WriterTableLayoutError):
        bind_writer_table_layouts_v1(_scope(), [layout], allowed_static_labels=allowed_labels)


def test_table_rows_must_contain_a_bound_factual_unit() -> None:
    layout = deepcopy(_layout())
    layout["rows"][1]["cells"][1] = {"static_text": "Condition"}

    with pytest.raises(WriterTableLayoutError, match="no source-bound factual cell"):
        bind_writer_table_layouts_v1(
            _scope(),
            [layout],
            allowed_static_labels=STATIC_LABELS,
        )


def test_local_projection_keeps_unit_identity_source_context_and_unbound_paragraphs() -> None:
    bound = bind_writer_table_layouts_v1(_scope(), [_layout()], allowed_static_labels=STATIC_LABELS)

    projected = project_writer_table_layouts_v1(bound, _validated_output(bound))

    assert projected["writer_task_basis_hash"] == bound["writer_task_basis_hash"]
    assert projected["source_inventory_binding"] == bound["source_inventory_binding"]
    assert projected["max_table_cells"] == 6
    assert [block["block_kind"] for block in projected["blocks"]] == ["table", "paragraph"]
    table, paragraph = projected["blocks"]
    assert table["table_layout_schema_version"] == WRITER_TABLE_LAYOUT_VERSION
    assert table["block_id"] == "writer_table_study-results"
    factual_cells = [cell for row in table["rows"] for cell in row["cells"] if cell["cell_kind"] == "factual_output_unit"]
    assert [cell["block_id"] for cell in factual_cells] == [
        f"writer_cell_{PRIMARY_UNIT}",
        f"writer_cell_{OPTIONAL_UNIT}",
    ]
    assert factual_cells[0]["text"] == "The sample included 24 participants [[cite_ref:R001]]."
    assert factual_cells[1]["required_source_context"]["qualifier_source_claim_ids"] == ["claim:qualifier"]
    assert all(cell["source_validation_status"] == "canonical_source_inventory_verified" for cell in factual_cells)
    assert paragraph["writer_output_unit_id"] == UNUSED_UNIT
    assert paragraph["block_id"] == f"writer_unit_{UNUSED_UNIT}"


def test_local_projection_rejects_missing_cell_units_and_markdown_table_bypass() -> None:
    bound = bind_writer_table_layouts_v1(_scope(), [_layout()], allowed_static_labels=STATIC_LABELS)
    missing_cell = _validated_output(bound)
    missing_cell["blocks"] = [
        block for block in missing_cell["blocks"]
        if block["writer_output_unit_id"] != OPTIONAL_UNIT
    ]
    with pytest.raises(WriterTableLayoutError, match="missing validated Writer unit"):
        project_writer_table_layouts_v1(bound, missing_cell)

    markdown_bypass = _validated_output(bound)
    markdown_bypass["blocks"][0]["text"] = (
        "| Measure | Finding |\n| --- | --- |\n| Sample | 24 participants [[cite_ref:R001]] |"
    )
    with pytest.raises(WriterTableLayoutError, match="not atomic"):
        project_writer_table_layouts_v1(bound, markdown_bypass)

    escaped_pipe_bypass = _validated_output(bound)
    escaped_pipe_bypass["blocks"][0]["text"] = (
        "| Measure | Finding |\n| --- | --- |\n| A \\| B | 24 participants [[cite_ref:R001]] |"
    )
    with pytest.raises(WriterTableLayoutError, match="not atomic"):
        project_writer_table_layouts_v1(bound, escaped_pipe_bypass)
