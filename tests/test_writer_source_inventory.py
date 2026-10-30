from __future__ import annotations

import json
from pathlib import Path
from copy import deepcopy

import pytest

from outline.v3_artifacts import OutlineArtifact
from outline.semantic_chunking import build_paper_content_layers
from outline.v3_evidence import build_outline_evidence_views
from outline.v3_models import compute_v3_hash
from services.artifact_registry import ArtifactDependencyRefV2, ArtifactRegistry
from runtime.provider_runtime import hash_json
from services.writer_source_inventory import (
    SOURCE_INVENTORY_ARTIFACT_ID,
    VerifiedWriterSourceInventoryV1,
    WriterSourceInventoryError,
    load_writer_source_inventory_v1,
)
from services.writer_task_scope import (
    build_writer_output_maximum_specimen_v1,
    build_writer_task_scope_v1,
    validate_writer_task_output_v1,
)


JOB_ID = "writer-source-job"
PAPER_KEY = "paper-A"
PRIMARY_TEXT = "Treatment improves the measured outcome in Study 1."
QUALIFIER_TEXT = "The effect is bounded to the stated study condition."
PLANNED_CLAIM = "Treatment improves the outcome under the stated study condition."
SOURCE_SUMMARY = {
    "status": "success",
    "paper_info": {
        "canonical_paper_key": PAPER_KEY,
        "title": "A controlled study of treatment effects",
        "authors": ["Author, A"],
        "year": 2025,
    },
    "ai_summary": {
        "routing": {
            "paper_type": "empirical",
            "classification_status": "resolved",
            "route_confidence": "high",
        },
        "core_analysis": {
            "summary": "A treatment effect was observed within the reported study condition.",
            "findings": [PRIMARY_TEXT],
            "limitations": [QUALIFIER_TEXT],
            "conclusions": ["The effect holds within the stated boundary."],
            "methodology": "Controlled experiment",
            "research_gap": ["Replication remains useful."],
            "future_research_directions": ["Replicate under a second condition."],
        },
        "specialized_details": {
            "empirical": {
                "analysis_technique": "Controlled comparison",
                "data_source_and_size": "One controlled experiment.",
                "research_questions_or_hypotheses": ["Does treatment change the measured outcome?"],
                "studies": [{
                    "study_id": "S1",
                    "findings": [PRIMARY_TEXT],
                    "limitations": [QUALIFIER_TEXT],
                }],
            },
        },
        "quality_audit": {
            "needs_manual_review": False,
            "missing_critical_fields": [],
            "conflict_flags": [],
        },
    },
}
SUMMARY_HASH = compute_v3_hash(SOURCE_SUMMARY)


def _source_models(summaries: list[dict] | None = None):
    source_rows = summaries or [deepcopy(SOURCE_SUMMARY)]
    evidence_views = build_outline_evidence_views(source_rows, JOB_ID, strict_status=True)
    content_layers = build_paper_content_layers(
        source_rows,
        evidence_views,
        job_id=JOB_ID,
        strict_status=True,
    )
    return evidence_views, content_layers


def _canonical_ids(content_layers):
    dossier = content_layers.dossiers[0]
    unit = dossier.research_units[0]
    primary = next(claim for claim in unit.claims if claim.text == PRIMARY_TEXT and claim.claim_type == "empirical_finding")
    dependency = next(dep for dep in unit.interpretation_dependencies if dep.primary_claim_id == primary.claim_id)
    qualifier = next(claim for claim in unit.claims if claim.claim_id in dependency.required_source_claim_ids)
    primary_field = next(field_id for field_id in unit.source_field_ids if field_id not in dependency.required_source_field_ids)
    return {
        "dossier": dossier,
        "unit": unit,
        "primary": primary,
        "qualifier": qualifier,
        "dependency": dependency,
        "primary_field": primary_field,
        "qualifier_field": dependency.required_source_field_ids[0],
    }


_CANONICAL_EVIDENCE_VIEWS, _CANONICAL_LAYERS = _source_models()
_CANONICAL = _canonical_ids(_CANONICAL_LAYERS)
STUDY_ID = _CANONICAL["unit"].study_id
PRIMARY_CLAIM_ID = _CANONICAL["primary"].claim_id
QUALIFIER_CLAIM_ID = _CANONICAL["qualifier"].claim_id
PRIMARY_EVIDENCE_ID = _CANONICAL["primary"].evidence_ids[0]
QUALIFIER_EVIDENCE_ID = _CANONICAL["dependency"].required_evidence_ids[0]
PRIMARY_FIELD_ID = _CANONICAL["primary_field"]
QUALIFIER_FIELD_ID = _CANONICAL["qualifier_field"]


def _register_outline_artifact(
    registry: ArtifactRegistry,
    tmp_path: Path,
    *,
    artifact_id: str,
    node_id: str,
    payload: dict,
    dependency_hashes: dict[str, str] | None = None,
    depends_on=(),
    status: str = "ready",
) -> object:
    artifact = OutlineArtifact(
        job_id=JOB_ID,
        dependency_hashes=dict(dependency_hashes or {}),
        payload=payload,
    )
    path = tmp_path / f"{node_id}.json"
    path.write_text(json.dumps(artifact.to_dict(), ensure_ascii=False, sort_keys=True), encoding="utf-8")
    return registry.register_file(
        artifact_role="outline_v3_node_output",
        artifact_type=OutlineArtifact.artifact_type,
        artifact_version=OutlineArtifact.artifact_version,
        path=path,
        producer="outline.v3_executor.OutlineV3Executor",
        status=status,
        depends_on=list(depends_on),
        artifact_id=artifact_id,
        metadata={"job_id": JOB_ID, "node_id": node_id, "content_hash": artifact.content_hash},
    )


def _stage1_payload(summaries: list[dict]) -> dict:
    return {
        "artifact_type": "stage1_canonical_summaries",
        "artifact_version": "v1",
        "job_id": JOB_ID,
        "summary_set_hash": hash_json(summaries),
        "summaries": summaries,
    }


def _register_current_stage1(registry: ArtifactRegistry, tmp_path: Path, summaries: list[dict]):
    payload = _stage1_payload(summaries)
    versioned_id = f"outline-v3:stage1-summaries:{payload['summary_set_hash']}"
    immutable_path = tmp_path / "stage1-immutable.json"
    immutable_path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    immutable = registry.register_file(
        artifact_role="stage1_input",
        artifact_type="stage1_canonical_summaries",
        artifact_version="v1",
        path=immutable_path,
        producer="outline.v3_executor.OutlineV3Executor",
        artifact_id=versioned_id,
        metadata={"immutable": True, "summary_set_hash": payload["summary_set_hash"], "versioned_artifact_id": versioned_id},
    )
    pointer_path = tmp_path / "stage1-current.json"
    pointer_path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    pointer = registry.register_file(
        artifact_role="stage1_input",
        artifact_type="stage1_canonical_summaries",
        artifact_version="v1",
        path=pointer_path,
        producer="outline.v3_executor.OutlineV3Executor",
        artifact_id="stage1_summaries",
        depends_on=[ArtifactDependencyRefV2.from_record(immutable)],
        metadata={
            "pointer_role": "current",
            "current_version_artifact_id": versioned_id,
            "summary_set_hash": payload["summary_set_hash"],
        },
    )
    return pointer


def _registry_with_inventory(
    tmp_path: Path,
    *,
    stage1_summaries: list[dict] | None = None,
    evidence_summaries: list[dict] | None = None,
    layer_overrides: dict | None = None,
    load_inventory: bool = True,
):
    tmp_path.mkdir(parents=True, exist_ok=True)
    registry = ArtifactRegistry(tmp_path / "artifact_registry.json", JOB_ID)
    current_summaries = stage1_summaries or [deepcopy(SOURCE_SUMMARY)]
    evidence_rows = evidence_summaries or [deepcopy(SOURCE_SUMMARY)]
    stage1_pointer = _register_current_stage1(registry, tmp_path, current_summaries)
    evidence_views, content_layers = _source_models(evidence_rows)
    evidence_payload = evidence_views.to_dict()
    evidence_stage1_hash = stage1_pointer.content_hash
    if hash_json(current_summaries) != hash_json(evidence_rows):
        prior_payload = _stage1_payload(evidence_rows)
        prior_path = tmp_path / "stage1-prior.json"
        prior_path.write_text(json.dumps(prior_payload, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        prior = registry.register_file(
            artifact_role="stage1_input",
            artifact_type="stage1_canonical_summaries",
            artifact_version="v1",
            path=prior_path,
            producer="outline.v3_executor.OutlineV3Executor",
            artifact_id=f"outline-v3:stage1-summaries:{prior_payload['summary_set_hash']}",
            metadata={
                "immutable": True,
                "summary_set_hash": prior_payload["summary_set_hash"],
                "versioned_artifact_id": f"outline-v3:stage1-summaries:{prior_payload['summary_set_hash']}",
            },
        )
        evidence_stage1_hash = prior.content_hash
    evidence_record = _register_outline_artifact(
        registry,
        tmp_path,
        artifact_id="outline-v3:outline_evidence_views",
        node_id="outline_evidence_views",
        payload=evidence_payload,
        dependency_hashes={"stage1_summaries": evidence_stage1_hash},
    )
    layers_payload = content_layers.to_dict()
    if layer_overrides:
        layers_payload.update(layer_overrides)
    layer_record = _register_outline_artifact(
        registry,
        tmp_path,
        artifact_id=SOURCE_INVENTORY_ARTIFACT_ID,
        node_id="outline_content_layers",
        payload=layers_payload,
        dependency_hashes={"outline_evidence_views": compute_v3_hash(evidence_payload)},
        depends_on=[ArtifactDependencyRefV2.from_record(evidence_record)],
    )
    inventory = load_writer_source_inventory_v1(registry) if load_inventory else None
    members = _canonical_ids(content_layers)
    dossier = members["dossier"]
    unit = members["unit"]
    primary = members["primary"]
    dependency = members["dependency"]
    claim_ids = [primary.claim_id, *dependency.required_source_claim_ids]
    evidence_ids = sorted(set([*primary.evidence_ids, *dependency.required_evidence_ids]))
    packet = {
        "section_id": "section:effect",
        "planned_claims": [PLANNED_CLAIM],
        "claim_support": [{
            "claim": PLANNED_CLAIM,
            "paper_key": dossier.paper_id,
            "study_id": STUDY_ID,
            "source_claim_ids": claim_ids,
            "evidence_ids": evidence_ids,
            "source_field_ids": list(unit.source_field_ids),
        }],
        "paper_keys": [dossier.paper_id],
        "relation_ids": [],
        "source_summary_hashes": list(content_layers.source_summary_hashes),
        "evidence_view_hashes": [evidence_views.views[0].view_hash],
        "evidence_items": [{
            "paper_key": dossier.paper_id,
            "summary_hash": dossier.source_summary_hash,
            "view_hash": evidence_views.views[0].view_hash,
            "fields": {"findings": [PRIMARY_TEXT], "limitations": [QUALIFIER_TEXT]},
            "source_fields": {},
            "interpretation_context": [],
        }],
        "retrieval_provenance": {"selection": "section_targeted", "paper_keys": [dossier.paper_id]},
    }
    return registry, inventory, packet, layer_record, evidence_record


def _build_scope(packet: dict, catalog: dict, inventory):
    return build_writer_task_scope_v1(packet, catalog, source_inventory=inventory)


def _catalog() -> dict:
    return {"entries": [{"ref_id": "R001", "canonical_paper_key": PAPER_KEY, "status": "active"}]}


def test_loads_ready_registry_closure_and_supports_complete_canonical_task(tmp_path: Path) -> None:
    registry, inventory, packet, layer_record, _ = _registry_with_inventory(tmp_path)
    assert inventory.is_verified is True
    assert inventory.artifact_id == SOURCE_INVENTORY_ARTIFACT_ID
    assert inventory.artifact_hash == layer_record.content_hash
    assert inventory.content_hash
    paper = inventory.paper_by_key()[PAPER_KEY]
    assert {claim.claim_id for claim in paper.claims} >= {PRIMARY_CLAIM_ID, QUALIFIER_CLAIM_ID}
    assert {field.source_field_id for field in paper.source_fields} >= {PRIMARY_FIELD_ID, QUALIFIER_FIELD_ID}
    assert {dependency.primary_claim_id for dependency in paper.interpretation_dependencies} >= {PRIMARY_CLAIM_ID}

    scope = _build_scope(packet, _catalog(), inventory)
    assert scope["scope_status"] == "ready"
    assert scope["source_authority_status"] == "canonical_claim_and_evidence_inventory_verified"
    assert scope["usable_for_provider_admission"] is True
    task = scope["tasks"][0]
    assert task["status"] == "ready"
    canonical = task["canonical_source_bundle"][0]
    assert canonical["source_claims"][0]["text"] in {PRIMARY_TEXT, QUALIFIER_TEXT}
    assert {item["text"] for item in canonical["evidence"]} == {PRIMARY_TEXT, QUALIFIER_TEXT}
    assert {PRIMARY_TEXT, QUALIFIER_TEXT}.issubset(
        {row["source_value"] for row in canonical["source_fields"]}
    )
    assert any(dep["required_source_claim_ids"] == [QUALIFIER_CLAIM_ID] for dep in canonical["interpretation_dependencies"])
    output = validate_writer_task_output_v1(scope, build_writer_output_maximum_specimen_v1(scope))
    assert output["scope_status"] == "ready"
    assert output["source_authority_status"] == "canonical_claim_and_evidence_inventory_verified"
    assert output["usable_for_provider_admission"] is True


def test_shared_qualifier_closure_is_scoped_to_the_selected_primary_claim(tmp_path: Path) -> None:
    summary = deepcopy(SOURCE_SUMMARY)
    second_primary_text = "The second measured effect changes in the same study."
    summary["ai_summary"]["specialized_details"]["empirical"]["studies"][0]["findings"].append(
        second_primary_text
    )
    registry, inventory, packet, _, _ = _registry_with_inventory(
        tmp_path,
        stage1_summaries=[summary],
        evidence_summaries=[summary],
    )
    paper = inventory.paper_by_key()[PAPER_KEY]
    second_primary = next(claim for claim in paper.claims if claim.text == second_primary_text)
    shared_dependencies = [
        dependency
        for dependency in paper.interpretation_dependencies
        if QUALIFIER_CLAIM_ID in dependency.required_source_claim_ids
    ]
    assert {dependency.primary_claim_id for dependency in shared_dependencies} == {
        PRIMARY_CLAIM_ID,
        second_primary.claim_id,
    }

    complete_scope = _build_scope(packet, _catalog(), inventory)
    assert complete_scope["scope_status"] == "ready"
    canonical = complete_scope["tasks"][0]["canonical_source_bundle"][0]
    assert {row["primary_claim_id"] for row in canonical["interpretation_dependencies"]} == {
        PRIMARY_CLAIM_ID
    }
    assert {row["claim_id"] for row in canonical["source_claims"]} == {
        PRIMARY_CLAIM_ID,
        QUALIFIER_CLAIM_ID,
    }

    missing_qualifier_packet = deepcopy(packet)
    support = missing_qualifier_packet["claim_support"][0]
    support["source_claim_ids"] = [PRIMARY_CLAIM_ID]
    support["evidence_ids"] = [PRIMARY_EVIDENCE_ID]
    support["source_field_ids"] = [PRIMARY_FIELD_ID]
    missing_scope = _build_scope(missing_qualifier_packet, _catalog(), inventory)
    assert missing_scope["scope_status"] == "needs_review"
    assert {
        "missing_interpretation_qualifier_claim",
        "missing_interpretation_qualifier_evidence",
        "missing_interpretation_qualifier_field",
    }.issubset(missing_scope["tasks"][0]["reason_codes"])


def test_registry_file_tampering_is_rejected_by_ready_closure(tmp_path: Path) -> None:
    registry, _, _, record, _ = _registry_with_inventory(tmp_path)
    Path(record.path).write_text("{}", encoding="utf-8")
    with pytest.raises(WriterSourceInventoryError, match="dependency closure"):
        load_writer_source_inventory_v1(registry)


def test_quarantined_content_layers_are_not_an_authority(tmp_path: Path) -> None:
    registry, _, _, record, _ = _registry_with_inventory(tmp_path)
    registry.update_record(record.artifact_id, status="quarantined")
    with pytest.raises(WriterSourceInventoryError, match="dependency closure"):
        load_writer_source_inventory_v1(registry)


def test_inner_content_hash_and_schema_are_verified(tmp_path: Path) -> None:
    registry, _, _, _, _ = _registry_with_inventory(
        tmp_path / "bad-inner-hash",
        layer_overrides={"content_hash": "0" * 64},
        load_inventory=False,
    )
    with pytest.raises(WriterSourceInventoryError, match="payload hash"):
        load_writer_source_inventory_v1(registry)

    wrong_schema_path = tmp_path / "wrong-schema"
    other_registry, _, _, _, _ = _registry_with_inventory(
        wrong_schema_path,
        layer_overrides={"unexpected": "not a v3 field"},
        load_inventory=False,
    )
    with pytest.raises(WriterSourceInventoryError, match="payload schema"):
        load_writer_source_inventory_v1(other_registry)


def test_new_stage1_source_with_old_evidence_views_is_rejected(tmp_path: Path) -> None:
    changed_source = deepcopy(SOURCE_SUMMARY)
    changed_source["ai_summary"]["core_analysis"]["findings"] = ["The newly registered source says something different."]
    registry, _, _, _, _ = _registry_with_inventory(
        tmp_path,
        stage1_summaries=[changed_source],
        evidence_summaries=[deepcopy(SOURCE_SUMMARY)],
        load_inventory=False,
    )
    with pytest.raises(WriterSourceInventoryError, match="stale relative to the current Stage 1 source pointer"):
        load_writer_source_inventory_v1(registry)


def test_unsealed_inventory_object_is_not_accepted_by_scope(tmp_path: Path) -> None:
    _, verified, packet, _, _ = _registry_with_inventory(tmp_path)
    unsealed = VerifiedWriterSourceInventoryV1(
        registry_job_id=verified.registry_job_id,
        artifact_id=verified.artifact_id,
        artifact_hash=verified.artifact_hash,
        content_hash=verified.content_hash,
        papers=verified.papers,
    )
    scope = _build_scope(packet, _catalog(), unsealed)
    assert scope["usable_for_provider_admission"] is False
    assert scope["tasks"][0]["status"] == "needs_review"
    assert "unverified_source_inventory" in scope["tasks"][0]["reason_codes"]


def test_generic_claim_without_support_rows_is_blocked_even_with_inventory(tmp_path: Path) -> None:
    _, inventory, packet, _, _ = _registry_with_inventory(tmp_path)
    packet["claim_support"] = []
    scope = _build_scope(packet, _catalog(), inventory)
    assert scope["tasks"][0]["status"] == "needs_review"
    assert "missing_claim_support" in scope["tasks"][0]["reason_codes"]
    assert scope["usable_for_provider_admission"] is False


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        ("mismatched_paper", "unknown_support_source"),
        ("fabricated_claim_id", "unknown_source_claim_id"),
        ("fabricated_evidence_id", "unknown_evidence_id"),
        ("fabricated_field_id", "unknown_source_field_id"),
        ("wrong_study", "source_claim_wrong_study"),
        ("missing_qualifier", "missing_interpretation_qualifier_claim"),
        ("missing_qualifier_evidence", "missing_interpretation_qualifier_evidence"),
        ("missing_qualifier_field", "missing_interpretation_qualifier_field"),
    ],
)
def test_noncanonical_or_incomplete_support_is_not_provider_admissible(
    tmp_path: Path,
    mutation: str,
    reason: str,
) -> None:
    _, inventory, packet, _, _ = _registry_with_inventory(tmp_path)
    support = packet["claim_support"][0]
    if mutation == "mismatched_paper":
        support["paper_key"] = "paper-B"
    elif mutation == "fabricated_claim_id":
        support["source_claim_ids"][0] = "claim:paper-A:study:s1:made-up"
    elif mutation == "fabricated_evidence_id":
        support["evidence_ids"][0] = "evidence:paper-A:study:s1:made-up"
    elif mutation == "fabricated_field_id":
        support["source_field_ids"][0] = "source-field:made-up"
    elif mutation == "wrong_study":
        support["study_id"] = "paper-A:study:s2"
    elif mutation == "missing_qualifier":
        support["source_claim_ids"] = [PRIMARY_CLAIM_ID]
    elif mutation == "missing_qualifier_evidence":
        support["evidence_ids"] = [PRIMARY_EVIDENCE_ID]
    elif mutation == "missing_qualifier_field":
        support["source_field_ids"] = [PRIMARY_FIELD_ID]
    scope = _build_scope(packet, _catalog(), inventory)
    assert scope["scope_status"] == "needs_review"
    assert scope["usable_for_provider_admission"] is False
    assert reason in scope["tasks"][0]["reason_codes"]
