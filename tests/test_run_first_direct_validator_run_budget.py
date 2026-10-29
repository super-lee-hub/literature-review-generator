from __future__ import annotations

import hashlib
import json
import time
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from runtime.control_plane import ControlPlaneError, ReviewControlPlane
from runtime.job_spec import RuntimeJobSpec, RuntimeSourceSpec
from runtime.provider_routes import build_reachable_provider_route_plan
from runtime.provider_run_envelope import ProviderRunEnvelopeV1
from runtime.provider_runtime import (
    ProviderAggregateBudgetV2,
    ProviderBudgetController,
    ProviderBudgetV1,
    ProviderRuntime,
    ProviderRuntimeLedger,
    RuntimeControlIdentityV1,
    bind_provider_run_budget,
    bind_runtime_control_context,
    build_provider_run_scope_hashes_v1,
    current_runtime_control_context,
    provider_budget_controller_from_environment,
    provider_run_budget_controller_from_environment,
)
from runtime.stage_planning import build_stage_plan
from services.artifact_registry import ArtifactDependencyRefV2, ArtifactRegistry
from services.job_workspace import JobWorkspace

_API_CONFIG = {
    "api_key": "fixture-only",
    "model": "validator-fixture",
    "api_base": "https://validator.example.test/v1",
    "provider_family": "openai",
    "endpoint_type": "chat_completions",
}


def _budget(*, calls: int = 8, wall: float = 600) -> ProviderAggregateBudgetV2:
    return ProviderAggregateBudgetV2(
        max_provider_calls_total=calls,
        max_output_tokens_total=20_000,
        max_retry_attempts_total=2,
        max_wall_seconds=wall,
    )


def _clear_acceptance_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "AUTO_GENERATE_ACCEPTANCE_BUDGET_JSON",
        "AUTO_GENERATE_ACCEPTANCE_BUDGET_STATE_PATH",
        "AUTO_GENERATE_ACCEPTANCE_CONTEXT_JSON",
        "AUTO_GENERATE_ACCEPTANCE_RUN_ID",
    ):
        monkeypatch.delenv(name, raising=False)


def _provider_runtime(
    controller: ProviderBudgetController | None,
    ledger_path: Path,
    *,
    call_id: str,
) -> ProviderRuntime:
    kwargs = {"aggregate_budget": controller} if controller is not None else {}
    return ProviderRuntime(
        budget=ProviderBudgetV1(
            max_calls=10,
            max_total_tokens=5_000,
            max_elapsed_seconds=300,
            max_retries_per_call=2,
        ),
        ledger=ProviderRuntimeLedger(ledger_path),
        job_id="job-direct-validator",
        attempt_id="attempt-direct-validator",
        stage_name="validate",
        route="Validator_API",
        node_id=call_id,
        call_id=call_id,
        endpoint_type="chat_completions",
        test_only=True,
        **kwargs,
    )


def _provider_call(
    controller: ProviderBudgetController,
    ledger_path: Path,
    *,
    call_id: str,
    workspace: JobWorkspace,
    registry: ArtifactRegistry,
    output_tokens: int = 50,
    result_status: str = "success",
    requested_retry_attempts: int = 0,
    attempts: int = 1,
) -> Any:
    identity = RuntimeControlIdentityV1(
        job_id=workspace.job_id,
        workspace_path=workspace.root_dir,
        acceptance_run_id=f"run:{workspace.job_id}",
        provider_budget_started=True,
    )
    with bind_runtime_control_context(identity, workspace=workspace, registry=registry):
        runtime = _provider_runtime(controller, ledger_path, call_id=call_id)
        admission = runtime.admit(
            estimated_tokens=20,
            requested_output_tokens=output_tokens,
            requested_retry_attempts=requested_retry_attempts,
        )
        runtime.mark_transport_started(admission)
        result: dict[str, Any] = {
            "status": result_status,
            "content": {"answer": "local fixture"},
            "attempts": attempts,
        }
        if result_status == "success":
            result.update(
                {
                    "input_tokens": 10,
                    "output_tokens": output_tokens // 2,
                    "total_tokens": 10 + output_tokens // 2,
                }
            )
        return runtime.complete(
            admission=admission,
            prompt=f"prompt:{call_id}",
            input_payload={"call": call_id},
            api_config=_API_CONFIG,
            result=result,
        )


def _run_fixture(
    tmp_path: Path,
    *,
    budget: ProviderAggregateBudgetV2 | None = None,
    seed_usage: bool = True,
    include_envelope: bool = True,
) -> dict[str, Any]:
    run_budget = budget or _budget()
    job_id = "job-direct-validator"
    workspace = JobWorkspace.create(str(tmp_path / "output"), "direct-validator", job_id)
    source_dir = tmp_path / "papers"
    source_dir.mkdir()
    config_path = tmp_path / "config.ini"
    config_path.write_text("[local fixture]\nvalue = safe\n", encoding="utf-8")
    spec = RuntimeJobSpec(
        project_name="direct-validator",
        source=RuntimeSourceSpec(mode="direct", pdf_folder=str(source_dir)),
        job_id=job_id,
        config=str(config_path),
        action="validate_review",
        workspace_path=workspace.root_dir,
        queue_file=str(tmp_path / "queue.json"),
        metadata={
            "requested_stages": ["validate"],
            "validation_required": True,
            "provider_budget": run_budget.to_dict(),
        },
    )
    stage_plan = build_stage_plan(
        action=spec.action,
        requested_stages=["validate"],
        validation_enabled=True,
        validation_required=True,
    )
    config = {"Validator_API": dict(_API_CONFIG)}
    route_plan = build_reachable_provider_route_plan(
        config,
        action=spec.action,
        requested_stages=stage_plan.requested_stages,
        stage_plan=stage_plan,
    )
    config_hash = hashlib.sha256(b"effective validator config").hexdigest()
    source_hash = hashlib.sha256(b"prepared source snapshot").hexdigest()
    scope_hashes = build_provider_run_scope_hashes_v1(
        normalized_runtime_spec=spec.to_dict(),
        effective_config_sha256=config_hash,
        source_snapshot_sha256=source_hash,
        review_goal=spec.metadata.get("review_intent") or {},
        route_plan=route_plan,
        stage_work_contract=stage_plan.to_dict(),
    )

    registry = ArtifactRegistry(workspace.paths.registry_path, job_id)
    runtime_spec_path = Path(workspace.artifact_path("runtime_job_spec_v1.json"))
    runtime_spec_path.write_text(
        json.dumps(spec.to_dict(), ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    runtime_spec_record = registry.register_file(
        artifact_role="runtime_job_spec",
        artifact_type="runtime_job_spec",
        artifact_version="v1",
        path=runtime_spec_path,
        producer="test_run_first_direct_validator_run_budget",
        artifact_id="runtime_job_spec",
    )
    source_inventory_path = Path(workspace.artifact_path("source_inventory_v1.json"))
    source_inventory_path.write_text(
        json.dumps({"schema_version": "source-inventory/v1", "files": []}),
        encoding="utf-8",
    )
    source_inventory_record = registry.register_file(
        artifact_role="source_inventory",
        artifact_type="source_inventory",
        artifact_version="v1",
        path=source_inventory_path,
        producer="test_run_first_direct_validator_run_budget",
        artifact_id="source_inventory",
    )

    state_path = Path(workspace.log_path("acceptance_budget_state_v1.json"))
    controller = ProviderBudgetController(run_budget)
    controller.bind_state_path(
        state_path,
        acceptance_run_id=f"run:{job_id}",
        state_started=False,
    )
    first_receipt = None
    if seed_usage and run_budget.max_provider_calls_total > 0:
        first_receipt = _provider_call(
            controller,
            Path(workspace.log_path("seed_provider_receipts.jsonl")),
            call_id="seed-call",
            workspace=workspace,
            registry=registry,
            output_tokens=80,
            result_status="outcome_unknown",
            requested_retry_attempts=1,
            attempts=2,
        )

    envelope_path: Path | None = None
    if include_envelope:
        envelope = ProviderRunEnvelopeV1(
            job_id=job_id,
            run_id=f"run:{job_id}",
            budget_authority="explicit_runtime_spec",
            provider_budget=run_budget,
            scope_hashes=scope_hashes,
            projection_identity_hash=hashlib.sha256(b"fixture projection").hexdigest(),
            admission_status="ready_with_jit",
            known_lower_bounds={
                "provider_calls": 0,
                "output_tokens": 0,
                "retry_attempts": 0,
            },
            conditional_work_upper_bounds={
                "logical_calls_upper_bound": 1,
                "physical_attempts_upper_bound": 3,
                "output_tokens_all_attempts": 300,
                "wall_seconds_upper_bound": 180,
                "unknown_exposure_count": 0,
            },
        )
        envelope_path = Path(workspace.artifact_path("provider_run_envelope_v1.json"))
        envelope_path.write_text(
            json.dumps(envelope.to_dict(), ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
        registry.register_file(
            artifact_role="provider_run_envelope",
            artifact_type="provider_run_envelope",
            artifact_version="v1",
            path=envelope_path,
            producer="runtime.runner.AgentRuntimeRunner",
            artifact_id="provider_run_envelope",
            depends_on=[
                ArtifactDependencyRefV2.from_record(runtime_spec_record),
                ArtifactDependencyRefV2.from_record(source_inventory_record),
            ],
            metadata={
                "job_id": job_id,
                "envelope_sha256": envelope.to_dict()["envelope_sha256"],
            },
        )

    context = SimpleNamespace(
        workspace=workspace,
        registry=registry,
        required_stages=stage_plan.required_stages,
        fingerprint_bundle={"config_hash": config_hash, "source_hash": source_hash},
        readiness_policy_snapshot={"stage_plan": stage_plan.to_dict()},
        source_inventory={"files": []},
    )
    session = SimpleNamespace(context=context, stage_host=SimpleNamespace(config=config))
    return {
        "control": ReviewControlPlane(repo_root=Path(__file__).resolve().parents[1]),
        "spec": spec,
        "session": session,
        "workspace": workspace,
        "registry": registry,
        "controller": controller,
        "state_path": state_path,
        "envelope_path": envelope_path,
        "first_receipt": first_receipt,
    }


def _state_snapshot(path: Path) -> bytes:
    return path.read_bytes()


@pytest.mark.parametrize("prebound_controller", (False, True))
def test_direct_validator_reuses_existing_ordinary_v2_run_and_runtime_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    prebound_controller: bool,
) -> None:
    _clear_acceptance_environment(monkeypatch)
    fixture = _run_fixture(tmp_path)
    state_path = fixture["state_path"]
    before = json.loads(state_path.read_text(encoding="utf-8"))
    deadline_before = before["absolute_deadline_epoch"]
    assert before["calls_used"] == 2
    assert before["retry_attempts_used"] == 1
    assert before["output_tokens_used"] == 80

    control = fixture["control"]
    spec = fixture["spec"]
    session = fixture["session"]
    existing_controller = fixture["controller"]
    provider_binding = (
        bind_provider_run_budget(existing_controller)
        if prebound_controller
        else nullcontext()
    )
    with provider_binding, control._direct_validator_provider_context(
        spec,
        session,
        operation="external validation",
        validator_route_fingerprint="route-fingerprint-fixture",
    ) as bound_controller:
        assert bound_controller is not None
        assert bound_controller.budget == ProviderAggregateBudgetV2.from_mapping(
            spec.metadata["provider_budget"]
        )
        assert bound_controller.state_path == state_path.resolve()
        if prebound_controller:
            assert bound_controller is existing_controller
        runtime = _provider_runtime(
            None,
            Path(session.context.workspace.log_path("direct_validation_receipts.jsonl")),
            call_id="direct-validation-call",
        )
        assert runtime.aggregate_budget is bound_controller
        admission = runtime.admit(
            estimated_tokens=20,
            requested_output_tokens=120,
            requested_retry_attempts=0,
        )
        runtime.mark_transport_started(admission)
        receipt = runtime.complete(
            admission=admission,
            prompt="prompt:direct-validation-call",
            input_payload={"call": "direct-validation-call"},
            api_config=_API_CONFIG,
            result={
                "status": "success",
                "content": {"answer": "local fixture"},
                "attempts": 1,
                "input_tokens": 10,
                "output_tokens": 60,
                "total_tokens": 70,
            },
        )
        assert receipt.status == "success"

    after = json.loads(state_path.read_text(encoding="utf-8"))
    assert after["absolute_deadline_epoch"] == deadline_before
    assert after["acceptance_run_id"] == f"run:{spec.job_id}"
    assert after["calls_used"] == 3
    assert after["output_tokens_used"] == 140
    assert after["retry_attempts_used"] == 1
    assert fixture["first_receipt"].metadata["local_budget_usage_status"] == "unknown"
    assert current_runtime_control_context() is None
    assert provider_run_budget_controller_from_environment() is None
    assert provider_budget_controller_from_environment() is None


@pytest.mark.parametrize(
    "failure",
    (
        "missing_envelope",
        "missing_state",
        "missing_identity",
        "budget_mismatch",
        "run_identity_mismatch",
        "pristine",
        "zero",
        "expired",
    ),
)
def test_direct_validator_rejects_unstarted_or_mismatched_run_without_ledger_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    _clear_acceptance_environment(monkeypatch)
    fixture = _run_fixture(
        tmp_path,
        budget=_budget(calls=0) if failure == "zero" else _budget(),
        seed_usage=failure not in {"pristine", "zero"},
        include_envelope=failure != "missing_envelope",
    )
    state_path: Path = fixture["state_path"]
    spec = fixture["spec"]
    if failure == "missing_state":
        state_path.unlink()
    elif failure == "missing_identity":
        state_path.with_name(state_path.name + ".identity.json").unlink()
    elif failure == "budget_mismatch":
        mismatched_budget = _budget(calls=9).to_dict()
        spec = RuntimeJobSpec(
            **{
                **spec.to_dict(),
                "metadata": {**spec.metadata, "provider_budget": mismatched_budget},
            }
        )
    elif failure == "run_identity_mismatch":
        identity_path = state_path.with_name(state_path.name + ".identity.json")
        state_payload = json.loads(state_path.read_text(encoding="utf-8"))
        identity_payload = json.loads(identity_path.read_text(encoding="utf-8"))
        state_payload["acceptance_run_id"] = "run:another-job"
        identity_payload["acceptance_run_id"] = "run:another-job"
        state_path.write_text(json.dumps(state_payload, sort_keys=True), encoding="utf-8")
        identity_path.write_text(
            json.dumps(identity_payload, sort_keys=True),
            encoding="utf-8",
        )
    elif failure == "expired":
        payload = json.loads(state_path.read_text(encoding="utf-8"))
        first_started = time.time() - 1_000
        payload["first_started_epoch"] = first_started
        payload["absolute_deadline_epoch"] = first_started + payload["budget"]["max_wall_seconds"]
        state_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")

    registry_path = Path(fixture["registry"].registry_path)
    before_state = state_path.read_bytes() if state_path.exists() else None
    before_identity = state_path.with_name(state_path.name + ".identity.json").read_bytes() if state_path.with_name(state_path.name + ".identity.json").exists() else None
    before_registry = registry_path.read_bytes()
    before_envelope = (
        fixture["envelope_path"].read_bytes()
        if fixture["envelope_path"] is not None
        else None
    )
    with pytest.raises(ControlPlaneError):
        fixture["control"]._direct_validator_provider_context(
            spec,
            fixture["session"],
            operation="external validation",
            validator_route_fingerprint="route-fingerprint-fixture",
        ).__enter__()

    assert (state_path.read_bytes() if state_path.exists() else None) == before_state
    identity_path = state_path.with_name(state_path.name + ".identity.json")
    assert (identity_path.read_bytes() if identity_path.exists() else None) == before_identity
    assert registry_path.read_bytes() == before_registry
    if fixture["envelope_path"] is not None:
        assert fixture["envelope_path"].read_bytes() == before_envelope

