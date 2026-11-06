"""Canonical selected-source scope retained for later plan-bound replay checks."""

from copy import deepcopy

import pytest

from runtime.provider_runtime import hash_json
from runtime.source_revision import PROVIDER_SEMANTIC_CONTRACT_V2
from tests.test_topic_source_scope import source_case as _shared_source_case
from tests.test_topic_source_scope_integration import _wire


@pytest.fixture(name="source_case")
def selected_sources(tmp_path):
    versions = _shared_source_case.__wrapped__(tmp_path)
    for version in versions:
        version["executor"].provider_semantic_contract_version = PROVIDER_SEMANTIC_CONTRACT_V2
    return versions


def _scope(request):
    from outline.topic_source_scope import topic_source_replay_scope_v1

    return topic_source_replay_scope_v1(request)


def test_replay_scope_separates_unchanged_and_changed_selected_content(source_case):
    before, after = source_case
    old, new = _wire(before), _wire(after, ordinal=99)
    original = deepcopy(old)
    assert _scope(old) == _scope(new)
    assert _scope(old)["canonical_paper_keys"] == ["paper-4", "paper-5"]
    assert _scope(old)["selected_source_content_hash"] == (
        old["evidence_projection"]["selected_source_projection"]["content_hash"]
    )
    assert _scope(_wire(before, papers=("paper-0", "paper-9")))["selected_source_content_hash"] != (
        _scope(_wire(after, papers=("paper-0", "paper-9")))["selected_source_content_hash"]
    )
    assert old == original


def test_non_source_question_change_is_not_hidden_by_equal_source_scope(source_case):
    request = _wire(source_case[0])
    changed = deepcopy(request)
    changed["topics"][0]["question"] = "A different organization question."
    assert _scope(request)["selected_source_content_hash"] == _scope(changed)["selected_source_content_hash"]
    assert _scope(request)["non_source_scope_hash"] != _scope(changed)["non_source_scope_hash"]


def test_legacy_request_has_no_new_replay_scope(source_case):
    request = _wire(source_case[0])
    request.pop("provider_semantic_contract_version")
    assert _scope(request) is None


@pytest.mark.parametrize("value", [None, "", "A" * 64, "x" * 64])
def test_successor_scope_rejects_invalid_content_hash(source_case, value):
    from outline.topic_source_scope import TopicSourceScopeError

    request = _wire(source_case[0])
    request["evidence_projection"]["selected_source_projection"]["content_hash"] = value
    with pytest.raises(TopicSourceScopeError, match="content hash"):
        _scope(request)


def test_request_audit_captures_scope_without_changing_transport_body(source_case):
    executor = source_case[0]["executor"]
    request = _wire(source_case[0])
    original_hash = hash_json(request)
    node_id = executor._topic_provider_node_id(request, 1)
    route = executor._node_route(node_id, "candidate_1_provider_generation")
    index = executor._begin_request_payload_audit(
        node_id=node_id, request=request, route=route, profile=route.profile,
        binding={}, api_config={}, call_id="scope-capture-test", semantic_node_id="topic_synthesis",
        transport_node_id="candidate_1_provider_generation", replay_key_hash="replay-key",
        replay_status="pending", transport=None, budget=route.profile.estimate_request(request),
        effective_input_cap=32_000, requested_output_tokens=2_048,
    )
    row = executor._request_payload_audit[index]
    assert row["source_revision_scope"] == _scope(request)
    assert row["payload_hash"] == original_hash == hash_json(request)
    assert row["status"] == "pending"
    assert row.get("semantic_validation_status") != "accepted"
