"""Durable Outline Intelligence v3 node DAG and fail-closed resume planning."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
import json
from pathlib import Path
import threading
from typing import Any, Dict, Iterable, List, Literal, Mapping, Optional, Sequence, Tuple

from runtime.attempt_store import _write_json_exclusive
from services.artifact_registry import ArtifactRecord, ArtifactRegistry

from outline.v3_models import compute_v3_hash


OUTLINE_V3_NODE_ARTIFACT_TYPE = "outline_v3_node_dag"
OUTLINE_V3_NODE_ARTIFACT_VERSION = "v1"
OUTLINE_V3_NODE_ARTIFACT_ROLE = "outline_v3_node_dag_snapshot"
OUTLINE_V3_NODE_DIR = "outline_v3/node_dag"

NodeStatus = Literal["pending", "running", "succeeded", "failed", "blocked", "cancelled", "stale"]
_NODE_STATUSES = frozenset({"pending", "running", "succeeded", "failed", "blocked", "cancelled", "stale"})


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _safe_text(value: Any) -> str:
    return str(value or "").strip()


def _stable_unique(values: Iterable[Any]) -> List[str]:
    result: Dict[str, str] = {}
    for value in values:
        text = _safe_text(value)
        if text:
            result.setdefault(text.casefold(), text)
    return [result[key] for key in sorted(result)]


def _stable_mapping(value: Any) -> Dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    return {str(key): value[key] for key in sorted(value, key=lambda item: str(item))}


@dataclass(frozen=True)
class OutlineNodeRecord:
    """One node state in the durable v3 DAG."""

    node_id: str
    node_version: str = "v3"
    status: NodeStatus = "pending"
    input_artifact_ids: List[str] = field(default_factory=list)
    input_hash: str = ""
    output_hash: str = ""
    output_artifact_ids: List[str] = field(default_factory=list)
    model_route: str = ""
    model_name: str = ""
    provider: str = ""
    prompt_template_hash: str = ""
    prompt_payload_hash: str = ""
    config_snapshot: Dict[str, Any] = field(default_factory=dict)
    budget_snapshot: Dict[str, Any] = field(default_factory=dict)
    receipt_ids: List[str] = field(default_factory=list)
    attempt_ids: List[str] = field(default_factory=list)
    idempotency_key: str = ""
    created_at: str = field(default_factory=_utc_now_iso)
    completed_at: str = ""
    depends_on: List[str] = field(default_factory=list)
    diagnostics: List[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not _safe_text(self.node_id):
            raise ValueError("node_id is required")
        if self.status not in _NODE_STATUSES:
            raise ValueError(f"unsupported Outline v3 node status: {self.status!r}")
        if not _safe_text(self.node_version):
            raise ValueError("node_version is required")
        if not self.idempotency_key:
            object.__setattr__(self, "idempotency_key", self.compute_idempotency_key())

    def compute_idempotency_key(self) -> str:
        payload = {
            'node_id': self.node_id,
            'node_version': self.node_version,
            'input_artifact_ids': sorted(set(self.input_artifact_ids)),
            'depends_on': sorted(set(self.depends_on)),
            'prompt_template_hash': self.prompt_template_hash,
            'prompt_payload_hash': self.prompt_payload_hash,
        }
        return f"node:{compute_v3_hash(payload)[:32]}"

    def canonical_payload(self) -> Dict[str, Any]:
        return {
            "node_id": self.node_id,
            "node_version": self.node_version,
            "status": self.status,
            "input_artifact_ids": _stable_unique(self.input_artifact_ids),
            "input_hash": self.input_hash,
            "output_hash": self.output_hash,
            "output_artifact_ids": _stable_unique(self.output_artifact_ids),
            "model_route": self.model_route,
            "model_name": self.model_name,
            "provider": self.provider,
            "prompt_template_hash": self.prompt_template_hash,
            "prompt_payload_hash": self.prompt_payload_hash,
            "config_snapshot": _stable_mapping(self.config_snapshot),
            "budget_snapshot": _stable_mapping(self.budget_snapshot),
            "receipt_ids": _stable_unique(self.receipt_ids),
            "attempt_ids": _stable_unique(self.attempt_ids),
            "idempotency_key": self.idempotency_key,
            "depends_on": _stable_unique(self.depends_on),
            "diagnostics": _stable_unique(self.diagnostics),
        }

    @property
    def content_hash(self) -> str:
        return compute_v3_hash(self.canonical_payload())

    def to_dict(self) -> Dict[str, Any]:
        payload = self.canonical_payload()
        payload.update({
            "created_at": self.created_at,
            "completed_at": self.completed_at,
            "content_hash": self.content_hash,
        })
        return payload

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "OutlineNodeRecord":
        raw_status = str(data.get("status") or "pending")
        status = raw_status if raw_status in _NODE_STATUSES else "stale"
        return cls(
            node_id=str(data.get("node_id") or ""),
            node_version=str(data.get("node_version") or "v3"),
            status=status,  # type: ignore[arg-type]
            input_artifact_ids=_stable_unique(data.get("input_artifact_ids") or []),
            input_hash=str(data.get("input_hash") or ""),
            output_hash=str(data.get("output_hash") or ""),
            output_artifact_ids=_stable_unique(data.get("output_artifact_ids") or []),
            model_route=str(data.get("model_route") or ""),
            model_name=str(data.get("model_name") or ""),
            provider=str(data.get("provider") or ""),
            prompt_template_hash=str(data.get("prompt_template_hash") or ""),
            prompt_payload_hash=str(data.get("prompt_payload_hash") or ""),
            config_snapshot=_stable_mapping(data.get("config_snapshot")),
            budget_snapshot=_stable_mapping(data.get("budget_snapshot")),
            receipt_ids=_stable_unique(data.get("receipt_ids") or []),
            attempt_ids=_stable_unique(data.get("attempt_ids") or []),
            idempotency_key=str(data.get("idempotency_key") or ""),
            created_at=str(data.get("created_at") or _utc_now_iso()),
            completed_at=str(data.get("completed_at") or ""),
            depends_on=_stable_unique(data.get("depends_on") or []),
            diagnostics=_stable_unique(data.get("diagnostics") or []),
        )


@dataclass(frozen=True)
class OutlineNodeDAG:
    job_id: str
    dag_version: str = "v3"
    nodes: List[OutlineNodeRecord] = field(default_factory=list)
    created_at: str = field(default_factory=_utc_now_iso)
    updated_at: str = field(default_factory=_utc_now_iso)
    snapshot_sequence: int = 0

    def __post_init__(self) -> None:
        if not _safe_text(self.job_id):
            raise ValueError("job_id is required")
        node_ids = [node.node_id for node in self.nodes]
        if len(node_ids) != len(set(node_ids)):
            raise ValueError("Outline v3 DAG node_id values must be unique")
        known = set(node_ids)
        for node in self.nodes:
            missing = sorted(set(node.depends_on) - known)
            if missing:
                raise ValueError(f"node {node.node_id!r} depends on unknown nodes: {missing}")

    def node_map(self) -> Dict[str, OutlineNodeRecord]:
        return {node.node_id: node for node in self.nodes}

    def get(self, node_id: str) -> OutlineNodeRecord:
        try:
            return self.node_map()[node_id]
        except KeyError as exc:
            raise KeyError(f"unknown Outline v3 node: {node_id}") from exc

    def canonical_payload(self) -> Dict[str, Any]:
        return {
            "job_id": self.job_id,
            "dag_version": self.dag_version,
            "nodes": [node.to_dict() for node in sorted(self.nodes, key=lambda item: item.node_id)],
        }

    @property
    def content_hash(self) -> str:
        return compute_v3_hash(self.canonical_payload())

    @property
    def completed_node_ids(self) -> List[str]:
        return [node.node_id for node in sorted(self.nodes, key=lambda item: item.node_id) if node.status == "succeeded"]

    @property
    def failed_node_ids(self) -> List[str]:
        return [
            node.node_id
            for node in sorted(self.nodes, key=lambda item: item.node_id)
            if node.status in {"failed", "blocked", "cancelled", "stale"}
        ]

    def to_dict(self) -> Dict[str, Any]:
        payload = self.canonical_payload()
        payload.update({
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "snapshot_sequence": self.snapshot_sequence,
            "content_hash": self.content_hash,
        })
        return payload

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "OutlineNodeDAG":
        return cls(
            job_id=str(data.get("job_id") or ""),
            dag_version=str(data.get("dag_version") or "v3"),
            nodes=[OutlineNodeRecord.from_dict(item) for item in data.get("nodes", []) if isinstance(item, Mapping)],
            created_at=str(data.get("created_at") or _utc_now_iso()),
            updated_at=str(data.get("updated_at") or _utc_now_iso()),
            snapshot_sequence=int(data.get("snapshot_sequence") or 0),
        )


def _node(node_id: str, depends_on: Sequence[str] = ()) -> OutlineNodeRecord:
    dependencies = _stable_unique(depends_on)
    return OutlineNodeRecord(
        node_id=node_id,
        input_artifact_ids=list(dependencies),
        depends_on=list(dependencies),
    )


def create_outline_v3_node_dag(job_id: str, *, candidate_count: int = 5) -> OutlineNodeDAG:
    """Create the canonical v3 node graph without executing any provider call."""

    if candidate_count <= 0:
        raise ValueError("candidate_count must be positive")
    nodes: List[OutlineNodeRecord] = [
        _node("outline_evidence_views"),
        _node("global_corpus_ledger", ["outline_evidence_views"]),
        _node("multi_view_matrix", ["outline_evidence_views", "global_corpus_ledger"]),
        _node("relation_candidates", ["multi_view_matrix"]),
        _node("global_relation_map", ["relation_candidates", "multi_view_matrix"]),
        _node("review_intent"),
        _node("coverage_contract", ["global_corpus_ledger", "review_intent"]),
        _node(
            "organizing_axes",
            ["global_corpus_ledger", "multi_view_matrix", "global_relation_map", "review_intent", "coverage_contract"],
        ),
    ]
    candidate_ids = [f"candidate_{index}" for index in range(1, candidate_count + 1)]
    for candidate_id in candidate_ids:
        nodes.append(_node(candidate_id, ["organizing_axes", "global_relation_map", "coverage_contract"]))
        nodes.append(_node(f"{candidate_id}_provider_generation", [candidate_id]))
    provider_nodes = [f"{candidate_id}_provider_generation" for candidate_id in candidate_ids]
    nodes.extend([
        _node("structure_critique", provider_nodes),
        _node("coverage_critique", provider_nodes),
        _node("evidence_critique", ["global_relation_map", *provider_nodes]),
        _node("arbitration", ["structure_critique", "coverage_critique", "evidence_critique"]),
        _node("selected_candidate", ["arbitration"]),
        _node("section_evidence_packets", ["selected_candidate", "global_corpus_ledger", "global_relation_map"]),
        _node("final_outline", ["section_evidence_packets"]),
        _node("coverage_audit", ["final_outline", "coverage_contract"]),
        _node("stability_audit", ["coverage_audit"]),
        _node("stage_health", ["stability_audit"]),
        _node("adoption", ["stage_health", "coverage_audit", "stability_audit"]),
    ])
    return OutlineNodeDAG(job_id=job_id, nodes=nodes)


@dataclass(frozen=True)
class ResumePlan:
    failed_node_id: str = ""
    rerun_node_ids: List[str] = field(default_factory=list)
    preserved_node_ids: List[str] = field(default_factory=list)
    pending_node_ids: List[str] = field(default_factory=list)
    forbidden_node_ids: List[str] = field(default_factory=list)
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "failed_node_id": self.failed_node_id,
            "rerun_node_ids": _stable_unique(self.rerun_node_ids),
            "preserved_node_ids": _stable_unique(self.preserved_node_ids),
            "pending_node_ids": _stable_unique(self.pending_node_ids),
            "forbidden_node_ids": _stable_unique(self.forbidden_node_ids),
            "reason": self.reason,
        }


def _descendants(dag: OutlineNodeDAG, root: str) -> List[str]:
    reverse: Dict[str, List[str]] = {}
    for node in dag.nodes:
        for dependency in node.depends_on:
            reverse.setdefault(dependency, []).append(node.node_id)
    discovered: set[str] = set()
    queue = [root]
    while queue:
        current = queue.pop(0)
        for child in sorted(reverse.get(current, [])):
            if child in discovered:
                continue
            discovered.add(child)
            queue.append(child)
    return sorted(discovered)


def plan_outline_v3_resume(dag: OutlineNodeDAG, failed_node_id: Optional[str] = None) -> ResumePlan:
    """Plan only the failed node and its downstream closure.

    Completed candidate nodes are preserved when a critique or arbitration
    node fails; an adoption failure has no downstream nodes and therefore does
    not rerun the outline.
    """

    failures = dag.failed_node_ids
    target = failed_node_id or (failures[0] if failures else "")
    if not target:
        pending = [node.node_id for node in dag.nodes if node.status == "pending"]
        return ResumePlan(
            pending_node_ids=pending,
            preserved_node_ids=dag.completed_node_ids,
            reason="no failed node; resume pending nodes only",
        )
    node = dag.get(target)
    if node.status not in {"failed", "blocked", "cancelled", "stale"}:
        raise ValueError(f"node {target!r} is not retryable from status {node.status!r}")
    rerun = sorted({target, *_descendants(dag, target)})
    preserved = [node_id for node_id in dag.completed_node_ids if node_id not in rerun]
    pending = [node.node_id for node in dag.nodes if node.status == "pending" and node.node_id not in rerun]
    forbidden = [
        node_id
        for node_id in preserved
        if node_id in {"final_outline", "adoption"} and target in {"structure_critique", "coverage_critique", "evidence_critique"}
    ]
    return ResumePlan(
        failed_node_id=target,
        rerun_node_ids=rerun,
        preserved_node_ids=preserved,
        pending_node_ids=pending,
        forbidden_node_ids=forbidden,
        reason="retry failed node and downstream nodes; preserve completed upstream nodes",
    )


def apply_outline_v3_resume(dag: OutlineNodeDAG, plan: ResumePlan) -> OutlineNodeDAG:
    """Return a new DAG state with the planned closure reset to pending."""

    rerun = set(plan.rerun_node_ids)
    nodes: List[OutlineNodeRecord] = []
    for node in dag.nodes:
        if node.node_id not in rerun:
            nodes.append(node)
            continue
        nodes.append(replace(
            node,
            status="pending",
            input_hash="",
            output_hash="",
            output_artifact_ids=[],
            completed_at="",
            diagnostics=_stable_unique([*node.diagnostics, "retry_requested"]),
        ))
    return replace(dag, nodes=nodes, updated_at=_utc_now_iso())


class OutlineNodeStore:
    """Append-only snapshot store for the v3 DAG."""

    def __init__(self, workspace: Any, registry: ArtifactRegistry | None = None) -> None:
        self.workspace = workspace
        self.registry = registry
        if hasattr(workspace, "artifact_path"):
            root = Path(str(workspace.artifact_path(OUTLINE_V3_NODE_DIR)))
        else:
            root = Path(str(workspace)) / "artifacts" / OUTLINE_V3_NODE_DIR
        self.directory = root
        self._lock = _node_store_lock(str(root))

    def _snapshot_path(self, sequence: int) -> Path:
        return self.directory / f"snapshot-{sequence:06d}.json"

    def _paths(self) -> List[Path]:
        return sorted(self.directory.glob("snapshot-*.json")) if self.directory.exists() else []

    def load(self) -> OutlineNodeDAG | None:
        with self._lock:
            paths = self._paths()
            if not paths:
                return None
            latest: OutlineNodeDAG | None = None
            for expected, path in enumerate(paths, start=1):
                if path.name != f"snapshot-{expected:06d}.json":
                    raise ValueError(f"Outline v3 node snapshot sequence gap at {expected}: {path.name}")
                try:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                    raise ValueError(f"cannot read Outline v3 node snapshot {path}: {exc}") from exc
                if not isinstance(payload, Mapping):
                    raise ValueError(f"Outline v3 node snapshot must be an object: {path}")
                dag = OutlineNodeDAG.from_dict(payload)
                if dag.snapshot_sequence != expected:
                    raise ValueError(f"Outline v3 snapshot sequence mismatch: {path}")
                if str(payload.get("content_hash") or "") != dag.content_hash:
                    raise ValueError(f"Outline v3 node DAG hash mismatch: {path}")
                latest = dag
            return latest

    def save(self, dag: OutlineNodeDAG) -> Tuple[OutlineNodeDAG, ArtifactRecord | None]:
        with self._lock:
            existing = self.load()
            if existing is not None and existing.job_id != dag.job_id:
                raise ValueError("Outline v3 node DAG job_id does not match existing snapshots")
            sequence = (existing.snapshot_sequence if existing is not None else 0) + 1
            current = replace(
                dag,
                snapshot_sequence=sequence,
                created_at=existing.created_at if existing is not None else dag.created_at,
                updated_at=_utc_now_iso(),
            )
            path = self._snapshot_path(sequence)
            _write_json_exclusive(path, current.to_dict())
            record: ArtifactRecord | None = None
            if self.registry is not None:
                record = self.registry.register_file(
                    artifact_role=OUTLINE_V3_NODE_ARTIFACT_ROLE,
                    artifact_type=OUTLINE_V3_NODE_ARTIFACT_TYPE,
                    artifact_version=OUTLINE_V3_NODE_ARTIFACT_VERSION,
                    path=path,
                    producer="runtime.outline_v3_dag.OutlineNodeStore",
                    artifact_id=f"outline-v3-node-dag:{sequence:06d}",
                    metadata={
                        "snapshot_sequence": sequence,
                        "dag_hash": current.content_hash,
                        "job_id": current.job_id,
                    },
                )
            return current, record

    def ensure(self, job_id: str, *, candidate_count: int = 5) -> OutlineNodeDAG:
        current = self.load()
        if current is not None:
            if current.job_id != job_id:
                raise ValueError("existing Outline v3 node DAG belongs to another job")
            return current
        current, _record = self.save(create_outline_v3_node_dag(job_id, candidate_count=candidate_count))
        return current

    def record_node(
        self,
        node_id: str,
        *,
        status: NodeStatus,
        input_hash: str = "",
        output_hash: str = "",
        output_artifact_ids: Sequence[str] = (),
        model_route: str = "",
        model_name: str = "",
        provider: str = "",
        prompt_template_hash: str = "",
        prompt_payload_hash: str = "",
        config_snapshot: Optional[Mapping[str, Any]] = None,
        budget_snapshot: Optional[Mapping[str, Any]] = None,
        receipt_ids: Sequence[str] = (),
        attempt_ids: Sequence[str] = (),
        diagnostics: Sequence[str] = (),
    ) -> OutlineNodeDAG:
        dag = self.load()
        if dag is None:
            raise ValueError("Outline v3 node DAG has not been initialized")
        current = dag.get(node_id)
        if status == "succeeded":
            missing = [dependency for dependency in current.depends_on if dag.get(dependency).status != "succeeded"]
            if missing:
                raise ValueError(f"cannot succeed node {node_id!r}; dependencies are not complete: {missing}")
            if not output_hash:
                raise ValueError("a succeeded Outline v3 node requires output_hash")
        updated = replace(
            current,
            status=status,
            input_hash=input_hash or current.input_hash,
            output_hash=output_hash,
            output_artifact_ids=_stable_unique(output_artifact_ids),
            model_route=model_route or current.model_route,
            model_name=model_name or current.model_name,
            provider=provider or current.provider,
            prompt_template_hash=prompt_template_hash or current.prompt_template_hash,
            prompt_payload_hash=prompt_payload_hash or current.prompt_payload_hash,
            config_snapshot=_stable_mapping(config_snapshot) if config_snapshot is not None else current.config_snapshot,
            budget_snapshot=_stable_mapping(budget_snapshot) if budget_snapshot is not None else current.budget_snapshot,
            receipt_ids=_stable_unique([*current.receipt_ids, *receipt_ids]),
            attempt_ids=_stable_unique([*current.attempt_ids, *attempt_ids]),
            completed_at=_utc_now_iso() if status in {"succeeded", "failed", "blocked", "cancelled", "stale"} else "",
            diagnostics=_stable_unique([*current.diagnostics, *diagnostics]),
        )
        nodes = [updated if node.node_id == node_id else node for node in dag.nodes]
        updated_dag, _record = self.save(replace(dag, nodes=nodes))
        return updated_dag

    def retry_node(self, node_id: str) -> Tuple[OutlineNodeDAG, ResumePlan]:
        dag = self.load()
        if dag is None:
            raise ValueError("Outline v3 node DAG has not been initialized")
        plan = plan_outline_v3_resume(dag, node_id)
        updated, _record = self.save(apply_outline_v3_resume(dag, plan))
        return updated, plan

    def resume(self) -> Tuple[OutlineNodeDAG, ResumePlan]:
        dag = self.load()
        if dag is None:
            raise ValueError("Outline v3 node DAG has not been initialized")
        plan = plan_outline_v3_resume(dag)
        if plan.rerun_node_ids:
            dag, _record = self.save(apply_outline_v3_resume(dag, plan))
        return dag, plan


_NODE_STORE_LOCKS_GUARD = threading.Lock()
_NODE_STORE_LOCKS: Dict[str, threading.RLock] = {}


def _node_store_lock(path: str) -> threading.RLock:
    key = str(Path(path).resolve()).casefold()
    with _NODE_STORE_LOCKS_GUARD:
        return _NODE_STORE_LOCKS.setdefault(key, threading.RLock())


OutlineNodeRecordV1 = OutlineNodeRecord
OutlineNodeDAGV1 = OutlineNodeDAG
ResumePlanV1 = ResumePlan


__all__ = [
    "OUTLINE_V3_NODE_ARTIFACT_TYPE",
    "OUTLINE_V3_NODE_ARTIFACT_VERSION",
    "OUTLINE_V3_NODE_ARTIFACT_ROLE",
    "OutlineNodeRecord",
    "OutlineNodeDAG",
    "ResumePlan",
    "create_outline_v3_node_dag",
    "plan_outline_v3_resume",
    "apply_outline_v3_resume",
    "OutlineNodeStore",
    "OutlineNodeRecordV1",
    "OutlineNodeDAGV1",
    "ResumePlanV1",
]
