"""A later audit publication must preserve an earlier recovery dependency."""
from pathlib import Path

import pytest

from runtime.provider_runtime import hash_text
from services.artifact_registry import RegistryError
from tests.test_outline_v3_semantic_execution import _executor
from tests.test_topic_batch_incremental_persistence import _request


def _call(executor, node):
    executor._run_semantic_provider_call(
        node, _request(executor), {"global_navigation": hash_text("navigation")},
    )


def test_same_epoch_audit_publication_preserves_prior_dependency(tmp_path):
    executor = _executor(tmp_path, stability_mode="off")
    _call(executor, "topic_synthesis_provider:batch:1")
    executor._persist_audit_evidence()
    original = executor.artifact_records["request_payload_audit"]
    consumer = executor._persist_semantic_provider_output(
        "topic_synthesis_provider:retained", {"preserved": True},
        dependency_hashes={}, update_parent_node=False,
        recovery_sources=(original,),
    )
    executor.registry.verify_ready_artifact_closure(consumer)

    _call(executor, "topic_synthesis_provider:batch:2")
    executor._audit_artifacts_persisted = False
    executor._persist_audit_evidence()
    current = executor.artifact_records["request_payload_audit"]
    assert current.artifact_id != original.artifact_id
    assert executor.registry.get(original.artifact_id) == original
    executor.registry.verify_ready_artifact_closure(original)
    executor.registry.verify_ready_artifact_closure(current)
    executor.registry.verify_ready_artifact_closure(consumer)


def test_tampered_prior_audit_still_rejects_dependent_output(tmp_path):
    executor = _executor(tmp_path, stability_mode="off")
    _call(executor, "topic_synthesis_provider:batch:1")
    executor._persist_audit_evidence()
    audit = executor.artifact_records["request_payload_audit"]
    consumer = executor._persist_semantic_provider_output(
        "topic_synthesis_provider:retained", {"preserved": True},
        dependency_hashes={}, update_parent_node=False,
        recovery_sources=(audit,),
    )
    Path(audit.path).write_bytes(b'{}\n')
    with pytest.raises(RegistryError):
        executor.registry.verify_ready_artifact_closure(consumer)
