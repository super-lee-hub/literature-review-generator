from __future__ import annotations

from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from outline.candidate_output_scope import build_candidate_output_scope_v1
from services.artifact_registry import ArtifactRegistry
from services.job_workspace import JobWorkspace
from services.writer_source_inventory import load_writer_source_inventory_v1
from services.writer_task_scope import (
    build_writer_task_scope_v1,
    validate_writer_task_output_v1,
)
from services.writer_wire_projection import project_writer_scope_for_provider_v1
from tests.scientific_support_oracle import ControlledPdfCorpus, evaluate_assertion
from tests.test_run_first_scale_integration import (
    _compact_candidate,
    _reader_content,
    _scale_papers,
    _source_bound_writer_response,
    _write_scale_pdf,
)
from tests.writer_source_fixture import bind_production_writer_sources

_EXPECTED_FACTS: dict[int, tuple[str, int | None, int, int]] = {
    0: ("increase", 4, 24, 30),
    1: ("increase", 2, 24, 30),
    2: ("null", None, 24, 30),
    3: ("decrease", 2, 24, 30),
    4: ("increase", 1, 24, 30),
    5: ("increase", 3, 24, 30),
    6: ("decrease", 1, 24, 30),
    7: ("null", None, 24, 30),
    8: ("increase", 5, 24, 30),
    9: ("increase", 2, 24, 30),
}

_EXPECTED_FACT_CONDITIONS = {
    0: "fixed-value coupon",
    1: "reference-price label and no-promotion shelf label",
    2: "loyalty-points reminder followed purchase; comparison shoppers saw no reminder",
    3: "percentage-off shelf label was compared with the regular-price label",
    4: "member-only threshold offer was compared with the standard displayed price",
    5: "minimum-spend free-shipping offer was compared with standard delivery fees",
    6: "two-item multi-buy offer was compared with its regular single-item price",
    7: "aisle-end display was compared with standard shelf placement",
    8: "cashback rebate was compared with the regular price without a rebate",
    9: "two-item bundle price was compared with separate item prices",
}


def _write_corpus(tmp_path: Path, papers: list[Any]) -> dict[str, Path]:
    paths: dict[str, Path] = {}
    for paper in papers:
        path = tmp_path / f"{paper.stem}.pdf"
        _write_scale_pdf(path, paper)
        paths[paper.paper_key] = path
    return paths


def _gold_candidate_claim(paper: Any) -> str:
    return " ".join(
        value.strip()
        for value in (
            paper.title,
            paper.finding,
            paper.condition_description,
            paper.sample_description,
            paper.limitation,
        )
        if value and value.strip()
    )


def test_first_ten_scale_sources_have_distinct_conditions_and_unchanged_gold_rows() -> None:
    papers = _scale_papers(10)
    assert len(papers) == 10
    conditions = [paper.condition_description for paper in papers]
    assert all(value.strip() for value in conditions)
    assert len(set(conditions)) == len(conditions)
    assert [index for index, value in enumerate(conditions) if "Study " in value] == [0]

    # These three source passages are the original positive gold fixtures.
    assert conditions[0] == "Study A tested a fixed-value coupon; Study B tested a bundle-price display."
    assert conditions[1].startswith("A reference-price label was compared with a no-promotion shelf label")
    assert conditions[4] == "A member-only threshold offer was compared with the standard displayed price."

    # Condition coverage is added without changing the original effect,
    # sample, follow-up, or baseline limitation rows.
    for paper in papers:
        direction, magnitude, sample_size, follow_up = _EXPECTED_FACTS[paper.index]
        assert (paper.finding, paper.sample_description, paper.limitation) == (
            (
                "The treatment produced no measurable change in the primary outcome in the observed controlled sample (n=24)."
                if direction == "null"
                else f"The treatment {'increased' if direction == 'increase' else 'reduced'} the primary outcome by {magnitude} points in the observed controlled sample (n=24)."
            ),
            (
                "The article reports two separately labeled samples, Study A (n=24) and Study B (n=31). "
                "The single source estimate is for Study A (n=24); no separate Study B (n=31) effect estimate "
                "and no common pooled effect are reported."
                if paper.index == 0
                else "One observed controlled sample (n=24)."
            ),
            "Results apply only to the observed controlled sample and the 30-day follow-up; they do not establish a longer-term effect."
            + (" The two source-labeled samples remain distinct." if paper.index == 0 else ""),
        )
        assert sample_size == 24 and follow_up == 30


def test_generated_pdfs_parse_all_ten_gold_conditions_and_keep_unknowns_unjudgable(
    tmp_path: Path,
) -> None:
    papers = _scale_papers(10)
    corpus = ControlledPdfCorpus.from_pdfs(_write_corpus(tmp_path, papers))
    assert len(corpus.documents) == 10

    for paper in papers:
        source = corpus.documents[paper.paper_key]
        direction, magnitude, sample_size, follow_up = _EXPECTED_FACTS[paper.index]
        assert not source.parse_diagnostics, (paper.index, source.parse_diagnostics)
        assert len(source.facts) == 1
        fact = source.facts[0]
        assert (fact.direction, fact.magnitude_points, fact.sample_size, fact.follow_up_days) == (
            direction, magnitude, sample_size, follow_up,
        )
        assert _EXPECTED_FACT_CONDITIONS[paper.index] in fact.condition
        supported_gold = evaluate_assertion(
            corpus,
            corpus.authored_assertion_from_source_titles(
                _gold_candidate_claim(paper),
                required_paper_keys=(paper.paper_key,),
            ),
        )
        assert supported_gold.judgment == "supported", (paper.index, supported_gold)
        if paper.index == 0:
            assert fact.study_label == "Study A"
            assert [(sample.study_label, sample.sample_size) for sample in source.study_samples] == [
                ("Study A", 24), ("Study B", 31),
            ]
            assert "bundle-price display" in source.study_samples[1].condition
        else:
            assert len(source.study_samples) == 1

    known = papers[2]
    valid_text = (
        f"{known.title} reports {known.finding} Condition detail: "
        f"{known.condition_description} {known.limitation}"
    )
    valid = evaluate_assertion(
        corpus,
        corpus.authored_assertion_from_source_titles(
            valid_text,
            required_paper_keys=(known.paper_key,),
        ),
    )
    assert valid.judgment == "supported", valid

    for altered in (
        valid_text.replace(known.condition_description, "", 1),
        valid_text.replace(known.condition_description, "a different offer condition", 1),
    ):
        verdict = evaluate_assertion(
            corpus,
            corpus.authored_assertion_from_source_titles(
                altered,
                required_paper_keys=(known.paper_key,),
            ),
        )
        assert verdict.judgment == "unsupported", verdict
        assert "condition_omitted_or_mismatched" in verdict.issue_codes

    unknown = replace(known, condition_description="")
    unknown_path = tmp_path / "unknown-condition.pdf"
    _write_scale_pdf(unknown_path, unknown)
    unknown_corpus = ControlledPdfCorpus.from_pdfs({known.paper_key: unknown_path})
    unknown_source = unknown_corpus.documents[known.paper_key]
    assert "natural_condition_unresolved" in unknown_source.parse_diagnostics
    unjudgable = evaluate_assertion(
        unknown_corpus,
        unknown_corpus.authored_assertion_from_source_titles(
            f"{known.title} reports {known.finding} {known.limitation}",
            required_paper_keys=(known.paper_key,),
        ),
    )
    assert unjudgable.judgment == "unjudgable", unjudgable
    assert "unjudgable_source_condition" in unjudgable.issue_codes


@pytest.mark.parametrize("paper_index", (0, 1, 4))
def test_condition_is_in_the_primary_finding_qualifier_closure_and_candidate_scope(
    tmp_path: Path,
    paper_index: int,
) -> None:
    paper = _scale_papers(5)[paper_index]
    summary = _reader_content(paper)
    limitations = str(summary["core_analysis"]["limitations"])
    assert paper.limitation in limitations
    assert paper.condition_description in limitations

    workspace = JobWorkspace.create(str(tmp_path), "scale-condition-scope", job_id="scale-condition-scope")
    registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    service = SimpleNamespace(registry=registry, workspace=workspace, summaries=[summary])
    bind_production_writer_sources(service, [])
    inventory = load_writer_source_inventory_v1(registry)
    source = inventory.paper_by_key()[paper.paper_key]

    primary = next(
        claim for claim in source.claims
        if claim.claim_type == "empirical_finding" and paper.finding in claim.text
    )
    condition_fields = {
        field.source_field_id
        for field in source.source_fields
        if field.canonical_field == "limitations"
        and paper.condition_description.casefold() in field.source_value.casefold()
    }
    assert condition_fields
    dependencies = [
        item for item in source.interpretation_dependencies
        if item.primary_claim_id == primary.claim_id
    ]
    assert dependencies
    assert condition_fields.issubset({
        field_id for item in dependencies for field_id in item.required_source_field_ids
    })
    qualifier_claim_ids = {
        claim_id for item in dependencies for claim_id in item.required_source_claim_ids
    }
    assert any(
        claim.claim_id in qualifier_claim_ids
        and claim.claim_type == "author_interpretation"
        and paper.condition_description.casefold() in claim.text.casefold()
        for claim in source.claims
    )

    source_claims = {claim.claim_id: claim for claim in source.claims}
    source_claim_ids = {primary.claim_id}
    evidence_ids = set(primary.evidence_ids)
    source_field_ids: set[str] = set()
    pending = [primary.claim_id]
    while pending:
        current = pending.pop()
        for dependency in source.interpretation_dependencies:
            if dependency.primary_claim_id != current:
                continue
            assert dependency.scope != "unresolved"
            new_claim_ids = set(dependency.required_source_claim_ids) - source_claim_ids
            source_claim_ids.update(new_claim_ids)
            pending.extend(sorted(new_claim_ids))
            evidence_ids.update(dependency.required_evidence_ids)
            source_field_ids.update(dependency.required_source_field_ids)
    for claim_id in source_claim_ids:
        evidence_ids.update(source_claims[claim_id].evidence_ids)

    claim = {
        "claim_id": "synthesis:topic_synthesis:condition-closure",
        "claim_type": "empirical_finding",
        "fragment_id": "topic:condition-closure:fragment:1",
        "paper_key": paper.paper_key,
        "primary_claim_id": primary.claim_id,
        "source_claim_ids": sorted(source_claim_ids),
        "evidence_ids": sorted(evidence_ids),
        "source_field_ids": sorted(source_field_ids),
        "text": primary.text,
    }
    route = {
        "topic_id": "topic:condition-closure",
        "logical_node_id": "topic_synthesis:condition-closure",
        "paper_ids": [paper.paper_key],
        "fragments": [{
            "fragment_id": "topic:condition-closure:fragment:1",
            "paper_ids": [paper.paper_key],
            "provider_results": [{
                "result_id": "condition-result:1",
                "provider_output": {"claims": [claim]},
            }],
        }],
    }
    scope = build_candidate_output_scope_v1(inventory, [route])
    slot = next(item for item in scope.claim_slots if item.primary_claim_id == primary.claim_id)
    assert condition_fields.issubset(set(slot.source_field_ids))
    assert qualifier_claim_ids.issubset(set(slot.source_claim_ids))

    group = next(item for item in scope.claim_groups if item.claim_group_id == slot.claim_group_id)
    scope_wire = {
        "claim_group_ids": [group.claim_group_id],
        "required_claim_group_indices": [0],
        "organization_tasks": [{
            "claim_group_index": 0,
            "claim_group_id": group.claim_group_id,
            "task_id": group.task_id,
            "synthesis_claim_id": group.synthesis_claim_id,
            "claim_kind": group.claim_kind,
            "claim_seed": group.claim_seed,
            "paper_keys": list(group.paper_keys),
            "support_slot_ids": list(group.support_slot_ids),
            "task_contract": dict(group.task_contract),
        }],
        "claim_slots": [{"claim_group_index": 0, **item.to_dict()} for item in scope.claim_slots],
    }
    request = {
        "candidate_id": "condition-candidate",
        "organizing_logic": "Preserve source conditions",
        "candidate_output_scope": scope_wire,
        "hierarchy": {
            "claim_group_ids": [group.claim_group_id],
            "group_assignment": {group.claim_group_id: "condition-shard"},
            "shard_id": "condition-shard",
        },
        "shared_semantic_context": {
            "interpretation_source_tables": {
                "source_claims": [asdict(claim) for claim in source.claims],
                "source_fields": [asdict(field) for field in source.source_fields],
            },
        },
        "evidence": [{"paper_key": paper.paper_key, "title": paper.title}],
    }
    response = _compact_candidate("candidate_provider_generation", request)
    claim_text = str(response["sections"][0]["claims"][0]["text"])
    assert paper.condition_description.rstrip(".") in claim_text

    pdf_path = tmp_path / f"{paper.stem}.pdf"
    _write_scale_pdf(pdf_path, paper)
    corpus = ControlledPdfCorpus.from_pdfs({paper.paper_key: pdf_path})
    verdict = evaluate_assertion(
        corpus,
        corpus.authored_assertion_from_source_titles(
            claim_text,
            required_paper_keys=(paper.paper_key,),
        ),
    )
    assert verdict.judgment == "supported", verdict

    qualifier_evidence_ids = {
        evidence_id
        for dependency in dependencies
        for evidence_id in dependency.required_evidence_ids
    }
    for qualifier_claim_id in qualifier_claim_ids:
        qualifier_evidence_ids.update(source_claims[qualifier_claim_id].evidence_ids)
    packet = {
        "section_id": f"section:condition:{paper.index}",
        "planned_claims": [claim_text],
        "claim_support": [{
            "claim": claim_text,
            "paper_key": paper.paper_key,
            "primary_claim_id": primary.claim_id,
            "source_claim_ids": list(slot.source_claim_ids),
            "evidence_ids": list(slot.evidence_ids),
            "source_field_ids": list(slot.source_field_ids),
            "qualifier_source_claim_ids": sorted(qualifier_claim_ids),
            "qualifier_evidence_ids": sorted(qualifier_evidence_ids),
            "qualifier_source_field_ids": list(slot.source_field_ids),
            "source_summary_hash": source.source_summary_hash,
        }],
        "paper_keys": [paper.paper_key],
        "relation_ids": list(slot.relation_ids),
        "source_summary_hashes": [source.source_summary_hash],
        "evidence_view_hashes": [source.evidence_view_hash],
        "evidence_items": [{
            "paper_key": paper.paper_key,
            "summary_hash": source.source_summary_hash,
            "view_hash": source.evidence_view_hash,
            "fields": {"findings": [primary.text]},
            "source_fields": {},
            "interpretation_context": [],
        }],
        "retrieval_provenance": {
            "selection": "section_targeted",
            "paper_keys": [paper.paper_key],
            "view_hashes": [source.evidence_view_hash],
        },
    }
    citation_catalog = {"entries": [{
        "ref_id": "R001",
        "canonical_paper_key": paper.paper_key,
        "title": paper.title,
        "status": "active",
    }]}
    writer_scope = build_writer_task_scope_v1(
        packet,
        citation_catalog,
        source_inventory=inventory,
    )
    assert writer_scope["scope_status"] == "ready", writer_scope
    writer_task = writer_scope["tasks"][0]
    assert set(slot.source_field_ids).issubset(set(writer_task["source_field_ids"]))
    assert set(slot.source_field_ids).issubset(set(writer_task["qualifier_source_field_ids"]))
    assert any(
        paper.condition_description.casefold()
        in str(field.get("source_value") or "").casefold()
        for row in writer_task["canonical_source_bundle"]
        for field in row.get("source_fields") or ()
        if isinstance(field, dict)
    )
    writer_wire = project_writer_scope_for_provider_v1(writer_scope)
    writer_trace: list[dict[str, Any]] = []
    writer_response = _source_bound_writer_response(
        {
            "writer_task_scope": writer_wire,
            "citation_ref_catalog": citation_catalog["entries"],
        },
        writer_trace,
    )
    assert writer_response is not None
    writer_content = writer_response["content"]
    writer_text = str(writer_content["blocks"][0]["text"])
    assert paper.condition_description.rstrip(".") in writer_text
    validate_writer_task_output_v1(writer_scope, writer_content)
    assert set(condition_fields).issubset(
        set(writer_trace[0]["tasks"][0]["qualifier_source_field_ids"])
    )
