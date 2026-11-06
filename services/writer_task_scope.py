"""Finite, source-bound Writer task scopes for Review v3 paragraphs.

The section packet has planned claims and flattened support rows, but no
separate source-claim text ledger. V1 therefore preserves complete support
rows, makes a required planned-claim unit, and allows one optional expansion
unit per distinct source-claim member. Each unit is one cited sentence and
gets a source-closure-derived text limit with a 4,096-character schema cap;
oversize text is rejected, never cut.
Tables are outside this paragraph-only envelope until their rows/cells carry
equivalent task bindings.
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import asdict, dataclass
from dataclasses import field as dataclass_field
from typing import Any

from outline.candidate_output_scope import CandidateOutputScopeV1
from outline.candidate_paper_level_study_mentions import (
    paper_level_study_mention_occurrences_v1,
)
from outline.v3_models import compute_v3_hash
from services.citation_ref_catalog import extract_ref_ids_from_token
from services.sentence_segmenter import segment_sentences
from services.writer_source_inventory import (
    SOURCE_INVENTORY_ARTIFACT_ID,
    VerifiedWriterSourceInventoryV1,
)

WRITER_TASK_SCOPE_VERSION = "writer_task_scope/v1"
WRITER_OUTPUT_TEXT_HARD_CAP_CHARS = 4096
WRITER_REASON_CODE_MAX_CHARS = 64
WRITER_SELECTED_SOURCE_CONTRACT = "source-closed-provider/v2"
_SELECTED_SOURCE_BINDING_VERSION = "writer-selected-source-binding/v2"
_SELECTED_BASIS_VERSION = "writer-selected-task-basis/v2"
_BASIS_TASK_FIELDS = (
    "writer_task_id", "task_kind", "planned_claim", "duplicate_index", "status",
    "reason_codes", "support_rows", "source_evidence", "canonical_source_bundle",
    "source_membership_status", "allowed_ref_ids", "output_units",
)
_CONTEXT_SOURCE_FIELDS = frozenset({
    "paper_key", "source_summary_hash", "source_summary_hashes", "dossier_content_hash",
    "evidence_view_hash", "dossier_status", "diagnostics", "allowed_ref_ids",
})

_CLAIM_IDS = ("source_claim_ids", "primary_claim_id", "required_source_claim_ids", "qualifier_source_claim_ids")
_EVIDENCE_IDS = ("evidence_ids", "primary_evidence_ids", "required_evidence_ids", "qualifier_evidence_ids")
_FIELD_IDS = ("source_field_ids", "required_source_field_ids", "qualifier_source_field_ids")
_QUALIFIER_CLAIMS = ("required_source_claim_ids", "qualifier_source_claim_ids")
_QUALIFIER_EVIDENCE = ("required_evidence_ids", "qualifier_evidence_ids")
_QUALIFIER_FIELDS = ("required_source_field_ids", "qualifier_source_field_ids")
_CITATION_TOKEN = re.compile(r"\[\[cite_ref:[^\]]+\]\]")
_STUDY_MENTION = re.compile(
    r"\b(?:study|experiment|trial)\s*[A-Za-z]?[1-9]\d*\b|研究\s*[A-Za-z]?[1-9]\d*|实验\s*[A-Za-z]?[1-9]\d*",
    re.IGNORECASE,
)

_VERIFIED_CANDIDATE_SCOPE_SEAL = object()


@dataclass(frozen=True)
class VerifiedCandidateOutputScopeV1:
    """Candidate scope granted after Review service Registry verification."""

    scope: CandidateOutputScopeV1
    _seal: object = dataclass_field(default=None, repr=False, compare=False)

    @property
    def is_verified(self) -> bool:
        return self._seal is _VERIFIED_CANDIDATE_SCOPE_SEAL


def _verified_candidate_output_scope(scope: CandidateOutputScopeV1) -> VerifiedCandidateOutputScopeV1:
    """Internal bridge for an exact current Registry lineage proof."""
    return VerifiedCandidateOutputScopeV1(scope=scope, _seal=_VERIFIED_CANDIDATE_SCOPE_SEAL)
_SOURCE_TEXT_FIELDS = {
    "claim", "source_value", "text", "summary", "finding", "condition", "boundary", "findings", "conclusions", "limitations",
    "research_gaps", "future_directions", "mechanisms", "sample_or_context", "method",
    "qualifier_text", "condition_text", "boundary_text", "interpretation", "interpretation_text",
    "relevance", "theories", "constructs", "research_questions",
}


class WriterTaskScopeError(ValueError):
    """Writer output or its source scope cannot be safely bound."""


def _items(value: Any) -> list[Any]:
    if value is None:
        return []
    return list(value) if isinstance(value, Sequence) and not isinstance(value, (str, bytes)) else [value]


def _text(value: Any) -> str:
    # Do not use truthiness: meaningful source values 0 and False must survive.
    return "" if value is None else str(value).strip()


def _ids(rows: Sequence[Mapping[str, Any]], fields: Sequence[str]) -> list[str]:
    values = {_text(value) for row in rows for field in fields for value in _items(row.get(field))}
    return sorted((value for value in values if value), key=lambda value: (value.casefold(), value))


def _stable_rows(rows: Sequence[Any]) -> list[Any]:
    return sorted((dict(row) if isinstance(row, Mapping) else row for row in rows), key=compute_v3_hash)


def _id(prefix: str, value: Any) -> str:
    return f"{prefix}_{compute_v3_hash(value)[:24]}"


def _source_text_chars(value: Any) -> int:
    """Count distinct source-derived prose fields, excluding identifiers."""
    texts: set[str] = set()

    def visit(item: Any, field: str = "") -> None:
        if isinstance(item, Mapping):
            for key, child in item.items():
                visit(child, str(key))
        elif isinstance(item, Sequence) and not isinstance(item, (str, bytes)):
            for child in item:
                visit(child, field)
        elif field in _SOURCE_TEXT_FIELDS and isinstance(item, str) and item.strip():
            texts.add(item.strip())

    visit(value)
    return sum(len(text) for text in texts)


def _unit_text_limit(claim: str, rows: Sequence[Any], evidence: Sequence[Any]) -> tuple[int, int]:
    source_chars = max(len(claim), _source_text_chars((rows, evidence)))
    # Two characters of expansion per source character plus sentence/citation
    # overhead; the 4,096-character ceiling is a schema safety cap.
    limit = min(WRITER_OUTPUT_TEXT_HARD_CAP_CHARS, max(160, source_chars * 2 + 160))
    return limit, source_chars


def _paper_level_study_mention_spans(
    claim: str,
    rows: Sequence[Mapping[str, Any]],
    *,
    source_inventory: VerifiedWriterSourceInventoryV1 | None,
    candidate_output_scope: VerifiedCandidateOutputScopeV1 | None,
) -> set[tuple[int, int]]:
    """Return only mentions proved by exact source claim, evidence, and scope slots."""

    if (
        not isinstance(source_inventory, VerifiedWriterSourceInventoryV1)
        or type(source_inventory) is not VerifiedWriterSourceInventoryV1
        or not source_inventory.is_verified
        or type(candidate_output_scope) is not VerifiedCandidateOutputScopeV1
        or not candidate_output_scope.is_verified
        or not rows
    ):
        return set()

    scope = candidate_output_scope.scope
    if (
        type(scope) is not CandidateOutputScopeV1
        or scope.registry_job_id != source_inventory.registry_job_id
        or scope.source_inventory_artifact_id != source_inventory.artifact_id
        or scope.source_inventory_artifact_hash != source_inventory.artifact_hash
        or scope.source_inventory_content_hash != source_inventory.content_hash
    ):
        return set()
    slots_by_id = {slot.claim_slot_id: slot for slot in scope.claim_slots}
    papers = source_inventory.paper_by_key()
    proofs_by_support: list[set[tuple[int, int]]] = []
    for row in rows:
        slot = slots_by_id.get(_text(row.get("claim_slot_id")))
        paper = papers.get(_text(row.get("paper_key")))
        if slot is None or paper is None:
            return set()
        source_claim = next(
            (
                item for item in paper.claims
                if item.claim_id == _text(row.get("primary_claim_id"))
            ),
            None,
        )
        if source_claim is None:
            return set()
        occurrences = paper_level_study_mention_occurrences_v1(
            claim,
            source_claim=asdict(source_claim),
            support_row=row,
            scope_slot=slot.to_dict(),
        )
        proofs_by_support.append({(item.start, item.end) for item in occurrences})
    if not proofs_by_support:
        return set()
    return set.intersection(*proofs_by_support)


def _has_unproved_study_mention(
    claim: str,
    proved_spans: set[tuple[int, int]],
) -> bool:
    return any(
        match.span() not in proved_spans
        for match in _STUDY_MENTION.finditer(claim)
    )


def _paper(raw: Any) -> str:
    if not isinstance(raw, Mapping):
        return ""
    return _text(raw.get("paper_key") or raw.get("canonical_paper_key"))


def _active_refs(catalog: Mapping[str, Any]) -> tuple[dict[str, list[str]], set[str]]:
    by_paper: dict[str, set[str]] = defaultdict(set)
    owners: dict[str, set[str]] = defaultdict(set)
    for entry in _items(catalog.get("entries")):
        if not isinstance(entry, Mapping) or entry.get("status") != "active":
            continue
        paper, ref = _text(entry.get("canonical_paper_key")), _text(entry.get("ref_id"))
        if paper and ref:
            by_paper[paper].add(ref)
            owners[ref].add(paper)
    conflicts = {ref for ref, papers in owners.items() if len(papers) > 1}
    return ({paper: sorted(refs) for paper, refs in by_paper.items()}, conflicts)


def _packet_bundle(packet: Mapping[str, Any]) -> dict[str, Any]:
    """Canonicalized full packet snapshot used for source preservation and hashing."""
    set_fields = {"planned_claims", "paper_keys", "relation_ids", "source_summary_hashes", "evidence_view_hashes"}
    bundle: dict[str, Any] = {
        str(key): value for key, value in packet.items()
        if key not in {"claim_support", "evidence_items", "relation_evidence", *set_fields}
    }
    for field in set_fields:
        values = [_text(value) for value in _items(packet.get(field)) if value is not None]
        bundle[field] = sorted(values, key=lambda value: (value.casefold(), value))
    for field in ("claim_support", "evidence_items", "relation_evidence"):
        bundle[field] = _stable_rows(_items(packet.get(field)))
    return bundle


def _task_reasons(
    claim: str,
    rows: Sequence[Mapping[str, Any]],
    *,
    packet_papers: set[str],
    evidence_papers: set[str],
    source_hashes: set[str],
    refs_by_paper: Mapping[str, Sequence[str]],
    ref_conflicts: set[str],
    paper_level_study_mention_spans: set[tuple[int, int]] | None = None,
    validate_packet_source_hashes: bool = True,
) -> list[str]:
    reasons: set[str] = set()
    if not rows:
        return ["missing_claim_support"]
    if not _ids(rows, _CLAIM_IDS):
        reasons.add("missing_source_claim_identity")
    if not _ids(rows, _EVIDENCE_IDS):
        reasons.add("missing_evidence_identity")
    for row in rows:
        if _text(row.get("claim")) != claim:
            reasons.add("support_claim_mismatch")
        paper_key = _text(row.get("paper_key"))
        if not paper_key or paper_key not in packet_papers or paper_key not in evidence_papers:
            reasons.add("unknown_support_source")
            continue
        refs = set(refs_by_paper.get(paper_key, ()))
        if not refs:
            reasons.add("source_has_no_active_citation_ref")
        if refs & ref_conflicts:
            reasons.add("conflicting_citation_ref_source")
        if not _ids([row], _CLAIM_IDS):
            reasons.add("support_row_missing_source_claim_identity")
        if not _ids([row], _EVIDENCE_IDS):
            reasons.add("support_row_missing_evidence_identity")
        if (
            _text(row.get("study_id"))
            or _has_unproved_study_mention(claim, paper_level_study_mention_spans or set())
        ) and not _ids([row], _FIELD_IDS):
            reasons.add("scoped_claim_missing_source_field_identity")
        hashes = set(_ids([row], ("source_summary_hash", "source_summary_hashes")))
        if validate_packet_source_hashes and hashes and not hashes.issubset(source_hashes):
            reasons.add("unknown_source_summary_hash")
        for required, available, reason in (
            (_ids([row], _QUALIFIER_CLAIMS), _ids([row], ("source_claim_ids",)), "missing_required_qualifier_claim"),
            (_ids([row], _QUALIFIER_EVIDENCE), _ids([row], ("evidence_ids",)), "missing_required_qualifier_evidence"),
            (_ids([row], _QUALIFIER_FIELDS), _ids([row], ("source_field_ids",)), "missing_required_qualifier_field"),
        ):
            if not set(required).issubset(available):
                reasons.add(reason)
    return sorted(reasons)


def _owner_matches(owner_study_ids: Sequence[str], row_study_id: str) -> bool:
    owners = set(owner_study_ids)
    if row_study_id:
        return row_study_id in owners or "" in owners
    return "" in owners


def _canonical_source_support(
    packet: Mapping[str, Any],
    claim: str,
    rows: Sequence[Mapping[str, Any]],
    inventory: VerifiedWriterSourceInventoryV1,
    *,
    paper_level_study_mention_spans: set[tuple[int, int]] | None = None,
) -> tuple[list[str], list[dict[str, Any]]]:
    """Exact-join candidate support IDs to Registry-verified dossier rows."""
    reasons: set[str] = set()
    if type(inventory) is not VerifiedWriterSourceInventoryV1 or not inventory.is_verified:
        return ["unverified_source_inventory"], []
    papers = inventory.paper_by_key()
    packet_papers = {_text(value) for value in _items(packet.get("paper_keys")) if _text(value)}
    packet_hashes = {_text(value) for value in _items(packet.get("source_summary_hashes")) if _text(value)}
    raw_evidence = [item for item in _items(packet.get("evidence_items")) if isinstance(item, Mapping)]
    packet_evidence: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for item in raw_evidence:
        paper_key = _paper(item)
        if paper_key:
            packet_evidence[paper_key].append(item)

    canonical_rows: list[dict[str, Any]] = []
    for row in rows:
        if _text(row.get("claim")) != claim:
            reasons.add("support_claim_mismatch")
        paper_key = _text(row.get("paper_key"))
        paper = papers.get(paper_key)
        if not paper or paper_key not in packet_papers:
            reasons.add("unknown_support_source")
            continue
        # Partial paper-level coverage is diagnostic; admission is decided by
        # this assertion's exact claim/evidence/qualifier closure below.
        if paper.dossier_status not in {"ready", "partial"}:
            reasons.add("source_dossier_not_ready")
        evidence_items = packet_evidence.get(paper_key, [])
        if len(evidence_items) != 1:
            reasons.add("section_evidence_source_not_unique")
        elif (
            _text(evidence_items[0].get("summary_hash")) != paper.source_summary_hash
            or _text(evidence_items[0].get("view_hash")) != paper.evidence_view_hash
            or paper.source_summary_hash not in packet_hashes
        ):
            reasons.add("stale_section_evidence_packet")
        declared_row_hashes = set(_ids([row], ("source_summary_hash", "source_summary_hashes")))
        if declared_row_hashes and not declared_row_hashes.issubset(set(paper.source_summary_hashes)):
            reasons.add("support_source_summary_hash_mismatch")

        claim_map = {item.claim_id: item for item in paper.claims}
        evidence_map = {item.evidence_id: item for item in paper.evidence}
        field_map = {item.source_field_id: item for item in paper.source_fields}
        dep_rows = list(paper.interpretation_dependencies)
        declared_claim_ids = set(_ids([row], _CLAIM_IDS))
        explicit_source_claim_ids = set(_ids([row], ("source_claim_ids",)))
        declared_evidence_ids = set(_ids([row], _EVIDENCE_IDS))
        explicit_evidence_ids = set(_ids([row], ("evidence_ids",)))
        declared_field_ids = set(_ids([row], _FIELD_IDS))
        explicit_field_ids = set(_ids([row], ("source_field_ids",)))
        row_study = _text(row.get("study_id"))
        if not explicit_source_claim_ids:
            reasons.add("missing_source_claim_identity")
        if not explicit_evidence_ids:
            reasons.add("missing_evidence_identity")
        if (
            row_study
            or _has_unproved_study_mention(claim, paper_level_study_mention_spans or set())
        ) and not explicit_field_ids:
            reasons.add("scoped_claim_missing_source_field_identity")

        claim_text_by_id: dict[str, str] = {}
        for source_claim_id in sorted(declared_claim_ids):
            source_claim = claim_map.get(source_claim_id)
            if source_claim is None:
                reasons.add("unknown_source_claim_id")
                continue
            if source_claim.source_summary_hash not in {"", paper.source_summary_hash}:
                reasons.add("stale_source_claim_summary_hash")
            if not _owner_matches(source_claim.owner_study_ids, row_study):
                reasons.add("source_claim_wrong_study")
            claim_text_by_id[source_claim_id] = source_claim.text

        for evidence_id in sorted(declared_evidence_ids):
            evidence = evidence_map.get(evidence_id)
            if evidence is None:
                reasons.add("unknown_evidence_id")
                continue
            if not _owner_matches(evidence.owner_study_ids, row_study):
                reasons.add("evidence_wrong_study")
            if not evidence.text and not any(
                evidence_id in claim.evidence_ids for claim in paper.claims
                if claim.claim_id in declared_claim_ids
            ):
                reasons.add("canonical_evidence_text_missing")

        for field_id in sorted(declared_field_ids):
            source_field = field_map.get(field_id)
            if source_field is None:
                reasons.add("unknown_source_field_id")
                continue
            if source_field.source_summary_hash not in {"", paper.source_summary_hash}:
                reasons.add("stale_source_field_summary_hash")
            if source_field.scope == "explicit_study":
                if not row_study or row_study not in source_field.owner_study_ids:
                    reasons.add("source_field_wrong_study")
            elif source_field.scope == "unresolved":
                reasons.add("unresolved_source_field_scope")
            elif row_study and source_field.scope != "paper":
                reasons.add("source_field_wrong_study")

        claim_evidence = {
            evidence_id
            for source_claim_id in declared_claim_ids
            if (source_claim := claim_map.get(source_claim_id)) is not None
            for evidence_id in source_claim.evidence_ids
        }
        if not claim_evidence.issubset(explicit_evidence_ids):
            reasons.add("source_claim_evidence_missing")
        if explicit_evidence_ids - claim_evidence:
            reasons.add("evidence_not_bound_to_source_claim")

        applicable_dependencies = [
            dependency
            for dependency in dep_rows
            if dependency.primary_claim_id in declared_claim_ids
        ]
        closure_claim_ids = set(declared_claim_ids)
        closure_evidence_ids = set(declared_evidence_ids)
        closure_field_ids = set(declared_field_ids)
        for dependency in applicable_dependencies:
            if dependency.scope == "unresolved":
                reasons.add("unresolved_interpretation_dependency")
            if dependency.scope == "explicit_study" and (
                not row_study or row_study != dependency.owner_study_id
            ):
                reasons.add("interpretation_dependency_wrong_study")
            required_claim_ids = {
                dependency.primary_claim_id,
                *dependency.required_source_claim_ids,
            }
            if not required_claim_ids.issubset(declared_claim_ids):
                reasons.add("missing_interpretation_qualifier_claim")
            required_evidence_ids = set(dependency.required_evidence_ids)
            primary = claim_map.get(dependency.primary_claim_id)
            if primary is not None:
                required_evidence_ids.update(primary.evidence_ids)
            if not required_evidence_ids.issubset(declared_evidence_ids):
                reasons.add("missing_interpretation_qualifier_evidence")
            if not set(dependency.required_source_field_ids).issubset(declared_field_ids):
                reasons.add("missing_interpretation_qualifier_field")
            closure_claim_ids.update(required_claim_ids)
            closure_evidence_ids.update(required_evidence_ids)
            closure_field_ids.update(dependency.required_source_field_ids)

        for source_claim_id in closure_claim_ids:
            source_claim = claim_map.get(source_claim_id)
            if source_claim is not None:
                claim_text_by_id.setdefault(source_claim_id, source_claim.text)
        canonical_claim_rows = [
            {
                "claim_id": item.claim_id,
                "claim_type": item.claim_type,
                "text": item.text,
                "study_id": row_study if row_study in item.owner_study_ids else (item.owner_study_ids[0] if item.owner_study_ids else ""),
                "evidence_ids": list(item.evidence_ids),
                "source_locator": item.source_locator,
                "source_summary_hash": item.source_summary_hash,
            }
            for claim_id in sorted(closure_claim_ids)
            if (item := claim_map.get(claim_id)) is not None
        ]
        canonical_evidence = []
        for evidence_id in sorted(closure_evidence_ids):
            evidence = evidence_map.get(evidence_id)
            if evidence is None:
                continue
            text = evidence.text or next(
                (claim_text_by_id[claim_id] for claim_id in sorted(claim_text_by_id)
                 if evidence_id in claim_map[claim_id].evidence_ids),
                None,
            )
            canonical_evidence.append({"evidence_id": evidence_id, "text": text})
        canonical_fields = [
            {
                "source_field_id": item.source_field_id,
                "source_path": item.source_path,
                "source_value": item.source_value,
                "disposition": item.disposition,
                "canonical_field": item.canonical_field,
                "scope": item.scope,
                "study_id": row_study if row_study in item.owner_study_ids else item.source_study_id,
                "interpretation_required": item.interpretation_required,
                "source_summary_hash": item.source_summary_hash,
                "derived_value": item.derived_value,
                "source_study_id": item.source_study_id,
            }
            for field_id in sorted(closure_field_ids)
            if (item := field_map.get(field_id)) is not None
        ]
        canonical_dependencies = [
            {
                "primary_claim_id": item.primary_claim_id,
                "required_source_claim_ids": list(item.required_source_claim_ids),
                "required_evidence_ids": list(item.required_evidence_ids),
                "required_source_field_ids": list(item.required_source_field_ids),
                "scope": item.scope,
                "study_id": item.study_id,
                "owner_study_id": item.owner_study_id,
                "reason": item.reason,
            }
            for item in applicable_dependencies
        ]
        relevant_units = [
            json.loads(unit.canonical_json)
            for unit in paper.units
            if (row_study and unit.study_id == row_study)
            or set(unit.claim_ids).intersection(closure_claim_ids)
        ]
        canonical_rows.append({
            "paper_key": paper.paper_key,
            "dossier_id": paper.dossier_id,
            "dossier_content_hash": paper.dossier_content_hash,
            "source_summary_hash": paper.source_summary_hash,
            "dossier_status": paper.dossier_status,
            "dossier_diagnostics": list(paper.diagnostics),
            "study_id": row_study,
            "source_claims": canonical_claim_rows,
            "evidence": canonical_evidence,
            "source_fields": canonical_fields,
            "interpretation_dependencies": canonical_dependencies,
            "research_units": relevant_units,
        })
    return sorted(reasons), canonical_rows


def _make_task(
    section_id: str,
    claim: str,
    rows: Sequence[Any],
    *,
    packet: Mapping[str, Any],
    bundle: Mapping[str, Any],
    refs_by_paper: Mapping[str, Sequence[str]],
    ref_conflicts: set[str],
    source_inventory: VerifiedWriterSourceInventoryV1 | None = None,
    candidate_output_scope: VerifiedCandidateOutputScopeV1 | None = None,
    duplicate_index: int | None = None,
    kind: str = "planned_claim",
    forced_reasons: Sequence[str] = (),
) -> dict[str, Any]:
    full_rows = _stable_rows(rows)
    mappings = [row for row in full_rows if isinstance(row, Mapping)]
    packet_papers = {_text(value) for value in _items(packet.get("paper_keys")) if _text(value)}
    source_items = [row for row in bundle.get("evidence_items", []) if isinstance(row, Mapping)]
    evidence_papers = {_paper(row) for row in source_items if _paper(row)}
    source_hashes = {_text(value) for value in _items(packet.get("source_summary_hashes")) if _text(value)}
    reasons = set(forced_reasons)
    paper_level_spans = _paper_level_study_mention_spans(
        claim,
        mappings,
        source_inventory=source_inventory,
        candidate_output_scope=candidate_output_scope,
    )
    reasons.update(_task_reasons(
        claim,
        mappings,
        packet_papers=packet_papers,
        evidence_papers=evidence_papers,
        source_hashes=source_hashes,
        refs_by_paper=refs_by_paper,
        ref_conflicts=ref_conflicts,
        paper_level_study_mention_spans=paper_level_spans,
        validate_packet_source_hashes=source_inventory is None,
    ))
    canonical_source_reasons: list[str] = []
    canonical_source_bundle: list[dict[str, Any]] = []
    inventory_verified = bool(
        isinstance(source_inventory, VerifiedWriterSourceInventoryV1)
        and type(source_inventory) is VerifiedWriterSourceInventoryV1
        and source_inventory.is_verified
    )
    if source_inventory is not None:
        if inventory_verified:
            canonical_source_reasons, canonical_source_bundle = _canonical_source_support(
                packet,
                claim,
                mappings,
                source_inventory,
                paper_level_study_mention_spans=paper_level_spans,
            )
        else:
            canonical_source_reasons = ["unverified_source_inventory"]
        reasons.update(canonical_source_reasons)
    source_claim_ids = _ids(mappings, _CLAIM_IDS)
    evidence_ids = _ids(mappings, _EVIDENCE_IDS)
    field_ids = _ids(mappings, _FIELD_IDS)
    declared_qualifier_claim_ids = set(_ids(mappings, _QUALIFIER_CLAIMS))
    declared_qualifier_evidence_ids = set(_ids(mappings, _QUALIFIER_EVIDENCE))
    declared_qualifier_field_ids = set(_ids(mappings, _QUALIFIER_FIELDS))
    derived_qualifier_claim_ids: set[str] = set()
    derived_qualifier_evidence_ids: set[str] = set()
    derived_qualifier_field_ids: set[str] = set()
    if inventory_verified and not canonical_source_reasons and source_inventory is not None:
        canonical_rows_by_paper: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        for canonical_row in canonical_source_bundle:
            canonical_paper_key = _text(canonical_row.get("paper_key"))
            if canonical_paper_key:
                canonical_rows_by_paper[canonical_paper_key].append(canonical_row)
        for row in mappings:
            paper_key = _text(row.get("paper_key"))
            if not paper_key:
                continue
            paper_rows = canonical_rows_by_paper.get(paper_key, ())
            if not paper_rows:
                reasons.add("unknown_support_source")
                continue
            declared_source_claim_ids = set(_ids([row], ("source_claim_ids",)))
            explicit_primary_claim_id = _text(row.get("primary_claim_id"))
            if explicit_primary_claim_id:
                primary_claim_ids = {explicit_primary_claim_id}
            else:
                # A legacy support row can omit its primary id. Resolve only
                # dependency primaries that it explicitly lists as source
                # claim IDs; never infer a primary by subtracting qualifiers.
                primary_claim_ids = {
                    _text(dependency.get("primary_claim_id"))
                    for canonical_row in paper_rows
                    for dependency in canonical_row.get("interpretation_dependencies") or ()
                    if isinstance(dependency, Mapping)
                    and _text(dependency.get("primary_claim_id")) in declared_source_claim_ids
                }
            row_study_id = _text(row.get("study_id"))
            for primary_claim_id in primary_claim_ids:
                primary_claim_rows = [
                    source_claim
                    for canonical_row in paper_rows
                    for source_claim in canonical_row.get("source_claims") or ()
                    if isinstance(source_claim, Mapping)
                    and _text(source_claim.get("claim_id")) == primary_claim_id
                ]
                if not primary_claim_rows:
                    reasons.add("unknown_source_claim_id")
                    continue
                if len({
                    _text(source_claim.get("study_id"))
                    for source_claim in primary_claim_rows
                }) > 1:
                    reasons.add("source_claim_wrong_study")
                    continue
                primary_study_id = _text(primary_claim_rows[0].get("study_id"))
                if row_study_id and primary_study_id and row_study_id != primary_study_id:
                    reasons.add("source_claim_wrong_study")
                    continue
                if not row_study_id and primary_study_id:
                    reasons.add("source_claim_wrong_study")
                    continue
                for canonical_row in paper_rows:
                    for dependency in canonical_row.get("interpretation_dependencies") or ():
                        if not isinstance(dependency, Mapping):
                            reasons.add("invalid_interpretation_dependency")
                            continue
                        if _text(dependency.get("primary_claim_id")) != primary_claim_id:
                            continue
                        dependency_scope = _text(dependency.get("scope"))
                        dependency_owner = _text(dependency.get("owner_study_id"))
                        dependency_study = _text(dependency.get("study_id"))
                        if dependency_scope == "unresolved":
                            reasons.add("unresolved_interpretation_dependency")
                            continue
                        if dependency_scope == "explicit_study":
                            if (
                                not row_study_id
                                or dependency_study != row_study_id
                                or dependency_owner != row_study_id
                            ):
                                reasons.add("interpretation_dependency_wrong_study")
                                continue
                        elif dependency_scope == "paper":
                            # owner_study_id identifies the source unit that
                            # contributed this verified dependency; it does
                            # not narrow a paper-scoped qualifier. Only a
                            # non-empty dependency study ID conflicts with a
                            # paper-level dependency.
                            if dependency_study:
                                reasons.add("interpretation_dependency_wrong_study")
                                continue
                        else:
                            reasons.add("unresolved_interpretation_dependency")
                            continue
                        derived_qualifier_claim_ids.update(
                            _ids([dependency], ("required_source_claim_ids",))
                        )
                        derived_qualifier_evidence_ids.update(
                            _ids([dependency], ("required_evidence_ids",))
                        )
                        derived_qualifier_field_ids.update(
                            _ids([dependency], ("required_source_field_ids",))
                        )

    if not derived_qualifier_claim_ids.issubset(set(source_claim_ids)):
        reasons.add("missing_interpretation_qualifier_claim")
    if not derived_qualifier_evidence_ids.issubset(set(evidence_ids)):
        reasons.add("missing_interpretation_qualifier_evidence")
    if not derived_qualifier_field_ids.issubset(set(field_ids)):
        reasons.add("missing_interpretation_qualifier_field")
    qualifier_claim_ids = sorted(declared_qualifier_claim_ids | derived_qualifier_claim_ids)
    qualifier_evidence_ids = sorted(declared_qualifier_evidence_ids | derived_qualifier_evidence_ids)
    qualifier_field_ids = sorted(declared_qualifier_field_ids | derived_qualifier_field_ids)
    paper_keys = sorted({_text(row.get("paper_key")) for row in mappings if _text(row.get("paper_key"))})
    usable_papers = set(paper_keys) & packet_papers & evidence_papers
    allowed_refs = sorted({ref for paper in usable_papers for ref in refs_by_paper.get(paper, ()) if ref not in ref_conflicts})
    if mappings and not allowed_refs:
        reasons.add("no_allowed_citation_refs")
    packet_source_evidence = [row for row in source_items if _paper(row) in usable_papers]
    source_evidence = canonical_source_bundle if inventory_verified else packet_source_evidence
    text_limit, source_text_chars = _unit_text_limit(claim, full_rows, source_evidence)
    if allowed_refs and min(len(f"[[cite_ref:{ref}]]") for ref in allowed_refs) + 2 > text_limit:
        reasons.add("citation_ref_exceeds_task_text_bound")
    status = "needs_review" if reasons else "ready"
    identity = {
        "section_id": section_id,
        "kind": kind,
        "claim": claim,
        "support_rows": full_rows,
        "allowed_ref_ids": allowed_refs,
        "duplicate_index": duplicate_index,
    }
    task_id = _id("wtv1", identity)
    units: list[dict[str, Any]] = []
    if status == "ready":
        closure = {
            "source_claim_ids": source_claim_ids,
            "evidence_ids": evidence_ids,
            "source_field_ids": field_ids,
            "qualifier_source_claim_ids": qualifier_claim_ids,
            "qualifier_evidence_ids": qualifier_evidence_ids,
            "qualifier_source_field_ids": qualifier_field_ids,
        }
        slot_specs: list[tuple[str, str | None, bool]] = [("planned_claim", None, True)]
        primary_ids = set(_ids(mappings, ("primary_claim_id",)))
        slot_specs.extend(("source_claim_expansion", source_id, False) for source_id in source_claim_ids if source_id not in primary_ids)
        for unit_kind, source_id, required in slot_specs:
            units.append({
                "writer_output_unit_id": _id("wou1", {"task": task_id, "kind": unit_kind, "source_claim_id": source_id, "claim": claim if source_id is None else None}),
                "unit_kind": unit_kind,
                "source_claim_id": source_id,
                "required": required,
                "max_sentences": 1,
                "max_text_chars": text_limit,
                "max_text_utf8_bytes": text_limit * 4,
                "source_closure_text_chars": source_text_chars,
                "allowed_ref_ids": allowed_refs,
                "required_source_context": closure,
            })
    return {
        "writer_task_id": task_id,
        "task_kind": kind,
        "planned_claim": claim,
        "duplicate_index": duplicate_index,
        "status": status,
        "reason_codes": sorted(reasons),
        "paper_keys": paper_keys,
        "source_claim_ids": source_claim_ids,
        "evidence_ids": evidence_ids,
        "source_field_ids": field_ids,
        "qualifier_source_claim_ids": qualifier_claim_ids,
        "qualifier_evidence_ids": qualifier_evidence_ids,
        "qualifier_source_field_ids": qualifier_field_ids,
        "allowed_ref_ids": allowed_refs,
        "support_rows": full_rows,
        "source_evidence": source_evidence,
        "packet_evidence_unverified": packet_source_evidence,
        "canonical_source_bundle": canonical_source_bundle,
        "source_membership_status": (
            "verified" if inventory_verified and not canonical_source_reasons
            else "needs_review" if source_inventory is not None
            else "not_verified"
        ),
        "output_units": units,
    }


def _max_response_bytes(scope: Mapping[str, Any]) -> int:
    blocks, dispositions, text_bytes = [], [], 0
    basis = scope["writer_task_basis_hash"]
    for task in scope["tasks"]:
        task_id = task["writer_task_id"]
        if task["status"] == "ready":
            dispositions.append({"writer_task_id": task_id, "writer_task_basis_hash": basis, "disposition": "covered"})
            for unit in task["output_units"]:
                blocks.append({"writer_task_id": task_id, "writer_output_unit_id": unit["writer_output_unit_id"], "writer_task_basis_hash": basis, "text": ""})
                text_bytes += unit["max_text_chars"] * 6  # worst-case JSON escaping per source character
        else:
            dispositions.append({"writer_task_id": task_id, "writer_task_basis_hash": basis, "disposition": "needs_review", "reason_code": "r" * WRITER_REASON_CODE_MAX_CHARS})
    skeleton = {"blocks": blocks, "task_dispositions": dispositions}
    return len(json.dumps(skeleton, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) + text_bytes


def _selected_source_contract(scope: Mapping[str, Any], version: str | None) -> bool:
    if not isinstance(scope, Mapping):
        raise WriterTaskScopeError("Writer semantic scope must be an object")
    if version is None:
        if any(name in scope for name in ("provider_semantic_contract_version", "selected_source_binding",
                                          "selected_catalog_entries", "selected_context_sources")):
            raise WriterTaskScopeError("selected Writer scope requires its explicit semantic contract")
        return False
    if version != WRITER_SELECTED_SOURCE_CONTRACT or scope.get("provider_semantic_contract_version") != version:
        raise WriterTaskScopeError("Writer semantic contract or scope marker is unsupported")
    return True


def _selected_source_binding(tasks: Sequence[Mapping[str, Any]], catalog_entries: Any,
                             context_sources: Any) -> dict[str, Any]:
    if not isinstance(catalog_entries, list):
        raise WriterTaskScopeError("selected Writer catalog entries are absent")
    if not isinstance(context_sources, list) or not context_sources:
        raise WriterTaskScopeError("selected Writer context source pins are absent")
    contexts = {}
    for source in context_sources:
        if (not isinstance(source, Mapping) or set(source) != _CONTEXT_SOURCE_FIELDS
                or not isinstance(source.get("paper_key"), str) or not source["paper_key"]
                or source["paper_key"] in contexts
                or source.get("dossier_status") not in {"ready", "partial"}
                or any(not isinstance(source.get(name), list) for name in ("source_summary_hashes", "diagnostics", "allowed_ref_ids"))
                or source["source_summary_hash"] not in source["source_summary_hashes"]
                or any(not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None
                       for value in source["source_summary_hashes"])
                or any(not isinstance(value, str) for value in source["diagnostics"])
                or not source["allowed_ref_ids"]
                or any(not isinstance(value, str) or not value for value in source["allowed_ref_ids"])
                or len(set(source["allowed_ref_ids"])) != len(source["allowed_ref_ids"])
                or any(not isinstance(source.get(name), str) or re.fullmatch(r"[0-9a-f]{64}", source[name]) is None
                       for name in ("source_summary_hash", "dossier_content_hash", "evidence_view_hash"))):
            raise WriterTaskScopeError("selected Writer context source identity is incomplete or duplicated")
        contexts[source["paper_key"]] = source
    papers = set(contexts)
    refs = {ref for source in context_sources for ref in source["allowed_ref_ids"]}
    sources = []
    for task in tasks:
        bundle = task.get("canonical_source_bundle")
        if (task.get("status") != "ready" or task.get("source_membership_status") != "verified"
                or not isinstance(bundle, list) or not bundle):
            raise WriterTaskScopeError("selected Writer task has no verified complete source closure")
        task_papers = set(task.get("paper_keys") or [])
        bundle_papers = {row.get("paper_key") for row in bundle if isinstance(row, Mapping)}
        if not task_papers or task_papers != bundle_papers or not task_papers.issubset(papers):
            raise WriterTaskScopeError("selected Writer source paper membership differs")
        expected_refs = {ref for paper in task_papers for ref in contexts[paper]["allowed_ref_ids"]}
        if set(task.get("allowed_ref_ids") or []) != expected_refs:
            raise WriterTaskScopeError("selected Writer task reference ownership differs")
        for row in bundle:
            if not isinstance(row, Mapping) or any(not isinstance(row.get(name), list) for name in (
                "source_claims", "evidence", "source_fields", "interpretation_dependencies", "research_units",
            )):
                raise WriterTaskScopeError("selected Writer canonical source fields are incomplete")
            pin = contexts[row["paper_key"]]
            if any(row.get(name) != pin[name] for name in ("source_summary_hash", "dossier_content_hash", "dossier_status")):
                raise WriterTaskScopeError("selected Writer source rows differ from their context authority pins")
        sources.append({"writer_task_id": task["writer_task_id"], "canonical_source_bundle": bundle})
    required_catalog = {"ref_id", "scope", "paper_id", "canonical_paper_key", "title", "authors",
                        "year", "doi", "source_summary_hash", "status"}
    observed_refs: list[str] = []
    for row in catalog_entries:
        if (not isinstance(row, Mapping) or not required_catalog.issubset(row)
                or row.get("status") != "active" or row.get("canonical_paper_key") not in papers
                or row.get("ref_id") not in refs
                or row["ref_id"] not in contexts[row["canonical_paper_key"]]["allowed_ref_ids"]):
            raise WriterTaskScopeError("selected Writer catalog contains foreign or incomplete source identity")
        observed_refs.append(row["ref_id"])
    if set(observed_refs) != refs or len(observed_refs) != len(refs):
        raise WriterTaskScopeError("selected Writer catalog references are missing or duplicated")
    return {
        "schema_version": _SELECTED_SOURCE_BINDING_VERSION,
        "paper_keys": sorted(papers),
        "source_projection_hash": compute_v3_hash({"task_sources": sources, "context_sources": context_sources}),
        "selected_catalog_hash": compute_v3_hash(catalog_entries),
    }


def _selected_task_basis(scope: Mapping[str, Any]) -> str:
    rows = [{key: deepcopy(task[key]) for key in _BASIS_TASK_FIELDS} for task in scope["tasks"]]
    if scope.get("schema_version") == "writer_task_scope/v2":
        # The existing table binder promotes optional expansion units. Reopen
        # the known base paragraph contract for its stored base-basis check;
        # the independent table verifier checks every promoted unit/layout.
        selected_units = set(scope["table_layout_binding"]["required_table_unit_ids"])
        for task in rows:
            for unit in task["output_units"]:
                required = unit["unit_kind"] == "planned_claim" or unit["writer_output_unit_id"] in selected_units
                if unit.get("required") is not required:
                    raise WriterTaskScopeError("selected Writer table promotion differs from its exact layout")
                unit["required"] = unit["unit_kind"] == "planned_claim"
    return compute_v3_hash({
        "version": _SELECTED_BASIS_VERSION,
        "section_id": scope["section_id"], "packet_bundle": scope["source_bundle"],
        "active_ref_mapping": scope["active_ref_mapping"],
        "selected_source_binding": scope["selected_source_binding"], "tasks": rows,
    })


def verify_writer_selected_source_scope_v2(scope: Mapping[str, Any], *,
                                          provider_semantic_contract_version: str | None = None) -> None:
    """Check selected scope integrity; Registry/source admission stays external."""
    if not _selected_source_contract(scope, provider_semantic_contract_version):
        return
    try:
        if scope.get("schema_version") not in (WRITER_TASK_SCOPE_VERSION, "writer_task_scope/v2"):
            raise WriterTaskScopeError("selected Writer scope schema is unsupported")
        current_binding = scope.get("source_inventory_binding")
        if (not isinstance(current_binding, Mapping) or current_binding.get("artifact_id") != SOURCE_INVENTORY_ARTIFACT_ID
                or any(not isinstance(current_binding.get(name), str)
                       or re.fullmatch(r"[0-9a-f]{64}", current_binding[name]) is None
                       for name in ("artifact_hash", "content_hash"))):
            raise WriterTaskScopeError("selected Writer scope lacks its complete current inventory authority binding")
        tasks = scope["tasks"]
        if not isinstance(tasks, list) or not tasks:
            raise WriterTaskScopeError("selected Writer task rows are absent")
        task_ids = [task["writer_task_id"] for task in tasks]
        max_units = sum(len(task["output_units"]) for task in tasks)
        min_units = sum(unit.get("required") is True for task in tasks for unit in task["output_units"])
        if (len(set(task_ids)) != len(tasks) or scope.get("required_task_ids") != task_ids
                or type(scope.get("task_count")) is not int or scope["task_count"] != len(tasks)
                or type(scope.get("max_output_units")) is not int or type(scope.get("min_required_output_units")) is not int
                or scope.get("max_output_units") != max_units or scope.get("min_required_output_units") != min_units):
            raise WriterTaskScopeError("selected Writer task identities or finite output limits differ")
        if scope.get("max_serialized_output_bytes_upper_bound") != _max_response_bytes(scope):
            raise WriterTaskScopeError("selected Writer serialized output limit differs")
        contexts = scope.get("selected_context_sources")
        expected = _selected_source_binding(tasks, scope.get("selected_catalog_entries"), contexts)
        assert isinstance(contexts, list)
        if (expected["paper_keys"] != sorted(scope["source_bundle"]["paper_keys"])
                or {row["paper_key"]: row["allowed_ref_ids"] for row in contexts} != scope["active_ref_mapping"]):
            raise WriterTaskScopeError("selected Writer context pins do not cover its complete packet/ref set")
        if scope.get("selected_source_binding") != expected:
            raise WriterTaskScopeError("selected Writer source binding changed")
        basis = _selected_task_basis(scope)
        recorded = scope.get("writer_task_basis_hash")
        if scope.get("schema_version") == "writer_task_scope/v2":
            from services.writer_table_layout import _verified_bound_scope

            _verified_bound_scope(scope)
            recorded = scope.get("base_writer_task_basis_hash")
        if recorded != basis:
            raise WriterTaskScopeError("selected Writer task basis changed")
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, WriterTaskScopeError):
            raise
        raise WriterTaskScopeError("selected Writer scope is incomplete or malformed") from exc


def build_writer_task_scope_v1(
    packet: Mapping[str, Any],
    catalog: Mapping[str, Any],
    *,
    source_inventory: VerifiedWriterSourceInventoryV1 | None = None,
    candidate_output_scope: VerifiedCandidateOutputScopeV1 | None = None,
    provider_semantic_contract_version: str | None = None,
) -> dict[str, Any]:
    """Build source-bound tasks; raw packet IDs alone never authorize Writer admission."""
    if not isinstance(packet, Mapping) or not isinstance(catalog, Mapping):
        raise WriterTaskScopeError("Writer scope requires a packet and citation catalog")
    if provider_semantic_contract_version not in (None, WRITER_SELECTED_SOURCE_CONTRACT):
        raise WriterTaskScopeError("Writer semantic contract is unsupported")
    section_id = _text(packet.get("section_id"))
    if not section_id:
        raise WriterTaskScopeError("Writer scope requires section_id")
    bundle = _packet_bundle(packet)
    refs_by_paper, ref_conflicts = _active_refs(catalog)
    raw_claims = _items(packet.get("planned_claims"))
    claims = [_text(value) for value in raw_claims if value is not None and _text(value)]
    counts, occurrence = Counter(claims), Counter()
    support = _items(packet.get("claim_support"))
    support_rows = [dict(row) for row in support if isinstance(row, Mapping)]
    valid_claims = set(claims)
    by_claim: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in support_rows:
        claim = _text(row.get("claim"))
        if claim in valid_claims:
            by_claim[claim].append(row)

    tasks: list[dict[str, Any]] = []
    for claim in claims:
        occurrence[claim] += 1
        duplicate = occurrence[claim] if counts[claim] > 1 else None
        tasks.append(_make_task(
            section_id, claim, by_claim.get(claim, []), packet=packet, bundle=bundle,
            refs_by_paper=refs_by_paper, ref_conflicts=ref_conflicts,
            source_inventory=source_inventory,
            candidate_output_scope=candidate_output_scope,
            duplicate_index=duplicate,
            forced_reasons=("duplicate_planned_claim_identity",) if duplicate else (),
        ))
    for value in raw_claims:
        if value is None or not _text(value):
            task = _make_task(
                section_id, "", [], packet=packet, bundle=bundle, refs_by_paper=refs_by_paper,
                ref_conflicts=ref_conflicts, source_inventory=source_inventory,
                candidate_output_scope=candidate_output_scope,
                duplicate_index=1, kind="malformed_planned_claim",
                forced_reasons=("empty_or_null_planned_claim",),
            )
            task["raw_planned_claim"] = value
            tasks.append(task)

    unmatched = [row for row in support_rows if _text(row.get("claim")) not in valid_claims]
    malformed = [row for row in support if not isinstance(row, Mapping)]
    orphan_occurrence: Counter[str] = Counter()
    for row in _stable_rows(unmatched):
        digest = compute_v3_hash(row)
        orphan_occurrence[digest] += 1
        tasks.append(_make_task(
            section_id, _text(row.get("claim")), [row], packet=packet, bundle=bundle,
            refs_by_paper=refs_by_paper, ref_conflicts=ref_conflicts,
            source_inventory=source_inventory,
            candidate_output_scope=candidate_output_scope,
            duplicate_index=orphan_occurrence[digest], kind="orphan_claim_support",
            forced_reasons=("support_without_planned_claim",),
        ))
    for index, row in enumerate(_stable_rows(malformed), start=1):
        task = _make_task(
            section_id, "", [], packet=packet, bundle=bundle, refs_by_paper=refs_by_paper,
            ref_conflicts=ref_conflicts, source_inventory=source_inventory,
            candidate_output_scope=candidate_output_scope,
            duplicate_index=index, kind="malformed_claim_support",
            forced_reasons=("malformed_claim_support",),
        )
        task["support_rows"] = [row]
        tasks.append(task)
    tasks.sort(key=lambda task: task["writer_task_id"])
    if len({task["writer_task_id"] for task in tasks}) != len(tasks):
        raise WriterTaskScopeError("Writer task identity collision requires explicit review")

    packet_papers = {_text(value) for value in _items(packet.get("paper_keys")) if _text(value)}
    ref_mapping = {paper: refs for paper, refs in sorted(refs_by_paper.items()) if paper in packet_papers}
    inventory_verified = bool(
        isinstance(source_inventory, VerifiedWriterSourceInventoryV1)
        and type(source_inventory) is VerifiedWriterSourceInventoryV1
        and source_inventory.is_verified
    )
    verified_inventory = source_inventory if inventory_verified else None
    inventory_binding = {
        "artifact_id": verified_inventory.artifact_id if verified_inventory is not None else "",
        "artifact_hash": verified_inventory.artifact_hash if verified_inventory is not None else "",
        "content_hash": verified_inventory.content_hash if verified_inventory is not None else "",
    }
    structural_ready = bool(tasks) and all(task["status"] == "ready" for task in tasks)
    authority_status = (
        "canonical_claim_and_evidence_inventory_verified"
        if inventory_verified and structural_ready
        else "canonical_claim_and_evidence_inventory_incomplete"
        if inventory_verified
        else "canonical_claim_and_evidence_inventory_not_verified"
    )
    basis = compute_v3_hash({
        "version": WRITER_TASK_SCOPE_VERSION,
        "section_id": section_id,
        "packet_bundle": bundle,
        "active_ref_mapping": ref_mapping,
        "source_inventory_binding": inventory_binding,
        "tasks": [{key: task[key] for key in (
            "writer_task_id", "task_kind", "planned_claim", "duplicate_index", "status",
            "reason_codes", "support_rows", "source_evidence", "canonical_source_bundle",
            "source_membership_status", "allowed_ref_ids", "output_units",
        )} for task in tasks],
    })
    scope = {
        "schema_version": WRITER_TASK_SCOPE_VERSION,
        "scope_status": "ready" if structural_ready else "needs_review",
        "source_authority_status": authority_status,
        "usable_for_provider_admission": inventory_verified and structural_ready,
        "section_id": section_id,
        "writer_task_basis_hash": basis,
        "source_inventory_binding": inventory_binding,
        "task_count": len(tasks),
        "required_task_ids": [task["writer_task_id"] for task in tasks],
        "tasks": tasks,
        "source_bundle": bundle,
        "active_ref_mapping": ref_mapping,
        "source_identity_validation": {
            "verified": [
                "claim, evidence, and field IDs match the canonical dossier under the packet paper and summary hash",
                "source ID ownership matches the source paper and study scope",
                "primary-claim interpretation dependencies and qualifier closure are complete",
            ] if inventory_verified else [],
            "limitation": (
                "canonical ID membership does not decide semantic truth; downstream validation remains required"
                if inventory_verified
                else "the raw section packet has no independent canonical claim/evidence authority"
            ),
        },
        "max_output_units": sum(len(task["output_units"]) for task in tasks),
        "min_required_output_units": sum(task["status"] == "ready" for task in tasks),
        "output_contract": {
            "block_fields": ["writer_task_id", "writer_output_unit_id", "writer_task_basis_hash", "text"],
            "disposition_fields": ["writer_task_id", "writer_task_basis_hash", "disposition", "reason_code_if_needs_review"],
            "text_limit_formula": "min(4096, max(160, 2 * distinct task source-prose characters + 160))",
            "max_text_utf8_bytes_per_unit": "4 * derived max_text_chars",
            "max_sentences_per_unit": 1,
            "requires_structured_citation": True,
            "expansion_policy": "one required claim unit plus one optional unit per distinct source claim member; all units carry full qualifier/source closure",
            "content_unit_kind": "paragraph",
            "table_bindings": "outside_v1; table rows and cells require equivalent source task bindings",
            "oversize_policy": "reject_without_truncation",
        },
    }
    if provider_semantic_contract_version is not None:
        if not inventory_verified or not structural_ready:
            raise WriterTaskScopeError("selected Writer scope requires complete verified source tasks")
        allowed_refs = {ref for refs in ref_mapping.values() for ref in refs}
        selected_catalog = [dict(row) for row in _items(catalog.get("entries"))
                            if isinstance(row, Mapping) and row.get("status") == "active"
                            and row.get("ref_id") in allowed_refs]
        scope["provider_semantic_contract_version"] = provider_semantic_contract_version
        scope["selected_catalog_entries"] = selected_catalog
        assert verified_inventory is not None
        papers_by_key = verified_inventory.paper_by_key()
        if packet_papers - set(papers_by_key) or packet_papers - set(ref_mapping):
            raise WriterTaskScopeError("selected Writer packet has an unknown or uncitable context source")
        scope["selected_context_sources"] = [{
            "paper_key": key, "source_summary_hash": papers_by_key[key].source_summary_hash,
            "source_summary_hashes": list(papers_by_key[key].source_summary_hashes),
            "dossier_content_hash": papers_by_key[key].dossier_content_hash,
            "evidence_view_hash": papers_by_key[key].evidence_view_hash,
            "dossier_status": papers_by_key[key].dossier_status,
            "diagnostics": list(papers_by_key[key].diagnostics), "allowed_ref_ids": ref_mapping[key],
        } for key in sorted(packet_papers)]
        scope["selected_source_binding"] = _selected_source_binding(tasks, selected_catalog, scope["selected_context_sources"])
        scope["writer_task_basis_hash"] = _selected_task_basis(scope)
    scope["max_serialized_output_bytes_upper_bound"] = _max_response_bytes(scope)
    return scope


def build_writer_output_maximum_specimen_v1(scope: Mapping[str, Any]) -> dict[str, Any]:
    """Build a schema-max paragraph response with every available unit emitted."""
    basis = _text(scope.get("writer_task_basis_hash"))
    if not re.fullmatch(r"[0-9a-f]{64}", basis):
        raise WriterTaskScopeError("Invalid writer_task_basis_hash")
    blocks, dispositions = [], []
    for task in scope.get("tasks", []):
        task_id = task["writer_task_id"]
        if task["status"] != "ready":
            dispositions.append({"writer_task_id": task_id, "writer_task_basis_hash": basis, "disposition": "needs_review", "reason_code": "source_scope_incomplete"})
            continue
        dispositions.append({"writer_task_id": task_id, "writer_task_basis_hash": basis, "disposition": "covered"})
        for unit in task["output_units"]:
            refs = unit["allowed_ref_ids"]
            if not refs:
                raise WriterTaskScopeError("Ready output unit has no allowed citation ref")
            citation = f"[[cite_ref:{refs[0]}]]"
            limit = unit["max_text_chars"]
            if len(citation) + 2 > limit:
                raise WriterTaskScopeError("Citation exceeds the configured text limit")
            text = "😀" * (limit - len(citation) - 1) + "." + citation
            blocks.append({
                "writer_task_id": task_id,
                "writer_output_unit_id": unit["writer_output_unit_id"],
                "writer_task_basis_hash": basis,
                "text": text,
            })
    return {"blocks": blocks, "task_dispositions": dispositions}


def validate_writer_task_output_v1(scope: Mapping[str, Any], payload: Mapping[str, Any], *,
                                  provider_semantic_contract_version: str | None = None) -> dict[str, Any]:
    """Reject missing/duplicate/foreign tasks, units, refs, sentences, or oversized text."""
    if not isinstance(scope, Mapping) or not isinstance(payload, Mapping):
        raise WriterTaskScopeError("Scope and Writer output must be objects")
    verify_writer_selected_source_scope_v2(scope, provider_semantic_contract_version=provider_semantic_contract_version)
    if scope.get("schema_version") == "writer_task_scope/v2":
        from services.writer_table_layout import _verified_bound_scope

        _verified_bound_scope(scope)
    elif scope.get("schema_version") != WRITER_TASK_SCOPE_VERSION:
        raise WriterTaskScopeError("Writer task scope schema version is unsupported")
    if set(payload) != {"blocks", "task_dispositions"}:
        raise WriterTaskScopeError("Writer output contains fields outside the finite response schema")
    basis = _text(scope.get("writer_task_basis_hash"))
    if not re.fullmatch(r"[0-9a-f]{64}", basis):
        raise WriterTaskScopeError("Invalid writer_task_basis_hash")
    tasks: dict[str, Mapping[str, Any]] = {}
    units: dict[tuple[str, str], Mapping[str, Any]] = {}
    for task in scope.get("tasks", []):
        task_id = _text(task.get("writer_task_id"))
        if not task_id or task_id in tasks:
            raise WriterTaskScopeError("Scope has missing or duplicate task identity")
        tasks[task_id] = task
        for unit in task.get("output_units", []):
            key = (task_id, _text(unit.get("writer_output_unit_id")))
            if not key[1] or key in units:
                raise WriterTaskScopeError("Scope has missing or duplicate output-unit identity")
            units[key] = unit
    if not tasks:
        raise WriterTaskScopeError("Scope has no planned or retained support tasks")

    dispositions: dict[str, Mapping[str, Any]] = {}
    raw_dispositions = payload.get("task_dispositions")
    if not isinstance(raw_dispositions, list):
        raise WriterTaskScopeError("Writer output must include task_dispositions")
    for row in raw_dispositions:
        if not isinstance(row, Mapping) or set(row) - {"writer_task_id", "writer_task_basis_hash", "disposition", "reason_code"}:
            raise WriterTaskScopeError("Invalid Writer task disposition schema")
        task_id = _text(row.get("writer_task_id"))
        if task_id not in tasks:
            raise WriterTaskScopeError(f"Foreign task identity {task_id!r}")
        if task_id in dispositions:
            raise WriterTaskScopeError(f"Repeated task disposition {task_id}")
        if _text(row.get("writer_task_basis_hash")) != basis:
            raise WriterTaskScopeError(f"Stale or partial task basis hash for {task_id}")
        disposition, reason = _text(row.get("disposition")), _text(row.get("reason_code"))
        if disposition not in {"covered", "needs_review"}:
            raise WriterTaskScopeError(f"Invalid disposition for {task_id}")
        if disposition == "needs_review" and (not reason or len(reason) > WRITER_REASON_CODE_MAX_CHARS or not re.fullmatch(r"[a-z][a-z0-9_]*", reason)):
            raise WriterTaskScopeError(f"Invalid needs_review reason for {task_id}")
        if disposition == "covered" and reason:
            raise WriterTaskScopeError(f"Covered task {task_id} cannot have a reason")
        dispositions[task_id] = row
    if set(dispositions) != set(tasks):
        missing = sorted(set(tasks) - set(dispositions))
        raise WriterTaskScopeError(f"Writer output omits task dispositions: {', '.join(missing)}")

    raw_blocks = payload.get("blocks")
    if not isinstance(raw_blocks, list) or len(raw_blocks) > int(scope.get("max_output_units") or 0):
        raise WriterTaskScopeError("Writer output exceeds its finite block envelope")
    used: set[tuple[str, str]] = set()
    blocks: list[dict[str, Any]] = []
    block_fields = {"writer_task_id", "writer_output_unit_id", "writer_task_basis_hash", "text"}
    for row in raw_blocks:
        if not isinstance(row, Mapping) or set(row) != block_fields:
            raise WriterTaskScopeError("Invalid Writer block schema")
        task_id, unit_id = _text(row.get("writer_task_id")), _text(row.get("writer_output_unit_id"))
        key = (task_id, unit_id)
        if task_id not in tasks:
            raise WriterTaskScopeError(f"Foreign task identity {task_id!r}")
        if key not in units:
            raise WriterTaskScopeError(f"Foreign output unit {unit_id!r}")
        if key in used:
            raise WriterTaskScopeError(f"Repeated output unit {unit_id}")
        used.add(key)
        if dispositions[task_id].get("disposition") != "covered" or tasks[task_id].get("status") != "ready":
            raise WriterTaskScopeError(f"Non-adoptable or needs-review task {task_id} has factual output")
        if _text(row.get("writer_task_basis_hash")) != basis:
            raise WriterTaskScopeError(f"Stale or partial block basis hash for {unit_id}")
        text, unit = row.get("text"), units[key]
        if not isinstance(text, str) or not text.strip():
            raise WriterTaskScopeError(f"Empty Writer output unit {unit_id}")
        if len(text) > int(unit["max_text_chars"]):
            raise WriterTaskScopeError(f"Output unit {unit_id} exceeds {unit['max_text_chars']} characters; no truncation is allowed")
        try:
            text_bytes = len(text.encode("utf-8"))
        except UnicodeEncodeError as exc:
            raise WriterTaskScopeError(f"Output unit {unit_id} is not valid UTF-8 text") from exc
        if text_bytes > int(unit["max_text_utf8_bytes"]):
            raise WriterTaskScopeError(f"Output unit {unit_id} exceeds {unit['max_text_utf8_bytes']} UTF-8 bytes")
        from services.writer_table_layout import _is_complete_pipe_table

        if _is_complete_pipe_table(text):
            raise WriterTaskScopeError(f"Output unit {unit_id} requires a bound table layout; raw Markdown tables are not paragraph units")
        if len(segment_sentences(text)) != 1:
            raise WriterTaskScopeError(f"Output unit {unit_id} must contain exactly one sentence")
        refs: list[str] = []
        for match in _CITATION_TOKEN.finditer(text):
            token_refs = extract_ref_ids_from_token(match.group(0))
            if not token_refs:
                raise WriterTaskScopeError(f"Output unit {unit_id} contains a malformed citation token")
            refs.extend(token_refs)
        refs = list(dict.fromkeys(refs))
        if not refs:
            raise WriterTaskScopeError(f"Output unit {unit_id} has no structured citation")
        foreign_refs = sorted(set(refs) - set(unit["allowed_ref_ids"]))
        if foreign_refs:
            raise WriterTaskScopeError(f"Output unit {unit_id} cites foreign refs: {', '.join(foreign_refs)}")
        blocks.append(dict(row))

    for task_id, task in tasks.items():
        disposition = dispositions[task_id].get("disposition")
        task_units = [key for key in units if key[0] == task_id]
        used_task_units = [key for key in used if key[0] == task_id]
        if disposition == "needs_review" and used_task_units:
            raise WriterTaskScopeError(f"Needs-review task {task_id} has factual output")
        if task.get("status") == "needs_review" and disposition != "needs_review":
            raise WriterTaskScopeError(f"Non-adoptable task {task_id} must remain needs_review")
        if disposition == "covered":
            primary = [key for key in task_units if units[key].get("unit_kind") == "planned_claim"]
            if len(primary) != 1 or primary[0] not in used:
                raise WriterTaskScopeError(f"Covered task {task_id} omits its required planned-claim unit")
            missing_required = [key for key in task_units if units[key].get("required") is True and key not in used]
            if missing_required:
                raise WriterTaskScopeError(f"Covered task {task_id} omits a required output unit")

    output_status = "ready" if all(row.get("disposition") == "covered" for row in dispositions.values()) else "needs_review"
    return {
        "schema_version": scope["schema_version"],
        "writer_task_basis_hash": basis,
        "scope_status": output_status,
        "source_authority_status": str(scope.get("source_authority_status") or "canonical_claim_and_evidence_inventory_not_verified"),
        "source_inventory_binding": dict(scope.get("source_inventory_binding") or {}),
        "usable_for_provider_admission": bool(scope.get("usable_for_provider_admission") is True and output_status == "ready"),
        "blocks": blocks,
        "task_dispositions": [dict(dispositions[key]) for key in sorted(dispositions)],
        "block_count": len(blocks),
        "max_output_units": int(scope.get("max_output_units") or 0),
    }
