"""Source-closed shared comparisons before candidate-specific organization."""
from copy import deepcopy

import pytest

from outline.candidate_relation_fragments import (
    CandidateComparisonFragmentError,
    build_candidate_comparison_fragments_request_v1,
    estimate_candidate_comparison_output_tokens_v1,
    validate_candidate_comparison_fragments_output_v1,
)
from outline.evidence_alias import build_alias_map
from runtime.provider_context import ProviderContextProfile
from tests.test_run_first_candidate_adversarial import _sealed_relation_scope


def _case(tmp_path, *, aliases=False):
    _workspace, _registry, _summaries, _inventory, scope, context = _sealed_relation_scope(tmp_path)
    groups = [group.claim_group_id for group in scope.claim_groups]
    request = build_candidate_comparison_fragments_request_v1(
        scope, groups, context, required_group_ids=groups[:1],
        alias_map=build_alias_map(scope.paper_keys, scope.selected_relation_ids) if aliases else None,
    )
    return scope, context, groups, request


def _output(request):
    return {
        'schema_version': 'candidate-comparison-fragments-output/v1',
        'parent_scope_hash': request['parent_scope_hash'],
        'scope_hash': request['scope_hash'],
        'fragments': [
            {'group_index': index, 'materialization_state': 'materialized',
             'text': 'The sources differ within their explicitly recorded bounded conditions.',
             'reason_code': None}
            for index in range(len(request['claim_group_ids']))
        ],
    }


@pytest.mark.parametrize('aliases', [False, True])
def test_shared_request_preserves_every_owned_side_and_qualifier_without_axis(tmp_path, aliases):
    scope, context, groups, request = _case(tmp_path, aliases=aliases)
    assert request['claim_group_ids'] == sorted(groups)
    assert request['parent_scope_hash'] == scope.content_hash
    assert request['scope_hash'] == scope.for_groups(groups).content_hash
    assert not {'candidate_id', 'organizing_axis', 'axis_hash', 'plan_hash'}.intersection(request)
    rows = request['shared_semantic_context']['interpretation_source_tables']['source_fields']
    expected_fields = {field for slot in scope.claim_slots for field in slot.source_field_ids}
    assert {row['source_field_id'] for row in rows} == expected_fields
    original_fields = {row['source_field_id']: row['source_value'] for row in context['interpretation_source_tables']['source_fields']}
    assert {row['source_field_id']: row['source_value'] for row in rows} == {
        key: original_fields[key] for key in expected_fields
    }
    assert all(slot['study_id'] == '' for slot in request['comparison_scope']['claim_slots'])
    papers = {slot['paper_key'] for slot in request['comparison_scope']['claim_slots']}
    assert len(papers) == 4
    assert all(paper.startswith('P') for paper in papers) if aliases else papers == set(scope.paper_keys)
    validate_candidate_comparison_fragments_output_v1(_output(request), request)


@pytest.mark.parametrize('bad_index', [True, -1, 99])
def test_output_rejects_foreign_or_noninteger_group_indices(tmp_path, bad_index):
    _scope, _context, _groups, request = _case(tmp_path)
    output = _output(request)
    output['fragments'][0]['group_index'] = bad_index
    with pytest.raises(CandidateComparisonFragmentError):
        validate_candidate_comparison_fragments_output_v1(output, request)


def test_optional_unresolved_is_explicit_but_required_cannot_be_deferred(tmp_path):
    _scope, _context, _groups, request = _case(tmp_path)
    output = _output(request)
    optional = next(i for i in range(len(output['fragments'])) if i not in request['required_group_indices'])
    output['fragments'][optional].update(materialization_state='unresolved', text=None, reason_code='optional_unresolved')
    validate_candidate_comparison_fragments_output_v1(output, request)
    output['fragments'][request['required_group_indices'][0]].update(
        materialization_state='unresolved', text=None, reason_code='optional_unresolved',
    )
    with pytest.raises(CandidateComparisonFragmentError, match='required'):
        validate_candidate_comparison_fragments_output_v1(output, request)


@pytest.mark.parametrize('mutation', ['missing', 'duplicate', 'foreign_hash', 'extra_claims', 'wire_tamper'])
def test_complete_response_and_request_identity_fail_closed(tmp_path, mutation):
    _scope, _context, _groups, request = _case(tmp_path)
    output = _output(request)
    if mutation == 'missing':
        output['fragments'].pop()
    elif mutation == 'duplicate':
        output['fragments'][1] = deepcopy(output['fragments'][0])
    elif mutation == 'foreign_hash':
        output['scope_hash'] = 'f' * 64
    elif mutation == 'extra_claims':
        output['claims'] = ['Unsupported extra fact.']
    else:
        request['comparison_scope']['claim_slots'][0]['source_field_ids'].pop()
    with pytest.raises(CandidateComparisonFragmentError):
        validate_candidate_comparison_fragments_output_v1(output, request)


def test_source_field_missing_blocks_locally_and_output_reserve_counts_all_groups(tmp_path):
    scope, context, groups, request = _case(tmp_path)
    profile = ProviderContextProfile.conservative(provider='fixture', model='outline', endpoint_type='internal')
    full = estimate_candidate_comparison_output_tokens_v1(request, profile.estimate_tokens)
    single = build_candidate_comparison_fragments_request_v1(scope, groups[:1], context, required_group_ids=groups[:1])
    assert full > estimate_candidate_comparison_output_tokens_v1(single, profile.estimate_tokens)
    context['interpretation_source_tables']['source_fields'].clear()
    with pytest.raises((CandidateComparisonFragmentError, ValueError), match='source text'):
        build_candidate_comparison_fragments_request_v1(scope, groups, context, required_group_ids=groups[:1])
