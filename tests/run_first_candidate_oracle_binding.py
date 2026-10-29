"""Registry-bound proof that one exact candidate claim is single-source scoped."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from hashlib import sha256
import json
import os
from pathlib import Path
from typing import Any

from outline.candidate_fragment_bank import (
    validate_candidate_fragment_bank_intrinsic_v1,
    validate_candidate_fragment_bank_v1,
)
from outline.candidate_output_scope import (
    CandidateOutputClaimGroupV1,
    CandidateOutputScopeError,
    CandidateOutputScopeV1,
    CandidateOutputSupportSlotV1,
    _canonicalize_claim_lineage,
    _claim_group,
    _inventory_indexes,
    _source_closure,
    validate_candidate_output_scope_intrinsic_v1,
)
from runtime.provider_runtime import hash_json
from services.artifact_registry import ArtifactDependencyRefV2, ArtifactRecord
from services.writer_source_inventory import load_writer_source_inventory_v1

PROOF_SCHEMA_VERSION = "verified-single-source-candidate-bindings/v1"
_BINDING_BASIS = "adopted_outline_registry_closure_exact_bank_scope_slots/v1"
_VERIFIED_SEAL = object()


class CandidateOracleBindingError(ValueError):
    """The adopted Outline cannot establish exact Registry source bindings."""


@dataclass(frozen=True, slots=True)
class _SingleSourceBinding:
    section_index: int
    claim_index: int
    exact_text: str = field(repr=False)
    text_sha256: str
    support_rows_sha256: str
    paper_key: str
    bank_group_ids: tuple[str, ...]
    claim_slot_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _SharedWholeAssertionBinding:
    section_index: int
    claim_index: int
    exact_text: str = field(repr=False)
    text_sha256: str
    support_rows_sha256: str
    paper_keys: tuple[str, ...]
    bank_group_ids: tuple[str, ...]
    claim_slot_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class VerifiedSingleSourceCandidateBindings:
    """Private-seal token accepted only when issued by the Registry verifier."""

    adopted_outline_artifact_id: str
    adopted_outline_artifact_hash: str
    final_outline_artifact_id: str
    final_outline_artifact_hash: str
    fragment_bank_artifact_id: str
    fragment_bank_artifact_hash: str
    fragment_bank_content_hash: str
    candidate_scope_artifact_id: str
    candidate_scope_artifact_hash: str
    candidate_scope_hash: str
    source_inventory_artifact_id: str
    source_inventory_artifact_hash: str
    source_inventory_content_hash: str
    _bindings: tuple[_SingleSourceBinding, ...] = field(default=(), repr=False)
    _shared_bindings: tuple[_SharedWholeAssertionBinding, ...] = field(default=(), repr=False)
    _rejected_occurrences: tuple[tuple[int, int, str], ...] = field(default=(), repr=False)
    _seal: object = field(default=None, repr=False, compare=False)

    @property
    def is_verified(self) -> bool:
        return self._seal is _VERIFIED_SEAL

    def matches(
        self,
        section_index: int,
        claim_index: int,
        exact_text: str,
        support_rows: Sequence[Mapping[str, Any]],
    ) -> str | None:
        """Return one paper key only for the exact verified occurrence and rows."""
        if not self.is_verified or type(section_index) is not int or type(claim_index) is not int:
            return None
        rows_hash = _support_rows_sha256(support_rows)
        if rows_hash is None:
            return None
        matches = [
            item for item in self._bindings
            if item.section_index == section_index and item.claim_index == claim_index
        ]
        if len(matches) != 1:
            return None
        binding = matches[0]
        if exact_text != binding.exact_text or _text_sha256(exact_text) != binding.text_sha256:
            return None
        if rows_hash != binding.support_rows_sha256:
            return None
        return binding.paper_key

    def matches_shared(
        self,
        section_index: int,
        claim_index: int,
        exact_text: str,
        support_rows: Sequence[Mapping[str, Any]],
    ) -> tuple[str, ...] | None:
        """Return source keys for one exact claim backed by independent groups."""
        if not self.is_verified or type(section_index) is not int or type(claim_index) is not int:
            return None
        rows_hash = _support_rows_sha256(support_rows)
        if rows_hash is None:
            return None
        matches = [
            item for item in self._shared_bindings
            if item.section_index == section_index and item.claim_index == claim_index
        ]
        if len(matches) != 1:
            return None
        binding = matches[0]
        if exact_text != binding.exact_text or _text_sha256(exact_text) != binding.text_sha256:
            return None
        if rows_hash != binding.support_rows_sha256:
            return None
        return binding.paper_keys

    def to_dict(self) -> dict[str, Any]:
        """Return hashes and Registry identities without copying source prose."""
        return {
            "schema_version": PROOF_SCHEMA_VERSION,
            "is_verified": self.is_verified,
            "binding_basis": _BINDING_BASIS,
            "adopted_outline": {
                "artifact_id": self.adopted_outline_artifact_id,
                "content_hash": self.adopted_outline_artifact_hash,
            },
            "final_outline": {
                "artifact_id": self.final_outline_artifact_id,
                "content_hash": self.final_outline_artifact_hash,
            },
            "fragment_bank": {
                "artifact_id": self.fragment_bank_artifact_id,
                "content_hash": self.fragment_bank_artifact_hash,
                "bank_content_hash": self.fragment_bank_content_hash,
            },
            "candidate_scope": {
                "artifact_id": self.candidate_scope_artifact_id,
                "content_hash": self.candidate_scope_artifact_hash,
                "scope_hash": self.candidate_scope_hash,
            },
            "source_inventory": {
                "artifact_id": self.source_inventory_artifact_id,
                "artifact_hash": self.source_inventory_artifact_hash,
                "content_hash": self.source_inventory_content_hash,
            },
            "bindings": [
                {
                    "section_index": item.section_index,
                    "claim_index": item.claim_index,
                    "claim_text_sha256": item.text_sha256,
                    "support_rows_sha256": item.support_rows_sha256,
                    "paper_key": item.paper_key,
                    "bank_group_ids": list(item.bank_group_ids),
                    "claim_slot_ids": list(item.claim_slot_ids),
                }
                for item in self._bindings
            ],
            "shared_bindings": [
                {
                    "section_index": item.section_index,
                    "claim_index": item.claim_index,
                    "claim_text_sha256": item.text_sha256,
                    "support_rows_sha256": item.support_rows_sha256,
                    "paper_keys": list(item.paper_keys),
                    "bank_group_ids": list(item.bank_group_ids),
                    "claim_slot_ids": list(item.claim_slot_ids),
                }
                for item in self._shared_bindings
            ],
            "rejected_occurrences": [
                {"section_index": section, "claim_index": claim, "reason": reason}
                for section, claim, reason in self._rejected_occurrences
            ],
        }


def _text_sha256(text: str) -> str:
    return sha256(text.encode("utf-8")).hexdigest()


def _support_rows_sha256(rows: Any) -> str | None:
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes, bytearray)) or not rows:
        return None
    normalized: list[dict[str, Any]] = []
    slot_ids: list[str] = []
    try:
        for row in rows:
            if not isinstance(row, Mapping):
                return None
            item = dict(row)
            slot_id = str(item.get("claim_slot_id") or "")
            if not slot_id:
                return None
            slot_ids.append(slot_id)
            normalized.append(item)
        if len(slot_ids) != len(set(slot_ids)):
            return None
        normalized.sort(key=lambda item: str(item["claim_slot_id"]))
        return hash_json(normalized)
    except (TypeError, ValueError, KeyError):
        return None


def _path_identity(value: str | os.PathLike[str]) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(value)))


def _record_identity(record: ArtifactRecord) -> tuple[str, str, str, str, str, str]:
    return (
        str(record.job_id), str(record.artifact_id), str(record.artifact_type),
        str(record.artifact_version), _path_identity(record.path), str(record.content_hash),
    )


def _verify_record_closure(registry: Any, record: ArtifactRecord, label: str) -> ArtifactRecord:
    try:
        verified = registry.verify_ready_artifact_closure(record)
    except Exception as exc:
        raise CandidateOracleBindingError(f"{label} Registry closure is not ready") from exc
    if not isinstance(verified, ArtifactRecord) or _record_identity(verified) != _record_identity(record):
        raise CandidateOracleBindingError(f"{label} changed identity during Registry verification")
    return verified


def _read_record_json(record: ArtifactRecord, label: str) -> dict[str, Any]:
    path = Path(record.path)
    try:
        before = path.read_bytes()
        before_hash = sha256(before).hexdigest()
        if before_hash != record.content_hash:
            raise CandidateOracleBindingError(f"{label} bytes differ from the exact Registry hash")
        value = json.loads(before.decode("utf-8"))
        after_hash = sha256(path.read_bytes()).hexdigest()
    except CandidateOracleBindingError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CandidateOracleBindingError(f"{label} cannot be read as exact Registry JSON") from exc
    if after_hash != before_hash:
        raise CandidateOracleBindingError(f"{label} changed while it was being read")
    if not isinstance(value, Mapping):
        raise CandidateOracleBindingError(f"{label} JSON root is not an object")
    return dict(value)


def _exact_local_dependency(
    ref: ArtifactDependencyRefV2,
    records_by_id: Mapping[str, Sequence[ArtifactRecord]],
    *,
    owner_job_id: str,
    label: str,
) -> ArtifactRecord:
    if ref.dependency_kind != "local_job":
        raise CandidateOracleBindingError(f"{label} is not a local Registry dependency")
    if (
        ref.job_id != owner_job_id or not ref.artifact_id or not ref.artifact_type
        or not ref.path or not ref.content_hash
    ):
        raise CandidateOracleBindingError(f"{label} dependency is missing exact job/path/hash identity")
    candidates = [
        row for row in records_by_id.get(ref.artifact_id, ())
        if row.job_id == ref.job_id
        and row.artifact_type == ref.artifact_type
        and _path_identity(row.path) == _path_identity(ref.path)
        and row.content_hash == ref.content_hash
    ]
    if len(candidates) != 1:
        raise CandidateOracleBindingError(f"{label} exact dependency has no unique Registry record")
    return candidates[0]


def _walk_exact_local_closure(
    root: ArtifactRecord,
    records: Sequence[ArtifactRecord],
) -> tuple[ArtifactRecord, ...]:
    records_by_id: dict[str, list[ArtifactRecord]] = {}
    for row in records:
        records_by_id.setdefault(row.artifact_id, []).append(row)
    visited: dict[tuple[str, str, str, str, str, str], ArtifactRecord] = {}
    pending = [root]
    while pending:
        record = pending.pop()
        identity = _record_identity(record)
        if identity in visited:
            continue
        visited[identity] = record
        for raw_ref in record.depends_on:
            if not isinstance(raw_ref, ArtifactDependencyRefV2):
                raise CandidateOracleBindingError("Registry dependency row is not a typed exact reference")
            if raw_ref.dependency_kind == "external_job":
                continue
            dependency = _exact_local_dependency(
                raw_ref, records_by_id, owner_job_id=root.job_id,
                label=f"{record.artifact_id}->{raw_ref.artifact_id}",
            )
            pending.append(dependency)
    return tuple(visited.values())


def _scope_from_payload(payload: Mapping[str, Any]) -> CandidateOutputScopeV1:
    try:
        validate_candidate_output_scope_intrinsic_v1(payload)
        binding = payload["source_inventory_binding"]
        groups: list[CandidateOutputClaimGroupV1] = []
        for row in payload["claim_groups"]:
            groups.append(CandidateOutputClaimGroupV1(
                claim_group_id=row["claim_group_id"], task_id=row["task_id"],
                topic_id=row["topic_id"], fragment_id=row["fragment_id"],
                provider_result_id=row["provider_result_id"], synthesis_claim_id=row["synthesis_claim_id"],
                claim_hash=row["claim_hash"], paper_keys=tuple(row["paper_keys"]),
                support_slot_ids=tuple(row["support_slot_ids"]), relation_ids=tuple(row["relation_ids"]),
                claim_kind=row.get("claim_kind", "provider_authored_source_closed_claim"),
                claim_seed=row.get("claim_seed", False),
                task_contract=tuple((row.get("task_contract") or {}).items()),
            ))
        slots = tuple(CandidateOutputSupportSlotV1(
            claim_slot_id=row["claim_slot_id"], claim_group_id=row["claim_group_id"],
            task_id=row["task_id"], topic_id=row["topic_id"], fragment_id=row["fragment_id"],
            provider_result_id=row["provider_result_id"], synthesis_claim_id=row["synthesis_claim_id"],
            paper_key=row["paper_key"], primary_claim_id=row["primary_claim_id"],
            study_id=row["study_id"], source_claim_ids=tuple(row["source_claim_ids"]),
            evidence_ids=tuple(row["evidence_ids"]), source_field_ids=tuple(row["source_field_ids"]),
            relation_ids=tuple(row["relation_ids"]),
        ) for row in payload["claim_slots"])
        scope = CandidateOutputScopeV1(
            registry_job_id=binding["registry_job_id"],
            source_inventory_artifact_id=binding["artifact_id"],
            source_inventory_artifact_hash=binding["artifact_hash"],
            source_inventory_content_hash=binding["content_hash"],
            task_ids=tuple(payload["task_ids"]),
            selected_relation_ids=tuple(payload["selected_relation_ids"]),
            claim_groups=tuple(groups), claim_slots=slots,
        )
    except (CandidateOracleBindingError, CandidateOutputScopeError):
        raise
    except (TypeError, ValueError, KeyError) as exc:
        raise CandidateOracleBindingError("candidate scope cannot be reconstructed from its exact schema") from exc
    if scope.to_dict() != dict(payload):
        raise CandidateOracleBindingError("candidate scope reconstruction changed its exact payload")
    return scope


def _verify_scope_sources_against_inventory(
    scope: CandidateOutputScopeV1,
    bank: Mapping[str, Any],
    inventory: Any,
) -> None:
    """Recompute every slot/group closure from the verified source inventory."""
    try:
        indexes = _inventory_indexes(inventory)
        groups = {group.claim_group_id: group for group in scope.claim_groups}
        entries = {
            str(row["claim_group"]["claim_group_id"]): row
            for row in bank["groups"]
            if isinstance(row, Mapping) and isinstance(row.get("claim_group"), Mapping)
        }
        canonical_graph: dict[str, Mapping[str, Any]] = {}
        for entry in entries.values():
            claim = entry.get("canonical_claim")
            if not isinstance(claim, Mapping):
                continue
            claim_id = str(claim.get("claim_id") or "")
            if not claim_id:
                raise CandidateOracleBindingError("bank canonical claim has no synthesis identity")
            prior = canonical_graph.get(claim_id)
            if prior is not None and hash_json(dict(prior)) != hash_json(dict(claim)):
                raise CandidateOracleBindingError("bank has conflicting canonical claims with one identity")
            canonical_graph[claim_id] = dict(claim)

        for group_id, group in groups.items():
            entry = entries.get(group_id)
            if entry is None:
                raise CandidateOracleBindingError("bank omits a candidate scope group")
            local_slots = [slot for slot in scope.claim_slots if slot.claim_group_id == group_id]
            expected_slots = sorted((slot.to_dict() for slot in local_slots), key=lambda row: row["claim_slot_id"])
            if entry.get("support_slots") != expected_slots:
                raise CandidateOracleBindingError("bank group support differs from exact scope slots")
            if group.claim_seed:
                for slot in local_slots:
                    paper = indexes["papers"].get(slot.paper_key)
                    if paper is None:
                        raise CandidateOracleBindingError("seed slot paper is absent from verified inventory")
                    source_ids, evidence_ids, closure_fields, study_id = _source_closure(
                        paper, slot.primary_claim_id, indexes,
                    )
                    # Relation seed slots additionally bind direct source fields
                    # whose values exactly equal their own verified evidence text.
                    expected_fields = set(closure_fields)
                    fields_by_paper = indexes["fields"].get(slot.paper_key, {})
                    evidence_by_paper = indexes["evidence"].get(slot.paper_key, {})
                    for evidence_id in evidence_ids:
                        evidence = evidence_by_paper.get(evidence_id)
                        evidence_text = str(evidence.text or "").strip() if evidence is not None else ""
                        if not evidence_text:
                            continue
                        normalized_text = evidence_text.casefold()
                        for source_field in fields_by_paper.values():
                            values = {
                                str(source_field.source_value or "").strip().casefold(),
                                str(source_field.derived_value or "").strip().casefold(),
                            }
                            if normalized_text not in values:
                                continue
                            owners = {str(value) for value in source_field.owner_study_ids if str(value)}
                            if owners and (not study_id or not owners.issubset({study_id})):
                                continue
                            if source_field.scope == "explicit_study" and (
                                not owners or not study_id or study_id not in owners
                            ):
                                continue
                            expected_fields.add(source_field.source_field_id)
                    if (
                        source_ids != slot.source_claim_ids
                        or evidence_ids != slot.evidence_ids
                        or expected_fields != set(slot.source_field_ids)
                        or study_id != slot.study_id
                    ):
                        raise CandidateOracleBindingError("comparison seed slot differs from actual source closure")
                continue

            claim = entry.get("canonical_claim")
            if not isinstance(claim, Mapping) or entry.get("entry_kind") != "canonical_claim":
                raise CandidateOracleBindingError("non-seed bank group lacks its canonical source claim")
            normalized = _canonicalize_claim_lineage(claim, canonical_graph, indexes)
            if normalized != dict(claim) or hash_json(normalized) != group.claim_hash:
                raise CandidateOracleBindingError("bank canonical claim is not the exact source-normalized scope claim")
            computed_group, computed_slots = _claim_group(
                inventory=inventory,
                indexes=indexes,
                task_id=group.task_id,
                topic_id=group.topic_id,
                fragment_id=group.fragment_id,
                provider_result_id=group.provider_result_id,
                claim=normalized,
                selected_relation_ids=scope.selected_relation_ids,
                task_paper_keys=group.paper_keys,
            )
            if computed_group.to_dict() != group.to_dict():
                raise CandidateOracleBindingError("canonical bank claim group differs from verified inventory closure")
            rebuilt_slots = sorted((slot.to_dict() for slot in computed_slots), key=lambda row: row["claim_slot_id"])
            if rebuilt_slots != expected_slots:
                raise CandidateOracleBindingError("canonical bank support arrays differ from actual source/qualifier closure")
    except CandidateOracleBindingError:
        raise
    except (CandidateOutputScopeError, TypeError, ValueError, KeyError) as exc:
        raise CandidateOracleBindingError("candidate scope source membership/qualifier closure failed") from exc


def _candidate_support_projection(text: str, slot: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "claim_slot_id": slot["claim_slot_id"],
        "task_id": slot["task_id"],
        "claim": text,
        "paper_key": slot["paper_key"],
        "primary_claim_id": slot["primary_claim_id"],
        "source_claim_ids": list(slot["source_claim_ids"]),
        "evidence_ids": list(slot["evidence_ids"]),
        "source_field_ids": list(slot["source_field_ids"]),
        "study_id": slot["study_id"],
    }


def _candidate_claim_text(raw_claim: Any) -> str:
    if isinstance(raw_claim, Mapping):
        value = raw_claim.get("text") if raw_claim.get("text") is not None else raw_claim.get("claim")
        return value if isinstance(value, str) else ""
    return raw_claim if isinstance(raw_claim, str) else ""


def _bind_claim_occurrence(
    section_index: int,
    claim_index: int,
    exact_text: str,
    raw_support_rows: Any,
    entries_by_slot: Mapping[str, tuple[Mapping[str, Any], Mapping[str, Any]]],
    section_paper_keys: Any,
) -> tuple[_SingleSourceBinding | _SharedWholeAssertionBinding | None, str | None]:
    if not isinstance(raw_support_rows, Sequence) or isinstance(raw_support_rows, (str, bytes, bytearray)):
        return None, "claim_support_not_array"
    support_rows = [row for row in raw_support_rows
                    if isinstance(row, Mapping) and row.get("claim") == exact_text]
    if not support_rows:
        return None, "claim_support_missing"
    normalized_rows: list[dict[str, Any]] = []
    selected_groups: dict[str, Mapping[str, Any]] = {}
    selected_slot_ids: list[str] = []
    paper_keys: set[str] = set()
    for raw in support_rows:
        row = dict(raw)
        slot_id = str(row.get("claim_slot_id") or "")
        if not slot_id or slot_id in selected_slot_ids:
            return None, "support_slot_missing_or_duplicated"
        pair = entries_by_slot.get(slot_id)
        if pair is None:
            return None, "support_slot_not_in_verified_bank"
        entry, slot = pair
        group_id = str(entry.get("claim_group", {}).get("claim_group_id") or "")
        if (
            entry.get("materialization_state") != "materialized"
            or entry.get("text") != exact_text
            or not group_id
            or row != _candidate_support_projection(exact_text, slot)
        ):
            return None, "support_row_differs_from_exact_bank_slot"
        paper_key = str(slot.get("paper_key") or "")
        if not paper_key:
            return None, "bank_slot_source_identity_missing"
        paper_keys.add(paper_key)
        selected_groups[group_id] = entry
        selected_slot_ids.append(slot_id)
        normalized_rows.append(row)
    if not isinstance(section_paper_keys, Sequence) or isinstance(section_paper_keys, (str, bytes, bytearray)):
        return None, "section_paper_keys_missing"
    declared_section_papers = {str(value) for value in section_paper_keys if str(value)}
    if not paper_keys.issubset(declared_section_papers):
        return None, "support_source_not_declared_by_section"
    for group_id, entry in selected_groups.items():
        expected_slots = {
            str(slot.get("claim_slot_id") or "")
            for slot in entry.get("support_slots") or ()
        }
        observed_slots = {
            str(row.get("claim_slot_id") or "")
            for row in normalized_rows
            if str(row.get("claim_slot_id") or "") in entries_by_slot
            and entries_by_slot[str(row["claim_slot_id"])][0]["claim_group"]["claim_group_id"] == group_id
        }
        if not expected_slots or observed_slots != expected_slots:
            return None, "support_slots_do_not_cover_complete_bank_group"
    rows_hash = _support_rows_sha256(normalized_rows)
    if rows_hash is None:
        return None, "support_rows_hash_invalid"
    if len(paper_keys) > 1:
        if len(selected_groups) < 2 or len(selected_groups) != len(paper_keys):
            return None, "shared_support_requires_independent_single_source_groups"
        for group_id, entry in selected_groups.items():
            group = entry.get("claim_group")
            group_papers = (
                {str(value) for value in group.get("paper_keys", ()) if str(value)}
                if isinstance(group, Mapping) else set()
            )
            slot_papers = {
                str(slot.get("paper_key") or "")
                for slot in entry.get("support_slots") or ()
                if isinstance(slot, Mapping)
            }
            if (
                entry.get("entry_kind") != "canonical_claim"
                or not isinstance(group, Mapping)
                or group.get("claim_seed") is True
                or group_papers != slot_papers
                or len(group_papers) != 1
            ):
                return None, "shared_support_requires_independent_single_source_groups"
        return _SharedWholeAssertionBinding(
            section_index=section_index, claim_index=claim_index, exact_text=exact_text,
            text_sha256=_text_sha256(exact_text), support_rows_sha256=rows_hash,
            paper_keys=tuple(sorted(paper_keys)),
            bank_group_ids=tuple(sorted(selected_groups)),
            claim_slot_ids=tuple(sorted(selected_slot_ids)),
        ), None
    if len(paper_keys) != 1:
        return None, "support_spans_multiple_source_papers"
    return _SingleSourceBinding(
        section_index=section_index, claim_index=claim_index, exact_text=exact_text,
        text_sha256=_text_sha256(exact_text), support_rows_sha256=rows_hash,
        paper_key=next(iter(paper_keys)),
        bank_group_ids=tuple(sorted(selected_groups)),
        claim_slot_ids=tuple(sorted(selected_slot_ids)),
    ), None


def verify_single_source_candidate_bindings(
    registry: Any,
    exact_outline_record: ArtifactRecord,
) -> VerifiedSingleSourceCandidateBindings:
    """Verify one adopted Outline through its exact local bank/scope closure.

    Only whole claim occurrences whose complete same-paper support rows equal
    the exact materialized bank slots receive a mapping. This grants no facts
    from text alone; callers still must pass the unchanged claim to the PDF
    evaluator bound to the returned paper key.
    """
    if not isinstance(exact_outline_record, ArtifactRecord):
        raise CandidateOracleBindingError("exact adopted Outline must be a Registry ArtifactRecord")
    if (
        exact_outline_record.artifact_type != "adopted_outline"
        or exact_outline_record.artifact_version != "v3"
        or exact_outline_record.status != "ready"
        or exact_outline_record.job_id != getattr(registry, "job_id", None)
    ):
        raise CandidateOracleBindingError("root is not the exact ready adopted Outline for this Registry")
    adopted_record = _verify_record_closure(registry, exact_outline_record, "adopted Outline")
    adopted_doc = _read_record_json(adopted_record, "adopted Outline")
    adopted_payload = adopted_doc.get("payload")
    if (
        not isinstance(adopted_payload, Mapping)
        or adopted_payload.get("status") != "adopted"
        or adopted_payload.get("adoption_id") != adopted_record.artifact_id
        or adopted_payload.get("adoption_identity") != adopted_record.artifact_id
    ):
        raise CandidateOracleBindingError("adopted Outline identity is invalid")
    final_refs = [
        ref for ref in adopted_record.depends_on
        if isinstance(ref, ArtifactDependencyRefV2) and ref.artifact_type == "final_outline"
    ]
    if len(final_refs) != 1:
        raise CandidateOracleBindingError("adopted Outline must bind one exact final_outline dependency")
    final_ref = final_refs[0]
    final_hash = str(adopted_payload.get("final_outline_hash") or "")
    if (
        not final_hash or final_hash != str(adopted_payload.get("expected_hash") or "")
        or final_ref.content_hash != final_hash
        or adopted_doc.get("dependency_hashes", {}).get(final_ref.artifact_id) != final_hash
    ):
        raise CandidateOracleBindingError("adopted Outline does not bind the expected final_outline hash")
    records = registry.list_records()
    records_by_id: dict[str, list[ArtifactRecord]] = {}
    for row in records:
        records_by_id.setdefault(row.artifact_id, []).append(row)
    final_record = _exact_local_dependency(
        final_ref, records_by_id, owner_job_id=adopted_record.job_id,
        label="adopted Outline final_outline",
    )
    final_doc = _read_record_json(final_record, "final_outline")
    final_payload = final_doc.get("payload")
    if (
        final_record.artifact_type != "final_outline"
        or final_record.artifact_version != "v3"
        or not isinstance(final_payload, Mapping)
        or dict(final_payload) != adopted_payload.get("final_outline")
    ):
        raise CandidateOracleBindingError("adopted Outline embeds a different final_outline payload")
    closure_records = _walk_exact_local_closure(final_record, records)
    bank_records = [row for row in closure_records if row.artifact_type == "outline_candidate_fragment_bank"]
    scope_records = [row for row in closure_records if row.artifact_type == "outline_candidate_output_scope"]
    if len(bank_records) != 1 or len(scope_records) != 1:
        raise CandidateOracleBindingError("final_outline closure must contain one exact fragment bank and scope")
    bank_record, scope_record = bank_records[0], scope_records[0]
    if bank_record.artifact_version != "v1" or scope_record.artifact_version != "v1":
        raise CandidateOracleBindingError("candidate fragment bank/scope versions are unsupported")
    bank_doc = _read_record_json(bank_record, "candidate fragment bank")
    scope_doc = _read_record_json(scope_record, "candidate output scope")
    scope = _scope_from_payload(scope_doc)
    try:
        validate_candidate_fragment_bank_intrinsic_v1(bank_doc)
        validate_candidate_fragment_bank_v1(bank_doc, scope)
    except (TypeError, ValueError, KeyError) as exc:
        raise CandidateOracleBindingError("candidate fragment bank is not bound to the exact scope") from exc
    scope_binding = scope_doc["source_inventory_binding"]
    if (
        scope.registry_job_id != registry.job_id
        or scope_binding.get("registry_job_id") != registry.job_id
        or bank_doc.get("scope_binding", {}).get("registry_job_id") != registry.job_id
        or bank_doc.get("scope_binding", {}).get("scope_hash") != scope.content_hash
    ):
        raise CandidateOracleBindingError("candidate bank/scope job or hash binding differs")
    bank_scope_refs = [
        ref for ref in bank_record.depends_on
        if isinstance(ref, ArtifactDependencyRefV2)
        and ref.artifact_id == scope_record.artifact_id
        and ref.content_hash == scope_record.content_hash
        and ref.job_id == scope_record.job_id
        and _path_identity(ref.path) == _path_identity(scope_record.path)
    ]
    if len(bank_scope_refs) != 1:
        raise CandidateOracleBindingError("fragment bank must bind the exact candidate output scope")
    scope_inventory_refs = [
        ref for ref in scope_record.depends_on
        if isinstance(ref, ArtifactDependencyRefV2)
        and ref.artifact_id == scope_binding.get("artifact_id")
    ]
    if len(scope_inventory_refs) != 1:
        raise CandidateOracleBindingError("candidate scope must bind exactly one source inventory dependency")
    inventory_record = _exact_local_dependency(
        scope_inventory_refs[0], records_by_id,
        owner_job_id=registry.job_id, label="candidate scope source inventory",
    )
    if (
        scope_binding.get("artifact_hash") != inventory_record.content_hash
        or scope_binding.get("artifact_id") != inventory_record.artifact_id
    ):
        raise CandidateOracleBindingError("candidate scope source inventory Registry identity differs")
    inventory_doc = _read_record_json(inventory_record, "source inventory")
    inventory_payload = inventory_doc.get("payload")
    inventory_content_hash = (
        inventory_payload.get("content_hash") if isinstance(inventory_payload, Mapping)
        else inventory_doc.get("content_hash") or inventory_doc.get("inventory_hash")
    )
    if inventory_content_hash != scope_binding.get("content_hash"):
        raise CandidateOracleBindingError("candidate scope source inventory content hash differs")
    try:
        verified_inventory = load_writer_source_inventory_v1(registry)
    except Exception as exc:
        raise CandidateOracleBindingError("canonical source inventory READY closure failed") from exc
    if (
        not verified_inventory.is_verified
        or verified_inventory.registry_job_id != registry.job_id
        or verified_inventory.artifact_id != inventory_record.artifact_id
        or verified_inventory.artifact_hash != inventory_record.content_hash
        or verified_inventory.content_hash != scope_binding.get("content_hash")
    ):
        raise CandidateOracleBindingError("canonical source inventory is not the exact bound inventory")
    _verify_scope_sources_against_inventory(scope, bank_doc, verified_inventory)

    entries_by_slot: dict[str, tuple[Mapping[str, Any], Mapping[str, Any]]] = {}
    for entry in bank_doc.get("groups") or ():
        if not isinstance(entry, Mapping):
            raise CandidateOracleBindingError("candidate fragment bank contains a malformed group")
        slots = entry.get("support_slots") or ()
        if not isinstance(slots, list):
            raise CandidateOracleBindingError("candidate fragment bank support slots are malformed")
        for slot in slots:
            if not isinstance(slot, Mapping):
                raise CandidateOracleBindingError("candidate fragment bank has a malformed support slot")
            slot_id = str(slot.get("claim_slot_id") or "")
            if not slot_id or slot_id in entries_by_slot:
                raise CandidateOracleBindingError("candidate fragment bank repeats a support slot")
            entries_by_slot[slot_id] = (entry, slot)

    bindings: list[_SingleSourceBinding] = []
    shared_bindings: list[_SharedWholeAssertionBinding] = []
    rejected: list[tuple[int, int, str]] = []
    sections = final_payload.get("sections")
    if not isinstance(sections, list):
        raise CandidateOracleBindingError("final_outline sections are not an array")
    for section_index, section in enumerate(sections):
        if not isinstance(section, Mapping):
            rejected.append((section_index, -1, "section_not_object"))
            continue
        raw_claims = section.get("claims") or ()
        if not isinstance(raw_claims, Sequence) or isinstance(raw_claims, (str, bytes, bytearray)):
            rejected.append((section_index, -1, "claims_not_array"))
            continue
        raw_support_rows = section.get("claim_support") or ()
        if not isinstance(raw_support_rows, Sequence) or isinstance(raw_support_rows, (str, bytes, bytearray)):
            raw_support_rows = ()
        for claim_index, raw_claim in enumerate(raw_claims):
            exact_text = _candidate_claim_text(raw_claim)
            if not exact_text:
                rejected.append((section_index, claim_index, "claim_text_missing"))
                continue
            binding, reason = _bind_claim_occurrence(
                section_index, claim_index, exact_text, raw_support_rows, entries_by_slot,
                section.get("paper_keys"),
            )
            if binding is None:
                rejected.append((section_index, claim_index, reason or "source_binding_missing"))
                continue
            if isinstance(binding, _SharedWholeAssertionBinding):
                shared_bindings.append(binding)
            else:
                bindings.append(binding)

    proof_records = (adopted_record, final_record, bank_record, scope_record, inventory_record)
    try:
        # The root closure rechecks every transitive dependency in one Registry
        # snapshot; byte reads then pin the exact inputs used to build the token.
        _verify_record_closure(registry, adopted_record, "adopted Outline final recheck")
        for record in proof_records[1:]:
            _read_record_json(record, f"{record.artifact_type} final recheck")
        _read_record_json(adopted_record, "adopted Outline final recheck")
    except CandidateOracleBindingError:
        raise
    except Exception as exc:
        raise CandidateOracleBindingError("Registry proof artifacts changed before sealing") from exc

    return VerifiedSingleSourceCandidateBindings(
        adopted_outline_artifact_id=adopted_record.artifact_id,
        adopted_outline_artifact_hash=adopted_record.content_hash,
        final_outline_artifact_id=final_record.artifact_id,
        final_outline_artifact_hash=final_record.content_hash,
        fragment_bank_artifact_id=bank_record.artifact_id,
        fragment_bank_artifact_hash=bank_record.content_hash,
        fragment_bank_content_hash=str(bank_doc.get("content_hash") or ""),
        candidate_scope_artifact_id=scope_record.artifact_id,
        candidate_scope_artifact_hash=scope_record.content_hash,
        candidate_scope_hash=scope.content_hash,
        source_inventory_artifact_id=inventory_record.artifact_id,
        source_inventory_artifact_hash=inventory_record.content_hash,
        source_inventory_content_hash=str(scope_binding.get("content_hash") or ""),
        _bindings=tuple(bindings),
        _shared_bindings=tuple(shared_bindings),
        _rejected_occurrences=tuple(rejected),
        _seal=_VERIFIED_SEAL,
    )
