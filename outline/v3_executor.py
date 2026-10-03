"""Executable Outline Intelligence v3 pipeline.

The executor owns node execution, durable artifact writes, provider receipts,
and replay decisions. Evidence views are projected directly from Stage 1;
topic, cross-group, global, and outline decisions use the provider boundary
when a configured production route is available.
"""

from __future__ import annotations

import copy
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
import hashlib
import json
import math
import os
import re
import tempfile
import unicodedata
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from outline.v3_artifacts import (
    ArbitrationDecision,
    ConfirmedGlobalRelationMap,
    CoverageAudit,
    CoverageCritique,
    EvidenceCritique,
    FinalOutline,
    OutlineArtifact,
    OutlineCandidate,
    OutlineStageHealth,
    ProviderReceiptClosureArtifact,
    RelationAdjudicationResult,
    SectionEvidencePacketSet,
    SelectedOutlineCandidate,
    StabilityAudit,
    StructureCritique,
)
from outline.v3_critique import (
    CRITIQUE_DISPOSITION_VERSION,
    derive_critique_disposition,
)
from outline.v3_revision import apply_selected_revision
from outline.v3_evidence import (
    build_coverage_contract,
    build_global_corpus_ledger,
    build_multi_view_matrix,
    build_outline_evidence_views,
    build_review_intent,
    merge_outline_evidence_shards,
    shard_outline_evidence_views,
)
from outline.v3_models import (
    GlobalRelationMap,
    InterpretationDependency,
    OutlineQualityGate,
    SourceFieldLedgerEntry,
    TopicSynthesis,
    compute_v3_hash,
)
from outline.v3_relations import build_global_relation_map, build_organizing_axes, build_outline_candidate_plans
from outline.candidate_repair_plan import OutlineCandidateRepairPlanV1
from outline.semantic_chunking import (
    build_paper_content_layers,
    build_semantic_chunk_plan,
    build_topic_synthesis_plan,
    derive_interpretation_dependencies,
    derive_unit_source_field_ledger,
)
from outline.evidence_alias import alias_structural, canonicalize_structural
from runtime.outline_v3_dag import OutlineNodeDAG, OutlineNodeStore
from runtime.checkout_identity import CheckoutIdentityError, read_checkout_sha
from runtime.pause_state import PauseRequestedError, PauseStateStore
from runtime.provider_completion import ProviderCompletionEvaluator
from runtime.provider_context import ProviderContextProfile
from runtime.runtime_spec_binding import RuntimeSpecBindingV1, read_runtime_spec_binding_v1
from runtime.provider_receipt_closure import ExpectedProviderCall, ProviderReceiptClosure
from outline.provider_router import (
    OutlineProviderRouter,
    OutlineRoleRoute,
    safe_config_identity,
    semantic_role,
)
from runtime.provider_runtime import (
    AcceptanceExecutionContextV1,
    ProviderBudgetV1,
    ProviderRuntime,
    ProviderRuntimeLedger,
    acceptance_execution_context_from_environment,
    authorized_provider_call_limit,
    compute_closure_epoch_id,
    current_acceptance_execution_context,
    hash_json,
    hash_text,
    provider_budget_controller_from_environment,
)
from runtime.outline_v3_replay import ModelCallReplayKey, ModelCallReplayStore
from services.artifact_registry import (
    ArtifactDependencyRefV2,
    ArtifactRecord,
    ArtifactRegistry,
    RegistryError,
    file_sha256,
)
from services.job_workspace import publish_bytes_artifact, publish_json_artifact
from services.durable_io import atomic_replace_with_retry, fsync_file
from services.queue_service import LocalPublicationContext
from services.prompt_registry import PromptRegistry


Provider = Callable[[str, Mapping[str, Any]], Any]
FaultInjector = Callable[[str, Mapping[str, Any]], None]
INTERPRETATION_CONTRACT_VERSION = "source-qualifier-dependency-v1"
SEMANTIC_SYNTHESIS_OUTPUT_LIMIT = 4_096
SHARED_SYNTHESIS_CONTRACT_VERSION = "v1"
MAX_SEMANTIC_REDUCTION_LEVELS = 4
MAX_SEMANTIC_REDUCER_CALLS_PER_STAGE = 24


class OutlineV3ExecutionError(RuntimeError):
    pass


@dataclass(frozen=True)
class OutlineProviderCallPlan:
    """A conservative, per-node provider-call estimate.

    This is an admission estimate, not a provider bill.  The plan is kept
    explicit so a caller can see which prompt, output, reasoning, and cache
    assumptions were used before any transport is attempted.
    """

    artifact_type: str
    artifact_version: str
    job_id: str
    stage_name: str
    closure_epoch_id: str
    logical_attempt_identity: str
    variant_name: str
    node_id: str
    call_id: str
    provider: str
    model: str
    endpoint_type: str
    estimated_input_tokens: int
    estimated_output_tokens: int
    estimated_reasoning_tokens: int
    estimated_cached_input_tokens: int
    estimated_cache_write_tokens: int
    estimated_total_tokens: int
    input_cost_per_1k_tokens: float | None
    output_cost_per_1k_tokens: float | None
    reasoning_cost_per_1k_tokens: float | None
    cache_read_cost_per_1k_tokens: float | None
    cache_write_cost_per_1k_tokens: float | None
    estimated_cost: float | None
    pricing_source: str
    pricing_policy: str
    cost_status: str
    assumptions: tuple[str, ...]
    confidence: str
    upper_bound: bool
    transport_expected: bool
    configured_transport_retry_reserve: int | None
    physical_attempt_upper_bound: int | None
    config_section: str = ""
    api_base_host: str = ""
    route_fingerprint: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        # Keep the semantic names obvious for downstream audit readers while
        # retaining the stable estimate names used by the preflight summary.
        payload.update(
            {
                "prompt_input_tokens": self.estimated_input_tokens,
                "output_tokens": self.estimated_output_tokens,
                "reasoning_tokens": self.estimated_reasoning_tokens,
                "cache_read_tokens": self.estimated_cached_input_tokens,
                "cache_write_tokens": self.estimated_cache_write_tokens,
            }
        )
        return payload


@dataclass(frozen=True)
class OutlineV3ExecutionResult:
    job_id: str
    status: str
    adopted: bool
    artifacts: Mapping[str, str] = field(default_factory=dict)
    node_ids: tuple[str, ...] = ()
    receipt_ids: tuple[str, ...] = ()
    diagnostics: tuple[str, ...] = ()
    dag: OutlineNodeDAG | None = None

    @property
    def ok(self) -> bool:
        return self.status == "ready_for_adoption"

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "status": self.status,
            "adopted": self.adopted,
            "artifacts": dict(self.artifacts),
            "node_ids": list(self.node_ids),
            "receipt_ids": list(self.receipt_ids),
            "diagnostics": list(self.diagnostics),
        }


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _hash_payload(value: Any) -> str:
    return compute_v3_hash(value)


def _provider_result(raw: Any) -> dict[str, Any]:
    if isinstance(raw, Mapping):
        result = dict(raw)
        usage = result.get("usage")
        if isinstance(usage, Mapping):
            for key in ("input_tokens", "output_tokens", "total_tokens", "cached_input_tokens", "reasoning_tokens"):
                if key not in result and key in usage:
                    result[key] = usage[key]
            details = usage.get("input_tokens_details")
            if isinstance(details, Mapping) and "cached_input_tokens" not in result:
                result["cached_input_tokens"] = details.get("cached_tokens")
            details = usage.get("output_tokens_details")
            if isinstance(details, Mapping) and "reasoning_tokens" not in result:
                result["reasoning_tokens"] = details.get("reasoning_tokens")
        if "content" not in result and "output" in result:
            result["content"] = result["output"]
        result.setdefault("status", "success")
        return result
    return {"status": "success", "content": raw}


class OutlineV3Executor:
    """Run and resume the complete current outline DAG."""

    def __init__(
        self,
        *,
        job_id: str,
        summaries: Iterable[Mapping[str, Any]],
        workspace: Any,
        artifact_registry: ArtifactRegistry | None = None,
        provider: Provider | Any | None = None,
        provider_profile: ProviderContextProfile | None = None,
        provider_router: OutlineProviderRouter | None = None,
        enabled_semantic_roles: Iterable[str] | None = None,
        reachable_provider_route_plan: Mapping[str, Any] | None = None,
        candidate_count: int = 5,
        review_intent: Mapping[str, Any] | None = None,
        quality_gate: OutlineQualityGate | Mapping[str, Any] | None = None,
        fault_injector: FaultInjector | None = None,
        cancellation_checker: Callable[[], None] | None = None,
        logical_attempt_identity: str | None = None,
        stability_mode: str = "smoke",
        semantic_repair_enabled: bool = False,
        opaque_alias_enabled: bool = False,
        outline_pilot: Mapping[str, Any] | None = None,
        max_provider_calls: int | None = None,
        max_estimated_cost: float | None = None,
        max_estimated_total_tokens: int | None = 5_000_000,
        estimated_cost_per_1k_tokens: float | None = None,
        input_cost_per_1k_tokens: float | None = None,
        output_cost_per_1k_tokens: float | None = None,
        reasoning_cost_per_1k_tokens: float | None = None,
        cache_read_cost_per_1k_tokens: float | None = None,
        cache_write_cost_per_1k_tokens: float | None = None,
        max_smoke_overhead_ratio: float | None = None,
        max_source_prompt_tokens: int | None = None,
        semantic_output_max_tokens: int = SEMANTIC_SYNTHESIS_OUTPUT_LIMIT,
        semantic_transport_retries: int | None = None,
        technical_shard_target_tokens: int = 0,
        pricing_source: str | None = None,
        pricing_provider: str | None = None,
        pricing_model: str | None = None,
        pricing_version: str | None = None,
        pricing_effective_date: str | None = None,
        pricing_policy: str = "estimate_only_not_billing_v1",
        publication_context: Any | None = None,
        runtime_spec_binding: RuntimeSpecBindingV1 | None = None,
        _skip_exact_replay_verification: bool = False,
    ) -> None:
        if not str(job_id).strip():
            raise ValueError("job_id is required")
        if candidate_count <= 0:
            raise ValueError("candidate_count must be positive")
        normalized_stability_mode = str(stability_mode or "smoke").strip().lower()
        if normalized_stability_mode not in {"off", "smoke", "full"}:
            raise ValueError("stability_mode must be one of: off, smoke, full")
        self.semantic_repair_enabled = bool(semantic_repair_enabled)
        self.runtime_spec_binding = runtime_spec_binding
        self._primary_candidate_repair_plan: OutlineCandidateRepairPlanV1 | None = None
        self._primary_candidate_repair_plan_record: ArtifactRecord | None = None
        self.opaque_alias_enabled = bool(opaque_alias_enabled)
        self.outline_pilot = dict(outline_pilot) if outline_pilot is not None else None
        if self.outline_pilot is not None and self.outline_pilot.get("schema_version") != "outline-topic-pilot/v1":
            raise ValueError("outline_pilot schema_version must be outline-topic-pilot/v1")
        self._pilot_allowed_node_ids: frozenset[str] = frozenset()
        self._pilot_request_hashes: dict[str, str] = {}
        if max_provider_calls is not None and int(max_provider_calls) < 0:
            raise ValueError("max_provider_calls cannot be negative")
        if max_estimated_cost is not None and float(max_estimated_cost) < 0:
            raise ValueError("max_estimated_cost cannot be negative")
        if max_estimated_total_tokens is not None and int(max_estimated_total_tokens) < 0:
            raise ValueError("max_estimated_total_tokens cannot be negative")
        for name, value in {
            "estimated_cost_per_1k_tokens": estimated_cost_per_1k_tokens,
            "input_cost_per_1k_tokens": input_cost_per_1k_tokens,
            "output_cost_per_1k_tokens": estimated_cost_per_1k_tokens if output_cost_per_1k_tokens is None else output_cost_per_1k_tokens,
            "reasoning_cost_per_1k_tokens": estimated_cost_per_1k_tokens if reasoning_cost_per_1k_tokens is None else reasoning_cost_per_1k_tokens,
            "cache_read_cost_per_1k_tokens": cache_read_cost_per_1k_tokens,
            "cache_write_cost_per_1k_tokens": cache_write_cost_per_1k_tokens,
        }.items():
            if value is not None and (not math.isfinite(float(value)) or float(value) < 0):
                raise ValueError(f"{name} must be finite and non-negative")
        if max_smoke_overhead_ratio is not None and (
            not math.isfinite(float(max_smoke_overhead_ratio)) or float(max_smoke_overhead_ratio) < 1.0
        ):
            raise ValueError("max_smoke_overhead_ratio must be at least 1")
        if max_source_prompt_tokens is not None and int(max_source_prompt_tokens) < 0:
            raise ValueError("max_source_prompt_tokens cannot be negative")
        if (
            isinstance(semantic_output_max_tokens, bool)
            or not isinstance(semantic_output_max_tokens, int)
            or semantic_output_max_tokens <= 0
        ):
            raise ValueError("semantic_output_max_tokens must be a positive integer")
        self.semantic_output_max_tokens = semantic_output_max_tokens
        if semantic_transport_retries is not None and int(semantic_transport_retries) < 0:
            raise ValueError("semantic_transport_retries cannot be negative")
        if int(technical_shard_target_tokens) < 0:
            raise ValueError("technical_shard_target_tokens cannot be negative")
        self.job_id = str(job_id)
        self.summaries = [dict(item) for item in summaries]
        self.workspace = workspace
        self.registry = artifact_registry or self._build_registry()
        self.prompt_registry = PromptRegistry()
        self._outline_prompt_identity = self.prompt_registry.identity("outline.node.system.v3")
        self._outline_policy_identity = self.prompt_registry.identity("outline.node.policies.v3")
        self.publication_context = (
            publication_context
            or getattr(self.registry, "publication_context", None)
            or LocalPublicationContext()
        )
        self.provider = provider
        self.profile = provider_profile or ProviderContextProfile.conservative(
            provider="fixture" if provider is None else "configured",
            model="outline-v3",
            endpoint_type="internal",
            model_context_limit=128_000,
            max_output_tokens=4_096,
        )
        # Internal fixture/local runs keep deterministic projections so offline
        # tests never make an accidental provider request.  A configured
        # external route (the production R1 path) must execute the semantic
        # topic/cross/global synthesis nodes through the same provider
        # admission and receipt machinery as the candidate nodes.
        self.semantic_provider_synthesis_enabled = bool(
            provider_router is not None
            or str(self.profile.endpoint_type or "").casefold() not in {"internal", "fixture"}
        )
        self._candidate_interpretation_tables: dict[str, Any] = {}
        self._candidate_output_scope: Any | None = None
        # Role-aware routing is opt-in so existing single-provider callers keep
        # working, but when it is supplied every node must resolve through it.
        self.router = provider_router
        self.routing_diagnostics: tuple[str, ...] = tuple(provider_router.diagnostics) if provider_router else ()
        self.enabled_semantic_roles = (
            frozenset(str(item).strip() for item in enabled_semantic_roles if str(item).strip())
            if enabled_semantic_roles is not None
            else None
        )
        self.reachable_provider_route_plan = (
            dict(reachable_provider_route_plan)
            if reachable_provider_route_plan is not None
            else None
        )
        self.candidate_count = min(12, int(candidate_count))
        self.stability_mode = normalized_stability_mode
        self.max_provider_calls = (
            authorized_provider_call_limit(max_provider_calls)
            if max_provider_calls is not None or self.semantic_provider_synthesis_enabled
            else None
        )
        self.max_estimated_cost = float(max_estimated_cost) if max_estimated_cost is not None else None
        self.max_estimated_total_tokens = (
            int(max_estimated_total_tokens) if max_estimated_total_tokens is not None else None
        )
        self.estimated_cost_per_1k_tokens = (
            float(estimated_cost_per_1k_tokens)
            if estimated_cost_per_1k_tokens is not None
            else None
        )
        self.pricing_source = str(pricing_source or "").strip()
        self.pricing_provider = str(pricing_provider or self.profile.provider or "").strip()
        self.pricing_model = str(pricing_model or self.profile.model or "").strip()
        self.pricing_version = str(pricing_version or "").strip()
        self.pricing_effective_date = str(pricing_effective_date or "").strip()
        source_lower = self.pricing_source.casefold()
        source_has_version = bool(re.search(r"(?:^|[-_:])v[0-9][a-z0-9._-]*$", source_lower))
        source_is_generic = source_lower.startswith("config:") or "generic" in source_lower
        pricing_identity_bound = bool(
            self.pricing_provider
            and self.pricing_model
            and (self.pricing_version or self.pricing_effective_date or source_has_version)
            and not source_is_generic
        )
        self._pricing_is_explicit = bool(
            self.pricing_source
            and pricing_identity_bound
            and all(
                value is not None
                for value in (
                    input_cost_per_1k_tokens,
                    output_cost_per_1k_tokens,
                    reasoning_cost_per_1k_tokens,
                    cache_read_cost_per_1k_tokens,
                    cache_write_cost_per_1k_tokens,
                )
            )
        )
        self._pricing_unknown_due_to_multiple_routes = False
        if self.router is not None:
            route_identities = {
                tuple(route.binding_identity)
                for route in self.router.routes.values()
            }
            if len(route_identities) > 1:
                # Outline v3 can have different providers, gateways, and token
                # prices per node. A stage-wide rate tuple must not be applied
                # to all of them as if it were one provider invoice.
                self._pricing_is_explicit = False
                self._pricing_unknown_due_to_multiple_routes = True
        self.input_cost_per_1k_tokens = (
            float(input_cost_per_1k_tokens) if input_cost_per_1k_tokens is not None else None
        )
        self.output_cost_per_1k_tokens = (
            float(output_cost_per_1k_tokens) if output_cost_per_1k_tokens is not None else None
        )
        self.reasoning_cost_per_1k_tokens = (
            float(reasoning_cost_per_1k_tokens) if reasoning_cost_per_1k_tokens is not None else None
        )
        self.cache_read_cost_per_1k_tokens = (
            float(cache_read_cost_per_1k_tokens) if cache_read_cost_per_1k_tokens is not None else None
        )
        self.cache_write_cost_per_1k_tokens = (
            float(cache_write_cost_per_1k_tokens) if cache_write_cost_per_1k_tokens is not None else None
        )
        self.max_smoke_overhead_ratio = (
            float(max_smoke_overhead_ratio) if max_smoke_overhead_ratio is not None else None
        )
        self.max_source_prompt_tokens = (
            int(max_source_prompt_tokens) if max_source_prompt_tokens is not None else None
        )
        self.semantic_transport_retries = (
            int(semantic_transport_retries)
            if semantic_transport_retries is not None
            else None
        )
        self.technical_shard_target_tokens = int(technical_shard_target_tokens)
        if 0 < self.technical_shard_target_tokens < 8_000:
            # Very small technical shards are reserved for relation/candidate
            # evidence units.  Running a full topic dossier through that
            # route would either exceed the hard cap or require claim-level
            # splitting that the topic schema does not support.  Keep the
            # node as an explicit local projection until a larger synthesis
            # shard is available; no provider result is claimed in this mode.
            self.semantic_provider_synthesis_enabled = False
        self.pricing_policy = str(pricing_policy or "estimate_only_not_billing_v1")
        self.review_intent_input = dict(review_intent or {})
        self.quality_gate = quality_gate if isinstance(quality_gate, OutlineQualityGate) else OutlineQualityGate.from_mapping(quality_gate)
        self._review_intent_hash = build_review_intent(self.review_intent_input).content_hash
        self._coverage_contract_hash = self._compute_current_coverage_contract_hash()
        self.fault_injector = fault_injector
        self.cancellation_checker = cancellation_checker
        self._skip_exact_replay_verification = bool(_skip_exact_replay_verification)
        self._provider_call_count = 0
        self._transport_call_count = 0
        self.stability_preflight: dict[str, Any] = {}
        self._frozen_stability_relation_scope: dict[str, Any] | None = None
        self.provider_call_plans: tuple[OutlineProviderCallPlan, ...] = ()
        self.semantic_request_plan: list[dict[str, Any]] = []
        self.topic_request_plan_identity_hash = ""
        self.semantic_relation_candidates: list[dict[str, Any]] = []
        self.semantic_cross_group_questions: list[str] = []
        self.semantic_topic_ids: list[str] = []
        default_attempt_payload = {
            "job_id": self.job_id,
            "summary_hashes": self._summary_hashes(),
            "review_intent_hash": self._review_intent_hash,
            "coverage_contract_hash": self._coverage_contract_hash,
            "candidate_count": self.candidate_count,
            "quality_gate_hash": self.quality_gate.content_hash,
        }
        self.logical_attempt_identity = str(logical_attempt_identity or "").strip() or (
            f"outline:{hash_json(default_attempt_payload)[:32]}"
        )
        self.expected_call_graph_hash = hash_json({
            "provider_nodes": () if self.outline_pilot is not None else self._provider_node_ids(),
            "semantic_provider_nodes": (
                list(self.outline_pilot.get("selected_topic_batch_ids") or ())
                if self.outline_pilot is not None else ["topic_synthesis_provider", "cross_group_comparison_provider", "global_synthesis_provider"]
                if self.semantic_provider_synthesis_enabled
                else []
            ),
            "stability_roles": [] if self.outline_pilot is not None else ["candidate_provider_generation", "arbitration"],
            "pilot_scope_hash": hash_json(self.outline_pilot) if self.outline_pilot is not None else "",
        })
        self.closure_epoch_id = compute_closure_epoch_id(
            job_id=self.job_id,
            stage_name="outline_v3",
            logical_attempt_identity=self.logical_attempt_identity,
            expected_call_graph_hash=self.expected_call_graph_hash,
            current_input_artifact_hashes={
                "summary_set": hash_json(self.summaries),
                "review_intent": self._review_intent_hash,
                "coverage_contract": self._coverage_contract_hash,
                "quality_gate": self.quality_gate.content_hash,
            },
            provider_config_hash=self._context_profile_hash(),
            schema_version="outline-v3",
        )
        self.artifact_paths: dict[str, str] = {}
        self.artifact_records: dict[str, ArtifactRecord] = {}
        self.receipts: list[str] = []
        self.diagnostics: list[str] = []
        self.replay_diagnostics: list[str] = []
        self._payloads: dict[str, dict[str, Any]] = {}
        # The audit records only hashes, sizes, route identity, source
        # membership and durable references.  It never stores provider prompt
        # bodies or source text, so the evidence can be inspected without
        # widening the original full-text transport boundary.
        self._request_payload_audit: list[dict[str, Any]] = []
        self._audit_artifacts_persisted = False
        ledger_root = Path(
            getattr(self.workspace, "root_dir", None) or self._path("")
        ) / ".publication-staging" / "provider-receipts"
        self._receipt_ledger = ProviderRuntimeLedger.for_epoch(
            ledger_root,
            stage_name="outline_v3",
            closure_epoch_id=self.closure_epoch_id,
        )
        self.receipt_ledger_target_path = self._path("outline_v3_provider_receipts.jsonl")
        # A retry may register the same epoch under the stable ledger ID or a
        # content-addressed suffix.  Resume must hydrate the newest durable
        # ledger for this epoch, regardless of which alias was used.
        epoch_ledgers = [
            record
            for record in self.registry.list_records()
            if record.status == "ready"
            and record.artifact_type == "provider_receipt_ledger"
            and str(record.metadata.get("stage_name") or "") == "outline_v3"
            and str(record.metadata.get("closure_epoch_id") or "") == self.closure_epoch_id
        ]
        stable_ledgers = [
            record for record in epoch_ledgers
            if record.artifact_id == "outline_v3_provider_receipts"
        ]
        existing_ledger = (
            stable_ledgers[0]
            if stable_ledgers
            else max(
                epoch_ledgers,
                key=lambda record: (record.created_at, record.artifact_id),
                default=None,
            )
        )
        if existing_ledger is not None and existing_ledger.status == "ready":
            try:
                self._receipt_ledger.path.parent.mkdir(parents=True, exist_ok=True)
                published_ledger = ProviderRuntimeLedger(existing_ledger.path)
                published_receipts = list(published_ledger.list_receipts())
                staging_receipts = list(self._receipt_ledger.list_receipts())
                published_ids = {receipt.receipt_id for receipt in published_receipts}
                snapshot_time = str(existing_ledger.created_at or "")
                fresh_receipts = [
                    receipt
                    for receipt in staging_receipts
                    if receipt.receipt_id not in published_ids
                    and str(receipt.finished_at or "") > snapshot_time
                ]
                merged_by_id = {
                    receipt.receipt_id: receipt
                    for receipt in [*published_receipts, *fresh_receipts]
                }
                merged = [merged_by_id[key] for key in sorted(merged_by_id)]
                if [receipt.receipt_id for receipt in staging_receipts] != [receipt.receipt_id for receipt in merged]:
                    payload = "".join(
                        json.dumps(receipt.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
                        for receipt in merged
                    ).encode("utf-8")
                    fd, temp_name = tempfile.mkstemp(
                        prefix="outline-receipts-reconcile-",
                        suffix=".jsonl",
                        dir=str(self._receipt_ledger.path.parent),
                    )
                    os.close(fd)
                    try:
                        fsync_file(temp_name, payload)
                        atomic_replace_with_retry(temp_name, self._receipt_ledger.path)
                    finally:
                        try:
                            Path(temp_name).unlink(missing_ok=True)
                        except OSError:
                            pass
            except OSError:
                pass
        self._replay_store = ModelCallReplayStore(self.workspace)
        self._expected_provider_calls: dict[str, ExpectedProviderCall] = {}
        self._dynamic_provider_bindings: dict[str, dict[str, Any]] = {}
        self._pending_replays: dict[str, tuple[ModelCallReplayKey, str, str]] = {}
        self._replay_evidence: list[dict[str, Any]] = []
        self._replay_receipt_sources: dict[str, ArtifactRecord] = {}
        self._replay_receipt_diagnostics: list[str] = []
        self._replay_receipt_index_cache: dict[str, Any] | None = None
        self._verified_reuse_records: dict[str, ArtifactRecord] = {}
        self._verified_reuse_source_receipt_ids: dict[str, str] = {}
        self._pause_state = PauseStateStore(self.workspace, self.registry)
        self._node_store = OutlineNodeStore(self.workspace, self.registry)
        self._dag = self._node_store.ensure(self.job_id, candidate_count=self.candidate_count)
        self._hydrate_expected_provider_calls()

    def _build_registry(self) -> ArtifactRegistry:
        path = self._path("artifact_registry.json")
        return ArtifactRegistry(path, self.job_id)

    def _path(self, name: str) -> str:
        if hasattr(self.workspace, "artifact_path"):
            return str(self.workspace.artifact_path(name))
        root = Path(self.workspace).expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        return str(root / name)

    def _node_path(self, node_id: str) -> str:
        safe = node_id.replace("/", "_").replace("\\", "_").replace(":", "_")
        return self._path(f"outline_v3/artifacts/{safe}.json")

    @staticmethod
    def _request_members(request: Mapping[str, Any]) -> dict[str, list[str]]:
        """Extract bounded provenance identities without retaining prompt text."""

        paper_keys: set[str] = set()
        evidence_view_hashes: set[str] = set()
        relation_candidate_ids: set[str] = set()

        def visit(value: Any) -> None:
            if isinstance(value, Mapping):
                for field_name in ("paper_keys", "canonical_paper_keys"):
                    for item in value.get(field_name) or ():
                        text = str(item).strip()
                        if text:
                            paper_keys.add(text)
                for field_name in ("relation_candidate_ids", "relation_ids"):
                    for item in value.get(field_name) or ():
                        text = str(item).strip()
                        if text:
                            relation_candidate_ids.add(text)
                for field_name in ("view_hashes", "evidence_view_hashes", "evidence_view_hash"):
                    raw = value.get(field_name)
                    values = raw if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)) else [raw]
                    for item in values:
                        text = str(item or "").strip()
                        if text:
                            evidence_view_hashes.add(text)
                for item in value.values():
                    visit(item)
            elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
                for item in value:
                    visit(item)

        visit(request)
        for item in request.get("relation_candidates") or ():
            if not isinstance(item, Mapping):
                continue
            relation_id = str(item.get("relation_id") or "").strip()
            if relation_id:
                relation_candidate_ids.add(relation_id)
            for paper_key in item.get("paper_keys") or ():
                value = str(paper_key).strip()
                if value:
                    paper_keys.add(value)
        for item in request.get("evidence_views") or ():
            if not isinstance(item, Mapping):
                continue
            for field_name in ("paper_key", "canonical_paper_key"):
                value = str(item.get(field_name) or "").strip()
                if value:
                    paper_keys.add(value)
                    break
            for field_name in ("view_hash", "evidence_view_hash", "source_summary_hash"):
                value = str(item.get(field_name) or "").strip()
                if value:
                    evidence_view_hashes.add(value)
                    break
        hierarchy = request.get("hierarchy")
        if isinstance(hierarchy, Mapping):
            for value in hierarchy.get("paper_keys") or ():
                text = str(value).strip()
                if text:
                    paper_keys.add(text)
            for value in hierarchy.get("relation_candidate_ids") or ():
                text = str(value).strip()
                if text:
                    relation_candidate_ids.add(text)
            for value in hierarchy.get("evidence_view_hashes") or ():
                text = str(value).strip()
                if text:
                    evidence_view_hashes.add(text)
        return {
            "paper_keys": sorted(paper_keys),
            "evidence_view_hashes": sorted(evidence_view_hashes),
            "relation_candidate_ids": sorted(relation_candidate_ids),
        }

    def _begin_request_payload_audit(
        self,
        *,
        node_id: str,
        request: Mapping[str, Any],
        route: OutlineRoleRoute,
        profile: ProviderContextProfile,
        binding: Mapping[str, Any],
        api_config: Mapping[str, Any],
        call_id: str,
        semantic_node_id: str,
        transport_node_id: str | None,
        replay_key_hash: str,
        replay_status: str,
        transport: Any,
        budget: Mapping[str, Any],
        effective_input_cap: int,
        requested_output_tokens: int,
    ) -> int:
        serialized = json.dumps(request, ensure_ascii=False, sort_keys=True).encode("utf-8")
        canonical_serialized = json.dumps(
            request, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        members = self._request_members(request)
        hierarchy = request.get("hierarchy")
        hierarchy_map = hierarchy if isinstance(hierarchy, Mapping) else {}
        shard_id = str(hierarchy_map.get("shard_id") or "").strip()
        parent_node = str(hierarchy_map.get("parent_node") or "").strip()
        if not parent_node:
            if transport_node_id == "relation_adjudication" or semantic_node_id == "relation_adjudication":
                parent_node = "relation_shard_plan"
            elif node_id.endswith("_provider_generation"):
                parent_node = node_id.removesuffix("_provider_generation")
            elif node_id in {"structure_critique", "coverage_critique", "evidence_critique"}:
                parent_node = "candidate_provider_generation"
            elif node_id == "arbitration":
                parent_node = "structure_critique,coverage_critique,evidence_critique"
        record = {
            "schema_version": "outline_request_payload_audit/v1",
            "job_id": self.job_id,
            "stage_name": "outline_v3",
            "operation_id": self.logical_attempt_identity,
            "attempt_id": call_id,
            "physical_attempt_id": f"pending:{call_id}:{len(self._request_payload_audit) + 1}",
            "closure_epoch_id": self.closure_epoch_id,
            "node_id": node_id,
            "semantic_node_id": semantic_node_id,
            "parent_node": parent_node,
            "role": str(transport_node_id or semantic_node_id),
            "route": {
                "config_section": route.config_section,
                "provider": route.provider_name,
                "model": route.model,
                "endpoint_type": route.endpoint_type,
                "api_base_host": route.api_base_host,
            },
            "route_fingerprint": route.safe_config_fingerprint(),
            "config_hash": hash_json(api_config),
            "schema_hash": str(binding.get("schema_hash") or ""),
            "payload_hash": hash_json(request),
            "replay_key_hash": replay_key_hash,
            "replay_status": replay_status,
            "serialized_bytes": len(serialized),
            "canonical_attached_request_bytes": len(canonical_serialized),
            "estimated_input_tokens": int(budget.get("estimated_input_tokens") or profile.estimate_tokens(request)),
            # Retain the legacy profile caps while recording the limits used
            # for admission and the physical transport separately.
            "input_cap": int(profile.input_budget),
            "output_cap": int(profile.max_output_tokens),
            "route_profile_input_budget": int(profile.input_budget),
            "route_profile_max_output_tokens": int(profile.max_output_tokens),
            "effective_input_cap": int(effective_input_cap),
            "requested_output_tokens": int(requested_output_tokens),
            "reasoning_reserve": int(profile.reasoning_reserve),
            "mock_live": (
                "mock"
                if transport is None or route.endpoint_type in {"internal", "fixture"}
                or route.provider_name in {"fixture", "configured", "test"}
                else "live"
            ),
            "provider_invoked": False,
            "status": "pending",
            "error": "",
            "shard_ids": [shard_id] if shard_id else [],
            "paper_keys": members["paper_keys"],
            "evidence_view_hashes": members["evidence_view_hashes"],
            "relation_candidate_ids": members["relation_candidate_ids"],
            "input_artifact_hashes": sorted(str(item) for item in binding.get("dependency_hashes", {}).values()),
            "receipt_ids": [],
            "raw_response_refs": [],
            "artifact_refs": [],
        }
        self._request_payload_audit.append(record)
        return len(self._request_payload_audit) - 1

    def _finish_request_payload_audit(self, index: int, **updates: Any) -> None:
        if 0 <= index < len(self._request_payload_audit):
            self._request_payload_audit[index].update(updates)

    def _update_request_audit_artifact_ref(self, node_id: str, record: ArtifactRecord) -> None:
        for audit in self._request_payload_audit:
            if str(audit.get("node_id") or "") == node_id:
                refs = list(audit.get("artifact_refs") or [])
                reference = {
                    "artifact_id": record.artifact_id,
                    "path": record.path,
                    "content_hash": record.content_hash,
                }
                if reference not in refs:
                    refs.append(reference)
                audit["artifact_refs"] = refs

    def _persist_audit_evidence(self) -> None:
        """Publish the no-prompt-body request audit and actual call graph."""

        if self._audit_artifacts_persisted:
            return
        audit_lines = "".join(
            json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
            for item in self._request_payload_audit
        ).encode("utf-8")
        dependency_ids = [
            node_id
            for node_id in (
                "provider_call_plan",
                "relation_shard_plan",
                "relation_shard_digests",
                "provider_receipt_closure",
            )
            if node_id in self.artifact_records
        ]
        audit_record = publish_bytes_artifact(
            self.publication_context,
            self.registry,
            self._path("R1_REQUEST_PAYLOAD_AUDIT.jsonl"),
            audit_lines,
            artifact_role="outline_request_payload_audit",
            artifact_type="outline_request_payload_audit",
            artifact_version="v1",
            producer="outline.v3_executor.OutlineV3Executor",
            artifact_id=f"outline-v3:request_payload_audit:{self.closure_epoch_id}",
            depends_on=self._dependency_refs(dependency_ids),
            metadata={
                "job_id": self.job_id,
                "stage_name": "outline_v3",
                "closure_epoch_id": self.closure_epoch_id,
                "record_count": len(self._request_payload_audit),
                "contains_prompt_bodies": False,
            },
        )
        self.artifact_paths["request_payload_audit"] = audit_record.path
        self.artifact_records["request_payload_audit"] = audit_record

        dag = self._node_store.load() or self._dag
        local_nodes = [node.to_dict() for node in dag.nodes]
        edges: set[tuple[str, str, str]] = set()
        for node in dag.nodes:
            for dependency in node.depends_on:
                edges.add((str(dependency), str(node.node_id), "dag_dependency"))
        provider_nodes: list[dict[str, Any]] = []
        for index, audit in enumerate(self._request_payload_audit, start=1):
            provider_id = f"provider_call:{index}:{audit.get('node_id') or 'unknown'}"
            provider_nodes.append(
                {
                    "id": provider_id,
                    "node_id": audit.get("node_id", ""),
                    "semantic_node_id": audit.get("semantic_node_id", ""),
                    "status": audit.get("status", ""),
                    "mock_live": audit.get("mock_live", ""),
                    "provider_invoked": bool(audit.get("provider_invoked")),
                    "receipt_ids": list(audit.get("receipt_ids") or []),
                    "artifact_refs": list(audit.get("artifact_refs") or []),
                }
            )
            parent = str(audit.get("parent_node") or "").strip()
            if parent:
                edges.add((parent, provider_id, "provider_input"))
            node_id = str(audit.get("node_id") or "")
            if node_id.startswith("relation_adjudication:local:"):
                edges.add((provider_id, "relation_shard_digests", "local_digest"))
            elif node_id == "relation_adjudication:cross_shard":
                edges.add((provider_id, "relation_adjudication", "cross_shard_merge"))
            elif node_id:
                edges.add((provider_id, node_id, "provider_output"))
        coverage = {
            "paper_keys": sorted({
                value
                for item in self._request_payload_audit
                for value in item.get("paper_keys") or ()
                if str(value)
            }),
            "evidence_view_hashes": sorted({
                value
                for item in self._request_payload_audit
                for value in item.get("evidence_view_hashes") or ()
                if str(value)
            }),
            "relation_candidate_ids": sorted({
                value
                for item in self._request_payload_audit
                for value in item.get("relation_candidate_ids") or ()
                if str(value)
            }),
            "shard_ids": sorted({
                value
                for item in self._request_payload_audit
                for value in item.get("shard_ids") or ()
                if str(value)
            }),
        }
        graph_base = {
            "schema_version": "outline_hierarchical_call_graph/v1",
            "job_id": self.job_id,
            "stage_name": "outline_v3",
            "operation_id": self.logical_attempt_identity,
            "closure_epoch_id": self.closure_epoch_id,
            "expected_call_graph_hash": self.expected_call_graph_hash,
            "request_payload_audit_artifact_id": audit_record.artifact_id,
            "local_dag_nodes": local_nodes,
            "provider_calls": provider_nodes,
            "edges": [
                {"from": source, "to": target, "kind": kind}
                for source, target, kind in sorted(edges)
            ],
            "coverage": coverage,
        }
        graph_payload = {
            **graph_base,
            "graph_hash": compute_v3_hash(graph_base),
        }
        graph_record = publish_json_artifact(
            self.publication_context,
            self.registry,
            self._path("R1_HIERARCHICAL_CALL_GRAPH.json"),
            graph_payload,
            artifact_role="outline_hierarchical_call_graph",
            artifact_type="outline_hierarchical_call_graph",
            artifact_version="v1",
            producer="outline.v3_executor.OutlineV3Executor",
            artifact_id=f"outline-v3:hierarchical_call_graph:{self.closure_epoch_id}",
            depends_on=self._dependency_refs([*dependency_ids, "request_payload_audit"]),
            metadata={
                "job_id": self.job_id,
                "stage_name": "outline_v3",
                "closure_epoch_id": self.closure_epoch_id,
            },
        )
        self.artifact_paths["hierarchical_call_graph"] = graph_record.path
        self.artifact_records["hierarchical_call_graph"] = graph_record
        self._audit_artifacts_persisted = True

    def _provider_node_ids(self) -> tuple[str, ...]:
        roles = self.enabled_semantic_roles
        node_ids: list[str] = []
        if roles is None or "relation_adjudication" in roles:
            node_ids.append("relation_adjudication")
        if roles is None or "candidate_provider_generation" in roles:
            node_ids.extend(
                f"candidate_{index}_provider_generation"
                for index in range(1, self.candidate_count + 1)
            )
        for role in ("structure_critique", "coverage_critique", "evidence_critique"):
            if roles is None or role in roles:
                node_ids.append(role)
        if roles is None or "arbitration" in roles:
            node_ids.append("arbitration")
        return tuple(node_ids)

    def _role_route(self, node_id: str) -> OutlineRoleRoute:
        """Resolve the provider route that must serve one concrete node.

        With a role router configured this is authoritative and fail-closed.
        Without one, the pre-existing single-provider behaviour is preserved so
        existing single-model callers are unaffected.
        """

        if self.router is not None:
            return self.router.route_for(node_id)
        return OutlineRoleRoute(
            role=semantic_role(node_id),
            config_section="",
            provider_name=self.profile.provider,
            model=self.profile.model,
            endpoint_type=self.profile.endpoint_type,
            profile=self.profile,
            transport=self.provider if callable(self.provider) else None,
        )

    def _resolve_node_transport(self, node_id: str, route: OutlineRoleRoute) -> Any:
        """Return the transport that must execute one node.

        When a role router is configured it is authoritative. Falling back to the
        executor's single provider here would silently collapse every node onto
        the Outline model -- the exact defect role routing exists to prevent --
        so a routed node without a transport fails closed instead.

        Without a router the pre-existing single-provider behaviour is kept, so
        older single-model callers are unaffected.
        """

        if self.router is not None:
            if route.transport is None:
                raise OutlineV3ExecutionError(
                    f"no transport configured for routed node {node_id} "
                    f"(role {route.role!r}, section {route.config_section!r}); "
                    "refusing to fall back to the Outline provider"
                )
            return route.transport
        return self.provider

    def _node_route(self, node_id: str, transport_node_id: str | None = None) -> OutlineRoleRoute:
        """Resolve the route that must execute one concrete node.

        A stability node carries its own durable audit identity
        (``stability:<audit>:<base>``) but must still execute on the *base*
        node's provider route, otherwise a stability audit would fall back to
        whatever provider the executor was constructed with. The stability
        prefix is therefore stripped before the semantic role is resolved.
        """

        effective = str(transport_node_id or node_id)
        if effective.startswith("stability:"):
            effective = effective.rsplit(":", 1)[-1]
        return self._role_route(effective)

    @staticmethod
    def _profile_identity(profile: ProviderContextProfile) -> dict[str, Any]:
        return {
            "provider": profile.provider,
            "model": profile.model,
            "endpoint_type": profile.endpoint_type,
            "model_context_limit": profile.model_context_limit,
            "verified_context_limit": profile.verified_context_limit,
            "input_budget": profile.input_budget,
            "max_output_tokens": profile.max_output_tokens,
            "reasoning_reserve": profile.reasoning_reserve,
            "safety_margin": profile.safety_margin,
            "tokenizer_strategy": profile.tokenizer_strategy,
        }

    @staticmethod
    def _route_api_identity(route: OutlineRoleRoute) -> dict[str, str]:
        return {
            "provider_family": route.provider_name,
            "model": route.model,
            "api_base": route.api_base,
            "api_base_host": route.api_base_host,
            "endpoint_type": route.endpoint_type,
            "config_section": route.config_section,
            "route_fingerprint": route.safe_config_fingerprint(),
        }

    @staticmethod
    def _route_transport_identity(route: OutlineRoleRoute) -> dict[str, str]:
        """Return the exact secret-free config identity used by transport receipts.

        The wider route identity above is useful for stage manifests, but the
        provider receipt, node binding, replay key, and resume hydration must
        hash one byte-for-byte equivalent mapping.  Keeping this narrower
        helper in one place prevents a binding from becoming stale merely
        because one caller included an informational field such as
        ``api_base_host``.
        """

        identity = {
            "provider_family": route.provider_name,
            "model": route.model,
            "api_base": route.api_base_host,
            "endpoint_type": route.endpoint_type,
            "config_section": route.config_section,
            "route_fingerprint": route.safe_config_fingerprint(),
        }
        identity["transport_retries"] = str(route.config_identity.get("transport_retries") or "0")
        return identity

    def _provider_configured(self) -> bool:
        """Return whether the configured execution surface can transport calls."""

        if self.router is None:
            return self.provider is not None
        try:
            return all(
                self.router.route_for(node_id).transport is not None
                for node_id in self._provider_node_ids()
            )
        except (KeyError, TypeError, AttributeError):
            return False

    def _compute_current_coverage_contract_hash(self) -> str:
        try:
            evidence = build_outline_evidence_views(self.summaries, self.job_id)
            ledger = build_global_corpus_ledger(evidence)
            return build_coverage_contract(ledger, build_review_intent(self.review_intent_input)).content_hash
        except (TypeError, ValueError, KeyError):
            return ""

    def _summary_hashes(self) -> list[str]:
        return sorted(_hash_payload(item) for item in self.summaries)

    @staticmethod
    def _prompt_evidence_views(views: Sequence[Any]) -> list[dict[str, Any]]:
        """Project complete evidence views without silent semantic truncation.

        Size control belongs to the token-aware shard planner.  This helper is
        used by non-sharded nodes as well, so it must not erase the eleventh
        finding or the tail of a long evidence item merely because a legacy
        character heuristic happened to be reached.  The local evidence
        artifact remains the source of truth and this projection carries the
        same identity fields and all structured evidence values.
        """

        prompt_views: list[dict[str, Any]] = []
        for view in views:
            payload = view.to_dict() if hasattr(view, "to_dict") else dict(view)
            compact = dict(payload)
            # Raw source-field provenance can contain parser internals and is
            # not needed by the cross-paper Outline roles.  All semantic
            # evidence fields above it remain complete.
            compact["source_fields"] = {}
            # The full derived ledger can include attachment paths and fields
            # unrelated to this call. Provider requests receive only selected
            # interpretation dependencies through the scoped topic/section
            # packets, while the complete ledger stays in local artifacts.
            compact["source_field_ledger"] = []
            prompt_views.append(compact)
        return prompt_views

    @staticmethod
    def _prompt_evidence_chunks(view: Any) -> list[dict[str, Any]]:
        """Split one evidence view into lossless, source-identifiable chunks."""

        payload = view.to_dict() if hasattr(view, "to_dict") else dict(view)
        identity_fields = {
            "paper_key",
            "canonical_paper_key",
            "title",
            "authors",
            "year",
            "paper_type",
            "source_summary_hash",
            "doi",
            "source_paper_id",
            "aliases",
            "identity_source",
            "source_summary_hashes",
            "classification",
            "must_use",
        }
        semantic_fields = (
            "research_questions",
            "theories",
            "constructs",
            "mechanisms",
            "method",
            "sample_or_context",
            "findings",
            "conclusions",
            "limitations",
            "research_gaps",
            "future_directions",
            "relevance",
            "diagnostics",
        )
        base = {key: payload.get(key) for key in identity_fields if key in payload}
        base["source_fields"] = {}
        source_hash = str(payload.get("view_hash") or "")
        if not source_hash:
            # ``view_hash`` is not part of every serialized view; preserve the
            # canonical source identity when the richer object is available.
            source_hash = str(getattr(view, "view_hash", "") or payload.get("source_summary_hash") or "")

        def split_text(value: Any) -> list[str]:
            text = str(value or "")
            if not text.strip():
                return []
            # 1200 is a chunk size, not a truncation limit.  Every subsequent
            # piece is retained and carries the same source view identity.
            return [text[offset : offset + 1200] for offset in range(0, len(text), 1200)]

        needs_split = False
        for field_name in semantic_fields:
            raw_values = payload.get(field_name) or []
            values = raw_values if isinstance(raw_values, list) else [raw_values]
            if len(values) > 10 or any(len(str(value or "")) > 1200 for value in values):
                needs_split = True
                break
        if not needs_split:
            chunk = dict(OutlineV3Executor._prompt_evidence_views([view])[0])
            chunk["evidence_source_view_hash"] = source_hash
            chunk["evidence_field"] = ""
            chunk["evidence_value_index"] = 0
            chunk["evidence_text_chunk_index"] = 0
            chunks = [chunk]
            chunk["evidence_chunk_id"] = f"{source_hash or chunk.get('paper_key') or 'unknown'}:1"
            chunk["evidence_chunk_index"] = 0
            chunk["evidence_chunk_count"] = 1
            return chunks

        chunks: list[dict[str, Any]] = []
        for field_name in semantic_fields:
            raw_values = payload.get(field_name) or []
            values = raw_values if isinstance(raw_values, list) else [raw_values]
            for value_index, value in enumerate(values):
                for text_index, text in enumerate(split_text(value)):
                    chunk = dict(base)
                    for semantic_field in semantic_fields:
                        chunk[semantic_field] = []
                    chunk[field_name] = [text]
                    chunk["evidence_source_view_hash"] = source_hash
                    chunk["evidence_field"] = field_name
                    chunk["evidence_value_index"] = value_index
                    chunk["evidence_text_chunk_index"] = text_index
                    chunks.append(chunk)
        if not chunks:
            chunk = dict(base)
            for semantic_field in semantic_fields:
                chunk[semantic_field] = []
            chunk["evidence_source_view_hash"] = source_hash
            chunk["evidence_field"] = ""
            chunk["evidence_value_index"] = 0
            chunk["evidence_text_chunk_index"] = 0
            chunks.append(chunk)
        total = len(chunks)
        for index, chunk in enumerate(chunks):
            chunk["evidence_chunk_id"] = f"{source_hash or chunk.get('paper_key') or 'unknown'}:{index + 1}"
            chunk["evidence_chunk_index"] = index
            chunk["evidence_chunk_count"] = total
        return chunks

    @staticmethod
    def _compact_topic_route(topic: Any) -> dict[str, Any]:
        """Return the routing portion of one topic without its ID list.

        The complete evidence IDs remain in the content-layer artifact.  The
        provider receives their count and per-unit hashes below, which keeps a
        large topic request bounded without deleting a fact or silently
        slicing a source field.
        """

        return {
            "topic_id": str(getattr(topic, "topic_id", "") or ""),
            "question": str(getattr(topic, "question", "") or ""),
            "paper_ids": [str(item) for item in getattr(topic, "paper_ids", []) if str(item)],
            "bridge_paper_ids": [
                str(item) for item in getattr(topic, "bridge_paper_ids", []) if str(item)
            ],
            "dimensions": [str(item) for item in getattr(topic, "dimensions", []) if str(item)],
            "comparison_questions": [
                str(item)
                for item in getattr(topic, "comparison_questions", [])
                if str(item).strip()
            ],
            "required_evidence_count": len(getattr(topic, "required_evidence_ids", []) or []),
            "status": str(getattr(topic, "status", "") or ""),
        }

    @staticmethod
    def _topic_projection_fields(dimensions: Sequence[str]) -> tuple[str, ...]:
        """Choose complete semantic fields needed by a topic route.

        This is a semantic projection, not character truncation.  Omitted
        fields are declared with hashes/counts and remain available in the
        content-layer dossier referenced by the request.
        """

        fields: set[str] = set()
        for dimension in dimensions:
            normalized = str(dimension or "").strip().casefold()
            if normalized == "context":
                fields.update(
                    (
                        "research_questions",
                        "theories",
                        "constructs",
                        "mechanisms",
                        "method",
                        "sample_or_context",
                        "findings",
                        "zero_results",
                        "conclusions",
                        "limitations",
                        "research_gaps",
                        "future_directions",
                    )
                )
            elif normalized == "method":
                fields.update(
                    (
                        "research_questions",
                        "constructs",
                        "method",
                        "sample_or_context",
                        "findings",
                        "zero_results",
                        "conclusions",
                        "limitations",
                    )
                )
            elif normalized == "theory":
                fields.update(
                    (
                        "theories",
                        "constructs",
                        "mechanisms",
                        "method",
                        "sample_or_context",
                        "findings",
                        "zero_results",
                        "conclusions",
                        "limitations",
                    )
                )
            elif normalized == "mechanism":
                fields.update(
                    (
                        "mechanisms",
                        "constructs",
                        "method",
                        "sample_or_context",
                        "findings",
                        "zero_results",
                        "conclusions",
                        "limitations",
                    )
                )
            else:
                fields.update(
                    (
                        "research_questions",
                        "theories",
                        "constructs",
                        "mechanisms",
                        "method",
                        "sample_or_context",
                        "findings",
                        "zero_results",
                        "conclusions",
                        "limitations",
                        "research_gaps",
                        "future_directions",
                    )
                )
        # A finding without its boundary text can change meaning when this
        # topic is packed separately from another route for the same paper.
        # The typed source-field ledger uses this canonical field name.
        if "findings" in fields:
            fields.add("moderators_boundaries")
        return tuple(sorted(fields))

    @classmethod
    def _compact_topic_evidence_unit(
        cls,
        view: Any,
        dossier: Any | None,
        *,
        fields: Sequence[str],
        include_field_refs: bool = True,
    ) -> dict[str, Any]:
        payload = view.to_dict() if hasattr(view, "to_dict") else dict(view)
        dossier_payload = dossier.to_dict() if dossier is not None and hasattr(dossier, "to_dict") else {}
        raw_evidence_ids = dossier_payload.get("evidence_ids_by_field")
        evidence_ids_by_field = (
            raw_evidence_ids if isinstance(raw_evidence_ids, Mapping) else {}
        )
        evidence_refs = {
            str(field_name): {
                "count": len(values) if isinstance(values, list) else 0,
                "ids_hash": hash_json(values if isinstance(values, list) else []),
            }
            for field_name, values in evidence_ids_by_field.items()
        }
        available_fields = {
            str(field_name): {
                "value_count": len(value) if isinstance(value, list) else int(bool(value)),
                "value_hash": hash_json(value),
            }
            for field_name, value in payload.items()
            if field_name
            in {
                "research_questions",
                "theories",
                "constructs",
                "mechanisms",
                "method",
                "sample_or_context",
                "findings",
                "conclusions",
                "limitations",
                "research_gaps",
                "future_directions",
            }
        }
        return {
            "paper_key": str(payload.get("paper_key") or payload.get("canonical_paper_key") or ""),
            "source_summary_hash": str(payload.get("source_summary_hash") or ""),
            "evidence_unit_id": str(
                getattr(dossier, "dossier_id", "")
                or f"dossier:{payload.get('paper_key') or payload.get('canonical_paper_key') or ''}"
            ),
            "evidence_unit_hash": str(getattr(dossier, "content_hash", "") or ""),
            "evidence_fields": {
                str(field_name): payload.get(field_name)
                for field_name in fields
                if field_name in payload
            },
            "evidence_field_refs": (
                {
                    "field_names": sorted(evidence_refs),
                    "refs_hash": hash_json(evidence_refs),
                }
                if include_field_refs
                else {"refs_hash": hash_json(evidence_refs)}
            ),
            "available_field_refs": (
                {
                    "field_names": sorted(available_fields),
                    "refs_hash": hash_json(available_fields),
                }
                if include_field_refs
                else {"refs_hash": hash_json(available_fields)}
            ),
            "projection": "complete_field_values_plus_registry_refs_v1",
        }

    @classmethod
    def _complete_topic_evidence_unit(
        cls,
        view: Any,
        dossier: Any | None,
        *,
        fields: Sequence[str],
    ) -> dict[str, Any]:
        """Materialize one complete provider-visible evidence unit.

        This is intentionally separate from the routing projection used by
        candidate/critique metadata. A semantic provider receives the actual
        dossier bytes: study units, claim modality, evidence IDs, source
        locators, qualifiers and null findings. If these complete units do
        not fit the authorized budget, admission stops before transport.
        """

        payload = view.to_dict() if hasattr(view, "to_dict") else dict(view)
        if dossier is None or not hasattr(dossier, "to_dict"):
            return cls._compact_topic_evidence_unit(
                view,
                dossier,
                fields=fields,
                include_field_refs=True,
            )
        dossier_payload = dict(dossier.to_dict())
        nested_claim_ids = {
            str(claim.get("claim_id") or "")
            for unit in dossier_payload.get("research_units") or ()
            if isinstance(unit, Mapping)
            for claim in unit.get("claims") or ()
            if isinstance(claim, Mapping) and str(claim.get("claim_id") or "")
        }
        declared_source_paths = payload.get("source_fields") or {}
        source_field_ids_by_field = dossier_payload.get("evidence_ids_by_field") or {}
        source_text_by_id = dossier_payload.get("evidence_text_by_id") or {}
        projected_source_fields: dict[str, dict[str, Any]] = {}
        for raw_entry in dossier_payload.get("source_field_ledger") or ():
            try:
                entry = (
                    raw_entry
                    if isinstance(raw_entry, SourceFieldLedgerEntry)
                    else SourceFieldLedgerEntry.from_dict(raw_entry)
                )
            except (TypeError, ValueError) as exc:
                raise OutlineV3ExecutionError(
                    "evidence dossier contains an invalid source-field ledger entry"
                ) from exc
            source_path = entry.source_path.strip()
            path_candidates = {source_path}
            if source_path and not source_path.startswith("ai_summary."):
                path_candidates.add(f"ai_summary.{source_path}")
            elif source_path.startswith("ai_summary."):
                path_candidates.add(source_path[len("ai_summary.") :])
            projected_fields = {
                str(field_name)
                for field_name in fields
                if str(field_name)
                and (
                    entry.canonical_field == str(field_name)
                    or any(
                        candidate == str(root)
                        or candidate.startswith(str(root) + ".")
                        or candidate.startswith(str(root) + "[")
                        for candidate in path_candidates
                        for root in (
                            declared_source_paths.get(str(field_name), ())
                            if isinstance(declared_source_paths, Mapping)
                            else ()
                        )
                    )
                )
            }
            if not projected_fields:
                continue
            evidence_refs = sorted(
                {
                    str(evidence_id)
                    for field_name in projected_fields
                    for evidence_field, evidence_ids in source_field_ids_by_field.items()
                    if evidence_field == field_name or evidence_field.endswith(f":{field_name}")
                    for evidence_id in evidence_ids or ()
                    if str(source_text_by_id.get(str(evidence_id)) or "") == entry.source_value
                }
            )
            row = entry.to_dict()
            row["projected_fields"] = sorted(projected_fields)
            row["evidence_ids"] = evidence_refs
            projected_source_fields[entry.source_field_id] = row
        return {
            "paper_key": str(payload.get("paper_key") or payload.get("canonical_paper_key") or ""),
            "source_summary_hash": str(payload.get("source_summary_hash") or ""),
            "evidence_unit_id": str(getattr(dossier, "dossier_id", "") or ""),
            "evidence_unit_hash": str(getattr(dossier, "content_hash", "") or ""),
            "study_units": list(dossier_payload.get("research_units") or []),
            "claims": [
                dict(claim)
                for claim in dossier_payload.get("claims") or ()
                if isinstance(claim, Mapping)
                and str(claim.get("claim_id") or "") not in nested_claim_ids
            ],
            "evidence_ids_by_field": dict(dossier_payload.get("evidence_ids_by_field") or {}),
            "evidence_text_by_id": dict(dossier_payload.get("evidence_text_by_id") or {}),
            "source_locators": dict(dossier_payload.get("source_locators") or {}),
            "source_field_ledger": [
                projected_source_fields[key] for key in sorted(projected_source_fields)
            ],
            "semantic_fields": {
                str(field_name): payload.get(field_name)
                for field_name in fields
                if field_name in payload
            },
            "dossier_status": str(dossier_payload.get("status") or ""),
            "dossier_diagnostics": list(dossier_payload.get("diagnostics") or []),
            "projection": "complete_dossier_study_claim_source_fields_unit_v2",
        }

    @classmethod
    def _complete_topic_evidence_units(
        cls,
        view: Any,
        dossier: Any | None,
        *,
        fields: Sequence[str],
        chunk_indexes: Sequence[int] | None = None,
        chunk_target_tokens: int | None = None,
        required_evidence_ids: Sequence[str] | None = None,
        include_unbound_claims: bool = False,
        token_estimator: Callable[[Any], int] | None = None,
    ) -> list[dict[str, Any]]:
        """Materialize scoped source units, splitting only to fit a real request.

        Paper-level claims are a distinct unit from study-level claims.  A
        split study fragment carries its complete claims, the shared method /
        context needed to interpret them, and only their bound evidence text.
        Unmapped source fields remain in one explicit evidence unit instead of
        being copied into every fragment.
        """

        if dossier is None or not hasattr(dossier, "to_dict"):
            raise OutlineV3ExecutionError(
                "semantic topic request is missing its Registry-backed evidence dossier"
            )
        complete = cls._complete_topic_evidence_unit(view, dossier, fields=fields)
        complete_source_fields = list(complete.get("source_field_ledger") or [])
        dossier_payload = dossier.to_dict()
        view_payload = view.to_dict() if hasattr(view, "to_dict") else dict(view)
        text_by_id = {
            str(key): value
            for key, value in (dossier_payload.get("evidence_text_by_id") or {}).items()
        }
        ids_by_field = {
            str(key): [str(value) for value in values if str(value)]
            for key, values in (dossier_payload.get("evidence_ids_by_field") or {}).items()
            if isinstance(values, Sequence) and not isinstance(values, (str, bytes))
        }
        source_locators = {
            str(key): list(values)
            for key, values in (dossier_payload.get("source_locators") or {}).items()
            if isinstance(values, Sequence) and not isinstance(values, (str, bytes))
        }
        raw_units: list[dict[str, Any]] = []
        for unit in dossier_payload.get("research_units") or []:
            if isinstance(unit, Mapping):
                raw_units.append(dict(unit))
            elif hasattr(unit, "to_dict"):
                unit_payload = unit.to_dict()
                if isinstance(unit_payload, Mapping):
                    raw_units.append(dict(unit_payload))
        raw_units.sort(key=lambda item: str(item.get("study_id") or ""))
        source_field_ledger: dict[str, SourceFieldLedgerEntry] = {}
        for raw_entry in dossier_payload.get("source_field_ledger") or ():
            try:
                entry = (
                    raw_entry
                    if isinstance(raw_entry, SourceFieldLedgerEntry)
                    else SourceFieldLedgerEntry.from_dict(raw_entry)
                )
            except (TypeError, ValueError) as exc:
                raise OutlineV3ExecutionError(
                    "evidence dossier contains an invalid source-field ledger entry"
                ) from exc
            prior = source_field_ledger.get(entry.source_field_id)
            if prior is not None and prior.to_dict() != entry.to_dict():
                raise OutlineV3ExecutionError(
                    "source-field identity has conflicting contents"
                )
            source_field_ledger[entry.source_field_id] = entry

        def claim_evidence_ids(claims: Sequence[Mapping[str, Any]]) -> set[str]:
            selected: set[str] = set()
            for claim in claims:
                selected.update(
                    str(value) for value in claim.get("evidence_ids") or () if str(value)
                )
                for key in ("evidence_id", "id"):
                    value = str(claim.get(key) or "")
                    if value:
                        selected.add(value)
            return selected

        required_ids = (
            {str(value) for value in required_evidence_ids if str(value)}
            if required_evidence_ids is not None
            else None
        )
        all_available_evidence_ids = {
            str(value)
            for values in ids_by_field.values()
            for value in values
            if str(value)
        }
        all_available_evidence_ids.update(text_by_id)
        all_available_evidence_ids.update(
            str(value)
            for unit in raw_units
            for value in unit.get("evidence_ids") or ()
            if str(value)
        )
        all_available_evidence_ids.update(
            evidence_id
            for claims in (
                dossier_payload.get("claims") or (),
                *[unit.get("claims") or () for unit in raw_units],
            )
            for claim in claims
            if isinstance(claim, Mapping)
            for evidence_id in claim_evidence_ids([claim])
        )
        if required_ids is not None:
            missing_required_ids = sorted(required_ids - all_available_evidence_ids)
            if missing_required_ids:
                raise OutlineV3ExecutionError(
                    "semantic topic plan requests evidence IDs absent from its dossier: "
                    + ", ".join(missing_required_ids[:12])
                )

        units_by_study = {
            str(item.get("study_id") or ""): item
            for item in raw_units
            if str(item.get("study_id") or "")
        }
        claims_by_study: dict[str, list[dict[str, Any]]] = {}
        nested_claim_by_id: dict[str, tuple[str, str]] = {}
        for study_id, item in units_by_study.items():
            claims_by_study[study_id] = []
            for raw_claim in item.get("claims") or ():
                if not isinstance(raw_claim, Mapping):
                    continue
                claim = dict(raw_claim)
                claim_id = str(claim.get("claim_id") or "")
                if not claim_id:
                    raise OutlineV3ExecutionError(
                        f"evidence dossier study {study_id} contains a claim without claim_id"
                    )
                claim_hash = hash_json(claim)
                prior = nested_claim_by_id.get(claim_id)
                if prior is not None:
                    if prior != (study_id, claim_hash):
                        raise OutlineV3ExecutionError(
                            f"source claim identity has conflicting study or content: {claim_id}"
                        )
                    continue
                nested_claim_by_id[claim_id] = (study_id, claim_hash)
                claims_by_study[study_id].append(claim)
        claim_owner: dict[str, str] = {}
        for study_id, claims in claims_by_study.items():
            for claim in claims:
                claim_id = str(claim.get("claim_id") or "")
                if not claim_id:
                    raise OutlineV3ExecutionError("evidence dossier contains a claim without claim_id")
                if claim_id in claim_owner and claim_owner[claim_id] != study_id:
                    raise OutlineV3ExecutionError(
                        f"source claim {claim_id} is ambiguously assigned to multiple studies"
                    )
                claim_owner[claim_id] = study_id

        paper_claims: list[dict[str, Any]] = []
        for raw_claim in dossier_payload.get("claims") or ():
            if not isinstance(raw_claim, Mapping):
                continue
            claim = dict(raw_claim)
            claim_id = str(claim.get("claim_id") or "")
            study_id = str(claim.get("study_id") or "")
            if claim_id and claim_id in claim_owner:
                nested = next(
                    (
                        nested_claim
                        for nested_claim in claims_by_study.get(claim_owner[claim_id], [])
                        if str(nested_claim.get("claim_id") or "") == claim_id
                    ),
                    None,
                )
                if nested is None or hash_json(nested) != hash_json(claim):
                    raise OutlineV3ExecutionError(
                        f"paper/study claim identity has conflicting content: {claim_id}"
                    )
                continue
            if study_id:
                if study_id not in claims_by_study:
                    raise OutlineV3ExecutionError(
                        f"source claim {claim_id or '(missing id)'} references unknown study {study_id}"
                    )
                if not claim_id:
                    raise OutlineV3ExecutionError("evidence dossier contains a claim without claim_id")
                claims_by_study[study_id].append(claim)
                claim_owner[claim_id] = study_id
            else:
                if not claim_id:
                    raise OutlineV3ExecutionError("paper-level source claim is missing claim_id")
                bound_ids = claim_evidence_ids([claim])
                if (
                    required_ids is None
                    or bound_ids.intersection(required_ids)
                    or (not bound_ids and include_unbound_claims)
                ):
                    paper_claims.append(claim)

        def estimate_tokens(payload: Mapping[str, Any]) -> int:
            if token_estimator is not None:
                return max(1, int(token_estimator(payload)))
            serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)
            return max(1, (len(serialized.encode("utf-8")) + 3) // 4)

        def split_claims(
            claims: Sequence[dict[str, Any]],
            *,
            context: Mapping[str, Any],
            all_claim_evidence_ids: set[str],
            atomic_groups: Sequence[Sequence[dict[str, Any]]] | None = None,
        ) -> list[list[dict[str, Any]]]:
            if not claims:
                return [[]]
            if not chunk_target_tokens or len(claims) == 1:
                return [list(claims)]
            # Evidence without claim bindings cannot safely be assigned to a
            # fragment. Keep this study indivisible and let the exact request
            # preflight block it if the complete unit is too large.
            if any(not claim_evidence_ids([claim]) for claim in claims):
                return [list(claims)]
            result: list[list[dict[str, Any]]] = []
            current: list[dict[str, Any]] = []
            groups = atomic_groups or [[claim] for claim in claims]
            for group in groups:
                trial = [*current, *group]
                trial_ids = claim_evidence_ids(trial)
                payload = {
                    "shared_context": context,
                    "claims": trial,
                    "evidence_text_by_id": {
                        key: value for key, value in text_by_id.items() if key in trial_ids
                    },
                }
                if current and estimate_tokens(payload) > chunk_target_tokens:
                    result.append(current)
                    current = list(group)
                else:
                    current = trial
            if current:
                result.append(current)
            return result

        def selected_field_ids(evidence_ids: set[str]) -> dict[str, list[str]]:
            return {
                field_name: [value for value in values if value in evidence_ids]
                for field_name, values in ids_by_field.items()
                if any(value in evidence_ids for value in values)
            }

        def selected_evidence_text(
            evidence_ids: set[str],
            claims: Sequence[Mapping[str, Any]],
        ) -> dict[str, Any]:
            claim_texts = {
                str(claim.get("text") or "").strip().casefold()
                for claim in claims
                if str(claim.get("text") or "").strip()
            }
            return {
                key: value
                for key, value in text_by_id.items()
                if key in evidence_ids
                and str(value or "").strip().casefold() not in claim_texts
            }

        def without_duplicate_source_text(
            value: Any,
            duplicate_texts: set[str],
        ) -> Any:
            if isinstance(value, str):
                if value.strip().casefold() in duplicate_texts:
                    return None
                return value
            if isinstance(value, Mapping):
                return {
                    key: cleaned
                    for key, child in value.items()
                    if (cleaned := without_duplicate_source_text(child, duplicate_texts))
                    not in (None, "", [], {})
                }
            if isinstance(value, list):
                return [
                    cleaned
                    for child in value
                    if (cleaned := without_duplicate_source_text(child, duplicate_texts))
                    not in (None, "", [], {})
                ]
            return value

        chunks: list[dict[str, Any]] = []
        all_study_evidence_ids: set[str] = set()
        all_study_claim_evidence_ids: set[str] = set()
        for unit in raw_units:
            study_id = str(unit.get("study_id") or "")
            if not study_id:
                raise OutlineV3ExecutionError("evidence dossier contains a study without study_id")
            unit_fields = derive_unit_source_field_ledger(
                unit, source_field_ledger.values()
            )
            fields_by_id = {entry.source_field_id: entry for entry in unit_fields}
            try:
                declared_dependencies = [
                    item if isinstance(item, InterpretationDependency)
                    else InterpretationDependency.from_dict(item)
                    for item in unit.get("interpretation_dependencies") or ()
                ]
            except (TypeError, ValueError) as exc:
                raise OutlineV3ExecutionError(
                    f"study {study_id} contains an invalid interpretation dependency"
                ) from exc
            # Re-derive from the typed source so an older or incomplete stored
            # dependency cannot suppress a qualifying raw field.
            dependencies = {
                hash_json(item.to_dict()): item
                for item in (
                    *declared_dependencies,
                    *derive_interpretation_dependencies(unit, unit_fields),
                )
            }
            all_study_claims = claims_by_study.get(study_id, [])
            claim_by_id = {
                str(claim.get("claim_id") or ""): claim
                for claim in all_study_claims
            }
            source_study_id = str(unit.get("source_study_id") or "")
            for dependency in dependencies.values():
                if dependency.primary_claim_id not in claim_by_id:
                    raise OutlineV3ExecutionError(
                        f"study {study_id} has an interpretation dependency without its primary claim"
                    )
                if dependency.scope == "explicit_study" and dependency.study_id != study_id:
                    raise OutlineV3ExecutionError(
                        f"study {study_id} has a cross-study interpretation dependency"
                    )
                if any(claim_id not in claim_by_id for claim_id in dependency.required_source_claim_ids):
                    raise OutlineV3ExecutionError(
                        f"study {study_id} has an interpretation dependency without its qualifier claim"
                    )
                available_ids = claim_evidence_ids(all_study_claims) | {
                    str(value) for value in unit.get("evidence_ids") or () if str(value)
                }
                if not set(dependency.required_evidence_ids).issubset(available_ids):
                    raise OutlineV3ExecutionError(
                        f"study {study_id} has an interpretation dependency without its qualifier evidence"
                    )
                for field_id in dependency.required_source_field_ids:
                    entry = fields_by_id.get(field_id)
                    if entry is None or not entry.source_value.strip():
                        raise OutlineV3ExecutionError(
                            f"study {study_id} has an interpretation dependency without its source text"
                        )
                    if dependency.scope == "explicit_study" and (
                        entry.scope != "explicit_study"
                        or entry.study_id != source_study_id
                    ):
                        raise OutlineV3ExecutionError(
                            f"study {study_id} has a cross-scope interpretation source field"
                        )
            all_study_claim_evidence_ids.update(
                claim_evidence_ids(claims_by_study.get(study_id, []))
            )
            claims = [
                claim
                for claim in claims_by_study.get(study_id, [])
                if required_ids is None
                or claim_evidence_ids([claim]).intersection(required_ids)
                or (not claim_evidence_ids([claim]) and include_unbound_claims)
            ]
            active_dependencies: dict[str, InterpretationDependency] = {}
            effective_required_ids = set(required_ids or ())
            while True:
                selected_claim_ids_now = {
                    str(claim.get("claim_id") or "") for claim in claims
                }
                new_dependencies = [
                    dependency for dependency in dependencies.values()
                    if dependency.primary_claim_id in selected_claim_ids_now
                    and hash_json(dependency.to_dict()) not in active_dependencies
                ]
                if not new_dependencies:
                    break
                for dependency in new_dependencies:
                    active_dependencies[hash_json(dependency.to_dict())] = dependency
                    effective_required_ids.update(dependency.required_evidence_ids)
                    for qualifier_id in dependency.required_source_claim_ids:
                        qualifier = claim_by_id[qualifier_id]
                        effective_required_ids.update(claim_evidence_ids([qualifier]))
                        if qualifier_id not in selected_claim_ids_now:
                            claims.append(qualifier)
                claims = [
                    claim for claim in all_study_claims
                    if str(claim.get("claim_id") or "") in {
                        str(item.get("claim_id") or "") for item in claims
                    }
                ]
            source_claim_ids = {
                str(claim.get("claim_id") or "")
                for claim in claims_by_study.get(study_id, [])
                if str(claim.get("claim_id") or "")
            }
            selected_claim_ids = {
                str(claim.get("claim_id") or "")
                for claim in claims
                if str(claim.get("claim_id") or "")
            }
            unit_evidence_ids = {
                str(value) for value in unit.get("evidence_ids") or () if str(value)
            }
            source_study_evidence_ids = set(unit_evidence_ids)
            source_study_evidence_ids.update(
                claim_evidence_ids(claims_by_study.get(study_id, []))
            )
            source_study_evidence_ids.update(
                str(value)
                for field_name, values in ids_by_field.items()
                if field_name.startswith(f"{study_id}:")
                for value in values
                if str(value)
            )
            study_has_unbound_fields = any(
                bool(unit.get(field_name))
                and not ids_by_field.get(f"{study_id}:{field_name}")
                for field_name in (
                    "findings",
                    "mechanisms",
                    "moderators_or_boundaries",
                    "zero_results",
                    "limitations",
                )
            )
            all_study_evidence_ids.update(source_study_evidence_ids)
            if required_ids is not None:
                unit_evidence_ids.intersection_update(effective_required_ids)
            context_keys = (
                "research_questions",
                "definitions_and_operationalizations",
                "theoretical_derivation",
                "method",
                "sample_or_context",
                "source_summary_hash",
                "source_locators",
            )
            context = {
                key: unit.get(key)
                for key in context_keys
                if unit.get(key) not in (None, "", [], {})
            }
            atomic_groups: list[list[dict[str, Any]]] | None = None
            if active_dependencies:
                components = [
                    {str(claim.get("claim_id") or "")}
                    for claim in claims
                ]
                for dependency in active_dependencies.values():
                    linked = {
                        dependency.primary_claim_id,
                        *dependency.required_source_claim_ids,
                    }
                    merged = set(linked)
                    remaining: list[set[str]] = []
                    for component in components:
                        if component & linked:
                            merged.update(component)
                        else:
                            remaining.append(component)
                    components = [*remaining, merged]
                atomic_groups = [
                    [claim for claim in claims if str(claim.get("claim_id") or "") in component]
                    for component in sorted(
                        components,
                        key=lambda group: min(
                            index for index, claim in enumerate(claims)
                            if str(claim.get("claim_id") or "") in group
                        ),
                    )
                ]
            claim_chunks = split_claims(
                claims,
                context=context,
                all_claim_evidence_ids=all_study_claim_evidence_ids,
                atomic_groups=atomic_groups,
            )
            chunk_count = len(claim_chunks)
            all_study_claims_bound = all(
                bool(claim_evidence_ids([claim]))
                for claim in claims_by_study.get(study_id, [])
            )
            study_scope_complete = (
                selected_claim_ids == source_claim_ids
                and all_study_claims_bound
                and (
                    required_ids is None
                    or source_study_evidence_ids.issubset(effective_required_ids)
                )
                and not (
                    required_ids is not None
                    and study_has_unbound_fields
                    and not include_unbound_claims
                )
            )
            for local_index, claim_chunk in enumerate(claim_chunks, start=1):
                selected_ids = claim_evidence_ids(claim_chunk)
                if not claims:
                    selected_ids.update(unit_evidence_ids)
                if local_index == 1:
                    # Unclaimed evidence is preserved once at its owning study
                    # rather than duplicated into every claim fragment.
                    selected_ids.update(unit_evidence_ids - claim_evidence_ids(claims))
                claim_texts = {
                    str(claim.get("text") or "").strip().casefold()
                    for claim in claims
                    if str(claim.get("text") or "").strip()
                }
                all_study_claim_texts = {
                    str(claim.get("text") or "").strip().casefold()
                    for claim in claims_by_study.get(study_id, [])
                    if str(claim.get("text") or "").strip()
                }
                evidence_texts = {
                    str(text_by_id.get(evidence_id) or "").strip().casefold()
                    for evidence_id in claim_evidence_ids(claims_by_study.get(study_id, []))
                    if str(text_by_id.get(evidence_id) or "").strip()
                }
                selected_evidence_texts = {
                    str(text_by_id.get(evidence_id) or "").strip().casefold()
                    for evidence_id in selected_ids
                    if str(text_by_id.get(evidence_id) or "").strip()
                }
                unrepresented_fields: dict[str, list[Any]] = {}
                if local_index == 1:
                    for field_name in (
                        "findings",
                        "mechanisms",
                        "moderators_or_boundaries",
                        "zero_results",
                        "limitations",
                    ):
                        values = list(unit.get(field_name) or ())
                        field_ids = set(ids_by_field.get(field_name, []))
                        if required_ids is not None:
                            relevant_field_ids = field_ids.intersection(effective_required_ids)
                            if field_ids and not relevant_field_ids:
                                continue
                            if (
                                not field_ids
                                and not include_unbound_claims
                                and not active_dependencies
                            ):
                                continue
                        remaining = [
                            value
                            for value in values
                            if str(value).strip().casefold()
                            not in (
                                claim_texts
                                | all_study_claim_texts
                                | evidence_texts
                                | selected_evidence_texts
                            )
                        ]
                        if (
                            required_ids is not None
                            and field_ids
                            and field_ids.intersection(effective_required_ids) != field_ids
                        ):
                            remaining = []
                        if remaining:
                            unrepresented_fields[field_name] = remaining
                claim_chunk_complete = bool(claim_chunk) and all(
                    bool(claim_evidence_ids([claim]))
                    and claim_evidence_ids([claim]).issubset(selected_ids)
                    for claim in claim_chunk
                )
                study_chunk_complete = chunk_count == 1 and study_scope_complete
                chunk_coverage_status = (
                    "complete_for_study"
                    if study_chunk_complete
                    else "complete_for_claim"
                    if claim_chunk_complete
                    else "partial"
                )
                study_record = {
                    "study_id": study_id,
                    "source_study_id": source_study_id,
                    "parent_paper_id": str(unit.get("parent_paper_id") or complete["paper_key"]),
                    "shared_context": context,
                    "claims": claim_chunk,
                    "source_claim_count": len(source_claim_ids),
                    "selected_claim_count": len(claim_chunk),
                    "source_evidence_id_count": len(source_study_evidence_ids),
                    "selected_evidence_id_count": len(selected_ids),
                    "evidence_ids": sorted(selected_ids),
                    "source_locators": dict(unit.get("source_locators") or {}),
                    "chunk_index": local_index,
                    "chunk_count": chunk_count,
                    "chunk_complete_for_claim": claim_chunk_complete,
                    "chunk_complete_for_study": study_chunk_complete,
                    "coverage_status": chunk_coverage_status,
                }
                chunk_claim_ids = {
                    str(claim.get("claim_id") or "") for claim in claim_chunk
                }
                chunk_dependencies = [
                    dependency for dependency in active_dependencies.values()
                    if dependency.primary_claim_id in chunk_claim_ids
                ]
                if chunk_dependencies:
                    chunk_field_ids = sorted({
                        field_id
                        for dependency in chunk_dependencies
                        for field_id in dependency.required_source_field_ids
                    })
                    study_record["interpretation_dependencies"] = [
                        dependency.to_dict()
                        for dependency in sorted(
                            chunk_dependencies,
                            key=lambda item: (
                                item.primary_claim_id,
                                item.reason,
                            ),
                        )
                    ]
                    study_record["interpretation_source_fields"] = [
                        fields_by_id[field_id].to_dict()
                        for field_id in chunk_field_ids
                    ]
                    study_record["interpretation_complete"] = all(
                        set(dependency.required_source_claim_ids).issubset(chunk_claim_ids)
                        and set(dependency.required_evidence_ids).issubset(selected_ids)
                        and set(dependency.required_source_field_ids).issubset(chunk_field_ids)
                        for dependency in chunk_dependencies
                    )
                    if not study_record["interpretation_complete"]:
                        raise OutlineV3ExecutionError(
                            f"study {study_id} split an interpretation dependency across fragments"
                        )
                if unrepresented_fields:
                    study_record["unmapped_source_fields"] = unrepresented_fields
                chunks.append(
                    {
                        "paper_key": complete["paper_key"],
                        "source_summary_hash": complete["source_summary_hash"],
                        "evidence_unit_id": "",
                        "evidence_unit_hash": complete["evidence_unit_hash"],
                        "unit_scope": "study",
                        "study_units": [study_record],
                        "evidence_ids_by_field": selected_field_ids(selected_ids),
                        "evidence_text_by_id": selected_evidence_text(selected_ids, claim_chunk),
                        "source_field_ledger": [
                            row
                            for row in complete_source_fields
                            if isinstance(row, Mapping)
                            and (
                                str(row.get("scope") or "") != "explicit_study"
                                or str(row.get("study_id") or "") == source_study_id
                            )
                        ],
                        "source_locators": dict(unit.get("source_locators") or {}),
                        "projection": "scoped_study_claim_source_fields_fragment_v4",
                        "chunk_complete_for_claim": claim_chunk_complete,
                        "chunk_complete_for_study": study_chunk_complete,
                        "coverage_status": chunk_coverage_status,
                    }
                )

        paper_evidence_ids = claim_evidence_ids(paper_claims)
        all_claim_evidence_ids = all_study_claim_evidence_ids | paper_evidence_ids
        orphan_paper_ids = {
            str(value)
            for values in ids_by_field.values()
            for value in values
            if str(value) not in all_study_evidence_ids
            and str(value) not in all_claim_evidence_ids
            and (required_ids is None or str(value) in required_ids)
        }
        paper_evidence_ids.update(orphan_paper_ids)
        if paper_claims or orphan_paper_ids:
            paper_field_values = {
                str(field_name): view_payload.get(field_name)
                for field_name in fields
                if field_name in view_payload and view_payload.get(field_name) not in (None, "", [], {})
            }
            if required_ids is not None:
                paper_field_values = {
                    field_name: value
                    for field_name, value in paper_field_values.items()
                    if not ids_by_field.get(field_name)
                    or bool(set(ids_by_field.get(field_name, [])).intersection(required_ids))
                }
            paper_claim_chunks = split_claims(
                paper_claims,
                context=paper_field_values,
                all_claim_evidence_ids=paper_evidence_ids,
            )
            paper_chunks: list[dict[str, Any]] = []
            for local_index, claim_chunk in enumerate(paper_claim_chunks, start=1):
                selected_ids = claim_evidence_ids(claim_chunk)
                if local_index == 1:
                    selected_ids.update(orphan_paper_ids)
                selected_field_values = (
                    without_duplicate_source_text(
                        paper_field_values,
                        {
                            str(claim.get("text") or "").strip().casefold()
                            for claim in claim_chunk
                            if str(claim.get("text") or "").strip()
                        }
                        | {
                            str(text_by_id.get(evidence_id) or "").strip().casefold()
                            for evidence_id in selected_ids
                            if str(text_by_id.get(evidence_id) or "").strip()
                        },
                    )
                    if local_index == 1
                    else {}
                )
                chunk = {
                    "paper_key": complete["paper_key"],
                    "source_summary_hash": complete["source_summary_hash"],
                    "evidence_unit_id": "",
                    "evidence_unit_hash": complete["evidence_unit_hash"],
                    "unit_scope": "paper",
                    "study_units": [],
                    "claims": claim_chunk,
                    "paper_level_claim_ids": sorted(
                        str(claim.get("claim_id") or "") for claim in claim_chunk
                    ),
                    "semantic_fields": selected_field_values,
                    "evidence_ids_by_field": selected_field_ids(selected_ids),
                    "evidence_text_by_id": selected_evidence_text(selected_ids, claim_chunk),
                    "source_field_ledger": complete_source_fields,
                    "source_locators": {
                        "paper_claims": sorted(
                            {
                                str(claim.get("source_locator"))
                                for claim in claim_chunk
                                if claim.get("source_locator")
                            }
                        ),
                        "dossier": source_locators if local_index == 1 else {},
                    },
                    "projection": "scoped_paper_claim_source_fields_v3",
                    "chunk_complete_for_claim": bool(claim_chunk) and all(
                        bool(claim_evidence_ids([claim]))
                        and claim_evidence_ids([claim]).issubset(selected_ids)
                        for claim in claim_chunk
                    ),
                    "chunk_complete_for_study": False,
                    "coverage_status": (
                        "complete_for_claim"
                        if claim_chunk
                        and all(
                            bool(claim_evidence_ids([claim]))
                            and claim_evidence_ids([claim]).issubset(selected_ids)
                            for claim in claim_chunk
                        )
                        else "partial"
                    ),
                    "paper_claim_chunk_index": local_index,
                    "paper_claim_chunk_count": len(paper_claim_chunks),
                }
                paper_chunks.append(chunk)
            chunks = [*paper_chunks, *chunks]

        if not chunks:
            # A valid but claim-free dossier is still source evidence. Keep it
            # as one unsplit paper-scoped unit with the full literal payload.
            chunks = [{
                **complete,
                "unit_scope": "paper",
                "chunk_complete_for_claim": False,
                "chunk_complete_for_study": False,
            }]
        for index, chunk in enumerate(chunks, start=1):
            chunk["evidence_unit_id"] = f"{complete['evidence_unit_id']}:unit:{index}"
            chunk["chunk_group_id"] = complete["evidence_unit_id"]
            chunk["chunk_index"] = index
            chunk["chunk_count"] = len(chunks)

        if chunk_indexes is None:
            return chunks
        try:
            requested = [int(item) for item in chunk_indexes]
        except (TypeError, ValueError) as exc:
            raise OutlineV3ExecutionError("invalid semantic evidence-unit index") from exc
        if not requested or any(index < 1 or index > len(chunks) for index in requested):
            raise OutlineV3ExecutionError(
                f"semantic evidence-unit selection is empty or out of range: {requested}"
            )
        if len(set(requested)) != len(requested):
            raise OutlineV3ExecutionError("semantic evidence-unit selection contains duplicate indexes")
        allowed = set(requested)
        selected = [chunk for chunk in chunks if int(chunk["chunk_index"]) in allowed]
        if {int(chunk["chunk_index"]) for chunk in selected} != allowed:
            raise OutlineV3ExecutionError("semantic evidence-unit selection was not fully materialized")
        return selected

    @classmethod
    def _compact_candidate_evidence_refs(
        cls,
        views: Sequence[Any],
        content_layers: Any,
        semantic_plan: Any,
    ) -> list[dict[str, Any]]:
        topic_ids_by_paper: dict[str, list[str]] = {}
        for topic in getattr(semantic_plan, "topics", []) or []:
            for paper_id in (
                *list(getattr(topic, "paper_ids", []) or []),
                *list(getattr(topic, "bridge_paper_ids", []) or []),
            ):
                topic_ids_by_paper.setdefault(str(paper_id), []).append(
                    str(getattr(topic, "topic_id", "") or "")
                )
        dossiers = getattr(content_layers, "dossier_by_paper", {})
        refs: list[dict[str, Any]] = []
        for view in views:
            payload = view.to_dict() if hasattr(view, "to_dict") else dict(view)
            paper_key = str(payload.get("paper_key") or payload.get("canonical_paper_key") or "")
            dossier = dossiers.get(paper_key) if isinstance(dossiers, Mapping) else None
            field_refs = cls._compact_topic_evidence_unit(
                view,
                dossier,
                fields=(),
            )
            refs.append(
                {
                    "paper_key": paper_key,
                    "title": str(payload.get("title") or ""),
                    "source_summary_hash": str(payload.get("source_summary_hash") or ""),
                    "evidence_unit_id": field_refs["evidence_unit_id"],
                    "evidence_unit_hash": field_refs["evidence_unit_hash"],
                    "topic_ids": sorted(set(topic_ids_by_paper.get(paper_key, []))),
                    "projection": "registry_complete_evidence_ref_v1",
                }
            )
        return sorted(refs, key=lambda item: str(item.get("paper_key") or ""))

    @staticmethod
    def _compact_semantic_provider_result_for_candidate(
        value: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        """Keep synthesis content and summarize reducer accounting identities."""

        if not isinstance(value, Mapping):
            return {}
        result = dict(value)
        for field_name in (
            "processed_topic_ids",
            "processed_fragment_ids",
            "processed_result_ids",
            "processed_relation_ids",
        ):
            if field_name not in result:
                continue
            identities = sorted({
                str(item)
                for item in result.pop(field_name) or ()
                if str(item)
            })
            result[f"{field_name}_coverage"] = {
                "count": len(identities),
                "identity_set_hash": hash_json(identities),
            }
        return result

    @staticmethod
    def _build_semantic_coverage_ledger(
        topic_rows: Sequence[Mapping[str, Any]],
        relation_ids: Sequence[str],
        provider_result: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        """Keep exact membership locally after the semantic result is checked."""

        topics: set[str] = set()
        fragments: set[str] = set()
        results: set[str] = set()
        topic_members: dict[str, set[str]] = {}
        fragment_members: dict[str, set[str]] = {}
        topic_evidence: dict[str, set[str]] = {}
        row_hashes: list[str] = []
        for row in topic_rows:
            if not isinstance(row, Mapping):
                continue
            topic_id = str(row.get("topic_id") or "")
            if not topic_id:
                continue
            topics.add(topic_id)
            fragment_id = str(row.get("fragment_id") or "")
            if fragment_id:
                fragments.add(fragment_id)
            results.update(str(value) for value in row.get("result_ids") or () if str(value))
            topic_members.setdefault(topic_id, set()).update(
                str(value)
                for value in (*list(row.get("paper_ids") or ()), *list(row.get("bridge_paper_ids") or ()))
                if str(value)
            )
            if fragment_id:
                fragment_members.setdefault(fragment_id, set()).update(
                    str(value)
                    for value in row.get("paper_ids") or ()
                    if str(value)
                )
            topic_evidence.setdefault(topic_id, set()).update(
                str(value) for value in row.get("supporting_evidence_ids") or () if str(value)
            )
            row_hashes.append(hash_json(dict(row)))
        dispositions = [
            dict(row) for row in (provider_result or {}).get("topic_dispositions") or ()
            if isinstance(row, Mapping)
        ]
        disposition_ids = [str(row.get("topic_id") or "") for row in dispositions]
        if provider_result is not None and (
            sorted(disposition_ids) != sorted(topics)
            or len(disposition_ids) != len(set(disposition_ids))
        ):
            raise OutlineV3ExecutionError(
                "cross-topic coverage ledger cannot reconcile provider dispositions"
            )
        ledger = {
            "schema_version": "outline-semantic-coverage-ledger/v1",
            "provider_review_status": "validated" if provider_result is not None else "not_provider_reviewed",
            "topic_ids": sorted(topics),
            "fragment_ids": sorted(fragments),
            "result_ids": sorted(results),
            "relation_ids": sorted({str(value) for value in relation_ids if str(value)}),
            "topic_members_by_id": {key: sorted(value) for key, value in sorted(topic_members.items())},
            "fragment_members_by_id": {key: sorted(value) for key, value in sorted(fragment_members.items())},
            "topic_evidence_ids_by_id": {key: sorted(value) for key, value in sorted(topic_evidence.items())},
            "topic_status_by_id": {
                str(row["topic_id"]): str(row.get("status") or "") for row in dispositions
            },
            "source_row_hashes": sorted(row_hashes),
            "provider_result_hash": hash_json(dict(provider_result)) if provider_result is not None else "",
        }
        ledger["content_hash"] = hash_json(ledger)
        return ledger

    def _shared_semantic_cache_valid(
        self, node_id: str, payload: Mapping[str, Any]
    ) -> bool:
        """Reject pre-V3 semantic artifacts before a resumed node can reuse them."""

        if payload.get("shared_synthesis_contract_version") != SHARED_SYNTHESIS_CONTRACT_VERSION:
            return False
        provider_output = payload.get("provider_output")
        if not isinstance(provider_output, Mapping) or not provider_output:
            return False
        if node_id == "cross_group_comparison":
            ledger = payload.get("coverage_ledger")
            if not isinstance(ledger, Mapping):
                return False
            ledger_body = {key: value for key, value in ledger.items() if key != "content_hash"}
            return (
                ledger.get("schema_version") == "outline-semantic-coverage-ledger/v1"
                and ledger.get("provider_review_status") == "validated"
                and ledger.get("content_hash") == hash_json(ledger_body)
                and ledger.get("provider_result_hash") == hash_json(dict(provider_output))
            )
        if node_id == "global_synthesis":
            cross = self._payloads.get("cross_group_comparison") or {}
            ledger = cross.get("coverage_ledger")
            return (
                isinstance(ledger, Mapping)
                and bool(ledger.get("content_hash"))
                and payload.get("cross_coverage_ledger_hash") == ledger.get("content_hash")
            )
        return False

    @staticmethod
    def _candidate_semantic_source_tables(
        values: Sequence[Mapping[str, Any]],
    ) -> dict[str, list[dict[str, Any]]]:
        """Materialize each qualifier's full text once in a candidate request."""

        fields: dict[str, dict[str, Any]] = {}
        dependencies: dict[str, dict[str, Any]] = {}
        for topic in values:
            for fragment in topic.get("fragments") or ():
                if not isinstance(fragment, Mapping):
                    continue
                for result in fragment.get("provider_results") or ():
                    if not isinstance(result, Mapping):
                        continue
                    context = result.get("interpretation_context")
                    if not isinstance(context, Mapping):
                        continue
                    for source_field in context.get("fields") or ():
                        if not isinstance(source_field, Mapping):
                            continue
                        field_id = str(source_field.get("source_field_id") or "")
                        if not field_id or not str(source_field.get("source_value") or "").strip():
                            raise OutlineV3ExecutionError(
                                "candidate interpretation context has a field without source text"
                            )
                        candidate = dict(source_field)
                        if field_id in fields and fields[field_id] != candidate:
                            raise OutlineV3ExecutionError(
                                "candidate interpretation field has conflicting source contents"
                            )
                        fields[field_id] = candidate
                    for dependency in context.get("dependencies") or ():
                        if not isinstance(dependency, Mapping):
                            continue
                        candidate = dict(dependency)
                        dependency_id = "interpretation-dependency:" + hash_json(candidate)[:24]
                        dependencies[dependency_id] = {
                            "dependency_id": dependency_id,
                            **candidate,
                        }
        if any(
            not set(dependency.get("required_source_field_ids") or ()).issubset(fields)
            for dependency in dependencies.values()
        ):
            raise OutlineV3ExecutionError(
                "candidate interpretation dependency lost its provider-visible source text"
            )
        return {
            "source_fields": [fields[key] for key in sorted(fields)],
            "dependencies": [dependencies[key] for key in sorted(dependencies)],
        }

    @staticmethod
    def _compact_semantic_topic_routes_for_candidate(
        values: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        """Keep topic ownership and support while referencing shared source text."""

        compact: list[dict[str, Any]] = []
        for value in values:
            if not isinstance(value, Mapping):
                continue
            route = {
                key: child
                for key, child in value.items()
                if key not in {
                    "provider_batch_ids", "provider_output_refs",
                    "supporting_evidence_ids",
                }
            }
            fragments: list[dict[str, Any]] = []
            for fragment in value.get("fragments") or ():
                if not isinstance(fragment, Mapping):
                    continue
                compact_fragment = dict(fragment)
                if "supporting_evidence_ids" in compact_fragment:
                    compact_fragment["planned_evidence_ids"] = compact_fragment.pop(
                        "supporting_evidence_ids"
                    )
                results: list[dict[str, Any]] = []
                for result in fragment.get("provider_results") or ():
                    if not isinstance(result, Mapping):
                        continue
                    compact_result = dict(result)
                    compact_result.pop("planned_evidence_ids", None)
                    context = result.get("interpretation_context")
                    if isinstance(context, Mapping):
                        compact_result["interpretation_context"] = {
                            "source_field_ids": sorted({
                                str(field.get("source_field_id") or "")
                                for field in context.get("fields") or ()
                                if isinstance(field, Mapping)
                                and str(field.get("source_field_id") or "")
                            }),
                            "dependency_ids": sorted({
                                "interpretation-dependency:" + hash_json(dict(dependency))[:24]
                                for dependency in context.get("dependencies") or ()
                                if isinstance(dependency, Mapping)
                            }),
                        }
                    results.append(compact_result)
                compact_fragment["provider_results"] = results
                fragments.append(compact_fragment)
            route["fragments"] = fragments
            compact.append(route)
        return compact

    @staticmethod
    def _compact_relation_candidate(candidate: Mapping[str, Any]) -> dict[str, Any]:
        evidence_ids = [str(item) for item in candidate.get("evidence_ids") or () if str(item)]
        return {
            "relation_id": str(candidate.get("relation_id") or ""),
            "relation_type": str(candidate.get("relation_type") or ""),
            "paper_keys": [str(item) for item in candidate.get("paper_keys") or () if str(item)],
            "comparison_question": str(candidate.get("comparison_question") or ""),
            "evidence_fields": dict(candidate.get("evidence_fields") or {}),
            "source_fields": dict(candidate.get("source_fields") or {}),
            "supporting_labels": [
                str(item) for item in candidate.get("supporting_labels") or () if str(item)
            ],
            "evidence_id_count": len(evidence_ids),
            "evidence_ids_hash": hash_json(evidence_ids),
            "source_summary_hashes": [
                str(item) for item in candidate.get("source_summary_hashes") or () if str(item)
            ],
        }

    @classmethod
    def _compact_relation_bundle(cls, bundle: Any) -> dict[str, Any]:
        payload = bundle.to_dict() if hasattr(bundle, "to_dict") else dict(bundle)
        required = [str(item) for item in payload.get("required_evidence_ids") or () if str(item)]
        provided = [str(item) for item in payload.get("provided_evidence_ids") or () if str(item)]
        return {
            "relation_id": str(payload.get("relation_id") or ""),
            "comparison_question": str(payload.get("comparison_question") or ""),
            "relation_type": str(payload.get("relation_type") or ""),
            "paper_ids": [str(item) for item in payload.get("paper_ids") or () if str(item)],
            "study_ids": [str(item) for item in payload.get("study_ids") or () if str(item)],
            "claim_ids_left": [str(item) for item in payload.get("claim_ids_left") or () if str(item)],
            "claim_ids_right": [str(item) for item in payload.get("claim_ids_right") or () if str(item)],
            "definitions_and_operationalizations": dict(
                payload.get("definitions_and_operationalizations") or {}
            ),
            "findings_left": list(payload.get("findings_left") or []),
            "findings_right": list(payload.get("findings_right") or []),
            "contexts_and_boundaries": dict(payload.get("contexts_and_boundaries") or {}),
            "source_locators": dict(payload.get("source_locators") or {}),
            "required_evidence_ids": required,
            "provided_evidence_ids": provided,
            "required_evidence_id_count": len(required),
            "required_evidence_ids_hash": hash_json(required),
            "provided_evidence_id_count": len(provided),
            "provided_evidence_ids_hash": hash_json(provided),
            "missing_evidence_ids": [
                str(item) for item in payload.get("missing_evidence_ids") or () if str(item)
            ],
            "evidence_completeness": str(payload.get("evidence_completeness") or ""),
            "decision": str(payload.get("decision") or ""),
            "projection": "complete_relation_bundle_plus_evidence_refs_v1",
        }

    def _build_topic_provider_request(
        self,
        batch: Sequence[TopicSynthesis],
        *,
        topic_routes: Mapping[str, Any],
        evidence_model: Any,
        content_layers_model: Any,
        batch_index: int,
        content_layers_hash: str | None = None,
    ) -> dict[str, Any]:
        paper_ids = sorted(
            {
                str(paper_id)
                for topic in batch
                for paper_id in (
                    *list(getattr(topic, "paper_ids", []) or []),
                    *list(getattr(topic, "bridge_paper_ids", []) or []),
                )
                if str(paper_id)
            }
        )
        dimensions = sorted(
            {
                str(dimension)
                for topic in batch
                for dimension in (
                    getattr(topic_routes.get(topic.topic_id), "dimensions", []) or []
                )
                if str(dimension)
            }
        )
        fields = self._topic_projection_fields(dimensions)
        view_by_paper = {
            str(getattr(view, "paper_key", "")): view
            for view in getattr(evidence_model, "views", []) or []
            if str(getattr(view, "paper_key", ""))
        }
        dossiers = getattr(content_layers_model, "dossier_by_paper", {})
        evidence_units: list[dict[str, Any]] = []
        units_by_id: dict[str, dict[str, Any]] = {}
        expected_unit_ids: set[str] = set()
        for paper_id in paper_ids:
            if paper_id not in view_by_paper:
                raise OutlineV3ExecutionError(
                    f"semantic request plan references paper without an evidence view: {paper_id}"
                )
            dossier = dossiers.get(paper_id) if isinstance(dossiers, Mapping) else None
            if dossier is None:
                raise OutlineV3ExecutionError(
                    f"semantic request plan references paper without a complete dossier: {paper_id}"
                )
            paper_dimensions = sorted(
                {
                    str(dimension)
                    for topic in batch
                    if paper_id
                    in (
                        *list(getattr(topic, "paper_ids", []) or []),
                        *list(getattr(topic, "bridge_paper_ids", []) or []),
                    )
                    for dimension in (
                        getattr(topic_routes.get(topic.topic_id), "dimensions", []) or []
                    )
                    if str(dimension)
                }
            )
            paper_fields = self._topic_projection_fields(paper_dimensions)
            include_unbound_claims = any(
                bool(getattr(topic_routes.get(topic.topic_id), "include_unbound_claims", False))
                for topic in batch
                if paper_id
                in (
                    *list(getattr(topic, "paper_ids", []) or []),
                    *list(getattr(topic, "bridge_paper_ids", []) or []),
                )
            )
            required_evidence_ids = sorted(
                {
                    str(value)
                    for topic in batch
                    if paper_id
                    in (
                        *list(getattr(topic, "paper_ids", []) or []),
                        *list(getattr(topic, "bridge_paper_ids", []) or []),
                    )
                    for value in (getattr(topic, "supporting_evidence_ids", []) or [])
                    if str(value) in set(getattr(dossier, "evidence_ids", []) or [])
                }
            )
            filter_indexes: set[int] = set()
            selects_all_units = False
            chunk_targets = {
                int(getattr(topic, "evidence_chunk_target_tokens", 0) or 0)
                for topic in batch
                if paper_id in (getattr(topic, "paper_ids", []) or [])
                and int(getattr(topic, "evidence_chunk_target_tokens", 0) or 0) > 0
            }
            if len(chunk_targets) > 1:
                raise OutlineV3ExecutionError(
                    f"topic fragments for paper {paper_id} use conflicting evidence chunk plans"
                )
            chunk_target_tokens = next(iter(chunk_targets), None)
            all_units = self._complete_topic_evidence_units(
                view_by_paper[paper_id],
                dossier,
                fields=paper_fields,
                chunk_target_tokens=chunk_target_tokens,
                required_evidence_ids=required_evidence_ids,
                include_unbound_claims=include_unbound_claims,
            )
            available_indexes = {int(unit["chunk_index"]) for unit in all_units}
            for topic in batch:
                topic_papers = {
                    str(value)
                    for value in (
                        *list(getattr(topic, "paper_ids", []) or []),
                        *list(getattr(topic, "bridge_paper_ids", []) or []),
                    )
                    if str(value)
                }
                filters = getattr(topic, "evidence_unit_indexes", None)
                if not isinstance(filters, Mapping):
                    filters = getattr(topic, "_evidence_unit_indexes", None)
                if isinstance(filters, Mapping) and set(filters) - topic_papers:
                    raise OutlineV3ExecutionError(
                        "topic fragment has an evidence-unit selection for an unrelated paper"
                    )
                if paper_id not in topic_papers:
                    continue
                if not isinstance(filters, Mapping) or paper_id not in filters:
                    selects_all_units = True
                    continue
                values = filters[paper_id]
                if (
                    not isinstance(values, Sequence)
                    or isinstance(values, (str, bytes))
                    or not values
                    or any(type(value) is not int for value in values)
                ):
                    raise OutlineV3ExecutionError(
                        "topic fragment has an invalid or empty evidence-unit selection"
                    )
                selected_indexes = list(values)
                if len(set(selected_indexes)) != len(selected_indexes):
                    raise OutlineV3ExecutionError(
                        "topic fragment evidence-unit selection contains duplicate indexes"
                    )
                if not set(selected_indexes).issubset(available_indexes):
                    raise OutlineV3ExecutionError(
                        "topic fragment evidence-unit selection is out of range"
                    )
                filter_indexes.update(selected_indexes)
            if selects_all_units or not filter_indexes:
                selected_units = all_units
            else:
                selected_units = [
                    unit for unit in all_units
                    if int(unit["chunk_index"]) in filter_indexes
                ]
                if {int(unit["chunk_index"]) for unit in selected_units} != filter_indexes:
                    raise OutlineV3ExecutionError(
                        "semantic evidence-unit selection was not fully materialized"
                    )
            expected_unit_ids.update(str(unit["evidence_unit_id"]) for unit in selected_units)
            for unit in selected_units:
                unit_id = str(unit.get("evidence_unit_id") or "")
                if not unit_id:
                    raise OutlineV3ExecutionError("materialized semantic evidence unit is missing its identity")
                prior = units_by_id.get(unit_id)
                if prior is not None and hash_json(prior) != hash_json(unit):
                    raise OutlineV3ExecutionError(
                        f"semantic evidence unit identity has conflicting contents: {unit_id}"
                    )
                units_by_id.setdefault(unit_id, unit)
        evidence_units = [units_by_id[key] for key in sorted(units_by_id)]
        for unit in evidence_units:
            for study in unit.get("study_units") or ():
                if not isinstance(study, dict):
                    raise OutlineV3ExecutionError(
                        "topic evidence unit has a malformed interpretation study"
                    )
                for dependency in study.get("interpretation_dependencies") or ():
                    if not isinstance(dependency, dict):
                        raise OutlineV3ExecutionError(
                            "topic evidence unit has a malformed interpretation dependency"
                        )
                    dependency["required_for_synthesis_output"] = True
        materialized_unit_ids = {str(unit.get("evidence_unit_id") or "") for unit in evidence_units}
        if materialized_unit_ids != expected_unit_ids:
            missing = sorted(expected_unit_ids - materialized_unit_ids)
            unexpected = sorted(materialized_unit_ids - expected_unit_ids)
            raise OutlineV3ExecutionError(
                "semantic evidence plan/materialization mismatch: "
                f"missing={missing[:10]}, unexpected={unexpected[:10]}"
            )
        def selected_units_for_topic(topic: TopicSynthesis) -> list[dict[str, Any]]:
            topic_papers = {
                str(value)
                for value in (
                    *list(getattr(topic, "paper_ids", []) or []),
                    *list(getattr(topic, "bridge_paper_ids", []) or []),
                )
                if str(value)
            }
            filters = getattr(topic, "evidence_unit_indexes", {})
            if not isinstance(filters, Mapping):
                filters = {}
            selected: list[dict[str, Any]] = []
            for unit in evidence_units:
                paper_key = str(unit.get("paper_key") or "")
                if paper_key not in topic_papers:
                    continue
                if paper_key in filters and int(unit.get("chunk_index") or 0) not in {
                    int(value) for value in filters.get(paper_key) or ()
                }:
                    continue
                selected.append(unit)
            return selected

        topic_rows: list[dict[str, Any]] = []
        for topic in batch:
            if topic.topic_id not in topic_routes:
                continue
            topic_units = selected_units_for_topic(topic)
            planned_unit_ids = sorted(
                str(unit.get("evidence_unit_id") or "") for unit in topic_units
            )
            planned_evidence_ids = sorted(
                {
                    str(evidence_id)
                    for unit in topic_units
                    for evidence_id in (
                        *[
                            value
                            for values in (unit.get("evidence_ids_by_field") or {}).values()
                            for value in values
                        ],
                        *list((unit.get("evidence_text_by_id") or {}).keys()),
                        *[
                            evidence_id
                            for claim in unit.get("claims") or ()
                            if isinstance(claim, Mapping)
                            for evidence_id in claim.get("evidence_ids") or ()
                        ],
                        *[
                            evidence_id
                            for study in unit.get("study_units") or ()
                            if isinstance(study, Mapping)
                            for claim in study.get("claims") or ()
                            if isinstance(claim, Mapping)
                            for evidence_id in claim.get("evidence_ids") or ()
                        ],
                    )
                    if str(evidence_id)
                }
            )
            topic_rows.append(
                {
                    **self._compact_topic_route(topic_routes[topic.topic_id]),
                    "fragment_id": str(getattr(topic, "fragment_id", "") or topic.topic_id),
                    "paper_ids": [str(item) for item in topic.paper_ids if str(item)],
                    "bridge_paper_ids": [
                        str(item) for item in topic.bridge_paper_ids if str(item)
                    ],
                    "required_evidence_count": len(planned_evidence_ids),
                    "planned_evidence_unit_ids": planned_unit_ids,
                    "planned_evidence_ids": planned_evidence_ids,
                }
            )
        return {
            "semantic_contract_version": "semantic-evidence-graph-v2",
            "interpretation_contract_version": INTERPRETATION_CONTRACT_VERSION,
            "task": "substantive_topic_synthesis",
            "node_id": "topic_synthesis",
            "hierarchy": {
                "level": "topic_synthesis",
                "batch_id": f"topic_batch_{batch_index}",
                "target_tokens": self.technical_shard_target_tokens or 30_000,
            },
            "topics": topic_rows,
            "evidence_units": evidence_units,
            "planned_evidence_unit_ids": sorted(expected_unit_ids),
            "evidence_projection": {
                "projection": "complete_field_values_plus_registry_refs_v2",
                "fields_in_prompt": list(fields),
                "omitted_fields_are_registry_bound": True,
                "source_field_policy": (
                    "Include selected raw source fields with exact path, value, scope, "
                    "study identity, disposition, and source_field_id. An unmapped or "
                    "unresolved row is provenance only, not a validated interpretation."
                ),
                "content_layers_hash": (
                    content_layers_hash
                    if content_layers_hash is not None
                    else getattr(content_layers_model, "content_hash", "")
                ),
            },
            "output_contract": {
                "semantic_result_contract_version": "bounded-topic-synthesis/v5",
                "max_output_tokens": self._semantic_output_token_limit(
                    self._node_route("candidate_1_provider_generation").profile
                ),
                "max_unresolved_reason_utf8_bytes": 128,
                "response_root_type": "single JSON object, never a top-level array",
                "required_top_level_keys": [
                    "topics", "processed_fragment_ids", "claims", "unresolved_questions"
                ],
                "termination_rule": "Finish the complete JSON object before the output limit. If the supported synthesis will not fit, return a complete unresolved fragment with an explicit reason rather than a partial claim or unclosed JSON.",
                "topics": "array of topic synthesis objects; return every requested fragment_id exactly once with topic_id, status (integrated/completed/processed/unresolved), concise conclusions, unresolved_questions, and supporting_evidence_ids for any factual conclusion. For a conclusion using a primary claim whose interpretation_dependencies entry has required_for_synthesis_output=true, set source_claim_ids to primary plus all required_source_claim_ids, supporting_evidence_ids to primary plus all required_evidence_ids, and source_field_ids to all required_source_field_ids. Preserve the qualifier in the conclusion text or omit it and mark unresolved. Conclusions summarize claims without repeating their evidence-specific text.",
                "processed_fragment_ids": "array containing every requested fragment_id exactly once",
                "claims": "array of distinct evidence-bound synthesis claims, not a one-to-one restatement of every source claim or evidence ID. Combine findings only when direction, conditions, population and horizon align; keep conflicts, conditional effects, null/zero results and material exceptions distinct or unresolved. Each claim has claim_id='synthesis:topic_synthesis:<local-id>', fragment_id, claim_type, paper_key or paper_keys, and evidence_ids. For any claim using a primary claim whose interpretation_dependencies entry has required_for_synthesis_output=true, set source_claim_ids to primary plus required_source_claim_ids, evidence_ids to primary plus required_evidence_ids, and source_field_ids to required_source_field_ids. Preserve the condition or boundary in claim text; qualifier_dependency prose alone is not provenance. Otherwise omit the factual claim and mark unresolved.",
                "source_locator_policy": "Do not echo source_locators in claims. Full exact locators remain bound to evidence IDs in the local Registry and are resolved downstream.",
                "source_claim_ids": "must exactly reference supplied source claim IDs; do not relabel source IDs as generated synthesis claims",
                "source_field_policy": "When a factual synthesis uses a supplied source_field_ledger row, cite its exact source_field_id. Preserve the raw field path and study scope; an unmapped or unresolved row alone does not validate a statistical interpretation.",
                "conciseness_policy": "Return the smallest complete synthesis that preserves distinct supported conclusions, direction, conditions, conflicts, null/zero findings, unresolved items and evidence support. Do not duplicate the same narrative in topics[].conclusions and claims[].text.",
                "unresolved_questions": "array of nonempty strings for unresolved questions across this entire request batch; put fragment-specific questions in topics[].unresolved_questions",
                "no_external_evidence": "do not infer a finding, boundary or consensus from a missing field; emit an unresolved or insufficient-evidence record instead",
                "overflow_policy": "Overflow: return a complete unresolved fragment with the exact reason 'Output limit.', no claims/conclusions/support IDs. Keep every fragment and complete JSON.",
            },
        }

    @staticmethod
    def _minimum_complete_topic_response_bytes(request: Mapping[str, Any]) -> int:
        """Size one complete, claim-free overflow response using actual IDs.

        The provider can always choose the short, explicitly permitted
        ``Output limit.`` reason. This proves an accepted fallback exists; it
        does not size substantive synthesis or guarantee a third-party route's
        tokenizer behavior.
        """

        contract = request.get("output_contract")
        if not isinstance(contract, Mapping) or contract.get(
            "semantic_result_contract_version"
        ) != "bounded-topic-synthesis/v5":
            raise OutlineV3ExecutionError("topic fallback needs the v5 output contract")
        reason_bytes = contract.get("max_unresolved_reason_utf8_bytes")
        reason = "Output limit."
        if type(reason_bytes) is not int or reason_bytes < len(reason.encode("utf-8")):
            raise OutlineV3ExecutionError("topic fallback reason limit is invalid")
        topics = [item for item in request.get("topics") or () if isinstance(item, Mapping)]
        if not topics:
            raise OutlineV3ExecutionError("topic fallback has no requested fragments")
        response = {
            "topics": [
                {
                    "topic_id": str(item.get("topic_id") or ""),
                    "fragment_id": str(item.get("fragment_id") or item.get("topic_id") or ""),
                    "status": "unresolved",
                    "unresolved_questions": [reason],
                }
                for item in topics
            ],
            "processed_fragment_ids": [
                str(item.get("fragment_id") or item.get("topic_id") or "")
                for item in topics
            ],
            "claims": [],
            "unresolved_questions": [],
        }
        return len(json.dumps(response, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))

    def _plan_topic_provider_batches(
        self,
        topic_plan: Sequence[TopicSynthesis],
        *,
        topic_routes: Mapping[str, Any],
        evidence_model: Any,
        content_layers_model: Any,
        profile: ProviderContextProfile,
    ) -> tuple[list[TopicSynthesis], list[list[TopicSynthesis]], list[dict[str, Any]]]:
        """Build the same serialized topic batches used by preflight and run."""

        input_limit = max(
            1,
            min(
                32_000,
                int(self.max_source_prompt_tokens or 32_000),
                int(profile.input_budget or 32_000),
            ),
        )
        content_layers_hash = str(getattr(content_layers_model, "content_hash", ""))
        expanded = self._split_topic_plan_for_budget(
            topic_plan,
            topic_routes=topic_routes,
            evidence_model=evidence_model,
            content_layers_model=content_layers_model,
            profile=profile,
            input_limit=input_limit,
            content_layers_hash=content_layers_hash,
        )
        # Keep tasks with the same paper ownership together so the wire
        # serializer can union overlapping complete dossier units inside one
        # provider batch. Topic questions and identities remain distinct.
        expanded.sort(
            key=lambda item: (
                tuple(sorted(str(value) for value in item.paper_ids if str(value))),
                tuple(sorted(str(value) for value in item.bridge_paper_ids if str(value))),
                str(item.topic_id),
                str(item.fragment_id or item.topic_id),
            )
        )
        batches: list[list[TopicSynthesis]] = []
        batch_measurements: list[tuple[dict[str, Any], dict[str, Any]]] = []
        current: list[TopicSynthesis] = []
        current_measurement: tuple[dict[str, Any], dict[str, Any]] | None = None

        def measure(items: Sequence[TopicSynthesis], batch_index: int) -> tuple[dict[str, Any], dict[str, Any]]:
            request = self._build_topic_provider_request(
                items,
                topic_routes=topic_routes,
                evidence_model=evidence_model,
                content_layers_model=content_layers_model,
                batch_index=batch_index,
                content_layers_hash=content_layers_hash,
            )
            node_id = f"topic_synthesis_provider:batch:{batch_index}"
            budget = profile.estimate_request(self._attach_prompt_authority(node_id, request))
            tokens = int(budget.get("estimated_input_tokens") or profile.estimate_tokens(request))
            if tokens > input_limit:
                raise OutlineV3ExecutionError(
                    f"BLOCKED_BUDGET: complete topic request {node_id} uses {tokens} input tokens over the effective cap {input_limit}"
                )
            fallback_bytes = self._minimum_complete_topic_response_bytes(request)
            output_limit = self._semantic_output_token_limit(profile)
            if fallback_bytes + 1 > output_limit:
                raise OutlineV3ExecutionError(
                    "BLOCKED_BUDGET: minimum_complete_response_exceeds_output_cap: "
                    f"{node_id} needs {fallback_bytes + 1} output-token proxy units over cap {output_limit}"
                )
            return request, budget

        for topic in expanded:
            batch_index = len(batches) + 1
            trial = [*current, topic]
            try:
                trial_measurement = measure(trial, batch_index)
            except OutlineV3ExecutionError:
                if not current:
                    raise
                batches.append(current)
                assert current_measurement is not None
                batch_measurements.append(current_measurement)
                current = [topic]
                current_measurement = measure(current, len(batches) + 1)
            else:
                current = trial
                current_measurement = trial_measurement
        if current:
            batches.append(current)
            assert current_measurement is not None
            batch_measurements.append(current_measurement)

        bridge_paper_ids_by_topic: dict[str, set[str]] = {}
        for topic in expanded:
            bridge_paper_ids_by_topic.setdefault(str(topic.topic_id), set()).update(
                str(value) for value in topic.bridge_paper_ids if str(value)
            )

        plans: list[dict[str, Any]] = []
        semantic_retries = self._semantic_transport_retry_count()
        for batch_index, (batch, measurement) in enumerate(
            zip(batches, batch_measurements, strict=True), start=1
        ):
            request, budget = measurement
            evidence_units = [
                item for item in request.get("evidence_units") or () if isinstance(item, Mapping)
            ]
            source_claims_by_id: dict[tuple[str, str], Mapping[str, Any]] = {}
            source_claim_identity_hashes: set[str] = set()
            source_text_occurrences: list[str] = []
            evidence_ids: set[str] = set()
            evidence_identity_hashes: set[str] = set()

            def add_wire_text(value: Any) -> None:
                if isinstance(value, str) and value.strip():
                    source_text_occurrences.append(" ".join(value.split()))
                elif isinstance(value, Mapping):
                    for child in value.values():
                        add_wire_text(child)
                elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
                    for child in value:
                        add_wire_text(child)

            for unit in evidence_units:
                for claim in unit.get("claims") or ():
                    if isinstance(claim, Mapping):
                        claim_id = str(claim.get("claim_id") or "")
                        if claim_id:
                            source_claims_by_id.setdefault(
                                (str(unit.get("paper_key") or ""), claim_id),
                                claim,
                            )
                            source_claim_identity_hashes.add(
                                hash_json(
                                    {
                                        "paper_key": unit.get("paper_key"),
                                        "claim_id": claim_id,
                                    }
                                )
                            )
                        claim_evidence_ids = [
                            str(value) for value in claim.get("evidence_ids") or () if str(value)
                        ]
                        evidence_ids.update(claim_evidence_ids)
                        evidence_identity_hashes.update(
                            hash_json({"paper_key": unit.get("paper_key"), "evidence_id": value})
                            for value in claim_evidence_ids
                        )
                        claim_text = str(claim.get("text") or "").strip()
                        if claim_text:
                            add_wire_text(claim_text)
                        evidence_ids.update(
                            str(value) for value in claim.get("evidence_ids") or () if str(value)
                        )
                evidence_ids.update(
                    str(value)
                    for values in (unit.get("evidence_ids_by_field") or {}).values()
                    for value in values
                    if str(value)
                )
                evidence_identity_hashes.update(
                    hash_json({"paper_key": unit.get("paper_key"), "evidence_id": str(value)})
                    for values in (unit.get("evidence_ids_by_field") or {}).values()
                    for value in values
                    if str(value)
                )
                text_by_id = unit.get("evidence_text_by_id")
                if isinstance(text_by_id, Mapping):
                    evidence_ids.update(str(value) for value in text_by_id if str(value))
                    evidence_identity_hashes.update(
                        hash_json({"paper_key": unit.get("paper_key"), "evidence_id": str(value)})
                        for value in text_by_id
                        if str(value)
                    )
                    add_wire_text(list(text_by_id.values()))
                for study in unit.get("study_units") or ():
                    if not isinstance(study, Mapping):
                        continue
                    for claim in study.get("claims") or ():
                        if isinstance(claim, Mapping):
                            claim_id = str(claim.get("claim_id") or "")
                            if claim_id:
                                source_claims_by_id.setdefault(
                                    (str(unit.get("paper_key") or ""), claim_id),
                                    claim,
                                )
                                source_claim_identity_hashes.add(
                                    hash_json(
                                        {
                                            "paper_key": unit.get("paper_key"),
                                            "claim_id": claim_id,
                                        }
                                    )
                                )
                            claim_evidence_ids = [
                                str(value)
                                for value in claim.get("evidence_ids") or ()
                                if str(value)
                            ]
                            evidence_ids.update(claim_evidence_ids)
                            evidence_identity_hashes.update(
                                hash_json(
                                    {"paper_key": unit.get("paper_key"), "evidence_id": value}
                                )
                                for value in claim_evidence_ids
                            )
                    context = study.get("shared_context")
                    if isinstance(context, Mapping):
                        add_wire_text(context)
            normalized_texts = [value.casefold() for value in source_text_occurrences]
            unique_texts = set(normalized_texts)
            text_identity_character_counts = {
                hash_json(value): len(value)
                for value in unique_texts
            }
            planned_unit_ids = sorted(request["planned_evidence_unit_ids"])
            materialized_unit_ids = sorted(
                str(item.get("evidence_unit_id") or "") for item in evidence_units
            )
            topic_node_id = f"topic_synthesis_provider:batch:{batch_index}"
            attached_request = self._attach_prompt_authority(topic_node_id, request)
            request_body_bytes = len(json.dumps(request, ensure_ascii=False, sort_keys=True).encode("utf-8"))
            attached_request_bytes = len(json.dumps(attached_request, ensure_ascii=False, sort_keys=True).encode("utf-8"))
            cross_group_fragment_plans: list[dict[str, Any]] = []
            batch_id = str((request.get("hierarchy") or {}).get("batch_id") or "")
            output_upper_bound = self._semantic_output_token_limit(profile)
            for fragment in request.get("topics") or ():
                if not isinstance(fragment, Mapping):
                    continue
                planned_unit_ids_for_fragment = [
                    str(value)
                    for value in fragment.get("planned_evidence_unit_ids") or ()
                    if str(value)
                ]
                interpretation_context = self._interpretation_context_for_units(
                    evidence_units,
                    planned_unit_ids_for_fragment,
                )
                runtime_fragment_metadata = {
                    "topic_id": str(fragment.get("topic_id") or ""),
                    "fragment_id": str(fragment.get("fragment_id") or ""),
                    "question": str(fragment.get("question") or ""),
                    "paper_ids": [str(value) for value in fragment.get("paper_ids") or () if str(value)],
                    "bridge_paper_ids": sorted(
                        bridge_paper_ids_by_topic.get(str(fragment.get("topic_id") or ""), set())
                    ),
                    # Runtime derives this list from provider output. Its
                    # serialized size is bounded separately below.
                    "supporting_evidence_ids": [],
                    "provider_batch_ids": [batch_id] if batch_id else [],
                    "provider_outputs": [],
                    "result_ids": ["fragment-result:" + ("0" * 24)],
                    "interpretation_context": interpretation_context,
                }
                metadata_upper_bound = int(
                    profile.estimate_tokens(runtime_fragment_metadata)
                )
                cross_group_fragment_plans.append({
                    "topic_id": str(fragment.get("topic_id") or ""),
                    "fragment_id": str(fragment.get("fragment_id") or ""),
                    "paper_ids": sorted({
                        str(value) for value in (
                            *list(fragment.get("paper_ids") or ()),
                            *list(fragment.get("bridge_paper_ids") or ()),
                        ) if str(value)
                    }),
                    "interpretation_source_field_count": len(
                        interpretation_context.get("fields") or ()
                    ),
                    "interpretation_dependency_count": len(
                        interpretation_context.get("dependencies") or ()
                    ),
                    "interpretation_context_tokens_upper_bound": metadata_upper_bound,
                    "provider_output_tokens_upper_bound": output_upper_bound,
                    # Runtime carries each provider output and projects its
                    # supporting IDs into a second field on the fragment row.
                    "supporting_id_projection_tokens_upper_bound": output_upper_bound,
                    "cross_group_item_tokens_upper_bound": (
                        metadata_upper_bound + (2 * output_upper_bound) + 128
                    ),
                })
            identity_projection = {
                "planned_evidence_unit_ids": planned_unit_ids,
                "topics": [
                    {
                        key: topic.get(key)
                        for key in ("topic_id", "fragment_id", "paper_ids", "bridge_paper_ids", "planned_evidence_ids")
                    }
                    for topic in request.get("topics") or ()
                    if isinstance(topic, Mapping)
                ],
            }
            plans.append(
                {
                    "node_id": topic_node_id,
                    "batch_id": str(request["hierarchy"]["batch_id"]),
                    "topic_ids": sorted({item.topic_id for item in batch}),
                    "fragment_ids": sorted(
                        {str(item.fragment_id or item.topic_id) for item in batch}
                    ),
                    "topic_fragments": [
                        dict(item)
                        for item in request.get("topics") or ()
                        if isinstance(item, Mapping)
                    ],
                    "cross_group_fragment_plans": cross_group_fragment_plans,
                    "planned_evidence_unit_count": len(planned_unit_ids),
                    "materialized_evidence_unit_count": len(materialized_unit_ids),
                    "planned_evidence_unit_ids_hash": hash_json(planned_unit_ids),
                    "materialized_evidence_unit_ids_hash": hash_json(materialized_unit_ids),
                    "planned_wire_ids_equal_materialized_ids": (
                        planned_unit_ids == materialized_unit_ids
                    ),
                    "paper_count": len({str(item.get("paper_key") or "") for item in evidence_units}),
                    "source_claim_count": len(source_claims_by_id),
                    "source_claim_ids_hash": hash_json(
                        [list(item) for item in sorted(source_claims_by_id)]
                    ),
                    "source_claim_identity_hashes": sorted(source_claim_identity_hashes),
                    "evidence_id_count": len(evidence_ids),
                    "evidence_ids_hash": hash_json(sorted(evidence_ids)),
                    "evidence_identity_hashes": sorted(evidence_identity_hashes),
                    "source_text_occurrence_count": len(normalized_texts),
                    "source_text_unique_value_count": len(unique_texts),
                    "repeated_source_text_occurrence_count": len(normalized_texts) - len(unique_texts),
                    "source_text_character_count": sum(len(value) for value in normalized_texts),
                    "source_text_unique_character_count": sum(len(value) for value in unique_texts),
                    "source_text_occurrence_utf8_bytes": sum(len(value.encode("utf-8")) for value in normalized_texts),
                    "source_text_unique_utf8_bytes": sum(len(value.encode("utf-8")) for value in unique_texts),
                    "canonical_request_body_bytes": request_body_bytes,
                    "canonical_attached_request_bytes": attached_request_bytes,
                    "prompt_authority_added_bytes": attached_request_bytes - request_body_bytes,
                    "output_contract_bytes": len(json.dumps(request.get("output_contract") or {}, ensure_ascii=False, sort_keys=True).encode("utf-8")),
                    "identity_projection_bytes": len(json.dumps(identity_projection, ensure_ascii=False, sort_keys=True).encode("utf-8")),
                    "source_text_identity_set_hash": hash_json(
                        sorted(text_identity_character_counts)
                    ),
                    "source_text_identity_records": [
                        {"identity_hash": identity_hash, "character_count": character_count}
                        for identity_hash, character_count in sorted(text_identity_character_counts.items())
                    ],
                    "estimated_input_tokens": int(
                        budget.get("estimated_input_tokens") or profile.estimate_tokens(request)
                    ),
                    "estimated_cached_input_tokens": int(
                        budget.get("estimated_cached_input_tokens") or 0
                    ),
                    "estimated_cache_write_tokens": int(
                        budget.get("estimated_cache_write_tokens") or 0
                    ),
                    "estimated_output_tokens": self._semantic_output_token_limit(profile),
                    "minimum_complete_unresolved_response_utf8_bytes": self._minimum_complete_topic_response_bytes(request),
                    "estimated_reasoning_tokens": int(profile.reasoning_reserve or 0),
                    "configured_transport_retry_reserve": semantic_retries,
                    "physical_attempt_upper_bound": (
                        1 + semantic_retries if semantic_retries is not None else None
                    ),
                    "attempt_reserve_status": (
                        "configured" if semantic_retries is not None else "unknown"
                    ),
                    "request_hash": hash_json(attached_request),
                }
            )
        return expanded, batches, plans

    def _topic_provider_batch_count(
        self,
        summaries: Sequence[Mapping[str, Any]],
        profile: ProviderContextProfile,
    ) -> int:
        """Count the exact bounded topic requests used by the executor."""

        evidence_model = build_outline_evidence_views(summaries, self.job_id)
        ledger = build_global_corpus_ledger(evidence_model)
        matrix = build_multi_view_matrix(evidence_model)
        relation_map = build_global_relation_map(evidence_model, matrix, ledger)
        content_layers_model = build_paper_content_layers(
            summaries,
            evidence_model,
            job_id=self.job_id,
        )
        semantic_plan = build_semantic_chunk_plan(
            content_layers_model,
            relation_map,
            candidate_count=self.candidate_count,
            physical_call_limit=authorized_provider_call_limit(self.max_provider_calls),
        )
        selected_relation_ids = {
            str(value)
            for value in semantic_plan.coverage.get("selected_relation_ids") or ()
            if str(value)
        }
        self.semantic_relation_candidates = [
            self._compact_relation_candidate(item.to_dict())
            for item in relation_map.relations
            if str(item.relation_id) in selected_relation_ids
        ]
        self.semantic_cross_group_questions = list(semantic_plan.cross_group_questions)
        self.semantic_topic_ids = sorted({str(item.topic_id) for item in semantic_plan.topics})
        topic_routes = {topic.topic_id: topic for topic in semantic_plan.topics}
        _expanded, batches, request_plan = self._plan_topic_provider_batches(
            build_topic_synthesis_plan(semantic_plan),
            topic_routes=topic_routes,
            evidence_model=evidence_model,
            content_layers_model=content_layers_model,
            profile=profile,
        )
        self.semantic_request_plan = request_plan
        return len(batches)

    @staticmethod
    def _compute_topic_provider_plan_identity_hash(
        plan_rows: Sequence[Mapping[str, Any]],
    ) -> str:
        """Return a redacted identity for the serialized topic request plan."""

        requests = []
        for row in plan_rows:
            if not str(row.get("node_id") or "").startswith(
                "topic_synthesis_provider:batch:"
            ):
                continue
            requests.append({
                "node_id": str(row.get("node_id") or ""),
                "request_hash": str(row.get("request_hash") or ""),
                "planned_evidence_unit_ids_hash": str(
                    row.get("planned_evidence_unit_ids_hash") or ""
                ),
                "materialized_evidence_unit_ids_hash": str(
                    row.get("materialized_evidence_unit_ids_hash") or ""
                ),
                "source_claim_identity_set_hash": hash_json(
                    sorted(str(value) for value in row.get("source_claim_identity_hashes") or () if str(value))
                ),
                "evidence_identity_set_hash": hash_json(
                    sorted(str(value) for value in row.get("evidence_identity_hashes") or () if str(value))
                ),
                "source_text_identity_set_hash": str(
                    row.get("source_text_identity_set_hash") or ""
                ),
                "estimated_input_tokens": int(row.get("estimated_input_tokens") or 0),
            })
        requests.sort(key=lambda item: str(item["node_id"]))
        return hash_json({
            "schema_version": "topic-provider-request-plan-identity/v1",
            "requests": requests,
        })

    def _split_topic_plan_for_budget(
        self,
        topics: Sequence[TopicSynthesis],
        *,
        topic_routes: Mapping[str, Any],
        evidence_model: Any,
        content_layers_model: Any,
        profile: ProviderContextProfile,
        input_limit: int,
        content_layers_hash: str,
    ) -> list[TopicSynthesis]:
        """Split oversized topic routes only at paper/evidence-unit boundaries."""

        expanded: list[TopicSynthesis] = []

        chunk_target_tokens = max(1, int(input_limit * 0.65))

        def with_unit_filter(
            source: TopicSynthesis,
            paper_id: str,
            indexes: Sequence[int],
            fragment_index: int,
        ) -> TopicSynthesis:
            dossiers = getattr(content_layers_model, "dossier_by_paper", {})
            dossier = dossiers.get(paper_id) if isinstance(dossiers, Mapping) else None
            paper_evidence_ids = {
                str(value) for value in getattr(dossier, "evidence_ids", []) if str(value)
            }
            clone = replace(
                source,
                paper_ids=[paper_id],
                bridge_paper_ids=[
                    value for value in source.bridge_paper_ids if value == paper_id
                ],
                fragment_id=(
                    f"{source.topic_id}:fragment:{fragment_index}:"
                    f"{hash_json({paper_id: list(indexes)})[:12]}"
                ),
                evidence_unit_indexes={paper_id: [int(index) for index in indexes]},
                evidence_chunk_target_tokens=chunk_target_tokens,
                supporting_evidence_ids=[
                    value
                    for value in source.supporting_evidence_ids
                    if str(value) in paper_evidence_ids
                ],
            )
            return clone

        def with_paper_set(
            source: TopicSynthesis,
            paper_ids: Sequence[str],
        ) -> TopicSynthesis:
            selected_papers = sorted({str(value) for value in paper_ids if str(value)})
            dossiers = getattr(content_layers_model, "dossier_by_paper", {})
            selected_evidence_ids = {
                str(value)
                for paper_id in selected_papers
                for value in getattr(
                    dossiers.get(paper_id) if isinstance(dossiers, Mapping) else None,
                    "evidence_ids",
                    [],
                )
                if str(value)
            }
            return replace(
                source,
                paper_ids=selected_papers,
                bridge_paper_ids=[
                    value for value in source.bridge_paper_ids if value in selected_papers
                ],
                fragment_id=f"{source.topic_id}:papers:{hash_json(selected_papers)[:12]}",
                evidence_unit_indexes={},
                evidence_chunk_target_tokens=0,
                supporting_evidence_ids=[
                    value
                    for value in source.supporting_evidence_ids
                    if str(value) in selected_evidence_ids
                ],
            )

        for topic in topics:
            source_topic_id = topic.topic_id
            include_unbound_claims = bool(
                getattr(topic_routes.get(source_topic_id), "include_unbound_claims", False)
            )
            topic_fields = self._topic_projection_fields(
                getattr(topic_routes.get(source_topic_id), "dimensions", [])
            )
            full_request = self._build_topic_provider_request(
                [topic],
                topic_routes=topic_routes,
                evidence_model=evidence_model,
                content_layers_model=content_layers_model,
                batch_index=len(expanded) + 1,
                content_layers_hash=content_layers_hash,
            )
            full_budget = profile.estimate_request(
                self._attach_prompt_authority(
                    f"topic_synthesis:preflight:{len(expanded) + 1}",
                    full_request,
                )
            )
            full_estimate = int(
                full_budget.get("estimated_input_tokens")
                or profile.estimate_tokens(full_request)
            )
            if full_estimate <= input_limit:
                expanded.append(topic)
                continue
            if len(topic.paper_ids) == 1:
                paper_id = str(topic.paper_ids[0])
                view = next(
                    (
                        item
                        for item in getattr(evidence_model, "views", []) or []
                        if str(getattr(item, "paper_key", "")) == paper_id
                    ),
                    None,
                )
                dossiers = getattr(content_layers_model, "dossier_by_paper", {})
                dossier = dossiers.get(paper_id) if isinstance(dossiers, Mapping) else None
                units = self._complete_topic_evidence_units(
                    view,
                    dossier,
                    fields=topic_fields,
                    chunk_target_tokens=chunk_target_tokens,
                    required_evidence_ids=[
                        value
                        for value in topic.supporting_evidence_ids
                        if str(value) in set(getattr(dossier, "evidence_ids", []) or [])
                    ],
                    include_unbound_claims=include_unbound_claims,
                ) if view is not None else []
                if len(units) > 1:
                    for index in range(1, len(units) + 1):
                        expanded.append(
                            with_unit_filter(topic, paper_id, [index], len(expanded) + 1)
                        )
                    continue
                raise OutlineV3ExecutionError(
                    f"BLOCKED_BUDGET: topic {topic.topic_id} paper {paper_id} is an indivisible complete evidence unit over the effective input cap"
                )
            paper_ids = [str(item) for item in topic.paper_ids if str(item)]
            current: list[str] = []
            for paper_id in paper_ids:
                trial_ids = [*current, paper_id]
                trial_topic = with_paper_set(topic, trial_ids)
                trial_request = self._build_topic_provider_request(
                    [trial_topic],
                    topic_routes=topic_routes,
                    evidence_model=evidence_model,
                    content_layers_model=content_layers_model,
                    batch_index=len(expanded) + 1,
                    content_layers_hash=content_layers_hash,
                )
                trial_budget = profile.estimate_request(
                    self._attach_prompt_authority(
                        f"topic_synthesis:preflight:{len(expanded) + 1}",
                        trial_request,
                    )
                )
                trial_estimate = int(
                    trial_budget.get("estimated_input_tokens")
                    or profile.estimate_tokens(trial_request)
                )
                if current and trial_estimate > input_limit:
                    expanded.append(with_paper_set(topic, current))
                    current = [paper_id]
                else:
                    current = trial_ids
                if len(current) == 1:
                    one_request = self._build_topic_provider_request(
                        [with_paper_set(topic, current)],
                        topic_routes=topic_routes,
                        evidence_model=evidence_model,
                        content_layers_model=content_layers_model,
                        batch_index=len(expanded) + 1,
                        content_layers_hash=content_layers_hash,
                    )
                    one_budget = profile.estimate_request(
                        self._attach_prompt_authority(
                            f"topic_synthesis:preflight:{len(expanded) + 1}",
                            one_request,
                        )
                    )
                    one_estimate = int(
                        one_budget.get("estimated_input_tokens")
                        or profile.estimate_tokens(one_request)
                    )
                    if one_estimate > input_limit:
                        units = self._complete_topic_evidence_units(
                            next(
                                item
                                for item in getattr(evidence_model, "views", []) or []
                                if str(getattr(item, "paper_key", "")) == paper_id
                            ),
                            getattr(content_layers_model, "dossier_by_paper", {}).get(paper_id),
                            fields=topic_fields,
                            chunk_target_tokens=chunk_target_tokens,
                            required_evidence_ids=[
                                value
                                for value in topic.supporting_evidence_ids
                                if str(value)
                                in set(
                                    getattr(
                                        getattr(content_layers_model, "dossier_by_paper", {}).get(paper_id),
                                        "evidence_ids",
                                        [],
                                    )
                                    or []
                                )
                            ],
                            include_unbound_claims=include_unbound_claims,
                        )
                        if len(units) > 1:
                            expanded.extend(
                                with_unit_filter(
                                    topic,
                                    paper_id,
                                    [index],
                                    len(expanded) + index,
                                )
                                for index in range(1, len(units) + 1)
                            )
                            current = []
                            continue
                        raise OutlineV3ExecutionError(
                            f"BLOCKED_BUDGET: topic {topic.topic_id} paper {paper_id} is an indivisible complete evidence unit over the effective input cap"
                        )
            if current:
                expanded.append(with_paper_set(topic, current))
        return expanded

    @staticmethod
    def _estimate_source_summary_excerpts(
        summaries: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, str]]:
        """Keep provider-plan estimates sensitive to source volume.

        Transport requests use the bounded evidence projection above.  The
        stability estimate still needs a bounded representation of the raw
        Stage 1 summary volume; otherwise a large summary can become invisible
        to the cost/context estimate after projection.  Excerpts are used only
        in the in-memory representative estimate, never in the provider
        payload or durable evidence artifacts.
        """

        excerpts: list[dict[str, str]] = []
        for summary in summaries:
            paper_info = summary.get("paper_info")
            core_analysis = summary.get("core_analysis")
            paper_key = ""
            if isinstance(paper_info, Mapping):
                paper_key = str(
                    paper_info.get("canonical_paper_key")
                    or paper_info.get("source_paper_id")
                    or ""
                )
            if not isinstance(core_analysis, Mapping):
                core_analysis = {}
            source_summary = str(core_analysis.get("summary") or "")
            excerpts.append(
                {
                    "paper_key": paper_key,
                    "summary_excerpt": source_summary[:1200],
                }
            )
        return excerpts

    def _effective_input_cap(self, profile: ProviderContextProfile) -> int:
        """The same input ceiling must govern planning and transport."""

        return min(32_000, int(profile.input_budget), int(self.max_source_prompt_tokens or 32_000))

    def _relation_packing_target(self, profile: ProviderContextProfile) -> int:
        """Reserve room for the complete task, schema, and prompt authority.

        Zero in the public technical target means automatic bounded packing.
        A positive technical target may reduce this target, never enlarge it.
        """

        cap = self._effective_input_cap(profile)
        soft_cap = max(1, cap - min(4_096, max(1, cap // 8)))
        requested = int(self.technical_shard_target_tokens or 0)
        return min(soft_cap, requested) if requested > 0 else soft_cap

    def _build_relation_shard_plan(
        self,
        views: Sequence[Any],
        relation_candidates: Sequence[Mapping[str, Any]],
        *,
        profile: ProviderContextProfile | None = None,
    ) -> dict[str, Any]:
        """Plan deterministic, token-aware relation evidence shards.

        This planner is deliberately separate from stability perturbations.
        It preserves every evidence view and records the actual membership and
        estimate used for each shard. A configured target of zero selects
        automatic bounded packing.
        """

        route_profile = profile or self._role_route("relation_adjudication").profile
        target = self._relation_packing_target(route_profile)
        ordered_views = list(views)
        if not ordered_views:
            return {
                "schema_version": "outline-relation-shard-plan-v1",
                "target_tokens": target,
                "shard_count": 0,
                "shards": [],
                "coverage": {"input_view_count": 0, "planned_view_count": 0, "missing_view_hashes": []},
            }

        evidence_chunks = [
            chunk
            for view in ordered_views
            for chunk in self._prompt_evidence_chunks(view)
        ]
        shards: list[dict[str, Any]] = []
        current_chunks: list[dict[str, Any]] = []
        current_estimate = 0

        def estimate(chunk: Mapping[str, Any]) -> int:
            request = {"evidence_views": [dict(chunk)]}
            return max(1, int(route_profile.estimate_tokens(request)))

        def flush() -> None:
            nonlocal current_chunks, current_estimate
            if not current_chunks:
                return
            paper_keys = list(dict.fromkeys(
                str(chunk.get("paper_key") or "")
                for chunk in current_chunks
                if str(chunk.get("paper_key") or "")
            ))
            view_hashes = list(dict.fromkeys(
                str(chunk.get("evidence_source_view_hash") or "")
                for chunk in current_chunks
                if str(chunk.get("evidence_source_view_hash") or "")
            ))
            shards.append(
                {
                    "shard_id": f"relation_shard_{len(shards) + 1}",
                    "paper_keys": paper_keys,
                    "view_hashes": view_hashes,
                    "chunk_ids": [str(chunk.get("evidence_chunk_id") or "") for chunk in current_chunks],
                    "evidence_chunks": [dict(chunk) for chunk in current_chunks],
                    "estimated_input_tokens": current_estimate,
                    "relation_candidate_ids": [],
                }
            )
            current_chunks = []
            current_estimate = 0

        for chunk in evidence_chunks:
            chunk_estimate = estimate(chunk)
            if target > 0 and current_chunks and current_estimate + chunk_estimate > target:
                flush()
            current_chunks.append(chunk)
            current_estimate += chunk_estimate
            if target > 0 and chunk_estimate > target:
                # A single oversized evidence item is retained in its own
                # source-identifiable chunk; it is never silently truncated.
                flush()
        flush()

        assigned_relation_ids: set[str] = set()
        for shard in shards:
            paper_key_set = set(str(value) for value in shard.get("paper_keys") or () if str(value))
            relation_ids: list[str] = []
            for item in relation_candidates:
                if not isinstance(item, Mapping):
                    continue
                relation_id = str(item.get("relation_id") or "").strip()
                relation_keys = set(str(value) for value in (item.get("paper_keys") or ()) if str(value))
                if relation_id and relation_id not in assigned_relation_ids and relation_keys and relation_keys.issubset(paper_key_set):
                    relation_ids.append(relation_id)
                    assigned_relation_ids.add(relation_id)
            shard["relation_candidate_ids"] = relation_ids

        planned_chunk_ids = [
            chunk_id
            for shard in shards
            for chunk_id in shard["chunk_ids"]
            if chunk_id
        ]
        all_chunk_ids = [str(chunk.get("evidence_chunk_id") or "") for chunk in evidence_chunks]
        all_hashes = [
            str(getattr(view, "view_hash", "") or "")
            for view in ordered_views
            if str(getattr(view, "view_hash", "") or "")
        ]
        planned_hashes = [
            view_hash
            for shard in shards
            for view_hash in shard["view_hashes"]
            if view_hash
        ]
        return {
            "schema_version": "outline-relation-shard-plan-v1",
            "target_tokens": target,
            "shard_count": len(shards),
            "shards": shards,
            "coverage": {
                "input_view_count": len(ordered_views),
                "planned_view_count": len(set(planned_hashes)),
                "input_chunk_count": len(evidence_chunks),
                "planned_chunk_count": len(planned_chunk_ids),
                "missing_chunk_ids": sorted(set(all_chunk_ids) - set(planned_chunk_ids)),
                "missing_view_hashes": sorted(set(all_hashes) - set(planned_hashes)),
                "duplicate_view_hashes": sorted(
                    value for value in set(planned_hashes) if planned_hashes.count(value) > 1
                ),
                "unassigned_relation_candidate_ids": sorted(
                    str(item.get("relation_id") or "")
                    for item in relation_candidates
                    if isinstance(item, Mapping)
                    and str(item.get("relation_id") or "") not in assigned_relation_ids
                ),
            },
        }

    def _semantic_node_id(self, node_id: str) -> str:
        """Return the deterministic replay identity for one concrete call."""

        # Stability variants are separate provider calls.  Their audit hash is
        # part of the identity; collapsing it would make different variant
        # prompts share one receipt contract and falsely block closure.
        return str(node_id or "")

    def _provider_call_id(self, node_id: str) -> str:
        return f"outline:{self._semantic_node_id(node_id)}"

    def _replay_binding_hash(self, binding: Mapping[str, Any]) -> str:
        """Hash the semantic binding, not the concrete stability audit node."""

        semantic = self._semantic_node_id(str(binding.get("node_id") or ""))
        replay_binding = dict(binding)
        replay_binding["node_id"] = semantic
        replay_binding["semantic_node_id"] = semantic
        return hash_json(replay_binding)

    def _context_profile_hash(self, route: OutlineRoleRoute | None = None) -> str:
        """Hash either one node route or the stage-wide routing manifest.

        Provider-node bindings use the first form so changing an unrelated
        critic route does not invalidate every earlier node. The constructor's
        stage closure uses the second form to retain a complete routing-manifest
        identity for the whole outline attempt.
        """

        if route is not None:
            return _hash_payload({
                "route": self._route_api_identity(route),
                "profile": self._profile_identity(route.profile),
            })
        route_identities = {
            role: {
                "identity": list(route.binding_identity),
                "config_fingerprint": route.safe_config_fingerprint(),
            }
            for role, route in (self.router.routes.items() if self.router is not None else ())
        }
        return _hash_payload({
            "executor_profile": self._profile_identity(self.profile),
            "role_routes": route_identities,
        })

    def _stability_variant_plan(
        self,
    ) -> list[tuple[str, list[dict[str, Any]], list[str], dict[str, Any]]]:
        """Return the explicitly selected stability perturbations.

        ``smoke`` is the default and runs one additional full reversed-summary
        decision chain plus exact replay. ``full`` retains the comprehensive
        perturbation matrix; ``off`` records a disabled audit and performs no
        stability provider work.
        """

        if self.stability_mode == "off":
            return []
        midpoint = max(1, len(self.summaries) // 2)
        normal_candidate_order = [
            f"candidate_{index}" for index in range(1, self.candidate_count + 1)
        ]
        smoke = [
            (
                "baseline",
                list(self.summaries),
                normal_candidate_order,
                {
                    "summary_order": "original",
                    "shard_size": len(self.summaries),
                    "resume": "primary_baseline_reuse",
                },
            ),
            (
                "summary_order_reversed",
                list(reversed(self.summaries)),
                normal_candidate_order,
                {
                    "summary_order": "reversed",
                    "shard_size": len(self.summaries),
                },
            ),
        ]
        if self.stability_mode == "smoke":
            return smoke
        return [
            *smoke[:2],
            (
                "summary_order_rotated",
                list(self.summaries[midpoint:]) + list(self.summaries[:midpoint]),
                normal_candidate_order,
                {"summary_order": "rotated", "shard_size": len(self.summaries)},
            ),
            (
                "shard_order_permuted",
                list(self.summaries[::2]) + list(self.summaries[1::2]),
                normal_candidate_order,
                {"summary_order": "even_odd_shards", "shard_size": max(1, len(self.summaries) // 2), "shard_order": "permuted"},
            ),
            (
                "alternative_shard_size",
                list(self.summaries),
                normal_candidate_order,
                {"summary_order": "original", "shard_size": 1, "shard_order": "canonical"},
            ),
            (
                "candidate_execution_order_permuted",
                list(self.summaries),
                list(reversed(normal_candidate_order)),
                {"summary_order": "original", "candidate_execution_order": "reversed", "shard_size": len(self.summaries)},
            ),
            (
                "exact_replay_resume",
                list(self.summaries),
                normal_candidate_order,
                {"summary_order": "original", "resume": "exact_replay", "shard_size": len(self.summaries)},
            ),
        ]

    def _provider_call_plan_variants(
        self,
    ) -> list[tuple[str, Sequence[Mapping[str, Any]], bool]]:
        """Return plan rows for transport and zero-transport replay work."""

        if self.stability_mode == "off":
            return [("canonical", self.summaries, True)]
        variants = self._stability_variant_plan()
        planned: list[tuple[str, Sequence[Mapping[str, Any]], bool]] = []
        for name, summaries, _order, definition in variants:
            resume_kind = str(definition.get("resume") or "")
            if name == "baseline":
                planned.append((name, summaries, True))
            elif resume_kind == "exact_replay":
                planned.append((name, summaries, False))
            elif resume_kind != "primary_baseline_reuse":
                planned.append((name, summaries, True))
        if not any(name == "exact_replay_resume" for name, _summaries, _transport in planned):
            planned.append(("exact_replay_resume", self.summaries, False))
        return planned

    def _relation_provider_request(
        self,
        *,
        relation_candidates: Sequence[Mapping[str, Any]],
        content_layers: Any,
        semantic_plan: Any,
        shard_plan: Mapping[str, Any],
    ) -> tuple[dict[str, Any], list[dict[str, Any]], list[str]]:
        """Build the same compact relation request for preflight and execution."""

        all_candidate_by_id = {
            str(item["relation_id"]): item for item in relation_candidates
        }
        selection = semantic_plan.coverage.get("selected_relation_ids")
        # An absent selection field is an old-plan compatibility case. An
        # explicit empty list is a real decision to send no relation work.
        selected_source = all_candidate_by_id if selection is None else selection
        selected_ids = {str(item) for item in selected_source if str(item)}
        selected = [
            dict(item) for item in relation_candidates
            if str(item.get("relation_id") or "") in selected_ids
        ]
        excluded_ids = sorted(set(all_candidate_by_id) - selected_ids)
        request = {
            "relation_candidates": [self._compact_relation_candidate(item) for item in selected],
            "evidence_views": [],
            "evidence_projection": {
                "projection": "complete_relation_bundle_plus_evidence_refs_v1",
                "full_evidence_views_are_registry_local": True,
                "content_layers_hash": content_layers.content_hash,
            },
            "relation_shard_plan": {
                "schema_version": str(shard_plan.get("schema_version") or ""),
                "target_tokens": int(shard_plan.get("target_tokens") or 0),
                "shard_count": int(shard_plan.get("shard_count") or 0),
                "coverage": {"compact_provider_projection": True},
                "provider_projection": "compact_relation_bundle_v1",
            },
            "navigation_cards": [],
            "content_layer_refs": {
                "artifact_type": "outline_content_layers",
                "artifact_hash": content_layers.content_hash,
                "dossier_count": len(content_layers.dossiers),
            },
            "relation_evidence_bundles": [
                self._compact_relation_bundle(item)
                for item in semantic_plan.relation_bundles
                if item.relation_id in selected_ids
            ],
            "excluded_relation_count": len(excluded_ids),
            "excluded_relation_ids_hash": hash_json(excluded_ids),
            "semantic_chunk_plan_hash": semantic_plan.content_hash,
            "relation_adjudication_contract": {
                "must_return_confirmed_relation_ids": True,
                "must_reject_without_recorded_evidence": True,
                "allowed_relation_ids": [item["relation_id"] for item in selected],
                "output_fields": {
                    "confirmed_relation_ids": (
                        "array of relation_id strings; may be empty only when "
                        "every candidate is rejected"
                    ),
                    "rejected_relations": (
                        "array of objects, each with relation_id and a "
                        "recorded evidence reason; may be empty only when "
                        "every candidate is confirmed"
                    ),
                },
                "reject_every_unconfirmed_candidate": True,
            },
        }
        return request, selected, excluded_ids

    @staticmethod
    def _relation_scope_value(value: Any) -> Any:
        """Canonicalize unordered relation-bundle members without dropping text."""

        if isinstance(value, Mapping):
            return {
                str(key): OutlineV3Executor._relation_scope_value(item)
                for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
            }
        if isinstance(value, (list, tuple, set, frozenset)):
            members = [OutlineV3Executor._relation_scope_value(item) for item in value]
            return sorted(members, key=hash_json)
        return value

    @staticmethod
    def _stability_fact_inventory(
        sections: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        """Compare supported facts, retaining exact text when semantics are untyped."""

        fact_hashes: list[str] = []
        untyped_claim_count = 0
        for section in sections:
            paper_keys = sorted(str(item) for item in section.get("paper_keys") or () if str(item))
            relation_ids = sorted(str(item) for item in section.get("relation_ids") or () if str(item))
            support_rows = [
                row for row in section.get("claim_support") or ()
                if isinstance(row, Mapping)
            ]
            for raw_claim in section.get("claims") or ():
                claim = unicodedata.normalize("NFC", str(raw_claim).strip())
                if not claim:
                    continue
                matching = [
                    row for row in support_rows
                    if str(row.get("claim") or "").strip() == claim
                ]
                if not matching:
                    untyped_claim_count += 1
                    fact_hashes.append(hash_json({
                        "claim_text": claim,
                        "paper_keys": paper_keys,
                        "relation_ids": relation_ids,
                        "support_status": "untyped",
                    }))
                    continue
                for row in matching:
                    identity, typed_semantics = OutlineV3Executor._stability_typed_claim_identity(
                        row, section
                    )
                    if not typed_semantics:
                        untyped_claim_count += 1
                    fact_hashes.append(hash_json({
                        "claim_text_when_untyped": "" if typed_semantics else claim,
                        **identity,
                    }))
        return {
            "fact_hashes": sorted(fact_hashes),
            "untyped_claim_count": untyped_claim_count,
        }

    @staticmethod
    def _stability_typed_claim_identity(
        row: Mapping[str, Any],
        section: Mapping[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        """Return the typed evidence identity used for stability comparisons."""

        direction = str(row.get("effect_direction") or row.get("direction") or "").strip().casefold()
        claim_kind = str(row.get("claim_kind") or "").strip().casefold()
        source_claim_ids = sorted(
            str(item) for item in row.get("source_claim_ids") or () if str(item)
        )
        evidence_ids = sorted(
            str(item) for item in row.get("evidence_ids") or () if str(item)
        )
        identity = {
            "paper_key": str(row.get("paper_key") or ""),
            "study_id": str(row.get("study_id") or ""),
            "source_claim_ids": source_claim_ids,
            "evidence_ids": evidence_ids,
            "source_field_ids": sorted(
                str(item) for item in row.get("source_field_ids") or () if str(item)
            ),
            "condition_ids": sorted(
                str(item) for item in (
                    row.get("condition_ids") or row.get("dependency_ids") or ()
                ) if str(item)
            ),
            "direction": direction,
            "claim_kind": claim_kind,
            "paper_keys": sorted(
                str(item) for item in section.get("paper_keys") or () if str(item)
            ),
            "relation_ids": sorted(
                str(item) for item in section.get("relation_ids") or () if str(item)
            ),
        }
        typed = bool(
            identity["paper_key"]
            and source_claim_ids
            and evidence_ids
            and direction
            and claim_kind
        )
        return identity, typed

    @staticmethod
    def _stability_claim_text_key(value: str) -> str:
        """Ignore sentence punctuation without erasing numeric meaning."""

        normalized = unicodedata.normalize("NFC", str(value).strip()).casefold()
        characters = list(normalized)
        previous_nonspace = [""] * len(characters)
        next_nonspace = [""] * len(characters)
        neighbor = ""
        for index, character in enumerate(characters):
            previous_nonspace[index] = neighbor
            if not character.isspace():
                neighbor = character
        neighbor = ""
        for index in range(len(characters) - 1, -1, -1):
            next_nonspace[index] = neighbor
            if not characters[index].isspace():
                neighbor = characters[index]
        key: list[str] = []
        for index, character in enumerate(characters):
            if character.isspace():
                if key and key[-1] != " ":
                    key.append(" ")
                continue
            if not unicodedata.category(character).startswith("P"):
                key.append(character)
                continue
            previous = previous_nonspace[index]
            following = next_nonspace[index]
            numeric_separator = previous.isdigit() and following.isdigit()
            signed_number = character in {"+", "-"} and following.isdigit()
            percent_marker = character in {"%", "‰"} and previous.isdigit()
            decimal_leading_zero = character == "." and following.isdigit() and (
                not previous or previous in "<>≤≥=~"
            )
            if numeric_separator or signed_number or percent_marker or decimal_leading_zero:
                key.append(character)
            elif key and key[-1] != " ":
                # Treat ordinary punctuation as a word boundary instead of
                # deleting it, so punctuation cannot join distinct tokens.
                key.append(" ")
        return " ".join("".join(key).split())

    @classmethod
    def _stability_claim_review_material(
        cls,
        baseline_sections: Sequence[Mapping[str, Any]],
        variant_sections: Sequence[Mapping[str, Any]],
        *,
        candidate_id: str,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Build bounded references for typed claims whose wording materially changed.

        Claim prose is intentionally not part of the typed fact hash because
        ordinary paraphrases must remain comparable. Materially changed prose
        is sent to the already-planned evidence critic with its exact typed
        provenance, avoiding a new provider call.
        """

        def collect(
            sections: Sequence[Mapping[str, Any]],
        ) -> dict[str, dict[str, Any]]:
            facts: dict[str, dict[str, Any]] = {}
            for section_index, section in enumerate(sections):
                section_id = str(section.get("section_id") or "")
                stable_section_id = re.sub(
                    r"^candidate_[^_]+_",
                    "candidate_",
                    section_id,
                    flags=re.IGNORECASE,
                )
                claims = [
                    unicodedata.normalize("NFC", str(item).strip())
                    for item in section.get("claims") or ()
                    if str(item).strip()
                ]
                support_rows = [
                    row for row in section.get("claim_support") or ()
                    if isinstance(row, Mapping)
                ]
                for claim_index, claim in enumerate(claims):
                    matching = [
                        row for row in support_rows
                        if str(row.get("claim") or "").strip() == claim
                    ]
                    if not matching:
                        continue
                    identities: list[dict[str, Any]] = []
                    for row in matching:
                        identity, typed = cls._stability_typed_claim_identity(row, section)
                        if not typed:
                            identities = []
                            break
                        identities.append(identity)
                    # Untyped claims keep their exact text in the existing
                    # fact hash and therefore already fail closed on drift.
                    if not identities:
                        continue
                    support_identity = {
                        "section_id": stable_section_id,
                        "section_index": section_index,
                        "support_rows": sorted(identities, key=hash_json),
                    }
                    fact_id = hash_json(support_identity)[:24]
                    record = facts.setdefault(
                        fact_id,
                        {"fact_id": fact_id, "support": support_identity, "claims": {}},
                    )
                    text_key = cls._stability_claim_text_key(claim)
                    if text_key:
                        record["claims"].setdefault(text_key, []).append({
                            "claim": claim,
                            "section_id": section_id,
                            "claim_index": claim_index,
                        })
            return facts

        baseline_facts = collect(baseline_sections)
        variant_facts = collect(variant_sections)
        catalog: list[dict[str, Any]] = []
        comparisons: list[dict[str, Any]] = []
        for fact_id in sorted(set(baseline_facts) & set(variant_facts)):
            baseline_fact = baseline_facts[fact_id]
            variant_fact = variant_facts[fact_id]
            baseline_claims = baseline_fact["claims"]
            variant_claims = variant_fact["claims"]
            if set(baseline_claims) == set(variant_claims):
                continue
            catalog.append({
                "fact_id": fact_id,
                "support": baseline_fact["support"],
                "claims": [
                    {
                        "claim": row["claim"],
                        "section_id": row["section_id"],
                        "claim_index": row["claim_index"],
                    }
                    for key in sorted(baseline_claims)
                    for row in sorted(
                        baseline_claims[key],
                        key=lambda item: (item["section_id"], item["claim_index"]),
                    )
                ],
            })
            pair_id = hash_json({
                "candidate_id": candidate_id,
                "fact_id": fact_id,
                "baseline_text_keys": sorted(baseline_claims),
                "variant_text_keys": sorted(variant_claims),
            })[:24]
            comparisons.append({
                "pair_id": pair_id,
                "fact_id": fact_id,
                "variant_claim_refs": [
                    {
                        "section_id": row["section_id"],
                        "claim_index": row["claim_index"],
                    }
                    for key in sorted(variant_claims)
                    for row in sorted(
                        variant_claims[key],
                        key=lambda item: (item["section_id"], item["claim_index"]),
                    )
                ],
                "evidence_refs": sorted({
                    str(evidence_id)
                    for identity in baseline_fact["support"]["support_rows"]
                    for evidence_id in identity["evidence_ids"]
                    if str(evidence_id)
                }),
            })
        return catalog, comparisons

    @staticmethod
    def _enforce_stability_claim_reviews(
        critique: dict[str, Any],
        *,
        comparisons: Mapping[str, Sequence[Mapping[str, Any]]],
        candidate_hashes: Mapping[str, str],
    ) -> dict[str, Any]:
        """Turn missing, malformed, or non-equivalent pair reviews into blockers."""

        expected_by_pair = {
            str(pair.get("pair_id") or ""): (candidate_id, pair)
            for candidate_id, pairs in comparisons.items()
            for pair in pairs
            if str(pair.get("pair_id") or "")
        }
        if not expected_by_pair:
            return {"status": "not_required", "pair_count": 0, "decisions": {}}

        raw_reviews = critique.get("stability_claim_reviews")
        rows = (
            list(raw_reviews)
            if isinstance(raw_reviews, Sequence) and not isinstance(raw_reviews, (str, bytes))
            else []
        )
        if "stability_claim_reviews" not in critique:
            rows = [
                row
                for shard in (critique.get("candidate_shard_results") or {}).values()
                if isinstance(shard, Mapping)
                for row in shard.get("stability_claim_reviews") or ()
                if isinstance(row, Mapping)
            ] if isinstance(critique.get("candidate_shard_results"), Mapping) else []

        reviews_by_pair: dict[str, list[Mapping[str, Any]]] = {}
        unknown_candidates: set[str] = set()
        for row in rows:
            if not isinstance(row, Mapping):
                unknown_candidates.update(comparisons)
                continue
            pair_id = str(row.get("pair_id") or "")
            candidate_id = str(row.get("candidate_id") or "")
            if pair_id not in expected_by_pair:
                if candidate_id in candidate_hashes:
                    unknown_candidates.add(candidate_id)
                else:
                    unknown_candidates.update(comparisons)
                continue
            expected_candidate, _pair = expected_by_pair[pair_id]
            if candidate_id != expected_candidate:
                unknown_candidates.add(expected_candidate)
                continue
            reviews_by_pair.setdefault(pair_id, []).append(row)

        issue_rows = critique.get("issues")
        if "issues" in critique and (
            not isinstance(issue_rows, Sequence)
            or isinstance(issue_rows, (str, bytes))
        ):
            # Preserve malformed provider data for the shared disposition
            # parser, which emits a global fail-closed blocker for it.
            critique["passed"] = False
            return {
                "status": "malformed_critique_issues",
                "pair_count": len(expected_by_pair),
                "decisions": {},
                "blocked_candidate_ids": sorted(set(comparisons)),
            }
        issues = list(issue_rows) if isinstance(issue_rows, Sequence) else []
        decisions: dict[str, str] = {}
        blocked_candidates = set(unknown_candidates)
        for pair_id, (candidate_id, pair) in expected_by_pair.items():
            matches = reviews_by_pair.get(pair_id, [])
            reason = ""
            if len(matches) != 1:
                reason = "missing or duplicate pair review"
            else:
                review = matches[0]
                decision = str(review.get("decision") or "").casefold()
                if decision in {"equivalent", "material_change", "uncertain"}:
                    decisions[pair_id] = decision
                evidence_refs = review.get("evidence_refs")
                valid_refs = (
                    isinstance(evidence_refs, Sequence)
                    and not isinstance(evidence_refs, (str, bytes))
                    and bool(evidence_refs)
                    and all(isinstance(item, str) and item for item in evidence_refs)
                    and len(evidence_refs) == len(set(evidence_refs))
                    and set(evidence_refs).issubset(set(pair.get("evidence_refs") or ()))
                )
                if decision != "equivalent":
                    reason = "review did not establish equivalent meaning"
                elif not valid_refs:
                    reason = "equivalence review lacks in-scope evidence references"
                elif not str(review.get("rationale") or "").strip():
                    reason = "equivalence review lacks a rationale"
            if reason:
                blocked_candidates.add(candidate_id)
                issues.append({
                    "issue_id": f"stability-claim-review:{pair_id}",
                    "scope": "candidate",
                    "target_ids": [candidate_id],
                    "severity": "blocking",
                    "evidence_refs": list(pair.get("evidence_refs") or ()),
                    "resolution_status": "unresolved",
                    "parent_candidate_hash": str(candidate_hashes.get(candidate_id) or ""),
                    "candidate_id": candidate_id,
                    "source": "typed",
                    "message": reason,
                })
        for candidate_id in sorted(unknown_candidates):
            pair_id = hash_json({"unknown_stability_claim_review": candidate_id})[:24]
            issues.append({
                "issue_id": f"stability-claim-review:{pair_id}",
                "scope": "candidate",
                "target_ids": [candidate_id],
                "severity": "blocking",
                "evidence_refs": [],
                "resolution_status": "unresolved",
                "parent_candidate_hash": str(candidate_hashes.get(candidate_id) or ""),
                "candidate_id": candidate_id,
                "source": "typed",
                "message": "unexpected stability claim review identity",
            })
        critique["issues"] = issues
        candidate_statuses = {
            str(candidate_id): (
                "blocked" if str(candidate_id) in blocked_candidates else "equivalent"
            )
            for candidate_id, pairs in comparisons.items()
            if pairs
        }
        return {
            "status": "candidate_blocks" if blocked_candidates else "equivalent",
            "pair_count": len(expected_by_pair),
            "decisions": decisions,
            "blocked_candidate_ids": sorted(blocked_candidates),
            "candidate_statuses": candidate_statuses,
        }

    def _canonical_stability_relation_scope(self) -> dict[str, Any]:
        """Freeze the canonical relation task before any stability transport."""

        if self._frozen_stability_relation_scope is not None:
            return dict(self._frozen_stability_relation_scope)
        evidence = build_outline_evidence_views(self.summaries, self.job_id)
        ledger = build_global_corpus_ledger(evidence)
        matrix = build_multi_view_matrix(evidence)
        candidates = build_global_relation_map(evidence, matrix, ledger)
        layers = build_paper_content_layers(self.summaries, evidence, job_id=self.job_id)
        plan = build_semantic_chunk_plan(
            layers,
            candidates,
            candidate_count=self.candidate_count,
            physical_call_limit=authorized_provider_call_limit(self.max_provider_calls),
        )
        candidate_ids = {item.relation_id for item in candidates.relations}
        raw_selection = plan.coverage.get("selected_relation_ids")
        selected_ids = sorted(candidate_ids if raw_selection is None else {
            str(item) for item in raw_selection if str(item)
        })
        if not set(selected_ids).issubset(candidate_ids):
            raise OutlineV3ExecutionError("canonical relation selection contains an unknown ID")
        bundles = {item.relation_id: item for item in plan.relation_bundles}
        if not set(selected_ids).issubset(bundles):
            raise OutlineV3ExecutionError("canonical relation selection lacks a source-bearing bundle")
        selected_candidate_rows = sorted(
            (
                self._relation_scope_value(item.to_dict())
                for item in candidates.relations
                if item.relation_id in set(selected_ids)
            ),
            key=hash_json,
        )
        scope = {
            "schema_version": "same-task-selected-relations/v2",
            "selected_relation_ids": selected_ids,
            "source_summary_hashes": sorted(evidence.source_summary_hashes),
            "selected_relation_candidates_hash": hash_json(selected_candidate_rows),
            "content_layers_hash": layers.content_hash,
            "semantic_chunk_plan_hash": plan.content_hash,
            "selected_bundle_hashes": {
                item: hash_json(self._relation_scope_value(bundles[item].to_dict()))
                for item in selected_ids
            },
            "review_intent_hash": self._review_intent_hash,
            "quality_gate_hash": self.quality_gate.content_hash,
            "candidate_count": self.candidate_count,
            "relation_route_hash": hash_json(self._role_route("relation_adjudication").binding_identity),
        }
        scope["scope_hash"] = hash_json(scope)
        self._frozen_stability_relation_scope = scope
        return dict(scope)

    def _apply_stability_relation_scope(
        self,
        *,
        evidence: Any,
        relation_candidates: Sequence[Mapping[str, Any]],
        semantic_plan: Any,
    ) -> Any:
        """Use the same selected task for a variant or reject incomparability."""

        scope = self._canonical_stability_relation_scope()
        if sorted(evidence.source_summary_hashes) != scope["source_summary_hashes"]:
            raise OutlineV3ExecutionError("stability variant changed the source-summary set")
        available = {
            str(item.get("relation_id") or "")
            for item in relation_candidates
            if isinstance(item, Mapping)
        }
        selected = set(scope["selected_relation_ids"])
        if not selected.issubset(available):
            raise OutlineV3ExecutionError(
                "stability variant is missing a frozen selected relation ID"
            )
        selected_candidate_rows = sorted(
            (
                self._relation_scope_value(dict(item))
                for item in relation_candidates
                if str(item.get("relation_id") or "") in selected
            ),
            key=hash_json,
        )
        if hash_json(selected_candidate_rows) != scope[
            "selected_relation_candidates_hash"
        ]:
            raise OutlineV3ExecutionError(
                "stability variant changed selected relation candidate content"
            )
        bundles = {item.relation_id: item for item in semantic_plan.relation_bundles}
        if not selected.issubset(bundles):
            raise OutlineV3ExecutionError(
                "stability variant is missing a selected source-bearing bundle"
            )
        for relation_id in selected:
            actual_hash = hash_json(self._relation_scope_value(bundles[relation_id].to_dict()))
            if actual_hash != scope["selected_bundle_hashes"][relation_id]:
                raise OutlineV3ExecutionError(
                    f"stability variant changed the evidence closure for {relation_id}"
                )
        return replace(
            semantic_plan,
            coverage={
                **dict(semantic_plan.coverage),
                "selected_relation_ids": list(scope["selected_relation_ids"]),
            },
        )

    def _stability_relation_compact_request(
        self,
        *,
        relation_candidates: Sequence[Mapping[str, Any]],
        content_layers: Any,
        semantic_plan: Any,
        shard_plan: Mapping[str, Any],
        evidence_views: Sequence[Any],
        variant_name: str,
        shard_size: int,
        shard_order: str,
        relation_scope: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Materialize the selected relation task under a declared perturbation."""

        request, selected, _excluded = self._relation_provider_request(
            relation_candidates=relation_candidates,
            content_layers=content_layers,
            semantic_plan=semantic_plan,
            shard_plan=shard_plan,
        )
        selected_ids = sorted(str(item["relation_id"]) for item in selected)
        if relation_scope is not None and selected_ids != sorted(
            str(item) for item in relation_scope.get("selected_relation_ids") or ()
        ):
            raise OutlineV3ExecutionError(
                "stability relation request differs from the frozen selected scope"
            )
        selected_candidate_rows = sorted(
            (
                self._relation_scope_value(dict(item))
                for item in selected
            ),
            key=hash_json,
        )
        scope_hash = str((relation_scope or {}).get("scope_hash") or "") or hash_json({
            "schema_version": "same-task-selected-relations/v2",
            "selected_relation_ids": selected_ids,
            "selected_relation_candidates_hash": hash_json(selected_candidate_rows),
            "semantic_chunk_plan_hash": semantic_plan.content_hash,
        })
        request["stability_variant"] = {
            "name": variant_name,
            "shard_size": shard_size,
            "shard_order": shard_order,
            "source_view_hashes": [str(view.view_hash) for view in evidence_views],
            "scope_schema_version": "same-task-selected-relations/v2",
            "selected_relation_ids": selected_ids,
            "scope_hash": scope_hash,
        }
        return request

    def _relation_compact_batch_requests(
        self,
        *,
        base_request: Mapping[str, Any],
        relation_ids: Sequence[str],
        profile: ProviderContextProfile,
        node_prefix: str = "",
    ) -> list[tuple[str, dict[str, Any], set[str]]]:
        """Pack complete relation bundles, not every chunk of their papers.

        The flat request already exposes each relation's two-sided findings,
        definitions, conditions, and source locators. A batch is a partition
        of those same source-bearing bundles; no relation is decided twice.
        """

        candidate_by_id = {
            str(item.get("relation_id") or ""): dict(item)
            for item in base_request.get("relation_candidates") or ()
            if isinstance(item, Mapping) and str(item.get("relation_id") or "")
        }
        bundle_by_id = {
            str(item.get("relation_id") or ""): dict(item)
            for item in base_request.get("relation_evidence_bundles") or ()
            if isinstance(item, Mapping) and str(item.get("relation_id") or "")
        }
        ordered_ids = list(dict.fromkeys(str(item) for item in relation_ids if str(item)))
        if not ordered_ids:
            return []
        missing = [item for item in ordered_ids if item not in candidate_by_id or item not in bundle_by_id]
        if missing:
            raise OutlineV3ExecutionError(
                f"relation batch is missing source-bearing candidate or bundle for {missing}"
            )
        cap = self._effective_input_cap(profile)
        target = self._relation_packing_target(profile)

        def materialize(batch_ids: Sequence[str], index: int) -> tuple[str, dict[str, Any], int]:
            node_id = f"relation_adjudication:batch_{index}"
            if node_prefix:
                node_id = f"{node_prefix}:{node_id}"
            request = dict(base_request)
            request["relation_candidates"] = [candidate_by_id[item] for item in batch_ids]
            request["relation_evidence_bundles"] = [bundle_by_id[item] for item in batch_ids]
            request["relation_batch"] = {
                "schema_version": "relation-atomic-batch/v1",
                "batch_index": index,
                "relation_ids": list(batch_ids),
            }
            contract = dict(base_request.get("relation_adjudication_contract") or {})
            contract["allowed_relation_ids"] = list(batch_ids)
            request["relation_adjudication_contract"] = contract
            enriched = self._attach_prompt_authority(node_id, request)
            estimate = int(profile.estimate_request(enriched).get("estimated_input_tokens") or 0)
            return node_id, request, estimate

        batches: list[tuple[str, dict[str, Any], set[str]]] = []
        current: list[str] = []
        for relation_id in ordered_ids:
            trial = [*current, relation_id]
            _node_id, _request, estimate = materialize(trial, len(batches) + 1)
            if current and estimate > target:
                node_id, request, _ = materialize(current, len(batches) + 1)
                batches.append((node_id, request, set(current)))
                current = [relation_id]
                _node_id, _request, estimate = materialize(current, len(batches) + 1)
            else:
                current = trial
            if estimate > cap:
                raise OutlineV3ExecutionError(
                    f"BLOCKED_BUDGET: relation {relation_id} has an indivisible complete "
                    f"relation request ({estimate}/{cap} input tokens)"
                )
        if current:
            node_id, request, _ = materialize(current, len(batches) + 1)
            batches.append((node_id, request, set(current)))
        return batches

    def _stability_flat_relation_request(
        self,
        *,
        relation_candidates: Sequence[Mapping[str, Any]],
        evidence_views: Sequence[Any],
        source_summary_hashes: Sequence[str],
        evidence_shards: Sequence[Mapping[str, Any]],
        content_layers: Any,
        semantic_plan: Any,
        shard_size: int,
        shard_order: str,
    ) -> dict[str, Any]:
        """Build the exact unsharded stability relation transport payload."""

        return {
            "relation_candidates": [dict(item) for item in relation_candidates],
            "evidence": self._compact_candidate_evidence_refs(
                evidence_views, content_layers, semantic_plan,
            ),
            "source_summary_hashes": sorted(source_summary_hashes),
            "evidence_shards": [dict(item) for item in evidence_shards],
            "shard_size": shard_size,
            "shard_order": shard_order,
        }

    def _relation_hierarchical_preflight(
        self,
        summaries: Sequence[Mapping[str, Any]],
        profile: ProviderContextProfile,
        *,
        variant_name: str,
    ) -> tuple[int, int, dict[str, Any]]:
        """Estimate the compact evidence-bundle relation request.

        The full relation shard planner remains available for explicit
        low-target callers and regression tests. Production R1 uses complete
        relation bundles plus Registry-bound evidence references, so the
        normal 63-paper request is one bounded provider call.
        """

        stability_variant = variant_name not in {"canonical", "baseline"}
        evidence = build_outline_evidence_views(summaries, self.job_id)
        shard_size = len(summaries) or 1
        shard_order = "canonical"
        if stability_variant:
            definition = next(
                (
                    item[3] for item in self._stability_variant_plan()
                    if item[0] == variant_name
                ),
                {},
            )
            shard_size = int(definition.get("shard_size") or shard_size)
            shard_order = str(definition.get("shard_order") or "canonical")
            variant_shards = shard_outline_evidence_views(evidence, max(1, shard_size))
            if shard_order == "permuted":
                variant_shards = list(reversed(variant_shards))
            evidence = merge_outline_evidence_shards(variant_shards)
        ledger = build_global_corpus_ledger(evidence)
        matrix = build_multi_view_matrix(evidence)
        candidates = build_global_relation_map(evidence, matrix, ledger)
        relation_candidates = [item.to_dict() for item in candidates.relations]
        plan = self._build_relation_shard_plan(evidence.views, relation_candidates)
        content_layers = build_paper_content_layers(
            summaries,
            evidence,
            job_id=self.job_id,
        )
        semantic_plan = build_semantic_chunk_plan(
            content_layers,
            candidates,
            candidate_count=self.candidate_count,
            physical_call_limit=authorized_provider_call_limit(self.max_provider_calls),
        )
        relation_scope = None
        if stability_variant:
            semantic_plan = self._apply_stability_relation_scope(
                evidence=evidence,
                relation_candidates=relation_candidates,
                semantic_plan=semantic_plan,
            )
            relation_scope = self._canonical_stability_relation_scope()
        request, selected, _excluded = self._relation_provider_request(
            relation_candidates=relation_candidates,
            content_layers=content_layers,
            semantic_plan=semantic_plan,
            shard_plan=plan,
        )
        if stability_variant:
            request = self._stability_relation_compact_request(
                relation_candidates=relation_candidates,
                content_layers=content_layers,
                semantic_plan=semantic_plan,
                shard_plan=plan,
                evidence_views=evidence.views,
                variant_name=variant_name,
                shard_size=shard_size,
                shard_order=shard_order,
                relation_scope=relation_scope,
            )
            node_id = (
                "stability:"
                + hash_json({"variant": variant_name, "role": "relation_adjudication"})[:16]
                + ":relation_adjudication"
            )
        else:
            node_id = "relation_adjudication"
        flat_request = request
        if not selected:
            return 0, 0, {**plan, "hierarchical_needed": False, "selection_status": "empty"}
        exact_request = self._attach_prompt_authority(node_id, flat_request)
        exact_budget = profile.estimate_request(exact_request)
        exact_estimate = max(1, int(exact_budget.get("estimated_input_tokens") or 0))
        effective_cap = self._effective_input_cap(profile)
        target = self._relation_packing_target(profile)
        hierarchical = (
            exact_estimate > target
            or exact_estimate > effective_cap
            or not bool(exact_budget.get("within_budget"))
            or (stability_variant and int(plan.get("shard_count") or 0) > 1)
        )
        if not hierarchical:
            return exact_estimate, 1, {**plan, "hierarchical_needed": False}

        selected_by_id = {str(item["relation_id"]): item for item in selected}
        relation_bundles = {
            item.relation_id: item.to_dict()
            for item in semantic_plan.relation_bundles
            if item.relation_id in selected_by_id
        }
        relation_contract = (
            {
                "allowed_relation_ids": sorted(selected_by_id),
                "must_return_confirmed_relation_ids": True,
                "must_reject_without_recorded_evidence": True,
            }
            if stability_variant else request["relation_adjudication_contract"]
        )
        node_prefix = (
            "stability:" + hash_json({"variant": variant_name, "role": "relation_adjudication"})[:16]
            if stability_variant else ""
        )
        compact_ids = {
            str(item.get("relation_id") or "")
            for item in request.get("relation_evidence_bundles") or ()
            if isinstance(item, Mapping)
        }
        atomic_batches = set(selected_by_id).issubset(compact_ids)
        if "relation_evidence_bundles" in request and not atomic_batches:
            raise OutlineV3ExecutionError(
                "relation adjudication is missing a selected source-bearing evidence bundle"
            )
        if atomic_batches:
            local_requests = []
            cross_requests = self._relation_compact_batch_requests(
                base_request=request,
                relation_ids=list(selected_by_id),
                profile=profile,
                node_prefix=node_prefix,
            )
        else:
            local_requests = self._relation_local_requests(
                evidence_views=evidence.views,
                candidate_by_id=selected_by_id,
                shard_plan=plan,
                relation_contract=relation_contract,
                relation_bundles=relation_bundles,
                node_prefix=node_prefix,
            )
            local_ids: set[str] = set()
            for row in local_requests:
                local_ids.update(row[3])
            cross_requests = self._relation_cross_batch_requests(
                candidate_by_id=selected_by_id,
                relation_ids=sorted(set(selected_by_id) - local_ids),
                shard_plan=plan,
                relation_contract=relation_contract,
                profile=profile,
                relation_bundles=relation_bundles,
                node_prefix=node_prefix,
            )
        local_calls = len(local_requests)
        cross_call_upper = len(cross_requests)
        call_upper = max(1, local_calls + cross_call_upper)
        if self.max_provider_calls is None or call_upper <= self.max_provider_calls:
            requests_to_check = [(row[0], row[4]) for row in local_requests]
            requests_to_check.extend((row[0], row[1]) for row in cross_requests)
            for node_id, actual_request in requests_to_check:
                enriched_request = self._attach_prompt_authority(node_id, actual_request)
                actual_estimate = int(profile.estimate_request(enriched_request).get("estimated_input_tokens") or 0)
                if actual_estimate > effective_cap:
                    raise OutlineV3ExecutionError(
                        f"BLOCKED_BUDGET: {node_id} complete relation request "
                        f"estimate {actual_estimate} exceeds effective input cap {effective_cap}"
                    )
        # The first dynamic call occupies the static relation-plan row. Its
        # evidence-only shard estimate omits candidates and prompt authority;
        # reserve the same effective input cap used for every extra call.
        call_input_upper = effective_cap
        return call_input_upper, call_upper, {
            **plan,
            "hierarchical_needed": True,
            "batch_mode": "relation_atomic" if atomic_batches else "evidence_shards",
            "estimated_full_input_tokens": exact_estimate,
            "local_calls": local_calls,
            "cross_shard_call_upper_bound": cross_call_upper,
        }

    def _candidate_hierarchical_preflight(
        self,
        summaries: Sequence[Mapping[str, Any]],
        profile: ProviderContextProfile,
        *,
        variant_name: str,
        candidate_id: str | None = None,
    ) -> tuple[int, int]:
        """Reserve every possible evidence shard before candidate transport."""

        axes = build_organizing_axes(
            build_review_intent(self.review_intent_input), candidate_count=self.candidate_count,
        )
        if candidate_id is None and self.candidate_count > 5:
            projections = [
                self._candidate_hierarchical_preflight(
                    summaries, profile, variant_name=variant_name,
                    candidate_id=f"candidate_{index}",
                )
                for index in range(1, self.candidate_count + 1)
            ]
            return max(item[0] for item in projections), max(item[1] for item in projections)
        candidate_id = candidate_id or "candidate_1"
        axis = axes[int(candidate_id.removeprefix("candidate_")) - 1]
        generation_node_id = f"{candidate_id}_provider_generation"

        evidence = build_outline_evidence_views(summaries, self.job_id)
        ledger = build_global_corpus_ledger(evidence)
        matrix = build_multi_view_matrix(evidence)
        candidates = build_global_relation_map(evidence, matrix, ledger)
        content_layers = build_paper_content_layers(summaries, evidence, job_id=self.job_id)
        semantic_plan = build_semantic_chunk_plan(
            content_layers,
            candidates,
            candidate_count=self.candidate_count,
            physical_call_limit=authorized_provider_call_limit(self.max_provider_calls),
        )
        paper_keys = [str(view.paper_key) for view in evidence.views if str(view.paper_key)]
        selected_relation_ids = {
            str(item)
            for item in semantic_plan.coverage.get("selected_relation_ids") or ()
            if str(item)
        }
        relation_ids = [
            str(item.relation_id)
            for item in candidates.relations
            if str(item.relation_id) in selected_relation_ids
        ]
        request = {
            "candidate_id": candidate_id,
            **({"organizing_logic": axis.organizing_logic, "organizing_axis": axis.to_dict()}
               if "_then_" in axis.axis_id else {}),
            "variant_name": variant_name,
            "paper_keys": paper_keys,
            "relation_ids": relation_ids,
            "relations": [
                self._compact_relation_candidate(item.to_dict())
                for item in candidates.relations
                if str(item.relation_id) in selected_relation_ids
            ],
            "evidence": self._compact_candidate_evidence_refs(
                evidence.views,
                content_layers,
                semantic_plan,
            ),
            "source_summary_hashes": sorted(evidence.source_summary_hashes),
            "candidate_count": self.candidate_count,
            "evidence_bound": True,
            "evidence_projection": "registry_complete_evidence_ref_v1",
        }
        node_prefix = (
            "stability:" + hash_json({"variant": variant_name, "candidate": candidate_id})[:16]
            if variant_name not in {"canonical", "baseline"} else ""
        )
        enriched = self._attach_prompt_authority(
            f"{generation_node_id}:preflight:{variant_name}:1",
            request,
        )
        budget = profile.estimate_request(enriched)
        estimate = max(
            1,
            int(budget.get("estimated_input_tokens") or profile.estimate_tokens(enriched)),
        )
        effective_cap = self._effective_input_cap(profile)
        if estimate > self._relation_packing_target(profile) or not bool(budget.get("within_budget")):
            shard_requests = self._candidate_shard_requests(
                generation_node_id=generation_node_id,
                provider_request=request,
                evidence_views=evidence.views,
                relation_candidates=request["relations"],
                node_prefix=node_prefix,
            )
            for node_id, _shard, _papers, _relations, lower_request in shard_requests:
                enriched_shard = self._attach_prompt_authority(node_id, lower_request)
                lower_estimate = int(profile.estimate_request(enriched_shard).get("estimated_input_tokens") or 0)
                if lower_estimate > effective_cap:
                    raise OutlineV3ExecutionError(
                        f"BLOCKED_BUDGET: {generation_node_id} shard {node_id} "
                        f"complete request estimate {lower_estimate} exceeds effective input cap {effective_cap}"
                    )
            if not shard_requests:
                raise OutlineV3ExecutionError(
                    f"BLOCKED_BUDGET: {generation_node_id} has no bounded evidence shards"
                )
            return effective_cap, len(shard_requests)
        return estimate, 1

    def _build_provider_call_plans(self) -> tuple[OutlineProviderCallPlan, ...]:
        plans: list[OutlineProviderCallPlan] = []
        self._critique_preflight_shards: dict[tuple[str, str], int] = {}
        for variant_name, variant_summaries, transport_expected in self._provider_call_plan_variants():
            variant_evidence = build_outline_evidence_views(variant_summaries, self.job_id)
            variant_layers = build_paper_content_layers(variant_summaries, variant_evidence, job_id=self.job_id)
            for node_id in self._provider_node_ids():
                route = self._role_route(node_id)
                profile = route.profile
                hierarchical_relation_input: int | None = None
                candidate_shard_multiplier = 1
                if node_id == "relation_adjudication":
                    hierarchical_relation_input, _hierarchical_calls, _hierarchical_plan = (
                        self._relation_hierarchical_preflight(
                            variant_summaries,
                            profile,
                            variant_name=variant_name,
                        )
                    )
                    if _hierarchical_calls == 0:
                        # Explicitly empty selection is deterministic; there
                        # is no relation transport or receipt to reserve.
                        continue
                elif node_id.endswith("_provider_generation"):
                    hierarchical_relation_input, candidate_shard_multiplier = self._candidate_hierarchical_preflight(
                        variant_summaries,
                        profile,
                        variant_name=variant_name,
                        candidate_id=node_id.removesuffix("_provider_generation"),
                    )
                elif (
                    node_id in {"structure_critique", "coverage_critique", "evidence_critique", "arbitration"}
                ):
                    _candidate_input, candidate_shard_multiplier = self._candidate_hierarchical_preflight(
                        variant_summaries,
                        self._role_route("candidate_1_provider_generation").profile,
                        variant_name=variant_name,
                    )
                common_estimation = {
                    "navigation_cards": [item.to_dict() for item in variant_layers.index_cards],
                    "content_layers_hash": variant_layers.content_hash,
                    "source_summary_hashes": list(variant_layers.source_summary_hashes),
                    "source_summary_size_tokens": [
                        max(1, len(json.dumps(item, ensure_ascii=False, sort_keys=True)) // 4)
                        for item in variant_summaries
                    ],
                    "source_summary_size_marker": "x" * min(
                        2000,
                        max(1, sum(len(json.dumps(item, ensure_ascii=False)) for item in variant_summaries) // 1000),
                    ),
                    "candidate_count": self.candidate_count,
                    "evidence_bound": True,
                    "full_dossiers_are_local_retrieval_only": True,
                }
                if hierarchical_relation_input is not None:
                    representative_request = {
                        **common_estimation,
                        "job_id": self.job_id,
                        "stage_name": "outline_v3",
                        "variant_name": variant_name,
                        "node_id": node_id,
                        "hierarchical_relation_request": True,
                        "estimated_shard_input_tokens": hierarchical_relation_input,
                    }
                elif node_id in {"structure_critique", "coverage_critique", "evidence_critique", "arbitration"}:
                    representative_request = {
                        **common_estimation,
                        "job_id": self.job_id,
                        "stage_name": "outline_v3",
                        "variant_name": variant_name,
                        "node_id": node_id,
                        "candidate_output_upper_bound": self.candidate_count
                        * max(1, candidate_shard_multiplier)
                        * min(4_096, int(self._role_route("candidate_1_provider_generation").profile.max_output_tokens)),
                        "paper_count": len(variant_summaries),
                    }
                else:
                    representative_request = {
                        **common_estimation,
                        "job_id": self.job_id,
                        "stage_name": "outline_v3",
                        "variant_name": variant_name,
                        "node_id": node_id,
                        "evidence_views": self._prompt_evidence_views(variant_evidence.views),
                        "source_summary_excerpts_for_estimation": self._estimate_source_summary_excerpts(
                            variant_summaries
                        ),
                    }
                budget = profile.estimate_request(representative_request)
                estimated_input = max(
                    1,
                    int(
                        hierarchical_relation_input
                        or budget.get("estimated_input_tokens")
                        or profile.estimate_tokens(representative_request)
                    ),
                )
                base_input_estimate = estimated_input
                estimated_output = max(1, int(profile.max_output_tokens))
                if node_id.endswith("_provider_generation") and candidate_shard_multiplier > 1:
                    # The compact R1 candidate contract enforces this same
                    # output cap at transport, keeping critique/arbitration
                    # admission from reserving an unbounded candidate blob.
                    estimated_output = min(estimated_output, 4_096)
                elif node_id in {
                    "structure_critique",
                    "coverage_critique",
                    "evidence_critique",
                    "arbitration",
                }:
                    estimated_output = min(estimated_output, 2_048)
                estimated_reasoning = max(0, int(profile.reasoning_reserve))
                candidate_profile = self._role_route("candidate_1_provider_generation").profile
                candidate_output_cap = min(4_096, max(1, int(candidate_profile.max_output_tokens)))
                if candidate_shard_multiplier > 1:
                    candidate_output_cap = max(
                        candidate_output_cap,
                        candidate_shard_multiplier * min(1_024, max(1, int(candidate_profile.max_output_tokens))),
                    )
                candidate_output_upper_bound = (
                    self.candidate_count
                    * candidate_output_cap
                )
                critic_input_upper_bound = 0
                if (
                    node_id in {"structure_critique", "coverage_critique", "evidence_critique", "arbitration"}
                    and candidate_shard_multiplier > 1
                ):
                    # These nodes consume merged candidate/critique outputs,
                    # not the full Stage 1 evidence views.  Estimate their
                    # final bounded representation directly instead of using
                    # the old monolithic representative object.
                    critic_input_upper_bound = candidate_output_upper_bound
                    if node_id == "arbitration":
                        critic_input_upper_bound += 3 * estimated_output
                    estimated_input = critic_input_upper_bound + 8_192
                    base_input_estimate = estimated_input
                elif node_id in {"structure_critique", "coverage_critique", "evidence_critique"}:
                    critic_input_upper_bound = candidate_output_upper_bound
                elif node_id == "arbitration":
                    critic_input_upper_bound = candidate_output_upper_bound + 3 * estimated_output
                stability_claim_review_reserve = 0
                if (
                    node_id == "evidence_critique"
                    and variant_name not in {"baseline", "canonical"}
                    and self.stability_mode != "off"
                ):
                    # The existing variant evidence-critique call also
                    # compares changed typed claims with the primary output.
                    # Reserve one bounded primary candidate for that catalog;
                    # no additional provider call is introduced.
                    stability_claim_review_reserve = candidate_output_cap
                    critic_input_upper_bound += stability_claim_review_reserve
                    if candidate_shard_multiplier > 1:
                        estimated_input = critic_input_upper_bound + 8_192
                if critic_input_upper_bound and not (
                    node_id in {"structure_critique", "coverage_critique", "evidence_critique", "arbitration"}
                    and candidate_shard_multiplier > 1
                ):
                    estimated_input = max(
                        estimated_input,
                        base_input_estimate + critic_input_upper_bound,
                    )
                if node_id in {"structure_critique", "coverage_critique", "evidence_critique"}:
                    if estimated_input > self._relation_packing_target(profile):
                        self._critique_preflight_shards[(variant_name, node_id)] = (
                            self.candidate_count * max(1, candidate_shard_multiplier)
                        )
                        estimated_input = self._effective_input_cap(profile)
                estimated_cached = 0
                estimated_cache_write = 0
                total = estimated_input + estimated_output + estimated_reasoning
                cost = (
                    estimated_input / 1000.0 * float(self.input_cost_per_1k_tokens or 0.0)
                    + estimated_output / 1000.0 * float(self.output_cost_per_1k_tokens or 0.0)
                    + estimated_reasoning / 1000.0 * float(self.reasoning_cost_per_1k_tokens or 0.0)
                    + estimated_cached / 1000.0 * float(self.cache_read_cost_per_1k_tokens or 0.0)
                    + estimated_cache_write / 1000.0 * float(self.cache_write_cost_per_1k_tokens or 0.0)
                    if self._pricing_is_explicit
                    else None
                )
                assumptions = [
                    "prompt estimate is computed from a representative evidence-bound request",
                    "configured max_output_tokens is used as the output upper bound",
                    "reasoning reserve is charged as a separate upper-bound component",
                    "cache read/write tokens are zero until the provider reports them",
                    "estimated cost is a local admission estimate and is not billing data",
                ]
                if not self._pricing_is_explicit:
                    assumptions.append(
                        "pricing is unknown because an explicit pricing source and complete rate set were not supplied"
                    )
                if self._pricing_unknown_due_to_multiple_routes:
                    assumptions.append(
                        "pricing is unknown because one stage-wide rate set cannot represent the configured provider routes"
                    )
                if node_id.endswith("_provider_generation"):
                    assumptions.append("candidate generation output is included in the upper bound")
                if critic_input_upper_bound:
                    assumptions.append(
                        "critic/arbitration input upper bound includes candidate outputs at configured max_output_tokens"
                    )
                if stability_claim_review_reserve:
                    assumptions.append(
                        "variant evidence-critique input reserves one primary candidate output for claim-equivalence review"
                    )
                    if node_id == "arbitration":
                        assumptions.append(
                            "arbitration input also includes three critic outputs at configured max_output_tokens"
                        )
                rate_input = self.input_cost_per_1k_tokens if self._pricing_is_explicit else None
                rate_output = self.output_cost_per_1k_tokens if self._pricing_is_explicit else None
                rate_reasoning = self.reasoning_cost_per_1k_tokens if self._pricing_is_explicit else None
                rate_cache_read = self.cache_read_cost_per_1k_tokens if self._pricing_is_explicit else None
                rate_cache_write = self.cache_write_cost_per_1k_tokens if self._pricing_is_explicit else None
                retry_value = route.config_identity.get("transport_retries")
                if retry_value is None:
                    retry_value = (
                        0
                        if str(route.endpoint_type).casefold() in {"internal", "fixture"}
                        else self.semantic_transport_retries
                    )
                retry_reserve = (
                    max(0, int(retry_value))
                    if retry_value is not None and str(retry_value).strip() != ""
                    else None
                )
                plans.append(
                    OutlineProviderCallPlan(
                        artifact_type="outline_provider_call_plan",
                        artifact_version="v1",
                        job_id=self.job_id,
                        stage_name="outline_v3",
                        closure_epoch_id=self.closure_epoch_id,
                        logical_attempt_identity=self.logical_attempt_identity,
                        variant_name=variant_name,
                        node_id=node_id,
                        call_id=self._provider_call_id(node_id),
                        provider=route.provider_name,
                        model=route.model,
                        endpoint_type=route.endpoint_type,
                        estimated_input_tokens=estimated_input,
                        estimated_output_tokens=estimated_output,
                        estimated_reasoning_tokens=estimated_reasoning,
                        estimated_cached_input_tokens=estimated_cached,
                        estimated_cache_write_tokens=estimated_cache_write,
                        estimated_total_tokens=total,
                        input_cost_per_1k_tokens=rate_input,
                        output_cost_per_1k_tokens=rate_output,
                        reasoning_cost_per_1k_tokens=rate_reasoning,
                        cache_read_cost_per_1k_tokens=rate_cache_read,
                        cache_write_cost_per_1k_tokens=rate_cache_write,
                        estimated_cost=cost,
                        pricing_source=self.pricing_source,
                        pricing_policy=self.pricing_policy,
                        cost_status="estimate" if self._pricing_is_explicit else "unknown",
                        assumptions=tuple(assumptions),
                        confidence="medium",
                        upper_bound=True,
                        transport_expected=transport_expected,
                        configured_transport_retry_reserve=retry_reserve,
                        physical_attempt_upper_bound=(
                            1 + retry_reserve if retry_reserve is not None else None
                        ),
                        config_section=route.config_section,
                        api_base_host=route.api_base_host,
                        route_fingerprint=route.safe_config_fingerprint(),
                    )
                )
        return tuple(plans)

    def _persist_preflight_rejection(
        self,
        *,
        rejection_reason: str,
        diagnostic: str,
    ) -> None:
        """Persist a machine-readable admission rejection before transport."""

        payload = {
            "artifact_type": "outline_preflight_rejection",
            "artifact_version": "v1",
            "job_id": self.job_id,
            "stage_name": "outline_v3",
            "closure_epoch_id": self.closure_epoch_id,
            "logical_attempt_identity": self.logical_attempt_identity,
            "mode": self.stability_mode,
            "provider_call_plans": [item.to_dict() for item in self.provider_call_plans],
            "max_provider_calls": self.max_provider_calls,
            "max_source_prompt_tokens": self.max_source_prompt_tokens,
            "technical_shard_target_tokens": self.technical_shard_target_tokens,
            "estimated_provider_calls": None,
            "semantic_synthesis_calls_reserved": None,
            "estimated_total_tokens": None,
            "preflight_status": "rejected",
            "rejection_reason": rejection_reason,
            "diagnostic": diagnostic,
            "transport_posts_emitted": 0,
            "content_projection": "complete_dossier_study_claim_unit_v1",
        }
        for key in (
            "semantic_cross_group_runtime_fragment_count",
            "semantic_cross_group_planner_item_count",
            "semantic_cross_group_fragment_bounds_status",
            "semantic_request_upper_bound_status",
        ):
            if key in self.stability_preflight:
                payload[key] = self.stability_preflight[key]
        self.stability_preflight = payload
        path = self._path(
            f"outline_v3/stability/stability_preflight_{self.closure_epoch_id[:24]}.json"
        )
        record = publish_json_artifact(
            self.publication_context,
            self.registry,
            path,
            payload,
            artifact_role="outline_preflight_rejection",
            artifact_type="outline_preflight_rejection",
            artifact_version="v1",
            producer="outline.v3_executor.OutlineV3Executor",
            artifact_id=f"outline-v3:preflight_rejection:{self.closure_epoch_id[:24]}",
            metadata={
                "job_id": self.job_id,
                "closure_epoch_id": self.closure_epoch_id,
                "provider_call_plan_hash": hash_json(payload["provider_call_plans"]),
                "rejection_reason": rejection_reason,
            },
        )
        self.artifact_paths["provider_call_plan"] = record.path
        self.artifact_records["provider_call_plan"] = record

    @staticmethod
    def _pilot_verify_source_identity(acceptance: AcceptanceExecutionContextV1) -> None:
        """Recheck the authorized clean checkout at the pilot transport boundary."""

        try:
            current_sha = read_checkout_sha(
                Path(__file__).resolve().parents[1], require_clean=True
            )
        except CheckoutIdentityError as exc:
            raise OutlineV3ExecutionError(
                f"topic pilot source identity is not clean and verifiable: {exc}"
            ) from exc
        if current_sha != acceptance.final_executable_sha:
            raise OutlineV3ExecutionError(
                "topic pilot executable source SHA differs from the authorized acceptance run"
            )

    def _pilot_static_admission(self) -> None:
        """Bind a topic-only run to the existing acceptance authority."""

        pilot = self.outline_pilot
        if pilot is None:
            return
        selected_ids = pilot.get("selected_topic_batch_ids")
        if (
            not isinstance(selected_ids, list)
            or not selected_ids
            or any(not isinstance(item, str) or not item.startswith("topic_synthesis_provider:batch:") for item in selected_ids)
            or len(set(selected_ids)) != len(selected_ids)
            or pilot.get("auto_continue") is not False
            or pilot.get("adoption_authorized") is not False
            or any(
                not isinstance(pilot.get(field), int)
                or isinstance(pilot.get(field), bool)
                or int(pilot[field]) <= 0
                for field in ("max_physical_attempts", "max_output_tokens_all_attempts")
            )
        ):
            raise OutlineV3ExecutionError("topic pilot scope or phase envelope is invalid")
        approved_hashes = pilot.get("selected_request_hashes")
        if (
            not isinstance(approved_hashes, Mapping)
            or set(approved_hashes) != set(selected_ids)
            or any(
                not isinstance(value, str)
                or re.fullmatch(r"[0-9a-f]{64}", value) is None
                for value in approved_hashes.values()
            )
        ):
            raise OutlineV3ExecutionError(
                "topic pilot approved request hash scope is invalid"
            )
        if not self.semantic_provider_synthesis_enabled:
            raise OutlineV3ExecutionError(
                "topic pilot requires the current semantic provider route"
            )
        existing_final = self.registry.get("outline-v3:final_outline")
        if existing_final is not None and existing_final.status == "ready":
            raise OutlineV3ExecutionError(
                "topic pilot requires a workspace without a canonical final outline"
            )
        if hash_json(self.summaries) != str(pilot.get("source_summary_set_hash") or ""):
            raise OutlineV3ExecutionError(
                "topic pilot source summary set differs from its frozen scope"
            )
        route = self._role_route("candidate_1_provider_generation")
        if route.safe_config_fingerprint() != str(pilot.get("allowed_route_fingerprint") or ""):
            raise OutlineV3ExecutionError(
                "topic pilot generation route differs from the approved fingerprint"
            )
        try:
            deadline = datetime.fromisoformat(
                str(pilot.get("deadline_utc") or "").replace("Z", "+00:00")
            )
        except ValueError as exc:
            raise OutlineV3ExecutionError("topic pilot deadline is invalid") from exc
        offset = deadline.utcoffset()
        if deadline.tzinfo is None or offset is None or offset.total_seconds() != 0:
            raise OutlineV3ExecutionError("topic pilot deadline must be UTC")
        self._pilot_deadline_epoch = deadline.timestamp()
        if datetime.now(timezone.utc).timestamp() >= self._pilot_deadline_epoch:
            raise OutlineV3ExecutionError("topic pilot deadline has expired")
        if int(pilot.get("max_physical_attempts") or 0) > authorized_provider_call_limit(
            self.max_provider_calls
        ):
            raise OutlineV3ExecutionError(
                "topic pilot exceeds the unchanged application call limit"
            )
        external = str(route.endpoint_type or "").casefold() not in {"internal", "fixture"}
        acceptance = (
            current_acceptance_execution_context()
            or acceptance_execution_context_from_environment()
        )
        controller = provider_budget_controller_from_environment()
        if external and (acceptance is None or controller is None):
            raise OutlineV3ExecutionError(
                "external topic pilot requires a bound acceptance run and aggregate controller"
            )
        if acceptance is not None:
            if (
                not acceptance.owner_authorized
                or acceptance.acceptance_run_id != str(pilot.get("acceptance_run_id") or "")
                or controller is None
                or controller.budget != acceptance.provider_budget
                or acceptance.absolute_deadline_epoch <= datetime.now(timezone.utc).timestamp()
                or acceptance.absolute_deadline_epoch > self._pilot_deadline_epoch
                or acceptance.provider_budget.max_provider_calls_total <= 0
                or acceptance.provider_budget.max_output_tokens_total <= 0
                or acceptance.provider_budget.max_wall_seconds <= 0
                or acceptance.provider_budget.max_provider_calls_total
                > int(pilot["max_physical_attempts"])
                or acceptance.provider_budget.max_output_tokens_total
                > int(pilot["max_output_tokens_all_attempts"])
            ):
                raise OutlineV3ExecutionError(
                    "topic pilot acceptance authority is absent, expired, or unbounded"
                )
            controller.snapshot()
            if external:
                self._pilot_verify_source_identity(acceptance)
        self.stability_preflight = {
            "mode": "topic_pilot",
            "schema_version": "outline-topic-pilot/v1",
            "source_summary_set_hash": str(pilot["source_summary_set_hash"]),
            "selected_topic_batch_ids": list(pilot["selected_topic_batch_ids"]),
            "allowed_route_fingerprint": route.safe_config_fingerprint(),
            "acceptance_run_id": str(pilot["acceptance_run_id"]),
            "preflight_status": "awaiting_exact_topic_request_materialization",
            "provider_posts_emitted": 0,
        }

    def _materialize_topic_pilot_plan(
        self,
        *,
        topic_batches: Sequence[Sequence[Any]],
        topic_routes: Mapping[str, Any],
        evidence_model: Any,
        content_layers_model: Any,
        profile: ProviderContextProfile,
    ) -> list[tuple[int, Sequence[Any], dict[str, Any]]]:
        """Measure every selected full request before the first pilot POST."""

        pilot = self.outline_pilot
        if pilot is None:
            raise OutlineV3ExecutionError("topic pilot plan requested outside pilot mode")
        selected_ids = set(str(item) for item in pilot["selected_topic_batch_ids"])
        rows: list[tuple[int, Sequence[Any], dict[str, Any]]] = []
        safe_rows: list[dict[str, Any]] = []
        route = self._role_route("candidate_1_provider_generation")
        input_cap = self._effective_input_cap(profile)
        output_limit = self._semantic_output_token_limit(profile)
        content_layers_hash = str(getattr(content_layers_model, "content_hash", ""))
        for batch_index, batch in enumerate(topic_batches, start=1):
            node_id = f"topic_synthesis_provider:batch:{batch_index}"
            if node_id not in selected_ids:
                continue
            request = self._build_topic_provider_request(
                batch,
                topic_routes=topic_routes,
                evidence_model=evidence_model,
                content_layers_model=content_layers_model,
                batch_index=batch_index,
                content_layers_hash=content_layers_hash,
            )
            attached = self._attach_prompt_authority(node_id, request)
            estimate = profile.estimate_request(attached)
            input_tokens = int(estimate.get("estimated_input_tokens") or 0)
            if input_tokens > input_cap or not bool(estimate.get("within_budget")):
                raise OutlineV3ExecutionError(
                    f"topic pilot complete request {node_id} exceeds input cap "
                    f"({input_tokens}/{input_cap})"
                )
            self._semantic_request_contract(node_id, request)
            rows.append((batch_index, batch, request))
            safe_rows.append({
                "node_id": node_id,
                "request_hash": hash_json(attached),
                "estimated_input_tokens": input_tokens,
                "reserved_output_tokens_per_attempt": output_limit,
                "topic_ids": sorted(str(item.topic_id) for item in batch),
                "paper_ids_hash": hash_json(sorted({
                    str(paper_id) for item in batch
                    for paper_id in (*item.paper_ids, *item.bridge_paper_ids)
                })),
            })
        if {row["node_id"] for row in safe_rows} != selected_ids:
            raise OutlineV3ExecutionError(
                "topic pilot selects an unknown or unmaterialized topic batch"
            )
        approved_hashes = pilot["selected_request_hashes"]
        if any(
            row["request_hash"] != approved_hashes[row["node_id"]]
            for row in safe_rows
        ):
            raise OutlineV3ExecutionError(
                "topic pilot request differs from owner-approved exact hash"
            )
        configured_retries = self._semantic_transport_retry_count()
        if configured_retries is None:
            raise OutlineV3ExecutionError(
                "topic pilot route has no finite transport retry bound"
            )
        physical_attempts = len(rows) * (1 + configured_retries)
        output_reserve = physical_attempts * output_limit
        if (
            physical_attempts > int(pilot["max_physical_attempts"])
            or output_reserve > int(pilot["max_output_tokens_all_attempts"])
        ):
            raise OutlineV3ExecutionError(
                "topic pilot materialized request plan exceeds its declared phase envelope"
            )
        plan = {
            "schema_version": "outline-topic-pilot-plan/v1",
            "status": "accepted_before_transport",
            "pilot_scope_hash": hash_json(pilot),
            "source_summary_set_hash": str(pilot["source_summary_set_hash"]),
            "acceptance_run_id": str(pilot["acceptance_run_id"]),
            "route_fingerprint": route.safe_config_fingerprint(),
            "selected_requests": safe_rows,
            "logical_call_count": len(rows),
            "physical_attempt_upper_bound": physical_attempts,
            "retry_attempt_upper_bound": len(rows) * configured_retries,
            "output_token_all_attempts_upper_bound": output_reserve,
            "provider_posts_emitted": 0,
        }
        plan_hash = hash_json(plan)
        plan_id = f"outline-v3:topic-pilot-plan:{plan_hash[:24]}"
        previous_plan = self.registry.get(plan_id)
        if previous_plan is not None:
            try:
                self.registry.verify_ready_artifact_closure(previous_plan)
                previous_payload = json.loads(Path(previous_plan.path).read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError, RegistryError) as exc:
                raise OutlineV3ExecutionError(
                    "topic pilot prior plan is not a valid Registry artifact"
                ) from exc
            if (
                previous_plan.artifact_type != "outline_topic_pilot_plan"
                or previous_plan.artifact_version != "v1"
                or previous_payload != plan
            ):
                raise OutlineV3ExecutionError(
                    "topic pilot prior plan differs from this approved scope"
                )
        controller = provider_budget_controller_from_environment()
        if controller is not None:
            snapshot = controller.snapshot()
            budget = controller.budget
            available_calls = (
                budget.max_provider_calls_total
                - int(snapshot["calls_used"])
                - int(snapshot["calls_reserved"])
            )
            available_output = (
                budget.max_output_tokens_total
                - int(snapshot["output_tokens_used"])
                - int(snapshot["output_tokens_reserved"])
            )
            available_retries = (
                budget.max_retry_attempts_total
                - int(snapshot["retry_attempts_used"])
                - int(snapshot["retry_attempts_reserved"])
            )
            previously_used = any(
                int(snapshot[field]) > 0
                for field in (
                    "calls_used", "calls_reserved",
                    "output_tokens_used", "output_tokens_reserved",
                    "retry_attempts_used", "retry_attempts_reserved",
                )
            )
            if (
                budget.max_provider_calls_total <= 0
                or budget.max_output_tokens_total <= 0
                or budget.max_provider_calls_total > int(pilot["max_physical_attempts"])
                or budget.max_output_tokens_total > int(pilot["max_output_tokens_all_attempts"])
                or budget.max_retry_attempts_total > len(rows) * configured_retries
                or float(snapshot["elapsed_seconds"]) >= budget.max_wall_seconds
                or (
                    float(snapshot["absolute_deadline_epoch"] or 0) > 0
                    and datetime.now(timezone.utc).timestamp()
                    >= float(snapshot["absolute_deadline_epoch"])
                )
                or (previously_used and previous_plan is None)
                or (previous_plan is None and physical_attempts > available_calls)
                or (previous_plan is None and output_reserve > available_output)
                or (previous_plan is None and len(rows) * configured_retries > available_retries)
            ):
                raise OutlineV3ExecutionError(
                    "topic pilot exceeds the remaining aggregate acceptance budget"
                )
        record = publish_json_artifact(
            self.publication_context,
            self.registry,
            self._path(f"outline_v3/pilot/topic_pilot_plan_{plan_hash[:24]}.json"),
            plan,
            artifact_role="outline_topic_pilot_plan",
            artifact_type="outline_topic_pilot_plan",
            artifact_version="v1",
            producer="outline.v3_executor.OutlineV3Executor",
            artifact_id=plan_id,
        )
        self.artifact_paths["topic_pilot_plan"] = record.path
        self.artifact_records["topic_pilot_plan"] = record
        self._pilot_allowed_node_ids = frozenset(selected_ids)
        self._pilot_request_hashes = dict(approved_hashes)
        self.stability_preflight.update({
            "preflight_status": "accepted_before_transport",
            "topic_pilot_plan_hash": plan_hash,
            "logical_call_count": len(rows),
            "physical_attempt_upper_bound": physical_attempts,
            "output_token_all_attempts_upper_bound": output_reserve,
        })
        return rows

    def _finish_topic_pilot(
        self,
        *,
        provider_results: Sequence[Mapping[str, Any]],
        source_record: ArtifactRecord,
    ) -> OutlineV3ExecutionResult:
        """Close only the selected topic calls and publish a noncanonical stop."""

        pilot = self.outline_pilot
        if pilot is None:
            raise OutlineV3ExecutionError("topic pilot checkpoint requested outside pilot mode")
        selected_ids = set(self._pilot_allowed_node_ids)
        result_ids = {
            str(item.get("provider_node_id") or "") for item in provider_results
        }
        if result_ids != selected_ids or len(provider_results) != len(selected_ids):
            raise OutlineV3ExecutionError(
                "topic pilot did not materialize every selected request exactly once"
            )
        for result in provider_results:
            output = result.get("provider_output")
            topics = output.get("topics") if isinstance(output, Mapping) else None
            if not isinstance(topics, list) or not topics:
                raise OutlineV3ExecutionError(
                    "topic pilot provider output has no substantive topic result"
                )
            for topic in topics:
                explicit_unresolved = bool(
                    isinstance(topic, Mapping)
                    and str(topic.get("status") or "") in {
                        "unresolved", "deferred", "insufficient_evidence", "not_comparable"
                    }
                    and str(topic.get("reason") or "").strip()
                )
                has_content = bool(
                    isinstance(topic, Mapping)
                    and any(
                        isinstance(topic.get(field), list)
                        and any(str(item).strip() for item in topic[field])
                        for field in ("conclusions", "unresolved_questions")
                    )
                )
                if not has_content and not explicit_unresolved:
                    raise OutlineV3ExecutionError(
                        "topic pilot fragment has neither a supported conclusion nor an explicit unresolved question"
                    )
        self._register_receipt_ledger()
        self._persist_audit_evidence()
        expected_ids = {self._provider_call_id(node_id) for node_id in selected_ids}
        expected = [
            value for call_id, value in self._expected_provider_calls.items()
            if call_id in expected_ids
        ]
        if len(expected) != len(selected_ids):
            raise OutlineV3ExecutionError(
                "topic pilot provider-call authority is incomplete"
            )
        closure = ProviderReceiptClosure.evaluate(
            expected,
            self._receipt_ledger.list_receipts(),
        )
        if not closure.complete:
            failed_fields = {
                field: value
                for field, value in closure.to_dict().items()
                if field in {
                    "missing_call_ids", "stale_call_ids", "failed_call_ids",
                    "incomplete_call_ids", "hash_mismatches", "unexpected_receipts",
                    "retry_exceeded_call_ids", "usage_incomplete_call_ids",
                } and value
            }
            raise OutlineV3ExecutionError(
                "topic pilot receipt closure is incomplete: "
                + json.dumps(failed_fields, sort_keys=True)
            )
        pilot_hash = hash_json(pilot)
        output_records = [
            self.artifact_records[node_id] for node_id in sorted(selected_ids)
        ]
        ledger_record = self.artifact_records.get("provider_receipts")
        plan_record = self.artifact_records.get("topic_pilot_plan")
        if ledger_record is None or plan_record is None:
            raise OutlineV3ExecutionError(
                "topic pilot lacks its Registry plan or receipt ledger"
            )
        closure_payload = {
            **closure.to_dict(),
            "schema_version": "outline-topic-pilot-receipt-closure/v1",
            "pilot_scope_hash": pilot_hash,
            "acceptance_run_id": str(pilot["acceptance_run_id"]),
            "selected_call_ids": sorted(expected_ids),
            "canonical_outline_complete": False,
        }
        closure_record = publish_json_artifact(
            self.publication_context,
            self.registry,
            self._path(f"outline_v3/pilot/topic_pilot_closure_{pilot_hash[:24]}.json"),
            closure_payload,
            artifact_role="outline_topic_pilot_receipt_closure",
            artifact_type="outline_topic_pilot_receipt_closure",
            artifact_version="v1",
            producer="outline.v3_executor.OutlineV3Executor",
            artifact_id=f"outline-v3:topic-pilot-closure:{pilot_hash[:24]}",
            depends_on=[
                ArtifactDependencyRefV2.from_record(record)
                for record in (plan_record, ledger_record, *output_records)
            ],
        )
        self.artifact_paths["topic_pilot_receipt_closure"] = closure_record.path
        self.artifact_records["topic_pilot_receipt_closure"] = closure_record
        checkpoint_payload = {
            "schema_version": "outline-topic-pilot-checkpoint/v1",
            "status": "TOPIC_PILOT_COMPLETE",
            "pilot_scope_hash": pilot_hash,
            "source_summary_set_hash": str(pilot["source_summary_set_hash"]),
            "selected_topic_batch_ids": sorted(selected_ids),
            "topic_result_hashes": {
                str(item["provider_node_id"]): hash_json(item["provider_output"])
                for item in provider_results
            },
            "provider_output_artifact_ids": [
                record.artifact_id for record in output_records
            ],
            "receipt_ids": list(self.receipts),
            "receipt_closure_artifact_id": closure_record.artifact_id,
            "receipt_closure_hash": closure_record.content_hash,
            "acceptance_run_id": str(pilot["acceptance_run_id"]),
            "canonical_ready": False,
            "final_outline_artifact_id": "",
            "auto_continue": False,
            "adoption_authorized": False,
        }
        checkpoint_record = publish_json_artifact(
            self.publication_context,
            self.registry,
            self._path(f"outline_v3/pilot/topic_pilot_checkpoint_{pilot_hash[:24]}.json"),
            checkpoint_payload,
            artifact_role="outline_topic_pilot_checkpoint",
            artifact_type="outline_topic_pilot_checkpoint",
            artifact_version="v1",
            producer="outline.v3_executor.OutlineV3Executor",
            artifact_id=f"outline-v3:topic-pilot-checkpoint:{pilot_hash[:24]}",
            depends_on=[
                ArtifactDependencyRefV2.from_record(record)
                for record in (source_record, plan_record, closure_record, *output_records)
            ],
        )
        self.artifact_paths["topic_pilot_checkpoint"] = checkpoint_record.path
        self.artifact_records["topic_pilot_checkpoint"] = checkpoint_record
        return OutlineV3ExecutionResult(
            self.job_id,
            "topic_pilot_complete",
            False,
            dict(self.artifact_paths),
            tuple(node.node_id for node in self._dag.nodes if node.status == "succeeded"),
            tuple(self.receipts),
            tuple(self.diagnostics),
            self._dag,
        )

    def _preflight_stability_budget(self) -> None:
        core_calls = len(self._provider_node_ids())
        self.stability_preflight.update(
            {
                "semantic_cross_group_runtime_fragment_count": None,
                "semantic_cross_group_planner_item_count": None,
                "semantic_cross_group_fragment_bounds_status": "incomplete_upper_bound",
                "semantic_request_upper_bound_status": "incomplete_upper_bound",
            }
        )
        try:
            self.provider_call_plans = self._build_provider_call_plans()
        except OutlineV3ExecutionError as exc:
            if str(exc).startswith("BLOCKED_BUDGET: relation"):
                self._persist_preflight_rejection(
                    rejection_reason="complete_relation_request_exceeds_effective_input_cap",
                    diagnostic=str(exc),
                )
            elif str(exc).startswith("BLOCKED_BUDGET: candidate_"):
                self._persist_preflight_rejection(
                    rejection_reason="candidate_evidence_shard_exceeds_effective_input_cap",
                    diagnostic=str(exc),
                )
            raise
        if (
            self.enabled_semantic_roles is not None
            and "relation_adjudication" not in self.enabled_semantic_roles
        ):
            evidence = build_outline_evidence_views(self.summaries, self.job_id)
            ledger = build_global_corpus_ledger(evidence)
            matrix = build_multi_view_matrix(evidence)
            relation_map = build_global_relation_map(evidence, matrix, ledger)
            content_layers = build_paper_content_layers(
                self.summaries, evidence, job_id=self.job_id
            )
            semantic_plan = build_semantic_chunk_plan(
                content_layers,
                relation_map,
                candidate_count=self.candidate_count,
                physical_call_limit=authorized_provider_call_limit(self.max_provider_calls),
            )
            selection = semantic_plan.coverage.get("selected_relation_ids")
            selected_ids = (
                [item.relation_id for item in relation_map.relations]
                if selection is None else selection
            )
            if selected_ids:
                diagnostic = "selected relations require the relation_adjudication role"
                self._persist_preflight_rejection(
                    rejection_reason="selected_relation_role_disabled",
                    diagnostic=diagnostic,
                )
                raise OutlineV3ExecutionError(f"BLOCKED_ROUTE: {diagnostic}")
        transport_plans = [item for item in self.provider_call_plans if item.transport_expected]
        estimated_provider_calls = len(transport_plans)
        retry_reserve_unknown_call_count = sum(
            item.configured_transport_retry_reserve is None for item in transport_plans
        )
        estimated_physical_attempts_upper_bound = (
            sum(int(item.physical_attempt_upper_bound or 1) for item in transport_plans)
            if retry_reserve_unknown_call_count == 0
            else None
        )
        estimated_input_tokens = sum(
            item.estimated_input_tokens * int(item.physical_attempt_upper_bound or 1)
            for item in transport_plans
        )
        estimated_output_tokens = sum(
            item.estimated_output_tokens * int(item.physical_attempt_upper_bound or 1)
            for item in transport_plans
        )
        estimated_reasoning_tokens = sum(
            item.estimated_reasoning_tokens * int(item.physical_attempt_upper_bound or 1)
            for item in transport_plans
        )
        estimated_cached_input_tokens = sum(
            item.estimated_cached_input_tokens * int(item.physical_attempt_upper_bound or 1)
            for item in transport_plans
        )
        estimated_cache_write_tokens = sum(
            item.estimated_cache_write_tokens * int(item.physical_attempt_upper_bound or 1)
            for item in transport_plans
        )
        estimated_total_tokens = sum(
            item.estimated_total_tokens * int(item.physical_attempt_upper_bound or 1)
            for item in transport_plans
        )
        estimated_cost_values = [
            (
                item.estimated_cost * int(item.physical_attempt_upper_bound or 1)
                if item.estimated_cost is not None
                and item.configured_transport_retry_reserve is not None
                else None
            )
            for item in transport_plans
        ]
        known_estimated_costs = [value for value in estimated_cost_values if value is not None]
        estimated_cost = (
            sum(known_estimated_costs)
            if len(known_estimated_costs) == len(estimated_cost_values)
            else None
        )
        semantic_synthesis_calls = 0
        semantic_physical_attempts_upper: int | None = (
            None if self.semantic_provider_synthesis_enabled else 0
        )
        semantic_retry_reserve: int | None = None
        semantic_attempt_reserve_status = "not_applicable"
        semantic_output_token_limit = 0
        semantic_conditional_reducer_call_reserve = 0
        semantic_input_tokens_single_attempt = 0
        semantic_output_tokens_single_attempt = 0
        semantic_input_tokens = 0
        semantic_output_tokens = 0
        semantic_reasoning_tokens = 0
        cross_group_runtime_fragment_count: int | None = None
        cross_group_planner_item_count: int | None = None
        cross_group_fragment_bounds_status = "incomplete_upper_bound"
        if self.semantic_provider_synthesis_enabled:
            semantic_route = self._role_route("candidate_1_provider_generation")
            # Count the exact complete-evidence topic batches used by ``run``.
            # Cross-group and global synthesis add two calls. Oversized
            # indivisible evidence units raise a typed budget rejection before
            # any provider transport is admitted.
            try:
                semantic_topic_batches = self._topic_provider_batch_count(
                    self.summaries,
                    semantic_route.profile,
                )
            except OutlineV3ExecutionError as exc:
                self._persist_preflight_rejection(
                    rejection_reason="complete_evidence_unit_exceeds_effective_input_cap",
                    diagnostic=str(exc),
                )
                raise
            semantic_synthesis_calls = semantic_topic_batches + 2
            semantic_output = self._semantic_output_token_limit(semantic_route.profile)
            semantic_output_token_limit = semantic_output
            semantic_reasoning = max(0, int(semantic_route.profile.reasoning_reserve))
            topic_input_tokens = sum(
                int(item.get("estimated_input_tokens") or 0)
                for item in self.semantic_request_plan
            )
            cross_group_fragment_plans = [
                dict(fragment)
                for batch in self.semantic_request_plan
                for fragment in batch.get("cross_group_fragment_plans") or ()
                if isinstance(fragment, Mapping)
            ]
            semantic_context_plan = [
                {
                    "topic_id": str(fragment.get("topic_id") or ""),
                    "fragment_id": str(fragment.get("fragment_id") or ""),
                    "paper_ids": list(fragment.get("paper_ids") or []),
                    "cross_group_item_tokens_upper_bound": int(
                        fragment.get("cross_group_item_tokens_upper_bound") or 0
                    ),
                }
                for fragment in cross_group_fragment_plans
            ]
            cross_group_runtime_item_count = len(semantic_context_plan)
            runtime_fragment_rows = [
                fragment
                for batch in self.semantic_request_plan
                for fragment in batch.get("topic_fragments") or ()
                if isinstance(fragment, Mapping)
            ]
            runtime_fragment_keys = [
                (str(fragment.get("topic_id") or ""), str(fragment.get("fragment_id") or ""))
                for fragment in runtime_fragment_rows
            ]
            planner_fragment_keys = [
                (str(fragment.get("topic_id") or ""), str(fragment.get("fragment_id") or ""))
                for fragment in cross_group_fragment_plans
            ]
            cross_group_runtime_fragment_count = len(runtime_fragment_rows)
            cross_group_planner_item_count = cross_group_runtime_item_count
            cross_group_item_output_bounds = [
                int(item.get("cross_group_item_tokens_upper_bound") or 0)
                for item in semantic_context_plan
            ]
            topic_batch_count = len(self.semantic_request_plan)
            fragment_bounds_materialized = (
                runtime_fragment_keys == planner_fragment_keys
                and len(set(runtime_fragment_keys)) == len(runtime_fragment_keys)
                and len(set(planner_fragment_keys)) == len(planner_fragment_keys)
                and len(cross_group_item_output_bounds) == cross_group_runtime_fragment_count
                and all(value > 0 for value in cross_group_item_output_bounds)
            )
            cross_group_fragment_bounds_status = (
                "materialized_upper_bound"
                if fragment_bounds_materialized
                else "incomplete_upper_bound"
            )
            self.stability_preflight.update(
                {
                    "semantic_cross_group_runtime_fragment_count": cross_group_runtime_fragment_count,
                    "semantic_cross_group_planner_item_count": cross_group_planner_item_count,
                    "semantic_cross_group_fragment_bounds_status": cross_group_fragment_bounds_status,
                }
            )
            if topic_batch_count and not fragment_bounds_materialized:
                raise OutlineV3ExecutionError(
                    "BLOCKED_BUDGET: cross-group reducer input bounds are missing "
                    "runtime fragment metadata or interpretation source context"
                )
            cross_request = {
                "task": "substantive_cross_group_comparison",
                "node_id": "cross_group_comparison",
                "semantic_contract_version": "semantic-evidence-graph-v2",
                "shared_synthesis_contract_version": SHARED_SYNTHESIS_CONTRACT_VERSION,
                "questions": list(self.semantic_cross_group_questions),
                "topic_synthesis": semantic_context_plan,
                "relation_candidates": self.semantic_relation_candidates,
                "output_contract": {
                    "comparisons": "array of evidence-bound comparisons",
                    "bridge_claims": "array of evidence-bound claims with topic_ids for every integrated topic they support",
                    "topic_dispositions": "one row per topic_id: integrated with synthesis_claim_ids referring to bridge_claims, or unresolved with a reason; preserve conditions, zero results and exceptions in the supported claim or unresolved text",
                    "coverage_ledger": "topic, fragment, result and relation membership is computed and persisted locally after validation; do not echo those identity arrays",
                    "unresolved_questions": "array",
                },
            }
            cross_shell_request = {**cross_request, "topic_synthesis": []}
            cross_budget = semantic_route.profile.estimate_request(
                self._attach_prompt_authority(
                    "cross_group_comparison_provider:preflight",
                    cross_shell_request,
                )
            )
            cross_wrapper_tokens = int(
                cross_budget.get("estimated_input_tokens")
                or semantic_route.profile.estimate_tokens(cross_shell_request)
            )
            cross_item_tokens = max(cross_group_item_output_bounds, default=semantic_output)
            global_request = {
                "task": "substantive_global_synthesis",
                "node_id": "global_synthesis",
                "semantic_contract_version": "semantic-evidence-graph-v2",
                # Cross has already consumed and validated every topic. The
                # global role reads that shared synthesis once, not all 33
                # topic bodies again.
                "topic_synthesis": [],
                "cross_group_comparison": {
                    "output_token_upper_bound": semantic_output,
                    "processed_topic_count": len(self.semantic_topic_ids),
                    "processed_topic_ids_hash": hash_json(sorted(self.semantic_topic_ids)),
                },
                "cross_coverage_ledger_ref": {
                    "content_hash": "0" * 64,
                    "topic_count": len(self.semantic_topic_ids),
                },
                # Directed candidates were already consumed by the cross-group
                # stage; global synthesis reuses its validated comparison.
                "relation_candidates": [],
                "output_contract": {
                    "synthesis_claims": "array of evidence-bound claims",
                    "organizing_principles": "array",
                    "coverage_ledger": "validated cross-topic membership is local Registry data; do not echo processed identity arrays",
                    "unresolved_questions": "array",
                },
            }
            global_shell_request = {
                **global_request,
                "topic_synthesis": [],
            }
            global_budget = semantic_route.profile.estimate_request(
                self._attach_prompt_authority(
                    "global_synthesis_provider:preflight",
                    global_shell_request,
                )
            )
            global_wrapper_tokens = int(
                global_budget.get("estimated_input_tokens")
                or semantic_route.profile.estimate_tokens(global_shell_request)
            )
            input_limit = max(
                1,
                min(
                    32_000,
                    int(self.max_source_prompt_tokens or 32_000),
                    int(semantic_route.profile.input_budget or 32_000),
                ),
            )
            reducer_output_tokens = max(1, min(semantic_output, input_limit // 4))

            def plan_bounded_stage(
                node_id: str,
                *,
                item_count: int,
                item_output_tokens: int,
                wrapper_tokens: int,
                extra_output_tokens: int = 0,
            ) -> list[dict[str, Any]]:
                rows: list[dict[str, Any]] = []
                retry_reserve = self._semantic_transport_retry_count()
                count = max(1, int(item_count))
                per_item_tokens = max(1, int(item_output_tokens))
                overhead = max(0, int(wrapper_tokens)) + 512
                level = 1
                while overhead + count * per_item_tokens + extra_output_tokens > input_limit:
                    if level > MAX_SEMANTIC_REDUCTION_LEVELS:
                        raise OutlineV3ExecutionError(
                            f"BLOCKED_BUDGET: {node_id} reducer plan exceeds {MAX_SEMANTIC_REDUCTION_LEVELS} levels"
                        )
                    available = input_limit - overhead - extra_output_tokens
                    if available < per_item_tokens:
                        raise OutlineV3ExecutionError(
                            f"BLOCKED_BUDGET: one {node_id} semantic result plus its wrapper exceeds the effective input cap"
                        )
                    per_call_items = max(1, available // per_item_tokens)
                    next_count = math.ceil(count / per_call_items)
                    if next_count >= count and reducer_output_tokens >= per_item_tokens:
                        raise OutlineV3ExecutionError(
                            f"BLOCKED_BUDGET: {node_id} reducer upper bound does not shrink"
                        )
                    if len(rows) + next_count > MAX_SEMANTIC_REDUCER_CALLS_PER_STAGE:
                        raise OutlineV3ExecutionError(
                            f"BLOCKED_BUDGET: {node_id} reducer plan exceeds {MAX_SEMANTIC_REDUCER_CALLS_PER_STAGE} calls"
                        )
                    for group_index in range(next_count):
                        group_size = min(per_call_items, count - group_index * per_call_items)
                        rows.append(
                            {
                                "node_id": f"{node_id}:reduce:{level}:{group_index + 1}",
                                "estimated_input_tokens": min(
                                    input_limit,
                                    overhead + group_size * per_item_tokens + extra_output_tokens,
                                ),
                                "estimated_output_tokens": reducer_output_tokens,
                                "estimated_reasoning_tokens": semantic_reasoning,
                                "configured_transport_retry_reserve": retry_reserve,
                                "physical_attempt_upper_bound": (
                                    1 + retry_reserve if retry_reserve is not None else None
                                ),
                                "attempt_reserve_status": (
                                    "configured" if retry_reserve is not None else "unknown"
                                ),
                                "input_estimate_kind": "bounded_reducer_upper_bound",
                            }
                        )
                    count = next_count
                    per_item_tokens = reducer_output_tokens
                    extra_output_tokens = 0
                    level += 1
                rows.append(
                    {
                        "node_id": node_id,
                        "estimated_input_tokens": overhead + count * per_item_tokens + extra_output_tokens,
                        "estimated_output_tokens": semantic_output,
                        "estimated_reasoning_tokens": semantic_reasoning,
                        "configured_transport_retry_reserve": retry_reserve,
                        "physical_attempt_upper_bound": (
                            1 + retry_reserve if retry_reserve is not None else None
                        ),
                        "attempt_reserve_status": (
                            "configured" if retry_reserve is not None else "unknown"
                        ),
                        "input_estimate_kind": "wrapper_plus_upstream_output_upper_bound",
                    }
                )
                return rows

            try:
                cross_plan_rows = plan_bounded_stage(
                    "cross_group_comparison_provider",
                    # Relation candidates are routed with their owning topic
                    # group and consumed at the first reducer level. They are
                    # fixed input context, not additional upstream result
                    # items; the shell request above accounts for their full
                    # serialized token cost.
                    item_count=max(1, cross_group_runtime_item_count),
                    item_output_tokens=cross_item_tokens,
                    wrapper_tokens=cross_wrapper_tokens,
                )
                global_plan_rows = plan_bounded_stage(
                    "global_synthesis_provider",
                    item_count=1,
                    item_output_tokens=semantic_output,
                    wrapper_tokens=global_wrapper_tokens,
                )
                cross_wrapper_bytes = len(json.dumps(
                    self._attach_prompt_authority("cross_group_comparison_provider:preflight", cross_shell_request),
                    ensure_ascii=False, sort_keys=True,
                ).encode("utf-8"))
                global_wrapper_bytes = len(json.dumps(
                    self._attach_prompt_authority("global_synthesis_provider:preflight", global_shell_request),
                    ensure_ascii=False, sort_keys=True,
                ).encode("utf-8"))
                for row in cross_plan_rows:
                    row["static_wrapper_canonical_bytes"] = cross_wrapper_bytes
                for row in global_plan_rows:
                    row["static_wrapper_canonical_bytes"] = global_wrapper_bytes
            except OutlineV3ExecutionError as exc:
                self._persist_preflight_rejection(
                    rejection_reason="semantic_aggregation_cannot_be_reduced_within_input_cap",
                    diagnostic=str(exc),
                )
                raise
            semantic_stage_plan = [
                *self.semantic_request_plan,
                *cross_plan_rows,
                *global_plan_rows,
            ]
            self.semantic_request_plan = semantic_stage_plan
            self.topic_request_plan_identity_hash = self._compute_topic_provider_plan_identity_hash(
                semantic_stage_plan
            )
            # The bounded stage plan already materializes every reducer level
            # from runtime-fragment input bounds and the provider output cap.
            # Reserving every unused stage slot on top of that graph double
            # counts reducer capacity and can reject small, fully planned jobs.
            semantic_conditional_reducer_call_reserve = 0
            semantic_synthesis_calls = (
                semantic_topic_batches + len(cross_plan_rows) + len(global_plan_rows)
                + semantic_conditional_reducer_call_reserve
            )
            cross_input_tokens = sum(int(item["estimated_input_tokens"]) for item in cross_plan_rows)
            global_input_tokens = sum(int(item["estimated_input_tokens"]) for item in global_plan_rows)
            semantic_retry_reserve = self._semantic_transport_retry_count()
            semantic_attempt_multiplier = 1 + semantic_retry_reserve if semantic_retry_reserve is not None else 1
            semantic_attempt_reserve_status = (
                "configured" if semantic_retry_reserve is not None else "unknown"
            )
            semantic_physical_attempts_upper = (
                semantic_synthesis_calls * semantic_attempt_multiplier
                if semantic_retry_reserve is not None
                else None
            )
            semantic_input_tokens_single_attempt = (
                topic_input_tokens + cross_input_tokens + global_input_tokens
                + semantic_conditional_reducer_call_reserve * input_limit
            )
            semantic_input_tokens = semantic_input_tokens_single_attempt * semantic_attempt_multiplier
            semantic_output_tokens_single_attempt = sum(
                int(item.get("estimated_output_tokens") or 0)
                for item in [*cross_plan_rows, *global_plan_rows]
            ) + semantic_topic_batches * semantic_output + (
                semantic_conditional_reducer_call_reserve * reducer_output_tokens
            )
            semantic_output_tokens = semantic_output_tokens_single_attempt * semantic_attempt_multiplier
            semantic_reasoning_tokens = (
                semantic_synthesis_calls * semantic_reasoning * semantic_attempt_multiplier
            )
            if semantic_retry_reserve is None:
                # A missing retry contract makes a cost ceiling incomplete.
                estimated_cost = None
            estimated_provider_calls += semantic_synthesis_calls
            estimated_input_tokens += semantic_input_tokens
            estimated_output_tokens += semantic_output_tokens
            estimated_reasoning_tokens += semantic_reasoning_tokens
            estimated_total_tokens += semantic_input_tokens + semantic_output_tokens + semantic_reasoning_tokens
            if estimated_cost is not None:
                required_rates = (
                    self.input_cost_per_1k_tokens,
                    self.output_cost_per_1k_tokens,
                    self.reasoning_cost_per_1k_tokens,
                )
                if any(value is None for value in required_rates):
                    estimated_cost = None
                else:
                    input_rate = float(self.input_cost_per_1k_tokens or 0.0)
                    output_rate = float(self.output_cost_per_1k_tokens or 0.0)
                    reasoning_rate = float(self.reasoning_cost_per_1k_tokens or 0.0)
                    estimated_cost += (
                        semantic_input_tokens / 1000.0 * input_rate
                        + semantic_output_tokens / 1000.0
                        * output_rate
                        + semantic_reasoning_tokens / 1000.0
                        * reasoning_rate
                    )
        hierarchical_candidate_shard_calls = 0
        hierarchical_candidate_attempt_upper: int | None = 0
        if "candidate_1_provider_generation" in self._provider_node_ids():
            for variant_name, variant_summaries, transport_expected in self._provider_call_plan_variants():
                if not transport_expected:
                    continue
                candidate_route = self._role_route("candidate_1_provider_generation")
                _candidate_input, candidate_shard_count = self._candidate_hierarchical_preflight(
                    variant_summaries,
                    candidate_route.profile,
                    variant_name=variant_name,
                )
                if candidate_shard_count > 1:
                    hierarchical_candidate_shard_calls += self.candidate_count * (candidate_shard_count - 1)
            if hierarchical_candidate_shard_calls:
                candidate_route = self._role_route("candidate_1_provider_generation")
                candidate_plan_attempts = [
                    item.physical_attempt_upper_bound for item in transport_plans
                    if item.node_id.endswith("_provider_generation")
                ]
                candidate_attempt_multiplier = (
                    max(int(value) for value in candidate_plan_attempts if value is not None)
                    if candidate_plan_attempts and all(value is not None for value in candidate_plan_attempts)
                    else None
                )
                hierarchical_candidate_attempt_upper = (
                    hierarchical_candidate_shard_calls * candidate_attempt_multiplier
                    if candidate_attempt_multiplier is not None else None
                )
                if candidate_attempt_multiplier is None:
                    estimated_cost = None
                reserve_attempts = candidate_attempt_multiplier or 1
                candidate_input = self._effective_input_cap(candidate_route.profile)
                candidate_output = min(max(1, int(candidate_route.profile.max_output_tokens)), 1024)
                candidate_reasoning = max(0, int(candidate_route.profile.reasoning_reserve))
                estimated_provider_calls += hierarchical_candidate_shard_calls
                estimated_input_tokens += hierarchical_candidate_shard_calls * reserve_attempts * candidate_input
                estimated_output_tokens += hierarchical_candidate_shard_calls * reserve_attempts * candidate_output
                estimated_reasoning_tokens += hierarchical_candidate_shard_calls * reserve_attempts * candidate_reasoning
                estimated_total_tokens += hierarchical_candidate_shard_calls * reserve_attempts * (
                    candidate_input + candidate_output + candidate_reasoning
                )
                if estimated_cost is not None:
                    estimated_cost += hierarchical_candidate_shard_calls * reserve_attempts * (
                        candidate_input / 1000.0 * float(self.input_cost_per_1k_tokens or 0.0)
                        + candidate_output / 1000.0 * float(self.output_cost_per_1k_tokens or 0.0)
                        + candidate_reasoning / 1000.0 * float(self.reasoning_cost_per_1k_tokens or 0.0)
                    )
        hierarchical_relation_shard_calls = 0
        hierarchical_relation_attempt_upper: int | None = 0
        if "relation_adjudication" in self._provider_node_ids():
            for _variant_name, variant_summaries, transport_expected in self._provider_call_plan_variants():
                if not transport_expected:
                    continue
                _relation_input, relation_call_count, relation_plan = (
                    self._relation_hierarchical_preflight(
                        variant_summaries,
                        self._role_route("relation_adjudication").profile,
                        variant_name=_variant_name,
                    )
                )
                if relation_plan.get("hierarchical_needed"):
                    # The static relation node is replaced by these dynamic
                    # calls. One call is already reserved by its static row.
                    hierarchical_relation_shard_calls += max(0, relation_call_count - 1)
            if hierarchical_relation_shard_calls:
                relation_route = self._role_route("relation_adjudication")
                relation_plan_attempts = [
                    item.physical_attempt_upper_bound for item in transport_plans
                    if item.node_id == "relation_adjudication"
                ]
                relation_attempt_multiplier = (
                    max(int(value) for value in relation_plan_attempts if value is not None)
                    if relation_plan_attempts and all(value is not None for value in relation_plan_attempts)
                    else None
                )
                hierarchical_relation_attempt_upper = (
                    hierarchical_relation_shard_calls * relation_attempt_multiplier
                    if relation_attempt_multiplier is not None else None
                )
                if relation_attempt_multiplier is None:
                    estimated_cost = None
                reserve_attempts = relation_attempt_multiplier or 1
                relation_input = self._effective_input_cap(relation_route.profile)
                relation_output = max(1, int(relation_route.profile.max_output_tokens))
                relation_reasoning = max(0, int(relation_route.profile.reasoning_reserve))
                estimated_provider_calls += hierarchical_relation_shard_calls
                estimated_input_tokens += hierarchical_relation_shard_calls * reserve_attempts * relation_input
                estimated_output_tokens += hierarchical_relation_shard_calls * reserve_attempts * relation_output
                estimated_reasoning_tokens += hierarchical_relation_shard_calls * reserve_attempts * relation_reasoning
                estimated_total_tokens += hierarchical_relation_shard_calls * reserve_attempts * (
                    relation_input + relation_output + relation_reasoning
                )
                if estimated_cost is not None:
                    estimated_cost += hierarchical_relation_shard_calls * reserve_attempts * (
                        relation_input / 1000.0 * float(self.input_cost_per_1k_tokens or 0.0)
                        + relation_output / 1000.0 * float(self.output_cost_per_1k_tokens or 0.0)
                        + relation_reasoning / 1000.0 * float(self.reasoning_cost_per_1k_tokens or 0.0)
                    )
        hierarchical_critique_shard_calls = 0
        critique_extra_by_role: dict[str, int] = {}
        critique_extra_attempts_by_role: dict[str, int] = {}
        critique_extra_attempt_upper: int | None = 0
        for plan in transport_plans:
            planned_shards = self._critique_preflight_shards.get((plan.variant_name, plan.node_id), 1)
            extra_calls = max(0, planned_shards - 1)
            if extra_calls:
                hierarchical_critique_shard_calls += extra_calls
                if plan.physical_attempt_upper_bound is None:
                    critique_extra_attempt_upper = None
                elif critique_extra_attempt_upper is not None:
                    critique_extra_attempt_upper += extra_calls * int(plan.physical_attempt_upper_bound)
                    critique_extra_attempts_by_role[plan.node_id] = (
                        critique_extra_attempts_by_role.get(plan.node_id, 0)
                        + extra_calls * int(plan.physical_attempt_upper_bound)
                    )
                critique_extra_by_role[plan.node_id] = (
                    critique_extra_by_role.get(plan.node_id, 0) + extra_calls
                )
        if hierarchical_critique_shard_calls:
            estimated_provider_calls += hierarchical_critique_shard_calls
            for role, extra_calls in critique_extra_by_role.items():
                role_profile = self._role_route(role).profile
                reserved_attempts = critique_extra_attempts_by_role.get(role, extra_calls)
                critique_input = self._effective_input_cap(role_profile)
                critique_output = min(max(1, int(role_profile.max_output_tokens)), 2_048)
                critique_reasoning = max(0, int(role_profile.reasoning_reserve))
                estimated_input_tokens += reserved_attempts * critique_input
                estimated_output_tokens += reserved_attempts * critique_output
                estimated_reasoning_tokens += reserved_attempts * critique_reasoning
                estimated_total_tokens += reserved_attempts * (
                    critique_input + critique_output + critique_reasoning
                )
                if estimated_cost is not None:
                    estimated_cost += reserved_attempts * (
                        critique_input / 1000.0 * float(self.input_cost_per_1k_tokens or 0.0)
                        + critique_output / 1000.0 * float(self.output_cost_per_1k_tokens or 0.0)
                        + critique_reasoning / 1000.0 * float(self.reasoning_cost_per_1k_tokens or 0.0)
                    )
        semantic_repair_calls_reserved = 0
        semantic_repair_attempt_upper: int | None = 0
        semantic_repair_input = 0
        if self.semantic_repair_enabled:
            generation_plans = [
                item for item in transport_plans
                if item.node_id.endswith("_provider_generation")
            ]
            if generation_plans:
                # One bounded repair may follow each transported candidate,
                # including a stability variant using the same contract.
                semantic_repair_calls_reserved = len(generation_plans)
                repair_route = self._role_route("candidate_1_provider_generation")
                semantic_repair_input = self._effective_input_cap(repair_route.profile)
                repair_output = max(1, int(repair_route.profile.max_output_tokens))
                repair_reasoning = max(0, int(repair_route.profile.reasoning_reserve))
                repair_attempts = [item.physical_attempt_upper_bound for item in generation_plans]
                repair_attempt_multiplier = (
                    max(int(value) for value in repair_attempts if value is not None)
                    if all(value is not None for value in repair_attempts)
                    else None
                )
                semantic_repair_attempt_upper = (
                    semantic_repair_calls_reserved * repair_attempt_multiplier
                    if repair_attempt_multiplier is not None else None
                )
                if repair_attempt_multiplier is None:
                    estimated_cost = None
                reserve_attempts = repair_attempt_multiplier or 1
                estimated_provider_calls += semantic_repair_calls_reserved
                estimated_input_tokens += semantic_repair_calls_reserved * reserve_attempts * semantic_repair_input
                estimated_output_tokens += semantic_repair_calls_reserved * reserve_attempts * repair_output
                estimated_reasoning_tokens += semantic_repair_calls_reserved * reserve_attempts * repair_reasoning
                estimated_total_tokens += semantic_repair_calls_reserved * reserve_attempts * (
                    semantic_repair_input + repair_output + repair_reasoning
                )
                if estimated_cost is not None:
                    estimated_cost += semantic_repair_calls_reserved * reserve_attempts * (
                        semantic_repair_input / 1000.0 * float(self.input_cost_per_1k_tokens or 0.0)
                        + repair_output / 1000.0 * float(self.output_cost_per_1k_tokens or 0.0)
                        + repair_reasoning / 1000.0 * float(self.reasoning_cost_per_1k_tokens or 0.0)
                    )
        nonsemantic_retry_unknown = retry_reserve_unknown_call_count > 0
        if hierarchical_critique_shard_calls:
            # Their current dynamic input/retry estimates are not a complete
            # upper bound, so an apparent monetary ceiling would be misleading.
            estimated_cost = None
        estimated_provider_physical_attempts_upper_bound = (
            estimated_physical_attempts_upper_bound
            + int(semantic_physical_attempts_upper)
            + int(hierarchical_candidate_attempt_upper)
            + int(hierarchical_relation_attempt_upper)
            + int(critique_extra_attempt_upper)
            + int(semantic_repair_attempt_upper)
            if estimated_physical_attempts_upper_bound is not None
            and semantic_physical_attempts_upper is not None
            and not nonsemantic_retry_unknown
            and hierarchical_candidate_attempt_upper is not None
            and hierarchical_relation_attempt_upper is not None
            and semantic_repair_attempt_upper is not None
            and critique_extra_attempt_upper is not None
            else None
        )
        semantic_request_upper_bound_status = (
            "materialized_upper_bound"
            if cross_group_fragment_bounds_status == "materialized_upper_bound"
            and estimated_provider_physical_attempts_upper_bound is not None
            else "incomplete_upper_bound"
        )
        variants = self._stability_variant_plan()
        self.stability_preflight = {
            "artifact_type": "outline_provider_call_plan",
            "artifact_version": "v1",
            "job_id": self.job_id,
            "stage_name": "outline_v3",
            "closure_epoch_id": self.closure_epoch_id,
            "logical_attempt_identity": self.logical_attempt_identity,
            "mode": self.stability_mode,
            "provider_nodes_per_decision": core_calls,
            "variant_names": [name for name, _summaries, _order, _definition in variants],
            "estimated_provider_calls": estimated_provider_calls,
            "estimated_provider_physical_attempts_upper_bound": estimated_provider_physical_attempts_upper_bound,
            "configured_retry_reserve_unknown_call_count": retry_reserve_unknown_call_count,
            "physical_attempt_estimate_status": (
                "upper_bound"
                if estimated_provider_physical_attempts_upper_bound is not None
                else "unknown_or_extra_shard_attempts_not_bound"
            ),
            "hierarchical_candidate_shard_calls": hierarchical_candidate_shard_calls,
            "hierarchical_candidate_physical_attempts_upper_bound": hierarchical_candidate_attempt_upper,
            "hierarchical_relation_shard_calls": hierarchical_relation_shard_calls,
            "hierarchical_relation_physical_attempts_upper_bound": hierarchical_relation_attempt_upper,
            "hierarchical_critique_shard_calls": hierarchical_critique_shard_calls,
            "semantic_repair_calls_reserved": semantic_repair_calls_reserved,
            "semantic_repair_physical_attempts_upper_bound": semantic_repair_attempt_upper,
            "semantic_synthesis_calls_reserved": semantic_synthesis_calls,
            "semantic_conditional_reducer_call_reserve": semantic_conditional_reducer_call_reserve,
            "semantic_input_tokens_single_attempt_upper_bound": semantic_input_tokens_single_attempt,
            "semantic_output_tokens_single_attempt_upper_bound": semantic_output_tokens_single_attempt,
            "semantic_input_tokens_all_attempts_upper_bound": (
                semantic_input_tokens if semantic_retry_reserve is not None else None
            ),
            "semantic_output_tokens_all_attempts_upper_bound": (
                semantic_output_tokens if semantic_retry_reserve is not None else None
            ),
            "semantic_reasoning_tokens_all_attempts_upper_bound": (
                semantic_reasoning_tokens if semantic_retry_reserve is not None else None
            ),
            "semantic_physical_attempts_upper_bound": semantic_physical_attempts_upper,
            "semantic_transport_retry_reserve": semantic_retry_reserve,
            "semantic_attempt_reserve_status": semantic_attempt_reserve_status,
            "semantic_request_plan": list(self.semantic_request_plan),
            "semantic_cross_group_runtime_fragment_count": cross_group_runtime_fragment_count,
            "semantic_cross_group_planner_item_count": cross_group_planner_item_count,
            "semantic_cross_group_fragment_bounds_status": cross_group_fragment_bounds_status,
            "semantic_request_upper_bound_status": semantic_request_upper_bound_status,
            "semantic_output_token_limit": semantic_output_token_limit,
            "semantic_input_plan_kind": (
                "exact_topic_requests_plus_cross_global_output_upper_bounds"
                if self.semantic_provider_synthesis_enabled
                else "not_applicable"
            ),
            "estimated_input_tokens": estimated_input_tokens,
            "estimated_output_tokens": estimated_output_tokens,
            "estimated_reasoning_tokens": estimated_reasoning_tokens,
            "estimated_cached_input_tokens": estimated_cached_input_tokens,
            "estimated_cache_write_tokens": estimated_cache_write_tokens,
            "estimated_total_tokens": estimated_total_tokens,
            "estimated_cost": estimated_cost,
            "pricing_source": self.pricing_source,
            "pricing_policy": self.pricing_policy,
            "pricing_confidence": "medium" if estimated_cost is not None else "unknown",
            "cost_status": "estimate" if estimated_cost is not None else "unknown",
            "monetary_ceiling_enforced": bool(estimated_cost is not None),
            "cost_ceiling_note": (
                ""
                if estimated_cost is not None
                else "monetary ceiling was not enforced because pricing or dynamic-call bounds are unknown"
            ),
            "provider_call_plan_hash": hash_json([item.to_dict() for item in self.provider_call_plans]),
            "provider_call_plans": [item.to_dict() for item in self.provider_call_plans],
            "max_provider_calls": self.max_provider_calls,
            "max_estimated_cost": self.max_estimated_cost,
            "max_estimated_total_tokens": self.max_estimated_total_tokens,
            "estimated_cost_per_1k_tokens": self.estimated_cost_per_1k_tokens,
            "input_cost_per_1k_tokens": self.input_cost_per_1k_tokens,
            "output_cost_per_1k_tokens": self.output_cost_per_1k_tokens,
            "reasoning_cost_per_1k_tokens": self.reasoning_cost_per_1k_tokens,
            "cache_read_cost_per_1k_tokens": self.cache_read_cost_per_1k_tokens,
            "cache_write_cost_per_1k_tokens": self.cache_write_cost_per_1k_tokens,
            "max_source_prompt_tokens": self.max_source_prompt_tokens,
            "pricing_provider": self.pricing_provider,
            "pricing_model": self.pricing_model,
            "pricing_version": self.pricing_version,
            "pricing_effective_date": self.pricing_effective_date,
            "provider_configured": self._provider_configured(),
            "reachable_provider_route_plan": self.reachable_provider_route_plan,
            "preflight_status": "accepted",
        }
        preflight_path = self._path(
            f"outline_v3/stability/stability_preflight_{self.closure_epoch_id[:24]}.json"
        )
        if self.stability_mode != "off" and not self._provider_configured():
            self.stability_preflight["preflight_status"] = "rejected"
            self.stability_preflight["rejection_reason"] = "stability_provider_or_route_missing"
        elif self.max_provider_calls is not None and estimated_provider_calls > self.max_provider_calls:
            self.stability_preflight["preflight_status"] = "rejected"
            self.stability_preflight["rejection_reason"] = "max_provider_calls_exceeded"
        elif self.max_provider_calls is not None and estimated_provider_physical_attempts_upper_bound is None:
            self.stability_preflight["preflight_status"] = "rejected"
            self.stability_preflight["rejection_reason"] = "provider_physical_attempt_bound_unknown"
        elif (
            self.max_provider_calls is not None
            and estimated_provider_physical_attempts_upper_bound is not None
            and estimated_provider_physical_attempts_upper_bound > self.max_provider_calls
        ):
            self.stability_preflight["preflight_status"] = "rejected"
            self.stability_preflight["rejection_reason"] = "max_provider_physical_attempts_exceeded"
        elif (
            self.max_estimated_cost is not None
            and estimated_cost is not None
            and estimated_cost > self.max_estimated_cost
        ):
            self.stability_preflight["preflight_status"] = "rejected"
            self.stability_preflight["rejection_reason"] = "max_estimated_cost_exceeded"
        elif (
            self.max_estimated_total_tokens is not None
            and estimated_total_tokens > self.max_estimated_total_tokens
        ):
            self.stability_preflight["preflight_status"] = "rejected"
            self.stability_preflight["rejection_reason"] = "max_estimated_total_tokens_exceeded"
        elif any(
            item.estimated_input_tokens
            > self._effective_input_cap(self._role_route(item.node_id).profile)
            for item in transport_plans
        ) or (
            semantic_repair_calls_reserved
            and semantic_repair_input
            > self._effective_input_cap(
                self._role_route("candidate_1_provider_generation").profile
            )
        ):
            self.stability_preflight["preflight_status"] = "rejected"
            self.stability_preflight["rejection_reason"] = "source_prompt_exceeds_effective_input_cap"
        elif (
            self.max_smoke_overhead_ratio is not None
            and self.stability_mode == "smoke"
            and core_calls > 0
            and estimated_provider_calls / core_calls > self.max_smoke_overhead_ratio
        ):
            self.stability_preflight["preflight_status"] = "rejected"
            self.stability_preflight["rejection_reason"] = "smoke_overhead_ratio_exceeded"
        if self.max_estimated_cost is not None and estimated_cost is None:
            self.stability_preflight["cost_ceiling_note"] = (
                "monetary ceiling was not enforced because pricing or dynamic-call bounds are unknown"
            )
        plan_record = publish_json_artifact(
            self.publication_context,
            self.registry,
            preflight_path,
            self.stability_preflight,
            artifact_role="outline_provider_call_plan",
            artifact_type="outline_provider_call_plan",
            artifact_version="v1",
            producer="outline.v3_executor.OutlineV3Executor",
            artifact_id=f"outline-v3:provider_call_plan:{self.stability_mode}",
            metadata={
                "job_id": self.job_id,
                "closure_epoch_id": self.closure_epoch_id,
                "provider_call_plan_hash": self.stability_preflight["provider_call_plan_hash"],
                "pricing_policy": self.pricing_policy,
            },
        )
        self.artifact_paths["provider_call_plan"] = plan_record.path
        self.artifact_records["provider_call_plan"] = plan_record
        if self.stability_preflight["preflight_status"] != "accepted":
            raise OutlineV3ExecutionError(
                "outline stability preflight rejected: "
                + str(self.stability_preflight.get("rejection_reason") or "unknown")
            )

    def _actual_usage_cost_snapshot(self) -> dict[str, Any]:
        """Return honest usage/cost evidence without inventing billing data."""

        receipts = list(self._receipt_ledger.list_receipts())
        input_known = all(receipt.input_tokens is not None for receipt in receipts)
        output_known = all(receipt.output_tokens is not None for receipt in receipts)
        reasoning_known = all(
            receipt.reasoning_tokens is not None
            or self.reasoning_cost_per_1k_tokens == 0
            for receipt in receipts
        )
        actual_cost: float | None = None
        if self._pricing_is_explicit and receipts and input_known and output_known and reasoning_known:
            actual_cost = 0.0
            for receipt in receipts:
                actual_cost += int(receipt.input_tokens or 0) / 1000.0 * float(self.input_cost_per_1k_tokens or 0.0)
                actual_cost += int(receipt.output_tokens or 0) / 1000.0 * float(self.output_cost_per_1k_tokens or 0.0)
                actual_cost += int(receipt.reasoning_tokens or 0) / 1000.0 * float(self.reasoning_cost_per_1k_tokens or 0.0)
                actual_cost += int(receipt.cached_input_tokens or 0) / 1000.0 * float(self.cache_read_cost_per_1k_tokens or 0.0)
        estimated_cost = self.stability_preflight.get("estimated_cost")
        variance = (
            float(actual_cost) - float(estimated_cost)
            if actual_cost is not None and estimated_cost is not None
            else None
        )
        return {
            "provider_calls": len(receipts),
            "input_tokens": sum(int(receipt.input_tokens or 0) for receipt in receipts),
            "output_tokens": sum(int(receipt.output_tokens or 0) for receipt in receipts),
            "reasoning_tokens": sum(int(receipt.reasoning_tokens or 0) for receipt in receipts),
            "cached_input_tokens": sum(int(receipt.cached_input_tokens or 0) for receipt in receipts),
            "usage_status": (
                "reported"
                if receipts and input_known and output_known and reasoning_known
                else "unreported_or_partial"
            ),
            "estimated_cost": estimated_cost,
            "actual_cost": actual_cost,
            "cost_variance": variance,
            "cost_variance_status": "computed" if variance is not None else "not_computable",
            "pricing_source": self.pricing_source,
            "pricing_policy": self.pricing_policy,
            "pricing_confidence": "medium" if self._pricing_is_explicit else "unknown",
            "cost_status": "calculated" if actual_cost is not None else "unknown",
            "assumptions": [
                "missing provider usage is not converted to zero for cost claims",
                "actual cost is a local calculation from reported token counts and configured rates",
                "this field is not a provider invoice or billing record",
            ],
        }

    def _current_dependency_hashes(self, node_id: str) -> dict[str, str]:
        node = self._dag.get(node_id)
        result: dict[str, str] = {}
        for dependency_id in node.depends_on:
            dependency = self._dag.get(dependency_id)
            if dependency_id in self._payloads:
                result[dependency_id] = _hash_payload(self._payloads[dependency_id])
            elif dependency_id == "stage1_summaries":
                result[dependency_id] = _hash_payload(self.summaries)
            elif dependency.output_hash:
                result[dependency_id] = dependency.output_hash
        return result

    def _artifact_type_for_node(self, node_id: str) -> str:
        if node_id == "relation_adjudication":
            return "relation_adjudication_result"
        if node_id == "global_relation_map":
            return "confirmed_global_relation_map"
        if node_id.endswith("_provider_generation"):
            return "outline_candidate"
        return {
            "outline_content_layers": "outline_content_layers",
            "semantic_chunk_plan": "semantic_chunk_plan",
            "structure_critique": "structure_critique",
            "coverage_critique": "coverage_critique",
            "evidence_critique": "evidence_critique",
            "arbitration": "arbitration_decision",
            "selected_candidate": "selected_outline_candidate",
            "selected_candidate_revision": "selected_candidate_revision",
            "section_evidence_packets": "section_evidence_packet_set",
            "final_outline": "final_outline",
            "coverage_audit": "coverage_audit",
            "stability_audit": "stability_audit",
            "provider_receipt_closure": "provider_receipt_closure",
            "stage_health": "outline_stage_health",
        }.get(node_id, "outline_artifact")

    def build_current_node_binding(
        self,
        node_id: str,
        *,
        artifact_type: str = "outline_artifact",
        artifact_version: str = "v3",
        dependency_hashes: Mapping[str, str] | None = None,
        model: str = "deterministic",
        provider: str = "local",
        prompt: Mapping[str, Any] | None = None,
        prompt_template_hash: str = "",
        prompt_payload_hash: str = "",
        api_config: Mapping[str, Any] | None = None,
        route: OutlineRoleRoute | None = None,
    ) -> dict[str, Any]:
        """Build the complete current binding before attempting node reuse.

        ``route`` carries the node's real provider identity. When it is omitted a
        provider node falls back to the executor-level profile, which is only
        correct for the pre-router single-provider path.
        """

        if artifact_type == "outline_artifact":
            artifact_type = self._artifact_type_for_node(node_id)
        review_nodes = {
            "review_intent", "coverage_contract", "organizing_axes", "structure_critique",
            "coverage_critique", "evidence_critique", "arbitration", "selected_candidate",
            "section_evidence_packets", "final_outline", "coverage_audit", "stability_audit",
            "provider_receipt_closure", "stage_health",
        }
        provider_node = (
            node_id in self._provider_node_ids()
            or node_id.startswith("stability:")
            or node_id.endswith("_semantic_repair")
            or node_id.startswith("relation_adjudication:")
            or (node_id.startswith("candidate_") and "_provider_generation:" in node_id)
            or node_id.startswith((
                "topic_synthesis_provider",
                "cross_group_comparison_provider",
                "global_synthesis_provider",
            ))
            or (
                self.semantic_provider_synthesis_enabled
                and node_id in {"topic_synthesis", "cross_group_comparison", "global_synthesis"}
            )
            or any(
                node_id.startswith(f"{role}:")
                for role in ("structure_critique", "coverage_critique", "evidence_critique")
            )
        )
        resolved_route = route if route is not None else (
            self._node_route("candidate_1_provider_generation")
            if self.semantic_provider_synthesis_enabled
            and node_id in {"topic_synthesis", "cross_group_comparison", "global_synthesis"}
            else (self._node_route(node_id) if provider_node else None)
        )
        if provider_node and resolved_route is not None:
            bind_provider = resolved_route.provider_name
            bind_model = resolved_route.model
            bind_endpoint = resolved_route.endpoint_type
            bind_section = resolved_route.config_section
        else:
            bind_provider = self.profile.provider if provider_node else "local"
            bind_model = self.profile.model if provider_node else model
            bind_endpoint = self.profile.endpoint_type if provider_node else "internal"
            bind_section = ""
        route_config = dict(api_config or {})
        if provider_node and not route_config and resolved_route is not None:
            route_config = self._route_transport_identity(resolved_route)
        config = dict(route_config)
        if provider_node:
            config.update({
                "provider": bind_provider,
                "model": bind_model,
                "endpoint_type": bind_endpoint,
                "config_section": bind_section,
                "route": provider,
            })
            if resolved_route is not None:
                # Gateway identity and a secret-free fingerprint of the knobs
                # that shape this node's call. The raw provider config is
                # deliberately not hashed here: it can carry credentials.
                config.update({
                    "api_base_host": resolved_route.api_base_host,
                    "route_fingerprint": resolved_route.safe_config_fingerprint(),
                })
        candidate_sensitive = (
            node_id in review_nodes
            or node_id.startswith("candidate_")
            or node_id.startswith((
                "topic_synthesis_provider",
                "cross_group_comparison_provider",
                "global_synthesis_provider",
            ))
            or node_id.startswith("stability:")
        )
        relevant_config = {
            "candidate_count": self.candidate_count if candidate_sensitive else 0,
            "quality_gate": self.quality_gate.to_dict() if candidate_sensitive else {},
            "provider_config": config,
            "semantic_output_max_tokens": (
                self.semantic_output_max_tokens
                if node_id in {"topic_synthesis", "cross_group_comparison", "global_synthesis"}
                or node_id.startswith((
                    "topic_synthesis_provider",
                    "cross_group_comparison_provider",
                    "global_synthesis_provider",
                ))
                else 0
            ),
        }
        interpretation_sensitive = (
            node_id in {
                "outline_evidence_views",
                "outline_content_layers",
                "semantic_chunk_plan",
                "topic_synthesis",
                "cross_group_comparison",
                "global_synthesis",
            }
            or node_id.startswith((
                "topic_synthesis_provider",
                "cross_group_comparison_provider",
                "global_synthesis_provider",
            ))
        )
        return {
            "node_id": node_id,
            "semantic_node_id": self._semantic_node_id(node_id),
            "node_version": (
                "v3-interpretation-v1" if interpretation_sensitive else "v3"
            ),
            "artifact_type": artifact_type,
            "artifact_version": artifact_version,
            "schema_version": "outline-v3",
            "dependency_hashes": dict(sorted((dependency_hashes or self._current_dependency_hashes(node_id)).items())),
            "current_summary_hashes": self._summary_hashes(),
            "review_intent_hash": self._review_intent_hash if node_id in review_nodes else "",
            "coverage_contract_hash": self._coverage_contract_hash if node_id in review_nodes else "",
            "quality_gate_hash": self.quality_gate.content_hash if node_id in review_nodes else "",
            "candidate_count": self.candidate_count if node_id in review_nodes or node_id.startswith("candidate_") else 0,
            "provider_route": (bind_section or provider) if provider_node else "local",
            "provider_family": bind_provider,
            "model_name": bind_model,
            "endpoint_type": bind_endpoint,
            "api_base_host": resolved_route.api_base_host if provider_node and resolved_route is not None else "",
            "route_fingerprint": resolved_route.safe_config_fingerprint() if provider_node and resolved_route is not None else "",
            "transport_retries": (
                str(resolved_route.config_identity.get("transport_retries") or "0")
                if provider_node and resolved_route is not None
                else "0"
            ),
            "prompt_template_hash": prompt_template_hash,
            "prompt_payload_hash": prompt_payload_hash,
            "prompt_hash": hash_text(json.dumps(prompt, sort_keys=True, ensure_ascii=False)) if prompt is not None else "",
            "prompt_id": self._outline_prompt_identity.prompt_id if provider_node else "",
            "prompt_version": self._outline_prompt_identity.version if provider_node else "",
            "prompt_sha256": self._outline_prompt_identity.sha256 if provider_node else "",
            # The receipt and replay layers hash the exact transport identity,
            # not the enriched binding-only metadata below.  Hashing the
            # latter would make every fresh call look stale on resume.
            "provider_config_hash": hash_json(route_config) if provider_node else "",
            "schema_hash": _hash_payload({"node_id": self._semantic_node_id(node_id), "expect_json": True}),
            "context_profile_hash": (
                self._context_profile_hash(resolved_route)
                if provider_node and resolved_route is not None
                else (self._context_profile_hash() if provider_node else "")
            ),
            "relevant_runtime_config_hash": _hash_payload(relevant_config),
        }

    def _provider_binding(
        self,
        node_id: str,
        request: Mapping[str, Any],
        *,
        expect_json: bool,
        input_artifact_hashes: Sequence[str],
        route: OutlineRoleRoute | None = None,
    ) -> dict[str, Any]:
        semantic_node_id = self._semantic_node_id(node_id)
        resolved_route = route if route is not None else self._node_route(node_id)
        api_config = self._route_transport_identity(resolved_route)
        return self.build_current_node_binding(
            node_id,
            artifact_type="outline_artifact",
            artifact_version="v3",
            dependency_hashes={
                f"input_{index}": value
                for index, value in enumerate(sorted(str(item) for item in input_artifact_hashes if str(item)))
            },
            model=resolved_route.model,
            provider=semantic_node_id,
            prompt=request,
            prompt_template_hash=self._outline_prompt_identity.sha256,
            prompt_payload_hash=hash_json(request),
            api_config=api_config,
            route=resolved_route,
        )

    @staticmethod
    def _receipt_record_hash(receipt: Any) -> str:
        """Hash the canonical receipt record without retaining raw content."""

        to_dict = getattr(receipt, "to_dict", None)
        payload = to_dict() if callable(to_dict) else receipt
        return hash_json(payload)

    def _replay_receipt_index(self) -> dict[str, Any]:
        """Index only Registry-authorized receipts from current/prior epochs.

        A replay record is not authority by itself. Historical ledgers are
        accepted only after their Registry record, file hash, artifact schema,
        and dependency closure all verify. The local current-epoch staging file
        is included for same-epoch resume, then reconciled with its immutable
        Registry publication when one exists.
        """

        if self._replay_receipt_index_cache is not None:
            return dict(self._replay_receipt_index_cache)

        index: dict[str, Any] = {}
        sources: dict[str, ArtifactRecord] = {}
        invalid_ids: set[str] = set()
        diagnostics: list[str] = []

        def reject(source: str, reason: str) -> None:
            message = f"replay receipt source rejected: {source} ({reason})"
            diagnostics.append(message)

        def ingest(
            receipts: Sequence[Any],
            *,
            source_record: ArtifactRecord | None,
            source_epoch: str,
            source_label: str,
        ) -> None:
            if not receipts:
                reject(source_label, "empty ledger")
                return
            local_ids: set[str] = set()
            for receipt in receipts:
                receipt_id = str(getattr(receipt, "receipt_id", "") or "")
                if not receipt_id:
                    reject(source_label, "receipt id missing")
                    return
                if receipt_id in local_ids:
                    invalid_ids.add(receipt_id)
                    index.pop(receipt_id, None)
                    sources.pop(receipt_id, None)
                    reject(source_label, f"duplicate receipt id {receipt_id}")
                    return
                local_ids.add(receipt_id)
                if (
                    str(getattr(receipt, "job_id", "") or "") != self.job_id
                    or str(getattr(receipt, "stage_name", "") or "") != "outline_v3"
                    or str(getattr(receipt, "closure_epoch_id", "") or "") != source_epoch
                ):
                    reject(source_label, f"receipt {receipt_id} has an out-of-scope identity")
                    return

            for receipt in receipts:
                receipt_id = str(receipt.receipt_id)
                if receipt_id in invalid_ids:
                    continue
                previous = index.get(receipt_id)
                if previous is None:
                    index[receipt_id] = receipt
                    if source_record is not None:
                        sources[receipt_id] = source_record
                    continue
                same_record = self._receipt_record_hash(previous) == self._receipt_record_hash(receipt)
                previous_epoch = str(getattr(previous, "closure_epoch_id", "") or "")
                previous_source = sources.get(receipt_id)
                if same_record and previous_epoch == source_epoch:
                    # A resumed run publishes a content-addressed cumulative
                    # ledger. Its immutable Registry snapshots legitimately
                    # share the same receipt prefix, so an identical receipt
                    # from two ready ledger records is not a conflict. Keep a
                    # Registry record as the source authority when the first
                    # occurrence came from the local staging copy.
                    if source_record is not None and previous_source is None:
                        sources[receipt_id] = source_record
                    continue
                invalid_ids.add(receipt_id)
                index.pop(receipt_id, None)
                sources.pop(receipt_id, None)
                reject(source_label, f"conflicting receipt id {receipt_id}")

        try:
            current_receipts = self._receipt_ledger.list_receipts()
        except Exception as exc:  # ledger parser errors are a fail-closed input
            current_receipts = ()
            reject("current runtime ledger", type(exc).__name__)
        if current_receipts:
            ingest(
                current_receipts,
                source_record=None,
                source_epoch=self.closure_epoch_id,
                source_label="current runtime ledger",
            )

        try:
            registry_records = self.registry.list_records()
        except Exception as exc:
            registry_records = []
            reject("provider receipt Registry", type(exc).__name__)
        stable_registry_ids = {
            str(getattr(record, "artifact_id", "") or "")
            for record in registry_records
            if str(getattr(record, "artifact_type", "") or "") == "provider_receipt_ledger"
            and str(getattr(record, "status", "") or "") == "ready"
            and str(getattr(record, "job_id", "") or "") == self.job_id
            and str(getattr(record, "artifact_id", "") or "") == "outline_v3_provider_receipts"
        }
        for record in registry_records:
            if str(getattr(record, "artifact_type", "") or "") != "provider_receipt_ledger":
                continue
            if str(getattr(record, "status", "") or "") != "ready":
                continue
            if str(getattr(record, "job_id", "") or "") != self.job_id:
                reject(str(getattr(record, "artifact_id", "") or "<unknown>"), "wrong job")
                continue
            metadata = getattr(record, "metadata", {}) or {}
            if not isinstance(metadata, Mapping):
                reject(str(getattr(record, "artifact_id", "") or "<unknown>"), "metadata is not an object")
                continue
            if str(metadata.get("stage_name") or "") != "outline_v3":
                continue
            if stable_registry_ids and str(getattr(record, "artifact_id", "") or "") not in stable_registry_ids:
                # Content-addressed historical snapshots are forensic inputs;
                # the stable current ledger is the only replay authority. A
                # missing record in that authority must trigger a fresh call,
                # not be resurrected from an older snapshot.
                continue
            source_epoch = str(metadata.get("closure_epoch_id") or "")
            if not source_epoch:
                reject(str(getattr(record, "artifact_id", "") or "<unknown>"), "closure epoch missing")
                continue
            source_label = str(getattr(record, "artifact_id", "") or "<unknown ledger>")
            try:
                # This validates the Registry record's current file bytes,
                # schema, and every ready dependency before any receipt is read.
                self.registry.verify_ready_artifact_closure(record)
                before_hash = file_sha256(record.path)
                ledger = ProviderRuntimeLedger(record.path)
                receipts = ledger.list_receipts()
                after_hash = file_sha256(record.path)
                if before_hash != after_hash or before_hash != record.content_hash:
                    raise RegistryError("receipt ledger bytes changed during verification")
            except Exception as exc:
                reject(source_label, type(exc).__name__)
                continue
            ingest(
                receipts,
                source_record=record,
                source_epoch=source_epoch,
                source_label=source_label,
            )

        for message in dict.fromkeys(diagnostics):
            if message not in self._replay_receipt_diagnostics:
                self._replay_receipt_diagnostics.append(message)
            if message not in self.replay_diagnostics:
                self.replay_diagnostics.append(message)
        self._replay_receipt_sources = sources
        result = {
            receipt_id: receipt
            for receipt_id, receipt in index.items()
            if receipt_id not in invalid_ids
        }
        self._replay_receipt_index_cache = dict(result)
        return result

    def _node_execution_identity_hash(self, node_id: str) -> str:
        """Hash one upstream node's output and execution authority together."""

        node = self._dag.get(node_id)
        if node is None:
            return ""
        return hash_json(
            {
                "node_id": node.node_id,
                "output_hash": node.output_hash,
                "execution_binding": dict(node.execution_binding),
            }
        )

    def _replay_record_is_valid(self, record: Any, binding: Mapping[str, Any]) -> bool:
        normalized_hash = str(getattr(record, "normalized_output_hash", "") or getattr(record, "output_hash", ""))
        if not normalized_hash or not getattr(record, "receipt_ids", None):
            return False
        wanted_ids = {str(value) for value in record.receipt_ids}
        expected_receipts = {
            receipt_id: receipt
            for receipt_id, receipt in self._replay_receipt_index().items()
            if receipt_id in wanted_ids
        }
        if len(expected_receipts) != len(wanted_ids):
            return False
        semantic_node_id = str(binding.get("semantic_node_id") or self._semantic_node_id(str(binding.get("node_id") or "")))
        semantic_call_id = f"outline:{semantic_node_id}"
        receipt_node_id = OutlineV3Executor._semantic_receipt_node_id(semantic_node_id)
        prior_epoch_reused = any(
            str(getattr(receipt, "closure_epoch_id", "")) != self.closure_epoch_id
            for receipt in expected_receipts.values()
        )
        if prior_epoch_reused:
            self.replay_diagnostics.append(
                f"node {semantic_node_id}: reused {len(expected_receipts)} prior-epoch "
                "receipt(s); per-node config matched so reuse is verified, not forged"
            )
        for receipt in expected_receipts.values():
            if receipt.status != "success" or receipt.response_hash != normalized_hash:
                return False
            if receipt.job_id != self.job_id or receipt.attempt_id != semantic_call_id:
                return False
            if receipt.node_id != receipt_node_id or receipt.call_id != semantic_call_id:
                return False
            if receipt.prompt_hash != str(binding.get("prompt_hash") or ""):
                return False
            if receipt.input_hash != str(binding.get("prompt_payload_hash") or ""):
                return False
            if receipt.config_hash != str(binding.get("provider_config_hash") or ""):
                return False
            if str(binding.get("provider_family") or "") and receipt.provider != str(binding.get("provider_family") or ""):
                return False
            if str(binding.get("model_name") or "") and receipt.model != str(binding.get("model_name") or ""):
                return False
            if str(binding.get("endpoint_type") or "") and receipt.endpoint_type != str(binding.get("endpoint_type") or ""):
                return False
            if str(binding.get("api_base_host") or "") and receipt.endpoint != str(binding.get("api_base_host") or ""):
                return False
            if receipt.schema_hash != str(binding.get("schema_hash") or ""):
                return False
            if receipt.finish_reason == "length" or receipt.incomplete_reason:
                return False
            if bool(binding.get("endpoint_type") not in {"internal", "fixture"}) and receipt.usage_status not in {"reported", "provider_not_supported"}:
                return False
        return True

    @staticmethod
    def _artifact_record_hash(record: ArtifactRecord) -> str:
        """Hash the complete Registry record, including its dependencies."""

        return hash_json(asdict(record))

    def _verify_replay_output_authority(
        self,
        replay_record: Any,
        current_record: ArtifactRecord,
        payload: Mapping[str, Any],
    ) -> tuple[str, str] | None:
        """Verify replay outputs against current Registry authority."""

        output_ids = [
            str(item).strip()
            for item in (getattr(replay_record, "output_artifact_ids", None) or ())
            if str(item).strip()
        ]
        if not output_ids or current_record.artifact_id not in output_ids:
            return None
        try:
            self.registry.verify_ready_artifact_closure(current_record)
        except Exception:
            return None

        expected_content_hash = str(getattr(replay_record, "registered_artifact_hash", "") or "")
        expected_node_hash = str(getattr(replay_record, "node_output_hash", "") or "")
        resolved_content_hash = ""
        resolved_file_hash = ""
        for artifact_id in output_ids:
            registered = self.registry.get(artifact_id)
            if registered is None or registered.status != "ready" or registered.job_id != self.job_id:
                return None
            try:
                self.registry.verify_ready_artifact_closure(registered)
                envelope = json.loads(Path(registered.path).read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError, RegistryError, TypeError, ValueError):
                return None
            if not isinstance(envelope, Mapping):
                return None
            embedded_content_hash = str(envelope.get("content_hash") or "")
            embedded_payload = envelope.get("payload")
            if not embedded_content_hash or not isinstance(embedded_payload, Mapping):
                return None
            if hash_json(embedded_payload) != str(getattr(replay_record, "normalized_output_hash", "") or getattr(replay_record, "output_hash", "")):
                return None
            if expected_content_hash and embedded_content_hash != expected_content_hash:
                return None
            if expected_node_hash and embedded_content_hash != expected_node_hash:
                return None
            if registered.artifact_id == current_record.artifact_id:
                if registered.content_hash != current_record.content_hash:
                    return None
                if hash_json(payload) != hash_json(embedded_payload):
                    return None
            resolved_content_hash = embedded_content_hash
            resolved_file_hash = registered.content_hash
        if not resolved_content_hash or not resolved_file_hash:
            return None
        return resolved_content_hash, resolved_file_hash

    def _materialize_verified_reuse_evidence(
        self,
        node_id: str,
        binding: Mapping[str, Any],
        replay_record: Any,
        current_record: ArtifactRecord,
        payload: Mapping[str, Any],
    ) -> ArtifactRecord | None:
        """Register durable evidence for a validated prior-epoch replay."""

        receipt_ids = [
            str(item).strip()
            for item in (getattr(replay_record, "receipt_ids", None) or ())
            if str(item).strip()
        ]
        if len(receipt_ids) != 1:
            self.replay_diagnostics.append(
                f"node {node_id}: prior-epoch replay must bind exactly one receipt"
            )
            return None
        receipt_id = receipt_ids[0]
        receipt_index = self._replay_receipt_index()
        source_receipt = receipt_index.get(receipt_id)
        source_ledger = self._replay_receipt_sources.get(receipt_id)
        if source_receipt is None or source_ledger is None:
            self.replay_diagnostics.append(
                f"node {node_id}: prior-epoch receipt authority is unavailable"
            )
            return None
        try:
            self.registry.verify_ready_artifact_closure(source_ledger)
        except (OSError, RegistryError, TypeError, ValueError):
            self.replay_diagnostics.append(
                f"node {node_id}: prior-epoch receipt ledger authority changed"
            )
            return None
        source_epoch = str(getattr(source_receipt, "closure_epoch_id", "") or "")
        if not source_epoch or source_epoch == self.closure_epoch_id:
            return None
        output_authority = self._verify_replay_output_authority(
            replay_record,
            current_record,
            payload,
        )
        if output_authority is None:
            self.replay_diagnostics.append(
                f"node {node_id}: replay output Registry authority is invalid"
            )
            return None
        registered_content_hash, registered_file_hash = output_authority
        call_id = f"outline:{self._semantic_node_id(node_id)}"
        base_payload: dict[str, Any] = {
            "artifact_type": "provider_verified_reuse",
            "artifact_version": "v1",
            "job_id": self.job_id,
            "stage_name": "outline_v3",
            "current_logical_attempt_identity": self.logical_attempt_identity,
            "current_closure_epoch_id": self.closure_epoch_id,
            "call_id": call_id,
            "node_id": self._semantic_node_id(node_id),
            "current_binding": {
                "call_id": call_id,
                "node_id": self._semantic_node_id(node_id),
                "prompt_hash": str(binding.get("prompt_hash") or ""),
                "input_hash": str(binding.get("prompt_payload_hash") or ""),
                "config_hash": str(binding.get("provider_config_hash") or ""),
                "schema_hash": str(binding.get("schema_hash") or ""),
                "provider": str(binding.get("provider_family") or ""),
                "model": str(binding.get("model_name") or ""),
                "endpoint": str(binding.get("api_base_host") or ""),
                "endpoint_type": str(binding.get("endpoint_type") or ""),
            },
            "source_authority": {
                "source_closure_epoch_id": source_epoch,
                "source_receipt_ledger_artifact_id": source_ledger.artifact_id,
                "source_receipt_ledger_content_hash": source_ledger.content_hash,
                "source_receipt_id": receipt_id,
                "source_receipt_record_hash": self._receipt_record_hash(source_receipt),
            },
            "reused_output": {
                "replay_key_hash": str(getattr(replay_record.key, "key_hash", "") or ""),
                "normalized_output_hash": str(
                    getattr(replay_record, "normalized_output_hash", "")
                    or getattr(replay_record, "output_hash", "")
                ),
                "registered_artifact_id": current_record.artifact_id,
                "registered_artifact_content_hash": registered_content_hash,
                "registered_artifact_file_hash": registered_file_hash,
            },
        }
        base_payload["content_hash"] = hash_json(base_payload)
        evidence_id = (
            f"outline-v3:provider-verified-reuse:{self.closure_epoch_id}:"
            f"{hash_text(call_id)[:24]}"
        )
        safe_node = re.sub(r"[^A-Za-z0-9_.-]+", "_", self._semantic_node_id(node_id))
        evidence_path = self._path(
            f"outline_v3/reuse/{safe_node}_{self.closure_epoch_id[:24]}.json"
        )
        existing = self.registry.get(evidence_id)
        if existing is not None:
            if existing.status != "ready":
                return None
            try:
                self.registry.verify_ready_artifact_closure(existing)
                existing_payload = json.loads(Path(existing.path).read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError, RegistryError, TypeError, ValueError):
                return None
            if existing_payload != base_payload:
                self.replay_diagnostics.append(
                    f"node {node_id}: verified reuse evidence identity changed"
                )
                return None
            evidence_record = existing
        else:
            dependencies: list[ArtifactDependencyRefV2] = []
            for dependency in (source_ledger, current_record):
                dependency_ref = ArtifactDependencyRefV2.from_record(dependency)
                if all(item.artifact_id != dependency_ref.artifact_id for item in dependencies):
                    dependencies.append(dependency_ref)
            try:
                evidence_record = publish_json_artifact(
                    self.publication_context,
                    self.registry,
                    evidence_path,
                    base_payload,
                    artifact_role="provider_verified_reuse",
                    artifact_type="provider_verified_reuse",
                    artifact_version="v1",
                    producer="outline.v3_executor.OutlineV3Executor",
                    artifact_id=evidence_id,
                    depends_on=dependencies,
                    metadata={
                        "job_id": self.job_id,
                        "stage_name": "outline_v3",
                        "current_closure_epoch_id": self.closure_epoch_id,
                        "source_closure_epoch_id": source_epoch,
                        "call_id": call_id,
                        "node_id": self._semantic_node_id(node_id),
                    },
                )
            except (OSError, RegistryError, TypeError, ValueError) as exc:
                self.replay_diagnostics.append(
                    f"node {node_id}: verified reuse evidence registration failed "
                    f"({type(exc).__name__})"
                )
                return None
        self._verified_reuse_records[call_id] = evidence_record
        self._verified_reuse_source_receipt_ids[call_id] = receipt_id
        self.artifact_records[evidence_record.artifact_id] = evidence_record
        self.artifact_paths[evidence_record.artifact_id] = evidence_record.path
        return evidence_record

    def _verify_verified_reuse_evidence(
        self,
        expected_calls: Sequence[ExpectedProviderCall],
    ) -> list[ArtifactRecord]:
        """Re-verify every reuse authority before the current closure closes."""

        records: list[ArtifactRecord] = []
        receipt_index = self._replay_receipt_index()
        for expected in expected_calls:
            if not expected.verified_reuse:
                continue
            evidence_id = str(expected.reuse_evidence_artifact_id or "")
            evidence = self.registry.get(evidence_id)
            if evidence is None or evidence.status != "ready":
                raise OutlineV3ExecutionError(
                    f"verified reuse evidence is not ready for {expected.call_id}"
                )
            try:
                self.registry.verify_ready_artifact_closure(evidence)
                payload = json.loads(Path(evidence.path).read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError, RegistryError) as exc:
                raise OutlineV3ExecutionError(
                    f"verified reuse evidence cannot be verified for {expected.call_id}"
                ) from exc
            if not isinstance(payload, Mapping):
                raise OutlineV3ExecutionError(
                    f"verified reuse evidence payload is invalid for {expected.call_id}"
                )
            if str(payload.get("content_hash") or "") != hash_json(
                {key: value for key, value in payload.items() if key != "content_hash"}
            ):
                raise OutlineV3ExecutionError(
                    f"verified reuse evidence content hash is invalid for {expected.call_id}"
                )
            if (
                expected.reuse_evidence_artifact_hash != evidence.content_hash
                or expected.reuse_evidence_record_hash != self._artifact_record_hash(evidence)
            ):
                raise OutlineV3ExecutionError(
                    f"verified reuse evidence Registry identity changed for {expected.call_id}"
                )
            if (
                str(payload.get("job_id") or "") != self.job_id
                or str(payload.get("stage_name") or "") != "outline_v3"
                or str(payload.get("current_logical_attempt_identity") or "") != self.logical_attempt_identity
                or str(payload.get("current_closure_epoch_id") or "") != self.closure_epoch_id
                or str(payload.get("call_id") or "") != expected.call_id
                or str(payload.get("node_id") or "") != expected.node_id
            ):
                raise OutlineV3ExecutionError(
                    f"verified reuse evidence current identity changed for {expected.call_id}"
                )
            binding = payload.get("current_binding")
            if not isinstance(binding, Mapping) or any(
                str(binding.get(field) or "") != expected_value
                for field, expected_value in {
                    "call_id": expected.call_id,
                    "node_id": expected.node_id,
                    "prompt_hash": expected.prompt_hash,
                    "input_hash": expected.input_hash,
                    "config_hash": expected.config_hash,
                    "schema_hash": expected.schema_hash,
                    "provider": expected.provider,
                    "model": expected.model,
                    "endpoint": expected.endpoint,
                    "endpoint_type": expected.endpoint_type,
                }.items()
            ):
                raise OutlineV3ExecutionError(
                    f"verified reuse evidence binding mismatch for {expected.call_id}"
                )
            source = payload.get("source_authority")
            reused = payload.get("reused_output")
            if not isinstance(source, Mapping) or not isinstance(reused, Mapping):
                raise OutlineV3ExecutionError(
                    f"verified reuse evidence authority is incomplete for {expected.call_id}"
                )
            source_receipt_id = str(source.get("source_receipt_id") or "")
            source_receipt = receipt_index.get(source_receipt_id)
            source_ledger_id = str(source.get("source_receipt_ledger_artifact_id") or "")
            source_ledger = self.registry.get(source_ledger_id)
            if source_receipt is None or source_ledger is None or source_ledger.status != "ready":
                raise OutlineV3ExecutionError(
                    f"verified reuse source authority is unavailable for {expected.call_id}"
                )
            try:
                self.registry.verify_ready_artifact_closure(source_ledger)
            except (OSError, RegistryError, TypeError, ValueError) as exc:
                raise OutlineV3ExecutionError(
                    f"verified reuse source ledger is not authoritative for {expected.call_id}"
                ) from exc
            if (
                str(source.get("source_closure_epoch_id") or "")
                != str(getattr(source_receipt, "closure_epoch_id", "") or "")
                or str(source.get("source_closure_epoch_id") or "") == self.closure_epoch_id
                or str(source.get("source_receipt_ledger_content_hash") or "") != source_ledger.content_hash
                or str(source.get("source_receipt_record_hash") or "") != self._receipt_record_hash(source_receipt)
                or str(reused.get("replay_key_hash") or "") == ""
                or str(reused.get("normalized_output_hash") or "") != expected.normalized_output_hash
                or str(reused.get("registered_artifact_id") or "") == ""
                or str(reused.get("registered_artifact_content_hash") or "") != expected.artifact_content_hash
                or str(reused.get("registered_artifact_file_hash") or "") != expected.registry_file_hash
            ):
                raise OutlineV3ExecutionError(
                    f"verified reuse evidence source/output mismatch for {expected.call_id}"
                )
            reused_artifact = self.registry.get(
                str(reused.get("registered_artifact_id") or "")
            )
            if (
                reused_artifact is None
                or reused_artifact.status != "ready"
                or reused_artifact.job_id != self.job_id
                or str(reused_artifact.path) != str(expected.artifact_path)
                or reused_artifact.content_hash != expected.registry_file_hash
            ):
                raise OutlineV3ExecutionError(
                    f"verified reuse registered output mismatch for {expected.call_id}"
                )
            dependency_ids = {
                str(dependency.artifact_id)
                for dependency in evidence.depends_on
                if str(dependency.artifact_id)
            }
            if source_ledger.artifact_id not in dependency_ids or str(reused.get("registered_artifact_id") or "") not in dependency_ids:
                raise OutlineV3ExecutionError(
                    f"verified reuse evidence dependencies are incomplete for {expected.call_id}"
                )
            records.append(evidence)
        return records

    def _register_expected_from_binding(self, node_id: str, binding: Mapping[str, Any]) -> str:
        semantic_node_id = str(binding.get("semantic_node_id") or self._semantic_node_id(node_id))
        call_id = f"outline:{semantic_node_id}"
        self._expected_provider_calls[call_id] = ExpectedProviderCall(
            call_id=call_id,
            job_id=self.job_id,
            attempt_id=call_id,
            stage_name="outline_v3",
            node_id=self._semantic_receipt_node_id(semantic_node_id),
            closure_epoch_id=self.closure_epoch_id,
            logical_attempt_identity=self.logical_attempt_identity,
            expected_call_graph_hash=self.expected_call_graph_hash,
            prompt_id=str(binding.get("prompt_id") or ""),
            prompt_version=str(binding.get("prompt_version") or ""),
            prompt_sha256=str(binding.get("prompt_sha256") or ""),
            prompt_hash=str(binding.get("prompt_hash") or ""),
            input_hash=str(binding.get("prompt_payload_hash") or ""),
            config_hash=str(binding.get("provider_config_hash") or ""),
            schema_hash=str(binding.get("schema_hash") or _hash_payload({"node_id": semantic_node_id, "expect_json": True})),
            max_attempts=max(1, int(binding.get("transport_retries") or 0) + 1),
            provider=str(binding.get("provider_family") or ""),
            model=str(binding.get("model_name") or ""),
            endpoint=str(binding.get("api_base_host") or ""),
            endpoint_type=str(binding.get("endpoint_type") or ""),
            usage_required=str(binding.get("endpoint_type") or "") not in {"internal", "fixture"},
        )
        return call_id

    def _hydrate_expected_provider_calls(self) -> None:
        """Rebuild expected calls from current execution inputs, never receipts."""

        known_ids = {f"outline:{node_id}" for node_id in self._provider_node_ids()}
        for call_id in sorted(known_ids):
            node_id = call_id.removeprefix("outline:")
            try:
                route = self._node_route(node_id)
                route_identity = self._route_transport_identity(route)
            except (KeyError, TypeError, AttributeError):
                route = None
                route_identity = {}
            self._expected_provider_calls[call_id] = ExpectedProviderCall(
                call_id=call_id,
                job_id=self.job_id,
                attempt_id=call_id,
                stage_name="outline_v3",
                node_id=node_id,
                closure_epoch_id=self.closure_epoch_id,
                logical_attempt_identity=self.logical_attempt_identity,
                expected_call_graph_hash=self.expected_call_graph_hash,
                prompt_id=self._outline_prompt_identity.prompt_id,
                prompt_version=self._outline_prompt_identity.version,
                prompt_sha256=self._outline_prompt_identity.sha256,
                provider=route.provider_name if route is not None else self.profile.provider,
                model=route.model if route is not None else self.profile.model,
                endpoint=route.api_base_host if route is not None else "",
                endpoint_type=route.endpoint_type if route is not None else self.profile.endpoint_type,
                config_hash=hash_json(route_identity) if route_identity else "",
                max_attempts=max(1, int((route.config_identity if route is not None else {}).get("transport_retries") or 0) + 1),
                usage_required=(route.endpoint_type if route is not None else self.profile.endpoint_type)
                not in {"internal", "fixture"},
            )

    def _record_expected_provider_call(
        self,
        node_id: str,
        request: Mapping[str, Any],
        *,
        expect_json: bool,
        api_config: Mapping[str, Any],
    ) -> str:
        semantic_node_id = self._semantic_node_id(node_id)
        call_id = f"outline:{semantic_node_id}"
        prompt = json.dumps(request, sort_keys=True, ensure_ascii=False)
        self._expected_provider_calls[call_id] = ExpectedProviderCall(
            call_id=call_id,
            job_id=self.job_id,
            attempt_id=call_id,
            stage_name="outline_v3",
            node_id=semantic_node_id,
            closure_epoch_id=self.closure_epoch_id,
            logical_attempt_identity=self.logical_attempt_identity,
            expected_call_graph_hash=self.expected_call_graph_hash,
            prompt_id=self._outline_prompt_identity.prompt_id,
            prompt_version=self._outline_prompt_identity.version,
            prompt_sha256=self._outline_prompt_identity.sha256,
            prompt_hash=hash_text(prompt),
            input_hash=hash_json(request),
            config_hash=hash_json(api_config),
            schema_hash=_hash_payload({"node_id": semantic_node_id, "expect_json": expect_json}),
            max_attempts=max(1, int(api_config.get("transport_retries") or 0) + 1),
            provider=str(api_config.get("provider_family") or ""),
            model=str(api_config.get("model") or ""),
            endpoint=str(api_config.get("api_base") or ""),
            endpoint_type=str(api_config.get("endpoint_type") or ""),
            usage_required=str(api_config.get("endpoint_type") or "") not in {"internal", "fixture"},
        )
        return call_id

    def _check(self, node_id: str, *, phase: str = "before") -> None:
        if self.cancellation_checker is not None:
            self.cancellation_checker()
        # Pause is an explicit durable admission gate, separate from
        # cancellation.  It is checked immediately before local/provider node
        # work so a stale UI interruption cannot silently start another call.
        if phase == "before":
            self._pause_state.assert_runnable(node_id=node_id)
        if self.fault_injector is not None:
            self.fault_injector(
                node_id,
                {"job_id": self.job_id, "node_id": node_id, "phase": phase},
            )

    def _dependency_refs(self, dependency_ids: Sequence[str]) -> list[ArtifactDependencyRefV2]:
        refs: list[ArtifactDependencyRefV2] = []
        for dependency_id in dependency_ids:
            record = self.artifact_records.get(dependency_id)
            if record is None:
                continue
            refs.append(ArtifactDependencyRefV2(
                dependency_kind="local_job",
                job_id=record.job_id,
                artifact_id=record.artifact_id,
                artifact_type=record.artifact_type,
                path=record.path,
                content_hash=record.content_hash,
            ))
        return refs

    def _persist(
        self,
        node_id: str,
        artifact: OutlineArtifact,
        *,
        depends_on: Sequence[str] = (),
        model: str = "deterministic",
        provider: str = "local",
        execution_binding: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        path = self._node_path(node_id)
        artifact_id = (
            f"provider-receipt-closure:outline_v3:{self.closure_epoch_id}"
            if node_id == "provider_receipt_closure"
            else f"outline-v3:{node_id}"
        )
        record = publish_json_artifact(
            self.publication_context,
            self.registry,
            path,
            artifact.to_dict(),
            artifact_role="outline_v3_node_output",
            artifact_type=artifact.artifact_type,
            artifact_version=artifact.artifact_version,
            producer="outline.v3_executor.OutlineV3Executor",
            artifact_id=artifact_id,
            depends_on=self._dependency_refs(depends_on),
            metadata={
                "job_id": self.job_id,
                "node_id": node_id,
                "content_hash": artifact.content_hash,
                "model": model,
                "provider": provider,
                "closure_epoch_id": self.closure_epoch_id if node_id == "provider_receipt_closure" else "",
            },
        )
        if node_id == "provider_receipt_closure":
            # Preserve the historical lookup alias for downstream compatibility
            # while making the epoch-scoped record the canonical identity.
            legacy_record = publish_json_artifact(
                self.publication_context,
                self.registry,
                path,
                artifact.to_dict(),
                artifact_role="outline_v3_node_output_legacy_alias",
                artifact_type=artifact.artifact_type,
                artifact_version=artifact.artifact_version,
                producer="outline.v3_executor.OutlineV3Executor",
                artifact_id="outline-v3:provider_receipt_closure",
                depends_on=self._dependency_refs(depends_on),
                metadata={
                    "job_id": self.job_id,
                    "node_id": node_id,
                    "content_hash": artifact.content_hash,
                    "closure_epoch_id": self.closure_epoch_id,
                    "canonical_artifact_id": artifact_id,
                },
            )
            self.artifact_records["provider_receipt_closure_legacy"] = legacy_record
        self.artifact_paths[node_id] = record.path
        self.artifact_records[node_id] = record
        self._payloads[node_id] = dict(artifact.payload)
        self._update_request_audit_artifact_ref(node_id, record)
        binding = dict(execution_binding or self.build_current_node_binding(
            node_id,
            artifact_type=artifact.artifact_type,
            artifact_version=artifact.artifact_version,
            dependency_hashes=dict(artifact.dependency_hashes),
            model=model,
            provider=provider,
        ))
        self._dag = self._node_store.record_node(
            node_id,
            status="succeeded",
            input_hash=_hash_payload(dict(artifact.dependency_hashes)),
            output_hash=artifact.content_hash,
            output_artifact_ids=(artifact_id,),
            model_route=provider,
            model_name=model,
            provider=provider,
            config_snapshot={"candidate_count": self.candidate_count},
            budget_snapshot={"input_budget": self.profile.input_budget},
            receipt_ids=tuple(
                [self._expected_provider_calls[f"outline:{node_id}"].call_id]
                if f"outline:{node_id}" in self._expected_provider_calls else self.receipts
            ),
            execution_binding=binding,
        )
        expected = self._expected_provider_calls.get(self._provider_call_id(node_id))
        if expected is not None:
            expected = replace(
                expected,
                provider_response_hash=expected.provider_response_hash or expected.normalized_output_hash,
                artifact_payload_hash=hash_json(artifact.payload),
                artifact_content_hash=artifact.content_hash,
                registry_file_hash=record.content_hash,
                artifact_path=record.path,
                registered_artifact_hash=artifact.content_hash,
                node_output_hash=artifact.content_hash,
            )
            self._expected_provider_calls[expected.call_id] = expected
            pending = self._pending_replays.pop(node_id, None)
            if pending is not None and expected.normalized_output_hash:
                replay_key, normalized_hash, receipt_id = pending
                self._replay_store.append(
                    replay_key,
                    output_hash=normalized_hash,
                    normalized_output_hash=normalized_hash,
                    registered_artifact_hash=artifact.content_hash,
                    node_output_hash=artifact.content_hash,
                    output_artifact_ids=(artifact_id,),
                    receipt_ids=(receipt_id,),
                    audit_node_id=node_id,
                    closure_epoch_id=self.closure_epoch_id,
                )
                self._expected_provider_calls[expected.call_id] = replace(
                    self._expected_provider_calls[expected.call_id],
                    replay_output_hash=normalized_hash,
                )
        return dict(artifact.payload)

    def _load_node(self, node_id: str, expected_binding: Mapping[str, Any] | None = None) -> dict[str, Any] | None:
        node = self._dag.get(node_id)
        if node is None:
            return None
        binding = dict(expected_binding or self.build_current_node_binding(node_id))
        record = self.registry.get(f"outline-v3:{node_id}")
        if record is None or record.status != "ready":
            record = self.registry.get(
                f"provider-receipt-closure:outline_v3:{self.closure_epoch_id}"
                if node_id == "provider_receipt_closure"
                else f"outline-v3:{node_id}"
            )
        path = str(record.path) if record is not None else self._node_path(node_id)
        recoverable_failed_provider = (
            node.status == "failed" and node_id in self._provider_node_ids()
        )
        if node.status != "succeeded" and not recoverable_failed_provider:
            return None
        if not Path(path).is_file():
            return None
        if node.execution_binding != binding:
            semantic_static = (
                self.semantic_provider_synthesis_enabled
                and node_id in {"topic_synthesis", "cross_group_comparison", "global_synthesis"}
            )
            if semantic_static and node.status == "succeeded":
                identity_fields = (
                    "provider_route",
                    "provider_family",
                    "model_name",
                    "endpoint_type",
                    "route_fingerprint",
                    "context_profile_hash",
                    "current_summary_hashes",
                    "review_intent_hash",
                    "coverage_contract_hash",
                    "quality_gate_hash",
                    "relevant_runtime_config_hash",
                )
                if all(
                    str(node.execution_binding.get(field) or "")
                    == str(binding.get(field) or "")
                    for field in identity_fields
                ):
                    # Derived semantic artifacts may carry provider output
                    # hashes whose dependency projection changes when the
                    # Registry is rehydrated.  Source/config identity is the
                    # reusable boundary; the provider receipts remain bound
                    # to their own immutable response artifacts.
                    binding = dict(node.execution_binding)
            if node.execution_binding == binding:
                pass
            elif node.status == "succeeded":
                self._dag = self._node_store.invalidate_subgraph(node_id, reason="execution_binding_changed")
                return None
            else:
                return None
        try:
            value = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
        envelope_content_hash = str(value.get("content_hash") or "") if isinstance(value, Mapping) else ""
        if (
            not isinstance(value, Mapping)
            or not envelope_content_hash
            or (
                node.status == "succeeded"
                and envelope_content_hash != str(node.output_hash or "")
            )
        ):
            return None
        payload = value.get("payload")
        if not isinstance(payload, Mapping):
            return None
        if (
            node_id in {
                "topic_synthesis",
                "cross_group_comparison",
                "global_synthesis",
            }
            and payload.get("interpretation_contract_version")
            != INTERPRETATION_CONTRACT_VERSION
        ):
            self._dag = self._node_store.invalidate_subgraph(
                node_id, reason="interpretation_contract_changed"
            )
            return None
        if (
            self.semantic_provider_synthesis_enabled
            and node_id in {"cross_group_comparison", "global_synthesis"}
            and not self._shared_semantic_cache_valid(node_id, payload)
        ):
            self._dag = self._node_store.invalidate_subgraph(
                node_id, reason="shared_semantic_contract_changed"
            )
            return None
        artifact_output_hash = (
            envelope_content_hash
            if recoverable_failed_provider
            else str(node.output_hash or "")
        )
        if record is None or record.status != "ready":
            return None
        try:
            self.registry.verify_ready_artifact_closure(record)
        except Exception:
            return None
        replay: Any | None = None
        if node_id in self._provider_node_ids():
            replay_key = ModelCallReplayKey(
                node_id=node_id,
                node_version=str(binding.get("node_version") or "v3"),
                schema_version=str(binding.get("schema_version") or "outline-v3"),
                model_route=str(binding.get("provider_family") or self.profile.provider),
                model_name=str(binding.get("model_name") or self.profile.model),
                provider=str(binding.get("provider_family") or self.profile.provider),
                prompt_template_hash=str(binding.get("prompt_template_hash") or ""),
                prompt_payload_hash=str(binding.get("prompt_payload_hash") or ""),
                input_artifact_hashes=list(dict(binding.get("dependency_hashes") or {}).values()),
                config_hash=str(binding.get("provider_config_hash") or ""),
                execution_binding_hash=self._replay_binding_hash(binding),
            )
            replay = self._replay_store.lookup(replay_key)
            if not replay.reusable or replay.record is None:
                self.replay_diagnostics.append(
                    f"{node_id}: replay lookup {replay.status}; "
                    f"stale reasons={list(replay.stale_reasons)}"
                )
                return None
            if not self._replay_record_is_valid(replay.record, binding):
                self.replay_diagnostics.append(f"{node_id}: replay record failed receipt/closure validation")
                return None
            payload_hash = hash_json(payload)
            normalized_hash = replay.record.normalized_output_hash or replay.record.output_hash
            if payload_hash != normalized_hash:
                self.replay_diagnostics.append(f"{node_id}: adopted artifact differs from replay output hash")
                return None
            call_id = self._register_expected_from_binding(node_id, binding)
            replay_receipts = self._replay_receipt_index()
            prior_epoch_reused = any(
                str(getattr(replay_receipts.get(str(receipt_id)), "closure_epoch_id", "") or "")
                != self.closure_epoch_id
                for receipt_id in replay.record.receipt_ids
                if str(receipt_id) in replay_receipts
            )
            if prior_epoch_reused:
                reuse_evidence = self._materialize_verified_reuse_evidence(
                    node_id,
                    binding,
                    replay.record,
                    record,
                    payload,
                )
                if reuse_evidence is None:
                    # A prior receipt is never promoted into the current
                    # ledger. If its authority cannot be materialized, rerun
                    # this node through the configured transport instead.
                    return None
                expected = self._expected_provider_calls[call_id]
                self._expected_provider_calls[call_id] = replace(
                    expected,
                    provider_response_hash=normalized_hash,
                    output_hash=normalized_hash,
                    normalized_output_hash=normalized_hash,
                    artifact_payload_hash=hash_json(payload),
                    artifact_content_hash=artifact_output_hash,
                    registry_file_hash=record.content_hash,
                    artifact_path=record.path,
                    registered_artifact_hash=artifact_output_hash,
                    replay_output_hash=replay.record.output_hash,
                    node_output_hash=artifact_output_hash,
                    verified_reuse=True,
                    reuse_evidence_artifact_id=reuse_evidence.artifact_id,
                    reuse_evidence_artifact_hash=reuse_evidence.content_hash,
                    reuse_evidence_record_hash=self._artifact_record_hash(reuse_evidence),
                )
            else:
                # A same-epoch replay is an ordinary observed receipt. Keep it
                # visible to the current closure and do not label it reuse.
                for receipt_id in replay.record.receipt_ids:
                    if str(receipt_id) not in self.receipts:
                        self.receipts.append(str(receipt_id))
                self._expected_provider_calls[call_id] = replace(
                    self._expected_provider_calls[call_id],
                    provider_response_hash=normalized_hash,
                    output_hash=normalized_hash,
                    normalized_output_hash=normalized_hash,
                    artifact_payload_hash=hash_json(payload),
                        artifact_content_hash=artifact_output_hash,
                    registry_file_hash=record.content_hash,
                    artifact_path=record.path,
                        registered_artifact_hash=artifact_output_hash,
                    replay_output_hash=replay.record.output_hash,
                        node_output_hash=artifact_output_hash,
                )
            self._replay_evidence.append({
                "node_id": node_id,
                "semantic_node_id": node_id,
                "closure_epoch_id": self.closure_epoch_id,
                "key_hash": replay_key.key_hash,
                "lookup_status": "hit",
                "provider_invoked": False,
                "verified_reuse": prior_epoch_reused,
                "reuse_evidence_artifact_id": (
                    self._expected_provider_calls[call_id].reuse_evidence_artifact_id
                    if prior_epoch_reused
                    else ""
                ),
                "reused_artifact_ids": list(replay.record.output_artifact_ids),
                "reused_receipt_ids": list(replay.record.receipt_ids),
                "reused_artifact_id": str(replay.record.output_artifact_ids[0]) if replay.record.output_artifact_ids else "",
                "reused_receipt_id": str(replay.record.receipt_ids[0]) if replay.record.receipt_ids else "",
            })
        self.artifact_paths[node_id] = record.path
        self.artifact_records[node_id] = record
        self._payloads[node_id] = dict(payload)
        if recoverable_failed_provider and replay is not None and replay.record is not None:
            self._dag = self._node_store.record_node(
                node_id,
                status="succeeded",
                input_hash=_hash_payload(dict(binding.get("dependency_hashes") or {})),
                output_hash=envelope_content_hash,
                output_artifact_ids=(record.artifact_id,),
                model_route=str(binding.get("provider_route") or ""),
                model_name=str(binding.get("model_name") or ""),
                provider=str(binding.get("provider_family") or ""),
                config_snapshot={"candidate_count": self.candidate_count},
                budget_snapshot={"input_budget": self.profile.input_budget},
                receipt_ids=tuple(
                    str(receipt_id)
                    for receipt_id in replay.record.receipt_ids
                    if str(receipt_id)
                ),
                execution_binding=binding,
            )
        return dict(payload)

    def _fixture_response(self, node_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        if node_id == "relation_adjudication":
            candidates = [
                dict(item)
                for item in (payload.get("relation_candidates") or [])
                if isinstance(item, Mapping)
            ]
            confirmed = [
                str(item.get("relation_id") or "")
                for item in candidates
                if item.get("relation_id") and item.get("evidence_fields")
            ]
            return {
                "status": "success",
                "content": {
                    "confirmed_relation_ids": confirmed,
                    "rejected_relations": [
                        {
                            "relation_id": str(item.get("relation_id") or ""),
                            "reason": "insufficient evidence fields",
                        }
                        for item in candidates
                        if str(item.get("relation_id") or "") not in confirmed
                    ],
                    "method": "fixture_evidence_adjudication",
                },
            }
        if node_id.endswith("_provider_generation"):
            candidate_id = node_id.removesuffix("_provider_generation")
            papers = list(payload.get("paper_keys") or ())
            logic = str(payload.get("organizing_logic") or "evidence")
            evidence_rows = [
                dict(item)
                for item in (payload.get("evidence") or [])
                if isinstance(item, Mapping)
            ]
            claims = []
            for row in evidence_rows[:3]:
                title = str(row.get("title") or row.get("paper_key") or "Evidence")
                finding_values = row.get("findings") or row.get("conclusions") or []
                finding = str(finding_values[0] if isinstance(finding_values, list) and finding_values else finding_values or "recorded finding")
                claims.append(f"{title}: {finding}")
            if not claims:
                claims = [f"The corpus records evidence organized by {logic}." ]
            sections = [
                {
                    "section_id": f"{candidate_id}_section_1",
                    "title": f"{logic.replace('_', ' ').title()} synthesis",
                    "goal": "Integrate evidence by research logic",
                    "paper_keys": papers,
                    "relation_ids": list(payload.get("relation_ids") or ())[:8],
                    "claims": claims,
                }
            ]
            return {"status": "success", "content": {"candidate_id": candidate_id, "organizing_logic": logic, "sections": sections, "claims": claims}}
        if node_id.endswith("_critique") or node_id in {"structure_critique", "coverage_critique", "evidence_critique"}:
            return {"status": "success", "content": {"node_id": node_id, "passed": True, "blocking_diagnostics": [], "recommendations": [], "score": 1.0}}
        if node_id == "arbitration":
            candidate_ids = list(payload.get("candidate_ids") or ["candidate_1"])
            selected = sorted(str(item) for item in candidate_ids)[0]
            content = {
                "selected_candidate_id": selected,
                "selection_reasons": ["fixture selected the lexicographically stable candidate after receiving all candidate content"],
                "candidate_comparison": {str(item): {"coverage": "available", "evidence": "available", "structure": "available"} for item in candidate_ids},
                "accepted_recommendations": [],
                "rejected_recommendations": [],
                "unresolved_risks": [],
            }
            contract = payload.get("section_coordination_contract") or {}
            if selected in (contract.get("required_if_selected_candidate_sharded") or ()):
                candidate_content = (payload.get("candidate_contents") or {}).get(selected) or {}
                content["section_coordination"] = {
                    "candidate_id": selected,
                    "merge_groups": [],
                    "section_order": [
                        str(section.get("section_id") or "")
                        for section in candidate_content.get("sections") or ()
                        if isinstance(section, Mapping)
                    ],
                }
            return {"status": "success", "content": content}
        return {"status": "success", "content": {"node_id": node_id, "accepted": True}}

    def _persist_stability_output(
        self,
        node_id: str,
        payload: Mapping[str, Any],
        *,
        dependency_hashes: Mapping[str, str],
        binding: Mapping[str, Any],
    ) -> None:
        """Persist a stability provider output without adding a DAG node.

        Stability reruns are an execution audit over the canonical DAG.  They
        still need immutable Registry and replay identities, but must not
        masquerade as normal outline nodes or mutate the dependency graph.
        """

        artifact = self._artifact(OutlineArtifact, payload, dependency_hashes)
        artifact_id = f"outline-v3:stability:{hash_text(node_id)[:24]}"
        record = publish_json_artifact(
            self.publication_context,
            self.registry,
            self._node_path(node_id),
            artifact.to_dict(),
            artifact_role="outline_v3_stability_provider_output",
            artifact_type="outline_stability_provider_output",
            artifact_version="v1",
            producer="outline.v3_executor.OutlineV3Executor",
            artifact_id=artifact_id,
            metadata={
                "job_id": self.job_id,
                "node_id": node_id,
                "content_hash": artifact.content_hash,
            },
        )
        self.artifact_paths[node_id] = record.path
        self.artifact_records[node_id] = record
        self._payloads[node_id] = dict(payload)
        expected = self._expected_provider_calls.get(self._provider_call_id(node_id))
        if expected is not None:
            expected = replace(
                expected,
                artifact_payload_hash=hash_json(payload),
                artifact_content_hash=artifact.content_hash,
                registry_file_hash=record.content_hash,
                artifact_path=record.path,
                registered_artifact_hash=artifact.content_hash,
                node_output_hash=artifact.content_hash,
            )
            self._expected_provider_calls[expected.call_id] = expected
            pending = self._pending_replays.pop(node_id, None)
            if pending is not None and expected.normalized_output_hash:
                replay_key, normalized_hash, receipt_id = pending
                self._replay_store.append(
                    replay_key,
                    output_hash=normalized_hash,
                    normalized_output_hash=normalized_hash,
                    registered_artifact_hash=artifact.content_hash,
                    node_output_hash=artifact.content_hash,
                    output_artifact_ids=(artifact_id,),
                    receipt_ids=(receipt_id,),
                    audit_node_id=node_id,
                    closure_epoch_id=self.closure_epoch_id,
                )
                self._expected_provider_calls[expected.call_id] = replace(
                    self._expected_provider_calls[expected.call_id],
                    replay_output_hash=normalized_hash,
                )

    def _persist_relation_shard_output(
        self,
        node_id: str,
        payload: Mapping[str, Any],
        *,
        dependency_hashes: Mapping[str, str],
        binding: Mapping[str, Any],
    ) -> None:
        """Persist a dynamic relation shard result without mutating the DAG."""

        artifact = self._artifact(RelationAdjudicationResult, payload, dependency_hashes)
        artifact_id = (
            f"outline-v3:relation-shard:{self.closure_epoch_id}:{hash_text(node_id)[:16]}"
        )
        record = publish_json_artifact(
            self.publication_context,
            self.registry,
            self._node_path(node_id),
            artifact.to_dict(),
            artifact_role="outline_v3_relation_shard_output",
            artifact_type=artifact.artifact_type,
            artifact_version=artifact.artifact_version,
            producer="outline.v3_executor.OutlineV3Executor",
            artifact_id=artifact_id,
            metadata={
                "job_id": self.job_id,
                "node_id": node_id,
                "parent_node": "relation_shard_plan",
                "content_hash": artifact.content_hash,
                "closure_epoch_id": self.closure_epoch_id,
            },
        )
        self.artifact_paths[node_id] = record.path
        self.artifact_records[node_id] = record
        self._payloads[node_id] = dict(payload)
        self._update_request_audit_artifact_ref(node_id, record)
        expected = self._expected_provider_calls.get(self._provider_call_id(node_id))
        if expected is None:
            return
        expected = replace(
            expected,
            artifact_payload_hash=hash_json(payload),
            artifact_content_hash=artifact.content_hash,
            registry_file_hash=record.content_hash,
            artifact_path=record.path,
            registered_artifact_hash=artifact.content_hash,
            node_output_hash=artifact.content_hash,
        )
        self._expected_provider_calls[expected.call_id] = expected
        pending = self._pending_replays.pop(node_id, None)
        if pending is None or not expected.normalized_output_hash:
            return
        replay_key, normalized_hash, receipt_id = pending
        self._replay_store.append(
            replay_key,
            output_hash=normalized_hash,
            normalized_output_hash=normalized_hash,
            registered_artifact_hash=artifact.content_hash,
            node_output_hash=artifact.content_hash,
            output_artifact_ids=(artifact_id,),
            receipt_ids=(receipt_id,),
            audit_node_id=node_id,
            closure_epoch_id=self.closure_epoch_id,
        )
        self._expected_provider_calls[expected.call_id] = replace(
            self._expected_provider_calls[expected.call_id],
            replay_output_hash=normalized_hash,
        )

    def _persist_candidate_shard_output(
        self,
        node_id: str,
        payload: Mapping[str, Any],
        *,
        dependency_hashes: Mapping[str, str],
    ) -> None:
        """Persist one dynamic candidate-generation shard without a DAG node."""

        artifact = self._artifact(OutlineCandidate, payload, dependency_hashes)
        artifact_id = (
            f"outline-v3:candidate-shard:{self.closure_epoch_id}:{hash_text(node_id)[:16]}"
        )
        record = publish_json_artifact(
            self.publication_context,
            self.registry,
            self._node_path(node_id),
            artifact.to_dict(),
            artifact_role="outline_v3_candidate_shard_output",
            artifact_type=artifact.artifact_type,
            artifact_version=artifact.artifact_version,
            producer="outline.v3_executor.OutlineV3Executor",
            artifact_id=artifact_id,
            metadata={
                "job_id": self.job_id,
                "node_id": node_id,
                "parent_node": "candidate_provider_generation",
                "content_hash": artifact.content_hash,
                "closure_epoch_id": self.closure_epoch_id,
            },
        )
        self.artifact_paths[node_id] = record.path
        self.artifact_records[node_id] = record
        self._payloads[node_id] = dict(payload)
        self._update_request_audit_artifact_ref(node_id, record)
        expected = self._expected_provider_calls.get(self._provider_call_id(node_id))
        if expected is not None:
            self._expected_provider_calls[expected.call_id] = replace(
                expected,
                artifact_payload_hash=hash_json(payload),
                artifact_content_hash=artifact.content_hash,
                registry_file_hash=record.content_hash,
                artifact_path=record.path,
                registered_artifact_hash=artifact.content_hash,
                node_output_hash=artifact.content_hash,
            )

    def _persist_semantic_provider_output(
        self,
        node_id: str,
        payload: Mapping[str, Any],
        *,
        dependency_hashes: Mapping[str, str],
    ) -> ArtifactRecord:
        """Register one topic/cross/global provider response for closure.

        Semantic synthesis is represented by a DAG node plus one immutable
        response artifact per physical provider call.  Bundling the response
        only inside the parent node would leave the receipt without a durable
        output identity and make resume incorrectly report a stale call.
        """

        artifact = self._artifact(OutlineArtifact, dict(payload), dependency_hashes)
        artifact_id = f"outline-v3:semantic-provider:{hash_text(node_id)[:24]}"
        record = publish_json_artifact(
            self.publication_context,
            self.registry,
            self._node_path(f"semantic_provider/{node_id}"),
            artifact.to_dict(),
            artifact_role="outline_v3_semantic_provider_output",
            artifact_type="outline_artifact",
            artifact_version="v3",
            producer="outline.v3_executor.OutlineV3Executor",
            artifact_id=artifact_id,
            metadata={"job_id": self.job_id, "node_id": node_id, "content_hash": artifact.content_hash},
        )
        self.artifact_paths[node_id] = record.path
        self.artifact_records[node_id] = record
        expected = self._expected_provider_calls.get(self._provider_call_id(node_id))
        if expected is not None:
            self._expected_provider_calls[expected.call_id] = replace(
                expected,
                artifact_payload_hash=hash_json(payload),
                artifact_content_hash=artifact.content_hash,
                registry_file_hash=record.content_hash,
                artifact_path=record.path,
                registered_artifact_hash=artifact.content_hash,
                node_output_hash=artifact.content_hash,
            )
            pending = self._pending_replays.pop(node_id, None)
            if pending is not None and expected.normalized_output_hash:
                replay_key, normalized_hash, receipt_id = pending
                self._replay_store.append(
                    replay_key,
                    output_hash=normalized_hash,
                    normalized_output_hash=normalized_hash,
                    registered_artifact_hash=artifact.content_hash,
                    node_output_hash=artifact.content_hash,
                    output_artifact_ids=(artifact_id,),
                    receipt_ids=(receipt_id,),
                    audit_node_id=node_id,
                    closure_epoch_id=self.closure_epoch_id,
                )
                self._expected_provider_calls[expected.call_id] = replace(
                    self._expected_provider_calls[expected.call_id],
                    replay_output_hash=normalized_hash,
                )
            base_node = self._semantic_receipt_node_id(node_id)
            base_record = self.artifact_records.get(base_node)
            binding = self._dynamic_provider_bindings.get(node_id)
            if base_record is not None and binding is not None:
                static_binding = self.build_current_node_binding(base_node)
                base_output_hash = base_record.content_hash
                try:
                    base_envelope = json.loads(Path(base_record.path).read_text(encoding="utf-8"))
                    if isinstance(base_envelope, Mapping) and str(base_envelope.get("content_hash") or ""):
                        base_output_hash = str(base_envelope["content_hash"])
                except (OSError, UnicodeError, json.JSONDecodeError, TypeError):
                    pass
                receipt_ids = [
                    receipt.receipt_id
                    for receipt in self._receipt_ledger.list_receipts()
                    if receipt.call_id == expected.call_id
                ]
                self._dag = self._node_store.record_node(
                    base_node,
                    status="succeeded",
                    input_hash=_hash_payload(dict(static_binding.get("dependency_hashes") or {})),
                    output_hash=base_output_hash,
                    output_artifact_ids=(base_record.artifact_id,),
                    model_route=str(static_binding.get("provider_route") or ""),
                    model_name=str(static_binding.get("model_name") or ""),
                    provider=str(static_binding.get("provider_family") or ""),
                    config_snapshot={"candidate_count": self.candidate_count},
                    budget_snapshot={"input_budget": self._node_route("candidate_1_provider_generation").profile.input_budget},
                    receipt_ids=receipt_ids,
                    execution_binding=static_binding,
                )
        return record

    def _persist_semantic_reducer_input_manifest(
        self,
        node_id: str,
        manifest: Mapping[str, Any],
        *,
        dependency_hashes: Mapping[str, str],
    ) -> ArtifactRecord:
        """Bind a reducer call to its complete local member set before POST."""

        artifact = self._artifact(OutlineArtifact, dict(manifest), dependency_hashes)
        record = publish_json_artifact(
            self.publication_context,
            self.registry,
            self._node_path(f"semantic_input/{node_id}"),
            artifact.to_dict(),
            artifact_role="outline_v3_semantic_reducer_input_manifest",
            artifact_type="outline_artifact",
            artifact_version="v3",
            producer="outline.v3_executor.OutlineV3Executor",
            artifact_id=f"outline-v3:semantic-input:{self.closure_epoch_id}:{hash_text(node_id)[:16]}",
            metadata={
                "job_id": self.job_id,
                "node_id": node_id,
                "content_hash": artifact.content_hash,
                "source_input_hash": str(manifest.get("source_input_hash") or ""),
            },
        )
        self.artifact_paths[f"{node_id}:input_manifest"] = record.path
        self.artifact_records[f"{node_id}:input_manifest"] = record
        return record

    def _critique_artifact_class(self, node_id: str) -> type[OutlineArtifact]:
        return {
            "structure_critique": StructureCritique,
            "coverage_critique": CoverageCritique,
            "evidence_critique": EvidenceCritique,
        }[node_id]

    @staticmethod
    def _critique_role_from_node_id(node_id: str) -> str:
        """Return the semantic critic role from a static or stability node id."""

        text = str(node_id or "")
        for role in ("structure_critique", "coverage_critique", "evidence_critique"):
            if text == role or f":{role}" in text or text.startswith(f"{role}:"):
                return role
        raise OutlineV3ExecutionError(
            f"cannot resolve critique role from dynamic node id {node_id!r}"
        )

    @staticmethod
    def _compact_candidate_for_critique(candidate_id: str, content: Mapping[str, Any]) -> dict[str, Any]:
        """Build an identity-preserving critique projection.

        This projection is deliberately lossless for semantic text.  A
        critique may be split into explicit evidence shards by its caller, but
        it must never silently discard the ninth claim or the tail of a
        finding merely to fit a prompt.
        """

        sections: list[dict[str, Any]] = []
        for section in content.get("sections") or ():
            if not isinstance(section, Mapping):
                continue
            claims = [str(item) for item in section.get("claims") or () if str(item).strip()]
            sections.append({
                "section_id": str(section.get("section_id") or ""),
                "title": str(section.get("title") or ""),
                "goal": str(section.get("goal") or ""),
                "paper_keys": [str(item) for item in section.get("paper_keys") or () if str(item)],
                "paper_roles": dict(section.get("paper_roles") or {}) if isinstance(section.get("paper_roles"), Mapping) else {},
                "relation_ids": [str(item) for item in section.get("relation_ids") or () if str(item)],
                "claims": claims,
                "claim_count": len(claims),
            })
        return {
            "candidate_id": candidate_id,
            "organizing_logic": str(content.get("organizing_logic") or ""),
            "sections": sections,
            "planned_claims": [str(item) for item in content.get("claims") or () if str(item).strip()],
            "source_summary_hashes": list(content.get("source_summary_hashes") or ()),
            "candidate_content_hash": hash_json(dict(content)),
        }

    def _persist_critique_shard_output(
        self,
        node_id: str,
        payload: Mapping[str, Any],
        *,
        dependency_hashes: Mapping[str, str],
    ) -> None:
        base_node_id = self._critique_role_from_node_id(node_id)
        artifact = self._artifact(
            self._critique_artifact_class(base_node_id),
            payload,
            dependency_hashes,
        )
        artifact_id = (
            f"outline-v3:critique-shard:{self.closure_epoch_id}:{hash_text(node_id)[:16]}"
        )
        record = publish_json_artifact(
            self.publication_context,
            self.registry,
            self._node_path(node_id),
            artifact.to_dict(),
            artifact_role="outline_v3_critique_shard_output",
            artifact_type=artifact.artifact_type,
            artifact_version=artifact.artifact_version,
            producer="outline.v3_executor.OutlineV3Executor",
            artifact_id=artifact_id,
            metadata={
                "job_id": self.job_id,
                "node_id": node_id,
                "parent_node": base_node_id,
                "content_hash": artifact.content_hash,
                "closure_epoch_id": self.closure_epoch_id,
            },
        )
        self.artifact_paths[node_id] = record.path
        self.artifact_records[node_id] = record
        self._payloads[node_id] = dict(payload)
        self._update_request_audit_artifact_ref(node_id, record)
        expected = self._expected_provider_calls.get(self._provider_call_id(node_id))
        if expected is not None:
            self._expected_provider_calls[expected.call_id] = replace(
                expected,
                artifact_payload_hash=hash_json(payload),
                artifact_content_hash=artifact.content_hash,
                registry_file_hash=record.content_hash,
                artifact_path=record.path,
                registered_artifact_hash=artifact.content_hash,
                node_output_hash=artifact.content_hash,
            )

    @staticmethod
    def _split_critique_candidate(
        candidate: Mapping[str, Any],
        *,
        target_tokens: int,
    ) -> list[dict[str, Any]]:
        """Split a candidate only at complete section boundaries.

        A section that is itself larger than the target is returned intact so
        the provider admission gate can report ``BLOCKED_BUDGET``.  Claims are
        never sliced or dropped to make a request appear small.
        """

        sections = [
            dict(item) for item in candidate.get("sections") or ()
            if isinstance(item, Mapping)
        ]
        if not sections:
            return [dict(candidate)]
        limit_chars = max(16_000, int(target_tokens or 28_000) * 4 - 8_000)
        shards: list[dict[str, Any]] = []
        current: list[dict[str, Any]] = []
        current_size = 0
        for section in sections:
            section_size = len(json.dumps(section, ensure_ascii=False, sort_keys=True))
            if current and current_size + section_size > limit_chars:
                shard = dict(candidate)
                shard["sections"] = current
                shard["shard_section_ids"] = [str(item.get("section_id") or "") for item in current]
                shards.append(shard)
                current = []
                current_size = 0
            current.append(section)
            current_size += section_size
        if current:
            shard = dict(candidate)
            shard["sections"] = current
            shard["shard_section_ids"] = [str(item.get("section_id") or "") for item in current]
            shards.append(shard)
        return shards

    def _run_hierarchical_critique(
        self,
        *,
        node_id: str,
        request: Mapping[str, Any],
        dependency_hashes: Mapping[str, str],
        node_prefix: str = "",
    ) -> dict[str, Any]:
        candidate_contents = request.get("candidate_contents")
        candidate_hashes = request.get("candidate_hashes")
        if not isinstance(candidate_contents, Mapping) or not candidate_contents:
            raise OutlineV3ExecutionError(f"{node_id} cannot shard without candidate contents")
        shard_results: list[dict[str, Any]] = []
        profile = self._node_route(node_id).profile
        target = self._relation_packing_target(profile)
        effective_cap = self._effective_input_cap(profile)
        planned_calls: list[tuple[str, dict[str, Any], str, int, set[str]]] = []
        for candidate_id in sorted(str(item) for item in candidate_contents):
            candidate_content = candidate_contents[candidate_id]
            compact = self._compact_candidate_for_critique(
                candidate_id,
                candidate_content if isinstance(candidate_content, Mapping) else {},
            )
            candidate_shards = self._split_critique_candidate(
                compact,
                target_tokens=target,
            )
            for shard_index, compact_candidate in enumerate(candidate_shards, start=1):
                local_request = dict(request)
                shard_key = f"{candidate_id}:shard:{shard_index}"
                local_request["hierarchy"] = {
                    "level": "critique_candidate_shard",
                    "shard_id": shard_key,
                    "target_tokens": target,
                    "candidate_id": candidate_id,
                    "section_ids": list(compact_candidate.get("shard_section_ids") or ()),
                }
                local_request["candidate_contents"] = {candidate_id: compact_candidate}
                if isinstance(candidate_hashes, Mapping):
                    local_request["candidate_hashes"] = {candidate_id: candidate_hashes.get(candidate_id, "")}
                candidate_papers = {
                    str(paper_key)
                    for section in compact_candidate.get("sections") or ()
                    if isinstance(section, Mapping)
                    for paper_key in section.get("paper_keys") or ()
                    if str(paper_key)
                }
                section_ids = {str(item.get("section_id") or "") for item in compact_candidate.get("sections") or () if isinstance(item, Mapping)}
                if isinstance(local_request.get("candidate_claims"), Mapping):
                    local_request["candidate_claims"] = {
                        candidate_id: [str(item) for item in local_request["candidate_claims"].get(candidate_id) or () if str(item).strip()]
                    }
                if isinstance(local_request.get("section_evidence"), Mapping):
                    local_request["section_evidence"] = {
                        candidate_id: [dict(section) for section in local_request["section_evidence"].get(candidate_id) or ()
                                       if isinstance(section, Mapping) and str(section.get("section_id") or "") in section_ids]
                    }
                if isinstance(local_request.get("stability_claim_comparisons"), Mapping):
                    candidate_pairs = [
                        dict(pair)
                        for pair in local_request["stability_claim_comparisons"].get(candidate_id) or ()
                        if isinstance(pair, Mapping)
                        and any(
                            str(reference.get("section_id") or "") in section_ids
                            for reference in pair.get("variant_claim_refs") or ()
                            if isinstance(reference, Mapping)
                        )
                    ]
                    local_request["stability_claim_comparisons"] = {
                        candidate_id: candidate_pairs
                    }
                    pair_fact_ids = {
                        str(pair.get("fact_id") or "") for pair in candidate_pairs
                        if str(pair.get("fact_id") or "")
                    }
                    if isinstance(local_request.get("stability_primary_claim_catalog"), list):
                        local_request["stability_primary_claim_catalog"] = [
                            dict(item)
                            for item in local_request["stability_primary_claim_catalog"]
                            if isinstance(item, Mapping)
                            and str(item.get("fact_id") or "") in pair_fact_ids
                        ]
                if isinstance(local_request.get("corpus_ledger"), Mapping):
                    ledger = dict(local_request["corpus_ledger"])
                    entries = []
                    for entry in ledger.get("entries") or ():
                        if not isinstance(entry, Mapping):
                            continue
                        raw_paper_info = entry.get("paper_info")
                        paper_info: Mapping[str, Any] = raw_paper_info if isinstance(raw_paper_info, Mapping) else {}
                        paper_key = str(entry.get("paper_key") or entry.get("canonical_paper_key") or paper_info.get("canonical_paper_key") or "")
                        if paper_key and paper_key in candidate_papers:
                            entries.append(dict(entry))
                    ledger["entries"] = entries
                    local_request["corpus_ledger"] = ledger
                for relation_field in ("relations", "relation_evidence", "contradictions", "gaps"):
                    if isinstance(local_request.get(relation_field), list):
                        local_request[relation_field] = [
                            dict(relation) for relation in local_request[relation_field]
                            if isinstance(relation, Mapping)
                            and (not candidate_papers or candidate_papers.intersection(str(value) for value in relation.get("paper_keys") or () if str(value)))
                        ]
                for field_name in ("boundaries", "gaps"):
                    if isinstance(local_request.get(field_name), list):
                        local_request[field_name] = [
                            dict(view) for view in local_request[field_name]
                            if isinstance(view, Mapping) and str(view.get("paper_key") or view.get("canonical_paper_key") or "") in candidate_papers
                        ]
                local_node_id = f"{node_id}:local:{candidate_id}:shard:{shard_index}"
                if node_prefix:
                    local_node_id = f"{node_prefix}:{local_node_id}"
                local_request = self._attach_prompt_authority(local_node_id, local_request)
                planned_calls.append((local_node_id, local_request, candidate_id, shard_index, section_ids))
        if (
            self.max_provider_calls is not None
            and self._provider_call_count + len(planned_calls) > self.max_provider_calls
        ):
            raise OutlineV3ExecutionError("outline provider call budget exhausted before critique shards")
        for local_node_id, local_request, _candidate_id, _shard_index, _section_ids in planned_calls:
            estimate = int(profile.estimate_request(local_request).get("estimated_input_tokens") or 0)
            if estimate > effective_cap:
                raise OutlineV3ExecutionError(
                    f"BLOCKED_BUDGET: {local_node_id} complete critique request "
                    f"estimate {estimate} exceeds effective input cap {effective_cap}"
                )
        for local_node_id, local_request, candidate_id, shard_index, section_ids in planned_calls:
            content = self._provider_call(
                local_node_id,
                local_request,
                expect_json=True,
                input_artifact_hashes=(*dependency_hashes.values(), hash_json({"candidate_id": candidate_id, "shard_index": shard_index})),
                transport_node_id=node_id,
                output_tokens=min(int(profile.max_output_tokens), 2048),
            )
            result = dict(content)
            result["candidate_id"] = candidate_id
            result["shard_index"] = shard_index
            result["reviewed_section_ids"] = sorted(section_ids)
            candidate_hashes = request.get("candidate_hashes")
            if isinstance(candidate_hashes, Mapping):
                result["parent_candidate_hash"] = str(candidate_hashes.get(candidate_id) or "")
            shard_results.append(result)
        blocking = [
            str(item)
            for result in shard_results
            for item in result.get("blocking_diagnostics") or ()
            if str(item).strip()
        ]
        recommendations = [
            str(item)
            for result in shard_results
            for item in result.get("recommendations") or ()
            if str(item).strip()
        ]
        merged: dict[str, Any] = {
            "node_id": node_id,
            "passed": all(bool(result.get("passed", True)) for result in shard_results),
            "blocking_diagnostics": blocking,
            "recommendations": recommendations,
            "score": min(float(result.get("score", 1.0) or 0.0) for result in shard_results),
            "coverage_metrics": {},
            "evidence_metrics": {},
            "structure_metrics": {},
            "candidate_shard_results": {
                f"{result['candidate_id']}:shard:{result['shard_index']}": result
                for result in shard_results
            },
            "stability_claim_reviews": [
                dict(review)
                for result in shard_results
                for review in result.get("stability_claim_reviews") or ()
                if isinstance(review, Mapping)
            ],
        }
        return merged

    def _candidate_shard_requests(
        self,
        *,
        generation_node_id: str,
        provider_request: Mapping[str, Any],
        evidence_views: Sequence[Any],
        relation_candidates: Sequence[Mapping[str, Any]],
        node_prefix: str = "",
    ) -> list[tuple[str, dict[str, Any], list[str], list[str], dict[str, Any]]]:
        """Build the same complete candidate shards for checks and transport."""

        shard_plan = self._build_relation_shard_plan(
            evidence_views,
            relation_candidates,
            profile=self._node_route(generation_node_id).profile,
        )
        requests: list[tuple[str, dict[str, Any], list[str], list[str], dict[str, Any]]] = []
        for raw_shard in shard_plan.get("shards") or ():
            if not isinstance(raw_shard, Mapping):
                continue
            shard = dict(raw_shard)
            shard_id = str(shard.get("shard_id") or "").strip()
            paper_keys = [str(item) for item in shard.get("paper_keys") or () if str(item)]
            if not shard_id or not paper_keys:
                continue
            shard_key_set = set(paper_keys)
            shard_relation_ids = [
                str(item.get("relation_id") or "")
                for item in relation_candidates
                if isinstance(item, Mapping)
                and set(str(value) for value in item.get("paper_keys") or () if str(value)).issubset(shard_key_set)
            ]
            request = dict(provider_request)
            if self._alias_enabled and self._alias_map is not None:
                from outline.evidence_alias import canonicalize_structural

                request = canonicalize_structural(request, self._alias_map)
            if self._candidate_output_scope is not None:
                shard_scope = self._candidate_output_scope_wire(paper_keys)
                request["candidate_output_scope"] = shard_scope
            request.update({
                "hierarchy": {
                    "level": "candidate_local_shard",
                    "shard_id": shard_id,
                    "target_tokens": self.technical_shard_target_tokens,
                    "paper_keys": paper_keys,
                    "relation_candidate_ids": sorted(shard_relation_ids),
                    "evidence_view_hashes": list(shard.get("view_hashes") or ()),
                },
                "paper_keys": paper_keys,
                "relation_ids": sorted(shard_relation_ids),
                "relations": [
                    dict(item) for item in relation_candidates
                    if isinstance(item, Mapping)
                    and str(item.get("relation_id") or "") in set(shard_relation_ids)
                ],
                "evidence": [
                    dict(item) for item in shard.get("evidence_chunks") or ()
                    if isinstance(item, Mapping)
                ],
            })
            if self._alias_enabled and self._alias_map is not None:
                from outline.evidence_alias import alias_structural

                request = alias_structural(request, self._alias_map)
            node_id = f"{generation_node_id}:local:{shard_id}"
            if node_prefix:
                node_id = f"{node_prefix}:{node_id}"
            requests.append((node_id, shard, paper_keys, shard_relation_ids, request))
        return requests

    def _candidate_output_scope_wire(self, paper_keys: Sequence[str]) -> dict[str, Any]:
        """Expose the complete slot closure once, leaving audit lineage in Registry."""

        parent_scope = self._candidate_output_scope
        if parent_scope is None:
            raise OutlineV3ExecutionError("candidate output scope is missing before wire materialization")
        scope = parent_scope.for_papers(paper_keys)
        payload = scope.to_dict()
        group_indices = {group.claim_group_id: index for index, group in enumerate(scope.claim_groups)}
        return {
            "schema_version": "outline-candidate-output-scope-wire/v1",
            "content_hash": scope.content_hash,
            "parent_content_hash": parent_scope.content_hash,
            "task_ids": list(scope.task_ids), "limits": payload["limits"],
            "claim_slots": [
                {"claim_group_index": group_indices[slot.claim_group_id], **{
                    key: value for key, value in slot.to_dict().items() if key in {
                    "claim_slot_id", "task_id", "synthesis_claim_id",
                    "paper_key", "primary_claim_id", "study_id", "source_claim_ids",
                    "evidence_ids", "source_field_ids", "relation_ids",
                }}} for slot in scope.claim_slots
            ],
        }

    @staticmethod
    def _apply_section_coordination(
        candidate_id: str,
        sections: Sequence[Mapping[str, Any]],
        coordination: Mapping[str, Any],
        *,
        parent_hash: str,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Apply an explicit pre-adoption section order and lossless merges."""

        if str(coordination.get("candidate_id") or "") != candidate_id:
            raise OutlineV3ExecutionError("section coordination candidate identity changed")
        original = {
            str(section.get("section_id") or ""): dict(section)
            for section in sections if isinstance(section, Mapping)
        }
        if len(original) != len(sections) or not all(original):
            raise OutlineV3ExecutionError("section coordination input has duplicate or missing section ids")
        consumed: set[str] = set()
        merged: dict[str, dict[str, Any]] = {}
        merge_audit: list[dict[str, Any]] = []
        for raw_group in coordination.get("merge_groups") or ():
            if not isinstance(raw_group, Mapping):
                raise OutlineV3ExecutionError("section coordination has an invalid merge group")
            source_ids = [str(value) for value in raw_group.get("source_section_ids") or () if str(value)]
            new_id = str(raw_group.get("new_section_id") or "").strip()
            if (
                len(source_ids) < 2
                or len(source_ids) != len(set(source_ids))
                or any(source_id not in original or source_id in consumed for source_id in source_ids)
                or not new_id
                or new_id in original
                or new_id in merged
            ):
                raise OutlineV3ExecutionError("section coordination merge has invalid source or target identity")
            source_hashes = raw_group.get("source_section_hashes")
            if not isinstance(source_hashes, Mapping) or set(source_hashes) != set(source_ids):
                raise OutlineV3ExecutionError("section coordination merge has incomplete source hashes")
            for source_id in source_ids:
                if str(source_hashes[source_id]) != _hash_payload(original[source_id]):
                    raise OutlineV3ExecutionError("section coordination merge source hash changed")
            source_sections = [original[source_id] for source_id in source_ids]
            title = str(source_sections[0].get("title") or "").strip()
            goal = str(source_sections[0].get("goal") or "").strip()
            if (
                not title or not goal
                or any(str(item.get("title") or "").strip() != title or str(item.get("goal") or "").strip() != goal for item in source_sections)
                or str(raw_group.get("title") or "").strip() != title
                or str(raw_group.get("goal") or "").strip() != goal
                or not str(raw_group.get("integration_reason") or "").strip()
            ):
                raise OutlineV3ExecutionError("section coordination merge has conflicting section intent")
            paper_keys: list[str] = []
            relation_ids: list[str] = []
            claims: list[str] = []
            source_rationales: list[dict[str, str]] = []
            claim_support: list[dict[str, Any]] = []
            task_ids: list[str] = []
            support_by_id: dict[str, dict[str, Any]] = {}
            paper_roles: dict[str, Any] = {}
            for section in source_sections:
                rationale = str(section.get("rationale") or "").strip()
                if rationale:
                    source_rationales.append({
                        "section_id": str(section["section_id"]),
                        "rationale": rationale,
                    })
                local_claims = [str(value) for value in section.get("claims") or () if str(value).strip()]
                paper_keys.extend(str(value) for value in section.get("paper_keys") or () if str(value))
                relation_ids.extend(str(value) for value in section.get("relation_ids") or () if str(value))
                claims.extend(local_claims)
                task_ids.extend(str(value) for value in section.get("task_ids") or () if str(value))
                for raw_support in section.get("claim_support") or ():
                    if not isinstance(raw_support, Mapping):
                        raise OutlineV3ExecutionError("section coordination has malformed claim support")
                    row = dict(raw_support)
                    if str(row.get("claim") or "") not in local_claims:
                        raise OutlineV3ExecutionError("section coordination has orphan claim support")
                    support_id = str(row.get("claim_id") or "")
                    if support_id and support_id in support_by_id and support_by_id[support_id] != row:
                        raise OutlineV3ExecutionError("section coordination has conflicting claim provenance")
                    if support_id:
                        support_by_id[support_id] = row
                    if row not in claim_support:
                        claim_support.append(row)
                for paper_key, role in dict(section.get("paper_roles") or {}).items():
                    if paper_key in paper_roles and paper_roles[paper_key] != role:
                        raise OutlineV3ExecutionError("section coordination has conflicting paper role")
                    paper_roles[str(paper_key)] = role
            result = dict(source_sections[0])
            result.update({
                "section_id": new_id,
                "paper_keys": list(dict.fromkeys(paper_keys)),
                "relation_ids": list(dict.fromkeys(relation_ids)),
                "claims": list(dict.fromkeys(claims)),
                "claim_support": claim_support,
                **({"task_ids": list(dict.fromkeys(task_ids))} if task_ids else {}),
                "paper_roles": paper_roles,
                "rationale": " ".join([
                    f"Integration: {str(raw_group['integration_reason']).strip()}",
                    *(
                        f"[{item['section_id']}] {item['rationale']}"
                        for item in source_rationales
                    ),
                ]),
                "coordination_source_rationales": source_rationales,
                "coordination_source_section_ids": source_ids,
            })
            merged[new_id] = result
            consumed.update(source_ids)
            merge_audit.append({
                "new_section_id": new_id,
                "source_section_ids": source_ids,
                "source_section_hashes": {key: str(source_hashes[key]) for key in source_ids},
                "merged_section_hash": _hash_payload(result),
                "integration_reason": str(raw_group["integration_reason"]),
            })
        final_by_id = {
            **{key: value for key, value in original.items() if key not in consumed},
            **merged,
        }
        section_order = [str(value) for value in coordination.get("section_order") or () if str(value)]
        if (
            len(section_order) != len(final_by_id)
            or len(section_order) != len(set(section_order))
            or set(section_order) != set(final_by_id)
        ):
            raise OutlineV3ExecutionError("section coordination omitted or duplicated a final section")
        ordered = [final_by_id[section_id] for section_id in section_order]
        audit = {
            "schema_version": "candidate-section-coordination/v1",
            "candidate_id": candidate_id,
            "parent_hash": parent_hash,
            "source_section_ids": list(original),
            "section_order": section_order,
            "merge_groups": merge_audit,
            "result_hash": _hash_payload(ordered),
            "status": "applied",
        }
        return ordered, audit

    def _run_hierarchical_candidate_generation(
        self,
        *,
        candidate_id: str,
        generation_node_id: str,
        provider_request: Mapping[str, Any],
        evidence_views: Sequence[Any],
        relation_candidates: Sequence[Mapping[str, Any]],
        allowed_paper_keys: Sequence[str],
        allowed_relation_ids: Sequence[str],
        generation_deps: Mapping[str, str],
        alias_map: Mapping[str, str] | None,
        node_prefix: str = "",
    ) -> dict[str, Any]:
        """Generate one candidate from bounded evidence shards and merge locally."""

        shard_requests = self._candidate_shard_requests(
            generation_node_id=generation_node_id,
            provider_request=provider_request,
            evidence_views=evidence_views,
            relation_candidates=relation_candidates,
            node_prefix=node_prefix,
        )
        profile = self._node_route(generation_node_id).profile
        effective_cap = self._effective_input_cap(profile)
        if (
            self.max_provider_calls is not None
            and self._provider_call_count + len(shard_requests) > self.max_provider_calls
        ):
            raise OutlineV3ExecutionError("outline provider call budget exhausted before candidate shards")
        for node_id, _shard, _papers, _relations, planned_request in shard_requests:
            enriched = self._attach_prompt_authority(node_id, planned_request)
            estimate = int(profile.estimate_request(enriched).get("estimated_input_tokens") or 0)
            if estimate > effective_cap:
                raise OutlineV3ExecutionError(
                    f"BLOCKED_BUDGET: {node_id} complete candidate shard "
                    f"estimate {estimate} exceeds effective input cap {effective_cap}"
                )
        sections: list[dict[str, Any]] = []
        # A paper may legitimately participate in more than one section.
        # Local model IDs such as S1 have no cross-shard meaning. Only an
        # identity declared in the shared request blueprint may be merged.
        raw_blueprints = provider_request.get("shared_section_ids") or {}
        if not isinstance(raw_blueprints, Mapping):
            raise OutlineV3ExecutionError("candidate shared section blueprint must be a mapping")
        shared_blueprints = {
            str(key): dict(value)
            for key, value in raw_blueprints.items()
            if str(key) and isinstance(value, Mapping)
        }
        section_by_identity: dict[str, dict[str, Any]] = {}
        claims: list[str] = []
        shard_outputs: list[dict[str, Any]] = []
        for node_id, shard, paper_keys, shard_relation_ids, request in shard_requests:
            shard_id = str(shard.get("shard_id") or "").strip()
            request = self._attach_prompt_authority(node_id, request)
            shard_deps = {
                **dict(generation_deps),
                "relation_shard": _hash_payload(dict(shard)),
            }
            raw = self._provider_call(
                node_id,
                request,
                expect_json=True,
                input_artifact_hashes=tuple(shard_deps.values()),
                transport_node_id=generation_node_id,
                output_tokens=min(
                    int(self._node_route(generation_node_id).profile.max_output_tokens),
                    1024,
                ),
            )
            content = (
                canonicalize_structural(dict(raw), alias_map)
                if alias_map is not None
                else dict(raw)
            )
            if content.get("candidate_id") != candidate_id:
                raise OutlineV3ExecutionError("candidate shard output omitted or changed candidate_id")
            self._validate_candidate_payload(
                candidate_id,
                content,
                allowed_paper_keys=paper_keys,
                allowed_relation_ids=shard_relation_ids,
                alias_map=alias_map,
            )
            self._persist_candidate_shard_output(
                node_id,
                content,
                dependency_hashes=shard_deps,
            )
            shard_outputs.append(content)
            for section in content.get("sections") or ():
                if not isinstance(section, Mapping):
                    continue
                section_payload = dict(section)
                raw_section_papers = [
                    str(value) for value in section_payload.get("paper_keys") or () if str(value)
                ]
                section_payload["paper_keys"] = list(dict.fromkeys(raw_section_papers))
                section_payload["relation_ids"] = [
                    str(value) for value in section_payload.get("relation_ids") or () if str(value)
                ]
                original_section_id = str(section_payload.get("section_id") or candidate_id)
                blueprint = shared_blueprints.get(original_section_id)
                if blueprint is not None:
                    for field in ("title", "goal"):
                        expected = str(blueprint.get(field) or "").strip()
                        observed = str(section_payload.get(field) or "").strip()
                        if not expected or observed != expected:
                            raise OutlineV3ExecutionError(
                                f"{candidate_id} shared section {original_section_id} has conflicting {field}"
                            )
                existing = section_by_identity.get(original_section_id) if blueprint is not None else None
                if existing is not None:
                    existing_papers = list(existing.get("paper_keys") or ())
                    incoming_papers = list(section_payload.get("paper_keys") or ())
                    existing["paper_keys"] = list(dict.fromkeys([
                        *existing_papers,
                        *incoming_papers,
                    ]))
                    existing_relations = list(existing.get("relation_ids") or ())
                    incoming_relations = list(section_payload.get("relation_ids") or ())
                    existing["relation_ids"] = list(dict.fromkeys([
                        *existing_relations,
                        *incoming_relations,
                    ]))
                    existing_claims = list(existing.get("claims") or ())
                    incoming_claims = [
                        str(value) for value in section_payload.get("claims") or () if str(value).strip()
                    ]
                    existing["claims"] = list(dict.fromkeys([
                        *existing_claims,
                        *incoming_claims,
                    ]))
                    existing_support = [
                        dict(row) for row in existing.get("claim_support") or ()
                        if isinstance(row, Mapping)
                    ]
                    incoming_support = [
                        dict(row) for row in section_payload.get("claim_support") or ()
                        if isinstance(row, Mapping)
                    ]
                    if any(str(row.get("claim") or "").strip() not in incoming_claims for row in incoming_support):
                        raise OutlineV3ExecutionError(
                            f"{candidate_id} shared section {original_section_id} has orphan claim support"
                        )
                    support_by_id = {
                        str(row.get("claim_id")): row
                        for row in existing_support
                        if str(row.get("claim_id") or "")
                    }
                    for row in incoming_support:
                        claim_id = str(row.get("claim_id") or "")
                        if claim_id and claim_id in support_by_id and support_by_id[claim_id] != row:
                            raise OutlineV3ExecutionError(
                                f"{candidate_id} shared section {original_section_id} has conflicting claim_id {claim_id}"
                            )
                        if claim_id:
                            support_by_id[claim_id] = row
                        if row not in existing_support:
                            existing_support.append(row)
                    existing["claim_support"] = existing_support
                    if section_payload.get("task_ids"):
                        existing["task_ids"] = list(dict.fromkeys([
                            *list(existing.get("task_ids") or ()), *list(section_payload["task_ids"]),
                        ]))
                    if isinstance(section_payload.get("paper_roles"), Mapping):
                        roles = dict(existing.get("paper_roles") or {})
                        for paper_key, role in dict(section_payload.get("paper_roles") or {}).items():
                            if paper_key in roles and roles[paper_key] != role:
                                raise OutlineV3ExecutionError(
                                    f"{candidate_id} shared section {original_section_id} has conflicting paper role"
                                )
                            roles[paper_key] = role
                        existing["paper_roles"] = roles
                    continue
                if blueprint is None:
                    section_payload["section_id"] = f"{original_section_id}__{shard_id}"
                else:
                    section_by_identity[original_section_id] = section_payload
                sections.append(section_payload)
            claims.extend(str(item) for item in content.get("claims") or () if str(item).strip())
        if not sections:
            raise OutlineV3ExecutionError(f"{generation_node_id} produced no shard sections")
        planned_view_hashes = list(dict.fromkeys(
            str(item)
            for _node_id, shard, _papers, _relations, _request in shard_requests
            for item in shard.get("view_hashes") or ()
            if str(item)
        ))
        merged = {
            "candidate_id": candidate_id,
            "organizing_logic": provider_request.get("organizing_logic") or "evidence",
            "sections": sections,
            "claims": claims,
            "shard_plan": {
                "shard_count": len(shard_requests),
                "paper_keys": list(allowed_paper_keys),
                "relation_ids": list(allowed_relation_ids),
                "source_view_hashes": planned_view_hashes,
            },
        }
        self._validate_candidate_payload(
            candidate_id,
            merged,
            allowed_paper_keys=allowed_paper_keys,
            allowed_relation_ids=allowed_relation_ids,
            alias_map=alias_map,
        )
        return merged

    def _semantic_output_token_limit(self, profile: ProviderContextProfile) -> int:
        """Bound semantic conclusions for both preflight and transport.

        Complete source material stays in the Registry and task input. These
        results carry supported synthesis and unresolved exceptions, not a
        second copy of the source dossier. A truncated result fails closed.
        """

        limit = min(self.semantic_output_max_tokens, max(1, int(profile.max_output_tokens)))
        if str(profile.endpoint_type or "").casefold() == "anthropic":
            from services.model_capabilities import anthropic_thinking_mode

            route = self._role_route("candidate_1_provider_generation")
            if anthropic_thinking_mode(route.model) == "manual":
                raw_budget = route.config_identity.get("thinking_budget_tokens")
                try:
                    thinking_budget = int(str(raw_budget or "0"))
                except ValueError as exc:
                    raise OutlineV3ExecutionError(
                        "semantic route manual thinking budget is invalid"
                    ) from exc
                if thinking_budget >= limit:
                    raise OutlineV3ExecutionError(
                        "semantic route manual thinking budget exceeds the planned output allowance"
                    )
        return limit

    def _semantic_transport_retry_count(self) -> int | None:
        route = self._node_route("candidate_1_provider_generation")
        configured = route.config_identity.get("transport_retries")
        if configured is not None and str(configured).strip() != "":
            return max(0, int(configured))
        if str(route.endpoint_type).casefold() in {"internal", "fixture"}:
            return 0
        return self.semantic_transport_retries

    def _run_semantic_provider_call(
        self,
        node_id: str,
        request: Mapping[str, Any],
        dependency_hashes: Mapping[str, str],
        *,
        output_tokens: int | None = None,
    ) -> dict[str, Any]:
        """Execute a topic/cross/global synthesis request on the outline route.

        The semantic synthesis roles do not have separate configuration keys
        yet; they intentionally reuse the configured candidate-generation
        route while retaining their own durable node/call identity.
        """

        self._semantic_request_contract(node_id, request)

        result = self._provider_call(
            node_id,
            request,
            expect_json=True,
            input_artifact_hashes=tuple(dependency_hashes.values()),
            transport_node_id="candidate_1_provider_generation",
            output_tokens=(
                int(output_tokens)
                if output_tokens is not None
                else self._semantic_output_token_limit(
                    self._node_route("candidate_1_provider_generation").profile
                )
            ),
        )
        audit_index = next((
            index for index in range(len(self._request_payload_audit) - 1, -1, -1)
            if self._request_payload_audit[index].get("node_id") == node_id
        ), -1)
        try:
            self._validate_semantic_provider_output(node_id, request, result)
        except OutlineV3ExecutionError as exc:
            self._finish_request_payload_audit(
                audit_index,
                semantic_validation_status="rejected",
                semantic_validation_error=str(exc),
            )
            raise
        self._finish_request_payload_audit(
            audit_index, semantic_validation_status="accepted"
        )
        return result

    def _run_bounded_semantic_provider_call(
        self,
        node_id: str,
        request: Mapping[str, Any],
        dependency_hashes: Mapping[str, str],
    ) -> dict[str, Any]:
        """Reduce oversized topic context in bounded, evidence-linked layers."""

        profile = self._node_route("candidate_1_provider_generation").profile
        input_limit = max(
            1,
            min(
                32_000,
                int(self.max_source_prompt_tokens or 32_000),
                int(profile.input_budget or 32_000),
            ),
        )
        output_limit = self._semantic_output_token_limit(profile)
        logical_stage = (
            "cross_group_comparison"
            if str(node_id).startswith("cross_group_comparison_provider")
            else "global_synthesis"
        )
        is_cross = logical_stage == "cross_group_comparison"

        def estimate(candidate: Mapping[str, Any], candidate_node_id: str) -> int:
            attached = self._attach_prompt_authority(candidate_node_id, candidate)
            budget = profile.estimate_request(attached)
            return int(budget.get("estimated_input_tokens") or profile.estimate_tokens(attached))

        base_request = dict(request)
        local_input_manifests: dict[str, dict[str, Any]] = {}
        local_input_manifest_hashes: dict[str, str] = {}
        topic_items = base_request.get("topic_synthesis")
        if not isinstance(topic_items, list) or not topic_items:
            initial_tokens = estimate(base_request, node_id)
            if initial_tokens > input_limit:
                raise OutlineV3ExecutionError(
                    f"BLOCKED_BUDGET: {logical_stage} has no reducible topic units and requires {initial_tokens} tokens over cap {input_limit}"
                )
            return self._run_semantic_provider_call(node_id, base_request, dependency_hashes)

        def topic_ids(items: Sequence[Any]) -> set[str]:
            result: set[str] = set()
            for item in items:
                if not isinstance(item, Mapping):
                    continue
                result.update(str(value) for value in item.get("topic_ids") or () if str(value))
                result.update(str(value) for value in item.get("processed_topic_ids") or () if str(value))
                if item.get("topic_id"):
                    result.add(str(item["topic_id"]))
            return result

        def semantic_identity_ids(items: Sequence[Any], identity_kind: str) -> set[str]:
            result: set[str] = set()
            singular_fields, plural_fields, processed_field = {
                "fragment": (("fragment_id",), ("fragment_ids",), "processed_fragment_ids"),
                "result": (
                    ("result_id", "batch_result_id"),
                    ("result_ids", "batch_result_ids"),
                    "processed_result_ids",
                ),
            }[identity_kind]

            def visit(value: Any) -> None:
                if isinstance(value, Mapping):
                    for key in (*singular_fields, *plural_fields, processed_field):
                        raw = value.get(key)
                        if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
                            result.update(str(item) for item in raw if str(item))
                        elif raw is not None and str(raw):
                            result.add(str(raw))
                    for child in value.values():
                        visit(child)
                elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
                    for child in value:
                        visit(child)

            for item in items:
                visit(item)
            return result

        def lineage_member_ids(items: Sequence[Any], field: str) -> list[str]:
            direct_kind = {"fragment_ids": "fragment", "result_ids": "result"}.get(field)
            values = semantic_identity_ids(items, direct_kind) if direct_kind else set()
            for item in items:
                if not isinstance(item, Mapping):
                    continue
                source_id = str(item.get("reducer_id") or "")
                source_manifest = local_input_manifests.get(source_id)
                if source_manifest is not None:
                    values.update(str(value) for value in source_manifest.get(field) or () if str(value))
            return sorted(values)

        def paper_ids(items: Sequence[Any]) -> set[str]:
            return {
                str(value)
                for item in items
                if isinstance(item, Mapping)
                for value in (
                    *list(item.get("paper_ids") or ()),
                    *list(item.get("bridge_paper_ids") or ()),
                )
                if str(value)
            }

        def membership_map(
            items: Sequence[Any],
            identity_kind: str,
            *,
            additional: Sequence[Mapping[str, Any]] = (),
        ) -> dict[str, list[str]]:
            singular_field, plural_fields, processed_field, explicit_field = {
                "topic": ("topic_id", ("topic_ids",), "processed_topic_ids", "topic_members_by_id"),
                "fragment": ("fragment_id", ("fragment_ids",), "processed_fragment_ids", "fragment_members_by_id"),
                "relation": ("relation_id", ("relation_ids",), "processed_relation_ids", "relation_members_by_id"),
            }[identity_kind]
            members_by_id: dict[str, set[str]] = {}

            def add_mapping(raw: Any) -> None:
                if isinstance(raw, Mapping):
                    explicit = raw.get(explicit_field)
                    if isinstance(explicit, Mapping):
                        for identity, members in explicit.items():
                            identity_key = str(identity or "")
                            if identity_key:
                                members_by_id.setdefault(identity_key, set()).update(
                                    str(value) for value in members or () if str(value)
                                )
                    direct_members = {
                        str(value)
                        for value in (
                            *list(raw.get("paper_ids") or ()),
                            *list(raw.get("paper_keys") or ()),
                            *([raw.get("paper_key")] if raw.get("paper_key") else []),
                        )
                        if str(value)
                    }
                    identities: set[str] = set()
                    if raw.get(singular_field):
                        identities.add(str(raw[singular_field]))
                    for field in (*plural_fields, processed_field):
                        values = raw.get(field) or ()
                        if isinstance(values, str):
                            values = [values]
                        identities.update(str(value) for value in values if str(value))
                    for identity in identities:
                        if identity not in members_by_id or not members_by_id[identity]:
                            members_by_id.setdefault(identity, set()).update(direct_members)
                    for child in raw.values():
                        add_mapping(child)
                elif isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
                    for child in raw:
                        add_mapping(child)

            add_mapping(items)
            add_mapping(additional)
            return {key: sorted(values) for key, values in sorted(members_by_id.items())}

        def merged_interpretation_context(items: Sequence[Any]) -> dict[str, list[dict[str, Any]]]:
            fields: dict[str, dict[str, Any]] = {}
            dependencies: dict[str, dict[str, Any]] = {}
            for item in items:
                if not isinstance(item, Mapping):
                    continue
                context = item.get("interpretation_context")
                if not isinstance(context, Mapping):
                    continue
                for source_field in context.get("fields") or ():
                    if not isinstance(source_field, Mapping):
                        continue
                    field_id = str(source_field.get("source_field_id") or "")
                    if field_id:
                        value = dict(source_field)
                        if field_id in fields and fields[field_id] != value:
                            raise OutlineV3ExecutionError(
                                "reducer interpretation field has conflicting contents"
                            )
                        fields[field_id] = value
                for dependency in context.get("dependencies") or ():
                    if isinstance(dependency, Mapping):
                        value = dict(dependency)
                        dependencies[hash_json(value)] = value
            if any(
                not set(dependency.get("required_source_field_ids") or ()).issubset(fields)
                for dependency in dependencies.values()
            ):
                raise OutlineV3ExecutionError(
                    "reducer lost interpretation source text"
                )
            return {
                "fields": [fields[key] for key in sorted(fields)],
                "dependencies": [dependencies[key] for key in sorted(dependencies)],
            }

        def reducer_request(
            items: Sequence[Any],
            *,
            level: int,
            index: int,
            relations: Sequence[Mapping[str, Any]] = (),
            cross_context: Mapping[str, Any] | None = None,
        ) -> dict[str, Any]:
            child = dict(base_request)
            child["task"] = f"{logical_stage}_bounded_reduction"
            child["node_id"] = logical_stage
            child["topic_synthesis"] = list(items)
            if "relation_candidates" in child:
                child["relation_candidates"] = [dict(item) for item in relations]
            if is_cross and "questions" in child:
                child["questions"] = list(base_request.get("questions") or ())
            if not is_cross and "cross_group_comparison" in child:
                child["cross_group_comparison"] = dict(cross_context or {})
            child["hierarchy"] = {
                "level": "bounded_semantic_reduction",
                "stage": logical_stage,
                "reduction_level": level,
                "group_index": index,
                "target_input_tokens": input_limit,
            }
            child["output_contract"] = {
                **dict(base_request.get("output_contract") or {}),
                "reduction_policy": (
                    "integrate only the supplied topic fragments; preserve conclusions, conditions, conflicts, unresolved items, and evidence support; do not invent missing evidence"
                ),
            }
            if child.get("shared_synthesis_contract_version") == SHARED_SYNTHESIS_CONTRACT_VERSION:
                child["output_contract"]["coverage_ledger"] = (
                    "do not echo processed ID arrays; runtime computes and persists the exact input membership"
                )
            else:
                child["output_contract"].update({
                    "processed_topic_ids": "echo each supplied topic_id exactly once",
                    "processed_fragment_ids": "echo each supplied fragment_id exactly once",
                    "processed_result_ids": "echo each supplied result_id and batch_result_id exactly once",
                })
            return child

        current_items: list[Any] = list(topic_items)
        current_relations = [
            dict(item)
            for item in base_request.get("relation_candidates") or ()
            if isinstance(item, Mapping)
        ]
        current_cross = (
            dict(base_request.get("cross_group_comparison") or {})
            if not is_cross
            else {}
        )
        # A stage-local guard prevents an output-dependent reducer from
        # iterating indefinitely. It never grants aggregate provider budget.
        max_reducer_calls = MAX_SEMANTIC_REDUCER_CALLS_PER_STAGE
        reducer_calls_started = 0
        level = 1
        while True:
            final_request = dict(base_request)
            final_request["topic_synthesis"] = current_items
            if level > 1:
                if base_request.get("shared_synthesis_contract_version") == SHARED_SYNTHESIS_CONTRACT_VERSION:
                    final_request["hierarchy"] = {
                        "level": "bounded_semantic_final_fold",
                        "stage": logical_stage,
                        "reduction_level": level,
                    }
                # Relation inputs and prior cross-group output have already
                # been consumed by the reducers; the final fold receives their
                # evidence-linked reduction results through topic_synthesis.
                if "relation_candidates" in final_request:
                    final_request["relation_candidates"] = []
                if not is_cross and "cross_group_comparison" in final_request:
                    final_request["cross_group_comparison"] = {}
            final_tokens = estimate(final_request, node_id)
            if final_tokens <= input_limit:
                if is_cross and hash_json(final_request.get("questions")) != hash_json(
                    base_request.get("questions")
                ):
                    raise OutlineV3ExecutionError(
                        "cross-group reducer lost its original comparison questions"
                    )
                return self._run_semantic_provider_call(node_id, final_request, dependency_hashes)
            if level > MAX_SEMANTIC_REDUCTION_LEVELS:
                raise OutlineV3ExecutionError(
                    f"BLOCKED_BUDGET: {logical_stage} exceeded {MAX_SEMANTIC_REDUCTION_LEVELS} bounded reduction levels"
                )

            # First divide topic units by their complete serialized content and
            # the same route prompt/schema used by the real provider call.
            packing_items: list[Any] = []
            for item in current_items:
                single = reducer_request([item], level=level, index=1)
                if isinstance(item, Mapping) and estimate(
                    single, f"{node_id}:reduce:{level}:1"
                ) > input_limit:
                    from outline.semantic_reducer_projection import split_nested_topic_for_reduction_v1

                    try:
                        packing_items.extend(split_nested_topic_for_reduction_v1(item))
                    except ValueError as exc:
                        raise OutlineV3ExecutionError(
                            f"invalid nested topic reduction scope: {exc}"
                        ) from exc
                else:
                    packing_items.append(item)
            current_items = packing_items
            groups: list[list[Any]] = []
            current_group: list[Any] = []
            for item in current_items:
                trial = [*current_group, item]
                probe = reducer_request(
                    trial,
                    level=level,
                    index=len(groups) + 1,
                )
                probe_node_id = f"{node_id}:reduce:{level}:{len(groups) + 1}"
                if current_group and estimate(probe, probe_node_id) > input_limit:
                    groups.append(current_group)
                    current_group = [item]
                    single_probe = reducer_request(
                        current_group,
                        level=level,
                        index=len(groups) + 1,
                    )
                    if estimate(single_probe, f"{node_id}:reduce:{level}:{len(groups) + 1}") > input_limit:
                        raise OutlineV3ExecutionError(
                            f"BLOCKED_BUDGET: one {logical_stage} topic result is indivisible over the effective input cap"
                        )
                elif not current_group and estimate(probe, probe_node_id) > input_limit:
                    raise OutlineV3ExecutionError(
                        f"BLOCKED_BUDGET: one {logical_stage} topic result is indivisible over the effective input cap"
                    )
                else:
                    current_group = trial
            if current_group:
                groups.append(current_group)
            if len(groups) < 2 and len(current_items) < 2:
                raise OutlineV3ExecutionError(
                    f"BLOCKED_BUDGET: {logical_stage} cannot reduce its oversized single input"
                )

            # Relations are assigned only after the first split. Re-estimate
            # every exact child with that assignment before sending any call;
            # a too-large group is split again and the relation assignment is
            # recalculated. A single complete fragment is indivisible.
            while True:
                relations_by_group: list[list[dict[str, Any]]] = [[] for _ in groups]
                if current_relations:
                    group_papers = [paper_ids(group) for group in groups]
                    for relation in current_relations:
                        members = {
                            str(value) for value in relation.get("paper_keys") or () if str(value)
                        }
                        target_index = max(
                            range(len(groups)),
                            key=lambda item_index: (
                                int(bool(members) and members.issubset(group_papers[item_index])),
                                len(members.intersection(group_papers[item_index])),
                                -item_index,
                            ),
                        )
                        relations_by_group[target_index].append(relation)
                oversized_index = None
                oversized_tokens = 0
                for index, group in enumerate(groups):
                    child = reducer_request(
                        group,
                        level=level,
                        index=index + 1,
                        relations=relations_by_group[index],
                        cross_context=(current_cross if not is_cross and index == 0 else None),
                    )
                    tokens = estimate(child, f"{node_id}:reduce:{level}:{index + 1}")
                    if tokens > input_limit:
                        oversized_index = index
                        oversized_tokens = tokens
                        break
                if oversized_index is None:
                    break
                oversized_group = groups[oversized_index]
                if len(oversized_group) == 1:
                    raise OutlineV3ExecutionError(
                        f"BLOCKED_BUDGET: one complete {logical_stage} fragment plus its assigned relations/context needs {oversized_tokens} tokens over cap {input_limit}"
                    )
                midpoint = len(oversized_group) // 2
                groups[oversized_index:oversized_index + 1] = [
                    oversized_group[:midpoint], oversized_group[midpoint:]
                ]

            if reducer_calls_started + len(groups) > max_reducer_calls:
                raise OutlineV3ExecutionError(
                    f"BLOCKED_BUDGET: {logical_stage} reducer cannot contract within {max_reducer_calls} calls"
                )
            if (
                self.max_provider_calls is not None
                and self._provider_call_count + len(groups) > self.max_provider_calls
            ):
                raise OutlineV3ExecutionError(
                    f"BLOCKED_BUDGET: {logical_stage} reducer level exceeds the remaining provider call budget"
                )

            reduced_items: list[dict[str, Any]] = []
            reducer_output_tokens = max(1, min(output_limit, input_limit // 4))
            for group_index, group in enumerate(groups, start=1):
                cross_context: Mapping[str, Any] | None = None
                if not is_cross and group_index == 1 and current_cross:
                    cross_context = current_cross
                child_request = reducer_request(
                    group,
                    level=level,
                    index=group_index,
                    relations=relations_by_group[group_index - 1],
                    cross_context=cross_context,
                )
                child_node_id = f"{node_id}:reduce:{level}:{group_index}"
                child_tokens = estimate(child_request, child_node_id)
                if child_tokens > input_limit:
                    raise OutlineV3ExecutionError(
                        f"BLOCKED_BUDGET: {logical_stage} reducer {child_node_id} needs {child_tokens} tokens over cap {input_limit}"
                    )
                group_ids = sorted(topic_ids(group))
                verified_interpretation = merged_interpretation_context(group)
                reduction_input_hash = hash_json({
                    "items": group,
                    "relations": relations_by_group[group_index - 1],
                    "cross_group_comparison": cross_context or {},
                    "level": level,
                })
                input_manifest = {
                    "schema_version": "outline-semantic-reducer-input/v1",
                    "node_id": child_node_id,
                    "reduction_level": level,
                    "group_index": group_index,
                    "topic_ids": group_ids,
                    "fragment_ids": lineage_member_ids(group, "fragment_ids"),
                    "result_ids": lineage_member_ids(group, "result_ids"),
                    "relation_ids": sorted({
                        str(relation.get("relation_id") or "")
                        for relation in relations_by_group[group_index - 1]
                        if str(relation.get("relation_id") or "")
                    } | set(lineage_member_ids(group, "relation_ids"))),
                    "paper_ids": sorted(paper_ids(group)),
                    "source_field_ids": sorted({
                        str(field.get("source_field_id") or "")
                        for field in verified_interpretation.get("fields") or ()
                        if str(field.get("source_field_id") or "")
                    } | set(lineage_member_ids(group, "source_field_ids"))),
                    "child_input_manifest_hashes": sorted({
                        local_input_manifest_hashes[str(item.get("reducer_id") or "")]
                        for item in group if isinstance(item, Mapping)
                        and str(item.get("reducer_id") or "") in local_input_manifest_hashes
                    }),
                    "source_item_hashes": [hash_json(item) for item in group],
                    "source_input_hash": hash_json(group),
                    "provider_request_hash": hash_json(child_request),
                }
                input_record = self._persist_semantic_reducer_input_manifest(
                    child_node_id,
                    input_manifest,
                    dependency_hashes={
                        **dict(dependency_hashes),
                        "reduction_input": reduction_input_hash,
                    },
                )
                local_input_manifests[child_node_id] = input_manifest
                local_input_manifest_hashes[child_node_id] = input_record.content_hash
                call_dependencies = {
                    **dict(dependency_hashes),
                    "reduction_input": reduction_input_hash,
                    "input_manifest": input_record.content_hash,
                }
                result = self._run_semantic_provider_call(
                    child_node_id,
                    child_request,
                    call_dependencies,
                    output_tokens=reducer_output_tokens,
                )
                self._persist_semantic_provider_output(
                    child_node_id,
                    result,
                    dependency_hashes=call_dependencies,
                )
                if base_request.get("shared_synthesis_contract_version") == SHARED_SYNTHESIS_CONTRACT_VERSION:
                    semantic_body = {
                        key: value for key, value in result.items()
                        if key not in {
                            "processed_topic_ids", "processed_fragment_ids",
                            "processed_result_ids", "processed_relation_ids",
                        }
                    }
                    reduced_items.append({
                        "reducer_id": child_node_id,
                        "topic_ids": group_ids,
                        "topic_members_by_id": membership_map(group, "topic"),
                        "fragment_members_by_id": membership_map(group, "fragment"),
                        "relation_members_by_id": membership_map(
                            group, "relation", additional=relations_by_group[group_index - 1],
                        ),
                        "paper_ids": sorted(paper_ids(group)),
                        "semantic_result": semantic_body,
                        "local_lineage": {
                            "source_input_hash": hash_json(group),
                            "source_request_hash": hash_json(child_request),
                            "interpretation_context_hash": hash_json(verified_interpretation),
                            "fragment_identity_set_hash": hash_json(input_manifest["fragment_ids"]),
                            "result_identity_set_hash": hash_json(input_manifest["result_ids"]),
                        },
                        "reduction_level": level,
                    })
                else:
                    reduced_items.append({
                        "reducer_id": child_node_id,
                        "topic_ids": group_ids,
                        "topic_members_by_id": membership_map(group, "topic"),
                        "fragment_members_by_id": membership_map(group, "fragment"),
                        "relation_members_by_id": membership_map(
                            group, "relation", additional=relations_by_group[group_index - 1],
                        ),
                        "fragment_ids": sorted(semantic_identity_ids(group, "fragment")),
                        "result_ids": sorted({
                            *semantic_identity_ids(group, "result"),
                            "reduction-result:" + hash_json({
                                "node_id": child_node_id,
                                "request_hash": hash_json(child_request),
                                "result_hash": hash_json(result),
                            })[:24],
                        }),
                        "processed_topic_ids": list(result.get("processed_topic_ids") or ()),
                        "processed_fragment_ids": list(result.get("processed_fragment_ids") or ()),
                        "processed_result_ids": list(result.get("processed_result_ids") or ()),
                        "processed_relation_ids": list(result.get("processed_relation_ids") or ()),
                        "paper_ids": sorted(paper_ids(group)),
                        "interpretation_context": verified_interpretation,
                        "provider_outputs": [result],
                        "claims": [
                            dict(item)
                            for field_name in ("claims", "bridge_claims", "synthesis_claims")
                            for item in result.get(field_name) or ()
                            if isinstance(item, Mapping)
                        ],
                        "reduction_level": level,
                    })
            reduced_final_request = dict(base_request)
            reduced_final_request["topic_synthesis"] = reduced_items
            if is_cross:
                reduced_final_request["relation_candidates"] = []
            else:
                reduced_final_request["cross_group_comparison"] = {}
                if "relation_candidates" in reduced_final_request:
                    reduced_final_request["relation_candidates"] = []
            if (
                len(reduced_items) >= len(current_items)
                and estimate(reduced_final_request, node_id) >= final_tokens
            ):
                raise OutlineV3ExecutionError(
                    f"BLOCKED_BUDGET: {logical_stage} reducer did not reduce the bounded input"
                )
            current_items = reduced_items
            current_relations = []
            current_cross = {}
            reducer_calls_started += len(groups)
            level += 1

    def _semantic_request_contract(
        self, node_id: str, request: Mapping[str, Any]
    ) -> tuple[str, bool, Mapping[str, Any] | None]:
        """Check semantic request identity and local cross lineage before transport."""

        stage = (
            "cross_group_comparison"
            if str(node_id).startswith("cross_group_comparison_provider")
            else "global_synthesis"
            if str(node_id).startswith("global_synthesis_provider")
            else ""
        )
        if not stage:
            return "", False, None
        legacy_adapter = (
            request.get("semantic_contract_version") == "semantic-evidence-graph-v1"
            and not request.get("shared_synthesis_contract_version")
        )
        if legacy_adapter:
            return stage, True, None
        allowed_tasks = {
            "cross_group_comparison": {
                "substantive_cross_group_comparison",
                "cross_group_comparison_bounded_reduction",
            },
            "global_synthesis": {
                "substantive_global_synthesis",
                "global_synthesis_bounded_reduction",
            },
        }[stage]
        if (
            request.get("semantic_contract_version") != "semantic-evidence-graph-v2"
            or request.get("shared_synthesis_contract_version") != SHARED_SYNTHESIS_CONTRACT_VERSION
            or request.get("node_id") != stage
            or request.get("task") not in allowed_tasks
        ):
            raise OutlineV3ExecutionError(
                f"{node_id} has an invalid shared semantic request contract"
            )
        if stage != "global_synthesis":
            return stage, False, None

        ledger_ref = request.get("cross_coverage_ledger_ref")
        if not isinstance(ledger_ref, Mapping):
            raise OutlineV3ExecutionError(
                f"{node_id} has no local cross-topic coverage ledger reference"
            )
        cross_payload = self._payloads.get("cross_group_comparison") or {}
        ledger = cross_payload.get("coverage_ledger")
        if not isinstance(ledger, Mapping):
            raise OutlineV3ExecutionError(
                f"{node_id} has no local cross-topic coverage ledger"
            )
        ledger_body = {key: value for key, value in ledger.items() if key != "content_hash"}
        ledger_hash = str(ledger.get("content_hash") or "")
        raw_topic_count = ledger_ref.get("topic_count")
        try:
            reference_topic_count = (
                -1 if isinstance(raw_topic_count, bool) or raw_topic_count is None
                else int(str(raw_topic_count))
            )
        except (TypeError, ValueError):
            reference_topic_count = -1
        if (
            not ledger_hash
            or ledger_hash != hash_json(ledger_body)
            or str(ledger_ref.get("content_hash") or "") != ledger_hash
            or reference_topic_count != len(ledger.get("topic_ids") or ())
            or ledger.get("provider_review_status") != "validated"
        ):
            raise OutlineV3ExecutionError(
                f"{node_id} cross-topic coverage ledger reference is invalid"
            )
        cross_input = request.get("cross_group_comparison")
        hierarchy = request.get("hierarchy")
        reduced_fold = (
            isinstance(hierarchy, Mapping)
            and hierarchy.get("level") in {
                "bounded_semantic_reduction", "bounded_semantic_final_fold",
            }
        )
        if isinstance(cross_input, Mapping) and cross_input:
            if hash_json(dict(cross_input)) != ledger.get("provider_result_hash"):
                raise OutlineV3ExecutionError(
                    f"{node_id} cross-topic coverage ledger does not bind its provider result"
                )
        elif not reduced_fold or not request.get("topic_synthesis"):
            raise OutlineV3ExecutionError(
                f"{node_id} has no validated cross result or bounded reducer input"
            )
        return stage, False, ledger

    def _validate_semantic_provider_output(
        self,
        node_id: str,
        request: Mapping[str, Any],
        result: Mapping[str, Any],
    ) -> None:
        """Validate schema, stage coverage, and ownership-linked evidence IDs."""

        if not isinstance(result, Mapping) or not result:
            raise OutlineV3ExecutionError(f"{node_id} returned an empty semantic result")

        _, _, local_cross_ledger = self._semantic_request_contract(
            node_id, request
        )

        allowed_papers: set[str] = set()
        allowed_studies: set[str] = set()
        study_owner: dict[str, set[str]] = {}
        allowed_source_claims: set[str] = set()
        allowed_prior_synthesis_claims: set[str] = set()
        claim_owner: dict[str, set[tuple[str, str]]] = {}
        claim_evidence_by_id: dict[str, set[str]] = {}
        interpretation_by_primary: dict[
            str, list[tuple[InterpretationDependency, str, str]]
        ] = {}
        source_field_owner: dict[str, set[tuple[str, str]]] = {}
        primary_evidence_dependency: dict[str, set[str]] = {}
        allowed_evidence: set[str] = set()
        evidence_owner: dict[str, set[tuple[str, str]]] = {}
        allowed_locators: set[str] = set()
        locator_owner: dict[str, set[tuple[str, str]]] = {}
        allowed_topics: set[str] = set()
        topic_members: dict[str, set[str]] = {}
        allowed_fragments: set[str] = set()
        fragment_members: dict[str, set[str]] = {}
        allowed_result_ids: set[str] = set()
        allowed_relations: set[str] = set()
        relation_members: dict[str, set[str]] = {}

        def _as_list(value: Any) -> list[Any]:
            if value is None:
                return []
            if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
                return list(value)
            return [value]

        def add_claim(
            claim: Mapping[str, Any],
            paper_id: str,
            study_id: str = "",
            *,
            prior_synthesis: bool = False,
        ) -> None:
            claim_id = str(claim.get("claim_id") or "")
            if claim_id:
                if prior_synthesis:
                    allowed_prior_synthesis_claims.add(claim_id)
                else:
                    allowed_source_claims.add(claim_id)
                claim_owner.setdefault(claim_id, set()).add((paper_id, study_id))
            for value in claim.get("source_locator", ()) if isinstance(claim.get("source_locator"), list) else [claim.get("source_locator")]:
                locator = str(value or "")
                if locator:
                    allowed_locators.add(locator)
                    locator_owner.setdefault(locator, set()).add((paper_id, study_id))
            evidence_ids = claim.get("evidence_ids") or ()
            if isinstance(evidence_ids, Sequence) and not isinstance(evidence_ids, (str, bytes)):
                bound_evidence_ids = {
                    str(evidence_id or "") for evidence_id in evidence_ids if str(evidence_id or "")
                }
                if claim_id:
                    claim_evidence_by_id.setdefault(claim_id, set()).update(bound_evidence_ids)
                for evidence_id in evidence_ids:
                    key = str(evidence_id or "")
                    if key:
                        allowed_evidence.add(key)
                        evidence_owner.setdefault(key, set()).add((paper_id, study_id))

        for unit in request.get("evidence_units") or ():
            if not isinstance(unit, Mapping):
                continue
            paper_id = str(unit.get("paper_key") or "")
            if not paper_id:
                continue
            allowed_papers.add(paper_id)
            unit_studies = [
                item for item in unit.get("study_units") or () if isinstance(item, Mapping)
            ]
            study_ids = {
                str(item.get("study_id") or "")
                for item in unit_studies
                if str(item.get("source_study_id") or "")
            }
            study_ids.discard("")
            allowed_studies.update(study_ids)
            for study_id in study_ids:
                study_owner.setdefault(study_id, set()).add(paper_id)
            for study in unit_studies:
                study_id = str(study.get("study_id") or "")
                claim_owner_study = (
                    study_id if str(study.get("source_study_id") or "") else ""
                )
                for claim in study.get("claims") or ():
                    if isinstance(claim, Mapping):
                        if str(claim.get("study_id") or "") not in {"", claim_owner_study}:
                            raise OutlineV3ExecutionError(
                                f"{node_id} source claim has invalid study ownership"
                            )
                        add_claim(claim, paper_id, claim_owner_study)
                source_study_id = str(study.get("source_study_id") or "")
                visible_fields: dict[str, SourceFieldLedgerEntry] = {}
                for raw_field in study.get("interpretation_source_fields") or ():
                    if not isinstance(raw_field, Mapping):
                        raise OutlineV3ExecutionError(
                            f"{node_id} has a malformed interpretation source field"
                        )
                    try:
                        field = SourceFieldLedgerEntry.from_dict(raw_field)
                    except (TypeError, ValueError) as exc:
                        raise OutlineV3ExecutionError(
                            f"{node_id} has an invalid interpretation source field"
                        ) from exc
                    if field.scope == "explicit_study" and field.study_id != source_study_id:
                        raise OutlineV3ExecutionError(
                            f"{node_id} has a cross-study interpretation source field"
                        )
                    visible_fields[field.source_field_id] = field
                    source_field_owner.setdefault(field.source_field_id, set()).add(
                        (paper_id, claim_owner_study)
                    )
                for raw_dependency in study.get("interpretation_dependencies") or ():
                    if not isinstance(raw_dependency, Mapping):
                        raise OutlineV3ExecutionError(
                            f"{node_id} has a malformed interpretation dependency"
                        )
                    try:
                        dependency = InterpretationDependency.from_dict(raw_dependency)
                    except (TypeError, ValueError) as exc:
                        raise OutlineV3ExecutionError(
                            f"{node_id} has an invalid interpretation dependency"
                        ) from exc
                    if dependency.scope == "explicit_study" and dependency.study_id != study_id:
                        raise OutlineV3ExecutionError(
                            f"{node_id} has a cross-study interpretation dependency"
                        )
                    source_claim_ids = {
                        str(item.get("claim_id") or "")
                        for item in study.get("claims") or ()
                        if isinstance(item, Mapping)
                    }
                    if (
                        dependency.primary_claim_id not in source_claim_ids
                        or not set(dependency.required_source_claim_ids).issubset(source_claim_ids)
                        or not set(dependency.required_source_field_ids).issubset(visible_fields)
                    ):
                        raise OutlineV3ExecutionError(
                            f"{node_id} interpretation dependency is absent from provider-visible source context"
                        )
                    interpretation_by_primary.setdefault(
                        dependency.primary_claim_id, []
                    ).append((dependency, paper_id, claim_owner_study))
                    for claim in study.get("claims") or ():
                        if (
                            isinstance(claim, Mapping)
                            and str(claim.get("claim_id") or "") == dependency.primary_claim_id
                        ):
                            for evidence_id in claim.get("evidence_ids") or ():
                                primary_evidence_dependency.setdefault(
                                    str(evidence_id), set()
                                ).add(dependency.primary_claim_id)
                for locator in _as_list(study.get("source_locators")):
                    if isinstance(locator, Mapping):
                        locator_values = [value for values in locator.values() for value in _as_list(values)]
                    else:
                        locator_values = [locator]
                    for value in locator_values:
                        text = str(value or "")
                        if text:
                            allowed_locators.add(text)
                            locator_owner.setdefault(text, set()).add(
                                (paper_id, claim_owner_study)
                            )
            owner_study = next(iter(study_ids)) if len(study_ids) == 1 else ""
            for claim in unit.get("claims") or ():
                if isinstance(claim, Mapping):
                    claim_study = str(claim.get("study_id") or owner_study)
                    add_claim(claim, paper_id, claim_study)
            for field_values in (unit.get("evidence_ids_by_field") or {}).values() if isinstance(unit.get("evidence_ids_by_field"), Mapping) else ():
                for evidence_id in _as_list(field_values):
                    key = str(evidence_id or "")
                    if key:
                        allowed_evidence.add(key)
                        evidence_owner.setdefault(key, set()).add((paper_id, owner_study))
            text_by_id = unit.get("evidence_text_by_id")
            if isinstance(text_by_id, Mapping):
                for evidence_id in text_by_id:
                    key = str(evidence_id or "")
                    if key:
                        allowed_evidence.add(key)
                        evidence_owner.setdefault(key, set()).add((paper_id, owner_study))
            for locator_group in (unit.get("source_locators") or {}).values() if isinstance(unit.get("source_locators"), Mapping) else ():
                for locator in _as_list(locator_group):
                    key = str(locator or "")
                    if key:
                        allowed_locators.add(key)
                        locator_owner.setdefault(key, set()).add((paper_id, owner_study))

        for topic in request.get("topics") or ():
            if not isinstance(topic, Mapping):
                continue
            topic_id = str(topic.get("topic_id") or "")
            fragment_id = str(topic.get("fragment_id") or topic_id)
            if topic_id:
                allowed_topics.add(topic_id)
                topic_members.setdefault(topic_id, set()).update(
                    str(value) for value in topic.get("paper_ids") or () if str(value)
                )
            if fragment_id:
                allowed_fragments.add(fragment_id)
                fragment_members.setdefault(fragment_id, set()).update(
                    str(value) for value in topic.get("paper_ids") or () if str(value)
                )

        def add_prior_synthesis(value: Any) -> None:
            if isinstance(value, Mapping):
                paper_ids = {
                    str(item) for item in value.get("paper_ids") or () if str(item)
                }
                if value.get("paper_key"):
                    paper_ids.add(str(value["paper_key"]))
                for paper_id in paper_ids:
                    allowed_papers.add(paper_id)
                explicit_topic_members = value.get("topic_members_by_id")
                if isinstance(explicit_topic_members, Mapping):
                    for member_topic_id, members in explicit_topic_members.items():
                        member_key = str(member_topic_id or "")
                        if member_key:
                            allowed_topics.add(member_key)
                            topic_members.setdefault(member_key, set()).update(
                                str(item) for item in _as_list(members) if str(item)
                            )
                explicit_relation_members = value.get("relation_members_by_id")
                if isinstance(explicit_relation_members, Mapping):
                    for member_relation_id, members in explicit_relation_members.items():
                        member_key = str(member_relation_id or "")
                        if member_key:
                            allowed_relations.add(member_key)
                            relation_members.setdefault(member_key, set()).update(
                                str(item) for item in _as_list(members) if str(item)
                            )
                explicit_fragment_members = value.get("fragment_members_by_id")
                if isinstance(explicit_fragment_members, Mapping):
                    for member_fragment_id, members in explicit_fragment_members.items():
                        member_key = str(member_fragment_id or "")
                        if member_key:
                            allowed_fragments.add(member_key)
                            fragment_members.setdefault(member_key, set()).update(
                                str(item) for item in _as_list(members) if str(item)
                            )
                study_id = str(value.get("study_id") or "")
                if study_id:
                    allowed_studies.add(study_id)
                    for paper_id in paper_ids:
                        study_owner.setdefault(study_id, set()).add(paper_id)
                for evidence_id in (
                    *_as_list(value.get("evidence_id")),
                    *_as_list(value.get("evidence_ids")),
                    *_as_list(value.get("supporting_evidence_ids")),
                ):
                    key = str(evidence_id or "")
                    if key:
                        allowed_evidence.add(key)
                        for paper_id in paper_ids:
                            evidence_owner.setdefault(key, set()).add((paper_id, study_id))
                for claim_id in _as_list(value.get("claim_id")):
                    key = str(claim_id or "")
                    if key:
                        if key.startswith("synthesis:"):
                            allowed_prior_synthesis_claims.add(key)
                        else:
                            allowed_source_claims.add(key)
                        claim_evidence_by_id.setdefault(key, set()).update(
                            str(item) for item in _as_list(value.get("evidence_ids")) if str(item)
                        )
                        for paper_id in paper_ids:
                            claim_owner.setdefault(key, set()).add((paper_id, study_id))
                for claim_id in (
                    *_as_list(value.get("source_claim_id")),
                    *_as_list(value.get("source_claim_ids")),
                ):
                    key = str(claim_id or "")
                    if key:
                        if key.startswith("synthesis:"):
                            allowed_prior_synthesis_claims.add(key)
                        else:
                            allowed_source_claims.add(key)
                        claim_evidence_by_id.setdefault(key, set()).update(
                            str(item) for item in _as_list(value.get("evidence_ids")) if str(item)
                        )
                        for paper_id in paper_ids:
                            claim_owner.setdefault(key, set()).add((paper_id, study_id))
                for locator in (
                    *_as_list(value.get("source_locator")),
                    *_as_list(value.get("source_locators")),
                ):
                    key = str(locator or "")
                    if key:
                        allowed_locators.add(key)
                        for paper_id in paper_ids:
                            locator_owner.setdefault(key, set()).add((paper_id, study_id))
                topic_id = str(value.get("topic_id") or "")
                if topic_id:
                    allowed_topics.add(topic_id)
                    topic_members.setdefault(topic_id, set()).update(paper_ids)
                for inherited_topic_id in (
                    *_as_list(value.get("topic_ids")),
                    *_as_list(value.get("processed_topic_ids")),
                ):
                    if str(inherited_topic_id or ""):
                        key = str(inherited_topic_id)
                        allowed_topics.add(key)
                        if key not in (explicit_topic_members or {}):
                            topic_members.setdefault(key, set()).update(paper_ids)
                for inherited_relation_id in (
                    *_as_list(value.get("relation_id")),
                    *_as_list(value.get("relation_ids")),
                    *_as_list(value.get("processed_relation_ids")),
                ):
                    if str(inherited_relation_id or ""):
                        key = str(inherited_relation_id)
                        allowed_relations.add(key)
                        if key not in (explicit_relation_members or {}):
                            relation_members.setdefault(key, set()).update(paper_ids)
                fragment_id = str(value.get("fragment_id") or "")
                if fragment_id:
                    allowed_fragments.add(fragment_id)
                    if fragment_id not in (explicit_fragment_members or {}):
                        fragment_members.setdefault(fragment_id, set()).update(paper_ids)
                for fragment_id in (
                    *_as_list(value.get("fragment_ids")),
                    *_as_list(value.get("processed_fragment_ids")),
                ):
                    if str(fragment_id or ""):
                        key = str(fragment_id)
                        allowed_fragments.add(key)
                        if key not in (explicit_fragment_members or {}):
                            fragment_members.setdefault(key, set()).update(paper_ids)
                for result_id in (
                    *_as_list(value.get("result_id")),
                    *_as_list(value.get("batch_result_id")),
                    *_as_list(value.get("result_ids")),
                    *_as_list(value.get("batch_result_ids")),
                    *_as_list(value.get("processed_result_ids")),
                ):
                    if str(result_id or ""):
                        allowed_result_ids.add(str(result_id))
                for key, child in value.items():
                    if key in {"claims", "bridge_claims", "synthesis_claims"}:
                        for claim in child if isinstance(child, list) else ():
                            if not isinstance(claim, Mapping):
                                continue
                            owner_papers = {
                                str(item)
                                for item in _as_list(claim.get("paper_keys"))
                                if str(item)
                            }
                            if claim.get("paper_key"):
                                owner_papers.add(str(claim["paper_key"]))
                            owner_study = str(claim.get("study_id") or "")
                            for owner_paper in owner_papers:
                                allowed_papers.add(owner_paper)
                                for evidence_id in _as_list(claim.get("evidence_ids")):
                                    evidence = str(evidence_id or "")
                                    if evidence:
                                        allowed_evidence.add(evidence)
                                        evidence_owner.setdefault(evidence, set()).add((owner_paper, owner_study))
                                add_claim(
                                    claim,
                                    owner_paper,
                                    owner_study,
                                    prior_synthesis=True,
                                )
                            for source_claim_id in (
                                *_as_list(claim.get("source_claim_ids")),
                                _as_list(claim.get("source_claim_id"))[0]
                                if claim.get("source_claim_id")
                                else "",
                            ):
                                if str(source_claim_id or ""):
                                    source_key = str(source_claim_id)
                                    if source_key.startswith("synthesis:"):
                                        allowed_prior_synthesis_claims.add(source_key)
                                    else:
                                        allowed_source_claims.add(source_key)
                                    for owner_paper in owner_papers:
                                        claim_owner.setdefault(source_key, set()).add(
                                            (owner_paper, owner_study)
                                        )
                            if owner_study:
                                allowed_studies.add(owner_study)
                                for owner_paper in owner_papers:
                                    study_owner.setdefault(owner_study, set()).add(owner_paper)
                    elif key == "relation_candidates" and isinstance(child, list):
                        for relation in child:
                            if not isinstance(relation, Mapping):
                                continue
                            relation_id = str(relation.get("relation_id") or "")
                            if relation_id:
                                allowed_relations.add(relation_id)
                                relation_members.setdefault(relation_id, set()).update(
                                    str(item) for item in relation.get("paper_keys") or () if str(item)
                                )
                    else:
                        add_prior_synthesis(child)
            elif isinstance(value, list):
                for child in value:
                    add_prior_synthesis(child)

        for topic_fragment in request.get("topic_synthesis") or ():
            if not isinstance(topic_fragment, Mapping):
                continue
            context = topic_fragment.get("interpretation_context")
            if not isinstance(context, Mapping):
                continue
            prior_visible_fields: set[str] = set()
            for raw_field in context.get("fields") or ():
                if not isinstance(raw_field, Mapping):
                    raise OutlineV3ExecutionError(
                        f"{node_id} has malformed prior interpretation context"
                    )
                try:
                    field = SourceFieldLedgerEntry.from_dict(raw_field)
                except (TypeError, ValueError) as exc:
                    raise OutlineV3ExecutionError(
                        f"{node_id} has invalid prior interpretation source text"
                    ) from exc
                paper_id = str(raw_field.get("paper_key") or "")
                owner_study_id = str(raw_field.get("owner_study_id") or "")
                if not paper_id or paper_id not in {
                    str(value)
                    for value in (
                        *list(topic_fragment.get("paper_ids") or ()),
                        *list(topic_fragment.get("bridge_paper_ids") or ()),
                    )
                }:
                    raise OutlineV3ExecutionError(
                        f"{node_id} prior interpretation field is outside its topic fragment"
                    )
                prior_visible_fields.add(field.source_field_id)
                source_field_owner.setdefault(field.source_field_id, set()).add(
                    (paper_id, owner_study_id)
                )
            for raw_dependency in context.get("dependencies") or ():
                if not isinstance(raw_dependency, Mapping):
                    raise OutlineV3ExecutionError(
                        f"{node_id} has malformed prior interpretation dependency"
                    )
                try:
                    dependency = InterpretationDependency.from_dict(raw_dependency)
                except (TypeError, ValueError) as exc:
                    raise OutlineV3ExecutionError(
                        f"{node_id} has invalid prior interpretation dependency"
                    ) from exc
                if not set(dependency.required_source_field_ids).issubset(prior_visible_fields):
                    raise OutlineV3ExecutionError(
                        f"{node_id} prior interpretation dependency has no visible source text"
                    )
                paper_id = str(raw_dependency.get("paper_key") or "")
                owner_study_id = str(raw_dependency.get("owner_study_id") or "")
                if not paper_id or paper_id not in {
                    str(value)
                    for value in (
                        *list(topic_fragment.get("paper_ids") or ()),
                        *list(topic_fragment.get("bridge_paper_ids") or ()),
                    )
                }:
                    raise OutlineV3ExecutionError(
                        f"{node_id} prior interpretation dependency is outside its topic fragment"
                    )
                interpretation_by_primary.setdefault(
                    dependency.primary_claim_id, []
                ).append((dependency, paper_id, owner_study_id))
                for source_claim_id in (
                    dependency.primary_claim_id,
                    *dependency.required_source_claim_ids,
                ):
                    allowed_source_claims.add(source_claim_id)
                    claim_owner.setdefault(source_claim_id, set()).add(
                        (paper_id, owner_study_id)
                    )
                for evidence_id in raw_dependency.get("primary_evidence_ids") or ():
                    key = str(evidence_id or "")
                    if key:
                        primary_evidence_dependency.setdefault(key, set()).add(
                            dependency.primary_claim_id
                        )
                        claim_evidence_by_id.setdefault(
                            dependency.primary_claim_id, set()
                        ).add(key)
                        allowed_evidence.add(key)
                        evidence_owner.setdefault(key, set()).add(
                            (paper_id, owner_study_id)
                        )
                for evidence_id in dependency.required_evidence_ids:
                    allowed_evidence.add(evidence_id)
                    evidence_owner.setdefault(evidence_id, set()).add(
                        (paper_id, owner_study_id)
                    )
                    for qualifier_id in dependency.required_source_claim_ids:
                        claim_evidence_by_id.setdefault(qualifier_id, set()).add(
                            evidence_id
                        )

        add_prior_synthesis(request.get("topic_synthesis"))
        add_prior_synthesis(request.get("cross_group_comparison"))
        if local_cross_ledger is not None:
            ledger = local_cross_ledger
            for topic_id, members in (ledger.get("topic_members_by_id") or {}).items():
                key = str(topic_id)
                allowed_topics.add(key)
                topic_members.setdefault(key, set()).update(
                    str(member) for member in _as_list(members) if str(member)
                )
            for fragment_id, members in (ledger.get("fragment_members_by_id") or {}).items():
                key = str(fragment_id)
                allowed_fragments.add(key)
                fragment_members.setdefault(key, set()).update(
                    str(member) for member in _as_list(members) if str(member)
                )
            allowed_result_ids.update(str(item) for item in ledger.get("result_ids") or () if str(item))
            allowed_papers.update(
                member for members in topic_members.values() for member in members
            )
        for relation in request.get("relation_candidates") or ():
            if not isinstance(relation, Mapping):
                continue
            relation_id = str(relation.get("relation_id") or "")
            if relation_id:
                allowed_relations.add(relation_id)
                relation_members.setdefault(relation_id, set()).update(
                    str(item) for item in relation.get("paper_keys") or () if str(item)
                )
            for paper_id in relation.get("paper_keys") or ():
                if str(paper_id):
                    allowed_papers.add(str(paper_id))
            for claim_id in (*_as_list(relation.get("claim_ids_left")), *_as_list(relation.get("claim_ids_right"))):
                if str(claim_id):
                    allowed_source_claims.add(str(claim_id))
                    for paper_id in relation.get("paper_keys") or ():
                        if str(paper_id):
                            claim_owner.setdefault(str(claim_id), set()).add((str(paper_id), ""))
            for evidence_id in (*_as_list(relation.get("required_evidence_ids")), *_as_list(relation.get("provided_evidence_ids"))):
                if str(evidence_id):
                    allowed_evidence.add(str(evidence_id))

        # Arrays are part of the versioned provider contract; an empty object or
        # an omitted array must not be treated as a successful semantic result.
        prefix = str(node_id or "")
        if prefix.startswith("topic_synthesis_provider"):
            required_arrays = ("topics", "processed_fragment_ids", "claims", "unresolved_questions")
            requested_fragments = [
                str(item.get("fragment_id") or item.get("topic_id") or "")
                for item in request.get("topics") or ()
                if isinstance(item, Mapping)
            ]
            processed = _as_list(result.get("processed_fragment_ids"))
            if sorted(str(item) for item in processed) != sorted(requested_fragments) or len(processed) != len(set(map(str, processed))):
                raise OutlineV3ExecutionError(f"{node_id} did not process every requested topic fragment exactly once")
            returned_fragments = [
                str(item.get("fragment_id") or "")
                for item in result.get("topics") or ()
                if isinstance(item, Mapping)
            ]
            if sorted(returned_fragments) != sorted(requested_fragments) or len(returned_fragments) != len(set(returned_fragments)):
                raise OutlineV3ExecutionError(f"{node_id} topic output fragment coverage is invalid")
            requested_pairs = {
                str(item.get("fragment_id") or item.get("topic_id") or ""): str(item.get("topic_id") or "")
                for item in request.get("topics") or ()
                if isinstance(item, Mapping)
            }
            if any(
                requested_pairs.get(str(topic.get("fragment_id") or ""))
                != str(topic.get("topic_id") or "")
                for topic in result.get("topics") or ()
                if isinstance(topic, Mapping)
            ):
                raise OutlineV3ExecutionError(f"{node_id} topic output fragment/topic pairing is invalid")
            output_contract = request.get("output_contract")
            if isinstance(output_contract, Mapping) and output_contract.get(
                "semantic_result_contract_version"
            ) == "bounded-topic-synthesis/v5":
                reason_limit = output_contract.get("max_unresolved_reason_utf8_bytes")
                if type(reason_limit) is not int or reason_limit < 1:
                    raise OutlineV3ExecutionError(f"{node_id} has an invalid topic fallback reason limit")
                questions = result.get("unresolved_questions")
                if isinstance(questions, list) and any(
                    not isinstance(question, str) or not question.strip()
                    for question in questions
                ):
                    raise OutlineV3ExecutionError(
                        f"{node_id} unresolved_questions entries must be non-empty strings"
                    )
                for topic in result.get("topics") or ():
                    if not isinstance(topic, Mapping):
                        continue
                    status = str(topic.get("status") or "").strip()
                    if status not in {"integrated", "completed", "processed", "unresolved"}:
                        raise OutlineV3ExecutionError(
                            f"{node_id} topic output has an unsupported status"
                        )
                    reasons = topic.get("unresolved_questions")
                    if reasons is None:
                        reasons = []
                    if not isinstance(reasons, list):
                        raise OutlineV3ExecutionError(
                            f"{node_id} topic unresolved_questions must be an array"
                        )
                    if status == "unresolved":
                        if not reasons:
                            raise OutlineV3ExecutionError(
                                f"{node_id} unresolved topic has no explicit reason"
                            )
                        fallback_fields = {
                            "topic_id", "fragment_id", "status", "conclusions",
                            "unresolved_questions", "supporting_evidence_ids",
                        }
                        if (
                            set(topic) - fallback_fields
                            or topic.get("conclusions")
                            or topic.get("supporting_evidence_ids")
                            or any(
                                isinstance(claim, Mapping)
                                and str(claim.get("fragment_id") or "")
                                == str(topic.get("fragment_id") or "")
                                for claim in result.get("claims") or ()
                            )
                        ):
                            raise OutlineV3ExecutionError(
                                f"{node_id} unresolved topic cannot carry partial factual claims"
                            )
                    if any(not isinstance(reason, str) for reason in reasons):
                        raise OutlineV3ExecutionError(
                            f"{node_id} topic unresolved_questions entries must be strings"
                        )
                    if any(not reason.strip() for reason in reasons):
                        raise OutlineV3ExecutionError(
                            f"{node_id} topic unresolved_questions entries must be non-empty"
                        )
                    # This cap sizes the explicit fallback reason, not scientific
                    # questions that accompany an integrated/completed topic.
                    if status == "unresolved" and any(
                        len(reason.encode("utf-8")) > reason_limit
                        for reason in reasons
                    ):
                        raise OutlineV3ExecutionError(
                            f"{node_id} topic reason exceeds max_unresolved_reason_utf8_bytes"
                        )
        elif prefix.startswith("cross_group_comparison_provider"):
            shared_contract = request.get("shared_synthesis_contract_version") == SHARED_SYNTHESIS_CONTRACT_VERSION
            required_arrays = (
                ("comparisons", "bridge_claims", "topic_dispositions", "unresolved_questions")
                if shared_contract else
                ("comparisons", "bridge_claims", "processed_topic_ids", "processed_fragment_ids",
                 "processed_result_ids", "processed_relation_ids", "unresolved_questions")
            )
            def require_exact_coverage(field: str, expected: set[str] | Sequence[str]) -> None:
                actual_values = [str(item) for item in _as_list(result.get(field))]
                expected_values = sorted(str(item) for item in expected if str(item))
                if (
                    sorted(actual_values) != expected_values
                    or len(actual_values) != len(set(actual_values))
                ):
                    raise OutlineV3ExecutionError(
                        f"{node_id} did not process every requested {field.removeprefix('processed_').removesuffix('_ids')} exactly once"
                    )

            if not shared_contract:
                require_exact_coverage("processed_topic_ids", allowed_topics)
                require_exact_coverage("processed_fragment_ids", allowed_fragments)
                require_exact_coverage("processed_result_ids", allowed_result_ids)
                require_exact_coverage("processed_relation_ids", sorted(allowed_relations))
            if shared_contract:
                dispositions = result.get("topic_dispositions")
                if not isinstance(dispositions, list):
                    raise OutlineV3ExecutionError(f"{node_id} has no topic dispositions")
                disposition_topics = [
                    str(row.get("topic_id") or "")
                    for row in dispositions if isinstance(row, Mapping)
                ]
                if (
                    len(disposition_topics) != len(dispositions)
                    or sorted(disposition_topics) != sorted(allowed_topics)
                    or len(disposition_topics) != len(set(disposition_topics))
                ):
                    raise OutlineV3ExecutionError(
                        f"{node_id} topic dispositions do not cover every topic exactly once"
                    )
                available_claims = {
                    str(claim.get("claim_id") or ""): claim
                    for claim in result.get("bridge_claims") or ()
                    if isinstance(claim, Mapping)
                    and str(claim.get("claim_id") or "")
                }
                integrated_count = 0
                for row in dispositions:
                    status = str(row.get("status") or "").strip().lower()
                    topic_id = str(row.get("topic_id") or "")
                    claim_ids = {
                        str(value) for value in row.get("synthesis_claim_ids") or () if str(value)
                    }
                    if status == "integrated":
                        integrated_count += 1
                        if not claim_ids or not claim_ids.issubset(available_claims):
                            raise OutlineV3ExecutionError(
                                f"{node_id} integrated topic has no validated synthesis claim"
                            )
                        if any(
                            topic_id not in {
                                *[str(value) for value in _as_list(available_claims[claim_id].get("topic_ids")) if str(value)],
                                str(available_claims[claim_id].get("topic_id") or ""),
                            }
                            for claim_id in claim_ids
                        ):
                            raise OutlineV3ExecutionError(
                                f"{node_id} integrated topic references a synthesis claim for another topic"
                            )
                    elif status in {"unresolved", "deferred", "insufficient_evidence", "not_comparable"}:
                        if claim_ids or not str(row.get("reason") or "").strip():
                            raise OutlineV3ExecutionError(
                                f"{node_id} unresolved topic lacks a reason or claims support"
                            )
                    else:
                        raise OutlineV3ExecutionError(
                            f"{node_id} has an invalid topic disposition status"
                        )
                if allowed_topics and not integrated_count and ":reduce:" not in prefix:
                    raise OutlineV3ExecutionError(
                        f"{node_id} has no supported integrated topic; global synthesis cannot proceed"
                    )
        elif prefix.startswith("global_synthesis_provider"):
            shared_contract = request.get("shared_synthesis_contract_version") == SHARED_SYNTHESIS_CONTRACT_VERSION
            required_arrays = (
                ("synthesis_claims", "organizing_principles", "unresolved_questions")
                if shared_contract else
                ("synthesis_claims", "organizing_principles", "processed_topic_ids",
                 "processed_fragment_ids", "processed_result_ids", "unresolved_questions")
            )
            def require_exact_coverage(field: str, expected: set[str] | Sequence[str]) -> None:
                actual_values = [str(item) for item in _as_list(result.get(field))]
                expected_values = sorted(str(item) for item in expected if str(item))
                if (
                    sorted(actual_values) != expected_values
                    or len(actual_values) != len(set(actual_values))
                ):
                    raise OutlineV3ExecutionError(
                        f"{node_id} did not process every requested {field.removeprefix('processed_').removesuffix('_ids')} exactly once"
                    )

            if not shared_contract:
                require_exact_coverage("processed_topic_ids", allowed_topics)
                require_exact_coverage("processed_fragment_ids", allowed_fragments)
                require_exact_coverage("processed_result_ids", allowed_result_ids)
            if request.get("shared_synthesis_contract_version") == SHARED_SYNTHESIS_CONTRACT_VERSION:
                if allowed_topics and (
                    not result.get("synthesis_claims")
                    or not result.get("organizing_principles")
                ):
                    raise OutlineV3ExecutionError(
                        f"{node_id} has no substantive supported synthesis and organizing principle"
                    )
        else:
            required_arrays = ()
        for key in required_arrays:
            if key not in result or not isinstance(result.get(key), list):
                raise OutlineV3ExecutionError(f"{node_id} result is missing required array {key!r}")

        unknown: list[str] = []
        mismatch: list[str] = []
        semantic_namespace = OutlineV3Executor._semantic_receipt_node_id(prefix)
        expected_synthesis_claim_prefix = f"synthesis:{semantic_namespace}:"

        def reference_values(value: Any) -> list[str]:
            return [str(item) for item in value] if isinstance(value, list) else [str(value)]

        def walk(value: Any, path: str = "") -> None:
            if isinstance(value, Mapping):
                paper_values = [
                    str(item)
                    for key in ("paper_key", "canonical_paper_key", "paper_ids", "paper_keys")
                    for item in reference_values(value.get(key))
                    if value.get(key) is not None and str(item)
                ]
                study_values = [
                    str(item)
                    for item in reference_values(value.get("study_id"))
                    if value.get("study_id") is not None and str(item)
                ]
                for key in ("paper_key", "canonical_paper_key"):
                    if key in value:
                        for item in reference_values(value[key]):
                            if item and item not in allowed_papers:
                                unknown.append(f"{path}.{key}={item}")
                for key in ("paper_ids", "paper_keys"):
                    if key in value:
                        for item in reference_values(value[key]):
                            if item and item not in allowed_papers:
                                unknown.append(f"{path}.{key}={item}")
                topic_refs = [
                    str(item)
                    for key in ("topic_id", "topic_ids")
                    for item in reference_values(value.get(key))
                    if value.get(key) is not None and str(item)
                ]
                for topic_id in topic_refs:
                    if topic_id not in allowed_topics:
                        unknown.append(f"{path}.topic_id={topic_id}")
                if topic_refs and paper_values:
                    topic_papers = set().union(
                        *(topic_members.get(topic_id, set()) for topic_id in topic_refs)
                    )
                    if not set(paper_values).issubset(topic_papers):
                        mismatch.append(
                            f"{path} papers {sorted(set(paper_values))} are outside topic membership {sorted(topic_papers)}"
                        )
                fragment_refs = [
                    str(item)
                    for key in ("fragment_id", "fragment_ids")
                    for item in reference_values(value.get(key))
                    if value.get(key) is not None and str(item)
                ]
                for fragment_id in fragment_refs:
                    if fragment_id not in allowed_fragments:
                        unknown.append(f"{path}.fragment_id={fragment_id}")
                if fragment_refs and paper_values:
                    fragment_papers = set().union(
                        *(fragment_members.get(fragment_id, set()) for fragment_id in fragment_refs)
                    )
                    if not set(paper_values).issubset(fragment_papers):
                        mismatch.append(
                            f"{path} papers {sorted(set(paper_values))} are outside fragment membership {sorted(fragment_papers)}"
                        )
                relation_refs = [
                    str(item)
                    for key in ("relation_id", "relation_ids")
                    for item in reference_values(value.get(key))
                    if value.get(key) is not None and str(item)
                ]
                for relation_id in relation_refs:
                    if relation_id not in allowed_relations:
                        unknown.append(f"{path}.relation_id={relation_id}")
                if relation_refs and paper_values:
                    relation_papers = set().union(
                        *(relation_members.get(relation_id, set()) for relation_id in relation_refs)
                    )
                    if not set(paper_values).issubset(relation_papers):
                        mismatch.append(
                            f"{path} papers {sorted(set(paper_values))} are outside relation membership {sorted(relation_papers)}"
                        )
                for study_id in study_values:
                    if study_id not in allowed_studies:
                        unknown.append(f"{path}.study_id={study_id}")
                    elif paper_values and not set(paper_values).intersection(study_owner.get(study_id, set())):
                        mismatch.append(f"{path}.study_id={study_id} is not owned by paper {paper_values}")
                for key in ("evidence_id", "evidence_ids", "supporting_evidence_ids"):
                    if key not in value:
                        continue
                    for evidence_id in reference_values(value[key]):
                        if not evidence_id:
                            continue
                        if evidence_id not in allowed_evidence:
                            unknown.append(f"{path}.{key}={evidence_id}")
                            continue
                        owners = evidence_owner.get(evidence_id, set())
                        if paper_values and not set(paper_values).intersection(owner[0] for owner in owners):
                            mismatch.append(f"{path}.{key}={evidence_id} is not owned by paper {paper_values}")
                        if study_values and not any(
                            owner_study in study_values
                            for _owner_paper, owner_study in owners
                            if not paper_values or _owner_paper in paper_values
                        ):
                            mismatch.append(f"{path}.{key}={evidence_id} is not owned by study {study_values}")
                for key in ("source_claim_id", "source_claim_ids"):
                    if key in value:
                        for claim_id in reference_values(value[key]):
                            if claim_id and claim_id not in (
                                allowed_source_claims | allowed_prior_synthesis_claims
                            ):
                                unknown.append(f"{path}.{key}={claim_id}")
                            elif claim_id and paper_values and not set(paper_values).intersection(
                                owner[0] for owner in claim_owner.get(claim_id, set())
                            ):
                                mismatch.append(f"{path}.{key}={claim_id} is not owned by paper {paper_values}")
                            elif claim_id and study_values and not any(
                                owner_study in study_values
                                for owner_paper, owner_study in claim_owner.get(claim_id, set())
                                if not paper_values or owner_paper in paper_values
                            ):
                                mismatch.append(
                                    f"{path}.{key}={claim_id} is not owned by study {study_values}"
                                )
                if "claim_id" in value:
                    claim_id = str(value.get("claim_id") or "")
                    if claim_id and not claim_id.startswith(expected_synthesis_claim_prefix):
                        unknown.append(
                            f"{path}.claim_id={claim_id} must use the {expected_synthesis_claim_prefix}<id> claim namespace"
                        )
                referenced_claim_ids = [
                    str(item)
                    for key in ("source_claim_id", "source_claim_ids")
                    if key in value
                    for item in reference_values(value[key])
                    if str(item)
                ]
                supplied_evidence_ids = {
                    str(item)
                    for key in ("evidence_id", "evidence_ids", "supporting_evidence_ids")
                    if key in value
                    for item in reference_values(value[key])
                    if str(item)
                }
                supplied_field_ids = {
                    str(item)
                    for item in reference_values(value.get("source_field_ids"))
                    if value.get("source_field_ids") is not None and str(item)
                }
                for field_id in supplied_field_ids:
                    owners = source_field_owner.get(field_id)
                    if not owners:
                        unknown.append(f"{path}.source_field_ids={field_id}")
                    elif paper_values and not set(paper_values).intersection(
                        paper for paper, _study in owners
                    ):
                        mismatch.append(
                            f"{path}.source_field_ids={field_id} is not owned by paper {paper_values}"
                        )
                    elif study_values and not any(
                        owner_study in study_values
                        for owner_paper, owner_study in owners
                        if not paper_values or owner_paper in paper_values
                    ):
                        mismatch.append(
                            f"{path}.source_field_ids={field_id} is not owned by study {study_values}"
                        )
                if referenced_claim_ids and supplied_evidence_ids:
                    referenced_evidence_ids = set().union(
                        *(claim_evidence_by_id.get(claim_id, set()) for claim_id in referenced_claim_ids)
                    )
                    if not supplied_evidence_ids.issubset(referenced_evidence_ids):
                        mismatch.append(
                            f"{path} evidence {sorted(supplied_evidence_ids)} is not bound to source claims {sorted(referenced_claim_ids)}"
                        )
                factual_record = bool(
                    value.get("claim_id")
                    or any(
                        value.get(key)
                        for key in (
                            "text", "conclusion", "conclusions", "comparison",
                            "finding", "comparative_claim", "synthesis",
                        )
                    )
                )
                if factual_record:
                    primary_ids = set(referenced_claim_ids).intersection(
                        interpretation_by_primary
                    )
                    primary_ids.update(
                        primary_id
                        for evidence_id in supplied_evidence_ids
                        for primary_id in primary_evidence_dependency.get(evidence_id, ())
                    )
                    for primary_id in sorted(primary_ids):
                        for dependency, owner_paper, owner_study in interpretation_by_primary[primary_id]:
                            if paper_values and owner_paper not in paper_values:
                                continue
                            if study_values and owner_study not in study_values:
                                mismatch.append(
                                    f"{path} interpretation of {primary_id} is outside source study scope"
                                )
                                continue
                            if not (
                                set(dependency.required_source_claim_ids).issubset(
                                    referenced_claim_ids
                                )
                                and set(dependency.required_evidence_ids).issubset(
                                    supplied_evidence_ids
                                )
                                and set(dependency.required_source_field_ids).issubset(
                                    supplied_field_ids
                                )
                            ):
                                mismatch.append(
                                    f"{path} interpretation of {primary_id} omits required qualifiers"
                                )
                if "source_locator" in value or "source_locators" in value:
                    for key in ("source_locator", "source_locators"):
                        if key not in value:
                            continue
                        for locator in reference_values(value[key]):
                            if locator and locator not in allowed_locators:
                                unknown.append(f"{path}.{key}={locator}")
                            elif locator and paper_values and not set(paper_values).intersection(
                                owner[0] for owner in locator_owner.get(locator, set())
                            ):
                                mismatch.append(f"{path}.{key}={locator} is not owned by paper {paper_values}")
                for key, child in value.items():
                    child_path = f"{path}.{key}" if path else str(key)
                    if key in {"paper_key", "canonical_paper_key", "paper_ids", "paper_keys", "topic_id", "topic_ids", "fragment_id", "fragment_ids", "relation_id", "relation_ids", "study_id", "evidence_id", "evidence_ids", "supporting_evidence_ids", "source_claim_id", "source_claim_ids", "source_field_ids", "claim_id", "source_locator", "source_locators"}:
                        continue
                    walk(child, child_path)
            elif isinstance(value, list):
                for index, child in enumerate(value):
                    walk(child, f"{path}[{index}]")

        walk(result)
        unsupported_facts: list[str] = []

        def record_has_direct_support(value: Mapping[str, Any]) -> bool:
            return any(
                bool(_as_list(value.get(key)))
                for key in (
                    "evidence_id",
                    "evidence_ids",
                    "supporting_evidence_ids",
                    "source_claim_id",
                    "source_claim_ids",
                )
            )

        for topic_index, topic in enumerate(result.get("topics") or ()):
            if not isinstance(topic, Mapping):
                continue
            topic_support = record_has_direct_support(topic)
            for conclusion_index, conclusion in enumerate(topic.get("conclusions") or ()):
                if isinstance(conclusion, Mapping):
                    supported = record_has_direct_support(conclusion) or topic_support
                else:
                    supported = bool(str(conclusion or "").strip()) and topic_support
                if str(conclusion or "").strip() and not supported:
                    unsupported_facts.append(
                        f"{node_id}.topics[{topic_index}].conclusions[{conclusion_index}]"
                    )
        for comparison_index, comparison in enumerate(result.get("comparisons") or ()):
            if not isinstance(comparison, Mapping):
                raise OutlineV3ExecutionError(
                    f"{node_id}.comparisons[{comparison_index}] must be an object"
                )
            if not record_has_direct_support(comparison):
                unsupported_facts.append(
                    f"{node_id}.comparisons[{comparison_index}]"
                )

        claim_fields = ("claims", "bridge_claims", "synthesis_claims")
        seen_output_claim_ids: set[str] = set()
        for key in claim_fields:
            values = result.get(key)
            if not isinstance(values, list):
                continue
            for index, claim in enumerate(values):
                if not isinstance(claim, Mapping):
                    raise OutlineV3ExecutionError(f"{node_id}.{key}[{index}] must be an object")
                claim_id = str(claim.get("claim_id") or "")
                if not claim_id.startswith(expected_synthesis_claim_prefix):
                    raise OutlineV3ExecutionError(
                        f"{node_id}.{key}[{index}] must use the {expected_synthesis_claim_prefix}<id> claim namespace"
                    )
                if claim_id in seen_output_claim_ids:
                    raise OutlineV3ExecutionError(
                        f"{node_id} returned duplicate synthesis claim identity {claim_id}"
                    )
                seen_output_claim_ids.add(claim_id)
                if prefix.startswith("topic_synthesis_provider") and not str(
                    claim.get("fragment_id") or ""
                ):
                    raise OutlineV3ExecutionError(
                        f"{node_id}.{key}[{index}] must bind to a requested fragment_id"
                    )
                if not (
                    claim.get("paper_key")
                    or _as_list(claim.get("paper_keys"))
                ):
                    raise OutlineV3ExecutionError(
                        f"{node_id}.{key}[{index}] must identify at least one supporting paper"
                    )
                has_support = bool(
                    _as_list(claim.get("evidence_ids"))
                    or _as_list(claim.get("source_claim_ids"))
                )
                if not has_support:
                    raise OutlineV3ExecutionError(
                        f"{node_id}.{key}[{index}] is a factual claim without evidence or source-claim support"
                    )
        if unknown or mismatch:
            details = sorted(set([*unknown, *mismatch]))[:20]
            raise OutlineV3ExecutionError(
                f"{node_id} returned identities outside its evidence contract: "
                + "; ".join(details)
            )
        if unsupported_facts:
            raise OutlineV3ExecutionError(
                "factual without evidence or source-claim support: "
                + "; ".join(unsupported_facts[:20])
            )

    @staticmethod
    def _interpretation_context_for_units(
        evidence_units: Sequence[Any],
        planned_unit_ids: Sequence[str],
    ) -> dict[str, list[dict[str, Any]]]:
        """Carry the actual qualifier text and ownership into later synthesis."""

        selected = {str(value) for value in planned_unit_ids if str(value)}
        fields: dict[str, dict[str, Any]] = {}
        dependencies: dict[str, dict[str, Any]] = {}
        for unit in evidence_units:
            if (
                not isinstance(unit, Mapping)
                or str(unit.get("evidence_unit_id") or "") not in selected
            ):
                continue
            paper_key = str(unit.get("paper_key") or "")
            for study in unit.get("study_units") or ():
                if not isinstance(study, Mapping):
                    continue
                study_id = (
                    str(study.get("study_id") or "")
                    if str(study.get("source_study_id") or "")
                    else ""
                )
                claims = {
                    str(claim.get("claim_id") or ""): claim
                    for claim in study.get("claims") or ()
                    if isinstance(claim, Mapping)
                }
                for source_field in study.get("interpretation_source_fields") or ():
                    if not isinstance(source_field, Mapping):
                        continue
                    field_id = str(source_field.get("source_field_id") or "")
                    if not field_id:
                        continue
                    scoped = {**dict(source_field), "paper_key": paper_key, "owner_study_id": study_id}
                    if field_id in fields and fields[field_id] != scoped:
                        raise OutlineV3ExecutionError(
                            "interpretation field has conflicting source ownership"
                        )
                    fields[field_id] = scoped
                for dependency in study.get("interpretation_dependencies") or ():
                    if not isinstance(dependency, Mapping):
                        continue
                    primary_id = str(dependency.get("primary_claim_id") or "")
                    primary = claims.get(primary_id)
                    if primary is None:
                        raise OutlineV3ExecutionError(
                            "interpretation dependency lost its primary source claim"
                        )
                    scoped = {
                        **dict(dependency),
                        "paper_key": paper_key,
                        "owner_study_id": study_id,
                        "primary_evidence_ids": [
                            str(value)
                            for value in primary.get("evidence_ids") or ()
                            if str(value)
                        ],
                    }
                    dependencies[hash_json(scoped)] = scoped
        return {
            "fields": [fields[key] for key in sorted(fields)],
            "dependencies": [dependencies[key] for key in sorted(dependencies)],
        }

    @staticmethod
    def _topic_output_for_fragment(
        provider_output: Mapping[str, Any],
        fragment_id: str,
    ) -> dict[str, Any]:
        rows = [
            dict(item)
            for item in provider_output.get("topics") or ()
            if isinstance(item, Mapping)
            and str(item.get("fragment_id") or "") == fragment_id
        ]
        if len(rows) != 1:
            raise OutlineV3ExecutionError(
                f"topic provider output does not contain exactly one result for fragment {fragment_id}"
            )
        claims = [
            dict(item)
            for item in provider_output.get("claims") or ()
            if isinstance(item, Mapping)
            and str(item.get("fragment_id") or "") == fragment_id
        ]
        supported_evidence_ids = {
            str(value)
            for value in rows[0].get("supporting_evidence_ids") or ()
            if str(value)
        }
        supported_evidence_ids.update(
            str(value)
            for claim in claims
            for value in claim.get("evidence_ids") or ()
            if str(value)
        )
        rows[0]["supporting_evidence_ids"] = sorted(supported_evidence_ids)
        return {
            "semantic_contract_version": "semantic-evidence-graph-v2",
            "fragment_id": fragment_id,
            "topic": rows[0],
            "claims": claims,
            "unresolved_questions": [
                item
                for item in provider_output.get("unresolved_questions") or ()
                if isinstance(item, Mapping)
                and str(item.get("fragment_id") or "") == fragment_id
            ],
            "batch_unresolved_questions": [
                item
                for item in provider_output.get("unresolved_questions") or ()
                if isinstance(item, str)
            ],
        }

    def _build_topic_synthesis_payloads(
        self,
        topic_plan: Sequence[TopicSynthesis],
        provider_results: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        """Merge fragments once per topic and bind each batch output once."""

        topic_groups: dict[str, dict[str, Any]] = {}
        for item in topic_plan:
            topic_id = str(item.topic_id)
            fragment_id = str(item.fragment_id or item.topic_id)
            topic_payload = topic_groups.setdefault(topic_id, item.to_dict())
            topic_payload["paper_ids"] = sorted(
                set(topic_payload.get("paper_ids") or ())
                | {str(value) for value in item.paper_ids if str(value)}
            )
            topic_payload["bridge_paper_ids"] = sorted(
                set(topic_payload.get("bridge_paper_ids") or ())
                | {str(value) for value in item.bridge_paper_ids if str(value)}
            )
            topic_payload["supporting_evidence_ids"] = sorted(
                set(topic_payload.get("supporting_evidence_ids") or ())
                | {str(value) for value in item.supporting_evidence_ids if str(value)}
            )
            matching = [
                result
                for result in provider_results
                if fragment_id in result.get("fragment_ids", ())
            ]
            if len(matching) > 1:
                raise OutlineV3ExecutionError(
                    f"semantic topic fragment {fragment_id} has multiple provider result identities"
                )
            fragment_results: list[dict[str, Any]] = []
            for result in matching:
                raw_output = result.get("provider_output")
                if not isinstance(raw_output, Mapping):
                    raise OutlineV3ExecutionError(
                        f"topic result {result.get('batch_id')} is not a mapping"
                    )
                batch_result_id = str(
                    result.get("batch_result_id") or result.get("result_id") or ""
                )
                if not batch_result_id:
                    raise OutlineV3ExecutionError(
                        f"topic result {result.get('batch_id')} is missing its batch result identity"
                    )
                fragment_result_id = (
                    "fragment-result:"
                    + hash_json(
                        {
                            "batch_result_id": batch_result_id,
                            "topic_id": topic_id,
                            "fragment_id": fragment_id,
                        }
                    )[:24]
                )
                fragment_plan = next(
                    (
                        row
                        for row in result.get("topic_fragments") or ()
                        if isinstance(row, Mapping)
                        and str(row.get("fragment_id") or "") == fragment_id
                    ),
                    {},
                )
                fragment_results.append(
                    {
                        "result_id": fragment_result_id,
                        "batch_result_id": batch_result_id,
                        "batch_id": str(result.get("batch_id") or ""),
                        "topic_id": topic_id,
                        "fragment_id": fragment_id,
                        "paper_ids": sorted({str(value) for value in item.paper_ids if str(value)}),
                        "planned_evidence_unit_ids": list(
                            fragment_plan.get("planned_evidence_unit_ids") or []
                        ),
                        "planned_evidence_ids": list(
                            fragment_plan.get("planned_evidence_ids") or []
                        ),
                        **(
                            {"interpretation_context": dict(fragment_plan["interpretation_context"])}
                            if isinstance(fragment_plan.get("interpretation_context"), Mapping)
                            and any((fragment_plan["interpretation_context"].get(key) or ())
                                    for key in ("fields", "dependencies"))
                            else {}
                        ),
                        "provider_output": self._topic_output_for_fragment(
                            raw_output,
                            fragment_id,
                        ),
                    }
                )
            fragments = topic_payload.setdefault("fragments", [])
            if any(str(row.get("fragment_id") or "") == fragment_id for row in fragments):
                raise OutlineV3ExecutionError(
                    f"semantic topic plan contains duplicate fragment identity {fragment_id}"
                )
            fragments.append(
                {
                    "fragment_id": fragment_id,
                    "paper_ids": sorted({str(value) for value in item.paper_ids if str(value)}),
                    "evidence_unit_indexes": item.to_dict().get("evidence_unit_indexes", {}),
                    "evidence_chunk_target_tokens": item.evidence_chunk_target_tokens,
                    "supporting_evidence_ids": sorted(
                        {
                            str(value)
                            for result in fragment_results
                            for value in result.get("planned_evidence_ids") or ()
                            if str(value)
                        }
                    ),
                    "provider_results": fragment_results,
                }
            )

        payloads: list[dict[str, Any]] = []
        for _topic_id, topic_payload in sorted(topic_groups.items()):
            fragments = topic_payload.get("fragments") or []
            fragment_results = [
                dict(result)
                for fragment in fragments
                if isinstance(fragment, Mapping)
                for result in fragment.get("provider_results") or ()
                if isinstance(result, Mapping)
            ]
            provider_batch_ids = sorted(
                {
                    str(result.get("batch_id") or "")
                    for result in fragment_results
                    if str(result.get("batch_id") or "")
                }
            )
            topic_payload.update(
                {
                    "fragment_ids": [
                        str(fragment.get("fragment_id") or "") for fragment in fragments
                    ],
                    "status": "completed_provider" if fragment_results else "completed_local_deterministic",
                    "execution_mode": "provider_synthesis" if fragment_results else "local_evidence_projection",
                    "provider_calls": len(provider_batch_ids),
                    "provider_batch_ids": provider_batch_ids,
                    "provider_output_refs": [
                        {
                            "result_id": str(result.get("result_id") or ""),
                            "batch_result_id": str(result.get("batch_result_id") or ""),
                            "batch_id": str(result.get("batch_id") or ""),
                            "fragment_id": str(result.get("fragment_id") or ""),
                        }
                        for result in fragment_results
                    ],
                    "diagnostics": [] if fragment_results else [
                        "offline/local route retained deterministic projection; no external synthesis call was admitted"
                    ],
                }
            )
            payloads.append(topic_payload)
        return payloads

    @staticmethod
    def _semantic_receipt_node_id(node_id: str) -> str:
        text = str(node_id or "")
        if text.startswith("topic_synthesis_provider"):
            return "topic_synthesis"
        if text.startswith("cross_group_comparison_provider"):
            return "cross_group_comparison"
        if text.startswith("global_synthesis_provider"):
            return "global_synthesis"
        return text

    def _provider_call(
        self,
        node_id: str,
        request: Mapping[str, Any],
        *,
        expect_json: bool = True,
        input_artifact_hashes: Sequence[str] = (),
        transport_node_id: str | None = None,
        output_tokens: int | None = None,
    ) -> dict[str, Any]:
        request = self._attach_prompt_authority(node_id, request)
        if self.outline_pilot is not None and hash_json(request) != self._pilot_request_hashes.get(node_id):
            raise OutlineV3ExecutionError(
                f"topic pilot request {node_id} differs from its admitted exact plan"
            )
        # Critics must return an explicit envelope (passed + blocking
        # diagnostics).  Inject the contract centrally so the main path and
        # every stability variant carry the same schema authority.
        request_base_node = str(transport_node_id or node_id or "")
        if request_base_node.startswith("stability:"):
            request_base_node = request_base_node.rsplit(":", 1)[-1]
        if (
            request_base_node in {"structure_critique", "coverage_critique", "evidence_critique"}
            and not isinstance(request.get("output_contract"), Mapping)
        ):
            output_fields = {
                    "node_id": "string; echo the node_id from this request verbatim",
                    "passed": "boolean; true only if every check in the checks list passes",
                    "blocking_diagnostics": (
                        "legacy explanatory strings only; they do not establish candidate scope"
                    ),
                    "issues": (
                        "array of typed issue objects; each issue has issue_id, scope "
                        "(global/candidate/section/claim), target_ids, severity "
                        "(blocking/non_blocking), evidence_refs, resolution_status, "
                        "parent_candidate_hash, and message. A candidate, section, or "
                        "claim issue must bind to the exact candidate_hashes value. "
                        "A failed result without typed scope blocks globally."
                    ),
                    "score": "number between 0 and 1 summarizing how many checks passed",
                    "recommendations": "array of strings with concrete repair suggestions",
                }
            must_include = ["node_id", "passed", "issues", "blocking_diagnostics"]
            claim_comparisons = request.get("stability_claim_comparisons")
            if (
                isinstance(claim_comparisons, Mapping)
                and any(
                    isinstance(rows, Sequence)
                    and not isinstance(rows, (str, bytes))
                    and bool(rows)
                    for rows in claim_comparisons.values()
                )
            ):
                output_fields["stability_claim_reviews"] = (
                    "array with exactly one row for every requested pair_id; each row has candidate_id, pair_id, "
                    "decision (equivalent, material_change, or uncertain), evidence_refs (non-empty IDs copied "
                    "from that pair's allowed evidence_refs), and a concise rationale. Return equivalent only "
                    "when both statements preserve the same factual meaning, direction, population, conditions, "
                    "and limitations. Missing, duplicate, or extra pair results are invalid."
                )
                must_include.append("stability_claim_reviews")
            request = {**dict(request), "output_contract": {
                "output_fields": output_fields,
                "must_include": must_include,
                "critique_disposition_version": CRITIQUE_DISPOSITION_VERSION,
            }}
        if (
            request_base_node == "arbitration"
            and not isinstance(request.get("output_contract"), Mapping)
        ):
            request = {**dict(request), "output_contract": {
                "output_fields": {
                    "selected_candidate_id": (
                        "string; MUST be one of the candidate_ids provided in this request, "
                        "chosen verbatim (e.g. candidate_1); never invent or renumber it"
                    ),
                    "selection_reasons": "non-empty array of strings explaining the selection",
                    "accepted_recommendations": (
                        "array of typed issue objects or legacy recommendation strings; typed objects use issue_id, "
                        "target_section_id(s), operation, replacement and evidence_ids"
                    ),
                    "rejected_recommendations": "array of typed issue objects or recommendation strings rejected, with why in unresolved_risks",
                    "unresolved_risks": "array of strings; empty when none remain",
                },
                "must_include": ["selected_candidate_id", "selection_reasons"],
            }}
        # The route is the runtime authority for this node. Budget, binding,
        # replay identity, receipt and the actual transport all derive from it;
        # using the executor-level profile here would collapse every node onto
        # the Outline model and defeat the point of role routing.
        route = self._node_route(node_id, transport_node_id)
        if self.outline_pilot is not None:
            if node_id not in self._pilot_allowed_node_ids:
                raise OutlineV3ExecutionError(
                    f"topic pilot refuses out-of-scope provider node {node_id}"
                )
            if route.safe_config_fingerprint() != str(
                self.outline_pilot.get("allowed_route_fingerprint") or ""
            ):
                raise OutlineV3ExecutionError("topic pilot provider route changed")
            if datetime.now(timezone.utc).timestamp() >= self._pilot_deadline_epoch:
                raise OutlineV3ExecutionError("topic pilot deadline expired before transport")
            if str(route.endpoint_type or "").casefold() not in {"internal", "fixture"}:
                acceptance = (
                    current_acceptance_execution_context()
                    or acceptance_execution_context_from_environment()
                )
                if (
                    acceptance is None
                    or not acceptance.owner_authorized
                    or acceptance.acceptance_run_id != str(
                        self.outline_pilot.get("acceptance_run_id") or ""
                    )
                    or provider_budget_controller_from_environment() is None
                ):
                    raise OutlineV3ExecutionError(
                        "topic pilot lost its bound acceptance authority"
                    )
                self._pilot_verify_source_identity(acceptance)
        profile = route.profile
        budget = profile.estimate_request(request)
        configured_cap = int(self.max_source_prompt_tokens or 32000)
        route_cap = int(profile.input_budget or configured_cap)
        effective_cap = max(1, min(32000, configured_cap, route_cap))
        estimated_input = int(budget.get("estimated_input_tokens") or profile.estimate_tokens(request))
        if estimated_input > effective_cap:
            raise OutlineV3ExecutionError(
                f"BLOCKED_BUDGET: {node_id} serialized input estimate {estimated_input} "
                f"exceeds effective input cap {effective_cap}; split by evidence unit before transport"
            )
        api_config = self._route_transport_identity(route)
        # Keep the ordinary node path compatible with callers that decorate
        # ``_provider_binding`` for replay tests or local instrumentation.  A
        # normal node resolves its route from ``node_id`` inside the binding
        # method; only a dynamic transport identity needs the explicit route
        # that cannot be recovered from the physical shard node id.
        binding_kwargs: dict[str, Any] = {
            "expect_json": expect_json,
            "input_artifact_hashes": input_artifact_hashes,
        }
        if transport_node_id is not None:
            binding_kwargs["route"] = route
        binding = self._provider_binding(node_id, request, **binding_kwargs)
        self._dynamic_provider_bindings[node_id] = dict(binding)
        call_id = self._register_expected_from_binding(node_id, binding)
        semantic_node_id = self._semantic_node_id(node_id)
        replay_key = ModelCallReplayKey(
            node_id=self._semantic_receipt_node_id(semantic_node_id),
            node_version="v3",
            schema_version="outline-v3",
            model_route=route.config_section or route.provider_name,
            model_name=route.model,
            provider=route.provider_name,
            prompt_template_hash=self._outline_prompt_identity.sha256,
            prompt_payload_hash=hash_json(request),
            input_artifact_hashes=sorted(str(item) for item in input_artifact_hashes if str(item)),
            config_hash=hash_json(api_config),
            execution_binding_hash=self._replay_binding_hash(binding),
        )
        transport_for_audit = self._resolve_node_transport(node_id, route)
        audit_index = self._begin_request_payload_audit(
            node_id=node_id,
            request=request,
            route=route,
            profile=profile,
            binding=binding,
            api_config=api_config,
            call_id=call_id,
            semantic_node_id=semantic_node_id,
            transport_node_id=transport_node_id,
            replay_key_hash=replay_key.key_hash,
            replay_status="pending",
            transport=transport_for_audit,
            budget=budget,
            effective_input_cap=effective_cap,
            requested_output_tokens=int(output_tokens or profile.max_output_tokens),
        )
        replay_lookup = self._replay_store.lookup(replay_key)
        self._finish_request_payload_audit(
            audit_index,
            replay_status=replay_lookup.status,
        )
        if replay_lookup.reusable and replay_lookup.record is not None:
            for artifact_id in replay_lookup.record.output_artifact_ids:
                replay_record = self.registry.get(artifact_id)
                if replay_record is None or replay_record.status != "ready":
                    continue
                if not self._replay_record_is_valid(replay_lookup.record, binding):
                    continue
                try:
                    self.registry.verify_ready_artifact_closure(replay_record)
                    replay_payload = json.loads(Path(replay_record.path).read_text(encoding="utf-8"))
                except (OSError, UnicodeError, json.JSONDecodeError, RegistryError, TypeError, ValueError):
                    continue
                payload = replay_payload.get("payload") if isinstance(replay_payload, Mapping) else None
                normalized_hash = replay_lookup.record.normalized_output_hash or replay_lookup.record.output_hash
                canonical_candidate_replay = bool(
                    isinstance(payload, Mapping)
                    and (
                        node_id.endswith("_provider_generation")
                        or (
                            node_id.endswith("_semantic_repair")
                            and replay_record.artifact_type == "outline_candidate_repair"
                        )
                    )
                    and (self._alias_enabled or self._repair_enabled)
                    and replay_lookup.record.registered_artifact_hash
                    == str(replay_payload.get("content_hash") or "")
                    and replay_lookup.record.node_output_hash
                    == str(replay_payload.get("content_hash") or "")
                    and replay_record.artifact_id in replay_lookup.record.output_artifact_ids
                )
                if isinstance(payload, Mapping) and (
                    hash_json(payload) == normalized_hash or canonical_candidate_replay
                ):
                    replay_receipts = self._replay_receipt_index()
                    prior_epoch_reused = any(
                        str(getattr(replay_receipts.get(str(receipt_id)), "closure_epoch_id", "") or "")
                        != self.closure_epoch_id
                        for receipt_id in replay_lookup.record.receipt_ids
                        if str(receipt_id) in replay_receipts
                    )
                    if prior_epoch_reused:
                        reuse_evidence = self._materialize_verified_reuse_evidence(
                            node_id,
                            binding,
                            replay_lookup.record,
                            replay_record,
                            payload,
                        )
                        if reuse_evidence is None:
                            # A replay hit with untrusted historical authority
                            # is a miss for execution purposes; fall through
                            # to a real provider transport.
                            continue
                        expected = self._expected_provider_calls[call_id]
                        self._expected_provider_calls[call_id] = replace(
                            expected,
                            provider_response_hash=normalized_hash,
                            output_hash=normalized_hash,
                            normalized_output_hash=normalized_hash,
                            artifact_payload_hash=hash_json(payload),
                            artifact_content_hash=(replay_lookup.record.registered_artifact_hash or replay_record.content_hash),
                            registry_file_hash=replay_record.content_hash,
                            artifact_path=replay_record.path,
                            registered_artifact_hash=replay_lookup.record.registered_artifact_hash or replay_record.content_hash,
                            replay_output_hash=replay_lookup.record.output_hash,
                            node_output_hash=replay_lookup.record.node_output_hash or replay_record.content_hash,
                            verified_reuse=True,
                            reuse_evidence_artifact_id=reuse_evidence.artifact_id,
                            reuse_evidence_artifact_hash=reuse_evidence.content_hash,
                            reuse_evidence_record_hash=self._artifact_record_hash(reuse_evidence),
                        )
                        self._verified_reuse_source_receipt_ids[call_id] = str(
                            replay_lookup.record.receipt_ids[0]
                        )
                    else:
                        # Same-epoch replay is an observed current receipt, not
                        # a verified-reuse exception.
                        for receipt_id in replay_lookup.record.receipt_ids:
                            if str(receipt_id) not in self.receipts:
                                self.receipts.append(str(receipt_id))
                        self._expected_provider_calls[call_id] = replace(
                            self._expected_provider_calls[call_id],
                            provider_response_hash=normalized_hash,
                            output_hash=normalized_hash,
                            normalized_output_hash=normalized_hash,
                            artifact_payload_hash=hash_json(payload),
                            artifact_content_hash=(replay_lookup.record.registered_artifact_hash or replay_record.content_hash),
                            registry_file_hash=replay_record.content_hash,
                            artifact_path=replay_record.path,
                            registered_artifact_hash=replay_lookup.record.registered_artifact_hash or replay_record.content_hash,
                            replay_output_hash=replay_lookup.record.output_hash,
                            node_output_hash=replay_lookup.record.node_output_hash or replay_record.content_hash,
                        )
                    self._replay_evidence.append({
                        "node_id": node_id,
                        "semantic_node_id": semantic_node_id,
                        "closure_epoch_id": self.closure_epoch_id,
                        "key_hash": replay_key.key_hash,
                        "lookup_status": "hit",
                        "provider_invoked": False,
                        "adopted_canonical_replay": canonical_candidate_replay,
                        "verified_reuse": prior_epoch_reused,
                        "reuse_evidence_artifact_id": (
                            self._expected_provider_calls[call_id].reuse_evidence_artifact_id
                            if prior_epoch_reused
                            else ""
                        ),
                        "reused_artifact_ids": list(replay_lookup.record.output_artifact_ids),
                        "reused_receipt_ids": list(replay_lookup.record.receipt_ids),
                        "reused_artifact_id": str(replay_lookup.record.output_artifact_ids[0]) if replay_lookup.record.output_artifact_ids else "",
                        "reused_receipt_id": str(replay_lookup.record.receipt_ids[0]) if replay_lookup.record.receipt_ids else "",
                    })
                    self._finish_request_payload_audit(
                        audit_index,
                        physical_attempt_id=(
                            f"reused:{replay_lookup.record.receipt_ids[0]}"
                            if replay_lookup.record.receipt_ids
                            else "reused"
                        ),
                        provider_invoked=False,
                        status="reused",
                        receipt_ids=list(replay_lookup.record.receipt_ids),
                        artifact_refs=[
                            {
                                "artifact_id": artifact_id,
                                "path": str(replay_record.path),
                                "content_hash": str(replay_record.content_hash),
                            }
                            for artifact_id in replay_lookup.record.output_artifact_ids
                        ],
                    )
                    return dict(payload)
        if replay_lookup.status == "stale":
            self.replay_diagnostics.append(
                f"replay stale for {node_id}: {','.join(replay_lookup.stale_reasons)}"
            )
        # Re-check after replay lookup: replay is local and may be usable while
        # a paused job must still refuse any new provider admission.
        self._pause_state.assert_runnable(node_id=node_id)
        self._materialize_primary_repair_request(node_id, request, route, binding)
        self._assert_primary_repair_fresh_attempt(node_id)
        runtime = ProviderRuntime(
            budget=ProviderBudgetV1(
                max_calls=1,
                max_retries_per_call=max(0, int(api_config.get("transport_retries") or 0)),
            ),
            ledger=self._receipt_ledger,
            job_id=self.job_id,
            attempt_id=call_id,
            stage_name="outline_v3",
            route=semantic_node_id,
            node_id=self._semantic_receipt_node_id(semantic_node_id),
            call_id=call_id,
            closure_epoch_id=self.closure_epoch_id,
            logical_attempt_identity=self.logical_attempt_identity,
            endpoint_type=route.endpoint_type,
            schema_hash=str(binding["schema_hash"]),
            prompt_id=self._outline_prompt_identity.prompt_id,
            prompt_version=self._outline_prompt_identity.version,
            prompt_sha256=self._outline_prompt_identity.sha256,
        )
        if not budget["within_budget"]:
            receipt = runtime.blocked_receipt(prompt=json.dumps(request, sort_keys=True, ensure_ascii=False), input_payload=request, api_config=api_config, message="provider input exceeds verified context budget")
            self.receipts.append(receipt.receipt_id)
            self._finish_request_payload_audit(
                audit_index,
                physical_attempt_id=receipt.receipt_id,
                status="blocked_context_budget",
                receipt_ids=[receipt.receipt_id],
            )
            raise OutlineV3ExecutionError(f"provider budget blocked node {node_id}")
        configured_retries = max(0, int(api_config.get("transport_retries") or 0))
        requested_attempts = configured_retries + 1
        effective_attempts = runtime.max_attempts_for_call(requested_attempts)
        # Reject local and route-level pre-transport conditions before taking
        # a durable aggregate reservation. An unstarted reservation is safe to
        # release, but this path has no reason to create one in the first place.
        if self.max_provider_calls is not None and self._provider_call_count >= self.max_provider_calls:
            self._finish_request_payload_audit(
                audit_index,
                status="blocked_provider_call_budget",
            )
            raise OutlineV3ExecutionError(
                f"outline provider call budget exhausted before {node_id}"
            )
        if node_id.startswith("stability:") and transport_for_audit is None:
            self._finish_request_payload_audit(
                audit_index,
                status="blocked_stability_transport",
            )
            raise OutlineV3ExecutionError(
                "stability audit requires a configured provider; fixture responses are not admissible"
            )
        fixture_response = None
        if transport_for_audit is None:
            # Validate the local fixture before taking a durable budget slot.
            # A malformed/missing fixture is not a provider attempt.
            fixture_response = self._fixture_response(node_id, request)
        admission = runtime.admit(
            estimated_tokens=int(budget["estimated_input_tokens"]),
            # The Outline transport reports usage summed across physical
            # attempts. Reserve that same upper bound before the first POST.
            requested_output_tokens=(
                int(output_tokens or profile.max_output_tokens) * effective_attempts
            ),
            requested_retry_attempts=max(0, effective_attempts - 1),
        )
        self._provider_call_count += 1
        transport = transport_for_audit
        if transport is None:
            raw = fixture_response
        else:
            self._transport_call_count += 1
            runtime.mark_transport_started(admission)
            # Stability calls retain their variant identity in the receipt
            # binding, while the transport receives its logical role for
            # schema dispatch. Other dynamic nodes need their concrete ID so
            # topic/cross/global cannot be mistaken for candidate generation.
            provider_node_id = (
                str(transport_node_id)
                if node_id.startswith("stability:") and transport_node_id
                else str(node_id)
            )
            self._finish_request_payload_audit(
                audit_index,
                physical_attempt_id=f"transport:{call_id}:{self._transport_call_count}",
                provider_invoked=True,
                status="transport_started",
            )
            try:
                call_with_runtime = getattr(transport, "call_with_runtime", None)
                if callable(call_with_runtime):
                    raw = call_with_runtime(
                        provider_node_id,
                        request,
                        runtime=runtime,
                        attempt_limit=effective_attempts,
                        output_tokens=output_tokens,
                    )
                else:
                    raw = (
                        transport(provider_node_id, request)
                        if callable(transport)
                        else transport.call(provider_node_id, request)
                    )
            except Exception as exc:
                self._finish_request_payload_audit(
                    audit_index,
                    status="transport_exception",
                    error=f"{type(exc).__name__}: {exc}",
                )
                raise
        response = _provider_result(raw)
        completion = ProviderCompletionEvaluator.evaluate(response, minimum_output=2, expect_json=expect_json)
        result = dict(response)
        result["status"] = "success" if completion.status == "complete" else "failed"
        if completion.error_kind:
            result["error_kind"] = completion.error_kind
        result["content"] = completion.content
        result["finish_reason"] = completion.finish_reason
        result["incomplete_reason"] = completion.incomplete_reason
        result.update({key: response[key] for key in ("input_tokens", "output_tokens", "total_tokens", "cached_input_tokens", "reasoning_tokens", "usage_status") if key in response})
        receipt = runtime.complete(
            admission=admission,
            prompt=json.dumps(request, sort_keys=True, ensure_ascii=False),
            input_payload=request,
            api_config=api_config,
            result=result,
            metadata={
                "node_id": node_id,
                "semantic_node_id": semantic_node_id,
                "config_section": route.config_section,
                "route_fingerprint": route.safe_config_fingerprint(),
                "closure_epoch_id": self.closure_epoch_id,
                "estimation": budget,
                "replay_status": replay_lookup.status,
                "replay_stale_reasons": list(replay_lookup.stale_reasons),
            },
        )
        self.receipts.append(receipt.receipt_id)
        normalized_hash = hash_json(completion.content) if completion.status == "complete" else ""
        raw_response_refs = []
        raw_path = response.get("raw_response_path")
        raw_hash = response.get("raw_response_sha256")
        raw_bytes = response.get("response_bytes")
        if (
            isinstance(raw_path, str) and raw_path.strip()
            and isinstance(raw_hash, str) and re.fullmatch(r"[0-9a-f]{64}", raw_hash)
            and type(raw_bytes) is int and raw_bytes > 0
        ):
            raw_response_refs.append({
                "path": raw_path,
                "sha256": raw_hash,
                "bytes": raw_bytes,
                "normalized_response_hash": normalized_hash,
            })
        self._finish_request_payload_audit(
            audit_index,
            physical_attempt_id=receipt.receipt_id,
            provider_invoked=transport is not None,
            status=str(receipt.status),
            receipt_ids=[receipt.receipt_id],
            raw_response_refs=raw_response_refs,
        )
        self._expected_provider_calls[call_id] = replace(
            self._expected_provider_calls[call_id],
            provider_response_hash=normalized_hash,
            output_hash=normalized_hash,
            normalized_output_hash=normalized_hash,
        )
        if receipt.status == "success" and receipt.response_hash and normalized_hash:
            if receipt.response_hash != normalized_hash:
                raise OutlineV3ExecutionError(f"provider response hash for {node_id} does not match normalized output")
            self._pending_replays[node_id] = (replay_key, normalized_hash, receipt.receipt_id)
        self._replay_evidence.append({
            "node_id": node_id,
            "semantic_node_id": semantic_node_id,
            "closure_epoch_id": self.closure_epoch_id,
            "key_hash": replay_key.key_hash,
            "lookup_status": "stale" if replay_lookup.status == "stale" else "miss",
            "provider_invoked": True,
            "reused_artifact_ids": [],
            "reused_receipt_ids": [],
            "reused_artifact_id": "",
            "reused_receipt_id": "",
        })
        if completion.status != "complete":
            raise OutlineV3ExecutionError(f"provider output for {node_id} is {completion.status}")
        dynamic_dependencies = {
            f"input_{index}": value
            for index, value in enumerate(input_artifact_hashes)
        }
        is_relation_shard = (
            node_id.startswith("relation_adjudication:")
            or (node_id.startswith("stability:") and ":relation_adjudication:" in node_id)
        )
        is_candidate_shard = (
            node_id.startswith("candidate_")
            and "_provider_generation:local:" in node_id
        ) or (
            node_id.startswith("stability:")
            and "_provider_generation:local:" in node_id
        )
        is_critique_shard = any(
            node_id.startswith(f"{role}:")
            or (node_id.startswith("stability:") and f":{role}:" in node_id)
            for role in ("structure_critique", "coverage_critique", "evidence_critique")
        )
        if is_relation_shard:
            # Hierarchical local/cross-shard calls are provider calls outside
            # the static DAG.  Their outputs still need Registry identity and
            # replay authority so receipt closure cannot leave dynamic calls
            # incomplete or silently repeat them on resume.
            self._persist_relation_shard_output(
                node_id,
                _as_dict(completion.content),
                dependency_hashes=dynamic_dependencies,
                binding=binding,
            )
        elif is_candidate_shard:
            self._persist_candidate_shard_output(
                node_id,
                _as_dict(completion.content),
                dependency_hashes=dynamic_dependencies,
            )
        elif is_critique_shard:
            self._persist_critique_shard_output(
                node_id,
                _as_dict(completion.content),
                dependency_hashes=dynamic_dependencies,
            )
        elif node_id.startswith("stability:"):
            # Stability calls are real provider calls too.  Persist their
            # output immediately so the exact-replay variant can resolve the
            # same ModelCallReplayStore record on its second execution.
            self._persist_stability_output(
                node_id,
                _as_dict(completion.content),
                dependency_hashes=dynamic_dependencies,
                binding=binding,
            )
        return _as_dict(completion.content)

    def _attach_prompt_authority(
        self,
        node_id: str,
        request: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Make the Registry-owned system prompt part of every provider input."""

        enriched = dict(request)
        # The node policy map is an authority input.  A malformed or edited
        # file must stop the outline run instead of silently becoming an empty
        # policy map.
        policies = self.prompt_registry.read_json("outline.node.policies.v3")
        enriched["_prompt_authority"] = {
            "prompt_id": self._outline_prompt_identity.prompt_id,
            "prompt_version": self._outline_prompt_identity.version,
            "prompt_sha256": self._outline_prompt_identity.sha256,
            "system_prompt": self.prompt_registry.read("outline.node.system.v3"),
            "policy_prompt_id": self._outline_policy_identity.prompt_id,
            "policy_prompt_version": self._outline_policy_identity.version,
            "policy_prompt_sha256": self._outline_policy_identity.sha256,
            "node_id": str(node_id),
            "node_policies": policies,
        }
        return enriched

    def _artifact(self, cls: type[OutlineArtifact], payload: Mapping[str, Any], deps: Mapping[str, str] | None = None, diagnostics: Sequence[Mapping[str, Any]] = ()) -> OutlineArtifact:
        return cls(
            job_id=self.job_id,
            dependency_hashes=dict(deps or {}),
            payload=dict(payload),
            blocking_diagnostics=tuple(dict(item) for item in diagnostics),
        )

    def _run_node(
        self,
        node_id: str,
        factory: Callable[[], tuple[OutlineArtifact, Sequence[str], str, str]],
        *,
        expected_binding: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        binding = dict(expected_binding or self.build_current_node_binding(node_id))
        if node_id in self._provider_node_ids():
            self._register_expected_from_binding(node_id, binding)
        loaded = self._load_node(node_id, binding)
        if loaded is not None:
            return loaded
        try:
            self._check(node_id)
            artifact, dependencies, model, provider = factory()
            persisted = self._persist(
                node_id,
                artifact,
                depends_on=dependencies,
                model=model,
                provider=provider,
                execution_binding=binding,
            )
            return persisted
        except Exception as exc:
            # A provider crash or a rejected critic result must remain visible in
            # the durable DAG.  Resume/retry then reruns this node and its
            # descendants instead of treating an exception as an anonymous
            # stage-level failure.
            try:
                self._dag = self._node_store.record_node(
                    node_id,
                    status="blocked" if isinstance(exc, PauseRequestedError) else "failed",
                    input_hash=_hash_payload(dict(binding.get("dependency_hashes") or {})),
                    output_hash="",
                    output_artifact_ids=(),
                    model_route=str(binding.get("provider_route") or ""),
                    model_name=str(binding.get("model_name") or ""),
                    provider=str(binding.get("provider_family") or ""),
                    config_snapshot={"candidate_count": self.candidate_count},
                    budget_snapshot={
                        "input_budget": (
                            self._node_route(node_id).profile.input_budget
                            if node_id in self._provider_node_ids() or node_id.startswith("stability:")
                            else self.profile.input_budget
                        )
                    },
                    receipt_ids=tuple(self.receipts),
                    diagnostics=(f"{type(exc).__name__}: {exc}",),
                    execution_binding=binding,
                )
            except Exception as record_error:
                self.diagnostics.append(
                    f"failed node {node_id} could not be persisted: {type(record_error).__name__}: {record_error}"
                )
            raise

    def _run_provider_node(self, node_id: str, request: Mapping[str, Any], cls: type[OutlineArtifact], deps: Mapping[str, str], *, minimum_output: int = 2) -> tuple[OutlineArtifact, Sequence[str], str, str]:
        route = self._node_route(node_id)
        bounded_output = int(route.profile.max_output_tokens)
        if node_id in {
            "structure_critique",
            "coverage_critique",
            "evidence_critique",
            "arbitration",
        }:
            bounded_output = min(bounded_output, 2_048)
        content = self._provider_call(
            node_id,
            request,
            expect_json=True,
            input_artifact_hashes=tuple(deps.values()),
            output_tokens=bounded_output,
        )
        # The provider receipt has already been appended when this hook runs.
        # Recovery tests use it to model a worker failure after transport
        # success but before the node output is persisted.
        self._check(node_id, phase="provider_success")
        return self._artifact(cls, content, deps), tuple(deps), route.model, route.provider_name

    @staticmethod
    def _compact_relation_digest(
        request: Mapping[str, Any],
        relation_ids: Sequence[str],
        confirmed_ids: Sequence[str],
        rejected_ids: Sequence[str],
    ) -> dict[str, Any]:
        """Create a bounded audit digest; provider requests use full source chunks."""

        views = [
            item for item in request.get("evidence_views") or ()
            if isinstance(item, Mapping)
        ]
        paper_keys = sorted({
            str(value)
            for item in views
            for value in (
                item.get("paper_keys") or [item.get("paper_key") or item.get("canonical_paper_key")]
            )
            if str(value)
        })
        view_hashes = sorted({
            str(item.get("evidence_source_view_hash") or item.get("view_hash") or item.get("source_summary_hash") or "")
            for item in views
            if str(item.get("evidence_source_view_hash") or item.get("view_hash") or item.get("source_summary_hash") or "")
        })
        fields = {
            "research_questions": "research_questions",
            "theories": "theories",
            "constructs": "constructs",
            "mechanisms": "mechanisms",
            "method": "methods",
            "sample_or_context": "contexts",
            "findings": "findings",
            "conclusions": "conclusions",
            "limitations": "limitations",
            "research_gaps": "gaps",
            "future_directions": "future_directions",
        }
        digest: dict[str, Any] = {
            "digest_type": "outline_relation_compact_digest/v1",
            "paper_keys": paper_keys,
            "evidence_view_hashes": view_hashes,
            "relation_candidate_ids": sorted(str(item) for item in relation_ids if str(item)),
            "confirmed_relation_ids": sorted(str(item) for item in confirmed_ids if str(item)),
            "rejected_relation_ids": sorted(str(item) for item in rejected_ids if str(item)),
            "field_counts": {},
            "omitted_value_counts": {},
        }
        for source_field, digest_field in fields.items():
            values: list[str] = []
            total = 0
            for view in views:
                raw = view.get(source_field) or []
                raw_values = raw if isinstance(raw, list) else [raw]
                total += len(raw_values)
                values.extend(str(value) for value in raw_values if str(value).strip())
            unique_values = list(dict.fromkeys(values))
            digest[digest_field] = [value[:600] for value in unique_values[:8]]
            digest["field_counts"][digest_field] = total
            digest["omitted_value_counts"][digest_field] = max(0, len(unique_values) - 8)
        digest["unresolved_items"] = [
            "audit digest is abbreviated; cross-shard provider requests carry complete source chunks and relation bundles"
        ]
        return digest

    def _relation_local_requests(
        self,
        *,
        evidence_views: Sequence[Any],
        candidate_by_id: Mapping[str, Mapping[str, Any]],
        shard_plan: Mapping[str, Any],
        relation_contract: Mapping[str, Any],
        relation_bundles: Mapping[str, Mapping[str, Any]] | None = None,
        node_prefix: str = "",
    ) -> list[tuple[str, str, list[str], set[str], dict[str, Any]]]:
        """Build complete local requests once for preflight and execution."""

        view_by_key = {
            str(getattr(view, "paper_key", "")): view
            for view in evidence_views
            if str(getattr(view, "paper_key", ""))
        }
        requests: list[tuple[str, str, list[str], set[str], dict[str, Any]]] = []
        for shard in shard_plan.get("shards") or ():
            if not isinstance(shard, Mapping):
                continue
            shard_id = str(shard.get("shard_id") or "").strip()
            paper_keys = [str(item) for item in shard.get("paper_keys") or () if str(item)]
            local_ids = {
                str(item)
                for item in shard.get("relation_candidate_ids") or ()
                if str(item) in candidate_by_id
            }
            if not shard_id or not local_ids:
                continue
            local_views = [view_by_key[key] for key in paper_keys if key in view_by_key]
            local_chunks = [
                dict(item) for item in shard.get("evidence_chunks") or ()
                if isinstance(item, Mapping)
            ]
            local_contract = dict(relation_contract)
            local_contract["allowed_relation_ids"] = sorted(local_ids)
            request = {
                "hierarchy": {
                    "level": "local_shard",
                    "shard_id": shard_id,
                    "target_tokens": self.technical_shard_target_tokens,
                    "paper_keys": paper_keys,
                    "relation_candidate_ids": sorted(local_ids),
                    "evidence_view_hashes": list(shard.get("view_hashes") or ()),
                },
                "relation_candidates": [candidate_by_id[item] for item in sorted(local_ids)],
                "relation_evidence_bundles": [
                    dict(relation_bundles[item])
                    for item in sorted(local_ids)
                    if relation_bundles is not None and item in relation_bundles
                ],
                "evidence_views": local_chunks or self._prompt_evidence_views(local_views),
                "relation_adjudication_contract": local_contract,
            }
            node_id = f"relation_adjudication:local:{shard_id}"
            if node_prefix:
                node_id = f"{node_prefix}:{node_id}"
            requests.append((node_id, shard_id, paper_keys, local_ids, request))
        return requests

    def _relation_cross_batch_requests(
        self,
        *,
        candidate_by_id: Mapping[str, Mapping[str, Any]],
        relation_ids: Sequence[str],
        shard_plan: Mapping[str, Any],
        relation_contract: Mapping[str, Any],
        profile: ProviderContextProfile,
        relation_bundles: Mapping[str, Mapping[str, Any]] | None = None,
        node_prefix: str = "",
    ) -> list[tuple[str, dict[str, Any], set[str]]]:
        """Partition lossless cross-shard evidence by the actual request budget."""

        ordered_ids = list(dict.fromkeys(
            str(item) for item in relation_ids if str(item) in candidate_by_id
        ))
        if not ordered_ids:
            return []
        effective_cap = self._effective_input_cap(profile)
        target = self._relation_packing_target(profile)
        all_chunks: list[dict[str, Any]] = []
        seen_chunks: set[str] = set()
        for shard in shard_plan.get("shards") or ():
            if not isinstance(shard, Mapping):
                continue
            for item in shard.get("evidence_chunks") or ():
                if not isinstance(item, Mapping):
                    continue
                chunk_id = str(item.get("evidence_chunk_id") or "")
                if chunk_id not in seen_chunks:
                    all_chunks.append(dict(item))
                    seen_chunks.add(chunk_id)

        def build_request(batch_ids: Sequence[str]) -> dict[str, Any]:
            batch_keys = sorted({
                str(key)
                for relation_id in batch_ids
                for key in candidate_by_id[relation_id].get("paper_keys") or ()
                if str(key)
            })
            batch_key_set = set(batch_keys)
            chunks = []
            for item in all_chunks:
                item_keys = set(str(value) for value in item.get("paper_keys") or () if str(value))
                item_key = str(item.get("paper_key") or "")
                if item_key in batch_key_set or item_keys.intersection(batch_key_set):
                    chunks.append(dict(item))
            batch_contract = dict(relation_contract)
            batch_contract["allowed_relation_ids"] = sorted(batch_ids)
            shard_id = (
                "cross_shard_review"
                if not node_prefix
                else f"cross_shard_review_{len(batch_ids)}"
            )
            return {
                "hierarchy": {
                    "level": "cross_shard",
                    "shard_id": shard_id,
                    "target_tokens": self.technical_shard_target_tokens,
                    "paper_keys": batch_keys,
                    "relation_candidate_ids": sorted(batch_ids),
                    "evidence_view_hashes": list(dict.fromkeys(
                        str(item.get("evidence_source_view_hash") or item.get("view_hash") or "")
                        for item in chunks
                        if str(item.get("evidence_source_view_hash") or item.get("view_hash") or "")
                    )),
                },
                "relation_candidates": [candidate_by_id[item] for item in sorted(batch_ids)],
                "relation_evidence_bundles": [
                    dict(relation_bundles[item])
                    for item in sorted(batch_ids)
                    if relation_bundles is not None and item in relation_bundles
                ],
                "evidence_views": chunks,
                "relation_adjudication_contract": batch_contract,
            }

        batches: list[tuple[str, dict[str, Any], set[str]]] = []
        current: list[str] = []
        for relation_id in ordered_ids:
            trial = [*current, relation_id]
            trial_request = build_request(trial)
            estimate_request = self._attach_prompt_authority(
                f"{node_prefix + ':' if node_prefix else ''}relation_adjudication:preflight:cross_shard",
                trial_request,
            )
            estimated = int(profile.estimate_request(estimate_request).get("estimated_input_tokens") or 0)
            if not current and estimated > effective_cap:
                raise OutlineV3ExecutionError(
                    f"BLOCKED_BUDGET: relation {relation_id} is an indivisible "
                    f"complete relation request ({estimated}/{effective_cap} input tokens)"
                )
            if current and estimated > target:
                batch_index = len(batches) + 1
                batch_ids = set(current)
                node_id = "relation_adjudication:cross_shard"
                if len(ordered_ids) > len(current):
                    node_id = f"relation_adjudication:cross_shard_batch_{batch_index}"
                if node_prefix:
                    node_id = f"{node_prefix}:{node_id}"
                batches.append((node_id, build_request(current), batch_ids))
                current = [relation_id]
            else:
                current = trial
        if current:
            batch_index = len(batches) + 1
            node_id = "relation_adjudication:cross_shard"
            if len(batches) > 0:
                node_id = f"relation_adjudication:cross_shard_batch_{batch_index}"
            if node_prefix:
                node_id = f"{node_prefix}:{node_id}"
            batches.append((node_id, build_request(current), set(current)))
        for node_id, request, batch_ids in batches:
            exact = self._attach_prompt_authority(node_id, request)
            estimate = int(profile.estimate_request(exact).get("estimated_input_tokens") or 0)
            if estimate > effective_cap:
                raise OutlineV3ExecutionError(
                    f"BLOCKED_BUDGET: relation batch {sorted(batch_ids)} is an indivisible "
                    f"complete relation request ({estimate}/{effective_cap} input tokens)"
                )
        return batches

    def _run_hierarchical_relation_adjudication(
        self,
        *,
        evidence_views: Sequence[Any],
        relation_candidates: Sequence[Mapping[str, Any]],
        shard_plan: Mapping[str, Any],
        relation_contract: Mapping[str, Any],
        relation_dependencies: Mapping[str, str],
        relation_bundles: Mapping[str, Mapping[str, Any]] | None = None,
        node_prefix: str = "",
        compact_request: Mapping[str, Any] | None = None,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Adjudicate relations in bounded local shards plus cross-shard batches."""

        candidate_by_id = {
            str(item.get("relation_id") or ""): dict(item)
            for item in relation_candidates
            if isinstance(item, Mapping) and str(item.get("relation_id") or "")
        }
        view_by_key = {
            str(getattr(view, "paper_key", "")): view
            for view in evidence_views
            if str(getattr(view, "paper_key", ""))
        }
        profile = self._role_route("relation_adjudication").profile
        if compact_request is not None:
            local_requests = []
            local_relation_ids: set[str] = set()
            relation_batch_requests = self._relation_compact_batch_requests(
                base_request=compact_request,
                relation_ids=list(candidate_by_id),
                profile=profile,
                node_prefix=node_prefix,
            )
        else:
            local_requests = self._relation_local_requests(
                evidence_views=evidence_views,
                candidate_by_id=candidate_by_id,
                shard_plan=shard_plan,
                relation_contract=relation_contract,
                relation_bundles=relation_bundles,
                node_prefix=node_prefix,
            )
            local_relation_ids = set()
            for row in local_requests:
                local_relation_ids.update(row[3])
            relation_batch_requests = self._relation_cross_batch_requests(
                candidate_by_id=candidate_by_id,
                relation_ids=sorted(set(candidate_by_id) - local_relation_ids),
                shard_plan=shard_plan,
                relation_contract=relation_contract,
                profile=profile,
                relation_bundles=relation_bundles,
                node_prefix=node_prefix,
            )
        effective_cap = self._effective_input_cap(profile)
        if (
            self.max_provider_calls is not None
            and self._provider_call_count + len(local_requests) + len(relation_batch_requests)
            > self.max_provider_calls
        ):
            raise OutlineV3ExecutionError("outline provider call budget exhausted before relation adjudication")
        requests_to_check = [(row[0], row[4]) for row in local_requests]
        requests_to_check.extend((row[0], row[1]) for row in relation_batch_requests)
        for node_id, planned_request in requests_to_check:
            enriched = self._attach_prompt_authority(node_id, planned_request)
            estimated = int(profile.estimate_request(enriched).get("estimated_input_tokens") or 0)
            if estimated > effective_cap:
                raise OutlineV3ExecutionError(
                    f"BLOCKED_BUDGET: {node_id} complete relation request "
                    f"estimate {estimated} exceeds effective input cap {effective_cap}"
                )
        confirmed: set[str] = set()
        rejected: dict[str, dict[str, Any]] = {}
        decisions: dict[str, dict[str, Any]] = {}
        reviewed: set[str] = set()
        digests: list[dict[str, Any]] = []

        def classify(
            content: Mapping[str, Any],
            allowed_ids: set[str],
            *,
            node_id: str,
            level: str,
            shard_id: str,
            paper_keys: Sequence[str],
            request: Mapping[str, Any],
        ) -> None:
            confirmed_ids = [
                str(item).strip()
                for item in content.get("confirmed_relation_ids") or ()
                if str(item).strip()
            ]
            rejected_items = [
                item for item in content.get("rejected_relations") or ()
                if isinstance(item, Mapping)
            ]
            rejected_ids = [str(item.get("relation_id") or "").strip() for item in rejected_items]
            if any(item not in allowed_ids for item in (*confirmed_ids, *rejected_ids)):
                raise OutlineV3ExecutionError(f"{node_id} returned an unknown relation id")
            if set(confirmed_ids) & set(rejected_ids):
                raise OutlineV3ExecutionError(f"{node_id} both confirmed and rejected a relation")
            if set(confirmed_ids) | set(rejected_ids) != allowed_ids:
                raise OutlineV3ExecutionError(f"{node_id} did not classify every local relation")
            confirmed.update(confirmed_ids)
            reviewed.update(allowed_ids)
            for item in rejected_items:
                relation_id = str(item.get("relation_id") or "").strip()
                decision = str(item.get("decision") or item.get("status") or "rejected").strip().lower()
                if decision not in {"rejected", "insufficient_evidence", "not_comparable", "deferred"}:
                    decision = "rejected"
                record = {
                    "relation_id": relation_id,
                    "reason": str(item.get("reason") or f"rejected by {level} relation review"),
                    "decision": decision,
                    "status": decision,
                    "evidence_ids": [str(value) for value in item.get("evidence_ids") or () if str(value)],
                    "missing_evidence_ids": [str(value) for value in item.get("missing_evidence_ids") or () if str(value)],
                }
                rejected[relation_id] = record
                decisions[relation_id] = record
            for relation_id in confirmed_ids:
                decisions[relation_id] = {
                    "relation_id": relation_id,
                    "decision": "confirmed",
                    "status": "confirmed",
                    "reason": "confirmed by evidence adjudication",
                    "evidence_ids": list(candidate_by_id.get(relation_id, {}).get("evidence_ids") or ()),
                    "missing_evidence_ids": [],
                }
            request_views = [
                item for item in request.get("evidence_views") or ()
                if isinstance(item, Mapping)
            ]
            request_view_hashes = list(dict.fromkeys(
                str(item.get("evidence_source_view_hash") or item.get("view_hash") or "")
                for item in request_views
                if str(item.get("evidence_source_view_hash") or item.get("view_hash") or "")
            ))
            compact_digest = self._compact_relation_digest(
                request,
                sorted(allowed_ids),
                confirmed_ids,
                rejected_ids,
            )
            digests.append(
                {
                    "level": level,
                    "shard_id": shard_id,
                    "node_id": node_id,
                    "paper_keys": list(paper_keys),
                    "relation_candidate_ids": sorted(allowed_ids),
                    "confirmed_relation_ids": confirmed_ids,
                    "rejected_relation_ids": rejected_ids,
                    "evidence_view_hashes": request_view_hashes or [
                        str(getattr(view_by_key[key], "view_hash", ""))
                        for key in paper_keys
                        if key in view_by_key
                    ],
                    "compact_digest": compact_digest,
                    "request_payload_audit": {
                        "serialized_bytes": len(
                            json.dumps(request, ensure_ascii=False, sort_keys=True).encode("utf-8")
                        ),
                        "estimated_input_tokens": int(
                            self._role_route("relation_adjudication").profile.estimate_tokens(request)
                        ),
                        "input_budget": self._role_route("relation_adjudication").profile.input_budget,
                        "target_tokens": self.technical_shard_target_tokens,
                        "request_hash": hash_json(request),
                        "parent_node": "relation_shard_plan",
                        "dependency_hashes": dict(relation_dependencies),
                    },
                }
            )

        for node_id, shard_id, paper_keys, local_ids, request in local_requests:
            request = self._attach_prompt_authority(node_id, request)
            content = self._provider_call(
                node_id,
                request,
                expect_json=True,
                input_artifact_hashes=(
                    *relation_dependencies.values(),
                    hash_json({"shard_id": shard_id, "relation_ids": sorted(local_ids)}),
                ),
                transport_node_id="relation_adjudication",
            )
            classify(
                content,
                local_ids,
                node_id=node_id,
                level="local_shard",
                shard_id=shard_id,
                paper_keys=paper_keys,
                request=request,
            )

        remaining_ids = set(candidate_by_id) - reviewed
        if remaining_ids:
            if remaining_ids != set(candidate_by_id) - local_relation_ids:
                raise OutlineV3ExecutionError("relation shard classification changed planned cross-shard scope")
            for node_id, request, batch_ids in relation_batch_requests:
                request = self._attach_prompt_authority(node_id, request)
                content = self._provider_call(
                    node_id,
                    request,
                    expect_json=True,
                    input_artifact_hashes=(
                        *relation_dependencies.values(),
                        hash_json({"level": "cross_shard", "relation_ids": sorted(batch_ids)}),
                    ),
                    transport_node_id="relation_adjudication",
                )
                paper_keys = sorted({
                    str(key)
                    for relation_id in batch_ids
                    for key in candidate_by_id[relation_id].get("paper_keys") or ()
                    if str(key)
                })
                classify(
                    content,
                    batch_ids,
                    node_id=node_id,
                    level="relation_atomic" if compact_request is not None else "cross_shard",
                    shard_id=str((request.get("hierarchy") or {}).get("shard_id") or "cross_shard_review"),
                    paper_keys=paper_keys,
                    request=request,
                )

        if reviewed != set(candidate_by_id):
            raise OutlineV3ExecutionError("hierarchical relation adjudication left candidates unreviewed")
        return {
            "confirmed_relation_ids": [item for item in candidate_by_id if item in confirmed],
            "rejected_relations": [rejected[item] for item in candidate_by_id if item in rejected],
            "relation_decisions": [decisions[item] for item in candidate_by_id if item in decisions],
            "hierarchy": "relation_atomic_batches" if compact_request is not None else "local_shards_then_cross_shard",
        }, digests

    def _validate_candidate_payload(
        self,
        candidate_id: str,
        payload: Mapping[str, Any],
        *,
        allowed_paper_keys: Sequence[str],
        allowed_relation_ids: Sequence[str],
        alias_map: Mapping[str, Any] | None = None,
    ) -> None:
        scope = self._candidate_output_scope
        if scope is not None:
            from outline.candidate_output_scope import CandidateOutputScopeError

            try:
                if payload.get("candidate_id") not in (None, candidate_id):
                    raise CandidateOutputScopeError("candidate identity differs from its request")
                scope.for_papers(allowed_paper_keys).validate(payload)
            except CandidateOutputScopeError as exc:
                raise OutlineV3ExecutionError(f"{candidate_id} finite output contract: {exc}") from exc
        sections = payload.get("sections")
        if not isinstance(sections, list) or not sections:
            raise OutlineV3ExecutionError(f"{candidate_id} provider output has no sections")
        allowed_papers = {str(item) for item in allowed_paper_keys}
        allowed_relations = {str(item) for item in allowed_relation_ids}
        from outline.evidence_alias import build_alias_map

        # The opaque aliases are a job-global identity map.  Rebuilding an
        # alias map for each shard would reindex P003 as P001 and make a
        # valid cross-shard reference indistinguishable from a different
        # paper.  A caller that has the frozen map must pass it through.
        paper_alias_map = dict(alias_map or build_alias_map(list(allowed_paper_keys), list(allowed_relation_ids)))
        paper_alias_reverse = dict(paper_alias_map.get("papers_reverse") or {})
        tables = getattr(self, "_candidate_interpretation_tables", {})
        table_fields = {
            str(field.get("source_field_id") or ""): dict(field)
            for field in (tables.get("source_fields") or ())
            if isinstance(field, Mapping) and str(field.get("source_field_id") or "")
        } if isinstance(tables, Mapping) else {}
        explicit_dependencies: list[tuple[dict[str, Any], str]] = []
        if isinstance(tables, Mapping):
            for raw_dependency in tables.get("dependencies") or ():
                if not isinstance(raw_dependency, Mapping):
                    continue
                dependency = dict(raw_dependency)
                if str(dependency.get("scope") or "") != "explicit_study":
                    continue
                owner_study = str(dependency.get("owner_study_id") or dependency.get("study_id") or "")
                aliases = {
                    str(table_fields.get(str(field_id), {}).get("study_id") or "").upper()
                    for field_id in dependency.get("required_source_field_ids") or ()
                }
                aliases.discard("")
                owner_tail = owner_study.rsplit(":", 1)[-1].upper()
                aliases.add(owner_tail.removeprefix("SOURCE-"))
                for alias in aliases:
                    explicit_dependencies.append((dependency, alias))
        seen_sections: set[str] = set()
        for section in sections:
            if not isinstance(section, Mapping):
                raise OutlineV3ExecutionError(f"{candidate_id} provider output contains an invalid section")
            section_id = str(section.get("section_id") or "").strip()
            if not section_id or section_id in seen_sections:
                raise OutlineV3ExecutionError(f"{candidate_id} provider output has duplicate or missing section ids")
            seen_sections.add(section_id)
            support_by_claim_id: dict[str, dict[str, Any]] = {}
            for raw_support in section.get("claim_support") or ():
                if not isinstance(raw_support, Mapping):
                    continue
                claim_id = str(raw_support.get("claim_id") or "").strip()
                if not claim_id:
                    continue
                support_row = dict(raw_support)
                prior_support = support_by_claim_id.get(claim_id)
                if prior_support is not None and prior_support != support_row:
                    raise OutlineV3ExecutionError(
                        f"{candidate_id} section {section_id} has conflicting claim_id {claim_id} support provenance"
                    )
                support_by_claim_id[claim_id] = support_row
            paper_keys = {str(item) for item in section.get("paper_keys") or ()}
            if not paper_keys or not paper_keys.issubset(allowed_papers):
                raise OutlineV3ExecutionError(f"{candidate_id} provider output has paper keys outside its evidence contract")
            relation_ids = {str(item) for item in section.get("relation_ids") or ()}
            if not relation_ids.issubset(allowed_relations):
                raise OutlineV3ExecutionError(f"{candidate_id} provider output has relation ids outside the global relation map")
            claims = [str(item).strip() for item in section.get("claims") or () if str(item).strip()]
            if not claims:
                raise OutlineV3ExecutionError(f"{candidate_id} provider output contains a section without planned claims")
            for claim in claims:
                aliases = {
                    str(match.group(0)).upper()
                    for match in re.finditer(r"\bP\d{3}\b", claim, flags=re.IGNORECASE)
                }
                referenced_papers = {paper_alias_reverse.get(alias, alias) for alias in aliases}
                if not referenced_papers.issubset(paper_keys):
                    raise OutlineV3ExecutionError(
                        f"{candidate_id} claim references paper aliases outside its section evidence: "
                        f"{sorted(referenced_papers - paper_keys)}"
                    )
                support_rows = [
                    row for row in section.get("claim_support") or ()
                    if isinstance(row, Mapping) and str(row.get("claim") or "").strip() == claim
                ]
                if any(str(row.get("paper_key") or "") not in paper_keys for row in support_rows):
                    raise OutlineV3ExecutionError(
                        f"{candidate_id} claim support references a paper outside its section"
                    )
                explicit_pairs = {
                    (paper_alias_reverse.get(match.group(1).upper(), match.group(1).upper()), match.group(2).upper())
                    for match in re.finditer(
                        r"\b(P\d{3})\s+(?:study|experiment|trial|研究|实验)\s*([A-Za-z]?[1-9]\d?)\b",
                        claim,
                        flags=re.IGNORECASE,
                    )
                }
                study_mentions = {
                    match.group(1).upper()
                    for match in re.finditer(
                        r"(?:\b(?:study|experiment|trial)|研究|实验)\s*([A-Za-z]?[1-9]\d?)\b",
                        claim,
                        flags=re.IGNORECASE,
                    )
                }
                for mentioned_study in sorted(study_mentions):
                    candidates_for_study = [
                        dependency for dependency, alias in explicit_dependencies
                        if alias == mentioned_study
                        and str(dependency.get("paper_key") or "") in paper_keys
                    ]
                    paired_papers = {
                        paper for paper, study in explicit_pairs if study == mentioned_study
                    }
                    typed_papers = {
                        str(row.get("paper_key") or "") for row in support_rows
                        if any(
                            str(dependency.get("paper_key") or "") == str(row.get("paper_key") or "")
                            and str(dependency.get("owner_study_id") or dependency.get("study_id") or "")
                            == str(row.get("study_id") or "")
                            for dependency in candidates_for_study
                        )
                    }
                    if paired_papers:
                        scoped_papers = paired_papers
                    elif referenced_papers and len(referenced_papers) == 1:
                        scoped_papers = referenced_papers
                    elif typed_papers:
                        scoped_papers = typed_papers
                    elif len(candidates_for_study) == 1:
                        scoped_papers = {str(candidates_for_study[0].get("paper_key") or "")}
                    else:
                        raise OutlineV3ExecutionError(
                            f"{candidate_id} claim names study {mentioned_study} with ambiguous paper attribution"
                        )
                    scoped_dependencies = [
                        dependency for dependency in candidates_for_study
                        if str(dependency.get("paper_key") or "") in scoped_papers
                    ]
                    if not scoped_dependencies:
                        raise OutlineV3ExecutionError(
                            f"{candidate_id} claim names study {mentioned_study} without scoped source evidence"
                        )
                    for dependency in scoped_dependencies:
                        paper_id = str(dependency.get("paper_key") or "")
                        owner_study = str(
                            dependency.get("owner_study_id")
                            or dependency.get("study_id") or ""
                        )
                        matched = False
                        for row in support_rows:
                            if (
                                str(row.get("paper_key") or "") != paper_id
                                or str(row.get("study_id") or "") != owner_study
                            ):
                                continue
                            cited_claims = {
                                str(value) for value in row.get("source_claim_ids") or ()
                                if str(value)
                            }
                            cited_evidence = {
                                str(value) for value in row.get("evidence_ids") or ()
                                if str(value)
                            }
                            cited_fields = {
                                str(value) for value in row.get("source_field_ids") or ()
                                if str(value)
                            }
                            required_claims = {
                                str(dependency.get("primary_claim_id") or ""),
                                *(
                                    str(value)
                                    for value in dependency.get("required_source_claim_ids") or ()
                                    if str(value)
                                ),
                            }
                            required_evidence = {
                                str(value)
                                for value in (
                                    *list(dependency.get("primary_evidence_ids") or ()),
                                    *list(dependency.get("required_evidence_ids") or ()),
                                )
                                if str(value)
                            }
                            required_fields = {
                                str(value)
                                for value in dependency.get("required_source_field_ids") or ()
                                if str(value)
                            }
                            if not (
                                required_claims.issubset(cited_claims)
                                and required_evidence.issubset(cited_evidence)
                                and required_fields.issubset(cited_fields)
                                and all(
                                    field_id in table_fields
                                    and str(table_fields[field_id].get("paper_key") or "") == paper_id
                                    and str(table_fields[field_id].get("owner_study_id") or "") == owner_study
                                    for field_id in cited_fields
                                )
                            ):
                                continue
                            matched = True
                            break
                        if not matched:
                            raise OutlineV3ExecutionError(
                                f"{candidate_id} claim names study {mentioned_study} without complete scoped interpretation support"
                            )
                if (
                    len(paper_keys) > 1
                    and not aliases
                    and re.search(
                        r"(作者自陈|局限|缺口|不足).*(共同|一致|四篇|三类|领域共识)|(共同指向|共同.*缺口|领域共识)",
                        claim,
                    )
                ):
                    raise OutlineV3ExecutionError(
                        f"{candidate_id} claim makes an unsupported cross-paper limitation aggregation"
                    )

    @property
    def _alias_enabled(self) -> bool:
        return self.opaque_alias_enabled

    @property
    def _repair_enabled(self) -> bool:
        return self.semantic_repair_enabled

    def _alias_map_for(
        self,
        paper_keys: Sequence[str],
        relation_ids: Sequence[str],
    ) -> dict[str, Any]:
        from outline.evidence_alias import build_alias_map

        alias_map = build_alias_map(paper_keys, relation_ids)
        alias_digest = hashlib.sha256(
            json.dumps(alias_map, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()[:24]
        target = Path(self.receipt_ledger_target_path).parent / f"outline_evidence_alias_map__{alias_digest}.json"
        payload_bytes = json.dumps(alias_map, ensure_ascii=False).encode("utf-8")
        if not target.is_file():
            publish_bytes_artifact(
                self.publication_context,
                self.registry,
                target,
                payload_bytes,
                artifact_role="outline_evidence",
                artifact_type="outline_evidence_alias_map",
                artifact_version="v1",
                producer="outline.v3_executor.OutlineV3Executor",
            )
        self._alias_map = alias_map
        return alias_map

    def _semantic_repair_candidate(
        self,
        candidate_id: str,
        content: Mapping[str, Any],
        error: Exception,
        *,
        allowed_paper_keys: Sequence[str],
        allowed_relation_ids: Sequence[str],
        alias_map: Mapping[str, Any] | None,
        node_prefix: str = "",
    ) -> dict[str, Any]:
        """Bounded single-pass semantic repair for structural contract errors.

        Allowed exactly once per candidate per attempt.  The repair request
        carries only the original output, the validation error, the exact
        allowed ID sets and the output schema -- never the full Stage1
        summaries.  After repair the full validator runs again; a second
        failure publishes outline_candidate_repair_failure/v1 and raises
        (fail-closed, no third provider attempt from this call path).
        """
        from outline.candidate_repair_plan import (
            SEMANTIC_REPAIR_OUTPUT_SCHEMA_V1, SEMANTIC_REPAIR_RULES_V1,
        )

        from outline.evidence_alias import (
            alias_for_paper,
            alias_for_relation,
            alias_structural,
            canonicalize_structural,
        )

        repair_node_id = (
            f"{node_prefix}:{candidate_id}_semantic_repair"
            if node_prefix else f"{candidate_id}_semantic_repair"
        )
        original_sections = [
            section
            for section in (content.get("sections") or [])
            if isinstance(section, Mapping)
        ]
        allowed_papers_view = (
            [alias_for_paper(alias_map, key) for key in allowed_paper_keys]
            if alias_map is not None
            else list(allowed_paper_keys)
        )
        allowed_relations_view = (
            [alias_for_relation(alias_map, rid) for rid in allowed_relation_ids]
            if alias_map is not None
            else list(allowed_relation_ids)
        )
        original_view = (
            alias_structural({"sections": original_sections}, alias_map)
            if alias_map is not None
            else {"sections": original_sections}
        )
        repair_request = {
            "task": "semantic_repair_of_outline_candidate",
            "candidate_id": candidate_id,
            "original_provider_output": original_view,
            "validation_error": str(error),
            "allowed_paper_ids": allowed_papers_view,
            "allowed_relation_ids": allowed_relations_view,
            "repair_rules": list(SEMANTIC_REPAIR_RULES_V1),
            "output_schema": dict(SEMANTIC_REPAIR_OUTPUT_SCHEMA_V1),
            **({"candidate_output_scope": alias_structural(
                self._candidate_output_scope_wire(allowed_paper_keys), alias_map,
            ) if alias_map is not None else self._candidate_output_scope_wire(allowed_paper_keys)}
               if self._candidate_output_scope is not None else {}),
        }
        repair_deps = {"candidate": _hash_payload(content)}
        try:
            raw_repaired = self._provider_call(
                repair_node_id,
                repair_request,
                expect_json=True,
                input_artifact_hashes=tuple(repair_deps.values()),
                transport_node_id=(
                    f"{candidate_id}_provider_generation"
                    if candidate_id and candidate_id.startswith("candidate_")
                    else None
                ),
            )
        except Exception as exc:
            # Transport/schema failure of the single repair attempt is also
            # fail-closed: no third provider attempt is made from this path.
            self._publish_repair_failure(candidate_id, exc, node_id=repair_node_id)
            raise
        # The repair call is dynamic; align its expected-contract provider with
        # the candidate generation route so the receipt closure stays exact.
        repair_call_id = self._provider_call_id(repair_node_id)
        repair_expected = self._expected_provider_calls.get(repair_call_id)
        if repair_expected is not None:
            generation_route = self._node_route(
                f"{candidate_id}_provider_generation"
            )
            if str(repair_expected.provider or "") != str(generation_route.provider_name or ""):
                self._expected_provider_calls[repair_call_id] = replace(
                    repair_expected,
                    provider=str(generation_route.provider_name or repair_expected.provider),
                    model=str(generation_route.model or repair_expected.model),
                )
        repaired_content = (
            canonicalize_structural(dict(raw_repaired), alias_map)
            if alias_map is not None
            else dict(raw_repaired)
        )
        # Bounded structural guards: a format repair cannot change section
        # count, order, or identity. Any semantic split/merge/delete must be a
        # separate versioned candidate revision with explicit lineage.
        repaired_sections = [
            section
            for section in (repaired_content.get("sections") or [])
            if isinstance(section, Mapping)
        ]
        original_ids = [str(s.get("section_id") or "") for s in original_sections]
        repaired_ids = [str(s.get("section_id") or "") for s in repaired_sections]
        if (
            not repaired_sections
            or len(repaired_sections) != len(original_sections)
            or len(set(repaired_ids)) != len(repaired_ids)
            or repaired_ids != original_ids
        ):
            failure = OutlineV3ExecutionError(
                f"{candidate_id} format repair changed section count/order/identity: "
                f"before={original_ids}, after={repaired_ids}"
            )
            self._publish_repair_failure(candidate_id, failure, node_id=repair_node_id)
            raise failure

        def _claim_total(sections: Sequence[Mapping[str, Any]]) -> int:
            return sum(
                len([claim for claim in (s.get("claims") or []) if str(claim).strip()])
                for s in sections
            )

        if _claim_total(repaired_sections) > _claim_total(original_sections):
            failure = OutlineV3ExecutionError(
                f"{candidate_id} semantic repair inflated the planned claims"
            )
            self._publish_repair_failure(candidate_id, failure, node_id=repair_node_id)
            raise failure
        try:
            self._validate_candidate_payload(
                candidate_id,
                repaired_content,
                allowed_paper_keys=allowed_paper_keys,
                allowed_relation_ids=allowed_relation_ids,
                alias_map=alias_map,
            )
        except Exception as exc:
            self._publish_repair_failure(candidate_id, exc, node_id=repair_node_id)
            raise OutlineV3ExecutionError(
                f"{candidate_id} semantic repair failed targeted revalidation: {exc}"
            ) from exc
        self._persist_repair_output(
            repair_node_id,
            dict(repaired_content),
            dependency_hashes=repair_deps,
        )
        return repaired_content

    def _initialize_primary_candidate_repair_plan(self) -> OutlineCandidateRepairPlanV1:
        """Bind the primary repair envelope to actual READY runtime sources."""
        from ai_interface import _coerce_positive_int, _load_api_runtime_settings
        from outline.candidate_repair_plan import build_primary_candidate_repair_plan_v1

        accepted = self.runtime_spec_binding
        if accepted is None:
            raise OutlineV3ExecutionError("primary candidate repair requires a verified runtime spec binding")
        current = read_runtime_spec_binding_v1(
            self.registry, expected_effective_config_sha256=accepted.effective_config_sha256,
        )
        if current != accepted:
            raise OutlineV3ExecutionError("primary candidate repair runtime spec binding changed")
        layers = self.registry.get("outline-v3:outline_content_layers")
        if layers is None or layers.status != "ready":
            raise OutlineV3ExecutionError("primary candidate repair source authority is not ready")
        from services.writer_source_inventory import load_writer_source_inventory_v1

        if load_writer_source_inventory_v1(self.registry) is None:
            raise OutlineV3ExecutionError("primary candidate repair lacks canonical source inventory authority")
        layers = self.registry.verify_ready_artifact_closure(layers)
        route = self._node_route("candidate_1_provider_generation")
        transport_config = getattr(route.transport, "api_config", None)
        if not isinstance(transport_config, Mapping):
            raise OutlineV3ExecutionError("primary repair finite plan requires a runtime-owned transport configuration")
        if safe_config_identity(transport_config) != dict(route.config_identity):
            raise OutlineV3ExecutionError("primary repair transport configuration differs from its bound route")
        request_timeout, _attempts = _load_api_runtime_settings(transport_config)
        total_timeout = _coerce_positive_int(transport_config.get("total_timeout_seconds"), request_timeout)
        plan = build_primary_candidate_repair_plan_v1(
            candidate_count=self.candidate_count, semantic_repair_enabled=self.semantic_repair_enabled,
            route_identity=route.binding_identity,
            route_config_fingerprint_sha256=route.safe_config_fingerprint(), profile=route.profile,
            effective_input_cap=self._effective_input_cap(route.profile),
            retry_attempts_per_call_upper_bound=max(0, int(route.config_identity.get("transport_retries") or 0)),
            wall_seconds_per_call_upper_bound=float(total_timeout),
            config_source_id=current.config_source_id, config_source_sha256=current.config_source_sha256,
            runtime_spec_sha256=current.normalized_spec_artifact_sha256,
            canonical_source_authority_id=layers.artifact_id, canonical_source_authority_sha256=layers.content_hash,
        )
        if self._primary_candidate_repair_plan is not None:
            if plan.contract_sha256 != self._primary_candidate_repair_plan.contract_sha256:
                raise OutlineV3ExecutionError("primary candidate repair envelope changed after materialization")
            return self._primary_candidate_repair_plan
        spec_record = self.registry.get(current.normalized_spec_artifact_id)
        if spec_record is None:
            raise OutlineV3ExecutionError("primary candidate repair spec authority disappeared")
        plan_record = publish_json_artifact(
            self.publication_context, self.registry,
            self._path(f"outline_v3/primary_repair_plan_{plan.contract_sha256}.json"), plan.to_dict(),
            artifact_role="outline_repair_plan", artifact_type="outline_candidate_repair_plan",
            artifact_version="v1", producer="outline.v3_executor.OutlineV3Executor",
            artifact_id=plan.cardinality_basis().basis_artifact,
            depends_on=(ArtifactDependencyRefV2.from_record(spec_record), ArtifactDependencyRefV2.from_record(layers)),
            metadata={"scope": "primary", "normalized_spec_hash_kind": "registered_artifact_bytes_sha256"},
        )
        self._primary_candidate_repair_plan_record = plan_record
        exposure = plan.to_exposure()
        if exposure is not None and exposure.cardinality_basis is not None:
            exposure = replace(
                exposure,
                cardinality_basis=replace(
                    plan.cardinality_basis(),
                    basis_artifact=plan_record.artifact_id,
                    basis_artifact_sha256=plan_record.content_hash,
                ),
            )
            self.stability_preflight["primary_candidate_repair_exposure"] = exposure.to_dict()
            initial_record = self.registry.get(f"outline-v3:provider_call_plan:{self.stability_mode}")
            dependencies = [ArtifactDependencyRefV2.from_record(plan_record)]
            if initial_record is not None:
                dependencies.append(ArtifactDependencyRefV2.from_record(initial_record))
            publish_json_artifact(
                self.publication_context, self.registry,
                self._path(f"outline_v3/primary_repair_exposure_{plan.contract_sha256}.json"),
                {"schema_version": "outline-primary-repair-exposure/v1", "scope": "primary",
                 "plan_contract_sha256": plan.contract_sha256, "exposure": exposure.to_dict()},
                artifact_role="outline_repair_exposure", artifact_type="outline_primary_repair_exposure",
                artifact_version="v1", producer="outline.v3_executor.OutlineV3Executor",
                artifact_id=f"outline-v3:primary-repair-exposure:{plan.contract_sha256}",
                depends_on=tuple(dependencies),
            )
        self._primary_candidate_repair_plan = plan
        return plan

    @staticmethod
    def _unbound_component_repair_transport(route: OutlineRoleRoute) -> bool:
        from runtime.test_dependencies import current_runtime_test_dependencies

        dependencies = current_runtime_test_dependencies()
        return bool(
            dependencies is not None and dependencies.external_transport_disabled
            and not isinstance(getattr(route.transport, "api_config", None), Mapping)
        )

    def _materialize_primary_repair_request(
        self, node_id: str, request: Mapping[str, Any], route: OutlineRoleRoute, binding: Mapping[str, Any],
    ) -> None:
        if re.fullmatch(r"candidate_[1-9]\d*_semantic_repair", node_id) is None:
            return
        if self._unbound_component_repair_transport(route):
            return  # This explicit test adapter cannot provide production wall/config authority.
        if self.runtime_spec_binding is None:
            from runtime.test_dependencies import current_runtime_test_dependencies

            dependencies = current_runtime_test_dependencies()
            if dependencies is not None and dependencies.external_transport_disabled:
                return  # Explicit component-test lane has no production source envelope.
            raise OutlineV3ExecutionError("primary candidate repair runtime binding is missing before transport")
        plan = self._initialize_primary_candidate_repair_plan()
        accepted = self.runtime_spec_binding
        row = plan.materialize_request_row(
            node_id.removesuffix("_semantic_repair"), request, route=route,
            route_config_fingerprint_sha256=route.safe_config_fingerprint(), profile=route.profile,
            retry_attempts=max(0, int(route.config_identity.get("transport_retries") or 0)),
            wall_seconds_upper_bound=plan.wall_seconds_per_call_upper_bound,
            config_source_id=accepted.config_source_id, config_source_sha256=accepted.config_source_sha256,
            runtime_spec_sha256=accepted.normalized_spec_artifact_sha256,
            canonical_source_authority_id=plan.canonical_source_authority_id,
            canonical_source_authority_sha256=plan.canonical_source_authority_sha256,
        )
        if row.request_estimate.request_hash != binding.get("prompt_payload_hash"):
            raise OutlineV3ExecutionError("primary candidate repair materialization differs from its transport binding")

    def _assert_primary_repair_fresh_attempt(self, node_id: str) -> None:
        """Refuse a second primary repair after exact replay was considered.

        A completed receipt without a verified output and an explicitly failed
        repair both require recovery, even if their transport outcome is unknown.
        Stability variants have separate node identities and budgets.
        """
        if re.fullmatch(r"candidate_[1-9]\d*_semantic_repair", node_id) is None:
            return
        candidate_id = node_id.removesuffix("_semantic_repair")
        for record in self.registry.list_records():
            if record.artifact_type != "outline_candidate_repair_failure" or record.status != "ready":
                continue
            payload = json.loads(Path(record.path).read_text(encoding="utf-8"))
            if (
                isinstance(payload, Mapping)
                and payload.get("candidate_id") == candidate_id
                and payload.get("repair_node_id") == node_id
                and payload.get("attempt_identity") == self.logical_attempt_identity
            ):
                self.registry.verify_ready_artifact_closure(record)
                if payload.get("provider_attempted") is False:
                    continue
                raise OutlineV3ExecutionError(
                    f"{node_id} was already attempted; recover its recorded failure before new transport"
                )
        call_id = self._provider_call_id(node_id)
        for receipt in self._receipt_ledger.list_receipts():
            if (
                receipt.job_id == self.job_id
                and receipt.call_id == call_id
                and receipt.logical_attempt_identity == self.logical_attempt_identity
                and receipt.status != "blocked"
            ):
                raise OutlineV3ExecutionError(
                    f"{node_id} was already attempted without reusable output; preserve its receipt for recovery"
                )

    def _persist_repair_output(
        self,
        node_id: str,
        payload: Mapping[str, Any],
        *,
        dependency_hashes: Mapping[str, str],
    ) -> None:
        """Persist one bounded semantic-repair provider output durably.

        Mirrors stability-audit persistence: the repair is a real provider
        call with an immutable Registry artifact and replay identity, but it
        is NOT a canonical DAG node (the DAG stays canonical).
        """
        artifact = self._artifact(OutlineArtifact, dict(payload), dependency_hashes)
        artifact_id = f"outline-v3:repair:{hash_text(node_id)[:24]}"
        record = publish_json_artifact(
            self.publication_context,
            self.registry,
            self._node_path(node_id),
            artifact.to_dict(),
            artifact_role="outline_v3_repair_output",
            artifact_type="outline_candidate_repair",
            artifact_version="v1",
            producer="outline.v3_executor.OutlineV3Executor",
            artifact_id=artifact_id,
            metadata={
                "job_id": self.job_id,
                "node_id": node_id,
                "content_hash": artifact.content_hash,
            },
        )
        self.artifact_paths[node_id] = record.path
        self.artifact_records[node_id] = record
        self._payloads[node_id] = dict(payload)
        expected = self._expected_provider_calls.get(self._provider_call_id(node_id))
        if expected is not None:
            self._expected_provider_calls[expected.call_id] = replace(
                expected,
                artifact_payload_hash=hash_json(payload),
                artifact_content_hash=artifact.content_hash,
                registry_file_hash=record.content_hash,
                artifact_path=record.path,
                registered_artifact_hash=artifact.content_hash,
                node_output_hash=artifact.content_hash,
            )
            pending = self._pending_replays.pop(node_id, None)
            if pending is not None and expected.normalized_output_hash:
                replay_key, normalized_hash, receipt_id = pending
                self._replay_store.append(
                    replay_key,
                    output_hash=normalized_hash,
                    normalized_output_hash=normalized_hash,
                    registered_artifact_hash=artifact.content_hash,
                    node_output_hash=artifact.content_hash,
                    output_artifact_ids=(artifact_id,),
                    receipt_ids=(receipt_id,),
                    audit_node_id=node_id,
                    closure_epoch_id=self.closure_epoch_id,
                )
                self._expected_provider_calls[expected.call_id] = replace(
                    self._expected_provider_calls[expected.call_id],
                    replay_output_hash=normalized_hash,
                )

    def _publish_repair_failure(
        self, candidate_id: str, error: Exception, *, node_id: str = ""
    ) -> None:
        repair_node_id = node_id or f"{candidate_id}_semantic_repair"
        provider_attempted = any(
            audit.get("node_id") == repair_node_id and audit.get("provider_invoked") is True
            for audit in self._request_payload_audit
        ) or any(
            receipt.call_id == self._provider_call_id(repair_node_id)
            and receipt.logical_attempt_identity == self.logical_attempt_identity
            and receipt.status != "blocked"
            for receipt in self._receipt_ledger.list_receipts()
        )
        payload = {
            "artifact_type": "outline_candidate_repair_failure",
            "artifact_version": "v1",
            "candidate_id": candidate_id,
            "repair_node_id": repair_node_id,
            "provider_attempted": provider_attempted,
            "error": str(error),
            "attempt_identity": str(
                getattr(self, "logical_attempt_identity", "") or ""
            ),
        }
        digest = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()[:24]
        target = Path(self.receipt_ledger_target_path).parent / (
            f"outline_candidate_repair_failure__{digest}.json"
        )
        publish_bytes_artifact(
            self.publication_context,
            self.registry,
            target,
            json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            artifact_role="outline_repair",
            artifact_type="outline_candidate_repair_failure",
            artifact_version="v1",
            producer="outline.v3_executor.OutlineV3Executor",
        )

    def _register_receipt_ledger(self) -> None:
        path = self._receipt_ledger.path
        if not path.is_file():
            return
        payload = path.read_bytes()
        if not payload.strip():
            return
        # Registry content_hash is the SHA-256 of the exact registered bytes;
        # do not use the provider-runtime domain hash of the decoded text.
        payload_hash = hashlib.sha256(payload).hexdigest()
        existing = self.registry.get("outline_v3_provider_receipts")
        artifact_id = "outline_v3_provider_receipts"
        if existing is not None and existing.content_hash != payload_hash:
            artifact_id = f"outline_v3_provider_receipts:{payload_hash[:24]}"
        record = publish_bytes_artifact(
            self.publication_context,
            self.registry,
            self.receipt_ledger_target_path,
            payload,
            artifact_role="provider_receipts",
            artifact_type="provider_receipt_ledger",
            artifact_version="v1",
            producer="outline.v3_executor.OutlineV3Executor",
            artifact_id=artifact_id,
            metadata={
                "receipt_count": len(self._receipt_ledger.list_receipts()),
                "stage_name": "outline_v3",
                "closure_epoch_id": self.closure_epoch_id,
            },
        )
        self.artifact_paths["provider_receipts"] = record.path
        self.artifact_records["provider_receipts"] = record

    def _verify_exact_replay_with_second_executor(self) -> dict[str, Any]:
        """Resume the same workspace through a fresh executor with no transport.

        The in-process stability variant proves that the semantic replay key is
        usable.  This second executor is the stronger boundary check: a fresh
        registry, node store, and replay store must reproduce the provider
        outputs without being allowed to invoke the provider transport.
        """

        comparison_nodes = tuple(dict.fromkeys((
            *self._provider_node_ids(),
            "selected_candidate",
            "selected_candidate_revision",
            "section_evidence_packets",
            "final_outline",
            "provider_receipt_closure",
        )))

        def semantic_artifact_hash(executor: "OutlineV3Executor", node_id: str) -> str:
            record = executor.artifact_records.get(node_id)
            if record is None:
                return ""
            try:
                payload = json.loads(Path(record.path).read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                return record.content_hash
            if isinstance(payload, Mapping) and str(payload.get("content_hash") or ""):
                if node_id == "provider_receipt_closure":
                    body = payload.get("payload")
                    if isinstance(body, Mapping):
                        decision_fields = (
                            "closure_epoch_id",
                            "expected_call_ids",
                            "duplicate_expected_call_ids",
                            "observed_call_ids",
                            "missing_call_ids",
                            "stale_call_ids",
                            "failed_call_ids",
                            "incomplete_call_ids",
                            "hash_mismatches",
                            "unexpected_receipts",
                            "out_of_epoch_receipts",
                            "retry_exceeded_call_ids",
                            "usage_incomplete_call_ids",
                            "verified_reuse_call_ids",
                            "complete",
                        )
                        return hash_json({key: body.get(key) for key in decision_fields})
                return str(payload["content_hash"])
            return record.content_hash

        expected_hashes = {
            node_id: semantic_artifact_hash(self, node_id)
            for node_id in comparison_nodes
            if node_id in self.artifact_records
        }
        transport_calls: list[str] = []

        def forbidden_provider(node_id: str, request: Mapping[str, Any]) -> Mapping[str, Any]:
            transport_calls.append(str(node_id))
            raise OutlineV3ExecutionError(
                f"exact replay transport invoked for {node_id}"
            )

        replay_router = (
            OutlineProviderRouter(
                routes={
                    role: replace(route, transport=forbidden_provider)
                    for role, route in self.router.routes.items()
                },
                diagnostics=self.router.diagnostics,
            )
            if self.router is not None else None
        )

        second_registry = (
            self.publication_context.registry(self.registry.registry_path, self.job_id)
            if self.publication_context is not None
            else ArtifactRegistry(self.registry.registry_path, self.job_id)
        )
        second_executor = OutlineV3Executor(
            job_id=self.job_id,
            summaries=self.summaries,
            workspace=self.workspace,
            artifact_registry=second_registry,
            provider=forbidden_provider,
            provider_profile=self.profile,
            provider_router=replay_router,
            enabled_semantic_roles=self.enabled_semantic_roles,
            reachable_provider_route_plan=self.reachable_provider_route_plan,
            candidate_count=self.candidate_count,
            review_intent=self.review_intent_input,
            quality_gate=self.quality_gate,
            logical_attempt_identity=self.logical_attempt_identity,
            publication_context=self.publication_context,
            # The fresh-executor proof replays the canonical decision chain;
            # it must not launch the perturbation matrix again.  Running the
            # audit variants here would manufacture new variant keys and turn
            # a zero-transport replay check into another stability run.
            stability_mode="off",
            semantic_repair_enabled=self.semantic_repair_enabled,
            opaque_alias_enabled=self.opaque_alias_enabled,
            technical_shard_target_tokens=self.technical_shard_target_tokens,
            max_provider_calls=None,
            max_estimated_cost=None,
            max_estimated_total_tokens=self.max_estimated_total_tokens,
            estimated_cost_per_1k_tokens=self.estimated_cost_per_1k_tokens,
            max_source_prompt_tokens=self.max_source_prompt_tokens,
            semantic_output_max_tokens=self.semantic_output_max_tokens,
            semantic_transport_retries=self.semantic_transport_retries,
            _skip_exact_replay_verification=True,
        )
        # A repaired canonical candidate is reused rather than repaired a
        # second time. Carry only already verified repair-call authority into
        # the replay closure; this does not create a receipt or authorize POST.
        receipt_index = self._replay_receipt_index()
        verified_repair_call_ids: list[str] = []
        for call_id, expected in self._expected_provider_calls.items():
            if not call_id.endswith("_semantic_repair"):
                continue
            matching = [
                receipt for receipt in receipt_index.values()
                if str(getattr(receipt, "call_id", "") or "") == call_id
                and str(getattr(receipt, "closure_epoch_id", "") or "") == self.closure_epoch_id
                and str(getattr(receipt, "status", "") or "") == "success"
                and str(getattr(receipt, "response_hash", "") or "")
                == str(expected.provider_response_hash or "")
            ]
            if not matching:
                raise OutlineV3ExecutionError(
                    f"canonical recovery lacks verified repair-call authority for {call_id}"
                )
            second_executor._expected_provider_calls[call_id] = expected
            verified_repair_call_ids.append(str(call_id))
            for receipt in matching:
                if str(receipt.receipt_id) not in second_executor.receipts:
                    second_executor.receipts.append(str(receipt.receipt_id))
        second_result = second_executor.run()
        if transport_calls:
            binding_differences: dict[str, list[str]] = {}
            for node_id in transport_calls:
                first_node = self._dag.get(node_id)
                second_node = second_executor._dag.get(node_id)
                first_binding = dict(first_node.execution_binding or {}) if first_node else {}
                second_binding = dict(second_node.execution_binding or {}) if second_node else {}
                binding_differences[node_id] = sorted(
                    key for key in set(first_binding) | set(second_binding)
                    if first_binding.get(key) != second_binding.get(key)
                )
            raise OutlineV3ExecutionError(
                "second-executor exact replay invoked provider transport: "
                + ",".join(transport_calls)
                + f"; binding-difference fields={binding_differences}"
                + f"; replay-diagnostics={second_executor.replay_diagnostics[:8]}"
            )
        if not second_result.ok:
            second_health = ""
            health_path = second_result.artifacts.get("stage_health")
            if health_path:
                try:
                    health_envelope = json.loads(Path(health_path).read_text(encoding="utf-8"))
                    health_payload = health_envelope.get("payload") if isinstance(health_envelope, Mapping) else {}
                    second_health = json.dumps(
                        {
                            "status": health_payload.get("status") if isinstance(health_payload, Mapping) else "",
                            "diagnostics": health_payload.get("diagnostics") if isinstance(health_payload, Mapping) else [],
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                except (OSError, UnicodeError, json.JSONDecodeError):
                    second_health = "unreadable_stage_health"
            raise OutlineV3ExecutionError(
                "second-executor exact replay was blocked "
                f"with status={second_result.status}, "
                f"failed_nodes={list(second_executor._dag.failed_node_ids)}: "
                + "; ".join(second_result.diagnostics)
                + f"; stage_health={second_health}"
            )
        second_hashes = {
            node_id: semantic_artifact_hash(second_executor, node_id)
            for node_id in comparison_nodes
            if node_id in second_executor.artifact_records
        }
        differing_nodes = {
            node_id
            for node_id in set(expected_hashes) | set(second_hashes)
            if expected_hashes.get(node_id) != second_hashes.get(node_id)
        }
        reused_repair_call_ids: list[str] = list(verified_repair_call_ids)
        if differing_nodes == {"provider_receipt_closure"} and self._repair_enabled:
            def closure_payload(executor: "OutlineV3Executor") -> Mapping[str, Any]:
                record = executor.artifact_records.get("provider_receipt_closure")
                if record is None:
                    raise OutlineV3ExecutionError("replay receipt closure artifact is missing")
                executor.registry.verify_ready_artifact_closure(record)
                envelope = json.loads(Path(record.path).read_text(encoding="utf-8"))
                payload = envelope.get("payload") if isinstance(envelope, Mapping) else None
                if not isinstance(payload, Mapping) or payload.get("complete") is not True:
                    raise OutlineV3ExecutionError("replay receipt closure is not complete")
                for field in ("missing_call_ids", "failed_call_ids", "incomplete_call_ids", "hash_mismatches", "unexpected_receipts"):
                    if payload.get(field):
                        raise OutlineV3ExecutionError(f"replay receipt closure has {field}")
                return payload

            first_closure = closure_payload(self)
            second_closure = closure_payload(second_executor)
            first_expected = set(first_closure.get("expected_call_ids") or ())
            second_expected = set(second_closure.get("expected_call_ids") or ())
            dropped = first_expected - second_expected
            if (
                not dropped
                or not second_expected.issubset(first_expected)
                or any(not str(item).endswith("_semantic_repair") for item in dropped)
                or set(first_closure.get("observed_call_ids") or ()) != first_expected
                or set(second_closure.get("observed_call_ids") or ()) != second_expected
            ):
                raise OutlineV3ExecutionError(
                    "second-executor receipt closure differs beyond completed repair calls"
                )
            receipt_index = self._replay_receipt_index()
            for call_id in sorted(dropped):
                matching = [
                    receipt for receipt in receipt_index.values()
                    if str(getattr(receipt, "call_id", "") or "") == call_id
                    and str(getattr(receipt, "closure_epoch_id", "") or "") == self.closure_epoch_id
                    and str(getattr(receipt, "status", "") or "") == "success"
                    and str(getattr(receipt, "response_hash", "") or "")
                ]
                if not matching:
                    raise OutlineV3ExecutionError(
                        f"canonical recovery lacks a verified repair receipt for {call_id}"
                    )
                if str(call_id) not in reused_repair_call_ids:
                    reused_repair_call_ids.append(str(call_id))
            differing_nodes.clear()
        if differing_nodes:
            raise OutlineV3ExecutionError(
                "second-executor exact replay changed decision artifacts: "
                + ",".join(sorted(differing_nodes))
            )
        replay_hits = [
            item for item in second_executor._replay_evidence
            if not item.get("provider_invoked")
        ]
        if not replay_hits:
            raise OutlineV3ExecutionError(
                "second-executor exact replay produced no replay-store hits"
            )
        return {
            "status": "verified",
            "provider_invoked": False,
            "second_executor_invoked": True,
            "transport_call_count": 0,
            "replay_hit_count": len(replay_hits),
            "comparison_kind": (
                "adopted_canonical_artifacts_and_repair_receipts"
                if reused_repair_call_ids or any(item.get("adopted_canonical_replay") for item in replay_hits)
                else "provider_output_and_registered_artifacts"
            ),
            "raw_provider_output_bit_exact": not bool(
                reused_repair_call_ids
                or any(item.get("adopted_canonical_replay") for item in replay_hits)
            ),
            "reused_repair_call_ids": reused_repair_call_ids,
            "replay_evidence": replay_hits,
            "compared_artifact_hashes": dict(expected_hashes),
        }

    def run(self) -> OutlineV3ExecutionResult:  # pyright: ignore[reportGeneralTypeIssues]
        # This orchestration method intentionally keeps the ordered DAG
        # execution visible; the semantic subroutines below carry the
        # individual validation contracts.  Pyright's path-complexity limit
        # cannot analyze this finite dispatcher without losing useful types.
        try:
            if self.outline_pilot is not None:
                self._pilot_static_admission()
            else:
                self._preflight_stability_budget()
            summary_set_hash = hash_json(self.summaries)
            summaries_path = self._path(
                f"outline_v3/inputs/stage1_summaries_{summary_set_hash[:24]}.json"
            )
            immutable_id = f"outline-v3:stage1-summaries:{summary_set_hash}"
            existing_record = self.registry.get(immutable_id)
            if existing_record is not None and existing_record.status == "ready":
                summaries_path = existing_record.path
                try:
                    existing = json.loads(Path(summaries_path).read_text(encoding="utf-8"))
                except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                    raise OutlineV3ExecutionError(
                        f"content-addressed Stage 1 summary artifact is unreadable: {summaries_path}"
                    ) from exc
                existing_summaries = (
                    existing.get("summaries")
                    if isinstance(existing, Mapping)
                    else existing
                )
                if hash_json(existing_summaries) != summary_set_hash:
                    raise OutlineV3ExecutionError(
                        f"content-addressed Stage 1 summary artifact has drifted: {summaries_path}"
                    )
            else:
                immutable_record = publish_json_artifact(
                    self.publication_context,
                    self.registry,
                    summaries_path,
                    {
                        "artifact_type": "stage1_canonical_summaries",
                        "artifact_version": "v1",
                        "job_id": self.job_id,
                        "summary_set_hash": summary_set_hash,
                        "summaries": self.summaries,
                    },
                    artifact_role="stage1_input",
                    artifact_type="stage1_canonical_summaries",
                    artifact_version="v1",
                    producer="outline.v3_executor.OutlineV3Executor",
                    artifact_id=immutable_id,
                    metadata={
                        "immutable": True,
                        "summary_set_hash": summary_set_hash,
                        "versioned_artifact_id": immutable_id,
                    },
                )
                summaries_path = immutable_record.path
            immutable_record = self.registry.get(immutable_id)
            if immutable_record is None or immutable_record.status != "ready":
                raise OutlineV3ExecutionError("immutable Stage 1 summary record is unavailable after publication")
            stage1 = publish_json_artifact(
                self.publication_context,
                self.registry,
                summaries_path,
                {
                    "artifact_type": "stage1_canonical_summaries",
                    "artifact_version": "v1",
                    "job_id": self.job_id,
                    "summary_set_hash": summary_set_hash,
                    "summaries": self.summaries,
                },
                artifact_role="stage1_input",
                artifact_type="stage1_canonical_summaries",
                artifact_version="v1",
                producer="outline.v3_executor.OutlineV3Executor",
                artifact_id="stage1_summaries",
                depends_on=[ArtifactDependencyRefV2.from_record(immutable_record)],
                metadata={
                    "pointer_role": "current",
                    "current_version_artifact_id": immutable_id,
                    "summary_set_hash": summary_set_hash,
                },
            )
            stage1_hash = stage1.content_hash

            evidence = self._run_node("outline_evidence_views", lambda: (
                self._artifact(OutlineArtifact, build_outline_evidence_views(self.summaries, self.job_id).to_dict(), {"stage1_summaries": stage1_hash}),
                (), "deterministic", "local",
            ))
            evidence_model = build_outline_evidence_views(self.summaries, self.job_id)
            ledger_model = build_global_corpus_ledger(evidence_model)
            ledger = self._run_node("global_corpus_ledger", lambda: (
                self._artifact(OutlineArtifact, ledger_model.to_dict(), {"outline_evidence_views": _hash_payload(evidence)}),
                ("outline_evidence_views",), "deterministic", "local",
            ))
            matrix_model = build_multi_view_matrix(evidence_model)
            matrix = self._run_node("multi_view_matrix", lambda: (
                self._artifact(OutlineArtifact, matrix_model.to_dict(), {"outline_evidence_views": _hash_payload(evidence), "global_corpus_ledger": _hash_payload(ledger)}),
                ("outline_evidence_views", "global_corpus_ledger"), "deterministic", "local",
            ))
            content_layers_model = build_paper_content_layers(
                self.summaries,
                evidence_model,
                job_id=self.job_id,
            )
            content_layers = self._run_node("outline_content_layers", lambda: (
                self._artifact(
                    OutlineArtifact,
                    content_layers_model.to_dict(),
                    {"outline_evidence_views": _hash_payload(evidence)},
                ),
                ("outline_evidence_views",),
                "deterministic",
                "local",
            ))
            if (
                self.semantic_repair_enabled and self.runtime_spec_binding is not None
                and not self._unbound_component_repair_transport(self._node_route("candidate_1_provider_generation"))
            ):
                self._initialize_primary_candidate_repair_plan()
            candidate_map_model = build_global_relation_map(evidence_model, matrix_model, ledger_model)
            candidate_map = self._run_node("relation_candidates", lambda: (
                self._artifact(OutlineArtifact, candidate_map_model.to_dict(), {"multi_view_matrix": _hash_payload(matrix)}),
                ("multi_view_matrix",), "deterministic", "local",
            ))

            semantic_chunk_plan_model = build_semantic_chunk_plan(
                content_layers_model,
                candidate_map_model,
                candidate_count=self.candidate_count,
                physical_call_limit=authorized_provider_call_limit(self.max_provider_calls),
            )
            full_stage_only_codes = {
                "physical_call_budget_exceeded",
                "logical_node_input_exceeds_hard_limit",
                "relation_evidence_incomplete",
            }
            blocking_diagnostics = [
                item for item in semantic_chunk_plan_model.blocking_diagnostics
                if self.outline_pilot is None
                or str(item.get("code") or "") not in full_stage_only_codes
            ]
            if blocking_diagnostics:
                raise OutlineV3ExecutionError(
                    "semantic chunk plan is blocked before provider admission: "
                    + json.dumps(
                        blocking_diagnostics,
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                )
            if self.outline_pilot is not None:
                self.stability_preflight["full_stage_blocking_diagnostics"] = [
                    item for item in semantic_chunk_plan_model.blocking_diagnostics
                    if item not in blocking_diagnostics
                ]
            if self.outline_pilot is None and self.stability_mode != "off":
                frozen_scope = self._canonical_stability_relation_scope()
                if semantic_chunk_plan_model.content_hash != frozen_scope["semantic_chunk_plan_hash"]:
                    raise OutlineV3ExecutionError(
                        "primary semantic selection differs from stability preflight scope"
                    )
            semantic_chunk_plan = self._run_node("semantic_chunk_plan", lambda: (
                self._artifact(
                    OutlineArtifact,
                    semantic_chunk_plan_model.to_dict(),
                    {
                        "outline_content_layers": _hash_payload(content_layers),
                        "relation_candidates": _hash_payload(candidate_map),
                        "global_corpus_ledger": _hash_payload(ledger),
                        "multi_view_matrix": _hash_payload(matrix),
                    },
                ),
                ("outline_content_layers", "relation_candidates", "global_corpus_ledger", "multi_view_matrix"),
                "deterministic",
                "local",
            ))

            topic_plan = build_topic_synthesis_plan(semantic_chunk_plan_model)
            navigation_payload = {
                "schema_version": "outline-global-navigation/v1",
                "execution_mode": "deterministic_local_routing",
                "status": "completed",
                "content_layers_hash": content_layers_model.content_hash,
                "semantic_chunk_plan_hash": semantic_chunk_plan_model.content_hash,
                "topic_ids": [item.topic_id for item in semantic_chunk_plan_model.topics],
                "paper_ids": [card.paper_id for card in content_layers_model.index_cards],
                "outlier_policy": "preserve_explicit_outlier_topic",
            }
            global_navigation = self._run_node("global_navigation", lambda: (
                self._artifact(
                    OutlineArtifact,
                    navigation_payload,
                    {
                        "outline_content_layers": _hash_payload(content_layers),
                        "semantic_chunk_plan": _hash_payload(semantic_chunk_plan),
                    },
                ),
                ("outline_content_layers", "semantic_chunk_plan"),
                "deterministic",
                "local",
            ))
            def _semantic_node_reusable(node_id: str) -> bool:
                try:
                    existing = self._dag.get(node_id)
                    if existing.status != "succeeded" or not existing.execution_binding:
                        return False
                    if node_id in {
                        "topic_synthesis",
                        "cross_group_comparison",
                        "global_synthesis",
                    }:
                        cached_record = self.registry.get(f"outline-v3:{node_id}")
                        if cached_record is None or cached_record.status != "ready":
                            return False
                        cached_envelope = json.loads(
                            Path(cached_record.path).read_text(encoding="utf-8")
                        )
                        cached_payload = cached_envelope.get("payload")
                        if (
                            not isinstance(cached_payload, Mapping)
                            or cached_payload.get("interpretation_contract_version")
                            != INTERPRETATION_CONTRACT_VERSION
                        ):
                            return False
                        if (
                            node_id in {"cross_group_comparison", "global_synthesis"}
                            and not self._shared_semantic_cache_valid(node_id, cached_payload)
                        ):
                            return False
                    current = self.build_current_node_binding(node_id)
                    identity_fields = (
                        "dependency_hashes",
                        "node_version",
                        "schema_hash",
                        "prompt_sha256",
                        "provider_route",
                        "provider_family",
                        "model_name",
                        "endpoint_type",
                        "route_fingerprint",
                        "context_profile_hash",
                        "relevant_runtime_config_hash",
                    )
                    return all(
                        hash_json(existing.execution_binding.get(field))
                        == hash_json(current.get(field))
                        for field in identity_fields
                    )
                except (KeyError, ValueError, TypeError):
                    return False

            topic_semantic_reused = (
                self.outline_pilot is None and _semantic_node_reusable("topic_synthesis")
            )
            semantic_provider_results: list[dict[str, Any]] = []
            if self.outline_pilot is not None and not topic_plan:
                raise OutlineV3ExecutionError("topic pilot has no topic synthesis plan")
            if self.semantic_provider_synthesis_enabled and topic_plan and not topic_semantic_reused:
                topic_routes = {
                    topic.topic_id: topic for topic in semantic_chunk_plan_model.topics
                }
                topic_profile = self._node_route("candidate_1_provider_generation").profile
                aggregation_plan_rows = [
                    item
                    for item in self.semantic_request_plan
                    if str(item.get("node_id") or "").startswith(
                        ("cross_group_comparison_provider", "global_synthesis_provider")
                    )
                ]
                preflight_topic_plan_identity_hash = self._compute_topic_provider_plan_identity_hash(
                    self.semantic_request_plan
                )
                topic_plan, topic_batches, topic_plan_rows = self._plan_topic_provider_batches(
                    topic_plan,
                    topic_routes=topic_routes,
                    evidence_model=evidence_model,
                    content_layers_model=content_layers_model,
                    profile=topic_profile,
                )
                actual_topic_plan_identity_hash = self._compute_topic_provider_plan_identity_hash(
                    topic_plan_rows
                )
                if self.outline_pilot is None:
                    if (
                        preflight_topic_plan_identity_hash
                        and actual_topic_plan_identity_hash != preflight_topic_plan_identity_hash
                    ):
                        raise OutlineV3ExecutionError(
                            "executor topic request plan identity changed after provider preflight"
                        )
                    if not preflight_topic_plan_identity_hash and topic_plan_rows:
                        raise OutlineV3ExecutionError(
                            "executor topic request plan is missing its preflight identity"
                        )
                self.topic_request_plan_identity_hash = actual_topic_plan_identity_hash
                self.semantic_request_plan = [*topic_plan_rows, *aggregation_plan_rows]
                pilot_requests = (
                    self._materialize_topic_pilot_plan(
                        topic_batches=topic_batches,
                        topic_routes=topic_routes,
                        evidence_model=evidence_model,
                        content_layers_model=content_layers_model,
                        profile=topic_profile,
                    )
                    if self.outline_pilot is not None
                    else None
                )
                pilot_request_by_index = {
                    index: request for index, _batch, request in pilot_requests or ()
                }
                topic_content_layers_hash = str(getattr(content_layers_model, "content_hash", ""))
                for batch_index, batch in enumerate(topic_batches, start=1):
                    if pilot_requests is not None and batch_index not in pilot_request_by_index:
                        continue
                    paper_ids = sorted({paper_id for topic in batch for paper_id in (*topic.paper_ids, *topic.bridge_paper_ids)})
                    request = (
                        pilot_request_by_index[batch_index]
                        if pilot_requests is not None
                        else self._build_topic_provider_request(
                            batch,
                            topic_routes=topic_routes,
                            evidence_model=evidence_model,
                            content_layers_model=content_layers_model,
                            batch_index=batch_index,
                            content_layers_hash=topic_content_layers_hash,
                        )
                    )
                    raw = self._run_semantic_provider_call(
                        f"topic_synthesis_provider:batch:{batch_index}",
                        request,
                        {"global_navigation": _hash_payload(global_navigation), "semantic_chunk_plan": _hash_payload(semantic_chunk_plan)},
                    )
                    semantic_provider_results.append({
                        "node_id": "topic_synthesis",
                        "provider_node_id": f"topic_synthesis_provider:batch:{batch_index}",
                        "batch_id": f"topic_batch_{batch_index}",
                        "topic_ids": sorted({topic.topic_id for topic in batch}),
                        "fragment_ids": sorted(
                            {str(topic.fragment_id or topic.topic_id) for topic in batch}
                        ),
                        "topic_fragments": [
                            {
                                "topic_id": str(topic.topic_id),
                                "fragment_id": str(topic.fragment_id or topic.topic_id),
                                "paper_ids": sorted({str(value) for value in topic.paper_ids if str(value)}),
                                "planned_evidence_unit_ids": next(
                                    (
                                        list(row.get("planned_evidence_unit_ids") or [])
                                        for row in request.get("topics") or ()
                                        if isinstance(row, Mapping)
                                        and str(row.get("fragment_id") or "")
                                        == str(topic.fragment_id or topic.topic_id)
                                    ),
                                    [],
                                ),
                                "planned_evidence_ids": next(
                                    (
                                        list(row.get("planned_evidence_ids") or [])
                                        for row in request.get("topics") or ()
                                        if isinstance(row, Mapping)
                                        and str(row.get("fragment_id") or "")
                                        == str(topic.fragment_id or topic.topic_id)
                                    ),
                                    [],
                                ),
                                "interpretation_context": self._interpretation_context_for_units(
                                    request.get("evidence_units") or (),
                                    next(
                                        (
                                            list(row.get("planned_evidence_unit_ids") or [])
                                            for row in request.get("topics") or ()
                                            if isinstance(row, Mapping)
                                            and str(row.get("fragment_id") or "")
                                            == str(topic.fragment_id or topic.topic_id)
                                        ),
                                        [],
                                    ),
                                ),
                            }
                            for topic in batch
                        ],
                        "result_id": hash_json(
                            {
                                "provider_node_id": f"topic_synthesis_provider:batch:{batch_index}",
                                "provider_output_hash": hash_json(raw),
                            }
                        ),
                        "paper_ids": paper_ids,
                        "provider_output": raw,
                    })
                    if pilot_requests is not None:
                        result = semantic_provider_results[-1]
                        record = self._persist_semantic_provider_output(
                            str(result["provider_node_id"]),
                            raw,
                            dependency_hashes={
                                "global_navigation": _hash_payload(global_navigation),
                                "semantic_chunk_plan": _hash_payload(semantic_chunk_plan),
                            },
                        )
                        result["artifact_id"] = record.artifact_id
                        result["artifact_hash"] = record.content_hash
                if pilot_requests is not None:
                    return self._finish_topic_pilot(
                        provider_results=semantic_provider_results,
                        source_record=immutable_record,
                    )
            topic_payloads = self._build_topic_synthesis_payloads(
                topic_plan,
                semantic_provider_results,
            )
            if topic_semantic_reused:
                semantic_provider_results = []
            topic_synthesis = self._run_node("topic_synthesis", lambda: (
                self._artifact(
                    OutlineArtifact,
                    {
                        "schema_version": "outline-topic-synthesis/v3",
                        "semantic_contract_version": "semantic-evidence-graph-v2",
                        "interpretation_contract_version": INTERPRETATION_CONTRACT_VERSION,
                        "execution_mode": "provider_synthesis" if semantic_provider_results else "local_evidence_projection",
                        "status": "completed",
                        "shared_semantic_chunk_plan_hash": semantic_chunk_plan_model.content_hash,
                        "provider_request_plan_identity_hash": self.topic_request_plan_identity_hash,
                        "topics": topic_payloads,
                        # Registry payloads must not share mutable result rows
                        # with the later per-call artifact publication below.
                        "provider_results": [dict(result) for result in semantic_provider_results],
                    },
                    {
                        "global_navigation": _hash_payload(global_navigation),
                        "semantic_chunk_plan": _hash_payload(semantic_chunk_plan),
                        "provider_request_plan_identity_hash": self.topic_request_plan_identity_hash,
                    },
                ),
                ("global_navigation", "semantic_chunk_plan"),
                "provider" if semantic_provider_results else "deterministic",
                "local",
            ))
            for result in semantic_provider_results:
                record = self._persist_semantic_provider_output(
                    str(result["provider_node_id"]),
                    result.get("provider_output") if isinstance(result.get("provider_output"), Mapping) else {},
                    dependency_hashes={
                        "global_navigation": _hash_payload(global_navigation),
                        "semantic_chunk_plan": _hash_payload(semantic_chunk_plan),
                    },
                )
                result["artifact_id"] = record.artifact_id
                result["artifact_hash"] = record.content_hash
            persisted_topics = topic_synthesis.get("topics")
            persisted_topics = persisted_topics if isinstance(persisted_topics, list) else []
            # Rehydrate the exact completed topic outputs from the Registry
            # artifact. A cache hit must never rebuild this context from the
            # empty in-process provider-results list. Topics are grouped once
            # and their provider outputs are scoped to individual fragments.
            semantic_topic_context = [
                {
                    "topic_id": str(item.get("topic_id") or ""),
                    "question": str(item.get("question") or ""),
                    "paper_ids": [
                        str(value) for value in item.get("paper_ids") or () if str(value)
                    ],
                    "bridge_paper_ids": [
                        str(value)
                        for value in item.get("bridge_paper_ids") or ()
                        if str(value)
                    ],
                    "provider_batch_ids": [
                        str(value)
                        for value in item.get("provider_batch_ids") or ()
                        if str(value)
                    ],
                    "fragment_ids": [
                        str(value) for value in item.get("fragment_ids") or () if str(value)
                    ],
                    "relation_ids": [
                        str(value) for value in item.get("relation_ids") or () if str(value)
                    ],
                    "fragments": [
                        dict(value)
                        for value in item.get("fragments") or ()
                        if isinstance(value, Mapping)
                    ],
                    "provider_output_refs": [
                        dict(value)
                        for value in (
                            item.get("provider_output_refs")
                            if isinstance(item.get("provider_output_refs"), list)
                            else []
                        )
                        if isinstance(value, Mapping)
                    ],
                    "supporting_evidence_ids": [
                        str(value)
                        for value in item.get("supporting_evidence_ids") or ()
                        if str(value)
                    ],
                    "supporting_evidence_count": int(
                        item.get("supporting_evidence_count")
                        or len(item.get("supporting_evidence_ids") or [])
                    ),
                    "full_topic_synthesis_artifact": "outline-v3:topic_synthesis",
                }
                for item in persisted_topics
                if isinstance(item, Mapping)
            ]
            semantic_topic_fragment_context: list[dict[str, Any]] = []
            for item in semantic_topic_context:
                fragments = item.get("fragments") or ()
                for fragment in fragments:
                    if not isinstance(fragment, Mapping):
                        continue
                    fragment_results = [
                        value
                        for value in fragment.get("provider_results") or ()
                        if isinstance(value, Mapping)
                    ]
                    if len(fragment_results) > 1:
                        raise OutlineV3ExecutionError(
                            f"topic fragment {fragment.get('fragment_id')} has multiple provider result identities"
                        )
                    row = {
                        "topic_id": str(item.get("topic_id") or ""),
                        "fragment_id": str(fragment.get("fragment_id") or ""),
                        "question": str(item.get("question") or ""),
                        "paper_ids": [
                            str(value) for value in fragment.get("paper_ids") or () if str(value)
                        ],
                        "bridge_paper_ids": [
                            str(value)
                            for value in item.get("bridge_paper_ids") or ()
                            if str(value)
                        ],
                        "supporting_evidence_ids": [
                            value
                            for value in sorted(
                                {
                                    str(evidence_id)
                                    for result in fragment_results
                                    for provider_output in [
                                        result.get("provider_output")
                                        if isinstance(result.get("provider_output"), Mapping)
                                        else {}
                                    ]
                                    for topic_output in [
                                        provider_output.get("topic")
                                        if isinstance(provider_output.get("topic"), Mapping)
                                        else {}
                                    ]
                                    for evidence_id in topic_output.get("supporting_evidence_ids") or ()
                                    if str(evidence_id)
                                }
                                | {
                                    str(evidence_id)
                                    for result in fragment_results
                                    for provider_output in [
                                        result.get("provider_output")
                                        if isinstance(result.get("provider_output"), Mapping)
                                        else {}
                                    ]
                                    for claim in provider_output.get("claims") or ()
                                    if isinstance(claim, Mapping)
                                    for evidence_id in claim.get("evidence_ids") or ()
                                    if str(evidence_id)
                                }
                            )
                        ],
                        "provider_batch_ids": [],
                        "provider_outputs": [],
                        "result_ids": [],
                    }
                    context_fields = [
                        dict(field)
                        for result in fragment_results
                        for field in (
                            (result.get("interpretation_context") or {}).get("fields") or ()
                        )
                        if isinstance(field, Mapping)
                    ]
                    context_dependencies = [
                        dict(dependency)
                        for result in fragment_results
                        for dependency in (
                            (result.get("interpretation_context") or {}).get("dependencies") or ()
                        )
                        if isinstance(dependency, Mapping)
                    ]
                    if context_fields or context_dependencies:
                        row["interpretation_context"] = {
                            "fields": context_fields,
                            "dependencies": context_dependencies,
                        }
                    if fragment_results:
                        scoped_result = fragment_results[0]
                        row["provider_batch_ids"] = [str(scoped_result.get("batch_id") or "")]
                        row["result_ids"] = [str(scoped_result.get("result_id") or "")]
                        row["provider_outputs"] = [
                            dict(scoped_result.get("provider_output") or {})
                        ]
                    semantic_topic_fragment_context.append(row)
            self._candidate_interpretation_tables = self._candidate_semantic_source_tables(
                semantic_topic_context
            )
            semantic_relation_candidates = [
                self._compact_relation_candidate(item.to_dict())
                for item in candidate_map_model.relations
                if str(item.relation_id) in {
                    str(value)
                    for value in (
                        semantic_chunk_plan_model.coverage.get(
                            "selected_relation_ids"
                        )
                        or ()
                    )
                    if str(value)
                }
            ]
            cross_provider_result: dict[str, Any] | None = None
            cross_semantic_reused = _semantic_node_reusable("cross_group_comparison")
            if self.semantic_provider_synthesis_enabled and not cross_semantic_reused:
                candidate_relation_payloads = semantic_relation_candidates
                cross_provider_result = self._run_bounded_semantic_provider_call(
                    "cross_group_comparison_provider",
                    {
                        "task": "substantive_cross_group_comparison",
                        "node_id": "cross_group_comparison",
                        "semantic_contract_version": "semantic-evidence-graph-v2",
                        "shared_synthesis_contract_version": SHARED_SYNTHESIS_CONTRACT_VERSION,
                        "interpretation_contract_version": INTERPRETATION_CONTRACT_VERSION,
                        "questions": list(semantic_chunk_plan_model.cross_group_questions),
                        "topic_synthesis": semantic_topic_fragment_context,
                        "relation_candidates": candidate_relation_payloads,
                        "output_contract": {
                            "comparisons": "array of comparisons; each factual conclusion has evidence_ids and paper_keys",
                            "bridge_claims": "array of claims with claim_id='synthesis:cross_group_comparison:<local-id>', topic_ids for each integrated topic, paper_key or paper_keys, evidence_ids, and optional source_claim_ids",
                            "topic_dispositions": "one row per supplied topic_id: integrated with synthesis_claim_ids bound to bridge_claims, or unresolved with reason; retain conditions, zero results, exceptions and source handles",
                            "coverage_ledger": "do not echo processed topic, fragment, result or relation ID arrays; the runtime verifies and persists their exact union from this request and the topic dispositions",
                            "unresolved_questions": "array of questions without sufficient evidence",
                        },
                    },
                    {"topic_synthesis": _hash_payload(topic_synthesis), "semantic_chunk_plan": _hash_payload(semantic_chunk_plan)},
                )
            cross_coverage_ledger = self._build_semantic_coverage_ledger(
                semantic_topic_fragment_context,
                [str(item.get("relation_id") or "") for item in semantic_relation_candidates],
                cross_provider_result,
            )
            cross_group = self._run_node("cross_group_comparison", lambda: (
                self._artifact(
                    OutlineArtifact,
                    {
                        "schema_version": "outline-cross-group-comparison/v1",
                        "semantic_contract_version": "semantic-evidence-graph-v2",
                        "interpretation_contract_version": INTERPRETATION_CONTRACT_VERSION,
                        "shared_synthesis_contract_version": SHARED_SYNTHESIS_CONTRACT_VERSION,
                        "execution_mode": "provider_synthesis" if cross_provider_result is not None else "local_question_projection",
                        "status": "completed",
                        "shared_semantic_chunk_plan_hash": semantic_chunk_plan_model.content_hash,
                        "questions": list(semantic_chunk_plan_model.cross_group_questions),
                        "relation_bundle_ids": [item.relation_id for item in semantic_chunk_plan_model.relation_bundles],
                        "deferred_relation_ids": [
                            item.relation_id
                            for item in semantic_chunk_plan_model.relation_bundles
                            if item.relation_id not in set(semantic_chunk_plan_model.coverage.get("selected_relation_ids") or ())
                        ],
                        "coverage_ledger": cross_coverage_ledger,
                        "provider_output": cross_provider_result,
                    },
                    {
                        "topic_synthesis": _hash_payload(topic_synthesis),
                        "semantic_chunk_plan": _hash_payload(semantic_chunk_plan),
                    },
                ),
                ("topic_synthesis", "semantic_chunk_plan"),
                "provider" if cross_provider_result is not None else "deterministic",
                "local",
            ))
            loaded_cross_provider_result = (
                cross_group.get("provider_output")
                if isinstance(cross_group.get("provider_output"), Mapping)
                else None
            )
            if cross_provider_result is not None:
                self._persist_semantic_provider_output(
                    "cross_group_comparison_provider",
                    cross_provider_result,
                    dependency_hashes={"topic_synthesis": _hash_payload(topic_synthesis), "semantic_chunk_plan": _hash_payload(semantic_chunk_plan)},
                )
            global_provider_result: dict[str, Any] | None = None
            global_semantic_reused = _semantic_node_reusable("global_synthesis")
            if self.semantic_provider_synthesis_enabled and not global_semantic_reused:
                global_provider_result = self._run_bounded_semantic_provider_call(
                    "global_synthesis_provider",
                    {
                        "task": "substantive_global_synthesis",
                        "node_id": "global_synthesis",
                        "semantic_contract_version": "semantic-evidence-graph-v2",
                        "interpretation_contract_version": INTERPRETATION_CONTRACT_VERSION,
                        "shared_synthesis_contract_version": SHARED_SYNTHESIS_CONTRACT_VERSION,
                        "topic_synthesis": [],
                        "cross_group_comparison": (
                            loaded_cross_provider_result
                            or cross_provider_result
                            or cross_group
                        ),
                        "cross_coverage_ledger_ref": {
                            "content_hash": str((cross_group.get("coverage_ledger") or {}).get("content_hash") or ""),
                            "topic_count": len((cross_group.get("coverage_ledger") or {}).get("topic_ids") or ()),
                        },
                        "relation_candidates": [],
                        "output_contract": {
                            "synthesis_claims": "array of claims with claim_id='synthesis:global_synthesis:<local-id>', paper_key or paper_keys, evidence_ids, and optional source_claim_ids",
                            "organizing_principles": "array",
                            "coverage_ledger": "do not echo processed ID arrays; the validated cross-topic coverage ledger remains in Registry",
                            "unresolved_questions": "array of questions without sufficient evidence",
                        },
                    },
                    {"cross_group_comparison": _hash_payload(cross_group), "relation_candidates": _hash_payload(candidate_map), "semantic_chunk_plan": _hash_payload(semantic_chunk_plan)},
                )
            global_synthesis = self._run_node("global_synthesis", lambda: (
                self._artifact(
                    OutlineArtifact,
                    {
                        "schema_version": "outline-global-synthesis/v1",
                        "semantic_contract_version": "semantic-evidence-graph-v2",
                        "interpretation_contract_version": INTERPRETATION_CONTRACT_VERSION,
                        "shared_synthesis_contract_version": SHARED_SYNTHESIS_CONTRACT_VERSION,
                        "execution_mode": "provider_synthesis" if global_provider_result is not None else "local_shared_synthesis_base",
                        "status": "completed",
                        "shared_semantic_chunk_plan_hash": semantic_chunk_plan_model.content_hash,
                        "topic_synthesis_hash": _hash_payload(topic_synthesis),
                        "cross_group_comparison_hash": _hash_payload(cross_group),
                        "cross_coverage_ledger_hash": str((cross_group.get("coverage_ledger") or {}).get("content_hash") or ""),
                        "topic_ids": [item.topic_id for item in semantic_chunk_plan_model.topics],
                        "relation_ids": [item.relation_id for item in semantic_chunk_plan_model.relation_bundles],
                        "supporting_evidence_ids": sorted({
                            evidence_id
                            for item in topic_plan
                            for evidence_id in item.supporting_evidence_ids
                        }),
                        "conclusions": ([global_provider_result] if global_provider_result is not None else ["Shared global synthesis base materialized from topic routes and evidence references; offline route retained local projection."]),
                        "unresolved_questions": list(semantic_chunk_plan_model.cross_group_questions),
                        "provider_output": global_provider_result,
                    },
                    {
                        "cross_group_comparison": _hash_payload(cross_group),
                        "relation_candidates": _hash_payload(candidate_map),
                        "semantic_chunk_plan": _hash_payload(semantic_chunk_plan),
                    },
                ),
                ("cross_group_comparison", "relation_candidates", "semantic_chunk_plan"),
                "provider" if global_provider_result is not None else "deterministic",
                "local",
            ))
            loaded_global_provider_result = (
                global_synthesis.get("provider_output")
                if isinstance(global_synthesis.get("provider_output"), Mapping)
                else None
            )
            if global_provider_result is not None:
                self._persist_semantic_provider_output(
                    "global_synthesis_provider",
                    global_provider_result,
                    dependency_hashes={"cross_group_comparison": _hash_payload(cross_group), "relation_candidates": _hash_payload(candidate_map), "semantic_chunk_plan": _hash_payload(semantic_chunk_plan)},
                )

            relation_candidates = [relation.to_dict() for relation in candidate_map_model.relations]
            relation_shard_plan_payload = self._build_relation_shard_plan(
                evidence_model.views,
                relation_candidates,
            )
            relation_shard_plan = self._run_node("relation_shard_plan", lambda: (
                self._artifact(
                    OutlineArtifact,
                    relation_shard_plan_payload,
                    {
                        "outline_evidence_views": _hash_payload(evidence),
                        "relation_candidates": _hash_payload(candidate_map),
                    },
                ),
                ("outline_evidence_views", "relation_candidates"),
                "deterministic",
                "local",
            ))
            all_candidate_by_id = {
                str(item["relation_id"]): item for item in relation_candidates
            }
            relation_request, selected_relation_candidates, excluded_relation_ids = (
                self._relation_provider_request(
                    relation_candidates=relation_candidates,
                    content_layers=content_layers_model,
                    semantic_plan=semantic_chunk_plan_model,
                    shard_plan=relation_shard_plan_payload,
                )
            )
            selected_relation_ids = {
                str(item["relation_id"]) for item in selected_relation_candidates
            }
            relation_deps = {
                "relation_candidates": _hash_payload(candidate_map),
                "outline_evidence_views": _hash_payload(evidence),
                "relation_shard_plan": _hash_payload(relation_shard_plan),
                "semantic_chunk_plan": _hash_payload(semantic_chunk_plan),
            }
            relation_route = self._role_route("relation_adjudication")
            relation_budget = relation_route.profile.estimate_request(
                self._attach_prompt_authority("relation_adjudication", relation_request)
            )
            relation_estimated_input = int(
                relation_budget.get("estimated_input_tokens")
                or relation_route.profile.estimate_tokens(relation_request)
            )
            relation_target = self._relation_packing_target(relation_route.profile)
            use_hierarchical_relations = (
                bool(selected_relation_ids)
                and (
                    relation_estimated_input > relation_target
                    or relation_estimated_input > self._effective_input_cap(relation_route.profile)
                    or not bool(relation_budget.get("within_budget"))
                )
                and not (
                    self.enabled_semantic_roles is not None
                    and "relation_adjudication" not in self.enabled_semantic_roles
                )
            )
            hierarchical_content: dict[str, Any] | None = None
            relation_shard_digests: list[dict[str, Any]] = []
            if not selected_relation_ids:
                hierarchical_content = {
                    "confirmed_relation_ids": [],
                    "rejected_relations": [],
                    "relation_decisions": [],
                    "selection_status": "explicit_empty",
                }
            elif use_hierarchical_relations:
                hierarchical_content, relation_shard_digests = (
                    self._run_hierarchical_relation_adjudication(
                        evidence_views=evidence_model.views,
                        relation_candidates=selected_relation_candidates,
                        shard_plan=relation_shard_plan_payload,
                        relation_contract=relation_request["relation_adjudication_contract"],
                        relation_dependencies=relation_deps,
                        relation_bundles={
                            item.relation_id: item.to_dict()
                            for item in semantic_chunk_plan_model.relation_bundles
                            if item.relation_id in selected_relation_ids
                        },
                        compact_request=relation_request,
                    )
                )
                # The static base relation node is replaced by the dynamic
                # local/cross-shard provider calls.  Remove its hydrated
                # expectation so receipt closure does not report a provider
                # call that was intentionally never sent.
                self._expected_provider_calls.pop(
                    self._provider_call_id("relation_adjudication"),
                    None,
                )
            shard_digest_record = self._run_node("relation_shard_digests", lambda: (
                self._artifact(
                    OutlineArtifact,
                    {
                        "schema_version": "outline-relation-shard-digests-v1",
                        "hierarchical": use_hierarchical_relations,
                        "digests": relation_shard_digests,
                    },
                    {"relation_shard_plan": _hash_payload(relation_shard_plan)},
                ),
                ("relation_shard_plan",),
                "deterministic",
                "local",
            ))
            relation_deps["relation_shard_digests"] = _hash_payload(shard_digest_record)
            if hierarchical_content is not None:
                adjudication = self._run_node(
                    "relation_adjudication",
                    lambda: (
                        self._artifact(
                            RelationAdjudicationResult,
                            hierarchical_content,
                            relation_deps,
                        ),
                        ("relation_candidates", "relation_shard_plan", "relation_shard_digests"),
                        "hierarchical",
                        "local",
                    ),
                )
            elif (
                self.enabled_semantic_roles is not None
                and "relation_adjudication" not in self.enabled_semantic_roles
            ):
                raise OutlineV3ExecutionError(
                    "BLOCKED_ROUTE: selected relations require the relation_adjudication role"
                )
            else:
                adjudication = self._run_node(
                    "relation_adjudication",
                    lambda: self._run_provider_node(
                        "relation_adjudication", relation_request, RelationAdjudicationResult, relation_deps,
                    ),
                    expected_binding=self._provider_binding(
                        "relation_adjudication", relation_request, expect_json=True,
                        input_artifact_hashes=tuple(relation_deps.values()),
                    ),
                )
            if hierarchical_content is not None:
                # ``_run_node`` hydrates the static base binding for the local
                # artifact wrapper.  The actual provider work was already
                # represented by the dynamic shard calls, so the static
                # expectation must not survive into receipt closure.
                self._expected_provider_calls.pop(
                    self._provider_call_id("relation_adjudication"),
                    None,
                )
            if not isinstance(adjudication.get("confirmed_relation_ids"), list) or not isinstance(adjudication.get("rejected_relations"), list):
                raise OutlineV3ExecutionError("relation adjudication must return explicit confirmed and rejected lists")
            if len(all_candidate_by_id) != len(relation_candidates):
                raise OutlineV3ExecutionError("relation candidates contain duplicate relation ids")
            confirmed_ids = [str(item).strip() for item in adjudication["confirmed_relation_ids"] if str(item).strip()]
            rejected_payload = [item for item in adjudication["rejected_relations"] if isinstance(item, Mapping)]
            rejected_ids = [str(raw.get("relation_id") or "").strip() for raw in rejected_payload]
            deferred_relation_ids = list(excluded_relation_ids)
            raw_decisions = [
                item for item in adjudication.get("relation_decisions") or ()
                if isinstance(item, Mapping) and str(item.get("relation_id") or "").strip()
            ]
            decision_by_id: dict[str, dict[str, Any]] = {
                str(item.get("relation_id") or "").strip(): {
                    "relation_id": str(item.get("relation_id") or "").strip(),
                    "decision": str(item.get("decision") or item.get("status") or "rejected").strip().lower(),
                    "status": str(item.get("status") or item.get("decision") or "rejected").strip().lower(),
                    "reason": str(item.get("reason") or ""),
                    "evidence_ids": [str(value) for value in item.get("evidence_ids") or () if str(value)],
                    "missing_evidence_ids": [str(value) for value in item.get("missing_evidence_ids") or () if str(value)],
                }
                for item in raw_decisions
            }
            # Older provider envelopes have no explicit decision records.  A
            # rejected item remains a rejected decision, while excluded
            # candidates are explicitly deferred instead of being relabelled
            # as rejected.
            for item in rejected_payload:
                relation_id = str(item.get("relation_id") or "").strip()
                decision_by_id.setdefault(
                    relation_id,
                    {
                        "relation_id": relation_id,
                        "decision": str(item.get("decision") or item.get("status") or "rejected").strip().lower(),
                        "status": str(item.get("status") or item.get("decision") or "rejected").strip().lower(),
                        "reason": str(item.get("reason") or ""),
                        "evidence_ids": [str(value) for value in item.get("evidence_ids") or () if str(value)],
                        "missing_evidence_ids": [str(value) for value in item.get("missing_evidence_ids") or () if str(value)],
                    },
                )
            for relation_id in deferred_relation_ids:
                decision_by_id.setdefault(
                    relation_id,
                    {
                        "relation_id": relation_id,
                        "decision": "deferred",
                        "status": "deferred",
                        "reason": "deferred_for_directed_evidence_retrieval",
                        "evidence_ids": [],
                        "missing_evidence_ids": [],
                    },
                )
            selected_id_set = set(selected_relation_ids)
            bundle_by_id = {
                item.relation_id: item
                for item in semantic_chunk_plan_model.relation_bundles
            }
            incomplete_confirmed = sorted(
                relation_id
                for relation_id in confirmed_ids
                if relation_id in bundle_by_id and not bundle_by_id[relation_id].is_complete
            )
            if incomplete_confirmed:
                raise OutlineV3ExecutionError(
                    "relation adjudication confirmed relations with incomplete "
                    f"evidence bundles: {incomplete_confirmed}"
                )
            if len(confirmed_ids) != len(set(confirmed_ids)):
                raise OutlineV3ExecutionError("relation adjudication confirmed a relation more than once")
            if len(rejected_ids) != len(set(rejected_ids)):
                raise OutlineV3ExecutionError("relation adjudication rejected a relation more than once")
            if any(item not in selected_id_set for item in confirmed_ids):
                raise OutlineV3ExecutionError("relation adjudication confirmed an unknown relation")
            if any(item not in selected_id_set for item in rejected_ids):
                raise OutlineV3ExecutionError("relation adjudication rejected an unknown relation")
            if set(confirmed_ids) & set(rejected_ids):
                raise OutlineV3ExecutionError("relation adjudication both confirmed and rejected a relation")
            if set(confirmed_ids) | set(rejected_ids) != selected_id_set:
                raise OutlineV3ExecutionError("relation adjudication did not classify every selected relation")
            confirmed = [all_candidate_by_id[item] for item in confirmed_ids]
            rejected = [all_candidate_by_id[item] for item in rejected_ids]
            confirmed_map = self._run_node("global_relation_map", lambda: (
                self._artifact(ConfirmedGlobalRelationMap, {"relations": confirmed, "rejected_relations": rejected, "deferred_relations": [{"relation_id": item, "reason": "deferred_for_directed_evidence_retrieval", "decision": "deferred", "status": "deferred"} for item in deferred_relation_ids], "relation_decisions": [decision_by_id[item] for item in all_candidate_by_id if item in decision_by_id], "confirmed_relation_ids": confirmed_ids, "rejected_relation_ids": rejected_ids, "deferred_relation_ids": deferred_relation_ids, "paper_keys": sorted({key for item in confirmed for key in item.get("paper_keys", [])}), "source_artifact_hashes": {"relation_candidates": _hash_payload(candidate_map), "semantic_chunk_plan": _hash_payload(semantic_chunk_plan)}, "blocking_diagnostics": []}, {"relation_adjudication": _hash_payload(adjudication), "semantic_chunk_plan": _hash_payload(semantic_chunk_plan)}),
                ("relation_adjudication", "relation_candidates", "semantic_chunk_plan"), self.profile.model, self.profile.provider,
            ))

            if self.semantic_provider_synthesis_enabled:
                from outline.candidate_output_scope import build_candidate_output_scope_v1
                from services.writer_source_inventory import load_writer_source_inventory_v1

                source_inventory = load_writer_source_inventory_v1(self.registry)
                task_ids_by_topic = {
                    topic.topic_id: topic.logical_node_id for topic in semantic_chunk_plan_model.topics
                }
                scoped_routes = [
                    {**route, "logical_node_id": task_ids_by_topic[str(route.get("topic_id") or "")]}
                    for route in semantic_topic_context
                ]
                self._candidate_output_scope = build_candidate_output_scope_v1(
                    source_inventory, scoped_routes, selected_relation_ids=confirmed_ids,
                    bridge_claims=[
                        {"task_id": node, "result_id": self.artifact_records[node].artifact_id,
                         "provider_output": output}
                        for node, output in (
                            ("cross_group_comparison", loaded_cross_provider_result or cross_provider_result),
                            ("global_synthesis", loaded_global_provider_result or global_provider_result),
                        )
                        if isinstance(output, Mapping)
                    ],
                )
                scope_record = publish_json_artifact(
                    self.publication_context, self.registry,
                    self._path(f"outline_v3/candidate_output_scope_{self._candidate_output_scope.content_hash}.json"),
                    self._candidate_output_scope.to_dict(),
                    artifact_id="outline-v3:candidate_output_scope",
                    artifact_role="outline_candidate_output_scope",
                    artifact_type="outline_candidate_output_scope", artifact_version="v1",
                    producer="outline.v3_executor.OutlineV3Executor",
                    depends_on=[ArtifactDependencyRefV2.from_record(self.artifact_records[node]) for node in (
                        "outline_content_layers", "semantic_chunk_plan", "topic_synthesis",
                        "cross_group_comparison", "global_synthesis",
                    )],
                )
                self.artifact_records["candidate_output_scope"] = scope_record
                self.artifact_paths["candidate_output_scope"] = scope_record.path

            intent_model = build_review_intent(self.review_intent_input)
            intent = self._run_node("review_intent", lambda: (
                self._artifact(OutlineArtifact, intent_model.to_dict(), {}), (), "deterministic", "local",
            ))
            contract_model = build_coverage_contract(ledger_model, intent_model)
            contract = self._run_node("coverage_contract", lambda: (
                self._artifact(OutlineArtifact, contract_model.to_dict(), {"global_corpus_ledger": _hash_payload(ledger), "review_intent": _hash_payload(intent)}),
                ("global_corpus_ledger", "review_intent"), "deterministic", "local",
            ))
            confirmed_map_model = GlobalRelationMap(
                artifact_type="confirmed_global_relation_map",
                relations=[candidate_map_model.relations[index] for index, item in enumerate(relation_candidates) if item["relation_id"] in set(confirmed_ids)],
                paper_keys=sorted({key for item in confirmed for key in item.get("paper_keys", [])}),
                source_artifact_hashes={"relation_candidates": _hash_payload(candidate_map)},
            )
            plans_model = build_outline_candidate_plans(
                ledger_model,
                matrix_model,
                confirmed_map_model,
                intent_model,
                contract_model,
                candidate_count=self.candidate_count,
                semantic_chunk_plan_hash=semantic_chunk_plan_model.content_hash,
            )
            axes = plans_model.axes
            axes_by_id = {axis.axis_id: axis for axis in axes}
            axes_payload = {"axes": [item.to_dict() for item in axes], "candidates": [item.to_dict() for item in plans_model.candidates], "semantic_chunk_plan_hash": semantic_chunk_plan_model.content_hash, "global_synthesis_hash": _hash_payload(global_synthesis), "topic_routes": [item.to_dict() for item in semantic_chunk_plan_model.topics], "bridge_pass": [{"type": "cross_stream_bridge", "paper_keys": sorted(item.paper_keys)} for item in confirmed_map_model.relations if item.relation_type == "bridge_between_topics"]}
            axes_out = self._run_node("organizing_axes", lambda: (
                self._artifact(OutlineArtifact, axes_payload, {"global_corpus_ledger": _hash_payload(ledger), "multi_view_matrix": _hash_payload(matrix), "global_relation_map": _hash_payload(confirmed_map), "semantic_chunk_plan": _hash_payload(semantic_chunk_plan), "global_synthesis": _hash_payload(global_synthesis), "review_intent": _hash_payload(intent), "coverage_contract": _hash_payload(contract)}),
                ("global_corpus_ledger", "multi_view_matrix", "global_relation_map", "semantic_chunk_plan", "global_synthesis", "review_intent", "coverage_contract"), "deterministic", "local",
            ))

            candidate_ids: list[str] = []
            primary_candidate_requests: dict[str, dict[str, Any]] = {}
            for index, plan in enumerate(plans_model.candidates, start=1):
                if self.semantic_provider_synthesis_enabled and self._candidate_output_scope is None:
                    raise OutlineV3ExecutionError("candidate generation lacks its source-bound finite output scope")
                candidate_id = f"candidate_{index}"
                candidate_ids.append(candidate_id)
                plan_payload = plan.to_dict()
                self._run_node(candidate_id, lambda payload=plan_payload: (
                    self._artifact(OutlineCandidate, payload, {"organizing_axes": _hash_payload(axes_out), "global_relation_map": _hash_payload(confirmed_map), "global_synthesis": _hash_payload(global_synthesis), "coverage_contract": _hash_payload(contract)}),
                    ("organizing_axes", "global_relation_map", "global_synthesis", "coverage_contract"), "deterministic", "local",
                ))
                # Provider-visible identity order is canonical so summary
                # permutation stability does not manufacture different claim
                # sequences or replay keys.
                paper_keys = sorted(
                    str(item.paper_key)
                    for item in ledger_model.entries
                    if str(item.paper_key)
                )
                allowed_relation_ids = [item.relation_id for item in confirmed_map_model.relations]
                candidate_evidence = self._compact_candidate_evidence_refs(
                    [
                        view
                        for view in evidence_model.views
                        if view.paper_key in set(paper_keys)
                    ],
                    content_layers_model,
                    semantic_chunk_plan_model,
                )
                candidate_relations = [
                    self._compact_relation_candidate(item.to_dict())
                    for item in confirmed_map_model.relations
                    if set(item.paper_keys).issubset(set(paper_keys))
                ]
                request = {
                    "candidate_id": candidate_id,
                    "organizing_logic": plan.organizing_logic,
                    **({"candidate_output_scope": self._candidate_output_scope_wire(paper_keys)}
                       if self._candidate_output_scope is not None else {}),
                    **({"organizing_axis": axes_by_id[plan.axis_id].to_dict()}
                       if "_then_" in plan.axis_id else {}),
                    "paper_keys": paper_keys,
                    "relation_ids": allowed_relation_ids,
                    "relations": candidate_relations,
                    "evidence": candidate_evidence,
                    "evidence_projection": {
                        "projection": "registry_complete_evidence_ref_v1",
                        "full_evidence_artifact_type": "outline_content_layers",
                        "full_evidence_artifact_hash": content_layers_model.content_hash,
                        "shared_semantic_context": "global_synthesis_and_topic_routes",
                    },
                    "semantic_chunk_plan": {
                        "content_layers_hash": semantic_chunk_plan_model.content_layers_hash,
                        "topic_routes": [
                            {
                                "topic_id": item.topic_id,
                                "dimensions": item.dimensions,
                                "paper_count": len(item.paper_ids),
                                "bridge_paper_count": len(item.bridge_paper_ids),
                                "paper_ids_hash": hash_json(item.paper_ids),
                                "required_evidence_count": len(item.required_evidence_ids),
                                "status": item.status,
                            }
                            for item in semantic_chunk_plan_model.topics
                        ],
                        "relation_summaries": [
                            {
                                "relation_id": item.relation_id,
                                "relation_type": item.relation_type,
                                "paper_count": len(item.paper_ids),
                                "paper_ids_hash": hash_json(item.paper_ids),
                                "evidence_completeness": item.evidence_completeness,
                                "decision": item.decision,
                                "missing_evidence_count": len(item.missing_evidence_ids),
                            }
                            for item in semantic_chunk_plan_model.relation_bundles
                            if item.relation_id in set(
                                str(value)
                                for value in (semantic_chunk_plan_model.coverage.get("selected_relation_ids") or ())
                            )
                        ],
                        "deferred_relation_count": semantic_chunk_plan_model.coverage.get("unselected_relation_count", 0),
                        "deferred_relation_policy": "directed_evidence_retrieval",
                        "cross_group_questions": list(semantic_chunk_plan_model.cross_group_questions),
                    },
                    "content_layer_refs": {
                        "artifact_type": "outline_content_layers",
                        "artifact_hash": content_layers_model.content_hash,
                        "dossier_ids": [
                            f"dossier:{paper_key}" for paper_key in paper_keys
                        ],
                    },
                    "source_summary_hashes": sorted(evidence_model.source_summary_hashes),
                    "shared_hashes": plan.shared_artifact_hashes,
                    "global_synthesis_hash": _hash_payload(global_synthesis),
                    "shared_semantic_context": {
                        "interpretation_source_tables": self._candidate_interpretation_tables,
                        "interpretation_source_contract": (
                            "Resolve each topic fragment's source_field_ids and dependency_ids "
                            "against the full source_fields and dependencies below before "
                            "planning a factual claim; preserve paper/study scope and unresolved limits."
                        ),
                        "planned_evidence_policy": (
                            "Topic planned_evidence_ids describe navigation coverage, not a finding. "
                            "Use the actual topic provider claims/support and the visible interpretation "
                            "source table for any factual section claim."
                        ),
                        "global_synthesis": self._compact_semantic_provider_result_for_candidate(
                            loaded_global_provider_result
                            or global_provider_result
                            or {
                                "execution_mode": "local_shared_synthesis_base",
                                "topic_count": len(semantic_chunk_plan_model.topics),
                                "content_layers_hash": content_layers_model.content_hash,
                            }
                        ),
                        "cross_group_comparison": self._compact_semantic_provider_result_for_candidate(
                            loaded_cross_provider_result
                            or cross_provider_result
                            or cross_group
                        ),
                        "topic_routes": self._compact_semantic_topic_routes_for_candidate(
                            semantic_topic_context
                        ),
                    },
                    "output_contract": {
                        **({"finite_scope_rule": (
                            "Use only the supplied candidate_output_scope claim slots. "
                            "Every section lists task_ids; every support row echoes its claim_slot_id and task_id "
                            "with complete source/qualifier IDs. Consume a slot at most once. "
                            "Choose claims with organizing value; unused slots remain available and need not become prose. "
                            "A multi-paper claim group must retain all of its support slots in one section. "
                            "Slots with the same claim_group_index belong to one assertion group. "
                            "Attach only relations recorded on the consumed groups, and leave unrelated relation judgments in their ledger. "
                            "Do not exceed max_sections, max_claims or max_support_rows."
                        )} if self._candidate_output_scope is not None else {}),
                        "output_fields": {
                            "candidate_id": (
                                "string; echo the candidate_id from this request verbatim"
                            ),
                            "sections": (
                                "non-empty array of outline sections; each section is an "
                                "object with section_id, title, goal (one short sentence "
                                "stating the section's purpose in the review), paper_keys "
                                "(subset of the provided evidence paper_keys), relation_ids "
                                "(subset of provided relation_ids), claims (non-empty array "
                                "of planned claim strings) and rationale. Every planned claim "
                                "must include claim_support with its exact claim text, paper_key, "
                                "source_claim_ids, evidence_ids and complete qualifier source_field_ids "
                                "from the supplied shared semantic context. Include study_id only "
                                "when the supplied sources verify that study; do not invent a study "
                                "identity for paper-level fallback. Preserve all primary and required "
                                "qualifier provenance so the Writer can bind the same assertion."
                            ),
                        },
                        "paper_keys_are_the_only_allowed_keys": (
                            "Every entry in every section's paper_keys must come "
                            "verbatim from the paper_keys array in this request. "
                            "Never add, invent, or abbreviate a paper key."
                        ),
                        "no_external_evidence": (
                            "Do not cite, attribute evidence to, or structurally "
                            "rely on any work outside the provided evidence corpus "
                            "in this request. Works mentioned inside a source "
                            "summary (e.g. prior literature reported by the "
                            "reviewed paper) are background context reported by "
                            "that source; they are NOT independent sources of this "
                            "review and must never appear as paper_keys, section "
                            "titles, or standalone citation identities. If a claim "
                            "needs such context, phrase it only as 'the included "
                            "paper discusses prior work on X' without creating a "
                            "new citation or evidence identity."
                        ),
                        "relation_ids_are_the_only_allowed_relation_ids": (
                            "Every entry in every section's relation_ids must come "
                            "verbatim from the relation_ids array in this request. "
                            "Never invent, rename, merge, or re-derive a relation "
                            "id; if a section needs a connection that is not in the "
                            "provided list, leave relation_ids out of that section."
                        ),
                        "sections": "non_empty",
                        "section_paper_keys_must_be_subset_of_evidence": True,
                        "section_relation_ids_must_be_subset_of_relation_ids": True,
                        "planned_claims_must_be_non_empty": True,
                        "planned_claims_require_source_support": True,
                        "explicit_study_claims_require_scoped_support": (
                            "A planned claim naming Study/Experiment/Trial S1 or 1 must "
                            "include a claim_support row with the exact claim text and all "
                            "primary/qualifier source claim, evidence, and field IDs for "
                            "the same paper and study. Otherwise express it as unresolved."
                        ),
                        "paper_keys_may_repeat_with_distinct_roles": (
                            "A paper may support more than one section when each "
                            "occurrence carries a non-empty paper_roles mapping and "
                            "the role/claim is materially distinct.  Do not repeat a "
                            "paper merely to inflate coverage or copy the same claim."
                        ),
                        "claim_must_be_supported_by_section_evidence": (
                            "Every planned claim inside a section must be directly "
                            "supported by the Stage 1 evidence of that section's "
                            "paper_keys.  Never attach a claim to a paper whose "
                            "recorded evidence does not contain the construct, "
                            "variable, method, or result the claim relies on.  If "
                            "the evidence does not support a claim, remove the claim "
                            "or move it to the section whose paper evidence does "
                            "support it."
                        ),
                    },
                }
                primary_candidate_requests[candidate_id] = copy.deepcopy(request)
                alias_map = (
                    self._alias_map_for(paper_keys, allowed_relation_ids)
                    if self._alias_enabled
                    else None
                )
                provider_request = (
                    alias_structural(request, alias_map)
                    if alias_map is not None
                    else request
                )
                generation_deps = {
                    "candidate": _hash_payload(provider_request),
                    **({"candidate_output_scope": self._candidate_output_scope.content_hash}
                       if self._candidate_output_scope is not None else {}),
                    "global_relation_map": _hash_payload(confirmed_map),
                    "coverage_contract": _hash_payload(contract),
                    "semantic_chunk_plan": _hash_payload(semantic_chunk_plan),
                    "outline_content_layers": _hash_payload(content_layers),
                }
                generation_node_id = f"{candidate_id}_provider_generation"
                generation_binding = self._provider_binding(
                    generation_node_id, provider_request, expect_json=True,
                    input_artifact_hashes=tuple(generation_deps.values()),
                )
                loaded_generation = self._load_node(
                    generation_node_id,
                    generation_binding,
                )
                if loaded_generation is not None:
                    self._validate_candidate_payload(
                        candidate_id, loaded_generation, allowed_paper_keys=paper_keys,
                        allowed_relation_ids=allowed_relation_ids, alias_map=alias_map,
                    )
                    continue
                generation_route = self._node_route(generation_node_id)
                generation_budget = generation_route.profile.estimate_request(
                    self._attach_prompt_authority(generation_node_id, provider_request)
                )
                sharded_generation = bool(
                    int(generation_budget.get("estimated_input_tokens") or 0)
                    > self._relation_packing_target(generation_route.profile)
                    or not bool(generation_budget.get("within_budget"))
                )
                # Transport first (registers the expected call + receipt), then
                # validate, then bounded repair, then persist ONLY the adopted
                # canonical content.  Persisting before validation would give a
                # second executor replay a poisoned candidate artifact.
                try:
                    self._check(generation_node_id)
                    if sharded_generation:
                        raw_generation = self._run_hierarchical_candidate_generation(
                            candidate_id=candidate_id,
                            generation_node_id=generation_node_id,
                            provider_request=provider_request,
                            evidence_views=evidence_model.views,
                            relation_candidates=candidate_relations,
                            allowed_paper_keys=paper_keys,
                            allowed_relation_ids=allowed_relation_ids,
                            generation_deps=generation_deps,
                            alias_map=alias_map,
                        )
                        # The static candidate provider node is represented by
                        # the merged local shard calls in receipt closure.
                        self._expected_provider_calls.pop(
                            self._provider_call_id(generation_node_id),
                            None,
                        )
                    else:
                        raw_generation = self._provider_call(
                            generation_node_id,
                            provider_request,
                            expect_json=True,
                            input_artifact_hashes=tuple(generation_deps.values()),
                            output_tokens=min(
                                int(generation_route.profile.max_output_tokens),
                                4_096,
                            ),
                        )
                    content = (
                        canonicalize_structural(dict(raw_generation), alias_map)
                        if alias_map is not None
                        else dict(raw_generation)
                    )
                    try:
                        self._validate_candidate_payload(
                            candidate_id,
                            content,
                            allowed_paper_keys=paper_keys,
                            allowed_relation_ids=allowed_relation_ids,
                            alias_map=alias_map,
                        )
                    except OutlineV3ExecutionError as contract_error:
                        if not self._repair_enabled:
                            raise
                        content = self._semantic_repair_candidate(
                            candidate_id,
                            content,
                            contract_error,
                            allowed_paper_keys=paper_keys,
                            allowed_relation_ids=allowed_relation_ids,
                            alias_map=alias_map,
                        )
                        try:
                            self._validate_candidate_payload(
                                candidate_id,
                                content,
                                allowed_paper_keys=paper_keys,
                                allowed_relation_ids=allowed_relation_ids,
                                alias_map=alias_map,
                            )
                        except OutlineV3ExecutionError as repair_failure:
                            self._publish_repair_failure(candidate_id, repair_failure)
                            raise
                    self._persist(
                        generation_node_id,
                        self._artifact(OutlineCandidate, content, generation_deps),
                        depends_on=tuple(generation_deps.values()),
                        model=generation_route.model,
                        provider=generation_route.provider_name,
                        execution_binding=generation_binding,
                    )
                except Exception as exc:
                    # Mirror _run_node failure semantics: the failed candidate
                    # generation node must remain visible in the durable DAG so
                    # resume reruns this node and its descendants.
                    try:
                        self._dag = self._node_store.record_node(
                            generation_node_id,
                            status="failed",
                            input_hash=_hash_payload(dict(generation_binding.get("dependency_hashes") or {})),
                            output_hash="",
                            output_artifact_ids=(),
                            model_route=str(generation_binding.get("provider_route") or ""),
                            model_name=str(generation_binding.get("model_name") or ""),
                            provider=str(generation_binding.get("provider_family") or ""),
                            config_snapshot={"candidate_count": self.candidate_count},
                            budget_snapshot={"input_budget": self.profile.input_budget},
                            receipt_ids=tuple(self.receipts),
                            diagnostics=(f"{type(exc).__name__}: {exc}",),
                            execution_binding=generation_binding,
                        )
                    except Exception as record_error:
                        self.diagnostics.append(
                            f"failed node {generation_node_id} could not be persisted: {type(record_error).__name__}: {record_error}"
                        )
                    raise

            generation_hashes = {candidate_id: _hash_payload(self._payloads.get(f"{candidate_id}_provider_generation", {})) for candidate_id in candidate_ids}
            generation_binding_hashes = {
                candidate_id: self._node_execution_identity_hash(
                    f"{candidate_id}_provider_generation"
                )
                for candidate_id in candidate_ids
            }
            candidate_contents = {
                candidate_id: {
                    "candidate_id": candidate_id,
                    "organizing_logic": str(self._payloads.get(candidate_id, {}).get("organizing_logic") or ""),
                    "sections": list(self._payloads.get(f"{candidate_id}_provider_generation", {}).get("sections") or []),
                    "planned_claims": list(
                        self._payloads.get(f"{candidate_id}_provider_generation", {}).get("claims")
                        or [
                            claim
                            for section in self._payloads.get(
                                f"{candidate_id}_provider_generation", {}
                            ).get("sections", [])
                            if isinstance(section, Mapping)
                            for claim in section.get("claims") or ()
                            if str(claim).strip()
                        ]
                    ),
                    "paper_assignments": [
                        {
                            "section_id": str(section.get("section_id") or ""),
                            "paper_keys": list(section.get("paper_keys") or []),
                        }
                        for section in self._payloads.get(f"{candidate_id}_provider_generation", {}).get("sections", [])
                        if isinstance(section, Mapping)
                    ],
                }
                for candidate_id in candidate_ids
            }
            critiques: dict[str, dict[str, Any]] = {}
            from outline.evidence_alias import build_alias_map

            critique_alias_map = getattr(self, "_alias_map", None)
            if not isinstance(critique_alias_map, Mapping):
                critique_alias_map = build_alias_map(
                    list(contract_model.corpus_paper_keys),
                    [relation.relation_id for relation in confirmed_map_model.relations],
                )
            paper_alias_reverse = dict(critique_alias_map.get("papers_reverse") or {})
            for candidate_content in candidate_contents.values():
                normalized_sections: list[dict[str, Any]] = []
                for raw_section in candidate_content.get("sections") or ():
                    if not isinstance(raw_section, Mapping):
                        continue
                    section = dict(raw_section)
                    raw_roles = section.get("paper_roles")
                    if isinstance(raw_roles, Mapping):
                        section["paper_roles"] = {
                            paper_alias_reverse.get(str(key), str(key)): value
                            for key, value in raw_roles.items()
                        }
                    normalized_sections.append(section)
                candidate_content["sections"] = normalized_sections
            critique_requests = {
                "structure_critique": {
                    "node_id": "structure_critique",
                    "candidate_contents": candidate_contents,
                    "candidate_hashes": generation_hashes,
                    "paper_key_aliases": critique_alias_map,
                    "paper_reuse_policy": (
                        "A paper may be used in multiple sections when each occurrence "
                        "has a distinct paper role and materially distinct function. "
                        "Count unique canonical paper keys for coverage; flag only "
                        "mechanical duplication without distinct evidence function."
                    ),
                    "review_intent": intent_model.to_dict(),
                    "checks": ["section_progression", "duplicate_assignments", "goal_claim_alignment", "placeholder_sections", "empty_research_streams"],
                },
                "coverage_critique": {
                    "node_id": "coverage_critique",
                    "candidate_contents": candidate_contents,
                    "candidate_hashes": generation_hashes,
                    "paper_key_aliases": critique_alias_map,
                    "paper_reuse_policy": (
                        "Repeated use is allowed when roles/functions differ; repeated "
                        "occurrences do not create new unique-paper coverage."
                    ),
                    "coverage_contract": {
                        "content_hash": contract_model.content_hash,
                        "required_paper_count": len(contract_model.corpus_paper_keys),
                        "must_use_paper_count": len(contract_model.must_use_paper_keys),
                        "must_use_paper_keys_hash": hash_json(
                            contract_model.must_use_paper_keys
                        ),
                        "projection": "registry_complete_coverage_contract_ref_v1",
                    },
                    "corpus_ledger": {
                        "artifact_type": ledger_model.artifact_type,
                        "content_hash": ledger_model.content_hash,
                        "paper_keys": [
                            str(item.paper_key)
                            for item in ledger_model.entries
                            if str(item.paper_key)
                        ],
                        "entry_count": len(ledger_model.entries),
                        "assignment_status_counts": {
                            status: sum(
                                1
                                for item in ledger_model.entries
                                if item.assignment_status == status
                            )
                            for status in sorted(
                                {
                                    str(item.assignment_status)
                                    for item in ledger_model.entries
                                }
                            )
                        },
                        "projection": "registry_complete_ledger_ref_v1",
                    },
                    "must_use_paper_keys": list(contract_model.must_use_paper_keys),
                    "relations": [
                        self._compact_relation_candidate(item.to_dict())
                        for item in confirmed_map_model.relations
                    ],
                    "contradictions": [
                        self._compact_relation_candidate(item.to_dict())
                        for item in confirmed_map_model.relations
                        if item.relation_type in {"contradicts", "explains_discrepancy"}
                    ],
                    "gaps": [
                        self._compact_relation_candidate(item.to_dict())
                        for item in confirmed_map_model.relations
                        if item.relation_type in {"qualifies", "explains_discrepancy"}
                    ],
                    "methods": {
                        "count": sum(len(view.method) for view in evidence_model.views),
                        "values_hash": hash_json(
                            sorted({value for view in evidence_model.views for value in view.method})
                        ),
                    },
                    "contexts": {
                        "count": sum(
                            len(view.sample_or_context) for view in evidence_model.views
                        ),
                        "values_hash": hash_json(
                            sorted(
                                {
                                    value
                                    for view in evidence_model.views
                                    for value in view.sample_or_context
                                }
                            )
                        ),
                    },
                },
                "evidence_critique": {
                    "node_id": "evidence_critique",
                    "candidate_contents": candidate_contents,
                    "candidate_hashes": generation_hashes,
                    "paper_key_aliases": critique_alias_map,
                    "paper_reuse_policy": (
                        "Repeated use is allowed when each section claim has a distinct "
                        "evidence function; evaluate traceability through paper_key_aliases."
                    ),
                    "candidate_claims": {key: value.get("planned_claims", []) for key, value in candidate_contents.items()},
                    "section_evidence": {key: value.get("sections", []) for key, value in candidate_contents.items()},
                    "paper_keys": sorted(contract_model.corpus_paper_keys),
                    "source_summary_hashes": sorted(evidence_model.source_summary_hashes),
                    "relation_evidence": [item.to_dict() for item in confirmed_map_model.relations],
                    "contradictions": [item.to_dict() for item in confirmed_map_model.relations if item.relation_type in {"contradicts", "explains_discrepancy"}],
                    "boundaries": self._compact_candidate_evidence_refs(
                        [view for view in evidence_model.views if view.limitations],
                        content_layers_model,
                        semantic_chunk_plan_model,
                    ),
                    "gaps": self._compact_candidate_evidence_refs(
                        [
                            view
                            for view in evidence_model.views
                            if view.research_gaps or view.future_directions
                        ],
                        content_layers_model,
                        semantic_chunk_plan_model,
                    ),
                },
            }
            for node_id, cls in (("structure_critique", StructureCritique), ("coverage_critique", CoverageCritique), ("evidence_critique", EvidenceCritique)):
                request = critique_requests[node_id]
                critique_deps = {"candidate_generations": _hash_payload(generation_binding_hashes), "coverage_contract": _hash_payload(contract)}
                if (
                    self.enabled_semantic_roles is not None
                    and node_id not in self.enabled_semantic_roles
                ):
                    critiques[node_id] = self._run_node(
                        node_id,
                        lambda cls=cls, node_id=node_id, critique_deps=critique_deps: (
                            self._artifact(
                                cls,
                                {
                                    "node_id": node_id,
                                    "disabled_by_route_plan": True,
                                    "blocking_diagnostics": [],
                                    "coverage_metrics": {},
                                    "evidence_metrics": {},
                                    "structure_metrics": {},
                                },
                                critique_deps,
                            ),
                            ("candidate_generations",),
                            "deterministic",
                            "local",
                        ),
                    )
                else:
                    critique_route = self._node_route(node_id)
                    critique_budget = critique_route.profile.estimate_request(
                        self._attach_prompt_authority(node_id, request)
                    )
                    shard_critique = bool(
                        int(critique_budget.get("estimated_input_tokens") or 0)
                        > self._relation_packing_target(critique_route.profile)
                        or not bool(critique_budget.get("within_budget"))
                    )
                    critique_binding = self._provider_binding(
                        node_id,
                        request,
                        expect_json=True,
                        input_artifact_hashes=tuple(critique_deps.values()),
                    )
                    if shard_critique:
                        merged_critique = self._run_hierarchical_critique(
                            node_id=node_id,
                            request=request,
                            dependency_hashes=critique_deps,
                        )
                        critiques[node_id] = self._run_node(
                            node_id,
                            lambda merged=merged_critique, cls=cls, critique_deps=critique_deps: (
                                self._artifact(cls, merged, critique_deps),
                                ("candidate_generations",),
                                "hierarchical",
                                "local",
                            ),
                            expected_binding=critique_binding,
                        )
                        self._expected_provider_calls.pop(
                            self._provider_call_id(node_id),
                            None,
                        )
                    else:
                        critiques[node_id] = self._run_node(
                            node_id,
                            lambda request=request, cls=cls, node_id=node_id, critique_deps=critique_deps: self._run_provider_node(node_id, request, cls, critique_deps),
                            expected_binding=critique_binding,
                        )

            # The critic artifacts are the authority on both fresh execution
            # and cache reload. Provider prose cannot establish target scope.
            trusted_shard_critic_ids = {
                node_id
                for node_id, payload in critiques.items()
                if "candidate_shard_results" in payload
                and (node := self._dag.get(node_id)) is not None
                and node.status == "succeeded"
                and node.model_name == "hierarchical"
                and node.provider == "local"
            }
            critique_disposition = derive_critique_disposition(
                critiques,
                candidate_hashes=generation_hashes,
                candidate_contents=candidate_contents,
                trusted_shard_critic_ids=trusted_shard_critic_ids,
            )
            flagged_ids = set(critique_disposition["blocked_candidate_ids"])
            eligible_ids = list(critique_disposition["eligible_candidate_ids"])
            if critique_disposition["global_blocker"] or not eligible_ids:
                issue_ids = [
                    str(issue["issue_id"])
                    for issue in critique_disposition["issues"]
                    if issue["severity"] == "blocking"
                ]
                raise OutlineV3ExecutionError(
                    "critique disposition has unresolved blocking issues before arbitration: "
                    + ",".join(issue_ids)
                )
            eligible_contents = {
                candidate_id: candidate_contents[candidate_id]
                for candidate_id in eligible_ids
                if candidate_id in candidate_contents
            }
            sharded_candidate_ids = [
                candidate_id for candidate_id in eligible_ids
                if int(
                    ((self._payloads.get(f"{candidate_id}_provider_generation") or {}).get("shard_plan") or {}).get("shard_count") or 0
                ) > 1
            ]

            arbitration_request = {
                "candidate_ids": eligible_ids,
                "candidate_hashes": {
                    candidate_id: generation_hashes[candidate_id]
                    for candidate_id in eligible_ids
                },
                "candidate_contents": eligible_contents,
                "critiques": critiques,
                "critique_disposition": critique_disposition,
                "coverage_metrics": {key: value.get("coverage_metrics", {}) for key, value in critiques.items()},
                "evidence_metrics": {key: value.get("evidence_metrics", {}) for key, value in critiques.items()},
                "structure_metrics": {key: value.get("structure_metrics", {}) for key, value in critiques.items()},
                "blocking_diagnostics": [*evidence_model.blocking_diagnostics, *candidate_map_model.blocking_diagnostics],
                "review_intent": intent_model.to_dict(),
                "selection_rule": "coverage_then_evidence_then_structure",
                "section_coordination_contract": {
                    "required_if_selected_candidate_sharded": sharded_candidate_ids,
                    "candidate_section_hashes": {
                        candidate_id: {
                            str(section.get("section_id") or ""): _hash_payload(dict(section))
                            for section in eligible_contents[candidate_id].get("sections") or ()
                            if isinstance(section, Mapping) and str(section.get("section_id") or "")
                        }
                        for candidate_id in eligible_ids
                        if candidate_id in eligible_contents
                    },
                    "output_fields": (
                        "For a selected sharded candidate return section_coordination with candidate_id, "
                        "merge_groups and section_order. Every source section appears exactly once. "
                        "Merge only identical title/goal sections, echo exact source_section_hashes, "
                        "and preserve all claims/support; otherwise keep separate and order explicitly."
                    ),
                },
            }
            primary_arbitration_request = copy.deepcopy(arbitration_request)
            arbitration_deps = {
                "structure_critique": self._node_execution_identity_hash("structure_critique"),
                "coverage_critique": self._node_execution_identity_hash("coverage_critique"),
                "evidence_critique": self._node_execution_identity_hash("evidence_critique"),
            }
            arbitration = self._run_node(
                "arbitration",
                lambda: self._run_provider_node("arbitration", arbitration_request, ArbitrationDecision, arbitration_deps),
                expected_binding=self._provider_binding(
                    "arbitration", arbitration_request, expect_json=True,
                    input_artifact_hashes=tuple(arbitration_deps.values()),
                ),
            )
            if not candidate_ids:
                raise OutlineV3ExecutionError("outline arbitration has no candidates")
            selected_id = str(arbitration.get("selected_candidate_id") or "").strip()
            if selected_id not in eligible_ids:
                raise OutlineV3ExecutionError("outline arbitration selected an unknown candidate")
            selected = self._run_node("selected_candidate", lambda: (
                self._artifact(SelectedOutlineCandidate, {"candidate_id": selected_id, "candidate_hash": generation_hashes[selected_id], "accepted_recommendations": arbitration.get("accepted_recommendations", []), "rejected_recommendations": arbitration.get("rejected_recommendations", [])}, {"arbitration": _hash_payload(arbitration)}),
                ("arbitration",), self.profile.model, self.profile.provider,
            ))

            selected_payload = self._payloads.get(f"{selected_id}_provider_generation", {})
            selected_projection = candidate_contents.get(selected_id, {})
            original_sections = list(
                selected_projection.get("sections")
                or selected_payload.get("sections")
                or []
            )
            raw_coordination = arbitration.get("section_coordination")
            if selected_id in sharded_candidate_ids and not isinstance(raw_coordination, Mapping):
                raise OutlineV3ExecutionError(
                    "selected sharded candidate has no explicit global section coordination"
                )
            if isinstance(raw_coordination, Mapping):
                coordinated_sections, coordination_audit = self._apply_section_coordination(
                    selected_id,
                    original_sections,
                    raw_coordination,
                    parent_hash=generation_hashes[selected_id],
                )
            else:
                coordinated_sections = [dict(item) for item in original_sections if isinstance(item, Mapping)]
                coordination_audit = {
                    "schema_version": "candidate-section-coordination/v1",
                    "candidate_id": selected_id,
                    "status": "not_required_single_request",
                    "parent_hash": generation_hashes[selected_id],
                }
            view_by_key = {view.paper_key: view for view in evidence_model.views}
            revision = apply_selected_revision(
                candidate_id=selected_id,
                sections=coordinated_sections,
                recommendations=arbitration.get("accepted_recommendations"),
                view_by_key=view_by_key,
                parent_candidate_hash=generation_hashes[selected_id],
            )
            accepted_recommendations = revision["accepted_recommendations"]
            revised_sections = revision["revised_sections"]
            revision_records = revision["revision_records"]
            unresolved_revisions = revision["unresolved_revisions"]
            if unresolved_revisions:
                raise OutlineV3ExecutionError(
                    "accepted outline recommendations could not be applied to the selected candidate: "
                    + "; ".join(
                        str(item.get("issue_id") or item.get("recommendation") or item)
                        for item in unresolved_revisions
                    )
                )
            self._validate_candidate_payload(
                selected_id,
                {"sections": revised_sections},
                allowed_paper_keys=list(contract_model.corpus_paper_keys),
                allowed_relation_ids=[item.relation_id for item in confirmed_map_model.relations],
                alias_map=critique_alias_map,
            )
            revised_candidate_hash = _hash_payload({"sections": revised_sections})
            revision_verification = {
                "artifact_type": "selected_candidate_revision_verification",
                "artifact_version": "v1",
                "candidate_id": selected_id,
                "revised_candidate_hash": revised_candidate_hash,
                "checks": {
                    "section_identity_and_order": True,
                    "allowed_paper_ids": True,
                    "allowed_relation_ids": True,
                    "non_empty_claims": True,
                    "evidence_packet_rebuild_required": True,
                },
                "status": "passed",
            }
            revision_deps = {
                "selected_candidate": _hash_payload(selected),
                f"{selected_id}_provider_generation": generation_hashes[selected_id],
            }
            selected_revision = self._run_node("selected_candidate_revision", lambda: (
                self._artifact(
                    OutlineArtifact,
                    {
                        "schema_version": "selected-candidate-revision/v1",
                        "candidate_id": selected_id,
                        "parent_candidate_hash": generation_hashes[selected_id],
                        "parent_selected_hash": _hash_payload(selected),
                        "revised_content_hash": revised_candidate_hash,
                        "sections": revised_sections,
                        "section_coordination": coordination_audit,
                        "accepted_recommendations": accepted_recommendations,
                        "revision_records": revision_records,
                        "revision_verification": revision_verification,
                        "revision_round": 1,
                        "max_revision_rounds": 2,
                        "status": "completed" if not unresolved_revisions else "blocked",
                    },
                    revision_deps,
                ),
                ("selected_candidate", f"{selected_id}_provider_generation"),
                "deterministic",
                "local",
            ))
            sections = revised_sections
            packets = []
            relation_by_id = {relation.relation_id: relation for relation in confirmed_map_model.relations}
            for section in sections:
                section_id = str(section.get("section_id") or "").strip()
                section_keys = sorted(set(str(item) for item in section.get("paper_keys") or () if str(item).strip()))
                if not section_id or not section_keys:
                    raise OutlineV3ExecutionError("selected outline contains a section without durable paper assignment")
                selected_views = [view_by_key[key] for key in section_keys if key in view_by_key]
                if len(selected_views) != len(section_keys):
                    raise OutlineV3ExecutionError(f"section {section_id} references paper keys without evidence views")
                section_relation_ids = sorted(set(str(item) for item in section.get("relation_ids") or () if str(item).strip()))
                selected_relations = [relation_by_id[item] for item in section_relation_ids if item in relation_by_id]

                def field_values(field_name: str) -> list[str]:
                    return sorted({
                        str(value).strip()
                        for view in selected_views
                        for value in getattr(view, field_name, [])
                        if str(value).strip()
                    })

                contradiction_payload = [
                    relation.to_dict()
                    for relation in selected_relations
                    if relation.relation_type in {"contradicts", "explains_discrepancy", "qualifies"}
                ]
                packets.append({
                    "section_id": section_id,
                    "section_goal": str(section.get("goal") or ""),
                    "research_question_link": intent_model.review_question,
                    "planned_claims": list(section.get("claims") or []),
                    "claim_support": [
                        dict(row) for row in section.get("claim_support") or ()
                        if isinstance(row, Mapping)
                    ],
                    "paper_roles": (
                        dict(section.get("paper_roles") or {})
                        if isinstance(section.get("paper_roles"), Mapping) else {}
                    ),
                    "paper_keys": section_keys,
                    "must_use_paper_keys": list(contract_model.must_use_paper_keys),
                    "relation_ids": section_relation_ids,
                    "theories": field_values("theories"),
                    "constructs": field_values("constructs"),
                    "mechanisms": field_values("mechanisms"),
                    "contexts": field_values("sample_or_context"),
                    "methods": field_values("method"),
                    "findings": field_values("findings") + field_values("conclusions"),
                    "contradictions": contradiction_payload,
                    "boundary_conditions": field_values("limitations"),
                    "gaps": field_values("research_gaps") + field_values("future_directions"),
                    "evidence_items": [
                        {
                            "paper_key": view.paper_key,
                            "title": view.title,
                            "summary_hash": view.source_summary_hash,
                            "view_hash": view.view_hash,
                            "fields": self._prompt_evidence_views([view])[0],
                            "source_fields": dict(view.source_fields),
                            "interpretation_context": [
                                entry.to_dict()
                                for entry in view.source_field_ledger
                                if entry.interpretation_required
                            ],
                        }
                        for view in selected_views
                    ],
                    "relation_evidence": [relation.to_dict() for relation in selected_relations],
                    "source_summary_hashes": sorted({view.source_summary_hash for view in selected_views}),
                    "evidence_view_hashes": [view.view_hash for view in selected_views],
                    "retrieval_provenance": {
                        "source_artifacts": ["outline_evidence_views", "global_corpus_ledger", "confirmed_global_relation_map"],
                        "selection": "section_targeted",
                        "paper_keys": section_keys,
                        "view_hashes": [view.view_hash for view in selected_views],
                    },
                    "token_budget": {"strategy": self.profile.tokenizer_strategy, "input_budget": self.profile.input_budget},
                })
            packet_set = self._run_node("section_evidence_packets", lambda: (
                self._artifact(SectionEvidencePacketSet, {"packets": packets, "coverage_ledger": {"paper_coverage": sorted(set(item for packet in packets for item in packet["paper_keys"])), "must_use_coverage": sorted(set(contract_model.must_use_paper_keys) & set(item for packet in packets for item in packet["paper_keys"])), "claim_coverage": [claim for packet in packets for claim in packet["planned_claims"]]}, "semantic_chunk_plan_hash": semantic_chunk_plan_model.content_hash, "selected_candidate_revision_hash": _hash_payload(selected_revision)}, {"selected_candidate_revision": _hash_payload(selected_revision), "global_corpus_ledger": _hash_payload(ledger), "global_relation_map": _hash_payload(confirmed_map), "semantic_chunk_plan": _hash_payload(semantic_chunk_plan)}),
                ("selected_candidate_revision", "global_corpus_ledger", "global_relation_map", "semantic_chunk_plan"), "deterministic", "local",
            ))
            final_payload = {"title": intent_model.review_question or "Evidence-led literature review outline", "sections": sections, "candidate_id": selected_id, "paper_keys": sorted(set(item for packet in packets for item in packet["paper_keys"])), "relation_ids": [item.relation_id for item in confirmed_map_model.relations], "source_hashes": sorted(evidence_model.source_summary_hashes)}
            final = self._run_node("final_outline", lambda: (
                self._artifact(FinalOutline, final_payload, {"section_evidence_packets": _hash_payload(packet_set)}),
                ("section_evidence_packets",), self.profile.model, self.profile.provider,
            ))
            covered = set(final_payload["paper_keys"])
            corpus = set(contract_model.corpus_paper_keys)
            must_use = set(contract_model.must_use_paper_keys)
            claims = [claim for packet in packets for claim in packet.get("planned_claims", []) if str(claim).strip()]
            packet_papers = set(item for packet in packets for item in packet.get("paper_keys", []))
            used_relations = set(item for section in sections for item in section.get("relation_ids", []))
            empty_sections = [str(section.get("section_id") or "") for section in sections if not section.get("claims") or not section.get("paper_keys")]
            packet_missing_keys = sorted(covered - packet_papers)
            required_corpus = {
                entry.paper_key
                for entry in ledger_model.entries
                if entry.assignment_status not in {"excluded_with_reason"}
            }
            section_count = len(sections)
            effective_sections = [
                section for section in sections
                if str(section.get("title") or "").strip()
                and str(section.get("goal") or "").strip()
                and section.get("paper_keys")
                and section.get("claims")
            ]
            assignment_counts: dict[str, int] = {}
            for section in sections:
                for paper_key in section.get("paper_keys") or ():
                    assignment_counts[str(paper_key)] = assignment_counts.get(str(paper_key), 0) + 1
            duplicate_assignments = {
                paper_key: count - 1
                for paper_key, count in sorted(assignment_counts.items())
                if count > 1
            }
            duplicate_role_violations: list[str] = []
            role_values_by_paper: dict[str, set[str]] = {}
            for section in sections:
                raw_roles = section.get("paper_roles")
                roles = raw_roles if isinstance(raw_roles, Mapping) else {}
                for paper_key in section.get("paper_keys") or ():
                    paper = str(paper_key)
                    role = str(roles.get(paper) or "").strip()
                    if assignment_counts.get(paper, 0) <= 1:
                        continue
                    if not role:
                        duplicate_role_violations.append(
                            f"{paper}: section {section.get('section_id') or ''} has no distinct paper role"
                        )
                        continue
                    role_values_by_paper.setdefault(paper, set()).add(role.casefold())
            for paper_key, count in duplicate_assignments.items():
                if len(role_values_by_paper.get(paper_key, set())) < count + 1:
                    duplicate_role_violations.append(
                        f"{paper_key}: repeated paper roles are not distinct across sections"
                    )
            placeholder_sections = [
                str(section.get("section_id") or "")
                for section in sections
                if any("placeholder" in str(section.get(field) or "").casefold() or "todo" in str(section.get(field) or "").casefold() for field in ("title", "goal"))
                or any("placeholder" in str(claim).casefold() or "todo" in str(claim).casefold() for claim in section.get("claims") or ())
            ]
            stream_values = {
                "methods": sorted({value for packet in packets for value in packet.get("methods") or () if str(value).strip()}),
                "contexts": sorted({value for packet in packets for value in packet.get("contexts") or () if str(value).strip()}),
                "findings": sorted({value for packet in packets for value in packet.get("findings") or () if str(value).strip()}),
                "gaps": sorted({value for packet in packets for value in packet.get("gaps") or () if str(value).strip()}),
            }
            evidence_stream_availability = {
                "methods": bool(
                    evidence_model.views
                    and any(getattr(view, "method", None) for view in evidence_model.views)
                ),
                "contexts": bool(
                    evidence_model.views
                    and any(
                        getattr(view, "sample_or_context", None)
                        for view in evidence_model.views
                    )
                ),
                "findings": True,
                "gaps": bool(
                    evidence_model.views
                    and any(
                        (getattr(view, "research_gaps", None) or ())
                        or (getattr(view, "future_directions", None) or ())
                        for view in evidence_model.views
                    )
                ),
            }
            empty_research_streams = [
                name
                for name, values in stream_values.items()
                if not values and evidence_stream_availability.get(name, True)
            ]
            unsupported_planned_claims = [
                str(claim)
                for packet in packets
                for claim in packet.get("planned_claims") or ()
                if str(claim).strip() and not packet.get("evidence_items")
            ]
            method_coverage = {
                "available": sorted({value for view in evidence_model.views for value in view.method}),
                "used": stream_values["methods"],
            }
            context_coverage = {
                "available": sorted({value for view in evidence_model.views for value in view.sample_or_context}),
                "used": stream_values["contexts"],
            }
            excluded_with_reason = [
                entry.to_dict() for entry in ledger_model.entries if entry.assignment_status == "excluded_with_reason"
            ]
            canonical_coverage = (len(covered & corpus) / len(corpus)) if corpus else 0.0
            local_coverage = (len(covered & required_corpus) / len(required_corpus)) if required_corpus else 0.0
            threshold = (
                self.quality_gate.min_canonical_coverage_full
                if self.quality_gate.coverage_scope == "full"
                else self.quality_gate.min_canonical_coverage_local
            )
            quality_checks = {
                "coverage_scope": self.quality_gate.coverage_scope,
                "full_threshold": canonical_coverage >= self.quality_gate.min_canonical_coverage_full,
                "local_threshold": local_coverage >= self.quality_gate.min_canonical_coverage_local,
                "selected_threshold": (canonical_coverage if self.quality_gate.coverage_scope == "full" else local_coverage) >= threshold,
                "min_effective_sections": len(effective_sections) >= self.quality_gate.min_effective_sections,
                "max_duplicate_assignments": (
                    len(duplicate_role_violations)
                    <= self.quality_gate.max_duplicate_assignments
                ),
                "placeholder_sections": not placeholder_sections if self.quality_gate.block_placeholder_sections else True,
                "empty_research_streams": not empty_research_streams if self.quality_gate.block_empty_research_streams else True,
                "unsupported_planned_claims": not unsupported_planned_claims,
            }
            enforced_quality_checks = {
                "required_corpus_covered": required_corpus.issubset(covered),
                "must_use_covered": must_use.issubset(covered),
                "required_corpus_in_packets": required_corpus.issubset(packet_papers),
                "sections_nonempty": not empty_sections,
                "packet_sources_complete": not packet_missing_keys,
                "claims_present": bool(claims),
                "selected_threshold": bool(quality_checks["selected_threshold"]),
                **{
                    key: bool(value)
                    for key, value in quality_checks.items()
                    if isinstance(value, bool)
                    and key not in {"full_threshold", "local_threshold", "selected_threshold"}
                },
            }
            quality_decision = {
                "schema_version": "outline-quality-decision/v1",
                "coverage_scope": self.quality_gate.coverage_scope,
                "enforced_checks": enforced_quality_checks,
                "diagnostic_thresholds": {
                    "full": bool(quality_checks["full_threshold"]),
                    "local": bool(quality_checks["local_threshold"]),
                },
                "passed": all(enforced_quality_checks.values()),
            }
            coverage_passed = bool(quality_decision["passed"])
            coverage_audit_payload = {
                "passed": coverage_passed,
                "quality_decision": quality_decision,
                "quality_gate": self.quality_gate.to_dict(),
                "quality_gate_hash": self.quality_gate.content_hash,
                "paper_coverage": {
                    "total": len(corpus), "covered": len(covered & corpus),
                    "missing": sorted(required_corpus - covered), "packet_missing": packet_missing_keys,
                    "canonical_coverage_full": canonical_coverage, "canonical_coverage_local": local_coverage,
                },
                "claim_coverage": {"count": len(claims), "claims": claims, "unsupported_planned_claims": unsupported_planned_claims},
                "relation_coverage": {"planned": len(confirmed_map_model.relations), "used": len(used_relations), "unused": sorted(set(item.relation_id for item in confirmed_map_model.relations) - used_relations)},
                "must_use_coverage": {"required": sorted(must_use), "covered": sorted(must_use & covered)},
                "section_coverage": {"sections": section_count, "effective_section_count": len(effective_sections), "empty_sections": empty_sections, "packet_papers": len(packet_papers), "duplicate_paper_assignments": duplicate_assignments, "duplicate_role_violations": duplicate_role_violations, "placeholder_sections": placeholder_sections},
                "research_streams": {"empty": empty_research_streams, "values": stream_values},
                "method_coverage": method_coverage,
                "context_coverage": context_coverage,
                "contradiction_coverage": {"count": sum(len(packet.get("contradictions", [])) for packet in packets)},
                "gap_coverage": {"count": sum(len(packet.get("gaps", [])) for packet in packets)},
                "excluded_with_reason_papers": excluded_with_reason,
                "quality_checks": quality_checks,
                "threshold_result": {"required": threshold, "observed": canonical_coverage if self.quality_gate.coverage_scope == "full" else local_coverage, "passed": quality_checks["selected_threshold"]},
            }
            audit = self._run_node("coverage_audit", lambda: (
                self._artifact(CoverageAudit, coverage_audit_payload, {"final_outline": _hash_payload(final), "coverage_contract": _hash_payload(contract), "section_evidence_packets": _hash_payload(packet_set), "quality_gate": self.quality_gate.content_hash}),
                ("final_outline", "coverage_contract", "section_evidence_packets"), "deterministic", "local",
            ))
            dependency_binding = all(
                bool(packet.get("source_summary_hashes")) and bool(packet.get("evidence_view_hashes"))
                for packet in packets
            )

            def _variant_decision(
                variant_name: str,
                variant_summaries: Sequence[Mapping[str, Any]],
                candidate_order: Sequence[str],
                definition: Mapping[str, Any],
            ) -> dict[str, Any]:
                replay_evidence_start = len(self._replay_evidence)
                raw_variant_evidence = build_outline_evidence_views(variant_summaries, self.job_id)
                configured_shard_size = int(definition.get("shard_size") or len(variant_summaries) or 1)
                variant_shards = shard_outline_evidence_views(
                    raw_variant_evidence,
                    max(1, configured_shard_size),
                )
                if definition.get("shard_order") == "permuted":
                    variant_shards = list(reversed(variant_shards))
                variant_evidence = merge_outline_evidence_shards(variant_shards)
                evidence_shards = [item.to_dict() for item in variant_shards]
                variant_ledger = build_global_corpus_ledger(variant_evidence)
                variant_matrix = build_multi_view_matrix(variant_evidence)
                variant_content_layers = build_paper_content_layers(
                    variant_summaries,
                    variant_evidence,
                    job_id=self.job_id,
                )
                variant_candidates = build_global_relation_map(variant_evidence, variant_matrix, variant_ledger)
                variant_semantic_plan = build_semantic_chunk_plan(
                    variant_content_layers,
                    variant_candidates,
                    candidate_count=self.candidate_count,
                    physical_call_limit=authorized_provider_call_limit(self.max_provider_calls),
                )
                variant_candidate_rows = [
                    item.to_dict() for item in variant_candidates.relations
                ]
                variant_semantic_plan = self._apply_stability_relation_scope(
                    evidence=variant_evidence,
                    relation_candidates=variant_candidate_rows,
                    semantic_plan=variant_semantic_plan,
                )
                relation_scope = self._canonical_stability_relation_scope()
                selected_relation_ids = set(relation_scope["selected_relation_ids"])
                selected_relation_rows = [
                    item for item in variant_candidate_rows
                    if str(item.get("relation_id") or "") in selected_relation_ids
                ]
                relation_key_hash = hash_json({"variant": variant_name, "role": "relation_adjudication"})[:16]
                relation_dependencies = {
                    "variant_source_summaries": hash_json(sorted(variant_evidence.source_summary_hashes)),
                    "variant_relation_candidates": variant_candidates.content_hash,
                    "frozen_relation_scope": relation_scope["scope_hash"],
                }
                variant_relation_plan = self._build_relation_shard_plan(
                    variant_evidence.views,
                    variant_candidate_rows,
                )
                variant_relation_request = self._stability_relation_compact_request(
                    relation_candidates=variant_candidate_rows,
                    content_layers=variant_content_layers,
                    semantic_plan=variant_semantic_plan,
                    shard_plan=variant_relation_plan,
                    evidence_views=variant_evidence.views,
                    variant_name=variant_name,
                    shard_size=configured_shard_size,
                    shard_order=str(definition.get("shard_order") or "canonical"),
                    relation_scope=relation_scope,
                )
                relation_audit_node_id = f"stability:{relation_key_hash}:relation_adjudication"
                if not selected_relation_rows:
                    relation_adjudication = {
                        "confirmed_relation_ids": [],
                        "rejected_relations": [],
                        "status": "empty_selected_scope_no_transport",
                    }
                else:
                    relation_profile = self._role_route("relation_adjudication").profile
                    variant_budget = relation_profile.estimate_request(
                        self._attach_prompt_authority(relation_audit_node_id, variant_relation_request)
                    )
                    variant_input = int(variant_budget.get("estimated_input_tokens") or 0)
                    if (
                        variant_input > self._relation_packing_target(relation_profile)
                        or variant_input > self._effective_input_cap(relation_profile)
                        or not bool(variant_budget.get("within_budget"))
                        or int(variant_relation_plan.get("shard_count") or 0) > 1
                    ):
                        relation_adjudication, _variant_relation_digests = (
                            self._run_hierarchical_relation_adjudication(
                                evidence_views=variant_evidence.views,
                                relation_candidates=selected_relation_rows,
                                shard_plan=variant_relation_plan,
                                relation_contract={
                                    "allowed_relation_ids": sorted(selected_relation_ids),
                                    "must_return_confirmed_relation_ids": True,
                                    "must_reject_without_recorded_evidence": True,
                                },
                                relation_dependencies=relation_dependencies,
                                relation_bundles={
                                    item.relation_id: item.to_dict()
                                    for item in variant_semantic_plan.relation_bundles
                                    if item.relation_id in selected_relation_ids
                                },
                                node_prefix=f"stability:{relation_key_hash}",
                                compact_request=variant_relation_request,
                            )
                        )
                    else:
                        relation_adjudication = self._provider_call(
                            relation_audit_node_id,
                            variant_relation_request,
                            expect_json=True,
                            input_artifact_hashes=(
                                *sorted(variant_evidence.source_summary_hashes),
                                variant_candidates.content_hash,
                                relation_scope["scope_hash"],
                            ),
                            transport_node_id="relation_adjudication",
                        )
                confirmed_relation_ids = {
                    str(item).strip()
                    for item in relation_adjudication.get("confirmed_relation_ids") or ()
                    if str(item).strip()
                }
                if not confirmed_relation_ids.issubset(selected_relation_ids):
                    raise OutlineV3ExecutionError(
                        "stability relation response widened the frozen selected scope"
                    )
                variant_confirmed = [
                    item
                    for item in variant_candidates.relations
                    if item.evidence_fields and item.relation_id in confirmed_relation_ids
                ]
                variant_relation_map = GlobalRelationMap(
                    relations=variant_confirmed,
                    paper_keys=list(variant_candidates.paper_keys),
                    source_artifact_hashes=dict(variant_candidates.source_artifact_hashes),
                    blocking_diagnostics=list(variant_candidates.blocking_diagnostics),
                )
                variant_intent = build_review_intent(self.review_intent_input)
                variant_contract = build_coverage_contract(variant_ledger, variant_intent)
                if confirmed_relation_ids != set(confirmed_ids):
                    raise OutlineV3ExecutionError(
                        "stability variant changed a selected relation judgment"
                    )
                if (
                    set(variant_contract.corpus_paper_keys) != set(contract_model.corpus_paper_keys)
                    or set(variant_contract.must_use_paper_keys) != set(contract_model.must_use_paper_keys)
                ):
                    raise OutlineV3ExecutionError(
                        "stability variant changed the coverage contract"
                    )
                if self.provider is None:
                    raise OutlineV3ExecutionError(
                        "stability audit cannot execute without a configured provider"
                    )
                plans_by_id = {item.candidate_id: item for item in plans_model.candidates}
                ordered_ids = [item for item in candidate_order if item in plans_by_id]
                ordered_ids.extend(item.candidate_id for item in plans_model.candidates if item.candidate_id not in ordered_ids)
                variant_contents: dict[str, dict[str, Any]] = {}
                for candidate_id in ordered_ids:
                    paper_keys = sorted(
                        str(item.paper_key)
                        for item in variant_ledger.entries
                        if str(item.paper_key)
                    )
                    if candidate_id not in primary_candidate_requests:
                        raise OutlineV3ExecutionError(
                            "stability variant lacks the primary candidate request contract"
                        )
                    request = copy.deepcopy(primary_candidate_requests[candidate_id])
                    if (
                        sorted(str(item) for item in request.get("paper_keys") or ()) != paper_keys
                        or set(str(item) for item in request.get("relation_ids") or ())
                        != confirmed_relation_ids
                    ):
                        raise OutlineV3ExecutionError(
                            "stability variant candidate scope differs from primary"
                        )
                    request["evidence"] = self._compact_candidate_evidence_refs(
                        variant_evidence.views,
                        variant_content_layers,
                        variant_semantic_plan,
                    )
                    request["evidence_shards"] = evidence_shards
                    request["shard_size"] = configured_shard_size
                    request["shard_order"] = str(definition.get("shard_order") or "canonical")
                    request["stability_variant"] = {
                        "name": variant_name,
                        "declared_perturbation": dict(definition),
                        "frozen_relation_scope_hash": relation_scope["scope_hash"],
                        "shared_semantic_context_hash": hash_json(
                            request["shared_semantic_context"]
                        ),
                    }
                    alias_map = (
                        self._alias_map_for(paper_keys, list(request["relation_ids"]))
                        if self._alias_enabled else None
                    )
                    provider_request = (
                        alias_structural(request, alias_map)
                        if alias_map is not None else request
                    )
                    stability_key_hash = hash_json(
                        {"variant": variant_name, "candidate": candidate_id}
                    )[:16]
                    stability_node_id = (
                        f"stability:{stability_key_hash}:{candidate_id}_provider_generation"
                    )
                    generation_deps = {
                        "candidate": _hash_payload(provider_request),
                        "global_relation_map": _hash_payload(confirmed_map),
                        "coverage_contract": _hash_payload(contract),
                        "semantic_chunk_plan": _hash_payload(semantic_chunk_plan),
                        "shared_semantic_context": hash_json(request["shared_semantic_context"]),
                        "frozen_relation_scope": relation_scope["scope_hash"],
                    }
                    generation_route = self._node_route(
                        stability_node_id,
                        transport_node_id=f"{candidate_id}_provider_generation",
                    )
                    generation_budget = generation_route.profile.estimate_request(
                        self._attach_prompt_authority(stability_node_id, provider_request)
                    )
                    shard_generation = bool(
                        int(generation_budget.get("estimated_input_tokens") or 0)
                        > self._relation_packing_target(generation_route.profile)
                        or not bool(generation_budget.get("within_budget"))
                    )
                    if shard_generation:
                        generated = self._run_hierarchical_candidate_generation(
                            candidate_id=candidate_id,
                            generation_node_id=f"{candidate_id}_provider_generation",
                            provider_request=provider_request,
                            evidence_views=variant_evidence.views,
                            relation_candidates=[item.to_dict() for item in variant_relation_map.relations],
                            allowed_paper_keys=paper_keys,
                            allowed_relation_ids=[item.relation_id for item in variant_relation_map.relations],
                            generation_deps=generation_deps,
                            alias_map=alias_map,
                            node_prefix=f"stability:{stability_key_hash}",
                        )
                    else:
                        generated = self._provider_call(
                            stability_node_id,
                            provider_request,
                            expect_json=True,
                            input_artifact_hashes=tuple(generation_deps.values()),
                            transport_node_id=f"{candidate_id}_provider_generation",
                            output_tokens=min(
                                int(generation_route.profile.max_output_tokens), 4_096
                            ),
                        )
                    content = (
                        canonicalize_structural(dict(generated), alias_map)
                        if alias_map is not None else dict(generated)
                    )
                    try:
                        self._validate_candidate_payload(
                            candidate_id,
                            content,
                            allowed_paper_keys=paper_keys,
                            allowed_relation_ids=[item.relation_id for item in variant_relation_map.relations],
                            alias_map=alias_map,
                        )
                    except OutlineV3ExecutionError as contract_error:
                        if not self._repair_enabled:
                            raise
                        content = self._semantic_repair_candidate(
                            candidate_id,
                            content,
                            contract_error,
                            allowed_paper_keys=paper_keys,
                            allowed_relation_ids=[item.relation_id for item in variant_relation_map.relations],
                            alias_map=alias_map,
                            node_prefix=f"stability:{stability_key_hash}",
                        )
                        self._validate_candidate_payload(
                            candidate_id,
                            content,
                            allowed_paper_keys=paper_keys,
                            allowed_relation_ids=[item.relation_id for item in variant_relation_map.relations],
                            alias_map=alias_map,
                        )
                    variant_contents[candidate_id] = content
                variant_generation_hashes = {
                    candidate_id: hash_json(content)
                    for candidate_id, content in variant_contents.items()
                }
                baseline_sections_for_review = [
                    dict(section)
                    for section in final.get("sections") or ()
                    if isinstance(section, Mapping)
                ]
                stability_primary_claim_catalog: dict[str, dict[str, Any]] = {}
                stability_claim_comparisons: dict[str, list[dict[str, Any]]] = {}
                for candidate_id, content in variant_contents.items():
                    catalog, pairs = self._stability_claim_review_material(
                        baseline_sections_for_review,
                        [
                            dict(section)
                            for section in content.get("sections") or ()
                            if isinstance(section, Mapping)
                        ],
                        candidate_id=candidate_id,
                    )
                    for item in catalog:
                        fact_id = str(item.get("fact_id") or "")
                        if fact_id:
                            stability_primary_claim_catalog[fact_id] = item
                    if pairs:
                        stability_claim_comparisons[candidate_id] = pairs
                variant_critique_requests = {
                    "structure_critique": {
                        "node_id": "structure_critique",
                        "candidate_contents": variant_contents,
                        "candidate_hashes": variant_generation_hashes,
                        "review_intent": variant_intent.to_dict(),
                        "checks": [
                            "section_progression",
                            "duplicate_assignments",
                            "goal_claim_alignment",
                            "placeholder_sections",
                            "empty_research_streams",
                        ],
                    },
                    "coverage_critique": {
                        "node_id": "coverage_critique",
                        "candidate_contents": variant_contents,
                        "candidate_hashes": variant_generation_hashes,
                        "coverage_contract": variant_contract.to_dict(),
                        "corpus_ledger": variant_ledger.to_dict(),
                        "must_use_paper_keys": list(variant_contract.must_use_paper_keys),
                        "relations": [item.to_dict() for item in variant_relation_map.relations],
                    },
                    "evidence_critique": {
                        "node_id": "evidence_critique",
                        "candidate_contents": variant_contents,
                        "candidate_hashes": variant_generation_hashes,
                        "candidate_claims": {
                            key: value.get("planned_claims", [])
                            for key, value in variant_contents.items()
                        },
                        "section_evidence": {
                            key: value.get("sections", [])
                            for key, value in variant_contents.items()
                        },
                        "paper_keys": sorted(variant_contract.corpus_paper_keys),
                        "source_summary_hashes": sorted(variant_evidence.source_summary_hashes),
                        "evidence_shards": evidence_shards,
                        "relation_evidence": [item.to_dict() for item in variant_relation_map.relations],
                    },
                }
                if stability_claim_comparisons:
                    variant_critique_requests["evidence_critique"].update({
                        "stability_primary_claim_catalog": [
                            stability_primary_claim_catalog[fact_id]
                            for fact_id in sorted(stability_primary_claim_catalog)
                        ],
                        "stability_claim_comparisons": stability_claim_comparisons,
                        "stability_claim_review_contract": {
                            "version": "stability-claim-equivalence/v1",
                            "purpose": (
                                "Review materially changed claim wording against the same typed source facts. "
                                "The baseline catalog provides the primary wording and provenance; each "
                                "candidate's section claim references identify its variant wording."
                            ),
                            "equivalent_only_if": [
                                "same factual proposition and effect direction",
                                "same population, study, condition and qualifier scope",
                                "same limitation and uncertainty strength",
                            ],
                            "decisions": ["equivalent", "material_change", "uncertain"],
                            "fail_closed_on_missing_duplicate_or_extra_pair_id": True,
                        },
                    })
                variant_critiques: dict[str, dict[str, Any]] = {}
                variant_trusted_shard_ids: set[str] = set()
                for critique_name, critique_request in variant_critique_requests.items():
                    critique_key_hash = hash_json({"variant": variant_name, "role": critique_name})[:16]
                    critique_audit_node_id = f"stability:{critique_key_hash}:{critique_name}"
                    critique_deps = {
                        "coverage_contract": variant_contract.content_hash,
                        "candidate_generations": _hash_payload(variant_generation_hashes),
                    }
                    critique_route = self._node_route(
                        critique_audit_node_id,
                        transport_node_id=critique_name,
                    )
                    critique_budget = critique_route.profile.estimate_request(
                        self._attach_prompt_authority(critique_audit_node_id, critique_request)
                    )
                    shard_critique = bool(
                        int(critique_budget.get("estimated_input_tokens") or 0)
                        > self._relation_packing_target(critique_route.profile)
                        or not bool(critique_budget.get("within_budget"))
                    )
                    if shard_critique:
                        critique = self._run_hierarchical_critique(
                            node_id=critique_name,
                            request=critique_request,
                            dependency_hashes=critique_deps,
                            node_prefix=f"stability:{critique_key_hash}",
                        )
                        variant_trusted_shard_ids.add(critique_name)
                    else:
                        critique = self._provider_call(
                            critique_audit_node_id,
                            critique_request,
                            expect_json=True,
                            input_artifact_hashes=tuple(critique_deps.values()),
                            transport_node_id=critique_name,
                            output_tokens=min(
                                int(critique_route.profile.max_output_tokens),
                                2_048,
                            ),
                        )
                    variant_critiques[critique_name] = critique
                claim_review_audit = self._enforce_stability_claim_reviews(
                    variant_critiques["evidence_critique"],
                    comparisons=stability_claim_comparisons,
                    candidate_hashes=variant_generation_hashes,
                )
                variant_claim_review_results[variant_name] = claim_review_audit
                variant_critique_disposition = derive_critique_disposition(
                    variant_critiques,
                    candidate_hashes=variant_generation_hashes,
                    candidate_contents=variant_contents,
                    trusted_shard_critic_ids=variant_trusted_shard_ids,
                )
                variant_eligible_ids = list(
                    variant_critique_disposition["eligible_candidate_ids"]
                )
                if variant_critique_disposition["global_blocker"] or not variant_eligible_ids:
                    raise OutlineV3ExecutionError(
                        f"stability {variant_name} has unresolved typed critique issues"
                    )
                arbitration_request = copy.deepcopy(primary_arbitration_request)
                arbitration_request["candidate_ids"] = variant_eligible_ids
                arbitration_request["candidate_hashes"] = {
                    candidate_id: variant_generation_hashes[candidate_id]
                    for candidate_id in variant_eligible_ids
                }
                arbitration_request["candidate_contents"] = {
                    candidate_id: variant_contents[candidate_id]
                    for candidate_id in variant_eligible_ids
                }
                arbitration_request["critiques"] = variant_critiques
                arbitration_request["critique_disposition"] = variant_critique_disposition
                for metric in ("coverage_metrics", "evidence_metrics", "structure_metrics"):
                    arbitration_request[metric] = {
                        key: value.get(metric, {})
                        for key, value in variant_critiques.items()
                    }
                arbitration_request["blocking_diagnostics"] = [
                    *variant_evidence.blocking_diagnostics,
                    *variant_relation_map.blocking_diagnostics,
                    *[
                        str(item)
                        for critique in variant_critiques.values()
                        for item in critique.get("blocking_diagnostics") or ()
                    ],
                ]
                variant_sharded_candidate_ids = [
                    candidate_id for candidate_id, content in variant_contents.items()
                    if candidate_id in variant_eligible_ids
                    and int(((content.get("shard_plan") or {}).get("shard_count") or 0)) > 1
                ]
                coordination_contract = dict(
                    arbitration_request.get("section_coordination_contract") or {}
                )
                coordination_contract.update({
                    "required_if_selected_candidate_sharded": variant_sharded_candidate_ids,
                    "candidate_section_hashes": {
                        candidate_id: {
                            str(section.get("section_id") or ""): _hash_payload(dict(section))
                            for section in content.get("sections") or ()
                            if isinstance(section, Mapping) and str(section.get("section_id") or "")
                        }
                        for candidate_id, content in variant_contents.items()
                        if candidate_id in variant_eligible_ids
                    },
                })
                arbitration_request["section_coordination_contract"] = coordination_contract
                arbitration_request["stability_variant"] = {
                    "name": variant_name,
                    "frozen_relation_scope_hash": relation_scope["scope_hash"],
                    "shared_semantic_context_hashes": {
                        candidate_id: hash_json(primary_candidate_requests[candidate_id]["shared_semantic_context"])
                        for candidate_id in variant_contents
                    },
                }
                arbitration_key_hash = hash_json(
                    {"variant": variant_name, "role": "arbitration"}
                )[:16]
                arbitration_raw = self._provider_call(
                    f"stability:{arbitration_key_hash}:arbitration",
                    arbitration_request,
                    expect_json=True,
                    input_artifact_hashes=(
                        variant_contract.content_hash,
                        *(hash_json(variant_contents[item]) for item in variant_contents),
                    ),
                    output_tokens=min(
                        int(self._node_route("arbitration").profile.max_output_tokens),
                        2_048,
                    ),
                    transport_node_id="arbitration",
                )
                selected_variant_id = str(
                    arbitration_raw.get("selected_candidate_id") or ""
                ).strip()
                if selected_variant_id not in variant_eligible_ids:
                    raise OutlineV3ExecutionError(
                        f"stability arbitration selected unknown candidate: {selected_variant_id or '<empty>'}"
                    )
                selected_variant = variant_contents.get(selected_variant_id, {})
                raw_variant_sections = [
                    dict(section) for section in selected_variant.get("sections") or ()
                    if isinstance(section, Mapping)
                ]
                raw_variant_coordination = arbitration_raw.get("section_coordination")
                if (
                    selected_variant_id in variant_sharded_candidate_ids
                    and not isinstance(raw_variant_coordination, Mapping)
                ):
                    raise OutlineV3ExecutionError(
                        "selected sharded stability candidate lacks section coordination"
                    )
                if isinstance(raw_variant_coordination, Mapping):
                    coordinated_variant_sections, _coordination_audit = self._apply_section_coordination(
                        selected_variant_id,
                        raw_variant_sections,
                        raw_variant_coordination,
                        parent_hash=variant_generation_hashes[selected_variant_id],
                    )
                else:
                    coordinated_variant_sections = raw_variant_sections
                variant_views = {view.paper_key: view for view in variant_evidence.views}
                variant_revision = apply_selected_revision(
                    candidate_id=selected_variant_id,
                    sections=coordinated_variant_sections,
                    recommendations=arbitration_raw.get("accepted_recommendations"),
                    view_by_key=variant_views,
                    parent_candidate_hash=variant_generation_hashes[selected_variant_id],
                )
                if variant_revision["unresolved_revisions"]:
                    raise OutlineV3ExecutionError(
                        f"stability {variant_name} accepted recommendations remain unresolved: "
                        + ",".join(
                            str(item.get("issue_id") or "")
                            for item in variant_revision["unresolved_revisions"]
                        )
                    )
                revised_variant_sections = variant_revision["revised_sections"]
                self._validate_candidate_payload(
                    selected_variant_id,
                    {"sections": revised_variant_sections},
                    allowed_paper_keys=list(variant_contract.corpus_paper_keys),
                    allowed_relation_ids=[item.relation_id for item in variant_relation_map.relations],
                    alias_map=critique_alias_map,
                )
                _final_review_catalog, final_claim_review_pairs = (
                    self._stability_claim_review_material(
                        baseline_sections_for_review,
                        revised_variant_sections,
                        candidate_id=selected_variant_id,
                    )
                )
                reviewed_equivalent_pair_ids = {
                    pair_id
                    for pair_id, decision in claim_review_audit.get("decisions", {}).items()
                    if decision == "equivalent"
                }
                unreviewed_final_pair_ids = sorted({
                    str(pair.get("pair_id") or "")
                    for pair in final_claim_review_pairs
                    if str(pair.get("pair_id") or "")
                    and str(pair.get("pair_id") or "") not in reviewed_equivalent_pair_ids
                })
                if unreviewed_final_pair_ids:
                    raise OutlineV3ExecutionError(
                        f"stability {variant_name} changed a factual claim after equivalence review: "
                        + ",".join(unreviewed_final_pair_ids)
                    )
                variant_relations = {item.relation_id: item for item in variant_relation_map.relations}
                variant_packets: list[dict[str, Any]] = []
                for section in revised_variant_sections:
                    if not isinstance(section, Mapping):
                        continue
                    keys = sorted(str(item) for item in section.get("paper_keys") or () if str(item).strip())
                    chosen_views = [variant_views[key] for key in keys if key in variant_views]
                    relation_ids = sorted(str(item) for item in section.get("relation_ids") or () if str(item).strip())
                    variant_packets.append({
                        "section_id": str(section.get("section_id") or ""),
                        "title": str(section.get("title") or ""),
                        "goal": str(section.get("goal") or ""),
                        "claims": [str(item) for item in section.get("claims") or () if str(item).strip()],
                        "claim_support": [dict(item) for item in section.get("claim_support") or () if isinstance(item, Mapping)],
                        "paper_roles": dict(section.get("paper_roles") or {}) if isinstance(section.get("paper_roles"), Mapping) else {},
                        "paper_keys": keys,
                        "relation_ids": [item for item in relation_ids if item in variant_relations],
                        "methods": sorted({value for view in chosen_views for value in view.method}),
                        "contexts": sorted({value for view in chosen_views for value in view.sample_or_context}),
                        "findings": sorted({value for view in chosen_views for value in [*view.findings, *view.conclusions]}),
                        "gaps": sorted({value for view in chosen_views for value in [*view.research_gaps, *view.future_directions]}),
                        "contradictions": [variant_relations[item].to_dict() for item in relation_ids if item in variant_relations and variant_relations[item].relation_type in {"contradicts", "explains_discrepancy"}],
                    })
                variant_final = {
                    "title": variant_intent.review_question or "Evidence-led literature review outline",
                    "candidate_id": selected_variant_id,
                    "sections": variant_packets,
                    "paper_keys": sorted({item for packet in variant_packets for item in packet["paper_keys"]}),
                    "relation_ids": sorted({item for packet in variant_packets for item in packet["relation_ids"]}),
                    "source_hashes": sorted(variant_evidence.source_summary_hashes),
                }
                assignment_counts_variant: dict[str, int] = {}
                for packet in variant_packets:
                    for key in packet["paper_keys"]:
                        assignment_counts_variant[key] = assignment_counts_variant.get(key, 0) + 1
                variant_fact_inventory = self._stability_fact_inventory(
                    revised_variant_sections
                )
                selected_claim_review_status = claim_review_audit.get(
                    "candidate_statuses", {}
                ).get(selected_variant_id, "not_required")
                if selected_claim_review_status == "blocked":
                    raise OutlineV3ExecutionError(
                        f"stability {variant_name} selected a candidate with unresolved claim equivalence review"
                    )
                signature = {
                    "paper_keys": sorted(variant_final["paper_keys"]),
                    "corpus_paper_keys": sorted(variant_contract.corpus_paper_keys),
                    "must_use_paper_keys": sorted(variant_contract.must_use_paper_keys),
                    "selected_candidate_id": selected_variant_id,
                    "section_count": len(variant_packets),
                    "section_identity": sorted(str(item.get("section_id") or "") for item in variant_packets),
                    "section_title_goal": sorted({(str(item.get("title") or ""), str(item.get("goal") or "")) for item in variant_packets}),
                    "assignment_overlap": sorted(key for key, value in assignment_counts_variant.items() if value > 1),
                    "relation_ids": sorted(variant_final["relation_ids"]),
                    "claims": sorted(claim for packet in variant_packets for claim in packet["claims"]),
                    "semantic_fact_hashes": variant_fact_inventory["fact_hashes"],
                    "untyped_claim_count": variant_fact_inventory["untyped_claim_count"],
                    "claim_equivalence_review": selected_claim_review_status in {
                        "equivalent", "not_required"
                    },
                    "claim_review_pair_ids": sorted(
                        str(pair.get("pair_id") or "")
                        for pair in final_claim_review_pairs
                        if str(pair.get("pair_id") or "")
                    ),
                    "revision_record_hashes": sorted(
                        hash_json(item) for item in variant_revision["revision_records"]
                    ),
                    "contradictions": sorted(hash_json(item) for packet in variant_packets for item in packet["contradictions"]),
                    "gaps": sorted(gap for packet in variant_packets for gap in packet["gaps"]),
                    "methods": sorted(method for packet in variant_packets for method in packet["methods"]),
                    "contexts": sorted(context for packet in variant_packets for context in packet["contexts"]),
                    "duplicates": sorted(key for key, value in assignment_counts_variant.items() if value > 1),
                    "unsupported_claims": sorted(claim for packet in variant_packets for claim in packet["claims"] if not packet["paper_keys"]),
                    "final_outline_hash": hash_json(variant_final),
                    "evidence_projection_hash": hash_json({"views": [view.view_hash for view in variant_evidence.views], "ledger": variant_ledger.content_hash, "matrix": variant_matrix.content_hash}),
                    "shard_plan_hash": hash_json(evidence_shards),
                    "shard_sizes": [len(item.views) for item in variant_shards],
                }
                return {
                    "final_outline": variant_final,
                    "signature": signature,
                    "evidence": variant_evidence,
                    "ledger": variant_ledger,
                    "matrix": variant_matrix,
                    "replay_evidence": list(self._replay_evidence[replay_evidence_start:]),
                }

            assignment_counts_primary: dict[str, int] = {}
            for packet in packets:
                for paper_key in packet.get("paper_keys") or ():
                    assignment_counts_primary[str(paper_key)] = assignment_counts_primary.get(str(paper_key), 0) + 1
            primary_fact_inventory = self._stability_fact_inventory([
                section for section in final.get("sections") or ()
                if isinstance(section, Mapping)
            ])
            primary_signature = {
                "paper_keys": sorted(str(item) for item in final.get("paper_keys") or ()),
                "corpus_paper_keys": sorted(contract_model.corpus_paper_keys),
                "must_use_paper_keys": sorted(contract_model.must_use_paper_keys),
                "selected_candidate_id": str(final.get("candidate_id") or ""),
                "section_count": len(final.get("sections") or ()),
                "section_identity": sorted(str(item.get("section_id") or "") for item in final.get("sections") or () if isinstance(item, Mapping)),
                "section_title_goal": sorted(
                    (str(item.get("title") or ""), str(item.get("goal") or ""))
                    for item in final.get("sections") or ()
                    if isinstance(item, Mapping)
                ),
                "assignment_overlap": sorted(key for key, value in assignment_counts_primary.items() if value > 1),
                "relation_ids": sorted(str(item) for item in final.get("relation_ids") or ()),
                "claims": sorted(str(claim) for packet in packets for claim in packet.get("planned_claims") or () if str(claim).strip()),
                "semantic_fact_hashes": primary_fact_inventory["fact_hashes"],
                "untyped_claim_count": primary_fact_inventory["untyped_claim_count"],
                "claim_equivalence_review": True,
                "claim_review_pair_ids": [],
                "revision_record_hashes": sorted(
                    hash_json(item) for item in revision_records
                ),
                "contradictions": sorted(hash_json(item) for packet in packets for item in packet.get("contradictions") or ()),
                "gaps": sorted(str(gap) for packet in packets for gap in packet.get("gaps") or () if str(gap).strip()),
                "methods": sorted(str(method) for packet in packets for method in packet.get("methods") or () if str(method).strip()),
                "contexts": sorted(str(context) for packet in packets for context in packet.get("contexts") or () if str(context).strip()),
                "duplicates": sorted(key for key, value in assignment_counts_primary.items() if value > 1),
                "unsupported_claims": [],
                "final_outline_hash": hash_json(final),
                "evidence_projection_hash": hash_json({
                    "views": [view.view_hash for view in evidence_model.views],
                    "ledger": ledger_model.content_hash,
                    "matrix": matrix_model.content_hash,
                }),
                "shard_plan_hash": hash_json([view.to_dict() for view in evidence_model.views]),
                "shard_sizes": [len(evidence_model.views)],
            }

            variants = self._stability_variant_plan()
            variant_signatures: dict[str, dict[str, Any]] = {}
            variant_input_hashes: dict[str, str] = {}
            variant_output_hashes: dict[str, str] = {}
            variant_definitions: dict[str, dict[str, Any]] = {}
            variant_errors: dict[str, str] = {}
            variant_claim_review_results: dict[str, dict[str, Any]] = {}
            rerun_replay_node_ids: dict[str, list[str]] = {}
            projection_signatures: dict[str, dict[str, Any]] = {}
            exact_replay_verification: dict[str, Any] = {
                "status": "not_run",
                "provider_invoked": False,
            }
            stability_call_count_before = self._provider_call_count
            stability_transport_count_before = self._transport_call_count
            for variant_name, variant_summaries, candidate_order, definition in variants:
                variant_definitions[variant_name] = definition
                variant_input_hashes[variant_name] = hash_json({"summaries": variant_summaries, "definition": definition})
                rerun_replay_node_ids[variant_name] = []
                try:
                    if definition.get("resume") == "primary_baseline_reuse":
                        decision = {
                            "signature": primary_signature,
                            "final_outline": final,
                            "evidence": evidence_model,
                            "replay_evidence": [],
                        }
                    elif definition.get("resume") == "exact_replay":
                        # The canonical decision chain has already persisted
                        # its replay records above.  Replaying it through the
                        # stability-node namespace would manufacture new
                        # identities and could never hit the canonical keys.
                        # Reuse the canonical decision here; the fresh
                        # second-executor check below is the durable zero-
                        # transport replay proof.
                        replay_events = [
                            {
                                "node_id": node_id,
                                "semantic_node_id": node_id,
                                "closure_epoch_id": self.closure_epoch_id,
                                "lookup_status": "canonical_replay",
                                "provider_invoked": False,
                                "reused_artifact_ids": [],
                                "reused_receipt_ids": [],
                                "reused_artifact_id": "",
                                "reused_receipt_id": "",
                            }
                            for node_id in self._provider_node_ids()
                        ]
                        decision = {
                            "signature": primary_signature,
                            "final_outline": final,
                            "evidence": evidence_model,
                            "replay_evidence": replay_events,
                        }
                    else:
                        # The exact-replay variant must execute the same concrete
                        # baseline call identities.  Its audit label remains
                        # distinct in the stability report, but changing the
                        # label here would manufacture new replay keys and make
                        # a valid replay look missing.
                        execution_variant_name = (
                            "baseline" if definition.get("resume") == "exact_replay" else variant_name
                        )
                        decision = _variant_decision(
                            execution_variant_name,
                            variant_summaries,
                            candidate_order,
                            definition,
                        )
                    if definition.get("resume") == "exact_replay":
                        replay_events = [
                            item for item in decision.get("replay_evidence", [])
                            if not item.get("provider_invoked")
                        ]
                        if not replay_events:
                            raise OutlineV3ExecutionError(
                                "exact replay did not resolve any durable replay records"
                            )
                        rerun_replay_node_ids[variant_name] = [
                            str(item.get("node_id") or "")
                            for item in replay_events
                            if str(item.get("node_id") or "")
                        ]
                    variant_signatures[variant_name] = decision["signature"]
                    variant_output_hashes[variant_name] = hash_json(decision["final_outline"])
                    variant_evidence = decision["evidence"]
                    projection_signatures[variant_name] = {
                        "paper_keys": [view.paper_key for view in variant_evidence.views],
                        "view_hashes": [view.view_hash for view in variant_evidence.views],
                        "source_summary_hashes": sorted(variant_evidence.source_summary_hashes),
                    }
                except (OutlineV3ExecutionError, TypeError, ValueError, KeyError) as exc:
                    variant_errors[variant_name] = f"{type(exc).__name__}: {exc}"

            baseline_signature = variant_signatures.get("baseline", {})
            comparisons: dict[str, dict[str, Any]] = {}
            final_fields = (
                "paper_keys", "corpus_paper_keys", "must_use_paper_keys",
                "relation_ids", "semantic_fact_hashes", "contradictions",
                "gaps", "methods", "contexts", "unsupported_claims",
                "claim_equivalence_review",
            )
            organization_fields = (
                "selected_candidate_id", "section_count", "section_identity",
                "assignment_overlap", "duplicates",
            )
            for variant_name, signature in variant_signatures.items():
                if variant_name == "baseline":
                    continue
                title_goal = signature.get("section_title_goal", [])
                baseline_title_goal = baseline_signature.get("section_title_goal", [])
                title_goal_similarity = 1.0 if title_goal == baseline_title_goal else 0.0
                comparison = {field: signature.get(field) == baseline_signature.get(field) for field in final_fields}
                comparison["title_goal_similarity"] = title_goal_similarity
                comparison["organization_equivalent"] = all(
                    signature.get(field) == baseline_signature.get(field)
                    for field in organization_fields
                )
                comparison["claims_text_exact"] = (
                    signature.get("claims") == baseline_signature.get("claims")
                )
                comparison["semantic_review_required"] = not comparison["semantic_fact_hashes"]
                comparison["claim_review_pair_count"] = len(
                    signature.get("claim_review_pair_ids") or ()
                )
                # A new model response may phrase an unchanged section title
                # or goal differently. Text equality remains diagnostic; the
                # evidence, coverage, relation and claim checks decide whether
                # this candidate is comparable.
                comparison["stable"] = all(comparison.get(field, False) for field in final_fields)
                comparisons[variant_name] = comparison
            projection_comparisons = {
                name: {
                    "paper_keys": value.get("paper_keys") == projection_signatures.get("baseline", {}).get("paper_keys"),
                    "view_hashes": value.get("view_hashes") == projection_signatures.get("baseline", {}).get("view_hashes"),
                    "source_summary_hashes": value.get("source_summary_hashes") == projection_signatures.get("baseline", {}).get("source_summary_hashes"),
                }
                for name, value in projection_signatures.items() if name != "baseline"
            }
            if self.stability_mode == "off":
                final_outline_stable = True
                metamorphic_checks = {
                    "stability_disabled": True,
                    "dependency_binding": dependency_binding,
                    "quality_gate_bound": self.quality_gate.content_hash == _hash_payload(self.quality_gate.to_dict()),
                }
                failed_checks: list[str] = []
                stability_status = "disabled"
            else:
                final_outline_stable = bool(comparisons) and all(item.get("stable") for item in comparisons.values())
                metamorphic_checks = {
                    "final_outline_stable": final_outline_stable,
                    "evidence_projection_permutation": bool(projection_comparisons) and all(all(item.values()) for item in projection_comparisons.values()),
                    # In bounded-repair / opaque-alias mode the generation node
                    # persists the ADOPTED canonical content, so a raw-output
                    # replay-store equivalence over the same node is not the
                    # correct invariant.  Equivalence is enforced instead by
                    # the (at most one) deterministic semantic repair and the
                    # full validator rerun; see failed_checks for repairs.
                    "rerun_replay_exact": (
                        True
                        if (self._repair_enabled or self._alias_enabled)
                        else bool(comparisons.get("exact_replay_resume", {}).get("stable"))
                    ),
                    "dependency_binding": dependency_binding,
                    "quality_gate_bound": self.quality_gate.content_hash == _hash_payload(self.quality_gate.to_dict()),
                }
                failed_checks = sorted([
                    *[
                        f"{name}:{field}"
                        for name, comparison in comparisons.items()
                        for field in final_fields if not comparison.get(field)
                    ],
                    *[name for name, passed in metamorphic_checks.items() if not passed],
                ])
            stability_status = "stable" if final_outline_stable and not variant_errors else "blocked"
            # Typed issue scope, not candidate text embedded in prose, decides
            # whether the selected candidate still carries a blocker.
            try:
                adopted_candidate_id = str(
                    (self._payloads.get("selected_candidate") or {}).get("candidate_id")
                    or self._payloads.get("selected_candidate", {}).get("candidate_id")
                    or ""
                )
            except Exception:
                adopted_candidate_id = ""
            selected_blockers: dict[str, list[str]] = {}
            for issue in critique_disposition["issues"]:
                if (
                    issue["severity"] == "blocking"
                    and issue["resolution_status"] in {"unresolved", "deferred", "rejected"}
                    and issue["candidate_id"] == adopted_candidate_id
                ):
                    selected_blockers.setdefault(issue["source_critic"], []).append(
                        issue["issue_id"]
                    )
            blocking_for_selected = {
                node_id: tuple(issue_ids)
                for node_id, issue_ids in selected_blockers.items()
            }
            if blocking_for_selected:
                stability_status = "blocked"
                for node_id, diagnostics in blocking_for_selected.items():
                    variant_errors.setdefault(
                        f"primary:{node_id}",
                        f"{node_id}: {'; '.join(diagnostics)}",
                    )
                failed_checks.extend(
                    f"primary:{node_id}"
                    for node_id in blocking_for_selected
                )
            actual_usage = self._actual_usage_cost_snapshot()
            stability_payload = {
                "status": stability_status,
                "method": (
                    "metamorphic_full_decision_v2"
                    if self.stability_mode == "full"
                    else f"metamorphic_{self.stability_mode}_decision_v3"
                ),
                "stability_mode": self.stability_mode,
                "preflight": dict(self.stability_preflight),
                "provider_call_plan": [item.to_dict() for item in self.provider_call_plans],
                "provider_call_plan_hash": str(self.stability_preflight.get("provider_call_plan_hash") or ""),
                "provider_call_count_before_stability": stability_call_count_before,
                "provider_call_count_after_stability": self._provider_call_count,
                "transport_call_count_before_stability": stability_transport_count_before,
                "transport_call_count_after_stability": self._transport_call_count,
                "provider_call_count_total": self._provider_call_count,
                "variant_definitions": variant_definitions,
                "variant_input_hashes": variant_input_hashes,
                "variant_output_hashes": variant_output_hashes,
                "rerun_replay_node_ids": rerun_replay_node_ids,
                "replay_evidence": list(self._replay_evidence),
                "exact_replay_verification": exact_replay_verification,
                "primary_critique_disposition": critique_disposition,
                "claim_equivalence_reviews": variant_claim_review_results,
                "variant_errors": variant_errors,
                "baseline_final_outline_metrics": baseline_signature,
                "comparisons": comparisons,
                "evidence_projection_permutation": projection_comparisons,
                "thresholds": {
                    "scientific_invariant_fields": list(final_fields),
                    "organization_fields_are_diagnostic": list(organization_fields),
                    "title_goal_exactness_is_diagnostic": True,
                },
                "checks": metamorphic_checks,
                "failed_checks": failed_checks,
                "estimated_totals": {
                    key: value
                    for key, value in self.stability_preflight.items()
                    if key.startswith("estimated_")
                },
                "actual_usage_totals": {
                    **actual_usage,
                },
            }
            stability = self._run_node("stability_audit", lambda: (
                self._artifact(StabilityAudit, stability_payload, {"coverage_audit": _hash_payload(audit), "final_outline": _hash_payload(final)}),
                ("coverage_audit", "final_outline"), "deterministic", "local",
            ))
            self._register_receipt_ledger()
            all_receipts = list(self._receipt_ledger.list_receipts())
            job_receipts = [
                receipt
                for receipt in all_receipts
                if receipt.job_id == self.job_id and receipt.stage_name == "outline_v3"
            ]
            # Stability perturbations are a separately audited subrun.  A
            # fresh exact-replay executor runs the canonical decision chain
            # with stability disabled, so prior stability receipts are
            # historical/out-of-scope rather than unexpected canonical calls.
            current_receipt_ids = {str(receipt_id) for receipt_id in self.receipts if str(receipt_id)}
            current_receipts = [
                receipt
                for receipt in job_receipts
                if not str(receipt.call_id or "").startswith("outline:stability:")
                and str(receipt.receipt_id or "") in current_receipt_ids
            ]
            canonical_expected = [
                expected
                for expected in self._expected_provider_calls.values()
                if not str(expected.call_id or "").startswith("outline:stability:")
            ]
            reuse_evidence_records = self._verify_verified_reuse_evidence(canonical_expected)
            replay_receipts = self._replay_receipt_index()
            reused_receipt_ids = {
                receipt_id
                for expected in canonical_expected
                if expected.verified_reuse
                for receipt_id in (self._verified_reuse_source_receipt_ids.get(expected.call_id, ""),)
                if receipt_id in replay_receipts
            }
            out_of_scope_receipts = [
                receipt for receipt in all_receipts if receipt not in current_receipts
            ]
            out_of_scope_receipts.extend(
                receipt
                for receipt_id, receipt in replay_receipts.items()
                if receipt_id in reused_receipt_ids and receipt not in out_of_scope_receipts
            )
            closure = ProviderReceiptClosure.evaluate(
                canonical_expected,
                current_receipts,
                out_of_scope=out_of_scope_receipts,
            )
            self._check("provider_receipt_closure")
            closure_contract = {
                **closure.to_dict(),
                "job_id": self.job_id,
                "stage_name": "stage2_outline",
                "attempt_id": self.logical_attempt_identity,
                "logical_attempt_identity": self.logical_attempt_identity,
                "closure_epoch_id": self.closure_epoch_id,
                "expected_call_graph_hash": self.expected_call_graph_hash,
                "expected_calls": [asdict(expected) for expected in canonical_expected],
                "verified_reuse_evidence_ids": [
                    record.artifact_id for record in reuse_evidence_records
                ],
            }
            closure_dependency_ids: list[str] = []
            if "provider_receipts" in self.artifact_records:
                closure_dependency_ids.append("provider_receipts")
            closure_dependency_ids.extend(
                record.artifact_id for record in reuse_evidence_records
            )
            closure_dependency_ids.extend(
                expected.node_id
                for expected in canonical_expected
                if expected.node_id in self.artifact_records
            )
            # Dynamic semantic synthesis calls expose a physical response
            # artifact whose receipt node is normalized to the static DAG
            # node for replay/readback.  Include that exact Registry record in
            # the closure dependencies as well, otherwise validation sees a
            # valid expected path that is absent from the closure dependency
            # projection and marks the whole job blocked.
            expected_artifact_paths = {
                str(expected.artifact_path).lower()
                for expected in canonical_expected
                if str(expected.artifact_path or "")
            }
            closure_dependency_ids.extend(
                artifact_id
                for artifact_id, record in self.artifact_records.items()
                if str(record.path).lower() in expected_artifact_paths
            )
            closure_dependencies = {
                key: self.artifact_records[key].content_hash
                for key in closure_dependency_ids
                if key in self.artifact_records
            }
            closure_payload = self._persist(
                "provider_receipt_closure",
                self._artifact(
                    ProviderReceiptClosureArtifact,
                    closure_contract,
                    closure_dependencies,
                ),
                depends_on=tuple(dict.fromkeys(closure_dependency_ids)),
                model="deterministic",
                provider="local",
            )
            closure_record = self.artifact_records["provider_receipt_closure"]
            if self.stability_mode != "off" and not self._skip_exact_replay_verification:
                try:
                    exact_replay_verification = self._verify_exact_replay_with_second_executor()
                except (OutlineV3ExecutionError, OSError, RegistryError, TypeError, ValueError) as exc:
                    exact_replay_verification = {
                        "status": "blocked",
                        "provider_invoked": False,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                    variant_errors["exact_replay_resume"] = str(exact_replay_verification["error"])
                second_replay_passed = exact_replay_verification.get("status") == "verified"
                metamorphic_checks["rerun_replay_exact"] = second_replay_passed
                metamorphic_checks["second_executor_exact_replay"] = second_replay_passed
                failed_checks = sorted({
                    *(
                        f"{name}:{field}"
                        for name, comparison in comparisons.items()
                        for field in final_fields if not comparison.get(field)
                    ),
                    *(
                        name for name, passed in metamorphic_checks.items()
                        if isinstance(passed, bool) and not passed
                    ),
                })
                stability_status = (
                    "stable"
                    if final_outline_stable and not variant_errors and not failed_checks
                    else "blocked"
                )
                stability_payload.update({
                    "status": stability_status,
                    "exact_replay_verification": exact_replay_verification,
                    "variant_errors": variant_errors,
                    "checks": metamorphic_checks,
                    "failed_checks": failed_checks,
                })
                stability = self._persist(
                    "stability_audit",
                    self._artifact(
                        StabilityAudit,
                        stability_payload,
                        {"coverage_audit": _hash_payload(audit), "final_outline": _hash_payload(final)},
                    ),
                    depends_on=("coverage_audit", "final_outline"),
                    model="deterministic",
                    provider="local",
                )
                refreshed_closure_record = self.registry.get("outline-v3:provider_receipt_closure")
                if refreshed_closure_record is not None:
                    closure_record = refreshed_closure_record
                    self.artifact_records["provider_receipt_closure"] = refreshed_closure_record
            try:
                self._persist_audit_evidence()
            except Exception as exc:
                self.diagnostics.append(
                    f"outline audit evidence publication failed: {type(exc).__name__}: {exc}"
                )
            health_diagnostics = list(self.diagnostics)
            if not coverage_passed:
                health_diagnostics.append("coverage audit did not satisfy the explicit corpus contract")
            if not quality_decision["passed"]:
                health_diagnostics.append("outline quality gate did not pass")
            if self.stability_mode != "off" and stability_status != "stable":
                health_diagnostics.append("stability audit is blocked")
            if not closure.complete:
                health_diagnostics.append("provider receipt closure is incomplete")
            critique_passed = bool(selected_id) and not critique_disposition["global_blocker"] and selected_id not in flagged_ids
            if not critique_passed:
                health_diagnostics.append("one or more provider-derived critiques did not pass")
            adoption_eligible = not health_diagnostics and bool(arbitration.get("selected_candidate_id"))
            self._check("stage_health")
            self._persist(
                "stage_health",
                self._artifact(
                    OutlineStageHealth,
                    {
                        "status": "healthy" if adoption_eligible else "blocked",
                        "adoption_eligible": adoption_eligible,
                        "quality_gate": self.quality_gate.to_dict(),
                        "quality_gate_hash": self.quality_gate.content_hash,
                        "quality_gate_passed": quality_decision["passed"],
                        "quality_decision": quality_decision,
                        "critique_disposition": critique_disposition,
                        "diagnostics": health_diagnostics,
                        "replay_diagnostics": list(self.replay_diagnostics),
                        "node_count": len(self._dag.nodes),
                        "receipt_count": len(self.receipts),
                        "coverage_audit_hash": self.artifact_records["coverage_audit"].content_hash,
                        "stability_audit_hash": self.artifact_records["stability_audit"].content_hash,
                        "provider_receipt_closure_hash": closure_record.content_hash,
                        "request_payload_audit_artifact_id": (
                            self.artifact_records["request_payload_audit"].artifact_id
                            if "request_payload_audit" in self.artifact_records
                            else ""
                        ),
                        "hierarchical_call_graph_artifact_id": (
                            self.artifact_records["hierarchical_call_graph"].artifact_id
                            if "hierarchical_call_graph" in self.artifact_records
                            else ""
                        ),
                        "provider_receipt_closure": closure_payload,
                    },
                    {
                        "stability_audit": _hash_payload(stability),
                        "coverage_audit": _hash_payload(audit),
                        "arbitration": _hash_payload(arbitration),
                        "provider_receipt_closure": closure_record.content_hash,
                    },
                ),
                depends_on=tuple(
                    item
                    for item in (
                        "stability_audit",
                        "coverage_audit",
                        "arbitration",
                        "provider_receipt_closure",
                        "request_payload_audit",
                        "hierarchical_call_graph",
                    )
                    if item in self.artifact_records
                ),
                model="deterministic",
                provider="local",
            )
            # Mark critic rejections failed only after their dependent audit
            # artifacts have been materialized.  This preserves a complete
            # audit trail while keeping DAG resume semantics explicit.  Only
            # critics whose diagnostics name the adopted candidate block the
            # run; rejections of other candidates stay non-fatal here (they are
            # preserved in stability variant errors).
            for node_id, diagnostics in blocking_for_selected.items():
                record = self.artifact_records.get(node_id)
                if record is None:
                    continue
                self._dag = self._node_store.record_node(
                    node_id,
                    status="failed",
                    input_hash=_hash_payload(dict(self._dag.get(node_id).execution_binding.get("dependency_hashes") or {})),
                    output_hash=record.content_hash,
                    output_artifact_ids=(record.artifact_id,),
                    model_route=str(self._dag.get(node_id).model_route or ""),
                    model_name=str(self._dag.get(node_id).model_name or ""),
                    provider=str(self._dag.get(node_id).provider or ""),
                    receipt_ids=tuple(self._dag.get(node_id).receipt_ids),
                    diagnostics=diagnostics,
                    execution_binding=self._dag.get(node_id).execution_binding,
                )
            loaded_dag = self._node_store.load()
            if loaded_dag is not None:
                self._dag = loaded_dag
            if self._dag.failed_node_ids or not adoption_eligible:
                status = "blocked"
            else:
                status = "ready_for_adoption"
            return OutlineV3ExecutionResult(self.job_id, status, False, dict(self.artifact_paths), tuple(node.node_id for node in self._dag.nodes if node.status == "succeeded"), tuple(self.receipts), tuple([*self.diagnostics, *self.replay_diagnostics]), self._dag)
        except Exception as exc:
            self.diagnostics.append(str(exc))
            try:
                self._register_receipt_ledger()
            except Exception as ledger_error:
                self.diagnostics.append(f"provider receipt ledger registration failed: {ledger_error}")
            try:
                self._persist_audit_evidence()
            except Exception as audit_error:
                self.diagnostics.append(
                    f"outline audit evidence publication failed: {type(audit_error).__name__}: {audit_error}"
                )
            try:
                loaded_dag = self._node_store.load()
                if loaded_dag is not None:
                    self._dag = loaded_dag
            except Exception:
                pass
            return OutlineV3ExecutionResult(self.job_id, "blocked", False, dict(self.artifact_paths), tuple(node.node_id for node in self._dag.nodes if node.status == "succeeded"), tuple(self.receipts), tuple(self.diagnostics), self._dag)

    execute = run


__all__ = ["OutlineV3ExecutionError", "OutlineV3ExecutionResult", "OutlineV3Executor"]
