"""Immutable, source-bound manifests for the authoritative F1 corpus.

An F1 acceptance run is meaningful only when every selected PDF is tied to a
single owner-supplied manifest.  This module keeps that contract separate from
the mutable runtime workspace: the manifest describes exactly fifteen unique
PDF bytes, and callers may verify both its self-hash and the source files it
names.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
from typing import Any, Iterable, Mapping


class F1CorpusManifestError(ValueError):
    """Raised when an F1 corpus manifest is incomplete, unsafe, or stale."""


_MANIFEST_FIELDS = frozenset(
    {
        "artifact_type",
        "artifact_version",
        "schema_version",
        "corpus_id",
        "source_root",
        "sources",
        "content_sha256",
    }
)
_SOURCE_FIELDS = frozenset({"source_id", "relative_path", "sha256", "size_bytes"})
_CORPUS_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_SOURCE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_MAX_MANIFEST_BYTES = 2 * 1024 * 1024


def _reject_unknown(payload: Mapping[str, Any], allowed: frozenset[str], label: str) -> None:
    unknown = sorted(str(key) for key in payload if str(key) not in allowed)
    if unknown:
        raise F1CorpusManifestError(f"{label} contains unknown fields: {', '.join(unknown)}")


def _valid_sha256(value: Any) -> bool:
    text = str(value or "").strip()
    return bool(_SHA256_RE.fullmatch(text))


def _absolute_unresolved(path: str | Path) -> Path:
    return Path(os.path.abspath(os.path.expanduser(os.fspath(path))))


def _assert_no_reparse_components(path: str | Path, *, allow_missing_final: bool = False) -> Path:
    """Reject symlink/reparse ancestors before a corpus path is opened."""

    target = _absolute_unresolved(path)
    current = Path(target.anchor) if target.anchor else Path.cwd()
    parts = target.parts[1:] if target.anchor else target.parts
    for index, part in enumerate(parts):
        current = current / part
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            if allow_missing_final and index == len(parts) - 1:
                break
            raise F1CorpusManifestError(f"F1 corpus path component is missing: {current}") from None
        except OSError as exc:
            raise F1CorpusManifestError(f"F1 corpus path component is unreadable: {current}") from exc
        attributes = int(getattr(info, "st_file_attributes", 0) or 0)
        reparse_point = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0) or 0)
        if os.path.islink(current) or (reparse_point and attributes & reparse_point):
            raise F1CorpusManifestError(f"F1 corpus path contains a symlink or reparse point: {current}")
    return target


def _resolve_path(value: Any, *, field_name: str, origin_dir: str | Path | None) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise F1CorpusManifestError(f"{field_name} must be a non-empty JSON string")
    path = Path(value.strip()).expanduser()
    if not path.is_absolute() and origin_dir is not None:
        path = Path(origin_dir).expanduser() / path
    return _absolute_unresolved(path)


def _safe_relative_pdf_path(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise F1CorpusManifestError("F1 source relative_path must be a non-empty JSON string")
    text = value.strip()
    if "\\" in text or "\x00" in text:
        raise F1CorpusManifestError("F1 source relative_path must use safe POSIX separators")
    path = PurePosixPath(text)
    if path.is_absolute() or not path.parts:
        raise F1CorpusManifestError("F1 source relative_path must be relative")
    for part in path.parts:
        if part in {"", ".", ".."} or ":" in part:
            raise F1CorpusManifestError("F1 source relative_path contains an unsafe component")
    if path.suffix.casefold() != ".pdf":
        raise F1CorpusManifestError("F1 source relative_path must name a PDF")
    return path.as_posix()


def _canonical_bytes(
    *,
    corpus_id: str,
    sources: Iterable["F1CorpusSourceRecordV1"],
) -> bytes:
    """Return stable corpus identity bytes, excluding environment-local root paths."""

    payload = {
        "artifact_type": "f1_corpus_manifest",
        "artifact_version": "v1",
        "schema_version": "f1-corpus-manifest-v1",
        "corpus_id": corpus_id,
        "sources": [
            source.to_dict()
            for source in sorted(sources, key=lambda item: item.source_id.casefold())
        ],
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


@dataclass(frozen=True)
class F1CorpusSourceRecordV1:
    """One immutable source byte identity in an F1 manifest."""

    source_id: str
    relative_path: str
    sha256: str
    size_bytes: int

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "F1CorpusSourceRecordV1":
        if not isinstance(payload, Mapping):
            raise F1CorpusManifestError("F1 corpus source record must be a JSON object")
        _reject_unknown(payload, _SOURCE_FIELDS, "F1 corpus source record")
        source_id = str(payload.get("source_id") or "").strip()
        if not _SOURCE_ID_RE.fullmatch(source_id):
            raise F1CorpusManifestError("F1 corpus source_id is unsafe")
        sha256 = str(payload.get("sha256") or "").strip()
        if not _valid_sha256(sha256):
            raise F1CorpusManifestError("F1 corpus source sha256 must be lowercase SHA-256")
        size_bytes = payload.get("size_bytes")
        if isinstance(size_bytes, bool) or not isinstance(size_bytes, int) or size_bytes <= 0:
            raise F1CorpusManifestError("F1 corpus source size_bytes must be a positive integer")
        return cls(
            source_id=source_id,
            relative_path=_safe_relative_pdf_path(payload.get("relative_path")),
            sha256=sha256,
            size_bytes=size_bytes,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "relative_path": self.relative_path,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
        }


@dataclass(frozen=True)
class F1CorpusManifestV1:
    """A verified, exactly fifteen-paper F1 corpus declaration.

    ``content_sha256`` hashes the canonical corpus identity rather than the
    ``source_root``.  A runtime binding separately hashes the manifest file,
    allowing the same corpus declaration to move between approved workspaces
    without turning an environment path into paper identity.
    """

    corpus_id: str
    source_root: str
    sources: tuple[F1CorpusSourceRecordV1, ...]
    content_sha256: str
    manifest_path: str = ""
    manifest_sha256: str = ""

    @staticmethod
    def content_hash_for(
        corpus_id: str,
        sources: Iterable[F1CorpusSourceRecordV1],
    ) -> str:
        """Calculate the required ``content_sha256`` for a manifest payload."""

        return hashlib.sha256(
            _canonical_bytes(corpus_id=corpus_id, sources=sources)
        ).hexdigest()

    @classmethod
    def from_mapping(
        cls,
        payload: Mapping[str, Any],
        *,
        origin_dir: str | Path | None = None,
        manifest_path: str | Path | None = None,
        manifest_sha256: str = "",
        verify_source_files: bool = False,
    ) -> "F1CorpusManifestV1":
        if not isinstance(payload, Mapping):
            raise F1CorpusManifestError("F1 corpus manifest must be a JSON object")
        _reject_unknown(payload, _MANIFEST_FIELDS, "F1 corpus manifest")
        if (
            payload.get("artifact_type") != "f1_corpus_manifest"
            or payload.get("artifact_version") != "v1"
            or payload.get("schema_version") != "f1-corpus-manifest-v1"
        ):
            raise F1CorpusManifestError("F1 corpus manifest type or schema is invalid")
        corpus_id = str(payload.get("corpus_id") or "").strip()
        if not _CORPUS_ID_RE.fullmatch(corpus_id):
            raise F1CorpusManifestError("F1 corpus manifest corpus_id is unsafe")
        raw_sources = payload.get("sources")
        if not isinstance(raw_sources, (list, tuple)):
            raise F1CorpusManifestError("F1 corpus manifest sources must be a JSON array")
        sources = tuple(F1CorpusSourceRecordV1.from_mapping(item) for item in raw_sources)
        if len(sources) != 15:
            raise F1CorpusManifestError("F1 corpus manifest must contain exactly fifteen source records")
        source_ids = [item.source_id.casefold() for item in sources]
        hashes = [item.sha256 for item in sources]
        relative_paths = [item.relative_path.casefold() for item in sources]
        if len(source_ids) != len(set(source_ids)):
            raise F1CorpusManifestError("F1 corpus manifest source_ids must be unique")
        if len(hashes) != len(set(hashes)):
            raise F1CorpusManifestError("F1 corpus manifest source hashes must be unique")
        if len(relative_paths) != len(set(relative_paths)):
            raise F1CorpusManifestError("F1 corpus manifest relative paths must be unique")
        content_sha256 = str(payload.get("content_sha256") or "").strip()
        if not _valid_sha256(content_sha256):
            raise F1CorpusManifestError("F1 corpus manifest content_sha256 must be lowercase SHA-256")
        expected_content_hash = cls.content_hash_for(corpus_id, sources)
        if content_sha256 != expected_content_hash:
            raise F1CorpusManifestError("F1 corpus manifest content_sha256 does not match its source records")
        root = _resolve_path(
            payload.get("source_root"),
            field_name="F1 corpus manifest source_root",
            origin_dir=origin_dir,
        )
        manifest_target = ""
        if manifest_path is not None:
            manifest_target = str(_absolute_unresolved(manifest_path))
        if manifest_sha256 and not _valid_sha256(manifest_sha256):
            raise F1CorpusManifestError("F1 corpus manifest file hash must be lowercase SHA-256")
        result = cls(
            corpus_id=corpus_id,
            source_root=str(root),
            sources=sources,
            content_sha256=content_sha256,
            manifest_path=manifest_target,
            manifest_sha256=manifest_sha256,
        )
        if verify_source_files:
            result.verify_source_files()
        return result

    @classmethod
    def from_file(
        cls,
        path: str | Path,
        *,
        verify_source_files: bool = False,
        max_manifest_bytes: int = _MAX_MANIFEST_BYTES,
    ) -> "F1CorpusManifestV1":
        target = _assert_no_reparse_components(path)
        try:
            size = target.stat().st_size
            if size > max_manifest_bytes:
                raise F1CorpusManifestError("F1 corpus manifest exceeds the bounded read size")
            with target.open("rb") as handle:
                raw = handle.read(max_manifest_bytes + 1)
            if len(raw) > max_manifest_bytes or len(raw) != size:
                raise F1CorpusManifestError("F1 corpus manifest changed or exceeds the bounded read size")
            payload = json.loads(raw.decode("utf-8"))
        except F1CorpusManifestError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise F1CorpusManifestError("F1 corpus manifest is unreadable") from exc
        return cls.from_mapping(
            payload,
            origin_dir=target.parent,
            manifest_path=target,
            manifest_sha256=hashlib.sha256(raw).hexdigest(),
            verify_source_files=verify_source_files,
        )

    @property
    def source_ids(self) -> tuple[str, ...]:
        return tuple(item.source_id for item in self.sources)

    def source_by_id(self, source_id: str) -> F1CorpusSourceRecordV1:
        normalized = str(source_id or "").strip().casefold()
        for source in self.sources:
            if source.source_id.casefold() == normalized:
                return source
        raise F1CorpusManifestError(f"F1 corpus source_id is not declared: {source_id}")

    def validate_selection(self, source_ids: Iterable[str], *, gate: str) -> tuple[F1CorpusSourceRecordV1, ...]:
        selected_ids = tuple(str(item or "").strip() for item in source_ids)
        if not selected_ids or any(not _SOURCE_ID_RE.fullmatch(item) for item in selected_ids):
            raise F1CorpusManifestError("F1 corpus selection must contain safe source IDs")
        if len({item.casefold() for item in selected_ids}) != len(selected_ids):
            raise F1CorpusManifestError("F1 corpus selection source_ids must be unique")
        normalized_gate = str(gate or "").strip().upper()
        expected_count = {"C": 1, "D": 3, "Q": 15}.get(normalized_gate)
        if expected_count is None:
            raise F1CorpusManifestError("F1 corpus selection is only defined for Gates C, D, and Q")
        if len(selected_ids) != expected_count:
            raise F1CorpusManifestError(
                f"Gate {normalized_gate} requires exactly {expected_count} F1 source IDs"
            )
        selected = tuple(self.source_by_id(item) for item in selected_ids)
        if normalized_gate == "Q" and {item.source_id.casefold() for item in selected} != {
            item.source_id.casefold() for item in self.sources
        }:
            raise F1CorpusManifestError("Gate Q must select every declared F1 source record")
        return selected

    def _source_path(self, source: F1CorpusSourceRecordV1) -> Path:
        root = _assert_no_reparse_components(self.source_root)
        if not root.is_dir():
            raise F1CorpusManifestError("F1 corpus source_root is not a directory")
        candidate = root.joinpath(*PurePosixPath(source.relative_path).parts)
        _assert_no_reparse_components(candidate)
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise F1CorpusManifestError("F1 corpus source escapes source_root") from exc
        return candidate

    @staticmethod
    def _verify_file(path: Path, source: F1CorpusSourceRecordV1) -> None:
        try:
            before = os.stat(path)
            if not stat.S_ISREG(before.st_mode):
                raise F1CorpusManifestError("F1 corpus source is not a regular file")
            if before.st_size != source.size_bytes:
                raise F1CorpusManifestError(
                    f"F1 corpus source size does not match manifest: {source.source_id}"
                )
            digest = hashlib.sha256()
            first = b""
            with path.open("rb") as handle:
                while True:
                    chunk = handle.read(1024 * 1024)
                    if not chunk:
                        break
                    if not first:
                        first = chunk[:8]
                    digest.update(chunk)
                after = os.fstat(handle.fileno())
        except F1CorpusManifestError:
            raise
        except OSError as exc:
            raise F1CorpusManifestError(
                f"F1 corpus source is unreadable: {source.source_id}"
            ) from exc
        if not first.startswith(b"%PDF-"):
            raise F1CorpusManifestError(f"F1 corpus source is not a PDF: {source.source_id}")
        if (
            before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
            or before.st_ino != after.st_ino
        ):
            raise F1CorpusManifestError(
                f"F1 corpus source changed while being verified: {source.source_id}"
            )
        if digest.hexdigest() != source.sha256:
            raise F1CorpusManifestError(
                f"F1 corpus source hash does not match manifest: {source.source_id}"
            )

    def verify_source_files(
        self,
        source_ids: Iterable[str] | None = None,
    ) -> tuple[Path, ...]:
        """Reopen selected files and prove their bytes match the manifest."""

        selected = (
            self.sources
            if source_ids is None
            else tuple(self.source_by_id(item) for item in source_ids)
        )
        paths: list[Path] = []
        for source in selected:
            path = self._source_path(source)
            self._verify_file(path, source)
            paths.append(path)
        return tuple(paths)

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_type": "f1_corpus_manifest",
            "artifact_version": "v1",
            "schema_version": "f1-corpus-manifest-v1",
            "corpus_id": self.corpus_id,
            "source_root": self.source_root,
            "sources": [source.to_dict() for source in self.sources],
            "content_sha256": self.content_sha256,
        }


__all__ = [
    "F1CorpusManifestError",
    "F1CorpusManifestV1",
    "F1CorpusSourceRecordV1",
]
