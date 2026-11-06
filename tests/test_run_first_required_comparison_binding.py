from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from outline.v3_artifacts import (
    AdoptedOutline,
    ConfirmedGlobalRelationMap,
    FinalOutline,
    SectionEvidencePacketSet,
)
from runtime.job_spec import RuntimeJobSpec, RuntimeSourceSpec, save_runtime_job_spec
from runtime.provider_run_envelope import ProviderRunEnvelopeV1
from runtime.provider_runtime import ProviderAggregateBudgetV2
from services.artifact_registry import ArtifactDependencyRefV2, ArtifactRegistry
from services.job_workspace import JobWorkspace
from validation.required_comparison_binding import (
    required_comparison_key,
    resolve_required_comparison_task_bindings,
)


_PAPER_KEYS = ("paper-a", "paper-b")
_QUESTION = "Compare the reported effects within their source boundaries."
_RELATION_TYPE = "reported_magnitude_contrast"
_RELATION_ID = "relation:paper-a:paper-b"
_RELATION_TASK_ID = f"relation_comparison:{_RELATION_ID}"
_SECTION_ID = "candidate_1_relation_1"
_CLAIM = "Study A reported +4, whereas Study B reported +2 in its observed sample."
_BASIS = "a" * 64


def _write_json(path: Path, payload: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    return path


def _register_outline_artifact(
    workspace: JobWorkspace,
    registry: ArtifactRegistry,
    *,
    artifact_id: str,
    artifact_role: str,
    artifact_class: type[Any],
    payload: dict[str, Any],
    dependencies: tuple[Any, ...] = (),
) -> Any:
    dependency_hashes = {
        row.artifact_id: str(row.metadata.get("content_hash") or row.content_hash)
        for row in dependencies
    }
    artifact = artifact_class(
        job_id=registry.job_id,
        dependency_hashes=dependency_hashes,
        payload=payload,
    )
    path = _write_json(
        Path(workspace.artifact_path(f"required_comparison_binding/{artifact_id.replace(':', '_')}.json")),
        artifact.to_dict(),
    )
    return registry.register_file(
        artifact_id=artifact_id,
        artifact_role=artifact_role,
        artifact_type=artifact.artifact_type,
        artifact_version=artifact.artifact_version,
        path=path,
        producer="tests.test_run_first_required_comparison_binding",
        depends_on=[ArtifactDependencyRefV2.from_record(row) for row in dependencies],
        metadata={"content_hash": artifact.content_hash},
    )


def _support_rows() -> list[dict[str, Any]]:
    rows = []
    for paper in _PAPER_KEYS:
        rows.append({
            "claim": _CLAIM,
            "claim_slot_id": f"slot:{paper}",
            "task_id": _RELATION_TASK_ID,
            "paper_key": paper,
            "primary_claim_id": f"claim:{paper}:finding",
            "source_claim_ids": [f"claim:{paper}:finding", f"claim:{paper}:boundary"],
            "evidence_ids": [f"evidence:{paper}:finding", f"evidence:{paper}:boundary"],
            "source_field_ids": [f"field:{paper}:sample", f"field:{paper}:limit"],
            "primary_evidence_ids": [f"evidence:{paper}:finding"],
            "qualifier_source_claim_ids": [f"claim:{paper}:boundary"],
            "qualifier_evidence_ids": [f"evidence:{paper}:boundary"],
            "qualifier_source_field_ids": [f"field:{paper}:sample", f"field:{paper}:limit"],
            "study_id": "",
        })
    return rows


def _spec_payload(job_id: str, root: Path, *, with_intent: bool = True) -> RuntimeJobSpec:
    config = _write_json(root / "not-json.json", {"test": "fixture"})
    metadata: dict[str, Any] = {}
    if with_intent:
        metadata["review_intent"] = {
            "required_comparisons": [{
                "paper_keys": list(_PAPER_KEYS),
                "question": _QUESTION,
                "relation_type": _RELATION_TYPE,
            }],
        }
    return RuntimeJobSpec(
        project_name="required-comparison-binding",
        source=RuntimeSourceSpec(mode="direct", pdf_folder=str(root)),
        job_id=job_id,
        config=str(config),
        action="run_all",
        metadata=metadata,
    )


def _build_case(
    root: Path,
    *,
    with_intent: bool = True,
    relation_question: str = _QUESTION,
    packet_question: str = _QUESTION,
    outline_question: str = _QUESTION,
    relation_type: str = _RELATION_TYPE,
    include_outline: bool = True,
    include_adoption: bool = True,
    table: bool = False,
) -> dict[str, Any]:
    job_id = "required-comparison-binding-job"
    workspace = JobWorkspace.create(str(root / "output"), "required-comparison-binding", job_id)
    registry = ArtifactRegistry(workspace.paths.registry_path, job_id)

    spec = _spec_payload(job_id, root, with_intent=with_intent)
    spec_path = Path(workspace.artifact_path("runtime_job_spec_v1.json"))
    save_runtime_job_spec(spec_path, spec)
    registry.register_file(
        artifact_id="runtime_job_spec",
        artifact_role="runtime_spec",
        artifact_type="runtime_job_spec",
        artifact_version="v1",
        path=spec_path,
        producer="tests.test_run_first_required_comparison_binding",
    )

    support = _support_rows()
    relation_map = _register_outline_artifact(
        workspace,
        registry,
        artifact_id="outline-v3:global_relation_map",
        artifact_role="outline_v3_node_output",
        artifact_class=ConfirmedGlobalRelationMap,
        payload={
            "relations": [{
                "relation_id": _RELATION_ID,
                "relation_type": relation_type,
                "paper_keys": list(_PAPER_KEYS),
                "source_paper_key": _PAPER_KEYS[0],
                "target_paper_key": _PAPER_KEYS[1],
                "diagnostics": ["required_user_comparison"],
            }],
            "rejected_relations": [],
            "deferred_relations": [],
            "relation_decisions": [],
            "confirmed_relation_ids": [_RELATION_ID],
            "rejected_relation_ids": [],
            "deferred_relation_ids": [],
            "paper_keys": list(_PAPER_KEYS),
            "source_artifact_hashes": {},
            "blocking_diagnostics": [],
        },
    )
    packet_record = _register_outline_artifact(
        workspace,
        registry,
        artifact_id="outline-v3:section_evidence_packets",
        artifact_role="outline_v3_node_output",
        artifact_class=SectionEvidencePacketSet,
        payload={
            "packets": [{
                "section_id": _SECTION_ID,
                "section_goal": packet_question,
                "planned_claims": [_CLAIM],
                "claim_support": support,
                "paper_keys": list(_PAPER_KEYS),
                "relation_ids": [_RELATION_ID],
                "source_summary_hashes": ["1" * 64, "2" * 64],
            }],
            "coverage_ledger": {},
            "semantic_chunk_plan_hash": "3" * 64,
            "selected_candidate_revision_hash": "4" * 64,
        },
        dependencies=(relation_map,),
    )
    outline_payload = {
        "title": "A/B comparison",
        "sections": [{
            "section_id": _SECTION_ID,
            "title": "Comparison",
            "goal": outline_question,
            "task_ids": [_RELATION_TASK_ID],
            "paper_keys": list(_PAPER_KEYS),
            "relation_ids": [_RELATION_ID],
            "claims": [_CLAIM],
            "claim_support": support,
        }],
        "candidate_id": "candidate_1",
        "paper_keys": list(_PAPER_KEYS),
        "relation_ids": [_RELATION_ID],
        "source_hashes": {},
    }
    outline_record = None
    if include_outline:
        outline_record = _register_outline_artifact(
            workspace,
            registry,
            artifact_id="outline-v3:final_outline",
            artifact_role="outline_v3_node_output",
            artifact_class=FinalOutline,
            payload=outline_payload,
            dependencies=(packet_record,),
        )

    adoption_record = None
    adoption_id = "outline-v3:adoption:named"
    if include_adoption and outline_record is not None:
        adoption_payload = {
            "status": "adopted",
            "adoption_id": adoption_id,
            "adoption_identity": adoption_id,
            "current_pointer_artifact_id": "outline-v3:adoption:current",
            "current_pointer_role": "current",
            "actor": "tests",
            "reason": "fixture adoption",
            "expected_hash": outline_record.content_hash,
            "final_outline_hash": outline_record.content_hash,
            "coverage_audit_hash": "5" * 64,
            "stability_audit_hash": "6" * 64,
            "stage_health_hash": "7" * 64,
            "provider_receipt_closure_hash": "8" * 64,
            "final_outline": outline_payload,
        }
        adoption_record = _register_outline_artifact(
            workspace,
            registry,
            artifact_id=adoption_id,
            artifact_role="outline_v3_adoption",
            artifact_class=AdoptedOutline,
            payload=adoption_payload,
            dependencies=(outline_record,),
        )

    writer_task_id = "writer-task-ab"
    unit_id = "writer-unit-ab"
    basis = "a" * 64
    writer_task = {
        "writer_task_id": writer_task_id,
        "task_kind": "planned_claim",
        "planned_claim": _CLAIM,
        "status": "ready",
        "paper_keys": list(_PAPER_KEYS),
        "support_rows": support,
        "allowed_ref_ids": ["R001", "R002"],
        "output_units": [{
            "writer_output_unit_id": unit_id,
            "unit_kind": "planned_claim",
            "required": True,
            "allowed_ref_ids": ["R001", "R002"],
        }],
    }
    block = {
        "block_id": "writer-block-ab",
        "block_kind": "paragraph",
        "writer_task_id": writer_task_id,
        "writer_output_unit_id": unit_id,
        "writer_task_basis_hash": basis,
        "text": "Study A reported +4 [[cite_ref:R001]], whereas Study B reported +2 [[cite_ref:R002]].",
    }
    if table:
        block = {
            "block_id": "writer-table-ab",
            "block_kind": "table",
            "table_layout_schema_version": "writer_table_layout/v1",
            "table_id": "table-ab",
            "writer_task_basis_hash": basis,
            "headers": ["Comparison"],
            "rows": [{
                "row_id": "row-ab",
                "cells": [{
                    "cell_id": "cell-ab",
                    "block_id": "writer-block-ab",
                    "cell_kind": "factual_output_unit",
                    "writer_task_id": writer_task_id,
                    "writer_output_unit_id": unit_id,
                    "writer_task_basis_hash": basis,
                    "text": "Study A reported +4 [[cite_ref:R001]], whereas Study B reported +2 [[cite_ref:R002]].",
                }],
            }],
        }
    draft = {
        "artifact_type": "review_draft",
        "artifact_version": "v3",
        "generation_context": {
            "outline_artifact_id": "outline-v3:final_outline" if outline_record else "outline-v3:missing",
            "outline_artifact_hash": outline_record.content_hash if outline_record else "9" * 64,
            "adoption_artifact_id": adoption_id if adoption_record else "outline-v3:adoption:missing",
            "adoption_artifact_hash": adoption_record.content_hash if adoption_record else "0" * 64,
        },
        "content": {"sections": [{
            "section_id": _SECTION_ID,
            "section_title": "Comparison",
            "evidence_packet_id": _SECTION_ID,
            "writer_task_scope": {
                "schema_version": "writer_task_scope/v1",
                "scope_status": "ready",
                "usable_for_provider_admission": True,
                "section_id": _SECTION_ID,
                "writer_task_basis_hash": basis,
                "task_count": 1,
                "required_task_ids": [writer_task_id],
                "tasks": [writer_task],
            },
            "writer_task_dispositions": [{
                "writer_task_id": writer_task_id,
                "writer_task_basis_hash": basis,
                "disposition": "covered",
            }],
            "blocks": [block],
        }]},
    }
    return {
        "registry": registry,
        "workspace": workspace,
        "draft": draft,
        "spec": spec,
        "outline_record": outline_record,
        "adoption_record": adoption_record,
        "packet_record": packet_record,
        "relation_record": relation_map,
        "outline_payload": outline_payload,
        "packet_support": support,
    }


def test_required_comparison_resolves_exact_outline_task_and_per_side_support(tmp_path: Path) -> None:
    fixture = _build_case(tmp_path)

    bindings, issues = resolve_required_comparison_task_bindings(
        fixture["registry"], fixture["draft"],
    )

    key = required_comparison_key({
        "paper_keys": list(_PAPER_KEYS),
        "question": _QUESTION,
        "relation_type": _RELATION_TYPE,
    })
    assert issues == []
    assert bindings[key]["task_id"] == _RELATION_TASK_ID
    assert bindings[key]["relation_id"] == _RELATION_ID
    assert bindings[key]["section_id"] == _SECTION_ID
    assert bindings[key]["claim_slot_ids"] == ["slot:paper-a", "slot:paper-b"]
    assert bindings[key]["writer_output_unit_id"] == "writer-unit-ab"
    assert bindings[key]["outline_artifact_hash"] == fixture["outline_record"].content_hash
    assert bindings[key]["adoption_artifact_hash"] == fixture["adoption_record"].content_hash


def _register_generated_unrelated_candidate_scope(
    fixture: dict[str, Any], *, source_registry_job_id: str | None = None,
) -> Any:
    from types import SimpleNamespace

    from outline.candidate_output_scope import build_candidate_output_scope_v1
    from services.writer_source_inventory import load_writer_source_inventory_v1
    from tests.test_outline_v3_semantic_execution import _summary
    from tests.writer_source_fixture import bind_production_writer_sources

    registry = fixture["registry"]
    workspace = fixture["workspace"]
    service = SimpleNamespace(
        registry=registry,
        workspace=workspace,
        summaries=[_summary("scope-paper", "Unrelated scope source", "The source finding held within its recorded context.")],
    )
    bind_production_writer_sources(service, [])
    inventory = load_writer_source_inventory_v1(registry)
    paper = inventory.paper_by_key()["scope-paper"]
    primary = next(
        claim for claim in paper.claims
        if claim.claim_type == "empirical_finding" and claim.evidence_ids
    )
    dependencies = [
        row for row in paper.interpretation_dependencies
        if row.primary_claim_id == primary.claim_id
    ]
    source_claim_ids = {primary.claim_id}
    evidence_ids = set(primary.evidence_ids)
    source_field_ids: set[str] = set()
    for dependency in dependencies:
        source_claim_ids.update(dependency.required_source_claim_ids)
        evidence_ids.update(dependency.required_evidence_ids)
        source_field_ids.update(dependency.required_source_field_ids)
    claim_by_id = {claim.claim_id: claim for claim in paper.claims}
    for claim_id in source_claim_ids:
        evidence_ids.update(claim_by_id[claim_id].evidence_ids)
    route = {
        "topic_id": "topic:unrelated-scope",
        "logical_node_id": "topic_synthesis:unrelated-scope",
        "paper_ids": [paper.paper_key],
        "fragments": [{
            "fragment_id": "topic:unrelated-scope:fragment:1",
            "paper_ids": [paper.paper_key],
            "provider_results": [{
                "result_id": "topic-result:unrelated-scope:1",
                "provider_output": {"claims": [{
                    "claim_id": "synthesis:topic_synthesis:unrelated-scope:claim-1",
                    "fragment_id": "topic:unrelated-scope:fragment:1",
                    "claim_type": "empirical_finding",
                    "paper_key": paper.paper_key,
                    "text": primary.text,
                    "source_claim_ids": sorted(source_claim_ids),
                    "evidence_ids": sorted(evidence_ids),
                    "source_field_ids": sorted(source_field_ids),
                    "relation_ids": [],
                }]},
            }],
        }],
    }
    scope = build_candidate_output_scope_v1(inventory, [route])
    payload = scope.to_dict()
    if source_registry_job_id is not None:
        from runtime.provider_runtime import hash_json
        payload["source_inventory_binding"]["registry_job_id"] = source_registry_job_id
        payload["content_hash"] = hash_json({key: value for key, value in payload.items() if key != "content_hash"})
    inventory_record = registry.get(inventory.artifact_id)
    assert inventory_record is not None and inventory_record.job_id == registry.job_id
    path = _write_json(Path(workspace.artifact_path("unrelated-candidate-output-scope.json")), payload)
    return registry.register_file(
        artifact_id="outline-v3:candidate_output_scope",
        artifact_role="outline_candidate_output_scope",
        artifact_type="outline_candidate_output_scope",
        artifact_version="v1",
        path=path,
        producer="tests.test_run_first_required_comparison_binding",
        depends_on=[ArtifactDependencyRefV2.from_record(inventory_record)],
        metadata={"content_hash": payload["content_hash"]},
    )


def test_named_adoption_not_latest_pointer_or_static_candidate_scope_is_authority(tmp_path: Path) -> None:
    fixture = _build_case(tmp_path)
    registry = fixture["registry"]

    # A later, unrelated adoption and scope-shaped artifact must not invalidate
    # or replace the older adoption explicitly bound by this current draft.
    other_outline_payload = {
        "title": "Unrelated outline",
        "sections": [],
        "candidate_id": "candidate-other",
        "paper_keys": ["other-a", "other-b"],
        "relation_ids": [],
        "source_hashes": {},
    }
    other_outline = _register_outline_artifact(
        fixture["workspace"], registry,
        artifact_id="outline-v3:other_final_outline",
        artifact_role="outline_v3_node_output",
        artifact_class=FinalOutline,
        payload=other_outline_payload,
    )
    other_adoption_id = "outline-v3:adoption:other"
    other_adoption = _register_outline_artifact(
        fixture["workspace"], registry,
        artifact_id=other_adoption_id,
        artifact_role="outline_v3_adoption",
        artifact_class=AdoptedOutline,
        payload={
            "status": "adopted",
            "adoption_id": other_adoption_id,
            "adoption_identity": other_adoption_id,
            "current_pointer_artifact_id": "outline-v3:adoption:current",
            "current_pointer_role": "current",
            "actor": "tests",
            "reason": "unrelated later outline",
            "expected_hash": other_outline.content_hash,
            "final_outline_hash": other_outline.content_hash,
            "coverage_audit_hash": "1" * 64,
            "stability_audit_hash": "2" * 64,
            "stage_health_hash": "3" * 64,
            "provider_receipt_closure_hash": "4" * 64,
            "final_outline": other_outline_payload,
        },
        dependencies=(other_outline,),
    )
    pointer_path = _write_json(
        Path(fixture["workspace"].artifact_path("outline_v3_adoption_current.json")),
        {
            "artifact_type": "outline_adoption_pointer",
            "artifact_version": "v1",
            "job_id": registry.job_id,
            "role": "current",
            "current_adoption_artifact_id": other_adoption_id,
            "current_adoption_hash": other_adoption.content_hash,
            "adoption_identity": other_adoption_id,
            "updated_at": "later",
        },
    )
    registry.register_file(
        artifact_id="outline-v3:adoption:current",
        artifact_role="outline_v3_adoption_current",
        artifact_type="outline_adoption_pointer",
        artifact_version="v1",
        path=pointer_path,
        producer="tests",
        depends_on=[ArtifactDependencyRefV2.from_record(other_adoption)],
    )
    _register_generated_unrelated_candidate_scope(fixture)

    bindings, issues = resolve_required_comparison_task_bindings(registry, fixture["draft"])
    key = required_comparison_key({
        "paper_keys": list(_PAPER_KEYS), "question": _QUESTION, "relation_type": _RELATION_TYPE,
    })
    assert issues == []
    assert bindings[key]["adoption_artifact_id"] == fixture["adoption_record"].artifact_id
    assert bindings[key]["outline_artifact_id"] == fixture["outline_record"].artifact_id


def test_candidate_scope_rejects_foreign_source_inventory_job(tmp_path: Path) -> None:
    from services.artifact_registry import UnverifiedArtifact

    fixture = _build_case(tmp_path)
    with pytest.raises(UnverifiedArtifact, match="candidate scope belongs to another job"):
        _register_generated_unrelated_candidate_scope(
            fixture, source_registry_job_id="foreign-job",
        )


@pytest.mark.parametrize("mismatch", ["question", "relation_type"])
def test_required_comparison_requires_the_durable_question_and_relation_type(
    tmp_path: Path, mismatch: str,
) -> None:
    fixture = _build_case(
        tmp_path,
        packet_question="A different question." if mismatch == "question" else _QUESTION,
        outline_question="A different question." if mismatch == "question" else _QUESTION,
        relation_type="studies_same_construct" if mismatch == "relation_type" else _RELATION_TYPE,
    )

    bindings, issues = resolve_required_comparison_task_bindings(
        fixture["registry"], fixture["draft"],
    )

    assert bindings == {}
    assert issues == ["required_comparison_task_binding_missing:paper-a|paper-b"]


def test_required_comparison_rejects_missing_or_stale_named_outline_and_adoption(
    tmp_path: Path,
) -> None:
    fixture = _build_case(tmp_path)
    fixture["draft"]["generation_context"]["outline_artifact_hash"] = "0" * 64
    bindings, issues = resolve_required_comparison_task_bindings(
        fixture["registry"], fixture["draft"],
    )
    assert bindings == {}
    assert "named final Outline is missing or stale" in issues[0]

    fixture = _build_case(tmp_path / "missing-adoption", include_adoption=False)
    bindings, issues = resolve_required_comparison_task_bindings(
        fixture["registry"], fixture["draft"],
    )
    assert bindings == {}
    assert "named adoption is missing or stale" in issues[0]


def test_required_comparison_rejects_foreign_outline_record(tmp_path: Path) -> None:
    fixture = _build_case(tmp_path)
    registry = fixture["registry"]
    record = registry.get("outline-v3:final_outline")
    assert record is not None
    registry._artifacts[record.artifact_id] = replace(record, job_id="foreign-job")

    bindings, issues = resolve_required_comparison_task_bindings(registry, fixture["draft"])

    assert bindings == {}
    assert "named final Outline is missing or stale" in issues[0]


def test_required_comparison_rejects_tampered_spec_but_legacy_no_intent_is_not_gated(
    tmp_path: Path,
) -> None:
    fixture = _build_case(tmp_path)
    spec_record = fixture["registry"].get("runtime_job_spec")
    assert spec_record is not None
    Path(spec_record.path).write_bytes(Path(spec_record.path).read_bytes() + b"\n")
    bindings, issues = resolve_required_comparison_task_bindings(
        fixture["registry"], fixture["draft"],
    )
    assert bindings == {}
    assert "required_comparison_intent_untrusted" in issues[0]

    legacy = _build_case(tmp_path / "legacy", with_intent=False, include_outline=False, include_adoption=False)
    bindings, issues = resolve_required_comparison_task_bindings(legacy["registry"], legacy["draft"])
    assert bindings == {}
    assert issues == []


def test_required_comparison_fails_closed_when_a_current_run_lost_its_spec(
    tmp_path: Path,
) -> None:
    workspace = JobWorkspace.create(str(tmp_path / "output"), "missing-spec", "missing-spec-job")
    registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    envelope = ProviderRunEnvelopeV1(
        job_id=workspace.job_id,
        run_id=f"run:{workspace.job_id}",
        budget_authority="documented_default",
        provider_budget=ProviderAggregateBudgetV2(
            max_provider_calls_total=1,
            max_output_tokens_total=100,
            max_retry_attempts_total=0,
            max_wall_seconds=60,
        ),
        scope_hashes={
            name: "a" * 64
            for name in (
                "runtime_spec_sha256", "effective_config_sha256", "source_snapshot_sha256",
                "review_goal_sha256", "route_plan_sha256", "stage_work_contract_sha256",
            )
        },
        projection_identity_hash="b" * 64,
        admission_status="ready_with_jit",
        known_lower_bounds={"provider_calls": 0, "output_tokens": 0, "retry_attempts": 0},
        conditional_work_upper_bounds={
            "logical_calls_upper_bound": 1,
            "physical_attempts_upper_bound": 1,
            "output_tokens_all_attempts": 100,
            "wall_seconds_upper_bound": 60,
            "unknown_exposure_count": 0,
        },
    )
    envelope_path = _write_json(
        Path(workspace.artifact_path("provider_run_envelope_v1.json")),
        envelope.to_dict(),
    )
    registry.register_file(
        artifact_id="provider_run_envelope",
        artifact_role="provider_run_envelope",
        artifact_type="provider_run_envelope",
        artifact_version="v1",
        path=envelope_path,
        producer="tests",
        metadata={"job_id": workspace.job_id, "envelope_sha256": envelope.to_dict()["envelope_sha256"]},
    )
    bindings, issues = resolve_required_comparison_task_bindings(registry, {"artifact_type": "review_draft", "artifact_version": "v3"})
    assert bindings == {}
    assert issues == ["required_comparison_intent_untrusted:current runtime spec is missing"]


def test_required_comparison_resolves_native_table_factual_cell(tmp_path: Path) -> None:
    fixture = _build_case(tmp_path, table=True)

    bindings, issues = resolve_required_comparison_task_bindings(
        fixture["registry"], fixture["draft"],
    )

    key = required_comparison_key({
        "paper_keys": list(_PAPER_KEYS), "question": _QUESTION, "relation_type": _RELATION_TYPE,
    })
    assert issues == []
    assert bindings[key]["writer_output_unit_id"] == "writer-unit-ab"
