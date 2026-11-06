"""Canonical source labels stay bound to their claims across opaque aliases."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

import pytest

from outline.candidate_output_scope import build_candidate_output_scope_v1
from outline.candidate_relation_fragments import (
    build_candidate_comparison_fragments_request_v1,
)
from outline.evidence_alias import build_alias_map
from services.citation_ref_catalog import build_document_ref_catalog
from services.writer_task_scope import (
    build_writer_task_scope_v1,
    validate_writer_task_output_v1,
)
from services.writer_wire_projection import project_writer_scope_for_provider_v1
from tests.run_first_candidate_fragments_fixture import (
    candidate_fragment_fixture_response,
)
from tests.test_current_runtime_full_e2e import _source_bound_writer_response
from tests.test_writer_fixture_source_local_citations import (
    SECONDARY_QUALIFIER_TEXT,
    SECONDARY_TEXT,
    _summary,
)
from tests.test_writer_source_inventory import (
    JOB_ID,
    PRIMARY_TEXT,
    QUALIFIER_TEXT,
    _registry_with_inventory,
)

_PAPER_A = "10.5555/rfs-0000"
_PAPER_B = "10.5555/rfs-0001"
_TITLE_A = "RFS-PAPER-0000: 促销机制与结果边界"
_TITLE_B = "Threshold discounts and measured choice"


def _fixture_case(
    tmp_path,
    *,
    paper_keys: tuple[str, str] = (_PAPER_A, _PAPER_B),
    alias_order: tuple[str, str] | None = (_PAPER_B, _PAPER_A),
    reorder_source_rows: bool = True,
) -> dict[str, Any]:
    paper_a, paper_b = paper_keys
    summaries = [
        _summary(paper_a, _TITLE_A, ["Author, A"], 2021, "10.5555/rfs-0000"),
        _summary(paper_b, _TITLE_B, ["Author, B"], 2022, "10.5555/rfs-0001"),
    ]
    secondary = summaries[1]["ai_summary"]
    secondary["core_analysis"]["findings"] = [SECONDARY_TEXT]
    secondary["core_analysis"]["summary"] = SECONDARY_TEXT
    secondary["core_analysis"]["conclusions"] = [SECONDARY_TEXT]
    secondary["core_analysis"]["limitations"] = [SECONDARY_QUALIFIER_TEXT]
    secondary_empirical = secondary["specialized_details"]["empirical"]
    secondary_empirical["studies"][0]["findings"] = [SECONDARY_TEXT]
    secondary_empirical["studies"][0]["limitations"] = [SECONDARY_QUALIFIER_TEXT]

    _registry, inventory, _packet, _layers, evidence_views = _registry_with_inventory(
        tmp_path,
        stage1_summaries=summaries,
        evidence_summaries=summaries,
    )
    papers_by_key = inventory.paper_by_key()
    primary_by_paper: dict[str, Any] = {}
    dependency_by_paper: dict[str, Any] = {}
    required_evidence: set[str] = set()
    claim_ids_by_paper: dict[str, list[str]] = {}
    for paper_key in paper_keys:
        source = papers_by_key[paper_key]
        unit = source.units[0]
        primary = next(
            claim for claim in source.claims
            if claim.claim_id in unit.claim_ids and claim.claim_type == "empirical_finding"
        )
        dependency = next(
            row for row in source.interpretation_dependencies
            if row.primary_claim_id == primary.claim_id
        )
        primary_by_paper[paper_key] = primary
        dependency_by_paper[paper_key] = dependency
        claim_ids_by_paper[paper_key] = [primary.claim_id, *dependency.required_source_claim_ids]
        required_evidence.update((*primary.evidence_ids, *dependency.required_evidence_ids))

    relation_id = "relation:fixture:doi-comparison"
    relation_bundle = {
        "relation_id": relation_id,
        "relation_type": "qualifies",
        "comparison_question": "Compare both findings while retaining their recorded conditions.",
        "paper_ids": list(paper_keys),
        "claim_ids_left": claim_ids_by_paper[paper_a],
        "claim_ids_right": claim_ids_by_paper[paper_b],
        "findings_left": [primary_by_paper[paper_a].text],
        "findings_right": [primary_by_paper[paper_b].text],
        "required_evidence_ids": sorted(required_evidence),
        "provided_evidence_ids": sorted(required_evidence),
        "missing_evidence_ids": [],
        "evidence_completeness": "complete",
        "decision": "confirmed",
    }
    scope = build_candidate_output_scope_v1(
        inventory,
        [],
        selected_relation_ids=[relation_id],
        relation_bundles=[relation_bundle],
    )
    source_tables = {
        "source_fields": [asdict(row) for paper in inventory.papers for row in paper.source_fields],
        "dependencies": [asdict(row) for paper in inventory.papers for row in paper.interpretation_dependencies],
        "source_claims": [asdict(row) for paper in inventory.papers for row in paper.claims],
    }
    alias_map = build_alias_map(alias_order, scope.selected_relation_ids) if alias_order else None
    group_ids = [group.claim_group_id for group in scope.claim_groups]
    request = build_candidate_comparison_fragments_request_v1(
        scope,
        group_ids,
        {"interpretation_source_tables": source_tables, "topic_routes": []},
        required_group_ids=group_ids,
        alias_map=alias_map,
    )
    if reorder_source_rows:
        request["shared_semantic_context"]["interpretation_source_tables"]["source_claims"].reverse()
        request["shared_semantic_context"]["interpretation_source_tables"]["source_fields"].reverse()

    catalog = build_document_ref_catalog(
        summaries,
        project_name="fixture-source-labels",
        job_id=JOB_ID,
    )
    return {
        "summaries": summaries,
        "inventory": inventory,
        "evidence_views": evidence_views,
        "scope": scope,
        "request": request,
        "catalog": catalog,
        "paper_keys": paper_keys,
        "labels": {paper_a: _TITLE_A, paper_b: _TITLE_B},
        "primary_by_paper": primary_by_paper,
        "dependency_by_paper": dependency_by_paper,
    }


def _writer_packet(case: dict[str, Any], planned_claim: str) -> dict[str, Any]:
    inventory = case["inventory"]
    claim_support = []
    evidence_items = []
    for paper_key in case["paper_keys"]:
        source = inventory.paper_by_key()[paper_key]
        unit = source.units[0]
        primary = case["primary_by_paper"][paper_key]
        dependency = case["dependency_by_paper"][paper_key]
        claim_support.append({
            "claim": planned_claim,
            "paper_key": paper_key,
            "study_id": unit.study_id,
            "primary_claim_id": primary.claim_id,
            "source_claim_ids": sorted({primary.claim_id, *dependency.required_source_claim_ids}),
            "required_source_claim_ids": list(dependency.required_source_claim_ids),
            "evidence_ids": sorted({*primary.evidence_ids, *dependency.required_evidence_ids}),
            "required_evidence_ids": list(dependency.required_evidence_ids),
            "source_field_ids": list(unit.source_field_ids),
            "required_source_field_ids": list(dependency.required_source_field_ids),
            "source_summary_hash": source.source_summary_hash,
        })
        evidence_items.append({
            "paper_key": paper_key,
            "summary_hash": source.source_summary_hash,
            "view_hash": source.evidence_view_hash,
            "fields": {
                "findings": [case["primary_by_paper"][paper_key].text],
                "limitations": [
                    next(
                        claim.text for claim in source.claims
                        if claim.claim_id in case["dependency_by_paper"][paper_key].required_source_claim_ids
                    )
                ],
            },
        })
    return {
        "section_id": "section:required-comparison",
        "section_goal": "Compare both recorded source findings while retaining each boundary.",
        "planned_claims": [planned_claim],
        "claim_support": claim_support,
        "paper_keys": sorted(case["paper_keys"]),
        "relation_ids": [],
        "source_summary_hashes": sorted({
            inventory.paper_by_key()[key].source_summary_hash for key in case["paper_keys"]
        }),
        "evidence_view_hashes": sorted({
            inventory.paper_by_key()[key].evidence_view_hash for key in case["paper_keys"]
        }),
        "evidence_items": evidence_items,
    }


def _candidate_text(case: dict[str, Any], source_label_map: dict[str, str] | None) -> str:
    response = candidate_fragment_fixture_response(
        "candidate-provider-generation:fixture",
        case["request"],
        source_label_map=source_label_map,
    )
    assert response is not None
    assert len(response["fragments"]) == 1
    return str(response["fragments"][0]["text"])


def test_doi_comparison_labels_follow_claim_owners_through_alias_permutation_and_writer(tmp_path):
    case = _fixture_case(tmp_path)
    candidate_text = _candidate_text(case, case["labels"])

    # Reversed alias assignment and reversed source rows must not transfer a
    # paper title to the other source's finding or qualifier.
    comparison = candidate_text.casefold()
    assert comparison.index(_TITLE_B.casefold()) < comparison.index("whereas")
    assert comparison.index(SECONDARY_TEXT.rstrip(".").casefold()) < comparison.index("whereas")
    assert comparison.index(SECONDARY_QUALIFIER_TEXT.rstrip(".").casefold()) < comparison.index("whereas")
    assert comparison.index(_TITLE_A.casefold()) > comparison.index("whereas")
    assert comparison.index(PRIMARY_TEXT.rstrip(".").casefold()) > comparison.index("whereas")
    assert comparison.index(QUALIFIER_TEXT.rstrip(".").casefold()) > comparison.index("whereas")

    packet = _writer_packet(case, candidate_text)
    scope = build_writer_task_scope_v1(packet, case["catalog"], source_inventory=case["inventory"])
    assert scope["scope_status"] == "ready"
    envelope = {
        "writer_task_scope": project_writer_scope_for_provider_v1(scope),
        "citation_ref_catalog": case["catalog"]["entries"],
        "section": {"section_id": packet["section_id"]},
    }
    writer = _source_bound_writer_response(envelope, [])
    assert writer is not None
    content = writer["content"]
    assert validate_writer_task_output_v1(scope, content)["scope_status"] == "ready"
    emitted = content["blocks"][0]["text"]
    transition = emitted.casefold().index("whereas")
    entries = {
        row["canonical_paper_key"]: row
        for row in case["catalog"]["entries"]
        if row.get("status") == "active"
    }
    for paper_key, title, finding, qualifier in (
        (_PAPER_A, _TITLE_A, PRIMARY_TEXT, QUALIFIER_TEXT),
        (_PAPER_B, _TITLE_B, SECONDARY_TEXT, SECONDARY_QUALIFIER_TEXT),
    ):
        start = emitted.casefold().index(title.casefold())
        stop = emitted.casefold().index("whereas") if start < transition else len(emitted)
        start = min(start, transition)
        clause = emitted[start:stop]
        ref = f"[[cite_ref:{entries[paper_key]['ref_id']}]]"
        assert title in clause
        assert finding.rstrip(".") in clause
        assert qualifier.rstrip(".") in clause
        assert ref in clause


def test_source_label_map_rejects_missing_or_empty_labels(tmp_path):
    case = _fixture_case(tmp_path)
    with pytest.raises(ValueError, match="source label"):
        _candidate_text(case, {_PAPER_A: _TITLE_A})
    with pytest.raises(ValueError, match="source label"):
        _candidate_text(case, {_PAPER_A: "  ", _PAPER_B: _TITLE_B})


def test_source_label_map_rejects_ambiguous_wire_ownership(tmp_path):
    case = _fixture_case(tmp_path / "ambiguous")
    tables = case["request"]["shared_semantic_context"]["interpretation_source_tables"]
    claim = tables["source_claims"][0]
    other_wire_paper = next(
        slot["paper_key"] for slot in case["request"]["comparison_scope"]["claim_slots"]
        if slot["paper_key"] != claim["paper_key"]
    )
    tables["source_claims"].append({**claim, "paper_key": other_wire_paper})
    with pytest.raises(ValueError, match="ambiguous|conflicting|ownership"):
        _candidate_text(case, case["labels"])


def test_source_label_map_rejects_one_canonical_owner_under_two_wire_aliases(tmp_path):
    case = _fixture_case(tmp_path / "reused-owner")
    request = case["request"]
    slots = request["comparison_scope"]["claim_slots"]
    wire_keys = sorted({slot["paper_key"] for slot in slots})
    source_rows = request["shared_semantic_context"]["interpretation_source_tables"]["source_claims"]
    paper_a_wire = next(
        slot["paper_key"] for slot in slots
        if any(row["claim_id"] == slot["primary_claim_id"]
               and row["paper_key"] == slot["paper_key"]
               and row["claim_id"].startswith(f"claim:{_PAPER_A}:") for row in source_rows)
    )
    paper_a_ids = [
        row["claim_id"] for row in source_rows
        if row["paper_key"] == paper_a_wire and row["claim_id"].startswith(f"claim:{_PAPER_A}:")
    ]
    assert len(paper_a_ids) >= 2
    paper_b_wire = next(key for key in wire_keys if key != paper_a_wire)
    first_slot = next(slot for slot in slots if slot["paper_key"] == paper_a_wire)
    second_slot = next(slot for slot in slots if slot["paper_key"] == paper_b_wire)
    first_slot["primary_claim_id"] = paper_a_ids[0]
    first_slot["source_claim_ids"] = [paper_a_ids[0]]
    second_slot["primary_claim_id"] = paper_a_ids[1]
    second_slot["source_claim_ids"] = [paper_a_ids[1]]
    request["shared_semantic_context"]["interpretation_source_tables"]["source_claims"] = [
        {**row, "paper_key": paper_a_wire if row["claim_id"] == paper_a_ids[0] else paper_b_wire}
        for row in source_rows if row["claim_id"] in set(paper_a_ids[:2])
    ]
    with pytest.raises(ValueError, match="multiple wire papers"):
        _candidate_text(case, case["labels"])


def test_source_label_map_rejects_conflicting_titles_for_selected_sources(tmp_path):
    case = _fixture_case(tmp_path)
    with pytest.raises(ValueError, match="conflicting label"):
        _candidate_text(case, {_PAPER_A: _TITLE_A, _PAPER_B: _TITLE_A})


def test_unmapped_direct_fixture_keeps_rfs_filename_stem_labels(tmp_path):
    keys = ("rfspaper0000_unknown_author", "rfspaper0001_unknown_author")
    case = _fixture_case(
        tmp_path,
        paper_keys=keys,
        alias_order=None,
        reorder_source_rows=False,
    )
    text = _candidate_text(case, None)
    assert "rfs-paper-0000" in text
    assert "rfs-paper-0001" in text
    assert _TITLE_A not in text and _TITLE_B not in text
