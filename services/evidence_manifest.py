"""Canonical identity manifest for per-paper preprocess evidence."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from typing import Any, Mapping

from services.job_workspace import utc_now_iso


EVIDENCE_MANIFEST_VERSION = "v1"
_REQUIRED_EVIDENCE = {
    "normalized_text": "markdown_path",
    "chunks": "chunks_path",
    "page_index": "page_index_path",
}


def _file_hash(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class EvidenceArtifactRefV1:
    artifact_type: str
    path: str
    content_hash: str

    def to_dict(self) -> dict[str, str]:
        return {
            "artifact_type": self.artifact_type,
            "path": self.path,
            "content_hash": self.content_hash,
        }


@dataclass(frozen=True)
class EvidenceManifestV1:
    job_id: str
    canonical_paper_key: str
    artifacts: tuple[EvidenceArtifactRefV1, ...]
    created_at: str
    artifact_type: str = "evidence_manifest"
    artifact_version: str = EVIDENCE_MANIFEST_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_type": self.artifact_type,
            "artifact_version": self.artifact_version,
            "job_id": self.job_id,
            "canonical_paper_key": self.canonical_paper_key,
            "artifacts": [item.to_dict() for item in self.artifacts],
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "EvidenceManifestV1":
        if payload.get("artifact_type") != "evidence_manifest":
            raise ValueError("not an evidence_manifest")
        if payload.get("artifact_version") != EVIDENCE_MANIFEST_VERSION:
            raise ValueError("unsupported evidence_manifest version")
        artifacts = tuple(
            EvidenceArtifactRefV1(
                artifact_type=str(item.get("artifact_type") or ""),
                path=str(item.get("path") or ""),
                content_hash=str(item.get("content_hash") or ""),
            )
            for item in payload.get("artifacts") or ()
        )
        result = cls(
            job_id=str(payload.get("job_id") or ""),
            canonical_paper_key=str(payload.get("canonical_paper_key") or ""),
            artifacts=artifacts,
            created_at=str(payload.get("created_at") or ""),
        )
        if {item.artifact_type for item in artifacts} != set(_REQUIRED_EVIDENCE):
            raise ValueError("evidence_manifest must contain normalized_text, chunks, and page_index")
        return result


def build_evidence_manifest_v1(
    *,
    job_id: str,
    canonical_paper_key: str,
    preprocess: Mapping[str, Any],
) -> EvidenceManifestV1:
    artifacts = []
    for artifact_type, field_name in _REQUIRED_EVIDENCE.items():
        path = os.path.abspath(os.fspath(preprocess.get(field_name) or ""))
        if not path or not os.path.isfile(path):
            raise FileNotFoundError(
                f"required {artifact_type} evidence is missing for {canonical_paper_key}: {path}"
            )
        artifacts.append(
            EvidenceArtifactRefV1(
                artifact_type=artifact_type,
                path=path,
                content_hash=_file_hash(path),
            )
        )
    return EvidenceManifestV1(
        job_id=job_id,
        canonical_paper_key=canonical_paper_key,
        artifacts=tuple(artifacts),
        created_at=utc_now_iso(),
    )


def verified_evidence_paths(manifest: EvidenceManifestV1) -> dict[str, str]:
    """Return verified paths, failing closed on a missing or changed artifact."""
    result: dict[str, str] = {}
    for item in manifest.artifacts:
        if not item.path or not os.path.isfile(item.path):
            raise FileNotFoundError(f"evidence artifact is missing: {item.path}")
        if _file_hash(item.path) != item.content_hash:
            raise ValueError(f"evidence artifact hash mismatch: {item.artifact_type}")
        result[item.artifact_type] = item.path
    return result
