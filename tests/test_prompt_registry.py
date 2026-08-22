from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from services.prompt_registry import PromptRegistry, PromptRegistryError


def test_prompt_hash_is_stable_when_windows_materializes_crlf(tmp_path: Path) -> None:
    prompt_text = "system line\nuser line\n"
    prompt_path = tmp_path / "prompts" / "active" / "test.txt"
    prompt_path.parent.mkdir(parents=True)
    prompt_path.write_bytes(prompt_text.replace("\n", "\r\n").encode("utf-8"))
    registry_path = prompt_path.parents[1] / "registry.json"
    registry_path.write_text(
        json.dumps(
            {
                "prompts": [
                    {
                        "prompt_id": "test.prompt",
                        "version": "v1",
                        "status": "ACTIVE",
                        "owner": "test",
                        "path": "prompts/active/test.txt",
                        "sha256": hashlib.sha256(prompt_text.encode("utf-8")).hexdigest(),
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    registry = PromptRegistry(tmp_path)
    identity = registry.identity("test.prompt")

    assert identity.sha256 == hashlib.sha256(prompt_text.encode("utf-8")).hexdigest()
    assert registry.read("test.prompt") == prompt_text


def test_prompt_read_and_render_recheck_hash_after_registry_initialization(tmp_path: Path) -> None:
    prompt_path = tmp_path / "prompts" / "active" / "test.txt"
    prompt_path.parent.mkdir(parents=True)
    prompt_path.write_text("hello {{VALUE}}\n", encoding="utf-8")
    registry_path = prompt_path.parents[1] / "registry.json"
    registry_path.write_text(
        json.dumps(
            {
                "prompts": [
                    {
                        "prompt_id": "test.prompt",
                        "version": "v1",
                        "status": "ACTIVE",
                        "owner": "test",
                        "path": "prompts/active/test.txt",
                        "required_placeholders": ["VALUE"],
                        "sha256": hashlib.sha256(b"hello {{VALUE}}\n").hexdigest(),
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    registry = PromptRegistry(tmp_path)
    prompt_path.write_text("tampered {{VALUE}}\n", encoding="utf-8")

    with pytest.raises(PromptRegistryError):
        registry.read("test.prompt")
    with pytest.raises(PromptRegistryError):
        registry.render("test.prompt", {"VALUE": "value"})


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
