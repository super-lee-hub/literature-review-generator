from __future__ import annotations

import re
from typing import Any
from types import SimpleNamespace

import pytest

from tests.scientific_support_oracle import (
    audit_all_canonical_writer_claim_units,
    audit_all_selected_candidate_support,
)
from tests.test_scientific_support_oracle import _corpus, _good_sides


def _candidate_support(claim: str, paper_key: str, index: int) -> dict[str, Any]:
    return {
        "claim": claim,
        "claim_slot_id": f"slot:{index}:{paper_key}",
        "task_id": f"task:{index}",
        "paper_key": paper_key,
        "primary_claim_id": f"claim:{index}:{paper_key}",
        "source_claim_ids": [f"claim:{index}:{paper_key}", f"qualifier:{index}:{paper_key}"],
        "evidence_ids": [f"evidence:{index}:{paper_key}", f"qualifier-evidence:{index}:{paper_key}"],
        "source_field_ids": [f"field:{index}:{paper_key}"],
    }


def _candidate_section(section_id: str, claim: str, paper_keys: tuple[str, ...], index: int) -> dict[str, Any]:
    return {
        "section_id": section_id,
        "claims": [claim],
        "claim_support": [
            _candidate_support(claim, paper_key, index)
            for paper_key in paper_keys
        ],
    }


def _pair_text(*, changed_condition: bool = False, missing_first_title: bool = False) -> tuple[str, str]:
    side_a, side_b = _good_sides()
    if changed_condition:
        side_a = side_a.replace("under the fixed-value coupon", "under an offer")
    if missing_first_title:
        side_a = side_a.replace("Coupon Study A", "The first source")
    return side_a, side_b


def _paired_writer_manifest(
    *,
    changed_condition: bool = False,
    missing_first_title: bool = False,
    joint_end_citation: bool = False,
    unmapped_unit: bool = False,
    foreign_occurrence: bool = False,
    stale_bundle: bool = False,
    foreign_cluster: bool = False,
) -> tuple[dict[str, Any], str]:
    side_a, side_b = _pair_text(
        changed_condition=changed_condition,
        missing_first_title=missing_first_title,
    )
    if joint_end_citation:
        joint_token = "[[cite_ref:R001, R002]]"
        raw_text = (
            f"Within each recorded sample boundary, {side_a}; whereas {side_b} "
            f"{joint_token}."
        )
        citation_tokens = [joint_token]
        token_by_paper = {"paper-a": joint_token, "paper-b": joint_token}
    else:
        token_a = "[[cite_ref:R001]]"
        token_b = "[[cite_ref:R002]]"
        raw_text = (
            f"Within each recorded sample boundary, {side_a} {token_a}; "
            f"whereas {side_b} {token_b}."
        )
        citation_tokens = [token_a, token_b]
        token_by_paper = {"paper-a": token_a, "paper-b": token_b}
    claim_text = re.sub(r"\s+", " ", re.sub(r"\[\[cite_ref:[^\]]+\]\]", "", raw_text)).strip()
    occurrences: list[dict[str, Any]] = []
    for occurrence_id, paper_key, ref_id in (
        ("occ-a", "paper-a", "R001"),
        ("occ-b", "paper-b", "R002"),
    ):
        token = token_by_paper[paper_key]
        start = raw_text.index(token)
        occurrences.append({
            "occurrence_id": occurrence_id,
            "paper_id": paper_key,
            "paper_key": paper_key,
            "canonical_paper_key": paper_key,
            "block_id": "block-pair",
            "citation_token": token,
            "ref_id": ref_id,
            "spans": [{
                "span_id": f"span-{occurrence_id}",
                "start_offset": start,
                "end_offset": start + len(token),
                "text": token,
            }],
        })
    support_occurrence_ids = [] if unmapped_unit else ["occ-a", "occ-b"]
    support_paper_keys = [] if unmapped_unit else ["paper-a", "paper-b"]
    if foreign_occurrence:
        support_occurrence_ids.append("occ-foreign")
    unit = {
        "claim_unit_id": "unit-pair",
        "citation_set_key": "paper-a+paper-b",
        "block_id": "block-pair",
        "sentence_index": 0,
        "span_start": 0,
        "span_end": len(raw_text),
        "raw_text": raw_text,
        "display_text": claim_text,
        "claim_text": claim_text,
        "citation_tokens": citation_tokens,
        "block_anchor_hash": "a" * 8,
        "supporting_paper_ids": list(support_paper_keys),
        "supporting_paper_keys": list(support_paper_keys),
        "supporting_occurrence_ids": support_occurrence_ids,
        "alignment_status": "ambiguous" if unmapped_unit else "explicit",
        "alignment_confidence": 0.35 if unmapped_unit else 0.92,
    }
    manifest = {
        "occurrences": occurrences,
        "clusters": [
            {"cluster_id": "cluster-a", "paper_id": "paper-a", "paper_key": "paper-a", "occurrence_ids": ["occ-a"]},
            {"cluster_id": "cluster-b", "paper_id": "paper-b", "paper_key": "paper-b", "occurrence_ids": ["occ-b"]},
        ],
        "citation_sets": [{
            "bundle_id": "bundle-pair",
            "paper_keys": ["paper-a", "paper-b"],
            "occurrence_ids": ["occ-a", "occ-b"],
            "claim_units": [unit],
            "claim_texts": [claim_text],
        }],
    }
    if stale_bundle:
        manifest["citation_sets"][0]["occurrence_ids"] = ["occ-a"]
    if foreign_cluster:
        manifest["clusters"][0]["paper_key"] = "paper-foreign"
    return manifest, claim_text


def test_all_selected_candidate_audit_counts_sections_and_claim_occurrences() -> None:
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as temp_dir:
        corpus = _corpus(Path(temp_dir))
        side_a, side_b = _good_sides()
        good_pair = f"Within each recorded sample boundary, {side_a}; whereas {side_b}."
        changed_condition = _pair_text(changed_condition=True)
        changed_pair = f"Within each recorded sample boundary, {changed_condition[0]}; whereas {changed_condition[1]}."
        missing_anchor = _pair_text(missing_first_title=True)
        missing_anchor_pair = f"Within each recorded sample boundary, {missing_anchor[0]}; whereas {missing_anchor[1]}."
        sections = [
            _candidate_section("section-a", good_pair, ("paper-a", "paper-b"), 1),
            _candidate_section("section-b", good_pair, ("paper-a", "paper-b"), 2),
            _candidate_section("section-c", changed_pair, ("paper-a", "paper-b"), 3),
            _candidate_section("section-d", missing_anchor_pair, ("paper-a", "paper-b"), 4),
        ]
        audit = audit_all_selected_candidate_support(
            corpus,
            sections,
            required_comparisons=[{
                "paper_keys": ["paper-a", "paper-b"],
                "question": "Compare the source-reported findings within their recorded boundaries.",
            }],
        )

    assert len(audit["assertions"]) == 4
    assert [row["judgment"] for row in audit["assertions"]] == [
        "supported", "supported", "unsupported", "unjudgable",
    ]
    assert audit["assertion_counts"] == {"supported": 2, "unsupported": 1, "unjudgable": 1}
    assert audit["assertions"][0]["section_id"] != audit["assertions"][1]["section_id"]
    assert audit["assertions"][0]["claim_text_sha256"] == audit["assertions"][1]["claim_text_sha256"]
    assert all(row["support_row_count"] == 2 for row in audit["assertions"])
    assert audit["required_comparisons"][0]["status"] == "supported"
    assert len(audit["required_comparisons"][0]["supported_assertion_ids"]) == 2
    assert "unjudgable_source_title_anchor" in audit["assertions"][3]["issue_codes"]


def test_arbitrary_nonempty_single_source_identity_is_not_a_ready_binding(tmp_path) -> None:
    corpus = _corpus(tmp_path)
    side_a, _side_b = _good_sides()
    claim = side_a.replace('Coupon Study A reports ', '')
    section = _candidate_section('titleless-source', claim, ('paper-a',), 0)
    unverified = SimpleNamespace(is_verified=True, matches=lambda *_args: 'paper-a')
    with pytest.raises(ValueError, match='verified READY Registry binding'):
        audit_all_selected_candidate_support(corpus, [section], single_source_bindings=unverified)


def test_writer_required_pair_uses_resolved_claim_unit_support_not_bundle_union(tmp_path) -> None:
    corpus = _corpus(tmp_path)
    manifest, _claim_text = _paired_writer_manifest()
    audit = audit_all_canonical_writer_claim_units(
        corpus,
        manifest,
        required_comparisons=[{
            "paper_keys": ["paper-a", "paper-b"],
            "question": "Compare the source-reported findings within their recorded boundaries.",
        }],
    )
    unit = audit["claim_units"][0]
    assert unit["judgment"] == "supported", unit
    assert unit["paper_keys"] == ["paper-a", "paper-b"]
    assert unit["supporting_occurrence_ids"] == ["occ-a", "occ-b"]
    assert audit["required_comparisons"][0]["status"] == "supported"
    assert audit["required_comparisons"][0]["supported_assertion_ids"] == ["unit-pair"]


@pytest.mark.parametrize(
    ("mutation", "expected_judgment", "expected_issue"),
    [
        ("changed_condition", "unsupported", "condition_omitted_or_mismatched"),
        ("missing_anchor", "unjudgable", "unjudgable_source_title_anchor"),
        ("joint_end_citation", "unjudgable", "joint_multisource_citation_token"),
        ("unmapped_unit", "unjudgable", "writer_unit_supporting_paper_keys_missing"),
        ("foreign_occurrence", "unjudgable", "writer_unit_occurrence_id_unresolved"),
        ("stale_bundle", "unjudgable", "writer_unit_occurrence_not_in_citation_set"),
        ("foreign_cluster", "unjudgable", "writer_unit_cluster_source_mismatch"),
    ],
)
def test_canonical_writer_audit_requires_unit_level_source_and_citation_mapping(
    tmp_path,
    mutation: str,
    expected_judgment: str,
    expected_issue: str,
) -> None:
    corpus = _corpus(tmp_path)
    manifest, _claim_text = _paired_writer_manifest(
        changed_condition=mutation == "changed_condition",
        missing_first_title=mutation == "missing_anchor",
        joint_end_citation=mutation == "joint_end_citation",
        unmapped_unit=mutation == "unmapped_unit",
        foreign_occurrence=mutation == "foreign_occurrence",
        stale_bundle=mutation == "stale_bundle",
        foreign_cluster=mutation == "foreign_cluster",
    )
    audit = audit_all_canonical_writer_claim_units(
        corpus,
        manifest,
        required_comparisons=[{
            "paper_keys": ["paper-a", "paper-b"],
            "question": "Compare the source-reported findings within their recorded boundaries.",
        }],
    )
    row = audit["claim_units"][0]
    assert row["judgment"] == expected_judgment, row
    assert expected_issue in row["issue_codes"], row
    assert audit["required_comparisons"][0]["status"] != "supported"


def test_writer_audit_counts_unparseable_table_units_as_unjudgable(tmp_path) -> None:
    corpus = _corpus(tmp_path)
    good_manifest, _text = _paired_writer_manifest()
    unit = dict(good_manifest["citation_sets"][0]["claim_units"][0])
    token = "[[cite_ref:R001]]"
    raw_text = f"Coupon Study A reports source table cell 4 {token}."
    span_start = raw_text.index(token)
    unit.update({
        "claim_unit_id": "unit-table",
        "table_id": "table-1",
        "row_id": "row-1",
        "cell_id": "cell-1",
        "claim_text": "Coupon Study A reports source table cell 4.",
        "raw_text": raw_text,
        "display_text": "Coupon Study A reports source table cell 4.",
        "span_end": len(raw_text),
        "citation_tokens": [token],
        "supporting_paper_ids": ["paper-a"],
        "supporting_paper_keys": ["paper-a"],
        "supporting_occurrence_ids": ["occ-a"],
    })
    occurrence = good_manifest["occurrences"][0]
    occurrence["citation_token"] = token
    occurrence["ref_id"] = "R001"
    occurrence["spans"] = [{
        "span_id": "span-occ-a-table",
        "start_offset": span_start,
        "end_offset": span_start + len(token),
        "text": token,
    }]
    good_manifest["citation_sets"][0]["claim_units"].append(unit)
    audit = audit_all_canonical_writer_claim_units(corpus, good_manifest)
    assert len(audit["claim_units"]) == 2
    table_row = next(row for row in audit["claim_units"] if row["claim_unit_id"] == "unit-table")
    assert table_row["judgment"] == "unjudgable"
    assert table_row["issue_codes"] == ["writer_table_unit_outside_paragraph_source_oracle"]
