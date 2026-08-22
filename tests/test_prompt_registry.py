from __future__ import annotations

from pathlib import Path

import pytest

from services.prompt_registry import PromptRegistry, PromptRegistryError


def test_active_prompt_registry_is_hash_valid_and_has_no_orphans() -> None:
    registry = PromptRegistry()
    identities = registry.validate()
    assert identities
    assert all(item.status in {"ACTIVE", "LEGACY"} for item in identities)
    assert all(item.owner and item.version and len(item.sha256) == 64 for item in identities)


def test_delete_or_legacy_prompt_cannot_be_loaded_as_active() -> None:
    registry = PromptRegistry()
    with pytest.raises(PromptRegistryError):
        registry.read("validation.legacy.claims_batch.v1")


def test_required_placeholders_are_strictly_rendered() -> None:
    registry = PromptRegistry()
    with pytest.raises(PromptRegistryError):
        registry.render("stage1.analysis.user.v3", {"PAPER_FULL_TEXT": "text"})
    rendered = registry.render(
        "stage1.analysis.user.v3",
        {
                "PAPER_FULL_TEXT": "paper",
                "VISUAL_COVERAGE_JSON": "{}",
                "VISUAL_OBSERVATIONS_JSON": "[]",
                "SUMMARY_SCHEMA_CONTRACT": "{}",
        },
    )
    assert "{{PAPER_FULL_TEXT}}" not in rendered
    assert "paper" in rendered
