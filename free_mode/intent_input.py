"""Typed Free Mode input authority and deterministic ReviewIntent projection.

The external profile path is only metadata after intake.  The Registry
artifact bytes are the authority consumed by resume and downstream stages.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from free_mode.profile_manager import normalize_profile
from outline.v3_evidence import build_review_intent
from runtime.provider_runtime import hash_json
from services.artifact_registry import file_sha256


FREE_MODE_INTENT_INPUT_ARTIFACT_TYPE = "free_mode_intent_input"
FREE_MODE_INTENT_INPUT_ARTIFACT_VERSION = "v1"
FREE_MODE_INTENT_INPUT_ARTIFACT_ID = "free_mode_intent_input"
FREE_MODE_REVIEW_INTENT_PROJECTION_ARTIFACT_TYPE = "free_mode_review_intent_projection"
FREE_MODE_REVIEW_INTENT_PROJECTION_ARTIFACT_VERSION = "v1"
FREE_MODE_REVIEW_INTENT_PROJECTION_ARTIFACT_ID = "free_mode_review_intent_projection"

_PROFILE_LIST_FIELDS = (
    "focus_points",
    "exclusions",
    "theory_or_variable_focus",
    "outline_preferences",
    "writing_constraints",
    "conversation_notes",
)

_LITERAL_INTENT_FIELDS = (
    "scope",
    "target_audience",
    "desired_contribution",
    "language",
    "target_depth",
    "target_length",
)


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _stable_unique(values: Sequence[Any]) -> list[str]:
    by_key: dict[str, str] = {}
    for value in values:
        text = str(value).strip()
        if not text:
            continue
        by_key.setdefault(text.casefold(), text)
    return [by_key[key] for key in sorted(by_key)]


def _text_values(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, Mapping):
        items: list[Any] = []
        for key in sorted(value, key=str):
            items.extend(_text_values(value[key]))
        return _stable_unique(items)
    if isinstance(value, (list, tuple, set)):
        items = []
        for item in value:
            items.extend(_text_values(item))
        return _stable_unique(items)
    text = str(value).strip()
    return [text] if text else []


def project_review_intent(profile: Mapping[str, Any] | None) -> dict[str, Any]:
    """Map profile fields literally into the existing ReviewIntent contract."""

    normalized = normalize_profile(dict(profile or {}))
    intent: dict[str, Any] = {
        "review_question": str(normalized.get("research_goal") or "").strip(),
        "must_cover": _stable_unique(
            [
                *_text_values(normalized.get("focus_points")),
                *_text_values(normalized.get("theory_or_variable_focus")),
            ]
        ),
        "must_not_do": _text_values(normalized.get("exclusions")),
        "preferred_organizing_logic": "\n".join(
            _text_values(normalized.get("outline_preferences"))
        ),
    }
    for field_name in _LITERAL_INTENT_FIELDS:
        value = normalized.get(field_name)
        if isinstance(value, str) and str(value).strip():
            intent[field_name] = str(value).strip()
    return build_review_intent(intent).to_dict()


def _context_hash(
    *,
    source_kind: str,
    profile: Mapping[str, Any],
    raw_idea: str,
    review_intent: Mapping[str, Any],
) -> str:
    return hash_json(
        {
            "source_kind": source_kind,
            "profile": dict(profile),
            "raw_idea": raw_idea,
            "review_intent": dict(review_intent),
        }
    )


def build_free_mode_intent_envelope(
    *,
    profile_path: str = "",
    idea: str = "",
    job_id: str = "",
) -> dict[str, Any] | None:
    """Build one authoritative typed input envelope from profile or idea."""

    profile_path_text = str(profile_path or "").strip()
    idea_text = str(idea or "").strip()
    if bool(profile_path_text) == bool(idea_text):
        if not profile_path_text and not idea_text:
            return None
        raise ValueError("free_mode_profile and free_mode_idea are mutually exclusive")

    if profile_path_text:
        path = Path(profile_path_text).expanduser().resolve()
        if not path.is_file():
            raise ValueError(f"free mode profile is not a file: {path}")
        raw_bytes = path.read_bytes()
        try:
            parsed = json.loads(raw_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("free mode profile must be a UTF-8 JSON object") from exc
        if not isinstance(parsed, Mapping):
            raise ValueError("free mode profile must be a JSON object")
        profile = normalize_profile(dict(parsed))
        review_intent = project_review_intent(profile)
        payload: dict[str, Any] = {
            "artifact_type": FREE_MODE_INTENT_INPUT_ARTIFACT_TYPE,
            "artifact_version": FREE_MODE_INTENT_INPUT_ARTIFACT_VERSION,
            "schema_version": "1",
            "job_id": job_id,
            "source_kind": "profile",
            "created_from": "profile_file",
            "source_path": str(path),
            "profile_content_sha256": _sha256_bytes(raw_bytes),
            "profile": profile,
            "review_intent": review_intent,
            "context_hash": _context_hash(
                source_kind="profile",
                profile=profile,
                raw_idea="",
                review_intent=review_intent,
            ),
        }
    else:
        raw_idea = str(idea or "")
        normalized_idea = raw_idea.strip()
        review_intent = project_review_intent(None)
        payload = {
            "artifact_type": FREE_MODE_INTENT_INPUT_ARTIFACT_TYPE,
            "artifact_version": FREE_MODE_INTENT_INPUT_ARTIFACT_VERSION,
            "schema_version": "1",
            "job_id": job_id,
            "source_kind": "idea",
            "created_from": "ad_hoc_idea",
            "raw_idea": raw_idea,
            "normalized_idea": normalized_idea,
            "idea_text_sha256": _sha256_bytes(normalized_idea.encode("utf-8")),
            "review_intent": review_intent,
            "context_hash": _context_hash(
                source_kind="idea",
                profile={},
                raw_idea=raw_idea,
                review_intent=review_intent,
            ),
        }

    artifact_hash = _sha256_bytes(canonical_json(payload).encode("utf-8"))
    return {
        "payload": payload,
        "artifact_id": FREE_MODE_INTENT_INPUT_ARTIFACT_ID,
        "artifact_hash": artifact_hash,
        "review_intent": dict(review_intent),
        "context_hash": str(payload["context_hash"]),
    }


def build_review_intent_projection_payload(envelope: Mapping[str, Any]) -> dict[str, Any]:
    payload = dict(envelope["payload"])
    return {
        "artifact_type": FREE_MODE_REVIEW_INTENT_PROJECTION_ARTIFACT_TYPE,
        "artifact_version": FREE_MODE_REVIEW_INTENT_PROJECTION_ARTIFACT_VERSION,
        "schema_version": "1",
        "projection_version": "1",
        "job_id": str(payload.get("job_id") or ""),
        "review_intent": dict(envelope["review_intent"]),
        "free_mode_input_artifact_id": str(envelope["artifact_id"]),
        "free_mode_input_artifact_hash": str(envelope["artifact_hash"]),
        "free_mode_context_hash": str(envelope["context_hash"]),
    }


def build_free_mode_writer_context(envelope: Mapping[str, Any]) -> dict[str, Any]:
    payload = dict(envelope["payload"])
    profile = dict(payload.get("profile") or {})
    return {
        "free_mode_input_artifact_id": str(envelope["artifact_id"]),
        "free_mode_input_artifact_hash": str(envelope["artifact_hash"]),
        "free_mode_context_hash": str(envelope["context_hash"]),
        "source_kind": str(payload.get("source_kind") or ""),
        "review_intent": dict(envelope["review_intent"]),
        "profile": profile,
        "generated_prompt": str(profile.get("generated_prompt") or ""),
        "writing_constraints": list(profile.get("writing_constraints") or []),
        "conversation_notes": list(profile.get("conversation_notes") or []),
        "raw_idea": str(payload.get("raw_idea") or ""),
    }


def canonical_payload_bytes(envelope: Mapping[str, Any]) -> bytes:
    return canonical_json(envelope["payload"]).encode("utf-8")


def verify_free_mode_intent_input(registry: Any, envelope: Mapping[str, Any]) -> Any:
    """Verify the Registry artifact matches the frozen typed input envelope."""

    artifact_id = str(envelope["artifact_id"])
    expected_hash = str(envelope["artifact_hash"])
    record = registry.get(artifact_id)
    if record is None:
        raise ValueError(f"free mode input artifact is missing: {artifact_id}")
    if record.status != "ready":
        raise ValueError(f"free mode input artifact is not ready: {artifact_id}")
    if record.content_hash != expected_hash:
        raise ValueError("free mode input artifact hash mismatch")
    path = Path(record.path)
    try:
        if file_sha256(path) != expected_hash:
            raise ValueError("free mode input artifact file hash mismatch")
        if path.read_bytes() != canonical_payload_bytes(envelope):
            raise ValueError("free mode input artifact bytes do not match the typed payload")
    except (OSError, TypeError, ValueError) as exc:
        raise ValueError(f"free mode input artifact is unreadable: {exc}") from exc
    if str(record.job_id or "") != str(envelope["payload"].get("job_id") or ""):
        raise ValueError("free mode input artifact job identity mismatch")
    return record


def verify_free_mode_review_intent_projection(
    registry: Any,
    envelope: Mapping[str, Any],
) -> Any:
    """Verify the projection artifact and its Free Mode input dependency."""

    artifact_id = FREE_MODE_REVIEW_INTENT_PROJECTION_ARTIFACT_ID
    record = registry.get(artifact_id)
    if record is None or record.status != "ready":
        raise ValueError(f"free mode review intent projection is missing: {artifact_id}")
    path = Path(record.path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"free mode review intent projection is unreadable: {exc}") from exc
    if not isinstance(raw, Mapping):
        raise ValueError("free mode review intent projection must be a JSON object")
    expected = build_review_intent_projection_payload(envelope)
    if dict(raw) != expected:
        raise ValueError("free mode review intent projection payload mismatch")
    if file_sha256(path) != record.content_hash:
        raise ValueError("free mode review intent projection file hash mismatch")
    input_dependency = next(
        (
            dependency
            for dependency in record.depends_on
            if dependency.artifact_id == str(envelope["artifact_id"])
        ),
        None,
    )
    if (
        input_dependency is None
        or input_dependency.content_hash != str(envelope["artifact_hash"])
    ):
        raise ValueError("free mode review intent projection input dependency mismatch")
    verify_free_mode_intent_input(registry, envelope)
    return record


__all__ = [
    "FREE_MODE_INTENT_INPUT_ARTIFACT_ID",
    "FREE_MODE_INTENT_INPUT_ARTIFACT_TYPE",
    "FREE_MODE_INTENT_INPUT_ARTIFACT_VERSION",
    "FREE_MODE_REVIEW_INTENT_PROJECTION_ARTIFACT_ID",
    "FREE_MODE_REVIEW_INTENT_PROJECTION_ARTIFACT_TYPE",
    "FREE_MODE_REVIEW_INTENT_PROJECTION_ARTIFACT_VERSION",
    "build_free_mode_intent_envelope",
    "build_free_mode_writer_context",
    "build_review_intent_projection_payload",
    "canonical_payload_bytes",
    "canonical_json",
    "project_review_intent",
    "verify_free_mode_intent_input",
    "verify_free_mode_review_intent_projection",
]
