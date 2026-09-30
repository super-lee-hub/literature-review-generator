"""Focused offline regressions for PR25 V3-01 through V3-05.

The fixtures exercise the production request builders, candidate merger, and
candidate validator. Provider responses are local mappings; no external
provider or corpus is used.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping

import pytest

import outline.v3_executor as executor_module
from outline.v3_executor import OutlineV3ExecutionError
from runtime.provider_context import ProviderContextProfile
from runtime.outline_v3_dag import OutlineNodeRecord
from runtime.provider_runtime import ProviderRuntimeLedger, hash_json, hash_text
from test_outline_v3_semantic_execution import _configured_test_provider, _executor


@pytest.mark.parametrize("target_tokens", [0, 50_000])
def test_v3_01_preflight_uses_effective_cap_when_choosing_hierarchical_relation_requests(
    tmp_path, monkeypatch: pytest.MonkeyPatch, target_tokens: int,
) -> None:
    executor = _executor(
        tmp_path,
        stability_mode="off",
        technical_shard_target_tokens=target_tokens,
        max_source_prompt_tokens=32_000,
    )
    candidate = {"relation_id": "R1", "paper_keys": ["A", "B"]}
    relation = SimpleNamespace(relation_id="R1", to_dict=lambda: candidate)
    evidence = SimpleNamespace(views=[], source_summary_hashes=[])
    content_layers = SimpleNamespace(content_hash="layers-hash", dossiers=[])
    semantic_plan = SimpleNamespace(
        coverage={"selected_relation_ids": ["R1"]},
        relation_bundles=[SimpleNamespace(relation_id="R1", to_dict=lambda: candidate)],
        content_hash="semantic-plan-hash",
    )
    shard_plan = {"target_tokens": target_tokens, "shard_count": 2, "shards": []}

    monkeypatch.setattr(executor_module, "build_outline_evidence_views", lambda *_: evidence)
    monkeypatch.setattr(executor_module, "build_global_corpus_ledger", lambda *_: object())
    monkeypatch.setattr(executor_module, "build_multi_view_matrix", lambda *_: object())
    monkeypatch.setattr(
        executor_module,
        "build_global_relation_map",
        lambda *_: SimpleNamespace(relations=[relation]),
    )
    monkeypatch.setattr(executor_module, "build_paper_content_layers", lambda *_args, **_kwargs: content_layers)
    monkeypatch.setattr(executor_module, "build_semantic_chunk_plan", lambda *_args, **_kwargs: semantic_plan)
    monkeypatch.setattr(executor, "_build_relation_shard_plan", lambda *_: shard_plan)
    monkeypatch.setattr(
        executor,
        "_relation_provider_request",
        lambda **_: (
            {
                "fixture": "flat",
                "relation_adjudication_contract": {"allowed_relation_ids": ["R1"]},
            },
            [candidate],
            [],
        ),
    )
    monkeypatch.setattr(executor, "_relation_local_requests", lambda **_: [])
    monkeypatch.setattr(
        executor,
        "_relation_cross_batch_requests",
        lambda **_: [("relation_adjudication:cross_shard", {"fixture": "shard"}, {"R1"})],
    )
    monkeypatch.setattr(executor, "_attach_prompt_authority", lambda _node_id, request: dict(request))

    def estimate_request(request: Mapping[str, Any]) -> dict[str, Any]:
        estimate = 37_397 if request.get("fixture") == "flat" else 20_000
        return {"estimated_input_tokens": estimate, "within_budget": True}

    profile = SimpleNamespace(
        input_budget=100_000,
        estimate_request=estimate_request,
        estimate_tokens=lambda request: estimate_request(request)["estimated_input_tokens"],
    )

    _, call_upper, details = executor._relation_hierarchical_preflight(
        executor.summaries,
        profile,
        variant_name="canonical",
    )

    assert call_upper == 1
    assert details["hierarchical_needed"] is True


def test_v3_01_effective_cap_never_exceeds_transport_hard_ceiling(tmp_path) -> None:
    executor = _executor(
        tmp_path,
        stability_mode="off",
        max_source_prompt_tokens=48_000,
    )
    profile = ProviderContextProfile.conservative(
        provider="fixture",
        model="large-context-fixture",
        endpoint_type="fixture",
        model_context_limit=128_000,
        max_output_tokens=4_096,
    )

    assert profile.input_budget > 64_000
    assert executor._effective_input_cap(profile) == 32_000


def test_v3_01_candidate_shard_runner_enforces_the_transport_hard_cap(tmp_path, monkeypatch) -> None:
    executor = _executor(
        tmp_path,
        stability_mode="off",
        max_source_prompt_tokens=48_000,
    )
    profile = SimpleNamespace(
        input_budget=100_000,
        max_output_tokens=4_096,
        estimate_request=lambda _request: {"estimated_input_tokens": 40_000},
    )
    monkeypatch.setattr(executor, "_node_route", lambda _node_id: SimpleNamespace(profile=profile))
    monkeypatch.setattr(
        executor,
        "_candidate_shard_requests",
        lambda **_kwargs: [("candidate_1_provider_generation:local:shard_1", {}, [], [], {"fixture": True})],
    )
    monkeypatch.setattr(executor, "_attach_prompt_authority", lambda _node_id, request: dict(request))
    monkeypatch.setattr(
        executor,
        "_provider_call",
        lambda *_args, **_kwargs: pytest.fail("oversized candidate request reached provider transport"),
    )

    with pytest.raises(OutlineV3ExecutionError, match="estimate 40000 exceeds effective input cap 32000"):
        executor._run_hierarchical_candidate_generation(
            candidate_id="candidate_1",
            generation_node_id="candidate_1_provider_generation",
            provider_request={"candidate_id": "candidate_1"},
            evidence_views=[],
            relation_candidates=[],
            allowed_paper_keys=[],
            allowed_relation_ids=[],
            generation_deps={},
            alias_map={},
        )


def test_v3_01_relation_adjudicator_enforces_the_transport_hard_cap(tmp_path, monkeypatch) -> None:
    executor = _executor(
        tmp_path,
        stability_mode="off",
        max_source_prompt_tokens=48_000,
    )
    profile = SimpleNamespace(
        input_budget=100_000,
        estimate_request=lambda _request: {"estimated_input_tokens": 40_000},
    )
    monkeypatch.setattr(executor, "_role_route", lambda _role: SimpleNamespace(profile=profile))
    monkeypatch.setattr(
        executor,
        "_relation_compact_batch_requests",
        lambda **_kwargs: [("relation_adjudication:batch_1", {"fixture": True}, {"R1"})],
    )
    monkeypatch.setattr(executor, "_attach_prompt_authority", lambda _node_id, request: dict(request))
    monkeypatch.setattr(
        executor,
        "_provider_call",
        lambda *_args, **_kwargs: pytest.fail("oversized relation request reached provider transport"),
    )

    with pytest.raises(OutlineV3ExecutionError, match="estimate 40000 exceeds effective input cap 32000"):
        executor._run_hierarchical_relation_adjudication(
            evidence_views=[],
            relation_candidates=[{"relation_id": "R1", "paper_keys": ["A", "B"]}],
            shard_plan={},
            relation_contract={},
            relation_dependencies={},
            compact_request={"relation_candidates": []},
        )


def test_v3_01_cross_relation_packing_uses_the_effective_cap_not_the_larger_target(
    tmp_path,
) -> None:
    executor = _executor(
        tmp_path,
        stability_mode="off",
        technical_shard_target_tokens=50_000,
        max_source_prompt_tokens=32_000,
    )
    candidate_by_id = {
        "R1": {"relation_id": "R1", "paper_keys": ["A"]},
        "R2": {"relation_id": "R2", "paper_keys": ["B"]},
    }
    shard_plan = {
        "shards": [{
            "evidence_chunks": [
                {"evidence_chunk_id": "EA", "paper_key": "A", "text": "source A"},
                {"evidence_chunk_id": "EB", "paper_key": "B", "text": "source B"},
            ],
        }],
    }

    def estimate_tokens(request: Mapping[str, Any]) -> int:
        return 20_000 * len(request.get("relation_candidates") or ())

    profile = SimpleNamespace(
        input_budget=100_000,
        estimate_tokens=estimate_tokens,
        estimate_request=lambda request: {
            "estimated_input_tokens": estimate_tokens(request),
            "within_budget": True,
        },
    )
    batches = executor._relation_cross_batch_requests(
        candidate_by_id=candidate_by_id,
        relation_ids=["R1", "R2"],
        shard_plan=shard_plan,
        relation_contract={"allowed_relation_ids": ["R1", "R2"]},
        profile=profile,
    )

    assert len(batches) == 2
    assert all(
        profile.estimate_request(request)["estimated_input_tokens"] <= 32_000
        for _node_id, request, _relation_ids in batches
    )


def test_v3_01_zero_target_uses_automatic_bounded_cross_relation_packing(tmp_path) -> None:
    executor = _executor(
        tmp_path,
        stability_mode="off",
        technical_shard_target_tokens=0,
        max_source_prompt_tokens=32_000,
    )
    candidate_by_id = {
        "R1": {"relation_id": "R1", "paper_keys": ["A"]},
        "R2": {"relation_id": "R2", "paper_keys": ["B"]},
    }
    shard_plan = {
        "shards": [{"evidence_chunks": [
            {"evidence_chunk_id": "EA", "paper_key": "A", "text": "source A"},
            {"evidence_chunk_id": "EB", "paper_key": "B", "text": "source B"},
        ]}],
    }

    def estimate_tokens(request: Mapping[str, Any]) -> int:
        return 20_000 * len(request.get("relation_candidates") or ())

    profile = SimpleNamespace(
        input_budget=100_000,
        estimate_tokens=estimate_tokens,
        estimate_request=lambda request: {
            "estimated_input_tokens": estimate_tokens(request),
            "within_budget": True,
        },
    )
    batches = executor._relation_cross_batch_requests(
        candidate_by_id=candidate_by_id,
        relation_ids=["R1", "R2"],
        shard_plan=shard_plan,
        relation_contract={"allowed_relation_ids": ["R1", "R2"]},
        profile=profile,
    )

    assert len(batches) == 2
    assert set().union(*(relation_ids for _node_id, _request, relation_ids in batches)) == {"R1", "R2"}
    assert all(
        profile.estimate_request(request)["estimated_input_tokens"] <= 32_000
        for _node_id, request, _relation_ids in batches
    )


@pytest.mark.parametrize(("estimate", "blocked"), [(32_000, False), (32_001, True)])
def test_v3_01_automatic_packing_enforces_exact_hard_input_boundary(
    tmp_path, estimate: int, blocked: bool,
) -> None:
    executor = _executor(
        tmp_path,
        stability_mode="off",
        technical_shard_target_tokens=0,
        max_source_prompt_tokens=32_000,
    )
    candidate_by_id = {"R1": {"relation_id": "R1", "paper_keys": ["A"]}}
    shard_plan = {"shards": [{"evidence_chunks": [
        {"evidence_chunk_id": "EA", "paper_key": "A", "text": "source A"},
    ]}]}
    profile = SimpleNamespace(
        input_budget=100_000,
        estimate_tokens=lambda _request: estimate,
        estimate_request=lambda _request: {
            "estimated_input_tokens": estimate,
            "within_budget": True,
        },
    )

    if blocked:
        with pytest.raises(OutlineV3ExecutionError, match="indivisible complete relation request"):
            executor._relation_cross_batch_requests(
                candidate_by_id=candidate_by_id,
                relation_ids=["R1"],
                shard_plan=shard_plan,
                relation_contract={"allowed_relation_ids": ["R1"]},
                profile=profile,
            )
    else:
        batches = executor._relation_cross_batch_requests(
            candidate_by_id=candidate_by_id,
            relation_ids=["R1"],
            shard_plan=shard_plan,
            relation_contract={"allowed_relation_ids": ["R1"]},
            profile=profile,
        )
        assert len(batches) == 1
        assert profile.estimate_request(batches[0][1])["estimated_input_tokens"] == 32_000


def test_v3_01_production_compact_relation_packer_splits_at_effective_cap(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    executor = _executor(
        tmp_path,
        stability_mode="off",
        technical_shard_target_tokens=50_000,
        max_source_prompt_tokens=32_000,
    )
    monkeypatch.setattr(
        executor,
        "_attach_prompt_authority",
        lambda _node_id, request: dict(request),
    )
    candidate_rows = [
        {"relation_id": "R1", "paper_keys": ["A"]},
        {"relation_id": "R2", "paper_keys": ["B"]},
    ]
    bundle_rows = [
        {"relation_id": "R1", "evidence_ids": ["EA"]},
        {"relation_id": "R2", "evidence_ids": ["EB"]},
    ]
    profile = SimpleNamespace(
        input_budget=100_000,
        estimate_request=lambda request: {
            "estimated_input_tokens": 20_000 * len(request["relation_candidates"]),
            "within_budget": True,
        },
    )

    batches = executor._relation_compact_batch_requests(
        base_request={
            "relation_candidates": candidate_rows,
            "relation_evidence_bundles": bundle_rows,
            "relation_adjudication_contract": {},
        },
        relation_ids=["R1", "R2"],
        profile=profile,
    )

    assert len(batches) == 2
    assert set().union(*(relation_ids for _node_id, _request, relation_ids in batches)) == {
        "R1", "R2",
    }
    for _node_id, request, relation_ids in batches:
        assert profile.estimate_request(request)["estimated_input_tokens"] <= 32_000
        assert [item["relation_id"] for item in request["relation_candidates"]] == sorted(relation_ids)
        assert [item["relation_id"] for item in request["relation_evidence_bundles"]] == sorted(relation_ids)


@pytest.mark.parametrize(("estimate", "blocked"), [(32_000, False), (32_001, True)])
def test_v3_01_production_compact_relation_packer_enforces_exact_boundary(
    tmp_path, monkeypatch: pytest.MonkeyPatch, estimate: int, blocked: bool,
) -> None:
    executor = _executor(
        tmp_path,
        stability_mode="off",
        technical_shard_target_tokens=50_000,
        max_source_prompt_tokens=32_000,
    )
    monkeypatch.setattr(
        executor,
        "_attach_prompt_authority",
        lambda _node_id, request: dict(request),
    )
    profile = SimpleNamespace(
        input_budget=100_000,
        estimate_request=lambda _request: {
            "estimated_input_tokens": estimate,
            "within_budget": estimate <= 100_000,
        },
    )
    request = {
        "relation_candidates": [{"relation_id": "R1", "paper_keys": ["A"]}],
        "relation_evidence_bundles": [{"relation_id": "R1", "evidence_ids": ["EA"]}],
        "relation_adjudication_contract": {},
    }

    if blocked:
        with pytest.raises(OutlineV3ExecutionError, match="indivisible complete relation request"):
            executor._relation_compact_batch_requests(
                base_request=request,
                relation_ids=["R1"],
                profile=profile,
            )
    else:
        batches = executor._relation_compact_batch_requests(
            base_request=request,
            relation_ids=["R1"],
            profile=profile,
        )
        assert len(batches) == 1
        assert profile.estimate_request(batches[0][1])["estimated_input_tokens"] == 32_000


@pytest.mark.parametrize(
    ("target_tokens", "variant_name"),
    [
        (0, "canonical"),
        (50_000, "canonical"),
        (0, "stability:fixture"),
        (50_000, "stability:fixture"),
    ],
)
def test_v3_01_candidate_preflight_reserves_all_shards_under_effective_cap(
    tmp_path, monkeypatch: pytest.MonkeyPatch, target_tokens: int, variant_name: str,
) -> None:
    executor = _executor(
        tmp_path,
        stability_mode="off",
        technical_shard_target_tokens=target_tokens,
        max_source_prompt_tokens=32_000,
    )
    candidate = {"relation_id": "R1", "paper_keys": ["A"]}
    relation = SimpleNamespace(relation_id="R1", to_dict=lambda: candidate)
    evidence = SimpleNamespace(views=[], source_summary_hashes=["summary-hash"])
    candidates = SimpleNamespace(relations=[relation])
    content_layers = SimpleNamespace()
    semantic_plan = SimpleNamespace(
        coverage={"selected_relation_ids": ["R1"]},
        topics=[],
    )
    monkeypatch.setattr(executor_module, "build_outline_evidence_views", lambda *_: evidence)
    monkeypatch.setattr(executor_module, "build_global_corpus_ledger", lambda *_: object())
    monkeypatch.setattr(executor_module, "build_multi_view_matrix", lambda *_: object())
    monkeypatch.setattr(executor_module, "build_global_relation_map", lambda *_: candidates)
    monkeypatch.setattr(executor_module, "build_paper_content_layers", lambda *_args, **_kwargs: content_layers)
    monkeypatch.setattr(executor_module, "build_semantic_chunk_plan", lambda *_args, **_kwargs: semantic_plan)
    monkeypatch.setattr(executor, "_compact_candidate_evidence_refs", lambda *_: [])
    shard_requests = [
        (
            f"candidate_1_provider_generation:local:shard_{index}",
            {"shard_id": f"shard_{index}"},
            ["A"],
            ["R1"],
            {"fixture": "shard"},
        )
        for index in (1, 2)
    ]
    monkeypatch.setattr(executor, "_candidate_shard_requests", lambda **_: shard_requests)
    monkeypatch.setattr(executor, "_attach_prompt_authority", lambda _node_id, request: dict(request))

    def estimate_request(request: Mapping[str, Any]) -> dict[str, Any]:
        estimate = 20_000 if request.get("fixture") == "shard" else 37_397
        return {"estimated_input_tokens": estimate, "within_budget": True}

    profile = SimpleNamespace(
        input_budget=100_000,
        estimate_request=estimate_request,
        estimate_tokens=lambda request: estimate_request(request)["estimated_input_tokens"],
    )
    input_upper, call_upper = executor._candidate_hierarchical_preflight(
        [],
        profile,
        variant_name=variant_name,
    )

    assert input_upper == 32_000
    assert call_upper == 2


def test_v3_01_candidate_sharding_uses_candidate_route_cap_and_preserves_all_chunks(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    executor = _executor(
        tmp_path,
        stability_mode="off",
        technical_shard_target_tokens=0,
        max_source_prompt_tokens=32_000,
    )

    relation_route_profile = SimpleNamespace(
        input_budget=100_000,
        estimate_tokens=lambda request: 12_000 * len(request.get("evidence_views") or ()),
    )
    candidate_route_profile = SimpleNamespace(
        input_budget=16_000,
        estimate_tokens=lambda request: 12_000 * len(request.get("evidence_views") or ()),
    )
    monkeypatch.setattr(
        executor,
        "_role_route",
        lambda node_id: SimpleNamespace(
            profile=(
                relation_route_profile
                if node_id == "relation_adjudication"
                else candidate_route_profile
            )
        ),
    )
    views = [
        SimpleNamespace(paper_key="A", view_hash="view-A"),
        SimpleNamespace(paper_key="B", view_hash="view-B"),
    ]
    monkeypatch.setattr(
        executor,
        "_prompt_evidence_chunks",
        lambda view: [{
            "paper_key": view.paper_key,
            "evidence_chunk_id": f"chunk-{view.paper_key}",
            "evidence_source_view_hash": view.view_hash,
            "findings": [f"source {view.paper_key}"],
        }],
    )
    relation = {"relation_id": "R-cross", "paper_keys": ["A", "B"]}
    requests = executor._candidate_shard_requests(
        generation_node_id="candidate_1_provider_generation",
        provider_request={
            "candidate_id": "candidate_1",
            "organizing_logic": "evidence",
            "paper_keys": ["A", "B"],
            "relation_ids": ["R-cross"],
            "relations": [relation],
        },
        evidence_views=views,
        relation_candidates=[relation],
    )

    assert len(requests) == 2
    planned_chunks = [
        str(chunk["evidence_chunk_id"])
        for _node_id, shard, _papers, _relations, _request in requests
        for chunk in shard["evidence_chunks"]
    ]
    assert sorted(planned_chunks) == ["chunk-A", "chunk-B"]
    assert len(planned_chunks) == len(set(planned_chunks))


@pytest.mark.parametrize(
    ("candidate_path", "target_tokens"),
    [
        ("canonical", 0),
        ("canonical", 50_000),
        ("stability", 0),
        ("stability", 50_000),
    ],
)
def test_v3_01_candidate_dispatch_shards_against_hard_cap_for_canonical_and_stability(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    candidate_path: str,
    target_tokens: int,
) -> None:
    executor = _executor(
        tmp_path,
        provider=_configured_test_provider,
        stability_mode="smoke" if candidate_path == "stability" else "off",
        technical_shard_target_tokens=target_tokens,
        max_source_prompt_tokens=32_000,
    )
    provider_requests: list[tuple[str, Mapping[str, Any]]] = []
    original_provider_call = executor._provider_call

    def record_provider_call(node_id: str, request: Mapping[str, Any], **kwargs: Any) -> Any:
        provider_requests.append((node_id, dict(request)))
        return original_provider_call(node_id, request, **kwargs)

    monkeypatch.setattr(executor, "_provider_call", record_provider_call)
    original_estimate = ProviderContextProfile.estimate_request

    def estimate_candidate_request(
        profile: ProviderContextProfile,
        request: Mapping[str, Any],
    ) -> dict[str, Any]:
        candidate_id = str(request.get("candidate_id") or "")
        organizing_logic = str(request.get("organizing_logic") or "")
        hierarchy = request.get("hierarchy")
        if candidate_id and organizing_logic:
            if isinstance(hierarchy, Mapping) and hierarchy.get("level") == "candidate_local_shard":
                return {"estimated_input_tokens": 20_000, "within_budget": True}
            return {"estimated_input_tokens": 37_397, "within_budget": True}
        return original_estimate(profile, request)

    monkeypatch.setattr(ProviderContextProfile, "estimate_request", estimate_candidate_request)
    result = executor.run()

    candidate_shard_calls = [
        (node_id, request) for node_id, request in provider_requests
        if str(request.get("candidate_id") or "") == "candidate_1"
        and (request.get("hierarchy") or {}).get("level") == "candidate_local_shard"
    ]
    if candidate_path == "stability":
        candidate_shard_calls = [row for row in candidate_shard_calls if row[0].startswith("stability:")]
    else:
        candidate_shard_calls = [row for row in candidate_shard_calls if not row[0].startswith("stability:")]
    assert candidate_shard_calls, (
        f"status={result.status}; diagnostics={result.diagnostics}; "
        f"candidate_requests={[(node_id, request.get('hierarchy')) for node_id, request in provider_requests if request.get('candidate_id')]}"
    )
    assert not [
        node_id for node_id, request in provider_requests
        if str(request.get("candidate_id") or "") == "candidate_1"
        and (request.get("hierarchy") or {}).get("level") != "candidate_local_shard"
        and (candidate_path == "canonical" or node_id.startswith("stability:"))
    ]


def _install_candidate_shards(
    executor,
    monkeypatch: pytest.MonkeyPatch,
    *,
    outputs_by_paper: Mapping[str, Mapping[str, Any]],
    shared_section_ids: Mapping[str, Mapping[str, str]] | None = None,
    interpretation_tables: Mapping[str, Any] | None = None,
    alias_map: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    shared_blueprints = {
        str(section_id): dict(blueprint)
        for section_id, blueprint in (shared_section_ids or {}).items()
    }
    if interpretation_tables is not None:
        executor._candidate_interpretation_tables = dict(interpretation_tables)

    def provider(_node_id: str, request: Mapping[str, Any]) -> Mapping[str, Any]:
        paper_key = str((request.get("paper_keys") or [""])[0])
        return {"status": "success", "content": dict(outputs_by_paper[paper_key])}

    executor.provider = provider
    shards = []
    for index, paper_key in enumerate(outputs_by_paper, start=1):
        shard_id = f"shard_{index}"
        shard = {
            "shard_id": shard_id,
            "view_hashes": [f"view:{paper_key}"],
            "shared_section_ids": shared_blueprints,
        }
        request = {
            "candidate_id": "candidate_1",
            "paper_keys": [paper_key],
            "relation_ids": [],
            "hierarchy": {"shard_id": shard_id, "shared_section_ids": shared_blueprints},
        }
        shards.append((
            f"candidate_1_provider_generation:local:{shard_id}",
            shard,
            [paper_key],
            [],
            request,
        ))
    monkeypatch.setattr(executor, "_candidate_shard_requests", lambda **_: shards)

    return executor._run_hierarchical_candidate_generation(
        candidate_id="candidate_1",
        generation_node_id="candidate_1_provider_generation",
        provider_request={
            "candidate_id": "candidate_1",
            "organizing_logic": "evidence",
            "paper_keys": list(outputs_by_paper),
            "shared_section_ids": shared_blueprints,
        },
        evidence_views=[],
        relation_candidates=[],
        allowed_paper_keys=list(outputs_by_paper),
        allowed_relation_ids=[],
        generation_deps={"candidate": "fixture-candidate-hash"},
        alias_map=alias_map,
    )


def test_v3_02_same_local_section_id_in_independent_shards_stays_separate(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    executor = _executor(tmp_path, stability_mode="off")
    outputs = {
        "A": {
            "candidate_id": "candidate_1",
            "sections": [{
                "section_id": "S1",
                "title": "Psychological mechanism",
                "goal": "Explain the mechanism.",
                "paper_keys": ["A"],
                "relation_ids": [],
                "claims": ["Paper A reports a mechanism."],
            }],
        },
        "B": {
            "candidate_id": "candidate_1",
            "sections": [{
                "section_id": "S1",
                "title": "Measurement methods",
                "goal": "Compare the measures.",
                "paper_keys": ["B"],
                "relation_ids": [],
                "claims": ["Paper B uses a longitudinal design."],
            }],
        },
    }

    candidate = _install_candidate_shards(executor, monkeypatch, outputs_by_paper=outputs)

    assert len(candidate["sections"]) == 2
    assert {section["title"] for section in candidate["sections"]} == {
        "Psychological mechanism",
        "Measurement methods",
    }
    assert len({section["section_id"] for section in candidate["sections"]}) == 2


def _study_support_fixture(paper_key: str, study_number: str, claim: str) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    field_id = f"field:{paper_key}:study:{study_number}:boundary"
    owner_study = f"{paper_key}:study:{study_number}"
    primary_claim_id = f"claim:{paper_key}:study:{study_number}:effect"
    evidence_id = f"evidence:{paper_key}:study:{study_number}:effect"
    field = {
        "source_field_id": field_id,
        "source_value": f"The effect is scoped to Study {study_number}.",
        "paper_key": paper_key,
        "owner_study_id": owner_study,
        "study_id": study_number,
        "scope": "explicit_study",
    }
    dependency = {
        "dependency_id": f"interpretation-dependency:{paper_key}:{study_number}",
        "primary_claim_id": primary_claim_id,
        "primary_evidence_ids": [evidence_id],
        "required_source_claim_ids": [],
        "required_evidence_ids": [],
        "required_source_field_ids": [field_id],
        "scope": "explicit_study",
        "paper_key": paper_key,
        "study_id": owner_study,
        "owner_study_id": owner_study,
    }
    support = {
        "claim": claim,
        "paper_key": paper_key,
        "study_id": owner_study,
        "source_claim_ids": [primary_claim_id],
        "evidence_ids": [evidence_id],
        "source_field_ids": [field_id],
    }
    return field, dependency, support


def test_v3_03_shared_section_merge_keeps_claim_support_for_each_merged_claim(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    executor = _executor(tmp_path, stability_mode="off")
    claim_a = "P001 Study 1 reports the conditional effect."
    claim_b = "P002 Study 2 uses a longitudinal design."
    field_a, dependency_a, support_a = _study_support_fixture("A", "1", claim_a)
    field_b, dependency_b, support_b = _study_support_fixture("B", "2", claim_b)
    outputs = {
        "A": {"candidate_id": "candidate_1", "sections": [{
            "section_id": "shared:studies",
            "title": "Study-level evidence",
            "goal": "Compare the studies.",
            "paper_keys": ["A"],
            "relation_ids": [],
            "claims": [claim_a],
            "claim_support": [support_a],
        }]},
        "B": {"candidate_id": "candidate_1", "sections": [{
            "section_id": "shared:studies",
            "title": "Study-level evidence",
            "goal": "Compare the studies.",
            "paper_keys": ["B"],
            "relation_ids": [],
            "claims": [claim_b],
            "claim_support": [support_b],
        }]},
    }
    tables = {
        "source_fields": [field_a, field_b],
        "dependencies": [dependency_a, dependency_b],
    }
    alias_map = {"papers_reverse": {"P001": "A", "P002": "B"}}

    candidate = _install_candidate_shards(
        executor,
        monkeypatch,
        outputs_by_paper=outputs,
        shared_section_ids={
            "shared:studies": {
                "title": "Study-level evidence",
                "goal": "Compare the studies.",
            },
        },
        interpretation_tables=tables,
        alias_map=alias_map,
    )

    assert len(candidate["sections"]) == 1
    section = candidate["sections"][0]
    assert {row["claim"] for row in section["claim_support"]} == {claim_a, claim_b}


@pytest.mark.parametrize("conflicting_field", ["title", "goal"])
def test_v3_03_predeclared_shared_section_rejects_blueprint_conflict(
    tmp_path, monkeypatch: pytest.MonkeyPatch, conflicting_field: str,
) -> None:
    executor = _executor(tmp_path, stability_mode="off")
    blueprint = {"title": "Shared title", "goal": "Compare evidence."}
    first = {
        "candidate_id": "candidate_1",
        "sections": [{
            "section_id": "shared:results",
            **blueprint,
            "paper_keys": ["A"],
            "relation_ids": [],
            "claims": ["Paper A reports a bounded result."],
        }],
    }
    second_section = {
        "section_id": "shared:results",
        **blueprint,
        "paper_keys": ["B"],
        "relation_ids": [],
        "claims": ["Paper B reports a bounded result."],
    }
    second_section[conflicting_field] = f"Different {conflicting_field}."
    second = {"candidate_id": "candidate_1", "sections": [second_section]}

    with pytest.raises(OutlineV3ExecutionError, match=f"conflicting {conflicting_field}"):
        _install_candidate_shards(
            executor,
            monkeypatch,
            outputs_by_paper={"A": first, "B": second},
            shared_section_ids={"shared:results": blueprint},
        )


def test_v3_03_same_claim_id_with_conflicting_support_provenance_is_rejected(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    executor = _executor(tmp_path, stability_mode="off")
    claim = "The specified mechanism appears in both studies."
    blueprint = {"title": "Shared mechanisms", "goal": "Compare the mechanism."}
    outputs = {}
    for paper_key, source_claim_id, evidence_id in (
        ("A", "source:A:claim", "evidence:A"),
        ("B", "source:B:claim", "evidence:B"),
    ):
        outputs[paper_key] = {
            "candidate_id": "candidate_1",
            "sections": [{
                "section_id": "shared:mechanism",
                **blueprint,
                "paper_keys": [paper_key],
                "relation_ids": [],
                "claims": [claim],
                "claim_support": [{
                    "claim_id": "claim:shared-mechanism",
                    "claim": claim,
                    "paper_key": paper_key,
                    "study_id": f"{paper_key}:study:1",
                    "source_claim_ids": [source_claim_id],
                    "evidence_ids": [evidence_id],
                }],
            }],
        }

    with pytest.raises(OutlineV3ExecutionError, match="conflicting claim_id claim:shared-mechanism"):
        _install_candidate_shards(
            executor,
            monkeypatch,
            outputs_by_paper=outputs,
            shared_section_ids={"shared:mechanism": blueprint},
        )


def test_v3_03_same_section_claim_id_with_conflicting_support_fails_closed(
    tmp_path,
) -> None:
    executor = _executor(tmp_path, stability_mode="off")
    claim = "The reported pattern is directly compared."
    supports = [
        {
            "claim_id": "claim:shared",
            "claim": claim,
            "paper_key": "A",
            "study_id": "A:study:1",
            "source_claim_ids": ["source:A:claim"],
            "evidence_ids": ["evidence:A"],
        },
        {
            "claim_id": "claim:shared",
            "claim": claim,
            "paper_key": "B",
            "study_id": "B:study:1",
            "source_claim_ids": ["source:B:claim"],
            "evidence_ids": ["evidence:B"],
        },
    ]

    with pytest.raises(OutlineV3ExecutionError, match="conflicting claim_id claim:shared support provenance"):
        executor._validate_candidate_payload(
            "candidate:test",
            {
                "sections": [{
                    "section_id": "section:comparison",
                    "paper_keys": ["A", "B"],
                    "relation_ids": [],
                    "claims": [claim],
                    "claim_support": supports,
                }],
            },
            allowed_paper_keys=["A", "B"],
            allowed_relation_ids=[],
        )


def test_v3_04_study_number_is_scoped_to_claim_paper_before_support_validation(
    tmp_path,
) -> None:
    executor = _executor(tmp_path, stability_mode="off")
    claim = "P001 Study 1 reports the conditional effect."
    field_a, dependency_a, support_a = _study_support_fixture("A", "1", claim)
    field_b, dependency_b, _support_b = _study_support_fixture(
        "B", "1", "P002 Study 1 uses a longitudinal design."
    )
    executor._candidate_interpretation_tables = {
        "source_fields": [field_a, field_b],
        "dependencies": [dependency_a, dependency_b],
    }

    executor._validate_candidate_payload(
        "candidate:test",
        {
            "sections": [{
                "section_id": "shared:studies",
                "paper_keys": ["A", "B"],
                "relation_ids": [],
                "claims": [claim],
                "claim_support": [support_a],
            }],
        },
        allowed_paper_keys=["A", "B"],
        allowed_relation_ids=[],
        alias_map={"papers_reverse": {"P001": "A", "P002": "B"}},
    )


@pytest.mark.parametrize("mismatch", ["paper", "study"])
def test_v3_04_wrong_paper_or_study_support_still_fails_closed(tmp_path, mismatch: str) -> None:
    executor = _executor(tmp_path / mismatch, stability_mode="off")
    claim = "P001 Study 1 reports the conditional effect."
    field_a, dependency_a, support_a = _study_support_fixture("A", "1", claim)
    field_b, dependency_b, _support_b = _study_support_fixture("B", "1", "P002 Study 1 reports another effect.")
    bad_support = dict(support_a)
    if mismatch == "paper":
        bad_support.update(paper_key="B", study_id=dependency_b["owner_study_id"])
    else:
        bad_support["study_id"] = "A:study:2"
    executor._candidate_interpretation_tables = {
        "source_fields": [field_a, field_b],
        "dependencies": [dependency_a, dependency_b],
    }

    with pytest.raises(OutlineV3ExecutionError, match="without complete scoped interpretation support"):
        executor._validate_candidate_payload(
            "candidate:test",
            {"sections": [{
                "section_id": "shared:studies",
                "paper_keys": ["A", "B"],
                "relation_ids": [],
                "claims": [claim],
                "claim_support": [bad_support],
            }]},
            allowed_paper_keys=["A", "B"],
            allowed_relation_ids=[],
            alias_map={"papers_reverse": {"P001": "A", "P002": "B"}},
        )


def test_v3_04_missing_required_condition_support_still_fails_closed(tmp_path) -> None:
    executor = _executor(tmp_path, stability_mode="off")
    claim = "P001 Study 1 reports the conditional effect."
    field, dependency, support = _study_support_fixture("A", "1", claim)
    condition_field_id = "field:A:study:1:condition"
    dependency["required_source_claim_ids"] = ["claim:A:study:1:condition"]
    dependency["required_evidence_ids"] = ["evidence:A:study:1:condition"]
    dependency["required_source_field_ids"].append(condition_field_id)
    condition_field = {
        "source_field_id": condition_field_id,
        "source_value": "The effect depends on a boundary condition.",
        "paper_key": "A",
        "owner_study_id": "A:study:1",
        "study_id": "1",
        "scope": "explicit_study",
    }
    executor._candidate_interpretation_tables = {
        "source_fields": [field, condition_field],
        "dependencies": [dependency],
    }

    with pytest.raises(OutlineV3ExecutionError, match="without complete scoped interpretation support"):
        executor._validate_candidate_payload(
            "candidate:test",
            {"sections": [{
                "section_id": "study:1",
                "paper_keys": ["A"],
                "relation_ids": [],
                "claims": [claim],
                "claim_support": [support],
            }]},
            allowed_paper_keys=["A"],
            allowed_relation_ids=[],
            alias_map={"papers_reverse": {"P001": "A"}},
        )


def test_v3_05_missing_relation_selection_metadata_keeps_legacy_all_selection(
    tmp_path,
) -> None:
    executor = _executor(tmp_path, stability_mode="off")
    candidates = [
        {"relation_id": "R1", "paper_keys": ["A"]},
        {"relation_id": "R2", "paper_keys": ["B"]},
    ]
    bundles = [
        SimpleNamespace(
            relation_id=relation_id,
            to_dict=lambda relation_id=relation_id: {
                "relation_id": relation_id,
                "paper_keys": ["A" if relation_id == "R1" else "B"],
                "findings": [f"finding:{relation_id}"],
            },
        )
        for relation_id in ("R1", "R2")
    ]

    _request, selected, excluded = executor._relation_provider_request(
        relation_candidates=candidates,
        content_layers=SimpleNamespace(content_hash="layers-hash", dossiers=[]),
        semantic_plan=SimpleNamespace(
            coverage={},
            relation_bundles=bundles,
            content_hash="semantic-plan-hash",
        ),
        shard_plan={"schema_version": "fixture", "target_tokens": 0, "shard_count": 0},
    )

    assert [item["relation_id"] for item in selected] == ["R1", "R2"]
    assert excluded == []


def test_v3_05_explicit_empty_relation_selection_selects_no_candidates(
    tmp_path,
) -> None:
    executor = _executor(tmp_path, stability_mode="off")
    candidates = [
        {"relation_id": "R1", "paper_keys": ["A"]},
        {"relation_id": "R2", "paper_keys": ["B"]},
    ]
    content_layers = SimpleNamespace(content_hash="layers-hash", dossiers=[])
    semantic_plan = SimpleNamespace(
        coverage={"selected_relation_ids": []},
        relation_bundles=[],
        content_hash="semantic-plan-hash",
    )

    _, selected, excluded = executor._relation_provider_request(
        relation_candidates=candidates,
        content_layers=content_layers,
        semantic_plan=semantic_plan,
        shard_plan={"schema_version": "fixture", "target_tokens": 0, "shard_count": 0},
    )

    assert selected == []
    assert excluded == ["R1", "R2"]


def test_v3_05_runtime_empty_selection_defers_candidates_without_relation_post_or_reservation(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider_calls: list[str] = []

    def provider(node_id: str, request: Mapping[str, Any]) -> Mapping[str, Any]:
        provider_calls.append(node_id)
        return _configured_test_provider(node_id, request)

    executor = _executor(tmp_path, provider=provider, stability_mode="off")
    original_relation_request = executor._relation_provider_request
    candidate_ids_seen: list[str] = []

    def explicitly_empty_selection(**kwargs: Any) -> tuple[dict[str, Any], list[dict[str, Any]], list[str]]:
        request, _selected, _excluded = original_relation_request(**kwargs)
        all_ids = sorted(
            str(item.get("relation_id") or "")
            for item in kwargs["relation_candidates"]
            if str(item.get("relation_id") or "")
        )
        candidate_ids_seen.extend(all_ids)
        request["relation_candidates"] = []
        request["relation_evidence_bundles"] = []
        request["relation_adjudication_contract"] = {
            **dict(request.get("relation_adjudication_contract") or {}),
            "allowed_relation_ids": [],
        }
        request["excluded_relation_count"] = len(all_ids)
        request["excluded_relation_ids_hash"] = hash_json(all_ids)
        return request, [], all_ids

    monkeypatch.setattr(executor, "_relation_provider_request", explicitly_empty_selection)
    result = executor.run()

    assert result.ok is True, result.diagnostics
    assert candidate_ids_seen
    assert not any("relation_adjudication" in node_id for node_id in provider_calls)

    relation_map = json.loads(
        Path(result.artifacts["global_relation_map"]).read_text(encoding="utf-8")
    )["payload"]
    assert relation_map["deferred_relation_ids"] == sorted(set(candidate_ids_seen))
    assert relation_map["confirmed_relation_ids"] == []
    assert relation_map["rejected_relation_ids"] == []

    receipts = ProviderRuntimeLedger(result.artifacts["provider_receipts"]).list_receipts()
    assert not any("relation_adjudication" in item.node_id for item in receipts)
    relation_plan_rows = [
        item for item in executor.provider_call_plans
        if item.node_id == "relation_adjudication" and item.transport_expected
    ]
    assert relation_plan_rows == []


def test_v3_05_disabled_relation_role_keeps_explicit_empty_selection_local(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_builder = executor_module.build_semantic_chunk_plan

    def empty_selection(*args: Any, **kwargs: Any) -> Any:
        plan = original_builder(*args, **kwargs)
        return replace(plan, coverage={**plan.coverage, "selected_relation_ids": []})

    monkeypatch.setattr(executor_module, "build_semantic_chunk_plan", empty_selection)
    provider_calls: list[str] = []

    def provider(node_id: str, request: Mapping[str, Any]) -> Mapping[str, Any]:
        provider_calls.append(node_id)
        return _configured_test_provider(node_id, request)

    executor = _executor(
        tmp_path,
        provider=provider,
        stability_mode="off",
        enabled_semantic_roles=(
            "candidate_provider_generation", "structure_critique",
            "coverage_critique", "evidence_critique", "arbitration",
        ),
    )
    result = executor.run()

    assert result.ok is True, result.diagnostics
    relation_map = json.loads(
        Path(result.artifacts["global_relation_map"]).read_text(encoding="utf-8")
    )["payload"]
    assert relation_map["deferred_relation_ids"]
    assert relation_map["confirmed_relation_ids"] == []
    assert relation_map["rejected_relation_ids"] == []
    assert not any("relation_adjudication" in node_id for node_id in provider_calls)


def test_v3_05_disabled_relation_role_blocks_selected_work_before_provider(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_builder = executor_module.build_semantic_chunk_plan

    def select_one_relation(*args: Any, **kwargs: Any) -> Any:
        plan = original_builder(*args, **kwargs)
        candidates = args[1]
        relation_ids = [str(item.relation_id) for item in candidates.relations]
        assert relation_ids
        return replace(plan, coverage={**plan.coverage, "selected_relation_ids": relation_ids[:1]})

    monkeypatch.setattr(executor_module, "build_semantic_chunk_plan", select_one_relation)
    provider_calls: list[str] = []

    def provider(node_id: str, request: Mapping[str, Any]) -> Mapping[str, Any]:
        provider_calls.append(node_id)
        return _configured_test_provider(node_id, request)

    executor = _executor(
        tmp_path,
        provider=provider,
        stability_mode="off",
        enabled_semantic_roles=(
            "candidate_provider_generation", "structure_critique",
            "coverage_critique", "evidence_critique", "arbitration",
        ),
    )
    with pytest.raises(OutlineV3ExecutionError, match="selected relations require"):
        executor._preflight_stability_budget()
    assert provider_calls == []
    assert executor.artifact_records.get("provider_call_plan") is not None


def _long_bilingual_topic_fixture() -> tuple[dict[str, Any], dict[str, Any]]:
    topic_id = "topic:bilingual-supported"
    fragment_id = "fragment:bilingual-supported"
    study_id = "paper-a:study:1"
    topic_request = {
        "topics": [{
            "topic_id": topic_id,
            "fragment_id": fragment_id,
            "paper_ids": ["paper-a"],
            "planned_evidence_ids": ["E_EFFECT", "E_CONDITION", "E_METHOD"],
        }],
        "evidence_units": [{
            "paper_key": "paper-a",
            "unit_scope": "study",
            "study_units": [{
                "study_id": study_id,
                "source_study_id": "1",
                "claims": [
                    {"claim_id": "C_EFFECT", "study_id": study_id, "evidence_ids": ["E_EFFECT"]},
                    {"claim_id": "C_CONDITION", "study_id": study_id, "evidence_ids": ["E_CONDITION"]},
                    {"claim_id": "C_METHOD", "study_id": study_id, "evidence_ids": ["E_METHOD"]},
                ],
                "evidence_ids": ["E_EFFECT", "E_CONDITION", "E_METHOD"],
                "interpretation_dependencies": [{
                    "primary_claim_id": "C_EFFECT",
                    "required_source_claim_ids": ["C_CONDITION"],
                    "required_evidence_ids": ["E_CONDITION"],
                    "required_source_field_ids": [],
                    "scope": "explicit_study",
                    "study_id": study_id,
                }],
            }],
            "claims": [],
            "evidence_ids_by_field": {"findings": ["E_EFFECT", "E_CONDITION", "E_METHOD"]},
            "evidence_text_by_id": {
                "E_EFFECT": "The treatment improved the measured outcome in the reported condition.",
                "E_CONDITION": "The effect occurred only with the specified market-support condition.",
                "E_METHOD": "The study used a randomized comparison with repeated outcome measures.",
            },
        }],
    }
    english_effect = (
        "In the randomized comparison, the treatment improved the measured outcome, "
        "but the result is bounded by the market-support condition reported for Study 1. "
    )
    chinese_effect = (
        "在随机比较中，处理提升了测量结果，但这一结论仅适用于研究一明确报告的市场支持条件。"
    )
    english_method = (
        "The repeated-measure design strengthens the within-study contrast while leaving "
        "the wider population generalization unresolved. "
    )
    chinese_method = "重复测量设计增强了研究内比较，但对更广泛人群的外推仍未确定。"
    effect_text = (english_effect * 22) + (chinese_effect * 16)
    method_text = (english_method * 16) + (chinese_method * 12)
    topic_result = {
        "topics": [{
            "topic_id": topic_id,
            "fragment_id": fragment_id,
            "status": "processed",
            "conclusions": [],
            "unresolved_questions": [],
            "supporting_evidence_ids": ["E_EFFECT", "E_CONDITION", "E_METHOD"],
        }],
        "processed_fragment_ids": [fragment_id],
        "claims": [
            {
                "claim_id": "synthesis:topic_synthesis:bounded-effect",
                "claim_type": "empirical_finding",
                "topic_id": topic_id,
                "fragment_id": fragment_id,
                "paper_key": "paper-a",
                "study_id": study_id,
                "text": effect_text,
                "source_claim_ids": ["C_EFFECT", "C_CONDITION"],
                "evidence_ids": ["E_EFFECT", "E_CONDITION"],
            },
            {
                "claim_id": "synthesis:topic_synthesis:bounded-method",
                "claim_type": "method_interpretation",
                "topic_id": topic_id,
                "fragment_id": fragment_id,
                "paper_key": "paper-a",
                "study_id": study_id,
                "text": method_text,
                "source_claim_ids": ["C_METHOD"],
                "evidence_ids": ["E_METHOD"],
            },
        ],
        "unresolved_questions": [],
    }
    return topic_request, topic_result


def test_semantic_topic_result_with_bilingual_claims_and_conditions_fits_4096_reserve(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, content = _long_bilingual_topic_fixture()
    executor = _executor(tmp_path, stability_mode="off")
    node_id = "topic_synthesis_provider:batch:bounded"
    executor._dag = replace(
        executor._dag,
        nodes=[*executor._dag.nodes, OutlineNodeRecord(node_id=node_id)],
    )
    serialized_output_tokens = executor.profile.estimate_tokens(content)
    assert 0 < serialized_output_tokens <= 4_096
    output_reserves: list[int | None] = []
    original_provider_call = executor._provider_call

    def record_provider_call(node_id: str, payload: Mapping[str, Any], **kwargs: Any) -> Any:
        output_reserves.append(kwargs.get("output_tokens"))
        return original_provider_call(node_id, payload, **kwargs)

    monkeypatch.setattr(executor, "_provider_call", record_provider_call)
    executor.provider = lambda _node_id, _request: {
        "status": "success",
        "content": content,
        "output_tokens": serialized_output_tokens,
        "finish_reason": "stop",
    }

    result = executor._run_semantic_provider_call(
        node_id,
        request,
        {"input": "fixture-hash"},
        output_tokens=4_096,
    )

    assert output_reserves == [4_096]
    assert [claim["claim_id"] for claim in result["claims"]] == [
        "synthesis:topic_synthesis:bounded-effect",
        "synthesis:topic_synthesis:bounded-method",
    ]
    assert result["processed_fragment_ids"] == ["fragment:bilingual-supported"]
    assert set(result["claims"][0]["source_claim_ids"]) == {"C_EFFECT", "C_CONDITION"}


def test_semantic_topic_output_with_finish_length_fails_closed_under_4096_reserve(
    tmp_path,
) -> None:
    request, content = _long_bilingual_topic_fixture()
    executor = _executor(tmp_path, stability_mode="off")
    node_id = "topic_synthesis_provider:batch:truncated"
    executor._dag = replace(
        executor._dag,
        nodes=[*executor._dag.nodes, OutlineNodeRecord(node_id=node_id)],
    )
    serialized_output_tokens = executor.profile.estimate_tokens(content)
    assert serialized_output_tokens <= 4_096
    executor.provider = lambda _node_id, _request: {
        "status": "success",
        "content": content,
        "output_tokens": 4_096,
        "finish_reason": "length",
        "incomplete_reason": "max_output_tokens",
    }

    with pytest.raises(OutlineV3ExecutionError, match="provider output.*incomplete"):
        executor._run_semantic_provider_call(
            node_id,
            request,
            {"input": "fixture-hash"},
            output_tokens=4_096,
        )

    artifact_id = f"outline-v3:semantic-provider:{hash_text(node_id)[:24]}"
    assert executor.registry.get(artifact_id) is None


def _semantic_identity_sets(value: Any) -> dict[str, set[str]]:
    identities = {
        "topic": set(),
        "fragment": set(),
        "result": set(),
        "relation": set(),
    }
    fields = {
        "topic": ("topic_id", "topic_ids", "processed_topic_ids"),
        "fragment": ("fragment_id", "fragment_ids", "processed_fragment_ids"),
        "result": ("result_id", "batch_result_id", "result_ids", "batch_result_ids", "processed_result_ids"),
        "relation": ("relation_id", "relation_ids", "processed_relation_ids"),
    }

    def visit(item: Any) -> None:
        if isinstance(item, Mapping):
            for kind, names in fields.items():
                for name in names:
                    raw = item.get(name)
                    if isinstance(raw, (list, tuple, set)):
                        identities[kind].update(str(value) for value in raw if str(value))
                    elif raw is not None and str(raw):
                        identities[kind].add(str(raw))
            for child in item.values():
                visit(child)
        elif isinstance(item, (list, tuple)):
            for child in item:
                visit(child)

    roots = (
        value.get("topic_synthesis"),
        value.get("cross_group_comparison"),
        value.get("relation_candidates"),
    ) if isinstance(value, Mapping) else (value,)
    visit(roots)
    return identities


def _run_cross_global_contract_fixture(
    tmp_path,
    *,
    omit_topic_disposition: bool = False,
    all_topics_unresolved: bool = False,
):
    cross_requests: list[dict[str, Any]] = []
    cross_dispositions: list[list[dict[str, Any]]] = []
    cross_bridge_claims: list[list[dict[str, Any]]] = []
    global_requests: list[dict[str, Any]] = []
    global_outputs: list[dict[str, Any]] = []

    def provider(node_id: str, request: Mapping[str, Any]) -> Mapping[str, Any]:
        if node_id.startswith("topic_synthesis_provider"):
            return _configured_test_provider(node_id, request)
        if node_id.startswith("cross_group_comparison_provider"):
            identities = _semantic_identity_sets(request)
            topic_ids = sorted(identities["topic"])
            topic_rows = [
                item for item in request.get("topic_synthesis") or ()
                if isinstance(item, Mapping) and str(item.get("topic_id") or "") in topic_ids
            ]
            bridge_claims: list[dict[str, Any]] = []
            integrated_topic = ""
            if not all_topics_unresolved:
                supported_topic = next(
                    (
                        item for item in topic_rows
                        if item.get("paper_ids") and item.get("supporting_evidence_ids")
                    ),
                    None,
                )
                if isinstance(supported_topic, Mapping):
                    integrated_topic = str(supported_topic.get("topic_id") or "")
                    paper_key = str((supported_topic.get("paper_ids") or [""])[0] or "")
                    fragment_id = str(supported_topic.get("fragment_id") or "")
                    evidence_id = str((supported_topic.get("supporting_evidence_ids") or [""])[0] or "")
                    bridge_claims = [{
                        "claim_id": f"synthesis:cross_group_comparison:integrated-{hash_text(integrated_topic)[:10]}",
                        "topic_ids": [integrated_topic],
                        "fragment_id": fragment_id,
                        "paper_key": paper_key,
                        "text": "The supported topic finding holds under the stated condition.",
                        "evidence_ids": [evidence_id],
                    }]
            dispositions = []
            for topic_id in topic_ids:
                if topic_id == integrated_topic:
                    dispositions.append({
                        "topic_id": topic_id,
                        "status": "integrated",
                        "synthesis_claim_ids": [bridge_claims[0]["claim_id"]],
                    })
                else:
                    dispositions.append({
                        "topic_id": topic_id,
                        "status": "unresolved",
                        "reason": f"The supplied findings do not establish a comparable conclusion for {topic_id}.",
                    })
            if omit_topic_disposition and dispositions:
                dispositions.pop(0)
            cross_requests.append(dict(request))
            cross_dispositions.append(dispositions)
            cross_bridge_claims.append(bridge_claims)
            return {
                "status": "success",
                "content": {
                    "comparisons": [],
                    "bridge_claims": bridge_claims,
                    "topic_dispositions": dispositions,
                    "processed_topic_ids": topic_ids,
                    "processed_fragment_ids": sorted(identities["fragment"]),
                    "processed_result_ids": sorted(identities["result"]),
                    "processed_relation_ids": sorted(identities["relation"]),
                    "topic_members_by_id": {
                        str(item.get("topic_id") or ""): [
                            str(paper) for paper in item.get("paper_ids") or () if str(paper)
                        ]
                        for item in topic_rows
                        if str(item.get("topic_id") or "")
                    },
                    "fragment_members_by_id": {
                        str(item.get("fragment_id") or ""): [
                            str(paper) for paper in item.get("paper_ids") or () if str(paper)
                        ]
                        for item in topic_rows
                        if str(item.get("fragment_id") or "")
                    },
                    "unresolved_questions": [],
                },
            }
        if node_id.startswith("global_synthesis_provider"):
            identities = _semantic_identity_sets(request)
            global_requests.append(dict(request))
            cross_result = request.get("cross_group_comparison")
            bridge_claims = (
                [item for item in cross_result.get("bridge_claims") or () if isinstance(item, Mapping)]
                if isinstance(cross_result, Mapping)
                else []
            )
            synthesis_claims: list[dict[str, Any]] = []
            if bridge_claims:
                bridge = dict(bridge_claims[0])
                synthesis_claims = [{
                    "claim_id": "synthesis:global_synthesis:integrated-topic",
                    "topic_ids": list(bridge.get("topic_ids") or ()),
                    "fragment_id": str(bridge.get("fragment_id") or ""),
                    "paper_key": str(bridge.get("paper_key") or ""),
                    "text": "The integrated cross-topic finding is supported by its stated condition. 综合结论保留了该条件。",
                    "source_claim_ids": [str(bridge.get("claim_id") or "")],
                    "evidence_ids": [str(value) for value in bridge.get("evidence_ids") or () if str(value)],
                }]
            global_output = {
                "synthesis_claims": synthesis_claims,
                "organizing_principles": ["Synthesize integrated claims while retaining unresolved topics."],
                "processed_topic_ids": sorted(identities["topic"]),
                "processed_fragment_ids": sorted(identities["fragment"]),
                "processed_result_ids": sorted(identities["result"]),
                "unresolved_questions": [],
            }
            global_outputs.append(global_output)
            return {
                "status": "success",
                "content": global_output,
            }
        return _configured_test_provider(node_id, request)

    executor = _executor(tmp_path, provider=provider, stability_mode="off")
    executor.semantic_provider_synthesis_enabled = True
    try:
        result = executor.run()
    except OutlineV3ExecutionError:
        result = None
    return executor, result, cross_requests, cross_dispositions, cross_bridge_claims, global_requests, global_outputs


def test_cross_to_global_uses_validated_cross_result_without_topic_body_replay(tmp_path) -> None:
    executor, result, cross_requests, cross_dispositions, cross_bridge_claims, global_requests, global_outputs = _run_cross_global_contract_fixture(
        tmp_path,
        omit_topic_disposition=False,
    )

    assert result is not None and result.ok is True, getattr(result, "diagnostics", ())
    assert cross_requests and global_requests
    assert cross_requests[0]["shared_synthesis_contract_version"] == "v1"
    assert cross_dispositions
    global_request = global_requests[0]
    assert global_request["topic_synthesis"] == []
    cross_result = global_request["cross_group_comparison"]
    dispositions = cross_result["topic_dispositions"]
    assert dispositions
    assert {row["status"] for row in dispositions} == {"integrated", "unresolved"}
    assert any(str(row.get("reason") or "").strip() for row in dispositions if row["status"] == "unresolved")
    bridge_claim_by_id = {
        str(claim.get("claim_id") or ""): claim
        for claim in cross_result.get("bridge_claims") or ()
        if isinstance(claim, Mapping)
    }
    integrated = next(row for row in dispositions if row["status"] == "integrated")
    bridge_claim = bridge_claim_by_id[integrated["synthesis_claim_ids"][0]]
    assert bridge_claim["topic_ids"] == [integrated["topic_id"]]
    assert bridge_claim["evidence_ids"]
    global_result_claims = [
        claim for claim in global_outputs[0].get("synthesis_claims") or ()
        if isinstance(claim, Mapping)
    ]
    assert global_result_claims
    assert bridge_claim["claim_id"] in global_result_claims[0]["source_claim_ids"]


def test_missing_cross_topic_disposition_blocks_before_global_provider_call(tmp_path) -> None:
    _executor_instance, result, cross_requests, cross_dispositions, _cross_bridge_claims, global_requests, _global_outputs = _run_cross_global_contract_fixture(
        tmp_path,
        omit_topic_disposition=True,
    )

    assert cross_requests
    assert cross_requests[0].get("shared_synthesis_contract_version") == "v1"
    requested_topics = _semantic_identity_sets(cross_requests[0])["topic"]
    assert len(cross_dispositions[0]) < len(requested_topics)
    assert result is not None and result.status == "blocked"
    assert global_requests == []


def test_all_unresolved_cross_output_cannot_produce_successful_global_path(tmp_path) -> None:
    executor, result, cross_requests, cross_dispositions, bridge_claims, global_requests, _global_outputs = (
        _run_cross_global_contract_fixture(
            tmp_path,
            all_topics_unresolved=True,
        )
    )

    assert cross_requests
    assert cross_dispositions
    assert all(row["status"] == "unresolved" for row in cross_dispositions[0])
    assert bridge_claims == [[]]
    assert global_requests == []
    if result is not None and result.ok:
        cross_record = json.loads(
            Path(result.artifacts["cross_group_comparison"]).read_text(encoding="utf-8")
        )["payload"]
        assert cross_record.get("status") == "incomplete"


@pytest.mark.parametrize(
    ("integrated_topic", "reject_foreign_bridge"),
    [("topic:A", True), ("topic:B", False)],
)
def test_integrated_topic_disposition_must_own_its_bridge_claim(
    tmp_path, integrated_topic: str, reject_foreign_bridge: bool,
) -> None:
    executor = _executor(tmp_path, stability_mode="off")
    request = {
        "task": "substantive_cross_group_comparison",
        "node_id": "cross_group_comparison",
        "semantic_contract_version": "semantic-evidence-graph-v2",
        "shared_synthesis_contract_version": "v1",
        "topic_synthesis": [
            {"topic_id": "topic:A", "fragment_id": "fragment:A", "paper_ids": ["paper-a"]},
            {"topic_id": "topic:B", "fragment_id": "fragment:B", "paper_ids": ["paper-b"]},
        ],
        "evidence_units": [
            {
                "paper_key": "paper-a",
                "study_units": [{
                    "study_id": "paper-a:study:1",
                    "source_study_id": "1",
                    "claims": [{
                        "claim_id": "C_A",
                        "study_id": "paper-a:study:1",
                        "evidence_ids": ["E_A"],
                    }],
                }],
                "evidence_ids_by_field": {"findings": ["E_A"]},
                "evidence_text_by_id": {"E_A": "Paper A source finding."},
            },
            {
                "paper_key": "paper-b",
                "study_units": [{
                    "study_id": "paper-b:study:1",
                    "source_study_id": "1",
                    "claims": [{
                        "claim_id": "C_B",
                        "study_id": "paper-b:study:1",
                        "evidence_ids": ["E_B"],
                    }],
                }],
                "evidence_ids_by_field": {"findings": ["E_B"]},
                "evidence_text_by_id": {"E_B": "Paper B source finding."},
            },
        ],
    }
    other_topic = "topic:B" if integrated_topic == "topic:A" else "topic:A"
    result = {
        "comparisons": [],
        "bridge_claims": [{
            "claim_id": "synthesis:cross_group_comparison:bridge-b",
            "topic_ids": ["topic:B"],
            "fragment_id": "fragment:B",
            "paper_key": "paper-b",
            "study_id": "paper-b:study:1",
            "text": "Paper B supports this topic-specific synthesis.",
            "source_claim_ids": ["C_B"],
            "evidence_ids": ["E_B"],
        }],
        "topic_dispositions": [
            {
                "topic_id": integrated_topic,
                "status": "integrated",
                "synthesis_claim_ids": ["synthesis:cross_group_comparison:bridge-b"],
            },
            {
                "topic_id": other_topic,
                "status": "unresolved",
                "reason": "No validated cross-topic claim is available.",
            },
        ],
        "processed_topic_ids": ["topic:A", "topic:B"],
        "processed_fragment_ids": ["fragment:A", "fragment:B"],
        "processed_result_ids": [],
        "processed_relation_ids": [],
        "unresolved_questions": [],
    }

    if reject_foreign_bridge:
        with pytest.raises(
            OutlineV3ExecutionError,
            match="integrated topic references a synthesis claim for another topic",
        ):
            executor._validate_semantic_provider_output(
                "cross_group_comparison_provider",
                request,
                result,
            )
    else:
        executor._validate_semantic_provider_output(
            "cross_group_comparison_provider",
            request,
            result,
        )


@pytest.mark.parametrize("has_global_synthesis", [False, True])
def test_global_synthesis_requires_a_supported_substantive_result(
    tmp_path, has_global_synthesis: bool,
) -> None:
    executor = _executor(tmp_path, stability_mode="off")
    bridge_claim_id = "synthesis:cross_group_comparison:bridge-A"
    global_request = {
        "task": "substantive_global_synthesis",
        "node_id": "global_synthesis",
        "semantic_contract_version": "semantic-evidence-graph-v2",
        "shared_synthesis_contract_version": "v1",
        "topic_synthesis": [],
        "cross_group_comparison": {
            "shared_synthesis_contract_version": "v1",
            "bridge_claims": [{
                "claim_id": bridge_claim_id,
                "topic_ids": ["topic:A"],
                "fragment_id": "fragment:A",
                "paper_key": "paper-a",
                "text": "Paper A supports this topic-specific finding.",
                "evidence_ids": ["E_A"],
            }],
            "topic_dispositions": [{
                "topic_id": "topic:A",
                "status": "integrated",
                "synthesis_claim_ids": [bridge_claim_id],
            }],
            "processed_topic_ids": ["topic:A"],
            "processed_fragment_ids": ["fragment:A"],
            "processed_result_ids": [],
            "processed_relation_ids": [],
            "topic_members_by_id": {"topic:A": ["paper-a"]},
            "fragment_members_by_id": {"fragment:A": ["paper-a"]},
            "unresolved_questions": [],
        },
        "relation_candidates": [],
    }
    local_topic_rows = [{
        "topic_id": "topic:A",
        "fragment_id": "fragment:A",
        "paper_ids": ["paper-a"],
        "result_ids": [],
        "supporting_evidence_ids": ["E_A"],
    }]
    local_ledger = executor._build_semantic_coverage_ledger(
        local_topic_rows,
        [],
        global_request["cross_group_comparison"],
    )
    executor._payloads["cross_group_comparison"] = {"coverage_ledger": local_ledger}
    global_request["cross_coverage_ledger_ref"] = {
        "content_hash": local_ledger["content_hash"],
        "topic_count": 1,
    }
    synthesis_claims = ([{
        "claim_id": "synthesis:global_synthesis:integrated-A",
        "topic_ids": ["topic:A"],
        "fragment_id": "fragment:A",
        "paper_key": "paper-a",
        "text": "The integrated conclusion retains the supported boundary.",
        "source_claim_ids": [bridge_claim_id],
        "evidence_ids": ["E_A"],
    }] if has_global_synthesis else [])
    organizing_principles = (["Group claims by mechanism while retaining conditions."] if has_global_synthesis else [])
    result = {
        "synthesis_claims": synthesis_claims,
        "organizing_principles": organizing_principles,
        "processed_topic_ids": ["topic:A"],
        "processed_fragment_ids": ["fragment:A"],
        "processed_result_ids": [],
        "unresolved_questions": [],
    }

    if has_global_synthesis:
        executor._validate_semantic_provider_output(
            "global_synthesis_provider",
            global_request,
            result,
        )
    else:
        with pytest.raises(OutlineV3ExecutionError):
            executor._validate_semantic_provider_output(
                "global_synthesis_provider",
                global_request,
                result,
            )


def test_64_topic_dispositions_and_supported_claims_fit_4096_without_echoed_id_arrays(
    tmp_path,
) -> None:
    executor = _executor(tmp_path, stability_mode="off")
    topics = []
    for index in range(64):
        suffix = f"{index:02d}"
        paper_key = f"paper:{suffix}"
        evidence_id = f"E:{suffix}"
        topics.append({
            "topic_id": f"topic:{suffix}",
            "fragment_id": f"fragment:{suffix}",
            "paper_ids": [paper_key],
            "result_ids": [f"result:{suffix}"],
            "supporting_evidence_ids": [evidence_id],
            "claims": [{
                "claim_id": f"source:{suffix}",
                "paper_key": paper_key,
                "evidence_ids": [evidence_id],
            }],
        })
    request = {
        "task": "substantive_cross_group_comparison",
        "node_id": "cross_group_comparison",
        "semantic_contract_version": "semantic-evidence-graph-v2",
        "shared_synthesis_contract_version": "v1",
        "topic_synthesis": topics,
        "relation_candidates": [],
    }
    bridge_claims = [
        {
            "claim_id": f"synthesis:cross_group_comparison:bridge-{index:02d}",
            "topic_ids": [topics[index]["topic_id"]],
            "fragment_id": topics[index]["fragment_id"],
            "paper_key": topics[index]["paper_ids"][0],
            "text": f"The supported bilingual topic conclusion for study {index + 1}.",
            "source_claim_ids": [topics[index]["claims"][0]["claim_id"]],
            "evidence_ids": [topics[index]["supporting_evidence_ids"][0]],
        }
        for index in range(4)
    ]
    integrated_by_topic = {
        claim["topic_ids"][0]: claim["claim_id"] for claim in bridge_claims
    }
    response = {
        "comparisons": [],
        "bridge_claims": bridge_claims,
        "topic_dispositions": [
            ({
                "topic_id": topic["topic_id"],
                "status": "integrated",
                "synthesis_claim_ids": [integrated_by_topic[topic["topic_id"]]],
            } if topic["topic_id"] in integrated_by_topic else {
                "topic_id": topic["topic_id"],
                "status": "unresolved",
                "reason": "Unresolved under supplied evidence.",
            })
            for topic in topics
        ],
        "unresolved_questions": [],
    }
    profile = ProviderContextProfile.conservative(
        provider="fixture",
        model="disposition-size-check",
        endpoint_type="internal",
        model_context_limit=128_000,
        max_output_tokens=4_096,
    )
    serialized_output_tokens = profile.estimate_tokens(response)
    assert 0 < serialized_output_tokens <= 4_096
    assert not {
        "processed_topic_ids",
        "processed_fragment_ids",
        "processed_result_ids",
        "processed_relation_ids",
    }.intersection(response)

    local_coverage_ledger = executor._build_semantic_coverage_ledger(topics, [], response)
    assert local_coverage_ledger["provider_review_status"] == "validated"
    assert local_coverage_ledger["topic_ids"] == sorted(str(topic["topic_id"]) for topic in topics)
    assert len(local_coverage_ledger["fragment_ids"]) == 64
    assert len(local_coverage_ledger["result_ids"]) == 64
    assert local_coverage_ledger["relation_ids"] == []
    assert len(local_coverage_ledger["topic_status_by_id"]) == 64
    assert len([status for status in local_coverage_ledger["topic_status_by_id"].values() if status == "integrated"]) == 4

    executor._validate_semantic_provider_output(
        "cross_group_comparison_provider",
        request,
        response,
    )


def _coordination_sections() -> list[dict[str, Any]]:
    claim_a = "Paper A identifies the mechanism under its tested boundary."
    claim_b = "Paper B identifies the same mechanism in a different context."
    claim_c = "Paper C describes the measurement procedure."
    return [
        {
            "section_id": "candidate_1_section_1__shard_1",
            "title": "Mechanism and boundary",
            "goal": "Explain the mechanism while retaining its boundary.",
            "rationale": "Paper A locates the mechanism within its tested boundary.",
            "paper_keys": ["paper-a"],
            "relation_ids": ["R-A"],
            "claims": [claim_a],
            "claim_support": [{
                "claim_id": "claim:A:mechanism",
                "claim": claim_a,
                "paper_key": "paper-a",
                "source_claim_ids": ["C-A"],
                "evidence_ids": ["E-A"],
            }],
            "paper_roles": {"paper-a": "mechanism evidence"},
        },
        {
            "section_id": "candidate_1_section_1__shard_2",
            "title": "Mechanism and boundary",
            "goal": "Explain the mechanism while retaining its boundary.",
            "rationale": "Paper B extends the mechanism to a distinct context.",
            "paper_keys": ["paper-b"],
            "relation_ids": ["R-B"],
            "claims": [claim_b],
            "claim_support": [{
                "claim_id": "claim:B:mechanism",
                "claim": claim_b,
                "paper_key": "paper-b",
                "source_claim_ids": ["C-B"],
                "evidence_ids": ["E-B"],
            }],
            "paper_roles": {"paper-b": "boundary evidence"},
        },
        {
            "section_id": "candidate_1_section_2__shard_2",
            "title": "Measurement",
            "goal": "Describe how the focal construct was measured.",
            "rationale": "Paper C supplies the measurement procedure.",
            "paper_keys": ["paper-c"],
            "relation_ids": [],
            "claims": [claim_c],
            "claim_support": [{
                "claim_id": "claim:C:measurement",
                "claim": claim_c,
                "paper_key": "paper-c",
                "source_claim_ids": ["C-C"],
                "evidence_ids": ["E-C"],
            }],
            "paper_roles": {"paper-c": "measurement source"},
        },
    ]


def _coordination_merge_group(
    sections: list[dict[str, Any]],
    source_ids: list[str],
    *,
    new_section_id: str = "section:mechanism",
) -> dict[str, Any]:
    section_by_id = {str(section["section_id"]): section for section in sections}
    source_sections = [section_by_id[item] for item in source_ids if item in section_by_id]
    return {
        "new_section_id": new_section_id,
        "source_section_ids": source_ids,
        "title": source_sections[0]["title"] if source_sections else "Mechanism and boundary",
        "goal": source_sections[0]["goal"] if source_sections else "Explain the mechanism while retaining its boundary.",
        "integration_reason": "These sections share one declared mechanism and preserve distinct study evidence.",
        "source_section_hashes": {
            section_id: executor_module._hash_payload(section_by_id[section_id])
            for section_id in source_ids
            if section_id in section_by_id
        },
    }


def test_section_coordination_merge_preserves_claims_support_and_membership(tmp_path) -> None:
    executor = _executor(tmp_path, stability_mode="off")
    sections = _coordination_sections()
    source_a, source_b, source_c = [section["section_id"] for section in sections]
    group = _coordination_merge_group(sections, [source_a, source_b])

    ordered, audit = executor._apply_section_coordination(
        "candidate_1",
        sections,
        {
            "candidate_id": "candidate_1",
            "merge_groups": [group],
            "section_order": [source_c, "section:mechanism"],
        },
        parent_hash="candidate-parent-hash",
    )

    assert [section["section_id"] for section in ordered] == [source_c, "section:mechanism"]
    merged = ordered[1]
    assert merged["title"] == "Mechanism and boundary"
    assert merged["goal"] == "Explain the mechanism while retaining its boundary."
    assert set(merged["paper_keys"]) == {"paper-a", "paper-b"}
    assert set(merged["relation_ids"]) == {"R-A", "R-B"}
    assert set(merged["claims"]) == {sections[0]["claims"][0], sections[1]["claims"][0]}
    assert {row["claim_id"] for row in merged["claim_support"]} == {
        "claim:A:mechanism",
        "claim:B:mechanism",
    }
    assert merged["paper_roles"] == {
        "paper-a": "mechanism evidence",
        "paper-b": "boundary evidence",
    }
    assert merged["coordination_source_rationales"] == [
        {"section_id": source_a, "rationale": sections[0]["rationale"]},
        {"section_id": source_b, "rationale": sections[1]["rationale"]},
    ]
    assert all(section["rationale"] in merged["rationale"] for section in sections[:2])
    assert audit["parent_hash"] == "candidate-parent-hash"


def test_section_coordination_without_merge_can_reorder_all_sections(tmp_path) -> None:
    executor = _executor(tmp_path, stability_mode="off")
    sections = _coordination_sections()
    original = {section["section_id"]: section for section in sections}
    order = [sections[2]["section_id"], sections[0]["section_id"], sections[1]["section_id"]]

    ordered, _audit = executor._apply_section_coordination(
        "candidate_1",
        sections,
        {"candidate_id": "candidate_1", "merge_groups": [], "section_order": order},
        parent_hash="candidate-parent-hash",
    )

    assert [section["section_id"] for section in ordered] == order
    assert ordered == [original[section_id] for section_id in order]


@pytest.mark.parametrize("invalid_case", ["missing_source", "duplicate_source", "hash_mismatch", "omitted_final"])
def test_section_coordination_rejects_incomplete_or_invalid_source_accounting(
    tmp_path, invalid_case: str,
) -> None:
    executor = _executor(tmp_path / invalid_case, stability_mode="off")
    sections = _coordination_sections()
    source_a, source_b, source_c = [section["section_id"] for section in sections]
    group = _coordination_merge_group(sections, [source_a, source_b])
    order = ["section:mechanism", source_c]
    if invalid_case == "missing_source":
        group["source_section_ids"] = [source_a, "missing:source"]
        group["source_section_hashes"]["missing:source"] = "missing-hash"
    elif invalid_case == "duplicate_source":
        group["source_section_ids"] = [source_a, source_a]
    elif invalid_case == "hash_mismatch":
        group["source_section_hashes"][source_a] = "wrong-source-hash"
    else:
        order = ["section:mechanism"]

    with pytest.raises(OutlineV3ExecutionError):
        executor._apply_section_coordination(
            "candidate_1",
            sections,
            {"candidate_id": "candidate_1", "merge_groups": [group], "section_order": order},
            parent_hash="candidate-parent-hash",
        )


def test_section_coordination_rejects_title_or_goal_conflict(tmp_path) -> None:
    executor = _executor(tmp_path, stability_mode="off")
    sections = _coordination_sections()
    sections[1]["title"] = "A different section"
    source_a, source_b, _source_c = [section["section_id"] for section in sections]
    group = _coordination_merge_group(sections, [source_a, source_b])

    with pytest.raises(OutlineV3ExecutionError):
        executor._apply_section_coordination(
            "candidate_1",
            sections,
            {"candidate_id": "candidate_1", "merge_groups": [group], "section_order": ["section:mechanism"]},
            parent_hash="candidate-parent-hash",
        )


def test_section_coordination_rejects_conflicting_claim_id_provenance(tmp_path) -> None:
    executor = _executor(tmp_path, stability_mode="off")
    sections = _coordination_sections()
    first_claim = sections[0]["claims"][0]
    sections[1]["claims"] = [first_claim]
    sections[1]["claim_support"][0].update(
        claim_id="claim:A:mechanism",
        claim=first_claim,
        source_claim_ids=["C-B-conflict"],
        evidence_ids=["E-B-conflict"],
    )
    source_a, source_b, _source_c = [section["section_id"] for section in sections]
    group = _coordination_merge_group(sections, [source_a, source_b])

    with pytest.raises(OutlineV3ExecutionError):
        executor._apply_section_coordination(
            "candidate_1",
            sections,
            {"candidate_id": "candidate_1", "merge_groups": [group], "section_order": ["section:mechanism"]},
            parent_hash="candidate-parent-hash",
        )


@pytest.mark.parametrize("provide_coordination", [False, True])
def test_sharded_arbitration_requires_and_applies_coordination_without_extra_call(
    tmp_path, provide_coordination: bool,
) -> None:
    provider_nodes: list[str] = []
    arbitration_requests: list[dict[str, Any]] = []
    arbitration_selected: list[str] = []

    def provider(node_id: str, request: Mapping[str, Any]) -> Mapping[str, Any]:
        provider_nodes.append(node_id)
        if node_id == "arbitration":
            arbitration_requests.append(dict(request))
            response = dict(_configured_test_provider(node_id, request))
            content = dict(response.get("content") or {})
            selected_id = str(content.get("selected_candidate_id") or "")
            arbitration_selected.append(selected_id)
            if not provide_coordination:
                content.pop("section_coordination", None)
            else:
                candidate = (request.get("candidate_contents") or {}).get(selected_id) or {}
                content["section_coordination"] = {
                    "candidate_id": selected_id,
                    "merge_groups": [],
                    "section_order": [
                        str(section.get("section_id") or "")
                        for section in candidate.get("sections") or ()
                        if isinstance(section, Mapping)
                    ],
                }
            response["content"] = content
            return response
        return _configured_test_provider(node_id, request)

    executor = _executor(
        tmp_path,
        provider=provider,
        stability_mode="off",
        technical_shard_target_tokens=1,
    )
    result = executor.run()

    assert len(arbitration_requests) == 1
    coordination_contract = arbitration_requests[0]["section_coordination_contract"]
    assert coordination_contract["required_if_selected_candidate_sharded"]
    assert arbitration_selected[0] in coordination_contract["required_if_selected_candidate_sharded"]
    assert provider_nodes.count("arbitration") == 1
    assert not any("section_coordination" in node_id for node_id in provider_nodes)
    if not provide_coordination:
        assert result.status == "blocked"
        assert result.adopted is False
        assert "selected_candidate_revision" not in result.artifacts
        return

    assert result.ok is True, result.diagnostics
    revision = json.loads(
        Path(result.artifacts["selected_candidate_revision"]).read_text(encoding="utf-8")
    )["payload"]
    coordination_audit = revision["section_coordination"]
    assert coordination_audit["status"] == "applied"
    assert coordination_audit["merge_groups"] == []
    assert coordination_audit["section_order"] == [
        str(section["section_id"]) for section in revision["sections"]
    ]


def test_arbitration_can_select_candidate_two_and_preserves_its_revision_hash(tmp_path) -> None:
    arbitration_hashes: dict[str, str] = {}

    def provider(node_id: str, request: Mapping[str, Any]) -> Mapping[str, Any]:
        response = dict(_configured_test_provider(node_id, request))
        if node_id == "arbitration":
            arbitration_hashes.update(
                {str(key): str(value) for key, value in (request.get("candidate_hashes") or {}).items()}
            )
            content = dict(response.get("content") or {})
            assert "candidate_2" in request.get("candidate_ids", ())
            content["selected_candidate_id"] = "candidate_2"
            response["content"] = content
        return response

    result = _executor(
        tmp_path,
        provider=provider,
        stability_mode="off",
        candidate_count=2,
    ).run()

    assert result.ok is True, result.diagnostics
    assert result.status == "ready_for_adoption"
    assert result.adopted is False
    assert arbitration_hashes["candidate_2"]

    selected = json.loads(
        Path(result.artifacts["selected_candidate"]).read_text(encoding="utf-8")
    )["payload"]
    revision = json.loads(
        Path(result.artifacts["selected_candidate_revision"]).read_text(encoding="utf-8")
    )["payload"]
    assert selected["candidate_id"] == "candidate_2"
    assert selected["candidate_hash"] == arbitration_hashes["candidate_2"]
    assert revision["candidate_id"] == "candidate_2"
    assert revision["parent_candidate_hash"] == arbitration_hashes["candidate_2"]
    assert revision["status"] == "completed"
    assert revision["revised_content_hash"] == executor_module._hash_payload(
        {"sections": revision["sections"]}
    )


_CRITIQUE_ROLES = {"structure_critique", "evidence_critique", "coverage_critique"}


def _install_critique_request_estimates(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, int]]:
    original_estimate = ProviderContextProfile.estimate_request
    observed: list[tuple[str, str, int]] = []

    def estimate_request(
        profile: ProviderContextProfile,
        request: Mapping[str, Any],
    ) -> dict[str, Any]:
        node_id = str(request.get("node_id") or "")
        hierarchy = request.get("hierarchy")
        if node_id in _CRITIQUE_ROLES:
            level = str(hierarchy.get("level") or "") if isinstance(hierarchy, Mapping) else "flat"
            estimate = 20_000 if level == "critique_candidate_shard" else 37_397
            observed.append((node_id, level, estimate))
            return {"estimated_input_tokens": estimate, "within_budget": True}
        return original_estimate(profile, request)

    monkeypatch.setattr(ProviderContextProfile, "estimate_request", estimate_request)
    return observed


@pytest.mark.parametrize("target_tokens", [0, 50_000])
def test_critique_preflight_counts_all_extra_logical_and_physical_calls(
    tmp_path, monkeypatch: pytest.MonkeyPatch, target_tokens: int,
) -> None:
    executor = _executor(
        tmp_path,
        stability_mode="off",
        technical_shard_target_tokens=target_tokens,
        max_source_prompt_tokens=32_000,
    )
    observed = _install_critique_request_estimates(monkeypatch)

    executor._preflight_stability_budget()

    assert executor._effective_input_cap(executor._node_route("structure_critique").profile) == 32_000
    for role in sorted(_CRITIQUE_ROLES):
        assert (role, "flat", 37_397) in observed
    expected_extra_calls = len(_CRITIQUE_ROLES) * (executor.candidate_count - 1)
    preflight = executor.stability_preflight
    assert preflight["hierarchical_critique_shard_calls"] == expected_extra_calls
    static_calls = sum(1 for plan in executor.provider_call_plans if plan.transport_expected)
    assert preflight["estimated_provider_calls"] == static_calls + expected_extra_calls
    static_attempts = sum(
        int(plan.physical_attempt_upper_bound or 0)
        for plan in executor.provider_call_plans
        if plan.transport_expected
    )
    assert preflight["estimated_provider_physical_attempts_upper_bound"] == (
        static_attempts + expected_extra_calls
    )


@pytest.mark.parametrize("target_tokens", [0, 50_000])
def test_all_canonical_critique_roles_shard_before_flat_post_under_effective_cap(
    tmp_path, monkeypatch: pytest.MonkeyPatch, target_tokens: int,
) -> None:
    provider_nodes: list[str] = []

    def provider(node_id: str, request: Mapping[str, Any]) -> Mapping[str, Any]:
        provider_nodes.append(node_id)
        return _configured_test_provider(node_id, request)

    executor = _executor(
        tmp_path,
        provider=provider,
        stability_mode="off",
        technical_shard_target_tokens=target_tokens,
        max_source_prompt_tokens=32_000,
    )
    observed = _install_critique_request_estimates(monkeypatch)
    provider_requests: list[tuple[str, dict[str, Any]]] = []
    original_provider_call = executor._provider_call

    def record_provider_call(node_id: str, request: Mapping[str, Any], **kwargs: Any) -> Any:
        provider_requests.append((node_id, dict(request)))
        return original_provider_call(node_id, request, **kwargs)

    monkeypatch.setattr(executor, "_provider_call", record_provider_call)
    result = executor.run()

    flat_attempts = [
        node_id for node_id, request in provider_requests
        if node_id in _CRITIQUE_ROLES
        and (request.get("hierarchy") or {}).get("level") != "critique_candidate_shard"
    ]
    assert flat_attempts == [], (
        f"target={target_tokens}; flat critique attempted at 37,397/32,000: {flat_attempts}; "
        f"status={result.status}; estimates={observed}"
    )
    for role in sorted(_CRITIQUE_ROLES):
        role_shards = [
            (node_id, request)
            for node_id, request in provider_requests
            if node_id.startswith(f"{role}:local:")
            and (request.get("hierarchy") or {}).get("level") == "critique_candidate_shard"
        ]
        assert len(role_shards) == executor.candidate_count
        assert all(
            (role, "critique_candidate_shard", 20_000) in observed
            for _node_id, _request in role_shards
        )
    assert result.ok is True, result.diagnostics

    preflight = executor.stability_preflight
    expected_extra_calls = len(_CRITIQUE_ROLES) * (executor.candidate_count - 1)
    assert preflight["hierarchical_critique_shard_calls"] == expected_extra_calls
    static_calls = sum(1 for plan in executor.provider_call_plans if plan.transport_expected)
    assert preflight["estimated_provider_calls"] == static_calls + expected_extra_calls
    static_attempts = sum(
        int(plan.physical_attempt_upper_bound or 0)
        for plan in executor.provider_call_plans
        if plan.transport_expected
    )
    assert preflight["estimated_provider_physical_attempts_upper_bound"] == (
        static_attempts + expected_extra_calls
    )
    receipts = ProviderRuntimeLedger(result.artifacts["provider_receipts"]).list_receipts()
    critique_receipts = [
        item for item in receipts
        if any(item.node_id.startswith(f"{role}:local:") for role in _CRITIQUE_ROLES)
    ]
    assert len(critique_receipts) == len(_CRITIQUE_ROLES) * executor.candidate_count
    assert all(item.attempts == 1 for item in critique_receipts)
    assert len(provider_nodes) == len(receipts)


def _shared_v1_topic_rows(count: int = 64) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for index in range(count):
        suffix = f"{index:02d}"
        topic_id = f"topic:{suffix}"
        fragment_id = f"fragment:{suffix}"
        paper_key = f"paper:{suffix}"
        evidence_id = f"evidence:{suffix}"
        source_claim_id = f"source:{suffix}"
        rows.append({
            "topic_id": topic_id,
            "fragment_id": fragment_id,
            "paper_ids": [paper_key],
            "result_ids": [f"result:{suffix}"],
            "supporting_evidence_ids": [evidence_id],
            "claims": [{
                "claim_id": source_claim_id,
                "topic_ids": [topic_id],
                "fragment_id": fragment_id,
                "paper_key": paper_key,
                "text": f"The supported finding for topic {suffix} retains its study boundary.",
                "evidence_ids": [evidence_id],
            }],
            "bounded_context": (
                f"Topic {suffix}: the bilingual source describes a measured finding and its boundary. "
                "该来源报告了有条件的研究发现，并保留适用范围。 " * 18
            ),
        })
    return rows


def _find_topic_support(value: Any, topic_id: str) -> tuple[str, list[str]] | None:
    if isinstance(value, Mapping):
        own_topics = {
            str(item) for item in (
                *([value.get("topic_id")] if value.get("topic_id") else []),
                *list(value.get("topic_ids") or ()),
            ) if str(item)
        }
        if topic_id in own_topics:
            claim_id = str(value.get("claim_id") or "")
            evidence_ids = [
                str(item) for item in (
                    *list(value.get("evidence_ids") or ()),
                    *list(value.get("supporting_evidence_ids") or ()),
                ) if str(item)
            ]
            if claim_id and evidence_ids:
                return claim_id, evidence_ids
        for child in value.values():
            found = _find_topic_support(child, topic_id)
            if found is not None:
                return found
    elif isinstance(value, (list, tuple)):
        for child in value:
            found = _find_topic_support(child, topic_id)
            if found is not None:
                return found
    return None


@pytest.mark.parametrize(
    ("input_cap", "expect_noncontraction"),
    [(4_500, True), (8_000, False)],
)
def test_shared_v1_multilevel_reducer_persists_lineage_and_keeps_receipts_raw(
    tmp_path, monkeypatch: pytest.MonkeyPatch, input_cap: int, expect_noncontraction: bool,
) -> None:
    topic_rows = _shared_v1_topic_rows()
    all_papers = [str(row["paper_ids"][0]) for row in topic_rows]
    relation_id = "relation:shared-fixture"
    request = {
        "task": "substantive_cross_group_comparison",
        "node_id": "cross_group_comparison",
        "shared_synthesis_contract_version": "v1",
        "semantic_contract_version": "semantic-evidence-graph-v2",
        "topic_synthesis": topic_rows,
        "relation_candidates": [{
            "relation_id": relation_id,
            "paper_keys": [all_papers[0]],
            "claim_ids_left": ["source:00"],
            "required_evidence_ids": ["evidence:00"],
        }],
        "questions": ["Which supported findings share a mechanism, and under what conditions?"],
        "output_contract": {},
    }
    raw_outputs: dict[str, dict[str, Any]] = {}
    provider_requests: dict[str, dict[str, Any]] = {}

    def provider(node_id: str, provider_request: Mapping[str, Any]) -> Mapping[str, Any]:
        provider_requests[node_id] = dict(provider_request)
        topic_ids = sorted(_semantic_identity_sets(provider_request)["topic"])
        bridge_claims: list[dict[str, Any]] = []
        if "topic:00" in topic_ids:
            support = _find_topic_support(provider_request.get("topic_synthesis"), "topic:00")
            if support is not None:
                source_claim_id, evidence_ids = support
                bridge_claims.append({
                    "claim_id": f"synthesis:cross_group_comparison:bridge-{hash_text(node_id)[:12]}",
                    "topic_ids": ["topic:00"],
                    "fragment_id": "fragment:00",
                    "paper_key": "paper:00",
                    "text": "The finding remains supported when its study boundary is retained.",
                    "source_claim_ids": [source_claim_id],
                    "evidence_ids": evidence_ids,
                })
        dispositions = []
        integrated_id = bridge_claims[0]["claim_id"] if bridge_claims else ""
        for topic_id in topic_ids:
            if topic_id == "topic:00" and integrated_id:
                dispositions.append({
                    "topic_id": topic_id,
                    "status": "integrated",
                    "synthesis_claim_ids": [integrated_id],
                })
            else:
                dispositions.append({
                    "topic_id": topic_id,
                    "status": "unresolved",
                    "reason": "The available child synthesis does not support a distinct disposition.",
                })
        content = {
            "comparisons": [],
            "bridge_claims": bridge_claims,
            "topic_dispositions": dispositions,
            "unresolved_questions": [],
        }
        raw_outputs[node_id] = content
        return {"status": "success", "content": content}

    executor = _executor(
        tmp_path,
        provider=provider,
        stability_mode="off",
        max_source_prompt_tokens=input_cap,
    )
    captured_calls: dict[str, tuple[dict[str, Any], tuple[str, ...]]] = {}
    original_provider_call = executor._provider_call

    def capture_provider_call(node_id: str, provider_request: Mapping[str, Any], **kwargs: Any) -> Any:
        if ":reduce:" in node_id:
            manifest_record = executor.artifact_records.get(f"{node_id}:input_manifest")
            assert manifest_record is not None, f"{node_id} reached provider before input manifest publication"
            manifest = json.loads(Path(manifest_record.path).read_text(encoding="utf-8"))["payload"]
            assert manifest["provider_request_hash"] == hash_json(dict(provider_request))
            assert manifest_record.content_hash in tuple(kwargs.get("input_artifact_hashes") or ())
            assert manifest["source_item_hashes"] == [
                hash_json(item) for item in provider_request.get("topic_synthesis") or ()
            ]
            assert set(manifest["topic_ids"]) == _semantic_identity_sets(provider_request)["topic"]
            captured_calls[node_id] = (
                dict(provider_request),
                tuple(kwargs.get("input_artifact_hashes") or ()),
            )
        return original_provider_call(node_id, provider_request, **kwargs)

    monkeypatch.setattr(executor, "_provider_call", capture_provider_call)
    if expect_noncontraction:
        with pytest.raises(OutlineV3ExecutionError, match="reducer cannot contract"):
            executor._run_bounded_semantic_provider_call(
                "cross_group_comparison_provider",
                request,
                {"topic_synthesis": "fixture-topic-input"},
            )
        reducer_ids = [node_id for node_id in captured_calls if ":reduce:" in node_id]
        assert reducer_ids
        assert len(reducer_ids) <= 24
        assert "cross_group_comparison_provider" not in provider_requests
        return

    result = executor._run_bounded_semantic_provider_call(
        "cross_group_comparison_provider",
        request,
        {"topic_synthesis": "fixture-topic-input"},
    )

    reducer_ids = [node_id for node_id in captured_calls if ":reduce:" in node_id]
    assert reducer_ids, "the low cap must force semantic reducer calls"
    level_two_ids = [node_id for node_id in reducer_ids if ":reduce:2:" in node_id]
    assert level_two_ids, "the fixture must exercise a parent reducer level"
    for node_id in level_two_ids:
        parent_items = captured_calls[node_id][0]["topic_synthesis"]
        assert parent_items
        assert all("semantic_result" in item for item in parent_items)
        assert all(not {"provider_outputs", "claims", "interpretation_context"}.intersection(item) for item in parent_items)

    manifests = {
        node_id: json.loads(
            Path(executor.artifact_records[f"{node_id}:input_manifest"].path).read_text(encoding="utf-8")
        )["payload"]
        for node_id in reducer_ids
    }
    assert set().union(*(set(item["fragment_ids"]) for item in manifests.values())) == {
        str(row["fragment_id"]) for row in topic_rows
    }
    assert set().union(*(set(item["result_ids"]) for item in manifests.values())) == {
        str(value) for row in topic_rows for value in row["result_ids"]
    }
    assert relation_id in set().union(*(set(item["relation_ids"]) for item in manifests.values()))
    assert set().union(*(set(item["paper_ids"]) for item in manifests.values())) == set(all_papers)
    assert any(item.get("child_input_manifest_hashes") for item in manifests.values())

    ledger = executor._build_semantic_coverage_ledger(topic_rows, [relation_id], result)
    assert ledger["provider_review_status"] == "validated"
    assert ledger["topic_ids"] == sorted(str(row["topic_id"]) for row in topic_rows)
    assert ledger["fragment_ids"] == sorted(str(row["fragment_id"]) for row in topic_rows)
    assert ledger["result_ids"] == sorted(str(row["result_ids"][0]) for row in topic_rows)
    assert ledger["relation_ids"] == [relation_id]
    assert ledger["topic_status_by_id"]["topic:00"] == "integrated"
    assert len(ledger["topic_status_by_id"]) == len(topic_rows)

    receipts = {
        str((item.metadata or {}).get("node_id") or item.node_id): item
        for item in executor._receipt_ledger.list_receipts()
    }
    for node_id in reducer_ids:
        assert node_id in raw_outputs
        assert receipts[node_id].response_hash == hash_json(raw_outputs[node_id])
        assert not {
            "processed_topic_ids", "processed_fragment_ids",
            "processed_result_ids", "processed_relation_ids",
        }.intersection(raw_outputs[node_id])


def _shared_v1_qualifier_fixture() -> tuple[dict[str, Any], dict[str, Any]]:
    executor_field = {
        "source_field_id": "field:study:boundary",
        "source_path": "findings.boundary",
        "source_value": "The effect holds only under the stated market-support condition.",
        "paper_key": "paper:study",
        "owner_study_id": "paper:study:1",
        "study_id": "1",
        "disposition": "exact",
        "scope": "explicit_study",
    }
    dependency = {
        "dependency_id": "interpretation-dependency:study-effect",
        "primary_claim_id": "source:effect",
        "primary_evidence_ids": ["evidence:effect"],
        "required_source_claim_ids": ["source:condition"],
        "required_evidence_ids": ["evidence:condition"],
        "required_source_field_ids": [executor_field["source_field_id"]],
        "scope": "explicit_study",
        "paper_key": "paper:study",
        "study_id": "paper:study:1",
        "owner_study_id": "paper:study:1",
    }
    request = {
        "task": "cross_group_comparison_bounded_reduction",
        "node_id": "cross_group_comparison",
        "semantic_contract_version": "semantic-evidence-graph-v2",
        "shared_synthesis_contract_version": "v1",
        "topic_synthesis": [{
            "topic_id": "topic:study",
            "fragment_id": "fragment:study",
            "paper_ids": ["paper:study"],
        }],
        "evidence_units": [{
            "paper_key": "paper:study",
            "study_units": [{
                "study_id": "paper:study:1",
                "source_study_id": "1",
                "claims": [
                    {"claim_id": "source:effect", "study_id": "paper:study:1", "evidence_ids": ["evidence:effect"]},
                    {"claim_id": "source:condition", "study_id": "paper:study:1", "evidence_ids": ["evidence:condition"]},
                ],
                "interpretation_source_fields": [executor_field],
                "interpretation_dependencies": [dependency],
            }],
            "evidence_ids_by_field": {
                "findings": ["evidence:effect", "evidence:condition"],
            },
            "evidence_text_by_id": {
                "evidence:effect": "The measured outcome improved.",
                "evidence:condition": "The improvement depends on the stated condition.",
            },
        }],
    }
    complete_bridge = {
        "claim_id": "synthesis:cross_group_comparison:bounded-study-effect",
        "topic_ids": ["topic:study"],
        "fragment_id": "fragment:study",
        "paper_key": "paper:study",
        "study_id": "paper:study:1",
        "text": "The outcome improved within the supported study condition.",
        "source_claim_ids": ["source:effect", "source:condition"],
        "evidence_ids": ["evidence:effect", "evidence:condition"],
        "source_field_ids": [executor_field["source_field_id"]],
    }
    return request, complete_bridge


@pytest.mark.parametrize(
    ("invalid_mode", "expected_error"),
    [
        ("missing-qualifier", "omits required qualifiers"),
        ("invented-source-claim", "source:invented"),
    ],
)
def test_shared_v1_reducer_rejects_missing_qualifier_or_unsupported_source_claim(
    tmp_path, invalid_mode: str, expected_error: str,
) -> None:
    executor = _executor(tmp_path / invalid_mode, stability_mode="off")
    request, bridge = _shared_v1_qualifier_fixture()
    bridge = dict(bridge)
    if invalid_mode == "missing-qualifier":
        bridge["source_claim_ids"] = ["source:effect"]
        bridge["evidence_ids"] = ["evidence:effect"]
        bridge["source_field_ids"] = []
    else:
        bridge["source_claim_ids"] = ["source:effect", "source:condition", "source:invented"]
    result = {
        "comparisons": [],
        "bridge_claims": [bridge],
        "topic_dispositions": [{
            "topic_id": "topic:study",
            "status": "integrated",
            "synthesis_claim_ids": [bridge["claim_id"]],
        }],
        "unresolved_questions": [],
    }

    with pytest.raises(OutlineV3ExecutionError, match=expected_error):
        executor._validate_semantic_provider_output(
            "cross_group_comparison_provider:reduce:1:1",
            request,
            result,
        )


def _shared_v1_global_coverage_fixture(tmp_path):
    executor = _executor(tmp_path, stability_mode="off")
    topic_rows = [{
        "topic_id": "topic:ledger",
        "fragment_id": "fragment:ledger",
        "paper_ids": ["paper:ledger"],
        "result_ids": ["result:ledger"],
        "supporting_evidence_ids": ["evidence:ledger"],
    }]
    bridge_claim_id = "synthesis:cross_group_comparison:ledger-claim"
    cross_result = {
        "comparisons": [],
        "bridge_claims": [{
            "claim_id": bridge_claim_id,
            "topic_ids": ["topic:ledger"],
            "fragment_id": "fragment:ledger",
            "paper_key": "paper:ledger",
            "text": "The source supports this bounded finding.",
            "evidence_ids": ["evidence:ledger"],
        }],
        "topic_dispositions": [{
            "topic_id": "topic:ledger",
            "status": "integrated",
            "synthesis_claim_ids": [bridge_claim_id],
        }],
        "unresolved_questions": [],
    }
    ledger = executor._build_semantic_coverage_ledger(topic_rows, [], cross_result)
    executor._payloads["cross_group_comparison"] = {"coverage_ledger": ledger}
    global_request = {
        "task": "substantive_global_synthesis",
        "node_id": "global_synthesis",
        "semantic_contract_version": "semantic-evidence-graph-v2",
        "shared_synthesis_contract_version": "v1",
        "topic_synthesis": [],
        "cross_group_comparison": cross_result,
        "cross_coverage_ledger_ref": {
            "content_hash": ledger["content_hash"],
            "topic_count": len(ledger["topic_ids"]),
        },
        "relation_candidates": [],
    }
    global_result = {
        "synthesis_claims": [{
            "claim_id": "synthesis:global_synthesis:ledger-claim",
            "topic_ids": ["topic:ledger"],
            "fragment_id": "fragment:ledger",
            "paper_key": "paper:ledger",
            "text": "The global finding retains the evidenced study boundary.",
            "source_claim_ids": [bridge_claim_id],
            "evidence_ids": ["evidence:ledger"],
        }],
        "organizing_principles": ["Group findings while preserving their supported scope."],
        "unresolved_questions": [],
    }
    return executor, topic_rows, cross_result, ledger, global_request, global_result


def test_global_shared_v1_compact_request_binds_exact_local_cross_coverage_ledger(
    tmp_path,
) -> None:
    executor, topic_rows, cross_result, ledger, request, result = _shared_v1_global_coverage_fixture(tmp_path)

    assert request["topic_synthesis"] == []
    assert "cross_coverage_ledger_ref" in request
    assert not {
        "processed_topic_ids", "processed_fragment_ids", "processed_result_ids",
        "processed_relation_ids",
    }.intersection(cross_result)
    assert ledger["provider_review_status"] == "validated"
    assert ledger["topic_ids"] == [topic_rows[0]["topic_id"]]
    assert request["cross_coverage_ledger_ref"] == {
        "content_hash": ledger["content_hash"],
        "topic_count": 1,
    }
    assert ledger["content_hash"] == hash_json({
        key: value for key, value in ledger.items() if key != "content_hash"
    })

    executor._validate_semantic_provider_output("global_synthesis_provider", request, result)


@pytest.mark.parametrize(
    "tamper_mode",
    ["missing_ref", "wrong_hash", "wrong_topic_count", "tampered_local_ledger"],
)
def test_global_shared_v1_rejects_missing_or_tampered_local_cross_coverage_reference(
    tmp_path, tamper_mode: str,
) -> None:
    executor, _topic_rows, _cross_result, ledger, request, result = _shared_v1_global_coverage_fixture(
        tmp_path / tamper_mode
    )
    if tamper_mode == "missing_ref":
        request.pop("cross_coverage_ledger_ref")
        # Make the provider result otherwise self-contained so rejection must
        # come from the missing local Registry reference itself.
        request["cross_group_comparison"]["topic_members_by_id"] = {
            "topic:ledger": ["paper:ledger"],
        }
        request["cross_group_comparison"]["fragment_members_by_id"] = {
            "fragment:ledger": ["paper:ledger"],
        }
    elif tamper_mode == "wrong_hash":
        request["cross_coverage_ledger_ref"]["content_hash"] = "0" * 64
    elif tamper_mode == "wrong_topic_count":
        request["cross_coverage_ledger_ref"]["topic_count"] = 2
    else:
        tampered = dict(ledger)
        tampered["topic_ids"] = ["topic:forged"]
        executor._payloads["cross_group_comparison"] = {"coverage_ledger": tampered}

    with pytest.raises(OutlineV3ExecutionError) as exc_info:
        executor._validate_semantic_provider_output("global_synthesis_provider", request, result)
    assert "cross-topic coverage ledger" in str(exc_info.value)


def test_global_shared_v1_rejects_cross_result_body_changed_after_ledger_binding(
    tmp_path,
) -> None:
    executor, _topic_rows, _cross_result, ledger, request, result = _shared_v1_global_coverage_fixture(tmp_path)
    request["cross_group_comparison"]["bridge_claims"][0]["text"] = (
        "A different synthesis body was substituted after the local ledger was frozen."
    )

    # The local ledger and its request reference are still internally valid;
    # only the provider-result body no longer matches provider_result_hash.
    assert ledger["content_hash"] == hash_json({
        key: value for key, value in ledger.items() if key != "content_hash"
    })
    assert request["cross_coverage_ledger_ref"]["content_hash"] == ledger["content_hash"]
    assert ledger["provider_result_hash"] != hash_json(request["cross_group_comparison"])

    with pytest.raises(OutlineV3ExecutionError, match="cross-topic coverage ledger"):
        executor._validate_semantic_provider_output("global_synthesis_provider", request, result)


def test_global_provider_transport_is_not_invoked_for_cross_body_ledger_mismatch(
    tmp_path,
) -> None:
    executor, _topic_rows, _cross_result, _ledger, request, _result = (
        _shared_v1_global_coverage_fixture(tmp_path)
    )
    request["cross_group_comparison"]["bridge_claims"][0]["text"] = (
        "This body no longer matches the locally registered cross result."
    )
    provider_calls: list[str] = []

    def counting_provider(node_id: str, _request: Mapping[str, Any]) -> Mapping[str, Any]:
        provider_calls.append(node_id)
        return {"status": "success", "content": {}}

    executor.provider = counting_provider
    with pytest.raises(
        OutlineV3ExecutionError,
        match="cross-topic coverage ledger does not bind its provider result",
    ):
        executor._run_semantic_provider_call(
            "global_synthesis_provider",
            request,
            {"cross_group_comparison": "fixture-dependency"},
        )

    assert provider_calls == []


def test_cross_coverage_ledger_fragment_membership_excludes_topic_level_bridge_papers(
    tmp_path,
) -> None:
    executor = _executor(tmp_path, stability_mode="off")
    topic_rows = [
        {
            "topic_id": "topic:shared",
            "fragment_id": "fragment:F1",
            "paper_ids": ["paper:P1"],
            "bridge_paper_ids": ["paper:P2"],
            "result_ids": ["result:F1"],
            "supporting_evidence_ids": ["evidence:P1"],
        },
        {
            "topic_id": "topic:shared",
            "fragment_id": "fragment:F2",
            "paper_ids": ["paper:P2"],
            "bridge_paper_ids": ["paper:P2"],
            "result_ids": ["result:F2"],
            "supporting_evidence_ids": ["evidence:P2"],
        },
    ]
    provider_result = {
        "comparisons": [],
        "bridge_claims": [{
            "claim_id": "synthesis:cross_group_comparison:bridge-P2",
            "topic_ids": ["topic:shared"],
            "fragment_id": "fragment:F2",
            "paper_key": "paper:P2",
            "text": "The bridge finding is supported by P2 in fragment F2.",
            "evidence_ids": ["evidence:P2"],
        }],
        "topic_dispositions": [{
            "topic_id": "topic:shared",
            "status": "integrated",
            "synthesis_claim_ids": ["synthesis:cross_group_comparison:bridge-P2"],
        }],
        "unresolved_questions": [],
    }

    ledger = executor._build_semantic_coverage_ledger(topic_rows, [], provider_result)

    assert ledger["topic_members_by_id"]["topic:shared"] == ["paper:P1", "paper:P2"]
    assert ledger["fragment_members_by_id"] == {
        "fragment:F1": ["paper:P1"],
        "fragment:F2": ["paper:P2"],
    }


@pytest.mark.parametrize(
    "malformation",
    ["downgraded_with_legacy_arrays", "v1_task_missing", "v1_task_wrong"],
)
def test_global_shared_v1_cannot_bypass_local_ledger_with_legacy_shape_or_task_drift(
    tmp_path, malformation: str,
) -> None:
    executor, _topic_rows, _cross_result, _ledger, request, result = _shared_v1_global_coverage_fixture(
        tmp_path / malformation
    )
    request["cross_group_comparison"].update({
        "shared_synthesis_contract_version": "v1",
        "topic_members_by_id": {"topic:ledger": ["paper:ledger"]},
        "fragment_members_by_id": {"fragment:ledger": ["paper:ledger"]},
        "result_ids": ["result:ledger"],
    })
    if malformation == "downgraded_with_legacy_arrays":
        request.pop("shared_synthesis_contract_version")
        request.pop("cross_coverage_ledger_ref")
        request["cross_group_comparison"].update({
            "processed_topic_ids": ["topic:ledger"],
            "processed_fragment_ids": ["fragment:ledger"],
            "processed_result_ids": ["result:ledger"],
        })
        result.update({
            "processed_topic_ids": ["topic:ledger"],
            "processed_fragment_ids": ["fragment:ledger"],
            "processed_result_ids": ["result:ledger"],
        })
    elif malformation == "v1_task_missing":
        request.pop("task")
        request.pop("cross_coverage_ledger_ref")
    else:
        request["task"] = "not_substantive_global_synthesis"
        request.pop("cross_coverage_ledger_ref")

    with pytest.raises(OutlineV3ExecutionError, match="shared semantic request contract"):
        executor._validate_semantic_provider_output("global_synthesis_provider", request, result)


def test_same_binding_cross_semantic_cache_without_coverage_ledger_is_not_reused(
    tmp_path,
) -> None:
    executor = _executor(tmp_path, stability_mode="off")
    executor.semantic_provider_synthesis_enabled = True
    node_id = "cross_group_comparison"
    dag = executor._node_store.load()
    ancestors: set[str] = set()

    def collect_ancestors(current_id: str) -> None:
        for dependency_id in dag.get(current_id).depends_on:
            if dependency_id not in ancestors:
                ancestors.add(dependency_id)
                collect_ancestors(dependency_id)

    collect_ancestors(node_id)
    pending = set(ancestors)
    while pending:
        progressed = False
        dag = executor._node_store.load()
        for ancestor_id in list(pending):
            record = dag.get(ancestor_id)
            if all(dag.get(dependency_id).status == "succeeded" for dependency_id in record.depends_on):
                executor._dag = executor._node_store.record_node(
                    ancestor_id,
                    status="succeeded",
                    output_hash=hash_text(f"fixture-output:{ancestor_id}"),
                    execution_binding=record.execution_binding,
                )
                pending.remove(ancestor_id)
                progressed = True
        assert progressed, f"fixture DAG has unresolved dependencies: {sorted(pending)}"

    payload = {
        "schema_version": "outline-cross-group-comparison/v1",
        "semantic_contract_version": "semantic-evidence-graph-v2",
        "interpretation_contract_version": executor_module.INTERPRETATION_CONTRACT_VERSION,
        "shared_synthesis_contract_version": "v1",
        "status": "completed",
        "provider_output": {
            "comparisons": [],
            "bridge_claims": [{
                "claim_id": "synthesis:cross_group_comparison:cached",
                "topic_ids": ["topic:cached"],
                "paper_key": "paper:cached",
                "text": "A cached but unbound cross synthesis.",
                "evidence_ids": ["evidence:cached"],
            }],
            "topic_dispositions": [{
                "topic_id": "topic:cached",
                "status": "integrated",
                "synthesis_claim_ids": ["synthesis:cross_group_comparison:cached"],
            }],
            "unresolved_questions": [],
        },
        # Deliberately no coverage_ledger.
    }
    binding = executor.build_current_node_binding(node_id)
    artifact = executor._artifact(executor_module.OutlineArtifact, payload)
    executor._persist(
        node_id,
        artifact,
        execution_binding=binding,
    )

    cached_node = executor._dag.get(node_id)
    assert cached_node.status == "succeeded"
    assert cached_node.execution_binding == binding
    assert executor.registry.get("outline-v3:cross_group_comparison") is not None

    assert executor._load_node(node_id, binding) is None
    assert executor._shared_semantic_cache_valid(node_id, payload) is False



def test_v3_04_unrelated_paper_absent_remains_positive_control(tmp_path) -> None:
    executor = _executor(tmp_path, stability_mode="off")
    claim = "P001 Study 1 reports the conditional effect."
    field_a, dependency_a, support_a = _study_support_fixture("A", "1", claim)
    executor._candidate_interpretation_tables = {
        "source_fields": [field_a],
        "dependencies": [dependency_a],
    }

    executor._validate_candidate_payload(
        "candidate:test",
        {"sections": [{
            "section_id": "paper-a-study-1",
            "paper_keys": ["A"],
            "relation_ids": [],
            "claims": [claim],
            "claim_support": [support_a],
        }]},
        allowed_paper_keys=["A"],
        allowed_relation_ids=[],
        alias_map={"papers_reverse": {"P001": "A"}},
    )
