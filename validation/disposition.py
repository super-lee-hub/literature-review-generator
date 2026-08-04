"""Typed evidence for an intentionally skipped optional validation stage."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
from typing import Any, Mapping

from services.job_workspace import utc_now_iso


VALIDATION_DISPOSITION_ARTIFACT_TYPE = "validation_disposition"
VALIDATION_DISPOSITION_ARTIFACT_VERSION = "v1"


def _hash_payload(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class ValidationDispositionV1:
    """A fail-closed, hash-bound ``not_requested`` validation decision."""

    job_id: str
    stage_plan_hash: str
    spec_hash: str
    validation_enabled: bool
    validation_required: bool
    allow_unvalidated: bool
    actor: str
    reason: str
    review_draft_artifact_id: str
    review_draft_artifact_hash: str
    citation_manifest_artifact_id: str
    citation_manifest_artifact_hash: str
    review_docx_artifact_id: str
    review_docx_artifact_hash: str
    created_at: str
    artifact_type: str = VALIDATION_DISPOSITION_ARTIFACT_TYPE
    artifact_version: str = VALIDATION_DISPOSITION_ARTIFACT_VERSION
    status: str = "not_requested"
    disposition_hash: str = ""

    def canonical_payload(self) -> dict[str, Any]:
        payload = asdict(self)
        payload.pop("disposition_hash", None)
        return payload

    def computed_hash(self) -> str:
        return _hash_payload(self.canonical_payload())

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["disposition_hash"] = self.disposition_hash or self.computed_hash()
        return payload

    def validate(self) -> None:
        if self.artifact_type != VALIDATION_DISPOSITION_ARTIFACT_TYPE:
            raise ValueError("validation disposition artifact_type is invalid")
        if self.artifact_version != VALIDATION_DISPOSITION_ARTIFACT_VERSION:
            raise ValueError("validation disposition artifact_version is invalid")
        if self.status != "not_requested":
            raise ValueError("validation disposition status must be not_requested")
        if not self.job_id.strip() or not self.actor.strip() or not self.reason.strip():
            raise ValueError("validation disposition identity, actor, and reason are required")
        if self.validation_enabled or self.validation_required:
            raise ValueError("not_requested disposition cannot claim validation is enabled or required")
        if not self.allow_unvalidated:
            raise ValueError("not_requested disposition must explicitly allow unvalidated completion")
        for label, value in (
            ("stage_plan_hash", self.stage_plan_hash),
            ("spec_hash", self.spec_hash),
            ("review_draft_artifact_hash", self.review_draft_artifact_hash),
            ("citation_manifest_artifact_hash", self.citation_manifest_artifact_hash),
            ("review_docx_artifact_hash", self.review_docx_artifact_hash),
        ):
            if len(value) != 64 or any(char not in "0123456789abcdef" for char in value.lower()):
                raise ValueError(f"validation disposition {label} must be a SHA-256 hex digest")
        for label, value in (
            ("review_draft_artifact_id", self.review_draft_artifact_id),
            ("citation_manifest_artifact_id", self.citation_manifest_artifact_id),
            ("review_docx_artifact_id", self.review_docx_artifact_id),
        ):
            if not value.strip():
                raise ValueError(f"validation disposition {label} is required")
        expected = self.computed_hash()
        if self.disposition_hash and self.disposition_hash != expected:
            raise ValueError("validation disposition disposition_hash does not match its content")

    @classmethod
    def create(
        cls,
        *,
        job_id: str,
        stage_plan_hash: str,
        spec_hash: str,
        review_draft_artifact_id: str,
        review_draft_artifact_hash: str,
        citation_manifest_artifact_id: str,
        citation_manifest_artifact_hash: str,
        review_docx_artifact_id: str,
        review_docx_artifact_hash: str,
        actor: str,
        reason: str,
        created_at: str | None = None,
    ) -> "ValidationDispositionV1":
        value = cls(
            job_id=job_id,
            stage_plan_hash=stage_plan_hash,
            spec_hash=spec_hash,
            validation_enabled=False,
            validation_required=False,
            allow_unvalidated=True,
            actor=actor,
            reason=reason,
            review_draft_artifact_id=review_draft_artifact_id,
            review_draft_artifact_hash=review_draft_artifact_hash,
            citation_manifest_artifact_id=citation_manifest_artifact_id,
            citation_manifest_artifact_hash=citation_manifest_artifact_hash,
            review_docx_artifact_id=review_docx_artifact_id,
            review_docx_artifact_hash=review_docx_artifact_hash,
            created_at=created_at or utc_now_iso(),
        )
        value.validate()
        return value

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ValidationDispositionV1":
        required = (
            "artifact_type",
            "artifact_version",
            "job_id",
            "stage_plan_hash",
            "spec_hash",
            "validation_enabled",
            "validation_required",
            "allow_unvalidated",
            "actor",
            "reason",
            "review_draft_artifact_id",
            "review_draft_artifact_hash",
            "citation_manifest_artifact_id",
            "citation_manifest_artifact_hash",
            "review_docx_artifact_id",
            "review_docx_artifact_hash",
            "created_at",
            "status",
            "disposition_hash",
        )
        missing = [field for field in required if field not in payload]
        if missing:
            raise ValueError("validation disposition is missing: " + ", ".join(missing))
        value = cls(
            job_id=str(payload.get("job_id") or ""),
            stage_plan_hash=str(payload.get("stage_plan_hash") or ""),
            spec_hash=str(payload.get("spec_hash") or ""),
            validation_enabled=bool(payload.get("validation_enabled")),
            validation_required=bool(payload.get("validation_required")),
            allow_unvalidated=bool(payload.get("allow_unvalidated")),
            actor=str(payload.get("actor") or ""),
            reason=str(payload.get("reason") or ""),
            review_draft_artifact_id=str(payload.get("review_draft_artifact_id") or ""),
            review_draft_artifact_hash=str(payload.get("review_draft_artifact_hash") or ""),
            citation_manifest_artifact_id=str(payload.get("citation_manifest_artifact_id") or ""),
            citation_manifest_artifact_hash=str(payload.get("citation_manifest_artifact_hash") or ""),
            review_docx_artifact_id=str(payload.get("review_docx_artifact_id") or ""),
            review_docx_artifact_hash=str(payload.get("review_docx_artifact_hash") or ""),
            created_at=str(payload.get("created_at") or ""),
            artifact_type=str(payload.get("artifact_type") or ""),
            artifact_version=str(payload.get("artifact_version") or ""),
            status=str(payload.get("status") or ""),
            disposition_hash=str(payload.get("disposition_hash") or ""),
        )
        value.validate()
        return value


__all__ = [
    "VALIDATION_DISPOSITION_ARTIFACT_TYPE",
    "VALIDATION_DISPOSITION_ARTIFACT_VERSION",
    "ValidationDispositionV1",
]
