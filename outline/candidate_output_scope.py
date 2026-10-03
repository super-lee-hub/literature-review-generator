"""Finite source-backed cardinalities for Outline candidate output.

This module binds candidate claim slots to already validated semantic task
results and the Registry-verified Writer source inventory. It creates no
claims and does not require every source fact to be emitted in the outline.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from runtime.provider_runtime import hash_json
from services.writer_source_inventory import (
    VerifiedWriterInterpretationDependencyV1,
    VerifiedWriterSourceClaimV1,
    VerifiedWriterSourceEvidenceV1,
    VerifiedWriterSourceFieldV1,
    VerifiedWriterSourceInventoryV1,
    VerifiedWriterSourcePaperV1,
)

_HEX64 = re.compile(r"[0-9a-f]{64}")


class CandidateOutputScopeError(ValueError):
    """A candidate output scope or its submitted payload is not source-closed."""


def _string_ids(value: Any, label: str, *, required: bool = False) -> tuple[str, ...]:
    if value is None:
        values: list[Any] = []
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        values = list(value)
    else:
        raise CandidateOutputScopeError(f"{label} must be an array of IDs")
    if any(not isinstance(item, str) or not item.strip() for item in values):
        raise CandidateOutputScopeError(f"{label} contains an empty or non-string ID")
    result = tuple(sorted(set(values)))
    if len(result) != len(values):
        raise CandidateOutputScopeError(f"{label} contains duplicate IDs")
    if required and not result:
        raise CandidateOutputScopeError(f"{label} is required")
    return result


@dataclass(frozen=True, slots=True)
class CandidateOutputSupportSlotV1:
    """One source-owned paper support row for a validated synthesis claim."""

    claim_slot_id: str
    claim_group_id: str
    task_id: str
    topic_id: str
    fragment_id: str
    provider_result_id: str
    synthesis_claim_id: str
    paper_key: str
    primary_claim_id: str
    study_id: str
    source_claim_ids: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    source_field_ids: tuple[str, ...]
    relation_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "claim_slot_id": self.claim_slot_id,
            "claim_group_id": self.claim_group_id,
            "task_id": self.task_id,
            "topic_id": self.topic_id,
            "fragment_id": self.fragment_id,
            "provider_result_id": self.provider_result_id,
            "synthesis_claim_id": self.synthesis_claim_id,
            "paper_key": self.paper_key,
            "primary_claim_id": self.primary_claim_id,
            "study_id": self.study_id,
            "source_claim_ids": list(self.source_claim_ids),
            "evidence_ids": list(self.evidence_ids),
            "source_field_ids": list(self.source_field_ids),
            "relation_ids": list(self.relation_ids),
        }


@dataclass(frozen=True, slots=True)
class CandidateOutputClaimGroupV1:
    """One provider-authored synthesis assertion and its complete paper support."""

    claim_group_id: str
    task_id: str
    topic_id: str
    fragment_id: str
    provider_result_id: str
    synthesis_claim_id: str
    claim_hash: str
    paper_keys: tuple[str, ...]
    support_slot_ids: tuple[str, ...]
    relation_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "claim_group_id": self.claim_group_id,
            "task_id": self.task_id,
            "topic_id": self.topic_id,
            "fragment_id": self.fragment_id,
            "provider_result_id": self.provider_result_id,
            "synthesis_claim_id": self.synthesis_claim_id,
            "claim_hash": self.claim_hash,
            "paper_keys": list(self.paper_keys),
            "support_slot_ids": list(self.support_slot_ids),
            "relation_ids": list(self.relation_ids),
        }


@dataclass(frozen=True, slots=True)
class CandidateOutputScopeV1:
    """Immutable, source-derived candidate section/claim/support limits."""

    registry_job_id: str
    source_inventory_artifact_id: str
    source_inventory_artifact_hash: str
    source_inventory_content_hash: str
    task_ids: tuple[str, ...]
    selected_relation_ids: tuple[str, ...]
    claim_groups: tuple[CandidateOutputClaimGroupV1, ...]
    claim_slots: tuple[CandidateOutputSupportSlotV1, ...]

    @property
    def max_sections(self) -> int:
        return len(self.task_ids) + len(self.claim_groups)

    @property
    def max_claims(self) -> int:
        return len(self.claim_groups)

    @property
    def max_support_rows(self) -> int:
        return len(self.claim_slots)

    @property
    def paper_keys(self) -> tuple[str, ...]:
        return tuple(sorted({slot.paper_key for slot in self.claim_slots}))

    def _body(self) -> dict[str, Any]:
        return {
            "schema_version": "outline-candidate-output-scope/v1",
            "source_inventory_binding": {
                "registry_job_id": self.registry_job_id,
                "artifact_id": self.source_inventory_artifact_id,
                "artifact_hash": self.source_inventory_artifact_hash,
                "content_hash": self.source_inventory_content_hash,
            },
            "task_ids": list(self.task_ids),
            "selected_relation_ids": list(self.selected_relation_ids),
            "claim_groups": [item.to_dict() for item in self.claim_groups],
            "claim_slots": [item.to_dict() for item in self.claim_slots],
            "limits": {
                "max_sections": self.max_sections,
                "max_claims": self.max_claims,
                "max_support_rows": self.max_support_rows,
            },
            "scope_policy": "optional_source_closed_synthesis_slots_v1",
        }

    @property
    def content_hash(self) -> str:
        return hash_json(self._body())

    def to_dict(self) -> dict[str, Any]:
        result = self._body()
        result["content_hash"] = self.content_hash
        return result

    def for_papers(self, allowed_paper_keys: Sequence[str]) -> CandidateOutputScopeV1:
        """Return only complete claim groups whose support is inside the shard."""

        allowed = set(_string_ids(allowed_paper_keys, "allowed_paper_keys"))
        kept_groups = tuple(
            group for group in self.claim_groups
            if set(group.paper_keys).issubset(allowed)
        )
        kept_group_ids = {item.claim_group_id for item in kept_groups}
        kept_slots = tuple(
            slot for slot in self.claim_slots if slot.claim_group_id in kept_group_ids
        )
        kept_tasks = tuple(sorted({item.task_id for item in kept_groups}))
        return CandidateOutputScopeV1(
            registry_job_id=self.registry_job_id,
            source_inventory_artifact_id=self.source_inventory_artifact_id,
            source_inventory_artifact_hash=self.source_inventory_artifact_hash,
            source_inventory_content_hash=self.source_inventory_content_hash,
            task_ids=kept_tasks,
            selected_relation_ids=self.selected_relation_ids,
            claim_groups=kept_groups,
            claim_slots=kept_slots,
        )

    def validate(self, payload: Mapping[str, Any]) -> None:
        """Validate candidate cardinality, task identity and exact source closure."""

        if not isinstance(payload, Mapping):
            raise CandidateOutputScopeError("candidate payload must be an object")
        if not self.claim_groups or not self.claim_slots:
            raise CandidateOutputScopeError(
                "candidate output has no source-closed claim slots; hold admission unresolved"
            )
        sections = payload.get("sections")
        if not isinstance(sections, list) or not sections:
            raise CandidateOutputScopeError("candidate payload requires nonempty sections")
        if len(sections) > self.max_sections:
            raise CandidateOutputScopeError(
                f"candidate section count {len(sections)} exceeds source-derived limit {self.max_sections}"
            )
        slot_by_id = {slot.claim_slot_id: slot for slot in self.claim_slots}
        group_by_id = {group.claim_group_id: group for group in self.claim_groups}
        allowed_tasks = set(self.task_ids)
        allowed_relations = set(self.selected_relation_ids)
        used_slot_ids: set[str] = set()
        group_claims: dict[str, set[str]] = {}
        group_sections: dict[str, set[str]] = {}
        support_rows_count = 0
        claims_count = 0
        seen_sections: set[str] = set()

        for index, section in enumerate(sections):
            if not isinstance(section, Mapping):
                raise CandidateOutputScopeError(f"section {index} must be an object")
            section_id = str(section.get("section_id") or "").strip()
            if not section_id or section_id in seen_sections:
                raise CandidateOutputScopeError("candidate has duplicate or missing section IDs")
            seen_sections.add(section_id)
            task_ids = _string_ids(section.get("task_ids"), f"{section_id}.task_ids", required=True)
            if not set(task_ids).issubset(allowed_tasks):
                raise CandidateOutputScopeError(f"{section_id} contains a task outside the output scope")
            relation_ids = set(_string_ids(section.get("relation_ids"), f"{section_id}.relation_ids"))
            if not relation_ids.issubset(allowed_relations):
                raise CandidateOutputScopeError(f"{section_id} contains a relation outside the output scope")
            paper_keys = set(_string_ids(section.get("paper_keys"), f"{section_id}.paper_keys", required=True))
            claims = _string_ids(section.get("claims"), f"{section_id}.claims", required=True)
            if len(claims) > self.max_claims:
                raise CandidateOutputScopeError(
                    f"{section_id} claim count exceeds source-derived limit {self.max_claims}"
                )
            claims_count += len(claims)
            if claims_count > self.max_claims:
                raise CandidateOutputScopeError(
                    f"candidate claim count exceeds source-derived limit {self.max_claims}"
                )
            supports = section.get("claim_support")
            if not isinstance(supports, list) or not supports:
                raise CandidateOutputScopeError(f"{section_id} has no claim support rows")
            support_rows_count += len(supports)
            if support_rows_count > self.max_support_rows:
                raise CandidateOutputScopeError(
                    f"candidate support row count exceeds source-derived limit {self.max_support_rows}"
                )
            section_tasks: set[str] = set()
            section_papers: set[str] = set()
            section_support_relations: set[str] = set()
            claims_with_support: set[str] = set()
            for row_index, row in enumerate(supports):
                label = f"{section_id}.claim_support[{row_index}]"
                if not isinstance(row, Mapping):
                    raise CandidateOutputScopeError(f"{label} must be an object")
                slot_id = str(row.get("claim_slot_id") or "").strip()
                if not slot_id or slot_id not in slot_by_id:
                    raise CandidateOutputScopeError(f"{label} references an unknown claim_slot_id")
                if slot_id in used_slot_ids:
                    raise CandidateOutputScopeError(f"{label} reuses a claim_slot_id")
                used_slot_ids.add(slot_id)
                slot = slot_by_id[slot_id]
                if str(row.get("task_id") or "") != slot.task_id:
                    raise CandidateOutputScopeError(f"{label} task does not match its claim slot")
                if str(row.get("paper_key") or "") != slot.paper_key:
                    raise CandidateOutputScopeError(f"{label} paper does not match its claim slot")
                if str(row.get("primary_claim_id") or "") != slot.primary_claim_id:
                    raise CandidateOutputScopeError(f"{label} primary_claim_id does not match its claim slot")
                for field_name, expected in (
                    ("source_claim_ids", slot.source_claim_ids),
                    ("evidence_ids", slot.evidence_ids),
                    ("source_field_ids", slot.source_field_ids),
                ):
                    observed = _string_ids(row.get(field_name), f"{label}.{field_name}")
                    if observed != expected:
                        raise CandidateOutputScopeError(
                            f"{label} {field_name} differs from the verified source closure"
                        )
                observed_study = str(row.get("study_id") or "")
                if observed_study != slot.study_id:
                    raise CandidateOutputScopeError(f"{label} study_id differs from verified source ownership")
                claim_text = str(row.get("claim") or "").strip()
                if not claim_text or claim_text not in claims:
                    raise CandidateOutputScopeError(f"{label} is orphaned from section claims")
                group_claims.setdefault(slot.claim_group_id, set()).add(claim_text)
                group_sections.setdefault(slot.claim_group_id, set()).add(section_id)
                claims_with_support.add(claim_text)
                section_tasks.add(slot.task_id)
                section_papers.add(slot.paper_key)
                section_support_relations.update(slot.relation_ids)
            if claims_with_support != set(claims):
                raise CandidateOutputScopeError(f"{section_id} contains a claim without source support")
            if section_tasks != set(task_ids):
                raise CandidateOutputScopeError(f"{section_id} task_ids do not match its source-backed claims")
            if section_papers != paper_keys:
                raise CandidateOutputScopeError(f"{section_id} paper_keys do not match its source support")
            if not relation_ids.issubset(section_support_relations):
                raise CandidateOutputScopeError(
                    f"{section_id} assigns a selected relation with no consumed source claim group"
                )

        if any(len(texts) != 1 for texts in group_claims.values()):
            raise CandidateOutputScopeError("one synthesis claim group cannot justify multiple outline claims")
        for group_id in group_claims:
            if len(group_sections.get(group_id, set())) != 1:
                raise CandidateOutputScopeError(
                    f"claim group {group_id} cannot be split across multiple outline sections"
                )
            group = group_by_id[group_id]
            expected_slots = set(group.support_slot_ids)
            actual_slots = {
                slot.claim_slot_id
                for slot in self.claim_slots
                if slot.claim_group_id == group_id and slot.claim_slot_id in used_slot_ids
            }
            if actual_slots != expected_slots:
                raise CandidateOutputScopeError(
                    f"claim group {group_id} is missing one or more paper support closures"
                )


def _inventory_indexes(inventory: VerifiedWriterSourceInventoryV1) -> dict[str, dict[str, Any]]:
    paper_by_key = inventory.paper_by_key()
    claims: dict[str, dict[str, VerifiedWriterSourceClaimV1]] = {}
    evidence: dict[str, dict[str, VerifiedWriterSourceEvidenceV1]] = {}
    fields: dict[str, dict[str, VerifiedWriterSourceFieldV1]] = {}
    dependencies: dict[str, dict[str, list[VerifiedWriterInterpretationDependencyV1]]] = {}
    claim_owner_papers: dict[str, set[str]] = {}
    for paper in inventory.papers:
        claims[paper.paper_key] = {item.claim_id: item for item in paper.claims}
        for item in paper.claims:
            claim_owner_papers.setdefault(item.claim_id, set()).add(paper.paper_key)
        evidence[paper.paper_key] = {item.evidence_id: item for item in paper.evidence}
        fields[paper.paper_key] = {item.source_field_id: item for item in paper.source_fields}
        by_primary: dict[str, list[VerifiedWriterInterpretationDependencyV1]] = {}
        for item in paper.interpretation_dependencies:
            if item.primary_claim_id:
                by_primary.setdefault(item.primary_claim_id, []).append(item)
        dependencies[paper.paper_key] = by_primary
    return {
        "papers": paper_by_key,
        "claims": claims,
        "evidence": evidence,
        "fields": fields,
        "dependencies": dependencies,
        "claim_owner_papers": claim_owner_papers,
    }


def _claim_papers(claim: Mapping[str, Any], label: str) -> tuple[str, ...]:
    raw_papers = [claim.get("paper_key")] if "paper_key" in claim else claim.get("paper_keys")
    return _string_ids(raw_papers, f"{label}.paper_keys", required=True)


def _collect_claim_graph(
    topic_routes: Sequence[Mapping[str, Any]],
    bridge_rows: Sequence[tuple[str, str, str, Mapping[str, Any]]],
) -> dict[str, Mapping[str, Any]]:
    graph: dict[str, Mapping[str, Any]] = {}

    def add(claim: Any, label: str) -> None:
        if not isinstance(claim, Mapping):
            raise CandidateOutputScopeError(f"{label} contains a malformed synthesis claim")
        claim_id = str(claim.get("claim_id") or "").strip()
        if not claim_id.startswith("synthesis:"):
            raise CandidateOutputScopeError(f"{label} lacks an existing synthesis claim identity")
        prior = graph.get(claim_id)
        if prior is not None and hash_json(dict(prior)) != hash_json(dict(claim)):
            raise CandidateOutputScopeError(f"synthesis claim {claim_id} has conflicting task outputs")
        graph[claim_id] = dict(claim)

    for route in topic_routes:
        if not isinstance(route, Mapping):
            raise CandidateOutputScopeError("topic route must be an object")
        for fragment in route.get("fragments") or ():
            if not isinstance(fragment, Mapping):
                raise CandidateOutputScopeError("topic route contains a malformed fragment")
            for result in fragment.get("provider_results") or ():
                if not isinstance(result, Mapping):
                    raise CandidateOutputScopeError("topic fragment contains a malformed provider result")
                provider_output = result.get("provider_output")
                if not isinstance(provider_output, Mapping):
                    if result.get("provider_output") is None:
                        continue
                    raise CandidateOutputScopeError("topic provider result output must be an object")
                claims = provider_output.get("claims") or ()
                if not isinstance(claims, Sequence) or isinstance(claims, (str, bytes)):
                    raise CandidateOutputScopeError("topic provider claims must be an array")
                for claim in claims:
                    add(claim, "topic provider output")
    for _task_id, _result_id, _claim_id, claim in bridge_rows:
        add(claim, "bridge provider output")
    return graph


def _canonicalize_claim_lineage(
    claim: Mapping[str, Any],
    claim_graph: Mapping[str, Mapping[str, Any]],
    indexes: Mapping[str, Any],
) -> dict[str, Any]:
    """Resolve actual synthesis-to-synthesis references to verified source IDs."""

    claim_id = str(claim.get("claim_id") or "")
    papers = _claim_papers(claim, claim_id)
    source_ids = _string_ids(claim.get("source_claim_ids"), f"{claim_id}.source_claim_ids", required=True)

    def inherited_context(
        source_id: str,
        allowed_papers: set[str],
        stack: tuple[str, ...],
    ) -> tuple[set[str], set[str]]:
        if indexes["claim_owner_papers"].get(source_id):
            return set(), set()
        upstream = claim_graph.get(source_id)
        if upstream is None:
            raise CandidateOutputScopeError(
                f"source claim {source_id} is absent from both task lineage and verified inventory"
            )
        if source_id in stack:
            raise CandidateOutputScopeError("synthesis source-claim lineage contains a cycle")
        scoped_papers = allowed_papers.intersection(set(_claim_papers(upstream, source_id)))
        if not scoped_papers:
            raise CandidateOutputScopeError(
                f"synthesis source claim {source_id} has no paper in the enclosing claim scope"
            )
        evidence = set(_string_ids(upstream.get("evidence_ids"), f"{source_id}.evidence_ids"))
        fields = set(_string_ids(upstream.get("source_field_ids"), f"{source_id}.source_field_ids"))
        upstream_ids = _string_ids(
            upstream.get("source_claim_ids"), f"{source_id}.source_claim_ids", required=True,
        )
        for upstream_id in upstream_ids:
            nested_evidence, nested_fields = inherited_context(
                upstream_id, scoped_papers, (*stack, source_id),
            )
            evidence.update(nested_evidence)
            fields.update(nested_fields)
        return evidence, fields

    def expand(source_id: str, allowed_papers: set[str], stack: tuple[str, ...]) -> dict[str, set[str]]:
        owners = set(indexes["claim_owner_papers"].get(source_id, ()))
        if owners:
            if len(owners) != 1:
                raise CandidateOutputScopeError(
                    f"source claim {source_id} has ambiguous paper ownership in the verified inventory"
                )
            owner = next(iter(owners))
            if owner not in allowed_papers:
                raise CandidateOutputScopeError(
                    f"source claim {source_id} is outside its synthesis claim paper scope"
                )
            return {owner: {source_id}}
        upstream = claim_graph.get(source_id)
        if upstream is None:
            raise CandidateOutputScopeError(
                f"source claim {source_id} is absent from both task lineage and verified inventory"
            )
        if source_id in stack:
            raise CandidateOutputScopeError("synthesis source-claim lineage contains a cycle")
        upstream_papers = set(_claim_papers(upstream, source_id))
        scoped_papers = allowed_papers.intersection(upstream_papers)
        if not scoped_papers:
            raise CandidateOutputScopeError(
                f"synthesis source claim {source_id} has no paper in the enclosing claim scope"
            )
        upstream_ids = _string_ids(
            upstream.get("source_claim_ids"), f"{source_id}.source_claim_ids", required=True,
        )
        expanded: dict[str, set[str]] = {}
        for upstream_id in upstream_ids:
            for paper_key, canonical_ids in expand(
                upstream_id, scoped_papers, (*stack, source_id),
            ).items():
                expanded.setdefault(paper_key, set()).update(canonical_ids)
        if not scoped_papers.issubset(expanded):
            raise CandidateOutputScopeError(
                f"synthesis source claim {source_id} does not close every declared paper"
            )
        return expanded

    resolved_by_paper: dict[str, set[str]] = {}
    inherited_evidence: set[str] = set()
    inherited_fields: set[str] = set()
    for source_id in source_ids:
        for paper_key, canonical_ids in expand(source_id, set(papers), ()).items():
            resolved_by_paper.setdefault(paper_key, set()).update(canonical_ids)
        parent_evidence, parent_fields = inherited_context(source_id, set(papers), ())
        inherited_evidence.update(parent_evidence)
        inherited_fields.update(parent_fields)
    if not set(papers).issubset(resolved_by_paper):
        raise CandidateOutputScopeError(
            f"{claim_id} has no canonical source claim for one or more declared papers"
        )
    normalized = dict(claim)
    normalized["source_claim_ids"] = sorted(set().union(*resolved_by_paper.values()))
    normalized["evidence_ids"] = sorted({
        *_string_ids(claim.get("evidence_ids"), f"{claim_id}.evidence_ids"),
        *inherited_evidence,
    })
    normalized["source_field_ids"] = sorted({
        *_string_ids(claim.get("source_field_ids"), f"{claim_id}.source_field_ids"),
        *inherited_fields,
    })

    primary_by_paper: dict[str, set[str]] = {}
    explicit_by_paper = claim.get("primary_claim_ids_by_paper")
    explicit_primary_ids: list[tuple[str, tuple[str, ...]]] = []
    if isinstance(explicit_by_paper, Mapping):
        explicit_primary_ids.extend(
            (str(paper), _string_ids(values, f"{claim_id}.primary_claim_ids_by_paper[{paper}]", required=True))
            for paper, values in explicit_by_paper.items()
        )
    elif claim.get("primary_claim_ids") is not None:
        explicit_primary_ids.append(("", _string_ids(
            claim.get("primary_claim_ids"), f"{claim_id}.primary_claim_ids", required=True,
        )))
    elif claim.get("primary_claim_id") is not None:
        explicit_primary_ids.append(("", (str(claim.get("primary_claim_id") or ""),)))
    for paper_filter, primary_ids in explicit_primary_ids:
        if paper_filter and paper_filter not in papers:
            raise CandidateOutputScopeError(f"{claim_id} primary source names an unowned paper")
        allowed = {paper_filter} if paper_filter else set(papers)
        for primary_id in primary_ids:
            for paper_key, canonical_ids in expand(primary_id, allowed, ()).items():
                if not canonical_ids.issubset(resolved_by_paper.get(paper_key, set())):
                    raise CandidateOutputScopeError(
                        f"{claim_id} primary source lineage is not contained in its verified support"
                    )
                primary_by_paper.setdefault(paper_key, set()).update(canonical_ids)
    normalized_primaries: dict[str, set[str]] = {}
    if primary_by_paper:
        for paper_key, canonical_ids in resolved_by_paper.items():
            qualifier_ids = {
                str(required)
                for primary_id in canonical_ids
                for dependency in indexes["dependencies"][paper_key].get(primary_id, ())
                for required in dependency.required_source_claim_ids
            }
            inferred_primaries = canonical_ids - qualifier_ids
            declared = primary_by_paper.get(paper_key, set())
            selected = inferred_primaries.intersection(declared)
            if not selected:
                raise CandidateOutputScopeError(
                    f"{claim_id} does not identify an unambiguous canonical primary for {paper_key}"
                )
            normalized_primaries[paper_key] = selected
    normalized.pop("primary_claim_id", None)
    normalized.pop("primary_claim_ids", None)
    normalized.pop("primary_claim_ids_by_paper", None)
    if primary_by_paper:
        normalized["primary_claim_ids_by_paper"] = {
            paper: sorted(values) for paper, values in sorted(normalized_primaries.items())
        }
    return normalized


def _source_closure(
    paper: VerifiedWriterSourcePaperV1,
    primary_claim_id: str,
    indexes: Mapping[str, Any],
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...], str]:
    claims_by_paper = indexes["claims"][paper.paper_key]
    evidence_by_paper = indexes["evidence"][paper.paper_key]
    fields_by_paper = indexes["fields"][paper.paper_key]
    dependencies_by_primary = indexes["dependencies"][paper.paper_key]
    if primary_claim_id not in claims_by_paper:
        raise CandidateOutputScopeError(
            f"primary source claim {primary_claim_id} is absent from the verified source inventory"
        )
    claim_ids: set[str] = set()
    evidence_ids: set[str] = set()
    source_field_ids: set[str] = set()
    queue = [primary_claim_id]
    study_owners: set[str] = set()
    seen: set[str] = set()
    while queue:
        current = queue.pop()
        if current in seen:
            continue
        seen.add(current)
        claim = claims_by_paper.get(current)
        if claim is None:
            raise CandidateOutputScopeError(
                f"qualifier source claim {current} is absent from the verified source inventory"
            )
        claim_ids.add(current)
        owners = {str(item) for item in claim.owner_study_ids if str(item)}
        if len(owners) > 1:
            raise CandidateOutputScopeError(f"source claim {current} has ambiguous study ownership")
        study_owners.update(owners)
        evidence_ids.update(claim.evidence_ids)
        for dependency in dependencies_by_primary.get(current, ()):
            if dependency.scope == "unresolved":
                raise CandidateOutputScopeError(
                    f"source claim {current} has unresolved qualifier context"
                )
            owner = str(dependency.owner_study_id or "")
            if dependency.scope == "explicit_study" and (
                not owner or (owners and owner not in owners)
            ):
                raise CandidateOutputScopeError(
                    f"source claim {current} qualifier crosses its verified study owner"
                )
            if owner:
                study_owners.add(owner)
            queue.extend(dependency.required_source_claim_ids)
            evidence_ids.update(dependency.required_evidence_ids)
            source_field_ids.update(dependency.required_source_field_ids)
    if len(study_owners) > 1:
        raise CandidateOutputScopeError(
            f"source claim {primary_claim_id} qualifier closure crosses studies"
        )
    for evidence_id in evidence_ids:
        item = evidence_by_paper.get(evidence_id)
        if item is None:
            raise CandidateOutputScopeError(
                f"source evidence {evidence_id} is absent from the verified inventory"
            )
        evidence_owners = {str(value) for value in item.owner_study_ids if str(value)}
        if evidence_owners and study_owners and not evidence_owners.issubset(study_owners):
            raise CandidateOutputScopeError(
                f"source evidence {evidence_id} crosses the primary claim study"
            )
    for field_id in source_field_ids:
        item = fields_by_paper.get(field_id)
        if item is None:
            raise CandidateOutputScopeError(
                f"qualifier source field {field_id} is absent from the verified inventory"
            )
        field_owners = set(item.owner_study_ids)
        if field_owners and study_owners and not field_owners.issubset(study_owners):
            raise CandidateOutputScopeError(
                f"qualifier source field {field_id} crosses the primary claim study"
            )
    if not evidence_ids:
        raise CandidateOutputScopeError(
            f"source claim {primary_claim_id} has no source-closed evidence identity"
        )
    study_id = next(iter(study_owners), "")
    return (
        tuple(sorted(claim_ids)),
        tuple(sorted(evidence_ids)),
        tuple(sorted(source_field_ids)),
        study_id,
    )


def _claim_group(
    *,
    inventory: VerifiedWriterSourceInventoryV1,
    indexes: Mapping[str, Any],
    task_id: str,
    topic_id: str,
    fragment_id: str,
    provider_result_id: str,
    claim: Mapping[str, Any],
    selected_relation_ids: tuple[str, ...],
    task_paper_keys: Sequence[str] = (),
) -> tuple[CandidateOutputClaimGroupV1, list[CandidateOutputSupportSlotV1]]:
    claim_id = str(claim.get("claim_id") or "").strip()
    if not claim_id.startswith("synthesis:"):
        raise CandidateOutputScopeError("topic claim is missing its synthesis claim identity")
    source_claim_ids = _string_ids(
        claim.get("source_claim_ids"), f"{claim_id}.source_claim_ids", required=True,
    )
    if "paper_key" in claim:
        raw_papers = [claim.get("paper_key")]
        if claim.get("paper_keys") not in (None, [], ()):
            raise CandidateOutputScopeError(f"{claim_id} mixes paper_key and paper_keys")
    else:
        raw_papers = claim.get("paper_keys")
    paper_keys = _string_ids(raw_papers, f"{claim_id}.paper_keys", required=True)
    if task_paper_keys and not set(paper_keys).issubset(set(task_paper_keys)):
        raise CandidateOutputScopeError(f"{claim_id} references a paper outside its task fragment")
    relation_ids = _string_ids(claim.get("relation_ids"), f"{claim_id}.relation_ids")
    if not set(relation_ids).issubset(set(selected_relation_ids)):
        raise CandidateOutputScopeError(f"{claim_id} references a relation outside the selected relation scope")
    provider_evidence_ids = _string_ids(
        claim.get("evidence_ids"), f"{claim_id}.evidence_ids", required=True,
    )
    provider_field_ids = _string_ids(
        claim.get("source_field_ids"), f"{claim_id}.source_field_ids",
    )
    primary_claims_by_paper: dict[str, tuple[str, ...]] = {}
    closures: dict[str, dict[str, tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...], str]]] = {}
    for paper_key in paper_keys:
        paper = indexes["papers"].get(paper_key)
        if paper is None:
            raise CandidateOutputScopeError(
                f"{claim_id} references paper {paper_key} outside the verified inventory"
            )
        claims_by_paper = indexes["claims"][paper_key]
        refs_for_paper = tuple(
            source_id for source_id in source_claim_ids
            if source_id in claims_by_paper
        )
        if not refs_for_paper:
            raise CandidateOutputScopeError(
                f"{claim_id} has no verified source claim for paper {paper_key}"
            )
        unknown_for_paper = [
            source_id for source_id in source_claim_ids
            if source_id not in indexes["claims"].get(paper_key, {})
            and not any(source_id in rows for other, rows in indexes["claims"].items() if other != paper_key)
        ]
        if unknown_for_paper:
            raise CandidateOutputScopeError(
                f"{claim_id} references a source claim absent from the verified inventory"
            )
        source_ids = set(refs_for_paper)
        qualifier_ids = {
            str(required)
            for primary in source_ids
            for dependency in indexes["dependencies"][paper_key].get(primary, ())
            for required in dependency.required_source_claim_ids
        }
        inferred_primary_ids = sorted(source_ids - qualifier_ids)
        explicit_primary_values = claim.get("primary_claim_ids_by_paper")
        if isinstance(explicit_primary_values, Mapping) and paper_key in explicit_primary_values:
            primary_ids = _string_ids(
                explicit_primary_values[paper_key], f"{claim_id}.primary_claim_ids_by_paper[{paper_key}]",
                required=True,
            )
        elif claim.get("primary_claim_ids") is not None:
            primary_ids = tuple(
                item for item in _string_ids(claim.get("primary_claim_ids"), f"{claim_id}.primary_claim_ids", required=True)
                if item in source_ids
            )
        elif claim.get("primary_claim_id") is not None:
            primary_id = str(claim.get("primary_claim_id") or "")
            primary_ids = (
                (primary_id,)
                if primary_id in source_ids
                else (tuple(inferred_primary_ids) if len(inferred_primary_ids) == 1 else ())
            )
        elif len(inferred_primary_ids) == 1:
            primary_ids = (inferred_primary_ids[0],)
        else:
            raise CandidateOutputScopeError(
                f"{claim_id} has ambiguous primary source claims for paper {paper_key}"
            )
        if not primary_ids or not set(primary_ids).issubset(source_ids):
            raise CandidateOutputScopeError(
                f"{claim_id} has no owned primary source claim for paper {paper_key}"
            )
        primary_claims_by_paper[paper_key] = tuple(sorted(set(primary_ids)))
        per_paper_closures: dict[str, tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...], str]] = {}
        for primary_id in primary_claims_by_paper[paper_key]:
            per_paper_closures[primary_id] = _source_closure(paper, primary_id, indexes)
        union_claim_ids = set().union(*(set(value[0]) for value in per_paper_closures.values()))
        if union_claim_ids != source_ids:
            missing = sorted(union_claim_ids - source_ids)
            extra = sorted(source_ids - union_claim_ids)
            raise CandidateOutputScopeError(
                f"{claim_id} source_claim_ids are not the complete primary/qualifier closure "
                f"(missing={missing[:5]}, extra={extra[:5]})"
            )
        closures[paper_key] = per_paper_closures

    expected_evidence: set[str] = set()
    expected_fields: set[str] = set()
    expected_evidence_by_paper: dict[str, set[str]] = {}
    expected_fields_by_paper: dict[str, set[str]] = {}
    for paper_key, primary_map in closures.items():
        paper_evidence: set[str] = set()
        paper_fields: set[str] = set()
        for _claims, evidence_ids, field_ids, _study_id in primary_map.values():
            paper_evidence.update(evidence_ids)
            paper_fields.update(field_ids)
        expected_evidence.update(paper_evidence)
        expected_fields.update(paper_fields)
        expected_evidence_by_paper[paper_key] = paper_evidence
        expected_fields_by_paper[paper_key] = paper_fields
    if set(provider_evidence_ids) != expected_evidence:
        raise CandidateOutputScopeError(
            f"{claim_id} evidence_ids do not equal the verified primary/qualifier closure"
        )
    if set(provider_field_ids) != expected_fields:
        raise CandidateOutputScopeError(
            f"{claim_id} source_field_ids do not equal the verified qualifier closure"
        )

    group_identity = {
        "registry_job_id": inventory.registry_job_id,
        "task_id": task_id,
        "topic_id": topic_id,
        "fragment_id": fragment_id,
        "provider_result_id": provider_result_id,
        "synthesis_claim_id": claim_id,
        "claim_payload_hash": hash_json(dict(claim)),
    }
    group_id = "candidate-claim-group:v1:" + hash_json(group_identity)[:24]
    slots: list[CandidateOutputSupportSlotV1] = []
    for paper_key in paper_keys:
        paper = indexes["papers"][paper_key]
        for primary_id, (closure_claim_ids, closure_evidence_ids, closure_field_ids, study_id) in sorted(
            closures[paper_key].items()
        ):
            if not expected_evidence_by_paper[paper_key].issubset(set(provider_evidence_ids)):
                raise CandidateOutputScopeError(f"{claim_id} omits paper-scoped source evidence")
            primary_claim = indexes["claims"][paper_key][primary_id]
            primary_owners = {str(item) for item in primary_claim.owner_study_ids if str(item)}
            if study_id and primary_owners and study_id not in primary_owners:
                raise CandidateOutputScopeError(f"{claim_id} primary claim has conflicting study scope")
            for evidence_id in expected_evidence_by_paper[paper_key]:
                evidence = indexes["evidence"][paper_key].get(evidence_id)
                if evidence is None:
                    raise CandidateOutputScopeError(
                        f"{claim_id} source evidence {evidence_id} is not owned by {paper_key}"
                    )
            for field_id in expected_fields_by_paper[paper_key]:
                field = indexes["fields"][paper_key].get(field_id)
                if field is None:
                    raise CandidateOutputScopeError(
                        f"{claim_id} qualifier field {field_id} is not owned by {paper_key}"
                    )
            slot_identity = {
                "claim_group_id": group_id,
                "paper_key": paper_key,
                "primary_claim_id": primary_id,
            }
            slots.append(CandidateOutputSupportSlotV1(
                claim_slot_id="candidate-claim-slot:v1:" + hash_json(slot_identity)[:24],
                claim_group_id=group_id,
                task_id=task_id,
                topic_id=topic_id,
                fragment_id=fragment_id,
                provider_result_id=provider_result_id,
                synthesis_claim_id=claim_id,
                paper_key=paper_key,
                primary_claim_id=primary_id,
                study_id=study_id,
                source_claim_ids=closure_claim_ids,
                evidence_ids=closure_evidence_ids,
                source_field_ids=closure_field_ids,
                relation_ids=relation_ids,
            ))
    if not slots:
        raise CandidateOutputScopeError(f"{claim_id} has no source-closed paper support slots")
    group = CandidateOutputClaimGroupV1(
        claim_group_id=group_id,
        task_id=task_id,
        topic_id=topic_id,
        fragment_id=fragment_id,
        provider_result_id=provider_result_id,
        synthesis_claim_id=claim_id,
        claim_hash=hash_json(dict(claim)),
        paper_keys=paper_keys,
        support_slot_ids=tuple(sorted(item.claim_slot_id for item in slots)),
        relation_ids=relation_ids,
    )
    return group, slots


def _topic_claims(
    inventory: VerifiedWriterSourceInventoryV1,
    indexes: Mapping[str, Any],
    topic_routes: Sequence[Mapping[str, Any]],
    selected_relation_ids: tuple[str, ...],
    claim_graph: Mapping[str, Mapping[str, Any]],
) -> tuple[set[str], list[CandidateOutputClaimGroupV1], list[CandidateOutputSupportSlotV1]]:
    task_ids: set[str] = set()
    groups: list[CandidateOutputClaimGroupV1] = []
    slots: list[CandidateOutputSupportSlotV1] = []
    seen_topics: set[str] = set()
    seen_fragments: set[tuple[str, str]] = set()
    seen_results: set[tuple[str, str, str]] = set()
    seen_groups: set[str] = set()
    for route in topic_routes:
        if not isinstance(route, Mapping):
            raise CandidateOutputScopeError("topic route must be an object")
        topic_id = str(route.get("topic_id") or "").strip()
        task_id = str(route.get("logical_node_id") or "").strip()
        if not topic_id or not task_id or topic_id in seen_topics:
            raise CandidateOutputScopeError("topic routes require unique topic and existing logical task IDs")
        seen_topics.add(topic_id)
        task_ids.add(task_id)
        route_papers = _string_ids(route.get("paper_ids"), f"{topic_id}.paper_ids")
        fragments = route.get("fragments") or ()
        if not isinstance(fragments, Sequence) or isinstance(fragments, (str, bytes)):
            raise CandidateOutputScopeError(f"{topic_id}.fragments must be an array")
        for fragment in fragments:
            if not isinstance(fragment, Mapping):
                raise CandidateOutputScopeError(f"{topic_id} contains a malformed task fragment")
            fragment_id = str(fragment.get("fragment_id") or "").strip()
            if not fragment_id or (topic_id, fragment_id) in seen_fragments:
                raise CandidateOutputScopeError(f"{topic_id} has a missing or duplicate fragment identity")
            seen_fragments.add((topic_id, fragment_id))
            fragment_papers = _string_ids(
                fragment.get("paper_ids"), f"{fragment_id}.paper_ids"
            ) or route_papers
            if route_papers and fragment_papers and not set(fragment_papers).issubset(route_papers):
                raise CandidateOutputScopeError(f"{fragment_id} references a paper outside its topic route")
            results = fragment.get("provider_results") or ()
            if not isinstance(results, Sequence) or isinstance(results, (str, bytes)):
                raise CandidateOutputScopeError(f"{fragment_id}.provider_results must be an array")
            for result in results:
                if not isinstance(result, Mapping):
                    raise CandidateOutputScopeError(f"{fragment_id} contains a malformed provider result")
                result_id = str(result.get("result_id") or "").strip()
                provider_output = result.get("provider_output")
                if not result_id or not isinstance(provider_output, Mapping):
                    raise CandidateOutputScopeError(f"{fragment_id} provider result lacks its output identity")
                result_key = (task_id, fragment_id, result_id)
                if result_key in seen_results:
                    raise CandidateOutputScopeError(f"{fragment_id} repeats a provider result identity")
                seen_results.add(result_key)
                claims = provider_output.get("claims") or ()
                if not isinstance(claims, Sequence) or isinstance(claims, (str, bytes)):
                    raise CandidateOutputScopeError(f"{result_id}.claims must be an array")
                for claim in claims:
                    if not isinstance(claim, Mapping):
                        raise CandidateOutputScopeError(f"{result_id} contains a malformed synthesis claim")
                    if str(claim.get("fragment_id") or "") != fragment_id:
                        raise CandidateOutputScopeError(
                            f"synthesis claim fragment membership differs from {fragment_id}"
                        )
                    claim = _canonicalize_claim_lineage(claim, claim_graph, indexes)
                    group, group_slots = _claim_group(
                        inventory=inventory,
                        indexes=indexes,
                        task_id=task_id,
                        topic_id=topic_id,
                        fragment_id=fragment_id,
                        provider_result_id=result_id,
                        claim=claim,
                        selected_relation_ids=selected_relation_ids,
                        task_paper_keys=fragment_papers,
                    )
                    if group.claim_group_id in seen_groups:
                        raise CandidateOutputScopeError("semantic task results repeat a claim group identity")
                    seen_groups.add(group.claim_group_id)
                    groups.append(group)
                    slots.extend(group_slots)
    return task_ids, groups, slots


def _bridge_claim_rows(bridge_claims: Sequence[Mapping[str, Any]]) -> list[tuple[str, str, str, Mapping[str, Any]]]:
    rows: list[tuple[str, str, str, Mapping[str, Any]]] = []
    seen: set[tuple[str, str, str]] = set()
    for envelope in bridge_claims:
        if not isinstance(envelope, Mapping):
            raise CandidateOutputScopeError("bridge claim result must be an object")
        task_id = str(envelope.get("task_id") or envelope.get("node_id") or "").strip()
        result_id = str(envelope.get("result_id") or envelope.get("provider_result_id") or "").strip()
        provider_output = envelope.get("provider_output")
        if not task_id or not result_id or not isinstance(provider_output, Mapping):
            raise CandidateOutputScopeError("bridge claims require existing task, result and output identities")
        if task_id not in {"cross_group_comparison", "global_synthesis"}:
            raise CandidateOutputScopeError("bridge claim task is not a supported existing semantic task")
        claim_field = "bridge_claims" if task_id == "cross_group_comparison" else "synthesis_claims"
        claims = provider_output.get(claim_field) or ()
        if not isinstance(claims, Sequence) or isinstance(claims, (str, bytes)):
            raise CandidateOutputScopeError(f"{task_id}.{claim_field} must be an array")
        for claim in claims:
            if not isinstance(claim, Mapping):
                raise CandidateOutputScopeError(f"{task_id} contains a malformed bridge claim")
            claim_id = str(claim.get("claim_id") or "")
            if not claim_id.startswith(f"synthesis:{task_id}:"):
                raise CandidateOutputScopeError(f"{task_id} bridge claim has a foreign identity")
            identity = (task_id, result_id, claim_id)
            if identity in seen:
                raise CandidateOutputScopeError(f"{task_id} repeats a bridge claim identity")
            seen.add(identity)
            rows.append((task_id, result_id, claim_id, claim))
    return rows


def build_candidate_output_scope_v1(
    inventory: VerifiedWriterSourceInventoryV1,
    topic_routes: Sequence[Mapping[str, Any]],
    selected_relation_ids: Sequence[str] = (),
    *,
    bridge_claims: Sequence[Mapping[str, Any]] = (),
) -> CandidateOutputScopeV1:
    """Build finite output slots from verified source and existing task results.

    ``topic_routes`` must be the real persisted semantic task/fragment result
    projection and include each task's existing ``logical_node_id``. Optional
    bridge envelopes carry ``task_id``, ``result_id``, and ``provider_output``
    from a ready cross-group or global synthesis result.
    """

    if type(inventory) is not VerifiedWriterSourceInventoryV1 or not inventory.is_verified:
        raise CandidateOutputScopeError("candidate output scope requires a verified source inventory")
    if (
        not inventory.registry_job_id
        or not inventory.artifact_id
        or not _HEX64.fullmatch(str(inventory.artifact_hash or ""))
        or not _HEX64.fullmatch(str(inventory.content_hash or ""))
        or not inventory.papers
    ):
        raise CandidateOutputScopeError("verified source inventory binding is incomplete")
    if not isinstance(topic_routes, Sequence) or isinstance(topic_routes, (str, bytes)):
        raise CandidateOutputScopeError("topic_routes must be an array of existing task results")
    if not isinstance(bridge_claims, Sequence) or isinstance(bridge_claims, (str, bytes)):
        raise CandidateOutputScopeError("bridge_claims must be an array")
    if not isinstance(selected_relation_ids, Sequence) or isinstance(selected_relation_ids, (str, bytes)):
        raise CandidateOutputScopeError("selected_relation_ids must be an array")
    relation_ids = _string_ids(selected_relation_ids, "selected_relation_ids")
    indexes = _inventory_indexes(inventory)
    bridge_rows = _bridge_claim_rows(bridge_claims)
    claim_graph = _collect_claim_graph(topic_routes, bridge_rows)
    task_ids, groups, slots = _topic_claims(
        inventory,
        indexes,
        topic_routes,
        relation_ids,
        claim_graph,
    )
    topic_ids = {
        str(route.get("topic_id") or "")
        for route in topic_routes if isinstance(route, Mapping)
    }
    topic_papers: dict[str, set[str]] = {}
    for route in topic_routes:
        if not isinstance(route, Mapping):
            continue
        topic_id = str(route.get("topic_id") or "")
        papers = set(_string_ids(route.get("paper_ids"), f"{topic_id}.paper_ids"))
        for fragment in route.get("fragments") or ():
            if isinstance(fragment, Mapping):
                papers.update(_string_ids(
                    fragment.get("paper_ids"),
                    f"{fragment.get('fragment_id') or topic_id}.paper_ids",
                ))
        topic_papers[topic_id] = papers
    for task_id, result_id, _claim_id, original_claim in bridge_rows:
        claim = _canonicalize_claim_lineage(original_claim, claim_graph, indexes)
        if task_id == "cross_group_comparison":
            claim_topic_ids = _string_ids(claim.get("topic_ids"), f"{claim.get('claim_id')}.topic_ids", required=True)
            if not set(claim_topic_ids).issubset(topic_ids):
                raise CandidateOutputScopeError("bridge claim references a topic outside the ready task plan")
            raw_papers = (
                [claim.get("paper_key")]
                if "paper_key" in claim
                else claim.get("paper_keys")
            )
            claim_paper_keys = set(_string_ids(
                raw_papers, f"{claim.get('claim_id')}.paper_keys", required=True,
            ))
            allowed_bridge_papers = set().union(
                *(topic_papers.get(topic_id, set()) for topic_id in claim_topic_ids)
            )
            if not claim_paper_keys.issubset(allowed_bridge_papers):
                raise CandidateOutputScopeError("bridge claim paper scope is outside its declared topics")
        bridge_task = task_id
        group, group_slots = _claim_group(
            inventory=inventory,
            indexes=indexes,
            task_id=bridge_task,
            topic_id="",
            fragment_id=str(claim.get("fragment_id") or ""),
            provider_result_id=result_id,
            claim=claim,
            selected_relation_ids=relation_ids,
        )
        if group.claim_group_id in {item.claim_group_id for item in groups}:
            raise CandidateOutputScopeError("semantic results repeat a bridge claim group identity")
        groups.append(group)
        slots.extend(group_slots)
        task_ids.add(task_id)
    if len({item.claim_group_id for item in groups}) != len(groups):
        raise CandidateOutputScopeError("candidate output scope contains duplicate claim groups")
    if len({item.claim_slot_id for item in slots}) != len(slots):
        raise CandidateOutputScopeError("candidate output scope contains duplicate source support slots")
    return CandidateOutputScopeV1(
        registry_job_id=inventory.registry_job_id,
        source_inventory_artifact_id=inventory.artifact_id,
        source_inventory_artifact_hash=inventory.artifact_hash,
        source_inventory_content_hash=inventory.content_hash,
        task_ids=tuple(sorted(task_ids)),
        selected_relation_ids=relation_ids,
        claim_groups=tuple(sorted(groups, key=lambda item: item.claim_group_id)),
        claim_slots=tuple(sorted(slots, key=lambda item: item.claim_slot_id)),
    )


__all__ = [
    "CandidateOutputClaimGroupV1",
    "CandidateOutputScopeError",
    "CandidateOutputScopeV1",
    "CandidateOutputSupportSlotV1",
    "build_candidate_output_scope_v1",
]
