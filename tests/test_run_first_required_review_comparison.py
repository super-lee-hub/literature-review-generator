from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest
from services.review_draft import iter_review_text_blocks

from test_run_first_required_comparison_binding import (
    _PAPER_KEYS,
    _QUESTION,
    _RELATION_TYPE,
    _build_case,
)
from validation.closure import required_review_comparison_issues


def _case(tmp_path: Path, *, table: bool = False):
    fixture = _build_case(tmp_path, table=table)
    registry = fixture["registry"]
    draft = fixture["draft"]
    section = draft["content"]["sections"][0]
    writer_task = section["writer_task_scope"]["tasks"][0]
    output_unit = writer_task["output_units"][0]
    text_block = next(iter_review_text_blocks(section))
    binding = {
        "writer_task_id": writer_task["writer_task_id"],
        "writer_output_unit_id": output_unit["writer_output_unit_id"],
        "writer_task_basis_hash": section["writer_task_scope"]["writer_task_basis_hash"],
    }
    occurrences = [
        {
            "occurrence_id": f"occ-{paper_key}",
            "block_id": text_block["block_id"],
            "paper_key": paper_key,
            "canonical_paper_key": paper_key,
        }
        for paper_key in _PAPER_KEYS
    ]
    manifest = {
        "artifact_type": "citation_manifest",
        "artifact_version": "v3",
        "occurrences": occurrences,
        "citation_sets": [{
            "claim_units": [{
                "block_id": text_block["block_id"],
                "alignment_status": "explicit",
                **binding,
                "supporting_paper_keys": list(_PAPER_KEYS),
                "supporting_occurrence_ids": [row["occurrence_id"] for row in occurrences],
            }],
        }],
    }
    spec_record = registry.get("runtime_job_spec")
    assert spec_record is not None
    return registry, draft, manifest, Path(spec_record.path)


def test_required_comparison_uses_verified_outline_task_and_both_sources(tmp_path: Path):
    registry, draft, manifest, _path = _case(tmp_path)

    assert required_review_comparison_issues(registry, draft, manifest) == []


def test_required_comparison_accepts_same_bound_unit_in_native_table_cell(tmp_path: Path):
    registry, draft, manifest, _path = _case(tmp_path, table=True)

    assert required_review_comparison_issues(registry, draft, manifest) == []


@pytest.mark.parametrize(
    "failure",
    ["b_only", "split_units", "pooled", "foreign_occurrence", "stale_basis", "unbound"],
)
def test_required_comparison_rejects_incomplete_or_foreign_source_coverage(
    tmp_path: Path, failure: str,
):
    registry, draft, manifest, _path = _case(tmp_path)
    unit = manifest["citation_sets"][0]["claim_units"][0]
    if failure == "b_only":
        unit["supporting_paper_keys"] = [_PAPER_KEYS[1]]
        unit["supporting_occurrence_ids"] = [f"occ-{_PAPER_KEYS[1]}"]
    elif failure == "split_units":
        other = deepcopy(unit)
        unit.update(
            supporting_paper_keys=[_PAPER_KEYS[0]],
            supporting_occurrence_ids=[f"occ-{_PAPER_KEYS[0]}"],
        )
        other.update(
            supporting_paper_keys=[_PAPER_KEYS[1]],
            supporting_occurrence_ids=[f"occ-{_PAPER_KEYS[1]}"],
        )
        manifest["citation_sets"][0]["claim_units"].append(other)
    elif failure == "pooled":
        unit["alignment_status"] = "ambiguous"
    elif failure == "foreign_occurrence":
        manifest["occurrences"][0]["block_id"] = "another-block"
    elif failure == "stale_basis":
        unit["writer_task_basis_hash"] = "b" * 64
    else:
        draft["content"]["sections"][0].pop("writer_task_scope")

    issues = required_review_comparison_issues(registry, draft, manifest)

    if failure == "unbound":
        assert issues == ["required_comparison_writer_scope_missing:paper-a|paper-b"]
    else:
        assert issues == ["required_comparison_source_coverage_missing:paper-a|paper-b"]


def test_same_pair_in_an_unrelated_writer_task_cannot_discharge_required_task(tmp_path: Path):
    registry, draft, manifest, _path = _case(tmp_path)
    required_section = draft["content"]["sections"][0]
    unrelated = deepcopy(required_section)
    unrelated["section_id"] = "unrelated-section"
    unrelated["section_title"] = "Unrelated same-pair passage"
    scope = unrelated["writer_task_scope"]
    scope["section_id"] = "unrelated-section"
    scope["writer_task_basis_hash"] = "b" * 64
    task = scope["tasks"][0]
    task["writer_task_id"] = "unrelated-writer-task"
    task["output_units"][0]["writer_output_unit_id"] = "unrelated-writer-unit"
    for disposition in unrelated["writer_task_dispositions"]:
        disposition["writer_task_id"] = "unrelated-writer-task"
        disposition["writer_task_basis_hash"] = "b" * 64
    unrelated_block = next(iter_review_text_blocks(unrelated))
    unrelated_block["block_id"] = "unrelated-ab-block"
    unrelated_block["writer_task_id"] = "unrelated-writer-task"
    unrelated_block["writer_output_unit_id"] = "unrelated-writer-unit"
    unrelated_block["writer_task_basis_hash"] = "b" * 64
    draft["content"]["sections"].append(unrelated)

    unrelated_occurrences = [
        {
            "occurrence_id": f"unrelated-occ-{paper_key}",
            "block_id": "unrelated-ab-block",
            "paper_key": paper_key,
            "canonical_paper_key": paper_key,
        }
        for paper_key in _PAPER_KEYS
    ]
    manifest["occurrences"] = unrelated_occurrences
    manifest["citation_sets"][0]["claim_units"] = [{
        "block_id": "unrelated-ab-block",
        "alignment_status": "explicit",
        "writer_task_id": "unrelated-writer-task",
        "writer_output_unit_id": "unrelated-writer-unit",
        "writer_task_basis_hash": "b" * 64,
        "supporting_paper_keys": list(_PAPER_KEYS),
        "supporting_occurrence_ids": [row["occurrence_id"] for row in unrelated_occurrences],
    }]

    assert required_review_comparison_issues(registry, draft, manifest) == [
        "required_comparison_source_coverage_missing:paper-a|paper-b"
    ]


@pytest.mark.parametrize("mismatch", ["question", "relation_type"])
def test_required_comparison_requires_exact_outline_question_and_relation_type(
    tmp_path: Path, mismatch: str,
):
    fixture = _build_case(
        tmp_path,
        packet_question="Different question" if mismatch == "question" else _QUESTION,
        outline_question="Different question" if mismatch == "question" else _QUESTION,
        relation_type="studies_same_construct" if mismatch == "relation_type" else _RELATION_TYPE,
    )
    registry, draft = fixture["registry"], fixture["draft"]
    section = draft["content"]["sections"][0]
    block = next(iter_review_text_blocks(section))
    manifest = {
        "occurrences": [],
        "citation_sets": [{"claim_units": []}],
    }
    for paper_key in _PAPER_KEYS:
        occurrence_id = f"occ-{paper_key}"
        manifest["occurrences"].append({
            "occurrence_id": occurrence_id,
            "block_id": block["block_id"],
            "canonical_paper_key": paper_key,
        })
    writer_task = section["writer_task_scope"]["tasks"][0]
    output_unit = writer_task["output_units"][0]
    manifest["citation_sets"][0]["claim_units"].append({
        "block_id": block["block_id"],
        "alignment_status": "explicit",
        "writer_task_id": writer_task["writer_task_id"],
        "writer_output_unit_id": output_unit["writer_output_unit_id"],
        "writer_task_basis_hash": section["writer_task_scope"]["writer_task_basis_hash"],
        "supporting_paper_keys": list(_PAPER_KEYS),
        "supporting_occurrence_ids": [f"occ-{paper_key}" for paper_key in _PAPER_KEYS],
    })

    assert required_review_comparison_issues(registry, draft, manifest) == [
        "required_comparison_task_binding_missing:paper-a|paper-b"
    ]


def test_required_comparison_never_reads_tampered_intent_as_authority(tmp_path: Path):
    registry, draft, manifest, path = _case(tmp_path)
    path.write_bytes(path.read_bytes() + b"\n")

    assert any(
        "untrusted" in issue
        for issue in required_review_comparison_issues(registry, draft, manifest)
    )


def test_no_intent_preserves_legacy_unrestricted_closure(tmp_path: Path):
    fixture = _build_case(
        tmp_path,
        with_intent=False,
        include_outline=False,
        include_adoption=False,
    )

    assert required_review_comparison_issues(
        fixture["registry"], fixture["draft"], {"occurrences": [], "citation_sets": []},
    ) == []
