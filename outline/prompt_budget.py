"""Fail-closed prompt budgeting for Outline Intelligence v2."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from typing import Any, Mapping


PROMPT_BUDGET_VERSION = "v1"


class OutlinePromptBudgetExceeded(ValueError):
    """Raised when an indivisible controlled-corpus packet exceeds the budget."""


def estimate_prompt_tokens(text: str) -> int:
    """Conservative dependency-free estimate for mixed CJK/JSON prompts."""
    cjk = sum(1 for char in text if "\u3400" <= char <= "\u9fff")
    return cjk + math.ceil((len(text) - cjk) / 4)


@dataclass(frozen=True)
class PromptBudgetV1:
    model_context_limit: int
    max_output_tokens: int
    safety_margin_ratio: float = 0.10
    system_prompt_tokens: int = 0
    version: str = PROMPT_BUDGET_VERSION

    def __post_init__(self) -> None:
        if self.model_context_limit <= 0 or self.max_output_tokens <= 0:
            raise ValueError("context and output token limits must be positive")
        if not 0 <= self.safety_margin_ratio < 1:
            raise ValueError("safety margin ratio must be in [0, 1)")
        if self.input_budget_tokens <= 0:
            raise ValueError("prompt input budget must be positive")

    @property
    def safety_margin_tokens(self) -> int:
        return math.ceil(self.model_context_limit * self.safety_margin_ratio)

    @property
    def input_budget_tokens(self) -> int:
        return (
            self.model_context_limit
            - self.max_output_tokens
            - self.safety_margin_tokens
            - self.system_prompt_tokens
        )

    def fits(self, prompt: str) -> bool:
        return estimate_prompt_tokens(prompt) <= self.input_budget_tokens

    def assert_fits(self, prompt: str, *, stage: str) -> None:
        estimated = estimate_prompt_tokens(prompt)
        if estimated > self.input_budget_tokens:
            raise OutlinePromptBudgetExceeded(
                f"{stage} prompt requires about {estimated} tokens; budget is {self.input_budget_tokens}"
            )

    def metadata(self, prompt: str) -> dict[str, Any]:
        return {
            "version": self.version,
            "model_context_limit": self.model_context_limit,
            "max_output_tokens": self.max_output_tokens,
            "safety_margin_tokens": self.safety_margin_tokens,
            "system_prompt_tokens": self.system_prompt_tokens,
            "input_budget_tokens": self.input_budget_tokens,
            "estimated_input_tokens": estimate_prompt_tokens(prompt),
        }


def packet_hash(packet: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        packet, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
