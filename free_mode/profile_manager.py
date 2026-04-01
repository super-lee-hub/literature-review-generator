"""Persistence helpers for free-mode prompt profiles."""

from __future__ import annotations

import json
import os
from typing import Any, Dict, Optional


DEFAULT_PROFILE: Dict[str, Any] = {
    "research_goal": "",
    "concept_relationship": "",
    "focus_points": [],
    "exclusions": [],
    "theory_or_variable_focus": [],
    "outline_preferences": [],
    "writing_constraints": [],
    "generated_prompt": "",
    "conversation_notes": [],
}


def get_profile_path(output_dir: str, project_name: str) -> str:
    return os.path.join(output_dir, f"{project_name}_free_mode_profile.json")


def normalize_profile(profile: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    normalized = dict(DEFAULT_PROFILE)
    if profile:
        normalized.update(profile)
    for key in ("focus_points", "exclusions", "theory_or_variable_focus", "outline_preferences", "writing_constraints", "conversation_notes"):
        value = normalized.get(key, [])
        if not isinstance(value, list):
            normalized[key] = [str(value)] if value else []
    return normalized


def save_profile(profile: Dict[str, Any], output_dir: str, project_name: str) -> str:
    os.makedirs(output_dir, exist_ok=True)
    path = get_profile_path(output_dir, project_name)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(normalize_profile(profile), handle, ensure_ascii=False, indent=2)
    return path


def load_profile(output_dir: str, project_name: str) -> Optional[Dict[str, Any]]:
    path = get_profile_path(output_dir, project_name)
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as handle:
        return normalize_profile(json.load(handle))


def build_profile_context(profile: Optional[Dict[str, Any]]) -> str:
    """Render a compact profile block for downstream prompts."""

    normalized = normalize_profile(profile)
    if not any(str(value).strip() for value in normalized.values()):
        return ""

    return (
        "\n[FREE MODE PROFILE]\n"
        f"Research goal: {normalized['research_goal']}\n"
        f"Concept relationship: {normalized['concept_relationship']}\n"
        f"Focus points: {', '.join(normalized['focus_points'])}\n"
        f"Exclusions: {', '.join(normalized['exclusions'])}\n"
        f"Theory or variable focus: {', '.join(normalized['theory_or_variable_focus'])}\n"
        f"Outline preferences: {', '.join(normalized['outline_preferences'])}\n"
        f"Writing constraints: {', '.join(normalized['writing_constraints'])}\n"
        f"Generated prompt: {normalized['generated_prompt']}\n"
        f"Conversation notes: {', '.join(normalized['conversation_notes'])}\n"
    )
