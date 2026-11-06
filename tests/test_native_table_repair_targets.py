from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from services.review_draft import (
    build_review_draft,
    find_review_text_block,
    iter_review_text_blocks,
)
from validation.repair_apply import (
    _apply_explicit_cell_citation_mapping,
    _compute_anchor_hash,
    _get_block_text,
    _refresh_factual_cell_offsets,
    apply_patch,
    check_apply_guards,
)
from validation.repair_models import (
    DependencyHashBundle,
    PatchGranularity,
    PatchProposal,
    PatchTargetSignature,
    RepairRootCause,
    NOT_APPLICABLE,
)
from validation.repair_planner import _find_block_for_citation
from validation.repair_transaction import _find_block, _targeted_revalidate
from tests.test_writer_native_table_chain import (
    JOB_ID,
    _manifest,
    _service,
)


@pytest.fixture
def native_table_bundle(tmp_path: Path) -> dict[str, Any]:
    service, packet, outline = _service(tmp_path, [])
    result = service.run(outline_payload=outline, evidence_packets=[packet])
    output_path = tmp_path / "native-table-repair.docx"
    draft = build_review_draft(
        job_id=JOB_ID,
        project_name="native-table-repair",
        draft_id="native-table-repair-draft",
        title="Bounded table review",
        outline_artifact_id="outline-v3:final_outline",
        outline_source_path="local:outline",
        summary_file="local:summary",
        review_word_path=str(output_path),
        sections=result.sections,
        references=[],
        generation_mode="outline_v3",
        citation_ref_catalog=result.citation_ref_catalog,
    ).to_dict()
    manifest = _manifest(draft, result, output_path).to_dict()
    factual_cells = [
        find_review_text_block(draft, str(unit["block_id"]))
        for unit in iter_review_text_blocks(draft["content"]["sections"][0])
    ]
    assert all(cell is not None for cell in factual_cells)
    paper_ids = list(
        dict.fromkeys(
            str(item.get("paper_id") or item.get("paper_key") or "")
            for item in manifest["occurrences"]
            if str(item.get("paper_id") or item.get("paper_key") or "")
        )
    )
    paper_artifacts = [
        {
            "paper_identity": {
                "canonical_paper_key": paper_id,
                "source_paper_id": paper_id,
            },
            "analysis": {"ai_summary": {}},
            "stage1_inputs": {"selected_visual_refs": []},
        }
        for paper_id in paper_ids
    ]
    return {
        "draft": draft,
        "manifest": manifest,
        "catalog": result.citation_ref_catalog,
        "paper_artifacts": paper_artifacts,
        "output_path": output_path,
        "result": result,
        "factual_cells": factual_cells,
    }


def _proposal(
    cell: dict[str, Any],
    *,
    new_text: str,
    span_start: int | None = None,
    span_end: int | None = None,
    expected_text: str | None = None,
    anchor_hash: str | None = None,
) -> PatchProposal:
    old_text = str(cell.get("text") or "")
    paper_ids = list(
        dict.fromkeys(
            str(citation.get("canonical_paper_key") or citation.get("paper_id") or citation.get("paper_key") or "")
            for citation in cell.get("citations") or []
            if str(citation.get("canonical_paper_key") or citation.get("paper_id") or citation.get("paper_key") or "")
        )
    )
    return PatchProposal(
        proposal_id="native-table-repair-proposal",
        citation_id="native-table-citation-set",
        root_cause=RepairRootCause.REVIEW_DRIFT,
        granularity=(PatchGranularity.SPAN if span_start is not None else PatchGranularity.BLOCK),
        target=PatchTargetSignature(
            block_id=str(cell["block_id"]),
            anchor_text=old_text[:80],
            anchor_hash=anchor_hash or _compute_anchor_hash(old_text),
            span_start=span_start,
            span_end=span_end,
        ),
        original_text=old_text if expected_text is None else expected_text,
        proposed_text=new_text,
        confidence=1.0,
        fix_strategy="bounded_cell_repair",
        dependency_bundle=DependencyHashBundle(
            summary_hash=NOT_APPLICABLE,
            paper_artifact_hash=NOT_APPLICABLE,
            visual_manifest_hash=NOT_APPLICABLE,
            selected_visual_refs_hash=NOT_APPLICABLE,
        ),
        metadata={"paper_ids": paper_ids},
    )


def test_native_cell_repair_roundtrips_stable_identity_and_manifest(
    native_table_bundle: dict[str, Any],
) -> None:
    draft = native_table_bundle["draft"]
    cell = native_table_bundle["factual_cells"][0]
    original = deepcopy(cell)
    new_text = str(cell["text"]).replace("improves", "changes", 1)
    proposal = _proposal(cell, new_text=new_text)
    citation = SimpleNamespace(
        target_claim_unit={"block_id": cell["block_id"]},
        block_ids=[cell["block_id"]],
        details={},
    )

    assert _find_block_for_citation(draft, citation) is cell
    assert _find_block(draft, cell["block_id"]) is cell
    assert _get_block_text(draft, cell["block_id"]) == original["text"]
    record = apply_patch(
        proposal,
        draft,
        native_table_bundle["paper_artifacts"],
        "native-table-repair-job",
    )

    assert record is not None
    assert cell["text"] == new_text
    for name in (
        "block_id",
        "cell_id",
        "cell_kind",
        "writer_task_id",
        "writer_output_unit_id",
        "writer_task_basis_hash",
        "required_source_context",
        "allowed_ref_ids",
    ):
        assert cell[name] == original[name]
    table = draft["content"]["sections"][0]["blocks"][0]
    assert table["table_id"] == "results"
    assert table["rows"][0]["row_id"] == "finding"
    assert cell["span_map"] != original["span_map"]
    for citation in cell["citations"]:
        token = str(citation["citation_token"])
        start, end = int(citation["span_start"]), int(citation["span_end"])
        assert new_text[start:end] == token

    rebuilt = _manifest(
        draft,
        native_table_bundle["result"],
        native_table_bundle["output_path"],
    ).to_dict()
    rebuilt_units = [
        unit
        for bundle in rebuilt["citation_sets"]
        for unit in bundle["claim_units"]
    ]
    assert any(unit["block_id"] == cell["block_id"] for unit in rebuilt_units)
    assert any(
        item["block_id"] == cell["block_id"]
        for item in rebuilt["occurrences"]
    )
    targeted = _targeted_revalidate(
        draft,
        rebuilt,
        native_table_bundle["paper_artifacts"],
        native_table_bundle["catalog"],
    )
    assert targeted["passed"] is True, targeted


def test_targeted_revalidation_indexes_cells_and_rejects_foreign_refs(
    native_table_bundle: dict[str, Any],
) -> None:
    valid = _targeted_revalidate(
        native_table_bundle["draft"],
        native_table_bundle["manifest"],
        native_table_bundle["paper_artifacts"],
        native_table_bundle["catalog"],
    )
    assert valid["passed"] is True, valid
    assert valid["block_count"] == len(native_table_bundle["factual_cells"])
    assert valid["mapped_occurrence_count"] == len(native_table_bundle["manifest"]["occurrences"])

    foreign = deepcopy(native_table_bundle["draft"])
    cell_id = native_table_bundle["factual_cells"][0]["block_id"]
    foreign_cell = find_review_text_block(foreign, cell_id)
    assert foreign_cell is not None
    foreign_cell["text"] = foreign_cell["text"].replace("R001", "R999")
    result = _targeted_revalidate(
        foreign,
        native_table_bundle["manifest"],
        native_table_bundle["paper_artifacts"],
        native_table_bundle["catalog"],
    )
    assert result["passed"] is False
    assert "section_writer_scope_invalid:1" in result["diagnostics"]


def test_explicit_cell_citation_mapping_rebinds_only_the_occurrence(
    native_table_bundle: dict[str, Any],
) -> None:
    cell = deepcopy(native_table_bundle["factual_cells"][0])
    citation = cell["citations"][0]
    old_ref_id = str(citation["ref_id"])
    replacement_ref_id = next(
        (
            str(ref_id)
            for ref_id in ("R002", "R003", "R004")
            if ref_id != old_ref_id
        ),
        "R002",
    )
    cell["allowed_ref_ids"] = list(dict.fromkeys([*cell["allowed_ref_ids"], replacement_ref_id]))
    mapping = {
        "occurrence_id": "native-cell-occurrence",
        "expected_ref_id": old_ref_id,
        "replacement_ref_id": replacement_ref_id,
        "replacement_paper_id": "paper-B",
        "start_offset": citation["span_start"],
        "end_offset": citation["span_end"],
        "local_ref_id": citation["local_ref_id"],
    }
    new_text = str(cell["text"]).replace(old_ref_id, replacement_ref_id, 1)
    stable_identity = {
        name: deepcopy(cell[name])
        for name in (
            "block_id",
            "cell_id",
            "writer_task_id",
            "writer_output_unit_id",
            "writer_task_basis_hash",
            "required_source_context",
        )
    }

    assert _apply_explicit_cell_citation_mapping(cell, mapping) is True
    cell["text"] = new_text
    assert _refresh_factual_cell_offsets(cell, new_text) is True
    assert cell["citations"][0]["ref_id"] == replacement_ref_id
    assert cell["citations"][0]["paper_id"] == "paper-B"
    start, end = cell["citations"][0]["span_start"], cell["citations"][0]["span_end"]
    assert new_text[start:end] == f"[[cite_ref:{replacement_ref_id}]]"
    assert {
        name: cell[name]
        for name in stable_identity
    } == stable_identity


def test_repair_refuses_static_cells_and_stale_or_foreign_cell_edits(
    native_table_bundle: dict[str, Any],
) -> None:
    draft = native_table_bundle["draft"]
    table = draft["content"]["sections"][0]["blocks"][0]
    static_cell = table["rows"][0]["cells"][0]
    static_original = static_cell["text"]
    static_proposal = _proposal(static_cell, new_text="Changed static label")
    static_guard = check_apply_guards(
        static_proposal,
        draft,
        native_table_bundle["paper_artifacts"],
    )
    assert static_guard.can_apply is False
    assert apply_patch(
        static_proposal,
        draft,
        native_table_bundle["paper_artifacts"],
        "native-table-repair-job",
    ) is None
    assert static_cell["text"] == static_original

    cell = native_table_bundle["factual_cells"][0]
    old_text = str(cell["text"])
    stale_proposal = _proposal(
        cell,
        new_text=old_text.replace("improves", "changes", 1),
        anchor_hash="0" * 8,
    )
    stale_guard = check_apply_guards(
        stale_proposal,
        draft,
        native_table_bundle["paper_artifacts"],
    )
    assert stale_guard.can_apply is False
    assert apply_patch(
        stale_proposal,
        draft,
        native_table_bundle["paper_artifacts"],
        "native-table-repair-job",
    ) is None
    assert cell["text"] == old_text

    foreign_text = old_text.replace("R001", "R999")
    foreign_proposal = _proposal(cell, new_text=foreign_text)
    foreign_result = apply_patch(
        foreign_proposal,
        draft,
        native_table_bundle["paper_artifacts"],
        "native-table-repair-job",
    )
    assert foreign_result is None
    assert cell["text"] == old_text

    stale_span = _proposal(
        cell,
        new_text="Updated value [[cite_ref:R001]]",
        span_start=0,
        span_end=3,
        expected_text="not the first three cell characters",
    )
    stale_span_guard = check_apply_guards(
        stale_span,
        draft,
        native_table_bundle["paper_artifacts"],
    )
    assert stale_span_guard.can_apply is False
    assert any("Cell span is not bound" in reason for reason in stale_span_guard.block_reasons)
    assert cell["text"] == old_text
