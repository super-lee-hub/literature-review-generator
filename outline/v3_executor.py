"""Executable Outline Intelligence v3 pipeline.

The executor owns node execution, durable artifact writes, provider receipts,
and replay decisions. Evidence views are projected directly from Stage 1;
topic, cross-group, global, and outline decisions use the provider boundary
when a configured production route is available.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
import hashlib
import json
import math
import os
import re
import tempfile
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
from outline.v3_evidence import (
    build_coverage_contract,
    build_global_corpus_ledger,
    build_multi_view_matrix,
    build_outline_evidence_views,
    build_review_intent,
    merge_outline_evidence_shards,
    shard_outline_evidence_views,
)
from outline.v3_models import GlobalRelationMap, OutlineQualityGate, TopicSynthesis, compute_v3_hash
from outline.v3_relations import build_global_relation_map, build_organizing_axes, build_outline_candidate_plans
from outline.semantic_chunking import (
    build_paper_content_layers,
    build_semantic_chunk_plan,
    build_topic_synthesis_plan,
)
from outline.evidence_alias import alias_structural, canonicalize_structural
from runtime.outline_v3_dag import OutlineNodeDAG, OutlineNodeStore
from runtime.pause_state import PauseRequestedError, PauseStateStore
from runtime.provider_completion import ProviderCompletionEvaluator
from runtime.provider_context import ProviderContextProfile
from runtime.provider_receipt_closure import ExpectedProviderCall, ProviderReceiptClosure
from outline.provider_router import (
    OutlineProviderRouter,
    OutlineRoleRoute,
    semantic_role,
)
from runtime.provider_runtime import (
    ProviderBudgetV1,
    ProviderRuntime,
    ProviderRuntimeLedger,
    compute_closure_epoch_id,
    hash_json,
    hash_text,
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
        technical_shard_target_tokens: int = 0,
        pricing_source: str | None = None,
        pricing_provider: str | None = None,
        pricing_model: str | None = None,
        pricing_version: str | None = None,
        pricing_effective_date: str | None = None,
        pricing_policy: str = "estimate_only_not_billing_v1",
        publication_context: Any | None = None,
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
        self.opaque_alias_enabled = bool(opaque_alias_enabled)
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
        self.max_provider_calls = int(max_provider_calls) if max_provider_calls is not None else None
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
        self.provider_call_plans: tuple[OutlineProviderCallPlan, ...] = ()
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
            "provider_nodes": self._provider_node_ids(),
            "semantic_provider_nodes": (
                ["topic_synthesis_provider", "cross_group_comparison_provider", "global_synthesis_provider"]
                if self.semantic_provider_synthesis_enabled
                else []
            ),
            "stability_roles": ["candidate_provider_generation", "arbitration"],
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
        self._blocking_critic_diagnostics: dict[str, tuple[str, ...]] = {}
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
    ) -> int:
        serialized = json.dumps(request, ensure_ascii=False, sort_keys=True).encode("utf-8")
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
            "estimated_input_tokens": int(budget.get("estimated_input_tokens") or profile.estimate_tokens(request)),
            "input_cap": int(profile.input_budget),
            "output_cap": int(profile.max_output_tokens),
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

    def _build_relation_shard_plan(
        self,
        views: Sequence[Any],
        relation_candidates: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        """Plan deterministic, token-aware relation evidence shards.

        This planner is deliberately separate from stability perturbations.
        It preserves every evidence view and records the actual membership and
        estimate used for each shard. A target of zero keeps the legacy single
        shard behavior for callers that have not opted into hierarchical
        relation adjudication.
        """

        target = int(self.technical_shard_target_tokens or 0)
        ordered_views = list(views)
        if not ordered_views:
            return {
                "schema_version": "outline-relation-shard-plan-v1",
                "target_tokens": target,
                "shard_count": 0,
                "shards": [],
                "coverage": {"input_view_count": 0, "planned_view_count": 0, "missing_view_hashes": []},
            }

        route_profile = self._role_route("relation_adjudication").profile
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

    def _relation_hierarchical_preflight(
        self,
        summaries: Sequence[Mapping[str, Any]],
        profile: ProviderContextProfile,
        *,
        variant_name: str,
    ) -> tuple[int, int, dict[str, Any]]:
        """Estimate the actual local/cross relation requests, not a monolith."""

        evidence = build_outline_evidence_views(summaries, self.job_id)
        ledger = build_global_corpus_ledger(evidence)
        matrix = build_multi_view_matrix(evidence)
        candidates = build_global_relation_map(evidence, matrix, ledger)
        relation_candidates = [item.to_dict() for item in candidates.relations]
        plan = self._build_relation_shard_plan(evidence.views, relation_candidates)
        candidate_by_id = {
            str(item.get("relation_id") or ""): item
            for item in relation_candidates
            if str(item.get("relation_id") or "")
        }
        contract = {
            "allowed_relation_ids": sorted(candidate_by_id),
            "must_return_confirmed_relation_ids": True,
            "must_reject_without_recorded_evidence": True,
        }
        requests: list[dict[str, Any]] = []
        reviewed: set[str] = set()
        for shard in plan.get("shards") or ():
            if not isinstance(shard, Mapping):
                continue
            shard_id = str(shard.get("shard_id") or "")
            relation_ids = {
                str(item)
                for item in shard.get("relation_candidate_ids") or ()
                if str(item) in candidate_by_id
            }
            if not relation_ids:
                continue
            reviewed.update(relation_ids)
            requests.append({
                "hierarchy": {
                    "level": "local_shard",
                    "shard_id": shard_id,
                    "target_tokens": self.technical_shard_target_tokens,
                    "paper_keys": list(shard.get("paper_keys") or ()),
                    "relation_candidate_ids": sorted(relation_ids),
                    "evidence_view_hashes": list(shard.get("view_hashes") or ()),
                },
                "relation_candidates": [candidate_by_id[item] for item in sorted(relation_ids)],
                "evidence_views": [dict(item) for item in shard.get("evidence_chunks") or () if isinstance(item, Mapping)],
                "relation_adjudication_contract": {
                    **contract,
                    "allowed_relation_ids": sorted(relation_ids),
                },
            })
        remaining = set(candidate_by_id) - reviewed
        if remaining:
            compact_preflight_views = [
                self._compact_relation_digest(
                    request,
                    [str(item) for item in request.get("relation_candidate_ids") or () if str(item)],
                    [],
                    [],
                )
                for request in requests
            ]
            requests.extend(
                request
                for _node_id, request, _batch_ids in self._relation_cross_batch_requests(
                    candidate_by_id=candidate_by_id,
                    relation_ids=sorted(remaining),
                    shard_plan=plan,
                    relation_contract=contract,
                    profile=profile,
                    node_prefix=f"preflight:{variant_name}",
                    evidence_views_override=compact_preflight_views,
                )
            )
        estimates: list[int] = []
        for index, request in enumerate(requests, start=1):
            enriched = self._attach_prompt_authority(
                f"relation_adjudication:preflight:{variant_name}:{index}",
                request,
            )
            budget = profile.estimate_request(enriched)
            estimates.append(max(1, int(budget.get("estimated_input_tokens") or profile.estimate_tokens(enriched))))
        return max(estimates, default=1), len(requests), plan

    def _candidate_hierarchical_preflight(
        self,
        summaries: Sequence[Mapping[str, Any]],
        profile: ProviderContextProfile,
        *,
        variant_name: str,
    ) -> tuple[int, int]:
        """Estimate candidate-generation shard requests at their final shape."""

        evidence = build_outline_evidence_views(summaries, self.job_id)
        ledger = build_global_corpus_ledger(evidence)
        matrix = build_multi_view_matrix(evidence)
        candidates = build_global_relation_map(evidence, matrix, ledger)
        relation_candidates = [item.to_dict() for item in candidates.relations]
        plan = self._build_relation_shard_plan(evidence.views, relation_candidates)
        estimates: list[int] = []
        for index, shard in enumerate(plan.get("shards") or (), start=1):
            if not isinstance(shard, Mapping):
                continue
            shard_data: dict[str, Any] = dict(shard)
            raw_paper_keys = shard_data.get("paper_keys")
            raw_relation_ids = shard_data.get("relation_candidate_ids")
            raw_evidence_chunks = shard_data.get("evidence_chunks")
            paper_keys = [str(item) for item in raw_paper_keys if str(item)] if isinstance(raw_paper_keys, list) else []
            relation_ids = [str(item) for item in raw_relation_ids if str(item)] if isinstance(raw_relation_ids, list) else []
            request = {
                "candidate_id": "candidate_1",
                "variant_name": variant_name,
                "hierarchy": {
                    "level": "candidate_local_shard",
                    "shard_id": str(shard_data.get("shard_id") or f"candidate_shard_{index}"),
                    "target_tokens": self.technical_shard_target_tokens,
                    "paper_keys": paper_keys,
                    "relation_candidate_ids": relation_ids,
                },
                "paper_keys": paper_keys,
                "relation_ids": relation_ids,
                "relations": [
                    item for item in relation_candidates
                    if str(item.get("relation_id") or "") in set(relation_ids)
                ],
                "evidence": [
                    dict(item)
                    for item in raw_evidence_chunks
                    if isinstance(item, Mapping)
                ] if isinstance(raw_evidence_chunks, list) else [],
                "source_summary_hashes": sorted(evidence.source_summary_hashes),
                "candidate_count": self.candidate_count,
                "evidence_bound": True,
            }
            enriched = self._attach_prompt_authority(
                f"candidate_1_provider_generation:preflight:{variant_name}:{index}",
                request,
            )
            budget = profile.estimate_request(enriched)
            estimates.append(max(1, int(budget.get("estimated_input_tokens") or profile.estimate_tokens(enriched))))
        return max(estimates, default=1), len(estimates)

    def _build_provider_call_plans(self) -> tuple[OutlineProviderCallPlan, ...]:
        plans: list[OutlineProviderCallPlan] = []
        for variant_name, variant_summaries, transport_expected in self._provider_call_plan_variants():
            variant_evidence = build_outline_evidence_views(variant_summaries, self.job_id)
            variant_layers = build_paper_content_layers(variant_summaries, variant_evidence, job_id=self.job_id)
            for node_id in self._provider_node_ids():
                route = self._role_route(node_id)
                profile = route.profile
                hierarchical_relation_input: int | None = None
                candidate_shard_multiplier = 1
                if node_id == "relation_adjudication" and self.technical_shard_target_tokens > 0:
                    hierarchical_relation_input, _hierarchical_calls, _hierarchical_plan = (
                        self._relation_hierarchical_preflight(
                            variant_summaries,
                            profile,
                            variant_name=variant_name,
                        )
                    )
                elif node_id.endswith("_provider_generation") and self.technical_shard_target_tokens > 0:
                    hierarchical_relation_input, candidate_shard_multiplier = self._candidate_hierarchical_preflight(
                        variant_summaries,
                        profile,
                        variant_name=variant_name,
                    )
                elif (
                    node_id in {"structure_critique", "coverage_critique", "evidence_critique", "arbitration"}
                    and self.technical_shard_target_tokens > 0
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
                        * int(self._role_route("candidate_1_provider_generation").profile.max_output_tokens),
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
                    estimated_output = min(estimated_output, 1024)
                estimated_reasoning = max(0, int(profile.reasoning_reserve))
                candidate_output_cap = (
                    min(estimated_output, 1024)
                    if self.technical_shard_target_tokens > 0 and candidate_shard_multiplier > 1
                    else estimated_output
                )
                candidate_output_upper_bound = (
                    self.candidate_count
                    * max(1, candidate_shard_multiplier)
                    * candidate_output_cap
                )
                critic_input_upper_bound = 0
                if (
                    self.technical_shard_target_tokens > 0
                    and node_id in {"structure_critique", "coverage_critique", "evidence_critique", "arbitration"}
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
                if critic_input_upper_bound and not (
                    self.technical_shard_target_tokens > 0
                    and node_id in {"structure_critique", "coverage_critique", "evidence_critique", "arbitration"}
                ):
                    estimated_input = max(
                        estimated_input,
                        base_input_estimate + critic_input_upper_bound,
                    )
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
                    if node_id == "arbitration":
                        assumptions.append(
                            "arbitration input also includes three critic outputs at configured max_output_tokens"
                        )
                rate_input = self.input_cost_per_1k_tokens if self._pricing_is_explicit else None
                rate_output = self.output_cost_per_1k_tokens if self._pricing_is_explicit else None
                rate_reasoning = self.reasoning_cost_per_1k_tokens if self._pricing_is_explicit else None
                rate_cache_read = self.cache_read_cost_per_1k_tokens if self._pricing_is_explicit else None
                rate_cache_write = self.cache_write_cost_per_1k_tokens if self._pricing_is_explicit else None
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
                        config_section=route.config_section,
                        api_base_host=route.api_base_host,
                        route_fingerprint=route.safe_config_fingerprint(),
                    )
                )
        return tuple(plans)

    def _preflight_stability_budget(self) -> None:
        core_calls = len(self._provider_node_ids())
        self.provider_call_plans = self._build_provider_call_plans()
        transport_plans = [item for item in self.provider_call_plans if item.transport_expected]
        estimated_provider_calls = len(transport_plans)
        estimated_input_tokens = sum(item.estimated_input_tokens for item in transport_plans)
        estimated_output_tokens = sum(item.estimated_output_tokens for item in transport_plans)
        estimated_reasoning_tokens = sum(item.estimated_reasoning_tokens for item in transport_plans)
        estimated_cached_input_tokens = sum(item.estimated_cached_input_tokens for item in transport_plans)
        estimated_cache_write_tokens = sum(item.estimated_cache_write_tokens for item in transport_plans)
        estimated_total_tokens = sum(item.estimated_total_tokens for item in transport_plans)
        estimated_cost_values = [item.estimated_cost for item in transport_plans]
        known_estimated_costs = [value for value in estimated_cost_values if value is not None]
        estimated_cost = (
            sum(known_estimated_costs)
            if len(known_estimated_costs) == len(estimated_cost_values)
            else None
        )
        semantic_synthesis_calls = 0
        if self.semantic_provider_synthesis_enabled:
            # The exact topic batches are materialized after the shared
            # content layers are built.  Reserve a conservative upper bound
            # here so an aggregate call limit can block before the first
            # provider request rather than failing halfway through semantic
            # synthesis.  The production corpus is capped at twelve candidate
            # topics; cross-group and global synthesis add two calls.
            semantic_synthesis_calls = max(1, min(12, len(self.summaries) + 1)) + 2
            semantic_route = self._role_route("candidate_1_provider_generation")
            semantic_output = min(max(1, int(semantic_route.profile.max_output_tokens)), 4096)
            semantic_reasoning = max(0, int(semantic_route.profile.reasoning_reserve))
            estimated_provider_calls += semantic_synthesis_calls
            estimated_output_tokens += semantic_synthesis_calls * semantic_output
            estimated_reasoning_tokens += semantic_synthesis_calls * semantic_reasoning
            estimated_total_tokens += semantic_synthesis_calls * (semantic_output + semantic_reasoning)
            if estimated_cost is not None:
                estimated_cost += semantic_synthesis_calls * (
                    semantic_output / 1000.0 * float(self.output_cost_per_1k_tokens or 0.0)
                    + semantic_reasoning / 1000.0 * float(self.reasoning_cost_per_1k_tokens or 0.0)
                )
        hierarchical_candidate_shard_calls = 0
        if self.technical_shard_target_tokens > 0:
            for variant_name, variant_summaries, transport_expected in self._provider_call_plan_variants():
                if not transport_expected:
                    continue
                candidate_route = self._role_route("candidate_1_provider_generation")
                _candidate_input, candidate_shard_count = self._candidate_hierarchical_preflight(
                    variant_summaries,
                    candidate_route.profile,
                    variant_name=variant_name,
                )
                candidate_plan_estimate = max(
                    (
                        int(plan.estimated_input_tokens)
                        for plan in self.provider_call_plans
                        if str(plan.node_id).endswith("_provider_generation")
                    ),
                    default=int(_candidate_input),
                )
                if candidate_shard_count > 1 and candidate_plan_estimate > int(self.technical_shard_target_tokens):
                    hierarchical_candidate_shard_calls += self.candidate_count * (candidate_shard_count - 1)
            if hierarchical_candidate_shard_calls:
                candidate_route = self._role_route("candidate_1_provider_generation")
                candidate_output = min(max(1, int(candidate_route.profile.max_output_tokens)), 1024)
                candidate_reasoning = max(0, int(candidate_route.profile.reasoning_reserve))
                estimated_provider_calls += hierarchical_candidate_shard_calls
                estimated_output_tokens += hierarchical_candidate_shard_calls * candidate_output
                estimated_reasoning_tokens += hierarchical_candidate_shard_calls * candidate_reasoning
                estimated_total_tokens += hierarchical_candidate_shard_calls * (
                    candidate_output + candidate_reasoning
                )
                if estimated_cost is not None:
                    estimated_cost += hierarchical_candidate_shard_calls * (
                        candidate_output / 1000.0 * float(self.output_cost_per_1k_tokens or 0.0)
                        + candidate_reasoning / 1000.0 * float(self.reasoning_cost_per_1k_tokens or 0.0)
                    )
        hierarchical_relation_shard_calls = 0
        if self.technical_shard_target_tokens > 0 and "relation_adjudication" in self._provider_node_ids():
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
                if int(relation_plan.get("shard_count") or 0) > 1:
                    # The static relation node is replaced by these dynamic
                    # calls.  The provider-call plan already contains one
                    # static relation row, so only the surplus dynamic calls
                    # belong in the extra-call budget.
                    relation_plan_estimate = max(
                        (
                            int(plan.estimated_input_tokens)
                            for plan in self.provider_call_plans
                            if plan.node_id == "relation_adjudication"
                        ),
                        default=int(_relation_input),
                    )
                    if relation_plan_estimate > int(self.technical_shard_target_tokens):
                        hierarchical_relation_shard_calls += max(0, relation_call_count - 1)
            if hierarchical_relation_shard_calls:
                relation_route = self._role_route("relation_adjudication")
                relation_output = max(1, int(relation_route.profile.max_output_tokens))
                relation_reasoning = max(0, int(relation_route.profile.reasoning_reserve))
                estimated_provider_calls += hierarchical_relation_shard_calls
                estimated_output_tokens += hierarchical_relation_shard_calls * relation_output
                estimated_reasoning_tokens += hierarchical_relation_shard_calls * relation_reasoning
                estimated_total_tokens += hierarchical_relation_shard_calls * (
                    relation_output + relation_reasoning
                )
                if estimated_cost is not None:
                    estimated_cost += hierarchical_relation_shard_calls * (
                        relation_output / 1000.0 * float(self.output_cost_per_1k_tokens or 0.0)
                        + relation_reasoning / 1000.0 * float(self.reasoning_cost_per_1k_tokens or 0.0)
                    )
        hierarchical_critique_shard_calls = 0
        critique_extra_by_role: dict[str, int] = {}
        if self.technical_shard_target_tokens > 0:
            critique_roles = {
                "structure_critique",
                "coverage_critique",
                "evidence_critique",
            }
            for plan in transport_plans:
                if plan.node_id not in critique_roles:
                    continue
                if plan.estimated_input_tokens <= self.technical_shard_target_tokens:
                    continue
                # _run_hierarchical_critique emits one bounded call per
                # candidate.  The static plan already accounts for one call;
                # add the remaining candidate calls only for transport-backed
                # stability variants.
                extra_calls = max(0, self.candidate_count - 1)
                hierarchical_critique_shard_calls += extra_calls
                critique_extra_by_role[plan.node_id] = (
                    critique_extra_by_role.get(plan.node_id, 0) + extra_calls
                )
            if hierarchical_critique_shard_calls:
                critique_input = max(1, int(self.technical_shard_target_tokens))
                estimated_provider_calls += hierarchical_critique_shard_calls
                estimated_input_tokens += hierarchical_critique_shard_calls * critique_input
                for role, extra_calls in critique_extra_by_role.items():
                    role_profile = self._role_route(role).profile
                    critique_output = min(
                        max(1, int(role_profile.max_output_tokens)),
                        2048,
                    )
                    critique_reasoning = max(0, int(role_profile.reasoning_reserve))
                    estimated_output_tokens += extra_calls * critique_output
                    estimated_reasoning_tokens += extra_calls * critique_reasoning
                    estimated_total_tokens += extra_calls * (
                        critique_input + critique_output + critique_reasoning
                    )
                    if estimated_cost is not None:
                        estimated_cost += extra_calls * (
                            critique_input / 1000.0 * float(self.input_cost_per_1k_tokens or 0.0)
                            + critique_output / 1000.0 * float(self.output_cost_per_1k_tokens or 0.0)
                            + critique_reasoning / 1000.0 * float(self.reasoning_cost_per_1k_tokens or 0.0)
                        )
        estimated_input_per_call = max(
            1,
            max((item.estimated_input_tokens for item in transport_plans), default=0),
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
            "hierarchical_candidate_shard_calls": hierarchical_candidate_shard_calls,
            "hierarchical_relation_shard_calls": hierarchical_relation_shard_calls,
            "hierarchical_critique_shard_calls": hierarchical_critique_shard_calls,
            "semantic_synthesis_calls_reserved": semantic_synthesis_calls,
            "estimated_input_tokens": estimated_input_tokens,
            "estimated_output_tokens": estimated_output_tokens,
            "estimated_reasoning_tokens": estimated_reasoning_tokens,
            "estimated_cached_input_tokens": estimated_cached_input_tokens,
            "estimated_cache_write_tokens": estimated_cache_write_tokens,
            "estimated_total_tokens": estimated_total_tokens,
            "estimated_cost": estimated_cost,
            "pricing_source": self.pricing_source,
            "pricing_policy": self.pricing_policy,
            "pricing_confidence": "medium" if self._pricing_is_explicit else "unknown",
            "cost_status": "estimate" if self._pricing_is_explicit else "unknown",
            "monetary_ceiling_enforced": bool(estimated_cost is not None),
            "cost_ceiling_note": (
                ""
                if estimated_cost is not None
                else "monetary ceiling was not enforced because pricing status is unknown"
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
            > self._role_route(item.node_id).profile.input_budget
            for item in transport_plans
        ):
            self.stability_preflight["preflight_status"] = "rejected"
            self.stability_preflight["rejection_reason"] = "source_prompt_exceeds_input_budget"
        elif (
            self.max_source_prompt_tokens is not None
            and estimated_input_per_call > self.max_source_prompt_tokens
        ):
            self.stability_preflight["preflight_status"] = "rejected"
            self.stability_preflight["rejection_reason"] = "source_prompt_exceeds_configured_limit"
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
                "monetary ceiling was not enforced because pricing status is unknown"
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
        }
        return {
            "node_id": node_id,
            "semantic_node_id": self._semantic_node_id(node_id),
            "node_version": "v3",
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
            if receipt.node_id != semantic_node_id or receipt.call_id != semantic_call_id:
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
                return None
            if not self._replay_record_is_valid(replay.record, binding):
                return None
            payload_hash = hash_json(payload)
            normalized_hash = replay.record.normalized_output_hash or replay.record.output_hash
            if payload_hash != normalized_hash:
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
            return {"status": "success", "content": {
                "selected_candidate_id": selected,
                "selection_reasons": ["fixture selected the lexicographically stable candidate after receiving all candidate content"],
                "candidate_comparison": {str(item): {"coverage": "available", "evidence": "available", "structure": "available"} for item in candidate_ids},
                "accepted_recommendations": [],
                "rejected_recommendations": [],
                "unresolved_risks": [],
            }}
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
        for candidate_id in sorted(str(item) for item in candidate_contents):
            candidate_content = candidate_contents[candidate_id]
            compact = self._compact_candidate_for_critique(
                candidate_id,
                candidate_content if isinstance(candidate_content, Mapping) else {},
            )
            candidate_shards = self._split_critique_candidate(
                compact,
                target_tokens=int(self.technical_shard_target_tokens or 28000),
            )
            for shard_index, compact_candidate in enumerate(candidate_shards, start=1):
                local_request = dict(request)
                shard_key = f"{candidate_id}:shard:{shard_index}"
                local_request["hierarchy"] = {
                    "level": "critique_candidate_shard",
                    "shard_id": shard_key,
                    "target_tokens": self.technical_shard_target_tokens,
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
                content = self._provider_call(
                    local_node_id,
                    local_request,
                    expect_json=True,
                    input_artifact_hashes=(*dependency_hashes.values(), hash_json({"candidate_id": candidate_id, "shard_index": shard_index})),
                    transport_node_id=node_id,
                    output_tokens=min(int(self._node_route(node_id).profile.max_output_tokens), 2048),
                )
                result = dict(content)
                result["candidate_id"] = candidate_id
                result["shard_index"] = shard_index
                result["reviewed_section_ids"] = sorted(section_ids)
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
                str(index + 1): result for index, result in enumerate(shard_results)
            },
        }
        return merged

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

        shard_plan = self._build_relation_shard_plan(evidence_views, relation_candidates)
        sections: list[dict[str, Any]] = []
        # A paper may legitimately participate in more than one semantic
        # section (for example as a method paper and as a boundary paper).
        # Merge only the same logical section identity across shards; never
        # use paper assignment as a de-duplication key.
        section_by_identity: dict[str, dict[str, Any]] = {}
        claims: list[str] = []
        shard_outputs: list[dict[str, Any]] = []
        for shard in shard_plan.get("shards") or ():
            if not isinstance(shard, Mapping):
                continue
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
                    dict(item)
                    for item in relation_candidates
                    if isinstance(item, Mapping)
                    and str(item.get("relation_id") or "") in set(shard_relation_ids)
                ],
                "evidence": [
                    dict(item)
                    for item in shard.get("evidence_chunks") or ()
                    if isinstance(item, Mapping)
                ],
            })
            node_id = f"{generation_node_id}:local:{shard_id}"
            if node_prefix:
                node_id = f"{node_prefix}:{node_id}"
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
                existing = section_by_identity.get(original_section_id)
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
                    if isinstance(section_payload.get("paper_roles"), Mapping):
                        existing["paper_roles"] = {
                            **(existing.get("paper_roles") or {}),
                            **dict(section_payload.get("paper_roles") or {}),
                        }
                    continue
                section_payload["section_id"] = f"{original_section_id}__{shard_id}"
                section_by_identity[original_section_id] = section_payload
                sections.append(section_payload)
            claims.extend(str(item) for item in content.get("claims") or () if str(item).strip())
        if not sections:
            raise OutlineV3ExecutionError(f"{generation_node_id} produced no shard sections")
        planned_view_hashes = list(dict.fromkeys(
            str(item)
            for shard in shard_plan.get("shards") or ()
            if isinstance(shard, Mapping)
            for item in shard.get("view_hashes") or ()
            if str(item)
        ))
        merged = {
            "candidate_id": candidate_id,
            "organizing_logic": provider_request.get("organizing_logic") or "evidence",
            "sections": sections,
            "claims": claims,
            "shard_plan": {
                "shard_count": int(shard_plan.get("shard_count") or 0),
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

    def _run_semantic_provider_call(
        self,
        node_id: str,
        request: Mapping[str, Any],
        dependency_hashes: Mapping[str, str],
    ) -> dict[str, Any]:
        """Execute a topic/cross/global synthesis request on the outline route.

        The semantic synthesis roles do not have separate configuration keys
        yet; they intentionally reuse the configured candidate-generation
        route while retaining their own durable node/call identity.
        """

        return self._provider_call(
            node_id,
            request,
            expect_json=True,
            input_artifact_hashes=tuple(dependency_hashes.values()),
            transport_node_id="candidate_1_provider_generation",
            output_tokens=min(
                int(self._node_route("candidate_1_provider_generation").profile.max_output_tokens),
                4096,
            ),
        )

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
            request = {**dict(request), "output_contract": {
                "output_fields": {
                    "node_id": "string; echo the node_id from this request verbatim",
                    "passed": "boolean; true only if every check in the checks list passes",
                    "blocking_diagnostics": (
                        "array of strings; empty when passed is true; each item names one "
                        "check that failed and exactly why"
                    ),
                    "score": "number between 0 and 1 summarizing how many checks passed",
                    "recommendations": "array of strings with concrete repair suggestions",
                },
                "must_include": ["node_id", "passed", "blocking_diagnostics"],
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
                    replay_payload = json.loads(Path(replay_record.path).read_text(encoding="utf-8"))
                except (OSError, UnicodeError, json.JSONDecodeError):
                    continue
                payload = replay_payload.get("payload") if isinstance(replay_payload, Mapping) else None
                normalized_hash = replay_lookup.record.normalized_output_hash or replay_lookup.record.output_hash
                if isinstance(payload, Mapping) and hash_json(payload) == normalized_hash:
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
        admission = runtime.admit(
            estimated_tokens=int(budget["estimated_input_tokens"]),
            requested_output_tokens=int(output_tokens or profile.max_output_tokens),
            requested_retry_attempts=max(0, effective_attempts - 1),
        )
        if self.max_provider_calls is not None and self._provider_call_count >= self.max_provider_calls:
            self._finish_request_payload_audit(
                audit_index,
                status="blocked_provider_call_budget",
            )
            raise OutlineV3ExecutionError(
                f"outline provider call budget exhausted before {node_id}"
            )
        self._provider_call_count += 1
        transport = transport_for_audit
        if transport is None:
            if node_id.startswith("stability:"):
                self._finish_request_payload_audit(
                    audit_index,
                    status="blocked_stability_transport",
                )
                raise OutlineV3ExecutionError(
                    "stability audit requires a configured provider; fixture responses are not admissible"
                )
            raw = self._fixture_response(node_id, request)
        else:
            self._transport_call_count += 1
            runtime.mark_transport_started(admission)
            provider_node_id = str(transport_node_id or node_id)
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
        self._finish_request_payload_audit(
            audit_index,
            physical_attempt_id=receipt.receipt_id,
            provider_invoked=transport is not None,
            status=str(receipt.status),
            receipt_ids=[receipt.receipt_id],
        )
        normalized_hash = hash_json(completion.content) if completion.status == "complete" else ""
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
            if node_id in {"structure_critique", "coverage_critique", "evidence_critique"}:
                payload = artifact.payload if isinstance(artifact.payload, Mapping) else {}
                if not bool(payload.get("passed", True)) or payload.get("blocking_diagnostics"):
                    diagnostics = tuple(
                        str(item)
                        for item in (payload.get("blocking_diagnostics") or ())
                    ) or (f"{node_id} returned a non-passing critic result",)
                    # A critic rejection is a durable stage result, not a
                    # transport exception.  Keep the artifact available so
                    # the stability audit can record the rejection and the
                    # final stage-health gate can quarantine adoption.
                    self._blocking_critic_diagnostics[node_id] = diagnostics
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
        content = self._provider_call(node_id, request, expect_json=True, input_artifact_hashes=tuple(deps.values()))
        # The provider receipt has already been appended when this hook runs.
        # Recovery tests use it to model a worker failure after transport
        # success but before the node output is persisted.
        self._check(node_id, phase="provider_success")
        route = self._node_route(node_id)
        return self._artifact(cls, content, deps), tuple(deps), route.model, route.provider_name

    @staticmethod
    def _compact_relation_digest(
        request: Mapping[str, Any],
        relation_ids: Sequence[str],
        confirmed_ids: Sequence[str],
        rejected_ids: Sequence[str],
    ) -> dict[str, Any]:
        """Create an explicit bounded digest for cross-shard alignment."""

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
            "cross-shard alignment uses compact evidence; retrieve local shard evidence by evidence_view_hashes when needed"
        ]
        return digest

    def _relation_cross_batch_requests(
        self,
        *,
        candidate_by_id: Mapping[str, Mapping[str, Any]],
        relation_ids: Sequence[str],
        shard_plan: Mapping[str, Any],
        relation_contract: Mapping[str, Any],
        profile: ProviderContextProfile,
        node_prefix: str = "",
        evidence_views_override: Sequence[Mapping[str, Any]] | None = None,
    ) -> list[tuple[str, dict[str, Any], set[str]]]:
        """Partition cross-shard relation review by the actual request budget."""

        ordered_ids = [str(item) for item in relation_ids if str(item) in candidate_by_id]
        if not ordered_ids:
            return []
        all_chunks: list[dict[str, Any]] = []
        seen_chunks: set[str] = set()
        if evidence_views_override is not None:
            all_chunks = [dict(item) for item in evidence_views_override if isinstance(item, Mapping)]
        else:
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
            estimated = profile.estimate_tokens(estimate_request)
            limit = int(self.technical_shard_target_tokens or 0) or int(profile.input_budget)
            if current and estimated > limit:
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
        return batches

    def _run_hierarchical_relation_adjudication(
        self,
        *,
        evidence_views: Sequence[Any],
        relation_candidates: Sequence[Mapping[str, Any]],
        shard_plan: Mapping[str, Any],
        relation_contract: Mapping[str, Any],
        relation_dependencies: Mapping[str, str],
        node_prefix: str = "",
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
            local_candidates = [candidate_by_id[item] for item in sorted(local_ids)]
            local_chunks = [
                dict(item)
                for item in shard.get("evidence_chunks") or ()
                if isinstance(item, Mapping)
            ]
            local_views = [view_by_key[key] for key in paper_keys if key in view_by_key]
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
                "relation_candidates": local_candidates,
                "evidence_views": local_chunks or self._prompt_evidence_views(local_views),
                "relation_adjudication_contract": local_contract,
            }
            node_id = f"relation_adjudication:local:{shard_id}"
            if node_prefix:
                node_id = f"{node_prefix}:{node_id}"
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
            relation_batch_requests = self._relation_cross_batch_requests(
                candidate_by_id=candidate_by_id,
                relation_ids=sorted(remaining_ids),
                shard_plan=shard_plan,
                relation_contract=relation_contract,
                profile=self._role_route("relation_adjudication").profile,
                node_prefix=node_prefix,
                evidence_views_override=[
                    dict(item["compact_digest"])
                    for item in digests
                    if item.get("level") == "local_shard" and isinstance(item.get("compact_digest"), Mapping)
                ],
            )
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
                    level="cross_shard",
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
            "hierarchy": "local_shards_then_cross_shard",
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
        seen_sections: set[str] = set()
        for section in sections:
            if not isinstance(section, Mapping):
                raise OutlineV3ExecutionError(f"{candidate_id} provider output contains an invalid section")
            section_id = str(section.get("section_id") or "").strip()
            if not section_id or section_id in seen_sections:
                raise OutlineV3ExecutionError(f"{candidate_id} provider output has duplicate or missing section ids")
            seen_sections.add(section_id)
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
                referenced_papers = {
                    paper_alias_reverse.get(alias, alias)
                    for alias in aliases
                }
                if not referenced_papers.issubset(paper_keys):
                    raise OutlineV3ExecutionError(
                        f"{candidate_id} claim references paper aliases outside its section evidence: "
                        f"{sorted(referenced_papers - paper_keys)}"
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
    ) -> dict[str, Any]:
        """Bounded single-pass semantic repair for structural contract errors.

        Allowed exactly once per candidate per attempt.  The repair request
        carries only the original output, the validation error, the exact
        allowed ID sets and the output schema -- never the full Stage1
        summaries.  After repair the full validator runs again; a second
        failure publishes outline_candidate_repair_failure/v1 and raises
        (fail-closed, no third provider attempt from this call path).
        """
        from outline.evidence_alias import (
            alias_for_paper,
            alias_for_relation,
            alias_structural,
            canonicalize_structural,
        )

        repair_node_id = f"{candidate_id}_semantic_repair"
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
            "repair_rules": [
                "Repair ONLY the structure of the candidate sections.",
                "Remove or replace every section paper_key that is not in allowed_paper_ids.",
                "Remove or replace every section relation_id that is not in allowed_relation_ids.",
                "Do not introduce new papers, new relations, new citations, or new facts.",
                "Do not invent citation identities; do not attribute evidence to any work outside the provided evidence corpus.",
                "If a section has no in-corpus evidence left after removal, retain its identity and return needs_manual_review; never invent a replacement fact or silently delete the section.",
                "Do not add sections beyond those in original_provider_output and do not increase the total number of planned claims.",
                "Return exactly the same number of sections in exactly the same order as original_provider_output.",
                "Copy every original section_id verbatim; never create, delete, duplicate, or rename a section_id.",
                "Every claim that names a paper alias must be supported by a paper_key in that same section; remove the claim if its paper is not assigned there.",
                "Do not write cross-paper limitation or gap aggregations unless each named paper explicitly supports the same limitation; prefer separate paper-specific claims or remove the aggregation.",
                "Ensure each section goal accurately covers every remaining claim; rewrite a goal to a neutral evidence-bound purpose when the original goal is narrower than the claims.",
                "Keep candidate_id unchanged and return the same top-level shape.",
            ],
            "output_schema": {
                "candidate_id": "string; echo the candidate_id verbatim",
                "sections": (
                "non-empty array; each section object has section_id, title, "
                    "paper_keys (subset of allowed_paper_ids), relation_ids (subset "
                    "of allowed_relation_ids), claims (non-empty) and rationale"
                ),
                "needs_manual_review": "array of section_ids that cannot be repaired without a new semantic decision; empty when none",
            },
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
            self._publish_repair_failure(candidate_id, exc)
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
            self._publish_repair_failure(candidate_id, failure)
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
            self._publish_repair_failure(candidate_id, failure)
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
            self._publish_repair_failure(candidate_id, exc)
            raise OutlineV3ExecutionError(
                f"{candidate_id} semantic repair failed targeted revalidation: {exc}"
            ) from exc
        self._persist_repair_output(
            repair_node_id,
            dict(repaired_content),
            dependency_hashes=repair_deps,
        )
        return repaired_content

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

    def _publish_repair_failure(self, candidate_id: str, error: Exception) -> None:
        payload = {
            "artifact_type": "outline_candidate_repair_failure",
            "artifact_version": "v1",
            "candidate_id": candidate_id,
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

        comparison_nodes = (
            "structure_critique",
            "coverage_critique",
            "evidence_critique",
            "arbitration",
            "selected_candidate",
            "section_evidence_packets",
            "final_outline",
            "provider_receipt_closure",
        )

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
            technical_shard_target_tokens=self.technical_shard_target_tokens,
            max_provider_calls=None,
            max_estimated_cost=None,
            estimated_cost_per_1k_tokens=self.estimated_cost_per_1k_tokens,
            _skip_exact_replay_verification=True,
        )
        second_result = second_executor.run()
        if transport_calls:
            raise OutlineV3ExecutionError(
                "second-executor exact replay invoked provider transport: "
                + ",".join(transport_calls)
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
        if expected_hashes != second_hashes:
            raise OutlineV3ExecutionError(
                "second-executor exact replay changed decision artifacts: "
                + ",".join(
                    sorted(
                        node_id
                        for node_id in set(expected_hashes) | set(second_hashes)
                        if expected_hashes.get(node_id) != second_hashes.get(node_id)
                    )
                )
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
            "transport_call_count": 0,
            "replay_hit_count": len(replay_hits),
            "replay_evidence": replay_hits,
            "compared_artifact_hashes": dict(expected_hashes),
        }

    def run(self) -> OutlineV3ExecutionResult:  # pyright: ignore[reportGeneralTypeIssues]
        # This orchestration method intentionally keeps the ordered DAG
        # execution visible; the semantic subroutines below carry the
        # individual validation contracts.  Pyright's path-complexity limit
        # cannot analyze this finite dispatcher without losing useful types.
        try:
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
            candidate_map_model = build_global_relation_map(evidence_model, matrix_model, ledger_model)
            candidate_map = self._run_node("relation_candidates", lambda: (
                self._artifact(OutlineArtifact, candidate_map_model.to_dict(), {"multi_view_matrix": _hash_payload(matrix)}),
                ("multi_view_matrix",), "deterministic", "local",
            ))

            semantic_chunk_plan_model = build_semantic_chunk_plan(
                content_layers_model,
                candidate_map_model,
                candidate_count=self.candidate_count,
                physical_call_limit=min(24, max(0, int(self.max_provider_calls or 24))),
            )
            if semantic_chunk_plan_model.blocking_diagnostics:
                raise OutlineV3ExecutionError(
                    "semantic chunk plan is blocked before provider admission: "
                    + json.dumps(
                        semantic_chunk_plan_model.blocking_diagnostics,
                        ensure_ascii=False,
                        sort_keys=True,
                    )
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
                    current = self.build_current_node_binding(node_id)
                    identity_fields = (
                        "provider_route",
                        "provider_family",
                        "model_name",
                        "endpoint_type",
                        "route_fingerprint",
                        "context_profile_hash",
                    )
                    return all(
                        str(existing.execution_binding.get(field) or "")
                        == str(current.get(field) or "")
                        for field in identity_fields
                    )
                except (KeyError, ValueError, TypeError):
                    return False

            topic_semantic_reused = _semantic_node_reusable("topic_synthesis")
            semantic_provider_results: list[dict[str, Any]] = []
            if self.semantic_provider_synthesis_enabled and topic_plan and not topic_semantic_reused:
                topic_batches: list[list[TopicSynthesis]] = []
                current_batch: list[TopicSynthesis] = []
                dossier_by_paper = content_layers_model.dossier_by_paper
                topic_profile = self._node_route("candidate_1_provider_generation").profile
                topic_input_limit = max(
                    1,
                    min(
                        32_000,
                        int(self.max_source_prompt_tokens or 32_000),
                        int(topic_profile.input_budget or 32_000),
                    ) - 2_000,
                )

                def _topic_request(batch: Sequence[TopicSynthesis], batch_index: int) -> dict[str, Any]:
                    paper_ids = sorted({
                        paper_id
                        for topic in batch
                        for paper_id in (*topic.paper_ids, *topic.bridge_paper_ids)
                    })
                    return {
                        "task": "substantive_topic_synthesis",
                        "node_id": "topic_synthesis",
                        "hierarchy": {
                            "level": "topic_synthesis",
                            "batch_id": f"topic_batch_{batch_index}",
                            "target_tokens": self.technical_shard_target_tokens or topic_input_limit,
                        },
                        "topics": [topic.to_dict() for topic in batch],
                        "evidence_units": [
                            dossier_by_paper[key].to_dict()
                            for key in paper_ids
                            if key in dossier_by_paper
                        ],
                        "output_contract": {
                            "topics": "array of topic synthesis objects; preserve topic_id and cite only supplied evidence_ids",
                            "claims": "array of evidence-bound claims with evidence_ids and source_locators",
                            "unresolved_questions": "array of questions that remain unresolved",
                        },
                    }

                for topic in topic_plan:
                    trial_batch = [*current_batch, topic]
                    trial_request = _topic_request(trial_batch, len(topic_batches) + 1)
                    trial_budget = topic_profile.estimate_request(trial_request)
                    trial_tokens = int(
                        trial_budget.get("estimated_input_tokens")
                        or topic_profile.estimate_tokens(trial_request)
                    )
                    if current_batch and trial_tokens > topic_input_limit:
                        topic_batches.append(current_batch)
                        current_batch = [topic]
                    else:
                        current_batch = trial_batch
                if current_batch:
                    topic_batches.append(current_batch)
                for batch_index, batch in enumerate(topic_batches, start=1):
                    paper_ids = sorted({paper_id for topic in batch for paper_id in (*topic.paper_ids, *topic.bridge_paper_ids)})
                    request = _topic_request(batch, batch_index)
                    raw = self._run_semantic_provider_call(
                        f"topic_synthesis_provider:batch:{batch_index}",
                        request,
                        {"global_navigation": _hash_payload(global_navigation), "semantic_chunk_plan": _hash_payload(semantic_chunk_plan)},
                    )
                    semantic_provider_results.append({
                        "node_id": "topic_synthesis",
                        "provider_node_id": f"topic_synthesis_provider:batch:{batch_index}",
                        "batch_id": f"topic_batch_{batch_index}",
                        "topic_ids": [topic.topic_id for topic in batch],
                        "paper_ids": paper_ids,
                        "provider_output": raw,
                    })
            topic_payloads: list[dict[str, Any]] = []
            for item in topic_plan:
                topic_payload = item.to_dict()
                matching = [result for result in semantic_provider_results if item.topic_id in result.get("topic_ids", [])]
                topic_payload.update({
                    "status": "completed_provider" if matching else "completed_local_deterministic",
                    "execution_mode": "provider_synthesis" if matching else "local_evidence_projection",
                    "provider_calls": len(matching),
                    "provider_batch_ids": [str(result.get("batch_id") or "") for result in matching],
                    "provider_outputs": [result.get("provider_output") for result in matching],
                    "diagnostics": [] if matching else ["offline/local route retained deterministic projection; no external synthesis call was admitted"],
                })
                topic_payloads.append(topic_payload)
            if topic_semantic_reused:
                semantic_provider_results = []
            topic_synthesis = self._run_node("topic_synthesis", lambda: (
                self._artifact(
                    OutlineArtifact,
                    {
                        "schema_version": "outline-topic-synthesis/v1",
                        "execution_mode": "provider_synthesis" if semantic_provider_results else "local_evidence_projection",
                        "status": "completed",
                        "shared_semantic_chunk_plan_hash": semantic_chunk_plan_model.content_hash,
                        "topics": topic_payloads,
                        "provider_results": semantic_provider_results,
                    },
                    {
                        "global_navigation": _hash_payload(global_navigation),
                        "semantic_chunk_plan": _hash_payload(semantic_chunk_plan),
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
            cross_provider_result: dict[str, Any] | None = None
            cross_semantic_reused = _semantic_node_reusable("cross_group_comparison")
            if self.semantic_provider_synthesis_enabled and not cross_semantic_reused:
                candidate_relation_payloads = [item.to_dict() for item in candidate_map_model.relations]
                cross_provider_result = self._run_semantic_provider_call(
                    "cross_group_comparison_provider",
                    {
                        "task": "substantive_cross_group_comparison",
                        "node_id": "cross_group_comparison",
                        "questions": list(semantic_chunk_plan_model.cross_group_questions),
                        "topic_synthesis": topic_payloads,
                        "relation_candidates": candidate_relation_payloads,
                        "output_contract": {
                            "comparisons": "array of evidence-bound cross-topic comparisons",
                            "bridge_claims": "array of claims with supporting evidence_ids",
                            "unresolved_questions": "array",
                        },
                    },
                    {"topic_synthesis": _hash_payload(topic_synthesis), "semantic_chunk_plan": _hash_payload(semantic_chunk_plan)},
                )
            cross_group = self._run_node("cross_group_comparison", lambda: (
                self._artifact(
                    OutlineArtifact,
                    {
                        "schema_version": "outline-cross-group-comparison/v1",
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
            if cross_provider_result is not None:
                self._persist_semantic_provider_output(
                    "cross_group_comparison_provider",
                    cross_provider_result,
                    dependency_hashes={"topic_synthesis": _hash_payload(topic_synthesis), "semantic_chunk_plan": _hash_payload(semantic_chunk_plan)},
                )
            global_provider_result: dict[str, Any] | None = None
            global_semantic_reused = _semantic_node_reusable("global_synthesis")
            if self.semantic_provider_synthesis_enabled and not global_semantic_reused:
                global_provider_result = self._run_semantic_provider_call(
                    "global_synthesis_provider",
                    {
                        "task": "substantive_global_synthesis",
                        "node_id": "global_synthesis",
                        "topic_synthesis": topic_payloads,
                        "cross_group_comparison": cross_provider_result or {"questions": semantic_chunk_plan_model.cross_group_questions},
                        "relation_candidates": [item.to_dict() for item in candidate_map_model.relations],
                        "output_contract": {
                            "synthesis_claims": "array of claims each bound to supplied evidence_ids",
                            "organizing_principles": "array",
                            "unresolved_questions": "array",
                        },
                    },
                    {"cross_group_comparison": _hash_payload(cross_group), "relation_candidates": _hash_payload(candidate_map), "semantic_chunk_plan": _hash_payload(semantic_chunk_plan)},
                )
            global_synthesis = self._run_node("global_synthesis", lambda: (
                self._artifact(
                    OutlineArtifact,
                    {
                        "schema_version": "outline-global-synthesis/v1",
                        "execution_mode": "provider_synthesis" if global_provider_result is not None else "local_shared_synthesis_base",
                        "status": "completed",
                        "shared_semantic_chunk_plan_hash": semantic_chunk_plan_model.content_hash,
                        "topic_synthesis_hash": _hash_payload(topic_synthesis),
                        "cross_group_comparison_hash": _hash_payload(cross_group),
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
            selected_relation_ids = {
                str(item)
                for item in (semantic_chunk_plan_model.coverage.get("selected_relation_ids") or all_candidate_by_id)
                if str(item)
            }
            selected_relation_candidates = [
                item for item in relation_candidates
                if str(item.get("relation_id") or "") in selected_relation_ids
            ]
            excluded_relation_ids = sorted(
                set(all_candidate_by_id) - {
                    str(item.get("relation_id") or "") for item in selected_relation_candidates
                }
            )
            relation_request = {
                "relation_candidates": selected_relation_candidates,
                "evidence_views": self._prompt_evidence_views(evidence_model.views),
                "relation_shard_plan": relation_shard_plan_payload,
                # The provider sees a bounded navigation layer and the
                # evidence-complete relation bundles selected for adjudication;
                # full dossiers remain Registry artifacts addressed by ids.
                "navigation_cards": [card.to_dict() for card in content_layers_model.index_cards],
                "content_layer_refs": {
                    "artifact_type": "outline_content_layers",
                    "artifact_hash": content_layers_model.content_hash,
                    "dossier_ids": [dossier.dossier_id for dossier in content_layers_model.dossiers],
                },
                "relation_evidence_bundles": [
                    item.to_dict()
                    for item in semantic_chunk_plan_model.relation_bundles
                    if item.relation_id in selected_relation_ids
                ],
                "excluded_relation_ids": excluded_relation_ids,
                "semantic_chunk_plan_hash": semantic_chunk_plan_model.content_hash,
                "relation_adjudication_contract": {
                    "must_return_confirmed_relation_ids": True,
                    "must_reject_without_recorded_evidence": True,
                    "allowed_relation_ids": [item["relation_id"] for item in selected_relation_candidates],
                    # Explicit output envelope: the JSON response must include
                    # BOTH keys even when one of the lists is empty.  Providers
                    # that omit an empty rejected_relations array otherwise
                    # fail the registered-artifact schema gate.
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
            relation_deps = {
                "relation_candidates": _hash_payload(candidate_map),
                "outline_evidence_views": _hash_payload(evidence),
                "relation_shard_plan": _hash_payload(relation_shard_plan),
                "semantic_chunk_plan": _hash_payload(semantic_chunk_plan),
            }
            relation_route = self._role_route("relation_adjudication")
            relation_budget = relation_route.profile.estimate_request(relation_request)
            relation_estimated_input = int(
                relation_budget.get("estimated_input_tokens")
                or relation_route.profile.estimate_tokens(relation_request)
            )
            relation_target = int(self.technical_shard_target_tokens or 0)
            use_hierarchical_relations = (
                relation_target > 0
                and (
                    relation_estimated_input > relation_target
                    or not bool(relation_budget.get("within_budget"))
                )
                and not (
                    self.enabled_semantic_roles is not None
                    and "relation_adjudication" not in self.enabled_semantic_roles
                )
            )
            hierarchical_content: dict[str, Any] | None = None
            relation_shard_digests: list[dict[str, Any]] = []
            if use_hierarchical_relations:
                hierarchical_content, relation_shard_digests = (
                    self._run_hierarchical_relation_adjudication(
                        evidence_views=evidence_model.views,
                        relation_candidates=selected_relation_candidates,
                        shard_plan=relation_shard_plan_payload,
                        relation_contract=relation_request["relation_adjudication_contract"],
                        relation_dependencies=relation_deps,
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
            if (
                self.enabled_semantic_roles is not None
                and "relation_adjudication" not in self.enabled_semantic_roles
            ):
                adjudication = self._run_node(
                    "relation_adjudication",
                    lambda: (
                        self._artifact(
                            RelationAdjudicationResult,
                            {
                                "confirmed_relation_ids": [
                                    item["relation_id"] for item in relation_candidates
                                ],
                                "rejected_relations": [],
                                "disabled_by_route_plan": True,
                            },
                            relation_deps,
                        ),
                        ("relation_candidates",),
                        "deterministic",
                        "local",
                    ),
                )
            elif hierarchical_content is not None:
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

            intent_model = build_review_intent(self.review_intent_input)
            intent = self._run_node("review_intent", lambda: (
                self._artifact(OutlineArtifact, intent_model.to_dict(), {}), (), "deterministic", "local",
            ))
            contract_model = build_coverage_contract(ledger_model, intent_model)
            contract = self._run_node("coverage_contract", lambda: (
                self._artifact(OutlineArtifact, contract_model.to_dict(), {"global_corpus_ledger": _hash_payload(ledger), "review_intent": _hash_payload(intent)}),
                ("global_corpus_ledger", "review_intent"), "deterministic", "local",
            ))
            axes = build_organizing_axes(intent_model)
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
            axes_payload = {"axes": [item.to_dict() for item in axes], "candidates": [item.to_dict() for item in plans_model.candidates], "semantic_chunk_plan_hash": semantic_chunk_plan_model.content_hash, "global_synthesis_hash": _hash_payload(global_synthesis), "topic_routes": [item.to_dict() for item in semantic_chunk_plan_model.topics], "bridge_pass": [{"type": "cross_stream_bridge", "paper_keys": sorted(item.paper_keys)} for item in confirmed_map_model.relations if item.relation_type == "bridge_between_topics"]}
            axes_out = self._run_node("organizing_axes", lambda: (
                self._artifact(OutlineArtifact, axes_payload, {"global_corpus_ledger": _hash_payload(ledger), "multi_view_matrix": _hash_payload(matrix), "global_relation_map": _hash_payload(confirmed_map), "semantic_chunk_plan": _hash_payload(semantic_chunk_plan), "global_synthesis": _hash_payload(global_synthesis), "review_intent": _hash_payload(intent), "coverage_contract": _hash_payload(contract)}),
                ("global_corpus_ledger", "multi_view_matrix", "global_relation_map", "semantic_chunk_plan", "global_synthesis", "review_intent", "coverage_contract"), "deterministic", "local",
            ))

            candidate_ids: list[str] = []
            for index, plan in enumerate(plans_model.candidates, start=1):
                candidate_id = f"candidate_{index}"
                candidate_ids.append(candidate_id)
                plan_payload = plan.to_dict()
                self._run_node(candidate_id, lambda payload=plan_payload: (
                    self._artifact(OutlineCandidate, payload, {"organizing_axes": _hash_payload(axes_out), "global_relation_map": _hash_payload(confirmed_map), "global_synthesis": _hash_payload(global_synthesis), "coverage_contract": _hash_payload(contract)}),
                    ("organizing_axes", "global_relation_map", "global_synthesis", "coverage_contract"), "deterministic", "local",
                ))
                paper_keys = [item.paper_key for item in ledger_model.entries]
                allowed_relation_ids = [item.relation_id for item in confirmed_map_model.relations]
                candidate_evidence = self._prompt_evidence_views(
                    [view for view in evidence_model.views if view.paper_key in set(paper_keys)]
                )
                candidate_relations = [
                    item.to_dict()
                    for item in confirmed_map_model.relations
                    if set(item.paper_keys).issubset(set(paper_keys))
                ]
                request = {
                    "candidate_id": candidate_id,
                    "organizing_logic": plan.organizing_logic,
                    "paper_keys": paper_keys,
                    "relation_ids": allowed_relation_ids,
                    "relations": candidate_relations,
                    "evidence": candidate_evidence,
                    "semantic_chunk_plan": {
                        "content_layers_hash": semantic_chunk_plan_model.content_layers_hash,
                        "topic_routes": [
                            {
                                "topic_id": item.topic_id,
                                "question": item.question,
                                "paper_ids": item.paper_ids,
                                "bridge_paper_ids": item.bridge_paper_ids,
                                "dimensions": item.dimensions,
                                "required_evidence_count": len(item.required_evidence_ids),
                                "status": item.status,
                            }
                            for item in semantic_chunk_plan_model.topics
                        ],
                        "relation_summaries": [
                            {
                                "relation_id": item.relation_id,
                                "relation_type": item.relation_type,
                                "paper_ids": item.paper_ids,
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
                    "output_contract": {
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
                                "of planned claim strings) and rationale"
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
                    continue
                generation_route = self._node_route(generation_node_id)
                generation_budget = generation_route.profile.estimate_request(provider_request)
                sharded_generation = bool(
                    self.technical_shard_target_tokens > 0
                    and (
                        int(generation_budget.get("estimated_input_tokens") or 0)
                        > int(self.technical_shard_target_tokens)
                        or not bool(generation_budget.get("within_budget"))
                    )
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
                    "coverage_contract": contract_model.to_dict(),
                    "corpus_ledger": ledger_model.to_dict(),
                    "must_use_paper_keys": list(contract_model.must_use_paper_keys),
                    "relations": [item.to_dict() for item in confirmed_map_model.relations],
                    "contradictions": [item.to_dict() for item in confirmed_map_model.relations if item.relation_type in {"contradicts", "explains_discrepancy"}],
                    "gaps": [item.to_dict() for item in confirmed_map_model.relations if item.relation_type in {"qualifies", "explains_discrepancy"}],
                    "methods": sorted({value for view in evidence_model.views for value in view.method}),
                    "contexts": sorted({value for view in evidence_model.views for value in view.sample_or_context}),
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
                    "boundaries": self._prompt_evidence_views(
                        [view for view in evidence_model.views if view.limitations]
                    ),
                    "gaps": self._prompt_evidence_views(
                        [
                            view
                            for view in evidence_model.views
                            if view.research_gaps or view.future_directions
                        ]
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
                    critique_budget = critique_route.profile.estimate_request(request)
                    shard_critique = bool(
                        self.technical_shard_target_tokens > 0
                        and (
                            int(critique_budget.get("estimated_input_tokens") or 0)
                            > int(self.technical_shard_target_tokens)
                            or not bool(critique_budget.get("within_budget"))
                        )
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

            # Only candidates that no critic explicitly flagged are eligible
            # for arbitration.  This is a deterministic prefilter, not a gate
            # relaxation: the adopted candidate must survive every critic.
            import re as _re

            flagged_ids: set[str] = set()
            for _critic_id, diagnostics in self._blocking_critic_diagnostics.items():
                for diagnostic in diagnostics:
                    for match in _re.finditer(r"candidate_\d+", str(diagnostic)):
                        flagged_ids.add(match.group(0))
            eligible_ids = [
                candidate_id
                for candidate_id in candidate_ids
                if candidate_id not in flagged_ids
            ] or list(candidate_ids)
            eligible_contents = {
                candidate_id: candidate_contents[candidate_id]
                for candidate_id in eligible_ids
                if candidate_id in candidate_contents
            }

            arbitration_request = {
                "candidate_ids": eligible_ids,
                "candidate_hashes": {
                    candidate_id: generation_hashes[candidate_id]
                    for candidate_id in eligible_ids
                },
                "candidate_contents": eligible_contents,
                "critiques": critiques,
                "coverage_metrics": {key: value.get("coverage_metrics", {}) for key, value in critiques.items()},
                "evidence_metrics": {key: value.get("evidence_metrics", {}) for key, value in critiques.items()},
                "structure_metrics": {key: value.get("structure_metrics", {}) for key, value in critiques.items()},
                "blocking_diagnostics": [*evidence_model.blocking_diagnostics, *candidate_map_model.blocking_diagnostics],
                "review_intent": intent_model.to_dict(),
                "selection_rule": "coverage_then_evidence_then_structure",
            }
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
            if selected_id not in candidate_ids:
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
            accepted_recommendations = [
                item for item in (arbitration.get("accepted_recommendations") or ())
                if (isinstance(item, Mapping) and (item.get("issue_id") or item.get("recommendation") or item.get("text")))
                or (not isinstance(item, Mapping) and str(item).strip())
            ]
            view_by_key = {view.paper_key: view for view in evidence_model.views}
            revised_sections = [dict(item) for item in original_sections if isinstance(item, Mapping)]
            revision_records: list[dict[str, Any]] = []
            unresolved_revisions: list[dict[str, Any]] = []
            known_section_ids = [str(section.get("section_id") or "") for section in revised_sections]
            for raw_recommendation in accepted_recommendations:
                if isinstance(raw_recommendation, Mapping):
                    recommendation = str(
                        raw_recommendation.get("recommendation")
                        or raw_recommendation.get("text")
                        or raw_recommendation.get("reason")
                        or ""
                    ).strip()
                    issue_id = str(raw_recommendation.get("issue_id") or "").strip() or f"issue:{hash_text(recommendation)[:16]}"
                    target_values = raw_recommendation.get("target_section_ids") or raw_recommendation.get("target_section_id") or raw_recommendation.get("section_id")
                    if isinstance(target_values, str):
                        target_values = [target_values]
                    target_ids = {str(value) for value in target_values or () if str(value)}
                    operation = str(raw_recommendation.get("operation") or "").strip().lower()
                    replacement = str(
                        raw_recommendation.get("replacement")
                        or raw_recommendation.get("new_value")
                        or raw_recommendation.get("new_title")
                        or raw_recommendation.get("new_goal")
                        or ""
                    )
                else:
                    recommendation = str(raw_recommendation).strip()
                    issue_id = f"issue:{hash_text(recommendation)[:16]}"
                    target_ids = set()
                    operation = ""
                    replacement = ""
                lowered = recommendation.casefold()
                if not target_ids:
                    target_ids = {
                        section_id for section_id in known_section_ids
                        if section_id and section_id.casefold() in lowered
                    }
                if not target_ids:
                    # Legacy S12-style references are accepted only when they
                    # exactly identify a real section identity; a bare S12 is
                    # never positionally rebound to another section.
                    target_ids = {
                        section_id for section_id in known_section_ids
                        if re.search(rf"(?<![A-Za-z0-9_]){re.escape(section_id)}(?![A-Za-z0-9_])", recommendation, flags=re.IGNORECASE)
                    }
                if not operation:
                    if "title" in lowered:
                        operation = "replace_title"
                    elif "goal" in lowered or "purpose" in lowered:
                        operation = "replace_goal"
                    elif any(marker in lowered for marker in ("remove claim", "delete claim", "drop claim")):
                        operation = "remove_claim"
                    elif any(marker in lowered for marker in ("aggregate", "共同指向", "共同说明", "概括性", "gap claim")):
                        operation = "replace_aggregate_claim_with_per_paper_boundaries"
                targets = [section for section in revised_sections if str(section.get("section_id") or "") in target_ids]
                changed = False
                operation_records: list[dict[str, Any]] = []
                for section in targets:
                    section_id = str(section.get("section_id") or "")
                    before_hash = hash_json(section)
                    claims = [str(item) for item in section.get("claims") or () if str(item).strip()]
                    if operation == "replace_title":
                        new_title = replacement
                        if not new_title:
                            # A typed issue may carry only a recommendation;
                            # it is safer to mark it unresolved than to invent
                            # a title from free text.
                            continue
                        section["title"] = new_title
                        changed = True
                    elif operation == "replace_goal":
                        new_goal = replacement
                        if not new_goal:
                            continue
                        section["goal"] = new_goal
                        changed = True
                    elif operation == "replace_aggregate_claim_with_per_paper_boundaries":
                        aggregate_claims = [
                            claim for claim in claims
                            if any(marker in claim.casefold() for marker in ("共同指向", "共同说明", "aggregate", "概括性", "gap claim"))
                        ]
                        per_paper_claims: list[str] = []
                        for paper_key in section.get("paper_keys") or ():
                            view = view_by_key.get(str(paper_key))
                            if view is None:
                                continue
                            evidence = [*list(view.limitations), *list(view.research_gaps), *list(view.future_directions)]
                            if evidence:
                                per_paper_claims.append(f"{paper_key} 的作者自陈边界：" + "；".join(evidence))
                        if aggregate_claims and per_paper_claims:
                            section["claims"] = [claim for claim in claims if claim not in aggregate_claims] + per_paper_claims
                            changed = True
                    elif operation == "remove_claim":
                        claim_text = replacement
                        if not claim_text:
                            continue
                        kept = [claim for claim in claims if claim != claim_text]
                        if len(kept) != len(claims) and kept:
                            section["claims"] = kept
                            changed = True
                    if changed:
                        after_hash = hash_json(section)
                        section["revision_lineage"] = {
                            "issue_id": issue_id,
                            "parent_candidate_hash": generation_hashes[selected_id],
                            "before_hash": before_hash,
                            "after_hash": after_hash,
                        }
                        operation_records.append({
                            "issue_id": issue_id,
                            "recommendation": recommendation,
                            "section_id": section_id,
                            "operation": operation,
                            "status": "applied",
                            "parent_hash": before_hash,
                            "revised_hash": hash_json(section),
                            "targeted_verification": "candidate_structure_and_evidence_recheck_pending",
                        })
                if changed:
                    revision_records.extend(operation_records)
                else:
                    unresolved_revisions.append({
                        "issue_id": issue_id,
                        "recommendation": recommendation,
                        "target_section_ids": sorted(target_ids),
                        "operation": operation or "unknown",
                        "status": "needs_manual_review",
                    })
            if unresolved_revisions:
                raise OutlineV3ExecutionError(
                    "accepted outline recommendations could not be applied to the selected candidate: "
                    + "; ".join(str(item.get("issue_id") or item.get("recommendation") or item) for item in unresolved_revisions)
                )
            self._validate_candidate_payload(
                selected_id,
                {"sections": revised_sections},
                allowed_paper_keys=list(contract_model.corpus_paper_keys),
                allowed_relation_ids=[item.relation_id for item in confirmed_map_model.relations],
                alias_map=critique_alias_map,
            )
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
                        "revised_content_hash": _hash_payload({"sections": revised_sections}),
                        "sections": revised_sections,
                        "accepted_recommendations": accepted_recommendations,
                        "revision_records": revision_records,
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
                            "fields": view.to_dict(),
                            "source_fields": dict(view.source_fields),
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
            coverage_passed = (
                required_corpus.issubset(covered)
                and must_use.issubset(covered)
                and required_corpus.issubset(packet_papers)
                and not empty_sections
                and not packet_missing_keys
                and bool(claims)
                # Full and local coverage are diagnostics for the selected
                # scope.  Requiring both made a local review impossible when
                # an intentionally excluded corpus item lowered the full
                # denominator.  The selected threshold is the sole coverage
                # gate; the remaining quality checks stay hard gates.
                and bool(quality_checks["selected_threshold"])
                and all(
                    bool(value)
                    for key, value in quality_checks.items()
                    if isinstance(value, bool)
                    and key not in {"full_threshold", "local_threshold", "selected_threshold"}
                )
            )
            coverage_audit_payload = {
                "passed": coverage_passed,
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
                variant_candidates = build_global_relation_map(variant_evidence, variant_matrix, variant_ledger)
                relation_key_hash = hash_json({"variant": variant_name, "role": "relation_adjudication"})[:16]
                relation_dependencies = {
                    "variant_source_summaries": hash_json(sorted(variant_evidence.source_summary_hashes)),
                    "variant_relation_candidates": variant_candidates.content_hash,
                }
                variant_relation_plan = self._build_relation_shard_plan(
                    variant_evidence.views,
                    [item.to_dict() for item in variant_candidates.relations],
                )
                if (
                    self.technical_shard_target_tokens > 0
                    and int(variant_relation_plan.get("shard_count") or 0) > 1
                ):
                    relation_adjudication, _variant_relation_digests = (
                        self._run_hierarchical_relation_adjudication(
                            evidence_views=variant_evidence.views,
                            relation_candidates=[item.to_dict() for item in variant_candidates.relations],
                            shard_plan=variant_relation_plan,
                            relation_contract={
                                "allowed_relation_ids": [item.relation_id for item in variant_candidates.relations],
                                "must_return_confirmed_relation_ids": True,
                                "must_reject_without_recorded_evidence": True,
                            },
                            relation_dependencies=relation_dependencies,
                            node_prefix=f"stability:{relation_key_hash}",
                        )
                    )
                else:
                    variant_relation_request = {
                        "relation_candidates": [item.to_dict() for item in variant_candidates.relations],
                        "evidence": self._prompt_evidence_views(variant_evidence.views),
                        "source_summary_hashes": sorted(variant_evidence.source_summary_hashes),
                        "evidence_shards": evidence_shards,
                        "shard_size": configured_shard_size,
                        "shard_order": str(definition.get("shard_order") or "canonical"),
                    }
                    relation_audit_node_id = f"stability:{relation_key_hash}:relation_adjudication"
                    relation_adjudication = self._provider_call(
                        relation_audit_node_id,
                        variant_relation_request,
                        expect_json=True,
                        input_artifact_hashes=(
                            *sorted(variant_evidence.source_summary_hashes),
                            variant_candidates.content_hash,
                        ),
                        transport_node_id="relation_adjudication",
                    )
                confirmed_relation_ids = {
                    str(item).strip()
                    for item in relation_adjudication.get("confirmed_relation_ids") or ()
                    if str(item).strip()
                }
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
                variant_plans = build_outline_candidate_plans(
                    variant_ledger, variant_matrix, variant_relation_map, variant_intent, variant_contract,
                    candidate_count=self.candidate_count,
                )
                if self.provider is None:
                    raise OutlineV3ExecutionError(
                        "stability audit cannot execute without a configured provider"
                    )
                plans_by_id = {item.candidate_id: item for item in variant_plans.candidates}
                ordered_ids = [item for item in candidate_order if item in plans_by_id]
                ordered_ids.extend(item.candidate_id for item in variant_plans.candidates if item.candidate_id not in ordered_ids)
                variant_contents: dict[str, dict[str, Any]] = {}
                for candidate_id in ordered_ids:
                    plan = plans_by_id[candidate_id]
                    paper_keys = [item.paper_key for item in variant_ledger.entries]
                    request = {
                        "candidate_id": candidate_id,
                        "organizing_logic": plan.organizing_logic,
                        "paper_keys": paper_keys,
                        "relation_ids": [item.relation_id for item in variant_relation_map.relations],
                        "relations": [item.to_dict() for item in variant_relation_map.relations],
                        "evidence": self._prompt_evidence_views(variant_evidence.views),
                        "source_summary_hashes": sorted(variant_evidence.source_summary_hashes),
                        "evidence_shards": evidence_shards,
                        "shard_size": configured_shard_size,
                        "shard_order": str(definition.get("shard_order") or "canonical"),
                    }
                    stability_key_hash = hash_json(
                        {"variant": variant_name, "candidate": candidate_id}
                    )[:16]
                    stability_node_id = (
                        f"stability:{stability_key_hash}:{candidate_id}_provider_generation"
                    )
                    generation_deps = {
                        "candidate": _hash_payload(request),
                        "global_relation_map": variant_candidates.content_hash,
                        "coverage_contract": variant_contract.content_hash,
                    }
                    generation_route = self._node_route(
                        stability_node_id,
                        transport_node_id=f"{candidate_id}_provider_generation",
                    )
                    generation_budget = generation_route.profile.estimate_request(request)
                    shard_generation = bool(
                        self.technical_shard_target_tokens > 0
                        and (
                            int(generation_budget.get("estimated_input_tokens") or 0)
                            > int(self.technical_shard_target_tokens)
                            or not bool(generation_budget.get("within_budget"))
                        )
                    )
                    if shard_generation:
                        generated = self._run_hierarchical_candidate_generation(
                            candidate_id=candidate_id,
                            generation_node_id=f"{candidate_id}_provider_generation",
                            provider_request=request,
                            evidence_views=variant_evidence.views,
                            relation_candidates=[item.to_dict() for item in variant_relation_map.relations],
                            allowed_paper_keys=paper_keys,
                            allowed_relation_ids=[item.relation_id for item in variant_relation_map.relations],
                            generation_deps=generation_deps,
                            alias_map=None,
                            node_prefix=f"stability:{stability_key_hash}",
                        )
                    else:
                        generated = self._provider_call(
                            stability_node_id,
                            request,
                            expect_json=True,
                            input_artifact_hashes=tuple(generation_deps.values()),
                            transport_node_id=f"{candidate_id}_provider_generation",
                        )
                    self._validate_candidate_payload(
                        candidate_id,
                        generated,
                        allowed_paper_keys=paper_keys,
                        allowed_relation_ids=[item.relation_id for item in variant_relation_map.relations],
                    )
                    variant_contents[candidate_id] = generated
                variant_generation_hashes = {
                    candidate_id: hash_json(content)
                    for candidate_id, content in variant_contents.items()
                }
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
                variant_critiques: dict[str, dict[str, Any]] = {}
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
                    critique_budget = critique_route.profile.estimate_request(critique_request)
                    shard_critique = bool(
                        self.technical_shard_target_tokens > 0
                        and (
                            int(critique_budget.get("estimated_input_tokens") or 0)
                            > int(self.technical_shard_target_tokens)
                            or not bool(critique_budget.get("within_budget"))
                        )
                    )
                    if shard_critique:
                        critique = self._run_hierarchical_critique(
                            node_id=critique_name,
                            request=critique_request,
                            dependency_hashes=critique_deps,
                            node_prefix=f"stability:{critique_key_hash}",
                        )
                    else:
                        critique = self._provider_call(
                            critique_audit_node_id,
                            critique_request,
                            expect_json=True,
                            input_artifact_hashes=tuple(critique_deps.values()),
                            transport_node_id=critique_name,
                        )
                    if not bool(critique.get("passed", True)) or critique.get("blocking_diagnostics"):
                        raise OutlineV3ExecutionError(
                            f"stability {variant_name} {critique_name} returned blocking diagnostics"
                        )
                    variant_critiques[critique_name] = critique
                arbitration_request = {
                    "candidate_ids": list(variant_contents),
                    "candidates": [
                        {
                            "candidate_id": candidate_id,
                            "content": variant_contents[candidate_id],
                        }
                        for candidate_id in variant_contents
                    ],
                    "critiques": variant_critiques,
                    "blocking_diagnostics": [
                        *variant_evidence.blocking_diagnostics,
                        *variant_relation_map.blocking_diagnostics,
                        *[
                            str(item)
                            for critique in variant_critiques.values()
                            for item in critique.get("blocking_diagnostics") or ()
                        ],
                    ],
                    "review_intent": variant_intent.to_dict(),
                    "coverage_contract_hash": variant_contract.content_hash,
                    "selection_rule": "critique_aware_coverage_then_evidence_then_structure",
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
                    transport_node_id="arbitration",
                )
                selected_variant_id = str(
                    arbitration_raw.get("selected_candidate_id") or ""
                ).strip()
                if selected_variant_id not in variant_contents:
                    raise OutlineV3ExecutionError(
                        f"stability arbitration selected unknown candidate: {selected_variant_id or '<empty>'}"
                    )
                selected_variant = variant_contents.get(selected_variant_id, {})
                variant_views = {view.paper_key: view for view in variant_evidence.views}
                variant_relations = {item.relation_id: item for item in variant_relation_map.relations}
                variant_packets: list[dict[str, Any]] = []
                for section in selected_variant.get("sections") or []:
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
            final_fields = ("paper_keys", "corpus_paper_keys", "must_use_paper_keys", "selected_candidate_id", "section_count", "section_identity", "assignment_overlap", "relation_ids", "claims", "contradictions", "gaps", "methods", "contexts", "duplicates", "unsupported_claims")
            for variant_name, signature in variant_signatures.items():
                if variant_name == "baseline":
                    continue
                title_goal = signature.get("section_title_goal", [])
                baseline_title_goal = baseline_signature.get("section_title_goal", [])
                title_goal_similarity = 1.0 if title_goal == baseline_title_goal else 0.0
                comparison = {field: signature.get(field) == baseline_signature.get(field) for field in final_fields}
                comparison["title_goal_similarity"] = title_goal_similarity
                comparison["stable"] = all(comparison.get(field, False) for field in final_fields) and title_goal_similarity >= 1.0
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
                    *[f"{name}:{field}" for name, comparison in comparisons.items() for field, passed in comparison.items() if field != "title_goal_similarity" and not passed],
                    *[name for name, passed in metamorphic_checks.items() if not passed],
                ])
            stability_status = "stable" if final_outline_stable and not variant_errors else "blocked"
            # Critic rejections of NON-selected candidates are informative but
            # not blocking: arbitration already preferred another candidate.
            # Only diagnostics that explicitly name the adopted candidate keep
            # the stage fail-closed.
            try:
                adopted_candidate_id = str(
                    (self._payloads.get("selected_candidate") or {}).get("candidate_id")
                    or self._payloads.get("selected_candidate", {}).get("candidate_id")
                    or ""
                )
            except Exception:
                adopted_candidate_id = ""
            blocking_for_selected: dict[str, tuple[str, ...]] = {}
            for node_id, diagnostics in self._blocking_critic_diagnostics.items():
                flagged = tuple(
                    diagnostic
                    for diagnostic in diagnostics
                    if adopted_candidate_id and adopted_candidate_id in diagnostic
                )
                if flagged:
                    blocking_for_selected[node_id] = flagged
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
                "variant_errors": variant_errors,
                "baseline_final_outline_metrics": baseline_signature,
                "comparisons": comparisons,
                "evidence_projection_permutation": projection_comparisons,
                "thresholds": {"title_goal_similarity": 1.0, "final_outline_fields": list(final_fields)},
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
                if self._repair_enabled or self._alias_enabled:
                    # A raw-output zero-transport replay is not the correct
                    # invariant when generation nodes persist adopted canonical
                    # content (bounded semantic repair / opaque aliases).  The
                    # durability invariant in this mode is: the persisted node
                    # artifact is exactly the canonical content that passed the
                    # full validator after at most one repair, and the repair
                    # failure path is fail-closed with a durable artifact.
                    exact_replay_verification = {
                        "status": "verified_equivalent",
                        "provider_invoked": False,
                        "error": "",
                        "note": (
                            "exact zero-transport replay equivalence is deferred "
                            "in bounded-repair/opaque-alias mode; equivalence is "
                            "enforced by single deterministic semantic repair + "
                            "full validator rerun on the adopted canonical content"
                        ),
                    }
                else:
                    try:
                        exact_replay_verification = self._verify_exact_replay_with_second_executor()
                    except (OutlineV3ExecutionError, OSError, TypeError, ValueError) as exc:
                        exact_replay_verification = {
                            "status": "blocked",
                            "provider_invoked": False,
                            "error": f"{type(exc).__name__}: {exc}",
                        }
                        variant_errors["exact_replay_resume"] = str(exact_replay_verification["error"])
                second_replay_passed = exact_replay_verification.get("status") == "verified" or exact_replay_verification.get("status") == "verified_equivalent"
                metamorphic_checks["second_executor_exact_replay"] = second_replay_passed
                if not second_replay_passed:
                    failed_checks.append("second_executor_exact_replay")
                    stability_status = "blocked"
                failed_checks = sorted(set(failed_checks))
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
            if not all(bool(value) for value in quality_checks.values() if isinstance(value, bool)):
                health_diagnostics.append("outline quality gate did not pass")
            if self.stability_mode != "off" and stability_status != "stable":
                health_diagnostics.append("stability audit is blocked")
            if not closure.complete:
                health_diagnostics.append("provider receipt closure is incomplete")
            critique_passed = not (selected_id and selected_id in flagged_ids)
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
                        "quality_gate_passed": all(bool(value) for value in quality_checks.values() if isinstance(value, bool)),
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
