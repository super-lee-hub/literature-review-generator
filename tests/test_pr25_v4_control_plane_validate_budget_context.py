from __future__ import annotations

import configparser
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

import pytest

from config_loader import load_config
from runtime.control_plane import ControlPlaneError, ReviewControlPlane
from runtime.job_spec import RuntimeJobSpec, RuntimeSourceSpec
from runtime.orchestrator import AgentRuntimeBridge
from runtime.provider_routes import build_reachable_provider_route_plan
from runtime.provider_runtime import (
    AcceptanceExecutionContextV1,
    ProviderAggregateBudgetV1,
    ProviderBudgetController,
    bind_acceptance_execution_context,
)
from runtime.trust_admission import (
    acknowledgement_from_values,
    build_external_host_policy,
    validate_external_host_acknowledgement,
)
from tests import test_current_validation_repair_e2e as repair_fixture
from tests.test_current_runtime_full_e2e import _adjudicator_response


class _StopAfterValidate(Exception):
    def __init__(self, result: Mapping[str, Any]) -> None:
        self.result = dict(result)


def _acceptance_binding(tmp_path: Path) -> tuple[
    AcceptanceExecutionContextV1,
    ProviderBudgetController,
]:
    budget = ProviderAggregateBudgetV1(
        max_provider_calls_total=8,
        max_output_tokens_total=32_768,
        max_retry_attempts_total=8,
        max_wall_seconds=300,
    )
    context = AcceptanceExecutionContextV1(
        acceptance_run_id="standalone-validate-budget-control",
        final_executable_sha="d" * 40,
        absolute_deadline_epoch=(
            datetime.now(timezone.utc) + timedelta(minutes=5)
        ).timestamp(),
        provider_budget=budget,
        provider_budget_state_path=str(tmp_path / "accepted-budget-state.json"),
        evidence_root=str(tmp_path / "accepted-evidence"),
        process_event_log=str(tmp_path / "accepted-process-events.jsonl"),
        scenario_state_path=str(tmp_path / "accepted-scenario-state.json"),
        owner_authorized=True,
        provider_budget_state_started=False,
    )
    controller = ProviderBudgetController(budget)
    controller.bind_state_path(
        context.provider_budget_state_path,
        acceptance_run_id=context.acceptance_run_id,
        state_started=False,
    )
    return replace(context, provider_budget_state_started=True), controller


def _exercise_public_validate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    stage_scope: str,
    isolate_public_test_dependencies: bool = True,
    tamper_stage_host_route: bool = False,
    acceptance_binding: tuple[
        AcceptanceExecutionContextV1,
        ProviderBudgetController,
    ] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    for variable in (
        "AUTO_GENERATE_ACCEPTANCE_BUDGET_JSON",
        "AUTO_GENERATE_ACCEPTANCE_BUDGET_STATE_PATH",
        "AUTO_GENERATE_ACCEPTANCE_CONTEXT_JSON",
        "AUTO_GENERATE_ACCEPTANCE_RUN_ID",
    ):
        monkeypatch.delenv(variable, raising=False)

    original_spec_type = repair_fixture.RuntimeJobSpec
    original_control_plane = repair_fixture.ReviewControlPlane
    observed_calls: list[dict[str, Any]] = []
    route_scope: dict[str, Any] = {}

    def external_validator_spec(*args: Any, **kwargs: Any) -> Any:
        config_path = Path(str(kwargs["config"]))
        parser = configparser.ConfigParser(interpolation=None)
        parser.read(config_path, encoding="utf-8")
        validator = parser["Validator_API"]
        validator["api_key"] = "local-fake-validator-credential"
        validator["model"] = "validator-test"
        validator["api_base"] = "https://validator.example.test/v1"
        validator["provider_family"] = "deepseek"
        validator["endpoint_type"] = "chat_completions"
        validator["transport_retries"] = "0"
        parser["Preprocess"]["parser_mode"] = "local"
        parser["Preprocess"]["primary_parser"] = "local"
        parser["Preprocess"]["fallback_parser"] = "local"

        action = str(kwargs["action"])
        metadata = dict(kwargs.get("metadata") or {})
        if stage_scope == "review_only":
            action = "generate_review"
            metadata.update(
                {
                    "requested_stages": ["review"],
                    "validation_required": False,
                    "require_clean_validation": False,
                    "allow_unvalidated_when_validation_optional": True,
                }
            )
            kwargs["action"] = action
            # Keep the only route admitted by the saved review-only plan on an
            # official host. Validator remains external but out of that plan.
            parser["Writer_API"]["api_base"] = "https://api.openai.com/v1"

        requested_stages = metadata.get("requested_stages")
        with config_path.open("w", encoding="utf-8") as handle:
            parser.write(handle)
        normalized = load_config(
            str(config_path),
            action=action,
            requested_stages=requested_stages,
            free_mode_enabled=False,
        )
        route_plan = build_reachable_provider_route_plan(
            normalized,
            action=action,
            requested_stages=requested_stages,
        )
        policy = build_external_host_policy(normalized, route_plan)
        validator_in_plan = any(
            route.stage == "validate" and route.semantic_role == "validator"
            for route in route_plan.routes
        )
        if stage_scope == "validate":
            if not validator_in_plan:
                raise AssertionError("validate fixture omitted its Validator route")
            if policy.required_hosts != ("validator.example.test",):
                raise AssertionError(
                    f"unexpected host acknowledgement scope: {policy.required_hosts}"
                )
            acknowledgement = acknowledgement_from_values(
                policy,
                acknowledged=True,
                hosts=policy.required_hosts,
            )
            validated = validate_external_host_acknowledgement(
                policy,
                acknowledgement,
            )
            metadata["external_host_acknowledgement"] = acknowledgement
            route_scope.update(validated)
        else:
            if validator_in_plan:
                raise AssertionError("review-only saved stage unexpectedly includes Validator")
            if policy.required_hosts:
                raise AssertionError(
                    f"review-only acknowledgement scope is not empty: {policy.required_hosts}"
                )
            route_scope.update(
                {
                    "acknowledged": False,
                    "required_hosts": [],
                    "validator_in_saved_route_plan": False,
                }
            )
        kwargs["metadata"] = metadata
        return original_spec_type(*args, **kwargs)

    class CapturingControlPlane(original_control_plane):
        def repair_promote(self, **kwargs: Any) -> dict[str, Any]:
            # Reuse the durable repair fixture, but cross the public validate
            # endpoint and stop as soon as it returns.
            with monkeypatch.context() as isolated:
                if isolate_public_test_dependencies:
                    isolated.setattr(
                        "runtime.test_dependencies.current_runtime_test_dependencies",
                        lambda: None,
                    )
                if acceptance_binding is not None:
                    isolated.setattr(
                        "runtime.control_plane.read_checkout_sha",
                        lambda _root, *, require_clean: "d" * 40,
                    )
                if tamper_stage_host_route:
                    class ChangedRuntimeBridge(AgentRuntimeBridge):
                        def bootstrap(self, **bootstrap_kwargs: Any):
                            session = super().bootstrap(**bootstrap_kwargs)
                            session.stage_host.config["Validator_API"]["api_base"] = (
                                "https://changed.example.test/v1"
                            )
                            return session

                    isolated.setattr(
                        "runtime.control_plane.AgentRuntimeBridge",
                        ChangedRuntimeBridge,
                    )
                result = super().validate(workspace=kwargs["workspace"])
            raise _StopAfterValidate(result)

    def fake_adjudicator_call(
        _prompt: str,
        api_config: Mapping[str, Any],
        _system_prompt: str,
        **kwargs: Any,
    ) -> Mapping[str, Any]:
        runtime = kwargs.get("provider_runtime")
        observed_calls.append(
            {
                "host": "validator.example.test"
                if "validator.example.test" in str(api_config.get("api_base") or "")
                else "unexpected-route",
                "aggregate_controller_bound": bool(
                    getattr(runtime, "aggregate_budget", None)
                ),
                "local_budget": (
                    runtime.budget.to_dict()
                    if runtime is not None and hasattr(runtime, "budget")
                    else {}
                ),
            }
        )
        return _adjudicator_response()

    monkeypatch.setattr(repair_fixture, "RuntimeJobSpec", external_validator_spec)
    monkeypatch.setattr(repair_fixture, "ReviewControlPlane", CapturingControlPlane)
    monkeypatch.setattr(
        "validation.llm_adjudicator._call_ai_api",
        fake_adjudicator_call,
    )

    def run_fixture() -> dict[str, Any]:
        try:
            repair_fixture.test_current_control_plane_revalidates_and_promotes_quarantined_repair(
                tmp_path,
                monkeypatch,
            )
        except _StopAfterValidate as stopped:
            return stopped.result
        raise AssertionError("repair fixture did not reach the public validate boundary")

    if acceptance_binding is None:
        result = run_fixture()
    else:
        context, controller = acceptance_binding
        with bind_acceptance_execution_context(context, controller):
            result = run_fixture()
    return result, observed_calls, route_scope


def test_public_validate_blocks_external_validator_without_acceptance_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, calls, route_scope = _exercise_public_validate(
        tmp_path,
        monkeypatch,
        stage_scope="validate",
    )

    assert route_scope["acknowledged"] is True
    assert route_scope["required_hosts"] == ["validator.example.test"]
    assert calls == [], (
        "standalone validate reached the acknowledged external-shaped Validator "
        "without acceptance budget: "
        f"calls={calls}, public_status={result.get('status')!r}, "
        f"stage_success={(result.get('stage_result') or {}).get('success')!r}"
    )
    assert result["status"] == "blocked"


def test_in_process_test_adapter_does_not_exempt_active_validator_from_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, calls, _route_scope = _exercise_public_validate(
        tmp_path,
        monkeypatch,
        stage_scope="validate",
        isolate_public_test_dependencies=False,
    )

    assert calls == []
    assert result["status"] == "blocked"
    assert "aggregate acceptance run" in result["reason"]


def test_public_validate_bound_acceptance_context_control(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, calls, route_scope = _exercise_public_validate(
        tmp_path,
        monkeypatch,
        stage_scope="validate",
        acceptance_binding=_acceptance_binding(tmp_path),
    )

    assert route_scope["acknowledged"] is True
    assert result["stage_result"]["success"] is True
    assert result["stage_result"]["metadata"]["provider_receipt_closure_complete"] is True
    assert len(calls) == 1
    assert calls[0]["host"] == "validator.example.test"
    assert calls[0]["aggregate_controller_bound"] is True


def test_public_validate_blocks_route_change_after_bootstrap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, calls, _route_scope = _exercise_public_validate(
        tmp_path,
        monkeypatch,
        stage_scope="validate",
        acceptance_binding=_acceptance_binding(tmp_path),
        tamper_stage_host_route=True,
    )

    assert calls == []
    assert result["status"] == "blocked"
    assert "route changed after admission" in result["reason"]


def test_public_validate_rejects_validator_outside_review_only_saved_stage_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, calls, route_scope = _exercise_public_validate(
        tmp_path,
        monkeypatch,
        stage_scope="review_only",
    )

    assert route_scope["validator_in_saved_route_plan"] is False
    assert route_scope["required_hosts"] == []
    assert calls == [], (
        "public validate called Validator even though the persisted review-only "
        "stage plan omitted it: "
        f"calls={calls}, public_status={result.get('status')!r}, "
        f"stage_success={(result.get('stage_result') or {}).get('success')!r}"
    )
    assert result["status"] == "blocked"


def test_direct_validator_admission_excludes_unrun_remote_parser(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LLM_VALIDATOR_API", raising=False)
    config_path = tmp_path / "config.ini"
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(Path(__file__).resolve().parents[1] / "config.ini.example", encoding="utf-8")
    parser["Validator_API"].update({
        "api_key": "local-fake-validator-credential",
        "model": "validator-test",
        "api_base": "https://validator.example.test/v1",
        "provider_family": "deepseek",
        "endpoint_type": "chat_completions",
    })
    parser["Preprocess"].update({
        "parser_mode": "remote",
        "primary_parser": "mineru_remote",
        "mineru_base_url": "https://mineru.example.test/api/v4",
        "mineru_allowed_url_hosts": "results.example.test",
    })
    with config_path.open("w", encoding="utf-8") as handle:
        parser.write(handle)
    config = load_config(
        str(config_path), action="validate_review", requested_stages=["validate"],
        free_mode_enabled=False, allow_template_credentials=True,
    )
    route_plan = build_reachable_provider_route_plan(
        config, action="validate_review", requested_stages=["validate"],
    )
    direct_policy = build_external_host_policy(
        config, route_plan,
        provider_sections={"Validator_API"}, include_mineru=False,
    )
    full_policy = build_external_host_policy(config, route_plan)
    assert direct_policy.required_hosts == ("validator.example.test",)
    assert "mineru.example.test" in full_policy.required_hosts
    acknowledgement = acknowledgement_from_values(
        direct_policy, acknowledged=True, hosts=direct_policy.required_hosts,
    )
    spec = RuntimeJobSpec(
        project_name="direct-validation-policy",
        source=RuntimeSourceSpec(mode="direct", pdf_folder=str(tmp_path)),
        config=str(config_path), action="validate_review",
        metadata={"requested_stages": ["validate"]},
    )
    control = ReviewControlPlane(repo_root=tmp_path)
    with pytest.raises(ControlPlaneError, match="aggregate acceptance run"):
        control._admit_direct_validator_execution(
            spec,
            validator_host_acknowledgement=acknowledgement,
            operation="external validation",
        )

    context, controller = _acceptance_binding(tmp_path)
    with monkeypatch.context() as isolated:
        isolated.setattr(
            "runtime.control_plane.read_checkout_sha",
            lambda _root, *, require_clean: "d" * 40,
        )
        with bind_acceptance_execution_context(context, controller):
            fingerprint, admitted_ack = control._admit_direct_validator_execution(
                spec,
                validator_host_acknowledgement=acknowledgement,
                operation="external validation",
            )
            bridge = AgentRuntimeBridge(
                spec,
                direct_validation_route_fingerprint=fingerprint,
                direct_validation_host_acknowledgement=admitted_ack,
            )
    bridge.verify_direct_validation_route(config)
    changed_config = {section: dict(values) for section, values in config.items()}
    changed_config["Validator_API"]["api_base"] = "https://changed.example.test/v1"
    with pytest.raises(ValueError, match="route changed after admission"):
        bridge.verify_direct_validation_route(changed_config)
    parser["Validator_API"]["api_base"] = "https://changed.example.test/v1"
    with config_path.open("w", encoding="utf-8") as handle:
        parser.write(handle)
    with pytest.raises(ValueError, match="route changed after admission"):
        AgentRuntimeBridge(
            spec,
            direct_validation_route_fingerprint=fingerprint,
            direct_validation_host_acknowledgement=admitted_ack,
        )


def test_in_process_test_adapter_cannot_waive_direct_validator_host_ack(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LLM_VALIDATOR_API", raising=False)
    config_path = tmp_path / "config.ini"
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(Path(__file__).resolve().parents[1] / "config.ini.example", encoding="utf-8")
    parser["Validator_API"].update({
        "api_key": "local-fake-validator-credential",
        "model": "validator-test",
        "api_base": "https://validator.example.test/v1",
        "provider_family": "deepseek",
        "endpoint_type": "chat_completions",
    })
    parser["Preprocess"].update({
        "parser_mode": "local", "primary_parser": "local", "fallback_parser": "local",
    })
    with config_path.open("w", encoding="utf-8") as handle:
        parser.write(handle)
    spec = RuntimeJobSpec(
        project_name="direct-validation-host-policy",
        source=RuntimeSourceSpec(mode="direct", pdf_folder=str(tmp_path)),
        config=str(config_path), action="validate_review",
        metadata={"requested_stages": ["validate"]},
    )
    control = ReviewControlPlane(repo_root=tmp_path)
    with pytest.raises(ControlPlaneError, match="host admission failed"):
        control._admit_direct_validator_execution(
            spec, validator_host_acknowledgement=None,
            operation="external validation",
        )
