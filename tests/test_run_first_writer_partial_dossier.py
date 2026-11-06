from __future__ import annotations

from copy import deepcopy

import pytest

from outline.v3_models import PaperContentLayers
from services.writer_task_scope import build_writer_task_scope_v1
from tests.test_writer_source_inventory import (
    PRIMARY_CLAIM_ID,
    QUALIFIER_FIELD_ID,
    SOURCE_SUMMARY,
    _catalog,
    _registry_with_inventory,
    _source_models,
)


def _partial_source(tmp_path, *, dossier_status="partial", selected_dependency_unresolved=False):
    _views, layers = _source_models()
    payload = deepcopy(layers.to_dict())
    dossier = payload["dossiers"][0]
    dossier["status"] = dossier_status
    dossier["diagnostics"] = ["unrelated_source_analysis_field_missing"]
    if selected_dependency_unresolved:
        for unit in dossier["research_units"]:
            for row in unit["interpretation_dependencies"]:
                if row["primary_claim_id"] == PRIMARY_CLAIM_ID:
                    row["scope"] = "unresolved"
                    row["study_id"] = ""
                    row["owner_study_id"] = ""
    payload = PaperContentLayers.from_dict(payload).to_dict()
    return _registry_with_inventory(tmp_path, layer_overrides=payload)


def test_partial_paper_with_complete_selected_assertion_closure_is_provider_admissible(tmp_path):
    _registry, inventory, packet, _layer, _views = _partial_source(tmp_path)
    assert inventory.is_verified
    assert inventory.papers[0].dossier_status == "partial"
    scope = build_writer_task_scope_v1(packet, _catalog(), source_inventory=inventory)
    assert scope["scope_status"] == "ready"
    assert scope["usable_for_provider_admission"] is True
    canonical = scope["tasks"][0]["canonical_source_bundle"][0]
    assert canonical["dossier_status"] == "partial"
    assert canonical["dossier_diagnostics"] == ["unrelated_source_analysis_field_missing"]


def test_builder_derived_unrelated_missing_field_stays_diagnostic_for_selected_closure(tmp_path):
    summary = deepcopy(SOURCE_SUMMARY)
    del summary["ai_summary"]["specialized_details"]["empirical"]["analysis_technique"]
    _registry, inventory, packet, _layer, _views = _registry_with_inventory(
        tmp_path, stage1_summaries=[summary], evidence_summaries=[summary],
    )
    paper = inventory.papers[0]
    assert paper.dossier_status == "partial"
    assert paper.diagnostics == ("missing_source_field:specialized_details.empirical.analysis_technique",)
    scope = build_writer_task_scope_v1(packet, _catalog(), source_inventory=inventory)
    assert scope["usable_for_provider_admission"] is True
    assert scope["tasks"][0]["canonical_source_bundle"][0]["dossier_diagnostics"] == list(paper.diagnostics)


@pytest.mark.parametrize("corruption", ["missing_qualifier", "wrong_study", "blocked_dossier", "unresolved_dependency"])
def test_partial_readiness_does_not_authorize_missing_or_foreign_selected_support(tmp_path, corruption):
    _registry, inventory, packet, _layer, _views = _partial_source(
        tmp_path, dossier_status="blocked" if corruption == "blocked_dossier" else "partial",
        selected_dependency_unresolved=corruption == "unresolved_dependency",
    )
    if corruption == "missing_qualifier":
        packet["claim_support"][0]["source_field_ids"].remove(QUALIFIER_FIELD_ID)
    elif corruption == "wrong_study":
        packet["claim_support"][0]["study_id"] = "foreign-study"
    scope = build_writer_task_scope_v1(packet, _catalog(), source_inventory=inventory)
    assert scope["usable_for_provider_admission"] is False
    expected = {
        "missing_qualifier": "missing_interpretation_qualifier_field",
        "wrong_study": "source_claim_wrong_study",
        "blocked_dossier": "source_dossier_not_ready",
        "unresolved_dependency": "unresolved_interpretation_dependency",
    }[corruption]
    assert expected in scope["tasks"][0]["reason_codes"]
