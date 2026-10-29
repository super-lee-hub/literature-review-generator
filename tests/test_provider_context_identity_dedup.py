from __future__ import annotations

from copy import deepcopy

import pytest

from ai_interface import build_chat_completions_payload
from runtime.provider_context import ProviderContextProfile
from runtime.provider_runtime import canonical_provider_request_payload, hash_json


def _profile() -> ProviderContextProfile:
    return ProviderContextProfile.conservative(
        provider="generic", model="fixture", endpoint_type="chat_completions",
        model_context_limit=16_000, max_output_tokens=1_000,
        reasoning_reserve=100, safety_margin=100,
    )


def _request(text: str) -> dict:
    return canonical_provider_request_payload(
        prompt=text, system_prompt="Preserve sources and qualifiers.",
        user_content=None, response_format="json", max_output_tokens=1_000,
        temperature=0.2,
    )


def test_text_identity_counts_the_single_transmitted_prompt_and_preserves_hash() -> None:
    text = "A reports a zero effect; B reports a reverse effect under its qualifier. " * 300
    request = _request(text)
    before = deepcopy(request)
    transport = build_chat_completions_payload(
        text,
        {"model": "fixture", "provider_family": "generic", "endpoint_type": "chat_completions"},
        request["system"], max_tokens=1_000, temperature=0.2, response_format="json",
    )
    assert transport["messages"][1]["content"] == text
    profile = _profile()
    estimate = profile.estimate_request_v1(request)
    assert estimate.estimated_input_tokens == profile.estimate_tokens(
        {key: value for key, value in request.items() if key != "user"}
    )
    assert estimate.within_input_budget is True
    assert profile.estimate_tokens(request) > profile.input_budget
    assert estimate.request_hash == hash_json(before)
    assert request == before


@pytest.mark.parametrize("mutation", ["different_text", "second_text", "image", "extra_text_field", "other_version"])
def test_only_exact_single_text_duplicate_receives_deduplication(mutation: str) -> None:
    request = _request("source claim and indispensable qualifier" * 100)
    if mutation == "different_text":
        request["user_content"][0]["text"] += " altered"
    elif mutation == "second_text":
        request["user_content"].append({"type": "text", "text": "additional source"})
    elif mutation == "image":
        request["user_content"].append({"type": "image", "sha256": "a" * 64})
    elif mutation == "extra_text_field":
        request["user_content"][0]["extra"] = "future field"
    else:
        request["identity_version"] = "provider_request_identity/v2"
    profile = _profile()
    assert profile.estimate_request(request)["estimated_input_tokens"] == profile.estimate_tokens(request)


def test_additional_request_fields_and_output_reserves_still_affect_admission() -> None:
    request = _request("retained source" * 100)
    profile = _profile()
    before = profile.estimate_request_v1(request)
    request["future_source_context"] = {"qualifier": "q" * 40_000}
    after = profile.estimate_request_v1(request)
    assert after.estimated_input_tokens > before.estimated_input_tokens
    assert after.within_input_budget is False
    assert after.within_context_budget is False
    assert after.request_hash != before.request_hash
    assert after.estimated_total_tokens == (
        after.estimated_input_tokens + after.requested_output_tokens
        + after.reasoning_reserve_tokens + after.safety_margin_tokens
    )
