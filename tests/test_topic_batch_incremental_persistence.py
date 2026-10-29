"""A later topic failure must retain each already validated provider result."""

from collections.abc import Mapping

import pytest

from outline.semantic_chunking import TopicRoute, build_paper_content_layers
from outline.v3_evidence import build_outline_evidence_views
from outline.v3_executor import OutlineV3ExecutionError
from outline.v3_models import TopicSynthesis
from runtime.provider_runtime import hash_text
from tests.test_outline_v3_semantic_execution import _configured_test_provider, _executor


def _request(executor):
    evidence = build_outline_evidence_views(executor.summaries, executor.job_id)
    layers = build_paper_content_layers(executor.summaries, evidence, job_id=executor.job_id)
    topic = TopicSynthesis(
        topic_id="topic:retain-first", fragment_id="fragment:retain-first",
        paper_ids=["paper-a"],
        supporting_evidence_ids=list(layers.dossier_by_paper["paper-a"].evidence_ids),
    )
    return executor._build_topic_provider_request(
        [topic], topic_routes={topic.topic_id: TopicRoute(
            topic_id=topic.topic_id, question="Assess the recorded source finding.",
            paper_ids=["paper-a"], dimensions=["finding"],
        )}, evidence_model=evidence, content_layers_model=layers, batch_index=1,
    )


def test_completed_topic_is_durable_before_a_later_batch_fails(tmp_path):
    calls = []

    def provider(node_id: str, request: Mapping):
        calls.append(node_id)
        if node_id.endswith(":batch:2"):
            return {"status": "failed", "error_kind": "transient_network", "http_status": 503}
        return _configured_test_provider(node_id, request)

    first = _executor(tmp_path, provider=provider, stability_mode="off")
    assert first.outline_pilot is None
    request = _request(first)
    dependencies = {"global_navigation": hash_text("navigation"), "semantic_chunk_plan": hash_text("plan")}
    node = "topic_synthesis_provider:batch:1"
    result = first._run_semantic_provider_call(node, request, dependencies)
    with pytest.raises(OutlineV3ExecutionError):
        first._run_semantic_provider_call("topic_synthesis_provider:batch:2", request, dependencies)

    record = first.registry.get(f"outline-v3:semantic-provider:{hash_text(node)[:24]}")
    assert record is not None and record.status == "ready"
    first.registry.verify_ready_artifact_closure(record)
    assert first.registry.get("outline-v3:topic_synthesis") is None

    def forbid_transport(*_args, **_kwargs):
        pytest.fail("the completed first batch must replay without another provider call")

    resumed = _executor(tmp_path, provider=forbid_transport, stability_mode="off")
    assert resumed._run_semantic_provider_call(node, _request(resumed), dependencies) == result
    assert calls == [node, "topic_synthesis_provider:batch:2"]
