"""Repeated critics share an unchanged local projection without losing plan rows."""
from __future__ import annotations

from dataclasses import asdict, replace

from tests.test_run_first_future_preflight import _executor


def test_all_provider_rows_remain_while_repeated_candidate_work_is_materialized_once(tmp_path, monkeypatch):
    executor = _executor(tmp_path)
    nodes = ("candidate_1_provider_generation", "candidate_2_provider_generation",
             "structure_critique", "coverage_critique", "evidence_critique", "arbitration")
    monkeypatch.setattr(executor, "_provider_node_ids", lambda: nodes)
    seen = []

    def project(summaries, profile, *, variant_name, candidate_id=None):
        seen.append((variant_name, candidate_id or "candidate_1", asdict(profile)))
        executor._candidate_preflight_projection_state[f"{variant_name}:{candidate_id or 'candidate_1'}"] = {
            "status": "unmaterialized_legacy_paper_shard_projection",
        }
        return 32_000, 2

    monkeypatch.setattr(executor, "_candidate_hierarchical_preflight", project)
    first = executor._build_provider_call_plans()
    assert tuple(row.node_id for row in first) == nodes
    assert [row[1] for row in seen] == ["candidate_1", "candidate_2"]
    assert all(row.estimated_total_tokens > 0 for row in first)
    # A later operation must rebuild; the local result is not durable reuse.
    second = executor._build_provider_call_plans()
    assert second == first
    assert [row[1] for row in seen] == ["candidate_1", "candidate_2"] * 2


def test_profile_change_between_roles_cannot_use_previous_projection(tmp_path, monkeypatch):
    executor = _executor(tmp_path)
    original_route = executor._role_route
    changed = False
    seen = []

    def nodes():
        nonlocal changed
        yield "structure_critique"
        changed = True
        yield "coverage_critique"
        yield "evidence_critique"

    def route(node):
        result = original_route(node)
        if node == "candidate_1_provider_generation" and changed:
            result = replace(result, profile=replace(result.profile, max_output_tokens=512))
        return result

    def project(_summaries, profile, *, variant_name, candidate_id=None):
        seen.append(profile.max_output_tokens)
        return 32_000, 2

    monkeypatch.setattr(executor, "_provider_node_ids", nodes)
    monkeypatch.setattr(executor, "_role_route", route)
    monkeypatch.setattr(executor, "_candidate_hierarchical_preflight", project)
    rows = executor._build_provider_call_plans()
    assert tuple(row.node_id for row in rows) == ("structure_critique", "coverage_critique", "evidence_critique")
    assert len(seen) == 2 and seen[0] != seen[1] and seen[1] == 512


def test_all_candidate_projection_above_five_is_not_candidate_one(tmp_path, monkeypatch):
    executor = _executor(tmp_path, candidate_count=6)
    monkeypatch.setattr(executor, "_provider_node_ids", lambda: (
        "candidate_1_provider_generation", "structure_critique", "coverage_critique",
    ))
    seen = []

    def project(_summaries, _profile, *, variant_name, candidate_id=None):
        seen.append(candidate_id)
        return 32_000, 2 if candidate_id else 6

    monkeypatch.setattr(executor, "_candidate_hierarchical_preflight", project)
    rows = executor._build_provider_call_plans()
    assert len(rows) == 3
    assert seen == ["candidate_1", None]
