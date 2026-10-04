from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from outline.semantic_chunking import (
    build_paper_content_layers,
    derive_interpretation_dependencies,
    derive_unit_source_field_ledger,
)
from outline.v3_evidence import build_outline_evidence_views
from outline.v3_models import (
    EvidenceClaim,
    InterpretationDependency,
    ResearchUnit,
    SourceFieldLedgerEntry,
    compute_v3_hash,
)
from runtime.provider_runtime import hash_json
from summary_schema import normalize_ai_summary
from tests.test_outline_v3_semantic_execution import _summary


BOUNDARY = "REDACTED_BOUNDARY: the positive effect appeared only under the stated condition."
PRIVATE_R1 = Path(r"D:\tmp\astra-pr25-audit-20260926\private\r1")


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _pdf_hash_matches_typed_authority(actual: str, manifest: dict[str, Any], binding: dict[str, Any]) -> bool:
    return (
        len(actual) == 64
        and actual == str(manifest.get("source_pdf_content_sha256") or "")
        and actual == str(binding.get("source_pdf_content_sha256") or "")
    )


def _bound_pdf_hash_matches_typed_authority(
    manifest: dict[str, Any], binding: dict[str, Any]
) -> bool:
    pdf_path = Path(str(binding.get("source_pdf") or ""))
    return pdf_path.is_file() and _pdf_hash_matches_typed_authority(
        _sha256_path(pdf_path), manifest, binding
    )


def _source_artifact_hash_matches_typed_authority(manifest: dict[str, Any]) -> bool:
    artifact_path = Path(str(manifest.get("source_summary_artifact_path") or ""))
    expected = str(manifest.get("source_summary_artifact_hash") or "")
    return artifact_path.is_file() and len(expected) == 64 and _sha256_path(artifact_path) == expected


def _unit_scopes_are_source_safe(units: list[Any]) -> bool:
    return all(
        not unit.source_study_id
        and all(not claim.study_id for claim in unit.claims)
        and all(
            dependency.scope in {"paper", "unresolved"} and not dependency.study_id
            for dependency in unit.interpretation_dependencies
        )
        for unit in units
    )


def _layers(summary: dict[str, Any]):
    views = build_outline_evidence_views([summary], "evidence-v2-test")
    return views, build_paper_content_layers([summary], views, job_id="evidence-v2-test")


def _ai_summary(summary: dict[str, Any]) -> dict[str, Any]:
    nested = summary.get("ai_summary")
    return nested if isinstance(nested, dict) else summary


def test_source_field_ledger_classifies_projection_without_calling_nonexact_fields_lost():
    summary = _summary("paper-ledger", "Synthetic paper", "Finding with whitespace.")
    ai_summary = _ai_summary(summary)
    ai_summary["core_analysis"]["findings"] = "  Finding with whitespace.  "
    ai_summary["core_analysis"]["conclusions"] = "Exact conclusion."
    ai_summary["core_analysis"]["theoretical_framework"] = "Equity theory"
    ai_summary["core_analysis"]["boundary_signal"] = BOUNDARY
    summary["paper_info"]["journal"] = "Synthetic context journal"

    views, layers = _layers(summary)
    view = views.views[0]
    dossier = layers.dossier_by_paper["paper-ledger"]
    by_path = {entry.source_path: entry for entry in dossier.source_field_ledger}

    rewritten = by_path["core_analysis.findings"]
    assert rewritten.disposition == "rewritten"
    assert rewritten.source_value == "  Finding with whitespace.  "
    assert rewritten.derived_value == "Finding with whitespace."
    assert by_path["core_analysis.conclusions"].disposition == "exact"
    assert by_path["paper_info.journal"].disposition == "context"
    unmapped = by_path["core_analysis.boundary_signal"]
    assert unmapped.disposition == "unmapped"
    assert unmapped.interpretation_required is True
    assert BOUNDARY == unmapped.source_value
    assert view.source_field_ledger
    assert all(
        entry.disposition in {"exact", "rewritten", "context", "unmapped"}
        for entry in dossier.source_field_ledger
    )
    assert all(
        entry.disposition != "rewritten" or entry.derived_value
        for entry in dossier.source_field_ledger
    )


@pytest.mark.parametrize("study_count", [3, 4])
def test_explicit_three_and_four_study_ledgers_keep_source_owners(study_count: int):
    summary = _summary(f"paper-{study_count}-studies", "Redacted study example", "Paper-level summary.")
    summary["studies"] = [
        {
            "study_id": f"S{index}",
            "findings": [f"REDACTED finding {index}"],
            "boundary_conditions": [f"REDACTED boundary {index}"],
            "zero_results": [f"REDACTED null result {index}"],
        }
        for index in range(1, study_count + 1)
    ]

    _views, layers = _layers(summary)
    dossier = layers.dossier_by_paper[f"paper-{study_count}-studies"]
    assert [unit.source_study_id for unit in dossier.research_units] == [
        f"S{index}" for index in range(1, study_count + 1)
    ]
    assert [unit.study_id for unit in dossier.research_units] == [
        f"paper-{study_count}-studies:study:s{index}"
        for index in range(1, study_count + 1)
    ]

    ledger_by_id = {entry.source_field_id: entry for entry in dossier.source_field_ledger}
    for index, unit in enumerate(dossier.research_units, start=1):
        assert unit.source_field_ids
        owned = [ledger_by_id[field_id] for field_id in unit.source_field_ids]
        assert all(entry.scope == "explicit_study" for entry in owned)
        assert all(entry.study_id == f"S{index}" for entry in owned)
        dependency = next(
            item
            for item in unit.interpretation_dependencies
            if item.primary_claim_id
        )
        assert dependency.scope == "explicit_study"
        assert dependency.study_id == unit.study_id
        assert any("boundary" in entry.source_path for entry in owned)
        assert any("zero_results" in entry.source_path for entry in owned)


@pytest.mark.parametrize("study_count", [3, 4])
def test_unstructured_multi_study_summary_keeps_paper_and_unresolved_scope(study_count: int):
    summary = _summary(
        f"paper-unresolved-{study_count}",
        "Redacted multi-study summary",
        f"Across {study_count} studies, the pooled finding remains qualified.",
    )
    ai_summary = _ai_summary(summary)
    ai_summary["core_analysis"]["findings"] = (
        f"Across {study_count} studies, the pooled result followed different conditions."
    )
    ai_summary["core_analysis"]["research_gap"] = "The authors propose one replication gap."
    ai_summary["core_analysis"]["zero_results"] = "A null result is reported for one study."
    ai_summary["core_analysis"]["boundary_context"] = BOUNDARY

    _views, layers = _layers(summary)
    dossier = layers.dossier_by_paper[f"paper-unresolved-{study_count}"]
    assert "multi_study_mapping_unresolved" in dossier.diagnostics
    assert len(dossier.research_units) == 1
    unit = dossier.research_units[0]
    assert unit.source_study_id == ""
    assert unit.source_field_ids == []
    assert unit.zero_results == ["A null result is reported for one study."]
    assert any(
        claim.claim_type == "author_proposed_gap"
        and claim.text == "The authors propose one replication gap."
        and claim.study_id == ""
        for claim in dossier.claims
    )

    boundary = next(
        entry
        for entry in dossier.source_field_ledger
        if entry.source_path == "core_analysis.boundary_context"
    )
    gap = next(
        entry
        for entry in dossier.source_field_ledger
        if entry.source_path == "core_analysis.research_gap"
    )
    zero_result = next(
        entry
        for entry in dossier.source_field_ledger
        if entry.source_path == "core_analysis.zero_results"
    )
    assert boundary.disposition == "unmapped"
    assert boundary.scope == "unresolved"
    assert boundary.study_id == ""
    assert zero_result.disposition == "context"
    assert zero_result.canonical_field == "zero_results"
    assert zero_result.scope == "unresolved"
    assert zero_result.study_id == ""
    assert gap.scope == "paper"
    assert gap.study_id == ""
    assert any(
        item.scope == "unresolved" and item.study_id == ""
        for item in unit.interpretation_dependencies
    )


def test_post_builder_typed_unit_derives_finding_to_qualifier_dependencies():
    source_hash = "f" * 64
    study_id = "synthetic-paper:study:source-S1"
    claims = [
        EvidenceClaim(
            "C1", "empirical_finding", "The intervention improves preference.",
            study_id, ["E1"], "probe:study:result", source_hash,
        ),
        EvidenceClaim(
            "C2", "empirical_finding", "Improvement appears only under condition X.",
            study_id, ["E2"], "probe:study:boundary", source_hash,
        ),
        EvidenceClaim(
            "C3", "author_interpretation", "The mechanism was correlational, not manipulated.",
            study_id, ["E3"], "probe:study:mechanism", source_hash,
        ),
    ]
    unit = ResearchUnit(
        study_id=study_id,
        parent_paper_id="synthetic-paper",
        findings=[claims[0].text],
        mechanisms=[claims[2].text],
        moderators_or_boundaries=[claims[1].text],
        limitations=[BOUNDARY],
        claims=claims,
        source_locators={"study": ["probe:study"]},
        evidence_ids=["E1", "E2", "E3"],
        source_summary_hash=source_hash,
    )

    ledger = derive_unit_source_field_ledger(unit)
    dependency = next(
        item
        for item in derive_interpretation_dependencies(unit, ledger)
        if item.primary_claim_id == "C1"
    )
    by_id = {entry.source_field_id: entry for entry in ledger}

    assert dependency.scope == "explicit_study"
    assert dependency.study_id == study_id
    assert dependency.required_source_claim_ids == ["C2", "C3"]
    assert dependency.required_evidence_ids == ["E2", "E3"]
    assert dependency.required_source_field_ids
    assert any(
        by_id[field_id].source_value == BOUNDARY
        for field_id in dependency.required_source_field_ids
    )
    assert all(
        by_id[field_id].source_value
        for field_id in dependency.required_source_field_ids
    )


def test_source_field_and_dependency_wire_models_round_trip_and_reject_fake_owners():
    field_entry = SourceFieldLedgerEntry(
        source_field_id="source-field:boundary",
        source_path="studies[0].boundary_conditions",
        source_value=BOUNDARY,
        disposition="unmapped",
        scope="explicit_study",
        study_id="S1",
        interpretation_required=True,
        source_summary_hash="a" * 64,
    )
    dependency = InterpretationDependency(
        primary_claim_id="C1",
        required_source_claim_ids=["C2"],
        required_evidence_ids=["E2"],
        required_source_field_ids=[field_entry.source_field_id],
        scope="explicit_study",
        study_id="paper:study:s1",
        reason="boundary",
    )

    assert SourceFieldLedgerEntry.from_dict(field_entry.to_dict()) == field_entry
    assert InterpretationDependency.from_dict(dependency.to_dict()) == dependency
    with pytest.raises(ValueError, match="cannot claim a study_id"):
        SourceFieldLedgerEntry(
            source_field_id="source-field:paper",
            source_path="ai_summary.core_analysis.research_gap",
            source_value="Paper-level author gap.",
            disposition="context",
            scope="paper",
            study_id="paper:study:invented",
        )


@pytest.mark.parametrize("row_index", [0, 1], ids=["R1-row-001", "R1-row-002"])
def test_actual_r1_raw_summary_pdf_authority_reaches_current_evidence_builder(row_index: int):
    summaries_path = PRIVATE_R1 / "summaries.json"
    if not summaries_path.is_file():
        pytest.skip("frozen Astra private R1 source sample is unavailable in this environment")

    raw_summary_bytes = summaries_path.read_bytes()
    summaries_sha256 = hashlib.sha256(raw_summary_bytes).hexdigest()
    raw_rows = json.loads(raw_summary_bytes.decode("utf-8"))
    summary = raw_rows[row_index]
    paper_info = summary.get("paper_info")
    paper_key = str(paper_info.get("canonical_paper_key") or "") if isinstance(paper_info, dict) else ""
    assert paper_key

    matching_manifests = []
    for path in PRIVATE_R1.glob("typed_*.json"):
        manifest = json.loads(path.read_text(encoding="utf-8"))
        if str(manifest.get("canonical_paper_key") or "") == paper_key:
            matching_manifests.append(manifest)
    assert len(matching_manifests) == 1
    manifest = matching_manifests[0]

    raw_ai_summary = summary.get("ai_summary")
    raw_ai_summary = raw_ai_summary if isinstance(raw_ai_summary, dict) else summary
    normalized_raw_hash = hash_json(normalize_ai_summary(raw_ai_summary))
    typed_summary_payload = manifest.get("summary_payload")
    assert isinstance(typed_summary_payload, dict)
    normalized_typed_hash = hash_json(normalize_ai_summary(typed_summary_payload))
    assert normalized_raw_hash == normalized_typed_hash
    assert _source_artifact_hash_matches_typed_authority(manifest)

    binding = manifest.get("binding")
    assert isinstance(binding, dict)
    assert _bound_pdf_hash_matches_typed_authority(manifest, binding)

    source_summary_hash = compute_v3_hash(summary)
    views = build_outline_evidence_views([summary], f"actual-r1-row-{row_index + 1:03d}")
    layers = build_paper_content_layers(
        [summary], views, job_id=f"actual-r1-row-{row_index + 1:03d}"
    )
    assert len(layers.dossiers) == 1
    dossier = layers.dossiers[0]
    assert dossier.source_summary_hash == source_summary_hash
    assert views.views[0].source_summary_hash == source_summary_hash
    assert len(dossier.source_field_ledger) > 0
    assert all(
        entry.disposition in {"exact", "rewritten", "context", "unmapped"}
        for entry in dossier.source_field_ledger
    )
    assert all(
        entry.disposition != "rewritten" or entry.derived_value
        for entry in dossier.source_field_ledger
    )

    # These two frozen rows contain paper-level summaries with no explicit
    # source study IDs. Their gaps stay paper-owned and interpretation fields
    # with ambiguous study scope stay unresolved.
    assert dossier.research_units
    assert _unit_scopes_are_source_safe(dossier.research_units)
    gap_claims = [claim for claim in dossier.claims if claim.claim_type == "author_proposed_gap"]
    assert gap_claims
    assert not any(claim.study_id for claim in gap_claims)
    unresolved_fields = [
        entry
        for entry in dossier.source_field_ledger
        if entry.scope == "unresolved" and entry.interpretation_required
    ]
    assert len(unresolved_fields) > 0
    assert not any(entry.study_id for entry in unresolved_fields)
    dependencies = [
        dependency
        for unit in dossier.research_units
        for dependency in unit.interpretation_dependencies
    ]
    assert dependencies
    assert not any(dependency.scope == "explicit_study" for dependency in dependencies)
    assert not any(dependency.study_id for dependency in dependencies)
    assert any(dependency.scope == "unresolved" for dependency in dependencies)
    assert len(summaries_sha256) == 64
