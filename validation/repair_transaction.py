"""Audited, version-producing repair transactions.

The historical repair helpers operate on in-memory dictionaries.  That is
useful for tests and targeted rechecks, but it is not by itself a safe product
boundary.  ``RepairTransactionService`` adds the missing boundary: it creates
report-first plans from the current registered inputs, records the dependency
hash bundle, and writes any applied result as quarantined derived versions.
Canonical READY draft, manifest, outline, and DOCX artifacts are never
overwritten here.
"""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from services.artifact_registry import ArtifactRecord, ArtifactRegistry
from services.job_workspace import JobWorkspace, atomic_write_json, utc_now_iso
from validation.closure import ValidationClosureResult, ValidationClosureService
from validation.repair_apply import run_repair_apply
from validation.repair_models import (
    DependencyHashBundle,
    PatchGranularity,
    PatchProposal,
    PatchTargetSignature,
    RepairPlan,
    RepairPolicy,
    RepairRootCause,
)


REPAIR_TRANSACTION_ARTIFACT_TYPE = "repair_transaction"
REPAIR_TRANSACTION_ARTIFACT_VERSION = "v1"


def _hash(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _load_json(record: ArtifactRecord | None) -> dict[str, Any] | None:
    if record is None:
        return None
    try:
        payload = json.loads(Path(record.path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return dict(payload) if isinstance(payload, Mapping) else None


def _find_block(review_draft: Mapping[str, Any], block_id: str) -> Mapping[str, Any] | None:
    content = review_draft.get("content")
    if not isinstance(content, Mapping):
        return None
    for section in content.get("sections") or []:
        if not isinstance(section, Mapping):
            continue
        for block in section.get("blocks") or []:
            if isinstance(block, Mapping) and str(block.get("block_id") or "") == block_id:
                return block
    return None


def _root_cause(values: Sequence[Any]) -> RepairRootCause:
    allowed = {item.value: item for item in RepairRootCause}
    for value in values:
        candidate = str(getattr(value, "value", value) or "").strip().lower()
        if candidate in allowed:
            return allowed[candidate]
    return RepairRootCause.INSUFFICIENT_CONTEXT


def _parse_plan(payload: Mapping[str, Any]) -> RepairPlan:
    """Reconstruct a persisted plan without trusting its derived projections."""

    proposals: list[PatchProposal] = []
    for raw in payload.get("proposals") or ():
        if not isinstance(raw, Mapping):
            raise ValueError("repair plan proposal must be an object")
        target = raw.get("target")
        if not isinstance(target, Mapping):
            raise ValueError("repair plan proposal target is missing")
        dependency_bundle = raw.get("dependency_bundle")
        if not isinstance(dependency_bundle, Mapping):
            raise ValueError("repair plan proposal dependency bundle is missing")
        try:
            proposals.append(
                PatchProposal(
                    proposal_id=str(raw.get("proposal_id") or ""),
                    citation_id=str(raw.get("citation_id") or ""),
                    root_cause=RepairRootCause(str(raw.get("root_cause") or "insufficient_context")),
                    granularity=PatchGranularity(str(raw.get("granularity") or "block")),
                    target=PatchTargetSignature(
                        block_id=str(target.get("block_id") or ""),
                        anchor_text=str(target.get("anchor_text") or ""),
                        anchor_hash=str(target.get("anchor_hash") or ""),
                        span_start=target.get("span_start") if isinstance(target.get("span_start"), int) else None,
                        span_end=target.get("span_end") if isinstance(target.get("span_end"), int) else None,
                    ),
                    original_text=str(raw.get("original_text") or ""),
                    proposed_text=str(raw.get("proposed_text") or ""),
                    confidence=float(raw.get("confidence") or 0.0),
                    fix_strategy=str(raw.get("fix_strategy") or ""),
                    dependency_bundle=DependencyHashBundle.from_dict(dict(dependency_bundle)),
                    metadata=dict(raw.get("metadata") or {}),
                )
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid repair plan proposal: {exc}") from exc
    plan_bundle = payload.get("dependency_hash_bundle")
    return RepairPlan(
        plan_id=str(payload.get("plan_id") or ""),
        created_at=str(payload.get("created_at") or ""),
        created_from_job_id=str(payload.get("created_from_job_id") or ""),
        validation_report_id=str(payload.get("validation_report_id") or ""),
        proposals=proposals,
        policy=RepairPolicy(str(payload.get("policy") or RepairPolicy.REPORT_FIRST.value)),
        artifact_type=str(payload.get("artifact_type") or "repair_plan"),
        artifact_version=str(payload.get("artifact_version") or "v1"),
        dependency_hash_bundle=(
            DependencyHashBundle.from_dict(dict(plan_bundle))
            if isinstance(plan_bundle, Mapping)
            else None
        ),
    )


@dataclass(frozen=True)
class RepairTransactionRecord:
    transaction_id: str
    job_id: str
    status: str
    policy: str
    plan_id: str
    validation_artifact_id: str
    previous_artifact_ids: tuple[str, ...]
    previous_artifact_hashes: Mapping[str, str]
    applied_artifact_ids: tuple[str, ...] = ()
    applied_patch_ids: tuple[str, ...] = ()
    created_at: str = ""
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["previous_artifact_ids"] = list(self.previous_artifact_ids)
        payload["applied_artifact_ids"] = list(self.applied_artifact_ids)
        payload["applied_patch_ids"] = list(self.applied_patch_ids)
        return payload


class RepairTransactionService:
    def __init__(self, workspace: JobWorkspace, registry: ArtifactRegistry) -> None:
        self.workspace = workspace
        self.registry = registry
        self.closure_service = ValidationClosureService(workspace, registry)

    def _canonical_inputs(self) -> tuple[ArtifactRecord | None, ArtifactRecord | None, ArtifactRecord | None]:
        records = self.registry.list_records()
        def choose(artifact_type: str, version: str, preferred: str) -> ArtifactRecord | None:
            candidates = [
                item for item in records
                if item.status == "ready"
                and item.artifact_type == artifact_type
                and item.artifact_version == version
            ]
            return next((item for item in candidates if item.artifact_id == preferred), None) or max(
                candidates,
                key=lambda item: (item.created_at, item.artifact_id),
                default=None,
            )
        return (
            choose("review_draft", "v3", "review_draft"),
            choose("citation_manifest", "v3", "citation_manifest:v3"),
            choose("validation_run_result", "v1", ""),
        )

    def _dependency_bundle(self, closure: ValidationClosureResult) -> DependencyHashBundle:
        inputs = closure.input_artifacts
        draft = inputs.get("review_draft") if isinstance(inputs, Mapping) else {}
        manifest = inputs.get("citation_manifest") if isinstance(inputs, Mapping) else {}
        validation = closure.validation_artifact or {}
        outline_hash = ""
        for record in self.registry.list_records():
            if record.status == "ready" and record.artifact_type in {"outline", "reviewed_outline", "adopted_final_outline"}:
                outline_hash = record.content_hash
                break
        return DependencyHashBundle(
            summary_hash="",
            paper_artifact_hash="",
            visual_manifest_hash="",
            selected_visual_refs_hash="",
            review_draft_hash=str(draft.get("content_hash") or "") if isinstance(draft, Mapping) else "",
            citation_manifest_hash=str(manifest.get("content_hash") or "") if isinstance(manifest, Mapping) else "",
            outline_hash=outline_hash,
        )

    def _build_plan(self, closure: ValidationClosureResult) -> RepairPlan | None:
        draft_record, _manifest_record, validation_record = self._canonical_inputs()
        draft = _load_json(draft_record)
        validation = _load_json(validation_record)
        if draft is None or validation is None:
            return None
        claims = validation.get("claim_results")
        if not isinstance(claims, list):
            return None
        proposals: list[PatchProposal] = []
        for claim in claims:
            if not isinstance(claim, Mapping):
                continue
            verdict = str(claim.get("verdict") or "needs_review")
            if verdict == "supported":
                continue
            block_ids = [str(item) for item in (claim.get("block_ids") or []) if str(item)]
            block_id = block_ids[0] if block_ids else ""
            block = _find_block(draft, block_id) if block_id else None
            if block is None:
                # Keep the issue in the plan metadata rather than inventing a
                # target.  An ungrounded patch must never become applicable.
                continue
            block_text = str(block.get("text") or "")
            proposal_id = "patch:" + _hash(
                {
                    "validation": claim.get("claim_result_id"),
                    "block_id": block_id,
                    "draft_hash": draft_record.content_hash if draft_record else "",
                }
            )[:24]
            root_cause = _root_cause(claim.get("root_causes") or [])
            proposals.append(
                PatchProposal(
                    proposal_id=proposal_id,
                    citation_id=str(claim.get("citation_set_key") or claim.get("claim_result_id") or proposal_id),
                    root_cause=root_cause,
                    granularity=PatchGranularity.SPAN if claim.get("span_start") is not None else PatchGranularity.BLOCK,
                    target=PatchTargetSignature(
                        block_id=block_id,
                        anchor_text=block_text,
                        anchor_hash=hashlib.sha256(block_text.encode("utf-8")).hexdigest()[:8],
                        span_start=claim.get("span_start") if isinstance(claim.get("span_start"), int) else None,
                        span_end=claim.get("span_end") if isinstance(claim.get("span_end"), int) else None,
                    ),
                    original_text=str(claim.get("claim_text") or block_text),
                    # Report-first plans describe a candidate but do not
                    # propose an unverified rewrite.
                    proposed_text=str(claim.get("claim_text") or block_text),
                    confidence=0.0 if bool(claim.get("low_confidence")) else 0.5,
                    fix_strategy="manual_review_mapping_first",
                    dependency_bundle=self._dependency_bundle(closure),
                    metadata={
                        "validation_result_id": validation_record.artifact_id if validation_record else "",
                        "claim_result_id": str(claim.get("claim_result_id") or ""),
                        "verdict": verdict,
                        "report_only": True,
                    },
                )
            )
        plan_id = "repair-plan:" + _hash(
            {
                "job_id": self.workspace.job_id,
                "closure_hash": closure.evidence_hash,
                "proposals": [item.to_dict() for item in proposals],
            }
        )[:24]
        return RepairPlan(
            plan_id=plan_id,
            created_at=utc_now_iso(),
            created_from_job_id=self.workspace.job_id,
            validation_report_id=validation_record.artifact_id if validation_record else "",
            proposals=proposals,
            policy=RepairPolicy.REPORT_FIRST,
            dependency_hash_bundle=self._dependency_bundle(closure),
        )

    def create_report_only_plan(self, closure: ValidationClosureResult | None = None) -> dict[str, Any]:
        current = closure or self.closure_service.inspect()
        if current.status == "clean":
            return {
                "status": "not_needed",
                "job_id": self.workspace.job_id,
                "reason": "validation closure is clean; no repair plan is required",
                "mutation_performed": False,
            }
        plan = self._build_plan(current)
        if plan is None:
            return {
                "status": "blocked",
                "job_id": self.workspace.job_id,
                "reason": "canonical validation inputs are unavailable or invalid",
                "closure": current.to_dict(),
                "mutation_performed": False,
            }
        path = Path(
            self.workspace.artifact_path(
                f"repair_plans/{plan.plan_id.replace(':', '-')}.json"
            )
        )
        atomic_write_json(str(path), plan.to_dict())
        dependencies: list[dict[str, Any]] = []
        previous_records = [item for item in self._canonical_inputs() if item is not None]
        for record in previous_records:
            if record.status == "ready":
                dependencies.append(
                    {
                        "artifact_id": record.artifact_id,
                        "artifact_type": record.artifact_type,
                        "path": record.path,
                        "content_hash": record.content_hash,
                    }
                )
        record = self.registry.register_file(
            artifact_id=f"repair_plan:{plan.plan_id}",
            artifact_role="repair_plan",
            artifact_type="repair_plan",
            artifact_version=plan.artifact_version,
            path=path,
            producer="validation.repair_transaction.RepairTransactionService",
            depends_on=dependencies,
            metadata={
                "policy": plan.policy.value,
                "closure_status": current.status,
                "closure_evidence_hash": current.evidence_hash,
            },
        )
        transaction_id = "repair-tx:" + _hash(
            {
                "plan_id": plan.plan_id,
                "closure_hash": current.evidence_hash,
                "previous": [item.content_hash for item in previous_records],
            }
        )[:24]
        transaction = RepairTransactionRecord(
            transaction_id=transaction_id,
            job_id=self.workspace.job_id,
            status="planned_report_only",
            policy=plan.policy.value,
            plan_id=record.artifact_id,
            validation_artifact_id=plan.validation_report_id,
            previous_artifact_ids=tuple(item.artifact_id for item in previous_records),
            previous_artifact_hashes={item.artifact_id: item.content_hash for item in previous_records},
            created_at=utc_now_iso(),
            reason="report-first repair plan; no canonical artifact was modified",
        )
        transaction_path = Path(
            self.workspace.artifact_path(
                f"repair_transactions/{transaction_id.replace(':', '-')}.json"
            )
        )
        atomic_write_json(str(transaction_path), transaction.to_dict())
        transaction_record = self.registry.register_file(
            artifact_id=transaction_id,
            artifact_role="repair_transaction",
            artifact_type=REPAIR_TRANSACTION_ARTIFACT_TYPE,
            artifact_version=REPAIR_TRANSACTION_ARTIFACT_VERSION,
            path=transaction_path,
            producer="validation.repair_transaction.RepairTransactionService",
            depends_on=[
                {
                    "artifact_id": record.artifact_id,
                    "artifact_type": record.artifact_type,
                    "path": record.path,
                    "content_hash": record.content_hash,
                },
                *dependencies,
            ],
            metadata={"status": transaction.status, "policy": transaction.policy},
        )
        return {
            "status": "available",
            "job_id": self.workspace.job_id,
            "plan_id": plan.plan_id,
            "artifact_id": record.artifact_id,
            "path": record.path,
            "transaction_id": transaction_record.artifact_id,
            "transaction_path": transaction_record.path,
            "proposal_count": len(plan.proposals),
            "policy": plan.policy.value,
            "closure": current.to_dict(),
            "mutation_performed": True,
            "read_only": False,
        }

    def apply_plan(self, plan_id: str) -> dict[str, Any]:
        closure = self.closure_service.inspect()
        plan_record = self.registry.get(plan_id) or self.registry.get(f"repair_plan:{plan_id}")
        if plan_record is None or plan_record.status != "ready":
            return {"status": "blocked", "reason": "repair plan is not a verified ready artifact", "mutation_performed": False}
        plan_payload = _load_json(plan_record)
        if plan_payload is None:
            return {"status": "blocked", "reason": "repair plan JSON is unreadable", "mutation_performed": False}
        if str(plan_payload.get("policy") or "report_only") != RepairPolicy.AUTO_APPLY_SAFE.value:
            return {
                "status": "blocked",
                "reason": "repair_policy_report_only_requires_explicit_safe_plan",
                "plan_id": plan_id,
                "mutation_performed": False,
            }
        plan = _parse_plan(plan_payload)
        if plan.created_from_job_id != self.workspace.job_id:
            return {
                "status": "blocked",
                "reason": "repair plan belongs to a different job",
                "plan_id": plan_id,
                "mutation_performed": False,
            }
        draft_record, manifest_record, validation_record = self._canonical_inputs()
        review_draft = _load_json(draft_record)
        citation_manifest = _load_json(manifest_record)
        if review_draft is None or citation_manifest is None:
            return {
                "status": "blocked",
                "reason": "current canonical review draft and citation manifest are required",
                "plan_id": plan_id,
                "mutation_performed": False,
            }
        paper_artifacts = [
            payload
            for record in self.registry.list_records()
            if record.status == "ready"
            and record.artifact_type in {"paper_artifact", "stage1_paper_artifact"}
            for payload in [_load_json(record)]
            if payload is not None
        ]
        try:
            apply_payload = run_repair_apply(
                repair_plan=plan,
                review_draft=copy.deepcopy(review_draft),
                citation_manifest=copy.deepcopy(citation_manifest),
                paper_artifacts=paper_artifacts,
                job_id=self.workspace.job_id,
                dry_run=False,
                require_auto_safe=True,
            )
        except (OSError, TypeError, ValueError, KeyError) as exc:
            return {
                "status": "blocked",
                "reason": f"repair guard execution failed: {exc}",
                "plan_id": plan_id,
                "mutation_performed": False,
            }
        apply_result = dict(apply_payload.get("apply_result") or {})
        applied_count = int(apply_result.get("applied_count") or 0)
        if applied_count <= 0:
            return {
                "status": "blocked",
                "reason": "no proposal passed the current auto-safe dependency and anchor guards",
                "plan_id": plan_id,
                "apply_result": apply_result,
                "mutation_performed": False,
            }

        tx_seed = {
            "plan": plan_record.content_hash,
            "closure": closure.evidence_hash,
            "applied": apply_result.get("applied_proposals") or [],
        }
        transaction_id = "repair-tx:" + _hash(tx_seed)[:24]
        tx_dir = Path(self.workspace.artifact_path(f"repair_transactions/{transaction_id.replace(':', '-')}"))
        tx_dir.mkdir(parents=True, exist_ok=True)
        patched_draft = apply_payload.get("patched_review_draft")
        patched_manifest = apply_payload.get("patched_citation_manifest")
        if not isinstance(patched_draft, Mapping) or not isinstance(patched_manifest, Mapping):
            return {
                "status": "blocked",
                "reason": "repair adapter did not return derived draft and manifest objects",
                "plan_id": plan_id,
                "mutation_performed": False,
            }
        derived_draft_path = tx_dir / "review_draft_repaired.json"
        derived_manifest_path = tx_dir / "citation_manifest_repaired.json"
        apply_result_path = tx_dir / "repair_apply_result.json"
        atomic_write_json(str(derived_draft_path), dict(patched_draft))
        atomic_write_json(str(derived_manifest_path), dict(patched_manifest))
        atomic_write_json(str(apply_result_path), apply_payload)
        base_dependencies = [
            item
            for item in (plan_record, draft_record, manifest_record, validation_record)
            if item is not None
        ]
        dependency_payloads = [
            {
                "artifact_id": item.artifact_id,
                "artifact_type": item.artifact_type,
                "path": item.path,
                "content_hash": item.content_hash,
            }
            for item in base_dependencies
        ]
        derived_draft_record = self.registry.register_file(
            artifact_id=f"review_draft_repaired:{transaction_id}",
            artifact_role="review_draft_repaired",
            artifact_type="review_draft_repaired",
            artifact_version="v1",
            path=derived_draft_path,
            producer="validation.repair_transaction.RepairTransactionService",
            status="quarantined",
            depends_on=dependency_payloads,
            metadata={"transaction_id": transaction_id, "canonical_replacement": False},
        )
        derived_manifest_record = self.registry.register_file(
            artifact_id=f"citation_manifest_repaired:{transaction_id}",
            artifact_role="citation_manifest_repaired",
            artifact_type="citation_manifest_repaired",
            artifact_version="v1",
            path=derived_manifest_path,
            producer="validation.repair_transaction.RepairTransactionService",
            status="quarantined",
            depends_on=dependency_payloads,
            metadata={"transaction_id": transaction_id, "canonical_replacement": False},
        )
        apply_record = self.registry.register_file(
            artifact_id=f"repair_apply_result:{transaction_id}",
            artifact_role="repair_apply_result",
            artifact_type="repair_apply_result",
            artifact_version="v1",
            path=apply_result_path,
            producer="validation.repair_transaction.RepairTransactionService",
            status="quarantined",
            depends_on=[
                *dependency_payloads,
                {
                    "artifact_id": derived_draft_record.artifact_id,
                    "artifact_type": derived_draft_record.artifact_type,
                    "path": derived_draft_record.path,
                    "content_hash": derived_draft_record.content_hash,
                },
                {
                    "artifact_id": derived_manifest_record.artifact_id,
                    "artifact_type": derived_manifest_record.artifact_type,
                    "path": derived_manifest_record.path,
                    "content_hash": derived_manifest_record.content_hash,
                },
            ],
            metadata={"transaction_id": transaction_id, "canonical_replacement": False},
        )
        previous_records = [item for item in (draft_record, manifest_record, validation_record) if item is not None]
        transaction = RepairTransactionRecord(
            transaction_id=transaction_id,
            job_id=self.workspace.job_id,
            status="quarantined",
            policy=plan.policy.value,
            plan_id=plan_record.artifact_id,
            validation_artifact_id=validation_record.artifact_id if validation_record else "",
            previous_artifact_ids=tuple(item.artifact_id for item in previous_records),
            previous_artifact_hashes={item.artifact_id: item.content_hash for item in previous_records},
            applied_artifact_ids=(derived_draft_record.artifact_id, derived_manifest_record.artifact_id, apply_record.artifact_id),
            applied_patch_ids=tuple(str(item) for item in apply_result.get("applied_proposals") or ()),
            created_at=utc_now_iso(),
            reason="auto-safe structural repair produced quarantined derived artifacts; canonical artifacts were not replaced",
        )
        transaction_path = tx_dir / "repair_transaction.json"
        atomic_write_json(str(transaction_path), transaction.to_dict())
        transaction_record = self.registry.register_file(
            artifact_id=transaction_id,
            artifact_role="repair_transaction",
            artifact_type=REPAIR_TRANSACTION_ARTIFACT_TYPE,
            artifact_version=REPAIR_TRANSACTION_ARTIFACT_VERSION,
            path=transaction_path,
            producer="validation.repair_transaction.RepairTransactionService",
            status="quarantined",
            depends_on=[
                *dependency_payloads,
                {
                    "artifact_id": apply_record.artifact_id,
                    "artifact_type": apply_record.artifact_type,
                    "path": apply_record.path,
                    "content_hash": apply_record.content_hash,
                },
            ],
            metadata={"status": transaction.status, "canonical_replacement": False},
        )
        return {
            "status": "quarantined",
            "plan_id": plan_id,
            "transaction_id": transaction_record.artifact_id,
            "applied_artifact_ids": list(transaction.applied_artifact_ids),
            "applied_patch_ids": list(transaction.applied_patch_ids),
            "apply_result": apply_result,
            "mutation_performed": True,
            "canonical_replacement": False,
        }


__all__ = [
    "REPAIR_TRANSACTION_ARTIFACT_TYPE",
    "REPAIR_TRANSACTION_ARTIFACT_VERSION",
    "RepairTransactionRecord",
    "RepairTransactionService",
]
