"""Provider context budgets with conservative, request-complete estimation."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any


def writer_output_token_limit(api_config: Mapping[str, Any]) -> int:
    """Apply the production Writer's output default and minimum consistently."""

    raw = api_config.get("max_output_tokens") or 32000
    try:
        return max(256, int(raw))
    except (TypeError, ValueError):
        return 32000


@dataclass(frozen=True)
class ProviderRequestEstimateV1:
    """A secret-free estimate bound to one serialized provider request."""

    request_hash: str
    provider: str
    model: str
    endpoint_type: str
    estimated_input_tokens: int
    requested_output_tokens: int
    reasoning_reserve_tokens: int
    safety_margin_tokens: int
    estimated_total_tokens: int
    input_budget: int
    verified_context_limit: int
    within_input_budget: bool
    within_context_budget: bool
    tokenizer_strategy: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "provider-request-estimate-v1",
            "request_hash": self.request_hash,
            "provider": self.provider,
            "model": self.model,
            "endpoint_type": self.endpoint_type,
            "estimated_input_tokens": self.estimated_input_tokens,
            "requested_output_tokens": self.requested_output_tokens,
            "reasoning_reserve_tokens": self.reasoning_reserve_tokens,
            "safety_margin_tokens": self.safety_margin_tokens,
            "estimated_total_tokens": self.estimated_total_tokens,
            "input_budget": self.input_budget,
            "verified_context_limit": self.verified_context_limit,
            "within_input_budget": self.within_input_budget,
            "within_context_budget": self.within_context_budget,
            "tokenizer_strategy": self.tokenizer_strategy,
        }


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
    ) -> ProviderContextProfile:
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

    @classmethod
    def from_api_config(
        cls,
        api_config: Mapping[str, Any],
        *,
        max_output_tokens: int,
        default_provider: str = "configured",
        default_model: str = "",
        default_endpoint_type: str = "responses",
    ) -> ProviderContextProfile:
        """Use the same configured reserves for planning and transport."""

        def positive(value: Any, default: int) -> int:
            try:
                parsed = int(str(value).strip())
            except (TypeError, ValueError):
                return default
            return parsed if parsed > 0 else default

        return cls.conservative(
            provider=str(api_config.get("provider_family") or default_provider),
            model=str(api_config.get("model") or default_model),
            endpoint_type=str(api_config.get("endpoint_type") or default_endpoint_type),
            model_context_limit=positive(api_config.get("max_context_tokens"), 128_000),
            max_output_tokens=max(1, int(max_output_tokens)),
            reasoning_reserve=positive(api_config.get("reasoning_reserve_tokens"), 0),
            safety_margin=positive(api_config.get("safety_margin_tokens"), 256),
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

    def estimate_request_v1(
        self,
        request: Mapping[str, Any],
        *,
        requested_output_tokens: int | None = None,
        reasoning_reserve_tokens: int | None = None,
    ) -> ProviderRequestEstimateV1:
        """Estimate one exact request body for a cross-stage plan projection.

        The request body is hashed with the same helper used by provider
        bindings and receipts. The body itself is never copied into the
        projection. Callers should pass the mapping after the stage-specific
        builder has serialized its provider-visible schema.
        """

        if not isinstance(request, Mapping):
            raise TypeError("provider request must be a mapping")
        if isinstance(requested_output_tokens, bool) or isinstance(reasoning_reserve_tokens, bool):
            raise TypeError("provider token reserves must be integers")
        output = (
            self.max_output_tokens
            if requested_output_tokens is None
            else int(requested_output_tokens)
        )
        reasoning = (
            self.reasoning_reserve
            if reasoning_reserve_tokens is None
            else int(reasoning_reserve_tokens)
        )
        if output < 0 or output > self.max_output_tokens:
            raise ValueError("requested output reserve must fit the provider profile")
        if reasoning < 0:
            raise ValueError("reasoning reserve must be non-negative")

        estimate = self.estimate_request(request)
        input_tokens = int(estimate["estimated_input_tokens"])
        total = input_tokens + output + reasoning + self.safety_margin
        # Import lazily to keep this low-level profile independent of runtime
        # initialization while still sharing the canonical binding hash.
        from runtime.provider_runtime import hash_json

        return ProviderRequestEstimateV1(
            request_hash=hash_json(request),
            provider=self.provider,
            model=self.model,
            endpoint_type=self.endpoint_type,
            estimated_input_tokens=input_tokens,
            requested_output_tokens=output,
            reasoning_reserve_tokens=reasoning,
            safety_margin_tokens=self.safety_margin,
            estimated_total_tokens=total,
            input_budget=self.input_budget,
            verified_context_limit=self.verified_context_limit,
            within_input_budget=input_tokens <= self.input_budget,
            within_context_budget=total <= self.verified_context_limit,
            tokenizer_strategy=self.tokenizer_strategy,
        )


__all__ = ["ProviderContextProfile", "ProviderRequestEstimateV1", "writer_output_token_limit"]
