from __future__ import annotations

import configparser
import copy
from dataclasses import replace
from pathlib import Path
import re
from typing import Any

import pytest

from config_loader import load_config
from runtime.job_spec import RuntimeJobSpec, RuntimeSourceSpec
from runtime.provider_runtime import ProviderAggregateBudgetV2
from runtime.provider_routes import build_reachable_provider_route_plan
from runtime.runner import AgentRuntimeRunner, RuntimeExecutionResult
from runtime.stage_planning import build_stage_plan
from runtime.trust_admission import (
    acknowledgement_from_values,
    build_runtime_external_host_policy,
)
from services.job_runner import JobRunner
from services.queue_service import PersistentQueueService, QueueJobSpec, QueueRunner
from services.settings import ApplicationSettings


def _strict_queued_spec(tmp_path: Path) -> tuple[RuntimeJobSpec, dict[str, Any], dict[str, Any]]:
    config_path = tmp_path / "config.ini"
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(Path(__file__).resolve().parents[1] / "config.ini.example", encoding="utf-8")
    parser["Paths"]["output_path"] = str(tmp_path / "output")
    for section in parser.sections():
        if section.endswith("_API"):
            parser[section]["api_key"] = "local-fixture-only"
            hostname = re.sub(r"[^a-z0-9-]", "-", section.casefold().replace("_api", ""))
            parser[section]["api_base"] = f"https://{hostname}.example.test/v1"
    parser["Preprocess"]["parser_mode"] = "local"
    parser["Preprocess"]["primary_parser"] = "local"
    parser["Preprocess"]["fallback_parser"] = "local"
    with config_path.open("w", encoding="utf-8") as stream:
        parser.write(stream)

    config = load_config(
        str(config_path),
        action="run_all",
        requested_stages=["analyze", "outline"],
        free_mode_enabled=False,
        allow_template_credentials=False,
    )
    settings = ApplicationSettings.from_config(config)
    requested_stages = ["analyze", "outline"]
    stage_plan = build_stage_plan(
        action="run_all",
        requested_stages=requested_stages,
        validation_enabled=settings.review_validation_enabled(),
    )
    route_plan = build_reachable_provider_route_plan(
        config,
        action="run_all",
        requested_stages=requested_stages,
        free_mode_enabled=False,
        stage_plan=stage_plan,
    )
    external_policy = build_runtime_external_host_policy(
        config,
        route_plan,
        requested_stages=requested_stages,
        outline_pilot=None,
    )
    acknowledgement = acknowledgement_from_values(
        external_policy,
        acknowledged=True,
        hosts=list(external_policy.required_hosts),
    )
    budget = ProviderAggregateBudgetV2(
        max_provider_calls_total=200,
        max_output_tokens_total=2_000_000,
        max_retry_attempts_total=2,
        max_wall_seconds=3_600,
    ).to_dict()
    review_intent = {
        "review_question": "Compare the two mechanisms under their source conditions.",
        "required_comparisons": [
            {
                "paper_keys": ["paper-a", "paper-b"],
                "question": "Does the mechanism change across the two source contexts?",
                "relation_type": "mechanism_contrast",
            }
        ],
    }
    pdf_folder = tmp_path / "papers"
    pdf_folder.mkdir()
    (pdf_folder / "paper.pdf").write_bytes(b"%PDF-1.4\nqueue metadata fixture\n")
    spec = RuntimeJobSpec(
        project_name="queue-metadata",
        source=RuntimeSourceSpec(mode="direct", pdf_folder=str(pdf_folder)),
        config=str(config_path),
        action="run_all",
        metadata={
            "requested_stages": requested_stages,
            "review_intent": review_intent,
            "provider_budget": budget,
            "external_host_acknowledgement": acknowledgement,
        },
    )
    spec.validate()
    return spec, review_intent, budget


def test_queue_runner_executes_frozen_runtime_metadata_through_agent_runner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec, expected_intent, expected_budget = _strict_queued_spec(tmp_path)
    strict_request = spec.to_job_request()
    source_projection_request = replace(strict_request, runtime_spec_payload=None)
    assert JobRunner._request_snapshot(strict_request) == JobRunner._request_snapshot(
        source_projection_request
    )
    queue_path = tmp_path / "output" / "_queue" / "queue.json"
    queue = PersistentQueueService(queue_path)
    job_id = "queue-metadata-actual-run"
    queue.add_job(
        QueueJobSpec(
            job_id=job_id,
            job_type="run_all",
            project_name=spec.project_name,
            parameters=spec.to_dict(),
        )
    )

    frozen_job = queue.get_job(job_id)
    assert frozen_job is not None
    frozen_spec = frozen_job.execution_snapshot["runtime_spec"]
    assert frozen_spec["metadata"]["review_intent"] == expected_intent
    assert frozen_spec["metadata"]["provider_budget"] == expected_budget
    expected_acknowledgement = copy.deepcopy(
        frozen_spec["metadata"]["external_host_acknowledgement"]
    )
    captured_specs: list[RuntimeJobSpec] = []

    def capture_after_runtime_validation(
        runner: AgentRuntimeRunner,
        *,
        resume: bool,
    ) -> RuntimeExecutionResult:
        normalized = runner._normalized_spec(resume=resume)
        captured_specs.append(normalized)
        return RuntimeExecutionResult(
            job_id=str(normalized.job_id),
            workspace_path=str(normalized.workspace_path),
            job_status="completed",
            job_disposition="completed",
            canonical_ready=True,
            requires_attention=False,
            attempt_number=1,
            resumed_from_attempt=None,
            completed_stages=(),
            failed_stage=None,
            job_outcome_path="",
            message="metadata bridge fixture",
            completion_status="completed",
        )

    monkeypatch.setattr(AgentRuntimeRunner, "_execute", capture_after_runtime_validation)
    assert QueueRunner(queue, JobRunner()).run_single_job(job_id) is True

    assert len(captured_specs) == 1
    executed_spec = captured_specs[0]
    assert executed_spec.metadata["review_intent"] == expected_intent
    assert executed_spec.metadata["provider_budget"] == expected_budget
    assert executed_spec.metadata["external_host_acknowledgement"] == expected_acknowledgement
    assert executed_spec.metadata["stage_plan"]["requested_stages"] == ["analyze", "outline"]

    runtime = queue.get_job_runtime(job_id)
    assert runtime is not None
    assert runtime.state.value == "completed"


def test_runtime_job_request_rejects_unknown_metadata_and_conflicting_action_flags(
    tmp_path: Path,
) -> None:
    spec, _intent, _budget = _strict_queued_spec(tmp_path)
    request = spec.to_job_request()

    with pytest.raises(ValueError, match="conflicts with its strict RuntimeJobSpec payload"):
        replace(request, run_all=False)

    invalid_spec = replace(
        spec,
        metadata={**spec.metadata, "unrecognized_metadata": {"authority": True}},
    )
    with pytest.raises(ValueError, match="unknown fields"):
        invalid_spec.to_job_request()
