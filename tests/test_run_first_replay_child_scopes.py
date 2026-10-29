"""Dynamic semantic reducer calls keep concrete replay scopes."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from runtime.outline_v3_replay import ModelCallReplayStore
from runtime.provider_runtime import hash_json, hash_text
from tests.test_outline_v3_semantic_execution import _executor
from tests.test_run_first_semantic_reducer_closure import (
    _bounded_noise_topics,
    _bridge_claims,
    _context_fields,
    _source_comparison,
    _topic_ids,
)


def _source_bound_provider(
    calls: list[str],
):
    comparison, source_claim_ids, evidence_ids, source_field_values = _source_comparison()
    del comparison

    def provider(node_id: str, request: Mapping[str, Any]) -> Mapping[str, Any]:
        calls.append(str(node_id))
        topic_ids = sorted(_topic_ids(request.get("topic_synthesis") or ()))
        fields = _context_fields(request.get("topic_synthesis") or ())
        prior_bridges = _bridge_claims(request.get("topic_synthesis") or ())
        fields_by_id = {
            str(field.get("source_field_id") or ""): field
            for field in fields
        }
        bridge_claims: list[dict[str, Any]] = []
        if "topic:b-c" in topic_ids:
            if set(source_field_values).issubset(fields_by_id):
                field_ids = sorted(source_field_values)
                claim_ids = sorted(source_claim_ids)
                bridge_evidence = sorted(evidence_ids)
                text = "The B/C comparison preserves the recorded boundaries: " + "; ".join(
                    str(fields_by_id[field_id]["source_value"])
                    for field_id in field_ids
                )
            elif prior_bridges:
                prior = next(
                    (claim for claim in prior_bridges if "topic:b-c" in claim.get("topic_ids", ())),
                    prior_bridges[0],
                )
                field_ids = []
                claim_ids = [str(prior["claim_id"])]
                bridge_evidence = sorted(evidence_ids)
                text = "The earlier B/C comparison remains qualified by its source boundary."
            else:
                field_ids = []
                claim_ids = []
                bridge_evidence = []
                text = ""
            if claim_ids and bridge_evidence:
                bridge_claims.append({
                    "claim_id": f"synthesis:cross_group_comparison:{hash_text(node_id)[:12]}",
                    "topic_ids": ["topic:b-c"],
                    "fragment_id": "fragment:b-c",
                    "paper_keys": ["paper:b", "paper:c"],
                    "text": text,
                    "source_claim_ids": claim_ids,
                    "evidence_ids": bridge_evidence,
                    **({"source_field_ids": field_ids} if field_ids else {}),
                })

        bridge_ids = [str(claim["claim_id"]) for claim in bridge_claims]
        return {
            "status": "success",
            "content": {
                "comparisons": [],
                "bridge_claims": bridge_claims,
                "topic_dispositions": [
                    ({
                        "topic_id": topic_id,
                        "status": "integrated",
                        "synthesis_claim_ids": bridge_ids,
                    } if topic_id == "topic:b-c" and bridge_claims else {
                        "topic_id": topic_id,
                        "status": "unresolved",
                        "reason": "No source-complete comparison claim was returned for this topic.",
                    })
                    for topic_id in topic_ids
                ],
                "unresolved_questions": [],
            },
        }

    return provider


def _request() -> dict[str, Any]:
    topics = _bounded_noise_topics(64)
    topics[0] = _source_comparison()[0]
    return {
        "task": "substantive_cross_group_comparison",
        "node_id": "cross_group_comparison",
        "semantic_contract_version": "semantic-evidence-graph-v2",
        "shared_synthesis_contract_version": "v1",
        "topic_synthesis": topics,
        "relation_candidates": [],
        "questions": ["Compare the B/C findings while preserving their source boundaries."],
        "output_contract": {},
    }


def test_multilevel_reducer_children_resume_without_calls_and_tampering_is_stale(tmp_path):
    """Sibling reduce slots are distinct replay identities within one run."""

    calls: list[str] = []
    executor = _executor(
        tmp_path,
        provider=_source_bound_provider(calls),
        stability_mode="off",
        max_source_prompt_tokens=8_000,
    )
    request = _request()
    dependencies = {"topic_synthesis": "source-input-hash"}

    first = executor._run_bounded_semantic_provider_call(
        "cross_group_comparison_provider", request, dependencies,
    )
    executor._persist_semantic_provider_output(
        "cross_group_comparison_provider", first, dependency_hashes=dependencies,
    )
    first_calls = list(calls)
    assert first_calls

    replay_store = ModelCallReplayStore(executor.workspace)
    records = replay_store._read_records()
    reducer_records = [
        row for row in records
        if ":reduce:" in row.audit_node_id
        and row.audit_node_id.startswith("cross_group_comparison_provider:")
    ]
    assert len(reducer_records) >= 4, [row.audit_node_id for row in reducer_records]
    child_ids = {row.audit_node_id for row in reducer_records}
    assert len(child_ids) == len(reducer_records)
    assert all(row.key.node_id == row.audit_node_id for row in reducer_records)
    assert any(row.audit_node_id.endswith(":reduce:1:2") for row in reducer_records)

    resumed = executor._run_bounded_semantic_provider_call(
        "cross_group_comparison_provider", request, dependencies,
    )
    executor._persist_semantic_provider_output(
        "cross_group_comparison_provider", resumed, dependency_hashes=dependencies,
    )
    assert hash_json(resumed) == hash_json(first)
    assert calls == first_calls, "an exact local resume must not invoke the provider again"
    assert all(replay_store.lookup(row.key).reusable for row in reducer_records)

    child = next(row for row in reducer_records if row.audit_node_id.endswith(":reduce:1:2"))
    prompt_tamper = replay_store.lookup(
        replace(child.key, prompt_payload_hash=hash_text("tampered prompt")),
    )
    input_tamper = replay_store.lookup(
        replace(child.key, input_artifact_hashes=["tampered source input"]),
    )
    binding_tamper = replay_store.lookup(
        replace(child.key, execution_binding_hash=hash_text("tampered source binding")),
    )
    assert prompt_tamper.status == input_tamper.status == binding_tamper.status == "stale"
    assert "prompt_payload_changed" in prompt_tamper.stale_reasons
    assert "input_artifacts_changed" in input_tamper.stale_reasons
    assert "execution_binding_changed" in binding_tamper.stale_reasons


