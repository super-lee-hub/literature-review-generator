from __future__ import annotations

import json
from copy import deepcopy

from outline.semantic_request_wire import (
    expand_semantic_request_wire,
    pack_semantic_request_wire,
)
from services.review_generation_service import ReviewGenerationService
from services.writer_wire_projection import project_writer_scope_for_provider_v1
from tests.test_writer_wire_projection import _scope_with_shared_sources


def test_writer_scope_wire_is_lossless_and_reduces_oversize_payload(tmp_path):
    scope = _scope_with_shared_sources(tmp_path)
    scope["tasks"][0]["canonical_source_bundle"][0]["source_fields"][0]["falsey"] = {
        "zero": 0, "false": False, "null": None,
    }
    projected = project_writer_scope_for_provider_v1(scope)
    request = {"writer_task_scope": projected, "identity": "review-v3"}
    wire = pack_semantic_request_wire(request, source_keys=("writer_task_scope",))
    assert expand_semantic_request_wire(wire) == request
    assert len(json.dumps(wire, ensure_ascii=False, sort_keys=True)) < len(
        json.dumps(request, ensure_ascii=False, sort_keys=True)
    )


def test_writer_scope_wire_keeps_request_identity_and_does_not_mutate_source(tmp_path):
    scope = _scope_with_shared_sources(tmp_path)
    projected = project_writer_scope_for_provider_v1(scope)
    request = {"writer_task_scope": projected, "identity": "review-v3"}
    original = deepcopy(request)
    wire = pack_semantic_request_wire(request, source_keys=("writer_task_scope",))
    assert request == original
    expanded = expand_semantic_request_wire(wire)
    assert expanded == original
    assert wire["semantic_source_wire"]["expanded_request_hash"]


def test_writer_prompt_applies_wire_only_when_plain_request_exceeds_profile(tmp_path):
    scope = _scope_with_shared_sources(tmp_path)
    service = object.__new__(ReviewGenerationService)
    service.free_mode_context = {}
    service.summaries = [{"paper_info": {"canonical_paper_key": "paper-a", "title": "Paper A", "authors": [], "year": 2024}, "ai_summary": {"core_analysis": {"summary": "A finding", "methodology": "A method", "findings": "A finding", "conclusions": "A conclusion"}}}]
    service.provider_semantic_contract_version = None
    service._system_prompt = lambda: "writer system"
    service._max_output_tokens = lambda: 128
    class TinyProfile:
        def estimate_request(self, _request):
            return {"within_budget": False}
    service._provider_context_profile = lambda _config: TinyProfile()
    packet = {
        "section_id": "section-1", "section_goal": "goal", "research_question_link": "question",
        "paper_roles": [], "paper_keys": ["paper-a"], "must_use_paper_keys": ["paper-a"],
        "relation_ids": [], "contradictions": [], "boundary_conditions": [], "gaps": [],
    }
    prompt = service._prompt(
        {"section_id": "section-1"}, packet, {"entries": []}, [],
        writer_task_scope=scope,
        writer_config={"max_context_tokens": "1000", "max_output_tokens": "128"},
        max_output_tokens=128,
    )
    payload = json.loads(prompt)
    assert "semantic_source_wire" in payload
    expanded = expand_semantic_request_wire(payload)
    assert expanded["writer_task_scope"] == project_writer_scope_for_provider_v1(scope)


def test_writer_prompt_is_byte_identical_when_plain_request_fits(tmp_path):
    scope = _scope_with_shared_sources(tmp_path)
    service = object.__new__(ReviewGenerationService)
    service.free_mode_context = {}
    service.summaries = [{"paper_info": {"canonical_paper_key": "paper-a", "title": "Paper A", "authors": [], "year": 2024}, "ai_summary": {"core_analysis": {"summary": "A finding", "methodology": "A method", "findings": "A finding", "conclusions": "A conclusion"}}}]
    service.provider_semantic_contract_version = None
    service._system_prompt = lambda: "writer system"
    service._max_output_tokens = lambda: 128
    class LargeProfile:
        def estimate_request(self, _request):
            return {"within_budget": True}
    service._provider_context_profile = lambda _config: LargeProfile()
    packet = {
        "section_id": "section-1", "section_goal": "goal", "research_question_link": "question",
        "paper_roles": [], "paper_keys": ["paper-a"], "must_use_paper_keys": ["paper-a"],
        "relation_ids": [], "contradictions": [], "boundary_conditions": [], "gaps": [],
    }
    baseline = service._prompt({"section_id": "section-1"}, packet, {"entries": []}, [], writer_task_scope=scope)
    fitted = service._prompt(
        {"section_id": "section-1"}, packet, {"entries": []}, [],
        writer_task_scope=scope, writer_config={"max_context_tokens": "1000000"}, max_output_tokens=128,
    )
    assert fitted == baseline
