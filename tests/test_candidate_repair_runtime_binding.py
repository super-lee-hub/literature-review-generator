from __future__ import annotations

import configparser
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping

import pytest

from outline.candidate_repair_plan import (
    SEMANTIC_REPAIR_OUTPUT_SCHEMA_V1,
    SEMANTIC_REPAIR_RULES_V1,
    SEMANTIC_REPAIR_TASK_V1,
)
from outline.provider_router import (
    ROLE_SETTING_KEYS,
    OutlineProviderRouter,
    collect_routing_diagnostics,
)
from outline.v3_executor import OutlineV3ExecutionError, OutlineV3Executor, _hash_payload
from runtime.orchestrator import (
    AgentRuntimeBridge,
    InternalStageExecutorRegistry,
    _OutlineProviderTransportAdapter,
)
from runtime.provider_runtime import hash_json
from runtime.runtime_spec_binding import (
    RuntimeSpecBindingError,
    read_runtime_spec_binding_v1,
)
from runtime.job_spec import RuntimeJobSpec, RuntimeSourceSpec
from runtime.runner import AgentRuntimeRunner
from services.artifact_registry import (
    file_sha256,
)
from services.queue_service import LocalPublicationContext
from services.writer_source_inventory import WriterSourceInventoryError
from tests.test_current_runtime_full_e2e import _test_config
from tests.test_outline_v3_semantic_execution import _summary
from tests.writer_source_fixture import bind_production_writer_sources


class _LocalRepairProvider:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.errors: list[str] = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, _format: str, *_args: Any) -> None:
                return

            def do_POST(self) -> None:  # noqa: N802
                try:
                    body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                    payload = json.loads(body)
                    messages = payload.get("messages") or ()
                    user_text = next(
                        str(item.get("content") or "")
                        for item in messages
                        if isinstance(item, Mapping) and item.get("role") == "user"
                    )
                    envelope = json.loads(user_text)
                    if not isinstance(envelope, Mapping):
                        raise ValueError("repair envelope must be an object")
                    request_payload = envelope.get("request")
                    if not isinstance(request_payload, Mapping):
                        raise ValueError("repair request payload must be an object")
                    self.server.calls.append({  # type: ignore[attr-defined]
                        "path": self.path,
                        "model": str(payload.get("model") or ""),
                        "envelope": dict(envelope),
                    })
                    original = request_payload.get("original_provider_output")
                    original = original if isinstance(original, Mapping) else {}
                    content = {
                        "candidate_id": str(request_payload.get("candidate_id") or ""),
                        "sections": [
                            dict(item)
                            for item in original.get("sections") or ()
                            if isinstance(item, Mapping)
                        ],
                        "needs_manual_review": [],
                    }
                    content_text = json.dumps(content, ensure_ascii=False)
                    response = {
                        "id": f"chatcmpl-repair-{len(self.server.calls)}",  # type: ignore[attr-defined]
                        "object": "chat.completion",
                        "created": 1,
                        "model": str(payload.get("model") or "gpt-4.1-mini"),
                        "choices": [{
                            "index": 0,
                            "message": {"role": "assistant", "content": content_text},
                            "finish_reason": "stop",
                        }],
                        # This local endpoint simulates usage metadata. It proves
                        # the recorded transport exchange, not external billing.
                        "usage": {
                            "prompt_tokens": 240,
                            "completion_tokens": max(1, len(content_text) // 4),
                            "total_tokens": 240 + max(1, len(content_text) // 4),
                        },
                    }
                    encoded = json.dumps(response).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(encoded)))
                    self.end_headers()
                    self.wfile.write(encoded)
                except Exception as exc:
                    self.server.errors.append(repr(exc))  # type: ignore[attr-defined]
                    encoded = json.dumps({"error": {"message": str(exc)}}).encode("utf-8")
                    self.send_response(500)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(encoded)))
                    self.end_headers()
                    self.wfile.write(encoded)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.calls = self.calls  # type: ignore[attr-defined]
        self.server.errors = self.errors  # type: ignore[attr-defined]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def api_base(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}/v1"

    def close(self) -> None:
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()


@pytest.fixture
def local_repair_provider() -> Any:
    provider = _LocalRepairProvider()
    try:
        yield provider
    finally:
        provider.close()


def _repair_input(paper_key: str) -> tuple[dict[str, Any], dict[str, Any]]:
    original = {
        "candidate_id": "candidate_1",
        "organizing_logic": "evidence",
        "sections": [{
            "section_id": "candidate_1_section_1",
            "title": "Bounded result",
            "goal": "Present the source-grounded result.",
            "paper_keys": [paper_key],
            "relation_ids": [],
            "claims": ["The treatment improved the outcome in the tested context."],
            "rationale": "This statement preserves the source context.",
        }],
    }
    request = {
        "task": SEMANTIC_REPAIR_TASK_V1,
        "candidate_id": "candidate_1",
        "original_provider_output": original,
        "validation_error": "One section key requires structural repair.",
        "allowed_paper_ids": [paper_key],
        "allowed_relation_ids": [],
        "repair_rules": list(SEMANTIC_REPAIR_RULES_V1),
        "output_schema": dict(SEMANTIC_REPAIR_OUTPUT_SCHEMA_V1),
    }
    return original, request


def _prepare_runtime_executor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    api_base: str,
) -> tuple[OutlineV3Executor, Any, Any, Path, Any]:
    config_path = _test_config(tmp_path)
    parser = configparser.ConfigParser()
    parser.read(config_path, encoding="utf-8")
    parser["Outline_API"].update({
        "api_key": "loopback-repair-fixture-key",
        "model": "gpt-4.1-mini",
        "api_base": api_base,
        "provider_family": "generic",
        "endpoint_type": "chat_completions",
        "proxy_mode": "direct",
        "transport_retries": "0",
        "total_timeout_seconds": "9",
        "max_context_tokens": "32768",
        "max_output_tokens": "512",
        "reasoning_reserve_tokens": "0",
        "safety_margin_tokens": "128",
    })
    for setting_key in ROLE_SETTING_KEYS.values():
        parser["OutlineModels"][setting_key] = "Outline_API"
    with config_path.open("w", encoding="utf-8") as handle:
        parser.write(handle)

    pdf_dir = tmp_path / "papers"
    pdf_dir.mkdir()
    (pdf_dir / "source.pdf").write_bytes(b"%PDF-1.4\nfixture source identity\n")
    spec = RuntimeJobSpec(
        project_name="candidate-repair-binding",
        source=RuntimeSourceSpec(mode="direct", pdf_folder=str(pdf_dir)),
        job_id="candidate-repair-binding-job",
        config=str(config_path),
        action="run_all",
        queue_file=str(tmp_path / "queue.json"),
    )
    observed: dict[str, Any] = {}
    original_bootstrap = AgentRuntimeBridge.bootstrap

    def capture_bootstrap(self: Any, *args: Any, **kwargs: Any) -> Any:
        session = original_bootstrap(self, *args, **kwargs)
        observed["bridge"] = self
        observed["session"] = session
        return session

    def stop_before_source_intake(self: Any) -> None:
        raise RuntimeError("stop after normalized runtime spec publication")

    monkeypatch.setattr(AgentRuntimeBridge, "bootstrap", capture_bootstrap)
    monkeypatch.setattr(AgentRuntimeBridge, "build_source_bundle", stop_before_source_intake)
    runner_result = AgentRuntimeRunner(spec).run()
    assert runner_result.failed_stage == "source_intake", runner_result

    session = observed["session"]
    route_factory = InternalStageExecutorRegistry(observed["bridge"])
    workspace = session.context.workspace
    registry = session.context.registry
    spec_record = registry.get("runtime_job_spec")
    assert spec_record is not None and spec_record.status == "ready"
    registry.verify_ready_artifact_closure(spec_record)
    persisted_spec = json.loads(Path(spec_record.path).read_text(encoding="utf-8"))
    config_binding = spec_record.metadata["config_snapshot_binding"]
    assert config_binding["config_source_id"] == str(config_path.resolve())
    assert config_binding["config_source_sha256"] == file_sha256(config_path)
    assert config_binding["effective_config_sha256"] == session.context.fingerprint_bundle["config_hash"]
    assert config_binding["normalized_spec_payload_sha256"] == hash_json(persisted_spec)
    runtime_binding = read_runtime_spec_binding_v1(
        registry,
        expected_effective_config_sha256=session.context.fingerprint_bundle["config_hash"],
    )

    route_by_role = {}
    for role, setting_key in ROLE_SETTING_KEYS.items():
        section_name = str(session.stage_host.config["OutlineModels"][setting_key])
        route = route_factory._outline_route_for_section(
            session=session,
            role=role,
            section_name=section_name,
        )
        assert route is not None
        route_by_role[role] = route
    candidate_route = route_by_role["candidate_provider_generation"]
    assert isinstance(candidate_route.transport, _OutlineProviderTransportAdapter)
    assert isinstance(candidate_route.transport.api_config, Mapping)
    assert candidate_route.transport.profile == candidate_route.profile

    summaries = [_summary(
        "repair-paper",
        "Repair Paper",
        "The treatment improved the outcome in the tested context.",
    )]

    router = OutlineProviderRouter(
        routes=route_by_role,
        diagnostics=collect_routing_diagnostics(route_by_role),
    )

    def reject_legacy_transport(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("the legacy provider callback must not handle routed repair")

    executor = OutlineV3Executor(
        job_id=workspace.job_id,
        summaries=summaries,
        workspace=workspace,
        artifact_registry=registry,
        provider=reject_legacy_transport,
        provider_router=router,
        candidate_count=1,
        stability_mode="off",
        semantic_repair_enabled=True,
        runtime_spec_binding=runtime_binding,
        publication_context=LocalPublicationContext(),
    )
    # Persist the actual initial provider plan used by the primary exposure.
    # This readback is scoped to this fixture and makes no whole-stage cost claim.
    executor._preflight_stability_budget()
    initial_provider_plan = registry.get("outline-v3:provider_call_plan:off")
    assert initial_provider_plan is not None and initial_provider_plan.status == "ready"

    bind_production_writer_sources(executor, [])
    from services.writer_source_inventory import load_writer_source_inventory_v1

    source_inventory = load_writer_source_inventory_v1(registry)
    assert "repair-paper" in source_inventory.paper_by_key()
    evidence_record = registry.get("outline-v3:outline_evidence_views")
    assert evidence_record is not None and evidence_record.status == "ready"
    layers_record = registry.get("outline-v3:outline_content_layers")
    assert layers_record is not None and layers_record.status == "ready"
    verified_layers = registry.verify_ready_artifact_closure(layers_record)
    assert verified_layers.content_hash == layers_record.content_hash
    return executor, route_by_role, runtime_binding, config_path, layers_record


def _repair_call(
    executor: OutlineV3Executor,
    paper_key: str = "repair-paper",
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    original, request = _repair_input(paper_key)
    node_id = "candidate_1_semantic_repair"
    result = executor._provider_call(
        node_id,
        request,
        input_artifact_hashes=(_hash_payload(original),),
        transport_node_id="candidate_1_provider_generation",
    )
    return node_id, original, result


def _assert_primary_repair_exposure(executor: OutlineV3Executor) -> None:
    plan = executor._primary_candidate_repair_plan
    plan_record = executor._primary_candidate_repair_plan_record
    assert plan is not None and plan_record is not None
    assert plan_record.status == "ready"
    assert plan_record.artifact_type == "outline_candidate_repair_plan"
    assert plan_record.artifact_id == plan.cardinality_basis().basis_artifact
    assert plan_record.content_hash == file_sha256(plan_record.path)
    assert plan_record.metadata["scope"] == "primary"

    exposure_id = f"outline-v3:primary-repair-exposure:{plan.contract_sha256}"
    exposure_record = executor.registry.get(exposure_id)
    assert exposure_record is not None and exposure_record.status == "ready"
    assert exposure_record.artifact_type == "outline_primary_repair_exposure"
    executor.registry.verify_ready_artifact_closure(exposure_record)
    dependency_ids = {item.artifact_id for item in exposure_record.depends_on}
    assert plan_record.artifact_id in dependency_ids
    assert "outline-v3:provider_call_plan:off" in dependency_ids

    payload = json.loads(Path(exposure_record.path).read_text(encoding="utf-8"))
    assert payload["schema_version"] == "outline-primary-repair-exposure/v1"
    assert payload["scope"] == "primary"
    assert payload["plan_contract_sha256"] == plan.contract_sha256
    exposure = payload["exposure"]
    basis = exposure["cardinality_basis"]
    assert exposure["exposure_status"] == "bounded_conditional"
    assert exposure["logical_calls_upper_bound"] == plan.maximum_repair_calls == 1
    assert basis["maximum_count"] == 1
    assert basis["basis_artifact"] == plan_record.artifact_id
    assert basis["basis_artifact_sha256"] == plan_record.content_hash
    assert basis["basis_artifact_sha256"] == file_sha256(plan_record.path)
    assert exposure["wall_seconds_per_call_upper_bound"] == plan.wall_seconds_per_call_upper_bound
    assert executor.stability_preflight["primary_candidate_repair_exposure"] == exposure


def test_runtime_bound_primary_repair_materializes_exact_wire_row_and_replays(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    local_repair_provider: _LocalRepairProvider,
) -> None:
    executor, _routes, runtime_binding, _config_path, layers_record = _prepare_runtime_executor(
        tmp_path,
        monkeypatch,
        local_repair_provider.api_base,
    )
    node_id, original, repaired = _repair_call(executor)

    assert repaired["candidate_id"] == "candidate_1"
    assert repaired["sections"][0]["section_id"] == "candidate_1_section_1"
    assert len(local_repair_provider.calls) == 1
    assert local_repair_provider.errors == []
    wire = local_repair_provider.calls[0]
    assert wire["path"] == "/v1/chat/completions"
    assert wire["model"] == "gpt-4.1-mini"
    assert wire["envelope"]["node_id"] == node_id
    wire_request = wire["envelope"]["request"]
    assert wire_request["_prompt_authority"]["node_id"] == node_id
    binding = executor._dynamic_provider_bindings[node_id]
    assert binding["prompt_payload_hash"] == hash_json(wire_request)
    assert executor.runtime_spec_binding == runtime_binding
    assert executor.registry.verify_ready_artifact_closure(layers_record).content_hash == layers_record.content_hash

    plan = executor._primary_candidate_repair_plan
    route = executor._node_route("candidate_1_provider_generation")
    assert plan is not None
    row = plan.materialize_request_row(
        "candidate_1",
        wire_request,
        route=route,
        route_config_fingerprint_sha256=route.safe_config_fingerprint(),
        profile=route.profile,
        retry_attempts=0,
        wall_seconds_upper_bound=plan.wall_seconds_per_call_upper_bound,
        config_source_id=runtime_binding.config_source_id,
        config_source_sha256=runtime_binding.config_source_sha256,
        runtime_spec_sha256=runtime_binding.normalized_spec_artifact_sha256,
        canonical_source_authority_id=plan.canonical_source_authority_id,
        canonical_source_authority_sha256=plan.canonical_source_authority_sha256,
    )
    assert row.request_estimate.request_hash == binding["prompt_payload_hash"]
    assert row.wall_seconds_upper_bound == plan.wall_seconds_per_call_upper_bound
    _assert_primary_repair_exposure(executor)

    executor._persist_repair_output(
        node_id,
        repaired,
        dependency_hashes={"candidate": _hash_payload(original)},
    )
    executor.runtime_spec_binding = None
    replayed = executor._provider_call(
        node_id,
        _repair_input("repair-paper")[1],
        input_artifact_hashes=(_hash_payload(original),),
        transport_node_id="candidate_1_provider_generation",
    )
    assert replayed == repaired
    assert len(local_repair_provider.calls) == 1
    assert executor._replay_evidence[-1]["provider_invoked"] is False
    assert executor._replay_evidence[-1]["lookup_status"] == "hit"


@pytest.mark.parametrize(
    ("drift", "expected_error", "message"),
    [
        pytest.param("config", RuntimeSpecBindingError, "hash does not match current bytes", id="config-source"),
        pytest.param("spec", RuntimeSpecBindingError, "ready closure is invalid", id="runtime-spec"),
        pytest.param("source", WriterSourceInventoryError, "dependency closure is not ready", id="content-layers"),
        pytest.param("metadata", RuntimeSpecBindingError, "config snapshot binding is missing", id="missing-metadata"),
        pytest.param("profile", OutlineV3ExecutionError, "repair envelope changed", id="route-profile"),
        pytest.param("deadline", OutlineV3ExecutionError, "repair envelope changed", id="route-deadline"),
    ],
)
def test_primary_repair_runtime_drift_rejects_before_http_transport(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    local_repair_provider: _LocalRepairProvider,
    drift: str,
    expected_error: type[Exception],
    message: str,
) -> None:
    executor, routes, _runtime_binding, config_path, layers_record = _prepare_runtime_executor(
        tmp_path,
        monkeypatch,
        local_repair_provider.api_base,
    )
    node_id = "candidate_1_semantic_repair"
    original, request_payload = _repair_input("repair-paper")

    if drift in {"profile", "deadline"}:
        executor._initialize_primary_candidate_repair_plan()
        current_route = routes["candidate_provider_generation"]
        current_transport = current_route.transport
        assert isinstance(current_transport, _OutlineProviderTransportAdapter)
        api_config = dict(current_transport.api_config)
        profile = current_route.profile
        if drift == "profile":
            profile = type(profile).conservative(
                provider=profile.provider,
                model=profile.model,
                endpoint_type=profile.endpoint_type,
                model_context_limit=profile.model_context_limit,
                max_output_tokens=max(32, profile.max_output_tokens // 2),
                reasoning_reserve=profile.reasoning_reserve,
                safety_margin=profile.safety_margin,
            )
        else:
            api_config["total_timeout_seconds"] = "4"
        changed_transport = _OutlineProviderTransportAdapter(
            api_config=api_config,
            profile=profile,
            logger=logging.getLogger("tests.candidate-repair-runtime-binding"),
            system_prompt=current_transport.system_prompt,
        )
        routes["candidate_provider_generation"] = type(current_route)(
            role=current_route.role,
            config_section=current_route.config_section,
            provider_name=current_route.provider_name,
            model=current_route.model,
            endpoint_type=current_route.endpoint_type,
            profile=profile,
            transport=changed_transport,
            api_base=current_route.api_base,
            config_identity=api_config,
        )
    elif drift == "config":
        config_path.write_bytes(config_path.read_bytes() + b"\n# changed after runtime acceptance\n")
    elif drift == "spec":
        spec_record = executor.registry.get("runtime_job_spec")
        assert spec_record is not None
        Path(spec_record.path).write_bytes(Path(spec_record.path).read_bytes() + b" ")
    elif drift == "source":
        Path(layers_record.path).write_bytes(Path(layers_record.path).read_bytes() + b" ")
    elif drift == "metadata":
        executor.registry.update_record(
            "runtime_job_spec",
            metadata_updates={"config_snapshot_binding": None},
        )
    else:
        raise AssertionError(f"unknown drift case: {drift}")

    with pytest.raises(expected_error, match=message):
        executor._provider_call(
            node_id,
            request_payload,
            input_artifact_hashes=(_hash_payload(original),),
            transport_node_id="candidate_1_provider_generation",
        )
    assert local_repair_provider.calls == []
    assert executor._transport_call_count == 0


def test_opaque_transport_cannot_create_a_finite_primary_repair_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    local_repair_provider: _LocalRepairProvider,
) -> None:
    executor, routes, _runtime_binding, _config_path, _layers_record = _prepare_runtime_executor(
        tmp_path,
        monkeypatch,
        local_repair_provider.api_base,
    )
    current_route = routes["candidate_provider_generation"]
    calls: list[str] = []

    def opaque_transport(node_id: str, _request: Mapping[str, Any]) -> Mapping[str, Any]:
        calls.append(node_id)
        raise AssertionError("an opaque external transport must not be called")

    routes["candidate_provider_generation"] = type(current_route)(
        role=current_route.role,
        config_section=current_route.config_section,
        provider_name=current_route.provider_name,
        model=current_route.model,
        endpoint_type=current_route.endpoint_type,
        profile=current_route.profile,
        transport=opaque_transport,
        api_base=current_route.api_base,
        config_identity=current_route.config_identity,
    )

    with pytest.raises(
        OutlineV3ExecutionError,
        match="requires a runtime-owned transport configuration",
    ):
        executor._initialize_primary_candidate_repair_plan()
    assert executor._primary_candidate_repair_plan is None
    assert not any(
        record.artifact_type in {
            "outline_candidate_repair_plan",
            "outline_primary_repair_exposure",
        }
        for record in executor.registry.list_records()
    )
    assert calls == []
    assert local_repair_provider.calls == []


def test_explicit_offline_testdeps_keep_opaque_repair_in_component_lane(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    local_repair_provider: _LocalRepairProvider,
) -> None:
    from runtime.test_dependencies import RuntimeTestDependencies

    executor, routes, runtime_binding, _config_path, _layers_record = _prepare_runtime_executor(
        tmp_path,
        monkeypatch,
        local_repair_provider.api_base,
    )
    assert executor.runtime_spec_binding == runtime_binding
    assert executor._primary_candidate_repair_plan is None
    assert executor._primary_candidate_repair_plan_record is None

    current_route = routes["candidate_provider_generation"]
    calls: list[str] = []

    def opaque_component_adapter(node_id: str, _request: Mapping[str, Any]) -> Mapping[str, Any]:
        calls.append(node_id)
        raise AssertionError("explicit external-transport-disabled component lane cannot call transport")

    opaque_route = type(current_route)(
        role=current_route.role,
        config_section=current_route.config_section,
        provider_name=current_route.provider_name,
        model=current_route.model,
        endpoint_type=current_route.endpoint_type,
        profile=current_route.profile,
        transport=opaque_component_adapter,
        api_base=current_route.api_base,
        config_identity=current_route.config_identity,
    )
    routes["candidate_provider_generation"] = opaque_route
    dependencies = RuntimeTestDependencies(external_transport_disabled=True)
    monkeypatch.setattr(
        "runtime.test_dependencies.current_runtime_test_dependencies",
        lambda: dependencies,
    )

    original, request_payload = _repair_input("repair-paper")
    node_id = "candidate_1_semantic_repair"
    attached_request = executor._attach_prompt_authority(node_id, request_payload)
    binding = executor._provider_binding(
        node_id,
        attached_request,
        expect_json=True,
        input_artifact_hashes=(_hash_payload(original),),
        route=opaque_route,
    )
    executor._materialize_primary_repair_request(
        node_id,
        attached_request,
        opaque_route,
        binding,
    )

    assert executor._primary_candidate_repair_plan is None
    assert executor._primary_candidate_repair_plan_record is None
    assert "primary_candidate_repair_exposure" not in executor.stability_preflight
    assert not any(
        record.artifact_type in {
            "outline_candidate_repair_plan",
            "outline_primary_repair_exposure",
        }
        for record in executor.registry.list_records()
    )
    assert calls == []
    assert local_repair_provider.calls == []
