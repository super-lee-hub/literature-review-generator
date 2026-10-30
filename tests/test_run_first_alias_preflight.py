"""Read-only candidate sharding before any alias artifact is published."""

from outline.v3_evidence import build_outline_evidence_views
from tests.test_outline_v3_semantic_execution import _executor, _summary


def test_opaque_candidate_preflight_can_shard_before_alias_map_publication(tmp_path):
    summaries = [_summary("paper-a", "A", "A finding."), _summary("paper-b", "B", "B finding.")]
    executor = _executor(tmp_path, stability_mode="off")
    executor.opaque_alias_enabled = True
    evidence = build_outline_evidence_views(summaries, executor.job_id)
    shards = executor._candidate_shard_requests(
        generation_node_id="candidate_1_provider_generation",
        provider_request={"candidate_id": "candidate_1", "paper_keys": ["paper-a", "paper-b"]},
        evidence_views=evidence.views, relation_candidates=[],
    )
    assert shards
    assert {paper for _node, _request, papers, _relations, _assignment in shards for paper in papers} == {"paper-a", "paper-b"}
    assert executor._alias_map is None
    assert not any(record.artifact_type == "outline_evidence_alias_map" for record in executor.registry.list_records())
