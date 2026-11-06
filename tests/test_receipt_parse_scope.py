from __future__ import annotations

import json
import os
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from runtime import artifact_validators as validators
from runtime.artifact_validators import receipt_parse_scope
from runtime.provider_runtime import ProviderCallReceiptV1, ProviderRuntime, ProviderBudgetV1


def _payload(receipt_id: str, job_id: str = "job-a") -> dict:
    runtime = ProviderRuntime(
        budget=ProviderBudgetV1(max_calls=1), job_id=job_id,
        attempt_id="fixture", stage_name="stage1_analyze", test_only=True,
    )
    result = runtime.complete(
        admission=runtime.admit(estimated_tokens=1), prompt="local fixture",
        input_payload={"value": 1},
        api_config={"model": "fixture", "api_base": "http://127.0.0.1:1/v1"},
        result={"status": "success", "content": {"value": 1}},
    )
    return {**asdict(result), "receipt_id": receipt_id}


def test_same_bytes_nested_scope_and_changed_bytes_are_bounded(tmp_path, monkeypatch):
    first = json.dumps(_payload("r1"), separators=(",", ":")) + "\n"
    changed = json.dumps(_payload("r2"), separators=(",", ":")) + "\n"
    one, two = tmp_path / "one.jsonl", tmp_path / "two.jsonl"
    one.write_text(first, encoding="utf-8")
    two.write_text(first, encoding="utf-8")
    calls = []
    original_decode = ProviderCallReceiptV1.from_dict

    def decode(payload):
        calls.append(dict(payload))
        return original_decode(payload)

    monkeypatch.setattr(ProviderCallReceiptV1, "from_dict", staticmethod(decode))
    record = SimpleNamespace(artifact_version="v1")
    with receipt_parse_scope():
        validators._validate_receipt_ledger(record, one)
        with receipt_parse_scope():
            validators._validate_receipt_ledger(record, two)
        previous_stat = two.stat()
        two.write_text(changed, encoding="utf-8")
        os.utime(two, ns=(previous_stat.st_atime_ns, previous_stat.st_mtime_ns))
        validators._validate_receipt_ledger(record, two)
        assert len(calls) == 2
    validators._validate_receipt_ledger(record, one)
    assert len(calls) == 3


def test_cached_syntax_never_skips_record_contract_or_invalid_version(tmp_path):
    path = tmp_path / "ledger.jsonl"
    payload = _payload("receipt")
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    with receipt_parse_scope():
        validators._validate_receipt_ledger(SimpleNamespace(artifact_version="v1"), path)
        with pytest.raises(validators.ArtifactSchemaError, match="artifact_version v1"):
            validators._validate_receipt_ledger(SimpleNamespace(artifact_version="v2"), path)
        with pytest.raises(validators.ArtifactSchemaError):
            validators._validate_stage1_portable_provider_ledger(
                SimpleNamespace(artifact_version="v1", metadata={}), path,
            )
        payload["artifact_version"] = "invalid"
        path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
        with pytest.raises(validators.ArtifactSchemaError, match="not provider_call_receipt v2"):
            validators._validate_receipt_ledger(SimpleNamespace(artifact_version="v1"), path)
