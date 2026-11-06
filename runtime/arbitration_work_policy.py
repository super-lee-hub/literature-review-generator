"""Persist the arbitration surface; refine counts only from READY source work.

This record is not user approval or a provider reservation. The original run
envelope owns the finite budget. A deferred policy cannot admit a POST: the
executor must publish a source-derived branch and exact per-kind work first.
"""
from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from runtime.provider_run_envelope import ProviderRunEnvelopeV1
from runtime.provider_runtime import hash_json
from services.artifact_registry import (
    ArtifactDependencyRefV2,
    ArtifactRecord,
    ArtifactRegistry,
)
from services.job_workspace import JobWorkspace, publish_json_artifact

POLICY_SCHEMA = "arbitration-runtime-surface/v1"
POLICY_TYPE = "arbitration_runtime_surface"
POLICY_VERSION = "v1"
RUN_ENVELOPE_TYPE = "provider_run_envelope"
RUN_ENVELOPE_ROLE = "provider_run_envelope"
RUNTIME_SPEC_ROLE = "runtime_spec"
SOURCE_INVENTORY_ROLE = "source_inventory"
_STAGE1_BASIS_FIELDS = frozenset({
    "schema_version", "paper_work_item_count", "paper_work_items_sha256",
})
_SURFACE_FIELDS = frozenset({
    "schema_version", "job_id", "envelope_ref", "runtime_spec_ref", "source_inventory_ref",
    "scope_hashes", "original_provider_budget", "reachable_route_plan", "arbitrator_routes",
    "configured_candidate_count", "allowed_kinds", "count_status", "new_transport_ready",
    "source_work_item_basis", "admission_rule", "budget_or_deadline_extension",
    "legacy_request_measurement", "surface_hash",
})
ALLOWED_KINDS = frozenset({
    "arbitration_leaf_assessment_v2", "arbitration_reduce_new", "arbitration_decision",
    "arbitration_selector_detail_review", "arbitration_decision_final",
    "arbitration_layout_extract", "arbitration_layout_reduce", "arbitration_section_navigation",
    "arbitration_merge_detail_review", "arbitration_detail_finalize/v2",
})


def record_ref(record: ArtifactRecord) -> dict[str, Any]:
    return {"artifact_id": record.artifact_id, "artifact_type": record.artifact_type,
            "job_id": record.job_id, "path": record.path,
            "content_hash": record.content_hash, "status": record.status}


def _require_local_record(
    registry: ArtifactRegistry,
    record: ArtifactRecord,
    *,
    artifact_type: str,
    artifact_version: str,
    artifact_role: str,
    artifact_id: str | None = None,
    label: str,
) -> None:
    if (
        not isinstance(record, ArtifactRecord)
        or registry.get(record.artifact_id) != record
        or record.job_id != registry.job_id
        or record.status != "ready"
        or (artifact_id is not None and record.artifact_id != artifact_id)
        or record.artifact_type != artifact_type
        or record.artifact_version != artifact_version
        or record.artifact_role != artifact_role
    ):
        raise ValueError(f"{label} is not the expected current READY local artifact")
    registry.verify_ready_artifact_closure(record)


def _validate_stage1_work_item_basis(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping) or set(value) != _STAGE1_BASIS_FIELDS:
        raise ValueError("arbitration surface Stage 1 work-item basis has an invalid shape")
    count = value.get("paper_work_item_count")
    digest = value.get("paper_work_items_sha256")
    if (
        value.get("schema_version") != "stage1-paper-work-items/v1"
        or isinstance(count, bool)
        or not isinstance(count, int)
        or count < 0
        or not isinstance(digest, str)
        or re.fullmatch(r"[0-9a-f]{64}", digest) is None
    ):
        raise ValueError("arbitration surface Stage 1 work-item basis is invalid")
    return dict(value)


def _arbitrator_routes(route_plan: Mapping[str, Any]) -> list[dict[str, Any]]:
    routes = route_plan.get("routes")
    if not isinstance(routes, list) or any(not isinstance(row, Mapping) for row in routes):
        raise ValueError("arbitration surface reachable route plan is invalid")
    return [
        dict(row) for row in routes
        if row.get("semantic_role") == "arbitration" and row.get("enabled") and row.get("resolved")
    ]


def _validate_record_ref(ref: Any, *, label: str) -> dict[str, Any]:
    expected = {"artifact_id", "artifact_type", "job_id", "path", "content_hash", "status"}
    if not isinstance(ref, Mapping) or set(ref) != expected:
        raise ValueError(f"arbitration surface {label} reference is malformed")
    if (
        any(not isinstance(ref.get(key), str) or not ref[key]
            for key in ("artifact_id", "artifact_type", "job_id", "path", "status"))
        or ref.get("status") != "ready"
        or not isinstance(ref.get("content_hash"), str)
        or re.fullmatch(r"[0-9a-f]{64}", ref["content_hash"]) is None
    ):
        raise ValueError(f"arbitration surface {label} reference is invalid")
    return dict(ref)


def publish_arbitration_surface(
    *, registry: ArtifactRegistry, workspace: JobWorkspace, publication_context: Any,
    envelope_record: ArtifactRecord, route_plan: Mapping[str, Any], candidate_count: int,
    stage1_work_item_basis: Mapping[str, Any] | None,
    existing_run_envelope: bool = False,
) -> ArtifactRecord | None:
    if not isinstance(route_plan, Mapping) or workspace.job_id != registry.job_id:
        raise ValueError("arbitration surface route or workspace owner is invalid")
    routes = _arbitrator_routes(route_plan)
    if not routes:
        return None
    _require_local_record(
        registry, envelope_record, artifact_type=RUN_ENVELOPE_TYPE, artifact_version="v1",
        artifact_role=RUN_ENVELOPE_ROLE, label="arbitration surface run envelope",
    )
    envelope_payload = json.loads(Path(envelope_record.path).read_bytes())
    for _ in range(3):
        if envelope_payload.get("schema_version") == ProviderRunEnvelopeV1.SCHEMA_VERSION:
            break
        if not isinstance(envelope_payload.get("payload"), Mapping):
            break
        envelope_payload = envelope_payload["payload"]
    envelope = ProviderRunEnvelopeV1.from_dict(envelope_payload)
    if envelope.job_id != registry.job_id or hash_json(route_plan) != envelope.scope_hashes["route_plan_sha256"]:
        raise ValueError("arbitration surface differs from the original run route scope")
    if type(candidate_count) is not int or not 1 <= candidate_count <= 12:
        raise ValueError("arbitration surface has an invalid configured candidate count")
    spec = registry.get("runtime_job_spec")
    source = registry.get("source_inventory")
    if spec is None or source is None or spec.status != "ready" or source.status != "ready":
        raise ValueError("arbitration surface requires the run's READY spec and source inventory")
    _require_local_record(
        registry, spec, artifact_type="runtime_job_spec", artifact_version="v1",
        artifact_role=RUNTIME_SPEC_ROLE, artifact_id="runtime_job_spec",
        label="arbitration surface runtime spec",
    )
    _require_local_record(
        registry, source, artifact_type="source_inventory", artifact_version="v1",
        artifact_role=SOURCE_INVENTORY_ROLE, artifact_id="source_inventory",
        label="arbitration surface source inventory",
    )
    normalized_stage1_basis = _validate_stage1_work_item_basis(stage1_work_item_basis)
    body = {
        "schema_version": POLICY_SCHEMA, "job_id": registry.job_id,
        "envelope_ref": record_ref(envelope_record), "runtime_spec_ref": record_ref(spec),
        "source_inventory_ref": record_ref(source), "scope_hashes": dict(envelope.scope_hashes),
        "original_provider_budget": envelope.provider_budget.to_dict(),
        "reachable_route_plan": dict(route_plan),
        "arbitrator_routes": routes, "configured_candidate_count": candidate_count,
        "allowed_kinds": sorted(ALLOWED_KINDS),
        "count_status": "deferred_until_verified_source_and_candidate_work_is_READY",
        "new_transport_ready": False,
        "source_work_item_basis": normalized_stage1_basis,
        "admission_rule": "READY_source_branch_and_exact_kind_requests_then_original_atomic_provider_admit",
        "budget_or_deadline_extension": False,
        "legacy_request_measurement": "retain_existing_v1" if existing_run_envelope else "full_formal_text_wire_v1",
    }
    artifact_id = f"arbitration-runtime-surface:{envelope_record.content_hash[:24]}"
    existing = registry.get(artifact_id)
    if existing is not None:
        _require_local_record(
            registry, existing, artifact_type=POLICY_TYPE, artifact_version=POLICY_VERSION,
            artifact_role=POLICY_TYPE, artifact_id=artifact_id,
            label="persisted arbitration surface",
        )
        verify_arbitration_surface(registry, existing)
        saved = json.loads(Path(existing.path).read_bytes())
        # Bootstrap's transient work-item basis can be absent on a resume;
        # its original value remains pinned through the source inventory.
        body["source_work_item_basis"] = saved.get("source_work_item_basis")
        body["legacy_request_measurement"] = saved.get("legacy_request_measurement")
        body["surface_hash"] = hash_json(body)
        if saved != body:
            raise ValueError("persisted arbitration surface differs from its original scope")
        return existing
    body["surface_hash"] = hash_json(body)
    return publish_json_artifact(
        publication_context, registry,
        workspace.artifact_path(f"arbitration_runtime/surface_{envelope_record.content_hash[:24]}.json"),
        body, artifact_id=artifact_id, artifact_role=POLICY_TYPE, artifact_type=POLICY_TYPE,
        artifact_version=POLICY_VERSION, producer="runtime.arbitration_work_policy.publish_arbitration_surface",
        depends_on=[ArtifactDependencyRefV2.from_record(row) for row in (envelope_record, spec, source)],
        metadata={"job_id": registry.job_id, "surface_hash": body["surface_hash"]},
    )


def verify_arbitration_surface(registry: ArtifactRegistry, record: ArtifactRecord) -> dict[str, Any]:
    _require_local_record(
        registry, record, artifact_type=POLICY_TYPE, artifact_version=POLICY_VERSION,
        artifact_role=POLICY_TYPE, label="arbitration surface",
    )
    body = json.loads(Path(record.path).read_bytes())
    if (
        not isinstance(body, Mapping)
        or set(body) != _SURFACE_FIELDS
        or body.get("schema_version") != POLICY_SCHEMA
        or body.get("job_id") != registry.job_id
        or body.get("allowed_kinds") != sorted(ALLOWED_KINDS)
        or type(body.get("configured_candidate_count")) is not int
        or not 1 <= body["configured_candidate_count"] <= 12
        or body.get("count_status") != "deferred_until_verified_source_and_candidate_work_is_READY"
        or body.get("new_transport_ready") is not False
        or body.get("admission_rule") != "READY_source_branch_and_exact_kind_requests_then_original_atomic_provider_admit"
        or body.get("budget_or_deadline_extension") is not False
        or body.get("legacy_request_measurement") not in {"retain_existing_v1", "full_formal_text_wire_v1"}
        or not isinstance(body.get("scope_hashes"), Mapping)
        or not isinstance(body.get("original_provider_budget"), Mapping)
        or not isinstance(body.get("reachable_route_plan"), Mapping)
        or not isinstance(body.get("arbitrator_routes"), list)
        or body.get("surface_hash") != hash_json({k: v for k, v in body.items() if k != "surface_hash"})
    ):
        raise ValueError("arbitration surface content is invalid")

    ref_contracts = (
        ("envelope_ref", RUN_ENVELOPE_TYPE, "v1", RUN_ENVELOPE_ROLE, None),
        ("runtime_spec_ref", "runtime_job_spec", "v1", RUNTIME_SPEC_ROLE, "runtime_job_spec"),
        ("source_inventory_ref", "source_inventory", "v1", SOURCE_INVENTORY_ROLE, "source_inventory"),
    )
    dependencies = set(record.depends_on)
    resolved: dict[str, ArtifactRecord] = {}
    for name, artifact_type, artifact_version, artifact_role, artifact_id in ref_contracts:
        ref = _validate_record_ref(body[name], label=name)
        if ref["job_id"] != registry.job_id:
            raise ValueError(f"arbitration surface {name} is owned by another job")
        target = registry.get(ref["artifact_id"])
        if target is None:
            raise ValueError(f"arbitration surface {name} dependency is missing")
        _require_local_record(
            registry, target, artifact_type=artifact_type, artifact_version=artifact_version,
            artifact_role=artifact_role, artifact_id=artifact_id, label=f"arbitration surface {name}",
        )
        if record_ref(target) != ref:
            raise ValueError(f"arbitration surface {name} reference differs from its Registry record")
        if ArtifactDependencyRefV2.from_record(target) not in record.depends_on:
            raise ValueError("arbitration surface is missing its declared dependency")
        resolved[name] = target
    if dependencies != {ArtifactDependencyRefV2.from_record(row) for row in resolved.values()}:
        raise ValueError("arbitration surface has an undeclared or foreign dependency")

    envelope_record = resolved["envelope_ref"]
    envelope = ProviderRunEnvelopeV1.from_dict(json.loads(Path(envelope_record.path).read_bytes()))
    route_plan = body["reachable_route_plan"]
    if (
        envelope.job_id != registry.job_id
        or body["scope_hashes"] != dict(envelope.scope_hashes)
        or body["original_provider_budget"] != envelope.provider_budget.to_dict()
        or hash_json(route_plan) != envelope.scope_hashes["route_plan_sha256"]
        or _arbitrator_routes(route_plan) != body["arbitrator_routes"]
    ):
        raise ValueError("arbitration surface changed the original run budget or scope")
    _validate_stage1_work_item_basis(body.get("source_work_item_basis"))
    return dict(body)
