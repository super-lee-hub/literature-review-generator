"""Selected-source scope survives real block/draft normalization and strict echoes."""
from __future__ import annotations

from copy import deepcopy

import pytest

from runtime.source_revision import PROVIDER_SEMANTIC_CONTRACT_V2
from services.review_draft import ReviewSection, build_review_draft, validate_review_section_writer_scope
from services.review_generation_service import ReviewGenerationService
from services.writer_table_layout import bind_writer_table_layouts_v1
from tests.test_writer_selected_source_contract import _case, _output, _scope


def _section(tmp_path, *, version=PROVIDER_SEMANTIC_CONTRACT_V2, table=False):
    case = _case(tmp_path)
    scope = _scope(case, version=version)
    if table:
        task = scope["tasks"][0]
        unit = next(unit for unit in task["output_units"] if not unit["required"])
        scope = bind_writer_table_layouts_v1(scope, [{
            "schema_version": "writer_table_layout/v1", "table_id": "conditions",
            "headers": ["Finding"], "rows": [{"row_id": "condition", "cells": [{
                "writer_task_id": task["writer_task_id"],
                "writer_output_unit_id": unit["writer_output_unit_id"],
            }]}],
        }], allowed_static_labels=["Finding"])
    content = _output(scope)
    service = object.__new__(ReviewGenerationService)
    service.provider_semantic_contract_version = version
    refs = sorted({ref for task in scope["tasks"] for unit in task["output_units"] for ref in unit["allowed_ref_ids"]})
    blocks = service._normalize_blocks(
        {"status": "success", "content": content}, section_number=1,
        allowed_ref_ids=refs, catalog=case[1], writer_task_scope=scope,
    )
    section = {"section_number": 1, "section_title": "Unchanged source context", "blocks": blocks,
               "writer_task_scope": scope, "writer_task_dispositions": content["task_dispositions"]}
    if version is not None:
        section["provider_semantic_contract_version"] = version
    return section, case[1]


def _draft(section, catalog):
    return build_review_draft(
        job_id="selected-source-test", project_name="selected-source-test", draft_id="draft",
        outline_artifact_id="outline", outline_source_path="outline.json", summary_file="summary.json",
        review_word_path="review.docx", sections=[section], references=[], generation_mode="outline_v3",
        citation_ref_catalog=catalog,
    ).to_dict()


@pytest.mark.parametrize("table", [False, True])
def test_selected_source_contract_survives_actual_block_and_draft_consumers(tmp_path, table):
    section, catalog = _section(tmp_path, table=table)
    assert validate_review_section_writer_scope(section)
    draft = _draft(section, catalog)
    restored = draft["content"]["sections"][0]
    assert restored["provider_semantic_contract_version"] == PROVIDER_SEMANTIC_CONTRACT_V2
    assert restored["writer_task_scope"] == section["writer_task_scope"]
    assert restored["writer_task_dispositions"] == section["writer_task_dispositions"]
    assert validate_review_section_writer_scope(restored)
    if table:
        assert any(block.get("table_layout_schema_version") == "writer_table_layout/v1"
                   for block in restored["blocks"])


@pytest.mark.parametrize("fault", ["missing", "null", "unknown", "scope_marker_removed", "wrong_echo"])
def test_contract_or_echo_corruption_rejects_before_draft_normalization(tmp_path, fault):
    section, catalog = _section(tmp_path)
    damaged = deepcopy(section)
    if fault == "missing":
        damaged.pop("provider_semantic_contract_version")
    elif fault == "null":
        damaged["provider_semantic_contract_version"] = None
    elif fault == "unknown":
        damaged["provider_semantic_contract_version"] = "unsupported/v2"
    elif fault == "scope_marker_removed":
        damaged["writer_task_scope"].pop("provider_semantic_contract_version")
    else:
        damaged["blocks"][0]["writer_task_basis_hash"] = "f" * 64
    with pytest.raises(ValueError):
        _draft(damaged, catalog)


def test_legacy_section_and_draft_do_not_gain_new_contract_defaults(tmp_path):
    section, catalog = _section(tmp_path, version=None)
    before = deepcopy(section)
    assert validate_review_section_writer_scope(section)
    restored = _draft(section, catalog)["content"]["sections"][0]
    assert "provider_semantic_contract_version" not in restored
    assert "provider_semantic_contract_version" not in restored["writer_task_scope"]
    assert section == before
    assert ReviewSection(1, "Legacy", []).to_dict() == {
        "section_number": 1, "section_title": "Legacy", "blocks": [],
    }
