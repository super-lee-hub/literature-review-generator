from __future__ import annotations

import hashlib
from types import SimpleNamespace

import outline.pipeline as pipeline_module
import pytest
from outline.prompt_budget import estimate_prompt_tokens
from outline.pipeline import V2Pipeline


_ORIGINAL_PROMPTS = {
    "outline_candidates": "candidate original prompt",
    "structure_critique": "structure original prompt",
    "coverage_critique": "coverage original prompt",
    "outline_arbitration": "arbitration original prompt",
}


def _run_pipeline_with_context(monkeypatch, prompt_context: str):
    calls = []

    def fake_candidates(
        literature_map,
        synthesis_flow,
        candidate_count,
        generator_model,
        model_caller,
        _quality_gate,
        **_kwargs,
    ):
        model_caller(
            generator_model,
            _ORIGINAL_PROMPTS["outline_candidates"],
            {"stage": "outline_candidates"},
        )
        return (
            pipeline_module.generate_candidates_deterministic(
                literature_map,
                synthesis_flow,
                candidate_count,
                generator_model,
                "context-job",
            ),
            {},
        )

    def fake_critique(candidates, critic_model, role, model_caller):
        model_caller(
            critic_model,
            _ORIGINAL_PROMPTS[f"{role}_critique"],
            {"stage": f"{role}_critique"},
        )
        if role == "structure":
            return pipeline_module.run_structure_critique_deterministic(
                candidates, critic_model
            )
        return pipeline_module.run_coverage_critique_deterministic(
            candidates, critic_model
        )

    def fake_arbitration(candidates, critiques, arbitrator_model, model_caller):
        model_caller(
            arbitrator_model,
            _ORIGINAL_PROMPTS["outline_arbitration"],
            {"stage": "outline_arbitration"},
        )
        return pipeline_module.arbitrate_deterministic(
            candidates, critiques, arbitrator_model
        )

    def fake_model_caller(route, prompt, metadata):
        calls.append((route, prompt, dict(metadata)))
        return {}

    monkeypatch.setattr(
        pipeline_module, "generate_candidates_production_with_report", fake_candidates
    )
    monkeypatch.setattr(pipeline_module, "run_critique_production", fake_critique)
    monkeypatch.setattr(pipeline_module, "arbitrate_production", fake_arbitration)

    pipeline = V2Pipeline(
        job_id="context-job",
        summaries=[
            {
                "paper_info": {
                    "title": f"Context Paper {index}",
                    "authors": [f"Author {index}"],
                    "year": 2020 + index,
                },
                "themes": ["context"],
                "findings": "A focused finding.",
            }
            for index in range(3)
        ],
        model_caller=fake_model_caller,
        prompt_context=prompt_context,
    )
    result = pipeline.run(candidate_count=2, test_dev_mode=False)
    assert result.ok, result.errors
    return calls


def test_v2_pipeline_prefixes_context_and_records_effective_prompt_provenance(
    monkeypatch,
):
    context = "[FREE MODE IDEA]\nFocus on promotion mechanisms.\n"
    calls = _run_pipeline_with_context(monkeypatch, context)
    expected_hash = hashlib.sha256(context.encode("utf-8")).hexdigest()

    assert {metadata["stage"] for _route, _prompt, metadata in calls} == set(
        _ORIGINAL_PROMPTS
    )
    for _route, prompt, metadata in calls:
        original_prompt = _ORIGINAL_PROMPTS[metadata["stage"]]
        assert prompt == context + original_prompt
        assert metadata["prompt_context_present"] is True
        assert metadata["prompt_context_sha256"] == expected_hash
        assert metadata["prompt_budget"][
            "estimated_input_tokens"
        ] == estimate_prompt_tokens(prompt)
        assert metadata["prompt_budget"][
            "estimated_input_tokens"
        ] > estimate_prompt_tokens(original_prompt)


def test_v2_pipeline_empty_context_leaves_provider_prompts_unchanged(monkeypatch):
    calls = _run_pipeline_with_context(monkeypatch, "")
    empty_hash = hashlib.sha256(b"").hexdigest()

    for _route, prompt, metadata in calls:
        assert prompt == _ORIGINAL_PROMPTS[metadata["stage"]]
        assert metadata["prompt_context_present"] is False
        assert metadata["prompt_context_sha256"] == empty_hash
        assert metadata["prompt_budget"][
            "estimated_input_tokens"
        ] == estimate_prompt_tokens(prompt)


def test_main_passes_resolved_free_mode_context_to_v2_pipeline(monkeypatch):
    import main

    captured = {}

    class FakeCompat:
        def validate_outline_v2_config(self):
            return []

        def outline_test_dev_fixture_mode(self):
            return True

        def outline_candidate_count(self):
            return 3

        def outline_model(self):
            return "Outline_API"

        def structure_critic_model(self):
            return "Writer_API"

        def coverage_critic_model(self):
            return "Primary_Reader_API"

        def arbitrator_model(self):
            return "Outline_API"

        def outline_require_explicit_adopt(self):
            return False

    class CapturingPipeline:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def run(self, **_kwargs):
            return SimpleNamespace(ok=True, coverage_audit=None)

        def persist_artifacts(self, _result):
            return {}

    monkeypatch.setattr(pipeline_module, "V2Pipeline", CapturingPipeline)
    generator = object.__new__(main.LiteratureReviewGenerator)
    generator.job_workspace = None
    generator.summaries = []
    generator.artifact_registry = None
    generator.output_dir = ""
    generator.project_name = "review"
    generator.logger = SimpleNamespace(
        success=lambda *_args, **_kwargs: None,
        error=lambda *_args, **_kwargs: None,
    )
    generator._ensure_compat_config = lambda: FakeCompat()
    generator._outline_v2_model_call = lambda *_args, **_kwargs: {}
    generator._resolve_free_mode_context = lambda: "[FREE MODE IDEA]\nPromotion focus\n"
    generator._load_paper_artifacts_for_outline_v2 = lambda: []

    assert generator._create_literature_review_outline_v2() is True
    assert captured["prompt_context"] == "[FREE MODE IDEA]\nPromotion focus\n"


def test_stage_health_persists_prompt_context_provenance():
    from outline.stage_health import StageHealthCollector

    context_hash = hashlib.sha256(b"topic contract").hexdigest()
    collector = StageHealthCollector(lambda _route, _prompt, _metadata: {"ok": True})
    collector.call(
        "Outline_API",
        "effective prompt",
        {
            "stage": "outline_candidates",
            "prompt_context_present": True,
            "prompt_context_sha256": context_hash,
            "prompt_budget": {"estimated_input_tokens": 123},
        },
    )
    entry = collector.entry(
        "outline_candidates",
        "Outline_API",
        schema_valid=True,
    )

    assert entry.prompt_context_present is True
    assert entry.prompt_context_sha256 == context_hash
    assert entry.to_dict()["prompt_context_sha256"] == context_hash


def test_stage_health_persists_provider_request_receipts():
    from outline.stage_health import StageHealthCollector

    def model_caller(_route, _prompt, metadata):
        metadata.update(
            {
                "configured_model": "claude-fable-5",
                "response_model": "claude-fable-5",
                "provider_response_id": "response-1",
                "request_started_at": "2026-07-30T00:00:00Z",
                "request_completed_at": "2026-07-30T00:00:01Z",
                "transport_status": "success",
            }
        )
        return {"ok": True}

    collector = StageHealthCollector(model_caller)
    collector.call(
        "Outline_API",
        "effective prompt",
        {
            "stage": "outline_candidates",
            "prompt_context_present": True,
            "prompt_context_sha256": "a" * 64,
            "prompt_budget": {"estimated_input_tokens": 123},
        },
    )
    entry = collector.entry("outline_candidates", "Outline_API", schema_valid=True)

    assert len(entry.requests) == 1
    request = entry.requests[0]
    assert request["configured_model"] == "claude-fable-5"
    assert request["response_model"] == "claude-fable-5"
    assert request["provider_response_id"] == "response-1"
    assert request["status"] == "succeeded"


def test_stage_health_treats_null_provider_output_as_failure():
    from outline.stage_health import StageHealthCollector, content_hash

    collector = StageHealthCollector(lambda _route, _prompt, _metadata: None)

    with pytest.raises(ValueError, match="null"):
        collector.call(
            "Outline_API",
            "effective prompt",
            {"stage": "outline_candidates"},
        )

    entry = collector.entry("outline_candidates", "Outline_API", schema_valid=False)
    assert entry.execution_status == "failed"
    assert entry.output_hashes == (content_hash(None),)
    assert entry.adoption_eligible is False


def test_outline_model_call_records_actual_provider_model_and_receipt(monkeypatch):
    import main as main_module

    generator = object.__new__(main_module.LiteratureReviewGenerator)
    generator.config = {
        "API_Parameters": {},
        "Outline_API": {
            "api_key": "test-key",
            "api_base": "https://example.invalid/v1",
            "model": "claude-fable-5",
        },
    }
    generator.logger = SimpleNamespace(
        info=lambda *_args, **_kwargs: None,
        error=lambda *_args, **_kwargs: None,
    )

    monkeypatch.setattr(
        main_module,
        "get_outline_api_config",
        lambda config: dict(config["Outline_API"]),
    )
    monkeypatch.setattr(
        main_module,
        "_call_ai_api_text_detailed",
        lambda **_kwargs: {
            "status": "success",
            "content": '{"candidates": []}',
            "response_model": "claude-fable-5",
            "provider_response_id": "response-1",
            "attempt_count": 1,
            "http_attempt_count": 1,
        },
    )
    metadata = {"stage": "outline_candidates", "candidate_index": 1}

    result = generator._outline_v2_model_call("Outline_API", "prompt", metadata)

    assert result == {"candidates": []}
    assert metadata["configured_model"] == "claude-fable-5"
    assert metadata["response_model"] == "claude-fable-5"
    assert metadata["provider_response_id"] == "response-1"
    assert metadata["transport_status"] == "success"
    assert metadata["request_started_at"].endswith("Z")
    assert metadata["request_completed_at"].endswith("Z")
