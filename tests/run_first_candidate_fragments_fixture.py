"""Loopback model behavior for shared text and index-only organization.

This is a model fixture, not a production source validator or science acceptance.
"""
import hashlib
import re
from collections.abc import Mapping
from typing import Any

from outline.candidate_fragment_bank import (
    validate_candidate_organization_map_for_request_v1,
)
from outline.candidate_relation_fragments import (
    validate_candidate_comparison_fragments_output_v1,
)


def _labels_by_wire_paper(
    request: Mapping[str, Any], source_label_map: Mapping[str, str],
) -> dict[str, str]:
    """Resolve opaque paper aliases only through their canonical source claims."""
    if not isinstance(source_label_map, Mapping) or not source_label_map:
        raise ValueError("comparison source label map must contain canonical paper labels")

    labels_by_canonical: dict[str, str] = {}
    for raw_key, raw_label in source_label_map.items():
        if (
            not isinstance(raw_key, str) or not raw_key.strip() or raw_key != raw_key.strip()
            or not isinstance(raw_label, str) or not raw_label.strip()
        ):
            raise ValueError("comparison source label map contains an empty or invalid source label")
        labels_by_canonical[raw_key] = raw_label.strip()

    shared = request.get('shared_semantic_context')
    tables = shared.get('interpretation_source_tables') if isinstance(shared, Mapping) else None
    source_rows = tables.get('source_claims') if isinstance(tables, Mapping) else None
    scope = request.get('comparison_scope')
    slots = scope.get('claim_slots') if isinstance(scope, Mapping) else None
    if not isinstance(source_rows, list) or not isinstance(slots, list):
        raise TypeError("comparison source label ownership tables are missing")

    rows_by_claim: dict[str, list[Mapping[str, Any]]] = {}
    for row in source_rows:
        if not isinstance(row, Mapping):
            continue
        claim_id = row.get('claim_id')
        if isinstance(claim_id, str) and claim_id:
            rows_by_claim.setdefault(claim_id, []).append(row)

    claim_ids_by_wire_paper: dict[str, set[str]] = {}
    for slot in slots:
        if not isinstance(slot, Mapping):
            continue
        wire_key = slot.get('paper_key')
        if not isinstance(wire_key, str) or not wire_key:
            raise ValueError("comparison source label ownership has an empty wire paper key")
        claim_ids = {
            str(value) for value in slot.get('source_claim_ids') or () if str(value)
        }
        if not claim_ids:
            raise ValueError(f"comparison source label ownership has no claims for {wire_key!r}")
        claim_ids_by_wire_paper.setdefault(wire_key, set()).update(claim_ids)

    labels_by_wire: dict[str, str] = {}
    selected_label_owners: dict[str, str] = {}
    wire_by_selected_owner: dict[str, str] = {}
    for wire_key, claim_ids in claim_ids_by_wire_paper.items():
        canonical_owners: set[str] = set()
        for claim_id in claim_ids:
            rows = rows_by_claim.get(claim_id, [])
            if len(rows) != 1:
                raise ValueError(
                    f"comparison source label ownership for {wire_key!r} is missing or ambiguous"
                )
            if rows[0].get('paper_key') != wire_key:
                raise ValueError(
                    f"comparison source label ownership for {wire_key!r} conflicts with its source claim row"
                )

            owners = []
            for canonical_key in labels_by_canonical:
                prefix = f'claim:{canonical_key}:'
                if not claim_id.startswith(prefix):
                    continue
                suffix = claim_id[len(prefix):]
                if re.fullmatch(r'.+:[0-9a-f]{16}', suffix):
                    owners.append(canonical_key)
            if len(owners) != 1:
                raise ValueError(
                    f"comparison source label ownership for claim {claim_id!r} is missing or ambiguous"
                )
            canonical_owners.add(owners[0])

        if len(canonical_owners) != 1:
            raise ValueError(
                f"comparison source label ownership for {wire_key!r} is ambiguous or conflicting"
            )
        canonical_owner = next(iter(canonical_owners))
        prior_wire_key = wire_by_selected_owner.get(canonical_owner)
        if prior_wire_key is not None and prior_wire_key != wire_key:
            raise ValueError(
                f"comparison source label ownership maps multiple wire papers to {canonical_owner!r}"
            )
        wire_by_selected_owner[canonical_owner] = wire_key
        label = labels_by_canonical[canonical_owner]
        label_key = label.casefold()
        prior_owner = selected_label_owners.get(label_key)
        if prior_owner is not None and prior_owner != canonical_owner:
            raise ValueError("comparison source label map assigns a conflicting label to multiple sources")
        selected_label_owners[label_key] = canonical_owner
        labels_by_wire[wire_key] = label

    return labels_by_wire


def _comparison_response(
    node_id: str,
    request: Mapping[str, Any],
    *,
    source_label_map: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    # Reuse the existing source-quote fixture, including its strict qualifier
    # checks and paper-level reported-study handling. No new finding is inferred.
    from tests.test_run_first_scale_integration import _compact_candidate

    scope = request['comparison_scope']
    source_rows = request['shared_semantic_context']['interpretation_source_tables']['source_claims']
    if source_label_map is not None:
        labels = _labels_by_wire_paper(request, source_label_map)
    else:
        labels = {}
        for row in source_rows:
            # The direct scale fixture keeps its established PDF-stem label
            # when no explicit canonical metadata map is supplied.
            match = re.fullmatch(r'claim:rfspaper(\d{4})_unknown_author:.+', row['claim_id'])
            if match:
                labels[row['paper_key']] = f'rfs-paper-{match[1]}'
    fragments = []
    for index, group in enumerate(scope['claim_groups']):
        group_id = group['claim_group_id']
        slots = [dict(slot, claim_group_index=0) for slot in scope['claim_slots']
                 if slot['claim_group_id'] == group_id]
        papers = sorted({slot['paper_key'] for slot in slots})
        wire = {
            'claim_group_ids': [group_id], 'claim_slots': slots,
            'required_claim_group_indices': [0],
            'organization_tasks': [dict(group, claim_group_index=0)],
        }
        evidence_rows = []
        for pos, paper in enumerate(papers):
            title = (
                labels[paper]
                if source_label_map is not None
                else labels.get(paper, 'one included source' if pos == 0 else 'the other included source')
            )
            evidence_rows.append({'paper_key': paper, 'title': title})
        model_input = {
            'candidate_id': 'shared_source_comparison', 'candidate_output_scope': wire,
            'hierarchy': {'claim_group_ids': [group_id], 'shard_id': 'source_comparison'},
            'shared_semantic_context': request['shared_semantic_context'],
            'evidence': evidence_rows,
        }
        grounded = _compact_candidate(node_id, model_input)
        texts = [row['text'] for section in grounded['sections'] for row in section['claims']]
        assert len(texts) == 1
        fragments.append({'group_index': index, 'materialization_state': 'materialized',
                          'text': texts[0], 'reason_code': None})
    response = {'schema_version': 'candidate-comparison-fragments-output/v1',
                'parent_scope_hash': request['parent_scope_hash'], 'scope_hash': request['scope_hash'],
                'fragments': fragments}
    validate_candidate_comparison_fragments_output_v1(response, request)
    return response


def _family(text: str) -> str:
    folded = text.casefold()
    # These conditions are explicitly printed by the varied synthetic PDFs.
    for label, terms in (
        ('coupon', ('fixed-value coupon', 'digital coupon')),
        ('reference price', ('reference-price label', 'reference price')),
        ('loyalty', ('loyalty-points', 'loyalty-card', 'loyalty points')),
        ('percentage price', ('percentage-off', 'percentage off')),
        ('threshold', ('member-only threshold', 'minimum basket', 'threshold offer')),
        ('delivery', ('free-shipping', 'delivery fees')),
        ('multibuy', ('multi-buy', 'multi-item')),
        ('display', ('aisle-end', 'shelf placement')),
        ('rebate', ('cashback', 'rebate')),
        ('bundle', ('bundle-price', 'bundle price', 'separate item prices')),
    ):
        if any(term in folded for term in terms):
            return label
    if any(phrase in folded for phrase in ('no measurable', 'zero effect', 'did not change')):
        return 'null findings'
    if any(word in folded for word in ('decreased', 'negative', 'reduced')):
        return 'contrasting findings'
    return 'source findings'


def _organization_response(request: Mapping[str, Any], *, preferred_indices=()) -> dict[str, Any]:
    logical = {key: value for key, value in request.items() if key != '_prompt_authority'}
    cards, texts = logical['fragment_cards'], logical['text_pool']
    materialized = {row[0]: row for row in cards if row[3] == 'materialized'}
    selected = set(logical['required_group_indices']) | set(preferred_indices)
    covered = {paper for index in selected for paper in materialized[index][2]}
    missing = set(logical['required_paper_indices']) - covered
    dimensions = logical['axis_payload']['preferred_dimensions']
    tasks = logical['task_refs']
    while missing:
        candidates = [row for row in materialized.values() if set(row[2]) & missing]
        def rank(row):
            new = len(set(row[2]) & missing)
            task = tasks[row[4]].casefold()
            focus = next((i for i, dimension in enumerate(dimensions) if dimension.casefold() in task), len(dimensions))
            text = texts[row[1]]
            return (-new, len(row[2]) - new, focus, -len(text), hashlib.sha256(text.encode()).hexdigest(), row[0])
        chosen = min(candidates, key=rank)
        selected.add(chosen[0])
        missing -= set(chosen[2])
    buckets: dict[str, list[int]] = {}
    comparison_indices = {row['group_index'] for row in logical['required_comparison_sections']}
    for index in sorted(selected - comparison_indices):
        row = materialized[index]
        label = 'cross-source comparisons' if len(row[2]) > 1 else _family(texts[row[1]])
        buckets.setdefault(label, []).append(index)
    catalog = logical['metadata_catalog']
    sections = [{
        'section_index': position, 'metadata_index': row['metadata_index'],
        'fragment_indices': [row['group_index']],
        'paper_roles': [[paper, row['group_index']] for paper in row['paper_indices']],
    } for position, row in enumerate(logical['required_comparison_sections'])]
    for position, (family, indices) in enumerate(sorted(buckets.items())):
        papers = sorted({paper for index in indices for paper in materialized[index][2]})
        metadata = next((i for i, row in enumerate(catalog)
                         if family in row['title'].casefold()), position % len(catalog))
        sections.append({'section_index': len(sections), 'metadata_index': metadata,
                         'fragment_indices': indices,
                         'paper_roles': [[paper, next(index for index in indices if paper in materialized[index][2])]
                                         for paper in papers]})
    deferred = []
    for state, reason in (('unresolved', 'optional_unresolved'), ('materialized', 'not_selected_for_axis')):
        indices = [row[0] for row in cards if row[0] not in selected and row[3] == state]
        if indices:
            deferred.append({'reason_code': reason, 'fragment_indices': indices})
    headers = ('candidate_id', 'axis_hash', 'plan_hash', 'bank_hash', 'map_schema_hash',
               'metadata_catalog_hash', 'metadata_catalog_contract_hash', 'required_group_indices',
               'required_paper_indices', 'organization_contract_hash')
    response = {'schema_version': 'candidate-organization-map/v1',
                **{key: logical[key] for key in headers}, 'sections': sections, 'deferred': deferred}
    validate_candidate_organization_map_for_request_v1(response, logical)
    return response


def candidate_fragment_fixture_response(
    node_id: str,
    request: Mapping[str, Any],
    *,
    source_label_map: Mapping[str, str] | None = None,
) -> dict[str, Any] | None:
    if request.get('schema_version') == 'candidate-comparison-fragments-request/v1':
        return _comparison_response(node_id, request, source_label_map=source_label_map)
    if request.get('schema_version') == 'candidate-organization-request/v1':
        return _organization_response(request)
    return None
