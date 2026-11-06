from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from outline.candidate_output_scope import (
    CandidateOutputClaimGroupV1,
    CandidateOutputScopeV1,
    CandidateOutputSupportSlotV1,
)
from outline.v3_artifacts import AdoptedOutline, FinalOutline, SectionEvidencePacketSet
from runtime.provider_runtime import hash_json
from services.artifact_registry import ArtifactDependencyRefV2
from services.review_generation_service import ReviewGenerationService
from services.writer_task_scope import (
    VerifiedCandidateOutputScopeV1,
    _verified_candidate_output_scope,
    build_writer_task_scope_v1,
)
from tests.test_writer_source_inventory import (
    PAPER_KEY,
    SOURCE_SUMMARY,
    _catalog,
    _registry_with_inventory,
)

SOURCE_TEXT = "The authors interpret Study 1 and Study 2 as distinct boundary conditions"


def _fixture(tmp_path, *, terminal_period: bool = False):
    summary = deepcopy(SOURCE_SUMMARY)
    summary["ai_summary"]["core_analysis"]["limitations"] = [SOURCE_TEXT]
    registry, inventory, packet, layer_record, _ = _registry_with_inventory(
        tmp_path,
        stage1_summaries=[summary],
        evidence_summaries=[summary],
    )
    paper = inventory.paper_by_key()[PAPER_KEY]
    source_claim = next(
        claim for claim in paper.claims
        if claim.claim_type == "author_interpretation"
        and claim.source_locator == "stage1:ai_summary.limitations"
        and "Study 1" in claim.text
    )
    claim_text = source_claim.text + ("." if terminal_period and not source_claim.text.endswith(".") else "")
    row = {
        "claim_slot_id": "slot:paper-level-mention",
        "claim": claim_text,
        "paper_key": PAPER_KEY,
        "study_id": "",
        "primary_claim_id": source_claim.claim_id,
        "source_claim_ids": [source_claim.claim_id],
        "evidence_ids": list(source_claim.evidence_ids),
        "source_field_ids": [],
    }
    packet.update({"planned_claims": [claim_text], "claim_support": [row]})
    slot = CandidateOutputSupportSlotV1(
        claim_slot_id=row["claim_slot_id"],
        claim_group_id="group:paper-level-mention",
        task_id="topic_synthesis",
        topic_id="topic:one",
        fragment_id="fragment:one",
        provider_result_id="result:one",
        synthesis_claim_id="synthesis:topic_synthesis:one",
        paper_key=PAPER_KEY,
        primary_claim_id=source_claim.claim_id,
        study_id="",
        source_claim_ids=(source_claim.claim_id,),
        evidence_ids=tuple(source_claim.evidence_ids),
        source_field_ids=(),
    )
    group = CandidateOutputClaimGroupV1(
        claim_group_id="group:paper-level-mention",
        task_id="topic_synthesis",
        topic_id="topic:one",
        fragment_id="fragment:one",
        provider_result_id="result:one",
        synthesis_claim_id="synthesis:topic_synthesis:one",
        claim_hash=hash_json(claim_text),
        paper_keys=(PAPER_KEY,),
        support_slot_ids=(slot.claim_slot_id,),
    )
    scope = CandidateOutputScopeV1(
        registry_job_id=inventory.registry_job_id,
        source_inventory_artifact_id=inventory.artifact_id,
        source_inventory_artifact_hash=inventory.artifact_hash,
        source_inventory_content_hash=inventory.content_hash,
        task_ids=("topic_synthesis",),
        selected_relation_ids=(),
        claim_groups=(group,),
        claim_slots=(slot,),
    )
    return registry, inventory, packet, row, scope, layer_record


def _register_artifact(registry, tmp_path, artifact_id, artifact, *, role, dependencies=(), metadata=None):
    path = Path(tmp_path) / f"{artifact_id.replace(':', '_')}.json"
    path.write_text(json.dumps(artifact.to_dict(), ensure_ascii=False, sort_keys=True), encoding="utf-8")
    return registry.register_file(
        artifact_role=role,
        artifact_type=artifact.artifact_type,
        artifact_version=artifact.artifact_version,
        path=path,
        producer="outline.v3_executor.OutlineV3Executor",
        artifact_id=artifact_id,
        depends_on=list(dependencies),
        metadata={"job_id": registry.job_id, **dict(metadata or {})},
    )


def _registered_lineage(tmp_path, *, mutation: str = ""):
    registry, inventory, packet, _row, scope, layer_record = _fixture(tmp_path)
    scope_payload = scope.to_dict()
    if mutation == "foreign_inventory_binding":
        scope_payload["source_inventory_binding"]["content_hash"] = "0" * 64
        scope_payload["content_hash"] = hash_json({key: value for key, value in scope_payload.items() if key != "content_hash"})
    scope_path = Path(tmp_path) / "candidate-output-scope.json"
    scope_path.write_text(json.dumps(scope_payload, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    scope_record = registry.register_file(
        artifact_role="outline_candidate_output_scope",
        artifact_type="outline_candidate_output_scope",
        artifact_version="v1",
        path=scope_path,
        producer="outline.v3_executor.OutlineV3Executor",
        artifact_id="outline-v3:candidate_output_scope",
        depends_on=[ArtifactDependencyRefV2.from_record(layer_record)],
    )
    section = {
        "section_id": "section:paper-level-mention",
        "task_ids": ["topic_synthesis"],
        "relation_ids": [],
        "paper_keys": [PAPER_KEY],
        "claims": [packet["claim_support"][0]["claim"]],
        "claim_support": [{**packet["claim_support"][0], "task_id": "topic_synthesis"}],
    }
    final_payload = {
        "title": "Evidence-led review",
        "candidate_id": "candidate_1",
        "sections": [section],
        "paper_keys": [PAPER_KEY],
        "relation_ids": [],
        "source_hashes": packet["source_summary_hashes"],
    }
    packet_set = SectionEvidencePacketSet(
        job_id=registry.job_id,
        dependency_hashes={"candidate_output_scope": scope_record.content_hash},
        payload={"packets": [packet], "coverage_ledger": {}},
    )
    packet_record = _register_artifact(
        registry,
        tmp_path,
        "outline-v3:section_evidence_packets",
        packet_set,
        role="outline_v3_node_output",
        dependencies=[ArtifactDependencyRefV2.from_record(scope_record)],
        metadata={"node_id": "section_evidence_packets", "content_hash": packet_set.content_hash},
    )
    final = FinalOutline(
        job_id=registry.job_id,
        dependency_hashes={"section_evidence_packets": packet_record.content_hash},
        payload=final_payload,
    )
    final_record = _register_artifact(
        registry,
        tmp_path,
        "outline-v3:final_outline",
        final,
        role="outline_v3_node_output",
        dependencies=[ArtifactDependencyRefV2.from_record(packet_record)],
        metadata={"node_id": "final_outline", "content_hash": final.content_hash},
    )
    adoption_id = "outline-v3:adoption:test"
    adopted = AdoptedOutline(
        job_id=registry.job_id,
        dependency_hashes={final_record.artifact_id: final_record.content_hash},
        payload={
            "status": "adopted",
            "adoption_id": adoption_id,
            "adoption_identity": adoption_id,
            "current_pointer_artifact_id": "outline-v3:adoption:current",
            "current_pointer_role": "current",
            "actor": "test",
            "reason": "verified lineage fixture",
            "expected_hash": final_record.content_hash,
            "final_outline_hash": final_record.content_hash,
            "coverage_audit_hash": "a" * 64,
            "stability_audit_hash": "b" * 64,
            "stage_health_hash": "c" * 64,
            "provider_receipt_closure_hash": "d" * 64,
            "final_outline": final_payload,
        },
    )
    adopted_record = _register_artifact(
        registry,
        tmp_path,
        adoption_id,
        adopted,
        role="outline_v3_adoption",
        dependencies=[ArtifactDependencyRefV2.from_record(final_record)],
        metadata={"actor": "test", "reason": "fixture", "expected_hash": final_record.content_hash},
    )
    pointer_payload = {
        "artifact_type": "outline_adoption_pointer",
        "artifact_version": "v1",
        "job_id": registry.job_id,
        "role": "current",
        "current_adoption_artifact_id": "outline-v3:adoption:missing" if mutation == "wrong_adoption_key" else adoption_id,
        "current_adoption_hash": adopted_record.content_hash,
        "adoption_identity": adoption_id,
        "updated_at": adopted_record.created_at,
    }
    pointer_path = Path(tmp_path) / "adoption-current.json"
    pointer_path.write_text(json.dumps(pointer_payload, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    registry.register_file(
        artifact_role="outline_v3_adoption_current",
        artifact_type="outline_adoption_pointer",
        artifact_version="v1",
        path=pointer_path,
        producer="outline.adoption_transaction.OutlineAdoptionTransaction",
        artifact_id="outline-v3:adoption:current",
        depends_on=[ArtifactDependencyRefV2.from_record(adopted_record)],
        metadata={"role": "current"},
    )
    service = ReviewGenerationService.__new__(ReviewGenerationService)
    service.job_id = registry.job_id
    service.registry = registry
    return service, registry, inventory, packet, final_payload, scope_record


@pytest.mark.parametrize("terminal_period", [False, True])
def test_exact_ownerless_paper_level_text_is_admitted_with_verified_scope(tmp_path, terminal_period):
    _, inventory, packet, _, scope, _ = _fixture(tmp_path, terminal_period=terminal_period)

    result = build_writer_task_scope_v1(
        packet,
        _catalog(),
        source_inventory=inventory,
        candidate_output_scope=_verified_candidate_output_scope(scope),
    )

    assert result["usable_for_provider_admission"] is True
    assert result["tasks"][0]["status"] == "ready"


@pytest.mark.parametrize("mutation", ["changed_text", "foreign_claim", "foreign_evidence", "foreign_slot", "foreign_inventory", "explicit_study"])
def test_unproved_or_foreign_mentions_remain_blocked(tmp_path, mutation):
    _, inventory, packet, row, scope, _ = _fixture(tmp_path)
    scope_binding = scope
    if mutation == "changed_text":
        changed = SOURCE_TEXT + " and Study 3"
        packet["planned_claims"] = [changed]
        row["claim"] = changed
    elif mutation == "foreign_claim":
        row["primary_claim_id"] = "claim:foreign"
    elif mutation == "foreign_evidence":
        row["evidence_ids"] = ["evidence:foreign"]
    elif mutation == "foreign_slot":
        row["claim_slot_id"] = "slot:foreign"
    elif mutation == "foreign_inventory":
        scope_binding = CandidateOutputScopeV1(
            registry_job_id=scope.registry_job_id,
            source_inventory_artifact_id=scope.source_inventory_artifact_id,
            source_inventory_artifact_hash="0" * 64,
            source_inventory_content_hash=scope.source_inventory_content_hash,
            task_ids=scope.task_ids,
            selected_relation_ids=scope.selected_relation_ids,
            claim_groups=scope.claim_groups,
            claim_slots=scope.claim_slots,
        )
    elif mutation == "explicit_study":
        row["study_id"] = "study:explicit"

    result = build_writer_task_scope_v1(
        packet,
        _catalog(),
        source_inventory=inventory,
        candidate_output_scope=_verified_candidate_output_scope(scope_binding),
    )

    assert result["usable_for_provider_admission"] is False
    assert "scoped_claim_missing_source_field_identity" in result["tasks"][0]["reason_codes"]


def test_scope_without_registry_verification_and_unresolved_qualifiers_stay_fail_closed(tmp_path):
    _, inventory, packet, _, scope, _ = _fixture(tmp_path)
    unverified = build_writer_task_scope_v1(
        packet,
        _catalog(),
        source_inventory=inventory,
        candidate_output_scope=scope,
    )
    assert unverified["usable_for_provider_admission"] is False
    assert "scoped_claim_missing_source_field_identity" in unverified["tasks"][0]["reason_codes"]

    _, unresolved_inventory, unresolved_packet, _, _ = _registry_with_inventory(tmp_path / "unresolved")
    unresolved_packet["claim_support"][0]["source_field_ids"] = []
    unresolved = build_writer_task_scope_v1(unresolved_packet, _catalog(), source_inventory=unresolved_inventory)
    assert unresolved["usable_for_provider_admission"] is False
    assert "missing_interpretation_qualifier_field" in unresolved["tasks"][0]["reason_codes"]


def test_service_issues_scope_proof_only_from_registered_adopted_lineage(tmp_path):
    service, _registry, inventory, packet, final_payload, _scope_record = _registered_lineage(tmp_path)

    authority = service._verified_writer_candidate_scope(
        outline_payload=final_payload,
        evidence_packets=[packet],
        source_inventory=inventory,
    )

    assert type(authority) is VerifiedCandidateOutputScopeV1
    assert authority.is_verified is True
    result = build_writer_task_scope_v1(
        packet,
        _catalog(),
        source_inventory=inventory,
        candidate_output_scope=authority,
    )
    assert result["usable_for_provider_admission"] is True


@pytest.mark.parametrize(
    ("mutation", "input_change"),
    [
        ("missing_scope", ""),
        ("stale_scope", ""),
        ("foreign_inventory_binding", ""),
        ("", "packet"),
        ("wrong_adoption_key", ""),
    ],
)
def test_service_refuses_missing_stale_or_mismatched_lineage(tmp_path, mutation, input_change):
    service, registry, inventory, packet, final_payload, scope_record = _registered_lineage(
        tmp_path,
        mutation=mutation if mutation == "foreign_inventory_binding" else "wrong_adoption_key" if mutation == "wrong_adoption_key" else "",
    )
    if mutation == "missing_scope":
        registry.update_record(scope_record.artifact_id, status="quarantined")
    elif mutation == "stale_scope":
        Path(scope_record.path).write_text("{}", encoding="utf-8")
    packets = [dict(packet)]
    if input_change == "packet":
        packets[0]["planned_claims"] = ["Different packet claim."]

    authority = service._verified_writer_candidate_scope(
        outline_payload=final_payload,
        evidence_packets=packets,
        source_inventory=inventory,
    )

    assert authority is None
