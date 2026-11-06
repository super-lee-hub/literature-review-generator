"""Registered result schemas bind the exact request and provider response."""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from outline.v3_artifacts import CandidateComparisonFragmentsResult, CandidateOrganizationMapResult
from runtime.artifact_validators import ArtifactSchemaError, validate_registered_artifact
from runtime.provider_runtime import hash_json
from tests.test_run_first_candidate_comparison_fragments import _case as _comparison_case, _output
from tests.test_run_first_candidate_fragment_bank import (
    _build_bank, _complete_map, _organization_context, _sealed_scope,
)


def _map_case(tmp_path):
    fixture = _sealed_scope(tmp_path)
    scope = fixture[-1]
    _canonical, bank = _build_bank(fixture)
    request, _axis, _plan, _catalog = _organization_context(
        bank, scope, candidate_id='candidate_1', required_group_ids=[],
    )
    unresolved = [row[0] for row in request['fragment_cards'] if row[3] == 'unresolved']
    response = _complete_map(request, deferred=[{
        'reason_code': 'optional_unresolved', 'fragment_indices': unresolved,
    }])
    return bank, request, response


def _result(cls, request, response, job='fragment-bank-job'):
    wire_request = deepcopy(request)
    wire_request['_prompt_authority'] = {'prompt_id': 'fixture', 'system_prompt': 'Fixture source policy.'}
    return cls(
        job_id=job, payload=deepcopy(response), provider_request=wire_request,
        dependency_hashes={'request_payload_hash': hash_json(wire_request)},
    )


def _validate(tmp_path, artifact, *, version=None, owner=None, body=None):
    path = tmp_path / 'registered-result.json'
    payload = body if body is not None else artifact.to_dict()
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
    record = SimpleNamespace(
        artifact_type=artifact.artifact_type,
        artifact_version=version or artifact.artifact_version,
        job_id=owner or artifact.job_id,
    )
    validate_registered_artifact(record, path)


def test_map_result_roundtrips_exact_response_and_canonical_request(tmp_path):
    _bank, request, response = _map_case(tmp_path)
    artifact = _result(CandidateOrganizationMapResult, request, response)
    assert artifact.to_dict()['payload'] == response
    assert CandidateOrganizationMapResult.from_dict(artifact.to_dict()).to_dict() == artifact.to_dict()
    _validate(tmp_path, artifact)
    changed = deepcopy(request)
    changed['axis_payload']['label'] = 'A different local axis'
    other = _result(CandidateOrganizationMapResult, changed, response)
    assert artifact.content_hash != other.content_hash
    with pytest.raises(ArtifactSchemaError):
        _validate(tmp_path, other)


@pytest.mark.parametrize('mutation', ['version', 'owner', 'missing_request', 'request_hash', 'free_fact', 'bool_index'])
def test_registered_map_result_rejects_context_and_schema_forgery(tmp_path, mutation):
    _bank, request, response = _map_case(tmp_path)
    if mutation == 'free_fact':
        response['sections'][0]['title'] = 'Unsupported new empirical assertion.'
    if mutation == 'bool_index':
        response['sections'][0]['fragment_indices'][0] = True
    artifact = _result(CandidateOrganizationMapResult, request, response)
    body = artifact.to_dict()
    if mutation == 'missing_request':
        body.pop('provider_request')
    if mutation == 'request_hash':
        body['dependency_hashes']['request_payload_hash'] = '0' * 64
    with pytest.raises(ArtifactSchemaError):
        _validate(tmp_path, artifact, body=body,
                  version='v1' if mutation == 'version' else None,
                  owner='foreign-job' if mutation == 'owner' else None)


def test_registered_comparison_result_preserves_exact_source_request(tmp_path):
    _scope, _context, _groups, request = _comparison_case(tmp_path)
    response = _output(request)
    artifact = _result(CandidateComparisonFragmentsResult, request, response, job='candidate-replay-job')
    _validate(tmp_path, artifact)
    response['fragments'][0]['group_index'] = True
    malformed = _result(CandidateComparisonFragmentsResult, request, response, job=artifact.job_id)
    with pytest.raises(ArtifactSchemaError):
        _validate(tmp_path, malformed)


def test_flat_bank_schema_rejects_foreign_job_or_modified_text(tmp_path):
    bank, _request, _response = _map_case(tmp_path)
    path = tmp_path / 'bank.json'
    record = SimpleNamespace(artifact_type='outline_candidate_fragment_bank', artifact_version='v1',
                             job_id=bank['scope_binding']['registry_job_id'])
    path.write_text(json.dumps(bank), encoding='utf-8')
    validate_registered_artifact(record, path)
    record.job_id = 'foreign-job'
    with pytest.raises(ArtifactSchemaError, match='another job'):
        validate_registered_artifact(record, path)
    record.job_id = bank['scope_binding']['registry_job_id']
    corrupted = deepcopy(bank)
    entry = next(row for row in corrupted['groups'] if row['materialization_state'] == 'materialized')
    entry['text'] = 'A replacement unsupported assertion.'
    path.write_text(json.dumps(corrupted), encoding='utf-8')
    with pytest.raises(ArtifactSchemaError):
        validate_registered_artifact(record, path)


@pytest.mark.parametrize('mutation', [None, 'planned_empty_task', 'owner', 'hash', 'slot', 'limit', 'float_limit', 'binding'])
def test_registered_candidate_scope_has_current_intrinsic_validator(tmp_path, mutation):
    from runtime.artifact_validators import CURRENT_PRODUCTION_ARTIFACT_TYPES

    scope = _sealed_scope(tmp_path)[-1]
    if mutation == 'planned_empty_task':
        from dataclasses import replace

        scope = replace(scope, task_ids=tuple(sorted((*scope.task_ids, 'topic_synthesis:unresolved'))))
    body = scope.to_dict()
    record = SimpleNamespace(
        artifact_type='outline_candidate_output_scope', artifact_version='v1',
        job_id=scope.registry_job_id,
        depends_on=[SimpleNamespace(artifact_id=scope.source_inventory_artifact_id,
                                    content_hash=scope.source_inventory_artifact_hash)],
    )
    if mutation == 'owner':
        record.job_id = 'foreign-job'
    elif mutation == 'hash':
        body['content_hash'] = '0' * 64
    elif mutation == 'slot':
        body['claim_slots'][0]['paper_key'] = 'foreign-paper'
    elif mutation == 'limit':
        body['limits']['max_support_rows'] += 1
    elif mutation == 'float_limit':
        body['limits']['max_claims'] = float(body['limits']['max_claims'])
    elif mutation == 'binding':
        record.depends_on = []
    if mutation in {'slot', 'limit'}:
        body['content_hash'] = hash_json({key: value for key, value in body.items() if key != 'content_hash'})
    path = tmp_path / 'registered-scope.json'
    path.write_text(json.dumps(body), encoding='utf-8')
    if mutation in {None, 'planned_empty_task'}:
        assert record.artifact_type in CURRENT_PRODUCTION_ARTIFACT_TYPES
        validate_registered_artifact(record, path)
    else:
        with pytest.raises(ArtifactSchemaError):
            validate_registered_artifact(record, path)
