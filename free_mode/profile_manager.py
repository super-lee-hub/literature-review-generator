"""Persistence helpers for free-mode prompt profiles."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

from services.job_workspace import (
    WorkspacePathError,
    atomic_write_json,
    is_reparse_path,
    validate_path_component,
)

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


def _reject_reparse_ancestors(path: Path) -> None:
    current = path.absolute()
    while True:
        if is_reparse_path(current):
            raise WorkspacePathError(
                f"Free Mode profile path contains a symlink or reparse point: {current}"
            )
        parent = current.parent
        if parent == current:
            return
        current = parent


def get_profile_path(output_dir: str, project_name: str) -> str:
    root = Path(output_dir).expanduser().absolute()
    safe_project = validate_path_component(project_name, field_name="project_name")
    candidate = root / f"{safe_project}_free_mode_profile.json"
    _reject_reparse_ancestors(root)
    if is_reparse_path(candidate):
        raise WorkspacePathError(
            f"Free Mode profile path must not be a symlink or reparse point: {candidate}"
        )
    root_real = Path(os.path.realpath(root))
    candidate_parent_real = Path(os.path.realpath(candidate.parent))
    try:
        common = os.path.commonpath([str(root_real), str(candidate_parent_real)])
    except ValueError as exc:
        raise WorkspacePathError("Free Mode profile path is on a different drive") from exc
    if os.path.normcase(common) != os.path.normcase(str(root_real)):
        raise WorkspacePathError("Free Mode profile path escapes its output root")
    return str(candidate)


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
    root = Path(output_dir).expanduser().absolute()
    _reject_reparse_ancestors(root)
    os.makedirs(root, exist_ok=True)
    path = get_profile_path(output_dir, project_name)
    atomic_write_json(path, normalize_profile(profile))
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
