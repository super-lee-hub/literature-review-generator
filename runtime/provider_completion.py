"""Fail-closed evaluation of provider responses."""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Mapping


@dataclass(frozen=True)
class ProviderCompletion:
    status: str
    content: Any = None
    error_kind: str | None = None
    finish_reason: str = ""
    incomplete_reason: str = ""
    message: str = ""


class ProviderCompletionEvaluator:
    """Normalize transport responses before they can be treated as success."""

    @classmethod
    def evaluate(
        cls,
        response: Mapping[str, Any] | Any,
        *,
        minimum_output: int = 1,
        expect_json: bool = False,
    ) -> ProviderCompletion:
        if not isinstance(response, Mapping):
            response = {"content": response}
        content = response.get("content")
        finish_reason = str(response.get("finish_reason") or "")
        incomplete_reason = str(response.get("incomplete_reason") or "")
        response_status = str(response.get("status") or "success").casefold()
        if response_status in {"cancelled", "canceled"}:
            return ProviderCompletion("cancelled", content, "cancelled", finish_reason, incomplete_reason)
        if response_status in {"failed", "error", "blocked"}:
            return ProviderCompletion("transport_failed", content, str(response.get("error_kind") or "transient_network"), finish_reason, incomplete_reason, str(response.get("message") or ""))
        if finish_reason == "length":
            return ProviderCompletion("incomplete_length", content, "invalid_response", finish_reason, incomplete_reason)
        if incomplete_reason or response_status == "incomplete":
            return ProviderCompletion("incomplete_reasoning_budget", content, "invalid_response", finish_reason, incomplete_reason)
        if content is None or (isinstance(content, str) and not content.strip()):
            return ProviderCompletion("empty_output", content, "invalid_response", finish_reason, incomplete_reason)
        if isinstance(content, str) and len(content.strip()) < minimum_output:
            return ProviderCompletion("tiny_output", content, "invalid_response", finish_reason, incomplete_reason)
        if expect_json:
            if isinstance(content, str):
                try:
                    content = json.loads(content)
                except json.JSONDecodeError as exc:
                    return ProviderCompletion("malformed_json", content, "invalid_response", finish_reason, incomplete_reason, str(exc))
            if not isinstance(content, Mapping):
                return ProviderCompletion("schema_invalid", content, "invalid_response", finish_reason, incomplete_reason)
        return ProviderCompletion("complete", content, None, finish_reason, incomplete_reason)


__all__ = ["ProviderCompletion", "ProviderCompletionEvaluator"]
