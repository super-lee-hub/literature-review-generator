from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from runtime.arbitration_work_policy import (
    ALLOWED_KINDS,
    POLICY_TYPE,
    publish_arbitration_surface,
    verify_arbitration_surface,
)
from runtime.provider_run_envelope import ProviderRunEnvelopeV1
from runtime.provider_runtime import ProviderAggregateBudgetV2, hash_json
from services.artifact_registry import ArtifactDependencyRefV2, UnverifiedArtifact
from services.job_workspace import JobWorkspace, publish_json_artifact
from services.queue_service import LocalPublicationContext


def _case(
    tmp_path: Path,
    *,
    envelope_type: str = "provider_run_envelope",
    envelope_version: str = "v1",
    envelope_role: str = "provider_run_envelope",
    spec_type: str = "runtime_job_spec",
    spec_version: str = "v1",
    spec_role: str = "runtime_spec",
    source_type: str = "source_inventory",
    source_version: str = "v1",
    source_role: str = "source_inventory",
) -> dict[str, Any]:
    workspace = JobWorkspace.create(str(tmp_path / "output"), "policy-test", "policy-job")
    publication = LocalPublicationContext()
    registry = publication.registry(workspace.paths.registry_path, workspace.job_id)
    spec = publish_json_artifact(
        publication,
        registry,
        workspace.artifact_path("runtime_job_spec.json"),
        {"artifact_type": "runtime_job_spec", "artifact_version": "v1", "job_id": workspace.job_id},
        artifact_id="runtime_job_spec",
        artifact_role=spec_role,
        artifact_type=spec_type,
        artifact_version=spec_version,
        producer=__name__,
    )
    source = publish_json_artifact(
        publication,
        registry,
        workspace.artifact_path("source_inventory.json"),
        {"artifact_type": "source_inventory", "artifact_version": "v1", "job_id": workspace.job_id},
        artifact_id="source_inventory",
        artifact_role=source_role,
        artifact_type=source_type,
        artifact_version=source_version,
        producer=__name__,
    )
    route_plan = {
        "schema_version": "reachable-provider-route-plan/v1",
        "routes": [{
            "stage": "outline",
            "semantic_role": "arbitration",
            "enabled": True,
            "resolved": True,
            "section_name": "Outline_API",
        }],
    }
    scope_hashes = {
        "runtime_spec_sha256": hash_json({"spec": workspace.job_id}),
        "effective_config_sha256": hash_json({"config": workspace.job_id}),
        "source_snapshot_sha256": hash_json({"source": workspace.job_id}),
        "review_goal_sha256": hash_json({"goal": workspace.job_id}),
        "route_plan_sha256": hash_json(route_plan),
        "stage_work_contract_sha256": hash_json({"stages": ["outline"]}),
    }
    envelope = ProviderRunEnvelopeV1(
        job_id=workspace.job_id,
        run_id=f"run:{workspace.job_id}",
        budget_authority="explicit_runtime_spec",
        provider_budget=ProviderAggregateBudgetV2(
            max_provider_calls_total=20,
            max_output_tokens_total=100_000,
            max_retry_attempts_total=0,
            max_wall_seconds=600,
        ),
        scope_hashes=scope_hashes,
        projection_identity_hash=hash_json({"projection": workspace.job_id}),
        admission_status="ready_with_jit",
        known_lower_bounds={"provider_calls": 1, "output_tokens": 1, "retry_attempts": 0},
        conditional_work_upper_bounds={"logical_calls_upper_bound": None},
    )
    envelope_record = publish_json_artifact(
        publication,
        registry,
        workspace.artifact_path("provider_run_envelope.json"),
        envelope.to_dict(),
        artifact_id="provider_run_envelope_v1",
        artifact_role=envelope_role,
        artifact_type=envelope_type,
        artifact_version=envelope_version,
        producer=__name__,
        depends_on=[ArtifactDependencyRefV2.from_record(spec), ArtifactDependencyRefV2.from_record(source)],
        metadata={
            "job_id": workspace.job_id,
            "envelope_sha256": envelope.to_dict()["envelope_sha256"],
        },
    )
    return {
        "workspace": workspace,
        "publication": publication,
        "registry": registry,
        "spec": spec,
        "source": source,
        "envelope_record": envelope_record,
        "route_plan": route_plan,
    }


def _publish_surface(case: dict[str, Any], *, existing_run_envelope: bool = False,
                     stage1_work_item_basis: dict[str, Any] | None = None):
    return publish_arbitration_surface(
        registry=case["registry"],
        workspace=case["workspace"],
        publication_context=case["publication"],
        envelope_record=case["envelope_record"],
        route_plan=case["route_plan"],
        candidate_count=5,
        stage1_work_item_basis=stage1_work_item_basis,
        existing_run_envelope=existing_run_envelope,
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("envelope_type", "wrong_envelope"),
        ("envelope_role", "wrong_role"),
    ],
)
def test_publish_requires_the_current_v1_run_envelope_record(tmp_path, field, value):
    case = _case(tmp_path, **{field: value})

    with pytest.raises(ValueError, match="run envelope is not the expected current READY local artifact"):
        _publish_surface(case)


def test_registry_rejects_unregistered_run_envelope_version_before_policy_publish(tmp_path):
    with pytest.raises(UnverifiedArtifact, match="no version-aware current validator"):
        _case(tmp_path, envelope_version="v2")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("spec_type", "wrong_spec"),
        ("spec_version", "v2"),
        ("spec_role", "wrong_role"),
        ("source_type", "wrong_source"),
        ("source_version", "v2"),
        ("source_role", "wrong_role"),
    ],
)
def test_publish_requires_current_ready_spec_and_source_owner_records(tmp_path, field, value):
    case = _case(tmp_path, **{field: value})

    with pytest.raises(ValueError):
        _publish_surface(case)


@pytest.mark.parametrize(
    ("artifact_type", "artifact_version", "artifact_role"),
    [
        ("wrong_type", "v1", POLICY_TYPE),
        (POLICY_TYPE, "v2", POLICY_TYPE),
        (POLICY_TYPE, "v1", "wrong_role"),
    ],
)
def test_verify_rejects_non_v1_or_wrong_type_policy_records(
    tmp_path, artifact_type, artifact_version, artifact_role,
):
    case = _case(tmp_path)
    surface = _publish_surface(case)
    assert surface is not None
    payload = json.loads(Path(surface.path).read_text(encoding="utf-8"))
    wrong = publish_json_artifact(
        case["publication"],
        case["registry"],
        case["workspace"].artifact_path(f"{artifact_version}-{artifact_type}-{artifact_role}.json"),
        payload,
        artifact_id=f"wrong-surface-{artifact_version}-{artifact_type}-{artifact_role}",
        artifact_role=artifact_role,
        artifact_type=artifact_type,
        artifact_version=artifact_version,
        producer=__name__,
        depends_on=list(surface.depends_on),
    )

    with pytest.raises(ValueError, match="arbitration surface is not the expected current READY local artifact"):
        verify_arbitration_surface(case["registry"], wrong)


def test_existing_v1_envelope_remains_deferred_without_extending_budget(tmp_path):
    case = _case(tmp_path)

    surface = _publish_surface(case, existing_run_envelope=True)

    assert surface is not None
    body = verify_arbitration_surface(case["registry"], surface)
    assert body["legacy_request_measurement"] == "retain_existing_v1"
    assert body["count_status"] == "deferred_until_verified_source_and_candidate_work_is_READY"
    assert body["new_transport_ready"] is False
    assert body["budget_or_deadline_extension"] is False
    assert body["allowed_kinds"] == sorted(ALLOWED_KINDS)


def test_resume_preserves_persisted_basis_and_legacy_measurement_when_bootstrap_basis_is_absent(tmp_path):
    case = _case(tmp_path)
    basis = {
        "schema_version": "stage1-paper-work-items/v1",
        "paper_work_item_count": 3,
        "paper_work_items_sha256": hash_json({"papers": [1, 2, 3]}),
    }
    original = _publish_surface(case, stage1_work_item_basis=basis)
    assert original is not None

    resumed = _publish_surface(case, existing_run_envelope=True, stage1_work_item_basis=None)

    assert resumed == original
    body = verify_arbitration_surface(case["registry"], resumed)
    assert body["source_work_item_basis"] == basis
    assert body["legacy_request_measurement"] == "full_formal_text_wire_v1"


@pytest.mark.parametrize(
    "basis",
    [
        {"schema_version": "stage1-paper-work-items/v2", "paper_work_item_count": 1,
         "paper_work_items_sha256": "a" * 64},
        {"schema_version": "stage1-paper-work-items/v1", "paper_work_item_count": True,
         "paper_work_items_sha256": "a" * 64},
        {"schema_version": "stage1-paper-work-items/v1", "paper_work_item_count": 1,
         "paper_work_items_sha256": "not-a-digest"},
    ],
)
def test_publish_rejects_malformed_source_work_item_basis(tmp_path, basis):
    case = _case(tmp_path)

    with pytest.raises(ValueError, match="Stage 1 work-item basis"):
        _publish_surface(case, stage1_work_item_basis=basis)
