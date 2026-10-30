"""Shared candidate calls must bind the actual transport identity."""
import pytest

from runtime.provider_runtime import hash_json
from tests.test_run_first_candidate_adversarial import (
    _LocalCandidateAPI, _executor, _sealed_relation_scope,
)


@pytest.mark.parametrize('node,artifact_type', [
    ('candidate_comparison_fragments_provider:source-owned', 'candidate_comparison_fragments_result'),
    ('candidate_organization_provider:candidate_1', 'candidate_organization_map_result'),
])
def test_shared_candidate_provider_binding_retains_actual_route(tmp_path, node, artifact_type):
    workspace, registry, summaries, _inventory, scope, _context = _sealed_relation_scope(tmp_path)
    with _LocalCandidateAPI() as server:
        executor = _executor(workspace, registry, summaries, server, scope)
        route = executor._node_route(node)
        request = executor._attach_prompt_authority(node, {'source_hash': scope.content_hash})
        binding = executor._provider_binding(node, request, expect_json=True,
                                             input_artifact_hashes=(scope.content_hash,))
        assert binding['provider_family'] == route.provider_name
        assert binding['model_name'] == route.model
        assert binding['endpoint_type'] == route.endpoint_type
        assert binding['api_base_host'] == route.api_base_host
        assert binding['route_fingerprint'] == route.safe_config_fingerprint()
        assert binding['provider_config_hash'] == hash_json(executor._route_transport_identity(route))
        assert binding['prompt_payload_hash'] == hash_json(request)
        assert binding['artifact_type'] == artifact_type
        assert not server.requests


def test_compiled_candidate_binding_is_local_and_receipt_free(tmp_path):
    workspace, registry, summaries, _inventory, scope, _context = _sealed_relation_scope(tmp_path)
    with _LocalCandidateAPI() as server:
        executor = _executor(workspace, registry, summaries, server, scope)
        binding = executor.build_current_node_binding(
            'candidate_1_provider_generation', artifact_type='outline_candidate',
            dependency_hashes={'bank': scope.content_hash}, model='deterministic',
            provider='local', provider_execution=False,
        )
        assert binding['provider_family'] == 'local'
        assert binding['model_name'] == 'deterministic'
        assert binding['endpoint_type'] == 'internal'
        assert binding['provider_route'] == 'local'
        assert all(binding[key] == '' for key in (
            'api_base_host', 'route_fingerprint', 'provider_config_hash', 'prompt_id', 'prompt_sha256',
        ))
        assert not executor.receipts
        assert not server.requests
