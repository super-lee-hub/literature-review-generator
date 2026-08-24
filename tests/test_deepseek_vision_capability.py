from __future__ import annotations

from services.multimodal_capability import detect_multimodal_capability


def test_deepseek_vision_exact_model_is_image_capable() -> None:
    capability = detect_multimodal_capability(
        {
            "model": "deepseek-v4-flash-vision-exp",
            "api_base": "https://api.deepseek.com",
            "provider_family": "deepseek",
        }
    )
    assert capability.supports_image_input is True
    assert capability.provider == "deepseek"
    assert capability.transport_format == "chat_completions_image_url"
    assert capability.experimental is True
    assert capability.image_token_upper_bound == 384
    assert capability.supports_base64 is True
    assert capability.supports_files_api is True


def test_ordinary_deepseek_flash_is_text_only() -> None:
    capability = detect_multimodal_capability(
        {
            "model": "deepseek-v4-flash",
            "api_base": "https://api.deepseek.com",
            "provider_family": "deepseek",
        }
    )
    assert capability.supports_image_input is False
    assert capability.transport_format == "text_only"
