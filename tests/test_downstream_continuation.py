"""Additional-only continuation contracts against disposable Registry graphs."""

from __future__ import annotations

import configparser
import json
import time
from dataclasses import replace
from pathlib import Path

import pytest

import runtime.downstream_continuation as continuation
from outline.v3_artifacts import AdoptedOutline, FinalOutline, SectionEvidencePacketSet
from runtime.attempt_store import AttemptAlreadyRunningError, AttemptExecutionLease
from runtime.downstream_continuation import (
    ALLOW_PROVIDER_STAGES,
    CURRENT_ID,
    DownstreamContinuationError,
    bind_current_continuation,
    controller_for_continuation,
    current_continuation,
    load_current_continuation,
    prepare_downstream_continuation,
    validate_continuation_artifact,
    validate_continuation_controller,
    validate_current_continuation_controller,
)
from runtime.job_spec import RuntimeJobSpec, RuntimeSourceSpec
from runtime.provider_run_envelope import ProviderRunEnvelopeV1
from runtime.provider_runtime import ProviderAggregateBudgetV2, hash_json
from services.artifact_registry import (
    ArtifactDependencyRefV2,
    ArtifactRegistry,
    file_sha256,
)
from services.configuration_service import default_config_sections
from services.job_workspace import JobWorkspace, atomic_write_json
from services.source_inventory import SourceInventoryV1

APPROVAL = {
    "actor": "researcher",
    "reason": "finish adopted review and validation",
    "authorization_reference": "human:continue-the-remaining-stages",
}


def _case(tmp_path: Path, *, host: str = "http://127.0.0.1:2737/v1"):
    workspace = JobWorkspace.create(
        str(tmp_path / "out"), "continuation", job_id="job-1"
    )
    registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    config = configparser.ConfigParser(interpolation=None)
    config.read_dict(default_config_sections())
    for section in ("Writer_API", "Validator_API"):
        config[section].update(
            api_base=host,
            api_key="local-fixture",
            model="controlled-model",
            endpoint_type="chat_completions",
        )
    config["Validation"]["review_enabled"] = "true"
    config_path = tmp_path / "config.ini"
    with config_path.open("w", encoding="utf-8") as handle:
        config.write(handle)
    pdfs = tmp_path / "papers"
    pdfs.mkdir()
    parent_budget = ProviderAggregateBudgetV2(1407, 78000000, 0, 1)
    spec = RuntimeJobSpec(
        project_name="continuation",
        source=RuntimeSourceSpec(mode="direct", pdf_folder=str(pdfs)),
        job_id=workspace.job_id,
        config=str(config_path),
        workspace_path=workspace.root_dir,
        metadata={"provider_budget": parent_budget.to_dict()},
    ).to_dict()

    def register(
        artifact_id,
        artifact_type,
        payload,
        *,
        version="v1",
        role=None,
        refs=(),
        metadata=None,
    ):
        path = Path(workspace.artifact_path(artifact_id.replace(":", "_") + ".json"))
        atomic_write_json(path, payload)
        return registry.register_file(
            path=path,
            artifact_id=artifact_id,
            artifact_type=artifact_type,
            artifact_version=version,
            artifact_role=role or artifact_type,
            producer="tests",
            depends_on=list(refs),
            metadata=metadata,
        )

    config_hash = file_sha256(config_path)
    spec_record = register(
        "runtime_job_spec",
        "runtime_job_spec",
        spec,
        metadata={
            "config_snapshot_binding": {
                "schema_version": "runtime-config-source/v1",
                "config_source_id": str(config_path),
                "config_source_sha256": config_hash,
                "effective_config_sha256": config_hash,
                "normalized_spec_payload_sha256": hash_json(spec),
            }
        },
    )
    inventory = register(
        "source_inventory",
        "source_inventory",
        SourceInventoryV1("direct", "continuation", (), ()).to_dict(),
    )
    hashes = {
        "runtime_spec_sha256": hash_json(spec),
        "effective_config_sha256": config_hash,
        "source_snapshot_sha256": hash_json("source"),
        "review_goal_sha256": hash_json({}),
        "route_plan_sha256": hash_json("routes"),
        "stage_work_contract_sha256": hash_json("stages"),
    }
    envelope = ProviderRunEnvelopeV1(
        job_id=workspace.job_id,
        run_id=f"run:{workspace.job_id}",
        budget_authority="explicit_runtime_spec",
        provider_budget=parent_budget,
        scope_hashes=hashes,
        projection_identity_hash=hash_json("projection"),
        admission_status="ready_with_jit",
        known_lower_bounds={},
        conditional_work_upper_bounds={},
    ).to_dict()
    register(
        "provider_run_envelope",
        "provider_run_envelope",
        envelope,
        refs=[ArtifactDependencyRefV2.from_record(r) for r in (spec_record, inventory)],
        metadata={
            "job_id": workspace.job_id,
            "envelope_sha256": envelope["envelope_sha256"],
        },
    )
    final = FinalOutline(
        job_id=workspace.job_id,
        payload={
            "title": "Review",
            "sections": [{"section_id": "s1"}],
            "paper_keys": [],
            "source_hashes": {},
        },
    )
    final_record = register(
        "outline-v3:final_outline", "final_outline", final.to_dict(), version="v3"
    )
    packets = SectionEvidencePacketSet(
        job_id=workspace.job_id, payload={"packets": [], "coverage_ledger": {}}
    )
    register(
        "outline-v3:section_evidence_packets",
        "section_evidence_packet_set",
        packets.to_dict(),
        version="v3",
    )
    adopted_id = "outline-v3:adoption:one"
    adopted = AdoptedOutline(
        job_id=workspace.job_id,
        payload={
            "status": "adopted",
            "adoption_id": "one",
            "adoption_identity": adopted_id,
            "current_pointer_artifact_id": "outline-v3:adoption:current",
            "current_pointer_role": "current",
            "actor": "researcher",
            "reason": "explicit adoption",
            "expected_hash": final_record.content_hash,
            "final_outline_hash": final_record.content_hash,
            "coverage_audit_hash": "a" * 64,
            "stability_audit_hash": "b" * 64,
            "stage_health_hash": "c" * 64,
            "provider_receipt_closure_hash": "d" * 64,
            "final_outline": final.payload,
        },
    )
    adopted_record = register(
        adopted_id,
        "adopted_outline",
        adopted.to_dict(),
        version="v3",
        refs=[ArtifactDependencyRefV2.from_record(final_record)],
    )
    register(
        "outline-v3:adoption:current",
        "outline_adoption_pointer",
        {
            "artifact_type": "outline_adoption_pointer",
            "artifact_version": "v1",
            "job_id": workspace.job_id,
            "role": "current",
            "current_adoption_artifact_id": adopted_id,
            "current_adoption_hash": adopted_record.content_hash,
            "adoption_identity": adopted_id,
            "updated_at": "2026-10-25T00:00:00Z",
        },
        role="outline_v3_adoption_current",
        refs=[ArtifactDependencyRefV2.from_record(adopted_record)],
        metadata={
            "role": "current",
            "current_adoption_artifact_id": adopted_id,
            "current_adoption_hash": adopted_record.content_hash,
        },
    )
    old = Path(workspace.log_path("acceptance_budget_state_v1.json"))
    started = time.time() - 100
    atomic_write_json(
        old,
        {
            "schema_version": "provider-aggregate-budget-v5",
            "budget": parent_budget.to_dict(),
            "acceptance_run_id": f"run:{workspace.job_id}",
            "calls_used": 605,
            "output_tokens_used": 634586,
            "retry_attempts_used": 0,
            "calls_reserved": 0,
            "output_tokens_reserved": 0,
            "retry_attempts_reserved": 0,
            "reservations": [],
            "first_started_epoch": started,
            "absolute_deadline_epoch": started + 1,
            "owner_id": "historical",
            "primary_repair_scopes": {},
        },
    )
    identity = old.with_name(old.name + ".identity.json")
    atomic_write_json(
        identity,
        {
            "schema_version": "provider-aggregate-budget-identity-v1",
            "state_path": str(old),
            "acceptance_run_id": f"run:{workspace.job_id}",
            "created_at": "2026-10-25T00:00:00Z",
        },
    )
    return workspace, registry, spec, old, identity, register


def _prepare(case, **overrides):
    workspace, registry, spec, *_ = case
    return prepare_downstream_continuation(
        workspace,
        registry,
        budget=ProviderAggregateBudgetV2(802, 77365414, 0, 7200),
        spec_payload=spec,
        **{**APPROVAL, **overrides},
    )


def test_additional_only_preserves_parent_and_combines_usage_once(tmp_path):
    case = _case(tmp_path)
    workspace, registry, spec, old, identity, _ = case
    original = old.read_bytes(), identity.read_bytes()
    assert load_current_continuation(workspace, registry) is None
    grant = _prepare(case)
    assert (old.read_bytes(), identity.read_bytes()) == original
    assert dict(grant.carried_usage) == {
        "calls_used": 605,
        "output_tokens_used": 634586,
        "retry_attempts_used": 0,
    }
    controller = controller_for_continuation(grant)
    assert controller.snapshot()["calls_used"] == 0
    assert grant.overall_usage(controller.snapshot())["calls_used"] == 605
    state = json.loads(grant.state_path.read_text())
    state.update(calls_used=2, output_tokens_used=3)
    atomic_write_json(grant.state_path, state)
    reloaded = controller_for_continuation(
        load_current_continuation(workspace, registry, spec)
    )
    assert reloaded.snapshot()["calls_used"] == 2
    assert grant.overall_usage(reloaded.snapshot()) == {
        "calls_used": 607,
        "output_tokens_used": 634589,
        "retry_attempts_used": 0,
    }
    assert _prepare(case).run_id == grant.run_id
    assert (old.read_bytes(), identity.read_bytes()) == original
    assert current_continuation() is None
    with pytest.raises(RuntimeError), bind_current_continuation(grant):
        assert validate_current_continuation_controller(
            reloaded, runtime_spec_budget=spec["metadata"]["provider_budget"]
        )
        raise RuntimeError("restore context")
    assert current_continuation() is None
    assert not validate_current_continuation_controller(reloaded)


@pytest.mark.parametrize(
    "host", ["http://127.0.0.1:9/v1", "http://localhost:9/v1", "http://[::1]:9/v1"]
)
def test_loopback_hosts_with_ports_are_accepted(tmp_path, host):
    _prepare(_case(tmp_path, host=host))


@pytest.mark.parametrize(
    "fault",
    [
        "remote",
        "live_parent",
        "reservation",
        "budget_parent",
        "identity",
        "over_calls",
        "over_output",
        "retry",
        "active_lease",
    ],
)
def test_prepare_rejects_unapproved_exposure_and_active_parent(tmp_path, fault):
    case = _case(
        tmp_path,
        host="https://example.org/v1" if fault == "remote" else "http://127.0.0.1:9/v1",
    )
    workspace, registry, spec, old, identity, _ = case
    state = json.loads(old.read_text())
    budget = ProviderAggregateBudgetV2(802, 77365414, 0, 7200)
    if fault == "live_parent":
        state["first_started_epoch"] = time.time()
        state["absolute_deadline_epoch"] = state["first_started_epoch"] + 1
    elif fault == "reservation":
        state["calls_reserved"] = 1
    elif fault == "budget_parent":
        state["budget"]["max_provider_calls_total"] += 1
    elif fault == "identity":
        payload = json.loads(identity.read_text())
        payload["acceptance_run_id"] = "foreign"
        atomic_write_json(identity, payload)
    elif fault == "over_calls":
        budget = ProviderAggregateBudgetV2(803, 77365414, 0, 7200)
    elif fault == "over_output":
        budget = ProviderAggregateBudgetV2(802, 77365415, 0, 7200)
    elif fault == "retry":
        budget = ProviderAggregateBudgetV2(802, 77365414, 1, 7200)
    atomic_write_json(old, state)
    if fault == "active_lease":
        with (
            AttemptExecutionLease(workspace),
            pytest.raises(AttemptAlreadyRunningError),
        ):
            _prepare(case)
    else:
        with pytest.raises(DownstreamContinuationError):
            prepare_downstream_continuation(
                workspace, registry, budget=budget, spec_payload=spec, **APPROVAL
            )
    assert registry.get(CURRENT_ID) is None


@pytest.mark.parametrize(
    "fault",
    [
        "old_bytes",
        "new_identity",
        "grant_bytes",
        "adoption",
        "source",
        "config",
        "scope",
        "expired",
    ],
)
def test_load_fails_closed_on_drift_or_expired_grant(tmp_path, fault, monkeypatch):
    case = _case(tmp_path)
    workspace, registry, spec, old, _identity, _register = case
    grant = _prepare(case)
    if fault == "old_bytes":
        old.write_bytes(old.read_bytes() + b" ")
    elif fault == "new_identity":
        grant.state_path.with_name(grant.state_path.name + ".identity.json").write_text(
            "{}"
        )
    elif fault == "grant_bytes":
        Path(grant.artifact_record.path).write_text("{}")
    elif fault in {"adoption", "source"}:
        artifact_id = (
            "outline-v3:adoption:current" if fault == "adoption" else "source_inventory"
        )
        record = registry.get(artifact_id)
        record.metadata["changed"] = True
        registry.register_file(
            path=record.path,
            artifact_id=record.artifact_id,
            artifact_role=record.artifact_role,
            artifact_type=record.artifact_type,
            artifact_version=record.artifact_version,
            producer=record.producer,
            depends_on=record.depends_on,
            metadata=record.metadata,
        )
    elif fault == "config":
        Path(spec["config"]).write_text("[changed]\nvalue=1")
    elif fault == "expired":
        monkeypatch.setattr(
            "runtime.downstream_continuation.time.time",
            lambda: grant.absolute_deadline_epoch + 1,
        )
    with pytest.raises((DownstreamContinuationError, ValueError)):
        load_current_continuation(
            workspace,
            registry,
            spec,
            expected_scope_hashes={} if fault == "scope" else None,
        )


def test_head_cannot_fork_and_verified_fields_cannot_be_forged(tmp_path):
    case = _case(tmp_path)
    _workspace, registry, *_ = case
    grant = _prepare(case)
    with pytest.raises(DownstreamContinuationError):
        _prepare(case, authorization_reference="second authorization")
    with pytest.raises(DownstreamContinuationError):
        replace(grant, job_id="foreign")
    with pytest.raises(DownstreamContinuationError):
        replace(grant, allow_provider_stages=("stage1_analyze",))
    with pytest.raises(DownstreamContinuationError):
        validate_continuation_controller(
            grant,
            controller_for_continuation(grant),
            runtime_spec_budget=ProviderAggregateBudgetV2(1, 2, 0, 3),
        )
    foreign = JobWorkspace.create(str(tmp_path / "other"), "other", job_id="foreign")
    with pytest.raises(DownstreamContinuationError):
        load_current_continuation(foreign, registry)
    assert grant.allow_provider_stages == ALLOW_PROVIDER_STAGES
    validate_continuation_artifact(
        grant.artifact_record, Path(grant.artifact_record.path)
    )


@pytest.mark.parametrize(
    "change",
    [
        "used_record",
        "current_head",
        "typed_edge",
        "source_bytes",
        "old_bytes",
        "config_bytes",
        "unrelated_append",
    ],
)
def test_load_fences_late_used_changes_but_allows_unrelated_append(
    tmp_path, monkeypatch, change
):
    case = _case(tmp_path)
    workspace, registry, spec, old, _identity, register = case
    grant = _prepare(case)
    identity_path = grant.state_path.with_name(grant.state_path.name + ".identity.json")
    read_object = continuation._object
    fired = False

    def read_then_change(path):
        nonlocal fired
        payload = read_object(path)
        if path != identity_path or fired:
            return payload
        fired = True
        if change in {"used_record", "current_head", "typed_edge"}:
            # Keep the revision unchanged: the fence must compare full used records.
            registry_payload = read_object(Path(registry.registry_path))
            target = (
                CURRENT_ID
                if change == "current_head"
                else "outline-v3:adoption:current"
            )
            row = next(
                r for r in registry_payload["artifacts"] if r["artifact_id"] == target
            )
            if change == "typed_edge":
                row["depends_on"][0]["artifact_id"] = "another-adoption"
            else:
                row["metadata"]["late_change"] = True
            atomic_write_json(registry.registry_path, registry_payload)
        elif change == "source_bytes":
            record = registry.get("outline-v3:final_outline")
            Path(record.path).write_bytes(Path(record.path).read_bytes() + b" ")
        elif change == "old_bytes":
            old.write_bytes(old.read_bytes() + b" ")
        elif change == "config_bytes":
            config_path = Path(spec["config"])
            config_path.write_bytes(config_path.read_bytes() + b"\n")
        else:
            register("unrelated-ready", "unrelated_test", {"value": 1})
        return payload

    monkeypatch.setattr(continuation, "_object", read_then_change)
    if change == "unrelated_append":
        assert (
            load_current_continuation(workspace, registry, spec).run_id == grant.run_id
        )
    else:
        with pytest.raises(DownstreamContinuationError, match="changed during load"):
            load_current_continuation(workspace, registry, spec)
    assert fired
