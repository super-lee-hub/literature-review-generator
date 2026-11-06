from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, Mapping

import pytest

import ai_interface
from runtime.provider_context import validator_output_token_limit
from runtime.provider_runtime import ProviderRuntime, canonical_provider_request_payload, hash_json
from validation.adjudication_reuse import _receipt_matches_payload, _request_payload
from validation.llm_adjudicator import AdjudicationPacket
import validation.llm_adjudicator as llm_adjudicator


def _packet(stage: str = "stronger") -> AdjudicationPacket:
    return AdjudicationPacket(
        citation_set_key="set-output",
        stage=stage,
        claim_text="The cited result is supported.",
        claim_context="",
        block_context="",
        claim_type="result",
        claim_type_confidence=0.9,
        claim_type_rationale="",
        paper_ids=["paper-1"],
        claim_units=[],
        target_claim_unit={},
        claim_unit_results=[],
        paper_identity_hints={},
        per_paper_evidence_packets={},
        evidence_excerpt_list=[],
        trimmed_candidate_counts={},
        evidence_status="evidence_gap",
        disposition="manual_review",
    )


@pytest.mark.parametrize(
    ("endpoint_type", "model", "wire_field", "response"),
    [
        (
            "chat_completions",
            "validator-fixture-chat",
            "max_tokens",
            {"choices": [{"message": {"content": '{"ok":true}'}, "finish_reason": "stop"}]},
        ),
        (
            "responses",
            "gpt-4.1-mini",
            "max_output_tokens",
            {"output_text": '{"ok":true}', "status": "completed"},
        ),
        (
            "anthropic",
            "claude-3-5-sonnet-latest",
            "max_tokens",
            {"content": [{"type": "text", "text": '{"ok":true}'}], "stop_reason": "end_turn"},
        ),
    ],
)
def test_validator_wire_output_override_matches_canonical_reservation_and_receipt(
    monkeypatch: pytest.MonkeyPatch,
    endpoint_type: str,
    model: str,
    wire_field: str,
    response: Mapping[str, Any],
) -> None:
    sent: list[dict[str, Any]] = []

    class FakeResponse:
        status_code = 200
        headers = {"content-type": "application/json"}

        def __init__(self) -> None:
            self._body = json.dumps(response).encode("utf-8")
            self.content = self._body

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return dict(response)

        def iter_content(self, chunk_size: int):
            del chunk_size
            yield self._body

        def close(self) -> None:
            return None

    def fake_post(_url: str, *, api_config: Mapping[str, Any], **kwargs: Any) -> FakeResponse:
        del api_config
        sent.append(dict(kwargs["json"]))
        return FakeResponse()

    monkeypatch.setattr(ai_interface, "_post_with_proxy_mode", fake_post)
    config = {
        "api_key": "fixture-secret",
        "api_base": "https://provider.example/v1",
        "provider_family": "anthropic" if endpoint_type == "anthropic" else "openai",
        "endpoint_type": endpoint_type,
        "model": model,
        "max_context_tokens": "32768",
        "max_output_tokens": "4096",
    }
    result = ai_interface._call_ai_api_detailed(
        "validation prompt",
        config,
        "validation system prompt",
        max_tokens=6144,
        wire_output_tokens=6144,
        response_format="json",
        retry_attempts=1,
        provider_runtime=ProviderRuntime(test_only=True),
    )

    assert result["status"] == "success", result
    assert len(sent) == 1
    assert sent[0][wire_field] == 6144
    receipt = result["provider_receipt"]
    assert receipt["metadata"]["requested_output_tokens"] == 6144
    assert receipt["metadata"]["requested_output_tokens_per_attempt"] == 6144
    expected_payload = canonical_provider_request_payload(
        prompt="validation prompt",
        system_prompt="validation system prompt",
        user_content=None,
        response_format="json",
        max_output_tokens=6144,
        temperature=0.3,
    )
    assert receipt["input_hash"] == hash_json(expected_payload)


def test_wire_output_over_reservation_or_explicit_hard_limit_blocks_before_post(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int] = []
    monkeypatch.setattr(ai_interface, "_post_with_proxy_mode", lambda *_args, **_kwargs: calls.append(1))
    cases = [
        (
            {
                "api_key": "fixture-secret",
                "api_base": "https://provider.example/v1",
                "provider_family": "openai",
                "endpoint_type": "responses",
                "model": "gpt-4.1-mini",
                "max_context_tokens": "32768",
                "max_output_tokens": "6144",
            },
            4096,
        ),
        (
            {
                "api_key": "fixture-secret",
                "api_base": "https://provider.example/v1",
                "provider_family": "openai",
                "endpoint_type": "responses",
                "model": "gpt-4.1-mini",
                "max_context_tokens": "32768",
                "max_output_tokens": "8192",
                "provider_max_output_tokens": "6144",
            },
            8192,
        ),
    ]

    for config, reserved_output in cases:
        result = ai_interface._call_ai_api_detailed(
            "prompt",
            config,
            "system",
            max_tokens=reserved_output,
            response_format="json",
            retry_attempts=1,
            provider_runtime=ProviderRuntime(test_only=True),
        )
        assert result["status"] == "failed"
        assert result["provider_http_attempts_started"] == 0
        assert result["provider_receipt"]["status"] != "success"
    assert calls == []


def test_stage_output_target_clamps_explicit_caps_and_changes_reuse_identity() -> None:
    packet = _packet()
    default_config = {"max_output_tokens": "4096"}
    capped_config = {
        "max_output_tokens": "4096",
        "provider_max_output_tokens": "4096",
        "max_output_tokens_limit": "6144",
    }
    uncapped_request = _request_payload(packet, default_config)
    capped_request = _request_payload(packet, capped_config)

    assert uncapped_request["max_output_tokens"] == 6144
    assert capped_request["max_output_tokens"] == 4096
    assert hash_json(uncapped_request) != hash_json(capped_request)


def test_reuse_requires_receipt_output_metadata_and_rejects_old_receipt() -> None:
    payload = {
        "source_receipt_id": "receipt-1",
        "job_id": "job-1",
        "attempt_id": "attempt-1",
        "node_id": "stronger:set-output",
        "call_id": "call-1",
        "source_provider_closure_epoch_id": "epoch-1",
        "prompt_hash": "prompt-hash",
        "input_hash": "input-hash",
        "redacted_provider_config_hash": "config-hash",
        "schema_hash": "schema-hash",
        "normalized_result_hash": "response-hash",
        "requested_output_tokens_per_attempt": 6144,
    }
    receipt_fields = {
        "receipt_id": "receipt-1",
        "job_id": "job-1",
        "attempt_id": "attempt-1",
        "stage_name": "stage4_validate",
        "node_id": "stronger:set-output",
        "call_id": "call-1",
        "closure_epoch_id": "epoch-1",
        "logical_attempt_identity": "attempt-1",
        "prompt_hash": "prompt-hash",
        "input_hash": "input-hash",
        "config_hash": "config-hash",
        "schema_hash": "schema-hash",
        "response_hash": "response-hash",
        "status": "success",
    }

    old_receipt = SimpleNamespace(**receipt_fields, metadata={})
    current_receipt = SimpleNamespace(
        **receipt_fields,
        metadata={"requested_output_tokens_per_attempt": 6144},
    )
    assert not _receipt_matches_payload(old_receipt, payload)
    assert _receipt_matches_payload(current_receipt, payload)


def test_stronger_adjudicator_passes_the_same_output_target_to_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    observed: list[dict[str, Any]] = []

    def capture(_prompt: str, _config: Mapping[str, Any], _system: str, **kwargs: Any) -> dict[str, Any]:
        observed.append(dict(kwargs))
        return {"status": "supported", "confidence": 0.9}

    monkeypatch.setattr(llm_adjudicator, "_call_ai_api", capture)
    result = llm_adjudicator.run_adjudication_stage(
        SimpleNamespace(logger=None),
        {"api_key": "fixture-secret", "model": "validator-model", "max_output_tokens": "4096"},
        _packet(),
    )

    assert result is not None
    assert observed[0]["max_tokens"] == 6144
    assert observed[0]["wire_output_tokens"] == 6144


def test_stronger_adjudicator_applies_explicit_hard_output_ceiling(monkeypatch: pytest.MonkeyPatch) -> None:
    observed: list[dict[str, Any]] = []

    def capture(_prompt: str, _config: Mapping[str, Any], _system: str, **kwargs: Any) -> dict[str, Any]:
        observed.append(dict(kwargs))
        return {"status": "supported", "confidence": 0.9}

    monkeypatch.setattr(llm_adjudicator, "_call_ai_api", capture)
    config = {
        "api_key": "fixture-secret",
        "model": "validator-model",
        "max_output_tokens": "4096",
        "provider_max_output_tokens": "6144",
        "max_output_tokens_limit": "4096",
    }
    assert validator_output_token_limit(config, "stronger") == 4096
    report = llm_adjudicator.run_adjudication_stage(
        SimpleNamespace(logger=None), config, _packet()
    )

    assert report is not None
    assert observed[0]["max_tokens"] == 4096
    assert observed[0]["wire_output_tokens"] == 4096


def test_validator_output_hard_limit_rejects_fractional_value() -> None:
    with pytest.raises(ValueError, match="max_output_tokens_limit"):
        validator_output_token_limit(
            {"max_output_tokens": "4096", "max_output_tokens_limit": 1.5},
            "stronger",
        )
