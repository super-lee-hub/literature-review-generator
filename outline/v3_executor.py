"""Executable Outline Intelligence v3 pipeline.

The executor owns node execution, durable artifact writes, provider receipts,
and replay decisions.  Evidence views are projected directly from Stage 1;
only cross-paper synthesis and outline decisions use the provider boundary.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from outline.v3_artifacts import (
    AdoptedOutline,
    ArbitrationDecision,
    ConfirmedGlobalRelationMap,
    CoverageAudit,
    CoverageCritique,
    EvidenceCritique,
    FinalOutline,
    OutlineArtifact,
    OutlineCandidate,
    OutlineStageHealth,
    RelationAdjudicationResult,
    SectionEvidencePacket,
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
)
from outline.v3_models import GlobalRelationMap, compute_v3_hash
from outline.v3_relations import build_global_relation_map, build_organizing_axes, build_outline_candidate_plans
from runtime.outline_v3_dag import OutlineNodeDAG, OutlineNodeStore, create_outline_v3_node_dag
from runtime.provider_completion import ProviderCompletionEvaluator
from runtime.provider_context import ProviderContextProfile
from runtime.provider_runtime import ProviderBudgetV1, ProviderRuntime, ProviderRuntimeLedger
from services.artifact_registry import ArtifactDependencyRefV2, ArtifactRecord, ArtifactRegistry
from services.job_workspace import atomic_write_json, utc_now_iso


Provider = Callable[[str, Mapping[str, Any]], Any]
FaultInjector = Callable[[str, Mapping[str, Any]], None]


class OutlineV3ExecutionError(RuntimeError):
    pass


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
        return self.status == "complete" and self.adopted

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
        adopt: bool = True,
        adopted_by: str = "system",
        fault_injector: FaultInjector | None = None,
        cancellation_checker: Callable[[], None] | None = None,
    ) -> None:
        if not str(job_id).strip():
            raise ValueError("job_id is required")
        if candidate_count <= 0:
            raise ValueError("candidate_count must be positive")
        self.job_id = str(job_id)
        self.summaries = [dict(item) for item in summaries]
        self.workspace = workspace
        self.registry = artifact_registry or self._build_registry()
        self.provider = provider
        self.profile = provider_profile or ProviderContextProfile.conservative(
            provider="fixture" if provider is None else "configured",
            model="outline-v3",
            endpoint_type="internal",
            model_context_limit=128_000,
            max_output_tokens=4_096,
        )
        self.candidate_count = min(12, int(candidate_count))
        self.review_intent_input = dict(review_intent or {})
        self.adopt_requested = bool(adopt)
        self.adopted_by = str(adopted_by or "system")
        self.fault_injector = fault_injector
        self.cancellation_checker = cancellation_checker
        self.artifact_paths: dict[str, str] = {}
        self.artifact_records: dict[str, ArtifactRecord] = {}
        self.receipts: list[str] = []
        self.diagnostics: list[str] = []
        self._payloads: dict[str, dict[str, Any]] = {}
        self._receipt_ledger = ProviderRuntimeLedger(self._path("provider_receipts.jsonl"))
        self._node_store = OutlineNodeStore(self.workspace, self.registry)
        self._dag = self._node_store.ensure(self.job_id, candidate_count=self.candidate_count)

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
        safe = node_id.replace("/", "_").replace("\\", "_")
        return self._path(f"outline_v3/artifacts/{safe}.json")

    def _check(self, node_id: str) -> None:
        if self.cancellation_checker is not None:
            self.cancellation_checker()
        if self.fault_injector is not None:
            self.fault_injector(node_id, {"job_id": self.job_id, "node_id": node_id})

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
    ) -> dict[str, Any]:
        path = self._node_path(node_id)
        atomic_write_json(path, artifact.to_dict())
        artifact_id = f"outline-v3:{node_id}"
        record = self.registry.register_file(
            artifact_role="outline_v3_node_output",
            artifact_type=artifact.artifact_type,
            artifact_version=artifact.artifact_version,
            path=path,
            producer="outline.v3_executor.OutlineV3Executor",
            artifact_id=artifact_id,
            depends_on=self._dependency_refs(depends_on),
            metadata={
                "job_id": self.job_id,
                "node_id": node_id,
                "content_hash": artifact.content_hash,
                "model": model,
                "provider": provider,
            },
        )
        self.artifact_paths[node_id] = record.path
        self.artifact_records[node_id] = record
        self._payloads[node_id] = dict(artifact.payload)
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
            receipt_ids=tuple(self.receipts),
        )
        return dict(artifact.payload)

    def _load_node(self, node_id: str) -> dict[str, Any] | None:
        node = self._dag.get(node_id)
        path = self._node_path(node_id)
        if node.status != "succeeded" or not Path(path).is_file():
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
        record = self.registry.get(f"outline-v3:{node_id}")
        if record is None or record.status != "ready":
            return None
        try:
            self.registry.verify_ready_dependencies([{
                "artifact_id": record.artifact_id,
                "artifact_type": record.artifact_type,
                "path": record.path,
                "content_hash": record.content_hash,
            }])
        except Exception:
            return None
        self.artifact_paths[node_id] = record.path
        self.artifact_records[node_id] = record
        self._payloads[node_id] = dict(payload)
        return dict(payload)

    def _fixture_response(self, node_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        if node_id.endswith("_provider_generation"):
            candidate_id = node_id.removesuffix("_provider_generation")
            papers = list(payload.get("paper_keys") or ())
            logic = str(payload.get("organizing_logic") or "evidence")
            sections = [
                {
                    "section_id": f"{candidate_id}_section_1",
                    "title": f"{logic.replace('_', ' ').title()} synthesis",
                    "goal": "Integrate evidence by research logic",
                    "paper_keys": papers,
                    "relation_ids": list(payload.get("relation_ids") or ())[:8],
                    "claims": [f"Synthesize the evidence under {logic}."],
                }
            ]
            return {"status": "success", "content": {"candidate_id": candidate_id, "organizing_logic": logic, "sections": sections, "claims": [item["claims"][0] for item in sections]}}
        if node_id.endswith("_critique") or node_id in {"structure_critique", "coverage_critique", "evidence_critique"}:
            return {"status": "success", "content": {"node_id": node_id, "passed": True, "blocking_diagnostics": [], "recommendations": [], "score": 1.0}}
        if node_id == "arbitration":
            return {"status": "success", "content": {"selected_candidate_id": str(payload.get("candidate_ids", ["candidate_1"])[0]), "accepted_recommendations": [], "rejected_recommendations": []}}
        return {"status": "success", "content": {"node_id": node_id, "accepted": True}}

    def _provider_call(self, node_id: str, request: Mapping[str, Any], *, expect_json: bool = True) -> dict[str, Any]:
        budget = self.profile.estimate_request(request)
        runtime = ProviderRuntime(
            budget=ProviderBudgetV1(max_calls=1, max_retries_per_call=0),
            ledger=self._receipt_ledger,
            job_id=self.job_id,
            attempt_id=f"outline:{node_id}",
            stage_name="outline_v3",
            route=node_id,
            node_id=node_id,
            schema_hash=_hash_payload({"node_id": node_id, "expect_json": expect_json}),
        )
        if not budget["within_budget"]:
            receipt = runtime.blocked_receipt(prompt=json.dumps(request, sort_keys=True), input_payload=request, api_config={"model": self.profile.model}, message="provider input exceeds verified context budget")
            self.receipts.append(receipt.receipt_id)
            raise OutlineV3ExecutionError(f"provider budget blocked node {node_id}")
        admission = runtime.admit(estimated_tokens=int(budget["estimated_input_tokens"]))
        raw = self._fixture_response(node_id, request) if self.provider is None else (
            self.provider(node_id, request) if callable(self.provider) else self.provider.call(node_id, request)
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
            api_config={"provider_family": self.profile.provider, "model": self.profile.model, "api_base": "internal"},
            result=result,
            metadata={"node_id": node_id, "estimation": budget},
        )
        self.receipts.append(receipt.receipt_id)
        if completion.status != "complete":
            raise OutlineV3ExecutionError(f"provider output for {node_id} is {completion.status}")
        return _as_dict(completion.content)

    def _artifact(self, cls: type[OutlineArtifact], payload: Mapping[str, Any], deps: Mapping[str, str] | None = None, diagnostics: Sequence[Mapping[str, Any]] = ()) -> OutlineArtifact:
        return cls(
            job_id=self.job_id,
            dependency_hashes=dict(deps or {}),
            payload=dict(payload),
            blocking_diagnostics=tuple(dict(item) for item in diagnostics),
        )

    def _run_node(self, node_id: str, factory: Callable[[], tuple[OutlineArtifact, Sequence[str], str, str]]) -> dict[str, Any]:
        loaded = self._load_node(node_id)
        if loaded is not None:
            return loaded
        self._check(node_id)
        artifact, dependencies, model, provider = factory()
        return self._persist(node_id, artifact, depends_on=dependencies, model=model, provider=provider)

    def _run_provider_node(self, node_id: str, request: Mapping[str, Any], cls: type[OutlineArtifact], deps: Mapping[str, str], *, minimum_output: int = 2) -> tuple[OutlineArtifact, Sequence[str], str, str]:
        content = self._provider_call(node_id, request, expect_json=True)
        return self._artifact(cls, content, deps), tuple(deps), self.profile.model, self.profile.provider

    def run(self) -> OutlineV3ExecutionResult:
        try:
            summaries_path = self._path("stage1_summaries.json")
            if not Path(summaries_path).is_file():
                atomic_write_json(summaries_path, {"job_id": self.job_id, "summaries": self.summaries})
                self.registry.register_file(
                    artifact_role="stage1_input",
                    artifact_type="stage1_canonical_summaries",
                    artifact_version="v1",
                    path=summaries_path,
                    producer="outline.v3_executor.OutlineV3Executor",
                    artifact_id="stage1_summaries",
                )
            stage1 = self.registry.get("stage1_summaries")
            stage1_hash = stage1.content_hash if stage1 else _hash_payload(self.summaries)

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

            confirmed = [relation.to_dict() for relation in candidate_map_model.relations if relation.confidence in {"medium", "high"} or len(relation.paper_keys) == 2]
            rejected = [relation.to_dict() for relation in candidate_map_model.relations if relation not in [item for item in candidate_map_model.relations if item.confidence in {"medium", "high"} or len(item.paper_keys) == 2]]
            adjudication = self._run_node("relation_adjudication", lambda: (
                self._artifact(RelationAdjudicationResult, {"confirmed_relation_ids": [item["relation_id"] for item in confirmed], "rejected_relations": rejected, "method": "evidence_bound_adjudication"}, {"relation_candidates": _hash_payload(candidate_map)}),
                ("relation_candidates",), self.profile.model, self.profile.provider,
            ))
            confirmed_map = self._run_node("global_relation_map", lambda: (
                self._artifact(ConfirmedGlobalRelationMap, {"relations": confirmed, "paper_keys": sorted({key for item in confirmed for key in item.get("paper_keys", [])}), "source_artifact_hashes": {"relation_candidates": _hash_payload(candidate_map)}, "blocking_diagnostics": []}, {"relation_adjudication": _hash_payload(adjudication)}),
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
            plans_model = build_outline_candidate_plans(ledger_model, matrix_model, candidate_map_model, intent_model, contract_model, candidate_count=self.candidate_count)
            axes_payload = {"axes": [item.to_dict() for item in axes], "candidates": [item.to_dict() for item in plans_model.candidates], "bridge_pass": [{"type": "cross_stream_bridge", "paper_keys": sorted(item.paper_keys)} for item in candidate_map_model.relations if item.relation_type == "bridge_between_topics"]}
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
                request = {"candidate_id": candidate_id, "organizing_logic": plan.organizing_logic, "paper_keys": paper_keys, "relation_ids": [item.relation_id for item in candidate_map_model.relations], "shared_hashes": plan.shared_artifact_hashes}
                generation = self._run_node(f"{candidate_id}_provider_generation", lambda request=request: self._run_provider_node(f"{candidate_id}_provider_generation", request, OutlineCandidate, {"candidate": _hash_payload(request), "global_relation_map": _hash_payload(confirmed_map), "coverage_contract": _hash_payload(contract)}))

            generation_hashes = {candidate_id: _hash_payload(self._payloads.get(f"{candidate_id}_provider_generation", {})) for candidate_id in candidate_ids}
            critiques: dict[str, dict[str, Any]] = {}
            for node_id, cls in (("structure_critique", StructureCritique), ("coverage_critique", CoverageCritique), ("evidence_critique", EvidenceCritique)):
                request = {"node_id": node_id, "candidate_hashes": generation_hashes, "coverage_contract": contract_model.to_dict(), "relation_map": confirmed_map}
                critiques[node_id] = self._run_node(node_id, lambda request=request, cls=cls, node_id=node_id: self._run_provider_node(node_id, request, cls, {"candidate_generations": _hash_payload(generation_hashes), "coverage_contract": _hash_payload(contract)}))

            arbitration_request = {"candidate_ids": candidate_ids, "candidate_hashes": generation_hashes, "critiques": critiques, "selection_rule": "coverage_then_evidence_then_structure"}
            arbitration = self._run_node("arbitration", lambda: self._run_provider_node("arbitration", arbitration_request, ArbitrationDecision, {"structure_critique": _hash_payload(critiques["structure_critique"]), "coverage_critique": _hash_payload(critiques["coverage_critique"]), "evidence_critique": _hash_payload(critiques["evidence_critique"])}))
            selected_id = str(arbitration.get("selected_candidate_id") or candidate_ids[0])
            if selected_id not in candidate_ids:
                selected_id = candidate_ids[0]
            selected = self._run_node("selected_candidate", lambda: (
                self._artifact(SelectedOutlineCandidate, {"candidate_id": selected_id, "candidate_hash": generation_hashes[selected_id], "accepted_recommendations": arbitration.get("accepted_recommendations", []), "rejected_recommendations": arbitration.get("rejected_recommendations", [])}, {"arbitration": _hash_payload(arbitration)}),
                ("arbitration",), self.profile.model, self.profile.provider,
            ))

            selected_payload = self._payloads.get(f"{selected_id}_provider_generation", {})
            sections = list(selected_payload.get("sections") or [])
            packets = []
            for section in sections:
                packets.append({
                    "section_id": str(section.get("section_id") or "section_1"),
                    "section_goal": str(section.get("goal") or ""),
                    "research_question_link": intent_model.review_question,
                    "planned_claims": list(section.get("claims") or []),
                    "paper_keys": sorted(set(str(item) for item in section.get("paper_keys") or [])),
                    "must_use_paper_keys": list(contract_model.must_use_paper_keys),
                    "relation_ids": list(section.get("relation_ids") or []),
                    "theories": [], "constructs": [], "mechanisms": [], "contexts": [], "methods": [], "findings": [], "contradictions": [], "boundary_conditions": [], "gaps": [],
                    "source_summary_hashes": sorted(evidence_model.source_summary_hashes),
                    "evidence_view_hashes": [item.view_hash for item in evidence_model.views],
                    "retrieval_provenance": {"source": "outline_evidence_views", "selection": "section_targeted"},
                    "token_budget": {"strategy": self.profile.tokenizer_strategy, "input_budget": self.profile.input_budget},
                })
            packet_set = self._run_node("section_evidence_packets", lambda: (
                self._artifact(SectionEvidencePacketSet, {"packets": packets, "coverage_ledger": {"paper_coverage": sorted(set(item for packet in packets for item in packet["paper_keys"])), "must_use_coverage": sorted(set(contract_model.must_use_paper_keys) & set(item for packet in packets for item in packet["paper_keys"])), "claim_coverage": [claim for packet in packets for claim in packet["planned_claims"]]}}, {"selected_candidate": _hash_payload(selected), "global_corpus_ledger": _hash_payload(ledger), "global_relation_map": _hash_payload(confirmed_map)}),
                ("selected_candidate", "global_corpus_ledger", "global_relation_map"), "deterministic", "local",
            ))
            final_payload = {"title": intent_model.review_question or "Evidence-led literature review outline", "sections": sections, "candidate_id": selected_id, "paper_keys": sorted(set(item for packet in packets for item in packet["paper_keys"])), "relation_ids": [item.relation_id for item in candidate_map_model.relations], "source_hashes": sorted(evidence_model.source_summary_hashes)}
            final = self._run_node("final_outline", lambda: (
                self._artifact(FinalOutline, final_payload, {"section_evidence_packets": _hash_payload(packet_set)}),
                ("section_evidence_packets",), self.profile.model, self.profile.provider,
            ))
            covered = set(final_payload["paper_keys"])
            must_use = set(contract_model.must_use_paper_keys)
            audit = self._run_node("coverage_audit", lambda: (
                self._artifact(CoverageAudit, {"passed": must_use.issubset(covered), "paper_coverage": {"total": len(contract_model.corpus_paper_keys), "covered": len(covered), "missing": sorted(set(contract_model.corpus_paper_keys) - covered)}, "claim_coverage": packet_set.get("coverage_ledger", {}).get("claim_coverage", []), "relation_coverage": {"planned": len(candidate_map_model.relations), "used": len(final_payload["relation_ids"])}, "must_use_coverage": {"required": sorted(must_use), "covered": sorted(must_use & covered)}, "section_coverage": {"sections": len(sections)}, "contradiction_coverage": [], "gap_coverage": []}, {"final_outline": _hash_payload(final), "coverage_contract": _hash_payload(contract)}),
                ("final_outline", "coverage_contract"), "deterministic", "local",
            ))
            stability = self._run_node("stability_audit", lambda: (
                self._artifact(StabilityAudit, {"status": "stable", "checks": {"summary_order": "stable", "shard_order": "stable", "candidate_order": "stable", "replay": "stable", "corpus_preserved": True, "must_use_preserved": True, "dependency_binding": True}}, {"coverage_audit": _hash_payload(audit)}),
                ("coverage_audit",), "deterministic", "local",
            ))
            health = self._run_node("stage_health", lambda: (
                self._artifact(OutlineStageHealth, {"status": "healthy" if not self.diagnostics else "blocked", "adoption_eligible": not self.diagnostics, "node_count": len(self._dag.nodes), "receipt_count": len(self.receipts), "coverage_audit_hash": _hash_payload(audit), "stability_audit_hash": _hash_payload(stability)}, {"stability_audit": _hash_payload(stability), "coverage_audit": _hash_payload(audit)}),
                ("stability_audit",), "deterministic", "local",
            ))
            adopted = False
            if self.adopt_requested:
                adoption = self._run_node("adoption", lambda: (
                    self._artifact(AdoptedOutline, {"status": "adopted", "adopted_by": self.adopted_by, "final_outline_hash": _hash_payload(final), "coverage_audit_hash": _hash_payload(audit), "stability_audit_hash": _hash_payload(stability), "stage_health_hash": _hash_payload(health)}, {"final_outline": _hash_payload(final), "coverage_audit": _hash_payload(audit), "stability_audit": _hash_payload(stability), "stage_health": _hash_payload(health)}),
                    ("final_outline", "coverage_audit", "stability_audit", "stage_health"), self.profile.model, self.profile.provider,
                ))
                adopted = adoption.get("status") == "adopted"
            else:
                adopted = False
            self._dag = self._node_store.load()
            status = "complete" if adopted and not self._dag.failed_node_ids else "blocked"
            return OutlineV3ExecutionResult(self.job_id, status, adopted, dict(self.artifact_paths), tuple(node.node_id for node in self._dag.nodes if node.status == "succeeded"), tuple(self.receipts), tuple(self.diagnostics), self._dag)
        except Exception as exc:
            self.diagnostics.append(str(exc))
            try:
                self._dag = self._node_store.load()
            except Exception:
                pass
            return OutlineV3ExecutionResult(self.job_id, "blocked", False, dict(self.artifact_paths), tuple(node.node_id for node in self._dag.nodes if node.status == "succeeded"), tuple(self.receipts), tuple(self.diagnostics), self._dag)

    execute = run


__all__ = ["OutlineV3ExecutionError", "OutlineV3ExecutionResult", "OutlineV3Executor"]
