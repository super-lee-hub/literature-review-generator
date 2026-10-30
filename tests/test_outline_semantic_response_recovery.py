from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import outline.semantic_response_recovery as recovery_module
import pytest
from outline.semantic_response_recovery import find_verified_topic_response

from runtime.provider_runtime import ProviderRuntimeLedger, hash_json

H_PROMPT = "a" * 64
H_PAYLOAD = "b" * 64
H_SCHEMA = "c" * 64
H_CONFIG = "d" * 64


class _Registry:
    def __init__(self, record: object, source: object) -> None:
        self.records = [record, source]
        self.job_id = "job-1"
        self.verify_calls = 0
        self.revision = 1

    def list_records(self):
        return list(self.records)

    def get(self, artifact_id: str):
        return next((record for record in self.records if record.artifact_id == artifact_id), None)

    def capture_record_snapshot(self):
        return self.revision, list(self.records)

    def verify_ready_artifact_closure(self, record, *, external_registry_resolver=None):
        self.verify_calls += 1
        return record


class _Ledger:
    def __init__(self, receipt: object) -> None:
        self.receipt = receipt

    def list_receipts(self):
        return (self.receipt,)


def _case(tmp_path: Path, *, raw_override: bytes | None = None, receipt_override=None):
    root = tmp_path / "workspace"
    root.mkdir()
    raw = json.dumps(
        {
            "id": "chatcmpl-1",
            "model": "fixture-model",
            "choices": [
                {
                    "message": {"role": "assistant", "content": '{"topics": [{"topic_id": "t1"}]}'},
                    "finish_reason": "stop",
                }
            ],
        },
        separators=(",", ":"),
    ).encode()
    path = root / "raw.json"
    path.write_bytes(raw_override if raw_override is not None else raw)
    raw_hash = hashlib.sha256(raw).hexdigest()
    content = {"topics": [{"topic_id": "t1"}]}
    normalized_hash = hash_json(content)
    audit = {
        "schema_version": "outline_request_payload_audit/v1",
        "job_id": "job-1",
        "stage_name": "outline_v3",
        "provider_invoked": True,
        "status": "success",
        "semantic_validation_status": "accepted",
        "physical_attempt_id": "receipt-1",
        "closure_epoch_id": "epoch-1",
        "operation_id": "operation-1",
        "node_id": "topic_synthesis_provider:batch:56",
        "attempt_id": "outline:topic_synthesis_provider:batch:56",
        "payload_hash": H_PAYLOAD,
        "replay_key_hash": "replay-hash",
        "route": {"provider": "fixture", "model": "fixture-model", "endpoint_type": "chat_completions", "api_base_host": "fixture-endpoint"},
        "route_fingerprint": "route-fingerprint",
        "config_hash": H_CONFIG,
        "schema_hash": H_SCHEMA,
        "input_artifact_hashes": ["input-a", "input-b"],
        "receipt_ids": ["receipt-1"],
        "raw_response_refs": [{"path": str(path), "sha256": raw_hash, "bytes": len(raw), "normalized_response_hash": normalized_hash}],
    }
    audit_path = root / "audit.jsonl"
    audit_bytes = (json.dumps(audit, separators=(",", ":")) + "\n").encode()
    audit_path.write_bytes(audit_bytes)
    record = SimpleNamespace(
        artifact_id="audit-1",
        artifact_type="outline_request_payload_audit",
        artifact_version="v1",
        status="ready",
        job_id="job-1",
        path=str(audit_path),
        content_hash=hashlib.sha256(audit_bytes).hexdigest(),
    )
    receipt = SimpleNamespace(
        artifact_type="provider_call_receipt",
        artifact_version="v2",
        receipt_id="receipt-1",
        attempt_id="outline:topic_synthesis_provider:batch:56",
        route="topic_synthesis_provider:batch:56",
        job_id="job-1",
        stage_name="outline_v3",
        node_id="topic_synthesis_provider:batch:56",
        call_id="outline:topic_synthesis_provider:batch:56",
        status="success",
        http_status=200,
        prompt_hash=H_PROMPT,
        schema_hash=H_SCHEMA,
        config_hash=H_CONFIG,
        input_hash=H_PAYLOAD,
        response_hash=normalized_hash,
        finished_at="2026-10-24T00:00:00Z",
        started_at="2026-10-24T00:00:00Z",
        finish_reason="stop",
        incomplete_reason="",
        provider="fixture",
        model="fixture-model",
        endpoint_type="chat_completions",
        metadata={"response_protocol": "json", "content_type": "application/json", "route_fingerprint": "route-fingerprint"},
        endpoint="fixture-endpoint",
        closure_epoch_id="epoch-1",
        logical_attempt_identity="operation-1",
        fallback_or_payload_mutations=(),
        first_token_at="",
        timeout_kind="",
        usage_status="unreported",
        prompt_id="",
        prompt_version="",
        prompt_sha256="",
        test_only=False,
        sequence=1,
        attempts=1,
        budget={},
    )
    if receipt_override:
        receipt = SimpleNamespace(**{**vars(receipt), **receipt_override})
    if receipt.status != "success":
        receipt.error_kind = "invalid_response"
    source_record = SimpleNamespace(
        artifact_id="provider-ledger",
        artifact_type="provider_receipt_ledger",
        artifact_version="v1",
        status="ready",
        job_id="job-1",
        path=str(root / "ledger.jsonl"),
        content_hash="",
    )
    ledger_bytes = (json.dumps(vars(receipt), separators=(",", ":")) + "\n").encode()
    Path(source_record.path).write_bytes(ledger_bytes)
    source_record.content_hash = hashlib.sha256(ledger_bytes).hexdigest()
    return root, _Registry(record, source_record), _Ledger(receipt), source_record


def test_positive_parser_and_receipt_binding(tmp_path: Path) -> None:
    root, registry, ledger, source = _case(tmp_path)
    verified_receipt = ProviderRuntimeLedger(source.path).list_receipts()[0]
    verified = find_verified_topic_response(
        registry, ledger, "job-1", "topic_synthesis_provider:batch:56", "replay-hash", H_PAYLOAD,
        H_PROMPT, H_SCHEMA, H_CONFIG, {"provider": "fixture", "model": "fixture-model", "endpoint_type": "chat_completions", "api_base_host": "fixture-endpoint"},
        "route-fingerprint", ["input-b", "input-a"], root,
        receipt_source_records={"receipt-1": source},
        verified_receipts={"receipt-1": verified_receipt},
    )
    assert verified is not None
    assert verified.payload == {"topics": [{"topic_id": "t1"}]}
    assert verified.normalized_hash == verified.raw_ref["normalized_response_hash"]
    assert registry.verify_calls == 2


def test_positive_accepts_semantic_base_receipt_node(tmp_path: Path) -> None:
    root, registry, ledger, source = _case(tmp_path, receipt_override={"node_id": "topic_synthesis"})
    verified_receipt = ProviderRuntimeLedger(source.path).list_receipts()[0]
    verified = find_verified_topic_response(
        registry, ledger, "job-1", "topic_synthesis_provider:batch:56", "replay-hash", H_PAYLOAD,
        H_PROMPT, H_SCHEMA, H_CONFIG, {"provider": "fixture", "model": "fixture-model", "endpoint_type": "chat_completions", "api_base_host": "fixture-endpoint"},
        "route-fingerprint", ["input-a", "input-b"], root,
        receipt_source_records={"receipt-1": source},
        verified_receipts={"receipt-1": verified_receipt},
    )
    assert verified is not None


@pytest.mark.parametrize("mutation", ["raw", "audit", "source", "registry"])
def test_late_mutation_after_parse_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str) -> None:
    root, registry, ledger, source = _case(tmp_path)
    audit_path = Path(registry.records[0].path)
    raw_path = Path(json.loads(audit_path.read_text(encoding="utf-8")).get("raw_response_refs")[0]["path"])
    source_path = Path(source.path)
    original = recovery_module.reparse_provider_response_spool

    def parse_then_mutate(*args, **kwargs):
        parsed = original(*args, **kwargs)
        if mutation == "raw":
            raw_path.write_bytes(raw_path.read_bytes() + b"late")
        elif mutation == "audit":
            audit_path.write_bytes(audit_path.read_bytes() + b"late\n")
        elif mutation == "source":
            source_path.write_bytes(source_path.read_bytes() + b"late\n")
        else:
            registry.revision = 2
        return parsed

    monkeypatch.setattr(recovery_module, "reparse_provider_response_spool", parse_then_mutate)
    verified_receipt = ProviderRuntimeLedger(source.path).list_receipts()[0]
    assert find_verified_topic_response(
        registry, ledger, "job-1", "topic_synthesis_provider:batch:56", "replay-hash", H_PAYLOAD,
        H_PROMPT, H_SCHEMA, H_CONFIG, {"provider": "fixture", "model": "fixture-model", "endpoint_type": "chat_completions", "api_base_host": "fixture-endpoint"},
        "route-fingerprint", ["input-a", "input-b"], root,
        receipt_source_records={"receipt-1": source}, verified_receipts={"receipt-1": verified_receipt},
    ) is None


@pytest.mark.parametrize(
    "kwargs",
    [
        {"raw_override": b"tampered"},
        {"receipt_override": {"job_id": "foreign"}},
        {"receipt_override": {"status": "failed"}},
        {"receipt_override": {"http_status": 500}},
        {"receipt_override": {"prompt_hash": "e" * 64}},
        {"receipt_override": {"schema_hash": "e" * 64}},
        {"receipt_override": {"config_hash": "e" * 64}},
        {"receipt_override": {"node_id": "other"}},
    ],
)
def test_tamper_or_identity_mismatch_is_a_cache_miss(tmp_path: Path, kwargs) -> None:
    root, registry, ledger, source = _case(tmp_path, **kwargs)
    verified_receipt = ProviderRuntimeLedger(source.path).list_receipts()[0]
    assert find_verified_topic_response(
        registry, ledger, "job-1", "topic_synthesis_provider:batch:56", "replay-hash", H_PAYLOAD,
        H_PROMPT, H_SCHEMA, H_CONFIG, {"provider": "fixture", "model": "fixture-model", "endpoint_type": "chat_completions", "api_base_host": "fixture-endpoint"},
        "route-fingerprint", ["input-a", "input-b"], root,
        receipt_source_records={"receipt-1": source},
        verified_receipts={"receipt-1": verified_receipt},
    ) is None
