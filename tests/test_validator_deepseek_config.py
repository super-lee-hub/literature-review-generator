from types import SimpleNamespace

import validator
import pytest


class _DummyConfig:
    def __init__(self, sections):
        self._sections = sections

    def get(self, section, fallback=None):
        return self._sections.get(section, fallback)


def test_validator_api_config_carries_deepseek_reasoning_options() -> None:
    generator = SimpleNamespace(
        config=_DummyConfig(
            {
                "Validator_API": {
                    "api_key": "sk-test",
                    "model": "deepseek-v4-pro",
                    "api_base": "https://api.deepseek.com",
                    "endpoint_type": "chat_completions",
                    "provider_family": "deepseek",
                    "thinking": "enabled",
                    "reasoning_effort": "max",
                    "max_context_tokens": "1000000",
                    "force_highest_reasoning": "true",
                }
            }
        )
    )

    api_config = validator._get_validator_api_config(generator)

    assert api_config is not None
    assert api_config.get("endpoint_type") == "chat_completions"
    assert api_config.get("provider_family") == "deepseek"
    assert api_config.get("thinking") == "enabled"
    assert api_config.get("reasoning_effort") == "max"
    assert api_config.get("max_context_tokens") == "1000000"
    assert api_config.get("force_highest_reasoning") == "true"


def test_validator_text_budget_rejects_partial_evidence() -> None:
    text = "中" * 20

    with pytest.raises(validator.ValidationContextBudgetError, match="requires"):
        validator._ensure_text_within_token_budget(text, 5)
