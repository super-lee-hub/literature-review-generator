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
from dataclasses import asdict, dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping, Sequence

from services.artifact_registry import (
    ArtifactDependencyRefV2,
    ArtifactRecord,
    ArtifactRegistry,
    file_sha256,
)
from services.job_workspace import JobWorkspace, atomic_write_json, utc_now_iso
from services.audit_record import AuditArtifactRefV1, AuditRecordV1
from validation.closure import ValidationClosureResult, ValidationClosureService
from validation.repair_apply import run_repair_apply
from validation.repair_models import (
    AutoSafePatch,
    DependencyHashBundle,
    ManualReviewAction,
    PatchGranularity,
    PatchProposal,
    PatchTargetSignature,
    RepairPlan,
    RepairIssue,
    RepairPolicy,
    RepairRootCause,
    RepairStructuralClosure,
    NOT_APPLICABLE,
)
from validation.semantic_revalidation import run_semantic_revalidation


REPAIR_TRANSACTION_ARTIFACT_TYPE = "repair_transaction"
REPAIR_TRANSACTION_ARTIFACT_VERSION = "v1"
CURRENT_REPAIR_POINTERS = {
    "review_draft": {
        "pointer_id": "review_draft:current",
        "fallback_id": "review_draft",
        "artifact_type": "review_draft",
        "artifact_version": "v3",
        "filename": "review_draft.json",
    },
    "citation_manifest": {
        "pointer_id": "citation_manifest:current",
        "fallback_id": "citation_manifest_v3",
        "artifact_type": "citation_manifest",
        "artifact_version": "v3",
        "filename": "citation_manifest.json",
    },
    "review_docx": {
        "pointer_id": "review_docx:current",
        "fallback_id": "review_docx",
        "artifact_type": "review_docx",
        "artifact_version": "v1",
        "filename": "review.docx",
    },
    "validation_run_result": {
        "pointer_id": "validation_run_result:current",
        "fallback_id": "",
        "artifact_type": "validation_run_result",
        "artifact_version": "v1",
        "filename": "validation_run_result_v1.json",
    },
}


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


def current_artifact_record(
    registry: ArtifactRegistry,
    kind: str,
) -> ArtifactRecord | None:
    """Resolve a current artifact only through its durable pointer.

    Before the first repair promotion there is no pointer, so the original
    canonical identity remains the explicit bootstrap fallback.  Once a
    pointer exists, malformed or stale targets fail closed rather than falling
    back to an older artifact.
    """

    spec = CURRENT_REPAIR_POINTERS.get(kind)
    if spec is None:
        raise ValueError(f"unknown current repair artifact kind: {kind}")
    pointer = registry.get(str(spec["pointer_id"]))
    if pointer is None:
        fallback_id = str(spec.get("fallback_id") or "")
        fallback_ids = [fallback_id]
        if kind == "citation_manifest":
            fallback_ids.append("citation_manifest:v3")
        if not any(fallback_ids):
            candidates = [
                record
                for record in registry.list_records()
                if record.status == "ready"
                and record.artifact_type == spec["artifact_type"]
                and record.artifact_version == spec["artifact_version"]
            ]
            return max(candidates, key=lambda item: (item.created_at, item.artifact_id), default=None)
        for candidate_id in fallback_ids:
            record = registry.get(candidate_id)
            if record is not None and record.status == "ready":
                return record
        return None
    if pointer.status != "ready" or pointer.artifact_type != "current_artifact_pointer":
        return None
    payload = _load_json(pointer)
    if payload is None:
        return None
    target_id = str(payload.get("target_artifact_id") or "").strip()
    target_hash = str(payload.get("target_content_hash") or "").strip()
    if not target_id or not target_hash:
        return None
    target = registry.get(target_id)
    if target is None or target.status != "ready":
        return None
    if (
        target.artifact_type != spec["artifact_type"]
        or target.artifact_version != spec["artifact_version"]
        or target.content_hash != target_hash
        or str(payload.get("pointer_kind") or "") != kind
    ):
        return None
    try:
        if file_sha256(target.path) != target_hash:
            return None
    except OSError:
        return None
    return target


def _write_current_artifact_pointer(
    workspace: JobWorkspace,
    registry: ArtifactRegistry,
    *,
    kind: str,
    target: ArtifactRecord,
    previous: ArtifactRecord | None,
    promotion_id: str,
) -> ArtifactRecord:
    spec = CURRENT_REPAIR_POINTERS[kind]
    pointer_path = Path(workspace.artifact_path(f"current/{spec['filename']}"))
    payload = {
        "artifact_type": "current_artifact_pointer",
        "artifact_version": "v1",
        "job_id": workspace.job_id,
        "pointer_kind": kind,
        "pointer_role": "current",
        "target_artifact_id": target.artifact_id,
        "target_content_hash": target.content_hash,
        "target_path": target.path,
        "previous_artifact_id": previous.artifact_id if previous is not None else "",
        "previous_content_hash": previous.content_hash if previous is not None else "",
        "promotion_transaction_id": promotion_id,
        "updated_at": utc_now_iso(),
    }
    atomic_write_json(str(pointer_path), payload)
    dependencies = [ArtifactDependencyRefV2.from_record(target)]
    if previous is not None and previous.artifact_id != target.artifact_id:
        dependencies.append(ArtifactDependencyRefV2.from_record(previous))
    return registry.register_file(
        artifact_id=str(spec["pointer_id"]),
        artifact_role="current_artifact_pointer",
        artifact_type="current_artifact_pointer",
        artifact_version="v1",
        path=pointer_path,
        producer="validation.repair_transaction.RepairTransactionService",
        depends_on=dependencies,
        metadata={
            "pointer_kind": kind,
            "pointer_role": "current",
            "target_artifact_id": target.artifact_id,
            "target_content_hash": target.content_hash,
            "promotion_transaction_id": promotion_id,
        },
    )


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


def _targeted_revalidate(
    review_draft: Mapping[str, Any],
    citation_manifest: Mapping[str, Any],
    paper_artifacts: Sequence[Mapping[str, Any]],
    citation_ref_catalog: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Recheck the structural closure of a derived repair result.

    Repair application is deliberately separate from the full semantic
    validator.  This small, deterministic check is the final boundary before
    a derived result is persisted: every section/block must remain addressable
    and every citation occurrence must resolve to a real block, ref, and paper.
    """

    diagnostics: list[str] = []
    content = review_draft.get("content")
    sections = content.get("sections") if isinstance(content, Mapping) else None
    if not isinstance(sections, list) or not sections:
        diagnostics.append("review_draft_sections_missing")

    block_ids: set[str] = set()
    block_count = 0
    if isinstance(sections, list):
        for section_index, section in enumerate(sections, start=1):
            if not isinstance(section, Mapping):
                diagnostics.append(f"section_not_object:{section_index}")
                continue
            blocks = section.get("blocks")
            if not isinstance(blocks, list) or not blocks:
                diagnostics.append(f"section_blocks_missing:{section_index}")
                continue
            for block_index, block in enumerate(blocks, start=1):
                if not isinstance(block, Mapping):
                    diagnostics.append(f"block_not_object:{section_index}:{block_index}")
                    continue
                block_id = str(block.get("block_id") or "").strip()
                if not block_id:
                    diagnostics.append(f"block_id_missing:{section_index}:{block_index}")
                elif block_id in block_ids:
                    diagnostics.append(f"duplicate_block_id:{block_id}")
                else:
                    block_ids.add(block_id)
                if not str(block.get("text") or "").strip():
                    diagnostics.append(f"block_text_empty:{block_id or f'{section_index}:{block_index}'}")
                block_count += 1

    manifest_occurrences = citation_manifest.get("occurrences")
    occurrences = manifest_occurrences if isinstance(manifest_occurrences, list) else []
    if manifest_occurrences is None:
        diagnostics.append("citation_occurrences_missing")
    occurrence_ids: set[str] = set()
    active_ref_ids = {
        str(entry.get("ref_id") or "").strip()
        for entry in (citation_ref_catalog or {}).get("entries", [])
        if isinstance(entry, Mapping)
        and entry.get("status") == "active"
        and str(entry.get("ref_id") or "").strip()
    }
    known_paper_ids: set[str] = set()
    for artifact in paper_artifacts:
        identity = artifact.get("paper_identity")
        if isinstance(identity, Mapping):
            known_paper_ids.update(
                str(identity.get(key) or "").strip()
                for key in ("canonical_paper_key", "source_paper_id")
                if str(identity.get(key) or "").strip()
            )
    for entry in (citation_ref_catalog or {}).get("entries", []):
        if isinstance(entry, Mapping) and entry.get("status") == "active":
            known_paper_ids.update(
                str(entry.get(key) or "").strip()
                for key in ("paper_id", "canonical_paper_key")
                if str(entry.get(key) or "").strip()
            )
    for field_name in ("paper_entries", "bibliography"):
        for entry in citation_manifest.get(field_name, []) or []:
            if isinstance(entry, Mapping):
                known_paper_ids.update(
                    str(entry.get(key) or "").strip()
                    for key in ("paper_id", "paper_key")
                    if str(entry.get(key) or "").strip()
                )

    unresolved_count = 0
    mapped_count = 0
    for index, occurrence in enumerate(occurrences, start=1):
        if not isinstance(occurrence, Mapping):
            diagnostics.append(f"citation_occurrence_not_object:{index}")
            unresolved_count += 1
            continue
        occurrence_id = str(occurrence.get("occurrence_id") or "").strip()
        if not occurrence_id:
            diagnostics.append(f"citation_occurrence_id_missing:{index}")
        elif occurrence_id in occurrence_ids:
            diagnostics.append(f"duplicate_citation_occurrence:{occurrence_id}")
        else:
            occurrence_ids.add(occurrence_id)
        block_id = str(occurrence.get("block_id") or "").strip()
        ref_id = str(occurrence.get("ref_id") or "").strip()
        paper_id = str(occurrence.get("paper_id") or occurrence.get("paper_key") or "").strip()
        unresolved = (
            not block_id
            or block_id not in block_ids
            or not ref_id
            or not paper_id
            or paper_id.lower() == "unknown"
            or (bool(active_ref_ids) and ref_id not in active_ref_ids)
            or (bool(known_paper_ids) and paper_id not in known_paper_ids)
        )
        if not block_id or block_id not in block_ids:
            diagnostics.append(f"citation_block_mapping_error:{occurrence_id or index}")
        if not ref_id:
            diagnostics.append(f"citation_ref_id_missing:{occurrence_id or index}")
        elif active_ref_ids and ref_id not in active_ref_ids:
            diagnostics.append(f"citation_ref_id_unresolved:{ref_id}")
        if not paper_id or paper_id.lower() == "unknown":
            diagnostics.append(f"citation_paper_id_unresolved:{occurrence_id or index}")
        elif known_paper_ids and paper_id not in known_paper_ids:
            diagnostics.append(f"citation_paper_id_unknown:{paper_id}")
        if unresolved:
            unresolved_count += 1
        else:
            mapped_count += 1

    result = {
        "passed": not diagnostics,
        "diagnostics": sorted(set(diagnostics)),
        "section_count": len(sections) if isinstance(sections, list) else 0,
        "block_count": block_count,
        "occurrence_count": len(occurrences),
        "mapped_occurrence_count": mapped_count,
        "unresolved_occurrence_count": unresolved_count,
    }
    result["evidence_hash"] = _hash(result)
    return result


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


@dataclass(frozen=True)
class RepairPromotionTransaction:
    """Immutable record for versioned repair outputs.

    Promotion creates new identities for the draft, manifest, DOCX, audit, and
    lineage.  It never overwrites a canonical path and never exports a
    quarantined artifact as if it were canonical.
    """

    transaction_id: str
    job_id: str
    source_transaction_id: str
    status: str
    actor: str
    reason: str
    canonical_version: str
    review_draft_artifact_id: str
    citation_manifest_artifact_id: str
    review_docx_artifact_id: str
    audit_artifact_id: str
    lineage_artifact_id: str
    canonical_input_hashes: Mapping[str, str]
    output_hashes: Mapping[str, str]
    created_at: str
    artifact_type: str = "repair_promotion_transaction"
    artifact_version: str = "v1"
    validation_run_result_artifact_id: str = ""
    current_pointer_artifact_ids: Mapping[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["canonical_input_hashes"] = dict(self.canonical_input_hashes)
        payload["output_hashes"] = dict(self.output_hashes)
        payload["current_pointer_artifact_ids"] = dict(self.current_pointer_artifact_ids)
        return payload


class RepairTransactionService:
    def __init__(self, workspace: JobWorkspace, registry: ArtifactRegistry) -> None:
        self.workspace = workspace
        self.registry = registry
        self.closure_service = ValidationClosureService(workspace, registry)

    def _canonical_inputs(self) -> tuple[ArtifactRecord | None, ArtifactRecord | None, ArtifactRecord | None]:
        return (
            current_artifact_record(self.registry, "review_draft"),
            current_artifact_record(self.registry, "citation_manifest"),
            current_artifact_record(self.registry, "validation_run_result"),
        )

    def _dependency_bundle(self, closure: ValidationClosureResult) -> DependencyHashBundle:
        records = [record for record in self.registry.list_records() if record.status == "ready"]
        inputs = closure.input_artifacts
        draft = inputs.get("review_draft") if isinstance(inputs, Mapping) else {}
        manifest = inputs.get("citation_manifest") if isinstance(inputs, Mapping) else {}

        def load(record: ArtifactRecord | None) -> dict[str, Any] | list[Any]:
            if record is None:
                return {}
            try:
                value = json.loads(Path(record.path).read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                return {}
            return value if isinstance(value, (dict, list)) else {}

        def choose(*artifact_types: str) -> ArtifactRecord | None:
            candidates = [item for item in records if item.artifact_type in artifact_types]
            return max(candidates, key=lambda item: (item.created_at, item.artifact_id), default=None)

        paper_records = [
            item for item in records
            if item.artifact_type in {"paper_artifact", "stage1_paper_artifact"}
        ]

        def paper_sort_key(record: ArtifactRecord) -> str:
            value = load(record)
            if isinstance(value, Mapping):
                identity = value.get("paper_identity")
                if isinstance(identity, Mapping):
                    canonical_key = str(identity.get("canonical_paper_key") or "").strip()
                    if canonical_key:
                        return canonical_key
            return record.artifact_id

        paper_payloads = [
            value for value in (
                load(item) for item in sorted(
                    paper_records,
                    key=paper_sort_key,
                )
            )
            if isinstance(value, Mapping)
        ]
        aggregate_summary: dict[str, Any] = {}
        for paper_payload in paper_payloads:
            analysis = paper_payload.get("analysis")
            if isinstance(analysis, Mapping) and isinstance(analysis.get("ai_summary"), Mapping):
                aggregate_summary.update(dict(analysis["ai_summary"]))
        visual_record = choose("visual_manifest")
        outline_record = next(
            (
                item for item in records
                if item.artifact_id == "outline-v3:final_outline"
                or item.artifact_type == "adopted_outline"
            ),
            None,
        )
        primary_paper = paper_payloads[0] if paper_payloads else {}
        visual_payload = load(visual_record)
        return DependencyHashBundle(
            summary_hash=_hash(aggregate_summary) if aggregate_summary else NOT_APPLICABLE,
            paper_artifact_hash=_hash(paper_payloads) if paper_payloads else NOT_APPLICABLE,
            visual_manifest_hash=_hash(visual_payload) if visual_record is not None else NOT_APPLICABLE,
            selected_visual_refs_hash=_hash(
                primary_paper.get("stage1_inputs", {}).get("selected_visual_refs", [])
                if isinstance(primary_paper.get("stage1_inputs"), Mapping)
                else []
            ) if primary_paper else NOT_APPLICABLE,
            review_draft_hash=(
                str(draft.get("content_hash") or "").strip() or NOT_APPLICABLE
                if isinstance(draft, Mapping)
                else NOT_APPLICABLE
            ),
            citation_manifest_hash=(
                str(manifest.get("content_hash") or "").strip() or NOT_APPLICABLE
                if isinstance(manifest, Mapping)
                else NOT_APPLICABLE
            ),
            outline_hash=outline_record.content_hash if outline_record is not None else NOT_APPLICABLE,
        )

    def _dependency_records(self) -> list[ArtifactRecord]:
        records = []
        for record in self.registry.list_records():
            if record.status != "ready":
                continue
            if record.artifact_type in {
                "review_draft",
                "citation_manifest",
                "validation_run_result",
                "summary_file",
                "stage1_canonical_summaries",
                "paper_artifact",
                "stage1_paper_artifact",
                "visual_manifest",
            } or record.artifact_id == "outline-v3:final_outline" or record.artifact_type == "adopted_outline":
                records.append(record)
        return records

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
        issues: list[RepairIssue] = []
        manual_review_actions: list[ManualReviewAction] = []
        auto_safe_patches: list[AutoSafePatch] = []
        for claim in claims:
            if not isinstance(claim, Mapping):
                continue
            verdict = str(claim.get("verdict") or "needs_review")
            if verdict == "supported":
                continue
            claim_id = str(claim.get("claim_result_id") or "").strip()
            issue_id = "repair-issue:" + _hash(
                {
                    "validation": validation_record.artifact_id if validation_record else "",
                    "claim_result_id": claim_id,
                    "block_ids": claim.get("block_ids") or [],
                }
            )[:24]
            block_ids = [str(item) for item in (claim.get("block_ids") or []) if str(item)]
            block_id = block_ids[0] if block_ids else ""
            block = _find_block(draft, block_id) if block_id else None
            root_cause = _root_cause(claim.get("root_causes") or [])
            evidence = [
                dict(item)
                for item in claim.get("evidence_candidates") or []
                if isinstance(item, Mapping)
            ]
            issues.append(
                RepairIssue(
                    issue_id=issue_id,
                    issue_type=root_cause.value,
                    severity="high" if bool(claim.get("low_confidence")) else "medium",
                    message=str(
                        claim.get("reasoning_summary")
                        or claim.get("repair_hint")
                        or "validation finding requires repair review"
                    ),
                    artifact_id=validation_record.artifact_id if validation_record else "",
                    citation_id=str(claim.get("citation_set_key") or claim_id),
                    block_id=block_id,
                    location={
                        "span_start": claim.get("span_start"),
                        "span_end": claim.get("span_end"),
                    },
                    evidence=evidence,
                    repairability="manual_review",
                    metadata={"verdict": verdict, "root_cause": root_cause.value},
                )
            )
            if block is None:
                # Keep the issue in the plan metadata rather than inventing a
                # target.  An ungrounded patch must never become applicable.
                manual_review_actions.append(
                    ManualReviewAction(
                        action_id=f"manual-review:{issue_id}",
                        issue_id=issue_id,
                        action="resolve_target_block_and_evidence",
                        rationale="the validation finding has no registered review block target",
                        required_inputs=["review_draft", "citation_manifest", "paper_artifacts"],
                    )
                )
                continue
            block_text = str(block.get("text") or "")
            proposal_id = "patch:" + _hash(
                {
                    "validation": claim.get("claim_result_id"),
                    "block_id": block_id,
                    "draft_hash": draft_record.content_hash if draft_record else "",
                }
            )[:24]
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
                    # Report-first plans describe an affected span but do not
                    # pretend that an unverified rewrite is ready to apply.
                    proposed_text="",
                    confidence=0.0 if bool(claim.get("low_confidence")) else 0.5,
                    fix_strategy="manual_review_mapping_first",
                    dependency_bundle=self._dependency_bundle(closure),
                    metadata={
                        "issue_id": issue_id,
                        "validation_result_id": validation_record.artifact_id if validation_record else "",
                        "claim_result_id": str(claim.get("claim_result_id") or ""),
                        "verdict": verdict,
                        "report_only": True,
                    },
                )
            )
            manual_review_actions.append(
                ManualReviewAction(
                    action_id=f"manual-review:{issue_id}",
                    issue_id=issue_id,
                    action="confirm_mapping_or_propose_structural_patch",
                    rationale="report-first plans never turn a validation finding into an automatic rewrite",
                    required_inputs=["review_draft", "citation_manifest", "paper_artifacts"],
                )
            )
        proposals.sort(
            key=lambda item: (
                0 if item.root_cause is RepairRootCause.CITATION_MAPPING_ERROR else 1,
                item.proposal_id,
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
            issues=issues,
            manual_review_actions=manual_review_actions,
            auto_safe_patches=auto_safe_patches,
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
        dependencies: list[ArtifactDependencyRefV2] = []
        previous_records = self._dependency_records()
        for record in previous_records:
            if record.status == "ready":
                dependencies.append(ArtifactDependencyRefV2.from_record(record))
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
                ArtifactDependencyRefV2.from_record(record),
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
        visual_manifest: dict[str, Any] = {}
        for record in self.registry.list_records():
            if record.status != "ready" or record.artifact_type != "visual_manifest":
                continue
            try:
                payload = json.loads(Path(record.path).read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                continue
            if isinstance(payload, Mapping):
                visual_manifest = dict(payload)
                break
        citation_ref_catalog: dict[str, Any] = {}
        catalog_record = self.registry.get("citation_ref_catalog")
        if catalog_record is not None and catalog_record.status == "ready":
            catalog_payload = _load_json(catalog_record)
            if isinstance(catalog_payload, Mapping):
                citation_ref_catalog = dict(catalog_payload)
        try:
            apply_payload = run_repair_apply(
                repair_plan=plan,
                review_draft=copy.deepcopy(review_draft),
                citation_manifest=copy.deepcopy(citation_manifest),
                paper_artifacts=paper_artifacts,
                job_id=self.workspace.job_id,
                dry_run=False,
                require_auto_safe=True,
                visual_manifest=visual_manifest,
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
        targeted_revalidation = _targeted_revalidate(
            patched_draft,
            patched_manifest,
            paper_artifacts,
            citation_ref_catalog,
        )
        if not targeted_revalidation["passed"]:
            return {
                "status": "blocked",
                "reason": "targeted revalidation failed; derived repair artifacts were not persisted",
                "plan_id": plan_id,
                "apply_result": apply_result,
                "targeted_revalidation": targeted_revalidation,
                "mutation_performed": False,
            }
        semantic_revalidation = run_semantic_revalidation(
            patched_draft,
            patched_manifest,
            paper_artifacts,
            citation_ref_catalog=citation_ref_catalog,
        )
        structural_closure = RepairStructuralClosure.from_results(
            targeted_revalidation,
            semantic_revalidation.to_dict(),
            canonical_input_hashes={
                record.artifact_id: record.content_hash
                for record in (draft_record, manifest_record, validation_record)
                if record is not None
            },
            derived_output_hashes={
                "review_draft_repaired": _hash(patched_draft),
                "citation_manifest_repaired": _hash(patched_manifest),
            },
        )
        if not structural_closure.passed:
            return {
                "status": "blocked",
                "reason": "repair structural closure failed; derived repair artifacts were not persisted",
                "plan_id": plan_id,
                "apply_result": apply_result,
                "targeted_revalidation": targeted_revalidation,
                "semantic_revalidation": semantic_revalidation.to_dict(),
                "repair_structural_closure": structural_closure.to_dict(),
                "mutation_performed": False,
            }
        apply_payload["targeted_revalidation"] = targeted_revalidation
        apply_payload["semantic_revalidation"] = semantic_revalidation.to_dict()
        apply_payload["repair_structural_closure"] = structural_closure.to_dict()
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
            ArtifactDependencyRefV2.from_record(item)
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
            metadata={
                "transaction_id": transaction_id,
                "canonical_replacement": False,
                "semantic_revalidation": semantic_revalidation.to_dict(),
            },
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
            metadata={
                "transaction_id": transaction_id,
                "canonical_replacement": False,
                "semantic_revalidation": semantic_revalidation.to_dict(),
            },
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
                ArtifactDependencyRefV2.from_record(derived_draft_record),
                ArtifactDependencyRefV2.from_record(derived_manifest_record),
            ],
            metadata={
                "transaction_id": transaction_id,
                "canonical_replacement": False,
                "semantic_revalidation": semantic_revalidation.to_dict(),
            },
        )
        previous_records = self._dependency_records()
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
                ArtifactDependencyRefV2.from_record(apply_record),
            ],
            metadata={
                "status": transaction.status,
                "canonical_replacement": False,
                "semantic_revalidation": semantic_revalidation.to_dict(),
            },
        )
        return {
            "status": "quarantined",
            "plan_id": plan_id,
            "transaction_id": transaction_record.artifact_id,
            "applied_artifact_ids": list(transaction.applied_artifact_ids),
            "applied_patch_ids": list(transaction.applied_patch_ids),
            "apply_result": apply_result,
            "repair_structural_closure": structural_closure.to_dict(),
            "mutation_performed": True,
            "canonical_replacement": False,
        }

    def promote_transaction(
        self,
        transaction_id: str,
        *,
        actor: str,
        reason: str,
        validation_result: Mapping[str, Any] | None = None,
        validation_record: ArtifactRecord | None = None,
        receipt_closure: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create versioned current outputs from a quarantined repair.

        This is an explicit, auditable transaction.  The existing canonical
        draft, manifest, and DOCX paths are never written, renamed, deleted, or
        replaced.  Promotion requires the current validation service's durable
        revalidation result and receipt closure; it then advances explicit
        content-addressed current pointers to new immutable versions.
        """

        actor = str(actor or "").strip()
        reason = str(reason or "").strip()
        if not actor or not reason:
            return {
                "status": "blocked",
                "reason": "promotion actor and reason are required",
                "transaction_id": transaction_id,
                "mutation_performed": False,
            }
        if validation_result is None or validation_record is None:
            return {
                "status": "blocked",
                "reason": "promotion requires a durable current-service revalidation result",
                "transaction_id": transaction_id,
                "mutation_performed": False,
            }
        revalidation_payload = validation_result.get("validation_run_result_payload")
        if not isinstance(revalidation_payload, Mapping):
            revalidation_payload = validation_result.get("validation_run_result")
            to_dict = getattr(revalidation_payload, "to_dict", None)
            if callable(to_dict):
                revalidation_payload = to_dict()
        if not isinstance(revalidation_payload, Mapping):
            return {
                "status": "blocked",
                "reason": "revalidation result payload is missing",
                "transaction_id": transaction_id,
                "mutation_performed": False,
            }
        try:
            from validation.run_result import ValidationRunResultV1, ValidationRunDisposition

            revalidation_model = ValidationRunResultV1.from_dict(dict(revalidation_payload))
        except (TypeError, ValueError, KeyError, RuntimeError) as exc:
            return {
                "status": "blocked",
                "reason": f"revalidation result is invalid: {exc}",
                "transaction_id": transaction_id,
                "mutation_performed": False,
            }
        if (
            not revalidation_model.contract_satisfied
            or revalidation_model.validation_disposition is not ValidationRunDisposition.CLEAN
        ):
            return {
                "status": "blocked",
                "reason": "semantic current-service revalidation is not clean",
                "transaction_id": transaction_id,
                "validation_disposition": revalidation_model.validation_disposition.value,
                "mutation_performed": False,
            }
        closure_payload = dict(receipt_closure or {})
        if not bool(closure_payload.get("complete")):
            closure_payload = dict(validation_result.get("provider_receipt_closure") or {})
        if not bool(closure_payload.get("complete")):
            return {
                "status": "blocked",
                "reason": "provider receipt closure is incomplete for promotion",
                "transaction_id": transaction_id,
                "mutation_performed": False,
            }
        source_record = self.registry.get(transaction_id) or self.registry.get(
            f"repair-tx:{transaction_id}"
        )
        if source_record is None or source_record.artifact_type != REPAIR_TRANSACTION_ARTIFACT_TYPE:
            return {
                "status": "blocked",
                "reason": "source repair transaction is not registered",
                "transaction_id": transaction_id,
                "mutation_performed": False,
            }
        source_payload = _load_json(source_record)
        if source_payload is None or source_record.status != "quarantined":
            return {
                "status": "blocked",
                "reason": "promotion requires a quarantined repair transaction",
                "transaction_id": transaction_id,
                "mutation_performed": False,
            }
        registered_revalidation = self.registry.get(validation_record.artifact_id)
        if (
            registered_revalidation is None
            or registered_revalidation.artifact_type != "validation_run_result_repaired"
            or registered_revalidation.content_hash != validation_record.content_hash
            or registered_revalidation.status != validation_record.status
            or not Path(registered_revalidation.path).is_file()
            or file_sha256(registered_revalidation.path) != registered_revalidation.content_hash
        ):
            return {
                "status": "blocked",
                "reason": "revalidation record is not the current durable Registry artifact",
                "transaction_id": transaction_id,
                "mutation_performed": False,
            }

        source_hash_prefix = source_record.content_hash[:16]
        promotion_id = f"repair-promotion:{source_hash_prefix}"
        existing_promotions = [
            item
            for item in self.registry.list_records()
            if item.artifact_id == promotion_id
            and item.status == "ready"
            and item.artifact_type == "repair_promotion_transaction"
        ]
        if existing_promotions:
            return {
                "status": "already_promoted",
                "transaction_id": transaction_id,
                "promotion_transaction_id": promotion_id,
                "mutation_performed": False,
            }

        applied_ids = [str(item) for item in source_payload.get("applied_artifact_ids") or ()]
        derived_records = [
            self.registry.get(item)
            for item in applied_ids
            if self.registry.get(item) is not None
        ]
        derived_draft_record = next(
            (item for item in derived_records if item is not None and item.artifact_type == "review_draft_repaired"),
            None,
        )
        derived_manifest_record = next(
            (item for item in derived_records if item is not None and item.artifact_type == "citation_manifest_repaired"),
            None,
        )
        draft_payload = _load_json(derived_draft_record)
        manifest_payload = _load_json(derived_manifest_record)
        if draft_payload is None or manifest_payload is None:
            return {
                "status": "blocked",
                "reason": "repair transaction does not contain derived draft and manifest",
                "transaction_id": transaction_id,
                "mutation_performed": False,
            }

        draft_record, manifest_record, canonical_validation_record = self._canonical_inputs()
        previous_docx_record = current_artifact_record(self.registry, "review_docx")
        previous_validation_record = canonical_validation_record
        if draft_record is None or manifest_record is None:
            return {
                "status": "blocked",
                "reason": "canonical draft and manifest are required for promotion lineage",
                "transaction_id": transaction_id,
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
        catalog_payload: dict[str, Any] = {}
        catalog_record = self.registry.get("citation_ref_catalog")
        if catalog_record is not None and catalog_record.status == "ready":
            loaded_catalog = _load_json(catalog_record)
            if loaded_catalog is not None:
                catalog_payload = loaded_catalog
        targeted = _targeted_revalidate(
            draft_payload,
            manifest_payload,
            paper_artifacts,
            catalog_payload,
        )
        semantic = run_semantic_revalidation(
            draft_payload,
            manifest_payload,
            paper_artifacts,
            citation_ref_catalog=catalog_payload,
        )
        structural = RepairStructuralClosure.from_results(
            targeted,
            semantic.to_dict(),
            canonical_input_hashes={
                item.artifact_id: item.content_hash
                for item in (draft_record, manifest_record, canonical_validation_record)
                if item is not None
            },
        )
        if not structural.passed:
            return {
                "status": "blocked",
                "reason": "repair structural closure failed during promotion",
                "transaction_id": transaction_id,
                "repair_structural_closure": structural.to_dict(),
                "mutation_performed": False,
            }

        versioned_suffix = source_hash_prefix
        versioned_draft_id = f"review_draft:v3:repair:{versioned_suffix}"
        versioned_manifest_id = f"citation_manifest:v3:repair:{versioned_suffix}"
        versioned_docx_id = f"review_docx:v1:repair:{versioned_suffix}"
        promotion_dir = Path(
            self.workspace.artifact_path(
                f"repair_promotions/{promotion_id.replace(':', '-') }"
            )
        )
        promotion_dir.mkdir(parents=True, exist_ok=True)
        promoted_draft_path = promotion_dir / "review_draft_v3.json"
        promoted_manifest_path = promotion_dir / "citation_manifest_v3.json"
        promoted_docx_path = promotion_dir / "review.docx"

        promoted_draft = copy.deepcopy(draft_payload)
        draft_identity = dict(promoted_draft.get("draft_identity") or {})
        draft_identity.update(
            {
                "draft_id": versioned_draft_id,
                "versioned_from_artifact_id": draft_record.artifact_id,
                "repair_promotion_transaction_id": promotion_id,
            }
        )
        promoted_draft["draft_identity"] = draft_identity
        generation_context = dict(promoted_draft.get("generation_context") or {})
        generation_context["repair_promotion_transaction_id"] = promotion_id
        promoted_draft["generation_context"] = generation_context
        projections = dict(promoted_draft.get("projections") or {})
        projections["repair_promotion_transaction_id"] = promotion_id
        projections["docx_path"] = str(promoted_docx_path)
        promoted_draft["projections"] = projections

        promoted_manifest = copy.deepcopy(manifest_payload)
        manifest_identity = dict(promoted_manifest.get("manifest_identity") or {})
        manifest_identity.update(
            {
                "manifest_id": versioned_manifest_id,
                "versioned_from_artifact_id": manifest_record.artifact_id,
                "repair_promotion_transaction_id": promotion_id,
            }
        )
        promoted_manifest["manifest_identity"] = manifest_identity
        review_reference = dict(promoted_manifest.get("review_reference") or {})
        review_reference["review_draft_path"] = str(promoted_draft_path)
        review_reference["review_word_path"] = str(promoted_docx_path)
        promoted_manifest["review_reference"] = review_reference
        manifest_dependencies = dict(promoted_manifest.get("dependencies") or {})
        manifest_dependencies["repair_promotion_transaction_id"] = promotion_id
        manifest_dependencies["versioned_review_draft_id"] = versioned_draft_id
        promoted_manifest["dependencies"] = manifest_dependencies

        try:
            atomic_write_json(str(promoted_draft_path), promoted_draft)
            atomic_write_json(str(promoted_manifest_path), promoted_manifest)
            from docx_writer import rebuild_review_docx_from_structured_artifacts

            rebuild_review_docx_from_structured_artifacts(
                SimpleNamespace(logger=None),
                promoted_draft,
                promoted_manifest,
                str(promoted_docx_path),
            )
        except (OSError, TypeError, ValueError, KeyError, RuntimeError) as exc:
            return {
                "status": "blocked",
                "reason": f"versioned repair output build failed: {exc}",
                "transaction_id": transaction_id,
                "repair_structural_closure": structural.to_dict(),
                "mutation_performed": False,
            }

        promoted_validation_id = f"validation_run_result:v1:repair:{versioned_suffix}"
        promoted_validation_path = promotion_dir / "validation_run_result_v1.json"
        promoted_validation_payload = copy.deepcopy(dict(revalidation_payload))
        promoted_validation_payload["validation_run_id"] = promoted_validation_id
        promoted_validation_payload["attempt_id"] = promotion_id
        promoted_validation_payload["input_artifacts"] = {
            **dict(promoted_validation_payload.get("input_artifacts") or {}),
            "review_draft_id": versioned_draft_id,
            "review_draft_hash": file_sha256(str(promoted_draft_path)),
            "citation_manifest_id": versioned_manifest_id,
            "citation_manifest_hash": file_sha256(str(promoted_manifest_path)),
        }
        try:
            from validation.run_result import ValidationRunResultV1

            promoted_validation_model = ValidationRunResultV1.from_dict(
                promoted_validation_payload
            )
            if not promoted_validation_model.contract_satisfied:
                raise ValueError("promoted validation result contract is not satisfied")
            atomic_write_json(str(promoted_validation_path), promoted_validation_payload)
        except (TypeError, ValueError, KeyError, RuntimeError, OSError) as exc:
            return {
                "status": "blocked",
                "reason": f"promoted validation result is invalid: {exc}",
                "transaction_id": transaction_id,
                "repair_structural_closure": structural.to_dict(),
                "mutation_performed": False,
            }

        base_dependencies = [
            ArtifactDependencyRefV2.from_record(item)
            for item in (draft_record, manifest_record, canonical_validation_record)
            if item is not None
        ]
        promoted_draft_record = self.registry.register_file(
            artifact_id=versioned_draft_id,
            artifact_role="repair_promotion_review_draft",
            artifact_type="review_draft",
            artifact_version="v3",
            path=promoted_draft_path,
            producer="validation.repair_transaction.RepairTransactionService.promote_transaction",
            depends_on=base_dependencies,
            metadata={
                "versioned": True,
                "canonical_replacement": False,
                "promotion_transaction_id": promotion_id,
            },
        )
        promoted_manifest_record = self.registry.register_file(
            artifact_id=versioned_manifest_id,
            artifact_role="repair_promotion_citation_manifest",
            artifact_type="citation_manifest",
            artifact_version="v3",
            path=promoted_manifest_path,
            producer="validation.repair_transaction.RepairTransactionService.promote_transaction",
            depends_on=[*base_dependencies, ArtifactDependencyRefV2.from_record(promoted_draft_record)],
            metadata={
                "versioned": True,
                "canonical_replacement": False,
                "promotion_transaction_id": promotion_id,
            },
        )
        promoted_docx_record = self.registry.register_file(
            artifact_id=versioned_docx_id,
            artifact_role="repair_promotion_review_docx",
            artifact_type="review_docx",
            artifact_version="v1",
            path=promoted_docx_path,
            producer="validation.repair_transaction.RepairTransactionService.promote_transaction",
            depends_on=[
                ArtifactDependencyRefV2.from_record(promoted_draft_record),
                ArtifactDependencyRefV2.from_record(promoted_manifest_record),
            ],
            metadata={
                "versioned": True,
                "canonical_replacement": False,
                "promotion_transaction_id": promotion_id,
            },
        )

        evidence_dependencies = []
        for evidence_id in promoted_validation_payload.get("input_artifacts", {}).get(
            "evidence_manifest_ids", ()
        ):
            evidence_record = self.registry.get(str(evidence_id))
            if evidence_record is not None and evidence_record.status == "ready":
                evidence_dependencies.append(ArtifactDependencyRefV2.from_record(evidence_record))
        promoted_validation_record = self.registry.register_file(
            artifact_id=promoted_validation_id,
            artifact_role="repair_promotion_validation_run_result",
            artifact_type="validation_run_result",
            artifact_version="v1",
            path=promoted_validation_path,
            producer="validation.repair_transaction.RepairTransactionService.promote_transaction",
            depends_on=[
                ArtifactDependencyRefV2.from_record(promoted_draft_record),
                ArtifactDependencyRefV2.from_record(promoted_manifest_record),
                ArtifactDependencyRefV2.from_record(promoted_docx_record),
                *evidence_dependencies,
            ],
            metadata={
                "versioned": True,
                "canonical_replacement": True,
                "promotion_transaction_id": promotion_id,
                "source_revalidation_artifact_id": validation_record.artifact_id,
                "provider_receipt_closure_complete": bool(closure_payload.get("complete")),
            },
        )

        output_records = [
            promoted_draft_record,
            promoted_manifest_record,
            promoted_docx_record,
            promoted_validation_record,
        ]
        output_refs = [
            AuditArtifactRefV1(
                artifact_id=item.artifact_id,
                artifact_type=item.artifact_type,
                job_id=item.job_id,
                content_hash=item.content_hash,
            )
            for item in output_records
        ]
        input_records = [
            item
            for item in (draft_record, manifest_record, canonical_validation_record)
            if item is not None
        ]
        input_refs = [
            AuditArtifactRefV1(
                artifact_id=item.artifact_id,
                artifact_type=item.artifact_type,
                job_id=item.job_id,
                content_hash=item.content_hash,
            )
            for item in input_records
        ]
        audit_id = f"repair-promotion-audit:{versioned_suffix}"
        audit = AuditRecordV1.create(
            audit_type="repair_promotion",
            job_id=self.workspace.job_id,
            attempt_id=promotion_id,
            producer="validation.repair_transaction.RepairTransactionService.promote_transaction",
            actor=actor,
            reason=reason,
            scope={
                "source_transaction_id": transaction_id,
                "canonical_replacement": True,
                "quarantined_export": False,
            },
            target_artifacts=output_refs,
            input_artifact_refs=input_refs,
            output_artifact_refs=output_refs,
            input_hashes={item.artifact_id: item.content_hash for item in input_records},
            policy_snapshot={
                "versioned_outputs_only": True,
                "overwrite_canonical": False,
                "delete_canonical": False,
                "export_quarantined": False,
                "require_structural_closure": True,
                "advance_current_pointers": True,
            },
            disposition="promoted_versioned",
            audit_id=audit_id,
        )
        audit_path = promotion_dir / "repair_promotion_audit.json"
        atomic_write_json(str(audit_path), audit.to_dict())
        audit_record = self.registry.register_file(
            artifact_id=audit_id,
            artifact_role="repair_promotion_audit",
            artifact_type="audit_record",
            artifact_version="v1",
            path=audit_path,
            producer="validation.repair_transaction.RepairTransactionService.promote_transaction",
            depends_on=[
                *base_dependencies,
                *(ArtifactDependencyRefV2.from_record(item) for item in output_records),
            ],
        )

        lineage_id = f"repair-lineage:{versioned_suffix}"
        lineage_path = promotion_dir / "repair_lineage.json"
        lineage_payload = {
            "artifact_type": "repair_lineage",
            "artifact_version": "v1",
            "job_id": self.workspace.job_id,
            "lineage_id": lineage_id,
            "source_transaction_id": transaction_id,
            "canonical_inputs": {item.artifact_id: item.content_hash for item in input_records},
            "derived_repair_inputs": {
                item.artifact_id: item.content_hash
                for item in derived_records
                if item is not None
            },
            "versioned_outputs": {item.artifact_id: item.content_hash for item in output_records},
            "structural_closure": structural.to_dict(),
            "canonical_replacement": True,
            "previous_canonical": {
                "review_draft": draft_record.artifact_id if draft_record is not None else "",
                "citation_manifest": manifest_record.artifact_id if manifest_record is not None else "",
                "review_docx": previous_docx_record.artifact_id if previous_docx_record is not None else "",
                "validation_run_result": previous_validation_record.artifact_id if previous_validation_record is not None else "",
            },
        }
        atomic_write_json(str(lineage_path), lineage_payload)
        lineage_record = self.registry.register_file(
            artifact_id=lineage_id,
            artifact_role="repair_lineage",
            artifact_type="repair_lineage",
            artifact_version="v1",
            path=lineage_path,
            producer="validation.repair_transaction.RepairTransactionService.promote_transaction",
            depends_on=[
                ArtifactDependencyRefV2.from_record(audit_record),
                *(ArtifactDependencyRefV2.from_record(item) for item in output_records),
            ],
        )

        pointer_records = {
            "review_draft": _write_current_artifact_pointer(
                self.workspace,
                self.registry,
                kind="review_draft",
                target=promoted_draft_record,
                previous=draft_record,
                promotion_id=promotion_id,
            ),
            "citation_manifest": _write_current_artifact_pointer(
                self.workspace,
                self.registry,
                kind="citation_manifest",
                target=promoted_manifest_record,
                previous=manifest_record,
                promotion_id=promotion_id,
            ),
            "review_docx": _write_current_artifact_pointer(
                self.workspace,
                self.registry,
                kind="review_docx",
                target=promoted_docx_record,
                previous=previous_docx_record,
                promotion_id=promotion_id,
            ),
            "validation_run_result": _write_current_artifact_pointer(
                self.workspace,
                self.registry,
                kind="validation_run_result",
                target=promoted_validation_record,
                previous=previous_validation_record,
                promotion_id=promotion_id,
            ),
        }
        pointer_ids = {
            kind: record.artifact_id for kind, record in pointer_records.items()
        }

        promotion = RepairPromotionTransaction(
            transaction_id=promotion_id,
            job_id=self.workspace.job_id,
            source_transaction_id=transaction_id,
            status="promoted",
            actor=actor,
            reason=reason,
            canonical_version="repair-v3",
            review_draft_artifact_id=promoted_draft_record.artifact_id,
            citation_manifest_artifact_id=promoted_manifest_record.artifact_id,
            review_docx_artifact_id=promoted_docx_record.artifact_id,
            audit_artifact_id=audit_record.artifact_id,
            lineage_artifact_id=lineage_record.artifact_id,
            canonical_input_hashes={item.artifact_id: item.content_hash for item in input_records},
            output_hashes={item.artifact_id: item.content_hash for item in output_records},
            created_at=utc_now_iso(),
            validation_run_result_artifact_id=promoted_validation_record.artifact_id,
            current_pointer_artifact_ids=pointer_ids,
        )
        promotion_path = promotion_dir / "repair_promotion_transaction.json"
        atomic_write_json(str(promotion_path), promotion.to_dict())
        promotion_record = self.registry.register_file(
            artifact_id=promotion_id,
            artifact_role="repair_promotion_transaction",
            artifact_type=promotion.artifact_type,
            artifact_version=promotion.artifact_version,
            path=promotion_path,
            producer="validation.repair_transaction.RepairTransactionService.promote_transaction",
            depends_on=[
                ArtifactDependencyRefV2.from_record(audit_record),
                ArtifactDependencyRefV2.from_record(lineage_record),
                *(ArtifactDependencyRefV2.from_record(item) for item in output_records),
                *(ArtifactDependencyRefV2.from_record(item) for item in pointer_records.values()),
            ],
            metadata={
                "status": promotion.status,
                "canonical_replacement": True,
                "quarantined_export": False,
                "current_pointer_artifact_ids": pointer_ids,
            },
        )
        return {
            "status": "promoted",
            "job_id": self.workspace.job_id,
            "transaction_id": transaction_id,
            "promotion_transaction_id": promotion_record.artifact_id,
            "versioned_artifact_ids": [item.artifact_id for item in output_records],
            "audit_artifact_id": audit_record.artifact_id,
            "lineage_artifact_id": lineage_record.artifact_id,
            "repair_structural_closure": structural.to_dict(),
            "canonical_replacement": True,
            "canonical_paths_unchanged": True,
            "current_pointer_artifact_ids": pointer_ids,
            "validation_run_result_artifact_id": promoted_validation_record.artifact_id,
            "receipt_closure": closure_payload,
            "quarantined_export": False,
            "mutation_performed": True,
        }

    def promote(
        self,
        transaction_id: str,
        *,
        actor: str,
        reason: str,
        validation_result: Mapping[str, Any] | None = None,
        validation_record: ArtifactRecord | None = None,
        receipt_closure: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Compatibility alias for the explicit promotion boundary."""

        return self.promote_transaction(
            transaction_id,
            actor=actor,
            reason=reason,
            validation_result=validation_result,
            validation_record=validation_record,
            receipt_closure=receipt_closure,
        )


__all__ = [
    "REPAIR_TRANSACTION_ARTIFACT_TYPE",
    "REPAIR_TRANSACTION_ARTIFACT_VERSION",
    "RepairPromotionTransaction",
    "RepairTransactionRecord",
    "RepairTransactionService",
    "current_artifact_record",
]
