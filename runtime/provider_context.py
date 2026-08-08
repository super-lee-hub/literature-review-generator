"""Provider context budgets with conservative, request-complete estimation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence


@dataclass(frozen=True)
class ProviderContextProfile:
    provider: str
    model: str
    endpoint_type: str
    model_context_limit: int
    verified_context_limit: int
    input_budget: int
    max_output_tokens: int
    reasoning_reserve: int = 0
    safety_margin: int = 256
    supports_usage_reporting: bool = True
    supports_reasoning_usage: bool = False
    supports_cached_usage: bool = False
    supports_streaming: bool = False
    tokenizer_strategy: str = "conservative_wordpiece_estimator"

    def __post_init__(self) -> None:
        values = (
            self.model_context_limit,
            self.verified_context_limit,
            self.input_budget,
            self.max_output_tokens,
            self.reasoning_reserve,
            self.safety_margin,
        )
        if any(int(value) < 0 for value in values):
            raise ValueError("provider context budgets must be non-negative")
        if self.verified_context_limit > self.model_context_limit:
            raise ValueError("verified_context_limit cannot exceed model_context_limit")
        allowed = self.verified_context_limit - self.max_output_tokens - self.reasoning_reserve - self.safety_margin
        if self.input_budget > allowed:
            raise ValueError("input_budget exceeds the verified provider context budget")

    @classmethod
    def conservative(
        cls,
        *,
        provider: str,
        model: str,
        endpoint_type: str,
        model_context_limit: int = 128_000,
        max_output_tokens: int = 8_192,
        reasoning_reserve: int = 2_048,
        safety_margin: int = 1_024,
        tokenizer_strategy: str = "conservative_wordpiece_estimator",
    ) -> "ProviderContextProfile":
        verified = max(1, int(model_context_limit * 0.8))
        input_budget = max(1, verified - max_output_tokens - reasoning_reserve - safety_margin)
        return cls(
            provider=provider,
            model=model,
            endpoint_type=endpoint_type,
            model_context_limit=model_context_limit,
            verified_context_limit=verified,
            input_budget=input_budget,
            max_output_tokens=max_output_tokens,
            reasoning_reserve=reasoning_reserve,
            safety_margin=safety_margin,
            tokenizer_strategy=tokenizer_strategy,
        )

    def estimate_tokens(self, value: Any) -> int:
        """Estimate a complete request without pretending characters are tokens."""

        if value is None:
            return 0
        if isinstance(value, Mapping):
            return 8 + sum(4 + self.estimate_tokens(key) + self.estimate_tokens(item) for key, item in value.items())
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            return 4 + sum(self.estimate_tokens(item) for item in value)
        text = str(value)
        if not text:
            return 0
        return max(1, (len(text.encode("utf-8")) + 2) // 3)

    def estimate_request(self, request: Mapping[str, Any]) -> dict[str, Any]:
        # Estimate the exact canonical request object.  Do not project a
        # caller-selected subset: evidence packets, relation candidates,
        # citation catalogs, visual references, and future request fields all
        # participate in admission automatically.
        input_tokens = self.estimate_tokens(request)
        total_reserved = input_tokens + self.max_output_tokens + self.reasoning_reserve + self.safety_margin
        return {
            "estimated_input_tokens": input_tokens,
            "input_budget": self.input_budget,
            "max_output_tokens": self.max_output_tokens,
            "reasoning_reserve": self.reasoning_reserve,
            "safety_margin": self.safety_margin,
            "estimated_total_tokens": total_reserved,
            "within_budget": input_tokens <= self.input_budget,
            "estimation_strategy": self.tokenizer_strategy,
        }


__all__ = ["ProviderContextProfile"]
