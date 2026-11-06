from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from services.artifact_registry import ArtifactRegistry
from services.job_workspace import JobWorkspace
from services.writer_source_inventory import load_writer_source_inventory_v1
from services.writer_task_scope import (
    build_writer_task_scope_v1,
    validate_writer_task_output_v1,
)
from services.writer_wire_projection import project_writer_scope_for_provider_v1
from tests.test_current_runtime_full_e2e import _source_bound_writer_response
from tests.test_run_first_scale_integration import _reader_content, _scale_papers
from tests.test_writer_source_inventory import SOURCE_SUMMARY
from tests.writer_source_fixture import bind_production_writer_sources


def test_writer_trace_distinguishes_raw_partition_arrays_from_canonical_dependency_closure(
    tmp_path: Path,
) -> None:
    paper = _scale_papers(2)[1]
    summary = _reader_content(paper)
    foreign_paper = _scale_papers(5)[4]
    foreign_summary = _reader_content(foreign_paper)
    workspace = JobWorkspace.create(
        str(tmp_path),
        "writer-qualifier-trace-diagnostic",
        job_id="writer-qualifier-trace-diagnostic",
    )
    registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    bind_production_writer_sources(
        SimpleNamespace(registry=registry, workspace=workspace, summaries=[summary, foreign_summary]),
        [],
    )
    inventory = load_writer_source_inventory_v1(registry)
    source = inventory.paper_by_key()[paper.paper_key]
    primary = next(
        claim for claim in source.claims
        if claim.claim_type == "empirical_finding" and paper.finding in claim.text
    )
    dependencies = [
        item for item in source.interpretation_dependencies
        if item.primary_claim_id == primary.claim_id
    ]
    assert dependencies
    qualifier_claim_ids = {
        value for dependency in dependencies for value in dependency.required_source_claim_ids
    }
    qualifier_evidence_ids = {
        value for dependency in dependencies for value in dependency.required_evidence_ids
    }
    qualifier_field_ids = {
        value for dependency in dependencies for value in dependency.required_source_field_ids
    }
    condition_field_ids = {
        field.source_field_id
        for field in source.source_fields
        if field.canonical_field == "limitations"
        and paper.condition_description.casefold() in field.source_value.casefold()
    }
    assert condition_field_ids
    assert condition_field_ids.issubset(qualifier_field_ids)

    source_claim_ids = {primary.claim_id, *qualifier_claim_ids}
    source_claims = {claim.claim_id: claim for claim in source.claims}
    evidence_ids = set(primary.evidence_ids) | qualifier_evidence_ids
    for claim_id in source_claim_ids:
        evidence_ids.update(source_claims[claim_id].evidence_ids)
    claim_text = (
        f"{paper.title} reports {paper.finding} {paper.condition_description} "
        f"{paper.limitation}"
    )
    packet = {
        "section_id": "section:writer-qualifier-diagnostic",
        "planned_claims": [claim_text],
        "claim_support": [{
            "claim": claim_text,
            "paper_key": paper.paper_key,
            "primary_claim_id": primary.claim_id,
            "source_claim_ids": sorted(source_claim_ids),
            "evidence_ids": sorted(evidence_ids),
            "source_field_ids": sorted(qualifier_field_ids),
            "source_summary_hash": source.source_summary_hash,
        }],
        "paper_keys": [paper.paper_key],
        "relation_ids": [],
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
    catalog_entry = {
        "ref_id": "R001",
        "canonical_paper_key": paper.paper_key,
        "title": paper.title,
        "status": "active",
    }
    canonical_scope = build_writer_task_scope_v1(
        packet,
        {"entries": [catalog_entry]},
        source_inventory=inventory,
    )
    assert canonical_scope["scope_status"] == "ready", canonical_scope
    task = canonical_scope["tasks"][0]
    assert source_claim_ids.issubset(set(task["source_claim_ids"]))
    assert evidence_ids.issubset(set(task["evidence_ids"]))
    assert qualifier_field_ids.issubset(set(task["source_field_ids"]))
    assert task["qualifier_source_claim_ids"] == sorted(qualifier_claim_ids)
    assert task["qualifier_evidence_ids"] == sorted(qualifier_evidence_ids)
    assert task["qualifier_source_field_ids"] == sorted(qualifier_field_ids)

    # The verified source bundle retains the dependency as well as its partition.
    bundle_rows = task["canonical_source_bundle"]
    stored_dependencies = [
        row
        for bundle_row in bundle_rows
        for row in bundle_row.get("interpretation_dependencies") or ()
    ]
    assert len(stored_dependencies) == len(dependencies)
    assert qualifier_claim_ids.issubset({
        value for row in stored_dependencies for value in row.get("required_source_claim_ids") or ()
    })
    assert qualifier_field_ids.issubset({
        value for row in stored_dependencies for value in row.get("required_source_field_ids") or ()
    })
    assert any(
        paper.condition_description.casefold() in str(field.get("source_value") or "").casefold()
        for row in bundle_rows
        for field in row.get("source_fields") or ()
        if isinstance(field, dict)
    )

    wire_scope = project_writer_scope_for_provider_v1(canonical_scope)
    wire_task = wire_scope["tasks"][0]
    assert wire_task["qualifier_source_claim_ids"] == sorted(qualifier_claim_ids)
    wire_store = wire_scope["evidence_store"]
    dependency_refs = [
        ref
        for row in wire_task["canonical_source_bundle_refs"]
        for ref in row.get("interpretation_dependencies_refs") or ()
    ]
    assert dependency_refs
    resolved_dependencies = [
        wire_store[ref]["value"]
        for ref in dependency_refs
        if ref in wire_store and wire_store[ref].get("kind") == "interpretation_dependency"
    ]
    assert len(resolved_dependencies) == len(dependencies)
    assert qualifier_field_ids.issubset({
        value for row in resolved_dependencies for value in row.get("required_source_field_ids") or ()
    })

    declared_packet = deepcopy(packet)
    declared_packet["claim_support"][0]["qualifier_source_claim_ids"] = [
        min(qualifier_claim_ids)
    ]
    declared_packet["claim_support"][0]["qualifier_evidence_ids"] = [
        min(qualifier_evidence_ids)
    ]
    declared_packet["claim_support"][0]["qualifier_source_field_ids"] = [
        min(qualifier_field_ids)
    ]
    declared_scope = build_writer_task_scope_v1(
        declared_packet,
        {"entries": [catalog_entry]},
        source_inventory=inventory,
    )
    declared_task = declared_scope["tasks"][0]
    assert declared_task["qualifier_source_claim_ids"] == sorted(qualifier_claim_ids)
    assert declared_task["qualifier_evidence_ids"] == sorted(qualifier_evidence_ids)
    assert declared_task["qualifier_source_field_ids"] == sorted(qualifier_field_ids)

    incomplete_packet = deepcopy(packet)
    omitted_field_id = min(qualifier_field_ids)
    incomplete_packet["claim_support"][0]["source_field_ids"].remove(omitted_field_id)
    incomplete_scope = build_writer_task_scope_v1(
        incomplete_packet,
        {"entries": [catalog_entry]},
        source_inventory=inventory,
    )
    incomplete_task = incomplete_scope["tasks"][0]
    assert incomplete_scope["scope_status"] == "needs_review"
    assert incomplete_task["status"] == "needs_review"
    assert "missing_interpretation_qualifier_field" in incomplete_task["reason_codes"]
    assert omitted_field_id not in incomplete_task["source_field_ids"]

    unbound_primary_packet = deepcopy(packet)
    unbound_primary_packet["claim_support"][0].pop("primary_claim_id")
    unbound_primary_scope = build_writer_task_scope_v1(
        unbound_primary_packet,
        {"entries": [catalog_entry]},
        source_inventory=inventory,
    )
    unbound_primary_task = unbound_primary_scope["tasks"][0]
    assert unbound_primary_scope["scope_status"] == "ready"
    assert unbound_primary_task["qualifier_source_claim_ids"] == sorted(qualifier_claim_ids)
    assert unbound_primary_task["qualifier_evidence_ids"] == sorted(qualifier_evidence_ids)
    assert unbound_primary_task["qualifier_source_field_ids"] == sorted(qualifier_field_ids)

    foreign_source = inventory.paper_by_key()[foreign_paper.paper_key]
    foreign_primary = next(
        claim for claim in foreign_source.claims
        if claim.claim_type == "empirical_finding" and foreign_paper.finding in claim.text
    )
    foreign_packet = deepcopy(packet)
    foreign_row = foreign_packet["claim_support"][0]
    foreign_row["source_claim_ids"].append(foreign_primary.claim_id)
    foreign_row["evidence_ids"].extend(foreign_primary.evidence_ids)
    foreign_row["primary_claim_id"] = foreign_primary.claim_id
    foreign_scope = build_writer_task_scope_v1(
        foreign_packet,
        {"entries": [catalog_entry]},
        source_inventory=inventory,
    )
    foreign_task = foreign_scope["tasks"][0]
    assert foreign_scope["scope_status"] == "needs_review"
    assert foreign_task["status"] == "needs_review"
    assert "unknown_source_claim_id" in foreign_task["reason_codes"]

    trace: list[dict[str, Any]] = []
    response = _source_bound_writer_response(
        {
            "writer_task_scope": wire_scope,
            "citation_ref_catalog": [catalog_entry],
        },
        trace,
    )
    assert response is not None
    validate_writer_task_output_v1(canonical_scope, response["content"])
    traced_task = trace[0]["tasks"][0]
    assert traced_task["source_claim_ids"] == wire_task["source_claim_ids"]
    assert traced_task["source_field_ids"] == wire_task["source_field_ids"]
    assert traced_task["qualifier_source_claim_ids"] == wire_task["qualifier_source_claim_ids"] == sorted(qualifier_claim_ids)


def test_missing_primary_partition_unions_all_exact_dependency_primaries(tmp_path: Path) -> None:
    paper = _scale_papers(2)[1]
    summary = _reader_content(paper)
    second_finding = "A second reported outcome differs in the same source condition."
    summary["core_analysis"]["findings"] = [paper.finding, second_finding]
    summary["core_analysis"]["conclusions"] = [paper.finding, second_finding]
    workspace = JobWorkspace.create(
        str(tmp_path),
        "writer-aggregate-qualifier-diagnostic",
        job_id="writer-aggregate-qualifier-diagnostic",
    )
    registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    bind_production_writer_sources(
        SimpleNamespace(registry=registry, workspace=workspace, summaries=[summary]),
        [],
    )
    inventory = load_writer_source_inventory_v1(registry)
    source = inventory.paper_by_key()[paper.paper_key]
    candidate_primary_claims = [
        claim for claim in source.claims
        if claim.claim_type == "empirical_finding"
        and (paper.finding in claim.text or second_finding in claim.text)
    ]
    candidate_primary_ids = {claim.claim_id for claim in candidate_primary_claims}
    dependencies = [
        dependency for dependency in source.interpretation_dependencies
        if dependency.primary_claim_id in candidate_primary_ids
    ]
    primary_ids = {dependency.primary_claim_id for dependency in dependencies}
    primary_claims = [claim for claim in candidate_primary_claims if claim.claim_id in primary_ids]
    assert len(primary_ids) >= 2
    assert {dependency.primary_claim_id for dependency in dependencies} == primary_ids
    qualifier_claim_ids = {
        value for dependency in dependencies for value in dependency.required_source_claim_ids
    }
    qualifier_evidence_ids = {
        value for dependency in dependencies for value in dependency.required_evidence_ids
    }
    qualifier_field_ids = {
        value for dependency in dependencies for value in dependency.required_source_field_ids
    }
    source_claims = {claim.claim_id: claim for claim in source.claims}
    source_claim_ids = primary_ids | qualifier_claim_ids
    evidence_ids = set(qualifier_evidence_ids)
    for claim_id in source_claim_ids:
        evidence_ids.update(source_claims[claim_id].evidence_ids)
    claim_text = f"{paper.title} reports findings within the recorded source boundaries."
    packet = {
        "section_id": "section:aggregate-qualifier-diagnostic",
        "planned_claims": [claim_text],
        "claim_support": [{
            "claim": claim_text,
            "paper_key": paper.paper_key,
            "source_claim_ids": sorted(source_claim_ids),
            "evidence_ids": sorted(evidence_ids),
            "source_field_ids": sorted(qualifier_field_ids),
        }],
        "paper_keys": [paper.paper_key],
        "relation_ids": [],
        "source_summary_hashes": [source.source_summary_hash],
        "evidence_view_hashes": [source.evidence_view_hash],
        "evidence_items": [{
            "paper_key": paper.paper_key,
            "summary_hash": source.source_summary_hash,
            "view_hash": source.evidence_view_hash,
            "fields": {"findings": [claim.text for claim in primary_claims]},
            "source_fields": {},
            "interpretation_context": [],
        }],
        "retrieval_provenance": {
            "selection": "section_targeted",
            "paper_keys": [paper.paper_key],
            "view_hashes": [source.evidence_view_hash],
        },
    }
    scope = build_writer_task_scope_v1(
        packet,
        {"entries": [{
            "ref_id": "R001",
            "canonical_paper_key": paper.paper_key,
            "title": paper.title,
            "status": "active",
        }]},
        source_inventory=inventory,
    )
    task = scope["tasks"][0]
    assert scope["scope_status"] == "ready", scope
    assert set(task["source_claim_ids"]) == source_claim_ids
    assert task["qualifier_source_claim_ids"] == sorted(qualifier_claim_ids)
    assert task["qualifier_evidence_ids"] == sorted(qualifier_evidence_ids)
    assert task["qualifier_source_field_ids"] == sorted(qualifier_field_ids)


def test_paper_qualifier_keeps_nonempty_containing_unit_owner(tmp_path: Path) -> None:
    summary = deepcopy(SOURCE_SUMMARY)
    workspace = JobWorkspace.create(
        str(tmp_path),
        "writer-paper-qualifier-owner",
        job_id="writer-paper-qualifier-owner",
    )
    registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    bind_production_writer_sources(
        SimpleNamespace(registry=registry, workspace=workspace, summaries=[summary]),
        [],
    )
    inventory = load_writer_source_inventory_v1(registry)
    source = inventory.paper_by_key()["paper-A"]
    existing_dependency = next(
        row for row in source.interpretation_dependencies
        if row.scope == "explicit_study"
    )
    primary = next(
        row for row in source.claims
        if row.claim_id == existing_dependency.primary_claim_id
    )
    paper_qualifier = next(
        row for row in source.claims
        if row.claim_type == "author_interpretation"
        and row.text == "The effect is bounded to the stated study condition."
        and ":limitations:" in row.claim_id
        and "" in row.owner_study_ids
    )
    paper_field = next(
        row for row in source.source_fields
        if row.scope == "paper"
        and "core_analysis.limitations" in row.source_path
    )

    # Exercise a verified model state where the dependency belongs to a
    # containing unit with an explicit owner while its qualifier remains
    # paper-scoped. The owner records provenance; scope controls applicability.
    owner = existing_dependency.owner_study_id
    assert owner
    owned_dependency = replace(
        existing_dependency,
        required_source_claim_ids=(paper_qualifier.claim_id,),
        required_evidence_ids=paper_qualifier.evidence_ids,
        required_source_field_ids=(paper_field.source_field_id,),
        scope="paper",
        study_id="",
    )
    owned_source = replace(
        source,
        interpretation_dependencies=tuple(
            owned_dependency if row == existing_dependency else row
            for row in source.interpretation_dependencies
        ),
    )
    owned_inventory = replace(inventory, papers=(owned_source,))
    claim_ids = {
        primary.claim_id,
        *owned_dependency.required_source_claim_ids,
    }
    claims_by_id = {row.claim_id: row for row in owned_source.claims}
    evidence_ids = {
        evidence_id
        for claim_id in claim_ids
        for evidence_id in claims_by_id[claim_id].evidence_ids
    }
    evidence_ids.update(owned_dependency.required_evidence_ids)
    field_ids = {
        owned_dependency.required_source_field_ids[0],
        next(
            row.source_field_id for row in owned_source.source_fields
            if row.scope == "explicit_study"
            and row.source_study_id == "S1"
            and row.canonical_field == "findings"
        ),
    }
    claim = "Treatment improves the measured outcome in Study 1 within the source-stated boundary."
    packet = {
        "section_id": "section:paper-qualifier-owner",
        "planned_claims": [claim],
        "claim_support": [{
            "claim": claim,
            "paper_key": owned_source.paper_key,
            "study_id": owner,
            "primary_claim_id": primary.claim_id,
            "source_claim_ids": sorted(claim_ids),
            "evidence_ids": sorted(evidence_ids),
            "source_field_ids": sorted(field_ids),
            "source_summary_hash": owned_source.source_summary_hash,
        }],
        "paper_keys": [owned_source.paper_key],
        "relation_ids": [],
        "source_summary_hashes": [owned_source.source_summary_hash],
        "evidence_view_hashes": [owned_source.evidence_view_hash],
        "evidence_items": [{
            "paper_key": owned_source.paper_key,
            "summary_hash": owned_source.source_summary_hash,
            "view_hash": owned_source.evidence_view_hash,
            "fields": {"findings": [primary.text]},
            "source_fields": {},
            "interpretation_context": [],
        }],
        "retrieval_provenance": {
            "selection": "section_targeted",
            "paper_keys": [owned_source.paper_key],
            "view_hashes": [owned_source.evidence_view_hash],
        },
    }
    catalog = {"entries": [{
        "ref_id": "R001",
        "canonical_paper_key": owned_source.paper_key,
        "title": "A controlled study of treatment effects",
        "status": "active",
    }]}

    scope = build_writer_task_scope_v1(packet, catalog, source_inventory=owned_inventory)
    task = scope["tasks"][0]
    assert scope["scope_status"] == "ready", task["reason_codes"]
    assert task["status"] == "ready"
    assert task["qualifier_source_claim_ids"] == sorted(owned_dependency.required_source_claim_ids)
    assert task["qualifier_evidence_ids"] == sorted(owned_dependency.required_evidence_ids)
    assert task["qualifier_source_field_ids"] == sorted(owned_dependency.required_source_field_ids)
    stored_dependency = next(
        row for row in task["canonical_source_bundle"][0]["interpretation_dependencies"]
        if row["primary_claim_id"] == primary.claim_id
    )
    assert stored_dependency["scope"] == "paper"
    assert stored_dependency["study_id"] == ""
    assert stored_dependency["owner_study_id"] == owner

    wrong_scope_dependency = replace(owned_dependency, study_id="S1")
    wrong_scope_source = replace(
        owned_source,
        interpretation_dependencies=(wrong_scope_dependency,),
    )
    wrong_scope_inventory = replace(owned_inventory, papers=(wrong_scope_source,))
    wrong_scope = build_writer_task_scope_v1(
        packet,
        catalog,
        source_inventory=wrong_scope_inventory,
    )
    assert wrong_scope["scope_status"] == "needs_review"
    assert "interpretation_dependency_wrong_study" in wrong_scope["tasks"][0]["reason_codes"]
