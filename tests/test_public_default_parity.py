"""Keep the shipped defaults from contradicting the documented architecture.

Three places describe the same routing: ``config.ini.example``,
``default_config_sections()`` and the README model table. When they drift, a new
user can follow the README into a configuration that does something else -- the
worst case being a default that puts a critique on the same model it is meant to
be reviewing.
"""

from __future__ import annotations

import configparser
from pathlib import Path
from typing import Dict, Mapping

import pytest

from services.configuration_service import default_config_sections
from services.model_capabilities import resolve_model_capability

REPO_ROOT = Path(__file__).resolve().parents[1]

# Semantic role -> the [OutlineModels] key that selects its section. The key
# spells the role "critic" while the router calls it "critique".
CRITIC_ROLE_KEYS: Dict[str, str] = {
    "structure_critique": "structure_critic_model",
    "coverage_critique": "coverage_critic_model",
    "evidence_critique": "evidence_critic_model",
}


def _example_sections() -> Mapping[str, Mapping[str, str]]:
    parser = configparser.ConfigParser()
    parser.read(REPO_ROOT / "config.ini.example", encoding="utf-8")
    return {name: dict(parser[name]) for name in parser.sections()}


def _identity(sections: Mapping[str, Mapping[str, str]], section_name: str) -> tuple[str, str, str]:
    capability = resolve_model_capability(dict(sections[section_name]))
    return (capability.provider_family, str(sections[section_name]["model"]), capability.endpoint_type)


@pytest.fixture(params=["example", "defaults"])
def sections(request: pytest.FixtureRequest) -> Mapping[str, Mapping[str, str]]:
    return _example_sections() if request.param == "example" else default_config_sections()


def test_outline_roles_are_all_configured(sections: Mapping[str, Mapping[str, str]]) -> None:
    roles = sections["OutlineModels"]
    for key in (
        "outline_model",
        "relation_adjudicator_model",
        "structure_critic_model",
        "coverage_critic_model",
        "evidence_critic_model",
        "arbitrator_model",
    ):
        assert str(roles.get(key) or "").strip(), f"{key} is not set"


def test_no_critic_defaults_to_the_generator_model(sections: Mapping[str, Mapping[str, str]]) -> None:
    """The shipped default must not review itself.

    Generation and arbitration sharing a model is intended; a critique sharing
    it is exactly the defect role routing exists to prevent.
    """

    roles = sections["OutlineModels"]
    generator = _identity(sections, roles["outline_model"])

    for role, key in CRITIC_ROLE_KEYS.items():
        section_name = roles[key]
        assert _identity(sections, section_name) != generator, (
            f"default {role} resolves to {section_name}, the same model as candidate "
            f"generation ({generator[1]}); the shipped default would self-review"
        )


def test_three_distinct_model_identities_appear_in_defaults(
    sections: Mapping[str, Mapping[str, str]],
) -> None:
    roles = sections["OutlineModels"]
    identities = {
        _identity(sections, roles["outline_model"]),
        _identity(sections, roles["structure_critic_model"]),
        _identity(sections, roles["coverage_critic_model"]),
    }
    assert len(identities) == 3, sorted(identities)


def test_example_and_defaults_agree_on_role_models() -> None:
    example = _example_sections()
    defaults = default_config_sections()

    for role in ("outline_model", "structure_critic_model", "coverage_critic_model"):
        example_section = example["OutlineModels"][role]
        default_section = defaults["OutlineModels"][role]
        assert _identity(example, example_section) == _identity(defaults, default_section), (
            f"{role}: config.ini.example uses {example_section} but setup defaults to "
            f"{default_section}, and the two resolve to different models"
        )


def test_documented_transport_combos_are_valid(sections: Mapping[str, Mapping[str, str]]) -> None:
    """A shipped default must survive the validator it will be checked against."""

    from config_validator import _validate_api_transport_combo

    for section_name in ("Primary_Reader_API", "Writer_API", "Outline_API", "Free_Mode_API"):
        section = sections.get(section_name)
        if not section:
            continue
        errors, _warnings = _validate_api_transport_combo(section_name, dict(section))
        assert errors == [], f"shipped default [{section_name}] fails validation: {errors}"


def test_anthropic_default_sends_the_effort_it_advertises(
    sections: Mapping[str, Mapping[str, str]],
) -> None:
    """The shipped default must send "high", not silently escalate to "max".

    ``force_highest_reasoning = true`` overrides ``reasoning_effort`` with the
    model's top level, which for Opus 5 is "max". The config then reads "high"
    while requesting "max": more expensive, slower, and far more likely to hit
    the 16k output ceiling. Asserting on the config string alone cannot catch
    this, so the check runs the default all the way to the request body.
    """

    from ai_interface import build_anthropic_messages_payload

    section = dict(sections.get("Outline_API") or {})
    if not section or str(section.get("endpoint_type") or "").strip() != "anthropic":
        pytest.skip("Outline_API is not an Anthropic route in these defaults")

    assert str(section.get("reasoning_effort") or "").strip() == "high"
    assert str(section.get("force_highest_reasoning") or "").strip().lower() != "true", (
        "Outline_API sets force_highest_reasoning, which overrides reasoning_effort "
        "with the model's top level and makes the shipped default request more "
        "than it advertises"
    )

    payload = build_anthropic_messages_payload(
        "hello", section, "sys", max_tokens=1024, temperature=0.3, response_format="text",
    )
    assert payload["output_config"]["effort"] == "high", (
        f"the default Anthropic request sends effort={payload['output_config']['effort']!r} "
        "while the configuration advertises high"
    )


def test_an_explicit_top_effort_is_not_rewritten_by_the_defaults(
    sections: Mapping[str, Mapping[str, str]],
) -> None:
    """A user who asks for max must get max -- the fix is about the default."""

    from ai_interface import build_anthropic_messages_payload

    section = dict(sections.get("Outline_API") or {})
    if not section or str(section.get("endpoint_type") or "").strip() != "anthropic":
        pytest.skip("Outline_API is not an Anthropic route in these defaults")

    section["reasoning_effort"] = "max"
    payload = build_anthropic_messages_payload(
        "hello", section, "sys", max_tokens=1024, temperature=0.3, response_format="text",
    )
    assert payload["output_config"]["effort"] == "max"


def test_no_secret_is_shipped_in_the_public_defaults(sections: Mapping[str, Mapping[str, str]]) -> None:
    import re

    secretish = re.compile(r"sk-[A-Za-z0-9_-]{12,}|eyJ[A-Za-z0-9_-]{10,}")

    def walk(node: object) -> None:
        if isinstance(node, Mapping):
            for key, value in node.items():
                if key == "api_key":
                    assert not secretish.search(str(value)), "a public default carries a real-looking key"
                walk(value)

    walk(sections)
