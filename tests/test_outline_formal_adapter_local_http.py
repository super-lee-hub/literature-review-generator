from __future__ import annotations

import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import Any, Mapping

import pytest

from outline.provider_router import OutlineProviderRouter, OutlineRoleRoute, collect_routing_diagnostics
from outline.v3_executor import OutlineV3Executor
from runtime.orchestrator import _OutlineProviderTransportAdapter
from runtime.provider_context import ProviderContextProfile
from services.artifact_registry import ArtifactRegistry
from services.job_workspace import JobWorkspace

from test_outline_v3_semantic_execution import _configured_test_provider, _summary


class _FormalLoopbackProvider:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - stdlib handler contract
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length)
                payload = json.loads(raw.decode("utf-8"))
                owner.requests.append({"bytes": len(raw), "payload": payload})
                content = ""
                messages = payload.get("messages") or []
                if messages:
                    user = messages[-1].get("content") if isinstance(messages[-1], Mapping) else ""
                    if isinstance(user, list):
                        user = " ".join(
                            str(item.get("text") or "")
                            for item in user
                            if isinstance(item, Mapping)
                        )
                    content = str(user or "")
                try:
                    envelope = json.loads(content)
                    node_id = str(envelope.get("node_id") or "")
                    request = envelope.get("request") if isinstance(envelope, Mapping) else {}
                except json.JSONDecodeError:
                    node_id, request = "", {}
                request = request if isinstance(request, Mapping) else {}
                owner.requests[-1].update({"node_id": node_id, "request": dict(request)})
                response = dict(_configured_test_provider(node_id, request))
                if node_id == "relation_adjudication":
                    relation_ids = [
                        str(item.get("relation_id") or "")
                        for item in request.get("relation_candidates") or ()
                        if isinstance(item, Mapping) and str(item.get("relation_id") or "")
                    ]
                    response["content"] = {
                        "confirmed_relation_ids": relation_ids,
                        "rejected_relations": [],
                    }
                body = json.dumps(
                    {
                        "model": "loopback-outline",
                        "usage": {"prompt_tokens": 37, "completion_tokens": 19, "total_tokens": 56},
                        "choices": [{
                            "message": {"content": json.dumps(response.get("content") or {}, ensure_ascii=False)},
                            "finish_reason": "stop",
                        }],
                    },
                    ensure_ascii=False,
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("X-Aihubmix-Request-Id", f"loopback-{len(owner.requests)}")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args: Any) -> None:
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = Thread(target=self.server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}/v1"

    def __enter__(self) -> "_FormalLoopbackProvider":
        self.thread.start()
        return self

    def __exit__(self, *_args: Any) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


@pytest.mark.parametrize(
    ("paper_count", "source_cap", "target_tokens", "call_cap", "transport_retries"),
    [
        (3, None, 1500, 24, 0), (3, None, 1500, 24, 2),
        (6, None, 1500, 24, 2),
        (3, 1000, 1500, 24, 2), (3, 1000, 0, 24, 2),
        (3, None, 1500, 14, 2),
    ],
    ids=["admitted", "physical_retry_budget", "over_call_budget", "over_shard_input_cap", "over_flat_input_cap", "candidate_shard_call_cap"],
)
def test_formal_outline_adapter_uses_loopback_socket_and_real_audit(
    tmp_path: Path, paper_count: int, source_cap: int | None,
    target_tokens: int, call_cap: int, transport_retries: int,
) -> None:
    with _FormalLoopbackProvider() as provider:
        profile = ProviderContextProfile.conservative(
            provider="loopback",
            model="loopback-outline",
            endpoint_type="chat_completions",
            model_context_limit=200_000,
            max_output_tokens=4_096,
        )
        adapter = _OutlineProviderTransportAdapter(
            api_config={
                "api_key": "loopback-key-123",
                "model": "loopback-outline",
                "api_base": provider.base_url,
                "provider_family": "loopback",
                "endpoint_type": "chat_completions",
                "proxy_mode": "direct",
                "transport_retries": str(transport_retries),
                "total_timeout_seconds": "10",
            },
            profile=profile,
            logger=logging.getLogger("test.formal-outline-adapter"),
            system_prompt="Return only JSON.",
        )
        route = OutlineRoleRoute(
            role="relation_adjudication",
            config_section="Loopback_API",
            provider_name="loopback",
            model="loopback-outline",
            endpoint_type="chat_completions",
            profile=profile,
            transport=adapter,
            api_base=provider.base_url,
            config_identity={"api_base": provider.base_url, "transport_retries": str(transport_retries)},
        )
        routes = {role: route for role in (
            "relation_adjudication",
            "candidate_provider_generation",
            "structure_critique",
            "coverage_critique",
            "evidence_critique",
            "arbitration",
        )}
        router = OutlineProviderRouter(routes=routes, diagnostics=collect_routing_diagnostics(routes))
        workspace = JobWorkspace.create(str(tmp_path), "outline", job_id="formal-loopback-outline")
        registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
        executor = OutlineV3Executor(
            job_id=workspace.job_id,
                summaries=[
                    _summary("paper-a", "Study A", "The treatment improved the outcome."),
                    _summary("paper-b", "Study B", "The effect held under a different context."),
                    _summary("paper-c", "Study C", "A boundary condition limits the effect."),
                    _summary("paper-d", "Study D", "The treatment improved the outcome."),
                    _summary("paper-e", "Study E", "The effect held under a different context."),
                    _summary("paper-f", "Study F", "A boundary condition limits the effect."),
                ][:paper_count],
            workspace=workspace,
            artifact_registry=registry,
            provider=lambda *_args: (_ for _ in ()).throw(AssertionError("legacy provider used")),
            provider_router=router,
            candidate_count=1,
            stability_mode="off",
            technical_shard_target_tokens=target_tokens,
            max_provider_calls=call_cap,
            max_source_prompt_tokens=source_cap,
            pricing_source="tests:explicit-rates-v1",
            input_cost_per_1k_tokens=0.0,
            output_cost_per_1k_tokens=0.001,
            reasoning_cost_per_1k_tokens=0.001,
            cache_read_cost_per_1k_tokens=0.0,
            cache_write_cost_per_1k_tokens=0.0,
        )
        result = executor.run()

    if transport_retries and paper_count == 3 and source_cap is None and call_cap == 24:
        assert result.status == "blocked"
        assert provider.requests == []
        preflight_path = next(tmp_path.rglob("stability_preflight_*.json"))
        preflight = json.loads(preflight_path.read_text(encoding="utf-8"))
        assert preflight["estimated_provider_calls"] <= 24
        assert preflight["estimated_provider_physical_attempts_upper_bound"] > 24
        assert preflight["rejection_reason"] == "max_provider_physical_attempts_exceeded"
        return
    if paper_count == 6:
        assert result.status == "blocked"
        assert provider.requests == []
        preflight_path = next(tmp_path.rglob("stability_preflight_*.json"))
        preflight = json.loads(preflight_path.read_text(encoding="utf-8"))
        assert preflight["rejection_reason"] == "max_provider_calls_exceeded"
        assert preflight["estimated_provider_calls"] > preflight["max_provider_calls"] == 24
        assert preflight["hierarchical_relation_shard_calls"] > 0
        return
    if call_cap == 14:
        assert result.status == "blocked"
        assert provider.requests == []
        preflight_path = next(tmp_path.rglob("stability_preflight_*.json"))
        preflight = json.loads(preflight_path.read_text(encoding="utf-8"))
        assert preflight["rejection_reason"] == "max_provider_calls_exceeded"
        assert preflight["hierarchical_candidate_shard_calls"] > 0
        assert len([row for row in preflight["provider_call_plans"] if row["transport_expected"]]) <= call_cap
        assert preflight["hierarchical_relation_shard_calls"] > 0
        assert preflight["hierarchical_candidate_shard_calls"] > 0
        assert call_cap < preflight["estimated_provider_calls"]
        return
    if source_cap is not None:
        assert result.status == "blocked"
        assert provider.requests == []
        preflight_path = next(tmp_path.rglob("stability_preflight_*.json"))
        preflight = json.loads(preflight_path.read_text(encoding="utf-8"))
        assert preflight["rejection_reason"] == "complete_relation_request_exceeds_effective_input_cap"
        assert preflight["transport_posts_emitted"] == 0
        return

    assert result.ok is True, result
    assert len(provider.requests) >= 8
    assert all(item["bytes"] > 0 for item in provider.requests)
    assert all("stream" not in item["payload"] or item["payload"]["stream"] is False for item in provider.requests)
    audit_path = Path(result.artifacts["request_payload_audit"])
    audit_rows = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert audit_rows
    assert all(row["mock_live"] == "live" for row in audit_rows)
    assert all(row["provider_invoked"] for row in audit_rows)
    assert any(
        row["role"] == "relation_adjudication"
        and row["node_id"].startswith("relation_adjudication:batch_")
        and row["relation_candidate_ids"]
        for row in audit_rows
    )
    assert all(row["receipt_ids"] for row in audit_rows)
    cross_requests = [
        item["request"] for item in provider.requests
        if item["node_id"].startswith("relation_adjudication:batch_")
    ]
    assert cross_requests
    assert all(request["relation_evidence_bundles"] for request in cross_requests)
    assert all(
        "digest_type" not in view
        for request in cross_requests for view in request["evidence_views"]
    )
    assert any(
        "The treatment improved the outcome." in json.dumps(request, ensure_ascii=False)
        for request in cross_requests
    )
    preflight_path = next(tmp_path.rglob("stability_preflight_*.json"))
    preflight = json.loads(preflight_path.read_text(encoding="utf-8"))
    assert len(provider.requests) <= preflight["estimated_provider_calls"]
    candidate_posts = sum(
        "candidate_1_provider_generation" in item["node_id"]
        for item in provider.requests
    )
    assert candidate_posts == 1 + preflight["hierarchical_candidate_shard_calls"]
    base_attempts = sum(
        int(row["physical_attempt_upper_bound"])
        for row in preflight["provider_call_plans"] if row["transport_expected"]
    )
    relation_plan = next(
        row for row in preflight["provider_call_plans"]
        if row["node_id"] == "relation_adjudication"
    )
    extra_attempts = (
        preflight["hierarchical_relation_shard_calls"]
        * relation_plan["physical_attempt_upper_bound"]
    )
    candidate_plan = next(
        row for row in preflight["provider_call_plans"]
        if row["node_id"] == "candidate_1_provider_generation"
    )
    candidate_extra_attempts = (
        preflight["hierarchical_candidate_shard_calls"]
        * candidate_plan["physical_attempt_upper_bound"]
    )
    critique_plan = next(
        row for row in preflight["provider_call_plans"]
        if row["node_id"] == "structure_critique"
    )
    critique_extra_attempts = (
        preflight["hierarchical_critique_shard_calls"]
        * critique_plan["physical_attempt_upper_bound"]
    )
    assert preflight["hierarchical_relation_physical_attempts_upper_bound"] == extra_attempts
    assert preflight["hierarchical_candidate_physical_attempts_upper_bound"] == candidate_extra_attempts
    assert preflight["estimated_provider_physical_attempts_upper_bound"] == (
        base_attempts + extra_attempts + candidate_extra_attempts + critique_extra_attempts
    )
    assert preflight["estimated_input_tokens"] >= (
        extra_attempts + candidate_extra_attempts + critique_extra_attempts
    ) * 32_000
    assert preflight["monetary_ceiling_enforced"] is False
    assert preflight["cost_status"] == "unknown"
    graph = json.loads(Path(result.artifacts["hierarchical_call_graph"]).read_text(encoding="utf-8"))
    assert len(graph["provider_calls"]) == len(audit_rows)
    assert any(edge["kind"] == "provider_input" for edge in graph["edges"])
