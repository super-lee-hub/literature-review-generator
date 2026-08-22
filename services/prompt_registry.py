"""Authoritative registry for prompts used by the production runtime.

Prompt files are inputs, not incidental text assets.  This module resolves
them from one registry, validates their content hash, and renders only the
placeholders declared by the registry entry.  Callers can persist the
returned identity in provider receipts and reuse bindings.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Mapping


_PLACEHOLDER_RE = re.compile(r"\{\{([A-Z0-9_]+)\}\}")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ACTIVE_STATUSES = frozenset({"ACTIVE", "LEGACY", "DELETE"})


class PromptRegistryError(ValueError):
    """Raised when prompt authority or placeholder contracts are invalid."""


@dataclass(frozen=True)
class PromptIdentity:
    prompt_id: str
    version: str
    status: str
    owner: str
    path: str
    sha256: str

    def to_dict(self) -> dict[str, str]:
        return {
            "prompt_id": self.prompt_id,
            "prompt_version": self.version,
            "status": self.status,
            "owner": self.owner,
            "path": self.path,
            "prompt_sha256": self.sha256,
        }


class PromptRegistry:
    """Load and validate prompt authority from ``prompts/registry.json``."""

    def __init__(self, root: str | Path | None = None) -> None:
        self.root = Path(root or Path(__file__).resolve().parents[1]).resolve()
        self.registry_path = self.root / "prompts" / "registry.json"
        try:
            payload = json.loads(self.registry_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise PromptRegistryError(f"unable to load prompt registry: {self.registry_path}") from exc
        if not isinstance(payload, Mapping):
            raise PromptRegistryError("prompt registry root must be an object")
        raw_prompts = payload.get("prompts")
        if not isinstance(raw_prompts, list):
            raise PromptRegistryError("prompt registry must contain a prompts array")
        self._entries: dict[str, dict[str, Any]] = {}
        for raw in raw_prompts:
            if not isinstance(raw, Mapping):
                raise PromptRegistryError("prompt registry entries must be objects")
            prompt_id = str(raw.get("prompt_id") or "").strip()
            if not prompt_id or prompt_id in self._entries:
                raise PromptRegistryError(f"duplicate or empty prompt_id: {prompt_id!r}")
            status = str(raw.get("status") or "").strip().upper()
            if status not in _ACTIVE_STATUSES:
                raise PromptRegistryError(f"unsupported prompt status for {prompt_id}: {status!r}")
            required = raw.get("required_placeholders") or []
            if not isinstance(required, list) or any(not str(item).strip() for item in required):
                raise PromptRegistryError(f"invalid required_placeholders for {prompt_id}")
            entry = dict(raw)
            entry["prompt_id"] = prompt_id
            entry["status"] = status
            entry["required_placeholders"] = [str(item).strip() for item in required]
            self._entries[prompt_id] = entry

    def ids(self, *, status: str | None = None) -> tuple[str, ...]:
        wanted = str(status or "").strip().upper()
        return tuple(sorted(
            prompt_id
            for prompt_id, entry in self._entries.items()
            if not wanted or str(entry.get("status") or "").upper() == wanted
        ))

    def entry(self, prompt_id: str, *, allow_non_active: bool = False) -> Mapping[str, Any]:
        key = str(prompt_id or "").strip()
        entry = self._entries.get(key)
        if entry is None:
            raise PromptRegistryError(f"unknown prompt_id: {key or '<empty>'}")
        if not allow_non_active and str(entry.get("status") or "").upper() != "ACTIVE":
            raise PromptRegistryError(f"prompt is not production-active: {key}")
        return dict(entry)

    def path(self, prompt_id: str, *, allow_non_active: bool = False) -> Path:
        entry = self.entry(prompt_id, allow_non_active=allow_non_active)
        raw_path = str(entry.get("path") or "").strip()
        if not raw_path:
            raise PromptRegistryError(f"prompt has no path: {prompt_id}")
        target = (self.root / raw_path).resolve()
        try:
            target.relative_to(self.root)
        except ValueError as exc:
            raise PromptRegistryError(f"prompt path escapes repository: {prompt_id}") from exc
        if not target.is_file():
            raise PromptRegistryError(f"prompt file does not exist: {target}")
        return target

    def identity(self, prompt_id: str, *, allow_non_active: bool = False) -> PromptIdentity:
        entry = self.entry(prompt_id, allow_non_active=allow_non_active)
        target = self.path(prompt_id, allow_non_active=allow_non_active)
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        declared = str(entry.get("sha256") or "").strip().lower()
        if not _SHA256_RE.fullmatch(declared):
            raise PromptRegistryError(f"prompt {prompt_id} has no valid declared sha256")
        if declared != digest:
            raise PromptRegistryError(
                f"prompt hash mismatch for {prompt_id}: registry={declared}, file={digest}"
            )
        return PromptIdentity(
            prompt_id=str(entry["prompt_id"]),
            version=str(entry.get("version") or ""),
            status=str(entry.get("status") or ""),
            owner=str(entry.get("owner") or ""),
            path=str(target),
            sha256=digest,
        )

    def read(self, prompt_id: str, *, allow_non_active: bool = False) -> str:
        # Every production read re-checks the declared content hash.  A caller
        # must never be able to use an edited prompt merely because the
        # registry object was instantiated before the edit.
        self.identity(prompt_id, allow_non_active=allow_non_active)
        return self.path(prompt_id, allow_non_active=allow_non_active).read_text(encoding="utf-8")

    def read_json(self, prompt_id: str, *, allow_non_active: bool = False) -> dict[str, Any]:
        """Read a Registry-authorized JSON prompt and fail closed on shape."""

        entry = self.entry(prompt_id, allow_non_active=allow_non_active)
        text = self.read(prompt_id, allow_non_active=allow_non_active)
        try:
            payload = json.loads(text)
        except (TypeError, UnicodeError, json.JSONDecodeError) as exc:
            raise PromptRegistryError(f"prompt {prompt_id} is not valid JSON") from exc
        if not isinstance(payload, dict):
            raise PromptRegistryError(f"prompt {prompt_id} JSON root must be an object")
        required_roles = entry.get("required_roles") or []
        if isinstance(required_roles, list):
            missing = [str(role) for role in required_roles if str(role) not in payload]
            if missing:
                raise PromptRegistryError(f"prompt {prompt_id} is missing required roles: {missing}")
            if any(not isinstance(payload.get(str(role)), str) or not str(payload.get(str(role))).strip() for role in required_roles):
                raise PromptRegistryError(f"prompt {prompt_id} has empty or non-string role policies")
        return {str(key): value for key, value in payload.items()}

    def render(
        self,
        prompt_id: str,
        values: Mapping[str, Any] | None = None,
        *,
        allow_non_active: bool = False,
        strict: bool = True,
    ) -> str:
        entry = self.entry(prompt_id, allow_non_active=allow_non_active)
        text = self.read(prompt_id, allow_non_active=allow_non_active)
        supplied = {str(key): "" if value is None else str(value) for key, value in dict(values or {}).items()}
        required = {str(item) for item in entry.get("required_placeholders") or []}
        found = set(_PLACEHOLDER_RE.findall(text))
        missing_declared = required - found
        if missing_declared:
            raise PromptRegistryError(
                f"prompt {prompt_id} declares placeholders absent from file: {sorted(missing_declared)}"
            )
        missing_values = required - supplied.keys()
        if strict and missing_values:
            raise PromptRegistryError(
                f"prompt {prompt_id} missing required values: {sorted(missing_values)}"
            )
        rendered = text
        for name in found:
            if name in supplied:
                rendered = rendered.replace("{{" + name + "}}", supplied[name])
        unresolved = sorted(set(_PLACEHOLDER_RE.findall(rendered)))
        if strict and unresolved:
            raise PromptRegistryError(f"prompt {prompt_id} has unresolved placeholders: {unresolved}")
        return rendered

    def validate(self) -> tuple[PromptIdentity, ...]:
        identities = tuple(self.identity(prompt_id, allow_non_active=True) for prompt_id in self.ids())
        active_paths = {
            str(self.path(prompt_id)).casefold()
            for prompt_id in self.ids(status="ACTIVE")
        }
        active_root = self.root / "prompts" / "active"
        actual_paths = {
            str(path.resolve()).casefold()
            for path in active_root.rglob("*")
            if path.is_file()
        }
        orphaned = sorted(actual_paths - active_paths)
        if orphaned:
            raise PromptRegistryError(f"orphan ACTIVE prompt files: {orphaned}")
        for identity in identities:
            entry = self.entry(identity.prompt_id, allow_non_active=True)
            owner = str(entry.get("owner") or "").strip()
            version = str(entry.get("version") or "").strip()
            if not owner or not version:
                raise PromptRegistryError(f"prompt {identity.prompt_id} requires owner and version")
            if str(entry.get("path") or "").lower().endswith(".json"):
                self.read_json(identity.prompt_id, allow_non_active=True)
        return identities


def default_prompt_registry() -> PromptRegistry:
    return PromptRegistry()


def load_active_prompt(prompt_id: str, values: Mapping[str, Any] | None = None) -> tuple[str, PromptIdentity]:
    registry = default_prompt_registry()
    identity = registry.identity(prompt_id)
    return registry.render(prompt_id, values), identity


__all__ = [
    "PromptIdentity",
    "PromptRegistry",
    "PromptRegistryError",
    "default_prompt_registry",
    "load_active_prompt",
]
