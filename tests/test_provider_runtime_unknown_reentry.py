"""Regression coverage for an unknown provider outcome on normal Outline resume.

Only the provider edge is faulted. The scale fixture, Runner, Registry, and
public ControlPlane resume path remain production implementations.
"""
from __future__ import annotations

import configparser
import hashlib
import json
import socket
import socketserver
import threading
from pathlib import Path
from typing import Any

import pytest

from runtime.control_plane import ReviewControlPlane
from runtime.job_spec import RuntimeJobSpec, RuntimeSourceSpec
from runtime.provider_runtime import (
    ProviderAggregateBudgetV2,
    ProviderBudgetController,
    ProviderBudgetV1,
    ProviderRuntime,
    ProviderRuntimeContractError,
    ProviderRuntimeLedger,
    hash_json,
)
from runtime.runner import AgentRuntimeRunner
from tests import test_run_first_scale_integration as scale

TARGET_ROUTE = "topic_synthesis_provider:batch:1"


def _recv_request(sock: socket.socket) -> bytes:
    data = bytearray()
    while b"\r\n\r\n" not in data:
        part = sock.recv(65536)
        if not part:
            return bytes(data)
        data.extend(part)
    head, body = bytes(data).split(b"\r\n\r\n", 1)
    content_length = 0
    for line in head.split(b"\r\n")[1:]:
        if line.lower().startswith(b"content-length:"):
            content_length = int(line.split(b":", 1)[1].strip())
    while len(body) < content_length:
        part = sock.recv(65536)
        if not part:
            break
        body += part
    return head + b"\r\n\r\n" + body[:content_length]


def _recv_response(sock: socket.socket) -> tuple[bytes, bytes, int]:
    data = bytearray()
    while b"\r\n\r\n" not in data:
        part = sock.recv(65536)
        if not part:
            return bytes(data), b"", 0
        data.extend(part)
    head, body = bytes(data).split(b"\r\n\r\n", 1)
    content_length: int | None = None
    for line in head.split(b"\r\n")[1:]:
        if line.lower().startswith(b"content-length:"):
            content_length = int(line.split(b":", 1)[1].strip())
    if content_length is None:
        while True:
            part = sock.recv(65536)
            if not part:
                break
            body += part
    else:
        while len(body) < content_length:
            part = sock.recv(65536)
            if not part:
                break
            body += part
        body = body[:content_length]
    status = int(head.split(b"\r\n", 1)[0].split()[1])
    return head, body, status


class _FaultState:
    def __init__(self, backend_port: int, fixture_calls: list[dict[str, Any]]) -> None:
        self.backend_port = backend_port
        self.fixture_calls = fixture_calls
        self.lock = threading.Lock()
        self.all_posts = 0
        self.target_posts = 0
        self.faulted_target_posts = 0
        self.target_events: list[dict[str, Any]] = []


class _Partial200ProxyHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        client = self.request
        client.settimeout(120)
        raw = _recv_request(client)
        if not raw or b"\r\n\r\n" not in raw:
            return
        head, body = raw.split(b"\r\n\r\n", 1)
        try:
            payload = json.loads(body)
            user_text = "\n".join(
                str(message.get("content") or "")
                for message in payload.get("messages", ())
                if isinstance(message, dict) and message.get("role") == "user"
            )
        except (json.JSONDecodeError, AttributeError, TypeError):
            user_text = ""
        is_target = TARGET_ROUTE in user_text or TARGET_ROUTE.encode() in body
        with self.server.state.lock:
            self.server.state.all_posts += 1
            post_index = self.server.state.all_posts
            target_number = 0
            if is_target:
                self.server.state.target_posts += 1
                target_number = self.server.state.target_posts

        # Forward to this test's dedicated loopback fixture only. Never persist
        # request headers, which can include the fixture-only Authorization value.
        forwarded_headers = [
            line for line in head.split(b"\r\n")
            if not line.lower().startswith(b"connection:")
        ]
        forwarded = (
            b"\r\n".join(forwarded_headers + [b"Connection: close"])
            + b"\r\n\r\n" + body
        )
        with socket.create_connection(("127.0.0.1", self.server.state.backend_port), timeout=120) as upstream:
            upstream.settimeout(120)
            upstream.sendall(forwarded)
            response_head, response_body, status = _recv_response(upstream)

        fixture_call = next(
            (row for row in reversed(self.server.state.fixture_calls)
             if row.get("node_id") == TARGET_ROUTE),
            None,
        )
        # Fault the first request and a possible re-entry. The second fault
        # bounds the RED repro before Writer or later Outline calls can run.
        should_fault = is_target and target_number <= 2 and status == 200
        event = {
            "post_index": post_index,
            "target_post_number": target_number or None,
            "fixture_node_id": (fixture_call or {}).get("node_id"),
            "backend_http_status": status,
            "backend_response_bytes": len(response_body),
            "request_body_sha256": hashlib.sha256(body).hexdigest(),
            "user_text_sha256": hashlib.sha256(user_text.encode("utf-8")).hexdigest(),
            "faulted_partial_200_disconnect": should_fault,
        }
        with self.server.state.lock:
            if is_target:
                self.server.state.target_events.append(event)
            if should_fault:
                self.server.state.faulted_target_posts += 1

        wire_head = response_head + b"\r\n\r\n"
        if should_fault:
            partial_body = response_body[:max(1, min(64, len(response_body) - 1))]
            client.sendall(wire_head + partial_body)
            return
        client.sendall(wire_head + response_body)


class _LoopbackProxy(socketserver.ThreadingTCPServer):
    allow_reuse_address = False
    daemon_threads = True
    request_queue_size = 64


def _runtime_snapshot(workspace_path: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    workspace, _registry = AgentRuntimeRunner._open_workspace(workspace_path)
    budget_path = Path(workspace.log_path("acceptance_budget_state_v1.json"))
    budget = json.loads(budget_path.read_text(encoding="utf-8"))
    receipts: dict[str, dict[str, Any]] = {}
    artifact_dir = Path(workspace_path) / "artifacts"
    for ledger_path in artifact_dir.glob("outline_v3_provider_receipts*.jsonl"):
        for line in ledger_path.read_text(encoding="utf-8").splitlines():
            try:
                receipt = json.loads(line)
            except json.JSONDecodeError:
                continue
            if receipt.get("route") != TARGET_ROUTE:
                continue
            receipts[receipt["receipt_id"]] = receipt
    return budget, list(receipts.values())


def _budget_projection(state: dict[str, Any]) -> dict[str, Any]:
    return {
        key: state.get(key)
        for key in (
            "budget", "acceptance_run_id", "calls_used", "calls_reserved",
            "output_tokens_used", "output_tokens_reserved",
            "retry_attempts_used", "retry_attempts_reserved",
            "absolute_deadline_epoch",
        )
    }


_UNIT_PROMPT = "unit-only synthetic provider receipt fixture; no transport is invoked"
_UNIT_INPUT = {"source": "synthetic-unit-fixture"}
_UNIT_API_CONFIG = {
    "provider_family": "unit_fixture",
    "model": "unit_fixture",
    "api_base": "http://127.0.0.1:1/v1",
    "endpoint_type": "chat_completions",
}


def _unit_runtime(
    ledger: ProviderRuntimeLedger,
    *,
    job_id: str = "unit-job",
    stage_name: str = "outline_v3",
    node_id: str = "topic_synthesis",
    call_id: str = TARGET_ROUTE,
    logical_attempt_identity: str = "logical-attempt-1",
    physical_attempt_id: str = "physical-attempt-1",
    aggregate_budget: ProviderBudgetController | None = None,
    response_snapshot_sink: Any = None,
) -> ProviderRuntime:
    controller = aggregate_budget or ProviderBudgetController(ProviderAggregateBudgetV2(
        max_provider_calls_total=8,
        max_output_tokens_total=512,
        max_retry_attempts_total=4,
        max_wall_seconds=600,
    ))
    return ProviderRuntime(
        budget=ProviderBudgetV1(max_calls=8, max_total_tokens=512),
        aggregate_budget=controller,
        ledger=ledger,
        job_id=job_id,
        attempt_id=physical_attempt_id,
        stage_name=stage_name,
        route="unit_fixture_route",
        node_id=node_id,
        call_id=call_id,
        closure_epoch_id="unit-closure-epoch",
        logical_attempt_identity=logical_attempt_identity,
        endpoint_type="chat_completions",
        response_snapshot_sink=response_snapshot_sink,
    )


def _unit_receipt(runtime: ProviderRuntime, *, status: str, error_kind: str | None = None) -> Any:
    admission = runtime.admit(estimated_tokens=3, requested_output_tokens=8)
    runtime.mark_transport_started(admission)
    result: dict[str, Any] = {"status": status, "content": {"answer": "known"}}
    if error_kind is not None:
        result.update({"error_kind": error_kind, "http_status": 200})
    else:
        result.update({"input_tokens": 1, "output_tokens": 1, "total_tokens": 2})
    return runtime.complete(
        admission=admission,
        prompt=_UNIT_PROMPT,
        input_payload=_UNIT_INPUT,
        api_config=_UNIT_API_CONFIG,
        result=result,
    )


def test_unit_unknown_receipt_blocks_same_logical_call_before_reservation(tmp_path: Path) -> None:
    ledger = ProviderRuntimeLedger(tmp_path / "unit-provider-receipts.jsonl")
    aggregate = ProviderBudgetController(ProviderAggregateBudgetV2(
        max_provider_calls_total=8,
        max_output_tokens_total=512,
        max_retry_attempts_total=4,
        max_wall_seconds=600,
    ))
    original = _unit_runtime(
        ledger,
        physical_attempt_id="physical-attempt-1",
        aggregate_budget=aggregate,
    )
    receipt = _unit_receipt(original, status="failed", error_kind="outcome_unknown")
    assert receipt.error_kind == "outcome_unknown"
    assert receipt.http_status == 200

    resumed = _unit_runtime(
        ledger,
        physical_attempt_id="physical-attempt-2",
        aggregate_budget=aggregate,
    )
    before_budget = _budget_projection(aggregate.snapshot())
    before_ledger = ledger.path.read_bytes()
    before_runtime = (
        resumed.calls, resumed.reserved_tokens, resumed.consumed_tokens,
        resumed.unknown_exposure_tokens,
    )

    with pytest.raises(ProviderRuntimeContractError, match="outcome_unknown"):
        resumed.admit(estimated_tokens=3, requested_output_tokens=8)

    assert _budget_projection(aggregate.snapshot()) == before_budget
    assert ledger.path.read_bytes() == before_ledger
    assert (
        resumed.calls, resumed.reserved_tokens, resumed.consumed_tokens,
        resumed.unknown_exposure_tokens,
    ) == before_runtime


@pytest.mark.parametrize("scope_change", ["job_id", "node_id", "logical_attempt_identity"])
def test_unit_unknown_receipt_does_not_block_an_independent_scope(
    tmp_path: Path, scope_change: str,
) -> None:
    ledger = ProviderRuntimeLedger(tmp_path / "unit-provider-receipts.jsonl")
    original = _unit_runtime(ledger, physical_attempt_id="physical-attempt-1")
    receipt = _unit_receipt(original, status="failed", error_kind="outcome_unknown")
    changed = {scope_change: f"independent-{scope_change}"}
    resumed = _unit_runtime(
        ledger,
        physical_attempt_id="physical-attempt-2",
        **changed,
    )

    admission = resumed.admit(estimated_tokens=1, requested_output_tokens=2)

    assert admission.sequence == 1
    assert receipt.error_kind == "outcome_unknown"
    assert resumed.calls == 1


def test_unit_changed_logical_identity_cannot_resend_same_request_hash(tmp_path: Path) -> None:
    ledger = ProviderRuntimeLedger(tmp_path / "unit-provider-receipts.jsonl")
    original = _unit_runtime(ledger, physical_attempt_id="physical-attempt-1")
    receipt = _unit_receipt(original, status="failed", error_kind="outcome_unknown")
    resumed = _unit_runtime(
        ledger,
        physical_attempt_id="physical-attempt-2",
        logical_attempt_identity="logical-attempt-2",
    )

    with pytest.raises(ProviderRuntimeContractError, match="outcome_unknown"):
        resumed.admit(
            estimated_tokens=1,
            requested_output_tokens=2,
            request_hash=hash_json(_UNIT_INPUT),
        )

    assert receipt.input_hash == hash_json(_UNIT_INPUT)
    assert resumed.calls == 0


def test_unit_changed_logical_identity_allows_different_request_hash(tmp_path: Path) -> None:
    ledger = ProviderRuntimeLedger(tmp_path / "unit-provider-receipts.jsonl")
    original = _unit_runtime(ledger, physical_attempt_id="physical-attempt-1")
    receipt = _unit_receipt(original, status="failed", error_kind="outcome_unknown")
    resumed = _unit_runtime(
        ledger,
        physical_attempt_id="physical-attempt-2",
        logical_attempt_identity="logical-attempt-2",
    )
    different_request_hash = hash_json({"source": "different synthetic unit input"})
    assert different_request_hash != receipt.input_hash

    admission = resumed.admit(
        estimated_tokens=1,
        requested_output_tokens=2,
        request_hash=different_request_hash,
    )

    assert admission.sequence == 1
    assert resumed.calls == 1


@pytest.mark.integration
@pytest.mark.parametrize("stage_name", ["stage3_review", "stage4_validate"])
def test_http_unknown_request_cannot_bypass_guard_with_new_runner_attempt(
    tmp_path: Path, stage_name: str,
) -> None:
    import ai_interface
    from runtime.provider_runtime import (
        ProviderAggregateBudgetV1, ProviderBudgetController, ProviderRuntime,
        ProviderRuntimeContractError, ProviderRuntimeLedger,
    )
    from tests.test_provider_local_http_integration import _LocalProvider, _config

    controller = ProviderBudgetController(ProviderAggregateBudgetV1(
        max_provider_calls_total=3, max_output_tokens_total=64,
    ))
    ledger = ProviderRuntimeLedger(tmp_path / "same-request.jsonl")

    def runtime(attempt: str) -> ProviderRuntime:
        return ProviderRuntime(
            aggregate_budget=controller, ledger=ledger, job_id="unknown-request-job",
            attempt_id=attempt, logical_attempt_identity=attempt,
            stage_name=stage_name, route="local-http", node_id="artifact-backed-node",
            call_id="artifact-backed-call", endpoint_type="chat_completions",
        )

    with _LocalProvider([("partial_close", b'{"choices":[')]) as provider:
        config = _config(provider.base_url)
        kwargs = {
            "max_tokens": 8, "retry_attempts": 0, "timeout_seconds": 0.2,
            "system_prompt": "Return JSON.", "response_format": "json",
        }
        first = ai_interface._call_ai_api_detailed(
            "The same artifact-backed request.", config,
            provider_runtime=runtime("physical-and-logical-one"), **kwargs,
        )
        assert first["error_kind"] == "outcome_unknown"
        before = controller.snapshot()
        ledger_before = ledger.path.read_bytes()
        assert before["calls_used"] == 1 < 3
        with pytest.raises(ProviderRuntimeContractError, match="outcome_unknown.*reconciliation"):
            ai_interface._call_ai_api_detailed(
                "The same artifact-backed request.", config,
                provider_runtime=runtime("physical-and-logical-two"), **kwargs,
            )
        assert len(provider.requests) == 1
        after = controller.snapshot()
        for field in ("calls_used", "calls_reserved", "output_tokens_used", "retry_attempts_used"):
            assert after[field] == before[field]
        assert ledger.path.read_bytes() == ledger_before


def test_normal_resume_does_not_resend_finalized_unknown_outline_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AUTO_GENERATE_OFFLINE_TESTS", "1")
    monkeypatch.setenv("AUTO_GENERATE_RUN_LIVE_API", "0")
    papers = scale._scale_papers(10)
    pdf_dir = tmp_path / "papers"
    pdf_dir.mkdir()
    for paper in papers:
        scale._write_scale_pdf(pdf_dir / f"{paper.stem}.pdf", paper)

    fixture, fixture_thread, _fixture_base = scale._start_scale_server(papers)
    state = _FaultState(fixture.server_address[1], fixture.calls)
    proxy = _LoopbackProxy(("127.0.0.1", 0), _Partial200ProxyHandler)
    proxy.state = state
    proxy_thread = threading.Thread(target=proxy.serve_forever, daemon=True)
    proxy_thread.start()
    try:
        api_base = f"http://127.0.0.1:{proxy.server_address[1]}/v1"
        config_path = scale._local_config(tmp_path, api_base, provider_call_limit=300)
        parsed_config = configparser.ConfigParser(interpolation=None)
        parsed_config.read(config_path, encoding="utf-8")
        assert all(
            section.get("api_base", "").startswith("http://127.0.0.1:")
            for _name, section in parsed_config.items()
            if section.get("api_base")
        )

        budget = {
            "schema_version": "provider-aggregate-budget-contract-v2",
            "max_provider_calls_total": 300,
            "max_output_tokens_total": 5_000_000,
            "max_retry_attempts_total": 0,
            "max_wall_seconds": 600,
        }
        required_comparisons = scale._scale_required_comparisons(papers, "baseline")
        review_intent = {
            "required_comparisons": [
                {
                    "paper_keys": pair,
                    "question": "Compare the two source-reported findings within their recorded sample boundaries.",
                    "relation_type": "reported_magnitude_contrast",
                }
                for pair in required_comparisons
            ]
        }
        spec = RuntimeJobSpec(
            project_name="provider-runtime-unknown-reentry",
            source=RuntimeSourceSpec(mode="direct", pdf_folder=str(pdf_dir)),
            job_id="provider-runtime-unknown-reentry",
            config=str(config_path),
            action="run_all",
            queue_file=str(tmp_path / "queue.json"),
            metadata={"review_intent": review_intent, "provider_budget": budget},
        )
        control = ReviewControlPlane(repo_root=Path(__file__).resolve().parents[1])
        first = AgentRuntimeRunner(spec).run()
        assert first.job_status == "failed", first
        assert first.failed_stage == "outline", first
        assert state.target_posts == 1, state.target_events
        assert state.faulted_target_posts == 1, state.target_events
        assert state.target_events[0]["fixture_node_id"] == TARGET_ROUTE
        assert state.target_events[0]["backend_http_status"] == 200

        budget_before, receipts_before = _runtime_snapshot(first.workspace_path)
        original_unknown = next(
            receipt for receipt in receipts_before
            if receipt.get("error_kind") == "outcome_unknown"
            and receipt.get("http_status") == 200
        )
        posts_before_resume = state.all_posts
        target_posts_before_resume = state.target_posts

        # Exercise the public normal resume on the exact same durable workspace.
        resume_result: dict[str, Any] | None = None
        resume_error: BaseException | None = None
        try:
            resume_result = control.resume(workspace=first.workspace_path)
        except (RuntimeError, ProviderRuntimeContractError) as exc:
            # A future explicit guard may reject resume as a public exception;
            # admission must still identify the unresolved provider outcome.
            resume_error = exc

        diagnostic = (
            f"{type(resume_error).__name__}: {resume_error}"
            if resume_error is not None
            else json.dumps(resume_result, ensure_ascii=False, default=str)
        ).casefold()
        assert "outcome_unknown" in diagnostic or "reconciliation" in diagnostic, {
            "resume_result": resume_result,
            "resume_exception": (
                f"{type(resume_error).__name__}: {resume_error}"
                if resume_error is not None else None
            ),
        }

        budget_after, receipts_after = _runtime_snapshot(first.workspace_path)
        assert state.target_posts == target_posts_before_resume, {
            "new_target_posts": state.target_events[target_posts_before_resume:],
            "all_posts_before_resume": posts_before_resume,
            "all_posts_after_resume": state.all_posts,
        }
        assert state.all_posts == posts_before_resume
        assert state.faulted_target_posts == 1
        original_after = next(
            receipt for receipt in receipts_after
            if receipt.get("receipt_id") == original_unknown.get("receipt_id")
        )
        assert original_after.get("error_kind") == "outcome_unknown"
        assert original_after.get("http_status") == 200
        assert _budget_projection(budget_after) == _budget_projection(budget_before)
    finally:
        proxy.shutdown()
        proxy.server_close()
        proxy_thread.join(timeout=3)
        fixture.shutdown()
        fixture.server_close()
        fixture_thread.join(timeout=3)
