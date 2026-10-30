from __future__ import annotations

import re
from copy import deepcopy
from typing import Any

import pytest

from services.citation_manifest import build_citation_manifest_from_review_draft
from services.citation_ref_catalog import (
    build_document_ref_catalog,
    extract_ref_ids_from_token,
)
from services.review_draft import _extract_citations_from_text
from services.writer_task_scope import (
    build_writer_task_scope_v1,
    validate_writer_task_output_v1,
)
from services.writer_wire_projection import (
    expand_writer_source_bundles_for_test_v1,
    project_writer_scope_for_provider_v1,
)
from tests.test_current_runtime_full_e2e import (
    _single_sentence_writer_claim,
    _source_bound_writer_response,
    _writer_source_statement_signature,
)
from tests.test_writer_source_inventory import (
    JOB_ID,
    PAPER_KEY,
    PRIMARY_TEXT,
    QUALIFIER_TEXT,
    SOURCE_SUMMARY,
    _registry_with_inventory,
)

SECONDARY_TEXT = "The second treatment lowered the measured outcome by 2 points in Study 1."
SECONDARY_QUALIFIER_TEXT = "The reduction applies only to the second recorded study condition."


def _summary(
    paper_key: str,
    title: str,
    authors: list[str],
    year: int,
    doi: str,
) -> dict[str, Any]:
    summary = deepcopy(SOURCE_SUMMARY)
    summary["paper_info"].update({
        "canonical_paper_key": paper_key,
        "source_paper_id": paper_key,
        "title": title,
        "authors": authors,
        "year": year,
        "doi": doi,
    })
    return summary


def _comparison_packet(inventory, catalog, papers: list[dict[str, Any]]) -> tuple[dict[str, Any], str]:
    bundle: list[tuple[Any, Any, Any, Any]] = []
    for summary in papers:
        paper_key = str(summary["paper_info"]["canonical_paper_key"])
        source = inventory.paper_by_key()[paper_key]
        unit = source.units[0]
        primary = next(
            claim for claim in source.claims
            if claim.claim_id in unit.claim_ids
            and claim.claim_type == "empirical_finding"
        )
        dependency = next(
            row for row in source.interpretation_dependencies
            if row.primary_claim_id == primary.claim_id
        )
        qualifier = next(
            claim for claim in source.claims
            if claim.claim_id in dependency.required_source_claim_ids
        )
        bundle.append((source, unit, primary, qualifier))

    entries_by_key = {
        str(row["canonical_paper_key"]): row
        for row in catalog["entries"]
        if row.get("status") == "active"
    }
    left_title = str(entries_by_key[PAPER_KEY]["title"])
    right_key = str(papers[1]["paper_info"]["canonical_paper_key"])
    right_title = str(entries_by_key[right_key]["title"])
    left_primary = next(row[2] for row in bundle if row[0].paper_key == PAPER_KEY)
    left_qualifier = next(row[3] for row in bundle if row[0].paper_key == PAPER_KEY)
    right_primary = next(row[2] for row in bundle if row[0].paper_key == right_key)
    right_qualifier = next(row[3] for row in bundle if row[0].paper_key == right_key)
    planned_claim = (
        f"Within the recorded source boundaries, {left_title} reports {left_primary.text}; "
        f"{left_qualifier.text}, whereas {right_title} reports {right_primary.text}; "
        f"{right_qualifier.text}"
    )

    support_rows = []
    evidence_items = []
    for source, unit, primary, qualifier in bundle:
        dependency = next(
            row for row in source.interpretation_dependencies
            if row.primary_claim_id == primary.claim_id
        )
        evidence_ids = sorted({*primary.evidence_ids, *dependency.required_evidence_ids})
        source_claim_ids = sorted({primary.claim_id, *dependency.required_source_claim_ids})
        source_field_ids = sorted(set(unit.source_field_ids))
        support_rows.append({
            "claim": planned_claim,
            "paper_key": source.paper_key,
            "study_id": unit.study_id,
            "primary_claim_id": primary.claim_id,
            "source_claim_ids": source_claim_ids,
            "required_source_claim_ids": list(dependency.required_source_claim_ids),
            "evidence_ids": evidence_ids,
            "required_evidence_ids": list(dependency.required_evidence_ids),
            "source_field_ids": source_field_ids,
            "required_source_field_ids": list(dependency.required_source_field_ids),
            "source_summary_hash": source.source_summary_hash,
        })
        evidence_items.append({
            "paper_key": source.paper_key,
            "summary_hash": source.source_summary_hash,
            "view_hash": source.evidence_view_hash,
            "fields": {"findings": [primary.text], "limitations": [qualifier.text]},
        })
    packet = {
        "section_id": "section:required-comparison",
        "section_goal": "Compare both recorded source findings while retaining each boundary.",
        "planned_claims": [planned_claim],
        "claim_support": support_rows,
        "paper_keys": sorted(row[0].paper_key for row in bundle),
        "relation_ids": [],
        "source_summary_hashes": sorted({row[0].source_summary_hash for row in bundle}),
        "evidence_view_hashes": sorted({row[0].evidence_view_hash for row in bundle}),
        "evidence_items": evidence_items,
    }
    return packet, planned_claim


def _manifest_for_writer_text(
    *,
    text: str,
    scope: dict[str, Any],
    content: dict[str, Any],
    catalog: dict[str, Any],
    summaries: list[dict[str, Any]],
):
    task = scope["tasks"][0]
    block_id = "s1_b1"
    block = {
        "block_id": block_id,
        "block_kind": "paragraph",
        "block_order": 1,
        "text": text,
        "citations": _extract_citations_from_text(
            text,
            block_id,
            citation_ref_catalog=catalog,
        ),
        "writer_task_id": task["writer_task_id"],
        "writer_output_unit_id": task["output_units"][0]["writer_output_unit_id"],
        "writer_task_basis_hash": scope["writer_task_basis_hash"],
    }
    section = {
        "section_number": 1,
        "section_title": "Required source comparison",
        "blocks": [block],
        "writer_task_scope": scope,
        "writer_task_dispositions": content["task_dispositions"],
    }
    result = validate_writer_task_output_v1(scope, {
        "blocks": [{key: block[key] for key in (
            "writer_task_id", "writer_output_unit_id", "writer_task_basis_hash", "text",
        )}],
        "task_dispositions": content["task_dispositions"],
    })
    assert result["scope_status"] == "ready"
    review_draft = {"content": {"sections": [section]}}
    return build_citation_manifest_from_review_draft(
        job_id=JOB_ID,
        project_name="source-local-citation-test",
        manifest_id="citation_manifest",
        review_draft_path="unused-review-draft.json",
        review_word_path="unused-review.docx",
        review_draft=review_draft,
        paper_summaries=summaries,
        citation_ref_catalog=catalog,
    )


def _claim_unit(manifest, paper_keys: set[str]) -> dict[str, Any]:
    bundle = next(
        row for row in manifest.citation_sets
        if set(row.paper_keys) == paper_keys
    )
    assert len(bundle.claim_units) == 1
    return bundle.claim_units[0]


def test_writer_attributes_titleless_single_source_bank_claim_without_rewording(tmp_path):
    papers = [_summary(PAPER_KEY, 'Recorded source', ['Author, A'], 2021, '10.5555/source-a')]
    _registry, inventory, packet, _layers, _evidence = _registry_with_inventory(
        tmp_path, stage1_summaries=papers, evidence_summaries=papers,
    )
    planned = f"{PRIMARY_TEXT.rstrip('.')}; {QUALIFIER_TEXT}"
    packet['planned_claims'] = [planned]
    packet['claim_support'][0]['claim'] = planned
    catalog = build_document_ref_catalog(papers, project_name='single-source', job_id=JOB_ID)
    scope = build_writer_task_scope_v1(packet, catalog, source_inventory=inventory)
    assert scope['scope_status'] == 'ready'
    wire = project_writer_scope_for_provider_v1(scope)
    before = deepcopy(wire)
    response = _source_bound_writer_response({
        'writer_task_scope': wire, 'citation_ref_catalog': catalog['entries'],
    }, [])
    assert response is not None and wire == before
    assert validate_writer_task_output_v1(scope, response['content'])['scope_status'] == 'ready'
    for block, task in zip(response['content']['blocks'], scope['tasks']):
        text = block['text']
        assert text.startswith('Recorded source reports ')
        assert _single_sentence_writer_claim(task['planned_claim']) in text
        assert len(re.findall(r'\[\[cite_ref:[^\]]+\]\]', text)) == 1
        assert PRIMARY_TEXT.rstrip('.') in text and QUALIFIER_TEXT.rstrip('.') in text


def test_writer_jointly_attributes_an_identical_complete_statement(tmp_path):
    papers = [
        _summary(PAPER_KEY, "Recorded source A", ["Author, A"], 2021, "10.5555/source-a"),
        _summary("paper-B", "Recorded source B", ["Author, B"], 2022, "10.5555/source-b"),
    ]
    _registry, inventory, _packet, _layer, _evidence = _registry_with_inventory(
        tmp_path, stage1_summaries=papers, evidence_summaries=papers,
    )
    catalog = build_document_ref_catalog(papers, project_name="shared-source", job_id=JOB_ID)
    packet, _ = _comparison_packet(inventory, catalog, papers)
    planned = f"{PRIMARY_TEXT.rstrip('.')}; {QUALIFIER_TEXT}"
    packet["planned_claims"] = [planned]
    for row in packet["claim_support"]:
        row["claim"] = planned
    scope = build_writer_task_scope_v1(packet, catalog, source_inventory=inventory)
    assert scope["scope_status"] == "ready"
    wire = project_writer_scope_for_provider_v1(scope)
    before = deepcopy(wire)
    response = _source_bound_writer_response({
        "writer_task_scope": wire, "citation_ref_catalog": catalog["entries"],
    }, [])
    assert response is not None and wire == before
    content = response["content"]
    text = content["blocks"][0]["text"]
    assert " each report " in text
    assert text.count(_single_sentence_writer_claim(planned)) == 1
    manifest = _manifest_for_writer_text(text=text, scope=scope, content=content, catalog=catalog, summaries=papers)
    unit = _claim_unit(manifest, {PAPER_KEY, "paper-B"})
    assert unit["alignment_status"] == "explicit"
    assert set(unit["supporting_paper_ids"]) == {PAPER_KEY, "paper-B"}


@pytest.mark.parametrize(
    "difference",
    ("evidence_text", "source_field_value", "dependency_scope", "duplicate_semantic_claim"),
)
def test_shared_writer_signature_requires_equal_complete_canonical_bundles(tmp_path, difference):
    papers = [
        _summary(PAPER_KEY, "Recorded source A", ["Author, A"], 2021, "10.5555/source-a"),
        _summary("paper-B", "Recorded source B", ["Author, B"], 2022, "10.5555/source-b"),
    ]
    _registry, inventory, _packet, _layer, _evidence = _registry_with_inventory(
        tmp_path, stage1_summaries=papers, evidence_summaries=papers,
    )
    catalog = build_document_ref_catalog(papers, project_name="shared-signature", job_id=JOB_ID)
    packet, _ = _comparison_packet(inventory, catalog, papers)
    planned = f"{PRIMARY_TEXT.rstrip('.')}; {QUALIFIER_TEXT}"
    packet["planned_claims"] = [planned]
    for row in packet["claim_support"]:
        row["claim"] = planned

    scope = build_writer_task_scope_v1(packet, catalog, source_inventory=inventory)
    assert scope["scope_status"] == "ready"
    wire = project_writer_scope_for_provider_v1(scope)
    bundles = expand_writer_source_bundles_for_test_v1(wire)[scope["tasks"][0]["writer_task_id"]]
    assert {row["paper_key"] for row in bundles} == {PAPER_KEY, "paper-B"}
    assert _writer_source_statement_signature(bundles, PAPER_KEY) == (
        _writer_source_statement_signature(bundles, "paper-B")
    )

    changed = deepcopy(bundles)
    right = next(row for row in changed if row["paper_key"] == "paper-B")
    if difference == "evidence_text":
        right["evidence"][0]["text"] += " A distinct source qualifier is recorded."
    elif difference == "source_field_value":
        limitation = next(
            row for row in right["source_fields"]
            if row.get("canonical_field") == "limitations"
        )
        limitation["source_value"] = "A different source-limited condition applies."
    elif difference == "dependency_scope":
        assert right["interpretation_dependencies"]
        right["interpretation_dependencies"][0]["scope"] = "paper"
    else:
        semantic_claim = deepcopy(right["source_claims"][0])
        semantic_claim["claim_id"] = f"{semantic_claim['claim_id']}:distinct-semantic-row"
        right["source_claims"].append(semantic_claim)

    assert _writer_source_statement_signature(changed, PAPER_KEY) != (
        _writer_source_statement_signature(changed, "paper-B")
    ), difference


def test_writer_rejects_joint_attribution_when_source_statements_differ(tmp_path):
    papers = [
        _summary(PAPER_KEY, "Recorded source A", ["Author, A"], 2021, "10.5555/source-a"),
        _summary("paper-B", "Recorded source B", ["Author, B"], 2022, "10.5555/source-b"),
    ]
    papers[1]["ai_summary"]["core_analysis"]["limitations"] = [SECONDARY_QUALIFIER_TEXT]
    papers[1]["ai_summary"]["specialized_details"]["empirical"]["studies"][0]["limitations"] = [SECONDARY_QUALIFIER_TEXT]
    _registry, inventory, _packet, _layer, _evidence = _registry_with_inventory(
        tmp_path, stage1_summaries=papers, evidence_summaries=papers,
    )
    catalog = build_document_ref_catalog(papers, project_name="different-source", job_id=JOB_ID)
    packet, _ = _comparison_packet(inventory, catalog, papers)
    planned = f"{PRIMARY_TEXT.rstrip('.')}; {QUALIFIER_TEXT}"
    packet["planned_claims"] = [planned]
    for row in packet["claim_support"]:
        row["claim"] = planned
    scope = build_writer_task_scope_v1(packet, catalog, source_inventory=inventory)
    assert scope["scope_status"] == "ready"
    wire = project_writer_scope_for_provider_v1(scope)
    bundles = expand_writer_source_bundles_for_test_v1(wire)[scope["tasks"][0]["writer_task_id"]]
    assert _writer_source_statement_signature(bundles, PAPER_KEY) != (
        _writer_source_statement_signature(bundles, "paper-B")
    )
    with pytest.raises(AssertionError, match="one source-specific clause"):
        _source_bound_writer_response({
            "writer_task_scope": wire,
            "citation_ref_catalog": catalog["entries"],
        }, [])


@pytest.mark.parametrize("left_title", [
    "Paper A",
    "The Price is Unfair! A Conceptual Framework of Price Fairness Perceptions",
    "Paper A? Recorded Source. Boundaries",
])
def test_writer_fixture_anchors_each_source_side_without_splitting_comparison(tmp_path, left_title):
    secondary = _summary("paper-B", "Paper B", ["Author, B"], 2022, "10.5555/source-b")
    secondary_core = secondary["ai_summary"]["core_analysis"]
    secondary_core["findings"] = [SECONDARY_TEXT]
    secondary_core["summary"] = SECONDARY_TEXT
    secondary_core["conclusions"] = [SECONDARY_TEXT]
    secondary_core["limitations"] = [SECONDARY_QUALIFIER_TEXT]
    secondary_empirical = secondary["ai_summary"]["specialized_details"]["empirical"]
    secondary_empirical["studies"][0]["findings"] = [SECONDARY_TEXT]
    secondary_empirical["studies"][0]["limitations"] = [SECONDARY_QUALIFIER_TEXT]
    papers = [
        _summary(PAPER_KEY, left_title, ["Author, A"], 2021, "10.5555/source-a"),
        secondary,
    ]
    _registry, inventory, _packet, _layer, _evidence = _registry_with_inventory(
        tmp_path,
        stage1_summaries=papers,
        evidence_summaries=papers,
    )
    catalog = build_document_ref_catalog(
        papers,
        project_name="source-local-citation-test",
        job_id=JOB_ID,
    )
    packet, planned_claim = _comparison_packet(inventory, catalog, papers)
    scope = build_writer_task_scope_v1(packet, catalog, source_inventory=inventory)
    assert scope["scope_status"] == "ready"
    assert len(scope["tasks"]) == 1
    task = scope["tasks"][0]
    primary_units = [unit for unit in task["output_units"] if unit.get("unit_kind") == "planned_claim"]
    assert len(primary_units) == 1 and primary_units[0]["required"] is True
    wire_scope = project_writer_scope_for_provider_v1(scope)
    envelope = {
        "writer_task_scope": wire_scope,
        "citation_ref_catalog": catalog["entries"],
        "section": {"section_id": packet["section_id"]},
    }
    trace: list[dict[str, Any]] = []
    response = _source_bound_writer_response(envelope, trace)
    assert response is not None and response["status"] == "success"
    content = response["content"]
    assert len(content["blocks"]) == 1, "the two source sides must remain one comparison output unit"
    block = content["blocks"][0]
    assert block["writer_task_id"] == task["writer_task_id"]
    assert block["writer_output_unit_id"] == primary_units[0]["writer_output_unit_id"]
    assert block["writer_task_basis_hash"] == scope["writer_task_basis_hash"]
    emitted = str(block["text"])
    def lexical(value: str) -> list[str]:
        return re.findall(r"[A-Za-z0-9]+|[\u3400-\u9fff]+", value.casefold())

    text_without_refs = re.sub(r"\s*\[\[cite_ref:[^\]]+\]\]", "", emitted).rstrip(" .!?;")
    assert lexical(text_without_refs) == lexical(planned_claim)
    assert PRIMARY_TEXT.rstrip(".") in emitted
    assert QUALIFIER_TEXT.rstrip(".") in emitted
    assert SECONDARY_TEXT.rstrip(".") in emitted
    assert SECONDARY_QUALIFIER_TEXT.rstrip(".") in emitted
    assert emitted.count(_single_sentence_writer_claim(left_title)) == 1 and emitted.count("Paper B") == 1

    entries_by_paper = {
        str(row["canonical_paper_key"]): row for row in catalog["entries"]
        if row.get("status") == "active"
    }
    paper_a_token = f"[[cite_ref:{entries_by_paper[PAPER_KEY]['ref_id']}]]"
    paper_b_token = f"[[cite_ref:{entries_by_paper['paper-B']['ref_id']}]]"
    transition_offset = emitted.casefold().index("whereas")
    assert emitted.index(paper_a_token) < transition_offset < emitted.index("Paper B")
    assert emitted.index(paper_b_token) > emitted.index("Paper B")
    expected_refs = {
        entries_by_paper[PAPER_KEY]["ref_id"],
        entries_by_paper["paper-B"]["ref_id"],
    }
    emitted_refs = [
        ref_id
        for token in re.findall(r"\[\[cite_ref:[^\]]+\]\]", emitted)
        for ref_id in extract_ref_ids_from_token(token)
    ]
    assert len(emitted_refs) == 2 and set(emitted_refs) == expected_refs
    assert len(set(emitted_refs)) == 2

    positive_manifest = _manifest_for_writer_text(
        text=emitted,
        scope=scope,
        content=content,
        catalog=catalog,
        summaries=papers,
    )
    positive_unit = _claim_unit(positive_manifest, {PAPER_KEY, "paper-B"})
    assert positive_unit["alignment_status"] == "explicit"
    assert set(positive_unit["supporting_paper_ids"]) == {PAPER_KEY, "paper-B"}
    assert set(positive_unit["citation_tokens"]) == {
        paper_a_token,
        paper_b_token,
    }

    old_end_joint_text = (
        f"{_single_sentence_writer_claim(planned_claim)} "
        f"[[cite_ref:{', '.join(sorted(expected_refs))}]]."
    )
    negative_manifest = _manifest_for_writer_text(
        text=old_end_joint_text,
        scope=scope,
        content=content,
        catalog=catalog,
        summaries=papers,
    )
    negative_unit = _claim_unit(negative_manifest, {PAPER_KEY, "paper-B"})
    assert negative_unit["alignment_status"] == "ambiguous"
    assert negative_unit["supporting_paper_ids"] == []

    tampered_content = deepcopy(content)
    tampered_content["blocks"][0]["text"] = emitted.replace(
        f"[[cite_ref:{emitted_refs[0]}]]",
        "[[cite_ref:R999]]",
        1,
    )
    with pytest.raises(Exception, match="cites foreign refs"):
        validate_writer_task_output_v1(scope, tampered_content)



def test_writer_fixture_rejects_repeated_source_title_after_sentence_transformation(tmp_path):
    title = "The Price is Unfair! A Conceptual Framework of Price Fairness Perceptions"
    papers = [
        _summary(PAPER_KEY, title, ["Author, A"], 2021, "10.5555/source-a"),
        _summary("paper-B", title, ["Author, B"], 2022, "10.5555/source-b"),
    ]
    _registry, inventory, _packet, _layer, _views = _registry_with_inventory(
        tmp_path, stage1_summaries=papers, evidence_summaries=papers,
    )
    catalog = build_document_ref_catalog(papers, project_name="duplicate-title", job_id=JOB_ID)
    packet, _claim = _comparison_packet(inventory, catalog, papers)
    scope = build_writer_task_scope_v1(packet, catalog, source_inventory=inventory)
    envelope = {"writer_task_scope": project_writer_scope_for_provider_v1(scope),
                "citation_ref_catalog": catalog["entries"]}
    with pytest.raises(AssertionError, match="one source-specific clause"):
        _source_bound_writer_response(envelope, [])
