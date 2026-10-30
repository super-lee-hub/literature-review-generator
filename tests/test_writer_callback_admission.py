from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from runtime import provider_runtime, test_dependencies
from runtime.provider_runtime import (
    ProviderAggregateBudgetV2,
    ProviderBudgetController,
    ProviderBudgetExceeded,
    ProviderRuntimeContractError,
)
from runtime.test_dependencies import RuntimeTestDependencies
from services import review_generation_service
from services.artifact_registry import ArtifactRegistry, file_sha256
from services.review_generation_service import ReviewGenerationService
from tests.test_writer_native_table_chain import _service as _native_table_service
from validation import closure as validation_closure


def _strict_controller(*, calls: int = 1) -> ProviderBudgetController:
    return ProviderBudgetController(
        ProviderAggregateBudgetV2(
            max_provider_calls_total=calls,
            max_output_tokens_total=100_000,
            max_retry_attempts_total=5,
            # The native fixture uses the service's default request deadline;
            # keep the wall allowance clearly above its full preflight bound.
            max_wall_seconds=172_800.0,
        )
    )


def _bind_controller(
    monkeypatch: pytest.MonkeyPatch,
    controller: ProviderBudgetController,
) -> None:
    # Preflight reads through the service alias; ProviderRuntime resolves the
    # same run-scoped controller from provider_runtime during admission.
    monkeypatch.setattr(
        review_generation_service,
        "provider_budget_controller_from_environment",
        lambda: controller,
    )
    monkeypatch.setattr(
        provider_runtime,
        "provider_budget_controller_from_environment",
        lambda: controller,
    )


def _install_test_dependencies(monkeypatch: pytest.MonkeyPatch) -> RuntimeTestDependencies:
    dependencies = RuntimeTestDependencies()
    monkeypatch.setattr(test_dependencies, "_ACTIVE_TEST_DEPENDENCIES", dependencies)
    assert test_dependencies.current_runtime_test_dependencies() is dependencies
    return dependencies


def _capture_runtimes(
    service: ReviewGenerationService,
    monkeypatch: pytest.MonkeyPatch,
) -> list[Any]:
    runtimes: list[Any] = []
    create_runtime = service._new_runtime

    def capture(section_id: str, **kwargs: Any) -> Any:
        runtime = create_runtime(section_id, **kwargs)
        runtimes.append(runtime)
        return runtime

    monkeypatch.setattr(service, "_new_runtime", capture)
    return runtimes


def test_callback_atomic_admission_rechecks_budget_after_preflight_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    callback_calls: list[dict[str, Any]] = []
    service, packet, outline = _native_table_service(tmp_path, [])
    writer = service.writer

    def callback(**kwargs: Any) -> Mapping[str, Any]:
        callback_calls.append(dict(kwargs))
        assert writer is not None
        return writer(**kwargs)

    service.writer = callback
    controller = _strict_controller(calls=1)
    _bind_controller(monkeypatch, controller)
    build_projection = review_generation_service.build_full_stage_request_plan_v1
    raced: list[bool] = []
    projections: list[Mapping[str, Any]] = []

    def admit_competing_call_after_snapshot(**kwargs: Any) -> Mapping[str, Any]:
        # _preflight_provider_request_inventory has already taken its budget
        # snapshot when it asks for this projection. Consume the single shared
        # call slot before the service's atomic per-call admission.
        if not raced:
            raced.append(True)
            reservation = controller.admit(
                requested_output_tokens=1,
                requested_retry_attempts=0,
                context={"call_id": "competing-writer"},
            )
            controller.complete(
                reservation,
                {"status": "success", "attempts": 1, "output_tokens": 1},
            )
        projection = build_projection(**kwargs)
        projections.append(projection)
        return projection

    monkeypatch.setattr(
        review_generation_service,
        "build_full_stage_request_plan_v1",
        admit_competing_call_after_snapshot,
    )

    with pytest.raises(ProviderBudgetExceeded):
        service.run(outline_payload=outline, evidence_packets=[packet])

    assert raced == [True]
    assert len(projections) == 1
    assert projections[0]["budget_status"]["provider_calls"] == "within_limit"
    assert callback_calls == []
    snapshot = controller.snapshot()
    assert snapshot["calls_used"] == 1
    assert snapshot["calls_reserved"] == 0


def test_opaque_callback_requires_in_process_test_dependencies_even_offline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AUTO_GENERATE_OFFLINE_TESTS", "1")
    monkeypatch.setattr(test_dependencies, "_ACTIVE_TEST_DEPENDENCIES", None)
    calls: list[dict[str, Any]] = []
    service, packet, outline = _native_table_service(tmp_path, [])
    writer = service.writer

    def callback(**kwargs: Any) -> Mapping[str, Any]:
        calls.append(dict(kwargs))
        assert writer is not None
        return writer(**kwargs)

    service.writer = callback

    with pytest.raises(RuntimeError, match="(?i)(test|callback|injected|opaque)"):
        service.run(outline_payload=outline, evidence_packets=[packet])

    assert os.environ["AUTO_GENERATE_OFFLINE_TESTS"] == "1"
    assert calls == []


def test_opaque_callback_emits_only_test_receipt_and_receives_no_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_test_dependencies(monkeypatch)
    service, packet, outline = _native_table_service(tmp_path, [])
    writer = service.writer
    observed: list[dict[str, Any]] = []
    runtimes = _capture_runtimes(service, monkeypatch)

    def callback(**kwargs: Any) -> Mapping[str, Any]:
        observed.append(dict(kwargs))
        assert writer is not None
        # The source-inventory callback does not need runtime access. Strip the
        # legacy argument if exercising this test against the pre-fix code.
        kwargs.pop("provider_runtime", None)
        return writer(**kwargs)

    service.writer = callback
    service.run(outline_payload=outline, evidence_packets=[packet])

    receipts = service.receipt_ledger.list_receipts()
    assert len(observed) == 1
    assert "provider_runtime" not in observed[0]
    assert len(receipts) == 1
    assert receipts[0].status == "success"
    assert receipts[0].test_only is True
    with pytest.raises(ProviderRuntimeContractError, match="test-only"):
        receipts[0].validate_acceptance_authority(
            expected_job_id=service.job_id,
            expected_stage_name="stage3_review",
        )
    # The captured section runtime remains useful for checking service-owned
    # lifecycle state, while the callback itself never receives it.
    assert len(runtimes) == 1


def test_callback_error_completes_failed_test_receipt_and_releases_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_test_dependencies(monkeypatch)
    calls: list[dict[str, Any]] = []
    service, packet, outline = _native_table_service(tmp_path, [])
    runtimes = _capture_runtimes(service, monkeypatch)
    controller = _strict_controller(calls=1)
    _bind_controller(monkeypatch, controller)

    def failing_callback(**kwargs: Any) -> Mapping[str, Any]:
        calls.append(dict(kwargs))
        raise RuntimeError("opaque callback failed")

    service.writer = failing_callback

    with pytest.raises(RuntimeError, match="opaque callback failed"):
        service.run(outline_payload=outline, evidence_packets=[packet])

    receipts = service.receipt_ledger.list_receipts()
    assert len(calls) == 1
    assert "provider_runtime" not in calls[0]
    assert len(receipts) == 1
    assert receipts[0].status == "failed"
    assert receipts[0].test_only is True
    snapshot = controller.snapshot()
    assert snapshot["calls_used"] == 1
    assert snapshot["calls_reserved"] == 0
    assert snapshot["retry_attempts_reserved"] == 0
    assert len(runtimes) == 1


def test_uninstrumented_default_writer_fails_closed_without_test_dependencies(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ai_interface

    monkeypatch.setattr(test_dependencies, "_ACTIVE_TEST_DEPENDENCIES", None)
    service, packet, outline = _native_table_service(tmp_path, [])
    writer = service.writer
    service.writer = None
    transport_calls: list[dict[str, Any]] = []

    def uninstrumented_transport(
        prompt: str,
        api_config: Mapping[str, Any],
        system_prompt: str,
        **kwargs: Any,
    ) -> Mapping[str, Any]:
        transport_calls.append(dict(kwargs))
        assert writer is not None
        result = writer(prompt_text=prompt, writer_api_config=api_config)
        return {
            **result,
            "attempts": 1,
            "usage_status": "provider_not_supported",
            "finish_reason": "stop",
        }

    monkeypatch.setattr(ai_interface, "_call_ai_api_detailed", uninstrumented_transport)

    with pytest.raises((RuntimeError, ProviderRuntimeContractError), match="(?i)receipt"):
        service.run(outline_payload=outline, evidence_packets=[packet])

    assert len(transport_calls) == 1
    assert not service.receipt_ledger.list_receipts()


def test_explicit_test_context_allows_only_test_receipt_for_mocked_transport(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ai_interface

    _install_test_dependencies(monkeypatch)
    service, packet, outline = _native_table_service(tmp_path, [])
    writer = service.writer
    service.writer = None
    transport_calls: list[dict[str, Any]] = []

    def uninstrumented_transport(
        prompt: str,
        api_config: Mapping[str, Any],
        system_prompt: str,
        **kwargs: Any,
    ) -> Mapping[str, Any]:
        transport_calls.append(dict(kwargs))
        assert writer is not None
        result = writer(prompt_text=prompt, writer_api_config=api_config)
        return {
            **result,
            "attempts": 1,
            "usage_status": "provider_not_supported",
            "finish_reason": "stop",
        }

    monkeypatch.setattr(ai_interface, "_call_ai_api_detailed", uninstrumented_transport)
    service.run(outline_payload=outline, evidence_packets=[packet])

    receipts = service.receipt_ledger.list_receipts()
    assert len(transport_calls) == 1
    assert len(receipts) == 1
    assert receipts[0].status == "success"
    assert receipts[0].test_only is True


def test_test_only_cached_section_is_not_reused_without_test_dependencies(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_test_dependencies(monkeypatch)
    service, packet, outline = _native_table_service(tmp_path, [])
    writer = service.writer
    assert writer is not None
    service.run(outline_payload=outline, evidence_packets=[packet])
    original_receipts = service.receipt_ledger.list_receipts()
    assert len(original_receipts) == 1 and original_receipts[0].test_only is True

    monkeypatch.setattr(test_dependencies, "_ACTIVE_TEST_DEPENDENCIES", None)
    second_calls: list[dict[str, Any]] = []

    def forbidden_callback(**kwargs: Any) -> Mapping[str, Any]:
        second_calls.append(dict(kwargs))
        return writer(**kwargs)

    resumed = ReviewGenerationService(
        job_id=service.job_id,
        attempt_id=service.attempt_id,
        workspace=service.workspace,
        artifact_registry=service.registry,
        settings=service.settings,
        summaries=service.summaries,
        writer=forbidden_callback,
    )

    with pytest.raises(RuntimeError, match="(?i)(test|callback|injected|opaque)"):
        resumed.run(outline_payload=outline, evidence_packets=[packet])

    assert second_calls == []


def test_fixture_receipt_closure_persists_offline_authority_and_verified_dependencies(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_test_dependencies(monkeypatch)
    service, packet, outline = _native_table_service(tmp_path, [])
    service.run(outline_payload=outline, evidence_packets=[packet])

    closure_record = service.registry.get("review:provider_receipt_closure")
    ledger_record = service.registry.get("review_provider_receipts")
    assert closure_record is not None and closure_record.status == "ready"
    assert ledger_record is not None and ledger_record.status == "ready"
    ArtifactRegistry._verify_ready_artifact(closure_record)
    ArtifactRegistry._verify_ready_artifact(ledger_record)
    service.registry.verify_ready_dependencies(closure_record.depends_on)
    ledger_dependency = next(
        dependency
        for dependency in closure_record.depends_on
        if dependency.artifact_id == ledger_record.artifact_id
    )
    assert ledger_dependency.content_hash == ledger_record.content_hash
    assert ledger_dependency.content_hash == file_sha256(ledger_record.path)

    document = json.loads(Path(closure_record.path).read_text(encoding="utf-8"))
    payload = document["payload"]
    assert payload["test_only"] is True
    assert payload["authority_scope"] == "offline_fixture"
    receipts = service.receipt_ledger.list_receipts()
    assert len(receipts) == 1
    assert receipts[0].test_only is True


@pytest.mark.parametrize("closure_marker", ["omitted", "false"])
def test_current_stage_authority_rejects_test_only_ledger_rows_without_closure_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    closure_marker: str,
) -> None:
    _install_test_dependencies(monkeypatch)
    service, packet, outline = _native_table_service(tmp_path, [])
    service.run(outline_payload=outline, evidence_packets=[packet])

    closure_record = service.registry.get("review:provider_receipt_closure")
    ledger_record = service.registry.get("review_provider_receipts")
    assert closure_record is not None and ledger_record is not None
    ArtifactRegistry._verify_ready_artifact(ledger_record)
    service.registry.verify_ready_dependencies(closure_record.depends_on)
    receipts = service.receipt_ledger.list_receipts()
    assert receipts and all(receipt.test_only is True for receipt in receipts)

    # Build an independently hash-bound closure projection with the fixture
    # marker removed or set false. The actual registry-owned receipt ledger and
    # every dependency hash remain unchanged and are verified below.
    document = json.loads(Path(closure_record.path).read_text(encoding="utf-8"))
    altered = deepcopy(document)
    payload = altered["payload"]
    payload.pop("authority_scope", None)
    altered.pop("authority_scope", None)
    if closure_marker == "omitted":
        payload.pop("test_only", None)
        altered.pop("test_only", None)
    else:
        payload["test_only"] = False
        altered["test_only"] = False
    variant_path = tmp_path / f"review-provider-closure-{closure_marker}.json"
    serialized = json.dumps(
        altered,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    variant_path.write_bytes(serialized)
    variant_record = replace(
        closure_record,
        path=str(variant_path),
        content_hash=hashlib.sha256(serialized).hexdigest(),
    )
    ArtifactRegistry._verify_ready_artifact(variant_record)
    service.registry.verify_ready_dependencies(variant_record.depends_on)
    assert file_sha256(ledger_record.path) == ledger_record.content_hash

    entry, blocking = validation_closure._provider_closure_entry(
        "review",
        variant_record,
        SimpleNamespace(artifact_id="review-terminal", content_hash="a" * 64),
        {
            "status": "succeeded",
            "stage_name": "stage3_review",
            "model_call_count": 1,
            "output_artifact_refs": [
                {
                    "artifact_id": variant_record.artifact_id,
                    "content_hash": variant_record.content_hash,
                }
            ],
        },
        service.registry,
    )

    assert any("test_only" in issue.casefold() for issue in blocking), blocking
    assert entry["complete"] is False
    assert entry["status"] == "blocked"
