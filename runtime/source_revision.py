"""Strict source-revision identities; parsing alone grants no execution authority.

Public admission and downstream proof verification are separate operations.
Legacy job specs do not acquire new serialized defaults from these helpers.
"""
from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

PROVIDER_SEMANTIC_CONTRACT_V2 = "source-closed-provider/v2"
LEGACY_PROVIDER_SEMANTIC_CONTRACT = "legacy/v1"
SOURCE_REVISION_PLAN_REF_VERSION = "source-revision-plan-ref/v1"


class SourceRevisionError(ValueError):
    """A proposed source revision is malformed or outside the supported mode."""


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SourceRevisionError(f"{field} must be a non-empty string")
    return value


def _digest(value: Any, field: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise SourceRevisionError(f"{field} must be a lowercase SHA-256 value")
    return value


def _absolute_path(value: Any, field: str) -> str:
    result = _text(value, field)
    path = Path(result)
    if not path.is_absolute() or ".." in path.parts:
        raise SourceRevisionError(f"{field} must be an absolute normalized path")
    return result


def _closed(payload: Mapping[str, Any], fields: set[str], label: str) -> None:
    if not isinstance(payload, Mapping) or set(payload) != fields:
        raise SourceRevisionError(f"{label} requires exactly: {', '.join(sorted(fields))}")


@dataclass(frozen=True)
class SourceRevisionPlanRefV1:
    schema_version: str
    path: str
    file_sha256: str
    content_hash: str

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> SourceRevisionPlanRefV1:
        _closed(payload, {"schema_version", "path", "file_sha256", "content_hash"}, "source_revision_plan")
        if payload["schema_version"] != SOURCE_REVISION_PLAN_REF_VERSION:
            raise SourceRevisionError("source_revision_plan schema_version is unsupported")
        return cls(
            schema_version=SOURCE_REVISION_PLAN_REF_VERSION,
            path=_absolute_path(payload["path"], "source_revision_plan.path"),
            file_sha256=_digest(payload["file_sha256"], "source_revision_plan.file_sha256"),
            content_hash=_digest(payload["content_hash"], "source_revision_plan.content_hash"),
        )

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


def provider_semantic_contract_version(metadata: Mapping[str, Any]) -> str:
    if "provider_semantic_contract_version" not in metadata:
        return LEGACY_PROVIDER_SEMANTIC_CONTRACT
    version = metadata["provider_semantic_contract_version"]
    if version != PROVIDER_SEMANTIC_CONTRACT_V2:
        raise SourceRevisionError("provider_semantic_contract_version is unsupported")
    return PROVIDER_SEMANTIC_CONTRACT_V2


def validate_source_revision_metadata(metadata: Mapping[str, Any]) -> None:
    version = provider_semantic_contract_version(metadata)
    if "source_revision_plan" in metadata:
        if version != PROVIDER_SEMANTIC_CONTRACT_V2:
            raise SourceRevisionError("source_revision_plan requires the explicit successor semantic contract")
        SourceRevisionPlanRefV1.from_mapping(metadata["source_revision_plan"])


def child_core_spec_payload(normalized_spec: Mapping[str, Any]) -> dict[str, Any]:
    """Project an already-normalized spec for the acyclic CoreHash boundary.

    The admission service must run the production normalizer first and check
    its idempotence after adding the reference. This helper neither normalizes
    paths/stages nor treats the projected value as an approved plan.
    """
    if not isinstance(normalized_spec, Mapping):
        raise SourceRevisionError("normalized child spec must be an object")
    core = deepcopy(dict(normalized_spec))
    metadata = core.get("metadata")
    if not isinstance(metadata, Mapping):
        raise SourceRevisionError("normalized child metadata must be an object")
    validate_source_revision_metadata(metadata)
    core["metadata"] = dict(metadata)
    core["metadata"].pop("source_revision_plan", None)
    return core


def child_core_spec_hash(normalized_spec: Mapping[str, Any]) -> str:
    from runtime.provider_runtime import hash_json

    return hash_json(child_core_spec_payload(normalized_spec))


@dataclass(frozen=True)
class PdfRevisionSourceV1:
    canonical_paper_key: str
    source_pdf: str
    pdf_sha256: str

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> PdfRevisionSourceV1:
        _closed(payload, {"canonical_paper_key", "source_pdf", "pdf_sha256"}, "revision source")
        return cls(
            canonical_paper_key=_text(payload["canonical_paper_key"], "canonical_paper_key"),
            source_pdf=_absolute_path(payload["source_pdf"], "source_pdf"),
            pdf_sha256=_digest(payload["pdf_sha256"], "pdf_sha256"),
        )


@dataclass(frozen=True)
class PdfReplacementDeltaV1:
    changed_paper_key: str
    unchanged_paper_keys: tuple[str, ...]
    old_sources: tuple[PdfRevisionSourceV1, ...]
    new_sources: tuple[PdfRevisionSourceV1, ...]


def classify_replacement_delta(
    old_sources: Sequence[Mapping[str, Any]], new_sources: Sequence[Mapping[str, Any]],
) -> PdfReplacementDeltaV1:
    """Validate first-mode declared byte identities, without reading files.

    Admission must independently hash the actual PDFs and verify their canonical
    identities. Source relocation alone cannot renew a completed run's scope.
    Added/removed/reordered/renamed sources remain separate unfinished modes.
    """
    if isinstance(old_sources, (str, bytes)) or isinstance(new_sources, (str, bytes)):
        raise SourceRevisionError("source maps must be arrays")
    old = tuple(PdfRevisionSourceV1.from_mapping(row) for row in old_sources)
    new = tuple(PdfRevisionSourceV1.from_mapping(row) for row in new_sources)
    old_keys = tuple(row.canonical_paper_key for row in old)
    new_keys = tuple(row.canonical_paper_key for row in new)
    if (not old or len(old_keys) != len(set(old_keys)) or len(new_keys) != len(set(new_keys))
            or old_keys != new_keys):
        raise SourceRevisionError("replace-existing-pdf/v1 requires identical unique ordered canonical keys")
    changed = tuple(left.canonical_paper_key for left, right in zip(old, new, strict=True)
                    if left.pdf_sha256 != right.pdf_sha256)
    if len(changed) != 1:
        raise SourceRevisionError("replace-existing-pdf/v1 requires exactly one changed PDF byte identity")
    return PdfReplacementDeltaV1(
        changed_paper_key=changed[0],
        unchanged_paper_keys=tuple(key for key in old_keys if key != changed[0]),
        old_sources=old, new_sources=new,
    )
