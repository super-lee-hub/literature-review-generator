from __future__ import annotations

import configparser
from datetime import datetime, timedelta, timezone
import json
import logging
from pathlib import Path
import shutil
from types import SimpleNamespace
from typing import Any, Mapping
from dataclasses import replace

import pytest

from outline.provider_router import OutlineProviderRouter, OutlineRoleRoute
from outline.v3_executor import OutlineV3Executor
from config_loader import load_config
from outline.evidence_projection import build_pack
from runtime.control_plane import ReviewControlPlane
from runtime.job_spec import RuntimeJobSpec, RuntimeSourceSpec, save_runtime_job_spec
from runtime.orchestrator import AgentRuntimeBridge, InternalStageExecutorRegistry
from runtime.provider_receipt_closure import ProviderReceiptClosure
from runtime.provider_context import ProviderContextProfile
from runtime.provider_runtime import (
    AcceptanceExecutionContextV1,
    ProviderAggregateBudgetV1,
    ProviderBudgetController,
    bind_acceptance_execution_context,
    hash_json,
)
from services.artifact_registry import ArtifactRegistry, file_sha256
from services.job_workspace import JobWorkspace
from services.settings import ApplicationSettings
from tests.test_current_runtime_full_e2e import _reader_summary, _write_pdf
from tests.test_outline_v3_semantic_execution import _configured_test_provider, _summary


class _PilotRequestHashCaptureStop(Exception):
    """Stop the provider-free fixture probe after the approved plan is built."""


@pytest.fixture(autouse=True)
def _fixture_checkout_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    # The fixture uses an external-shaped route with a local callable while
    # exercising uncommitted code. Source identity itself has separate tests
    # against clean/dirty temporary Git checkouts.
    monkeypatch.setattr(
        "outline.v3_executor.read_checkout_sha",
        lambda _root, *, require_clean: "c1ad0da869bc68869a521f60bbda07342fb16058",
    )


class _LocalTopicRouter:
    def __init__(self, *, emit_content: bool = True) -> None:
        self.calls: list[str] = []
        self.emit_content = emit_content

    def __call__(self, node_id: str, request: Mapping[str, Any]) -> Mapping[str, Any]:
        self.calls.append(node_id)
        if not node_id.startswith("topic_synthesis_provider:batch:"):
            raise AssertionError(f"out-of-scope provider route called: {node_id}")
        return self._response(node_id, request)

    def _response(self, node_id: str, request: Mapping[str, Any]) -> dict[str, Any]:
        response = dict(_configured_test_provider(node_id, request))
        content = response.get("content")
        if isinstance(content, dict):
            for topic in content.get("topics") or ():
                if not isinstance(topic, dict):
                    continue
                if self.emit_content:
                    # Keep the local pilot fixture substantive without making
                    # a factual claim that would need the source claim's full
                    # qualifier set (claim, evidence, and source fields).
                    topic["status"] = "unresolved"
                    topic["reason"] = "The local fixture cannot resolve this fragment without adjudication."
                    topic["unresolved_questions"] = [{
                        "fragment_id": str(topic.get("fragment_id") or ""),
                        "question": "Does the selected evidence support this topic under its stated conditions?",
                    }]
                else:
                    topic["conclusions"] = []
                    topic["unresolved_questions"] = []
        response.update({
            "input_tokens": 37,
            "output_tokens": 19,
            "total_tokens": 56,
            "usage_status": "reported",
        })
        return response


class _FailOnceTopicRouter(_LocalTopicRouter):
    def __init__(self, fail_once_node_id: str) -> None:
        super().__init__()
        self.fail_once_node_id = fail_once_node_id
        self.failure_emitted = False

    def __call__(self, node_id: str, request: Mapping[str, Any]) -> Mapping[str, Any]:
        self.calls.append(node_id)
        if not node_id.startswith("topic_synthesis_provider:batch:"):
            raise AssertionError(f"out-of-scope provider route called: {node_id}")
        response = self._response(node_id, request)
        if node_id == self.fail_once_node_id and not self.failure_emitted:
            self.failure_emitted = True
            response["status"] = "failed"
            response["error_kind"] = "transient_network"
        return response


def _pilot(
    summaries: list[dict[str, Any]],
    route: OutlineRoleRoute,
    *,
    acceptance_run_id: str,
    max_physical_attempts: int = 3,
    max_output_tokens_all_attempts: int = 12_288,
    deadline_utc: str | None = None,
    selected_topic_batch_ids: list[str] | None = None,
) -> dict[str, Any]:
    deadline = deadline_utc or (
        datetime.now(timezone.utc) + timedelta(minutes=2)
    ).isoformat(timespec="seconds").replace("+00:00", "Z")
    selected_ids = selected_topic_batch_ids or [
        "topic_synthesis_provider:batch:1"
    ]
    return {
        "schema_version": "outline-topic-pilot/v1",
        "selected_topic_batch_ids": selected_ids,
        "selected_request_hashes": {
            batch_id: "0" * 64 for batch_id in selected_ids
        },
        "source_summary_set_hash": hash_json(summaries),
        "allowed_route_fingerprint": route.safe_config_fingerprint(),
        "acceptance_run_id": acceptance_run_id,
        "max_physical_attempts": max_physical_attempts,
        "max_output_tokens_all_attempts": max_output_tokens_all_attempts,
        "deadline_utc": deadline,
        "auto_continue": False,
        "adoption_authorized": False,
    }


def _executor(
    tmp_path: Path,
    *,
    transport: _LocalTopicRouter,
    route: OutlineRoleRoute,
    pilot: Mapping[str, Any],
    summaries: list[dict[str, Any]] | None = None,
    max_source_prompt_tokens: int = 32_000,
    auto_select_materialized_batches: bool = False,
    capture_request_hashes: bool = True,
) -> tuple[OutlineV3Executor, ArtifactRegistry]:
    summaries = list(summaries or [
        _summary("paper-a", "Study A", "The treatment improved the outcome."),
        _summary("paper-b", "Study B", "The effect held under a second context."),
    ])

    def build(
        root: Path,
        pilot_config: Mapping[str, Any],
    ) -> tuple[OutlineV3Executor, ArtifactRegistry]:
        workspace = JobWorkspace.create(str(root), "pilot", job_id="topic-pilot-job")
        registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
        executor = OutlineV3Executor(
            job_id=workspace.job_id,
            summaries=summaries,
            workspace=workspace,
            artifact_registry=registry,
            provider=lambda *_args: (_ for _ in ()).throw(
                AssertionError("legacy provider route must not be used")
            ),
            provider_profile=route.profile,
            provider_router=OutlineProviderRouter(
                routes={"candidate_provider_generation": route}
            ),
            enabled_semantic_roles=("candidate_provider_generation",),
            candidate_count=1,
            stability_mode="off",
            max_provider_calls=12,
            max_source_prompt_tokens=max_source_prompt_tokens,
            outline_pilot=pilot_config,
        )
        return executor, registry

    selected_ids = pilot.get("selected_topic_batch_ids")
    if (
        capture_request_hashes
        and
        isinstance(selected_ids, list)
        and selected_ids
        and all(isinstance(item, str) for item in selected_ids)
        and len(selected_ids) == len(set(selected_ids))
    ):
        # Materialize exact prompt-attached hashes in a separate local probe.
        # The probe raises at the provider boundary, so it never calls the
        # local fake transport or a network endpoint.
        probe_pilot = dict(pilot)
        probe_pilot["deadline_utc"] = (
            datetime.now(timezone.utc) + timedelta(minutes=5)
        ).isoformat(timespec="seconds").replace("+00:00", "Z")
        probe_root = tmp_path / "request-hash-probe"
        probe, _probe_registry = build(probe_root, probe_pilot)
        captured_hashes: dict[str, str] = {}
        selected_scope = set(selected_ids)
        original_materializer = probe._materialize_topic_pilot_plan

        def capture_materialized_hashes(**kwargs: Any) -> Any:
            if auto_select_materialized_batches:
                batch_ids = [
                    f"topic_synthesis_provider:batch:{index}"
                    for index, _batch in enumerate(kwargs["topic_batches"], start=1)
                ]
                probe.outline_pilot["selected_topic_batch_ids"] = batch_ids
                probe.outline_pilot["selected_request_hashes"] = {
                    batch_id: "0" * 64 for batch_id in batch_ids
                }
                selected_scope.clear()
                selected_scope.update(batch_ids)
            original_contract = probe._semantic_request_contract

            def capture_contract(node_id: str, request: Mapping[str, Any]) -> None:
                if node_id in selected_scope:
                    request_hash = hash_json(
                        probe._attach_prompt_authority(node_id, request)
                    )
                    captured_hashes[node_id] = request_hash
                    probe.outline_pilot["selected_request_hashes"][node_id] = request_hash
                original_contract(node_id, request)

            probe._semantic_request_contract = capture_contract
            try:
                return original_materializer(**kwargs)
            finally:
                probe._semantic_request_contract = original_contract

        def stop_before_provider_call(
            _node_id: str,
            _request: Mapping[str, Any],
            _dependency_hashes: Mapping[str, str],
            **_kwargs: Any,
        ) -> dict[str, Any]:
            raise _PilotRequestHashCaptureStop()

        probe._materialize_topic_pilot_plan = capture_materialized_hashes
        probe._run_semantic_provider_call = stop_before_provider_call
        phase_attempt_cap = int(pilot.get("max_physical_attempts") or 1)
        phase_output_cap = int(pilot.get("max_output_tokens_all_attempts") or 1)
        configured_retries = max(
            0,
            int(route.config_identity.get("transport_retries") or 0),
        )
        phase_retry_cap = min(
            2,
            len(selected_ids) * configured_retries,
            max(0, phase_attempt_cap - len(selected_ids)),
        )
        probe_context, probe_controller = _acceptance_binding(
            probe_root,
            str(pilot.get("acceptance_run_id") or "request-hash-probe"),
            max_provider_calls_total=min(3, phase_attempt_cap),
            max_output_tokens_total=min(12_288, phase_output_cap),
            max_retry_attempts_total=min(2, phase_retry_cap),
        )
        with bind_acceptance_execution_context(probe_context, probe_controller):
            probe_result = probe.run()
        if set(captured_hashes) != selected_scope:
            raise AssertionError(
                "provider-free pilot request hash probe missed a selected batch: "
                f"selected={sorted(selected_scope)}, captured={sorted(captured_hashes)}, "
                f"status={probe_result.status}, diagnostics={probe_result.diagnostics}"
            )
        if not isinstance(pilot, dict):
            raise TypeError("pilot integration fixture requires a mutable mapping")
        pilot["selected_topic_batch_ids"] = list(
            probe.outline_pilot["selected_topic_batch_ids"]
        )
        pilot["selected_request_hashes"] = dict(captured_hashes)

    executor, registry = build(tmp_path, pilot)
    return executor, registry


def _route(
    transport: _LocalTopicRouter,
    *,
    endpoint_type: str = "chat_completions",
    api_base: str = "http://127.0.0.1:1/v1",
    transport_retries: int = 0,
    max_output_tokens: int = 512,
) -> OutlineRoleRoute:
    profile = ProviderContextProfile.conservative(
        provider="openai",
        model="pilot-test-model",
        endpoint_type=endpoint_type,
        model_context_limit=128_000,
        max_output_tokens=max_output_tokens,
    )
    return OutlineRoleRoute(
        role="candidate_provider_generation",
        config_section="Local_Pilot_API",
        provider_name="openai",
        model="pilot-test-model",
        endpoint_type=endpoint_type,
        profile=profile,
        transport=transport,
        api_base=api_base,
        config_identity={
            "provider_family": "openai",
            "model": "pilot-test-model",
            "endpoint_type": endpoint_type,
            "api_base": api_base,
            "max_context_tokens": "128000",
            "max_output_tokens": str(max_output_tokens),
            "transport_retries": str(transport_retries),
        },
    )


def _acceptance_binding(
    tmp_path: Path,
    acceptance_run_id: str,
    *,
    max_provider_calls_total: int = 3,
    max_output_tokens_total: int = 12_288,
    max_retry_attempts_total: int = 0,
    max_wall_seconds: float = 60,
) -> tuple[AcceptanceExecutionContextV1, ProviderBudgetController]:
    budget = ProviderAggregateBudgetV1(
        max_provider_calls_total=max_provider_calls_total,
        max_output_tokens_total=max_output_tokens_total,
        max_retry_attempts_total=max_retry_attempts_total,
        max_wall_seconds=max_wall_seconds,
    )
    context = AcceptanceExecutionContextV1(
        acceptance_run_id=acceptance_run_id,
        final_executable_sha="c1ad0da869bc68869a521f60bbda07342fb16058",
        absolute_deadline_epoch=(datetime.now(timezone.utc) + timedelta(seconds=max_wall_seconds)).timestamp(),
        provider_budget=budget,
        provider_budget_state_path=str(tmp_path / "provider_budget_state_v1.json"),
        evidence_root=str(tmp_path / "evidence"),
        process_event_log=str(tmp_path / "process_events.jsonl"),
        scenario_state_path=str(tmp_path / "scenario_state.json"),
        owner_authorized=True,
        provider_budget_state_started=False,
    )
    return context, ProviderBudgetController(budget)


def _safe_closure_debug(executor: OutlineV3Executor) -> dict[str, Any]:
    selected = set(executor._pilot_allowed_node_ids)
    expected = [
        call
        for call_id, call in executor._expected_provider_calls.items()
        if call_id in {executor._provider_call_id(node_id) for node_id in selected}
    ]
    ledger = executor._receipt_ledger
    receipts = ledger.list_receipts() if ledger is not None else []
    closure = ProviderReceiptClosure.evaluate(expected, receipts).to_dict()
    fields = (
        "complete",
        "expected_call_ids",
        "observed_call_ids",
        "missing_call_ids",
        "stale_call_ids",
        "failed_call_ids",
        "incomplete_call_ids",
        "unexpected_receipts",
        "out_of_scope_receipts",
        "out_of_epoch_receipts",
        "retry_exceeded_call_ids",
        "usage_incomplete_call_ids",
        "hash_mismatches",
    )
    return {
        "diagnostics": list(executor.diagnostics),
        "expected": [
            {
                "call_id": item.call_id,
                "node_id": item.node_id,
                "closure_epoch_prefix": item.closure_epoch_id[:12],
                "prompt_hash_prefix": item.prompt_hash[:12],
                "input_hash_prefix": item.input_hash[:12],
                "config_hash_prefix": item.config_hash[:12],
                "schema_hash_prefix": item.schema_hash[:12],
            }
            for item in expected
        ],
        "receipts": [
            {
                "receipt_id": item.receipt_id,
                "call_id": item.call_id,
                "node_id": item.node_id,
                "status": item.status,
                "closure_epoch_prefix": item.closure_epoch_id[:12],
                "prompt_hash_prefix": item.prompt_hash[:12],
                "input_hash_prefix": item.input_hash[:12],
                "config_hash_prefix": item.config_hash[:12],
                "schema_hash_prefix": item.schema_hash[:12],
                "response_hash_prefix": str(item.response_hash or "")[:12],
                "attempts": item.attempts,
                "usage_status": item.usage_status,
            }
            for item in receipts
        ],
        "closure": {key: closure.get(key) for key in fields},
    }


def _runner_pilot_spec(
    tmp_path: Path,
    *,
    monkeypatch: pytest.MonkeyPatch,
    transport_retries: int = 0,
    max_physical_attempts: int = 3,
    max_output_tokens_all_attempts: int = 12_288,
    deadline_delta_seconds: int = 120,
) -> tuple[Path, RuntimeJobSpec, dict[str, Any]]:
    pdf_dir = tmp_path / "papers"
    pdf_dir.mkdir()
    rows: list[dict[str, Any]] = []
    for key, title, finding in (
        ("paper-a", "Study A", "The treatment improved the outcome."),
        ("paper-b", "Study B", "The effect held under a second context."),
    ):
        pdf_path = pdf_dir / f"{key}.pdf"
        _write_pdf(pdf_path, title, finding)
        normalized = _reader_summary(key, title, finding)
        paper_info = dict(normalized["paper_info"])
        paper_info.update(
            {
                "source_paper_id": str(pdf_path),
                "source_mode": "direct",
                "source_pdf": str(pdf_path),
                "source_pdf_fingerprint": file_sha256(pdf_path),
            }
        )
        ai_summary = {
            field: value
            for field, value in normalized.items()
            if field not in {"status", "paper_info", "source_mode"}
        }
        rows.append({
            "status": "success",
            "source_mode": "direct",
            "paper_info": paper_info,
            "ai_summary": ai_summary,
        })

    summary_file = tmp_path / "frozen-stage1-summaries.json"
    summary_file.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    config_path = tmp_path / "config.ini"
    shutil.copyfile(Path(__file__).resolve().parents[1] / "config.ini.example", config_path)
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(config_path, encoding="utf-8")
    parser["Paths"]["output_path"] = str(tmp_path / "output")
    for section_name in parser.sections():
        if section_name.endswith("_API") and "api_base" in parser[section_name]:
            parser[section_name]["api_base"] = "http://127.0.0.1:1/v1"
    parser["Outline_API"]["provider_family"] = "deepseek"
    parser["Outline_API"]["model"] = "pilot-test-model"
    parser["Outline_API"]["endpoint_type"] = "chat_completions"
    parser["Outline_API"]["max_context_tokens"] = "128000"
    parser["Outline_API"]["max_output_tokens"] = "512"
    parser["Outline_API"]["transport_retries"] = str(transport_retries)
    parser["OutlineStability"]["mode"] = "off"
    parser["OutlineStability"]["max_provider_calls"] = "12"
    parser["OutlineStability"]["max_source_prompt_tokens"] = "32000"
    with config_path.open("w", encoding="utf-8") as handle:
        parser.write(handle)

    job_id = "topic-pilot-runtime-job"
    metadata = {"requested_stages": ["outline"]}
    base_spec = RuntimeJobSpec(
        project_name="topic-pilot-runtime",
        source=RuntimeSourceSpec(mode="direct", pdf_folder=str(pdf_dir)),
        job_id=job_id,
        config=str(config_path),
        action="generate_outline",
        summary_file=str(summary_file),
        queue_file=str(tmp_path / "queue.json"),
        metadata=metadata,
    )
    loaded_config = load_config(
        str(config_path),
        action="generate_outline",
        requested_stages=["outline"],
        free_mode_enabled=False,
        allow_template_credentials=True,
    )
    settings = ApplicationSettings.from_config(loaded_config)
    bridge = AgentRuntimeBridge(base_spec)
    route_registry = InternalStageExecutorRegistry(bridge)
    route_session = SimpleNamespace(
        stage_host=SimpleNamespace(
            config=loaded_config,
            settings=settings,
            logger=logging.getLogger("test.topic-pilot-route-fingerprint"),
        )
    )
    route = route_registry._outline_route_for_section(
        session=route_session,
        role="candidate_provider_generation",
        section_name=str(settings.outline_model() or ""),
    )
    assert route is not None
    packed = build_pack(
        rows,
        source_ref=str(summary_file),
        source_ref_sha256=file_sha256(summary_file),
        job_id=job_id,
    )
    pilot = {
        "schema_version": "outline-topic-pilot/v1",
        "selected_topic_batch_ids": ["topic_synthesis_provider:batch:1"],
        "selected_request_hashes": {
            "topic_synthesis_provider:batch:1": "0" * 64,
        },
        "source_summary_set_hash": hash_json(
            [dict(entry) for entry in packed.get("entries") or ()]
        ),
        "allowed_route_fingerprint": route.safe_config_fingerprint(),
        "acceptance_run_id": "acceptance-topic-pilot-test",
        "max_physical_attempts": max_physical_attempts,
        "max_output_tokens_all_attempts": max_output_tokens_all_attempts,
        "deadline_utc": (
            datetime.now(timezone.utc) + timedelta(seconds=deadline_delta_seconds)
        ).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "auto_continue": False,
        "adoption_authorized": False,
    }
    spec = replace(base_spec, metadata={**metadata, "outline_pilot": pilot})
    spec_path = tmp_path / "runtime-job-spec.json"
    save_runtime_job_spec(spec_path, spec)

    # Run a second, isolated local admission probe through the real Runner and
    # Bridge. It captures the exact materialized hash, then aborts before the
    # provider boundary; the persisted test spec below carries that hash.
    probe_root = tmp_path / "runner-request-hash-probe"
    probe_config_path = tmp_path / "runner-request-hash-probe.ini"
    shutil.copyfile(config_path, probe_config_path)
    probe_parser = configparser.ConfigParser(interpolation=None)
    probe_parser.read(probe_config_path, encoding="utf-8")
    probe_parser["Paths"]["output_path"] = str(probe_root / "output")
    with probe_config_path.open("w", encoding="utf-8") as handle:
        probe_parser.write(handle)
    probe_pilot = dict(pilot)
    probe_pilot["deadline_utc"] = (
        datetime.now(timezone.utc) + timedelta(minutes=5)
    ).isoformat(timespec="seconds").replace("+00:00", "Z")
    probe_spec = replace(
        base_spec,
        config=str(probe_config_path),
        queue_file=str(probe_root / "queue.json"),
        metadata={**metadata, "outline_pilot": probe_pilot},
    )
    probe_spec_path = tmp_path / "runner-request-hash-probe-spec.json"
    save_runtime_job_spec(probe_spec_path, probe_spec)
    captured_hashes: dict[str, str] = {}
    original_materializer = OutlineV3Executor._materialize_topic_pilot_plan
    original_semantic_contract = OutlineV3Executor._semantic_request_contract

    def materialize_and_capture(
        executor: OutlineV3Executor,
        *,
        topic_batches: Any,
        topic_routes: Any,
        evidence_model: Any,
        content_layers_model: Any,
        profile: Any,
    ) -> Any:
        def capture_contract(
            node_id: str,
            request: Mapping[str, Any],
        ) -> None:
            if node_id in set(executor.outline_pilot["selected_topic_batch_ids"]):
                request_hash = hash_json(
                    executor._attach_prompt_authority(node_id, request)
                )
                executor.outline_pilot["selected_request_hashes"][node_id] = request_hash
                captured_hashes[node_id] = request_hash
            original_semantic_contract(executor, node_id, request)

        executor._semantic_request_contract = capture_contract
        try:
            return original_materializer(
                executor,
                topic_batches=topic_batches,
                topic_routes=topic_routes,
                evidence_model=evidence_model,
                content_layers_model=content_layers_model,
                profile=profile,
            )
        finally:
            executor._semantic_request_contract = original_semantic_contract.__get__(
                executor, OutlineV3Executor
            )

    def stop_before_probe_transport(
        _executor: OutlineV3Executor,
        _node_id: str,
        _request: Mapping[str, Any],
        _dependency_hashes: Mapping[str, str],
        *,
        output_tokens: int | None = None,
    ) -> dict[str, Any]:
        del output_tokens
        raise _PilotRequestHashCaptureStop()

    with monkeypatch.context() as probe_patch:
        probe_patch.setattr(
            OutlineV3Executor,
            "_materialize_topic_pilot_plan",
            materialize_and_capture,
        )
        probe_patch.setattr(
            OutlineV3Executor,
            "_run_semantic_provider_call",
            stop_before_probe_transport,
        )
        probe_context, probe_controller = _acceptance_binding(
            probe_root,
            str(pilot["acceptance_run_id"]),
        )
        with bind_acceptance_execution_context(probe_context, probe_controller):
            ReviewControlPlane(
                repo_root=Path(__file__).resolve().parents[1]
            ).run(probe_spec_path)
    if set(captured_hashes) != set(pilot["selected_topic_batch_ids"]):
        raise AssertionError(
            "runner provider-free hash probe did not materialize every selected batch: "
            f"captured={sorted(captured_hashes)}"
        )
    pilot["selected_request_hashes"] = dict(captured_hashes)
    spec = replace(base_spec, metadata={**metadata, "outline_pilot": pilot})
    save_runtime_job_spec(spec_path, spec)
    return spec_path, spec, pilot


def _install_runner_fake_router(
    monkeypatch: pytest.MonkeyPatch,
    transport: _LocalTopicRouter,
) -> None:
    import runtime.orchestrator as orchestrator_module

    original_builder = orchestrator_module.build_outline_provider_router

    def fake_router_builder(**kwargs: Any) -> OutlineProviderRouter:
        router = original_builder(**kwargs)
        return OutlineProviderRouter(
            routes={
                role: replace(route, transport=transport)
                for role, route in router.routes.items()
            },
            diagnostics=router.diagnostics,
        )

    monkeypatch.setattr(
        orchestrator_module,
        "build_outline_provider_router",
        fake_router_builder,
    )


def test_topic_pilot_only_posts_selected_batches_and_closes_noncanonical_checkpoint(
    tmp_path: Path,
) -> None:
    acceptance_run_id = "offline-topic-pilot"
    transport = _LocalTopicRouter()
    summaries = [
        _summary("paper-a", "Study A", "The treatment improved the outcome."),
        _summary("paper-b", "Study B", "The effect held under a second context."),
    ]
    route = _route(transport)
    pilot = _pilot(summaries, route, acceptance_run_id=acceptance_run_id)
    executor, registry = _executor(
        tmp_path, transport=transport, route=route, pilot=pilot
    )
    context, controller = _acceptance_binding(tmp_path, acceptance_run_id)

    with bind_acceptance_execution_context(context, controller):
        result = executor.run()

    assert result.status == "topic_pilot_complete", _safe_closure_debug(executor)
    assert result.ok is False
    assert transport.calls == pilot["selected_topic_batch_ids"]
    assert all("cross_group" not in node_id for node_id in transport.calls)
    assert all("global_synthesis" not in node_id for node_id in transport.calls)
    assert all("relation_adjudication" not in node_id for node_id in transport.calls)
    assert all("candidate_" not in node_id for node_id in transport.calls)

    scope_hash = hash_json(pilot)
    checkpoint = registry.get(f"outline-v3:topic-pilot-checkpoint:{scope_hash[:24]}")
    closure = registry.get(f"outline-v3:topic-pilot-closure:{scope_hash[:24]}")
    assert checkpoint is not None and checkpoint.status == "ready"
    assert closure is not None and closure.status == "ready"
    checkpoint_payload = json.loads(Path(checkpoint.path).read_text(encoding="utf-8"))
    closure_payload = json.loads(Path(closure.path).read_text(encoding="utf-8"))
    assert checkpoint_payload["canonical_ready"] is False
    assert checkpoint_payload["adoption_authorized"] is False
    assert checkpoint_payload["final_outline_artifact_id"] == ""
    assert checkpoint_payload["receipt_closure_artifact_id"] == closure.artifact_id
    assert closure_payload["complete"] is True
    assert closure_payload["canonical_outline_complete"] is False
    assert registry.get("outline-v3:final_outline") is None


def test_topic_pilot_rejects_hollow_topic_output_without_checkpoint(tmp_path: Path) -> None:
    acceptance_run_id = "hollow-topic-pilot"
    transport = _LocalTopicRouter(emit_content=False)
    summaries = [
        _summary("paper-a", "Study A", "The treatment improved the outcome."),
        _summary("paper-b", "Study B", "The effect held under a second context."),
    ]
    route = _route(transport)
    pilot = _pilot(summaries, route, acceptance_run_id=acceptance_run_id)
    executor, registry = _executor(
        tmp_path, transport=transport, route=route, pilot=pilot
    )
    context, controller = _acceptance_binding(tmp_path, acceptance_run_id)

    with bind_acceptance_execution_context(context, controller):
        result = executor.run()

    assert result.status == "blocked"
    assert transport.calls == pilot["selected_topic_batch_ids"]
    assert any(
        "topic pilot fragment has neither a supported conclusion nor an explicit unresolved question"
        in diagnostic
        for diagnostic in result.diagnostics
    )
    scope_hash = hash_json(pilot)
    assert registry.get(f"outline-v3:topic-pilot-checkpoint:{scope_hash[:24]}") is None


def test_topic_pilot_external_route_without_acceptance_is_blocked_before_fake_post(
    tmp_path: Path,
) -> None:
    acceptance_run_id = "missing-acceptance"
    transport = _LocalTopicRouter()
    summaries = [
        _summary("paper-a", "Study A", "The treatment improved the outcome."),
        _summary("paper-b", "Study B", "The effect held under a second context."),
    ]
    route = _route(
        transport,
        endpoint_type="chat_completions",
        api_base="https://gateway.example.test/v1",
    )
    pilot = _pilot(summaries, route, acceptance_run_id=acceptance_run_id)
    executor, _registry = _executor(
        tmp_path,
        transport=transport,
        route=route,
        pilot=pilot,
    )

    result = executor.run()

    assert result.status == "blocked"
    assert transport.calls == []
    assert any("acceptance run" in item for item in result.diagnostics)


@pytest.mark.parametrize(
    ("max_physical_attempts", "max_output_tokens_all_attempts", "transport_retries", "deadline_delta"),
    [
        (1, 50_000, 1, 120),
        (12, 1, 0, 120),
        (12, 50_000, 0, -1),
    ],
    ids=["physical-attempt-cap", "output-token-cap", "expired-deadline"],
)
def test_topic_pilot_budget_or_deadline_rejection_emits_no_fake_posts(
    tmp_path: Path,
    max_physical_attempts: int,
    max_output_tokens_all_attempts: int,
    transport_retries: int,
    deadline_delta: int,
) -> None:
    acceptance_run_id = "bounded-offline-pilot"
    transport = _LocalTopicRouter()
    summaries = [
        _summary("paper-a", "Study A", "The treatment improved the outcome."),
        _summary("paper-b", "Study B", "The effect held under a second context."),
    ]
    route = _route(transport, transport_retries=transport_retries)
    deadline = (datetime.now(timezone.utc) + timedelta(seconds=deadline_delta)).isoformat(
        timespec="seconds"
    ).replace("+00:00", "Z")
    pilot = _pilot(
        summaries,
        route,
        acceptance_run_id=acceptance_run_id,
        max_physical_attempts=max_physical_attempts,
        max_output_tokens_all_attempts=max_output_tokens_all_attempts,
        deadline_utc=deadline,
    )
    executor, _registry = _executor(
        tmp_path,
        transport=transport,
        route=route,
        pilot=pilot,
    )
    phase_attempt_cap = max_physical_attempts
    phase_output_cap = max_output_tokens_all_attempts
    configured_retries = max(
        0,
        int(route.config_identity.get("transport_retries") or 0),
    )
    phase_retry_cap = min(
        2,
        len(pilot["selected_topic_batch_ids"]) * configured_retries,
        max(0, phase_attempt_cap - len(pilot["selected_topic_batch_ids"])),
    )
    context, controller = _acceptance_binding(
        tmp_path,
        acceptance_run_id,
        max_provider_calls_total=min(3, phase_attempt_cap),
        max_output_tokens_total=min(12_288, phase_output_cap),
        max_retry_attempts_total=min(2, phase_retry_cap),
    )

    with bind_acceptance_execution_context(context, controller):
        result = executor.run()

    assert result.status == "blocked"
    assert transport.calls == []


def test_topic_pilot_checkpoint_rejects_canonicalization_and_missing_closure_dependency(
    tmp_path: Path,
) -> None:
    acceptance_run_id = "checkpoint-validation-pilot"
    transport = _LocalTopicRouter()
    summaries = [
        _summary("paper-a", "Study A", "The treatment improved the outcome."),
        _summary("paper-b", "Study B", "The effect held under a second context."),
    ]
    route = _route(transport)
    pilot = _pilot(summaries, route, acceptance_run_id=acceptance_run_id)
    executor, registry = _executor(
        tmp_path, transport=transport, route=route, pilot=pilot
    )
    context, controller = _acceptance_binding(tmp_path, acceptance_run_id)
    with bind_acceptance_execution_context(context, controller):
        result = executor.run()
    assert result.status == "topic_pilot_complete"

    from dataclasses import replace

    from runtime.artifact_validators import ArtifactSchemaError, validate_registered_artifact

    scope_hash = hash_json(pilot)
    checkpoint = registry.get(f"outline-v3:topic-pilot-checkpoint:{scope_hash[:24]}")
    assert checkpoint is not None
    payload = json.loads(Path(checkpoint.path).read_text(encoding="utf-8"))
    payload["canonical_ready"] = True
    tampered_path = tmp_path / "tampered-topic-pilot-checkpoint.json"
    tampered_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ArtifactSchemaError, match="incomplete or canonicalized"):
        validate_registered_artifact(checkpoint, tampered_path)

    closure_id = str(payload["receipt_closure_artifact_id"])
    stripped = replace(
        checkpoint,
        depends_on=tuple(
            dependency
            for dependency in checkpoint.depends_on
            if str(dependency.artifact_id) != closure_id
        ),
    )
    with pytest.raises(ArtifactSchemaError, match="dependency binding is incomplete"):
        validate_registered_artifact(stripped, checkpoint.path)


def test_topic_pilot_rejects_wrong_approved_request_hash_before_fake_post(
    tmp_path: Path,
) -> None:
    acceptance_run_id = "wrong-approved-request-hash"
    transport = _LocalTopicRouter()
    summaries = [
        _summary("paper-a", "Study A", "The treatment improved the outcome."),
        _summary("paper-b", "Study B", "The effect held under a second context."),
    ]
    route = _route(transport)
    pilot = _pilot(summaries, route, acceptance_run_id=acceptance_run_id)
    executor, _registry = _executor(
        tmp_path, transport=transport, route=route, pilot=pilot
    )
    approved = dict(executor.outline_pilot["selected_request_hashes"])
    batch_id = pilot["selected_topic_batch_ids"][0]
    wrong_hash = "f" * 64 if approved[batch_id] != "f" * 64 else "e" * 64
    pilot["selected_request_hashes"][batch_id] = wrong_hash
    executor.outline_pilot["selected_request_hashes"][batch_id] = wrong_hash
    context, controller = _acceptance_binding(tmp_path, acceptance_run_id)

    with bind_acceptance_execution_context(context, controller):
        result = executor.run()

    assert result.status == "blocked"
    assert any(
        "topic pilot request differs from owner-approved exact hash" in item
        for item in result.diagnostics
    )
    assert transport.calls == []


def test_topic_pilot_rejects_aggregate_budget_wider_than_phase_envelope(
    tmp_path: Path,
) -> None:
    acceptance_run_id = "overwide-pilot-aggregate-budget"
    transport = _LocalTopicRouter()
    summaries = [
        _summary("paper-a", "Study A", "The treatment improved the outcome."),
        _summary("paper-b", "Study B", "The effect held under a second context."),
    ]
    route = _route(transport)
    pilot = _pilot(
        summaries,
        route,
        acceptance_run_id=acceptance_run_id,
        max_physical_attempts=3,
        max_output_tokens_all_attempts=12_288,
    )
    executor, _registry = _executor(
        tmp_path, transport=transport, route=route, pilot=pilot
    )
    context, controller = _acceptance_binding(
        tmp_path,
        acceptance_run_id,
        max_provider_calls_total=4,
        max_output_tokens_total=12_289,
        max_retry_attempts_total=3,
    )

    with bind_acceptance_execution_context(context, controller):
        result = executor.run()

    assert result.status == "blocked"
    assert any("acceptance authority" in item.casefold() for item in result.diagnostics), result.diagnostics
    assert transport.calls == []


def test_three_batch_topic_pilot_reserves_nine_attempts_before_first_call(
    tmp_path: Path,
) -> None:
    acceptance_run_id = "three-batch-nine-attempt-pilot"
    summaries = [
        _summary(
            f"paper-{index}",
            f"Study {index}",
            f"The treatment improved the measured outcome in context {index}.",
        )
        for index in range(1, 10)
    ]
    transport = _LocalTopicRouter()
    route = _route(
        transport, transport_retries=2, max_output_tokens=4096,
    )
    pilot = _pilot(
        summaries,
        route,
        acceptance_run_id=acceptance_run_id,
        max_physical_attempts=9,
        max_output_tokens_all_attempts=36_864,
        selected_topic_batch_ids=[
            "topic_synthesis_provider:batch:1",
            "topic_synthesis_provider:batch:2",
            "topic_synthesis_provider:batch:3",
        ],
    )
    executor, registry = _executor(
        tmp_path,
        transport=transport,
        route=route,
        pilot=pilot,
        summaries=summaries,
        max_source_prompt_tokens=30_000,
    )
    assert pilot["selected_topic_batch_ids"] == [
        "topic_synthesis_provider:batch:1",
        "topic_synthesis_provider:batch:2",
        "topic_synthesis_provider:batch:3",
    ]
    context, controller = _acceptance_binding(
        tmp_path,
        acceptance_run_id,
        max_provider_calls_total=9,
        max_output_tokens_total=36_864,
        max_retry_attempts_total=6,
    )

    with bind_acceptance_execution_context(context, controller):
        result = executor.run()

    assert result.status == "topic_pilot_complete", result.diagnostics
    assert transport.calls == pilot["selected_topic_batch_ids"]
    plan_record = executor.artifact_records["topic_pilot_plan"]
    registry.verify_ready_artifact_closure(plan_record)
    plan = json.loads(Path(plan_record.path).read_text(encoding="utf-8"))
    assert plan["logical_call_count"] == 3
    assert plan["physical_attempt_upper_bound"] == 9
    assert plan["retry_attempt_upper_bound"] == 6
    assert plan["output_token_all_attempts_upper_bound"] == 36_864
    assert controller.snapshot()["calls_used"] == 3


def test_topic_pilot_resume_reuses_ready_batch_and_spends_only_remaining_phase_call(
    tmp_path: Path,
) -> None:
    acceptance_run_id = "pilot-interrupted-resume"
    summaries = [
        _summary(
            f"paper-{index}",
            f"Study {index}",
            f"The treatment improved the measured outcome in context {index}.",
        )
        for index in range(1, 7)
    ]
    transport = _FailOnceTopicRouter(
        "topic_synthesis_provider:batch:2"
    )
    route = _route(transport, transport_retries=0)
    pilot = _pilot(
        summaries,
        route,
        acceptance_run_id=acceptance_run_id,
        selected_topic_batch_ids=[
            "topic_synthesis_provider:batch:1",
            "topic_synthesis_provider:batch:2",
        ],
        max_physical_attempts=3,
        max_output_tokens_all_attempts=12_288,
    )
    executor, _registry = _executor(
        tmp_path,
        transport=transport,
        route=route,
        pilot=pilot,
        summaries=summaries,
        max_source_prompt_tokens=30_000,
        auto_select_materialized_batches=True,
    )
    assert pilot["selected_topic_batch_ids"] == [
        "topic_synthesis_provider:batch:1",
        "topic_synthesis_provider:batch:2",
    ]

    first_context, first_controller = _acceptance_binding(
        tmp_path,
        acceptance_run_id,
        max_provider_calls_total=3,
        max_output_tokens_total=12_288,
        max_retry_attempts_total=0,
    )
    with bind_acceptance_execution_context(first_context, first_controller):
        first = executor.run()

    assert first.status == "blocked"
    assert transport.calls == [
        "topic_synthesis_provider:batch:1",
        "topic_synthesis_provider:batch:2",
    ]
    assert first_controller.snapshot()["calls_used"] == 2

    resumed, _resumed_registry = _executor(
        tmp_path,
        transport=transport,
        route=route,
        pilot=pilot,
        summaries=summaries,
        max_source_prompt_tokens=30_000,
        capture_request_hashes=False,
    )
    resumed_context, resumed_controller = _acceptance_binding(
        tmp_path,
        acceptance_run_id,
        max_provider_calls_total=3,
        max_output_tokens_total=12_288,
        max_retry_attempts_total=0,
    )
    with bind_acceptance_execution_context(resumed_context, resumed_controller):
        final = resumed.run()

    assert transport.calls == [
        "topic_synthesis_provider:batch:1",
        "topic_synthesis_provider:batch:2",
        "topic_synthesis_provider:batch:2",
    ]
    assert final.status == "topic_pilot_complete", _safe_closure_debug(resumed)
    snapshot = resumed_controller.snapshot()
    assert snapshot["calls_used"] == 3
    assert snapshot["calls_used"] <= int(pilot["max_physical_attempts"])


@pytest.mark.parametrize(
    "invalid_scope",
    [[], ["topic_synthesis_provider:batch:1", "topic_synthesis_provider:batch:1"]],
    ids=["empty", "duplicate"],
)
def test_direct_pilot_rejects_empty_or_duplicate_scope_before_transport(
    tmp_path: Path, invalid_scope: list[str]
) -> None:
    transport = _LocalTopicRouter()
    route = _route(transport)
    summaries = [
        _summary("paper-a", "Study A", "The treatment improved the outcome."),
        _summary("paper-b", "Study B", "The effect held under a second context."),
    ]
    pilot = _pilot(summaries, route, acceptance_run_id="invalid-scope")
    pilot["selected_topic_batch_ids"] = invalid_scope
    executor, _registry = _executor(
        tmp_path, transport=transport, route=route, pilot=pilot
    )

    result = executor.run()

    assert result.status == "blocked"
    assert any("scope or phase envelope is invalid" in item for item in result.diagnostics)
    assert transport.calls == []


def test_runner_records_pilot_substage_as_needs_review_without_canonical_ready(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec_path, spec, pilot = _runner_pilot_spec(tmp_path, monkeypatch=monkeypatch)
    transport = _LocalTopicRouter()
    _install_runner_fake_router(monkeypatch, transport)

    context, controller = _acceptance_binding(
        tmp_path,
        str(pilot["acceptance_run_id"]),
    )
    with bind_acceptance_execution_context(context, controller):
        result = ReviewControlPlane(
            repo_root=Path(__file__).resolve().parents[1]
        ).run(spec_path)

    assert result["job_status"] == "completed"
    assert result["job_disposition"] == "needs_review"
    assert result["canonical_ready"] is False
    assert tuple(result["completed_stages"]) == ("source_intake", "outline_topic_pilot")
    assert transport.calls == pilot["selected_topic_batch_ids"]

    registry = ArtifactRegistry(
        Path(result["workspace_path"]) / "artifact_registry.json",
        spec.job_id,
    )
    scope_hash = hash_json(pilot)
    checkpoint_id = f"outline-v3:topic-pilot-checkpoint:{scope_hash[:24]}"
    closure_id = f"outline-v3:topic-pilot-closure:{scope_hash[:24]}"
    checkpoint = registry.get(checkpoint_id)
    closure = registry.get(closure_id)
    assert checkpoint is not None and checkpoint.status == "ready"
    assert closure is not None and closure.status == "ready"
    assert registry.get("outline-v3:final_outline") is None
