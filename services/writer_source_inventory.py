"""Registry-verified canonical source inventory for Writer task admission.

The raw section packet is a candidate projection, not an authority for source
IDs. This module loads the persisted Outline v3 content-layer artifact,
verifies its Registry closure and hashes, and exposes immutable source
membership indexes for the Writer task-scope join.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from outline.semantic_chunking import derive_interpretation_dependencies
from outline.v3_artifacts import OutlineArtifact
from outline.v3_models import (
    InterpretationDependency,
    EvidenceClaim,
    OutlineEvidenceViews,
    PaperContentLayers,
    PaperEvidenceDossier,
    OutlineEvidenceView,
    ResearchUnit,
    SourceFieldLedgerEntry,
    compute_v3_hash,
)
from runtime.provider_runtime import hash_json
from services.artifact_registry import ArtifactRecord, ArtifactRegistry


SOURCE_INVENTORY_ARTIFACT_ID = "outline-v3:outline_content_layers"
SOURCE_INVENTORY_NODE_ID = "outline_content_layers"
SOURCE_INVENTORY_ARTIFACT_TYPE = "outline_artifact"
SOURCE_INVENTORY_ARTIFACT_VERSION = "v3"
SOURCE_INVENTORY_PAYLOAD_TYPE = "outline_content_layers"
SOURCE_INVENTORY_PAYLOAD_VERSION = "v3"
SOURCE_INVENTORY_PRODUCER = "outline.v3_executor.OutlineV3Executor"
SOURCE_INVENTORY_ROLE = "outline_v3_node_output"

_OUTER_KEYS = {
    "artifact_type", "artifact_version", "job_id", "dependency_hashes", "payload",
    "blocking_diagnostics", "status", "content_hash",
}
_LAYERS_KEYS = {
    "artifact_type", "artifact_version", "index_cards", "dossiers",
    "source_summary_hashes", "blocking_diagnostics", "status", "content_hash",
}
_EVIDENCE_VIEWS_KEYS = {
    "artifact_type", "artifact_version", "created_from_job_id", "views", "source_summary_hashes",
    "alias_crosswalk", "blocking_diagnostics", "shard_id", "shard_count", "status", "content_hash",
}
_EVIDENCE_VIEW_KEYS = {
    "paper_key", "canonical_paper_key", "title", "authors", "year", "paper_type",
    "research_questions", "theories", "constructs", "mechanisms", "method", "sample_or_context",
    "findings", "conclusions", "limitations", "research_gaps", "future_directions", "relevance",
    "source_summary_hash", "doi", "source_paper_id", "aliases", "identity_source",
    "source_summary_hashes", "source_fields", "classification", "must_use", "diagnostics",
    "source_field_ledger",
}
_INDEX_CARD_KEYS = {
    "paper_id", "research_questions", "key_constructs", "core_findings", "key_boundaries",
    "method_category", "topic_tags", "theories", "mechanisms", "evidence_package_id",
    "source_summary_hash", "source_locators",
}
_DOSSIER_KEYS = {
    "dossier_id", "paper_id", "source_summary_hash", "overall_context", "research_questions",
    "concept_definitions", "operationalizations", "theoretical_derivation", "findings",
    "research_units", "claims", "mechanism_evidence", "moderators_boundaries", "zero_results",
    "limitations", "source_locators", "evidence_ids_by_field", "evidence_text_by_id", "diagnostics",
    "status", "source_field_ledger", "evidence_ids", "content_hash",
}
_UNIT_KEYS = {
    "study_id", "parent_paper_id", "research_questions", "definitions_and_operationalizations",
    "theoretical_derivation", "method", "sample_or_context", "findings", "mechanisms",
    "moderators_or_boundaries", "zero_results", "limitations", "claims", "source_locators",
    "evidence_ids", "source_summary_hash", "source_study_id", "source_field_ids",
    "interpretation_dependencies",
}
_CLAIM_KEYS = {
    "claim_id", "claim_type", "text", "study_id", "evidence_ids", "source_locator",
    "source_summary_hash",
}
_FIELD_KEYS = {
    "source_field_id", "source_path", "source_value", "disposition", "canonical_field", "scope",
    "study_id", "interpretation_required", "source_summary_hash", "derived_value",
}
_DEPENDENCY_KEYS = {
    "primary_claim_id", "required_source_claim_ids", "required_evidence_ids",
    "required_source_field_ids", "scope", "study_id", "reason",
}
_HEX64 = re.compile(r"[0-9a-f]{64}")
_VERIFIED_SEAL = object()


class WriterSourceInventoryError(ValueError):
    """The canonical Writer source inventory is absent, stale, or malformed."""


@dataclass(frozen=True)
class VerifiedWriterSourceClaimV1:
    claim_id: str
    paper_key: str
    owner_study_ids: tuple[str, ...]
    claim_type: str
    text: str
    evidence_ids: tuple[str, ...]
    source_locator: str
    source_summary_hash: str


@dataclass(frozen=True)
class VerifiedWriterSourceEvidenceV1:
    evidence_id: str
    paper_key: str
    owner_study_ids: tuple[str, ...]
    text: str | None


@dataclass(frozen=True)
class VerifiedWriterSourceFieldV1:
    source_field_id: str
    paper_key: str
    owner_study_ids: tuple[str, ...]
    source_path: str
    source_value: str
    disposition: str
    canonical_field: str
    scope: str
    source_study_id: str
    interpretation_required: bool
    source_summary_hash: str
    derived_value: str


@dataclass(frozen=True)
class VerifiedWriterInterpretationDependencyV1:
    paper_key: str
    owner_study_id: str
    primary_claim_id: str
    required_source_claim_ids: tuple[str, ...]
    required_evidence_ids: tuple[str, ...]
    required_source_field_ids: tuple[str, ...]
    scope: str
    study_id: str
    reason: str


@dataclass(frozen=True)
class VerifiedWriterSourceUnitV1:
    paper_key: str
    study_id: str
    source_study_id: str
    claim_ids: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    source_field_ids: tuple[str, ...]
    canonical_json: str


@dataclass(frozen=True)
class VerifiedWriterSourcePaperV1:
    paper_key: str
    dossier_id: str
    source_summary_hash: str
    source_summary_hashes: tuple[str, ...]
    dossier_content_hash: str
    evidence_view_hash: str
    dossier_status: str
    diagnostics: tuple[str, ...]
    claims: tuple[VerifiedWriterSourceClaimV1, ...]
    evidence: tuple[VerifiedWriterSourceEvidenceV1, ...]
    source_fields: tuple[VerifiedWriterSourceFieldV1, ...]
    units: tuple[VerifiedWriterSourceUnitV1, ...]
    interpretation_dependencies: tuple[VerifiedWriterInterpretationDependencyV1, ...]


@dataclass(frozen=True)
class VerifiedWriterSourceInventoryV1:
    """Immutable indexes derived only from a reverified Registry artifact."""

    registry_job_id: str
    artifact_id: str
    artifact_hash: str
    content_hash: str
    papers: tuple[VerifiedWriterSourcePaperV1, ...]
    _seal: object = field(default=None, repr=False, compare=False)

    @property
    def is_verified(self) -> bool:
        return self._seal is _VERIFIED_SEAL

    def paper_by_key(self) -> dict[str, VerifiedWriterSourcePaperV1]:
        return {paper.paper_key: paper for paper in self.papers}


def _require_schema(value: Any, keys: set[str], label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != keys:
        raise WriterSourceInventoryError(f"{label} schema does not match the supported v3 contract")
    return value


def _require_rows(value: Any, label: str) -> list[Mapping[str, Any]]:
    if not isinstance(value, list) or any(not isinstance(item, Mapping) for item in value):
        raise WriterSourceInventoryError(f"{label} must be an array of objects")
    return list(value)


def _string_list(value: Any, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise WriterSourceInventoryError(f"{label} must be an array of strings")
    return tuple(value)


def _require_string(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise WriterSourceInventoryError(f"{label} must be a string")
    return value


def _require_string_lists(value: Any, label: str) -> None:
    if not isinstance(value, Mapping):
        raise WriterSourceInventoryError(f"{label} must be an object")
    for key, items in value.items():
        _require_string(key, f"{label} key")
        _string_list(items, f"{label}.{key}")


def _read_outline_artifact(record: ArtifactRecord, expected_node: str, *, registry_job_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    if (
        record.status != "ready"
        or record.job_id != registry_job_id
        or record.artifact_type != SOURCE_INVENTORY_ARTIFACT_TYPE
        or record.artifact_version != SOURCE_INVENTORY_ARTIFACT_VERSION
        or record.artifact_role != SOURCE_INVENTORY_ROLE
        or record.producer != SOURCE_INVENTORY_PRODUCER
        or str(record.metadata.get("node_id") or "") != expected_node
        or str(record.metadata.get("job_id") or "") != registry_job_id
    ):
        raise WriterSourceInventoryError(f"{record.artifact_id} is not a ready current Outline v3 node artifact")
    try:
        raw_bytes = Path(record.path).read_bytes()
        if hashlib.sha256(raw_bytes).hexdigest() != record.content_hash:
            raise WriterSourceInventoryError(f"{record.artifact_id} durable bytes differ from its Registry hash")
        envelope = json.loads(raw_bytes.decode("utf-8"))
    except WriterSourceInventoryError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise WriterSourceInventoryError(f"{record.artifact_id} cannot be read as a JSON artifact") from exc
    outer = _require_schema(envelope, _OUTER_KEYS, f"{record.artifact_id} outer artifact")
    if (
        outer.get("artifact_type") != SOURCE_INVENTORY_ARTIFACT_TYPE
        or outer.get("artifact_version") != SOURCE_INVENTORY_ARTIFACT_VERSION
        or outer.get("job_id") != registry_job_id
        or outer.get("status") != "ready"
        or outer.get("blocking_diagnostics") != []
        or not isinstance(outer.get("dependency_hashes"), Mapping)
        or not _HEX64.fullmatch(str(outer.get("content_hash") or ""))
    ):
        raise WriterSourceInventoryError(f"{record.artifact_id} outer envelope is not ready or valid")
    artifact = OutlineArtifact.from_dict(outer)
    if artifact.content_hash != outer.get("content_hash"):
        raise WriterSourceInventoryError(f"{record.artifact_id} outer payload hash is invalid")
    if str(record.metadata.get("content_hash") or "") != artifact.content_hash:
        raise WriterSourceInventoryError(f"{record.artifact_id} Registry metadata hash is stale")
    return dict(outer), dict(artifact.payload)


def _verified_stage1_source_hashes(
    registry: ArtifactRegistry,
    evidence_outer: Mapping[str, Any],
    *,
    external_registry_resolver: Callable[[str], ArtifactRegistry | None] | None,
) -> set[str]:
    pointer = registry.get("stage1_summaries")
    if pointer is None or (
        pointer.status != "ready"
        or pointer.job_id != registry.job_id
        or pointer.artifact_type != "stage1_canonical_summaries"
        or pointer.artifact_version != "v1"
        or pointer.artifact_role != "stage1_input"
        or pointer.producer != SOURCE_INVENTORY_PRODUCER
        or pointer.metadata.get("pointer_role") != "current"
    ):
        raise WriterSourceInventoryError("current canonical Stage 1 summary pointer is missing or invalid")
    immutable_id = str(pointer.metadata.get("current_version_artifact_id") or "")
    immutable_refs = [ref for ref in pointer.depends_on if ref.artifact_id == immutable_id]
    if (
        not immutable_id.startswith("outline-v3:stage1-summaries:")
        or len(pointer.depends_on) != 1
        or len(immutable_refs) != 1
    ):
        raise WriterSourceInventoryError("Stage 1 summary pointer is not bound to one immutable source artifact")
    immutable = registry.get(immutable_id)
    if (
        immutable is None
        or immutable.status != "ready"
        or immutable.artifact_type != "stage1_canonical_summaries"
        or immutable.artifact_version != "v1"
        or immutable.artifact_role != "stage1_input"
        or immutable.producer != SOURCE_INVENTORY_PRODUCER
        or immutable.metadata.get("immutable") is not True
        or str(immutable.metadata.get("versioned_artifact_id") or "") != immutable_id
        or immutable_refs[0].content_hash != immutable.content_hash
    ):
        raise WriterSourceInventoryError("immutable canonical Stage 1 summary record is invalid")
    try:
        verified_pointer = registry.verify_ready_artifact_closure(
            pointer,
            external_registry_resolver=external_registry_resolver,
        )
    except Exception as exc:
        raise WriterSourceInventoryError("canonical Stage 1 summary closure is not ready") from exc
    try:
        raw_bytes = Path(verified_pointer.path).read_bytes()
        if hashlib.sha256(raw_bytes).hexdigest() != verified_pointer.content_hash:
            raise WriterSourceInventoryError("current Stage 1 summary bytes differ from their Registry hash")
        payload = json.loads(raw_bytes.decode("utf-8"))
    except WriterSourceInventoryError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise WriterSourceInventoryError("current Stage 1 summary artifact cannot be read") from exc
    expected_keys = {"artifact_type", "artifact_version", "job_id", "summary_set_hash", "summaries"}
    if (
        not isinstance(payload, Mapping)
        or set(payload) != expected_keys
        or payload.get("artifact_type") != "stage1_canonical_summaries"
        or payload.get("artifact_version") != "v1"
        or payload.get("job_id") != registry.job_id
        or not isinstance(payload.get("summaries"), list)
        or not payload["summaries"]
        or any(not isinstance(item, Mapping) for item in payload["summaries"])
        or payload.get("summary_set_hash") != hash_json(payload["summaries"])
        or pointer.metadata.get("summary_set_hash") != payload.get("summary_set_hash")
        or immutable.metadata.get("summary_set_hash") != payload.get("summary_set_hash")
    ):
        raise WriterSourceInventoryError("current Stage 1 canonical summary schema or hash is invalid")
    if set(evidence_outer.get("dependency_hashes", {})) != {"stage1_summaries"}:
        raise WriterSourceInventoryError("evidence views have unexpected Stage 1 dependencies")
    if evidence_outer.get("dependency_hashes", {}).get("stage1_summaries") != verified_pointer.content_hash:
        raise WriterSourceInventoryError("evidence views are stale relative to the current Stage 1 source pointer")
    return {compute_v3_hash(item) for item in payload["summaries"]}


def _raw_dossier_to_paper(
    raw_dossier: Mapping[str, Any],
    dossier: PaperEvidenceDossier,
    allowed_source_hashes: set[str],
) -> VerifiedWriterSourcePaperV1:
    paper_key = dossier.paper_id
    if not paper_key or dossier.dossier_id != f"dossier:{paper_key}" or not dossier.source_summary_hash:
        raise WriterSourceInventoryError("canonical dossier identity or source summary hash is missing")
    if dossier.source_summary_hash not in allowed_source_hashes:
        raise WriterSourceInventoryError(f"dossier {paper_key} source hash is stale relative to its evidence view")
    if raw_dossier.get("content_hash") != dossier.content_hash:
        raise WriterSourceInventoryError(f"dossier {paper_key} content hash is invalid")
    if raw_dossier.get("evidence_ids") != dossier.evidence_ids:
        raise WriterSourceInventoryError(f"dossier {paper_key} evidence ID projection is invalid")
    if dossier.status not in {"ready", "partial", "blocked"}:
        raise WriterSourceInventoryError(f"dossier {paper_key} has an unknown readiness status")

    unit_by_id: dict[str, ResearchUnit] = {}
    for unit in dossier.research_units:
        if (
            not unit.study_id
            or unit.parent_paper_id != paper_key
            or unit.source_summary_hash not in ({""} | allowed_source_hashes)
            or unit.study_id in unit_by_id
        ):
            raise WriterSourceInventoryError(f"dossier {paper_key} has an invalid or duplicate research unit")
        unit_by_id[unit.study_id] = unit

    source_fields: dict[str, SourceFieldLedgerEntry] = {}
    field_owner: dict[str, tuple[str, ...]] = {}
    raw_fields = _require_rows(raw_dossier.get("source_field_ledger"), f"dossier {paper_key} source_field_ledger")
    source_markers: dict[str, list[str]] = defaultdict(list)
    for unit in unit_by_id.values():
        if unit.source_study_id:
            source_markers[unit.source_study_id].append(unit.study_id)
    for raw_field in raw_fields:
        _require_schema(raw_field, _FIELD_KEYS, f"dossier {paper_key} source field")
    for entry in dossier.source_field_ledger:
        if not entry.source_field_id or entry.source_field_id in source_fields:
            prior = source_fields.get(entry.source_field_id)
            if prior is None or prior.to_dict() != entry.to_dict():
                raise WriterSourceInventoryError(f"dossier {paper_key} has conflicting source field IDs")
            continue
        if entry.source_summary_hash not in ({""} | allowed_source_hashes):
            raise WriterSourceInventoryError(f"source field {entry.source_field_id} has a foreign summary hash")
        if entry.scope == "explicit_study":
            owners = source_markers.get(entry.study_id, [])
            if len(owners) != 1:
                raise WriterSourceInventoryError(f"source field {entry.source_field_id} has unresolved study ownership")
            field_owner[entry.source_field_id] = (owners[0],)
        elif entry.scope == "paper":
            field_owner[entry.source_field_id] = ("",)
        else:
            field_owner[entry.source_field_id] = ()
        source_fields[entry.source_field_id] = entry

    claim_rows: dict[str, EvidenceClaim] = {}
    claim_owners: dict[str, set[str]] = defaultdict(set)
    claim_sources: list[tuple[EvidenceClaim, str]] = []
    for claim in dossier.claims:
        claim_sources.append((claim, claim.study_id))
    for unit in unit_by_id.values():
        owner = unit.study_id if unit.source_study_id else ""
        for claim in unit.claims:
            if claim.study_id and owner and claim.study_id != owner:
                raise WriterSourceInventoryError(f"source claim {claim.claim_id} has a cross-study owner")
            claim_sources.append((claim, claim.study_id or owner))
    for claim, owner in claim_sources:
        if not claim.claim_id or claim.source_summary_hash not in ({""} | allowed_source_hashes):
            raise WriterSourceInventoryError(f"dossier {paper_key} has an invalid source claim")
        prior = claim_rows.get(claim.claim_id)
        if prior is not None and prior.to_dict() != claim.to_dict():
            raise WriterSourceInventoryError(f"source claim ID {claim.claim_id} has conflicting contents")
        claim_rows[claim.claim_id] = claim
        claim_owners[claim.claim_id].add(owner)
    for claim_id, owners in claim_owners.items():
        nonempty = {owner for owner in owners if owner}
        if len(nonempty) > 1:
            raise WriterSourceInventoryError(f"source claim ID {claim_id} crosses research-unit ownership")
        if nonempty:
            owners.discard("")

    evidence_ids: set[str] = set()
    evidence_ids.update(value for values in dossier.evidence_ids_by_field.values() for value in values)
    evidence_ids.update(dossier.evidence_text_by_id)
    evidence_owners: dict[str, set[str]] = defaultdict(set)
    for field_name, values in dossier.evidence_ids_by_field.items():
        owner = next((unit.study_id for unit in unit_by_id.values() if field_name.startswith(unit.study_id + ":")), "")
        for evidence_id in values:
            evidence_owners[evidence_id].add(owner)
    for unit in unit_by_id.values():
        owner = unit.study_id if unit.source_study_id else ""
        for evidence_id in unit.evidence_ids:
            evidence_ids.add(evidence_id)
            evidence_owners[evidence_id].add(owner)
    for claim_id, claim in claim_rows.items():
        for evidence_id in claim.evidence_ids:
            evidence_ids.add(evidence_id)
            evidence_owners[evidence_id].update(claim_owners[claim_id])
    for evidence_id in evidence_ids:
        evidence_owners.setdefault(evidence_id, {""})
    for evidence_id, owners in evidence_owners.items():
        nonempty = {owner for owner in owners if owner}
        if len(nonempty) > 1:
            raise WriterSourceInventoryError(f"evidence ID {evidence_id} crosses research-unit ownership")
        if nonempty:
            owners.discard("")

    dependencies: dict[str, VerifiedWriterInterpretationDependencyV1] = {}
    unit_rows: list[VerifiedWriterSourceUnitV1] = []
    available_fields = set(source_fields)
    for unit in unit_by_id.values():
        owner = unit.study_id if unit.source_study_id else ""
        unit_claim_ids = tuple(sorted({claim.claim_id for claim in unit.claims}))
        unit_evidence_ids = tuple(sorted(set(unit.evidence_ids)))
        unit_field_ids = tuple(sorted(set(unit.source_field_ids)))
        unit_claim_evidence = {
            evidence_id for claim in unit.claims for evidence_id in claim.evidence_ids
        }
        available_unit_evidence = set(unit_evidence_ids) | unit_claim_evidence
        declared = list(unit.interpretation_dependencies)
        derived = derive_interpretation_dependencies(unit, dossier.source_field_ledger)
        combined: dict[str, InterpretationDependency] = {}
        for dependency in (*declared, *derived):
            if dependency.primary_claim_id and dependency.primary_claim_id not in unit_claim_ids:
                raise WriterSourceInventoryError(f"dependency primary claim is absent from unit {unit.study_id}")
            if not set(dependency.required_source_claim_ids).issubset(unit_claim_ids):
                raise WriterSourceInventoryError(f"dependency qualifier claim is absent from unit {unit.study_id}")
            if not set(dependency.required_evidence_ids).issubset(available_unit_evidence):
                raise WriterSourceInventoryError(f"dependency evidence is absent from research unit {unit.study_id}")
            if not set(dependency.required_source_field_ids).issubset(available_fields):
                raise WriterSourceInventoryError(f"dependency field is absent from dossier {paper_key}")
            if dependency.scope == "explicit_study" and dependency.study_id != unit.study_id:
                raise WriterSourceInventoryError(f"dependency crosses research unit {unit.study_id}")
            if dependency.scope == "explicit_study" and not set(dependency.required_source_field_ids).issubset(unit_field_ids):
                raise WriterSourceInventoryError(f"dependency field is absent from research unit {unit.study_id}")
            for field_id in dependency.required_source_field_ids:
                source_field = source_fields.get(field_id)
                owners = field_owner.get(field_id, ())
                if source_field is None:
                    raise WriterSourceInventoryError(f"dependency field is absent from dossier {paper_key}")
                if dependency.scope == "explicit_study" and (
                    source_field.scope != "explicit_study" or unit.study_id not in owners
                ):
                    raise WriterSourceInventoryError(f"dependency field has wrong study ownership in {unit.study_id}")
                if dependency.scope == "paper" and source_field.scope == "explicit_study":
                    raise WriterSourceInventoryError(f"paper dependency uses a study-only field in {unit.study_id}")
            key = compute_v3_hash({"owner_study_id": owner, **dependency.to_dict()})
            combined[key] = dependency
        verified_deps: list[VerifiedWriterInterpretationDependencyV1] = []
        for dependency in sorted(combined.values(), key=lambda item: (item.primary_claim_id, item.scope, item.reason)):
            item = VerifiedWriterInterpretationDependencyV1(
                paper_key=paper_key,
                owner_study_id=owner,
                primary_claim_id=dependency.primary_claim_id,
                required_source_claim_ids=tuple(dependency.required_source_claim_ids),
                required_evidence_ids=tuple(dependency.required_evidence_ids),
                required_source_field_ids=tuple(dependency.required_source_field_ids),
                scope=dependency.scope,
                study_id=dependency.study_id,
                reason=dependency.reason,
            )
            verified_deps.append(item)
            dep_key = compute_v3_hash(item.__dict__)
            dependencies[dep_key] = item
        unit_rows.append(VerifiedWriterSourceUnitV1(
            paper_key=paper_key,
            study_id=unit.study_id,
            source_study_id=unit.source_study_id,
            claim_ids=unit_claim_ids,
            evidence_ids=unit_evidence_ids,
            source_field_ids=unit_field_ids,
            canonical_json=json.dumps(unit.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        ))

    claims = tuple(
        VerifiedWriterSourceClaimV1(
            claim_id=claim.claim_id,
            paper_key=paper_key,
            owner_study_ids=tuple(sorted(claim_owners[claim_id])),
            claim_type=claim.claim_type,
            text=claim.text,
            evidence_ids=tuple(claim.evidence_ids),
            source_locator=claim.source_locator,
            source_summary_hash=claim.source_summary_hash,
        )
        for claim_id, claim in sorted(claim_rows.items())
    )
    evidence = tuple(
        VerifiedWriterSourceEvidenceV1(
            evidence_id=evidence_id,
            paper_key=paper_key,
            owner_study_ids=tuple(sorted(evidence_owners[evidence_id])),
            text=dossier.evidence_text_by_id.get(evidence_id),
        )
        for evidence_id in sorted(evidence_ids)
    )
    fields = tuple(
        VerifiedWriterSourceFieldV1(
            source_field_id=field_id,
            paper_key=paper_key,
            owner_study_ids=field_owner[field_id],
            source_path=entry.source_path,
            source_value=entry.source_value,
            disposition=entry.disposition,
            canonical_field=entry.canonical_field,
            scope=entry.scope,
            source_study_id=entry.study_id,
            interpretation_required=entry.interpretation_required,
            source_summary_hash=entry.source_summary_hash,
            derived_value=entry.derived_value,
        )
        for field_id, entry in sorted(source_fields.items())
    )
    return VerifiedWriterSourcePaperV1(
        paper_key=paper_key,
        dossier_id=dossier.dossier_id,
        source_summary_hash=dossier.source_summary_hash,
        source_summary_hashes=tuple(sorted(allowed_source_hashes)),
        dossier_content_hash=dossier.content_hash,
        evidence_view_hash="",
        dossier_status=dossier.status,
        diagnostics=tuple(dossier.diagnostics),
        claims=claims,
        evidence=evidence,
        source_fields=fields,
        units=tuple(sorted(unit_rows, key=lambda item: item.study_id)),
        interpretation_dependencies=tuple(sorted(dependencies.values(), key=lambda item: (item.owner_study_id, item.primary_claim_id, item.scope, item.reason))),
    )


def _build_inventory_papers(
    payload: Mapping[str, Any],
    lineage_by_paper: Mapping[str, set[str]],
) -> tuple[VerifiedWriterSourcePaperV1, ...]:
    raw_dossiers = _require_rows(payload.get("dossiers"), "content layers dossiers")
    paper_ids: set[str] = set()
    papers: list[VerifiedWriterSourcePaperV1] = []
    for raw in raw_dossiers:
        _require_schema(raw, _DOSSIER_KEYS, "content-layer dossier")
        for name in (
            "dossier_id", "paper_id", "source_summary_hash", "status",
        ):
            _require_string(raw.get(name), f"dossier {name}")
        for name in (
            "overall_context", "research_questions", "concept_definitions", "operationalizations",
            "theoretical_derivation", "findings", "mechanism_evidence", "moderators_boundaries",
            "zero_results", "limitations", "diagnostics", "evidence_ids",
        ):
            _string_list(raw.get(name), f"dossier {raw.get('paper_id')} {name}")
        _require_string_lists(raw.get("source_locators"), "dossier source_locators")
        _require_string_lists(raw.get("evidence_ids_by_field"), "dossier evidence_ids_by_field")
        evidence_text = raw.get("evidence_text_by_id")
        if not isinstance(evidence_text, Mapping) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in evidence_text.items()
        ):
            raise WriterSourceInventoryError("dossier evidence_text_by_id must map strings to strings")
        dossier = PaperEvidenceDossier.from_dict(raw)
        if dossier.paper_id in paper_ids:
            raise WriterSourceInventoryError(f"duplicate canonical dossier paper {dossier.paper_id}")
        paper_ids.add(dossier.paper_id)
        _require_rows(raw.get("research_units"), f"dossier {dossier.paper_id} research_units")
        _require_rows(raw.get("claims"), f"dossier {dossier.paper_id} claims")
        for unit in raw.get("research_units", []):
            _require_schema(unit, _UNIT_KEYS, f"dossier {dossier.paper_id} research unit")
            for name in (
                "study_id", "parent_paper_id", "source_summary_hash", "source_study_id",
            ):
                _require_string(unit.get(name), f"research unit {name}")
            for name in (
                "research_questions", "theoretical_derivation", "method", "sample_or_context", "findings",
                "mechanisms", "moderators_or_boundaries", "zero_results", "limitations", "evidence_ids",
                "source_field_ids",
            ):
                _string_list(unit.get(name), f"research unit {name}")
            _require_string_lists(unit.get("definitions_and_operationalizations"), "research unit definitions_and_operationalizations")
            _require_string_lists(unit.get("source_locators"), "research unit source_locators")
            for claim in _require_rows(unit.get("claims"), "research-unit claims"):
                _require_schema(claim, _CLAIM_KEYS, "research-unit claim")
                for name in ("claim_id", "claim_type", "text", "study_id", "source_locator", "source_summary_hash"):
                    _require_string(claim.get(name), f"research-unit claim {name}")
                _string_list(claim.get("evidence_ids"), "research-unit claim evidence_ids")
            for dependency in _require_rows(unit.get("interpretation_dependencies"), "research-unit dependencies"):
                _require_schema(dependency, _DEPENDENCY_KEYS, "interpretation dependency")
                for name in ("primary_claim_id", "scope", "study_id", "reason"):
                    _require_string(dependency.get(name), f"interpretation dependency {name}")
                for name in ("required_source_claim_ids", "required_evidence_ids", "required_source_field_ids"):
                    _string_list(dependency.get(name), f"interpretation dependency {name}")
        for claim in raw.get("claims", []):
            _require_schema(claim, _CLAIM_KEYS, f"dossier {dossier.paper_id} claim")
            for name in ("claim_id", "claim_type", "text", "study_id", "source_locator", "source_summary_hash"):
                _require_string(claim.get(name), f"dossier claim {name}")
            _string_list(claim.get("evidence_ids"), "dossier claim evidence_ids")
        for source_field in _require_rows(raw.get("source_field_ledger"), "dossier source_field_ledger"):
            _require_schema(source_field, _FIELD_KEYS, "dossier source field")
            for name in (
                "source_field_id", "source_path", "source_value", "disposition", "canonical_field",
                "scope", "study_id", "source_summary_hash", "derived_value",
            ):
                _require_string(source_field.get(name), f"dossier source field {name}")
            if not isinstance(source_field.get("interpretation_required"), bool):
                raise WriterSourceInventoryError("source field interpretation_required must be boolean")
        lineage = lineage_by_paper.get(dossier.paper_id)
        if not lineage:
            raise WriterSourceInventoryError(f"dossier {dossier.paper_id} has no evidence-view source lineage")
        papers.append(_raw_dossier_to_paper(raw, dossier, lineage))
    return tuple(sorted(papers, key=lambda item: item.paper_key))


def load_writer_source_inventory_v1(
    registry: ArtifactRegistry,
    *,
    external_registry_resolver: Callable[[str], ArtifactRegistry | None] | None = None,
) -> VerifiedWriterSourceInventoryV1:
    """Reverify the exact current content-layer artifact and build canonical indexes."""
    record = registry.get(SOURCE_INVENTORY_ARTIFACT_ID)
    if record is None:
        raise WriterSourceInventoryError("canonical Outline v3 content layers are not registered")
    try:
        verified_record = registry.verify_ready_artifact_closure(
            record,
            external_registry_resolver=external_registry_resolver,
        )
    except Exception as exc:
        raise WriterSourceInventoryError("canonical Outline v3 content-layer dependency closure is not ready") from exc
    if verified_record.artifact_id != SOURCE_INVENTORY_ARTIFACT_ID:
        raise WriterSourceInventoryError("verified Registry root is not outline-v3:outline_content_layers")
    # The content-layer producer has exactly one direct input: the persisted
    # complete evidence-view artifact. Require it in addition to recursive
    # closure verification so a standalone forged node cannot claim authority.
    evidence_refs = [ref for ref in verified_record.depends_on if ref.artifact_id == "outline-v3:outline_evidence_views"]
    if len(evidence_refs) != 1:
        raise WriterSourceInventoryError("content layers do not bind exactly one outline evidence-view artifact")
    evidence_record = registry.get("outline-v3:outline_evidence_views")
    if evidence_record is None or evidence_refs[0].content_hash != evidence_record.content_hash:
        raise WriterSourceInventoryError("content layers bind a stale evidence-view artifact")
    try:
        verified_evidence_record = registry.verify_ready_artifact_closure(
            evidence_record,
            external_registry_resolver=external_registry_resolver,
        )
    except Exception as exc:
        raise WriterSourceInventoryError("canonical Outline v3 evidence views are not ready") from exc
    evidence_outer, evidence_payload = _read_outline_artifact(
        verified_evidence_record,
        "outline_evidence_views",
        registry_job_id=registry.job_id,
    )
    if set(evidence_payload) != _EVIDENCE_VIEWS_KEYS:
        raise WriterSourceInventoryError("upstream evidence-view payload has an unsupported v3 schema")
    if evidence_payload.get("artifact_type") != "outline_evidence_views" or evidence_payload.get("artifact_version") != "v3":
        raise WriterSourceInventoryError("upstream evidence-view payload has an unsupported schema")
    if evidence_payload.get("created_from_job_id") != registry.job_id:
        raise WriterSourceInventoryError("upstream evidence-view payload has the wrong job identity")
    try:
        evidence_view_model = OutlineEvidenceViews.from_dict(evidence_payload)
    except (TypeError, ValueError, KeyError) as exc:
        raise WriterSourceInventoryError("evidence-view payload cannot be parsed") from exc
    if (
        evidence_payload.get("status") != "ready"
        or evidence_payload.get("blocking_diagnostics") != []
        or evidence_view_model.status != evidence_payload.get("status")
        or evidence_view_model.content_hash != evidence_payload.get("content_hash")
        or evidence_payload.get("source_summary_hashes") != evidence_view_model.source_summary_hashes
    ):
        raise WriterSourceInventoryError("evidence-view payload hash or status is invalid")
    _string_list(evidence_payload.get("source_summary_hashes"), "evidence-view source_summary_hashes")
    if not isinstance(evidence_payload.get("alias_crosswalk"), Mapping):
        raise WriterSourceInventoryError("evidence-view alias_crosswalk must be an object")
    stage1_source_hashes = _verified_stage1_source_hashes(
        registry,
        evidence_outer,
        external_registry_resolver=external_registry_resolver,
    )
    if not set(evidence_view_model.source_summary_hashes).issubset(stage1_source_hashes):
        raise WriterSourceInventoryError("evidence views contain source hashes absent from current Stage 1 summaries")

    outer, payload = _read_outline_artifact(
        verified_record,
        SOURCE_INVENTORY_NODE_ID,
        registry_job_id=registry.job_id,
    )
    if set(payload) != _LAYERS_KEYS:
        raise WriterSourceInventoryError("content-layer payload schema does not match the supported v3 contract")
    if payload.get("artifact_type") != SOURCE_INVENTORY_PAYLOAD_TYPE or payload.get("artifact_version") != SOURCE_INVENTORY_PAYLOAD_VERSION:
        raise WriterSourceInventoryError("content-layer payload has an unsupported artifact type or version")
    if payload.get("status") != "ready" or payload.get("blocking_diagnostics") != []:
        raise WriterSourceInventoryError("content-layer payload is not ready")
    if not _HEX64.fullmatch(str(payload.get("content_hash") or "")):
        raise WriterSourceInventoryError("content-layer payload hash is malformed")
    _string_list(payload.get("source_summary_hashes"), "content-layer source_summary_hashes")
    _require_rows(payload.get("blocking_diagnostics"), "content-layer blocking_diagnostics")
    _require_rows(payload.get("index_cards"), "content layers index_cards")
    for card in payload.get("index_cards", []):
        _require_schema(card, _INDEX_CARD_KEYS, "content-layer index card")
        for name in ("paper_id", "method_category", "evidence_package_id", "source_summary_hash"):
            _require_string(card.get(name), f"content-layer index card {name}")
        for name in (
            "research_questions", "key_constructs", "core_findings", "key_boundaries", "topic_tags",
            "theories", "mechanisms", "source_locators",
        ):
            _string_list(card.get(name), f"content-layer index card {name}")
    try:
        layers = PaperContentLayers.from_dict(payload)
    except (TypeError, ValueError, KeyError) as exc:
        raise WriterSourceInventoryError("content-layer payload cannot be parsed") from exc
    if layers.content_hash != payload.get("content_hash") or layers.status != payload.get("status"):
        raise WriterSourceInventoryError("content-layer payload hash or status is invalid")
    if payload.get("source_summary_hashes") != layers.source_summary_hashes:
        raise WriterSourceInventoryError("content-layer source summary hash projection is invalid")
    if layers.source_summary_hashes != evidence_view_model.source_summary_hashes:
        raise WriterSourceInventoryError("content layers have a stale source-summary lineage")
    if set(outer["dependency_hashes"]) != {"outline_evidence_views"}:
        raise WriterSourceInventoryError("content-layer outer artifact has unexpected dependencies")

    evidence_dependency_hash = outer["dependency_hashes"].get("outline_evidence_views")
    if evidence_dependency_hash != compute_v3_hash(evidence_payload):
        raise WriterSourceInventoryError("content layers are not bound to the verified evidence-view payload")
    raw_evidence_views = _require_rows(evidence_payload.get("views"), "evidence-view payload views")
    views_by_paper: dict[str, list[OutlineEvidenceView]] = defaultdict(list)
    for view in raw_evidence_views:
        _require_schema(view, _EVIDENCE_VIEW_KEYS, "canonical evidence view")
        _require_rows(view.get("source_field_ledger"), "evidence-view source field ledger")
        for raw_field in view.get("source_field_ledger", []):
            _require_schema(raw_field, _FIELD_KEYS, "evidence-view source field")
        typed_view = OutlineEvidenceView.from_dict(view)
        lineage = list(typed_view.source_summary_hashes)
        expected_view_hash = (
            lineage[0]
            if len(lineage) == 1
            else compute_v3_hash({"source_summary_hashes": lineage})
            if lineage
            else ""
        )
        if (
            not lineage
            or not set(lineage).issubset(stage1_source_hashes)
            or typed_view.source_summary_hash != expected_view_hash
        ):
            raise WriterSourceInventoryError("evidence view source identity is not derived from the current Stage 1 summaries")
        if typed_view.paper_key:
            views_by_paper[typed_view.paper_key].append(typed_view)

    source_hashes = set(layers.source_summary_hashes)
    lineage_by_paper: dict[str, set[str]] = {}
    for paper_key, views in views_by_paper.items():
        if len(views) != 1:
            raise WriterSourceInventoryError(f"evidence view {paper_key} is not unique")
        view = views[0]
        view_hashes = set(view.source_summary_hashes)
        primary_view_hash = view.source_summary_hash
        expected_view_hash = (
            next(iter(view.source_summary_hashes))
            if len(view.source_summary_hashes) == 1
            else compute_v3_hash({"source_summary_hashes": list(view.source_summary_hashes)})
        )
        if not view.source_summary_hashes or primary_view_hash != expected_view_hash:
            raise WriterSourceInventoryError(f"evidence view {paper_key} source identity hash is invalid")
        lineage = view_hashes or ({primary_view_hash} if primary_view_hash else set())
        if not lineage.issubset(source_hashes):
            raise WriterSourceInventoryError(f"evidence view {paper_key} has source hashes absent from content layers")
        lineage_by_paper[paper_key] = lineage | ({primary_view_hash} if primary_view_hash else set())
    papers = _build_inventory_papers(payload, lineage_by_paper)
    for paper in papers:
        view = views_by_paper[paper.paper_key][0]
        if paper.source_summary_hash != view.source_summary_hash:
            raise WriterSourceInventoryError(f"dossier {paper.paper_key} is stale relative to its evidence view")
    papers = tuple(
        replace(paper, evidence_view_hash=views_by_paper[paper.paper_key][0].view_hash)
        for paper in papers
    )

    return VerifiedWriterSourceInventoryV1(
        registry_job_id=registry.job_id,
        artifact_id=verified_record.artifact_id,
        artifact_hash=verified_record.content_hash,
        content_hash=layers.content_hash,
        papers=papers,
        _seal=_VERIFIED_SEAL,
    )
