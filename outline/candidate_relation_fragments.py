"""Source-closed comparison text shared by the organization candidates."""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
import re
from typing import Any

from outline.candidate_output_scope import CandidateOutputScopeV1, _string_ids
from outline.candidate_request_packing import project_semantic_context
from outline.evidence_alias import alias_structural
from runtime.provider_runtime import hash_json

REQUEST_VERSION = 'candidate-comparison-fragments-request/v1'
OUTPUT_VERSION = 'candidate-comparison-fragments-output/v1'
_HASH = re.compile(r'[0-9a-f]{64}')
_OUTPUT_KEYS = {'schema_version', 'parent_scope_hash', 'scope_hash', 'fragments'}
_ROW_KEYS = {'group_index', 'materialization_state', 'text', 'reason_code'}


class CandidateComparisonFragmentError(ValueError):
    pass


def build_candidate_comparison_fragments_request_v1(
    scope: CandidateOutputScopeV1,
    group_ids: Sequence[str],
    source_context: Mapping[str, Any],
    *,
    required_group_ids: Sequence[str],
    parent_scope_hash: str | None = None,
    alias_map: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Project complete source supports; neither candidate nor axis enters the input.

    The executor may pass its operation-local, already verified parent hash.
    This function does not establish Registry or provider authority.
    """
    local = scope.for_groups(group_ids)
    if any(not group.claim_seed for group in local.claim_groups):
        raise CandidateComparisonFragmentError('shared comparison tasks must be existing comparison seeds')
    required = set(_string_ids(required_group_ids, 'required comparison groups'))
    if not required.issubset({group.claim_group_id for group in scope.claim_groups}):
        raise CandidateComparisonFragmentError('required comparison references a foreign scope group')
    parent_hash = parent_scope_hash if parent_scope_hash is not None else scope.content_hash
    if not isinstance(parent_hash, str) or not _HASH.fullmatch(parent_hash):
        raise CandidateComparisonFragmentError('parent scope hash is invalid')
    group_keys = [group.claim_group_id for group in local.claim_groups]
    # Comparison authors read the owned primary/qualifier source tables. They
    # do not need another copy of every already completed topic synthesis.
    direct_context = {
        'interpretation_source_tables': source_context.get('interpretation_source_tables'),
        'interpretation_source_contract': source_context.get('interpretation_source_contract', ''),
        'planned_evidence_policy': 'Compare the owned primary source facts with all recorded qualifiers.',
        'topic_routes': [],
    }
    request: dict[str, Any] = {
        'schema_version': REQUEST_VERSION,
        'parent_scope_hash': parent_hash,
        'scope_hash': local.content_hash,
        'claim_group_ids': group_keys,
        'required_group_indices': [i for i, key in enumerate(group_keys) if key in required],
        'comparison_scope': local.to_dict(),
        'shared_semantic_context': project_semantic_context(direct_context, local),
        'output_contract': {
            'schema_version': OUTPUT_VERSION,
            'top_level_fields': sorted(_OUTPUT_KEYS),
            'fragment_fields': sorted(_ROW_KEYS),
            'coverage': 'Return exactly one fragment for every comparison group index, without duplicates.',
            'materialized': 'Use exact group_index, materialization_state=materialized, authored text, reason_code=null.',
            'unresolved': 'Only optional groups may use materialization_state=unresolved, text=null, reason_code=optional_unresolved.',
            'source_policy': (
                'Author a concise comparison from both supplied source sides, preserving zero/reversed results '
                'and every necessary condition. A relation or study label is not an empirical finding. '
                'Do not invent study ownership, sources, statistics, or observations.'
            ),
        },
    }
    if alias_map is not None:
        request = alias_structural(request, alias_map)
    request['comparison_scope_wire_hash'] = hash_json(request['comparison_scope'])
    _validate_request(request)
    return request


def _validate_request(request: Mapping[str, Any]) -> int:
    if request.get('schema_version') != REQUEST_VERSION:
        raise CandidateComparisonFragmentError('shared comparison request schema is unsupported')
    for key in ('parent_scope_hash', 'scope_hash', 'comparison_scope_wire_hash'):
        if not isinstance(request.get(key), str) or not _HASH.fullmatch(request[key]):
            raise CandidateComparisonFragmentError(f'{key} is invalid')
    scope = request.get('comparison_scope')
    if not isinstance(scope, Mapping) or hash_json(scope) != request['comparison_scope_wire_hash']:
        raise CandidateComparisonFragmentError('comparison scope wire bytes differ from their bound hash')
    if scope.get('schema_version') != 'outline-candidate-output-scope/v1' or scope.get('content_hash') != request['scope_hash']:
        raise CandidateComparisonFragmentError('comparison request has a foreign scope identity')
    groups, slots = scope.get('claim_groups'), scope.get('claim_slots')
    if not isinstance(groups, list) or not groups or not isinstance(slots, list) or not slots:
        raise CandidateComparisonFragmentError('comparison request requires complete source groups and slots')
    group_ids = request.get('claim_group_ids')
    if (not isinstance(group_ids, list)
            or any(not isinstance(key, str) or not key for key in group_ids)
            or len(group_ids) != len(set(group_ids))
            or [group.get('claim_group_id') for group in groups if isinstance(group, Mapping)] != group_ids):
        raise CandidateComparisonFragmentError('comparison group assignment is not exact')
    if any(not isinstance(group, Mapping) or group.get('claim_seed') is not True for group in groups):
        raise CandidateComparisonFragmentError('comparison request includes a non-seed group')
    slot_ids: set[str] = set()
    field_owners: dict[str, set[str]] = {}
    for group in groups:
        local_slots = [slot for slot in slots if isinstance(slot, Mapping) and slot.get('claim_group_id') == group['claim_group_id']]
        if not local_slots or {slot.get('claim_slot_id') for slot in local_slots} != set(group.get('support_slot_ids') or ()):
            raise CandidateComparisonFragmentError('comparison task lost complete source support')
        if {slot.get('paper_key') for slot in local_slots} != set(group.get('paper_keys') or ()):
            raise CandidateComparisonFragmentError('comparison task lost a source side')
        for slot in local_slots:
            slot_id = slot.get('claim_slot_id')
            if not isinstance(slot_id, str) or not slot_id or slot_id in slot_ids:
                raise CandidateComparisonFragmentError('comparison request repeats or omits a support slot')
            slot_ids.add(slot_id)
            for field_id in slot.get('source_field_ids') or ():
                field_owners.setdefault(field_id, set()).add(slot['paper_key'])
    if len(slot_ids) != len(slots):
        raise CandidateComparisonFragmentError('comparison request imports an unassigned support slot')
    tables = (request.get('shared_semantic_context') or {}).get('interpretation_source_tables')
    fields = tables.get('source_fields') if isinstance(tables, Mapping) else None
    if not isinstance(fields, list) or {row.get('source_field_id') for row in fields if isinstance(row, Mapping)} != set(field_owners):
        raise CandidateComparisonFragmentError('comparison request lost required qualifier/source text')
    for row in fields:
        if not isinstance(row, Mapping):
            raise CandidateComparisonFragmentError('comparison source text is not an object')
        field_id = row.get('source_field_id')
        if (not isinstance(field_id, str) or not isinstance(row.get('source_value'), str)
                or row.get('paper_key') not in field_owners.get(field_id, set())):
            raise CandidateComparisonFragmentError('comparison source text has a foreign owner')
    required = request.get('required_group_indices')
    if (not isinstance(required, list)
            or any(type(i) is not int or not 0 <= i < len(groups) for i in required)
            or len(required) != len(set(required))):
        raise CandidateComparisonFragmentError('required comparison indices are invalid')
    return len(groups)


def validate_candidate_comparison_fragments_output_v1(
    payload: Mapping[str, Any], request: Mapping[str, Any],
) -> None:
    """Validate the response against its exact source-bearing request, without IO."""
    count = _validate_request(request)
    if not isinstance(payload, Mapping) or set(payload) != _OUTPUT_KEYS or payload.get('schema_version') != OUTPUT_VERSION:
        raise CandidateComparisonFragmentError('comparison response schema is invalid')
    for key in ('parent_scope_hash', 'scope_hash'):
        if payload.get(key) != request[key]:
            raise CandidateComparisonFragmentError(f'comparison response changed {key}')
    rows = payload.get('fragments')
    if not isinstance(rows, list) or len(rows) != count:
        raise CandidateComparisonFragmentError('comparison response must cover every assigned group')
    used: set[int] = set()
    for row in rows:
        if not isinstance(row, Mapping) or set(row) != _ROW_KEYS:
            raise CandidateComparisonFragmentError('comparison response row has unknown or missing fields')
        index = row.get('group_index')
        if type(index) is not int or not 0 <= index < count or index in used:
            raise CandidateComparisonFragmentError('comparison response has duplicate or foreign group indices')
        used.add(index)
        state, text, reason = row.get('materialization_state'), row.get('text'), row.get('reason_code')
        if state == 'materialized':
            if not isinstance(text, str) or not text.strip() or reason is not None:
                raise CandidateComparisonFragmentError('materialized comparison requires exact authored text')
        elif state == 'unresolved':
            if index in request['required_group_indices']:
                raise CandidateComparisonFragmentError('required comparison remains unresolved')
            if text is not None or reason != 'optional_unresolved':
                raise CandidateComparisonFragmentError('optional unresolved comparison must be explicit')
        else:
            raise CandidateComparisonFragmentError('comparison materialization state is invalid')


def estimate_candidate_comparison_output_tokens_v1(
    request: Mapping[str, Any], estimate_tokens: Callable[[Any], int],
) -> int:
    """Complete response shape plus a source-derived authored-text reserve."""
    count = _validate_request(request)
    shape = {
        'schema_version': OUTPUT_VERSION, 'parent_scope_hash': request['parent_scope_hash'],
        'scope_hash': request['scope_hash'], 'fragments': [
            {'group_index': index, 'materialization_state': 'materialized', 'text': '', 'reason_code': None}
            for index in range(count)
        ],
    }
    fields = {row['source_field_id']: row['source_value']
              for row in request['shared_semantic_context']['interpretation_source_tables']['source_fields']}
    source_reserve = 0
    for group in request['comparison_scope']['claim_groups']:
        values_by_paper: dict[str, set[str]] = {}
        for slot in request['comparison_scope']['claim_slots']:
            if slot['claim_group_id'] == group['claim_group_id']:
                values_by_paper.setdefault(slot['paper_key'], set()).update(
                    fields[field_id] for field_id in slot['source_field_ids']
                )
        source_reserve += sum(estimate_tokens(value) for values in values_by_paper.values() for value in values)
    return estimate_tokens(shape) + 256 * count + source_reserve
