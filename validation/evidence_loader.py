from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional


class ValidationSourceAuthorityError(RuntimeError):
    """Raised when formal validation cannot read authoritative evidence."""

    def __init__(self, message: str, *, path: str = "") -> None:
        self.path = str(path or "")
        super().__init__(message)

@dataclass(frozen=True)
class PreprocessEvidence:
    """预处理证据对象，包含所有可用的证据来源"""
    normalized_text: str = ""
    plain_text: str = ""
    page_index: List[Dict[str, Any]] = field(default_factory=list)
    chunks: List[Dict[str, Any]] = field(default_factory=list)
    structured_json: Dict[str, Any] = field(default_factory=dict)
    manifest: Dict[str, Any] = field(default_factory=dict)
    visual_artifacts: List[Dict[str, Any]] = field(default_factory=list)
    diagnostics: Dict[str, Any] = field(default_factory=dict)


class PreprocessEvidenceLoader:
    """预处理证据加载器，从磁盘加载各种预处理产物"""

    DEFAULT_MAX_ARTIFACT_BYTES = 128 * 1024 * 1024

    def __init__(self, *, max_artifact_bytes: int | None = None) -> None:
        limit = (
            self.DEFAULT_MAX_ARTIFACT_BYTES
            if max_artifact_bytes is None
            else int(max_artifact_bytes)
        )
        if limit <= 0:
            raise ValueError("max_artifact_bytes must be positive")
        self.max_artifact_bytes = limit
    
    def load_evidence(
        self,
        normalized_text_path: Optional[str] = None,
        plain_text_path: Optional[str] = None,
        page_index_path: Optional[str] = None,
        chunks_path: Optional[str] = None,
        structured_json_path: Optional[str] = None,
        manifest_path: Optional[str] = None,
        visual_artifacts_path: Optional[str] = None,
        diagnostics_path: Optional[str] = None,
        strict: bool = False,
    ) -> PreprocessEvidence:
        """加载预处理证据
        
        Args:
            plain_text_path: 纯文本文件路径
            page_index_path: 页面索引文件路径
            chunks_path: 分块文件路径
            structured_json_path: 结构化JSON文件路径
            manifest_path: 清单文件路径
            visual_artifacts_path: 视觉产物路径
            diagnostics_path: 诊断文件路径
            
        Returns:
            PreprocessEvidence: 加载的证据对象
        """
        if strict:
            return self._load_strict(
                normalized_text_path=normalized_text_path,
                plain_text_path=plain_text_path,
                page_index_path=page_index_path,
                chunks_path=chunks_path,
                structured_json_path=structured_json_path,
                manifest_path=manifest_path,
                visual_artifacts_path=visual_artifacts_path,
                diagnostics_path=diagnostics_path,
            )

        loaded_plain_text = self._load_text(plain_text_path)
        normalized_text = self._load_text(normalized_text_path) or loaded_plain_text
        plain_text = loaded_plain_text or normalized_text
        page_index = self._load_json(page_index_path, default=[])
        chunks = self._load_json(chunks_path, default=[])
        structured_json = self._load_json(structured_json_path, default={})
        manifest = self._load_json(manifest_path, default={})
        visual_artifacts = self._load_json(visual_artifacts_path, default=[])
        diagnostics = self._load_json(diagnostics_path, default={})
        
        return PreprocessEvidence(
            normalized_text=normalized_text,
            plain_text=plain_text,
            page_index=page_index,
            chunks=chunks,
            structured_json=structured_json,
            manifest=manifest,
            visual_artifacts=visual_artifacts,
            diagnostics=diagnostics,
        )

    def _load_strict(
        self,
        *,
        normalized_text_path: Optional[str],
        plain_text_path: Optional[str],
        page_index_path: Optional[str],
        chunks_path: Optional[str],
        structured_json_path: Optional[str],
        manifest_path: Optional[str],
        visual_artifacts_path: Optional[str],
        diagnostics_path: Optional[str],
    ) -> PreprocessEvidence:
        required = {
            "normalized text": normalized_text_path,
            "chunks": chunks_path,
            "page index": page_index_path,
            "manifest": manifest_path,
        }
        for label, path in required.items():
            if not path:
                raise ValidationSourceAuthorityError(
                    f"required validation evidence path is missing: {label}"
                )

        manifest_bytes = self._read_bytes(str(manifest_path), required=True, label="manifest")
        manifest = self._parse_json(
            manifest_bytes,
            path=str(manifest_path),
            label="manifest",
        )
        if not isinstance(manifest, dict):
            raise ValidationSourceAuthorityError(
                "validation evidence manifest must be a JSON object",
                path=str(manifest_path),
            )
        # The manifest is the root of trust for the other artifacts.  It is
        # intentionally not required to contain a self-hash.
        self._verify_hash(
            manifest,
            str(manifest_path),
            manifest_bytes,
            manifest_path=str(manifest_path),
            allow_missing=True,
        )

        def text_value(path: Optional[str], label: str, *, required_value: bool = False) -> str:
            if not path:
                if required_value:
                    raise ValidationSourceAuthorityError(
                        f"required validation evidence path is missing: {label}"
                    )
                return ""
            raw = self._read_bytes(path, required=required_value, label=label)
            self._verify_hash(
                manifest,
                path,
                raw,
                manifest_path=str(manifest_path),
            )
            try:
                value = raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ValidationSourceAuthorityError(
                    f"validation evidence is not valid UTF-8: {label}", path=path
                ) from exc
            return value

        def json_value(path: Optional[str], label: str, default: Any, *, required_value: bool = False) -> Any:
            if not path:
                if required_value:
                    raise ValidationSourceAuthorityError(
                        f"required validation evidence path is missing: {label}"
                    )
                return default
            raw = self._read_bytes(path, required=required_value, label=label)
            expectation = self._verify_hash(
                manifest,
                path,
                raw,
                manifest_path=str(manifest_path),
            )
            value = self._parse_json(raw, path=path, label=label)
            expected_schema = (
                expectation.get("schema_version")
                if isinstance(expectation, Mapping)
                else None
            )
            if expected_schema and isinstance(value, Mapping):
                actual_schema = value.get("schema_version")
                if actual_schema is not None and str(actual_schema) != str(expected_schema):
                    raise ValidationSourceAuthorityError(
                        f"validation evidence schema mismatch: {label}",
                        path=path,
                    )
            return value

        normalized_text = text_value(normalized_text_path, "normalized text", required_value=True)
        plain_text = text_value(plain_text_path, "plain text") or normalized_text
        page_index = json_value(page_index_path, "page index", [], required_value=True)
        chunks = json_value(chunks_path, "chunks", [], required_value=True)
        structured_json = json_value(structured_json_path, "structured JSON", {})
        visual_artifacts = json_value(visual_artifacts_path, "visual artifacts", [])
        diagnostics = json_value(diagnostics_path, "diagnostics", {})
        if not isinstance(page_index, list):
            raise ValidationSourceAuthorityError("page index evidence must be a JSON array", path=str(page_index_path))
        if not isinstance(chunks, list):
            raise ValidationSourceAuthorityError("chunks evidence must be a JSON array", path=str(chunks_path))
        if not isinstance(structured_json, dict):
            raise ValidationSourceAuthorityError("structured evidence must be a JSON object", path=str(structured_json_path or ""))
        if not isinstance(visual_artifacts, list):
            raise ValidationSourceAuthorityError("visual evidence must be a JSON array", path=str(visual_artifacts_path or ""))
        if not isinstance(diagnostics, dict):
            raise ValidationSourceAuthorityError("diagnostics evidence must be a JSON object", path=str(diagnostics_path or ""))
        return PreprocessEvidence(
            normalized_text=normalized_text,
            plain_text=plain_text,
            page_index=page_index,
            chunks=chunks,
            structured_json=structured_json,
            manifest=manifest,
            visual_artifacts=visual_artifacts,
            diagnostics=diagnostics,
        )

    def _read_bytes(
        self,
        path: str,
        *,
        required: bool,
        label: str,
    ) -> bytes:
        if not path:
            if required:
                raise ValidationSourceAuthorityError(
                    f"required validation evidence path is missing: {label}"
                )
            return b""
        try:
            if os.path.islink(path):
                raise ValidationSourceAuthorityError(
                    f"validation evidence path is a symlink: {label}",
                    path=path,
                )
            size_before = os.path.getsize(path)
            if size_before > self.max_artifact_bytes:
                raise ValidationSourceAuthorityError(
                    f"validation evidence exceeds the bounded read size for {label}",
                    path=path,
                )
            with open(path, "rb") as handle:
                raw = handle.read(self.max_artifact_bytes + 1)
                if len(raw) > self.max_artifact_bytes:
                    raise ValidationSourceAuthorityError(
                        f"validation evidence exceeds the bounded read size for {label}",
                        path=path,
                    )
                size_after = os.fstat(handle.fileno()).st_size
            if size_before != size_after or size_after != len(raw):
                raise ValidationSourceAuthorityError(
                    f"validation evidence changed while being read: {label}",
                    path=path,
                )
            return raw
        except ValidationSourceAuthorityError:
            raise
        except (OSError, ValueError) as exc:
            raise ValidationSourceAuthorityError(
                f"validation evidence cannot be read: {label}", path=path
            ) from exc

    @staticmethod
    def _parse_json(raw: bytes, *, path: str, label: str) -> Any:
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError) as exc:
            raise ValidationSourceAuthorityError(
                f"validation evidence JSON is invalid: {label}", path=path
            ) from exc

    @staticmethod
    def _verify_hash(
        manifest: Mapping[str, Any],
        path: str,
        raw: bytes,
        *,
        manifest_path: str = "",
        allow_missing: bool = False,
    ) -> Mapping[str, Any] | None:
        """Verify one exact manifest path and return its expectation.

        Relative manifest paths are resolved relative to the manifest itself.
        Matching by basename alone is deliberately forbidden: two artifacts
        with the same name in different directories must not share a hash
        entry.
        """

        target = Path(path).expanduser().resolve()
        root = Path(manifest_path).expanduser().resolve().parent if manifest_path else target.parent

        def canonical(candidate: Any) -> Path | None:
            value = str(candidate or "").strip()
            if not value:
                return None
            candidate_path = Path(value).expanduser()
            if not candidate_path.is_absolute():
                candidate_path = root / candidate_path
            return candidate_path.resolve()

        matches: list[Mapping[str, Any]] = []
        hashes = manifest.get("artifact_hashes")
        if isinstance(hashes, Mapping):
            for key, value in hashes.items():
                if canonical(key) == target:
                    matches.append(value if isinstance(value, Mapping) else {"sha256": value})
        if not matches:
            # EvidenceManifestV1 uses typed artifact entries rather than the
            # preprocess cache's artifact_hashes map.
            for entry in manifest.get("artifacts", ()) or ():
                if not isinstance(entry, Mapping):
                    continue
                entry_path = canonical(entry.get("path") or entry.get("relative_path"))
                if entry_path == target:
                    matches.append(
                        {
                            **dict(entry),
                            "sha256": entry.get("sha256") or entry.get("content_hash"),
                        }
                    )
        if len(matches) > 1:
            raise ValidationSourceAuthorityError(
                f"validation evidence identity is ambiguous: {target.name}",
                path=path,
            )
        if not matches:
            if allow_missing:
                return None
            raise ValidationSourceAuthorityError(
                f"validation evidence has no exact hash identity: {target.name}",
                path=path,
            )
        expectation = dict(matches[0])
        expected_text = str(
            expectation.get("sha256")
            or expectation.get("content_hash")
            or ""
        ).strip()
        if (
            len(expected_text) != 64
            or expected_text != expected_text.lower()
            or any(char not in "0123456789abcdef" for char in expected_text)
        ):
            raise ValidationSourceAuthorityError(
                f"validation evidence expected SHA-256 is invalid: {target.name}",
                path=path,
            )
        expected_size = expectation.get("size")
        if expected_size is not None:
            if isinstance(expected_size, bool):
                raise ValidationSourceAuthorityError(
                    f"validation evidence expected size is invalid: {target.name}",
                    path=path,
                )
            try:
                if int(expected_size) != len(raw):
                    raise ValidationSourceAuthorityError(
                        f"validation evidence size mismatch: {target.name}",
                        path=path,
                    )
            except (TypeError, ValueError) as exc:
                raise ValidationSourceAuthorityError(
                    f"validation evidence expected size is invalid: {target.name}",
                    path=path,
                ) from exc
        if hashlib.sha256(raw).hexdigest() != expected_text:
            raise ValidationSourceAuthorityError(
                f"validation evidence hash mismatch: {target.name}", path=path
            )
        return expectation
    
    def _load_text(self, path: Optional[str]) -> str:
        """加载文本文件"""
        if not path or not os.path.exists(path):
            return ""
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return f.read()
        except Exception:
            return ""
    
    def _load_json(self, path: Optional[str], default: Any) -> Any:
        """加载JSON文件"""
        if not path or not os.path.exists(path):
            return default
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return default


def build_evidence_context_from_preprocess(
    evidence: PreprocessEvidence,
    paper_artifact: Dict[str, Any],
) -> Dict[str, Any]:
    """从预处理证据构建证据上下文
    
    Args:
        evidence: 预处理证据对象
        paper_artifact: 论文产物
        
    Returns:
        Dict[str, Any]: 证据上下文
    """
    return {
        "normalized_text": evidence.normalized_text,
        "plain_text": evidence.plain_text,
        "page_index": evidence.page_index,
        "chunks": evidence.chunks,
        "structured_json": evidence.structured_json,
        "manifest": evidence.manifest,
        "visual_artifacts": evidence.visual_artifacts,
        "diagnostics": evidence.diagnostics,
        "paper_artifact": paper_artifact,
    }
