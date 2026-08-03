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
        return self.status in {"complete", "ready_for_adoption"}

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
        adopt: bool = False,
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
        if node is None:
            return None
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
        record = self.registry.register_file(
            artifact_role="provider_receipts",
            artifact_type="provider_receipt_ledger",
            artifact_version="v1",
            path=str(path),
            producer="outline.v3_executor.OutlineV3Executor",
            artifact_id="provider_receipts",
            metadata={"receipt_count": len(self._receipt_ledger.list_receipts())},
        )
        self.artifact_paths["provider_receipts"] = record.path
        self.artifact_records["provider_receipts"] = record

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
            adjudication = self._run_node("relation_adjudication", lambda: self._run_provider_node(
                "relation_adjudication",
                relation_request,
                RelationAdjudicationResult,
                {"relation_candidates": _hash_payload(candidate_map), "outline_evidence_views": _hash_payload(evidence)},
            ))
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
                generation = self._run_node(f"{candidate_id}_provider_generation", lambda request=request: self._run_provider_node(f"{candidate_id}_provider_generation", request, OutlineCandidate, {"candidate": _hash_payload(request), "global_relation_map": _hash_payload(confirmed_map), "coverage_contract": _hash_payload(contract)}))
                self._validate_candidate_payload(
                    candidate_id,
                    generation,
                    allowed_paper_keys=paper_keys,
                    allowed_relation_ids=allowed_relation_ids,
                )

            generation_hashes = {candidate_id: _hash_payload(self._payloads.get(f"{candidate_id}_provider_generation", {})) for candidate_id in candidate_ids}
            critiques: dict[str, dict[str, Any]] = {}
            for node_id, cls in (("structure_critique", StructureCritique), ("coverage_critique", CoverageCritique), ("evidence_critique", EvidenceCritique)):
                request = {"node_id": node_id, "candidate_hashes": generation_hashes, "coverage_contract": contract_model.to_dict(), "relation_map": confirmed_map}
                critiques[node_id] = self._run_node(node_id, lambda request=request, cls=cls, node_id=node_id: self._run_provider_node(node_id, request, cls, {"candidate_generations": _hash_payload(generation_hashes), "coverage_contract": _hash_payload(contract)}))

            arbitration_request = {"candidate_ids": candidate_ids, "candidate_hashes": generation_hashes, "critiques": critiques, "selection_rule": "coverage_then_evidence_then_structure"}
            arbitration = self._run_node("arbitration", lambda: self._run_provider_node("arbitration", arbitration_request, ArbitrationDecision, {"structure_critique": _hash_payload(critiques["structure_critique"]), "coverage_critique": _hash_payload(critiques["coverage_critique"]), "evidence_critique": _hash_payload(critiques["evidence_critique"])}))
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
            coverage_passed = (
                required_corpus.issubset(covered)
                and must_use.issubset(covered)
                and required_corpus.issubset(packet_papers)
                and not empty_sections
                and not packet_missing_keys
                and bool(claims)
            )
            audit = self._run_node("coverage_audit", lambda: (
                self._artifact(CoverageAudit, {"passed": coverage_passed, "paper_coverage": {"total": len(corpus), "covered": len(covered & corpus), "missing": sorted(required_corpus - covered), "packet_missing": packet_missing_keys}, "claim_coverage": {"count": len(claims), "claims": claims}, "relation_coverage": {"planned": len(confirmed_map_model.relations), "used": len(used_relations), "unused": sorted(set(item.relation_id for item in confirmed_map_model.relations) - used_relations)}, "must_use_coverage": {"required": sorted(must_use), "covered": sorted(must_use & covered)}, "section_coverage": {"sections": len(sections), "empty_sections": empty_sections, "packet_papers": len(packet_papers)}, "contradiction_coverage": {"count": sum(len(packet.get("contradictions", [])) for packet in packets)}, "gap_coverage": {"count": sum(len(packet.get("gaps", [])) for packet in packets)}}, {"final_outline": _hash_payload(final), "coverage_contract": _hash_payload(contract), "section_evidence_packets": _hash_payload(packet_set)}),
                ("final_outline", "coverage_contract", "section_evidence_packets"), "deterministic", "local",
            ))
            summary_order = [view.paper_key for view in evidence_model.views]
            section_order = [str(section.get("section_id") or "") for section in sections]
            candidate_order_ok = candidate_ids == sorted(candidate_ids, key=lambda item: int(item.rsplit("_", 1)[-1]))
            source_hashes_ok = set(evidence_model.source_summary_hashes) == {view.source_summary_hash for view in evidence_model.views}
            dependency_binding = all(
                bool(packet.get("source_summary_hashes")) and bool(packet.get("evidence_view_hashes"))
                for packet in packets
            )
            stability_checks = {
                "summary_order": summary_order == sorted(summary_order),
                "shard_order": summary_order == sorted(summary_order),
                "candidate_order": candidate_order_ok,
                "section_order": section_order == sorted(section_order),
                "replay": bool(final_payload.get("source_hashes")) and source_hashes_ok,
                "corpus_preserved": covered.issubset(corpus),
                "must_use_preserved": must_use.issubset(covered),
                "dependency_binding": dependency_binding,
            }
            stability_status = "stable" if all(stability_checks.values()) else "blocked"
            stability = self._run_node("stability_audit", lambda: (
                self._artifact(StabilityAudit, {"status": stability_status, "checks": stability_checks}, {"coverage_audit": _hash_payload(audit), "final_outline": _hash_payload(final)}),
                ("coverage_audit", "final_outline"), "deterministic", "local",
            ))
            health_diagnostics = list(self.diagnostics)
            if not coverage_passed:
                health_diagnostics.append("coverage audit did not satisfy the explicit corpus contract")
            if stability_status != "stable":
                health_diagnostics.append("stability audit is blocked")
            critique_passed = all(bool(item.get("passed")) and not item.get("blocking_diagnostics") for item in critiques.values())
            if not critique_passed:
                health_diagnostics.append("one or more provider-derived critiques did not pass")
            adoption_eligible = not health_diagnostics and bool(arbitration.get("selected_candidate_id"))
            health = self._run_node("stage_health", lambda: (
                self._artifact(OutlineStageHealth, {"status": "healthy" if adoption_eligible else "blocked", "adoption_eligible": adoption_eligible, "diagnostics": health_diagnostics, "node_count": len(self._dag.nodes), "receipt_count": len(self.receipts), "coverage_audit_hash": _hash_payload(audit), "stability_audit_hash": _hash_payload(stability)}, {"stability_audit": _hash_payload(stability), "coverage_audit": _hash_payload(audit), "arbitration": _hash_payload(arbitration)}),
                ("stability_audit", "coverage_audit", "arbitration"), "deterministic", "local",
            ))
            adopted = False
            if self.adopt_requested and adoption_eligible:
                adoption = self._run_node("adoption", lambda: (
                    self._artifact(AdoptedOutline, {"status": "adopted", "adopted_by": self.adopted_by, "final_outline_hash": _hash_payload(final), "coverage_audit_hash": _hash_payload(audit), "stability_audit_hash": _hash_payload(stability), "stage_health_hash": _hash_payload(health)}, {"final_outline": _hash_payload(final), "coverage_audit": _hash_payload(audit), "stability_audit": _hash_payload(stability), "stage_health": _hash_payload(health)}),
                    ("final_outline", "coverage_audit", "stability_audit", "stage_health"), self.profile.model, self.profile.provider,
                ))
                adopted = adoption.get("status") == "adopted"
            else:
                adopted = False
            self._register_receipt_ledger()
            loaded_dag = self._node_store.load()
            if loaded_dag is not None:
                self._dag = loaded_dag
            if self._dag.failed_node_ids or not adoption_eligible:
                status = "blocked"
            elif adopted:
                status = "complete"
            else:
                status = "ready_for_adoption"
            return OutlineV3ExecutionResult(self.job_id, status, adopted, dict(self.artifact_paths), tuple(node.node_id for node in self._dag.nodes if node.status == "succeeded"), tuple(self.receipts), tuple(self.diagnostics), self._dag)
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
