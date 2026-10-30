"""Fail-closed recovery of an Analyze publication missing only its terminal record.

This module never invokes Stage 1 generation.  It can propose a terminal
reconstruction only when the exact current Stage 1 outputs, expected-call graph,
receipt closure, and source bindings already exist and validate in the Registry.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Mapping

from runtime.reconcile import ProvenStageRecovery, RuntimeReconciler
from runtime.provider_receipt_closure import ExpectedProviderCall
from runtime.provider_runtime import (
    ProviderBudgetController,
    ProviderCallReceiptV1,
    ProviderRuntimeLedger,
    acceptance_execution_context_from_environment,
    current_acceptance_execution_context,
    current_runtime_control_context,
    hash_json,
)
from services.artifact_registry import (
    ArtifactDependencyRefV2,
    ArtifactRecord,
    ArtifactRegistry,
    RegistryError,
    file_sha256,
)
from services.job_workspace import JobWorkspace
from validation.closure import _provider_closure_entry


class Stage1PublicationRecoveryBlocked(RuntimeError):
    """A prior Stage 1 publication exists but cannot be proven safe to adopt."""


def _blocked(reason: str) -> Stage1PublicationRecoveryBlocked:
    return Stage1PublicationRecoveryBlocked(
        "STAGE1_PUBLICATION_RECOVERY_BLOCKED: " + reason
    )


@dataclass(frozen=True)
class Stage1PartialReplayCall:
    receipt: ProviderCallReceiptV1
    response: Mapping[str, Any]
    requested_output_tokens: int


@dataclass(frozen=True)
class Stage1PartialRecoveryContext:
    job_id: str
    logical_attempt_id: str
    closure_epoch_id: str
    expected_call_graph_hash: str
    source_bundle_hash: str
    runtime_spec_hash: str
    expected_call_graph_path: str
    expected_calls: tuple[ExpectedProviderCall, ...]
    replay_calls: Mapping[str, Stage1PartialReplayCall]
    pending_call_ids: frozenset[str]
    budget_state_path: str
    budget_deadline_epoch: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "expected_calls", tuple(self.expected_calls))
        object.__setattr__(self, "replay_calls", MappingProxyType(dict(self.replay_calls)))
        object.__setattr__(self, "pending_call_ids", frozenset(self.pending_call_ids))


def _partial_graph_hash(
    *,
    job_id: str,
    attempt_id: str,
    source_bundle_hash: str,
    runtime_spec_hash: str,
    calls: list[Mapping[str, Any]],
) -> str:
    return hash_json(
        {
            "identity_version": "stage1_expected_call_graph/v2",
            "job_id": job_id,
            "stage_name": "stage1_analyze",
            "attempt_id": attempt_id,
            "source_bundle_hash": source_bundle_hash,
            "runtime_spec_hash": runtime_spec_hash,
            "call_shapes": [
                {
                    "call_id": call.get("call_id"),
                    "node_id": call.get("node_id"),
                    "prompt_id": call.get("prompt_id"),
                    "schema_hash": call.get("schema_hash"),
                    "artifact_path": call.get("artifact_path"),
                    "max_attempts": call.get("max_attempts"),
                    "request_variants": list(call.get("request_variants") or ()),
                }
                for call in calls
            ],
        }
    )


def _partial_budget_proof(
    workspace: JobWorkspace,
    provider_budget_controller: Any | None,
) -> tuple[dict[str, Any], str, float]:
    if not isinstance(provider_budget_controller, ProviderBudgetController):
        raise _blocked("partial Stage 1 recovery requires its job-bound durable provider budget")
    try:
        control = current_runtime_control_context()
        if control is None:
            raise _blocked("partial Stage 1 recovery has no active runtime-control identity")
        identity = control.identity
        acceptance = (
            current_acceptance_execution_context()
            or acceptance_execution_context_from_environment()
        )
        if identity.job_id != workspace.job_id or Path(identity.workspace_path).resolve() != Path(workspace.root_dir).resolve():
            raise _blocked("partial Stage 1 recovery budget belongs to another workspace")
        if acceptance is None:
            expected_run_id = f"run:{workspace.job_id}"
            expected_state_path = Path(
                workspace.log_path("acceptance_budget_state_v1.json")
            ).expanduser().resolve()
        else:
            if not acceptance.owner_authorized:
                raise _blocked("partial Stage 1 recovery acceptance budget is not owner-authorized")
            expected_run_id = acceptance.acceptance_run_id
            expected_state_path = Path(
                acceptance.provider_budget_state_path
            ).expanduser().resolve()
            if provider_budget_controller.budget != acceptance.provider_budget:
                raise _blocked("partial Stage 1 recovery budget differs from its accepted contract")
        if (
            not expected_run_id
            or identity.acceptance_run_id != expected_run_id
            or provider_budget_controller.acceptance_run_id != expected_run_id
            or not provider_budget_controller.state_started
            or provider_budget_controller.state_path is None
            or provider_budget_controller.state_path.expanduser().resolve() != expected_state_path
        ):
            raise _blocked("partial Stage 1 recovery budget identity is not job-bound")
        provider_budget_controller.assert_state_integrity(
            acceptance_run_id=expected_run_id,
            state_started=True,
        )
        snapshot = provider_budget_controller.snapshot()
        state_path = provider_budget_controller.state_path
        assert state_path is not None
        try:
            state_payload = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise _blocked("partial Stage 1 recovery budget state is unreadable") from exc
        if not isinstance(state_payload, Mapping):
            raise _blocked("partial Stage 1 recovery budget state is not an object")
        for name in (
            "calls_used",
            "output_tokens_used",
            "retry_attempts_used",
            "calls_reserved",
            "output_tokens_reserved",
            "retry_attempts_reserved",
        ):
            value = snapshot.get(name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise _blocked(f"partial Stage 1 recovery budget counter is invalid: {name}")
            if state_payload.get(name) != value:
                raise _blocked(f"partial Stage 1 recovery budget counter changed: {name}")
        reservations = state_payload.get("reservations")
        if not isinstance(reservations, list) or reservations:
            raise _blocked("partial Stage 1 recovery has an outstanding provider reservation")
        if (
            snapshot.get("calls_reserved") != 0
            or snapshot.get("output_tokens_reserved") != 0
            or snapshot.get("retry_attempts_reserved") != 0
            or getattr(provider_budget_controller, "_reservations", None)
            or getattr(provider_budget_controller, "_ambiguous_reservation_ids", None)
        ):
            raise _blocked("partial Stage 1 recovery has started or unknown provider transport")
        deadline = snapshot.get("absolute_deadline_epoch")
        if isinstance(deadline, bool) or not isinstance(deadline, (int, float)) or not math.isfinite(float(deadline)):
            raise _blocked("partial Stage 1 recovery has no finite durable provider deadline")
        deadline = float(deadline)
        if deadline <= time.time():
            raise _blocked("partial Stage 1 recovery provider deadline has expired")
        if acceptance is not None and not math.isclose(
            deadline,
            float(acceptance.absolute_deadline_epoch),
            rel_tol=0.0,
            abs_tol=1e-6,
        ):
            raise _blocked("partial Stage 1 recovery provider deadline changed")
        return dict(snapshot), str(state_path), deadline
    except Stage1PublicationRecoveryBlocked:
        raise
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise _blocked(f"partial Stage 1 recovery budget could not be verified: {exc}") from exc


def prepare_partial_stage1_recovery(
    workspace: JobWorkspace,
    registry: ArtifactRegistry,
    *,
    expected_source_bundle_hash: str,
    expected_runtime_spec_hash: str,
    provider_budget_controller: Any | None,
    external_registry_resolver: Callable[[str], ArtifactRegistry | None] | None = None,
) -> Stage1PartialRecoveryContext | None:
    """Reparse exact successful Stage 1 calls when the remaining graph is provably unstarted.

    This deliberately supports only a frozen synthesis-only graph, one complete
    HTTP 200 receipt per completed call, and a job-bound aggregate budget whose
    entire usage is accounted for by that exact receipt ledger. Any started,
    failed, retried, unknown, or unbound attempt remains on the existing
    fail-closed recovery path.
    """

    if workspace.job_id != registry.job_id:
        raise _blocked("workspace and Registry jobs differ")
    graph_pointer = registry.get("stage1:provider_expected_call_graph")
    if graph_pointer is None:
        return None
    # A complete publication stays on the existing terminal-reconstruction
    # path. Partial response replay is only for the missing-closure/output case.
    if registry.get("stage1:provider_receipt_closure") is not None or registry.get("stage1_provider_receipts") is not None:
        return None
    summary_pointer = registry.get("summary_file")
    if summary_pointer is not None and summary_pointer.metadata.get("pointer_role") == "current":
        return None
    if any(
        record.artifact_type == "runtime_stage_terminal"
        and str(record.metadata.get("stage_name") or "") in {"analyze", "stage1_analyze"}
        for record in registry.list_records()
    ):
        return None
    if not expected_source_bundle_hash or not expected_runtime_spec_hash:
        raise _blocked("current source-bundle or runtime-spec hash was not supplied")

    try:
        source_bundle = _record(
            registry,
            "source_bundle",
            artifact_type="source_bundle",
            artifact_version="v1",
            external_registry_resolver=external_registry_resolver,
        )
        runtime_spec = _record(
            registry,
            "runtime_job_spec",
            artifact_type="runtime_job_spec",
            artifact_version="v1",
            external_registry_resolver=external_registry_resolver,
        )
        graph = _record(
            registry,
            "stage1:provider_expected_call_graph",
            artifact_type="provider_expected_call_graph",
            artifact_version="v1",
            external_registry_resolver=external_registry_resolver,
        )
    except Stage1PublicationRecoveryBlocked:
        raise
    if source_bundle.content_hash != expected_source_bundle_hash:
        raise _blocked("current source bundle differs from the published Stage 1 source")
    if runtime_spec.content_hash != expected_runtime_spec_hash:
        raise _blocked("current RuntimeJobSpec differs from the published Stage 1 configuration")

    source_document = _payload(_read_json(source_bundle.path, label="Stage 1 source bundle"))
    runtime_document = _payload(_read_json(runtime_spec.path, label="Stage 1 RuntimeJobSpec"))
    graph_document = _payload(_read_json(graph.path, label="Stage 1 expected-call graph"))
    graph_hash = str(graph_document.get("expected_call_graph_hash") or "")
    attempt_id = str(graph_document.get("attempt_id") or "")
    epoch_id = str(graph_document.get("closure_epoch_id") or "")
    expected_calls_raw = graph_document.get("expected_calls")
    if (
        str(graph_document.get("job_id") or "") != registry.job_id
        or str(graph_document.get("stage_name") or "") != "stage1_analyze"
        or str(graph_document.get("source_bundle_hash") or "") != expected_source_bundle_hash
        or str(graph_document.get("runtime_spec_hash") or "") != expected_runtime_spec_hash
        or graph.metadata.get("expected_call_graph_hash") != graph_hash
        or not attempt_id
        or not epoch_id
        or not isinstance(expected_calls_raw, list)
        or any(not isinstance(call, Mapping) for call in expected_calls_raw)
    ):
        raise _blocked("Stage 1 expected-call graph does not bind the current job/source/spec")
    if not expected_calls_raw or any(
        not str(call.get("call_id") or "").startswith("stage1_synthesis:")
        for call in expected_calls_raw
    ):
        # Empty/all-reuse and visual/MinerU call graphs remain on their existing
        # zero-transport or complete-publication paths.
        return None
    if _partial_graph_hash(
        job_id=registry.job_id,
        attempt_id=attempt_id,
        source_bundle_hash=expected_source_bundle_hash,
        runtime_spec_hash=expected_runtime_spec_hash,
        calls=expected_calls_raw,
    ) != graph_hash:
        raise _blocked("Stage 1 expected-call graph bytes do not match its declared identity")

    scope_pointer = registry.get("stage1_source_scope")
    if scope_pointer is None:
        raise _blocked("Stage 1 source scope is missing")
    source_scope = _record(
        registry,
        scope_pointer.artifact_id,
        artifact_type="stage1_source_scope",
        artifact_version="v1",
        external_registry_resolver=external_registry_resolver,
    )
    _requires_direct_ref(graph, source_scope, label="Stage 1 expected-call graph")
    _requires_direct_ref(source_scope, source_bundle, label="Stage 1 source scope")
    _requires_direct_ref(source_scope, runtime_spec, label="Stage 1 source scope")
    scope_document = _payload(_read_json(source_scope.path, label="Stage 1 source scope"))
    if (
        str(scope_document.get("job_id") or "") != registry.job_id
        or str(scope_document.get("source_bundle_hash") or "") != expected_source_bundle_hash
        or str(scope_document.get("runtime_spec_hash") or "") != expected_runtime_spec_hash
    ):
        raise _blocked("Stage 1 source scope differs from the current source/spec")

    work_items = source_document.get("paper_work_items")
    if not isinstance(work_items, list) or not work_items:
        if _pretransport_fallback_allowed(
            workspace,
            registry,
            provider_budget_controller,
            external_registry_resolver=external_registry_resolver,
        ):
            return None
        raise _blocked("Stage 1 source bundle has no paper work items")
    paper_keys = [
        str(item.get("canonical_paper_key") or "")
        for item in work_items
        if isinstance(item, Mapping)
    ]
    if (
        len(paper_keys) != len(work_items)
        or any(not key for key in paper_keys)
        or len(set(paper_keys)) != len(paper_keys)
    ):
        raise _blocked("Stage 1 source bundle paper identities are invalid")
    scoped_papers_raw = scope_document.get("papers")
    if not isinstance(scoped_papers_raw, list):
        raise _blocked("Stage 1 source scope has no immutable PDF identity list")
    scoped_papers = {
        str(item.get("paper_key") or ""): item
        for item in scoped_papers_raw
        if isinstance(item, Mapping) and str(item.get("paper_key") or "")
    }
    if len(scoped_papers) != len(scoped_papers_raw) or set(scoped_papers) != set(paper_keys):
        raise _blocked("Stage 1 source scope does not cover every source paper exactly once")
    for work_item in work_items:
        paper_key = str(work_item.get("canonical_paper_key") or "")
        scoped = scoped_papers[paper_key]
        source_pdf = str(work_item.get("source_pdf") or "").strip()
        try:
            resolved_pdf = Path(source_pdf).expanduser().resolve(strict=True)
            if (
                str(resolved_pdf) != str(Path(str(scoped.get("source_pdf") or "")).expanduser().resolve(strict=True))
                or file_sha256(resolved_pdf) != str(scoped.get("source_pdf_sha256") or "")
                or resolved_pdf.stat().st_size != int(scoped.get("size_bytes") or -1)
            ):
                raise _blocked(f"Stage 1 source PDF differs from its frozen scope: {paper_key}")
        except Stage1PublicationRecoveryBlocked:
            raise
        except (OSError, TypeError, ValueError) as exc:
            raise _blocked(f"Stage 1 source PDF cannot be verified: {paper_key}") from exc

    expected_call_ids = {f"stage1_synthesis:{key}" for key in paper_keys}
    call_by_id: dict[str, ExpectedProviderCall] = {}
    for raw_call in expected_calls_raw:
        try:
            call = ExpectedProviderCall.from_mapping(raw_call)
        except (TypeError, ValueError) as exc:
            raise _blocked("Stage 1 expected-call graph contains an invalid call") from exc
        if (
            not call.call_id.startswith("stage1_synthesis:")
            or call.job_id != registry.job_id
            or call.attempt_id != attempt_id
            or call.logical_attempt_identity != attempt_id
            or call.stage_name != "stage1_analyze"
            or call.closure_epoch_id != epoch_id
            or call.expected_call_graph_hash != graph_hash
            or not call.node_id
            or not call.schema_hash
            or not call.request_variants
            or call.call_id in call_by_id
        ):
            raise _blocked("partial Stage 1 recovery requires one exact synthesis graph per source")
        for variant in call.request_variants:
            if not isinstance(variant, Mapping) or any(
                not re.fullmatch(r"[0-9a-f]{64}", str(variant.get(name) or ""))
                for name in ("prompt_hash", "input_hash", "config_hash")
            ):
                raise _blocked("Stage 1 expected-call graph has an incomplete request variant")
        call_by_id[call.call_id] = call
    if set(call_by_id) != expected_call_ids or len(expected_calls_raw) != len(expected_call_ids):
        if _pretransport_fallback_allowed(
            workspace,
            registry,
            provider_budget_controller,
            external_registry_resolver=external_registry_resolver,
        ):
            return None
        raise _blocked("partial Stage 1 recovery graph does not cover exactly the source synthesis calls")
    if any(call_by_id[f"stage1_synthesis:{key}"].node_id != key for key in paper_keys):
        raise _blocked("Stage 1 synthesis call ownership differs from the source paper identities")

    safe_attempt = re.sub(r"[^A-Za-z0-9_.-]+", "_", attempt_id or "stage1")
    staged_path = Path(
        workspace.artifact_path(
            f".publication-staging/provider-receipts/stage1/{safe_attempt}.jsonl"
        )
    ).expanduser().resolve()
    stage1_staging_root = Path(
        workspace.artifact_path(".publication-staging/provider-receipts/stage1")
    ).expanduser().resolve()
    if not staged_path.is_relative_to(stage1_staging_root):
        raise _blocked("Stage 1 staged receipt ledger escapes its workspace")
    if not staged_path.is_file():
        if _pretransport_fallback_allowed(
            workspace,
            registry,
            provider_budget_controller,
            external_registry_resolver=external_registry_resolver,
        ):
            return None
        raise _blocked("Stage 1 staged receipt ledger is missing while prior transport cannot be ruled out")
    try:
        ledger = ProviderRuntimeLedger(staged_path)
        receipts = ledger.list_receipts()
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise _blocked(f"Stage 1 staged receipt ledger is invalid: {exc}") from exc
    if not receipts:
        if _pretransport_fallback_allowed(
            workspace,
            registry,
            provider_budget_controller,
            external_registry_resolver=external_registry_resolver,
        ):
            return None
        raise _blocked("Stage 1 has no receipts but prior transport cannot be ruled out")

    budget_snapshot, budget_state_path, budget_deadline = _partial_budget_proof(
        workspace, provider_budget_controller,
    )
    replay_calls: dict[str, Stage1PartialReplayCall] = {}
    replay_receipt_ids: set[str] = set()
    from ai_interface import reparse_provider_response_spool

    for receipt in receipts:
        expected = call_by_id.get(receipt.call_id)
        if (
            expected is None
            or receipt.call_id in replay_calls
            or not receipt.receipt_id
            or receipt.receipt_id in replay_receipt_ids
        ):
            raise _blocked("Stage 1 staged receipt has an unexpected or duplicate call identity")
        if (
            receipt.job_id != registry.job_id
            or receipt.attempt_id != attempt_id
            or receipt.logical_attempt_identity != attempt_id
            or receipt.stage_name != "stage1_analyze"
            or receipt.node_id != expected.node_id
            or receipt.prompt_id != expected.prompt_id
            or receipt.prompt_version != expected.prompt_version
            or receipt.prompt_sha256 != expected.prompt_sha256
            or receipt.closure_epoch_id != epoch_id
            or receipt.schema_hash != expected.schema_hash
            or receipt.status != "success"
            or receipt.error_kind is not None
            or receipt.http_status != 200
            or receipt.attempts != 1
            or receipt.finish_reason.strip().casefold() not in {"stop", "end_turn", "completed"}
            or receipt.incomplete_reason
            or receipt.timeout_kind
            or receipt.fallback_or_payload_mutations
            or receipt.test_only
            or receipt.route not in {"Primary_Reader_API", "Backup_Reader_API"}
            or receipt.response_hash is None
            or receipt.input_tokens is None
            or receipt.output_tokens is None
            or receipt.total_tokens is None
            or receipt.usage_status != "reported"
        ):
            raise _blocked(f"Stage 1 receipt is not a complete single-call success: {receipt.call_id}")
        matching_variants = [
            variant
            for variant in expected.request_variants
            if variant.get("prompt_hash") == receipt.prompt_hash
            and variant.get("input_hash") == receipt.input_hash
            and variant.get("config_hash") == receipt.config_hash
        ]
        if not matching_variants:
            raise _blocked(f"Stage 1 receipt request identity differs from the expected graph: {receipt.call_id}")
        metadata = receipt.metadata if isinstance(receipt.metadata, Mapping) else {}
        transport = metadata.get("transport_config")
        if not isinstance(transport, Mapping) or any(
            str(transport.get(field) or "") != str(expected_value or "")
            for field, expected_value in (
                ("provider_family", receipt.provider),
                ("model", receipt.model),
                ("endpoint_type", receipt.endpoint_type),
                ("api_base", receipt.endpoint),
            )
        ):
            raise _blocked(f"Stage 1 receipt route identity is incomplete: {receipt.call_id}")
        raw_path_values = [
            str(metadata.get(key) or "").strip()
            for key in ("raw_response_path", "raw_response_spool_path")
            if str(metadata.get(key) or "").strip()
        ]
        if not raw_path_values or len(set(raw_path_values)) != 1:
            raise _blocked(f"Stage 1 successful receipt has no unique retained raw response: {receipt.call_id}")
        raw_path = Path(raw_path_values[0]).expanduser().resolve()
        if not raw_path.is_relative_to(stage1_staging_root) or not raw_path.is_file():
            raise _blocked(f"Stage 1 retained raw response is missing or outside its staging root: {receipt.call_id}")
        raw_sha256 = str(metadata.get("raw_response_sha256") or "")
        response_bytes = metadata.get("response_bytes")
        response_protocol = str(metadata.get("response_protocol") or "").strip().casefold()
        if (
            not re.fullmatch(r"[0-9a-f]{64}", raw_sha256)
            or isinstance(response_bytes, bool)
            or not isinstance(response_bytes, int)
            or response_bytes < 0
            or response_protocol not in {"json", "sse"}
            or metadata.get("response_complete") is not True
            or (
                "response_body_complete" in metadata
                and metadata.get("response_body_complete") is not True
            )
        ):
            raise _blocked(f"Stage 1 retained raw response binding is incomplete: {receipt.call_id}")
        requested_output_tokens = metadata.get("requested_output_tokens_per_attempt")
        if (
            isinstance(requested_output_tokens, bool)
            or not isinstance(requested_output_tokens, int)
            or requested_output_tokens <= 0
        ):
            raise _blocked(f"Stage 1 receipt lacks its exact requested output-token count: {receipt.call_id}")
        try:
            # Content-Type was not retained in the receipt. An empty value makes
            # the parser infer JSON/SSE from the raw bytes rather than forcing a
            # protocol based on the receipt's claim.
            response = reparse_provider_response_spool(
                raw_path,
                raw_sha256,
                response_bytes,
                "",
                response_protocol,
                receipt.endpoint_type,
                receipt.model,
                "json",
                receipt.http_status,
                True,
            )
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise _blocked(f"Stage 1 raw response could not be independently reparsed: {receipt.call_id}") from exc
        if (
            response.get("status") != "success"
            or response.get("response_complete") is not True
            or response.get("response_body_complete") is not True
            or response.get("finish_reason") != receipt.finish_reason
            or hash_json(response.get("content")) != receipt.response_hash
            or response.get("input_tokens") != receipt.input_tokens
            or response.get("output_tokens") != receipt.output_tokens
            or response.get("total_tokens") != receipt.total_tokens
        ):
            raise _blocked(f"Stage 1 raw response differs from its successful receipt: {receipt.call_id}")
        replay_calls[receipt.call_id] = Stage1PartialReplayCall(
            receipt=receipt,
            response=MappingProxyType(dict(response)),
            requested_output_tokens=requested_output_tokens,
        )
        replay_receipt_ids.add(receipt.receipt_id)

    usage = ledger.usage_summary()
    if (
        usage.get("receipt_count") != len(receipts)
        or usage.get("physical_calls_used") != budget_snapshot.get("calls_used")
        or usage.get("output_tokens_reported") != budget_snapshot.get("output_tokens_used")
        or usage.get("unreported_output_receipts") != 0
        or usage.get("retry_attempts_used") != budget_snapshot.get("retry_attempts_used")
        or budget_snapshot.get("retry_attempts_used") != 0
    ):
        raise _blocked("Stage 1 receipt usage does not reconcile exactly to the durable provider budget")

    return Stage1PartialRecoveryContext(
        job_id=registry.job_id,
        logical_attempt_id=attempt_id,
        closure_epoch_id=epoch_id,
        expected_call_graph_hash=graph_hash,
        source_bundle_hash=expected_source_bundle_hash,
        runtime_spec_hash=expected_runtime_spec_hash,
        expected_call_graph_path=graph.path,
        expected_calls=tuple(
            call_by_id[str(raw_call.get("call_id") or "")]
            for raw_call in expected_calls_raw
        ),
        replay_calls=replay_calls,
        pending_call_ids=frozenset(set(call_by_id) - set(replay_calls)),
        budget_state_path=budget_state_path,
        budget_deadline_epoch=budget_deadline,
    )


def _read_json(path: str | Path, *, label: str) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise _blocked(f"{label} is unreadable: {exc}") from exc


def _payload(value: Any) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    nested = value.get("payload")
    return nested if isinstance(nested, Mapping) else value


def _record(
    registry: ArtifactRegistry,
    artifact_id: str,
    *,
    artifact_type: str,
    artifact_version: str,
    external_registry_resolver: Callable[[str], ArtifactRegistry | None] | None,
) -> ArtifactRecord:
    record = registry.get(artifact_id)
    if record is None:
        raise _blocked(f"required READY artifact is missing: {artifact_id}")
    if (
        record.status != "ready"
        or record.job_id != registry.job_id
        or record.artifact_type != artifact_type
        or record.artifact_version != artifact_version
        or not record.content_hash
    ):
        raise _blocked(f"artifact identity is invalid: {artifact_id}")
    try:
        registry.verify_ready_artifact_closure(
            record,
            external_registry_resolver=external_registry_resolver,
        )
    except (OSError, RegistryError, TypeError, ValueError) as exc:
        raise _blocked(f"artifact closure is untrusted for {artifact_id}: {exc}") from exc
    return record


def _ref(record: ArtifactRecord) -> ArtifactDependencyRefV2:
    return ArtifactDependencyRefV2.from_record(record)


def _same_ref(left: ArtifactDependencyRefV2, right: ArtifactRecord) -> bool:
    try:
        return left == ArtifactDependencyRefV2.from_record(right)
    except (OSError, TypeError, ValueError):
        return False


def _requires_direct_ref(
    owner: ArtifactRecord,
    dependency: ArtifactRecord,
    *,
    label: str,
) -> None:
    if not any(_same_ref(ref, dependency) for ref in owner.depends_on):
        raise _blocked(f"{label} does not directly bind {dependency.artifact_id}")


def _summary_rows(record: ArtifactRecord) -> list[Mapping[str, Any]]:
    document = _read_json(record.path, label="current summary output")
    if isinstance(document, list):
        rows = document
    elif isinstance(document, Mapping) and isinstance(document.get("summaries"), list):
        rows = document["summaries"]
    else:
        raise _blocked("current summary output is not a canonical summary list")
    if not all(isinstance(row, Mapping) for row in rows):
        raise _blocked("current summary output contains a non-object row")
    return list(rows)


def _receipt_rows(record: ArtifactRecord) -> list[Mapping[str, Any]]:
    rows: list[Mapping[str, Any]] = []
    try:
        for line in Path(record.path).read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, Mapping):
                raise _blocked("Stage 1 receipt ledger contains a non-object row")
            rows.append(value)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise _blocked(f"Stage 1 receipt ledger is unreadable: {exc}") from exc
    return rows


def _provider_attempted(receipt: Mapping[str, Any]) -> bool:
    metadata = receipt.get("metadata")
    return (
        str(receipt.get("status") or "") == "success"
        or (isinstance(metadata, Mapping) and bool(metadata.get("transport_config")))
    )


def _stage1_transport_receipts_exist(
    workspace: JobWorkspace,
    registry: ArtifactRegistry,
    *,
    external_registry_resolver: Callable[[str], ArtifactRegistry | None] | None,
) -> bool:
    """Fail closed when any registered or exact-attempt Stage 1 receipt shows transport."""

    ledger = registry.get("stage1_provider_receipts")
    if ledger is not None:
        if (
            ledger.status != "ready"
            or ledger.job_id != registry.job_id
            or ledger.artifact_type != "provider_receipt_ledger"
            or ledger.artifact_version != "v1"
        ):
            return True
        try:
            registry.verify_ready_artifact_closure(
                ledger,
                external_registry_resolver=external_registry_resolver,
            )
            rows = _receipt_rows(ledger)
        except (OSError, RegistryError, TypeError, ValueError):
            return True
        if any(
            str(row.get("job_id") or "") == registry.job_id
            and str(row.get("stage_name") or "") == "stage1_analyze"
            and _provider_attempted(row)
            for row in rows
        ):
            return True

    graph = registry.get("stage1:provider_expected_call_graph")
    if graph is None:
        return False
    try:
        document = _payload(_read_json(graph.path, label="Stage 1 expected-call graph"))
        attempt_id = str(document.get("attempt_id") or "")
        if not attempt_id:
            return True
        safe_attempt = re.sub(r"[^A-Za-z0-9_.-]+", "_", attempt_id or "stage1")
        staged_path = Path(
            workspace.artifact_path(
                f".publication-staging/provider-receipts/stage1/{safe_attempt}.jsonl"
            )
        )
        if not staged_path.is_file():
            return False
        staged_rows: list[Mapping[str, Any]] = []
        for line in staged_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, Mapping):
                return True
            staged_rows.append(value)
        return any(
            str(row.get("job_id") or "") == registry.job_id
            and str(row.get("stage_name") or "") == "stage1_analyze"
            and str(row.get("attempt_id") or "") == attempt_id
            and _provider_attempted(row)
            for row in staged_rows
        )
    except (OSError, UnicodeError, json.JSONDecodeError, RegistryError, TypeError, ValueError):
        return True


def _pretransport_no_call_proven(
    workspace: JobWorkspace,
    provider_budget_controller: Any | None,
) -> bool:
    """Require the Runner's job-bound durable budget to prove zero calls/reservations."""

    if provider_budget_controller is None:
        return False
    try:
        from runtime.provider_runtime import (
            ProviderBudgetController,
            acceptance_execution_context_from_environment,
            current_acceptance_execution_context,
            current_runtime_control_context,
        )

        control = current_runtime_control_context()
        if control is None or not isinstance(provider_budget_controller, ProviderBudgetController):
            return False
        identity = control.identity
        if (
            identity.job_id != workspace.job_id
            or Path(identity.workspace_path).resolve() != Path(workspace.root_dir).resolve()
        ):
            return False
        acceptance = (
            current_acceptance_execution_context()
            or acceptance_execution_context_from_environment()
        )
        if acceptance is None:
            expected_run_id = f"run:{workspace.job_id}"
            expected_state_path = Path(
                workspace.log_path("acceptance_budget_state_v1.json")
            ).expanduser().resolve()
        else:
            if not acceptance.owner_authorized:
                return False
            expected_run_id = acceptance.acceptance_run_id
            expected_state_path = Path(acceptance.provider_budget_state_path).expanduser().resolve()
            if provider_budget_controller.budget != acceptance.provider_budget:
                return False
        if (
            not expected_run_id
            or identity.acceptance_run_id != expected_run_id
            or provider_budget_controller.acceptance_run_id != expected_run_id
            or not provider_budget_controller.state_started
            or provider_budget_controller.state_path is None
            or provider_budget_controller.state_path.expanduser().resolve() != expected_state_path
        ):
            return False
        provider_budget_controller.assert_state_integrity(
            acceptance_run_id=expected_run_id,
            state_started=True,
        )
        snapshot = provider_budget_controller.snapshot()
        calls_used = snapshot.get("calls_used")
        calls_reserved = snapshot.get("calls_reserved")
        return (
            isinstance(calls_used, int)
            and not isinstance(calls_used, bool)
            and calls_used == 0
            and isinstance(calls_reserved, int)
            and not isinstance(calls_reserved, bool)
            and calls_reserved == 0
        )
    except (OSError, RegistryError, RuntimeError, TypeError, ValueError):
        return False


def _pretransport_fallback_allowed(
    workspace: JobWorkspace,
    registry: ArtifactRegistry,
    provider_budget_controller: Any | None,
    *,
    external_registry_resolver: Callable[[str], ArtifactRegistry | None] | None,
) -> bool:
    return (
        _pretransport_no_call_proven(workspace, provider_budget_controller)
        and not _stage1_transport_receipts_exist(
            workspace,
            registry,
            external_registry_resolver=external_registry_resolver,
        )
    )


def _validate_existing_analyze_terminals(
    terminal_records: list[ArtifactRecord],
    registry: ArtifactRegistry,
) -> None:
    """Validate terminal history bytes; current registered outputs decide eligibility."""

    if not terminal_records:
        return
    from runtime.stage_terminal import TerminalStageRecordV1

    for record in terminal_records:
        if (
            record.status != "ready"
            or record.job_id != registry.job_id
            or record.artifact_type != "runtime_stage_terminal"
            or record.artifact_version != "v1"
        ):
            raise _blocked("an Analyze terminal is invalid; existing reconciliation must handle it")
        try:
            ArtifactRegistry._verify_ready_artifact(record)
            terminal = TerminalStageRecordV1.from_dict(
                _read_json(record.path, label="existing Analyze terminal")
            )
        except (OSError, RegistryError, RuntimeError, TypeError, ValueError) as exc:
            raise _blocked(f"existing Analyze terminal is untrusted: {exc}") from exc
        if (
            terminal.job_id != registry.job_id
            or terminal.stage_name not in {"analyze", "stage1_analyze"}
            or terminal.status not in {"failed", "cancelled", "succeeded"}
        ):
            raise _blocked("existing Analyze terminal identity/status is invalid")


def _stage1_call_owner(call: Mapping[str, Any]) -> tuple[str, bool]:
    """Decode only the Stage 1 call/node forms emitted by the producer."""

    call_id = str(call.get("call_id") or "")
    node_id = str(call.get("node_id") or "")
    if call_id.startswith("stage1_synthesis:"):
        paper_key = call_id.removeprefix("stage1_synthesis:")
        expected_node = paper_key
        summary_receipt = True
    elif call_id.startswith("stage1_mineru_parse:"):
        paper_key = call_id.removeprefix("stage1_mineru_parse:")
        expected_node = f"{paper_key}:mineru_parse"
        summary_receipt = False
    else:
        visual_prefix = next(
            (
                prefix
                for prefix in (
                    "stage1_visual_scan:",
                    "stage1_visual_extract:",
                )
                if call_id.startswith(prefix)
            ),
            "",
        )
        if not visual_prefix:
            raise _blocked(f"unsupported Stage 1 call identity: {call_id or '<missing>'}")
        remainder = call_id.removeprefix(visual_prefix)
        paper_key, separator, batch_index = remainder.rpartition(":")
        if not separator or not paper_key or not batch_index.isdigit():
            raise _blocked(f"malformed Stage 1 visual call identity: {call_id}")
        visual_kind = "visual_scan" if visual_prefix == "stage1_visual_scan:" else "visual_extract"
        expected_node = f"{paper_key}:{visual_kind}:{batch_index}"
        summary_receipt = True
    if not paper_key or node_id != expected_node:
        raise _blocked(f"Stage 1 call/node ownership mismatch: {call_id}")
    return paper_key, summary_receipt


def _typed_empty_source_import(
    *,
    registry: ArtifactRegistry,
    source_bundle: ArtifactRecord,
    runtime_spec: ArtifactRecord,
    graph: ArtifactRecord,
    graph_document: Mapping[str, Any],
    source_document: Mapping[str, Any],
    runtime_document: Mapping[str, Any],
    closure_record: ArtifactRecord | None,
    ledger_record: ArtifactRecord | None,
    external_registry_resolver: Callable[[str], ArtifactRegistry | None] | None,
) -> bool:
    """Recognize only the deterministic, typed-manifest zero-call import route."""

    source_snapshot = source_document.get("source_snapshot")
    work_items = source_document.get("paper_work_items")
    reuse_paths = runtime_document.get("reuse_summary_files")
    if (
        not isinstance(work_items, list)
        or work_items
        or not isinstance(source_snapshot, Mapping)
        or source_snapshot.get("summary_only") is not True
        or source_snapshot.get("reuse_only_stage1") is not True
        or not isinstance(reuse_paths, list)
        or not reuse_paths
        or not all(isinstance(path, str) and path.strip() for path in reuse_paths)
        or closure_record is None
        or ledger_record is not None
    ):
        return False
    for path in reuse_paths:
        document = _payload(_read_json(path, label="typed Stage 1 reuse manifest"))
        if (
            str(document.get("artifact_type") or "") != "stage1_reusable_summary_manifest"
            or str(document.get("artifact_version") or "") not in {"v1", "v2"}
        ):
            return False

    closure = _record(
        registry,
        closure_record.artifact_id,
        artifact_type="provider_receipt_closure",
        artifact_version="v1",
        external_registry_resolver=external_registry_resolver,
    )
    _requires_direct_ref(closure, source_bundle, label="zero-call Stage 1 closure")
    _requires_direct_ref(closure, runtime_spec, label="zero-call Stage 1 closure")
    _requires_direct_ref(closure, graph, label="zero-call Stage 1 closure")
    graph_hash = str(graph_document.get("expected_call_graph_hash") or "")
    attempt_id = str(graph_document.get("attempt_id") or "")
    epoch_id = str(graph_document.get("closure_epoch_id") or "")
    closure_document = _read_json(closure.path, label="zero-call Stage 1 provider closure")
    closure_payload = _payload(closure_document)
    expected_ids = closure_payload.get("expected_call_ids")
    observed_ids = closure_payload.get("observed_call_ids")
    if (
        str(closure_document.get("job_id") or "") != registry.job_id
        or str(closure_document.get("stage_name") or "") != "stage1_analyze"
        or str(closure_document.get("attempt_id") or "") != attempt_id
        or str(closure_document.get("closure_epoch_id") or "") != epoch_id
        or str(closure_document.get("expected_call_graph_hash") or "") != graph_hash
        or str(closure.metadata.get("expected_call_graph_hash") or "") != graph_hash
        or closure_payload.get("complete") is not True
        or expected_ids != []
        or observed_ids != []
        or closure_document.get("expected_calls") != []
        or any(
            closure_payload.get(field)
            for field in (
                "missing_call_ids",
                "stale_call_ids",
                "failed_call_ids",
                "incomplete_call_ids",
                "unexpected_receipts",
                "out_of_scope_receipts",
                "out_of_epoch_receipts",
                "historical_receipts",
                "retry_exceeded_call_ids",
                "usage_incomplete_call_ids",
                "hash_mismatches",
                "verified_reuse_call_ids",
            )
        )
    ):
        raise _blocked("typed empty-source Stage 1 closure is not an exact zero-call closure")
    return True


def _all_reused_zero_call_recovery(
    *,
    workspace: JobWorkspace,
    registry: ArtifactRegistry,
    source_bundle: ArtifactRecord,
    runtime_spec: ArtifactRecord,
    graph: ArtifactRecord,
    graph_document: Mapping[str, Any],
    source_document: Mapping[str, Any],
    closure_record: ArtifactRecord | None,
    ledger_record: ArtifactRecord | None,
    expected_source_bundle_hash: str,
    expected_runtime_spec_hash: str,
    external_registry_resolver: Callable[[str], ArtifactRegistry | None] | None,
    schema_validators: Mapping[str, Any] | None,
) -> ProvenStageRecovery | None:
    """Reconstruct only a complete, source-closed all-reuse publication."""

    work_items = source_document.get("paper_work_items")
    expected_calls = graph_document.get("expected_calls")
    if (
        not isinstance(work_items, list)
        or not work_items
        or expected_calls != []
        or closure_record is None
        or ledger_record is not None
    ):
        return None
    paper_keys = [
        str(item.get("canonical_paper_key") or "")
        for item in work_items
        if isinstance(item, Mapping)
    ]
    if (
        len(paper_keys) != len(work_items)
        or any(not paper_key for paper_key in paper_keys)
        or len(set(paper_keys)) != len(paper_keys)
    ):
        return None
    work_item_by_key = {
        str(item.get("canonical_paper_key") or ""): item
        for item in work_items
        if isinstance(item, Mapping)
    }

    closure = _record(
        registry,
        closure_record.artifact_id,
        artifact_type="provider_receipt_closure",
        artifact_version="v1",
        external_registry_resolver=external_registry_resolver,
    )
    manifest_record = registry.get("summary_source_manifest")
    for dependency in (source_bundle, runtime_spec, graph, manifest_record):
        if dependency is None:
            return None
        _requires_direct_ref(closure, dependency, label="all-reuse Stage 1 closure")
    closure_document = _read_json(closure.path, label="all-reuse Stage 1 provider closure")
    closure_payload = _payload(closure_document)
    graph_hash = str(graph_document.get("expected_call_graph_hash") or "")
    attempt_id = str(graph_document.get("attempt_id") or "")
    epoch_id = str(graph_document.get("closure_epoch_id") or "")
    if (
        str(graph_document.get("source_bundle_hash") or "") != expected_source_bundle_hash
        or str(graph_document.get("runtime_spec_hash") or "") != expected_runtime_spec_hash
        or str(closure_document.get("job_id") or "") != registry.job_id
        or str(closure_document.get("stage_name") or "") != "stage1_analyze"
        or str(closure_document.get("attempt_id") or "") != attempt_id
        or str(closure_document.get("closure_epoch_id") or "") != epoch_id
        or str(closure_document.get("expected_call_graph_hash") or "") != graph_hash
        or closure_payload.get("complete") is not True
        or closure_payload.get("expected_call_ids") != []
        or closure_payload.get("observed_call_ids") != []
        or closure_document.get("expected_calls") != []
        or any(
            closure_payload.get(field)
            for field in (
                "missing_call_ids",
                "stale_call_ids",
                "failed_call_ids",
                "incomplete_call_ids",
                "unexpected_receipts",
                "out_of_scope_receipts",
                "out_of_epoch_receipts",
                "historical_receipts",
                "retry_exceeded_call_ids",
                "usage_incomplete_call_ids",
                "hash_mismatches",
                "verified_reuse_call_ids",
            )
        )
    ):
        raise _blocked("all-reuse Stage 1 closure is not an exact zero-call closure")

    summary_file = _record(
        registry,
        "summary_file",
        artifact_type="summary_file",
        artifact_version="v1",
        external_registry_resolver=external_registry_resolver,
    )
    summary_manifest = _record(
        registry,
        "summary_source_manifest",
        artifact_type="summary_source_manifest",
        artifact_version="v2",
        external_registry_resolver=external_registry_resolver,
    )
    progress = _record(
        registry,
        "stage1_progress_snapshot",
        artifact_type="stage1_progress_snapshot",
        artifact_version="v1",
        external_registry_resolver=external_registry_resolver,
    )
    versioned_summary_id = str(summary_file.metadata.get("current_version_artifact_id") or "")
    if summary_file.metadata.get("pointer_role") != "current" or not versioned_summary_id:
        raise _blocked("all-reuse summary pointer is not current")
    versioned_summary = _record(
        registry,
        versioned_summary_id,
        artifact_type="summary_file",
        artifact_version="v1",
        external_registry_resolver=external_registry_resolver,
    )
    if (
        versioned_summary.content_hash != summary_file.content_hash
        or Path(versioned_summary.path).resolve() != Path(summary_file.path).resolve()
        or versioned_summary.metadata.get("immutable") is not True
    ):
        raise _blocked("all-reuse current summary pointer differs from its immutable version")

    summary_rows = _summary_rows(summary_file)
    if len(summary_rows) != len(paper_keys):
        raise _blocked("all-reuse summary count differs from source work items")
    source_manifest = _payload(_read_json(summary_manifest.path, label="all-reuse summary source manifest"))
    source_items = source_manifest.get("source_items")
    if not isinstance(source_items, list) or len(source_items) != len(paper_keys):
        raise _blocked("all-reuse source manifest does not cover source work items")
    source_item_by_key = {
        str(item.get("canonical_paper_key") or ""): item
        for item in source_items
        if isinstance(item, Mapping)
    }
    if set(source_item_by_key) != set(paper_keys):
        raise _blocked("all-reuse source manifest paper identities differ")
    summary_by_key: dict[str, Mapping[str, Any]] = {}
    for row in summary_rows:
        paper_info = row.get("paper_info")
        paper_key = str(paper_info.get("canonical_paper_key") or "") if isinstance(paper_info, Mapping) else ""
        provider = row.get("provider")
        if (
            paper_key not in set(paper_keys)
            or paper_key in summary_by_key
            or str(row.get("status") or "") != "success"
            or not isinstance(provider, Mapping)
            or provider.get("receipt_ids") != []
        ):
            raise _blocked("all-reuse summary is not a successful provider-free paper row")
        summary_by_key[paper_key] = row
    for paper_key in paper_keys:
        source_item = source_item_by_key[paper_key]
        source_paper_id = str(work_item_by_key[paper_key].get("source_paper_id") or "")
        if (
            str(source_item.get("disposition") or "") != "reused"
            or source_item.get("provider_receipt_ids") != []
            or str(source_item.get("source_paper_id") or "") != source_paper_id
        ):
            raise _blocked(f"all-reuse source item is not provider-free for {paper_key}")
        paper_info = summary_by_key[paper_key].get("paper_info")
        if (
            not isinstance(paper_info, Mapping)
            or str(paper_info.get("source_paper_id") or "") != source_paper_id
        ):
            raise _blocked(f"all-reuse summary source identity differs for {paper_key}")

    paper_records: list[ArtifactRecord] = []
    for paper_key in paper_keys:
        paper_record = _record(
            registry,
            f"paper:{hashlib.sha256(paper_key.encode('utf-8')).hexdigest()[:24]}",
            artifact_type="paper_artifact",
            artifact_version="v1",
            external_registry_resolver=external_registry_resolver,
        )
        paper_records.append(paper_record)
    output_records = [
        summary_file,
        progress,
        summary_manifest,
        *paper_records,
        graph,
        closure,
    ]
    output_refs = tuple(_ref(record) for record in output_records)
    candidate_terminal = ArtifactRecord(
        artifact_id="stage1-all-reuse-recovery-probe",
        artifact_role="runtime_stage_terminal",
        artifact_type="runtime_stage_terminal",
        artifact_version="v1",
        path="",
        producer="runtime.stage1_publication_recovery.proof_probe",
        job_id=registry.job_id,
        status="ready",
        content_hash="",
    )
    _entry, issues = _provider_closure_entry(
        "analyze",
        closure,
        candidate_terminal,
        {
            "status": "succeeded",
            "stage_name": "analyze",
            "model_call_count": 0,
            "output_artifact_refs": [ref.to_dict() for ref in output_refs],
        },
        registry,
        current_set=None,
        external_registry_resolver=external_registry_resolver,
    )
    if issues:
        raise _blocked("all-reuse closure proof rejected recovery: " + "; ".join(issues))
    reconciler = RuntimeReconciler(
        workspace,
        registry,
        schema_validators=schema_validators,
        external_registry_resolver=external_registry_resolver,
    )
    for ref in output_refs:
        try:
            reconciler.validate_dependency_ref(ref)
        except (OSError, RegistryError, TypeError, ValueError) as exc:
            raise _blocked(f"all-reuse output reference is untrusted: {ref.artifact_id}: {exc}") from exc
    return ProvenStageRecovery(
        stage_name="analyze",
        attempt_id=attempt_id,
        output_artifact_refs=output_refs,
        input_artifact_refs=(),
        model_call_count=0,
    )


def try_recover_registered_stage1_publication(
    workspace: JobWorkspace,
    registry: ArtifactRegistry,
    *,
    expected_source_bundle_hash: str,
    expected_runtime_spec_hash: str,
    provider_budget_controller: Any | None = None,
    external_registry_resolver: Callable[[str], ArtifactRegistry | None] | None = None,
    schema_validators: Mapping[str, Any] | None = None,
) -> ProvenStageRecovery | None:
    """Return a terminal-reconstruction proposal for a fully closed Stage 1.

    ``None`` means the existing terminal/reconciliation path applies, a
    job-bound budget proves Stage 1 never crossed transport, or a typed empty
    source import must continue through its existing verifier. Incomplete
    evidence after transport may have started is blocked.
    """

    if workspace.job_id != registry.job_id:
        raise _blocked("workspace and Registry jobs differ")

    terminal_records = [
        record
        for record in registry.list_records()
        if record.artifact_type == "runtime_stage_terminal"
        and str(record.metadata.get("stage_name") or "") in {"analyze", "stage1_analyze"}
    ]
    _validate_existing_analyze_terminals(
        terminal_records,
        registry,
    )

    graph_record = registry.get("stage1:provider_expected_call_graph")
    closure_record = registry.get("stage1:provider_receipt_closure")
    ledger_record = registry.get("stage1_provider_receipts")
    if graph_record is None:
        if _pretransport_fallback_allowed(
            workspace,
            registry,
            provider_budget_controller,
            external_registry_resolver=external_registry_resolver,
        ):
            return None
        reason = (
            "no Stage 1 publication exists, but a zero-transport run is not proven"
            if closure_record is None and ledger_record is None
            else "Stage 1 closure/receipt evidence exists without its expected-call graph"
        )
        raise _blocked(reason)
    if not expected_source_bundle_hash or not expected_runtime_spec_hash:
        if _pretransport_fallback_allowed(
            workspace,
            registry,
            provider_budget_controller,
            external_registry_resolver=external_registry_resolver,
        ):
            return None
        raise _blocked("current source-bundle or runtime-spec hash was not supplied")

    try:
        source_bundle = _record(
            registry,
            "source_bundle",
            artifact_type="source_bundle",
            artifact_version="v1",
            external_registry_resolver=external_registry_resolver,
        )
        runtime_spec = _record(
            registry,
            "runtime_job_spec",
            artifact_type="runtime_job_spec",
            artifact_version="v1",
            external_registry_resolver=external_registry_resolver,
        )
    except Stage1PublicationRecoveryBlocked:
        if _pretransport_fallback_allowed(
            workspace,
            registry,
            provider_budget_controller,
            external_registry_resolver=external_registry_resolver,
        ):
            return None
        raise
    if source_bundle.content_hash != expected_source_bundle_hash:
        raise _blocked("current source bundle differs from the published Stage 1 source")
    if runtime_spec.content_hash != expected_runtime_spec_hash:
        raise _blocked("current RuntimeJobSpec differs from the published Stage 1 configuration")

    try:
        graph = _record(
            registry,
            "stage1:provider_expected_call_graph",
            artifact_type="provider_expected_call_graph",
            artifact_version="v1",
            external_registry_resolver=external_registry_resolver,
        )
    except Stage1PublicationRecoveryBlocked:
        if _pretransport_fallback_allowed(
            workspace,
            registry,
            provider_budget_controller,
            external_registry_resolver=external_registry_resolver,
        ):
            return None
        raise
    source_document = _payload(_read_json(source_bundle.path, label="Stage 1 source bundle"))
    runtime_document = _payload(_read_json(runtime_spec.path, label="Stage 1 RuntimeJobSpec"))
    graph_document = _payload(_read_json(graph.path, label="Stage 1 expected-call graph"))
    graph_hash = str(graph_document.get("expected_call_graph_hash") or "")
    attempt_id = str(graph_document.get("attempt_id") or "")
    epoch_id = str(graph_document.get("closure_epoch_id") or "")
    expected_calls = graph_document.get("expected_calls")
    if (
        str(graph_document.get("job_id") or "") != registry.job_id
        or str(graph_document.get("stage_name") or "") != "stage1_analyze"
        or str(graph_document.get("source_bundle_hash") or "") != expected_source_bundle_hash
        or str(graph_document.get("runtime_spec_hash") or "") != expected_runtime_spec_hash
        or not graph_hash
        or not attempt_id
        or not epoch_id
        or str(graph.metadata.get("expected_call_graph_hash") or "") != graph_hash
        or not isinstance(expected_calls, list)
        or any(not isinstance(call, Mapping) for call in expected_calls)
    ):
        raise _blocked("Stage 1 expected-call graph does not bind the current job/source/spec")

    if not expected_calls and _typed_empty_source_import(
        registry=registry,
        source_bundle=source_bundle,
        runtime_spec=runtime_spec,
        graph=graph,
        graph_document=graph_document,
        source_document=source_document,
        runtime_document=runtime_document,
        closure_record=closure_record,
        ledger_record=ledger_record,
        external_registry_resolver=external_registry_resolver,
    ):
        return None

    source_scope_record = registry.get("stage1_source_scope")
    if source_scope_record is None:
        if _pretransport_fallback_allowed(
            workspace,
            registry,
            provider_budget_controller,
            external_registry_resolver=external_registry_resolver,
        ):
            return None
        raise _blocked("Stage 1 source scope is missing")
    try:
        source_scope = _record(
            registry,
            source_scope_record.artifact_id,
            artifact_type="stage1_source_scope",
            artifact_version="v1",
            external_registry_resolver=external_registry_resolver,
        )
    except Stage1PublicationRecoveryBlocked:
        if _pretransport_fallback_allowed(
            workspace,
            registry,
            provider_budget_controller,
            external_registry_resolver=external_registry_resolver,
        ):
            return None
        raise
    _requires_direct_ref(graph, source_scope, label="Stage 1 expected-call graph")
    _requires_direct_ref(source_scope, source_bundle, label="Stage 1 source scope")
    _requires_direct_ref(source_scope, runtime_spec, label="Stage 1 source scope")

    if not expected_calls:
        reuse_recovery = _all_reused_zero_call_recovery(
            workspace=workspace,
            registry=registry,
            source_bundle=source_bundle,
            runtime_spec=runtime_spec,
            graph=graph,
            graph_document=graph_document,
            source_document=source_document,
            closure_record=closure_record,
            ledger_record=ledger_record,
            expected_source_bundle_hash=expected_source_bundle_hash,
            expected_runtime_spec_hash=expected_runtime_spec_hash,
            external_registry_resolver=external_registry_resolver,
            schema_validators=schema_validators,
        )
        if reuse_recovery is not None:
            return reuse_recovery
        raise _blocked("zero-call Stage 1 graph is not a fully closed all-reuse publication")
    if closure_record is None or ledger_record is None:
        if _pretransport_fallback_allowed(
            workspace,
            registry,
            provider_budget_controller,
            external_registry_resolver=external_registry_resolver,
        ):
            return None
        raise _blocked("Stage 1 publication is partial and prior transport cannot be ruled out")

    closure = _record(
        registry,
        "stage1:provider_receipt_closure",
        artifact_type="provider_receipt_closure",
        artifact_version="v1",
        external_registry_resolver=external_registry_resolver,
    )
    ledger = _record(
        registry,
        "stage1_provider_receipts",
        artifact_type="provider_receipt_ledger",
        artifact_version="v1",
        external_registry_resolver=external_registry_resolver,
    )
    summary_file = _record(
        registry,
        "summary_file",
        artifact_type="summary_file",
        artifact_version="v1",
        external_registry_resolver=external_registry_resolver,
    )
    summary_manifest = _record(
        registry,
        "summary_source_manifest",
        artifact_type="summary_source_manifest",
        artifact_version="v2",
        external_registry_resolver=external_registry_resolver,
    )
    progress = _record(
        registry,
        "stage1_progress_snapshot",
        artifact_type="stage1_progress_snapshot",
        artifact_version="v1",
        external_registry_resolver=external_registry_resolver,
    )

    current_version_id = str(summary_file.metadata.get("current_version_artifact_id") or "")
    if summary_file.metadata.get("pointer_role") != "current" or not current_version_id:
        raise _blocked("summary_file is not the current Registry-selected Stage 1 output")
    versioned_summary = _record(
        registry,
        current_version_id,
        artifact_type="summary_file",
        artifact_version="v1",
        external_registry_resolver=external_registry_resolver,
    )
    if (
        versioned_summary.content_hash != summary_file.content_hash
        or Path(versioned_summary.path).resolve() != Path(summary_file.path).resolve()
        or versioned_summary.metadata.get("immutable") is not True
    ):
        raise _blocked("current summary pointer does not match its immutable summary record")

    _requires_direct_ref(graph, source_scope, label="Stage 1 expected-call graph")
    _requires_direct_ref(source_scope, source_bundle, label="Stage 1 source scope")
    _requires_direct_ref(source_scope, runtime_spec, label="Stage 1 source scope")
    for dependency in (source_bundle, runtime_spec, graph, ledger, summary_manifest):
        _requires_direct_ref(closure, dependency, label="Stage 1 provider closure")
    for dependency in (source_bundle, ledger):
        _requires_direct_ref(summary_file, dependency, label="current summary output")
        _requires_direct_ref(summary_manifest, dependency, label="summary source manifest")
    _requires_direct_ref(progress, summary_file, label="Stage 1 progress snapshot")

    closure_document = _read_json(closure.path, label="Stage 1 provider closure")
    closure_payload = _payload(closure_document)
    closure_graph_hash = str(
        closure_document.get("expected_call_graph_hash")
        or closure_payload.get("expected_call_graph_hash")
        or ""
    )
    closure_epoch = str(
        closure_document.get("closure_epoch_id")
        or closure_payload.get("closure_epoch_id")
        or ""
    )
    closure_attempt = str(
        closure_document.get("attempt_id")
        or closure_payload.get("attempt_id")
        or ""
    )
    expected_call_ids = [
        str(call.get("call_id") or "")
        for call in expected_calls
        if isinstance(call, Mapping)
    ]
    if (
        len(expected_call_ids) != len(expected_calls)
        or any(not call_id for call_id in expected_call_ids)
        or len(set(expected_call_ids)) != len(expected_call_ids)
        or closure.metadata.get("expected_call_graph_hash") != graph_hash
        or str(closure.metadata.get("closure_epoch_id") or "") != epoch_id
        or closure_graph_hash != graph_hash
        or closure_epoch != epoch_id
        or closure_attempt != attempt_id
        or str(closure_document.get("job_id") or "") != registry.job_id
        or str(closure_document.get("stage_name") or "") != "stage1_analyze"
        or closure_payload.get("complete") is not True
    ):
        raise _blocked("Stage 1 receipt closure is incomplete or not bound to its call graph")

    work_items = source_document.get("paper_work_items")
    if not isinstance(work_items, list) or not work_items:
        raise _blocked("Stage 1 source bundle has no paper work items")
    work_item_by_key = {
        str(item.get("canonical_paper_key") or ""): item
        for item in work_items
        if isinstance(item, Mapping) and str(item.get("canonical_paper_key") or "")
    }
    paper_keys = list(work_item_by_key)
    if (
        len(paper_keys) != len(work_items)
        or any(not key for key in paper_keys)
        or len(set(paper_keys)) != len(paper_keys)
    ):
        raise _blocked("Stage 1 source bundle paper identities are invalid")

    summary_rows = _summary_rows(summary_file)
    if len(summary_rows) != len(paper_keys):
        raise _blocked("current summary count differs from the source work-item count")
    summary_by_key: dict[str, Mapping[str, Any]] = {}
    for row in summary_rows:
        paper_info = row.get("paper_info")
        paper_key = str(paper_info.get("canonical_paper_key") or "") if isinstance(paper_info, Mapping) else ""
        if (
            not paper_key
            or paper_key in summary_by_key
            or paper_key not in set(paper_keys)
            or str(row.get("status") or "") != "success"
        ):
            raise _blocked("current summary has an unknown, duplicate, or unsuccessful paper row")
        summary_by_key[paper_key] = row

    manifest_document = _payload(_read_json(summary_manifest.path, label="summary source manifest"))
    if (
        str(manifest_document.get("artifact_type") or "") != "summary_source_manifest"
        or str(manifest_document.get("artifact_version") or "") != "v2"
        or str(manifest_document.get("source_kind") or "")
        not in {"provider_generated", "runtime_summary_source"}
        or str(manifest_document.get("source_path") or "") != source_bundle.path
        or Path(str(manifest_document.get("materialized_summary_file") or "")).resolve()
        != Path(summary_file.path).resolve()
        or manifest_document.get("summary_count") != len(paper_keys)
    ):
        raise _blocked("summary source manifest does not bind the current source and summary outputs")
    source_items = manifest_document.get("source_items")
    if not isinstance(source_items, list):
        raise _blocked("summary source manifest has no source-item list")
    source_item_by_key: dict[str, Mapping[str, Any]] = {}
    for item in source_items:
        key = str(item.get("canonical_paper_key") or "") if isinstance(item, Mapping) else ""
        if not key or key in source_item_by_key:
            raise _blocked("summary source manifest contains an invalid or duplicate paper identity")
        source_item_by_key[key] = item
    if set(source_item_by_key) != set(paper_keys):
        raise _blocked("summary source manifest does not cover every source paper")
    for paper_key in paper_keys:
        work_item = work_item_by_key[paper_key]
        source_item = source_item_by_key[paper_key]
        if (
            str(source_item.get("disposition") or "") not in {"provider_generated", "reused"}
            or str(source_item.get("source_paper_id") or "")
            != str(work_item.get("source_paper_id") or "")
        ):
            raise _blocked(f"summary source item does not match the source bundle for {paper_key}")
        paper_info = summary_by_key[paper_key].get("paper_info")
        if (
            not isinstance(paper_info, Mapping)
            or str(paper_info.get("source_paper_id") or "")
            != str(work_item.get("source_paper_id") or "")
        ):
            raise _blocked(f"summary paper identity differs from the source bundle for {paper_key}")

    call_by_id: dict[str, Mapping[str, Any]] = {}
    owner_by_call_id: dict[str, str] = {}
    summary_call_ids_by_paper: dict[str, list[str]] = {paper_key: [] for paper_key in paper_keys}
    synthesis_call_ids_by_paper: dict[str, list[str]] = {paper_key: [] for paper_key in paper_keys}
    verified_reuse_call_ids: set[str] = set()
    for call in expected_calls:
        if not isinstance(call, Mapping):
            raise _blocked("Stage 1 expected-call graph contains a malformed call")
        call_id = str(call.get("call_id") or "")
        paper_key, summary_receipt = _stage1_call_owner(call)
        verified_reuse = call.get("verified_reuse") is True
        if (
            paper_key not in set(paper_keys)
            or not call_id
            or str(call.get("job_id") or "") != registry.job_id
            or str(call.get("stage_name") or "") != "stage1_analyze"
            or str(call.get("expected_call_graph_hash") or "") != graph_hash
        ):
            raise _blocked("Stage 1 expected-call graph call identity does not match source papers")
        if not verified_reuse and (
            str(call.get("attempt_id") or "") != attempt_id
            or str(call.get("logical_attempt_identity") or "") != attempt_id
            or str(call.get("closure_epoch_id") or "") != epoch_id
        ):
            raise _blocked("fresh Stage 1 provider call is not bound to the current attempt/epoch")
        call_by_id[call_id] = call
        owner_by_call_id[call_id] = paper_key
        if verified_reuse:
            verified_reuse_call_ids.add(call_id)
        elif summary_receipt:
            summary_call_ids_by_paper[paper_key].append(call_id)
            if call_id == f"stage1_synthesis:{paper_key}":
                synthesis_call_ids_by_paper[paper_key].append(call_id)

    for paper_key in paper_keys:
        source_item = source_item_by_key[paper_key]
        disposition = str(source_item.get("disposition") or "")
        synthesis_calls = synthesis_call_ids_by_paper[paper_key]
        summary_calls = summary_call_ids_by_paper[paper_key]
        if disposition == "provider_generated" and len(synthesis_calls) != 1:
            raise _blocked(f"generated Stage 1 summary has no unique synthesis call for {paper_key}")
        if disposition == "reused" and summary_calls:
            raise _blocked(f"reused Stage 1 summary has fresh provider calls for {paper_key}")

    ledger_rows = _receipt_rows(ledger)
    from runtime.provider_runtime import ProviderCallReceiptV1

    receipt_ids_by_paper: dict[str, list[str]] = {paper_key: [] for paper_key in paper_keys}
    fresh_call_ids = set(call_by_id) - verified_reuse_call_ids
    physical_call_count = 0
    seen_receipt_ids: set[str] = set()
    for row in ledger_rows:
        call_id = str(row.get("call_id") or "")
        call = call_by_id.get(call_id)
        if (
            call is None
            or call_id not in fresh_call_ids
            or str(row.get("job_id") or "") != registry.job_id
            or str(row.get("stage_name") or "") != "stage1_analyze"
            or str(row.get("closure_epoch_id") or "")
            != str(call.get("closure_epoch_id") or "")
        ):
            continue
        if row.get("test_only") is not False:
            raise _blocked(f"Stage 1 production receipt is test-only or unspecified: {call_id}")
        receipt_id = str(row.get("receipt_id") or "")
        if not receipt_id or receipt_id in seen_receipt_ids:
            raise _blocked(f"Stage 1 receipt identity is missing or duplicated: {call_id}")
        try:
            receipt = ProviderCallReceiptV1.from_dict(row)
        except (RuntimeError, TypeError, ValueError) as exc:
            raise _blocked(f"Stage 1 receipt is malformed for {call_id}: {exc}") from exc
        if isinstance(receipt.attempts, bool) or not isinstance(receipt.attempts, int) or receipt.attempts < 1:
            raise _blocked(f"Stage 1 physical attempt count is invalid for {call_id}")
        seen_receipt_ids.add(receipt_id)
        physical_call_count += receipt.attempts
        if call_id in summary_call_ids_by_paper[owner_by_call_id[call_id]]:
            receipt_ids_by_paper[owner_by_call_id[call_id]].append(receipt_id)

    for paper_key in paper_keys:
        summary_provider = summary_by_key[paper_key].get("provider")
        raw_summary_receipt_ids = (
            summary_provider.get("receipt_ids") if isinstance(summary_provider, Mapping) else None
        )
        raw_source_receipt_ids = source_item_by_key[paper_key].get("provider_receipt_ids")
        if (
            not isinstance(raw_summary_receipt_ids, list)
            or not all(isinstance(item, str) and item for item in raw_summary_receipt_ids)
            or not isinstance(raw_source_receipt_ids, list)
            or not all(isinstance(item, str) and item for item in raw_source_receipt_ids)
        ):
            raise _blocked(f"Stage 1 source receipt IDs are malformed for {paper_key}")
        summary_receipt_ids = list(raw_summary_receipt_ids)
        source_receipt_ids = list(raw_source_receipt_ids)
        if (
            summary_receipt_ids != receipt_ids_by_paper[paper_key]
            or source_receipt_ids != summary_receipt_ids
        ):
            raise _blocked(f"Stage 1 receipt/output binding is incomplete for {paper_key}")

    paper_records: list[ArtifactRecord] = []
    for paper_key in paper_keys:
        artifact_id = f"paper:{hashlib.sha256(paper_key.encode('utf-8')).hexdigest()[:24]}"
        paper_record = _record(
            registry,
            artifact_id,
            artifact_type="paper_artifact",
            artifact_version="v1",
            external_registry_resolver=external_registry_resolver,
        )
        paper_document = _payload(_read_json(paper_record.path, label=f"paper artifact {paper_key}"))
        identity = paper_document.get("paper_identity")
        if not isinstance(identity, Mapping) or str(identity.get("canonical_paper_key") or "") != paper_key:
            raise _blocked(f"paper artifact identity differs for {paper_key}")
        paper_records.append(paper_record)

    output_records = [
        summary_file,
        progress,
        summary_manifest,
        *paper_records,
        graph,
        closure,
        ledger,
    ]
    output_refs = tuple(_ref(record) for record in output_records)
    candidate_terminal = ArtifactRecord(
        artifact_id="stage1-publication-recovery-probe",
        artifact_role="runtime_stage_terminal",
        artifact_type="runtime_stage_terminal",
        artifact_version="v1",
        path="",
        producer="runtime.stage1_publication_recovery.proof_probe",
        job_id=registry.job_id,
        status="ready",
        content_hash="",
    )
    candidate_payload = {
        "status": "succeeded",
        "stage_name": "analyze",
        "model_call_count": physical_call_count,
        "output_artifact_refs": [ref.to_dict() for ref in output_refs],
    }
    _entry, issues = _provider_closure_entry(
        "analyze",
        closure,
        candidate_terminal,
        candidate_payload,
        registry,
        current_set=None,
        external_registry_resolver=external_registry_resolver,
    )
    if issues:
        raise _blocked("Stage 1 closure proof rejected recovery: " + "; ".join(issues))

    reconciler = RuntimeReconciler(
        workspace,
        registry,
        schema_validators=schema_validators,
        external_registry_resolver=external_registry_resolver,
    )
    for ref in output_refs:
        try:
            reconciler.validate_dependency_ref(ref)
        except (OSError, RegistryError, TypeError, ValueError) as exc:
            raise _blocked(f"Stage 1 output reference is untrusted: {ref.artifact_id}: {exc}") from exc

    return ProvenStageRecovery(
        stage_name="analyze",
        attempt_id=attempt_id,
        output_artifact_refs=output_refs,
        input_artifact_refs=(),
        model_call_count=physical_call_count,
    )


__all__ = [
    "Stage1PublicationRecoveryBlocked",
    "Stage1PartialReplayCall",
    "Stage1PartialRecoveryContext",
    "prepare_partial_stage1_recovery",
    "try_recover_registered_stage1_publication",
]
