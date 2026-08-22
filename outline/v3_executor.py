"""Executable Outline Intelligence v3 pipeline.

The executor owns node execution, durable artifact writes, provider receipts,
and replay decisions.  Evidence views are projected directly from Stage 1;
only cross-paper synthesis and outline decisions use the provider boundary.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
import hashlib
import json
import math
import re
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
from outline.v3_models import GlobalRelationMap, OutlineQualityGate, compute_v3_hash
from outline.v3_relations import build_global_relation_map, build_organizing_axes, build_outline_candidate_plans
from runtime.outline_v3_dag import OutlineNodeDAG, OutlineNodeStore
from runtime.provider_completion import ProviderCompletionEvaluator
from runtime.provider_context import ProviderContextProfile
from runtime.provider_receipt_closure import ExpectedProviderCall, ProviderReceiptClosure
from runtime.provider_runtime import (
    ProviderBudgetV1,
    ProviderRuntime,
    ProviderRuntimeLedger,
    compute_closure_epoch_id,
    hash_json,
    hash_text,
)
from runtime.outline_v3_replay import ModelCallReplayKey, ModelCallReplayStore
from services.artifact_registry import ArtifactDependencyRefV2, ArtifactRecord, ArtifactRegistry
from services.job_workspace import publish_bytes_artifact, publish_json_artifact
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
        candidate_count: int = 5,
        review_intent: Mapping[str, Any] | None = None,
        quality_gate: OutlineQualityGate | Mapping[str, Any] | None = None,
        fault_injector: FaultInjector | None = None,
        cancellation_checker: Callable[[], None] | None = None,
        logical_attempt_identity: str | None = None,
        stability_mode: str = "smoke",
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
        existing_ledger = max(
            epoch_ledgers,
            key=lambda record: (record.created_at, record.artifact_id),
            default=None,
        )
        if existing_ledger is not None and existing_ledger.status == "ready":
            try:
                self._receipt_ledger.path.parent.mkdir(parents=True, exist_ok=True)
                self._receipt_ledger.path.write_bytes(Path(existing_ledger.path).read_bytes())
            except OSError:
                pass
        self._replay_store = ModelCallReplayStore(self.workspace)
        self._expected_provider_calls: dict[str, ExpectedProviderCall] = {}
        self._pending_replays: dict[str, tuple[ModelCallReplayKey, str, str]] = {}
        self._replay_evidence: list[dict[str, Any]] = []
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

    def _provider_node_ids(self) -> tuple[str, ...]:
        return (
            "relation_adjudication",
            *(f"candidate_{index}_provider_generation" for index in range(1, self.candidate_count + 1)),
            "structure_critique",
            "coverage_critique",
            "evidence_critique",
            "arbitration",
        )

    def _compute_current_coverage_contract_hash(self) -> str:
        try:
            evidence = build_outline_evidence_views(self.summaries, self.job_id)
            ledger = build_global_corpus_ledger(evidence)
            return build_coverage_contract(ledger, build_review_intent(self.review_intent_input)).content_hash
        except (TypeError, ValueError, KeyError):
            return ""

    def _summary_hashes(self) -> list[str]:
        return sorted(_hash_payload(item) for item in self.summaries)

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

    def _context_profile_hash(self) -> str:
        return _hash_payload({
            "provider": self.profile.provider,
            "model": self.profile.model,
            "endpoint_type": self.profile.endpoint_type,
            "model_context_limit": self.profile.model_context_limit,
            "verified_context_limit": self.profile.verified_context_limit,
            "input_budget": self.profile.input_budget,
            "max_output_tokens": self.profile.max_output_tokens,
            "reasoning_reserve": self.profile.reasoning_reserve,
            "safety_margin": self.profile.safety_margin,
            "tokenizer_strategy": self.profile.tokenizer_strategy,
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

    def _build_provider_call_plans(self) -> tuple[OutlineProviderCallPlan, ...]:
        plans: list[OutlineProviderCallPlan] = []
        for variant_name, variant_summaries, transport_expected in self._provider_call_plan_variants():
            for node_id in self._provider_node_ids():
                representative_request = {
                    "job_id": self.job_id,
                    "stage_name": "outline_v3",
                    "variant_name": variant_name,
                    "node_id": node_id,
                    "summaries": list(variant_summaries),
                    "candidate_count": self.candidate_count,
                    "evidence_bound": True,
                }
                budget = self.profile.estimate_request(representative_request)
                estimated_input = max(
                    1,
                    int(budget.get("estimated_input_tokens") or self.profile.estimate_tokens(representative_request)),
                )
                base_input_estimate = estimated_input
                estimated_output = max(1, int(self.profile.max_output_tokens))
                estimated_reasoning = max(0, int(self.profile.reasoning_reserve))
                candidate_output_upper_bound = self.candidate_count * estimated_output
                critic_input_upper_bound = 0
                if node_id in {"structure_critique", "coverage_critique", "evidence_critique"}:
                    critic_input_upper_bound = candidate_output_upper_bound
                elif node_id == "arbitration":
                    critic_input_upper_bound = candidate_output_upper_bound + 3 * estimated_output
                if critic_input_upper_bound:
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
                        provider=self.profile.provider,
                        model=self.profile.model,
                        endpoint_type=self.profile.endpoint_type,
                        estimated_input_tokens=estimated_input,
                        estimated_output_tokens=estimated_output,
                        estimated_reasoning_tokens=estimated_reasoning,
                        estimated_cached_input_tokens=estimated_cached,
                        estimated_cache_write_tokens=estimated_cache_write,
                        estimated_total_tokens=total,
                        input_cost_per_1k_tokens=self.input_cost_per_1k_tokens,
                        output_cost_per_1k_tokens=self.output_cost_per_1k_tokens,
                        reasoning_cost_per_1k_tokens=self.reasoning_cost_per_1k_tokens,
                        cache_read_cost_per_1k_tokens=self.cache_read_cost_per_1k_tokens,
                        cache_write_cost_per_1k_tokens=self.cache_write_cost_per_1k_tokens,
                        estimated_cost=cost,
                        pricing_source=self.pricing_source,
                        pricing_policy=self.pricing_policy,
                        cost_status="estimate" if self._pricing_is_explicit else "unknown",
                        assumptions=tuple(assumptions),
                        confidence="medium",
                        upper_bound=True,
                        transport_expected=transport_expected,
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
            "provider_configured": self.provider is not None,
            "preflight_status": "accepted",
        }
        preflight_path = self._path(
            f"outline_v3/stability/stability_preflight_{self.closure_epoch_id[:24]}.json"
        )
        if self.stability_mode != "off" and self.provider is None:
            self.stability_preflight["preflight_status"] = "rejected"
            self.stability_preflight["rejection_reason"] = "stability_provider_missing"
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
        elif estimated_input_per_call > self.profile.input_budget:
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
            "structure_critique": "structure_critique",
            "coverage_critique": "coverage_critique",
            "evidence_critique": "evidence_critique",
            "arbitration": "arbitration_decision",
            "selected_candidate": "selected_outline_candidate",
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
    ) -> dict[str, Any]:
        """Build the complete current binding before attempting node reuse."""

        if artifact_type == "outline_artifact":
            artifact_type = self._artifact_type_for_node(node_id)
        review_nodes = {
            "review_intent", "coverage_contract", "organizing_axes", "structure_critique",
            "coverage_critique", "evidence_critique", "arbitration", "selected_candidate",
            "section_evidence_packets", "final_outline", "coverage_audit", "stability_audit",
            "provider_receipt_closure", "stage_health",
        }
        provider_node = node_id in self._provider_node_ids() or node_id.startswith("stability:")
        config = dict(api_config or {})
        if provider_node:
            config.update({
                "provider": self.profile.provider,
                "model": self.profile.model,
                "endpoint_type": self.profile.endpoint_type,
                "route": provider,
            })
        candidate_sensitive = (
            node_id in review_nodes
            or node_id.startswith("candidate_")
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
            "provider_route": provider if provider_node else "local",
            "provider_family": self.profile.provider if provider_node else "local",
            "model_name": self.profile.model if provider_node else model,
            "endpoint_type": self.profile.endpoint_type if provider_node else "internal",
            "prompt_template_hash": prompt_template_hash,
            "prompt_payload_hash": prompt_payload_hash,
            "prompt_hash": hash_text(json.dumps(prompt, sort_keys=True, ensure_ascii=False)) if prompt is not None else "",
            "prompt_id": self._outline_prompt_identity.prompt_id if provider_node else "",
            "prompt_version": self._outline_prompt_identity.version if provider_node else "",
            "prompt_sha256": self._outline_prompt_identity.sha256 if provider_node else "",
            "provider_config_hash": hash_json(api_config) if api_config is not None else "",
            "schema_hash": _hash_payload({"node_id": self._semantic_node_id(node_id), "expect_json": True}),
            "context_profile_hash": self._context_profile_hash() if provider_node else "",
            "relevant_runtime_config_hash": _hash_payload(relevant_config),
        }

    def _provider_binding(self, node_id: str, request: Mapping[str, Any], *, expect_json: bool, input_artifact_hashes: Sequence[str]) -> dict[str, Any]:
        semantic_node_id = self._semantic_node_id(node_id)
        api_config = {
            "provider_family": self.profile.provider,
            "model": self.profile.model,
            "api_base": "internal",
            "endpoint_type": self.profile.endpoint_type,
        }
        return self.build_current_node_binding(
            node_id,
            artifact_type="outline_artifact",
            artifact_version="v3",
            dependency_hashes={
                f"input_{index}": value
                for index, value in enumerate(sorted(str(item) for item in input_artifact_hashes if str(item)))
            },
            model=self.profile.model,
            provider=semantic_node_id,
            prompt=request,
            prompt_template_hash=self._outline_prompt_identity.sha256,
            prompt_payload_hash=hash_json(request),
            api_config=api_config,
        )

    def _replay_record_is_valid(self, record: Any, binding: Mapping[str, Any]) -> bool:
        normalized_hash = str(getattr(record, "normalized_output_hash", "") or getattr(record, "output_hash", ""))
        if not normalized_hash or not getattr(record, "receipt_ids", None):
            return False
        expected_receipts = {
            str(item.receipt_id): item
            for item in self._receipt_ledger.list_receipts()
            if str(item.receipt_id) in set(str(value) for value in record.receipt_ids)
        }
        if len(expected_receipts) != len(set(str(value) for value in record.receipt_ids)):
            return False
        semantic_node_id = str(binding.get("semantic_node_id") or self._semantic_node_id(str(binding.get("node_id") or "")))
        semantic_call_id = f"outline:{semantic_node_id}"
        for receipt in expected_receipts.values():
            if receipt.status != "success" or receipt.response_hash != normalized_hash:
                return False
            if receipt.job_id != self.job_id or receipt.attempt_id != semantic_call_id:
                return False
            if receipt.node_id != semantic_node_id or receipt.call_id != semantic_call_id:
                return False
            if receipt.closure_epoch_id != self.closure_epoch_id:
                return False
            if receipt.prompt_hash != str(binding.get("prompt_hash") or ""):
                return False
            if receipt.input_hash != str(binding.get("prompt_payload_hash") or ""):
                return False
            if receipt.config_hash != str(binding.get("provider_config_hash") or ""):
                return False
            if receipt.schema_hash != str(binding.get("schema_hash") or ""):
                return False
            if receipt.finish_reason == "length" or receipt.incomplete_reason:
                return False
            if bool(binding.get("endpoint_type") not in {"internal", "fixture"}) and receipt.usage_status not in {"reported", "provider_not_supported"}:
                return False
        return True

    def _register_expected_from_binding(self, node_id: str, binding: Mapping[str, Any]) -> str:
        semantic_node_id = str(binding.get("semantic_node_id") or self._semantic_node_id(node_id))
        call_id = f"outline:{semantic_node_id}"
        self._expected_provider_calls[call_id] = ExpectedProviderCall(
            call_id=call_id,
            job_id=self.job_id,
            attempt_id=call_id,
            stage_name="outline_v3",
            node_id=semantic_node_id,
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
            max_attempts=1,
            usage_required=self.profile.endpoint_type not in {"internal", "fixture"},
        )
        return call_id

    def _hydrate_expected_provider_calls(self) -> None:
        """Rebuild expected calls from current execution inputs, never receipts."""

        known_ids = {f"outline:{node_id}" for node_id in self._provider_node_ids()}
        for call_id in sorted(known_ids):
            node_id = call_id.removeprefix("outline:")
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
                max_attempts=1,
                usage_required=self.profile.endpoint_type not in {"internal", "fixture"},
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
            max_attempts=1,
            usage_required=self.profile.endpoint_type not in {"internal", "fixture"},
        )
        return call_id

    def _check(self, node_id: str, *, phase: str = "before") -> None:
        if self.cancellation_checker is not None:
            self.cancellation_checker()
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
        if node.status != "succeeded" or not Path(path).is_file():
            return None
        if node.execution_binding != binding:
            if node.status == "succeeded":
                self._dag = self._node_store.invalidate_subgraph(node_id, reason="execution_binding_changed")
            return None
        try:
            value = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
        if not isinstance(value, Mapping) or str(value.get("content_hash") or "") != str(node.output_hash or ""):
            return None
        payload = value.get("payload")
        if not isinstance(payload, Mapping):
            return None
        if record is None or record.status != "ready":
            return None
        try:
            self.registry.verify_ready_dependencies([ArtifactDependencyRefV2.from_record(record)])
        except Exception:
            return None
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
            self.receipts.extend(replay.record.receipt_ids)
            call_id = self._register_expected_from_binding(node_id, binding)
            self._expected_provider_calls[call_id] = replace(
                self._expected_provider_calls[call_id],
                provider_response_hash=normalized_hash,
                output_hash=normalized_hash,
                normalized_output_hash=normalized_hash,
                artifact_payload_hash=hash_json(payload),
                artifact_content_hash=node.output_hash,
                registry_file_hash=record.content_hash,
                artifact_path=record.path,
                registered_artifact_hash=node.output_hash,
                replay_output_hash=replay.record.output_hash,
                node_output_hash=node.output_hash,
            )
            self._replay_evidence.append({
                "node_id": node_id,
                "semantic_node_id": node_id,
                "closure_epoch_id": self.closure_epoch_id,
                "key_hash": replay_key.key_hash,
                "lookup_status": "hit",
                "provider_invoked": False,
                "reused_artifact_ids": list(replay.record.output_artifact_ids),
                "reused_receipt_ids": list(replay.record.receipt_ids),
                "reused_artifact_id": str(replay.record.output_artifact_ids[0]) if replay.record.output_artifact_ids else "",
                "reused_receipt_id": str(replay.record.receipt_ids[0]) if replay.record.receipt_ids else "",
            })
        self.artifact_paths[node_id] = record.path
        self.artifact_records[node_id] = record
        self._payloads[node_id] = dict(payload)
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

    def _provider_call(
        self,
        node_id: str,
        request: Mapping[str, Any],
        *,
        expect_json: bool = True,
        input_artifact_hashes: Sequence[str] = (),
        transport_node_id: str | None = None,
    ) -> dict[str, Any]:
        request = self._attach_prompt_authority(node_id, request)
        budget = self.profile.estimate_request(request)
        api_config = {
            "provider_family": self.profile.provider,
            "model": self.profile.model,
            "api_base": "internal",
            "endpoint_type": self.profile.endpoint_type,
        }
        binding = self._provider_binding(
            node_id,
            request,
            expect_json=expect_json,
            input_artifact_hashes=input_artifact_hashes,
        )
        call_id = self._register_expected_from_binding(node_id, binding)
        semantic_node_id = self._semantic_node_id(node_id)
        replay_key = ModelCallReplayKey(
            node_id=semantic_node_id,
            node_version="v3",
            schema_version="outline-v3",
            model_route=self.profile.provider,
            model_name=self.profile.model,
            provider=self.profile.provider,
            prompt_template_hash=self._outline_prompt_identity.sha256,
            prompt_payload_hash=hash_json(request),
            input_artifact_hashes=sorted(str(item) for item in input_artifact_hashes if str(item)),
            config_hash=hash_json(api_config),
            execution_binding_hash=self._replay_binding_hash(binding),
        )
        replay_lookup = self._replay_store.lookup(replay_key)
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
                    self.receipts.extend(replay_lookup.record.receipt_ids)
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
                        "reused_artifact_ids": list(replay_lookup.record.output_artifact_ids),
                        "reused_receipt_ids": list(replay_lookup.record.receipt_ids),
                        "reused_artifact_id": str(replay_lookup.record.output_artifact_ids[0]) if replay_lookup.record.output_artifact_ids else "",
                        "reused_receipt_id": str(replay_lookup.record.receipt_ids[0]) if replay_lookup.record.receipt_ids else "",
                    })
                    return dict(payload)
        if replay_lookup.status == "stale":
            self.replay_diagnostics.append(
                f"replay stale for {node_id}: {','.join(replay_lookup.stale_reasons)}"
            )
        runtime = ProviderRuntime(
            budget=ProviderBudgetV1(max_calls=1, max_retries_per_call=0),
            ledger=self._receipt_ledger,
            job_id=self.job_id,
            attempt_id=call_id,
            stage_name="outline_v3",
            route=semantic_node_id,
            node_id=semantic_node_id,
            call_id=call_id,
            closure_epoch_id=self.closure_epoch_id,
            logical_attempt_identity=self.logical_attempt_identity,
            endpoint_type=self.profile.endpoint_type,
            schema_hash=str(binding["schema_hash"]),
            prompt_id=self._outline_prompt_identity.prompt_id,
            prompt_version=self._outline_prompt_identity.version,
            prompt_sha256=self._outline_prompt_identity.sha256,
        )
        if not budget["within_budget"]:
            receipt = runtime.blocked_receipt(prompt=json.dumps(request, sort_keys=True, ensure_ascii=False), input_payload=request, api_config=api_config, message="provider input exceeds verified context budget")
            self.receipts.append(receipt.receipt_id)
            raise OutlineV3ExecutionError(f"provider budget blocked node {node_id}")
        admission = runtime.admit(estimated_tokens=int(budget["estimated_input_tokens"]))
        if self.max_provider_calls is not None and self._provider_call_count >= self.max_provider_calls:
            raise OutlineV3ExecutionError(
                f"outline provider call budget exhausted before {node_id}"
            )
        self._provider_call_count += 1
        if self.provider is None:
            if node_id.startswith("stability:"):
                raise OutlineV3ExecutionError(
                    "stability audit requires a configured provider; fixture responses are not admissible"
                )
            raw = self._fixture_response(node_id, request)
        else:
            self._transport_call_count += 1
            provider_node_id = str(transport_node_id or node_id)
            raw = (
                self.provider(provider_node_id, request)
                if callable(self.provider)
                else self.provider.call(provider_node_id, request)
            )
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
                "closure_epoch_id": self.closure_epoch_id,
                "estimation": budget,
                "replay_status": replay_lookup.status,
                "replay_stale_reasons": list(replay_lookup.stale_reasons),
            },
        )
        self.receipts.append(receipt.receipt_id)
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
        if node_id.startswith("stability:"):
            # Stability calls are real provider calls too.  Persist their
            # output immediately so the exact-replay variant can resolve the
            # same ModelCallReplayStore record on its second execution.
            self._persist_stability_output(
                node_id,
                _as_dict(completion.content),
                dependency_hashes={
                    f"input_{index}": value
                    for index, value in enumerate(input_artifact_hashes)
                },
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
        try:
            policies = json.loads(
                self.prompt_registry.read("outline.node.policies.v3")
            )
        except (OSError, UnicodeError, json.JSONDecodeError):
            policies = {}
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
                    status="failed",
                    input_hash=_hash_payload(dict(binding.get("dependency_hashes") or {})),
                    output_hash="",
                    output_artifact_ids=(),
                    model_route=str(binding.get("provider_route") or ""),
                    model_name=str(binding.get("model_name") or ""),
                    provider=str(binding.get("provider_family") or ""),
                    config_snapshot={"candidate_count": self.candidate_count},
                    budget_snapshot={"input_budget": self.profile.input_budget},
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
        return self._artifact(cls, content, deps), tuple(deps), self.profile.model, self.profile.provider

    def _validate_candidate_payload(
        self,
        candidate_id: str,
        payload: Mapping[str, Any],
        *,
        allowed_paper_keys: Sequence[str],
        allowed_relation_ids: Sequence[str],
    ) -> None:
        sections = payload.get("sections")
        if not isinstance(sections, list) or not sections:
            raise OutlineV3ExecutionError(f"{candidate_id} provider output has no sections")
        allowed_papers = {str(item) for item in allowed_paper_keys}
        allowed_relations = {str(item) for item in allowed_relation_ids}
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
            candidate_map_model = build_global_relation_map(evidence_model, matrix_model, ledger_model)
            candidate_map = self._run_node("relation_candidates", lambda: (
                self._artifact(OutlineArtifact, candidate_map_model.to_dict(), {"multi_view_matrix": _hash_payload(matrix)}),
                ("multi_view_matrix",), "deterministic", "local",
            ))

            relation_candidates = [relation.to_dict() for relation in candidate_map_model.relations]
            relation_request = {
                "relation_candidates": relation_candidates,
                "evidence_views": [view.to_dict() for view in evidence_model.views],
                "relation_adjudication_contract": {
                    "must_return_confirmed_relation_ids": True,
                    "must_reject_without_recorded_evidence": True,
                    "allowed_relation_ids": [item["relation_id"] for item in relation_candidates],
                },
            }
            relation_deps = {"relation_candidates": _hash_payload(candidate_map), "outline_evidence_views": _hash_payload(evidence)}
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
            candidate_by_id = {str(item["relation_id"]): item for item in relation_candidates}
            if not isinstance(adjudication.get("confirmed_relation_ids"), list) or not isinstance(adjudication.get("rejected_relations"), list):
                raise OutlineV3ExecutionError("relation adjudication must return explicit confirmed and rejected lists")
            if len(candidate_by_id) != len(relation_candidates):
                raise OutlineV3ExecutionError("relation candidates contain duplicate relation ids")
            confirmed_ids = [str(item).strip() for item in adjudication["confirmed_relation_ids"] if str(item).strip()]
            rejected_payload = [item for item in adjudication["rejected_relations"] if isinstance(item, Mapping)]
            rejected_ids = [str(raw.get("relation_id") or "").strip() for raw in rejected_payload]
            if len(confirmed_ids) != len(set(confirmed_ids)):
                raise OutlineV3ExecutionError("relation adjudication confirmed a relation more than once")
            if len(rejected_ids) != len(set(rejected_ids)):
                raise OutlineV3ExecutionError("relation adjudication rejected a relation more than once")
            if any(item not in candidate_by_id for item in confirmed_ids):
                raise OutlineV3ExecutionError("relation adjudication confirmed an unknown relation")
            if any(item not in candidate_by_id for item in rejected_ids):
                raise OutlineV3ExecutionError("relation adjudication rejected an unknown relation")
            if set(confirmed_ids) & set(rejected_ids):
                raise OutlineV3ExecutionError("relation adjudication both confirmed and rejected a relation")
            if set(confirmed_ids) | set(rejected_ids) != set(candidate_by_id):
                raise OutlineV3ExecutionError("relation adjudication did not classify every relation candidate")
            confirmed = [candidate_by_id[item] for item in confirmed_ids]
            rejected = [candidate_by_id[item] for item in rejected_ids]
            confirmed_map = self._run_node("global_relation_map", lambda: (
                self._artifact(ConfirmedGlobalRelationMap, {"relations": confirmed, "rejected_relations": rejected, "confirmed_relation_ids": confirmed_ids, "rejected_relation_ids": rejected_ids, "paper_keys": sorted({key for item in confirmed for key in item.get("paper_keys", [])}), "source_artifact_hashes": {"relation_candidates": _hash_payload(candidate_map)}, "blocking_diagnostics": []}, {"relation_adjudication": _hash_payload(adjudication)}),
                ("relation_adjudication", "relation_candidates"), self.profile.model, self.profile.provider,
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
            plans_model = build_outline_candidate_plans(ledger_model, matrix_model, confirmed_map_model, intent_model, contract_model, candidate_count=self.candidate_count)
            axes_payload = {"axes": [item.to_dict() for item in axes], "candidates": [item.to_dict() for item in plans_model.candidates], "bridge_pass": [{"type": "cross_stream_bridge", "paper_keys": sorted(item.paper_keys)} for item in confirmed_map_model.relations if item.relation_type == "bridge_between_topics"]}
            axes_out = self._run_node("organizing_axes", lambda: (
                self._artifact(OutlineArtifact, axes_payload, {"global_corpus_ledger": _hash_payload(ledger), "multi_view_matrix": _hash_payload(matrix), "global_relation_map": _hash_payload(confirmed_map), "review_intent": _hash_payload(intent), "coverage_contract": _hash_payload(contract)}),
                ("global_corpus_ledger", "multi_view_matrix", "global_relation_map", "review_intent", "coverage_contract"), "deterministic", "local",
            ))

            candidate_ids: list[str] = []
            for index, plan in enumerate(plans_model.candidates, start=1):
                candidate_id = f"candidate_{index}"
                candidate_ids.append(candidate_id)
                plan_payload = plan.to_dict()
                self._run_node(candidate_id, lambda payload=plan_payload: (
                    self._artifact(OutlineCandidate, payload, {"organizing_axes": _hash_payload(axes_out), "global_relation_map": _hash_payload(confirmed_map), "coverage_contract": _hash_payload(contract)}),
                    ("organizing_axes", "global_relation_map", "coverage_contract"), "deterministic", "local",
                ))
                paper_keys = [item.paper_key for item in ledger_model.entries]
                allowed_relation_ids = [item.relation_id for item in confirmed_map_model.relations]
                candidate_evidence = [
                    view.to_dict()
                    for view in evidence_model.views
                    if view.paper_key in set(paper_keys)
                ]
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
                    "source_summary_hashes": sorted(evidence_model.source_summary_hashes),
                    "shared_hashes": plan.shared_artifact_hashes,
                    "output_contract": {
                        "sections": "non_empty",
                        "section_paper_keys_must_be_subset_of_evidence": True,
                        "section_relation_ids_must_be_subset_of_relation_ids": True,
                        "planned_claims_must_be_non_empty": True,
                    },
                }
                generation_deps = {
                    "candidate": _hash_payload(request),
                    "global_relation_map": _hash_payload(confirmed_map),
                    "coverage_contract": _hash_payload(contract),
                }
                generation_node_id = f"{candidate_id}_provider_generation"
                generation = self._run_node(
                    generation_node_id,
                    lambda request=request, generation_deps=generation_deps: self._run_provider_node(
                        generation_node_id, request, OutlineCandidate, generation_deps,
                    ),
                    expected_binding=self._provider_binding(
                        generation_node_id, request, expect_json=True,
                        input_artifact_hashes=tuple(generation_deps.values()),
                    ),
                )
                self._validate_candidate_payload(
                    candidate_id,
                    generation,
                    allowed_paper_keys=paper_keys,
                    allowed_relation_ids=allowed_relation_ids,
                )

            generation_hashes = {candidate_id: _hash_payload(self._payloads.get(f"{candidate_id}_provider_generation", {})) for candidate_id in candidate_ids}
            candidate_contents = {
                candidate_id: {
                    "candidate_id": candidate_id,
                    "organizing_logic": str(self._payloads.get(candidate_id, {}).get("organizing_logic") or ""),
                    "sections": list(self._payloads.get(f"{candidate_id}_provider_generation", {}).get("sections") or []),
                    "planned_claims": list(self._payloads.get(f"{candidate_id}_provider_generation", {}).get("claims") or []),
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
            critique_requests = {
                "structure_critique": {
                    "node_id": "structure_critique",
                    "candidate_contents": candidate_contents,
                    "candidate_hashes": generation_hashes,
                    "review_intent": intent_model.to_dict(),
                    "checks": ["section_progression", "duplicate_assignments", "goal_claim_alignment", "placeholder_sections", "empty_research_streams"],
                },
                "coverage_critique": {
                    "node_id": "coverage_critique",
                    "candidate_contents": candidate_contents,
                    "candidate_hashes": generation_hashes,
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
                    "candidate_claims": {key: value.get("planned_claims", []) for key, value in candidate_contents.items()},
                    "section_evidence": {key: value.get("sections", []) for key, value in candidate_contents.items()},
                    "paper_keys": sorted(contract_model.corpus_paper_keys),
                    "source_summary_hashes": sorted(evidence_model.source_summary_hashes),
                    "relation_evidence": [item.to_dict() for item in confirmed_map_model.relations],
                    "contradictions": [item.to_dict() for item in confirmed_map_model.relations if item.relation_type in {"contradicts", "explains_discrepancy"}],
                    "boundaries": [view.to_dict() for view in evidence_model.views if view.limitations],
                    "gaps": [view.to_dict() for view in evidence_model.views if view.research_gaps or view.future_directions],
                },
            }
            for node_id, cls in (("structure_critique", StructureCritique), ("coverage_critique", CoverageCritique), ("evidence_critique", EvidenceCritique)):
                request = critique_requests[node_id]
                critique_deps = {"candidate_generations": _hash_payload(generation_hashes), "coverage_contract": _hash_payload(contract)}
                critiques[node_id] = self._run_node(
                    node_id,
                    lambda request=request, cls=cls, node_id=node_id, critique_deps=critique_deps: self._run_provider_node(node_id, request, cls, critique_deps),
                    expected_binding=self._provider_binding(
                        node_id, request, expect_json=True, input_artifact_hashes=tuple(critique_deps.values()),
                    ),
                )

            arbitration_request = {
                "candidate_ids": candidate_ids,
                "candidate_hashes": generation_hashes,
                "candidate_contents": candidate_contents,
                "critiques": critiques,
                "coverage_metrics": {key: value.get("coverage_metrics", {}) for key, value in critiques.items()},
                "evidence_metrics": {key: value.get("evidence_metrics", {}) for key, value in critiques.items()},
                "structure_metrics": {key: value.get("structure_metrics", {}) for key, value in critiques.items()},
                "blocking_diagnostics": [*evidence_model.blocking_diagnostics, *candidate_map_model.blocking_diagnostics],
                "review_intent": intent_model.to_dict(),
                "selection_rule": "coverage_then_evidence_then_structure",
            }
            arbitration_deps = {
                "structure_critique": _hash_payload(critiques["structure_critique"]),
                "coverage_critique": _hash_payload(critiques["coverage_critique"]),
                "evidence_critique": _hash_payload(critiques["evidence_critique"]),
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
            sections = list(selected_payload.get("sections") or [])
            packets = []
            view_by_key = {view.paper_key: view for view in evidence_model.views}
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
                self._artifact(SectionEvidencePacketSet, {"packets": packets, "coverage_ledger": {"paper_coverage": sorted(set(item for packet in packets for item in packet["paper_keys"])), "must_use_coverage": sorted(set(contract_model.must_use_paper_keys) & set(item for packet in packets for item in packet["paper_keys"])), "claim_coverage": [claim for packet in packets for claim in packet["planned_claims"]]}}, {"selected_candidate": _hash_payload(selected), "global_corpus_ledger": _hash_payload(ledger), "global_relation_map": _hash_payload(confirmed_map)}),
                ("selected_candidate", "global_corpus_ledger", "global_relation_map"), "deterministic", "local",
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
            empty_research_streams = [name for name, values in stream_values.items() if not values]
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
                "max_duplicate_assignments": sum(duplicate_assignments.values()) <= self.quality_gate.max_duplicate_assignments,
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
                and all(bool(value) for value in quality_checks.values() if isinstance(value, bool))
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
                "section_coverage": {"sections": section_count, "effective_section_count": len(effective_sections), "empty_sections": empty_sections, "packet_papers": len(packet_papers), "duplicate_paper_assignments": duplicate_assignments, "placeholder_sections": placeholder_sections},
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
                variant_relation_request = {
                    "relation_candidates": [item.to_dict() for item in variant_candidates.relations],
                    "evidence": [view.to_dict() for view in variant_evidence.views],
                    "source_summary_hashes": sorted(variant_evidence.source_summary_hashes),
                    "evidence_shards": evidence_shards,
                    "shard_size": configured_shard_size,
                    "shard_order": str(definition.get("shard_order") or "canonical"),
                }
                relation_key_hash = hash_json({"variant": variant_name, "role": "relation_adjudication"})[:16]
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
                        "evidence": [view.to_dict() for view in variant_evidence.views],
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
                    generated = self._provider_call(
                        stability_node_id,
                        request,
                        expect_json=True,
                        input_artifact_hashes=(
                            *sorted(variant_evidence.source_summary_hashes),
                            variant_contract.content_hash,
                        ),
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
                    critique = self._provider_call(
                        critique_audit_node_id,
                        critique_request,
                        expect_json=True,
                        input_artifact_hashes=(
                            variant_contract.content_hash,
                            *variant_generation_hashes.values(),
                        ),
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
                    "rerun_replay_exact": bool(comparisons.get("exact_replay_resume", {}).get("stable")),
                    "dependency_binding": dependency_binding,
                    "quality_gate_bound": self.quality_gate.content_hash == _hash_payload(self.quality_gate.to_dict()),
                }
                failed_checks = sorted([
                    *[f"{name}:{field}" for name, comparison in comparisons.items() for field, passed in comparison.items() if field != "title_goal_similarity" and not passed],
                    *[name for name, passed in metamorphic_checks.items() if not passed],
                ])
            stability_status = "stable" if final_outline_stable and not variant_errors else "blocked"
            if self._blocking_critic_diagnostics:
                stability_status = "blocked"
                for node_id, diagnostics in self._blocking_critic_diagnostics.items():
                    variant_errors.setdefault(
                        f"primary:{node_id}",
                        f"{node_id}: {'; '.join(diagnostics)}",
                    )
                failed_checks.extend(
                    f"primary:{node_id}"
                    for node_id in self._blocking_critic_diagnostics
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
            current_receipts = [
                receipt
                for receipt in job_receipts
                if not str(receipt.call_id or "").startswith("outline:stability:")
            ]
            out_of_scope_receipts = [
                receipt for receipt in all_receipts if receipt not in current_receipts
            ]
            canonical_expected = [
                expected
                for expected in self._expected_provider_calls.values()
                if not str(expected.call_id or "").startswith("outline:stability:")
            ]
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
            }
            closure_dependency_ids = ["provider_receipts"]
            closure_dependency_ids.extend(
                expected.node_id
                for expected in canonical_expected
                if expected.node_id in self.artifact_records
            )
            closure_payload = self._persist(
                "provider_receipt_closure",
                self._artifact(
                    ProviderReceiptClosureArtifact,
                    closure_contract,
                    {"provider_receipts": self.artifact_records["provider_receipts"].content_hash},
                ),
                depends_on=tuple(dict.fromkeys(closure_dependency_ids)),
                model="deterministic",
                provider="local",
            )
            closure_record = self.artifact_records["provider_receipt_closure"]
            if self.stability_mode != "off" and not self._skip_exact_replay_verification:
                try:
                    exact_replay_verification = self._verify_exact_replay_with_second_executor()
                except (OutlineV3ExecutionError, OSError, TypeError, ValueError) as exc:
                    exact_replay_verification = {
                        "status": "blocked",
                        "provider_invoked": False,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                    variant_errors["exact_replay_resume"] = str(exact_replay_verification["error"])
                second_replay_passed = exact_replay_verification.get("status") == "verified"
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
            health_diagnostics = list(self.diagnostics)
            if not coverage_passed:
                health_diagnostics.append("coverage audit did not satisfy the explicit corpus contract")
            if not all(bool(value) for value in quality_checks.values() if isinstance(value, bool)):
                health_diagnostics.append("outline quality gate did not pass")
            if self.stability_mode != "off" and stability_status != "stable":
                health_diagnostics.append("stability audit is blocked")
            if not closure.complete:
                health_diagnostics.append("provider receipt closure is incomplete")
            critique_passed = all(bool(item.get("passed")) and not item.get("blocking_diagnostics") for item in critiques.values())
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
                        "provider_receipt_closure": closure_payload,
                    },
                    {
                        "stability_audit": _hash_payload(stability),
                        "coverage_audit": _hash_payload(audit),
                        "arbitration": _hash_payload(arbitration),
                        "provider_receipt_closure": closure_record.content_hash,
                    },
                ),
                depends_on=("stability_audit", "coverage_audit", "arbitration", "provider_receipt_closure"),
                model="deterministic",
                provider="local",
            )
            # Mark critic rejections failed only after their dependent audit
            # artifacts have been materialized.  This preserves a complete
            # audit trail while keeping DAG resume semantics explicit.
            for node_id, diagnostics in self._blocking_critic_diagnostics.items():
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
                loaded_dag = self._node_store.load()
                if loaded_dag is not None:
                    self._dag = loaded_dag
            except Exception:
                pass
            return OutlineV3ExecutionResult(self.job_id, "blocked", False, dict(self.artifact_paths), tuple(node.node_id for node in self._dag.nodes if node.status == "succeeded"), tuple(self.receipts), tuple(self.diagnostics), self._dag)

    execute = run


__all__ = ["OutlineV3ExecutionError", "OutlineV3ExecutionResult", "OutlineV3Executor"]
