from __future__ import annotations

import configparser
from pathlib import Path
from typing import Any, Mapping

import pytest

from config_loader import load_config
from runtime.provider_routes import build_reachable_provider_route_plan
from runtime.provider_runtime import bind_acceptance_execution_context
from runtime.trust_admission import (
    acknowledgement_from_values,
    build_external_host_policy,
)
from tests import test_current_validation_repair_e2e as repair_fixture
from tests.test_current_runtime_full_e2e import _adjudicator_response
from tests.test_pr25_v4_repair_promote_budget_context import _acceptance_binding


class _StopAfterRepairPromote(Exception):
    def __init__(self, result: Mapping[str, Any]) -> None:
        self.result = dict(result)


@pytest.mark.parametrize(
    "allow_validator_revalidation", [False, True],
    ids=["review-ack-only", "separate-validator-ack-and-budget"],
)
def test_review_only_admission_does_not_authorize_external_repair_validator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    allow_validator_revalidation: bool,
) -> None:
    """Repair revalidation must admit Validator even if the saved job omitted validate."""

    monkeypatch.setenv("LLM_VALIDATOR_API", "")
    monkeypatch.setenv("LLM_WRITER_API", "")
    original_spec_type = repair_fixture.RuntimeJobSpec
    original_control_plane = repair_fixture.ReviewControlPlane
    route_evidence: dict[str, Any] = {}
    validator_acknowledgement: dict[str, Any] = {}
    calls: list[dict[str, Any]] = []

    def review_only_external_validator_spec(*args: Any, **kwargs: Any) -> Any:
        kwargs["action"] = "generate_review"
        metadata = dict(kwargs.get("metadata") or {})
        metadata.update(
            {
                "requested_stages": ["review"],
                "validation_required": False,
                "require_clean_validation": False,
                "allow_unvalidated_when_validation_optional": True,
            }
        )
        kwargs["metadata"] = metadata

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
        # The positive case deliberately has an unacknowledged Writer host in
        # the saved review-only spec. Direct repair revalidation must admit the
        # supplied Validator route, not re-run the saved Writer host policy.
        parser["Writer_API"]["api_key"] = "local-fake-writer-credential"
        parser["Writer_API"]["api_base"] = (
            "https://writer.example.test/v1"
            if allow_validator_revalidation else "http://127.0.0.1:9/v1"
        )
        parser["Validation"]["review_enabled"] = "true"
        parser["Preprocess"]["parser_mode"] = "local"
        parser["Preprocess"]["primary_parser"] = "local"
        parser["Preprocess"]["fallback_parser"] = "local"
        with config_path.open("w", encoding="utf-8") as handle:
            parser.write(handle)

        normalized = load_config(
            str(config_path),
            action="generate_review",
            requested_stages=["review"],
            free_mode_enabled=False,
        )
        route_plan = build_reachable_provider_route_plan(
            normalized,
            action="generate_review",
            requested_stages=["review"],
        )
        policy = build_external_host_policy(normalized, route_plan)
        acknowledgement = acknowledgement_from_values(
            policy,
            acknowledged=True,
            hosts=policy.required_hosts,
        )
        if allow_validator_revalidation:
            metadata.pop("external_host_acknowledgement", None)
        else:
            metadata["external_host_acknowledgement"] = acknowledgement
        route_evidence.update(
            {
                "semantic_roles": list(route_plan.semantic_roles),
                "required_hosts": list(policy.required_hosts),
                "acknowledged_hosts": list(acknowledgement["hosts"]),
                "validator_host_in_ack": "validator.example.test"
                in acknowledgement["hosts"],
            }
        )
        validator_route_plan = build_reachable_provider_route_plan(
            normalized,
            action="validate_review",
            requested_stages=["validate"],
        )
        validator_policy = build_external_host_policy(
            normalized, validator_route_plan
        )
        validator_acknowledgement.update(
            acknowledgement_from_values(
                validator_policy,
                acknowledged=True,
                hosts=validator_policy.required_hosts,
            )
        )
        return original_spec_type(*args, **kwargs)

    class CapturingControlPlane(original_control_plane):
        def repair_promote(self, **kwargs: Any) -> dict[str, Any]:
            # The bootstrap fixture uses pytest's local adapters. Disable them
            # only at the public repair boundary so a missing admission gate
            # cannot be hidden by the template-credential test shortcut.
            if allow_validator_revalidation:
                context, controller = _acceptance_binding(tmp_path)
                with bind_acceptance_execution_context(context, controller):
                    with monkeypatch.context() as isolated:
                        isolated.setattr(
                            "runtime.test_dependencies.current_runtime_test_dependencies",
                            lambda: None,
                        )
                        isolated.setattr(
                            "runtime.control_plane.read_checkout_sha",
                            lambda _root, *, require_clean: "e" * 40,
                        )
                        result = super().repair_promote(
                            validator_host_acknowledgement=validator_acknowledgement,
                            **kwargs,
                        )
            else:
                with monkeypatch.context() as isolated:
                    isolated.setattr(
                        "runtime.test_dependencies.current_runtime_test_dependencies",
                        lambda: None,
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
        calls.append(
            {
                "host": "validator.example.test"
                if "validator.example.test" in str(api_config.get("api_base") or "")
                else "unexpected-route",
                "aggregate_controller_bound": bool(
                    getattr(runtime, "aggregate_budget", None)
                ),
            }
        )
        return _adjudicator_response()

    monkeypatch.setattr(
        repair_fixture,
        "RuntimeJobSpec",
        review_only_external_validator_spec,
    )
    monkeypatch.setattr(repair_fixture, "ReviewControlPlane", CapturingControlPlane)
    monkeypatch.setattr(
        "validation.llm_adjudicator._call_ai_api",
        fake_adjudicator_call,
    )

    try:
        repair_fixture.test_current_control_plane_revalidates_and_promotes_quarantined_repair(
            tmp_path,
            monkeypatch,
        )
    except _StopAfterRepairPromote as stopped:
        result = stopped.result
    else:
        raise AssertionError("repair-promote fixture did not reach its public boundary")

    assert route_evidence["semantic_roles"] == ["writer"]
    assert route_evidence["required_hosts"] == (
        ["writer.example.test"] if allow_validator_revalidation else []
    )
    assert route_evidence["validator_host_in_ack"] is False
    if allow_validator_revalidation:
        assert validator_acknowledgement["hosts"] == ["validator.example.test"]
        assert result["status"] == "promoted", result
        assert calls == [{"host": "validator.example.test", "aggregate_controller_bound": True}]
    else:
        assert calls == [], (
            "review-only admission reached repair_promote's external-shaped Validator "
            "without Validator host acknowledgement or an aggregate controller; "
            f"observed calls={calls}, route_evidence={route_evidence}, "
            f"result_status={result.get('status')!r}, result_reason={result.get('reason')!r}"
        )
        assert result["status"] == "blocked"
        assert result["mutation_performed"] is False
