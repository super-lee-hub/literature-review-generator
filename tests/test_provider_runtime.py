from __future__ import annotations

import json

import ai_interface
from runtime.provider_context import ProviderContextProfile
from runtime.provider_runtime import (
    ProviderBudgetExceeded,
    ProviderBudgetV1,
    ProviderRuntime,
    ProviderRuntimeLedger,
    canonical_provider_request_payload,
    hash_json,
)


def test_provider_runtime_enforces_budget_and_emits_redacted_append_only_receipt(tmp_path) -> None:
    ledger = ProviderRuntimeLedger(tmp_path / "provider_receipts.jsonl")
    runtime = ProviderRuntime(
        budget=ProviderBudgetV1(max_calls=1, max_total_tokens=20),
        ledger=ledger,
        job_id="job-1",
        attempt_id="attempt-1",
        stage_name="analyze",
        route="Primary_Reader_API",
        node_id="paper-1",
        call_id="call-1",
        endpoint_type="chat_completions",
    )

    admission = runtime.admit(estimated_tokens=10)
    receipt = runtime.complete(
        admission=admission,
        prompt="private prompt",
        input_payload={"text": "private input"},
        api_config={
            "api_key": "super-secret",
            "model": "test-model",
            "api_base": "https://provider.example/v1",
        },
        result={"status": "success", "content": {"answer": "ok"}},
        metadata={
            "authorization": "Bearer super-secret",
            "safe": "value",
            "request_budget": {
                "max_output_tokens": 32000,
                "api_token": "nested-secret",
            },
            "requested_output_tokens": 32000,
        },
    )

    assert receipt.status == "success"
    assert receipt.prompt_hash != "private prompt"
    assert receipt.metadata["authorization"] == "[REDACTED_SECRET]"
    assert receipt.metadata["request_budget"]["max_output_tokens"] == 32000
    assert receipt.metadata["request_budget"]["api_token"] == "[REDACTED_SECRET]"
    assert receipt.metadata["requested_output_tokens"] == 32000
    persisted = ledger.list_receipts()
    assert persisted == (receipt,)
    assert "super-secret" not in (tmp_path / "provider_receipts.jsonl").read_text(encoding="utf-8")

    try:
        runtime.admit(estimated_tokens=1)
    except ProviderBudgetExceeded as exc:
        assert "call budget" in str(exc)
    else:
        raise AssertionError("second admission should be rejected by max_calls")


def test_provider_runtime_budget_mapping_is_fail_closed_for_invalid_limits() -> None:
    budget = ProviderBudgetV1.from_mapping(
        {"max_calls": "not-a-number", "max_total_tokens": "-4", "max_elapsed_seconds": "bad"}
    )
    assert budget == ProviderBudgetV1()


def test_ai_detailed_call_attaches_durable_provider_receipt(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        ai_interface,
        "_call_ai_api_detailed_uninstrumented",
        lambda *args, **kwargs: {"status": "success", "content": {"answer": "ok"}},
    )
    ledger = ProviderRuntimeLedger(tmp_path / "receipts.jsonl")
    runtime = ProviderRuntime(
        ledger=ledger,
        job_id="job-1",
        attempt_id="attempt-1",
        stage_name="analyze",
        test_only=True,
    )
    result = ai_interface._call_ai_api_detailed(
        "prompt",
        {
            "api_key": "secret",
            "model": "test-model",
            "api_base": "https://provider.example/v1",
        },
        "system",
        provider_runtime=runtime,
    )

    assert result["status"] == "success"
    assert result["provider_receipt"]["job_id"] == "job-1"
    assert len(ledger.list_receipts()) == 1
    assert json.loads((tmp_path / "receipts.jsonl").read_text(encoding="utf-8"))["status"] == "success"


def test_ai_detailed_call_blocks_before_transport_when_budget_is_exhausted(monkeypatch) -> None:
    called = False

    def fail_if_called(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("transport should not run after budget admission failure")

    monkeypatch.setattr(ai_interface, "_call_ai_api_detailed_uninstrumented", fail_if_called)
    runtime = ProviderRuntime(budget=ProviderBudgetV1(max_total_tokens=1), test_only=True)
    result = ai_interface._call_ai_api_detailed(
        "a long prompt",
        {"api_key": "secret", "model": "test-model", "api_base": "https://provider.example/v1"},
        "system",
        provider_runtime=runtime,
    )

    assert called is False
    assert result["status"] == "failed"
    assert result["error_kind"] == "budget_exhausted"
    assert result["provider_receipt"]["status"] == "failed"


def test_ai_detailed_call_enforces_retry_budget_and_records_attempts(monkeypatch) -> None:
    calls = 0

    def fail_transport(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise ai_interface.requests.exceptions.ConnectionError("temporary network failure")

    monkeypatch.setattr(ai_interface, "_post_with_proxy_mode", fail_transport)
    monkeypatch.setattr(ai_interface, "load_config", lambda: {"Performance": {"api_retry_attempts": "5"}})
    runtime = ProviderRuntime(budget=ProviderBudgetV1(max_retries_per_call=1), test_only=True)

    result = ai_interface._call_ai_api_detailed(
        "prompt",
        {"api_key": "secret", "model": "test-model", "api_base": "https://provider.example/v1"},
        "system",
        provider_runtime=runtime,
    )

    assert result["status"] == "failed"
    assert result["error_kind"] == "transient_network"
    assert calls == 2
    assert result["attempts"] == 2
    assert result["provider_receipt"]["attempts"] == 2


def test_low_level_http_mock_receipt_hash_matches_actual_multimodal_request(monkeypatch, tmp_path) -> None:
    image_path = tmp_path / "page.jpg"
    image_path.write_bytes(b"fake-image-bytes")
    captured: dict[str, object] = {}

    class Response:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {
                "choices": [
                    {
                        "message": {"content": '{"answer":"ok"}'},
                        "finish_reason": "stop",
                    }
                ]
            }

    def post(url: str, **kwargs: object) -> Response:
        captured["url"] = url
        captured["payload"] = kwargs["json"]
        return Response()

    monkeypatch.setattr(ai_interface, "_post_with_proxy_mode", post)
    ledger = ProviderRuntimeLedger(tmp_path / "receipts.jsonl")
    runtime = ProviderRuntime(
        ledger=ledger,
        job_id="job-1",
        attempt_id="attempt-1",
        stage_name="stage1_analyze",
        route="Primary_Reader_API",
        node_id="paper-1",
        call_id="stage1_synthesis:paper-1",
        endpoint_type="chat_completions",
    )
    config = {
        "api_key": "secret",
        "model": "deepseek-v4-flash-vision-exp",
        "api_base": "https://api.deepseek.com/v1",
        "provider_family": "deepseek",
    }
    user_content = [
        {"type": "text", "text": "use the image"},
        {"type": "local_image_path", "path": str(image_path), "visual_id": "page-001", "page_no": 1},
    ]
    result = ai_interface._call_ai_api_detailed(
        "prompt",
        config,
        "system",
        user_content=user_content,
        provider_runtime=runtime,
    )

    assert result["status"] == "success"
    assert captured["url"] == "https://api.deepseek.com/v1/chat/completions"
    payload = captured["payload"]
    assert isinstance(payload, dict)
    content = payload["messages"][1]["content"]
    assert any(item.get("type") == "image_url" for item in content)
    receipt = ledger.list_receipts()[0]
    expected = canonical_provider_request_payload(
        prompt="prompt",
        system_prompt="system",
        user_content=user_content,
        response_format="json",
        max_output_tokens=4000,
        temperature=0.3,
    )
    assert receipt.input_hash == hash_json(expected)
    assert receipt.metadata["images_actually_sent_count"] == 1
    assert receipt.metadata["successful_input_mode"] == "multimodal"


def test_low_level_http_mock_budget_omission_matches_receipt_and_payload(monkeypatch, tmp_path) -> None:
    first_image = tmp_path / "first.jpg"
    second_image = tmp_path / "second.jpg"
    first_image.write_bytes(b"first-image")
    second_image.write_bytes(b"second-image")
    captured: dict[str, object] = {}

    class Response:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {
                "choices": [
                    {
                        "message": {"content": '{"answer":"ok"}'},
                        "finish_reason": "stop",
                    }
                ]
            }

    def post(url: str, **kwargs: object) -> Response:
        captured["payload"] = kwargs["json"]
        return Response()

    monkeypatch.setattr(ai_interface, "_post_with_proxy_mode", post)
    ledger = ProviderRuntimeLedger(tmp_path / "receipts.jsonl")
    runtime = ProviderRuntime(
        ledger=ledger,
        job_id="job-1",
        attempt_id="attempt-1",
        stage_name="stage1_analyze",
        route="Primary_Reader_API",
        node_id="paper-1",
        call_id="stage1_synthesis:paper-1",
        endpoint_type="chat_completions",
    )
    config = {
        "api_key": "secret",
        "model": "deepseek-v4-flash-vision-exp",
        "api_base": "https://api.deepseek.com/v1",
        "provider_family": "deepseek",
    }
    first_ref = {
        "type": "local_image_path",
        "path": str(first_image),
        "visual_id": "page-001",
        "page_no": 1,
    }
    second_ref = {
        "type": "local_image_path",
        "path": str(second_image),
        "visual_id": "page-002",
        "page_no": 2,
    }
    user_content = [{"type": "text", "text": "text evidence"}, first_ref, second_ref]
    result = ai_interface._call_ai_api_detailed(
        "prompt",
        config,
        "system",
        user_content=user_content,
        provider_runtime=runtime,
        max_single_image_bytes=1000,
        max_request_image_bytes=300,
    )

    assert result["status"] == "success"
    payload = captured["payload"]
    assert isinstance(payload, dict)
    content = payload["messages"][1]["content"]
    assert sum(item.get("type") == "image_url" for item in content) == 1
    receipt = ledger.list_receipts()[0]
    assert receipt.input_hash == hash_json(
        canonical_provider_request_payload(
            prompt="prompt",
            system_prompt="system",
            user_content=[{"type": "text", "text": "text evidence"}, first_ref],
            response_format="json",
            max_output_tokens=4000,
            temperature=0.3,
        )
    )
    assert receipt.metadata["sent_visual_ids"] == ["page-001"]
    assert receipt.metadata["omissions"][0]["visual_id"] == "page-002"
    assert receipt.metadata["omissions"][0]["reason"] == "image_exceeds_request_byte_budget"


def test_low_level_http_mock_never_sends_one_child_of_broken_atomic_group(
    monkeypatch,
    tmp_path,
) -> None:
    first_image = tmp_path / "child-a.jpg"
    fallback_image = tmp_path / "page-fallback.jpg"
    first_image.write_bytes(b"child-a")
    fallback_image.write_bytes(b"page-fallback")
    second_image = tmp_path / "child-b-missing.jpg"
    captured: dict[str, object] = {}

    class Response:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {
                "choices": [
                    {
                        "message": {"content": '{"answer":"ok"}'},
                        "finish_reason": "stop",
                    }
                ]
            }

    def post(url: str, **kwargs: object) -> Response:
        captured["payload"] = kwargs["json"]
        return Response()

    monkeypatch.setattr(ai_interface, "_post_with_proxy_mode", post)
    ledger = ProviderRuntimeLedger(tmp_path / "receipts.jsonl")
    runtime = ProviderRuntime(
        ledger=ledger,
        job_id="job-atomic",
        attempt_id="attempt-atomic",
        stage_name="stage1_analyze",
        route="Primary_Reader_API",
        node_id="paper-atomic",
        call_id="stage1_synthesis:paper-atomic",
        endpoint_type="chat_completions",
    )
    group = {
        "raw_reinspection_group_id": "ambiguous-page-1",
        "raw_reinspection_resolution": "all_children",
        "raw_reinspection_atomic": True,
        "ambiguous_candidate_ids": ["child-a", "child-b"],
        "raw_reinspection_selected_ids": ["child-a", "child-b"],
        "raw_reinspection_fallback_ref": {
            "visual_id": "page-1",
            "page_no": 1,
            "artifact_type": "page_snapshot",
            "image_path": str(fallback_image),
        },
    }
    user_content = [
        {"type": "text", "text": "atomic evidence"},
        {"type": "text", "text": "child-a label"},
        {
            "type": "local_image_path",
            "path": str(first_image),
            "visual_id": "child-a",
            "page_no": 1,
            "artifact_type": "figure_crop",
            **group,
        },
        {"type": "text", "text": "child-b label"},
        {
            "type": "local_image_path",
            "path": str(second_image),
            "visual_id": "child-b",
            "page_no": 1,
            "artifact_type": "figure_crop",
            **group,
        },
    ]
    result = ai_interface._call_ai_api_detailed(
        "prompt",
        {
            "api_key": "secret",
            "model": "deepseek-v4-flash-vision-exp",
            "api_base": "https://api.deepseek.com/v1",
            "provider_family": "deepseek",
        },
        "system",
        user_content=user_content,
        provider_runtime=runtime,
        max_single_image_bytes=1_000,
        max_request_image_bytes=10_000,
    )

    assert result["status"] == "success"
    payload = captured["payload"]
    assert isinstance(payload, dict)
    wire_content = payload["messages"][1]["content"]
    wire_images = [item for item in wire_content if item.get("type") == "image_url"]
    assert len(wire_images) == 1
    receipt = ledger.list_receipts()[0]
    transport = result["transport_metadata"]
    assert transport["sent_visual_ids"] == ["page-1"]
    assert transport["raw_reinspection_groups"][0]["resolution"] == "page_snapshot_fallback"
    assert transport["raw_reinspection_groups"][0]["actual_sent_ids"] == ["page-1"]
    assert receipt.metadata["images_actually_sent_count"] == 1


def test_low_level_http_mock_omits_broken_atomic_group_as_a_unit(monkeypatch, tmp_path) -> None:
    first_image = tmp_path / "child-a.jpg"
    first_image.write_bytes(b"child-a")
    second_image = tmp_path / "child-b-missing.jpg"
    captured: dict[str, object] = {}

    class Response:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {
                "choices": [
                    {
                        "message": {"content": '{"answer":"ok"}'},
                        "finish_reason": "stop",
                    }
                ]
            }

    def post(url: str, **kwargs: object) -> Response:
        captured["payload"] = kwargs["json"]
        return Response()

    monkeypatch.setattr(ai_interface, "_post_with_proxy_mode", post)
    runtime = ProviderRuntime(
        ledger=ProviderRuntimeLedger(tmp_path / "receipts.jsonl"),
        job_id="job-atomic-no-fallback",
        attempt_id="attempt-atomic-no-fallback",
        stage_name="stage1_analyze",
        route="Primary_Reader_API",
        node_id="paper-atomic-no-fallback",
        call_id="stage1_synthesis:paper-atomic-no-fallback",
        endpoint_type="chat_completions",
    )
    group = {
        "raw_reinspection_group_id": "ambiguous-page-2",
        "raw_reinspection_resolution": "all_children",
        "raw_reinspection_atomic": True,
        "ambiguous_candidate_ids": ["child-a", "child-b"],
        "raw_reinspection_selected_ids": ["child-a", "child-b"],
    }
    result = ai_interface._call_ai_api_detailed(
        "prompt",
        {
            "api_key": "secret",
            "model": "deepseek-v4-flash-vision-exp",
            "api_base": "https://api.deepseek.com/v1",
            "provider_family": "deepseek",
        },
        "system",
        user_content=[
            {"type": "text", "text": "atomic evidence"},
            {
                "type": "local_image_path",
                "path": str(first_image),
                "visual_id": "child-a",
                "page_no": 2,
                **group,
            },
            {
                "type": "local_image_path",
                "path": str(second_image),
                "visual_id": "child-b",
                "page_no": 2,
                **group,
            },
        ],
        provider_runtime=runtime,
        max_single_image_bytes=1_000,
        max_request_image_bytes=10_000,
    )

    assert result["status"] == "success"
    payload = captured["payload"]
    assert isinstance(payload, dict)
    wire_content = payload["messages"][1]["content"]
    assert all(item.get("type") != "image_url" for item in wire_content)
    transport = result["transport_metadata"]
    assert transport["sent_visual_ids"] == []
    assert transport["raw_reinspection_groups"][0]["resolution"] == "not_represented"
    assert transport["omissions"][0]["raw_reinspection_group_id"] == "ambiguous-page-2"


def test_http_parameter_mutation_is_recorded_without_changing_logical_request_hash(
    monkeypatch,
    tmp_path,
) -> None:
    captured_payloads: list[dict[str, object]] = []

    class Response:
        def __init__(self, status_code: int, payload: dict[str, object]) -> None:
            self.status_code = status_code
            self._payload = payload
            self.text = json.dumps(payload)

        def raise_for_status(self) -> None:
            if self.status_code >= 400:
                raise ai_interface.requests.exceptions.HTTPError(response=self)

        def json(self) -> dict[str, object]:
            return self._payload

    def post(url: str, **kwargs: object) -> Response:
        payload = kwargs["json"]
        assert isinstance(payload, dict)
        captured_payloads.append(payload)
        if len(captured_payloads) == 1:
            return Response(
                400,
                {
                    "error": {
                        "code": "unsupported_parameter",
                        "message": "unsupported parameter: temperature",
                    }
                },
            )
        return Response(
            200,
            {
                "choices": [
                    {
                        "message": {"content": '{"answer":"ok"}'},
                        "finish_reason": "stop",
                    }
                ]
            },
        )

    monkeypatch.setattr(ai_interface, "_post_with_proxy_mode", post)
    ledger = ProviderRuntimeLedger(tmp_path / "receipts.jsonl")
    runtime = ProviderRuntime(
        budget=ProviderBudgetV1(max_retries_per_call=1),
        ledger=ledger,
        job_id="job-1",
        attempt_id="attempt-1",
        stage_name="stage1_analyze",
        route="Primary_Reader_API",
        node_id="paper-1",
        call_id="stage1_synthesis:paper-1",
        endpoint_type="chat_completions",
    )
    config = {
        "api_key": "secret",
        "model": "test-model",
        "api_base": "https://api.deepseek.com/v1",
        "provider_family": "generic",
        "transport_retries": "3",
    }
    result = ai_interface._call_ai_api_detailed(
        "prompt",
        config,
        "system",
        provider_runtime=runtime,
    )

    assert result["status"] == "success"
    assert len(captured_payloads) == 2
    assert "temperature" in captured_payloads[0]
    assert "temperature" not in captured_payloads[1]
    receipt = ledger.list_receipts()[0]
    assert receipt.attempts == 2
    assert receipt.fallback_or_payload_mutations == ("removed_temperature",)
    assert receipt.input_hash == hash_json(
        canonical_provider_request_payload(
            prompt="prompt",
            system_prompt="system",
            user_content=None,
            response_format="json",
            max_output_tokens=4000,
            temperature=0.3,
        )
    )


def test_backup_http_mock_is_text_only_and_receipt_route_is_backup(monkeypatch, tmp_path) -> None:
    image_path = tmp_path / "page.jpg"
    image_path.write_bytes(b"fake-image-bytes")
    captured: dict[str, object] = {}

    class Response:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {"choices": [{"message": {"content": '{"answer":"ok"}'}, "finish_reason": "stop"}]}

    def post(url: str, **kwargs: object) -> Response:
        captured["payload"] = kwargs["json"]
        return Response()

    monkeypatch.setattr(ai_interface, "_post_with_proxy_mode", post)
    ledger = ProviderRuntimeLedger(tmp_path / "receipts.jsonl")
    runtime = ProviderRuntime(
        ledger=ledger,
        job_id="job-1",
        attempt_id="attempt-1",
        stage_name="stage1_analyze",
        route="Primary_Reader_API",
        node_id="paper-1",
        call_id="stage1_synthesis:paper-1",
        endpoint_type="chat_completions",
    )
    primary = {
        "api_key": "primary-secret",
        "model": "deepseek-v4-flash-vision-exp",
        "api_base": "https://api.deepseek.com/v1",
        "provider_family": "deepseek",
    }
    backup = {
        "api_key": "backup-secret",
        "model": "deepseek-v4-flash",
        "api_base": "https://api.deepseek.com/v1",
        "provider_family": "deepseek",
    }
    user_content = [
        {"type": "text", "text": "text evidence"},
        {"type": "local_image_path", "path": str(image_path), "visual_id": "page-001", "page_no": 1},
    ]
    result = ai_interface.get_summary_from_ai_detailed(
        "prompt",
        primary,
        backup,
        engine_type="backup",
        user_content=user_content,
        provider_runtime=runtime,
        normalize_summary=False,
    )

    assert result["status"] == "success"
    payload = captured["payload"]
    content = payload["messages"][1]["content"]
    assert all(item.get("type") != "image_url" for item in content)
    receipt = ledger.list_receipts()[0]
    assert receipt.route == "Backup_Reader_API"
    assert receipt.input_hash == hash_json(
        canonical_provider_request_payload(
            prompt="prompt",
            system_prompt=ai_interface._load_stage1_system_prompt(),
            user_content=[{"type": "text", "text": "text evidence"}],
            response_format="json",
            max_output_tokens=8192,
            temperature=0.3,
        )
    )
    assert receipt.metadata["images_actually_sent_count"] == 0
    assert receipt.metadata["successful_input_mode"] == "text_only"


def test_provider_context_admission_counts_nested_evidence_and_reserves() -> None:
    profile = ProviderContextProfile(
        provider="test",
        model="test-model",
        endpoint_type="responses",
        model_context_limit=2_048,
        verified_context_limit=1_600,
        input_budget=500,
        max_output_tokens=400,
        reasoning_reserve=100,
        safety_margin=100,
    )
    base = profile.estimate_request(
        {
            "evidence_views": [{"paper_key": "paper-1", "text": "short"}],
            "relation_candidates": [{"relation_id": "r1"}],
            "review_intent": {"question": "compare"},
        }
    )
    expanded = profile.estimate_request(
        {
            "evidence_views": [{"paper_key": "paper-1", "text": "x" * 900}],
            "relation_candidates": [{"relation_id": "r1", "evidence": "y" * 300}],
            "review_intent": {"question": "compare", "coverage_contract": "z" * 300},
        }
    )

    assert expanded["estimated_input_tokens"] > base["estimated_input_tokens"]
    assert expanded["within_budget"] is False
    assert expanded["estimated_total_tokens"] == (
        expanded["estimated_input_tokens"]
        + profile.max_output_tokens
        + profile.reasoning_reserve
        + profile.safety_margin
    )
