from __future__ import annotations

import configparser
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

import pytest

from config_loader import load_config
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


class _StopAfterRepairPromote(Exception):
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
        acceptance_run_id="repair-promote-budget-control",
        final_executable_sha="e" * 40,
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


def _exercise_repair_promote(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    acceptance_binding: tuple[
        AcceptanceExecutionContextV1,
        ProviderBudgetController,
    ] | None,
    current_source_sha: str = "e" * 40,
    validator_api_base: str = "https://validator.example.test/v1",
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
    host_acknowledgement: dict[str, Any] = {}

    def external_validator_spec(*args: Any, **kwargs: Any) -> Any:
        config_path = Path(str(kwargs["config"]))
        parser = configparser.ConfigParser(interpolation=None)
        parser.read(config_path, encoding="utf-8")
        validator = parser["Validator_API"]
        validator["api_key"] = "local-fake-validator-credential"
        validator["model"] = "validator-test"
        validator["api_base"] = validator_api_base
        validator["provider_family"] = "deepseek"
        validator["endpoint_type"] = "chat_completions"
        validator["transport_retries"] = "0"
        parser["Preprocess"]["parser_mode"] = "local"
        parser["Preprocess"]["primary_parser"] = "local"
        parser["Preprocess"]["fallback_parser"] = "local"
        with config_path.open("w", encoding="utf-8") as handle:
            parser.write(handle)

        action = str(kwargs["action"])
        metadata = dict(kwargs.get("metadata") or {})
        requested_stages = metadata.get("requested_stages")
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
        acknowledgement = acknowledgement_from_values(
            policy,
            acknowledged=True,
            hosts=policy.required_hosts,
        )
        validated = validate_external_host_acknowledgement(policy, acknowledgement)
        expected_hosts = (
            () if urlsplit(validator_api_base).hostname in {"api.deepseek.com", "127.0.0.1"}
            else ("validator.example.test",)
        )
        if policy.required_hosts != expected_hosts:
            raise AssertionError(
                "fixture did not construct the intended external-shaped Validator route"
            )
        host_acknowledgement.update(validated)
        metadata["external_host_acknowledgement"] = acknowledgement
        kwargs["metadata"] = metadata
        return original_spec_type(*args, **kwargs)

    class CapturingControlPlane(original_control_plane):
        def repair_promote(self, **kwargs: Any) -> dict[str, Any]:
            # The fixture bootstrap uses the in-process test adapter for its
            # placeholder Reader/Writer routes. The public repair command must
            # be exercised without that adapter so the active external
            # Validator host and aggregate-budget gates cannot be bypassed.
            with monkeypatch.context() as isolated:
                isolated.setattr(
                    "runtime.test_dependencies.current_runtime_test_dependencies",
                    lambda: None,
                )
                if acceptance_binding is not None:
                    isolated.setattr(
                        "runtime.control_plane.read_checkout_sha",
                        lambda _root, *, require_clean: current_source_sha,
                    )
                result = super().repair_promote(**kwargs)
            raise _StopAfterRepairPromote(result)

    def fake_adjudicator_call(
        _prompt: str,
        api_config: Mapping[str, Any],
        _system_prompt: str,
        **kwargs: Any,
    ) -> Mapping[str, Any]:
        runtime = kwargs.get("provider_runtime")
        observed_calls.append(
            {
                "host": urlsplit(str(api_config.get("api_base") or "")).hostname,
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
        except _StopAfterRepairPromote as stopped:
            return stopped.result
        raise AssertionError("repair-promote fixture did not reach its public boundary")

    if acceptance_binding is None:
        result = run_fixture()
    else:
        context, controller = acceptance_binding
        with bind_acceptance_execution_context(context, controller):
            result = run_fixture()

    return result, observed_calls, host_acknowledgement


def test_standalone_repair_promote_blocks_external_validator_without_acceptance_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, calls, acknowledgement = _exercise_repair_promote(
        tmp_path,
        monkeypatch,
        acceptance_binding=None,
    )

    assert acknowledgement["acknowledged"] is True
    assert acknowledgement["required_hosts"] == ["validator.example.test"]
    assert calls == [], (
        "standalone repair-promote reached the fake external-shaped Validator without "
        "an acceptance budget; observed calls: "
        f"{calls}; repair result status={result.get('status')!r}, "
        f"reason={result.get('reason')!r}"
    )
    assert result["status"] == "blocked"
    assert result["mutation_performed"] is False


def test_repair_promote_acceptance_context_control_reaches_local_fake_validator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    acceptance_binding = _acceptance_binding(tmp_path)
    result, calls, acknowledgement = _exercise_repair_promote(
        tmp_path,
        monkeypatch,
        acceptance_binding=acceptance_binding,
    )

    assert acknowledgement["acknowledged"] is True
    assert acknowledgement["required_hosts"] == ["validator.example.test"]
    assert result["status"] == "promoted", result
    assert len(calls) == 1
    assert calls[0]["host"] == "validator.example.test"
    assert calls[0]["aggregate_controller_bound"] is True


def test_repair_promote_rejects_unstarted_aggregate_state_before_transport(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context, controller = _acceptance_binding(tmp_path)
    result, calls, _acknowledgement = _exercise_repair_promote(
        tmp_path,
        monkeypatch,
        acceptance_binding=(
            replace(context, provider_budget_state_started=False), controller
        ),
    )

    assert calls == []
    assert result["status"] == "blocked"
    assert "started, bounded aggregate acceptance run" in result["reason"]


def test_repair_promote_rejects_stale_source_sha_before_transport(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, calls, _acknowledgement = _exercise_repair_promote(
        tmp_path,
        monkeypatch,
        acceptance_binding=_acceptance_binding(tmp_path),
        current_source_sha="f" * 40,
    )

    assert calls == []
    assert result["status"] == "blocked"
    assert "executable SHA differs" in result["reason"]


def test_official_validator_host_still_requires_aggregate_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, calls, acknowledgement = _exercise_repair_promote(
        tmp_path,
        monkeypatch,
        acceptance_binding=None,
        validator_api_base="https://api.deepseek.com/v1",
    )

    assert acknowledgement["required_hosts"] == []
    assert calls == []
    assert result["status"] == "blocked"
    assert "aggregate acceptance run" in result["reason"]


def test_active_loopback_validator_still_requires_aggregate_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, calls, acknowledgement = _exercise_repair_promote(
        tmp_path,
        monkeypatch,
        acceptance_binding=None,
        validator_api_base="http://127.0.0.1:9/v1",
    )

    assert acknowledgement["required_hosts"] == []
    assert calls == []
    assert result["status"] == "blocked"
    assert "aggregate acceptance run" in result["reason"]
