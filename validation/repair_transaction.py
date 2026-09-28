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
import os
import re
import tempfile
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
from services.job_workspace import (
    JobWorkspace,
    atomic_write_json,
    publish_bytes_artifact,
    publish_json_artifact,
    utc_now_iso,
)
from services.audit_record import AuditArtifactRefV1, AuditRecordV1
from services.queue_service import LocalPublicationContext
from services.citation_manifest import build_citation_manifest_from_review_draft
from services.citation_ref_catalog import resolve_ref_id, validate_document_ref_catalog
from services.sentence_segmenter import SENTENCE_SEGMENTER_VERSION, segment_sentences
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


def _repair_apply_hash(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16]


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
    # CurrentArtifactSet is the authoritative production pointer.  The older
    # per-kind pointers remain readable only as a migration fallback for jobs
    # which predate the atomic set contract.
    try:
        current_set = registry.resolve_current_artifact_set()
    except Exception:
        if registry.current_artifact_set_pointer() is not None:
            return None
        current_set = None
    if current_set is not None:
        target_ids = {
            "review_draft": current_set.review_draft_artifact_id,
            "citation_manifest": current_set.citation_manifest_artifact_id,
            "review_docx": current_set.review_docx_artifact_id,
            "validation_run_result": current_set.validation_run_result_artifact_id,
        }
        target_hashes = {
            "review_draft": current_set.review_draft_artifact_hash,
            "citation_manifest": current_set.citation_manifest_artifact_hash,
            "review_docx": current_set.review_docx_artifact_hash,
            "validation_run_result": current_set.validation_run_result_artifact_hash,
        }
        target = registry.get(target_ids[kind])
        if target is None or target.status != "ready":
            return None
        if (
            target.artifact_type != spec["artifact_type"]
            or target.artifact_version != spec["artifact_version"]
            or target.content_hash != target_hashes[kind]
        ):
            return None
        try:
            if file_sha256(target.path) != target_hashes[kind]:
                return None
        except OSError:
            return None
        return target
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
    publication_context: Any | None = None,
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
    publication_context = (
        publication_context
        or getattr(registry, "publication_context", None)
        or LocalPublicationContext()
    )
    dependencies = [ArtifactDependencyRefV2.from_record(target)]
    if previous is not None and previous.artifact_id != target.artifact_id:
        dependencies.append(ArtifactDependencyRefV2.from_record(previous))
    return publish_json_artifact(
        publication_context,
        registry,
        pointer_path,
        payload,
        artifact_id=str(spec["pointer_id"]),
        artifact_role="current_artifact_pointer",
        artifact_type="current_artifact_pointer",
        artifact_version="v1",
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
        payload["artifact_type"] = REPAIR_TRANSACTION_ARTIFACT_TYPE
        payload["artifact_version"] = REPAIR_TRANSACTION_ARTIFACT_VERSION
        payload["previous_artifact_ids"] = list(self.previous_artifact_ids)
        payload["applied_artifact_ids"] = list(self.applied_artifact_ids)
        payload["applied_patch_ids"] = list(self.applied_patch_ids)
        return payload


@dataclass(frozen=True)
class CitationMappingCorrectionV1:
    """Explicit occurrence-level correction from a human reviewer."""

    occurrence_id: str
    expected_ref_id: str
    expected_paper_id: str
    replacement_ref_id: str
    replacement_paper_id: str

    def validate(self) -> None:
        if not all(
            str(value or "").strip()
            for value in (
                self.occurrence_id,
                self.expected_ref_id,
                self.expected_paper_id,
                self.replacement_ref_id,
                self.replacement_paper_id,
            )
        ):
            raise ValueError("citation mapping correction requires a complete occurrence and source mapping")
        if not re.fullmatch(r"R\d{3,}", self.expected_ref_id):
            raise ValueError("citation mapping expected_ref_id is invalid")
        if not re.fullmatch(r"R\d{3,}", self.replacement_ref_id):
            raise ValueError("citation mapping replacement_ref_id is invalid")

    def to_dict(self) -> dict[str, str]:
        self.validate()
        return asdict(self)


@dataclass(frozen=True)
class ManualRepairApprovalV1:
    """Typed, reviewer-approved correction bound to exact Registry inputs."""

    approval_id: str
    job_id: str
    source_report_plan_id: str
    source_report_plan_hash: str
    actor: str
    reason: str
    source_claim_id: str
    block_id: str
    expected_anchor_hash: str
    replacement_text: str
    source_evidence_ids: tuple[str, ...]
    canonical_input_ids: Mapping[str, str]
    canonical_input_hashes: Mapping[str, str]
    citation_mapping: CitationMappingCorrectionV1 | None = None
    created_at: str = field(default_factory=utc_now_iso)
    artifact_type: str = "manual_repair_approval"
    artifact_version: str = "v1"
    schema_version: str = "manual_repair_approval_v1"

    def validate(self) -> None:
        required = (
            self.approval_id,
            self.job_id,
            self.source_report_plan_id,
            self.actor,
            self.reason,
            self.source_claim_id,
            self.block_id,
            self.replacement_text,
        )
        if any(not str(value or "").strip() for value in required):
            raise ValueError("manual repair approval requires plan, reviewer, target, and replacement text")
        hashes = {
            **dict(self.canonical_input_hashes),
            "source_report_plan": self.source_report_plan_hash,
        }
        if set(self.canonical_input_ids) != {"review_draft", "citation_manifest", "validation"}:
            raise ValueError("manual repair approval must bind current draft, manifest, and validation identities")
        if set(self.canonical_input_hashes) != {"review_draft", "citation_manifest", "validation"}:
            raise ValueError("manual repair approval must bind current draft, manifest, and validation hashes")
        if any(
            len(str(value)) != 64
            or any(char not in "0123456789abcdef" for char in str(value).lower())
            for value in hashes.values()
        ):
            raise ValueError("manual repair approval input hashes must be SHA-256 values")
        if (
            len(self.expected_anchor_hash) != 64
            or any(char not in "0123456789abcdef" for char in self.expected_anchor_hash.lower())
        ):
            raise ValueError("manual repair approval expected_anchor_hash must be a SHA-256 value")
        if not self.source_evidence_ids or len(set(self.source_evidence_ids)) != len(self.source_evidence_ids):
            raise ValueError("manual repair approval requires unique source evidence identities")
        if self.citation_mapping is not None:
            self.citation_mapping.validate()
            if self.citation_mapping.replacement_paper_id not in self.source_evidence_ids:
                raise ValueError("corrected citation source must be included in source_evidence_ids")
            tokens = re.findall(r"\[\[cite_ref:(R\d{3,})\]\]", self.replacement_text)
            if tokens.count(self.citation_mapping.replacement_ref_id) != 1:
                raise ValueError("replacement text must contain the corrected citation exactly once")

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        payload = asdict(self)
        payload["source_evidence_ids"] = list(self.source_evidence_ids)
        payload["canonical_input_ids"] = dict(self.canonical_input_ids)
        payload["canonical_input_hashes"] = dict(self.canonical_input_hashes)
        payload["citation_mapping"] = (
            self.citation_mapping.to_dict() if self.citation_mapping is not None else None
        )
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
    validation_disposition_artifact_id: str = ""
    current_pointer_artifact_ids: Mapping[str, str] = field(default_factory=dict)
    current_artifact_set_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["canonical_input_hashes"] = dict(self.canonical_input_hashes)
        payload["output_hashes"] = dict(self.output_hashes)
        payload["current_pointer_artifact_ids"] = dict(self.current_pointer_artifact_ids)
        return payload


class RepairTransactionService:
    def __init__(
        self,
        workspace: JobWorkspace,
        registry: ArtifactRegistry,
        publication_context: Any | None = None,
    ) -> None:
        self.workspace = workspace
        self.registry = registry
        self.publication_context = (
            publication_context
            or getattr(registry, "publication_context", None)
            or LocalPublicationContext()
        )
        self.closure_service = ValidationClosureService(workspace, registry)

    def _publish_json(self, path: str | Path, payload: Any, **register_kwargs: Any) -> ArtifactRecord:
        return publish_json_artifact(
            self.publication_context,
            self.registry,
            path,
            payload,
            **register_kwargs,
        )

    def _publish_bytes(self, path: str | Path, payload: bytes, **register_kwargs: Any) -> ArtifactRecord:
        return publish_bytes_artifact(
            self.publication_context,
            self.registry,
            path,
            payload,
            **register_kwargs,
        )

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
            # Report-only planning records the issue and human action only.
            # A PatchProposal is an executable intent, so an empty
            # ``proposed_text`` is not allowed to masquerade as one.  A
            # proposal is created only by the separate auto-safe path after
            # a complete, guarded operation has been supplied.
            block_text = str(block.get("text") or "")
            manual_review_actions.append(
                ManualReviewAction(
                    action_id=f"manual-review:{issue_id}",
                    issue_id=issue_id,
                    action="confirm_mapping_or_propose_complete_structural_patch",
                    rationale="report-first plans never turn a validation finding into an automatic rewrite",
                    required_inputs=["review_draft", "citation_manifest", "paper_artifacts"],
                    metadata={
                        "block_text": block_text,
                        "span_start": claim.get("span_start"),
                        "span_end": claim.get("span_end"),
                        "proposal_allowed_only_after_guarded_operation": True,
                    },
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
        dependencies: list[ArtifactDependencyRefV2] = []
        previous_records = self._dependency_records()
        for record in previous_records:
            if record.status == "ready":
                dependencies.append(ArtifactDependencyRefV2.from_record(record))
        record = self._publish_json(
            path,
            plan.to_dict(),
            artifact_id=f"repair_plan:{plan.plan_id}",
            artifact_role="repair_plan",
            artifact_type="repair_plan",
            artifact_version=plan.artifact_version,
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
        transaction_record = self._publish_json(
            transaction_path,
            transaction.to_dict(),
            artifact_id=transaction_id,
            artifact_role="repair_transaction",
            artifact_type=REPAIR_TRANSACTION_ARTIFACT_TYPE,
            artifact_version=REPAIR_TRANSACTION_ARTIFACT_VERSION,
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

    def apply_manual_proposal(
        self,
        plan_id: str,
        manual_proposal: Mapping[str, Any],
        *,
        actor: str,
        reason: str,
    ) -> dict[str, Any]:
        """Apply one explicit reviewer correction into quarantined derived artifacts.

        The source report-only plan remains unchanged. The new typed approval is
        bound to the current draft, citation manifest, validation result, target
        block, source paper identities, and (for mapping repairs) one exact
        citation occurrence plus its old and replacement catalog mappings.
        """

        source_plan_id = str(plan_id or "").strip()
        actor = str(actor or "").strip()
        reason = str(reason or "").strip()
        if not source_plan_id or not actor or not reason:
            return {
                "status": "blocked",
                "plan_id": source_plan_id,
                "reason": "manual repair requires a report plan, reviewer, and reason",
                "mutation_performed": False,
            }
        if not isinstance(manual_proposal, Mapping):
            return {
                "status": "blocked",
                "plan_id": source_plan_id,
                "reason": "manual repair proposal must be an object",
                "mutation_performed": False,
            }
        allowed_fields = {
            "block_id",
            "expected_anchor_hash",
            "replacement_text",
            "source_evidence_ids",
            "citation_mapping",
        }
        if set(manual_proposal) - allowed_fields:
            return {
                "status": "blocked",
                "plan_id": source_plan_id,
                "reason": "manual repair proposal contains unsupported fields",
                "mutation_performed": False,
            }

        closure = self.closure_service.inspect()
        source_plan_record = self.registry.get(source_plan_id) or self.registry.get(
            f"repair_plan:{source_plan_id}"
        )
        if source_plan_record is None or source_plan_record.status != "ready":
            return {
                "status": "blocked",
                "plan_id": source_plan_id,
                "reason": "source report-only repair plan is not a verified ready artifact",
                "mutation_performed": False,
            }
        source_plan_payload = _load_json(source_plan_record)
        if (
            source_plan_payload is None
            or source_plan_payload.get("artifact_type") != "repair_plan"
            or source_plan_payload.get("artifact_version") != "v1"
            or source_plan_payload.get("policy") != RepairPolicy.REPORT_FIRST.value
            or str(source_plan_payload.get("created_from_job_id") or "") != self.workspace.job_id
        ):
            return {
                "status": "blocked",
                "plan_id": source_plan_id,
                "reason": "manual repair requires this job's report-only plan",
                "mutation_performed": False,
            }
        if file_sha256(source_plan_record.path) != source_plan_record.content_hash:
            return {
                "status": "blocked",
                "plan_id": source_plan_id,
                "reason": "source report-only plan bytes changed",
                "mutation_performed": False,
            }

        draft_record, manifest_record, validation_record = self._canonical_inputs()
        if any(
            record is None or record.status != "ready"
            for record in (draft_record, manifest_record, validation_record)
        ):
            return {
                "status": "blocked",
                "plan_id": source_plan_id,
                "reason": "current draft, citation manifest, and validation result are required",
                "mutation_performed": False,
            }
        assert draft_record is not None and manifest_record is not None and validation_record is not None
        for record in (draft_record, manifest_record, validation_record):
            try:
                actual_hash = file_sha256(record.path)
            except OSError as exc:
                return {
                    "status": "blocked",
                    "plan_id": source_plan_id,
                    "reason": f"current repair input cannot be read: {record.artifact_id}: {exc}",
                    "mutation_performed": False,
                }
            if actual_hash != record.content_hash:
                return {
                    "status": "blocked",
                    "plan_id": source_plan_id,
                    "reason": f"current repair input bytes changed: {record.artifact_id}",
                    "mutation_performed": False,
                }
        if str(source_plan_payload.get("validation_report_id") or "") != validation_record.artifact_id:
            return {
                "status": "blocked",
                "plan_id": source_plan_id,
                "reason": "source report-only plan names a different validation result",
                "mutation_performed": False,
            }
        source_plan_metadata = source_plan_record.metadata or {}
        if str(source_plan_metadata.get("closure_evidence_hash") or "") != closure.evidence_hash:
            return {
                "status": "blocked",
                "plan_id": source_plan_id,
                "reason": "validation findings changed after report-only planning",
                "mutation_performed": False,
            }
        if closure.blocking_issues:
            return {
                "status": "blocked",
                "plan_id": source_plan_id,
                "reason": "current validation closure has unresolved structural blockers",
                "blocking_issues": list(closure.blocking_issues),
                "mutation_performed": False,
            }
        if closure.semantic_status not in {"findings", "needs_review"}:
            return {
                "status": "blocked",
                "plan_id": source_plan_id,
                "reason": "manual repair requires current validation findings",
                "validation_disposition": closure.semantic_status,
                "mutation_performed": False,
            }

        dependency_bundle = source_plan_payload.get("dependency_hash_bundle")
        if not isinstance(dependency_bundle, Mapping) or (
            str(dependency_bundle.get("review_draft_hash") or "") != draft_record.content_hash
            or str(dependency_bundle.get("citation_manifest_hash") or "") != manifest_record.content_hash
        ):
            return {
                "status": "blocked",
                "plan_id": source_plan_id,
                "reason": "report-only plan is not bound to the current draft and citation manifest",
                "mutation_performed": False,
            }
        plan_dependencies = {item.artifact_id: item for item in source_plan_record.depends_on}
        for record in (draft_record, manifest_record, validation_record):
            dependency = plan_dependencies.get(record.artifact_id)
            if dependency is None or dependency.content_hash != record.content_hash:
                return {
                    "status": "blocked",
                    "plan_id": source_plan_id,
                    "reason": f"report-only plan dependency changed: {record.artifact_id}",
                    "mutation_performed": False,
                }

        try:
            review_draft = _load_json(draft_record)
            citation_manifest = _load_json(manifest_record)
            validation_payload = _load_json(validation_record)
            if review_draft is None or citation_manifest is None or validation_payload is None:
                raise ValueError("current repair inputs are not readable JSON objects")
            from validation.run_result import ValidationRunDisposition, ValidationRunResultV1

            validation_result = ValidationRunResultV1.from_dict(validation_payload)
            validation_result.validate()
        except (OSError, UnicodeError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            return {
                "status": "blocked",
                "plan_id": source_plan_id,
                "reason": f"current validation result is invalid: {exc}",
                "mutation_performed": False,
            }
        if (
            not validation_result.contract_satisfied
            or validation_result.validation_disposition
            not in {ValidationRunDisposition.FINDINGS, ValidationRunDisposition.NEEDS_REVIEW}
            or validation_result.input_artifacts.review_draft_id != draft_record.artifact_id
            or validation_result.input_artifacts.review_draft_hash != draft_record.content_hash
            or validation_result.input_artifacts.citation_manifest_id != manifest_record.artifact_id
            or validation_result.input_artifacts.citation_manifest_hash != manifest_record.content_hash
        ):
            return {
                "status": "blocked",
                "plan_id": source_plan_id,
                "reason": "manual repair validation is not bound to the exact current canonical inputs",
                "mutation_performed": False,
            }

        block_id = str(manual_proposal.get("block_id") or "").strip()
        expected_anchor_hash = str(manual_proposal.get("expected_anchor_hash") or "").strip().lower()
        replacement_text = str(manual_proposal.get("replacement_text") or "")
        raw_source_evidence_ids = manual_proposal.get("source_evidence_ids")
        if not isinstance(raw_source_evidence_ids, (list, tuple)):
            return {
                "status": "blocked",
                "plan_id": source_plan_id,
                "reason": "source_evidence_ids must be an explicit array",
                "mutation_performed": False,
            }
        source_evidence_ids = tuple(
            dict.fromkeys(
                str(item).strip()
                for item in raw_source_evidence_ids
                if str(item).strip()
            )
        )
        if (
            not block_id
            or not replacement_text.strip()
            or len(expected_anchor_hash) != 64
            or any(char not in "0123456789abcdef" for char in expected_anchor_hash)
            or not source_evidence_ids
        ):
            return {
                "status": "blocked",
                "plan_id": source_plan_id,
                "reason": "manual proposal needs a block, full anchor hash, replacement text, and source evidence",
                "mutation_performed": False,
            }
        draft_blocks = [
            block
            for section in (review_draft.get("content") or {}).get("sections", [])
            if isinstance(section, Mapping)
            for block in section.get("blocks", [])
            if isinstance(block, Mapping) and str(block.get("block_id") or "") == block_id
        ]
        if len(draft_blocks) != 1:
            return {
                "status": "blocked",
                "plan_id": source_plan_id,
                "reason": "manual proposal target block is missing or ambiguous",
                "mutation_performed": False,
            }
        original_text = str(draft_blocks[0].get("text") or "")
        if (
            not original_text
            or original_text == replacement_text
            or hashlib.sha256(original_text.encode("utf-8")).hexdigest() != expected_anchor_hash
        ):
            return {
                "status": "blocked",
                "plan_id": source_plan_id,
                "reason": "manual proposal anchor is stale or replacement text is unchanged",
                "mutation_performed": False,
            }

        matching_claims = [
            item
            for item in validation_result.claim_results
            if block_id in item.block_ids and item.verdict.value != "supported"
        ]
        if len(matching_claims) != 1:
            return {
                "status": "blocked",
                "plan_id": source_plan_id,
                "reason": "manual proposal must resolve exactly one current validation finding",
                "mutation_performed": False,
            }
        claim = matching_claims[0]
        issues = [
            item
            for item in source_plan_payload.get("issues") or ()
            if isinstance(item, Mapping) and str(item.get("block_id") or "") == block_id
        ]
        if not issues:
            return {
                "status": "blocked",
                "plan_id": source_plan_id,
                "reason": "target block is not a finding in the report-only plan",
                "mutation_performed": False,
            }

        paper_records = [
            record
            for record in self.registry.list_records()
            if record.status == "ready"
            and record.artifact_type in {"paper_artifact", "stage1_paper_artifact"}
        ]
        paper_payload_by_key: dict[str, dict[str, Any]] = {}
        paper_record_by_key: dict[str, ArtifactRecord] = {}
        for record in paper_records:
            payload = _load_json(record)
            identity = payload.get("paper_identity") if isinstance(payload, Mapping) else None
            paper_key = str(
                identity.get("canonical_paper_key") if isinstance(identity, Mapping) else ""
            ).strip()
            if paper_key and payload is not None:
                paper_payload_by_key[paper_key] = payload
                paper_record_by_key[paper_key] = record
        if not set(source_evidence_ids).issubset(paper_payload_by_key):
            return {
                "status": "blocked",
                "plan_id": source_plan_id,
                "reason": "manual proposal source evidence is not a ready current paper artifact",
                "mutation_performed": False,
            }

        raw_mapping = manual_proposal.get("citation_mapping")
        citation_mapping: CitationMappingCorrectionV1 | None = None
        catalog_record: ArtifactRecord | None = None
        catalog_payload: dict[str, Any] = {}
        target_occurrence: dict[str, Any] | None = None
        if raw_mapping is not None:
            if not isinstance(raw_mapping, Mapping):
                return {
                    "status": "blocked",
                    "plan_id": source_plan_id,
                    "reason": "citation_mapping must be an occurrence-level object",
                    "mutation_performed": False,
                }
            mapping_fields = {
                "occurrence_id",
                "expected_ref_id",
                "expected_paper_id",
                "replacement_ref_id",
                "replacement_paper_id",
            }
            if set(raw_mapping) != mapping_fields:
                return {
                    "status": "blocked",
                    "plan_id": source_plan_id,
                    "reason": "citation_mapping must bind one occurrence and complete old/new identities",
                    "mutation_performed": False,
                }
            citation_mapping = CitationMappingCorrectionV1(
                occurrence_id=str(raw_mapping.get("occurrence_id") or ""),
                expected_ref_id=str(raw_mapping.get("expected_ref_id") or ""),
                expected_paper_id=str(raw_mapping.get("expected_paper_id") or ""),
                replacement_ref_id=str(raw_mapping.get("replacement_ref_id") or ""),
                replacement_paper_id=str(raw_mapping.get("replacement_paper_id") or ""),
            )
            try:
                citation_mapping.validate()
            except ValueError as exc:
                return {
                    "status": "blocked",
                    "plan_id": source_plan_id,
                    "reason": str(exc),
                    "mutation_performed": False,
                }
            if claim.root_causes and _root_cause(claim.root_causes) is not RepairRootCause.CITATION_MAPPING_ERROR:
                return {
                    "status": "blocked",
                    "plan_id": source_plan_id,
                    "reason": "citation mapping approval does not match the validation root cause",
                    "mutation_performed": False,
                }
            occurrences = citation_manifest.get("occurrences")
            matching_occurrences = [
                dict(item)
                for item in occurrences or ()
                if isinstance(item, Mapping)
                and str(item.get("occurrence_id") or "") == citation_mapping.occurrence_id
            ]
            if (
                not isinstance(occurrences, list)
                or len(matching_occurrences) != 1
                or str(matching_occurrences[0].get("block_id") or "") != block_id
                or str(matching_occurrences[0].get("ref_id") or "") != citation_mapping.expected_ref_id
                or str(
                    matching_occurrences[0].get("canonical_paper_key")
                    or matching_occurrences[0].get("paper_id")
                    or ""
                ) != citation_mapping.expected_paper_id
            ):
                return {
                    "status": "blocked",
                    "plan_id": source_plan_id,
                    "reason": "citation occurrence identity or current mapping changed",
                    "mutation_performed": False,
                }
            target_occurrence = matching_occurrences[0]
            catalog_record = self.registry.get("citation_ref_catalog")
            if catalog_record is None or catalog_record.status != "ready":
                return {
                    "status": "blocked",
                    "plan_id": source_plan_id,
                    "reason": "current citation reference catalog is required for mapping repair",
                    "mutation_performed": False,
                }
            catalog_payload = _load_json(catalog_record) or {}
            try:
                validate_document_ref_catalog(catalog_payload)
            except (TypeError, ValueError, KeyError) as exc:
                return {
                    "status": "blocked",
                    "plan_id": source_plan_id,
                    "reason": f"citation reference catalog is invalid: {exc}",
                    "mutation_performed": False,
                }
            expected_entry = resolve_ref_id(catalog_payload, citation_mapping.expected_ref_id)
            replacement_entry = resolve_ref_id(catalog_payload, citation_mapping.replacement_ref_id)
            if (
                expected_entry is None
                or replacement_entry is None
                or str(expected_entry.get("canonical_paper_key") or expected_entry.get("paper_id") or "")
                != citation_mapping.expected_paper_id
                or str(replacement_entry.get("canonical_paper_key") or replacement_entry.get("paper_id") or "")
                != citation_mapping.replacement_paper_id
                or citation_mapping.replacement_paper_id not in source_evidence_ids
            ):
                return {
                    "status": "blocked",
                    "plan_id": source_plan_id,
                    "reason": "corrected citation must resolve through the current catalog to approved source evidence",
                    "mutation_performed": False,
                }
        elif _root_cause(claim.root_causes) is RepairRootCause.CITATION_MAPPING_ERROR:
            return {
                "status": "blocked",
                "plan_id": source_plan_id,
                "reason": "citation mapping findings require an explicit occurrence-level correction",
                "mutation_performed": False,
            }
        elif not set(source_evidence_ids).intersection(set(claim.paper_ids)):
            return {
                "status": "blocked",
                "plan_id": source_plan_id,
                "reason": "text correction source evidence must match the current validation finding",
                "mutation_performed": False,
            }

        summary_record = self.registry.get("summary_file")
        if summary_record is None or summary_record.status != "ready":
            return {
                "status": "blocked",
                "plan_id": source_plan_id,
                "reason": "current Stage 1 summary source is required to rebuild citations",
                "mutation_performed": False,
            }
        try:
            summary_payload = json.loads(Path(summary_record.path).read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            return {
                "status": "blocked",
                "plan_id": source_plan_id,
                "reason": f"current Stage 1 summary source is unreadable: {exc}",
                "mutation_performed": False,
            }
        if not isinstance(summary_payload, list) or not summary_payload:
            return {
                "status": "blocked",
                "plan_id": source_plan_id,
                "reason": "current Stage 1 summary source must be a non-empty array",
                "mutation_performed": False,
            }
        if catalog_record is None:
            catalog_record = self.registry.get("citation_ref_catalog")
        if catalog_record is None or catalog_record.status != "ready":
            return {
                "status": "blocked",
                "plan_id": source_plan_id,
                "reason": "current citation reference catalog is required to rebuild citations",
                "mutation_performed": False,
            }
        if not catalog_payload:
            catalog_payload = _load_json(catalog_record) or {}

        # Bind the executable PatchProposal to exactly the selected source
        # evidence. Normal auto-safe apply remains unavailable for this plan.
        selected_papers = [paper_payload_by_key[key] for key in source_evidence_ids]
        summary_bundle: dict[str, Any] = {}
        for paper_payload in selected_papers:
            analysis = paper_payload.get("analysis")
            if isinstance(analysis, Mapping) and isinstance(analysis.get("ai_summary"), Mapping):
                summary_bundle.update(dict(analysis["ai_summary"]))
        primary_paper = selected_papers[0]
        selected_visual_refs = (primary_paper.get("stage1_inputs") or {}).get("selected_visual_refs", [])
        proposal_dependencies = DependencyHashBundle(
            summary_hash=_repair_apply_hash(summary_bundle) if summary_bundle else NOT_APPLICABLE,
            paper_artifact_hash=(
                _repair_apply_hash(selected_papers if len(selected_papers) > 1 else selected_papers[0])
            ),
            visual_manifest_hash=NOT_APPLICABLE,
            selected_visual_refs_hash=_repair_apply_hash(selected_visual_refs),
            review_draft_hash=draft_record.content_hash,
            citation_manifest_hash=manifest_record.content_hash,
            outline_hash=str(dependency_bundle.get("outline_hash") or NOT_APPLICABLE),
        )
        original_hash_8 = hashlib.sha256(original_text.encode("utf-8")).hexdigest()[:8]
        approval_seed = {
            "source_report_plan_id": source_plan_record.artifact_id,
            "source_report_plan_hash": source_plan_record.content_hash,
            "canonical_input_ids": {
                "review_draft": draft_record.artifact_id,
                "citation_manifest": manifest_record.artifact_id,
                "validation": validation_record.artifact_id,
            },
            "canonical_input_hashes": {
                "review_draft": draft_record.content_hash,
                "citation_manifest": manifest_record.content_hash,
                "validation": validation_record.content_hash,
            },
            "actor": actor,
            "reason": reason,
            "claim_id": claim.claim_result_id,
            "block_id": block_id,
            "expected_anchor_hash": expected_anchor_hash,
            "replacement_text": replacement_text,
            "source_evidence_ids": list(source_evidence_ids),
            "citation_mapping": citation_mapping.to_dict() if citation_mapping is not None else None,
        }
        approval_id = "manual-approval:" + _hash(approval_seed)[:24]
        manual_plan_id = "manual-repair-plan:" + _hash({"approval_id": approval_id})[:24]
        manual_plan_artifact_id = f"repair_plan:{manual_plan_id}"
        manual_plan_record = self.registry.get(manual_plan_artifact_id)
        approval: ManualRepairApprovalV1
        if manual_plan_record is not None:
            existing_plan = _load_json(manual_plan_record)
            if not isinstance(existing_plan, Mapping):
                return {
                    "status": "blocked",
                    "plan_id": source_plan_id,
                    "reason": "existing manual repair plan payload is malformed",
                    "mutation_performed": False,
                }
            existing_approval = existing_plan.get("manual_approval")
            if not isinstance(existing_approval, Mapping):
                return {
                    "status": "blocked",
                    "plan_id": source_plan_id,
                    "reason": "existing manual approval payload is malformed",
                    "mutation_performed": False,
                }
            existing_approval_seed = {
                "source_report_plan_id": existing_approval.get("source_report_plan_id"),
                "source_report_plan_hash": existing_approval.get("source_report_plan_hash"),
                "canonical_input_ids": existing_approval.get("canonical_input_ids"),
                "canonical_input_hashes": existing_approval.get("canonical_input_hashes"),
                "actor": existing_approval.get("actor"),
                "reason": existing_approval.get("reason"),
                "claim_id": existing_approval.get("source_claim_id"),
                "block_id": existing_approval.get("block_id"),
                "expected_anchor_hash": existing_approval.get("expected_anchor_hash"),
                "replacement_text": existing_approval.get("replacement_text"),
                "source_evidence_ids": existing_approval.get("source_evidence_ids"),
                "citation_mapping": existing_approval.get("citation_mapping"),
            }
            if (
                manual_plan_record.status != "ready"
                or _hash(existing_approval_seed) != _hash(approval_seed)
            ):
                return {
                    "status": "blocked",
                    "plan_id": source_plan_id,
                    "reason": "existing manual approval identity conflicts with the current proposal",
                    "mutation_performed": False,
                }
            raw_mapping = existing_approval.get("citation_mapping")
            approval = ManualRepairApprovalV1(
                approval_id=str(existing_approval.get("approval_id") or ""),
                job_id=str(existing_approval.get("job_id") or ""),
                source_report_plan_id=str(existing_approval.get("source_report_plan_id") or ""),
                source_report_plan_hash=str(existing_approval.get("source_report_plan_hash") or ""),
                actor=str(existing_approval.get("actor") or ""),
                reason=str(existing_approval.get("reason") or ""),
                source_claim_id=str(existing_approval.get("source_claim_id") or ""),
                block_id=str(existing_approval.get("block_id") or ""),
                expected_anchor_hash=str(existing_approval.get("expected_anchor_hash") or ""),
                replacement_text=str(existing_approval.get("replacement_text") or ""),
                source_evidence_ids=tuple(str(item) for item in existing_approval.get("source_evidence_ids") or ()),
                canonical_input_ids=dict(existing_approval.get("canonical_input_ids") or {}),
                canonical_input_hashes=dict(existing_approval.get("canonical_input_hashes") or {}),
                citation_mapping=(
                    CitationMappingCorrectionV1(**dict(raw_mapping))
                    if isinstance(raw_mapping, Mapping)
                    else None
                ),
                created_at=str(existing_approval.get("created_at") or ""),
            )
            approval.validate()
        else:
            approval = ManualRepairApprovalV1(
                approval_id=approval_id,
                job_id=self.workspace.job_id,
                source_report_plan_id=source_plan_record.artifact_id,
                source_report_plan_hash=source_plan_record.content_hash,
                actor=actor,
                reason=reason,
                source_claim_id=claim.claim_result_id,
                block_id=block_id,
                expected_anchor_hash=expected_anchor_hash,
                replacement_text=replacement_text,
                source_evidence_ids=source_evidence_ids,
                canonical_input_ids=dict(approval_seed["canonical_input_ids"]),
                canonical_input_hashes=dict(approval_seed["canonical_input_hashes"]),
                citation_mapping=citation_mapping,
            )
            approval.validate()

        proposal = PatchProposal(
            proposal_id=approval.approval_id,
            citation_id=(
                approval.citation_mapping.occurrence_id
                if approval.citation_mapping is not None
                else claim.claim_result_id
            ),
            root_cause=_root_cause(claim.root_causes),
            granularity=PatchGranularity.BLOCK,
            target=PatchTargetSignature(
                block_id=block_id,
                anchor_text=original_text[:80] + ("..." if len(original_text) > 80 else ""),
                anchor_hash=original_hash_8,
            ),
            original_text=original_text,
            proposed_text=replacement_text,
            confidence=1.0,
            fix_strategy="explicit_manual_replacement",
            dependency_bundle=proposal_dependencies,
            metadata={
                "paper_ids": list(source_evidence_ids),
                "manual_approval_id": approval.approval_id,
                "source_claim_id": claim.claim_result_id,
                "source_report_plan_id": source_plan_record.artifact_id,
                "citation_occurrence_id": (
                    approval.citation_mapping.occurrence_id
                    if approval.citation_mapping is not None
                    else ""
                ),
            },
        )
        executable_plan = RepairPlan(
            plan_id=manual_plan_id,
            created_at=approval.created_at,
            created_from_job_id=self.workspace.job_id,
            validation_report_id=validation_record.artifact_id,
            proposals=[proposal],
            policy=RepairPolicy.REPORT_FIRST,
            dependency_hash_bundle=self._dependency_bundle(closure),
            issues=[],
            manual_review_actions=[],
        )
        manual_plan_payload = executable_plan.to_dict()
        manual_plan_payload["manual_approval"] = approval.to_dict()
        manual_plan_payload["source_report_plan_hash"] = source_plan_record.content_hash

        paper_artifacts_for_repair = list(paper_payload_by_key.values())
        try:
            guard_probe = run_repair_apply(
                repair_plan=executable_plan,
                review_draft=copy.deepcopy(review_draft),
                citation_manifest=copy.deepcopy(citation_manifest),
                paper_artifacts=paper_artifacts_for_repair,
                job_id=self.workspace.job_id,
                dry_run=True,
                require_auto_safe=False,
                visual_manifest={},
            )
            proposal_checks = list(guard_probe.get("proposal_checks") or ())
            if not proposal_checks or any(not bool(item.get("can_apply")) for item in proposal_checks):
                return {
                    "status": "blocked",
                    "plan_id": source_plan_id,
                    "reason": "manual proposal did not pass current version, anchor, and source guards",
                    "proposal_checks": proposal_checks,
                    "mutation_performed": False,
                }
            apply_payload = run_repair_apply(
                repair_plan=executable_plan,
                review_draft=copy.deepcopy(review_draft),
                citation_manifest=copy.deepcopy(citation_manifest),
                paper_artifacts=paper_artifacts_for_repair,
                job_id=self.workspace.job_id,
                dry_run=False,
                require_auto_safe=False,
                visual_manifest={},
            )
        except (OSError, TypeError, ValueError, KeyError) as exc:
            return {
                "status": "blocked",
                "plan_id": source_plan_id,
                "reason": f"manual repair guard execution failed: {exc}",
                "mutation_performed": False,
            }
        for applied_record in apply_payload.get("applied_records") or ():
            if isinstance(applied_record, dict):
                applied_record["applied_at"] = approval.created_at
        apply_result = dict(apply_payload.get("apply_result") or {})
        if int(apply_result.get("applied_count") or 0) != 1:
            return {
                "status": "blocked",
                "plan_id": source_plan_id,
                "reason": "manual proposal did not pass current version, anchor, and source guards",
                "apply_result": apply_result,
                "mutation_performed": False,
            }
        patched_draft = apply_payload.get("patched_review_draft")
        if not isinstance(patched_draft, Mapping):
            return {
                "status": "blocked",
                "plan_id": source_plan_id,
                "reason": "manual repair executor did not return a draft object",
                "mutation_performed": False,
            }
        patched_draft = copy.deepcopy(dict(patched_draft))
        repaired_content = patched_draft.get("content")
        repaired_sections = repaired_content.get("sections") if isinstance(repaired_content, Mapping) else None
        if isinstance(repaired_sections, list):
            for section in repaired_sections:
                if not isinstance(section, dict):
                    continue
                if str(section.get("title") or section.get("heading") or "").strip():
                    continue
                section_title = str(section.get("section_title") or "").strip()
                if section_title:
                    # Review v3 stores the heading as section_title; the repair
                    # semantic closure consumes title/heading. Preserve both in
                    # this derived candidate while leaving the canonical bytes intact.
                    section["title"] = section_title

        patched_block = _find_block(patched_draft, block_id)
        if not isinstance(patched_block, dict):
            return {
                "status": "blocked",
                "plan_id": source_plan_id,
                "reason": "patched draft no longer contains the approved target block",
                "mutation_performed": False,
            }
        if citation_mapping is not None and target_occurrence is not None:
            citations = patched_block.get("citations")
            if not isinstance(citations, list):
                return {
                    "status": "blocked",
                    "plan_id": source_plan_id,
                    "reason": "citation mapping repair target block has no structured citations",
                    "mutation_performed": False,
                }
            old_spans = target_occurrence.get("spans") or []
            old_span = old_spans[0] if old_spans and isinstance(old_spans[0], Mapping) else {}
            matches = [
                item
                for item in citations
                if isinstance(item, dict)
                and str(item.get("ref_id") or "") == citation_mapping.expected_ref_id
                and str(item.get("citation_token") or "")
                == f"[[cite_ref:{citation_mapping.expected_ref_id}]]"
                and int(item.get("span_start") or -1) == int(old_span.get("start_offset") or -2)
                and int(item.get("span_end") or -1) == int(old_span.get("end_offset") or -2)
            ]
            if len(matches) != 1:
                return {
                    "status": "blocked",
                    "plan_id": source_plan_id,
                    "reason": "citation block span does not uniquely match the approved occurrence",
                    "mutation_performed": False,
                }
            citation = matches[0]
            citation["ref_id"] = citation_mapping.replacement_ref_id
            citation["citation_token"] = f"[[cite_ref:{citation_mapping.replacement_ref_id}]]"
            citation["raw_text"] = citation["citation_token"]
            citation["paper_id"] = citation_mapping.replacement_paper_id
            citation["paper_key"] = citation_mapping.replacement_paper_id
            citation["canonical_paper_key"] = citation_mapping.replacement_paper_id
            block_text = str(patched_block.get("text") or "")
            cursor = 0
            for item in sorted(
                (value for value in citations if isinstance(value, dict)),
                key=lambda value: int(value.get("span_start") or 0),
            ):
                token = str(item.get("citation_token") or "")
                position = block_text.find(token, cursor) if token else -1
                if position < 0:
                    return {
                        "status": "blocked",
                        "plan_id": source_plan_id,
                        "reason": "citation metadata no longer matches the approved replacement text",
                        "mutation_performed": False,
                    }
                item["span_start"] = position
                item["span_end"] = position + len(token)
                item["raw_text"] = token
                cursor = position + len(token)
            patched_block["anchor_text"] = (
                block_text[:80] + ("..." if len(block_text) > 80 else "")
            )
            patched_block["anchor_hash"] = hashlib.sha256(block_text.encode("utf-8")).hexdigest()[:8]
            patched_block["span_map"] = {
                "segmenter_version": SENTENCE_SEGMENTER_VERSION,
                "sentences": [
                    item.to_dict(sentence_index=index)
                    for index, item in enumerate(segment_sentences(block_text), start=1)
                ],
            }

        tx_seed = {
            "manual_plan": manual_plan_id,
            "source_report_plan_hash": source_plan_record.content_hash,
            "closure_hash": closure.evidence_hash,
            "approval_id": approval.approval_id,
            "patched_draft_hash": _hash(patched_draft),
        }
        transaction_id = "repair-tx:" + _hash(tx_seed)[:24]
        tx_dir = Path(
            self.workspace.artifact_path(
                f"repair_transactions/{transaction_id.replace(':', '-') }"
            )
        )
        derived_draft_path = tx_dir / "review_draft_repaired.json"
        derived_manifest_path = tx_dir / "citation_manifest_repaired.json"
        try:
            manifest_model = build_citation_manifest_from_review_draft(
                job_id=self.workspace.job_id,
                project_name=self.workspace.project_name,
                manifest_id=f"repaired:{approval.approval_id}",
                review_draft_path=str(derived_draft_path),
                review_word_path=self.workspace.artifact_path("review.docx"),
                review_draft=patched_draft,
                paper_summaries=[dict(item) for item in summary_payload if isinstance(item, Mapping)],
                citation_ref_catalog=catalog_payload,
                citation_ref_catalog_path=catalog_record.path,
                citation_ref_catalog_hash=catalog_record.content_hash,
                render_policy=(
                    dict(citation_manifest.get("render_policy") or {})
                    if isinstance(citation_manifest.get("render_policy"), Mapping)
                    else None
                ),
            )
            patched_manifest = manifest_model.to_dict()
            patched_manifest.setdefault("repair_annotations", []).append(
                {
                    "approval_id": approval.approval_id,
                    "source_report_plan_id": source_plan_record.artifact_id,
                    "source_claim_id": approval.source_claim_id,
                    "block_id": block_id,
                    "occurrence_id": (
                        citation_mapping.occurrence_id if citation_mapping is not None else ""
                    ),
                    "source_evidence_ids": list(source_evidence_ids),
                    "approved_by": actor,
                    "reason": reason,
                    "original_block_hash": expected_anchor_hash,
                    "replacement_block_hash": hashlib.sha256(
                        replacement_text.encode("utf-8")
                    ).hexdigest(),
                    "citation_mapping": (
                        citation_mapping.to_dict() if citation_mapping is not None else None
                    ),
                    "created_at": approval.created_at,
                }
            )
        except (OSError, TypeError, ValueError, KeyError) as exc:
            return {
                "status": "blocked",
                "plan_id": source_plan_id,
                "reason": f"citation manifest rebuild failed: {exc}",
                "mutation_performed": False,
            }
        if citation_mapping is not None:
            repaired_occurrences = [
                item
                for item in patched_manifest.get("occurrences") or []
                if isinstance(item, Mapping)
                and str(item.get("block_id") or "") == block_id
                and str(item.get("ref_id") or "") == citation_mapping.replacement_ref_id
                and str(item.get("paper_id") or "") == citation_mapping.replacement_paper_id
            ]
            if len(repaired_occurrences) != 1:
                return {
                    "status": "blocked",
                    "plan_id": source_plan_id,
                    "reason": "rebuilt manifest does not preserve the approved occurrence-to-source correction",
                    "mutation_performed": False,
                }

        all_paper_artifacts = [
            payload for payload in paper_payload_by_key.values()
        ]
        citation_ref_catalog = catalog_payload
        targeted_revalidation = _targeted_revalidate(
            patched_draft,
            patched_manifest,
            all_paper_artifacts,
            citation_ref_catalog,
        )
        if not targeted_revalidation["passed"]:
            return {
                "status": "blocked",
                "plan_id": source_plan_id,
                "targeted_revalidation": targeted_revalidation,
                "reason": "manual correction failed citation and block structural checks",
                "mutation_performed": False,
            }
        semantic_revalidation = run_semantic_revalidation(
            patched_draft,
            patched_manifest,
            all_paper_artifacts,
            citation_ref_catalog=citation_ref_catalog,
        )
        structural_closure = RepairStructuralClosure.from_results(
            targeted_revalidation,
            semantic_revalidation.to_dict(),
            canonical_input_hashes={
                record.artifact_id: record.content_hash
                for record in (draft_record, manifest_record, validation_record)
            },
            derived_output_hashes={
                "review_draft_repaired": _hash(patched_draft),
                "citation_manifest_repaired": _hash(patched_manifest),
            },
        )
        if not structural_closure.passed:
            return {
                "status": "blocked",
                "plan_id": source_plan_id,
                "repair_structural_closure": structural_closure.to_dict(),
                "reason": "manual correction failed structural or semantic repair closure",
                "mutation_performed": False,
            }

        existing_transaction = self.registry.get(transaction_id)
        if (
            existing_transaction is not None
            and existing_transaction.status == "quarantined"
            and existing_transaction.artifact_type == REPAIR_TRANSACTION_ARTIFACT_TYPE
        ):
            existing_transaction_payload = _load_json(existing_transaction) or {}
            return {
                "status": "already_applied",
                "job_id": self.workspace.job_id,
                "plan_id": manual_plan_id,
                "source_report_plan_id": source_plan_record.artifact_id,
                "transaction_id": transaction_id,
                "applied_artifact_ids": list(existing_transaction_payload.get("applied_artifact_ids") or ()),
                "canonical_replacement": False,
                "mutation_performed": False,
                "idempotent_replay": True,
            }

        if manual_plan_record is None:
            dependencies = [source_plan_record, draft_record, manifest_record, validation_record]
            dependencies.extend(
                paper_record_by_key[key]
                for key in source_evidence_ids
                if key in paper_record_by_key
            )
            if catalog_record is not None:
                dependencies.append(catalog_record)
            if summary_record is not None:
                dependencies.append(summary_record)
            unique_dependencies = {
                record.artifact_id: ArtifactDependencyRefV2.from_record(record)
                for record in dependencies
            }
            manual_plan_path = Path(
                self.workspace.artifact_path(
                    f"repair_plans/{manual_plan_id.replace(':', '-')}.json"
                )
            )
            manual_plan_record = self._publish_json(
                manual_plan_path,
                manual_plan_payload,
                artifact_id=manual_plan_artifact_id,
                artifact_role="repair_plan",
                artifact_type="repair_plan",
                artifact_version="v1",
                producer="validation.repair_transaction.RepairTransactionService.apply_manual_proposal",
                status="ready",
                depends_on=list(unique_dependencies.values()),
                metadata={
                    "policy": RepairPolicy.REPORT_FIRST.value,
                    "manual_approval_id": approval.approval_id,
                    "source_report_plan_id": source_plan_record.artifact_id,
                    "closure_evidence_hash": closure.evidence_hash,
                },
            )
        else:
            self.registry.reload()
            manual_plan_record = self.registry.get(manual_plan_artifact_id)
            if manual_plan_record is None or manual_plan_record.status != "ready":
                return {
                    "status": "blocked",
                    "plan_id": source_plan_id,
                    "reason": "persisted manual approval plan is no longer a ready Registry artifact",
                    "mutation_performed": False,
                }

        tx_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_json(str(derived_draft_path), patched_draft)
        atomic_write_json(str(derived_manifest_path), patched_manifest)
        base_dependencies = [source_plan_record, manual_plan_record, draft_record, manifest_record, validation_record]
        base_dependency_refs = [ArtifactDependencyRefV2.from_record(item) for item in base_dependencies]
        derived_draft_record = self._publish_json(
            derived_draft_path,
            patched_draft,
            artifact_id=f"review_draft_repaired:{transaction_id}",
            artifact_role="review_draft_repaired",
            artifact_type="review_draft_repaired",
            artifact_version="v1",
            producer="validation.repair_transaction.RepairTransactionService.apply_manual_proposal",
            status="quarantined",
            depends_on=base_dependency_refs,
            metadata={
                "transaction_id": transaction_id,
                "canonical_replacement": False,
                "manual_approval_id": approval.approval_id,
                "source_report_plan_id": source_plan_record.artifact_id,
            },
        )
        derived_manifest_record = self._publish_json(
            derived_manifest_path,
            patched_manifest,
            artifact_id=f"citation_manifest_repaired:{transaction_id}",
            artifact_role="citation_manifest_repaired",
            artifact_type="citation_manifest_repaired",
            artifact_version="v1",
            producer="validation.repair_transaction.RepairTransactionService.apply_manual_proposal",
            status="quarantined",
            depends_on=[
                *base_dependency_refs,
                ArtifactDependencyRefV2.from_record(derived_draft_record),
            ],
            metadata={
                "transaction_id": transaction_id,
                "canonical_replacement": False,
                "manual_approval_id": approval.approval_id,
                "source_report_plan_id": source_plan_record.artifact_id,
            },
        )
        apply_payload.update(
            {
                "plan_id": manual_plan_id,
                "applied_count": 1,
                "rejected_count": int(apply_result.get("rejected_count") or 0),
                "patched_review_draft": patched_draft,
                "patched_citation_manifest": patched_manifest,
                "manual_approval": approval.to_dict(),
                "source_report_plan_id": source_plan_record.artifact_id,
                "targeted_revalidation": targeted_revalidation,
                "semantic_revalidation": semantic_revalidation.to_dict(),
                "repair_structural_closure": structural_closure.to_dict(),
            }
        )
        apply_result_path = tx_dir / "repair_apply_result.json"
        apply_record = self._publish_json(
            apply_result_path,
            apply_payload,
            artifact_id=f"repair_apply_result:{transaction_id}",
            artifact_role="repair_apply_result",
            artifact_type="repair_apply_result",
            artifact_version="v1",
            producer="validation.repair_transaction.RepairTransactionService.apply_manual_proposal",
            status="quarantined",
            depends_on=[
                *base_dependency_refs,
                ArtifactDependencyRefV2.from_record(derived_draft_record),
                ArtifactDependencyRefV2.from_record(derived_manifest_record),
            ],
            metadata={
                "transaction_id": transaction_id,
                "manual_approval_id": approval.approval_id,
                "canonical_replacement": False,
            },
        )
        previous_records = self._dependency_records()
        transaction = RepairTransactionRecord(
            transaction_id=transaction_id,
            job_id=self.workspace.job_id,
            status="quarantined",
            policy="manual_confirm",
            plan_id=manual_plan_record.artifact_id,
            validation_artifact_id=validation_record.artifact_id,
            previous_artifact_ids=tuple(item.artifact_id for item in previous_records),
            previous_artifact_hashes={item.artifact_id: item.content_hash for item in previous_records},
            applied_artifact_ids=(
                derived_draft_record.artifact_id,
                derived_manifest_record.artifact_id,
                apply_record.artifact_id,
            ),
            applied_patch_ids=(proposal.proposal_id,),
            created_at=approval.created_at,
            reason="explicit reviewer-approved correction was applied to quarantined derived inputs",
        )
        transaction_path = tx_dir / "repair_transaction.json"
        transaction_record = self._publish_json(
            transaction_path,
            transaction.to_dict(),
            artifact_id=transaction_id,
            artifact_role="repair_transaction",
            artifact_type=REPAIR_TRANSACTION_ARTIFACT_TYPE,
            artifact_version=REPAIR_TRANSACTION_ARTIFACT_VERSION,
            producer="validation.repair_transaction.RepairTransactionService.apply_manual_proposal",
            status="quarantined",
            depends_on=[
                ArtifactDependencyRefV2.from_record(manual_plan_record),
                ArtifactDependencyRefV2.from_record(source_plan_record),
                ArtifactDependencyRefV2.from_record(apply_record),
            ],
            metadata={
                "status": transaction.status,
                "policy": transaction.policy,
                "manual_approval_id": approval.approval_id,
            },
        )
        self.registry.reload()
        return {
            "status": "quarantined",
            "job_id": self.workspace.job_id,
            "plan_id": manual_plan_id,
            "manual_plan_artifact_id": manual_plan_record.artifact_id,
            "source_report_plan_id": source_plan_record.artifact_id,
            "transaction_id": transaction_record.artifact_id,
            "applied_artifact_ids": list(transaction.applied_artifact_ids),
            "applied_patch_ids": list(transaction.applied_patch_ids),
            "apply_result": apply_result,
            "approval_id": approval.approval_id,
            "canonical_replacement": False,
            "mutation_performed": True,
            "repair_structural_closure": structural_closure.to_dict(),
            "derived_hashes": {
                "review_draft": derived_draft_record.content_hash,
                "citation_manifest": derived_manifest_record.content_hash,
            },
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
        base_dependencies = [
            item
            for item in (plan_record, draft_record, manifest_record, validation_record)
            if item is not None
        ]
        dependency_payloads = [
            ArtifactDependencyRefV2.from_record(item)
            for item in base_dependencies
        ]
        derived_draft_record = self._publish_json(
            derived_draft_path,
            dict(patched_draft),
            artifact_id=f"review_draft_repaired:{transaction_id}",
            artifact_role="review_draft_repaired",
            artifact_type="review_draft_repaired",
            artifact_version="v1",
            producer="validation.repair_transaction.RepairTransactionService",
            status="quarantined",
            depends_on=dependency_payloads,
            metadata={
                "transaction_id": transaction_id,
                "canonical_replacement": False,
                "semantic_revalidation": semantic_revalidation.to_dict(),
            },
        )
        derived_manifest_record = self._publish_json(
            derived_manifest_path,
            dict(patched_manifest),
            artifact_id=f"citation_manifest_repaired:{transaction_id}",
            artifact_role="citation_manifest_repaired",
            artifact_type="citation_manifest_repaired",
            artifact_version="v1",
            producer="validation.repair_transaction.RepairTransactionService",
            status="quarantined",
            depends_on=dependency_payloads,
            metadata={
                "transaction_id": transaction_id,
                "canonical_replacement": False,
                "semantic_revalidation": semantic_revalidation.to_dict(),
            },
        )
        apply_record = self._publish_json(
            apply_result_path,
            apply_payload,
            artifact_id=f"repair_apply_result:{transaction_id}",
            artifact_role="repair_apply_result",
            artifact_type="repair_apply_result",
            artifact_version="v1",
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
        transaction_record = self._publish_json(
            transaction_path,
            transaction.to_dict(),
            artifact_id=transaction_id,
            artifact_role="repair_transaction",
            artifact_type=REPAIR_TRANSACTION_ARTIFACT_TYPE,
            artifact_version=REPAIR_TRANSACTION_ARTIFACT_VERSION,
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
        expected_versioned_draft_id = f"review_draft:v3:repair:{source_hash_prefix}"
        expected_versioned_manifest_id = f"citation_manifest:v3:repair:{source_hash_prefix}"
        input_artifacts = revalidation_model.input_artifacts
        if (
            input_artifacts.review_draft_id != expected_versioned_draft_id
            or input_artifacts.citation_manifest_id != expected_versioned_manifest_id
            or not input_artifacts.review_draft_hash
            or not input_artifacts.citation_manifest_hash
        ):
            return {
                "status": "blocked",
                "reason": "revalidation result is bound to different artifacts; validation bytes cannot be rebound",
                "transaction_id": transaction_id,
                "mutation_performed": False,
            }
        validated_draft_record = self.registry.get(input_artifacts.review_draft_id)
        validated_manifest_record = self.registry.get(input_artifacts.citation_manifest_id)
        if (
            validated_draft_record is None
            or validated_manifest_record is None
            or validated_draft_record.artifact_type != "review_draft"
            or validated_manifest_record.artifact_type != "citation_manifest"
            or validated_draft_record.artifact_version != "v3"
            or validated_manifest_record.artifact_version != "v3"
            or validated_draft_record.content_hash != input_artifacts.review_draft_hash
            or validated_manifest_record.content_hash != input_artifacts.citation_manifest_hash
            or validated_draft_record.content_hash != (derived_draft_record.content_hash if derived_draft_record else "")
            or validated_manifest_record.content_hash != (derived_manifest_record.content_hash if derived_manifest_record else "")
        ):
            return {
                "status": "blocked",
                "reason": "revalidation input artifacts are not the exact registered repair bytes",
                "transaction_id": transaction_id,
                "mutation_performed": False,
            }
        try:
            validated_draft_hash = file_sha256(validated_draft_record.path)
            validated_manifest_hash = file_sha256(validated_manifest_record.path)
        except OSError as exc:
            return {
                "status": "blocked",
                "reason": f"revalidation input artifact cannot be read: {exc}",
                "transaction_id": transaction_id,
                "mutation_performed": False,
            }
        if (
            validated_draft_hash != input_artifacts.review_draft_hash
            or validated_manifest_hash != input_artifacts.citation_manifest_hash
        ):
            return {
                "status": "blocked",
                "reason": "revalidation input artifact bytes changed after validation",
                "transaction_id": transaction_id,
                "mutation_performed": False,
            }
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
        versioned_draft_id = expected_versioned_draft_id
        versioned_manifest_id = expected_versioned_manifest_id
        versioned_docx_id = f"review_docx:v1:repair:{versioned_suffix}"
        promotion_dir = Path(
            self.workspace.artifact_path(
                f"repair_promotions/{promotion_id.replace(':', '-') }"
            )
        )
        promotion_dir.mkdir(parents=True, exist_ok=True)
        # The draft and manifest bytes below are the exact files which the
        # current validation service consumed.  Promotion may register these
        # identities as ready, but it must not rewrite their JSON or rebind a
        # validation result to post-hoc metadata.
        promoted_draft_path = Path(validated_draft_record.path)
        promoted_manifest_path = Path(validated_manifest_record.path)
        promoted_docx_target_path = promotion_dir / "review.docx"

        promoted_draft = _load_json(validated_draft_record)
        promoted_manifest = _load_json(validated_manifest_record)
        if promoted_draft is None or promoted_manifest is None:
            return {
                "status": "blocked",
                "reason": "validated repair bytes are not readable JSON",
                "transaction_id": transaction_id,
                "repair_structural_closure": structural.to_dict(),
                "mutation_performed": False,
            }

        temp_docx_fd, temp_docx_name = tempfile.mkstemp(
            prefix="repair-review-",
            suffix=".docx",
        )
        os.close(temp_docx_fd)
        try:
            from docx_writer import rebuild_review_docx_from_structured_artifacts

            rebuild_review_docx_from_structured_artifacts(
                SimpleNamespace(logger=None),
                promoted_draft,
                promoted_manifest,
                temp_docx_name,
            )
            promoted_docx_bytes = Path(temp_docx_name).read_bytes()
        except (OSError, TypeError, ValueError, KeyError, RuntimeError) as exc:
            return {
                "status": "blocked",
                "reason": f"versioned repair output build failed: {exc}",
                "transaction_id": transaction_id,
                "repair_structural_closure": structural.to_dict(),
                "mutation_performed": False,
            }
        finally:
            try:
                Path(temp_docx_name).unlink(missing_ok=True)
            except OSError:
                pass

        promoted_validation_id = f"validation_run_result:v1:repair:{versioned_suffix}"
        promoted_validation_path = Path(registered_revalidation.path)
        if file_sha256(promoted_validation_path) != registered_revalidation.content_hash:
            return {
                "status": "blocked",
                "reason": "durable validation result bytes changed before promotion",
                "transaction_id": transaction_id,
                "repair_structural_closure": structural.to_dict(),
                "mutation_performed": False,
            }

        closure_record_id = str(
            validation_result.get("provider_receipt_closure_record_id")
            or revalidation_payload.get("provider_receipt_closure_record_id")
            or ""
        ).strip()
        closure_record = self.registry.get(closure_record_id) if closure_record_id else None
        if (
            closure_record is None
            or closure_record.artifact_type != "provider_receipt_closure"
            or closure_record.status != "ready"
            or not closure_record.content_hash
            or file_sha256(closure_record.path) != closure_record.content_hash
        ):
            return {
                "status": "blocked",
                "reason": "provider receipt closure record is not a verified ready artifact",
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
        promoted_docx_record = self._publish_bytes(
            promoted_docx_target_path,
            promoted_docx_bytes,
            artifact_id=versioned_docx_id,
            artifact_role="repair_promotion_review_docx",
            artifact_type="review_docx",
            artifact_version="v1",
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
        for evidence_id in revalidation_payload.get("input_artifacts", {}).get(
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
                ArtifactDependencyRefV2.from_record(closure_record),
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
            closure_record,
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
        audit_record = self._publish_json(
            audit_path,
            audit.to_dict(),
            artifact_id=audit_id,
            artifact_role="repair_promotion_audit",
            artifact_type="audit_record",
            artifact_version="v1",
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
        lineage_record = self._publish_json(
            lineage_path,
            lineage_payload,
            artifact_id=lineage_id,
            artifact_role="repair_lineage",
            artifact_type="repair_lineage",
            artifact_version="v1",
            producer="validation.repair_transaction.RepairTransactionService.promote_transaction",
            depends_on=[
                ArtifactDependencyRefV2.from_record(audit_record),
                *(ArtifactDependencyRefV2.from_record(item) for item in output_records),
            ],
        )

        try:
            previous_set = self.registry.resolve_current_artifact_set()
        except Exception:
            if self.registry.current_artifact_set_pointer() is not None:
                return {
                    "status": "blocked",
                    "reason": "existing CurrentArtifactSet is untrusted; promotion is fail-closed",
                    "transaction_id": transaction_id,
                    "repair_structural_closure": structural.to_dict(),
                    "mutation_performed": False,
                }
            previous_set = None
        promotion = RepairPromotionTransaction(
            transaction_id=promotion_id,
            job_id=self.workspace.job_id,
            source_transaction_id=transaction_id,
            status="prepared",
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
            current_pointer_artifact_ids={},
            current_artifact_set_id="",
        )
        promotion_path = promotion_dir / "repair_promotion_transaction.json"
        promotion_record = self._publish_json(
            promotion_path,
            promotion.to_dict(),
            artifact_id=promotion_id,
            artifact_role="repair_promotion_transaction",
            artifact_type=promotion.artifact_type,
            artifact_version=promotion.artifact_version,
            producer="validation.repair_transaction.RepairTransactionService.promote_transaction",
            status="ready",
            depends_on=[
                ArtifactDependencyRefV2.from_record(audit_record),
                ArtifactDependencyRefV2.from_record(lineage_record),
                *(ArtifactDependencyRefV2.from_record(item) for item in output_records),
            ],
            metadata={
                "status": promotion.status,
                "promotion_state": "prepared",
                "commit_boundary": "current_artifact_set_pointer_cas",
                "canonical_replacement": True,
                "quarantined_export": False,
            },
        )
        current_set = self.registry.build_current_artifact_set(
            promotion_transaction_id=promotion_id,
            promotion_transaction_hash=promotion_record.content_hash,
            review_draft_artifact_id=promoted_draft_record.artifact_id,
            review_draft_artifact_hash=promoted_draft_record.content_hash,
            citation_manifest_artifact_id=promoted_manifest_record.artifact_id,
            citation_manifest_artifact_hash=promoted_manifest_record.content_hash,
            review_docx_artifact_id=promoted_docx_record.artifact_id,
            review_docx_artifact_hash=promoted_docx_record.content_hash,
            validation_run_result_artifact_id=promoted_validation_record.artifact_id,
            validation_run_result_artifact_hash=promoted_validation_record.content_hash,
            validation_receipt_closure_artifact_id=closure_record.artifact_id,
            validation_receipt_closure_artifact_hash=closure_record.content_hash,
            actor=actor,
            reason=reason,
            previous_set_id=previous_set.set_id if previous_set is not None else "",
        )
        current_set_pointer_record = self.registry.switch_current_artifact_set(
            current_set,
            prepared_promotion_record=promotion_record,
        )
        pointer_ids = {
            "current_artifact_set": current_set_pointer_record.artifact_id,
            "current_artifact_set_id": current_set.set_id,
        }

        registered_promotion = self.registry.get(promotion_id)
        if registered_promotion is None or registered_promotion.content_hash != promotion_record.content_hash:
            raise RuntimeError("prepared promotion transaction was not committed with current artifact set")
        return {
            "status": "promoted",
            "job_id": self.workspace.job_id,
            "transaction_id": transaction_id,
            "promotion_transaction_id": promotion_id,
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
