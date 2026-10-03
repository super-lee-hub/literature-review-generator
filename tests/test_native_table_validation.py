from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from services.review_draft import build_review_draft, iter_review_text_blocks
from services.artifact_registry import ArtifactDependencyRefV2
from tests.test_writer_native_table_chain import _manifest, _service
from validation.closure import _iter_blocks
from validation.current_validation import _input_contract, run_current_validation
from validation.review_validator import ReviewValidator
from validation.semantic_revalidation import run_semantic_revalidation


def _native_case(tmp_path: Path) -> tuple[dict[str, Any], dict[str, Any], Any, list[dict[str, Any]]]:
    calls: list[Any] = []
    generator, packet, outline = _service(tmp_path, calls)
    result = generator.run(outline_payload=outline, evidence_packets=[packet])
    output_path = tmp_path / "native-table-validation.docx"
    draft = build_review_draft(
        job_id=generator.job_id,
        project_name="native-table",
        draft_id="native-table-validation",
        title="Bounded table review",
        outline_artifact_id="outline-v3:final_outline",
        outline_source_path="local:outline",
        summary_file="local:summary",
        review_word_path=str(output_path),
        sections=result.sections,
        references=[],
        generation_mode="review_v3",
        citation_ref_catalog=result.citation_ref_catalog,
    ).to_dict()
    manifest = _manifest(draft, result, output_path).to_dict()
    first_paper_id = str(manifest["occurrences"][0]["paper_id"])
    papers = [{
        "paper_identity": {
            "canonical_paper_key": first_paper_id,
            "source_paper_id": first_paper_id,
        },
        "analysis": {"ai_summary": {}},
        "stage1_inputs": {},
    }]
    return draft, manifest, result, papers


def _input_contract_for(draft: dict[str, Any], manifest: dict[str, Any], papers: list[dict[str, Any]]):
    class EmptyRegistry:
        def reload(self) -> None:
            return None

        def list_records(self) -> list[Any]:
            return []

    service = SimpleNamespace(
        artifact_registry=EmptyRegistry(),
        review_draft_path="unregistered-review-draft.json",
        citation_manifest_path="unregistered-citation-manifest.json",
    )
    return _input_contract(service, draft, manifest, papers)


def _registered_validation_service(tmp_path: Path, draft: dict[str, Any], manifest: dict[str, Any]):
    from tests.test_current_validator_stage_preflight import _service as make_validation_service

    service = make_validation_service(tmp_path)
    draft_path = Path(service.workspace.artifact_path("native_table_review_draft.json"))
    manifest_path = Path(service.workspace.artifact_path("native_table_citation_manifest.json"))
    registered_draft = deepcopy(draft)
    registered_manifest = deepcopy(manifest)
    registered_draft["created_from_job_id"] = service.job_id
    registered_manifest["created_from_job_id"] = service.job_id
    draft_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    draft_path.write_text(json.dumps(registered_draft, ensure_ascii=False), encoding="utf-8")
    manifest_path.write_text(json.dumps(registered_manifest, ensure_ascii=False), encoding="utf-8")
    draft_record = service.artifact_registry.register_file(
        artifact_id="native-table-validation-draft",
        artifact_role="review_draft",
        artifact_type="review_draft",
        artifact_version="v3",
        path=draft_path,
        producer="tests.native_table_validation",
    )
    service.citation_manifest_record = service.artifact_registry.register_file(
        artifact_id="native-table-validation-manifest",
        artifact_role="citation_manifest",
        artifact_type="citation_manifest",
        artifact_version="v3",
        path=manifest_path,
        producer="tests.native_table_validation",
        depends_on=[ArtifactDependencyRefV2.from_record(draft_record).to_dict()],
    )
    service.review_draft_record = draft_record
    return service


def test_native_cells_are_current_validation_units_semantic_blocks_and_closure_blocks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    draft, manifest, generated, papers = _native_case(tmp_path)
    section = draft["content"]["sections"][0]
    cells = [block for block in iter_review_text_blocks(section) if block.get("table_id")]
    cell_ids = {str(cell["block_id"]) for cell in cells}
    assert len(cell_ids) == 2

    closure_blocks = _iter_blocks(draft)
    assert {str(block["block_id"]) for block in closure_blocks} == cell_ids
    assert all(block.get("block_kind") != "table" for block in closure_blocks)

    semantic = run_semantic_revalidation(
        draft,
        manifest,
        papers,
        citation_ref_catalog=generated.citation_ref_catalog,
    )
    assert semantic.passed is True
    assert semantic.block_count == 2
    assert semantic.mapped_occurrence_count == 2

    _inputs, expected_claims, has_citations, _complete, reasons = _input_contract_for(
        draft,
        manifest,
        papers,
    )
    assert has_citations is True
    assert expected_claims == len(manifest["citation_sets"])
    assert not any(reason.startswith("review_section_writer_scope_invalid:") for reason in reasons)
    assert "citation_manifest_missing_review_citations" not in reasons

    validator = ReviewValidator(draft, manifest, papers, {}, {})
    bundle = validator._get_citation_sets_from_manifest()[0]

    def empty_edge(**kwargs: Any) -> dict[str, Any]:
        return {
            "selected_visual_refs": [],
            "whole_claim_support": {"high": 0, "medium": 0},
            "segment_support": [
                {"high": 0, "medium": 0}
                for _segment in kwargs["segment_coverages"]
            ],
            "evidence_candidates": [],
        }

    monkeypatch.setattr(validator, "_resolve_validation_edge", empty_edge)
    result = validator._validate_citation_set(bundle)
    assert {str(unit["block_id"]) for unit in result.claim_units} == cell_ids
    assert all(unit["claim_text"] and unit["block_context"] for unit in result.claim_units)
    assert all(unit["table_id"] == "results" and unit["cell_id"] for unit in result.claim_units)
    assert result.block_context in {str(cell["text"]) for cell in cells}
    assert result.details["target_table_context"]["table_id"] == "results"
    assert "table results row" in result.claim_context


@pytest.mark.parametrize("mutation", ["missing", "duplicate"])
def test_missing_or_duplicate_native_cell_ids_fail_closed(tmp_path: Path, mutation: str) -> None:
    draft, manifest, generated, papers = _native_case(tmp_path)
    changed = deepcopy(draft)
    rows = changed["content"]["sections"][0]["blocks"][0]["rows"]
    if mutation == "missing":
        del rows[0]["cells"][1]["block_id"]
    else:
        rows[1]["cells"][1]["block_id"] = rows[0]["cells"][1]["block_id"]

    semantic = run_semantic_revalidation(
        changed,
        manifest,
        papers,
        citation_ref_catalog=generated.citation_ref_catalog,
    )
    assert semantic.passed is False
    assert any(
        item.startswith("writer_section_structure_invalid:")
        or item.startswith("writer_section_scope_invalid:")
        for item in semantic.diagnostics
    )
    _inputs, _expected, _has_citations, _complete, reasons = _input_contract_for(
        changed,
        manifest,
        papers,
    )
    assert any(reason.startswith("review_section_writer_scope_invalid:") for reason in reasons)
    if mutation == "duplicate":
        validator = ReviewValidator(changed, manifest, papers, {}, {})
        with pytest.raises(ValueError, match="duplicate"):
            validator._get_block_from_review_draft(rows[0]["cells"][1]["block_id"])


def test_foreign_cell_ref_and_stale_cell_or_occurrence_citation_spans_fail_closed(tmp_path: Path) -> None:
    draft, manifest, generated, papers = _native_case(tmp_path)
    foreign_ref_draft = deepcopy(draft)
    factual_cell = foreign_ref_draft["content"]["sections"][0]["blocks"][0]["rows"][0]["cells"][1]
    factual_cell["text"] = factual_cell["text"].replace("R001", "R999")
    semantic_foreign = run_semantic_revalidation(
        foreign_ref_draft,
        manifest,
        papers,
        citation_ref_catalog=generated.citation_ref_catalog,
    )
    assert semantic_foreign.passed is False
    assert any(item.startswith("writer_section_scope_invalid:") for item in semantic_foreign.diagnostics)
    _inputs, _expected, _has_citations, _complete, foreign_reasons = _input_contract_for(
        foreign_ref_draft,
        manifest,
        papers,
    )
    assert any(reason.startswith("review_section_writer_scope_invalid:") for reason in foreign_reasons)

    stale_cell_draft = deepcopy(draft)
    stale_cell = stale_cell_draft["content"]["sections"][0]["blocks"][0]["rows"][0]["cells"][1]
    stale_cell["citations"][0]["span_start"] += 1
    semantic_stale_cell = run_semantic_revalidation(
        stale_cell_draft,
        manifest,
        papers,
        citation_ref_catalog=generated.citation_ref_catalog,
    )
    assert semantic_stale_cell.passed is False
    assert any(item.startswith("writer_section_scope_invalid:") for item in semantic_stale_cell.diagnostics)
    _inputs, _expected, _has_citations, _complete, stale_cell_reasons = _input_contract_for(
        stale_cell_draft,
        manifest,
        papers,
    )
    assert any(reason.startswith("review_section_writer_scope_invalid:") for reason in stale_cell_reasons)
    validation_service = _registered_validation_service(tmp_path / "current-validation", draft, manifest)
    current_outcome = run_current_validation(
        validation_service,
        review_draft_override=stale_cell_draft,
        citation_manifest_override=manifest,
    )
    assert current_outcome["execution_status"] == "failed"
    assert current_outcome["validation_run_result"].diagnostics == ("validation_review_structure_invalid",)
    assert validation_service._expected_provider_calls == {}

    stale_manifest = deepcopy(manifest)
    stale_occurrence = stale_manifest["occurrences"][0]
    stale_occurrence["spans"][0]["start_offset"] += 1
    semantic_stale = run_semantic_revalidation(
        draft,
        stale_manifest,
        papers,
        citation_ref_catalog=generated.citation_ref_catalog,
    )
    assert semantic_stale.passed is False
    assert any(item.startswith("native_table_citation_span_stale:") for item in semantic_stale.diagnostics)
    _inputs, _expected, _has_citations, _complete, stale_reasons = _input_contract_for(
        draft,
        stale_manifest,
        papers,
    )
    assert any(reason.startswith("native_table_citation_span_invalid:") for reason in stale_reasons)
