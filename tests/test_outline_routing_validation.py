"""Regression tests for Outline role-route validation (item G)."""

from copy import deepcopy

import pytest

from services.configuration_service import default_config_sections
from services.settings import ApplicationSettings


def _complete_config() -> dict:
    return default_config_sections()


def test_validate_outline_config_passes_for_complete_default_routing() -> None:
    settings = ApplicationSettings.from_config(_complete_config())
    assert settings.validate_outline_config() == []


def test_validate_outline_config_flags_incomplete_routed_section() -> None:
    config = _complete_config()
    # Break a section that [OutlineModels] points at: it must stand on its own.
    config["Outline_API"]["api_base"] = ""
    settings = ApplicationSettings.from_config(config)
    errors = settings.validate_outline_config()
    assert any("Outline_API" in e and "complete route" in e for e in errors)


def test_validate_outline_config_flags_missing_role_section() -> None:
    config = _complete_config()
    config["OutlineModels"]["structure_critic_model"] = "Does_Not_Exist_API"
    settings = ApplicationSettings.from_config(config)
    errors = settings.validate_outline_config()
    assert any("Does_Not_Exist_API" in e for e in errors)


def test_outline_routing_diagnostics_flags_self_review() -> None:
    config = _complete_config()
    # Collapse every critique onto the candidate generator's own section.
    config["OutlineModels"]["structure_critic_model"] = "Outline_API"
    config["OutlineModels"]["coverage_critic_model"] = "Outline_API"
    config["OutlineModels"]["evidence_critic_model"] = "Outline_API"
    settings = ApplicationSettings.from_config(config)
    diagnostics = settings.outline_routing_diagnostics()
    assert any("self-review" in d for d in diagnostics)


def test_outline_role_section_defaults_avoid_primary_reader() -> None:
    # G: the shipped defaults must not fall back to Primary_Reader_API for the
    # critique roles, which predates role routing.
    config = _complete_config()
    settings = ApplicationSettings.from_config(config)
    assert settings.coverage_critic_model() == "Free_Mode_API"
    assert settings.evidence_critic_model() == "Writer_API"
