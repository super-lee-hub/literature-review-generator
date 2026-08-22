from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Dict, Mapping
from urllib.parse import urlparse


_OPENAI_MULTIMODAL_MODEL_PREFIXES = (
    "gpt-4o",
    "gpt-4.1",
    "gpt-4.5",
    "gpt-4-turbo",
    "gpt-4-vision",
)


@dataclass(frozen=True)
class MultimodalCapability:
    supports_image_input: bool
    provider: str
    model: str
    api_base: str
    transport_format: str
    reason: str
    experimental: bool = False
    image_token_upper_bound: int = 0
    supports_base64: bool = False
    supports_external_url: bool = False
    supports_files_api: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _guess_provider(api_base: str) -> str:
    parsed = urlparse(api_base)
    host = (parsed.netloc or parsed.path or "").lower()
    if "api.openai.com" in host:
        return "openai"
    if "openrouter.ai" in host:
        return "openrouter"
    if "azure" in host:
        return "azure_openai_compatible"
    if "anthropic" in host:
        return "anthropic_compatible"
    if "google" in host or "gemini" in host:
        return "google_compatible"
    if "aihubmix" in host:
        return "aihubmix"
    if host:
        return host
    return "unknown"


def detect_multimodal_capability(api_config: Mapping[str, Any] | None) -> MultimodalCapability:
    config = dict(api_config or {})
    api_base = str(config.get("api_base") or "").strip()
    model = str(config.get("model") or "").strip()
    provider = _guess_provider(api_base)

    if not api_base or not model:
        return MultimodalCapability(
            supports_image_input=False,
            provider=provider,
            model=model,
            api_base=api_base,
            transport_format="text_only",
            reason="missing_api_base_or_model",
        )

    parsed = urlparse(api_base)
    host = (parsed.netloc or parsed.path or "").lower()
    normalized_model = model.lower()

    if (
        normalized_model == "deepseek-v4-flash-vision-exp"
        and (provider == "deepseek" or "deepseek" in host or str(config.get("provider_family") or "").lower() == "deepseek")
    ):
        return MultimodalCapability(
            supports_image_input=True,
            provider="deepseek",
            model=model,
            api_base=api_base,
            transport_format="chat_completions_image_url",
            reason="deepseek_vision_experimental_model",
            experimental=True,
            image_token_upper_bound=384,
            supports_base64=True,
            supports_external_url=True,
            supports_files_api=True,
        )

    if "api.openai.com" in host and normalized_model.startswith(_OPENAI_MULTIMODAL_MODEL_PREFIXES):
        return MultimodalCapability(
            supports_image_input=True,
            provider="openai",
            model=model,
            api_base=api_base,
            transport_format="chat_completions_image_url",
            reason="official_openai_multimodal_model",
        )

    return MultimodalCapability(
        supports_image_input=False,
        provider=provider,
        model=model,
        api_base=api_base,
        transport_format="text_only",
        reason="conservative_fallback_for_unsupported_or_unclear_backend",
    )
