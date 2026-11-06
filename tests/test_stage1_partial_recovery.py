from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

import runtime.provider_runtime as provider_runtime
import runtime.stage1_publication_recovery as recovery
from runtime.provider_receipt_closure import ExpectedProviderCall
from runtime.provider_runtime import (
    ProviderCallReceiptV1,
    ProviderRuntimeLedger,
    hash_json,
)
from runtime.stage1_publication_recovery import (
    Stage1PublicationRecoveryBlocked,
    _partial_graph_hash,
    prepare_partial_stage1_recovery,
)
from services.artifact_registry import ArtifactDependencyRefV2
from tests.test_stage1_publication_recovery import (
    _MemoryRegistry,
    _TestWorkspace,
    _bind_zero_budget_controller,
)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _receipt(
    *,
    job_id: str,
    attempt_id: str,
    epoch_id: str,
    paper_key: str,
    input_hash: str,
    prompt_hash: str,
    config_hash: str,
    schema_hash: str,
    prompt_id: str,
    prompt_version: str,
    prompt_sha256: str,
    response_hash: str,
    raw_path: Path,
    raw_bytes: bytes,
    receipt_id: str = "receipt-partial-1",
) -> ProviderCallReceiptV1:
    return ProviderCallReceiptV1(
        artifact_type="provider_call_receipt",
        artifact_version="v2",
        receipt_id=receipt_id,
        sequence=1,
        job_id=job_id,
        attempt_id=attempt_id,
        stage_name="stage1_analyze",
        route="Primary_Reader_API",
        provider="generic",
        model="reader-local",
        endpoint="http://127.0.0.1:12656/v1",
        prompt_hash=prompt_hash,
        input_hash=input_hash,
        config_hash=config_hash,
        schema_hash=schema_hash,
        status="success",
        error_kind=None,
        http_status=200,
        provider_code=None,
        attempts=1,
        retry_after_seconds=None,
        input_tokens=5,
        output_tokens=10,
        total_tokens=15,
        response_hash=response_hash,
        started_at="2026-10-30T08:00:00Z",
        finished_at="2026-10-30T08:00:01Z",
        budget={},
        metadata={
            "transport_config": {
                "provider_family": "generic",
                "endpoint_type": "chat_completions",
                "api_base": "http://127.0.0.1:12656/v1",
                "model": "reader-local",
            },
            "raw_response_path": str(raw_path),
            "raw_response_sha256": hashlib.sha256(raw_bytes).hexdigest(),
            "response_bytes": len(raw_bytes),
            "response_protocol": "json",
            "response_complete": True,
            "requested_output_tokens_per_attempt": 10,
        },
        node_id=paper_key,
        call_id=f"stage1_synthesis:{paper_key}",
        closure_epoch_id=epoch_id,
        logical_attempt_identity=attempt_id,
        endpoint_type="chat_completions",
        finish_reason="stop",
        usage_status="reported",
        prompt_id=prompt_id,
        prompt_version=prompt_version,
        prompt_sha256=prompt_sha256,
    )


def _partial_case(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, Any]:
    job_id = "partial-recovery-unit-job"
    attempt_id = "attempt-stage1-original"
    epoch_id = _sha("original-closure-epoch")
    registry = _MemoryRegistry(tmp_path, job_id)
    workspace = _TestWorkspace(tmp_path, job_id)
    pdf_paths: dict[str, Path] = {}
    work_items = []
    for paper_key in ("paper-a", "paper-b"):
        pdf = tmp_path / "papers" / f"{paper_key}.pdf"
        pdf.parent.mkdir(parents=True, exist_ok=True)
        pdf.write_bytes(b"synthetic immutable source bytes " + paper_key.encode("ascii"))
        pdf_paths[paper_key] = pdf
        work_items.append(
            {
                "canonical_paper_key": paper_key,
                "source_paper_id": paper_key,
                "source_pdf": str(pdf.resolve()),
            }
        )
    source_bundle = registry.add_json(
        "source_bundle",
        "source_bundle",
        "v1",
        {
            "artifact_type": "source_bundle",
            "artifact_version": "v1",
            "job_id": job_id,
            "paper_work_items": work_items,
        },
    )
    runtime_spec = registry.add_json(
        "runtime_job_spec",
        "runtime_job_spec",
        "v1",
        {
            "artifact_type": "runtime_job_spec",
            "artifact_version": "v1",
            "job_id": job_id,
            "action": "analyze",
        },
    )
    scope = registry.add_json(
        "stage1_source_scope",
        "stage1_source_scope",
        "v1",
        {
            "schema_version": "stage1-source-scope/v1",
            "job_id": job_id,
            "source_bundle_hash": source_bundle.content_hash,
            "runtime_spec_hash": runtime_spec.content_hash,
            "papers": [
                {
                    "paper_key": paper_key,
                    "source_pdf": str(pdf.resolve()),
                    "source_pdf_sha256": hashlib.sha256(pdf.read_bytes()).hexdigest(),
                    "size_bytes": pdf.stat().st_size,
                    "page_count": 1,
                }
                for paper_key, pdf in pdf_paths.items()
            ],
        },
        dependencies=[
            ArtifactDependencyRefV2.from_record(source_bundle),
            ArtifactDependencyRefV2.from_record(runtime_spec),
        ],
    )
    schema_hash = _sha("stage1-summary-schema")
    config_hash = _sha("reader-local-config")
    calls: list[dict[str, Any]] = []
    for paper_key in ("paper-a", "paper-b"):
        prompt_hash = _sha(f"{paper_key}:prompt")
        input_hash = _sha(f"{paper_key}:input")
        calls.append(
            {
                "call_id": f"stage1_synthesis:{paper_key}",
                "job_id": job_id,
                "attempt_id": attempt_id,
                "stage_name": "stage1_analyze",
                "node_id": paper_key,
                "logical_attempt_identity": attempt_id,
                "closure_epoch_id": epoch_id,
                "expected_call_graph_hash": "",
                "prompt_hash": prompt_hash,
                "prompt_id": "stage1.analysis.user.v3",
                "prompt_version": "3",
                "prompt_sha256": _sha("stage1-user-prompt"),
                "input_hash": input_hash,
                "config_hash": config_hash,
                "schema_hash": schema_hash,
                "artifact_path": str(workspace.artifact_path(f"paper_artifacts/{paper_key}.json")),
                "max_attempts": 1,
                "usage_required": False,
                "request_variants": [
                    {
                        "prompt_hash": prompt_hash,
                        "input_hash": input_hash,
                        "config_hash": config_hash,
                    }
                ],
            }
        )
    graph_hash = _partial_graph_hash(
        job_id=job_id,
        attempt_id=attempt_id,
        source_bundle_hash=source_bundle.content_hash,
        runtime_spec_hash=runtime_spec.content_hash,
        calls=calls,
    )
    for call in calls:
        call["expected_call_graph_hash"] = graph_hash
    graph = registry.add_json(
        "stage1:provider_expected_call_graph",
        "provider_expected_call_graph",
        "v1",
        {
            "artifact_type": "provider_expected_call_graph",
            "artifact_version": "v1",
            "job_id": job_id,
            "stage_name": "stage1_analyze",
            "attempt_id": attempt_id,
            "logical_attempt_identity": attempt_id,
            "closure_epoch_id": epoch_id,
            "expected_call_graph_hash": graph_hash,
            "source_bundle_hash": source_bundle.content_hash,
            "runtime_spec_hash": runtime_spec.content_hash,
            "expected_calls": calls,
        },
        dependencies=[ArtifactDependencyRefV2.from_record(scope)],
        metadata={"expected_call_graph_hash": graph_hash},
    )
    staged_path = Path(
        workspace.artifact_path(
            f".publication-staging/provider-receipts/stage1/{attempt_id}.jsonl"
        )
    )
    raw_path = Path(
        workspace.artifact_path(
            ".publication-staging/provider-receipts/stage1/raw_responses/response-a.bin"
        )
    )
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    content = {"summary": "the retained provider response is locally reproducible"}
    raw_body = json.dumps(
        {
            "choices": [
                {
                    "message": {"role": "assistant", "content": json.dumps(content)},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 5, "completion_tokens": 10, "total_tokens": 15},
        },
        separators=(",", ":"),
    ).encode("utf-8")
    raw_path.write_bytes(raw_body)
    successful_call = calls[0]
    successful_receipt = _receipt(
        job_id=job_id,
        attempt_id=attempt_id,
        epoch_id=epoch_id,
        paper_key="paper-a",
        input_hash=successful_call["input_hash"],
        prompt_hash=successful_call["prompt_hash"],
        config_hash=successful_call["config_hash"],
        schema_hash=schema_hash,
        prompt_id=successful_call["prompt_id"],
        prompt_version=successful_call["prompt_version"],
        prompt_sha256=successful_call["prompt_sha256"],
        response_hash=hash_json(content),
        raw_path=raw_path,
        raw_bytes=raw_body,
    )
    staged_path.parent.mkdir(parents=True, exist_ok=True)
    staged_path.write_text(
        json.dumps(successful_receipt.to_dict(), ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    controller = _bind_zero_budget_controller(workspace, monkeypatch)
    monkeypatch.setattr(
        recovery,
        "current_runtime_control_context",
        provider_runtime.current_runtime_control_context,
    )
    reservation = controller.admit(
        requested_output_tokens=10,
        requested_retry_attempts=0,
        context={"job_id": job_id, "stage_name": "stage1_analyze", "call_id": successful_receipt.call_id},
    )
    controller.mark_transport_started(reservation)
    controller.complete(
        reservation,
        {"status": "success", "attempts": 1, "output_tokens": 10},
    )
    return {
        "registry": registry,
        "workspace": workspace,
        "source_hash": source_bundle.content_hash,
        "spec_hash": runtime_spec.content_hash,
        "controller": controller,
        "receipt": successful_receipt,
        "raw_path": raw_path,
        "staged_path": staged_path,
        "graph": graph,
        "calls": calls,
        "pdf_paths": pdf_paths,
        "content": content,
    }


def _prepare(case: dict[str, Any], *, source_hash: str | None = None):
    return prepare_partial_stage1_recovery(
        case["workspace"],
        case["registry"],  # type: ignore[arg-type]
        expected_source_bundle_hash=source_hash or case["source_hash"],
        expected_runtime_spec_hash=case["spec_hash"],
        provider_budget_controller=case["controller"],
    )


def _rewrite_receipt(case: dict[str, Any], receipt: ProviderCallReceiptV1) -> None:
    case["staged_path"].write_text(
        json.dumps(receipt.to_dict(), ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def test_partial_stage1_recovery_reparses_exact_success_and_preserves_unstarted_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _partial_case(tmp_path, monkeypatch)
    staged_before = case["staged_path"].read_bytes()
    state_path = case["controller"].state_path
    assert state_path is not None
    budget_before = state_path.read_bytes()

    context = _prepare(case)

    assert context is not None
    assert context.logical_attempt_id == "attempt-stage1-original"
    assert context.expected_calls[0].call_id == "stage1_synthesis:paper-a"
    assert set(context.replay_calls) == {"stage1_synthesis:paper-a"}
    assert context.replay_calls["stage1_synthesis:paper-a"].receipt.receipt_id == "receipt-partial-1"
    assert context.replay_calls["stage1_synthesis:paper-a"].response["content"] == case["content"]
    assert context.pending_call_ids == frozenset({"stage1_synthesis:paper-b"})
    assert case["staged_path"].read_bytes() == staged_before
    assert state_path.read_bytes() == budget_before


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_raw",
        "tampered_raw",
        "request_identity",
        "prompt_identity",
        "failed_receipt",
        "response_hash",
        "source_pdf",
        "wrong_source_hash",
        "budget_mismatch",
        "started_reservation",
        "body_incomplete",
    ],
)
def test_partial_stage1_recovery_fails_closed_on_unverifiable_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    case = _partial_case(tmp_path, monkeypatch)
    receipt: ProviderCallReceiptV1 = case["receipt"]
    if mutation == "missing_raw":
        metadata = dict(receipt.metadata)
        metadata.pop("raw_response_path", None)
        metadata.pop("raw_response_spool_path", None)
        _rewrite_receipt(case, replace(receipt, metadata=metadata))
    elif mutation == "tampered_raw":
        case["raw_path"].write_bytes(case["raw_path"].read_bytes() + b"changed")
    elif mutation == "request_identity":
        _rewrite_receipt(case, replace(receipt, input_hash=_sha("different request")))
    elif mutation == "prompt_identity":
        _rewrite_receipt(case, replace(receipt, prompt_version="different-prompt-version"))
    elif mutation == "failed_receipt":
        _rewrite_receipt(
            case,
            replace(receipt, status="failed", error_kind="invalid_response"),
        )
    elif mutation == "response_hash":
        _rewrite_receipt(case, replace(receipt, response_hash=_sha("different response")))
    elif mutation == "source_pdf":
        case["pdf_paths"]["paper-a"].write_bytes(b"changed source bytes")
    elif mutation == "wrong_source_hash":
        with pytest.raises(Stage1PublicationRecoveryBlocked, match="current source bundle differs"):
            _prepare(case, source_hash=_sha("wrong source bundle"))
        return
    elif mutation == "budget_mismatch":
        extra = case["controller"].admit(
            requested_output_tokens=1,
            requested_retry_attempts=0,
            context={"job_id": case["workspace"].job_id, "stage_name": "stage1_analyze"},
        )
        case["controller"].mark_transport_started(extra)
        case["controller"].complete(extra, {"status": "success", "attempts": 1, "output_tokens": 1})
    elif mutation == "started_reservation":
        pending = case["controller"].admit(
            requested_output_tokens=1,
            requested_retry_attempts=0,
            context={"job_id": case["workspace"].job_id, "stage_name": "stage1_analyze"},
        )
        case["controller"].mark_transport_started(pending)
    elif mutation == "body_incomplete":
        metadata = dict(receipt.metadata)
        metadata["response_body_complete"] = False
        _rewrite_receipt(case, replace(receipt, metadata=metadata))
    else:  # pragma: no cover - parametrize exhaustiveness
        raise AssertionError(mutation)

    with pytest.raises(Stage1PublicationRecoveryBlocked):
        _prepare(case)


def test_partial_stage1_recovery_rejects_duplicate_receipt_ids_before_pending_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _partial_case(tmp_path, monkeypatch)
    first: ProviderCallReceiptV1 = case["receipt"]
    second_call = case["calls"][1]
    second = replace(
        first,
        call_id=second_call["call_id"],
        node_id=second_call["node_id"],
        input_hash=second_call["input_hash"],
        prompt_hash=second_call["prompt_hash"],
        prompt_id=second_call["prompt_id"],
        prompt_version=second_call["prompt_version"],
        prompt_sha256=second_call["prompt_sha256"],
        receipt_id=first.receipt_id,
    )
    case["staged_path"].write_text(
        "".join(
            json.dumps(receipt.to_dict(), ensure_ascii=False, sort_keys=True) + "\n"
            for receipt in (first, second)
        ),
        encoding="utf-8",
    )
    extra = case["controller"].admit(
        requested_output_tokens=10,
        requested_retry_attempts=0,
        context={"job_id": case["workspace"].job_id, "stage_name": "stage1_analyze"},
    )
    case["controller"].mark_transport_started(extra)
    case["controller"].complete(extra, {"status": "success", "attempts": 1, "output_tokens": 10})

    with pytest.raises(Stage1PublicationRecoveryBlocked, match="duplicate call identity"):
        _prepare(case)
