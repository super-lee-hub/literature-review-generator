from types import SimpleNamespace

import validator


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


def test_validator_text_truncation_uses_token_budget() -> None:
    text = "中" * 20

    truncated = validator._truncate_text_to_token_budget(text, 5)

    assert validator.estimate_tokens(truncated) <= 5
    assert truncated == "中" * 5
