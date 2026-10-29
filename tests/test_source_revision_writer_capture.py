"""Actual Writer transport preserves source-closed responses for replay."""
from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from runtime import provider_runtime
from runtime.provider_runtime import (
    ProviderAggregateBudgetV2,
    ProviderBudgetController,
    ProviderCallReceiptV1,
    hash_json,
)
from runtime.runner import AgentRuntimeRunner
from runtime.source_revision import PROVIDER_SEMANTIC_CONTRACT_V2
from runtime.source_revision_response_capture import build_source_revision_response_sink
from services import review_generation_service as review_generation_service_module
from services.artifact_registry import ArtifactRegistry
from services.queue_service import LocalPublicationContext
from services.review_draft import (
    ReviewSection,
    build_review_draft,
    validate_review_section_writer_scope,
)
from services.review_generation_service import ReviewGenerationService
from services.writer_source_inventory import load_writer_source_inventory_v1
from services.writer_task_scope import verify_writer_selected_source_scope_v2
from tests.test_review_writer_stage_preflight import (
    _writer_fixture,
)
from tests.writer_source_fixture import scoped_writer_content

_METADATA = {"provider_semantic_contract_version": PROVIDER_SEMANTIC_CONTRACT_V2}
_WRITER_TEXT = "The effect remained stable in the enrolled sample [[cite_ref:R001]]."


def _bind_explicit_local_writer_budget(monkeypatch) -> None:
    controller = ProviderBudgetController(ProviderAggregateBudgetV2(
        max_provider_calls_total=64,
        max_output_tokens_total=100_000,
        max_retry_attempts_total=8,
        max_wall_seconds=3_600,
    ))
    monkeypatch.setattr(
        review_generation_service_module,
        "provider_budget_controller_from_environment",
        lambda: controller,
    )
    monkeypatch.setattr(
        provider_runtime,
        "provider_budget_controller_from_environment",
        lambda: controller,
    )


def _start_loopback_writer_server():
    requests: list[dict[str, Any]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            if self.path != "/v1/responses":
                self.send_error(404, "unexpected Writer endpoint")
                return
            length = int(self.headers.get("Content-Length") or 0)
            request = json.loads(self.rfile.read(length))
            prompt = request["input"][1]["content"][0]["text"]
            prompt_payload = json.loads(prompt)
            section_id = str(prompt_payload["section"]["section_id"])
            content = scoped_writer_content(prompt, _WRITER_TEXT)
            requests.append({
                "section_id": section_id,
                "prompt": prompt,
                "request": request,
                "content": content,
            })
            response = {
                "id": f"writer-response-{len(requests)}",
                "object": "response",
                "status": "completed",
                "model": str(request.get("model") or "writer"),
                "output_text": json.dumps(content, ensure_ascii=False),
                "usage": {"input_tokens": 120, "output_tokens": 40, "total_tokens": 160},
            }
            body = json.dumps(response, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *_args: Any) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, requests


def _loopback_settings(settings, server: ThreadingHTTPServer):
    sections = dict(settings.sections)
    writer_config = dict(sections["Writer_API"])
    writer_config.update({
        "api_base": f"http://127.0.0.1:{server.server_port}/v1",
        "endpoint_type": "responses",
        "provider_family": "openai_responses",
        "transport_retries": "0",
    })
    sections["Writer_API"] = writer_config
    return replace(settings, sections=sections)


def _source_closed_service(template, *, settings, attempt_id: str, registry, context):
    sink = build_source_revision_response_sink(_METADATA, template.workspace, registry, context)
    assert sink is not None
    service = ReviewGenerationService(
        job_id=template.job_id,
        attempt_id=attempt_id,
        workspace=template.workspace,
        artifact_registry=registry,
        settings=settings,
        summaries=template.summaries,
        writer=None,
        publication_context=context,
        response_snapshot_sink=sink,
        provider_semantic_contract_version=PROVIDER_SEMANTIC_CONTRACT_V2,
    )
    return service, sink


def _assert_review_draft_contract(service, sections, catalog) -> None:
    for section in sections:
        assert section["provider_semantic_contract_version"] == PROVIDER_SEMANTIC_CONTRACT_V2
        assert section["writer_task_scope"]["provider_semantic_contract_version"] == PROVIDER_SEMANTIC_CONTRACT_V2
        assert validate_review_section_writer_scope(section)

    draft = build_review_draft(
        job_id=service.job_id,
        project_name=service.workspace.project_name,
        draft_id="source-closed-writer-capture",
        outline_artifact_id="outline-v3:final_outline",
        outline_source_path="outline.json",
        summary_file="summary.json",
        review_word_path="review.docx",
        sections=sections,
        references=[],
        generation_mode="outline_v3",
        citation_ref_catalog=catalog,
    )
    review_section, = draft.content["sections"]
    assert isinstance(review_section, ReviewSection)
    assert review_section.provider_semantic_contract_version == PROVIDER_SEMANTIC_CONTRACT_V2
    assert review_section.to_dict()["provider_semantic_contract_version"] == PROVIDER_SEMANTIC_CONTRACT_V2
    restored, = draft.to_dict()["content"]["sections"]
    assert restored["provider_semantic_contract_version"] == PROVIDER_SEMANTIC_CONTRACT_V2
    assert validate_review_section_writer_scope(restored)


def test_actual_writer_http_captures_exact_source_closed_response_and_replays_without_post(
    tmp_path,
    monkeypatch,
) -> None:
    _bind_explicit_local_writer_budget(monkeypatch)
    template, outline, packets = _writer_fixture(tmp_path, writer=None)
    outline = {**outline, "sections": outline["sections"][:1]}
    packets = packets[:1]
    server, thread, requests = _start_loopback_writer_server()
    try:
        settings = _loopback_settings(template.settings, server)
        context = LocalPublicationContext()
        service, sink = _source_closed_service(
            template,
            settings=settings,
            attempt_id="source-closed-writer-attempt",
            registry=template.registry,
            context=context,
        )
        result = service.run(outline_payload=outline, evidence_packets=packets)

        receipts = service.receipt_ledger.list_receipts()
        assert len(receipts) == len(requests) == len(outline["sections"])
        assert all(receipt.status == "success" and receipt.test_only is False for receipt in receipts)
        assert all(receipt.http_status == 200 for receipt in receipts)
        for request in requests:
            prompt_payload = json.loads(request["prompt"])
            assert (
                prompt_payload["writer_task_scope"]["provider_semantic_contract_version"]
                == PROVIDER_SEMANTIC_CONTRACT_V2
            )

        captured: dict[str, tuple[str, str]] = {}
        for receipt in receipts:
            assert receipt.node_id == receipt.call_id.removeprefix("review:")
            record = sink.require(receipt)
            AgentRuntimeRunner._schema_validators()["provider_response_snapshot"](record, Path(record.path))
            raw_bytes = Path(record.path).read_bytes()
            payload = json.loads(raw_bytes)
            expected = next(row["content"] for row in requests if row["section_id"] == receipt.node_id)
            assert ProviderCallReceiptV1.from_dict(payload["receipt"]).to_dict() == receipt.to_dict()
            assert payload["receipt_hash"] == hash_json(receipt.to_dict())
            assert payload["content"] == expected
            assert payload["response_hash"] == receipt.response_hash == hash_json(expected)
            assert record.content_hash == hashlib.sha256(raw_bytes).hexdigest()
            captured[receipt.node_id] = (record.artifact_id, record.content_hash)

        sections_by_id = {str(section["evidence_packet_id"]): section for section in result.sections}
        assert set(sections_by_id) == {row["section_id"] for row in requests}
        for section_id, section in sections_by_id.items():
            assert section["provider_semantic_contract_version"] == PROVIDER_SEMANTIC_CONTRACT_V2
            assert section["writer_task_scope"]["provider_semantic_contract_version"] == PROVIDER_SEMANTIC_CONTRACT_V2
            assert any(block["text"] == _WRITER_TEXT for block in section["blocks"])
            assert section["provider_receipt_ids"] == [
                receipt.receipt_id for receipt in receipts if receipt.node_id == section_id
            ]
        _assert_review_draft_contract(service, result.sections, result.citation_ref_catalog)

        sent = len(requests)
        fresh_registry = ArtifactRegistry(template.workspace.paths.registry_path, template.job_id)
        fresh_context = LocalPublicationContext()
        replayed, fresh_sink = _source_closed_service(
            template,
            settings=settings,
            attempt_id="source-closed-writer-replay",
            registry=fresh_registry,
            context=fresh_context,
        )
        replay_result = replayed.run(outline_payload=outline, evidence_packets=packets)

        assert len(requests) == sent
        assert replayed.provider_request_inventory is not None
        assert all(row.verified_reuse for row in replayed.provider_request_inventory.requests)
        assert set(replayed._verified_reuse_receipts) == {f"review:{key}" for key in sections_by_id}
        assert replay_result.sections == result.sections
        current_inventory = load_writer_source_inventory_v1(fresh_registry)
        assert current_inventory is not None and current_inventory.is_verified
        for section_id, section in sections_by_id.items():
            scope = next(
                row["writer_task_scope"]
                for row in replay_result.sections
                if row["evidence_packet_id"] == section_id
            )
            assert scope["source_inventory_binding"] == {
                "artifact_id": current_inventory.artifact_id,
                "artifact_hash": current_inventory.artifact_hash,
                "content_hash": current_inventory.content_hash,
            }
            verify_writer_selected_source_scope_v2(
                scope,
                provider_semantic_contract_version=PROVIDER_SEMANTIC_CONTRACT_V2,
            )
            assert fresh_sink.require(next(receipt for receipt in receipts if receipt.node_id == section_id))
        _assert_review_draft_contract(replayed, replay_result.sections, replay_result.citation_ref_catalog)
        for receipt in receipts:
            record = fresh_sink.require(receipt)
            assert (record.artifact_id, record.content_hash) == captured[receipt.node_id]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
