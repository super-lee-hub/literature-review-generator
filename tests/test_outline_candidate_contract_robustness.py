"""Candidate_4-class regressions: opaque alias roundtrip, bounded semantic
repair, external-reference protection (P0-4 / P0-5 / P0-3e).

Drives the real OutlineV3Executor with a stub provider that violates the
candidate contract (outside-corpus paper keys, invented identities) exactly
like the F1 `problem_evidence_synthesis` candidate did, then verifies the
single bounded repair removes illegal IDs or the run fails closed.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

import pytest

from outline.evidence_alias import (
    alias_structural,
    build_alias_map,
    canonicalize_structural,
    canonical_for_alias,
    is_paper_alias,
)
from outline.v3_executor import OutlineV3Executor
from services.artifact_registry import ArtifactRegistry
from services.job_workspace import JobWorkspace
from tests.test_outline_v3_semantic_execution import (
    _configured_test_provider,
    _summary,
)

PAPER_A = "paper-a"
PAPER_B = "paper-b"
FAKE_DOI = "10.1234/fake-doi"
FAKE_SMITH = "Smith2020"


def _executor(
    tmp_path: Path,
    *,
    provider: Any = None,
    semantic_repair_enabled: bool = False,
    opaque_alias_enabled: bool = False,
    candidate_count: int = 2,
) -> OutlineV3Executor:
    workspace = JobWorkspace.create(str(tmp_path), "outline", job_id="outline-job")
    registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    return OutlineV3Executor(
        job_id=workspace.job_id,
        summaries=[
            _summary(PAPER_A, "Study A", "The treatment improved the outcome."),
            _summary(PAPER_B, "Study B", "The treatment improved the outcome under a different context."),
        ],
        workspace=workspace,
        artifact_registry=registry,
        provider=provider or _configured_test_provider,
        candidate_count=candidate_count,
        stability_mode="smoke",
        semantic_repair_enabled=semantic_repair_enabled,
        opaque_alias_enabled=opaque_alias_enabled,
        input_cost_per_1k_tokens=0.0,
        output_cost_per_1k_tokens=0.001,
        reasoning_cost_per_1k_tokens=0.001,
        cache_read_cost_per_1k_tokens=0.0,
        cache_write_cost_per_1k_tokens=0.0,
    )


def _sections(candidate_id: str, paper_keys, relation_ids=(), claims=("planned claim",)) -> dict:
    return {
        "status": "success",
        "content": {
            "candidate_id": candidate_id,
            "organizing_logic": "problem_evidence_synthesis",
            "sections": [
                {
                    "section_id": f"{candidate_id}_section_1",
                    "title": "Synthesis",
                    "paper_keys": list(paper_keys),
                    "relation_ids": list(relation_ids),
                    "claims": list(claims),
                }
            ],
            "claims": list(claims),
        },
    }


def _capturing_provider(
    holder: dict,
    *,
    bad_generation: Mapping | None = None,
    bad_repair: Mapping | None = None,
) -> Any:
    def provider(node_id: str, request: Mapping[str, Any]) -> Mapping[str, Any]:
        holder.setdefault("requests", []).append((node_id, json.dumps(request, ensure_ascii=False)))
        if (request or {}).get("task") == "semantic_repair_of_outline_candidate":
            if bad_repair is not None and str(request.get("candidate_id") or "") == "candidate_2":
                return dict(bad_repair)
            return _configured_test_provider(node_id, request)
        if (
            node_id == "candidate_2_provider_generation"
            and (request or {}).get("output_contract") is not None
            and bad_generation is not None
        ):
            return dict(bad_generation)
        return _configured_test_provider(node_id, request)

    return provider


def _is_repair_request(text: str) -> bool:
    try:
        return json.loads(text).get("task") == "semantic_repair_of_outline_candidate"
    except (ValueError, TypeError):
        return False


def _final_payload(executor: OutlineV3Executor) -> dict:
    record = executor.registry.get("outline-v3:final_outline")
    assert record is not None and record.status == "ready"
    envelope = json.loads(Path(record.path).read_text(encoding="utf-8"))
    payload = envelope.get("payload") if isinstance(envelope.get("payload"), Mapping) else envelope
    assert isinstance(payload, Mapping)
    return payload


def _flat_json_text(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False)


def test_candidate_external_paper_key_rejected_without_repair(tmp_path: Path) -> None:
    holder: dict[str, Any] = {}
    bad = _sections("candidate_2", [PAPER_A, FAKE_DOI, FAKE_SMITH])
    executor = _executor(tmp_path, provider=_capturing_provider(holder, bad_generation=bad))
    result = executor.run()
    assert result.ok is False
    assert any("outside its evidence contract" in item for item in result.diagnostics)
    # the request contract must carry the no-external-evidence semantics
    generation_request = next(
        text for node, text in holder["requests"] if node == "candidate_2_provider_generation"
    )
    assert "no_external_evidence" in generation_request


def test_candidate_semantic_repair_removes_illegal_ids(tmp_path: Path) -> None:
    holder: dict[str, Any] = {}
    bad = _sections("candidate_2", [PAPER_A, FAKE_DOI, FAKE_SMITH])
    cleaned = _sections("candidate_2", [PAPER_A])
    executor = _executor(
        tmp_path,
        provider=_capturing_provider(holder, bad_generation=bad, bad_repair=cleaned),
        semantic_repair_enabled=True,
    )
    result = executor.run()
    assert result.ok is True, result.diagnostics
    repairs = [
        t for _n, t in holder["requests"]
        if json.loads(t).get("task") == "semantic_repair_of_outline_candidate"
    ]
    assert len(repairs) == 1
    final_text = _flat_json_text(_final_payload(executor))
    assert FAKE_DOI not in final_text
    assert FAKE_SMITH not in final_text
    assert PAPER_A in final_text


def test_candidate_semantic_repair_is_bounded_once_and_fails_closed(tmp_path: Path) -> None:
    holder: dict[str, Any] = {}
    bad = _sections("candidate_2", [PAPER_A, FAKE_DOI, FAKE_SMITH])
    # repair returns the same invalid output -> second validation fails -> no 3rd call
    executor = _executor(
        tmp_path,
        provider=_capturing_provider(holder, bad_generation=bad, bad_repair=bad),
        semantic_repair_enabled=True,
    )
    result = executor.run()
    assert result.ok is False
    repairs = [
        t for _n, t in holder["requests"]
        if json.loads(t).get("task") == "semantic_repair_of_outline_candidate"
    ]
    generations = [
        t for _n, t in holder["requests"]
        if (json.loads(t).get("output_contract") is not None)
        and _n == "candidate_2_provider_generation"
    ]
    assert len(generations) == 1
    assert len(repairs) == 1
    failure_files = list(Path(tmp_path).rglob("outline_candidate_repair_failure__*.json"))
    assert failure_files
    failure = json.loads(failure_files[0].read_text(encoding="utf-8"))
    assert failure["artifact_type"] == "outline_candidate_repair_failure"
    assert failure["candidate_id"] == "candidate_2"


def test_candidate_repair_cannot_add_new_claims_or_sections(tmp_path: Path) -> None:
    holder: dict[str, Any] = {}
    bad = _sections("candidate_2", [PAPER_A, FAKE_DOI], claims=("only claim",))
    inflated = _sections("candidate_2", [PAPER_A], claims=("only claim", "invented second claim"))
    executor = _executor(
        tmp_path,
        provider=_capturing_provider(holder, bad_generation=bad, bad_repair=inflated),
        semantic_repair_enabled=True,
    )
    result = executor.run()
    assert result.ok is False
    assert any("inflated the planned claims" in item for item in result.diagnostics)
    assert [
        t for _n, t in holder["requests"]
        if json.loads(t).get("task") == "semantic_repair_of_outline_candidate"
    ]


def test_candidate_repair_prompt_never_carries_full_summaries(tmp_path: Path) -> None:
    holder: dict[str, Any] = {}
    bad = _sections("candidate_2", [PAPER_A, FAKE_DOI])
    cleaned = _sections("candidate_2", [PAPER_A])
    executor = _executor(
        tmp_path,
        provider=_capturing_provider(holder, bad_generation=bad, bad_repair=cleaned),
        semantic_repair_enabled=True,
    )
    result = executor.run()
    assert result.ok is True
    repair_request = next(
        text for _node, text in holder["requests"]
        if json.loads(text).get("task") == "semantic_repair_of_outline_candidate"
    )
    request = json.loads(repair_request)
    assert request["task"] == "semantic_repair_of_outline_candidate"
    assert "validation_error" in request
    assert request["allowed_paper_ids"] == [PAPER_A, PAPER_B]
    repair_text = repair_request
    for forbidden in ("ai_summary", "paper_info", "preprocess", "stage1_input"):
        assert forbidden not in repair_text
    assert "outside the provided evidence corpus" in repair_text


def test_candidate_alias_ids_roundtrip(tmp_path: Path) -> None:
    # With opaque aliases enabled the provider only ever sees/returns P/R tokens;
    # the persisted outline must contain canonical keys again.
    holder: dict[str, Any] = {}
    executor = _executor(
        tmp_path,
        provider=_capturing_provider(holder),
        opaque_alias_enabled=True,
    )
    result = executor.run()
    assert result.ok is True, result.diagnostics
    # provider boundary requests carry opaque tokens only (primary generation
    # requests carry the output_contract envelope; stability audit variants
    # intentionally reuse the same transport node name but are canonical)
    for node, text in holder["requests"]:
        if node in {"candidate_1_provider_generation", "candidate_2_provider_generation"}:
            request = json.loads(text)
            if request.get("output_contract") is None:
                continue
            assert all(is_paper_alias(item) for item in request["paper_keys"])
            assert all(str(item).startswith("R") for item in request["relation_ids"])
    final_text = _flat_json_text(_final_payload(executor))
    assert PAPER_A in final_text and PAPER_B in final_text
    assert "P001" not in final_text
    assert "P002" not in final_text


def test_external_reference_in_summary_cannot_become_outline_paper(tmp_path: Path) -> None:
    # External references mentioned inside a summary must never surface as
    # structural paper identities in the final outline.
    holder: dict[str, Any] = {}
    bad = _sections("candidate_2", [PAPER_A, FAKE_SMITH, FAKE_DOI])
    cleaned = _sections("candidate_2", [PAPER_B])
    executor = _executor(
        tmp_path,
        provider=_capturing_provider(holder, bad_generation=bad, bad_repair=cleaned),
        semantic_repair_enabled=True,
    )
    result = executor.run()
    assert result.ok is True
    final = _final_payload(executor)
    final_text = _flat_json_text(final)
    assert FAKE_SMITH not in final_text and FAKE_DOI not in final_text


def test_alias_map_build_and_canonical_remap_unit() -> None:
    keys = ["10.1016/j.ijresmar.2026.02.001", "中文长key_作者_2024", "10.1287/mnsc.2023.00193"]
    rels = ["rel-hash-α", "rel-hash-β"]
    alias_map = build_alias_map(keys, rels)
    assert list(alias_map["papers"].values()) == ["P001", "P002", "P003"]
    assert list(alias_map["relations"].values()) == ["R001", "R002"]
    for key in keys:
        assert canonical_for_alias(alias_map, alias_map["papers"][key]) == key
    assert alias_map["payload_sha256"]

    request = {
        "paper_keys": keys,
        "relation_ids": rels,
        "relations": [{"relation_id": rels[0], "paper_keys": keys[:2]}],
        "evidence": [{"paper_key": keys[1], "text": "x"}],
    }
    aliased = alias_structural(request, alias_map)
    assert aliased["paper_keys"] == ["P001", "P002", "P003"]
    assert aliased["relations"][0]["paper_keys"] == ["P001", "P002"]
    assert aliased["evidence"][0]["paper_key"] == "P002"
    restored = canonicalize_structural(aliased, alias_map)
    assert restored == request
