"""Structured PDF preprocessing with MinerU-first parsing and local fallbacks."""

from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import zipfile
from dataclasses import asdict, dataclass
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, cast
from urllib.parse import urljoin, urlparse

try:
    import pymupdf as fitz  # type: ignore
except ImportError:  # pragma: no cover - compatibility with older PyMuPDF releases.
    import fitz  # type: ignore
import requests  # type: ignore

from services.stage1_input_selector import Stage1InputSelection, select_stage1_input
from services.stage1_input_completeness import build_completeness_metrics, has_blocking_stage1_reason
from services.job_workspace import atomic_write_json
from services.durable_io import interprocess_file_lock
from preprocess.provider_circuit import ProviderCircuitBreaker, ProviderCircuitOpen

DEFAULT_MINERU_ALLOWED_URL_HOSTS = frozenset(
    {
        "mineru.oss-cn-shanghai.aliyuncs.com",
        "cdn-mineru.openxlab.org.cn",
    }
)
DEFAULT_MINERU_RESPONSE_MAX_BYTES = 64 * 1024 * 1024
DEFAULT_MINERU_ZIP_MAX_ENTRIES = 4096
DEFAULT_MINERU_ZIP_MAX_UNCOMPRESSED_BYTES = 256 * 1024 * 1024
DEFAULT_MINERU_ZIP_MAX_ENTRY_BYTES = 64 * 1024 * 1024
DEFAULT_MINERU_ZIP_MAX_COMPRESSION_RATIO = 200.0
DEFAULT_MINERU_JSON_MAX_BYTES = 32 * 1024 * 1024
DEFAULT_MINERU_TEXT_MAX_BYTES = 64 * 1024 * 1024
DEFAULT_MINERU_SOURCE_PDF_MAX_BYTES = 128 * 1024 * 1024
PREPROCESS_MANIFEST_SCHEMA_VERSION = "preprocess-manifest-v2"
PREPROCESS_ACTIVE_POINTER_SCHEMA_VERSION = "preprocess-active-generation-v1"
STAGE1_INPUT_SELECTOR_VERSION = "stage1-input-selector-v1"
PREPROCESS_IMPLEMENTATION_VERSION = "preprocess-service-20260907-v2"


class MineruArtifactError(RuntimeError):
    """Base class for malformed or resource-exhausting remote artifacts."""


class MineruArtifactLimitError(MineruArtifactError):
    """Raised when a remote response/archive exceeds a configured bound."""


class MineruArtifactFormatError(MineruArtifactError):
    """Raised when a remote artifact is not a valid supported archive."""


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


@dataclass
class PageDiagnostics:
    page_number: int
    text_length: int
    image_count: int
    scanned_candidate: bool
    used_ocr: bool
    low_quality: bool


@dataclass
class PreprocessResult:
    pdf_path: str
    cache_dir: str
    markdown_path: str
    plain_text_path: str
    page_index_path: str
    chunks_path: str
    diagnostics_path: str
    ocr_diagnostics_path: str
    ocr_artifact_path: str
    structured_json_path: str
    manifest_path: str
    stage1_input_path: str
    stage1_input_manifest_path: str
    stage1_quality_report_path: str
    markdown_text: str
    plain_text: str
    stage1_input_text: str
    page_index: List[Dict[str, Any]]
    page_diagnostics: List[PageDiagnostics]
    low_quality: bool
    scanned_like: bool
    used_ocr: bool
    extractor_used: str
    chunk_count: int
    local_rag_enabled: bool
    local_rag_built: bool
    local_rag_persist_dir: str
    layout_fidelity: str
    conversion_used: str
    mineru_attempted: bool
    mineru_succeeded: bool
    mineru_token_present: bool
    mineru_remote_requested: bool
    mineru_remote_enabled: bool
    mineru_base_url: str
    selected_text_source: str
    stage1_quality_level: str


class PreprocessManager:
    """Create stable preprocess artifacts before stage-one AI analysis."""

    def __init__(
        self,
        config: Optional[Dict[str, Any]] = None,
        logger: Any = None,
        *,
        mineru_circuit_breaker: ProviderCircuitBreaker | None = None,
    ):
        self.config = config or {}
        self.logger = logger
        preprocess_section = self.config.get("Preprocess", {}) if isinstance(self.config, dict) else {}
        paths_section = self.config.get("Paths", {}) if isinstance(self.config, dict) else {}

        self.enabled = _as_bool(preprocess_section.get("enabled", "true"), default=True)
        self.cache_root = preprocess_section.get("cache_dir") or os.path.join(
            paths_section.get("output_path", "./output"),
            "_preprocess_cache",
        )
        self.extractor_profile = str(preprocess_section.get("extractor_profile", "auto")).strip().lower()
        self.ocr_mode = str(preprocess_section.get("ocr_mode", "auto")).strip().lower()
        self.ocr_languages = str(preprocess_section.get("ocr_languages", "eng")).strip() or "eng"
        self.force_rebuild = _as_bool(preprocess_section.get("force_rebuild", "false"))
        self.enable_local_rag = _as_bool(preprocess_section.get("enable_local_rag", "false"))
        self.rag_backend = str(preprocess_section.get("rag_backend", "chroma")).strip().lower()
        self.local_rag_allow_model_download = _as_bool(
            preprocess_section.get("local_rag_allow_model_download", "false")
        )
        self.rag_persist_dir = os.path.join(self.cache_root, "_rag")

        self.parser_mode = str(preprocess_section.get("parser_mode", "local")).strip().lower() or "local"
        self.primary_parser = str(preprocess_section.get("primary_parser", "local")).strip().lower() or "local"
        self.fallback_parser = str(preprocess_section.get("fallback_parser", "local")).strip().lower() or "local"
        self.use_markdown_as_stage1_input = _as_bool(
            preprocess_section.get("use_markdown_as_stage1_input", "true"),
            default=True,
        )
        self.retain_structured_output = _as_bool(preprocess_section.get("retain_structured_output", "true"), default=True)
        self.retain_page_index = _as_bool(preprocess_section.get("retain_page_index", "true"), default=True)
        self.retain_diagnostics = _as_bool(preprocess_section.get("retain_diagnostics", "true"), default=True)
        self.force_docling_strategy = False
        self.source_pdf_max_bytes = max(
            1,
            _as_int(
                preprocess_section.get(
                    "source_pdf_max_bytes",
                    os.getenv("MINERU_SOURCE_PDF_MAX_BYTES", str(DEFAULT_MINERU_SOURCE_PDF_MAX_BYTES)),
                ),
                DEFAULT_MINERU_SOURCE_PDF_MAX_BYTES,
            ),
        )

        self.mineru_base_url = str(os.getenv("MINERU_BASE_URL", "https://mineru.net/api/v4")).strip().rstrip("/")
        self.mineru_api_token = str(os.getenv("MINERU_API_TOKEN", "")).strip()
        self.mineru_model_version = str(os.getenv("MINERU_MODEL_VERSION", "vlm")).strip() or "vlm"
        self.mineru_upload_endpoint = str(os.getenv("MINERU_UPLOAD_ENDPOINT", "/file-urls/batch")).strip() or "/file-urls/batch"
        templates_raw = os.getenv(
            "MINERU_POLL_ENDPOINT_TEMPLATES",
            "/extract-results/batch/{batch_id},/extract-results/{batch_id},/extract/task/{batch_id}",
        )
        self.mineru_poll_endpoint_templates = [
            item.strip()
            for item in str(templates_raw).split(",")
            if item.strip()
        ]
        self.mineru_poll_interval_seconds = _as_float(os.getenv("MINERU_POLL_INTERVAL_SECONDS", "3"), 3.0)
        self.mineru_poll_timeout_seconds = _as_float(os.getenv("MINERU_POLL_TIMEOUT_SECONDS", "900"), 900.0)
        self.mineru_request_max_retries = _as_int(os.getenv("MINERU_REQUEST_MAX_RETRIES", "2"), 2)
        self.mineru_retry_backoff_seconds = _as_float(os.getenv("MINERU_RETRY_BACKOFF_SECONDS", "1.5"), 1.5)
        self.mineru_response_max_bytes = max(
            1,
            _as_int(
                os.getenv("MINERU_RESPONSE_MAX_BYTES", str(DEFAULT_MINERU_RESPONSE_MAX_BYTES)),
                DEFAULT_MINERU_RESPONSE_MAX_BYTES,
            ),
        )
        self.mineru_zip_max_entries = max(
            1,
            _as_int(
                os.getenv("MINERU_ZIP_MAX_ENTRIES", str(DEFAULT_MINERU_ZIP_MAX_ENTRIES)),
                DEFAULT_MINERU_ZIP_MAX_ENTRIES,
            ),
        )
        self.mineru_zip_max_uncompressed_bytes = max(
            1,
            _as_int(
                os.getenv(
                    "MINERU_ZIP_MAX_UNCOMPRESSED_BYTES",
                    str(DEFAULT_MINERU_ZIP_MAX_UNCOMPRESSED_BYTES),
                ),
                DEFAULT_MINERU_ZIP_MAX_UNCOMPRESSED_BYTES,
            ),
        )
        self.mineru_zip_max_entry_bytes = max(
            1,
            _as_int(
                os.getenv("MINERU_ZIP_MAX_ENTRY_BYTES", str(DEFAULT_MINERU_ZIP_MAX_ENTRY_BYTES)),
                DEFAULT_MINERU_ZIP_MAX_ENTRY_BYTES,
            ),
        )
        self.mineru_zip_max_compression_ratio = max(
            1.0,
            _as_float(
                os.getenv(
                    "MINERU_ZIP_MAX_COMPRESSION_RATIO",
                    str(DEFAULT_MINERU_ZIP_MAX_COMPRESSION_RATIO),
                ),
                DEFAULT_MINERU_ZIP_MAX_COMPRESSION_RATIO,
            ),
        )
        self.mineru_json_max_bytes = max(
            1,
            _as_int(
                os.getenv("MINERU_JSON_MAX_BYTES", str(DEFAULT_MINERU_JSON_MAX_BYTES)),
                DEFAULT_MINERU_JSON_MAX_BYTES,
            ),
        )
        self.mineru_text_max_bytes = max(
            1,
            _as_int(
                os.getenv("MINERU_TEXT_MAX_BYTES", str(DEFAULT_MINERU_TEXT_MAX_BYTES)),
                DEFAULT_MINERU_TEXT_MAX_BYTES,
            ),
        )
        configured_allowed_hosts = {
            item.strip().lower()
            for item in str(os.getenv("MINERU_ALLOWED_URL_HOSTS", "")).split(",")
            if item.strip()
        }
        self.mineru_invalid_allowed_url_hosts = {
            item
            for item in configured_allowed_hosts
            if not self._is_safe_exact_mineru_host(item)
        }
        self.mineru_allowed_url_hosts = set(DEFAULT_MINERU_ALLOWED_URL_HOSTS)
        self.mineru_allowed_url_hosts.update(
            item
            for item in configured_allowed_hosts
            if item not in self.mineru_invalid_allowed_url_hosts
        )
        self.allow_local_parse_fallback = _as_bool(os.getenv("ALLOW_LOCAL_PARSE_FALLBACK", "true"), default=True)
        self.docling_timeout_seconds = _as_float(
            preprocess_section.get("docling_timeout_seconds", os.getenv("DOCLING_TIMEOUT_SECONDS", "300")),
            300.0,
        )
        self.ocr_timeout_seconds = _as_float(
            preprocess_section.get("ocr_timeout_seconds", os.getenv("OCR_TIMEOUT_SECONDS", "120")),
            120.0,
        )
        self.mineru_circuit_breaker = mineru_circuit_breaker or ProviderCircuitBreaker("mineru")
        self.processing_fingerprint = self._processing_fingerprint()
        self._last_mineru_upload_bytes = 0

    def preflight_mineru(self) -> None:
        """Validate local route configuration before creating a remote task."""
        self.mineru_circuit_breaker.ensure_closed()
        if not self.mineru_api_token:
            raise ValueError("MINERU_API_TOKEN is not configured")
        parsed = urlparse(self.mineru_base_url)
        if parsed.scheme.lower() != "https" or not parsed.hostname:
            raise ValueError("MINERU_BASE_URL must be an absolute HTTPS URL")
        self._validate_mineru_url(
            self.mineru_base_url,
            purpose="preflight URL",
            require_mineru_origin=True,
        )

    def prepare_pdf(
        self,
        pdf_path: str,
        *,
        pin_id: str = "",
    ) -> Optional[PreprocessResult]:
        """Build or reuse cached preprocess artifacts for a PDF."""

        if not pdf_path or not os.path.exists(pdf_path):
            return None
        if not self.enabled:
            return None

        source_identity = self._source_identity(pdf_path)
        cache_dir = os.path.join(self.cache_root, self._pdf_cache_key(pdf_path, source_identity))
        os.makedirs(cache_dir, exist_ok=True)
        with interprocess_file_lock(Path(cache_dir) / ".preprocess-cache"):
            return self._prepare_pdf_locked(
                pdf_path,
                source_identity=source_identity,
                cache_dir=cache_dir,
                pin_id=pin_id,
            )

    def _prepare_pdf_locked(
        self,
        pdf_path: str,
        *,
        source_identity: Dict[str, Any],
        cache_dir: str,
        pin_id: str = "",
    ) -> Optional[PreprocessResult]:
        try:
            return self._prepare_pdf_locked_impl(
                pdf_path,
                source_identity=source_identity,
                cache_dir=cache_dir,
                pin_id=pin_id,
            )
        except BaseException:
            # The cache-key lock makes it safe to clean all staging siblings:
            # no other prepare_pdf process can be building this key here.
            self._gc_generations(cache_dir, temp_ttl_seconds=0)
            raise

    def _prepare_pdf_locked_impl(
        self,
        pdf_path: str,
        *,
        source_identity: Dict[str, Any],
        cache_dir: str,
        pin_id: str = "",
    ) -> Optional[PreprocessResult]:
        """Build or reuse one source cache while holding its per-key lock."""

        active = self._active_generation(cache_dir)
        if pin_id and active is not None:
            self._pin_generation(
                cache_dir,
                Path(active[0]).name,
                pin_id=pin_id,
            )
        self._gc_generations(cache_dir)
        active = self._active_generation(cache_dir)
        if (not self.force_rebuild) and active is not None:
            generation_dir, active_paths = active
            required_paths = [
                active_paths["markdown_path"],
                active_paths["plain_text_path"],
                active_paths["page_index_path"],
                active_paths["chunks_path"],
                active_paths["diagnostics_path"],
                active_paths["ocr_diagnostics_path"],
                active_paths["ocr_artifact_path"],
                active_paths["structured_json_path"],
                active_paths["stage1_input_path"],
                active_paths["stage1_input_manifest_path"],
                active_paths["stage1_quality_report_path"],
            ]
            if self._cache_is_fresh(
                pdf_path,
                active_paths["manifest_path"],
                *required_paths,
            ):
                cached = self._load_cached_result(
                    pdf_path=pdf_path,
                    cache_dir=cache_dir,
                    generation_dir=generation_dir,
                    markdown_path=active_paths["markdown_path"],
                    plain_text_path=active_paths["plain_text_path"],
                    page_index_path=active_paths["page_index_path"],
                    chunks_path=active_paths["chunks_path"],
                    diagnostics_path=active_paths["diagnostics_path"],
                    ocr_diagnostics_path=active_paths["ocr_diagnostics_path"],
                    ocr_artifact_path=active_paths["ocr_artifact_path"],
                    structured_json_path=active_paths["structured_json_path"],
                    manifest_path=active_paths["manifest_path"],
                )
                if cached is not None:
                    return cached

        staging_dir = tempfile.mkdtemp(prefix=".generation.tmp-", dir=cache_dir)
        generation_id = f"generation-{uuid.uuid4().hex}"
        generation_dir = os.path.join(cache_dir, generation_id)
        artifact_paths = self._artifact_paths(staging_dir)
        published_artifact_paths = self._artifact_paths(generation_dir)
        extraction = self._extract_preferred_content(pdf_path)
        if not extraction:
            return None

        markdown_text = str(extraction.get("markdown_text", "") or "")
        plain_text = str(extraction.get("plain_text", "") or "")
        if not markdown_text:
            markdown_text = self._fallback_markdown_from_text(plain_text)
        if not plain_text:
            plain_text = self._plain_text_from_page_blocks(extraction.get("page_blocks", []))
        if not plain_text and markdown_text:
            plain_text = markdown_text

        page_index = extraction.get("page_index", [])
        page_diagnostics = extraction.get("page_diagnostics", [])
        page_blocks = extraction.get("page_blocks", [])
        if not page_index:
            page_index = self._build_page_index(page_blocks, page_diagnostics)

        stage1_selection = self._select_stage1_input(
            markdown_text=markdown_text,
            plain_text=plain_text,
            page_index=page_index,
        )
        chunks = self._build_chunks(stage1_selection.selected_text, page_index)
        stage1_manifest_payload, stage1_quality_report_payload = self._stage1_selection_payloads(
            selection=stage1_selection,
            artifact_paths=artifact_paths,
            chunk_count=len(chunks),
        )
        low_quality = any(item.low_quality for item in page_diagnostics)
        scanned_like = any(item.scanned_candidate for item in page_diagnostics)
        used_ocr = any(item.used_ocr for item in page_diagnostics) or bool(extraction.get("used_ocr"))
        local_rag_built = self._maybe_build_local_rag(
            collection_name=self._pdf_cache_key(pdf_path, source_identity),
            chunks=chunks,
            source_pdf_sha256=str(source_identity["sha256"]),
            processing_fingerprint=self.processing_fingerprint,
        )

        extractor_used = str(extraction.get("extractor_used", "fitz") or "fitz")
        layout_fidelity = str(extraction.get("layout_fidelity", "page_text") or "page_text")
        conversion_used = str(extraction.get("conversion_used", "native_pdf") or "native_pdf")
        mineru_attempted = bool(extraction.get("mineru_attempted"))
        mineru_succeeded = bool(extraction.get("mineru_succeeded"))
        mineru_token_present = bool(extraction.get("mineru_token_present"))
        mineru_remote_requested = bool(extraction.get("mineru_remote_requested"))
        mineru_remote_enabled = bool(extraction.get("mineru_remote_enabled"))
        structured_payload = extraction.get("structured_payload", {})
        ocr_page_numbers = [
            int(item.page_number)
            for item in page_diagnostics
            if bool(item.used_ocr)
        ]
        ocr_page_text_hashes = {
            str(item.page_number): hashlib.sha256(
                str(next(
                    (
                        page.get("text")
                        for page in page_index
                        if isinstance(page, Mapping)
                        and int(page.get("page_number") or 0) == int(item.page_number)
                    ),
                    "",
                ) or "").encode("utf-8")
            ).hexdigest()
            for item in page_diagnostics
            if bool(item.used_ocr)
        }

        diagnostics_payload = {
            "extractor_used": extractor_used,
            "layout_fidelity": layout_fidelity,
            "conversion_used": conversion_used,
            "low_quality": low_quality,
            "scanned_like": scanned_like,
            "used_ocr": used_ocr,
            "ocr_available": self._ocr_available(),
            "local_rag_enabled": self.enable_local_rag,
            "local_rag_allow_model_download": self.local_rag_allow_model_download,
            "local_rag_built": local_rag_built,
            "mineru_attempted": mineru_attempted,
            "mineru_succeeded": mineru_succeeded,
            "mineru_token_present": mineru_token_present,
            "mineru_remote_requested": mineru_remote_requested,
            "mineru_remote_enabled": mineru_remote_enabled,
            "mineru_base_url": self.mineru_base_url,
            "mineru_upload_bytes": int(extraction.get("mineru_upload_bytes") or self._last_mineru_upload_bytes),
            "page_diagnostics": [asdict(item) for item in page_diagnostics],
            "artifact_paths": {
                "normalized_md": artifact_paths["markdown_path"],
                "plain_text": artifact_paths["plain_text_path"],
                "page_index": artifact_paths["page_index_path"],
                "structured": artifact_paths["structured_json_path"],
                "chunks": artifact_paths["chunks_path"],
                "diagnostics": artifact_paths["diagnostics_path"],
                "ocr_diagnostics": artifact_paths["ocr_diagnostics_path"],
                "ocr_artifact": artifact_paths["ocr_artifact_path"],
                "prepare_manifest": artifact_paths["manifest_path"],
                "stage1_input": artifact_paths["stage1_input_path"],
                "stage1_input_manifest": artifact_paths["stage1_input_manifest_path"],
                "stage1_text_quality_report": artifact_paths["stage1_quality_report_path"],
            },
            "stage1_input": {
                "selected_text_source": stage1_selection.selected_source,
                "stage1_quality_level": stage1_selection.quality_level,
                "fallback_reason": stage1_selection.fallback_reason,
                "stage1_quality_reasons": stage1_selection.stage1_quality_reasons,
                "stage1_input_path": artifact_paths["stage1_input_path"],
                "stage1_input_manifest_path": artifact_paths["stage1_input_manifest_path"],
                "stage1_quality_report_path": artifact_paths["stage1_quality_report_path"],
            },
        }
        ocr_diagnostics_payload = {
            "artifact_type": "ocr_diagnostics",
            "artifact_version": "v1",
            "schema_version": "ocr-diagnostics-v1",
            "source_pdf_sha256": str(source_identity["sha256"]),
            "ocr_engine": "tesseract" if ocr_page_numbers else "none",
            "ocr_engine_version": "runtime-detected" if ocr_page_numbers else "not-used",
            "page_numbers": ocr_page_numbers,
            "ocr_page_count": len(ocr_page_numbers),
            "output_artifact_hashes": dict(ocr_page_text_hashes),
        }
        ocr_artifact_payload = {
            "artifact_type": "ocr_artifact",
            "artifact_version": "v1",
            "schema_version": "ocr-artifact-v1",
            "source_pdf_sha256": str(source_identity["sha256"]),
            "diagnostics_sha256": "",
            "page_numbers": ocr_page_numbers,
            "page_text_hashes": dict(ocr_page_text_hashes),
            "stage1_input_sha256": hashlib.sha256(
                str(stage1_selection.selected_text or "").encode("utf-8")
            ).hexdigest(),
        }
        manifest_payload = {
            "schema_version": PREPROCESS_MANIFEST_SCHEMA_VERSION,
            "implementation_version": PREPROCESS_IMPLEMENTATION_VERSION,
            "generation_id": os.path.basename(generation_dir),
            "pdf_path": pdf_path,
            "canonical_source_path": source_identity["canonical_path"],
            "source_pdf_sha256": source_identity["sha256"],
            "source_pdf_size": source_identity["size"],
            "file_size": source_identity["size"],
            "modified_time": source_identity["mtime"],
            "source_pdf_mtime_ns": source_identity["mtime_ns"],
            "source_pdf_file_id": source_identity["file_id"],
            "processing_fingerprint": self.processing_fingerprint,
            "extractor_used": extractor_used,
            "layout_fidelity": layout_fidelity,
            "conversion_used": conversion_used,
            "chunk_count": len(chunks),
            "low_quality": low_quality,
            "scanned_like": scanned_like,
            "used_ocr": used_ocr,
            "local_rag_enabled": self.enable_local_rag,
            "local_rag_allow_model_download": self.local_rag_allow_model_download,
            "local_rag_built": local_rag_built,
            "local_rag_persist_dir": self.rag_persist_dir,
            "mineru_attempted": mineru_attempted,
            "mineru_succeeded": mineru_succeeded,
            "mineru_token_present": mineru_token_present,
            "mineru_remote_requested": mineru_remote_requested,
            "mineru_remote_enabled": mineru_remote_enabled,
            "mineru_base_url": self.mineru_base_url,
            "mineru_upload_bytes": int(extraction.get("mineru_upload_bytes") or self._last_mineru_upload_bytes),
            "selected_text_source": stage1_selection.selected_source,
            "stage1_quality_level": stage1_selection.quality_level,
            "stage1_quality_reasons": stage1_selection.stage1_quality_reasons,
            "stage1_input_path": artifact_paths["stage1_input_path"],
            "stage1_input_manifest_path": artifact_paths["stage1_input_manifest_path"],
            "stage1_quality_report_path": artifact_paths["stage1_quality_report_path"],
            "artifacts": diagnostics_payload["artifact_paths"],
        }
        # Keep all metadata path references stable across the staging-directory
        # rename below.
        stage1_manifest_payload = self._rewrite_generation_paths(
            stage1_manifest_payload,
            staging_dir,
            generation_dir,
        )
        stage1_quality_report_payload = self._rewrite_generation_paths(
            stage1_quality_report_payload,
            staging_dir,
            generation_dir,
        )
        diagnostics_payload = self._rewrite_generation_paths(
            diagnostics_payload,
            staging_dir,
            generation_dir,
        )
        self._write_text_durable(artifact_paths["markdown_path"], markdown_text)
        self._write_text_durable(artifact_paths["plain_text_path"], plain_text)
        self._write_json_durable(artifact_paths["page_index_path"], page_index)
        self._write_json_durable(artifact_paths["chunks_path"], chunks)
        self._write_text_durable(artifact_paths["stage1_input_path"], stage1_selection.selected_text)
        self._write_json_durable(artifact_paths["stage1_input_manifest_path"], stage1_manifest_payload)
        self._write_json_durable(artifact_paths["stage1_quality_report_path"], stage1_quality_report_payload)
        self._write_json_durable(artifact_paths["diagnostics_path"], diagnostics_payload)
        self._write_json_durable(
            artifact_paths["ocr_diagnostics_path"],
            ocr_diagnostics_payload,
        )
        ocr_artifact_payload["diagnostics_sha256"] = self._file_sha256(
            artifact_paths["ocr_diagnostics_path"]
        )
        self._write_json_durable(
            artifact_paths["ocr_artifact_path"],
            ocr_artifact_payload,
        )
        self._write_json_durable(
            artifact_paths["structured_json_path"],
            {
                "pages": page_blocks,
                "page_index": page_index,
                "plain_text": plain_text,
                "markdown_text": markdown_text,
                "stage1_input_text": stage1_selection.selected_text,
                "source_payload": self._make_json_safe(structured_payload),
            },
        )
        manifest_payload["artifact_hashes"] = self._artifact_hashes(
            artifact_paths,
            exclude={"manifest_path"},
        )
        # Metadata is durable evidence and must never retain the staging path
        # which disappears when the generation is atomically published.
        manifest_payload = self._rewrite_generation_paths(
            manifest_payload,
            staging_dir,
            generation_dir,
        )
        self._write_json_durable(artifact_paths["manifest_path"], manifest_payload)
        os.replace(staging_dir, generation_dir)
        self._publish_active_generation(
            cache_dir,
            generation_dir,
            manifest_path=published_artifact_paths["manifest_path"],
        )
        if pin_id:
            self._pin_generation(cache_dir, generation_id, pin_id=pin_id)
        self._gc_generations(cache_dir, active_generation_id=generation_id)

        return PreprocessResult(
            pdf_path=pdf_path,
            cache_dir=cache_dir,
            markdown_path=published_artifact_paths["markdown_path"],
            plain_text_path=published_artifact_paths["plain_text_path"],
            page_index_path=published_artifact_paths["page_index_path"],
            chunks_path=published_artifact_paths["chunks_path"],
            diagnostics_path=published_artifact_paths["diagnostics_path"],
            ocr_diagnostics_path=published_artifact_paths["ocr_diagnostics_path"],
            ocr_artifact_path=published_artifact_paths["ocr_artifact_path"],
            structured_json_path=published_artifact_paths["structured_json_path"],
            manifest_path=published_artifact_paths["manifest_path"],
            stage1_input_path=published_artifact_paths["stage1_input_path"],
            stage1_input_manifest_path=published_artifact_paths["stage1_input_manifest_path"],
            stage1_quality_report_path=published_artifact_paths["stage1_quality_report_path"],
            markdown_text=markdown_text,
            plain_text=plain_text,
            stage1_input_text=stage1_selection.selected_text,
            page_index=page_index,
            page_diagnostics=page_diagnostics,
            low_quality=low_quality,
            scanned_like=scanned_like,
            used_ocr=used_ocr,
            extractor_used=extractor_used,
            chunk_count=len(chunks),
            local_rag_enabled=self.enable_local_rag,
            local_rag_built=local_rag_built,
            local_rag_persist_dir=self.rag_persist_dir,
            layout_fidelity=layout_fidelity,
            conversion_used=conversion_used,
            mineru_attempted=mineru_attempted,
            mineru_succeeded=mineru_succeeded,
            mineru_token_present=mineru_token_present,
            mineru_remote_requested=mineru_remote_requested,
            mineru_remote_enabled=mineru_remote_enabled,
            mineru_base_url=self.mineru_base_url,
            selected_text_source=stage1_selection.selected_source,
            stage1_quality_level=stage1_selection.quality_level,
        )

    def _artifact_paths(self, cache_dir: str) -> Dict[str, str]:
        return {
            "markdown_path": os.path.join(cache_dir, "normalized.md"),
            "plain_text_path": os.path.join(cache_dir, "plain_text.txt"),
            "page_index_path": os.path.join(cache_dir, "page_index.json"),
            "chunks_path": os.path.join(cache_dir, "chunks.json"),
            "diagnostics_path": os.path.join(cache_dir, "diagnostics.json"),
            "ocr_diagnostics_path": os.path.join(cache_dir, "ocr_diagnostics.json"),
            "ocr_artifact_path": os.path.join(cache_dir, "ocr_artifact.json"),
            "structured_json_path": os.path.join(cache_dir, "structured.json"),
            "manifest_path": os.path.join(cache_dir, "prepare_manifest.json"),
            "stage1_input_path": os.path.join(cache_dir, "stage1_input.md"),
            "stage1_input_manifest_path": os.path.join(cache_dir, "stage1_input_manifest.json"),
            "stage1_quality_report_path": os.path.join(cache_dir, "stage1_text_quality_report.json"),
        }

    @staticmethod
    def _rewrite_generation_paths(value: Any, staging_dir: str, generation_dir: str) -> Any:
        """Rewrite only exact staging prefixes inside durable metadata."""

        if isinstance(value, str):
            prefix = os.path.abspath(staging_dir)
            candidate = os.path.abspath(value) if os.path.isabs(value) else value
            if isinstance(candidate, str) and candidate.startswith(prefix):
                return os.path.abspath(generation_dir) + candidate[len(prefix):]
            return value
        if isinstance(value, dict):
            return {
                key: PreprocessManager._rewrite_generation_paths(
                    item, staging_dir, generation_dir
                )
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [
                PreprocessManager._rewrite_generation_paths(item, staging_dir, generation_dir)
                for item in value
            ]
        if isinstance(value, tuple):
            return tuple(
                PreprocessManager._rewrite_generation_paths(item, staging_dir, generation_dir)
                for item in value
            )
        return value

    def _gc_generations(
        self,
        cache_dir: str,
        *,
        active_generation_id: str | None = None,
        keep_generations: int = 1,
        temp_ttl_seconds: float = 24 * 60 * 60,
    ) -> dict[str, list[str]]:
        """Safely remove only stale, non-active cache generations."""

        root = Path(cache_dir).expanduser().resolve()
        active_id = str(active_generation_id or "").strip()
        if not active_id:
            try:
                pointer = json.loads((root / "active_generation.json").read_text(encoding="utf-8"))
                active_id = str(pointer.get("generation_id") or "").strip()
            except (OSError, UnicodeError, json.JSONDecodeError, AttributeError):
                active_id = ""
        now = time.time()
        removed_tmp: list[str] = []
        removed_generations: list[str] = []
        generation_dirs: list[tuple[float, Path]] = []
        for child in root.iterdir() if root.is_dir() else ():
            if child.is_symlink():
                continue
            if child.is_dir() and child.name.startswith(".generation.tmp-"):
                try:
                    if now - child.stat().st_mtime >= max(0.0, float(temp_ttl_seconds)):
                        shutil.rmtree(child)
                        removed_tmp.append(str(child))
                except OSError:
                    continue
            elif child.is_dir() and child.name.startswith("generation-"):
                try:
                    generation_dirs.append((child.stat().st_mtime, child))
                except OSError:
                    continue
        generation_dirs.sort(key=lambda item: item[0], reverse=True)
        keep_count = max(1, int(keep_generations))
        keep_names = {path.name for _mtime, path in generation_dirs[:keep_count]}
        keep_names.update(self._pinned_generation_ids(root))
        if active_id:
            keep_names.add(active_id)
        for _mtime, child in generation_dirs:
            if child.name in keep_names:
                continue
            try:
                shutil.rmtree(child)
                removed_generations.append(str(child))
            except OSError:
                continue
        return {
            "removed_tmp": removed_tmp,
            "removed_generations": removed_generations,
        }

    def _pin_generation(self, cache_dir: str, generation_id: str, *, pin_id: str) -> Path:
        """Persist a job-owned pin so cache GC cannot remove its authority."""

        generation_name = str(generation_id or "").strip()
        if not generation_name.startswith("generation-") or os.path.basename(generation_name) != generation_name:
            raise ValueError("preprocess generation pin requires a finalized generation")
        generation_dir = Path(cache_dir).expanduser().resolve() / generation_name
        manifest_path = generation_dir / "prepare_manifest.json"
        if not generation_dir.is_dir() or not manifest_path.is_file():
            raise FileNotFoundError(f"cannot pin missing preprocess generation: {generation_dir}")
        safe_pin = hashlib.sha256(str(pin_id).encode("utf-8")).hexdigest()
        pin_dir = Path(cache_dir).expanduser().resolve() / "generation_pins"
        pin_path = pin_dir / f"{safe_pin}-{generation_name}.json"
        atomic_write_json(
            str(pin_path),
            {
                "schema_version": "preprocess-generation-pin-v1",
                "pin_id": str(pin_id),
                "generation_id": generation_name,
                "manifest_path": str(manifest_path),
                "manifest_sha256": self._file_sha256(str(manifest_path)),
            },
        )
        return pin_path

    @staticmethod
    def _pinned_generation_ids(root: Path) -> set[str]:
        pin_dir = root / "generation_pins"
        pinned: set[str] = set()
        if not pin_dir.is_dir() or pin_dir.is_symlink():
            return pinned
        for pin_path in pin_dir.glob("*.json"):
            if pin_path.is_symlink():
                continue
            try:
                payload = json.loads(pin_path.read_text(encoding="utf-8"))
                generation_id = str(payload.get("generation_id") or "").strip()
                manifest_path = Path(str(payload.get("manifest_path") or "")).expanduser().resolve()
                expected_hash = str(payload.get("manifest_sha256") or "").strip().lower()
                generation_root = (root / generation_id).resolve()
                if (
                    payload.get("schema_version") == "preprocess-generation-pin-v1"
                    and generation_id.startswith("generation-")
                    and os.path.basename(generation_id) == generation_id
                    and manifest_path.parent == generation_root
                    and manifest_path.is_file()
                    and expected_hash
                    and PreprocessManager._file_sha256(str(manifest_path)) == expected_hash
                ):
                    pinned.add(generation_id)
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                continue
        return pinned

    @staticmethod
    def _write_text_durable(path: str, value: str) -> None:
        directory = os.path.dirname(os.path.abspath(path))
        os.makedirs(directory, exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(str(value))
            handle.flush()
            os.fsync(handle.fileno())

    @staticmethod
    def _write_json_durable(path: str, value: Any) -> None:
        directory = os.path.dirname(os.path.abspath(path))
        os.makedirs(directory, exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())

    @staticmethod
    def _file_sha256(path: str) -> str:
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _source_identity(self, pdf_path: str) -> Dict[str, Any]:
        canonical_path = os.path.realpath(os.path.abspath(pdf_path))
        with open(canonical_path, "rb") as handle:
            before = os.fstat(handle.fileno())
            digest = hashlib.sha256()
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
            after_handle = os.fstat(handle.fileno())
        after_path = os.stat(canonical_path)
        before_tuple = (
            int(before.st_dev),
            int(getattr(before, "st_ino", 0)),
            int(before.st_size),
            int(getattr(before, "st_mtime_ns", int(before.st_mtime * 1_000_000_000))),
        )
        after_tuple = (
            int(after_path.st_dev),
            int(getattr(after_path, "st_ino", 0)),
            int(after_path.st_size),
            int(getattr(after_path, "st_mtime_ns", int(after_path.st_mtime * 1_000_000_000))),
        )
        handle_tuple = (
            int(after_handle.st_dev),
            int(getattr(after_handle, "st_ino", 0)),
            int(after_handle.st_size),
            int(getattr(after_handle, "st_mtime_ns", int(after_handle.st_mtime * 1_000_000_000))),
        )
        if before_tuple != handle_tuple or before_tuple != after_tuple:
            raise RuntimeError(
                "source PDF changed while its content hash and stable file identity were being computed"
            )
        return {
            "canonical_path": canonical_path,
            "size": int(after_path.st_size),
            "mtime": float(after_path.st_mtime),
            "mtime_ns": int(after_path.st_mtime_ns),
            "file_id": f"{int(after_path.st_dev)}:{int(getattr(after_path, 'st_ino', 0))}",
            "sha256": digest.hexdigest(),
        }

    def _processing_fingerprint(self) -> str:
        payload = {
            "schema_version": PREPROCESS_MANIFEST_SCHEMA_VERSION,
            "implementation_version": PREPROCESS_IMPLEMENTATION_VERSION,
            "stage1_input_selector_version": STAGE1_INPUT_SELECTOR_VERSION,
            "preprocess": self._make_json_safe(self.config.get("Preprocess", {})),
            "stage1_input": self._make_json_safe(self.config.get("Stage1_Input", {})),
            "stage1_visual": self._make_json_safe(self.config.get("Stage1_Visual", {})),
            "extractor_profile": self.extractor_profile,
            "parser_mode": self.parser_mode,
            "primary_parser": self.primary_parser,
            "fallback_parser": self.fallback_parser,
            "ocr_mode": self.ocr_mode,
            "ocr_languages": self.ocr_languages,
            "mineru_model_version": self.mineru_model_version,
            "source_pdf_max_bytes": self.source_pdf_max_bytes,
            "local_rag_allow_model_download": self.local_rag_allow_model_download,
        }
        return hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    def _active_generation(self, cache_dir: str) -> Optional[tuple[str, Dict[str, str]]]:
        pointer_path = os.path.join(cache_dir, "active_generation.json")
        if not os.path.isfile(pointer_path) or os.path.islink(pointer_path):
            return None
        try:
            with open(pointer_path, "r", encoding="utf-8") as handle:
                pointer = json.load(handle)
            if not isinstance(pointer, dict):
                return None
            if pointer.get("schema_version") != PREPROCESS_ACTIVE_POINTER_SCHEMA_VERSION:
                return None
            generation_name = str(pointer.get("generation_id") or "").strip()
            if (
                not generation_name
                or os.path.basename(generation_name) != generation_name
                or not generation_name.startswith("generation-")
            ):
                return None
            generation_dir = os.path.join(cache_dir, generation_name)
            if os.path.realpath(generation_dir) != os.path.abspath(generation_dir):
                return None
            paths = self._artifact_paths(generation_dir)
            manifest_path = paths["manifest_path"]
            if str(pointer.get("manifest_sha256") or "") != self._file_sha256(manifest_path):
                return None
            return generation_dir, paths
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return None

    def _publish_active_generation(
        self,
        cache_dir: str,
        generation_dir: str,
        *,
        manifest_path: str,
    ) -> None:
        generation_name = os.path.basename(generation_dir)
        if not generation_name.startswith("generation-"):
            raise RuntimeError("preprocess active-generation pointer requires a finalized generation")
        pointer_path = os.path.join(cache_dir, "active_generation.json")
        if os.path.islink(pointer_path):
            raise RuntimeError("preprocess active-generation pointer is a symlink")
        atomic_write_json(
            pointer_path,
            {
                "schema_version": PREPROCESS_ACTIVE_POINTER_SCHEMA_VERSION,
                "generation_id": generation_name,
                "manifest_sha256": self._file_sha256(manifest_path),
            },
        )

    def _artifact_hashes(
        self,
        artifact_paths: Mapping[str, str],
        *,
        exclude: set[str] | None = None,
    ) -> Dict[str, Dict[str, Any]]:
        ignored = exclude or set()
        result: Dict[str, Dict[str, Any]] = {}
        for key, path in artifact_paths.items():
            if key in ignored:
                continue
            target = Path(path)
            result[target.name] = {
                "relative_path": target.name,
                "type": "json" if target.suffix.casefold() == ".json" else "text",
                "schema_version": PREPROCESS_MANIFEST_SCHEMA_VERSION,
                "size": int(os.path.getsize(path)),
                "sha256": self._file_sha256(path),
            }
        return result

    def _extract_preferred_content(self, pdf_path: str) -> Optional[Dict[str, Any]]:
        baseline_plain_text, baseline_page_diagnostics, baseline_page_blocks = self._extract_local_page_data(
            pdf_path,
            allow_ocr=False,
        )
        baseline_page_index = self._build_page_index(baseline_page_blocks, baseline_page_diagnostics)

        mineru_token_present = bool(self.mineru_api_token)
        mineru_attempted = False
        mineru_succeeded = False

        remote_requested = self.parser_mode in {"remote", "remote_first"} or (
            self.parser_mode == "hybrid" and self.primary_parser == "mineru_remote"
        )
        remote_enabled = remote_requested
        circuit_snapshot = self.mineru_circuit_breaker.snapshot
        if remote_requested and circuit_snapshot.open:
            remote_enabled = False
            self._log(
                f"MinerU disabled for this job because its circuit is open: {circuit_snapshot.reason}",
                level="warning",
            )
        if self.parser_mode == "hybrid" and remote_requested:
            remote_enabled = remote_enabled and self._should_try_remote_in_hybrid(
                baseline_plain_text=baseline_plain_text,
                baseline_page_diagnostics=baseline_page_diagnostics,
            )
            if not remote_enabled:
                baseline_text_length = len((baseline_plain_text or "").strip())
                total_pages = len(baseline_page_diagnostics)
                low_quality_pages = sum(1 for item in baseline_page_diagnostics if item.low_quality)
                scanned_candidate_pages = sum(1 for item in baseline_page_diagnostics if item.scanned_candidate)
                self._log(
                    "Hybrid preprocess kept the local parser because the baseline extraction looked healthy "
                    f"(text_length={baseline_text_length}, low_quality_pages={low_quality_pages}/{total_pages}, "
                    f"scanned_candidate_pages={scanned_candidate_pages}/{total_pages}).",
                    level="info",
                )

        if remote_enabled and mineru_token_present:
            mineru_attempted = True
            try:
                remote_result = self._extract_with_mineru_remote(
                    pdf_path=pdf_path,
                    baseline_page_diagnostics=baseline_page_diagnostics,
                    baseline_page_blocks=baseline_page_blocks,
                    baseline_page_index=baseline_page_index,
                )
                if remote_result and (remote_result.get("markdown_text") or remote_result.get("plain_text")):
                    remote_result["mineru_attempted"] = True
                    remote_result["mineru_succeeded"] = True
                    remote_result["mineru_token_present"] = mineru_token_present
                    remote_result["mineru_remote_requested"] = remote_requested
                    remote_result["mineru_remote_enabled"] = remote_enabled
                    return remote_result
            except MineruArtifactError:
                raise
            except Exception as exc:  # pragma: no cover - remote integration path.
                self._log(f"MinerU remote parsing failed, falling back to local parser: {exc}", level="warning")

        if remote_enabled and not mineru_token_present:
            self._log("MinerU remote parsing skipped because MINERU_API_TOKEN is not configured.", level="info")

        if not self.allow_local_parse_fallback and remote_requested and not mineru_succeeded:
            return {
                "markdown_text": "",
                "plain_text": "",
                "page_index": baseline_page_index,
                "page_diagnostics": baseline_page_diagnostics,
                "page_blocks": baseline_page_blocks,
                "extractor_used": "mineru_unavailable",
                "layout_fidelity": "none",
                "conversion_used": "native_pdf",
                "used_ocr": False,
                "structured_payload": {},
                "mineru_attempted": mineru_attempted,
                "mineru_succeeded": mineru_succeeded,
                "mineru_token_present": mineru_token_present,
                "mineru_remote_requested": remote_requested,
                "mineru_remote_enabled": remote_enabled,
            }

        local_result = self._extract_with_local_fallbacks(
            pdf_path=pdf_path,
            baseline_plain_text=baseline_plain_text,
            baseline_page_diagnostics=baseline_page_diagnostics,
            baseline_page_blocks=baseline_page_blocks,
            baseline_page_index=baseline_page_index,
        )
        if not local_result:
            return None

        local_result["mineru_attempted"] = mineru_attempted
        local_result["mineru_succeeded"] = mineru_succeeded
        local_result["mineru_token_present"] = mineru_token_present
        local_result["mineru_remote_requested"] = remote_requested
        local_result["mineru_remote_enabled"] = remote_enabled
        return local_result

    def _should_try_remote_in_hybrid(
        self,
        baseline_plain_text: str,
        baseline_page_diagnostics: List[PageDiagnostics],
    ) -> bool:
        stripped_length = len((baseline_plain_text or "").strip())
        if stripped_length < 1200:
            return True

        if not baseline_page_diagnostics:
            return False

        completeness_metrics = build_completeness_metrics(
            text=baseline_plain_text,
            page_count=len(baseline_page_diagnostics),
        )
        if has_blocking_stage1_reason(completeness_metrics.get("blocking_reasons")):
            self._log(
                "Hybrid preprocess will try MinerU because the local baseline looks incomplete "
                f"(text_length={stripped_length}, pages={len(baseline_page_diagnostics)}, "
                f"reasons={','.join(completeness_metrics.get('blocking_reasons') or [])}).",
                level="info",
            )
            return True

        total_pages = len(baseline_page_diagnostics)
        low_quality_pages = sum(1 for item in baseline_page_diagnostics if item.low_quality)
        scanned_candidate_pages = sum(1 for item in baseline_page_diagnostics if item.scanned_candidate)

        if scanned_candidate_pages / total_pages >= 0.25:
            return True
        if low_quality_pages / total_pages >= 0.5:
            return True
        return False

    def _extract_with_local_fallbacks(
        self,
        pdf_path: str,
        baseline_plain_text: str,
        baseline_page_diagnostics: List[PageDiagnostics],
        baseline_page_blocks: List[Dict[str, Any]],
        baseline_page_index: List[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        if self.force_docling_strategy:
            self._log(
                "Forced Docling strategy bypassed the existing local pipeline and called Docling directly.",
                level="info",
            )
            return self._extract_with_docling(
                pdf_path=pdf_path,
                baseline_page_diagnostics=baseline_page_diagnostics,
                baseline_page_blocks=baseline_page_blocks,
                baseline_page_index=baseline_page_index,
            )

        local_result = self._extract_with_existing_local_pipeline(pdf_path)
        if local_result and not self._should_try_docling_fallback(
            plain_text=str(local_result.get("plain_text", "") or ""),
            page_diagnostics=local_result.get("page_diagnostics", []),
        ):
            return local_result

        if local_result:
            self._log(
                "Trying Docling fallback because the local extraction looked incomplete or low-quality.",
                level="info",
            )

        docling_result = self._extract_with_docling(
            pdf_path=pdf_path,
            baseline_page_diagnostics=baseline_page_diagnostics,
            baseline_page_blocks=baseline_page_blocks,
            baseline_page_index=baseline_page_index,
        )
        if docling_result:
            return docling_result

        if local_result:
            return local_result

        return None

    def _extract_with_mineru_remote(
        self,
        pdf_path: str,
        baseline_page_diagnostics: List[PageDiagnostics],
        baseline_page_blocks: List[Dict[str, Any]],
        baseline_page_index: List[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        try:
            source_pdf_size = int(os.path.getsize(pdf_path))
        except OSError as exc:
            raise MineruArtifactError(f"cannot stat source PDF before upload: {exc}") from exc
        if source_pdf_size > self.source_pdf_max_bytes:
            raise MineruArtifactLimitError(
                "source PDF exceeds the configured upload byte limit "
                f"({source_pdf_size} > {self.source_pdf_max_bytes})"
            )
        self.preflight_mineru()
        upload_url = self._join_base_url(self.mineru_upload_endpoint)
        payload = {
            "files": [
                {
                    "name": os.path.basename(pdf_path),
                    "data_id": self._pdf_cache_key(pdf_path),
                }
            ],
            "model_version": self.mineru_model_version,
        }

        upload_response = self._request_json("post", upload_url, json=payload)
        if not upload_response:
            return None

        batch_id = str(self._find_first_value(upload_response, {"batch_id", "task_id", "id"}) or "").strip()
        upload_targets = self._normalize_upload_targets(
            self._find_first_value(upload_response, {"file_urls", "upload_urls", "urls"})
        )
        if not upload_targets:
            raise RuntimeError("MinerU upload response did not include presigned upload URLs.")

        self._last_mineru_upload_bytes = 0
        for target in upload_targets:
            self.mineru_circuit_breaker.ensure_closed()
            with open(pdf_path, "rb") as handle:
                response = requests.put(
                    self._validate_mineru_url(target, purpose="upload URL"),
                    data=handle,
                    timeout=120,
                    allow_redirects=False,
                )
            self._last_mineru_upload_bytes += source_pdf_size
            if response.status_code in {401, 403}:
                self.mineru_circuit_breaker.open(
                    reason="upload_authorization_rejected",
                    status_code=int(response.status_code),
                )
                raise ProviderCircuitOpen(
                    f"MinerU upload authorization rejected with HTTP {response.status_code}",
                    snapshot=self.mineru_circuit_breaker.snapshot,
                )
            response.raise_for_status()

        poll_payload = self._poll_mineru_result(batch_id=batch_id, seed_payload=upload_response)
        if not poll_payload:
            return None

        normalized = self._normalize_mineru_payload(
            payload=poll_payload,
            baseline_page_diagnostics=baseline_page_diagnostics,
            baseline_page_blocks=baseline_page_blocks,
            baseline_page_index=baseline_page_index,
        )
        if not normalized or (not normalized.get("markdown_text") and not normalized.get("plain_text")):
            return None

        normalized["extractor_used"] = "mineru"
        normalized["layout_fidelity"] = "layout_aware"
        normalized["conversion_used"] = "native_pdf"
        normalized["used_ocr"] = False
        normalized["mineru_upload_bytes"] = self._last_mineru_upload_bytes
        return normalized

    def _request_json(self, method: str, url: str, **kwargs: Any) -> Optional[Dict[str, Any]]:
        safe_url = self._validate_mineru_url(url, purpose="JSON request URL", require_mineru_origin=True)
        last_exception: Optional[Exception] = None
        for attempt in range(self.mineru_request_max_retries + 1):
            try:
                self.mineru_circuit_breaker.ensure_closed()
                response = requests.request(
                    method=method.upper(),
                    url=safe_url,
                    headers=self._mineru_headers(),
                    timeout=60,
                    allow_redirects=False,
                    **kwargs,
                )
                if response.status_code in {401, 403}:
                    self.mineru_circuit_breaker.open(
                        reason="authorization_rejected",
                        status_code=int(response.status_code),
                    )
                    raise ProviderCircuitOpen(
                        f"MinerU authorization rejected with HTTP {response.status_code}",
                        snapshot=self.mineru_circuit_breaker.snapshot,
                    )
                response.raise_for_status()
                content_length = getattr(response, "headers", {}).get("Content-Length")
                try:
                    advertised_bytes = int(content_length) if content_length else 0
                except (TypeError, ValueError):
                    advertised_bytes = 0
                if advertised_bytes > self.mineru_response_max_bytes:
                    raise MineruArtifactLimitError(
                        "MinerU JSON response exceeds the configured byte limit "
                        f"({advertised_bytes} > {self.mineru_response_max_bytes})"
                    )
                response_content = bytes(getattr(response, "content", b"") or b"")
                if len(response_content) > self.mineru_response_max_bytes:
                    raise MineruArtifactLimitError(
                        "MinerU JSON response exceeds the configured byte limit "
                        f"({len(response_content)} > {self.mineru_response_max_bytes})"
                    )
                return response.json()
            except ProviderCircuitOpen:
                raise
            except MineruArtifactError:
                raise
            except Exception as exc:  # pragma: no cover - transport path.
                last_exception = exc
                if attempt >= self.mineru_request_max_retries:
                    break
                time.sleep(self.mineru_retry_backoff_seconds * (attempt + 1))
        if last_exception:
            raise last_exception
        return None

    def _request_binary(self, url: str) -> bytes:
        safe_url = self._validate_mineru_url(url, purpose="binary artifact URL")
        last_exception: Optional[Exception] = None
        session = requests.Session()
        # MinerU result assets are served from a CDN. In some Windows/proxy
        # environments the HTTPS proxy breaks the CDN TLS handshake, while the
        # direct connection succeeds. Keep API JSON calls unchanged, but bypass
        # ambient proxies for binary artifact downloads.
        session.trust_env = False
        try:
            for attempt in range(self.mineru_request_max_retries + 1):
                try:
                    is_mineru_origin = self._is_mineru_origin_url(safe_url)
                    if is_mineru_origin:
                        self.mineru_circuit_breaker.ensure_closed()
                    headers = self._mineru_headers() if is_mineru_origin else {}
                    response = session.get(
                        safe_url,
                        headers=headers,
                        timeout=120,
                        allow_redirects=False,
                        stream=True,
                    )
                    status_code = int(getattr(response, "status_code", 200))
                    if status_code in {401, 403}:
                        self.mineru_circuit_breaker.open(
                            reason="artifact_authorization_rejected",
                            status_code=status_code,
                        )
                        raise ProviderCircuitOpen(
                            f"MinerU artifact authorization rejected with HTTP {status_code}",
                            snapshot=self.mineru_circuit_breaker.snapshot,
                        )
                    response.raise_for_status()
                    content_length = getattr(response, "headers", {}).get("Content-Length")
                    try:
                        advertised_bytes = int(content_length) if content_length else 0
                    except (TypeError, ValueError):
                        advertised_bytes = 0
                    if advertised_bytes > self.mineru_response_max_bytes:
                        raise MineruArtifactLimitError(
                            "MinerU response exceeds the configured byte limit "
                            f"({advertised_bytes} > {self.mineru_response_max_bytes})"
                        )
                    chunks: list[bytes] = []
                    total_bytes = 0
                    iterator = cast(
                        Callable[..., Iterable[Any]] | None,
                        getattr(response, "iter_content", None),
                    )
                    if callable(iterator):
                        for chunk in iterator(chunk_size=1024 * 1024):
                            if not chunk:
                                continue
                            chunk_bytes = bytes(chunk)
                            total_bytes += len(chunk_bytes)
                            if total_bytes > self.mineru_response_max_bytes:
                                raise MineruArtifactLimitError(
                                    "MinerU response exceeded the configured byte limit "
                                    f"({total_bytes} > {self.mineru_response_max_bytes})"
                                )
                            chunks.append(chunk_bytes)
                        return b"".join(chunks)
                    content = bytes(getattr(response, "content", b"") or b"")
                    if len(content) > self.mineru_response_max_bytes:
                        raise MineruArtifactLimitError(
                            "MinerU response exceeds the configured byte limit "
                            f"({len(content)} > {self.mineru_response_max_bytes})"
                        )
                    return content
                except ProviderCircuitOpen:
                    raise
                except MineruArtifactError:
                    raise
                except Exception as exc:  # pragma: no cover - transport path.
                    last_exception = exc
                    if attempt >= self.mineru_request_max_retries:
                        break
                    time.sleep(self.mineru_retry_backoff_seconds * (attempt + 1))
        finally:
            session.close()
        if last_exception:
            raise last_exception
        return b""

    def _poll_mineru_result(self, batch_id: str, seed_payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        candidate_urls = self._collect_mineru_candidate_urls(seed_payload, batch_id)
        if not candidate_urls:
            return seed_payload

        deadline = time.time() + self.mineru_poll_timeout_seconds
        last_payload: Dict[str, Any] = seed_payload
        success_states = {"done", "success", "succeeded", "completed", "finished"}
        failure_states = {"failed", "error", "cancelled", "canceled"}

        while time.time() < deadline:
            for url in candidate_urls:
                try:
                    payload = self._request_json("get", url)
                except ProviderCircuitOpen:
                    raise
                except Exception:  # pragma: no cover - transport path.
                    continue
                if not payload:
                    continue
                last_payload = payload
                state = str(self._find_first_value(payload, {"status", "state", "task_status", "result_state"}) or "").strip().lower()
                if state in failure_states:
                    raise RuntimeError(f"MinerU task failed with status '{state}'.")
                if self._payload_contains_mineru_result(payload) or state in success_states:
                    return payload
            time.sleep(self.mineru_poll_interval_seconds)

        if self._payload_contains_mineru_result(last_payload):
            return last_payload
        raise TimeoutError(f"Timed out waiting for MinerU batch '{batch_id or 'unknown'}'.")

    def _collect_mineru_candidate_urls(self, payload: Dict[str, Any], batch_id: str) -> List[str]:
        urls: List[str] = []
        for key in ("status_url", "result_url", "extract_result_url", "download_url"):
            value = self._find_first_value(payload, {key})
            if isinstance(value, str) and value.strip():
                urls.append(self._join_base_url(value))
        for template in self.mineru_poll_endpoint_templates:
            if batch_id and "{batch_id}" in template:
                urls.append(self._join_base_url(template.format(batch_id=batch_id)))
        deduped: List[str] = []
        seen = set()
        for url in urls:
            normalized = url.strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            deduped.append(normalized)
        return deduped

    def _payload_contains_mineru_result(self, payload: Any) -> bool:
        if self._find_first_value(payload, {"normalized_md", "md_content", "markdown", "markdown_text"}):
            return True
        if self._find_first_value(payload, {"content_list", "contentList", "page_index"}):
            return True
        if self._find_first_value(payload, {"result_zip_url", "zip_url", "artifact_zip_url", "full_zip_url"}):
            return True
        return False

    def _normalize_mineru_payload(
        self,
        payload: Dict[str, Any],
        baseline_page_diagnostics: List[PageDiagnostics],
        baseline_page_blocks: List[Dict[str, Any]],
        baseline_page_index: List[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        normalized_payload = payload
        structured_url = self._find_first_value(normalized_payload, {"structured_url", "json_url", "content_list_url"})
        if isinstance(structured_url, str) and structured_url.strip():
            try:
                downloaded = self._request_json("get", self._join_base_url(structured_url))
                if downloaded:
                    normalized_payload = downloaded
            except Exception:  # pragma: no cover - transport path.
                pass

        markdown_text = str(
            self._find_first_value(
                normalized_payload,
                {"normalized_md", "md_content", "markdown", "markdown_text", "md"},
            )
            or ""
        ).strip()
        plain_text = str(
            self._find_first_value(
                normalized_payload,
                {"plain_text", "content_text", "document_text"},
            )
            or ""
        ).strip()

        markdown_url = self._find_first_value(
            normalized_payload,
            {"markdown_url", "md_url", "normalized_md_url"},
        )
        if not markdown_text and isinstance(markdown_url, str) and markdown_url.strip():
            try:
                markdown_bytes = self._request_binary(self._join_base_url(markdown_url))
                markdown_text = markdown_bytes.decode("utf-8", errors="ignore").strip()
            except Exception:  # pragma: no cover - transport path.
                pass

        zip_url = self._find_first_value(
            normalized_payload,
            {"result_zip_url", "zip_url", "artifact_zip_url", "full_zip_url"},
        )
        zip_artifacts: Dict[str, Any] = {}
        if isinstance(zip_url, str) and zip_url.strip():
            try:
                zip_artifacts = self._artifacts_from_zip_bytes(self._request_binary(self._join_base_url(zip_url)))
            except MineruArtifactError:
                raise
            except Exception as exc:  # pragma: no cover - transport path.
                self._log(f"MinerU result zip download failed: {exc}", level="warning")
                zip_artifacts = {}

        if not markdown_text:
            markdown_text = str(zip_artifacts.get("markdown_text", "") or "")
        if not plain_text:
            plain_text = str(zip_artifacts.get("plain_text", "") or "")

        source_payload = zip_artifacts.get("structured_payload") or normalized_payload
        content_list = self._find_first_value(source_payload, {"content_list", "contentList"})
        if not content_list:
            content_list = zip_artifacts.get("content_list")

        page_index: List[Dict[str, Any]] = []
        page_index_from_remote = False
        if isinstance(content_list, list):
            page_index = self._build_page_index_from_content_list(content_list, baseline_page_index)
            page_index_from_remote = bool(page_index)
        if not page_index:
            page_index = self._coerce_page_index(zip_artifacts.get("page_index"))
            page_index_from_remote = bool(page_index)
        if not page_index:
            page_index = baseline_page_index

        if not plain_text and page_index and page_index_from_remote:
            plain_text = "\n\n".join(
                item["text"].strip()
                for item in page_index
                if str(item.get("text", "")).strip()
            ).strip()
        if not markdown_text and plain_text:
            markdown_text = self._fallback_markdown_from_text(plain_text)

        if not markdown_text and not plain_text:
            return None

        return {
            "markdown_text": markdown_text,
            "plain_text": plain_text,
            "page_index": page_index or baseline_page_index,
            "page_diagnostics": baseline_page_diagnostics,
            "page_blocks": baseline_page_blocks,
            "structured_payload": self._make_json_safe(source_payload),
        }

    def _artifacts_from_zip_bytes(self, raw_bytes: bytes) -> Dict[str, Any]:
        artifacts: Dict[str, Any] = {}
        if not raw_bytes:
            return artifacts
        if len(raw_bytes) > self.mineru_response_max_bytes:
            raise MineruArtifactLimitError(
                "MinerU archive response exceeds the configured byte limit "
                f"({len(raw_bytes)} > {self.mineru_response_max_bytes})"
            )
        try:
            archive_context = zipfile.ZipFile(io.BytesIO(raw_bytes))
        except (OSError, zipfile.BadZipFile) as exc:
            raise MineruArtifactFormatError("MinerU result is not a valid ZIP archive") from exc
        with archive_context as archive:
            infos = archive.infolist()
            if len(infos) > self.mineru_zip_max_entries:
                raise MineruArtifactLimitError(
                    "MinerU archive contains too many entries "
                    f"({len(infos)} > {self.mineru_zip_max_entries})"
                )
            total_uncompressed = 0
            for info in infos:
                entry_bytes = int(info.file_size or 0)
                compressed_bytes = int(info.compress_size or 0)
                total_uncompressed += entry_bytes
                if entry_bytes > self.mineru_zip_max_entry_bytes:
                    raise MineruArtifactLimitError(
                        f"MinerU archive entry is too large: {info.filename!r}"
                    )
                if total_uncompressed > self.mineru_zip_max_uncompressed_bytes:
                    raise MineruArtifactLimitError(
                        "MinerU archive exceeds the total uncompressed byte limit"
                    )
                if compressed_bytes > 0 and entry_bytes / compressed_bytes > self.mineru_zip_max_compression_ratio:
                    raise MineruArtifactLimitError(
                        f"MinerU archive entry has an excessive compression ratio: {info.filename!r}"
                    )
            markdown_candidate = None
            structured_candidate = None
            page_index_candidate = None
            plain_text_candidate = None
            for name in archive.namelist():
                lowered = name.lower()
                if lowered.endswith("normalized.md") or (lowered.endswith(".md") and markdown_candidate is None):
                    markdown_candidate = name
                elif lowered.endswith("structured.json") or (lowered.endswith(".json") and structured_candidate is None):
                    structured_candidate = name
                elif lowered.endswith("page_index.json"):
                    page_index_candidate = name
                elif lowered.endswith("plain_text.txt"):
                    plain_text_candidate = name
            if markdown_candidate:
                markdown_bytes = archive.read(markdown_candidate)
                if len(markdown_bytes) > self.mineru_text_max_bytes:
                    raise MineruArtifactLimitError(
                        f"MinerU markdown member exceeds {self.mineru_text_max_bytes} bytes"
                    )
                artifacts["markdown_text"] = markdown_bytes.decode("utf-8", errors="ignore")
            if plain_text_candidate:
                plain_text_bytes = archive.read(plain_text_candidate)
                if len(plain_text_bytes) > self.mineru_text_max_bytes:
                    raise MineruArtifactLimitError(
                        f"MinerU plain-text member exceeds {self.mineru_text_max_bytes} bytes"
                    )
                artifacts["plain_text"] = plain_text_bytes.decode("utf-8", errors="ignore")
            if structured_candidate:
                structured_bytes = archive.read(structured_candidate)
                if len(structured_bytes) > self.mineru_json_max_bytes:
                    raise MineruArtifactLimitError(
                        f"MinerU structured JSON exceeds {self.mineru_json_max_bytes} bytes"
                    )
                try:
                    artifacts["structured_payload"] = json.loads(
                        structured_bytes.decode("utf-8", errors="ignore")
                    )
                except (TypeError, ValueError, json.JSONDecodeError) as exc:
                    raise MineruArtifactFormatError(
                        f"MinerU structured JSON is invalid: {structured_candidate!r}"
                    ) from exc
                content_list = self._find_first_value(artifacts["structured_payload"], {"content_list", "contentList"})
                if isinstance(content_list, list):
                    artifacts["content_list"] = content_list
            if page_index_candidate:
                page_index_bytes = archive.read(page_index_candidate)
                if len(page_index_bytes) > self.mineru_json_max_bytes:
                    raise MineruArtifactLimitError(
                        f"MinerU page-index JSON exceeds {self.mineru_json_max_bytes} bytes"
                    )
                try:
                    artifacts["page_index"] = json.loads(
                        page_index_bytes.decode("utf-8", errors="ignore")
                    )
                except (TypeError, ValueError, json.JSONDecodeError) as exc:
                    raise MineruArtifactFormatError(
                        f"MinerU page-index JSON is invalid: {page_index_candidate!r}"
                    ) from exc
        return artifacts

    def _extract_with_docling(
        self,
        pdf_path: str,
        baseline_page_diagnostics: List[PageDiagnostics],
        baseline_page_blocks: List[Dict[str, Any]],
        baseline_page_index: List[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        try:
            with tempfile.TemporaryDirectory(prefix="auto-generate-docling-") as temp_dir:
                output_path = os.path.join(temp_dir, "result.json")
                completed = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "preprocess.docling_worker",
                        os.path.abspath(pdf_path),
                        output_path,
                    ],
                    cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=max(1.0, self.docling_timeout_seconds),
                    check=False,
                )
                if not os.path.isfile(output_path):
                    if completed.returncode != 0:
                        return None
                    raise RuntimeError("Docling worker produced no result artifact")
                with open(output_path, "r", encoding="utf-8") as handle:
                    payload = json.load(handle)
            if not payload.get("ok"):
                self._log(
                    "Docling worker failed: "
                    f"{payload.get('error_type') or 'WorkerError'}: {payload.get('error') or 'unknown error'}",
                    level="warning",
                )
                return None
            worker_result = payload.get("result") or {}
            markdown_text = str(worker_result.get("markdown_text") or "")
            plain_text = str(worker_result.get("plain_text") or markdown_text)
            structured_payload = dict(worker_result.get("structured_payload") or {})

            if not markdown_text and not plain_text:
                return None

            page_index = self._coerce_page_index(structured_payload.get("page_index")) if structured_payload else []
            if not page_index:
                page_index = baseline_page_index

            return {
                "markdown_text": markdown_text,
                "plain_text": plain_text,
                "page_index": page_index,
                "page_diagnostics": baseline_page_diagnostics,
                "page_blocks": baseline_page_blocks,
                "structured_payload": self._make_json_safe(structured_payload),
                "extractor_used": "docling",
                "layout_fidelity": "layout_aware",
                "conversion_used": "native_pdf",
                "used_ocr": False,
            }
        except subprocess.TimeoutExpired:
            self._log(
                f"Docling preprocessing timed out after {self.docling_timeout_seconds:.1f}s.",
                level="warning",
            )
            return None
        except Exception as exc:  # pragma: no cover - optional dependency path.
            self._log(f"Docling preprocessing fallback skipped: {exc}", level="warning")
            return None

    def _extract_with_existing_local_pipeline(self, pdf_path: str) -> Optional[Dict[str, Any]]:
        markdown_text = ""
        extractor_used = "fitz"
        if self.extractor_profile != "fitz":
            try:
                markdown_text = self._extract_with_pymupdf4llm(pdf_path)
                if markdown_text:
                    extractor_used = "pymupdf4llm"
            except Exception as exc:  # pragma: no cover - optional dependency path.
                self._log(f"PyMuPDF4LLM markdown extraction skipped: {exc}", level="warning")

        plain_text, page_diagnostics, page_blocks = self._extract_local_page_data(pdf_path, allow_ocr=True)
        page_index = self._build_page_index(page_blocks, page_diagnostics)

        if not markdown_text:
            markdown_text = self._fallback_markdown_from_text(plain_text)

        if not markdown_text and not plain_text:
            return None

        return {
            "markdown_text": markdown_text,
            "plain_text": plain_text,
            "page_index": page_index,
            "page_diagnostics": page_diagnostics,
            "page_blocks": page_blocks,
            "structured_payload": {
                "pages": page_blocks,
                "page_index": page_index,
            },
            "extractor_used": extractor_used,
            "layout_fidelity": "page_markdown" if extractor_used == "pymupdf4llm" else "page_text",
            "conversion_used": "native_pdf",
            "used_ocr": any(item.used_ocr for item in page_diagnostics),
        }

    def _extract_local_page_data(
        self,
        pdf_path: str,
        allow_ocr: bool,
    ) -> tuple[str, List[PageDiagnostics], List[Dict[str, Any]]]:
        import warnings
        parser_warnings: List[str] = []
        warning_markers = (
            "No common ancestor in structure tree",
            "OCR on page.number=",
            "pixScaleSmooth",
            "Image too small to scale",
            "Line cannot be recognized",
        )
        
        doc = fitz.open(pdf_path)
        plain_parts: List[str] = []
        page_diagnostics: List[PageDiagnostics] = []
        page_blocks: List[Dict[str, Any]] = []

        try:
            with warnings.catch_warnings(record=True) as caught_warnings:
                warnings.simplefilter("always")
                for page_number in range(doc.page_count):
                    page = doc.load_page(page_number)
                    raw_text_value = page.get_text("text")
                    raw_text = raw_text_value if isinstance(raw_text_value, str) else ""
                    image_count = len(page.get_images(full=True))
                    scanned_candidate = len(raw_text.strip()) < 50 and image_count > 0
                    used_ocr = False
                    effective_text = raw_text

                    # 避免 OCR 操作以减少崩溃风险
                    if allow_ocr and self._should_try_ocr(scanned_candidate):
                        ocr_text = self._ocr_page(page)
                        if ocr_text:
                            effective_text = ocr_text
                            used_ocr = True

                    plain_parts.append(f"\n--- Page {page_number + 1} ---\n{effective_text.strip()}\n")
                    page_blocks.append(
                        {
                            "page_number": page_number + 1,
                            "text": effective_text,
                            "image_count": image_count,
                            "blocks": self._make_json_safe(page.get_text("dict")),
                            "parser_warnings": [w for w in parser_warnings if f"page.number={page_number + 1}" in w],
                        }
                    )
                    page_diagnostics.append(
                        PageDiagnostics(
                            page_number=page_number + 1,
                            text_length=len(effective_text.strip()),
                            image_count=image_count,
                            scanned_candidate=scanned_candidate,
                            used_ocr=used_ocr,
                            low_quality=len(effective_text.strip()) < 80,
                        )
                    )

                parser_warnings.extend(
                    str(item.message)
                    for item in caught_warnings
                    if any(marker in str(item.message) for marker in warning_markers)
                )
        finally:
            doc.close()
        
        # 记录解析器警告
        if parser_warnings:
            self._log(
                f"第三方解析器警告（非致命错误）: {len(parser_warnings)} 条",
                level="info"
            )
            for warning in parser_warnings[:3]:  # 只显示前3条
                self._log(f"  - {warning}", level="info")
            if len(parser_warnings) > 3:
                self._log(f"  ... 还有 {len(parser_warnings) - 3} 条警告", level="info")

        return "".join(plain_parts).strip(), page_diagnostics, page_blocks

    # 添加线程锁，确保 PyMuPDF4LLM 在多线程环境中安全使用
    _pymupdf4llm_lock = threading.Lock()

    def _extract_with_pymupdf4llm(self, pdf_path: str) -> str:
        try:
            # 懒加载 pymupdf4llm，仅在需要时导入
            import pymupdf4llm  # type: ignore

            # 捕获可能的崩溃级错误
            try:
                # 使用线程锁确保同一时间只有一个线程使用 PyMuPDF4LLM
                with self._pymupdf4llm_lock:
                    markdown_output = pymupdf4llm.to_markdown(pdf_path)
                    if isinstance(markdown_output, str):
                        return markdown_output
                    if isinstance(markdown_output, list):
                        texts: List[str] = []
                        for item in markdown_output:
                            if isinstance(item, str):
                                texts.append(item)
                            elif isinstance(item, dict):
                                texts.append(str(item.get("text") or item.get("markdown") or ""))
                        return "\n\n".join([item for item in texts if item.strip()])
                    if isinstance(markdown_output, dict):
                        return str(markdown_output.get("text") or markdown_output.get("markdown") or "")
                    return ""
            except Exception as exc:
                self._log(f"PyMuPDF4LLM to_markdown failed: {exc}", level="warning")
                return ""
        except ImportError:
            self._log("PyMuPDF4LLM not available", level="info")
            return ""
        except Exception as exc:
            self._log(f"PyMuPDF4LLM extraction failed: {exc}", level="warning")
            return ""



    def _should_try_docling_fallback(
        self,
        plain_text: str,
        page_diagnostics: Iterable[Any],
    ) -> bool:
        if not str(plain_text or "").strip():
            return True

        diagnostics = list(page_diagnostics or [])
        if not diagnostics:
            return False

        def _flag(item: Any, field: str) -> bool:
            if isinstance(item, dict):
                return bool(item.get(field, False))
            return bool(getattr(item, field, False))

        total_pages = len(diagnostics)
        low_quality_pages = sum(1 for item in diagnostics if _flag(item, "low_quality"))
        scanned_candidate_pages = sum(1 for item in diagnostics if _flag(item, "scanned_candidate"))
        used_ocr_pages = sum(1 for item in diagnostics if _flag(item, "used_ocr"))

        if low_quality_pages / total_pages >= 0.5:
            return True
        if scanned_candidate_pages / total_pages >= 0.25:
            return True
        if used_ocr_pages > 0:
            return True
        return False

    def _normalize_upload_targets(self, value: Any) -> List[str]:
        targets: List[str] = []
        if isinstance(value, list):
            for item in value:
                if isinstance(item, str) and item.strip():
                    targets.append(item.strip())
                elif isinstance(item, dict):
                    for key in ("url", "upload_url", "file_url"):
                        candidate = item.get(key)
                        if isinstance(candidate, str) and candidate.strip():
                            targets.append(candidate.strip())
                            break
        elif isinstance(value, dict):
            for item in value.values():
                targets.extend(self._normalize_upload_targets(item))
        return targets

    def _find_first_value(self, payload: Any, target_keys: Iterable[str]) -> Any:
        lowered_keys = {key.lower() for key in target_keys}
        queue: List[Any] = [payload]
        while queue:
            current = queue.pop(0)
            if isinstance(current, dict):
                for key, value in current.items():
                    if key.lower() in lowered_keys and self._value_present(value):
                        return value
                    queue.append(value)
            elif isinstance(current, list):
                queue.extend(current)
        return None

    def _value_present(self, value: Any) -> bool:
        if value is None:
            return False
        if isinstance(value, str):
            return bool(value.strip())
        if isinstance(value, (list, dict, tuple, set)):
            return bool(value)
        return True

    def _join_base_url(self, value: str) -> str:
        stripped = value.strip()
        if stripped.startswith("http://") or stripped.startswith("https://"):
            return stripped
        return urljoin(f"{self.mineru_base_url}/", stripped.lstrip("/"))

    def _validate_mineru_url(
        self,
        value: str,
        *,
        purpose: str,
        require_mineru_origin: bool = False,
    ) -> str:
        url = self._join_base_url(value)
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise RuntimeError(f"MinerU {purpose} must be an absolute HTTP(S) URL.")
        if require_mineru_origin:
            if not self._is_mineru_origin_url(url):
                raise RuntimeError(f"MinerU {purpose} must use the configured MinerU service origin.")
            return url
        if not (self._is_mineru_origin_url(url) or self._is_allowed_mineru_host(url)):
            raise RuntimeError(
                f"MinerU {purpose} host is not trusted. Set MINERU_ALLOWED_URL_HOSTS to allow expected storage hosts."
            )
        return url

    def _is_mineru_origin_url(self, value: str) -> bool:
        parsed = urlparse(value)
        base = urlparse(self.mineru_base_url)
        return parsed.scheme == base.scheme and parsed.netloc.lower() == base.netloc.lower()

    def _is_allowed_mineru_host(self, value: str) -> bool:
        parsed = urlparse(value)
        hostname = (parsed.hostname or "").lower()
        return bool(parsed.scheme == "https" and hostname and hostname in self.mineru_allowed_url_hosts)

    @staticmethod
    def _is_safe_exact_mineru_host(value: str) -> bool:
        host = str(value or "").strip().lower()
        if not host or "*" in host or "/" in host or ":" in host or "@" in host:
            return False
        if host.startswith(".") or host.endswith(".") or ".." in host:
            return False
        return all(
            part and all(char.isalnum() or char == "-" for char in part)
            for part in host.split(".")
        )

    def _mineru_headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.mineru_api_token}",
            "Content-Type": "application/json",
        }

    def _build_page_index(
        self,
        page_blocks: List[Dict[str, Any]],
        page_diagnostics: List[PageDiagnostics],
    ) -> List[Dict[str, Any]]:
        diagnostics_by_page = {item.page_number: item for item in page_diagnostics}
        page_index: List[Dict[str, Any]] = []
        for block in page_blocks:
            page_number = int(block.get("page_number", 0) or 0)
            diagnostics = diagnostics_by_page.get(page_number)
            text = str(block.get("text", "") or "")
            page_index.append(
                {
                    "page_number": page_number,
                    "text": text,
                    "text_length": len(text.strip()),
                    "image_count": int(block.get("image_count", diagnostics.image_count if diagnostics else 0) or 0),
                    "block_count": len(block.get("blocks", {}).get("blocks", [])) if isinstance(block.get("blocks"), dict) else 0,
                    "scanned_candidate": diagnostics.scanned_candidate if diagnostics else False,
                    "used_ocr": diagnostics.used_ocr if diagnostics else False,
                    "low_quality": diagnostics.low_quality if diagnostics else len(text.strip()) < 80,
                }
            )
        return page_index

    def _build_page_index_from_content_list(
        self,
        content_list: List[Any],
        fallback_page_index: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        aggregated: Dict[int, List[str]] = {}
        for item in content_list:
            if not isinstance(item, dict):
                continue
            page_number = self._coerce_page_number(item)
            if page_number <= 0:
                continue
            text = self._page_text_from_content_item(item)
            if not text:
                continue
            aggregated.setdefault(page_number, []).append(text)

        if not aggregated:
            return fallback_page_index

        fallback_by_page = {
            int(entry.get("page_number", 0) or 0): entry
            for entry in fallback_page_index
            if int(entry.get("page_number", 0) or 0) > 0
        }
        page_index: List[Dict[str, Any]] = []
        for page_number in sorted(aggregated):
            joined = "\n".join(fragment for fragment in aggregated[page_number] if fragment.strip()).strip()
            fallback_entry = fallback_by_page.get(page_number, {})
            page_index.append(
                {
                    "page_number": page_number,
                    "text": joined,
                    "text_length": len(joined),
                    "image_count": int(fallback_entry.get("image_count", 0) or 0),
                    "block_count": int(fallback_entry.get("block_count", 0) or 0),
                    "scanned_candidate": bool(fallback_entry.get("scanned_candidate", False)),
                    "used_ocr": bool(fallback_entry.get("used_ocr", False)),
                    "low_quality": len(joined) < 80,
                }
            )
        return page_index

    def _coerce_page_index(self, value: Any) -> List[Dict[str, Any]]:
        if not isinstance(value, list):
            return []
        page_index: List[Dict[str, Any]] = []
        for item in value:
            if not isinstance(item, dict):
                continue
            page_number = int(item.get("page_number", 0) or 0)
            if page_number <= 0:
                continue
            text = str(item.get("text", "") or "")
            page_index.append(
                {
                    "page_number": page_number,
                    "text": text,
                    "text_length": int(item.get("text_length", len(text.strip())) or len(text.strip())),
                    "image_count": int(item.get("image_count", 0) or 0),
                    "block_count": int(item.get("block_count", 0) or 0),
                    "scanned_candidate": bool(item.get("scanned_candidate", False)),
                    "used_ocr": bool(item.get("used_ocr", False)),
                    "low_quality": bool(item.get("low_quality", len(text.strip()) < 80)),
                }
            )
        return page_index

    def _coerce_page_number(self, item: Dict[str, Any]) -> int:
        if "page_number" in item:
            return _as_int(item.get("page_number"), 0)
        if "page_no" in item:
            return _as_int(item.get("page_no"), 0)
        if "page_num" in item:
            return _as_int(item.get("page_num"), 0)
        if "page_index" in item:
            page_index = _as_int(item.get("page_index"), 0)
            return page_index + 1 if page_index == 0 else page_index
        if "page_idx" in item:
            return _as_int(item.get("page_idx"), -1) + 1
        return 0

    def _page_text_from_content_item(self, item: Dict[str, Any]) -> str:
        for key in ("text", "content", "markdown", "md", "latex", "html"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

    def _plain_text_from_page_blocks(self, page_blocks: List[Dict[str, Any]]) -> str:
        parts: List[str] = []
        for block in page_blocks:
            page_number = block.get("page_number", "")
            text = str(block.get("text", "") or "").strip()
            if not text:
                continue
            parts.append(f"--- Page {page_number} ---\n{text}")
        return "\n\n".join(parts).strip()

    def _fallback_markdown_from_text(self, plain_text: str) -> str:
        if not plain_text.strip():
            return ""
        sections = [block.strip() for block in plain_text.split("--- Page ") if block.strip()]
        if not sections:
            return plain_text
        parts = []
        for section in sections:
            lines = section.splitlines()
            page_marker = lines[0].strip(" -")
            content = "\n".join(lines[1:]).strip()
            parts.append(f"## Page {page_marker}\n\n{content}")
        return "\n\n".join(parts)

    def _select_stage1_input(
        self,
        *,
        markdown_text: str,
        plain_text: str,
        page_index: List[Dict[str, Any]],
        allow_reprocess: bool = True,
    ) -> Stage1InputSelection:
        return select_stage1_input(
            markdown_text=markdown_text,
            plain_text=plain_text,
            page_index=page_index,
            allow_reprocess=allow_reprocess,
        )

    def _write_stage1_selection_artifacts(
        self,
        *,
        selection: Stage1InputSelection,
        artifact_paths: Dict[str, str],
        page_index: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        chunks = self._build_chunks(selection.selected_text, page_index)
        stage1_manifest_payload, stage1_quality_report_payload = self._stage1_selection_payloads(
            selection=selection,
            artifact_paths=artifact_paths,
            chunk_count=len(chunks),
        )
        with open(artifact_paths["stage1_input_path"], "w", encoding="utf-8") as handle:
            handle.write(selection.selected_text)
        with open(artifact_paths["stage1_input_manifest_path"], "w", encoding="utf-8") as handle:
            json.dump(stage1_manifest_payload, handle, ensure_ascii=False, indent=2)
        with open(artifact_paths["stage1_quality_report_path"], "w", encoding="utf-8") as handle:
            json.dump(stage1_quality_report_payload, handle, ensure_ascii=False, indent=2)
        with open(artifact_paths["chunks_path"], "w", encoding="utf-8") as handle:
            json.dump(chunks, handle, ensure_ascii=False, indent=2)
        return chunks

    def _stage1_selection_payloads(
        self,
        *,
        selection: Stage1InputSelection,
        artifact_paths: Dict[str, str],
        chunk_count: int = 0,
    ) -> tuple[Dict[str, Any], Dict[str, Any]]:
        artifacts = {
            "stage1_input": artifact_paths["stage1_input_path"],
            "stage1_input_manifest": artifact_paths["stage1_input_manifest_path"],
            "stage1_text_quality_report": artifact_paths["stage1_quality_report_path"],
            "chunks": artifact_paths["chunks_path"],
        }
        manifest_payload = dict(selection.manifest_payload)
        manifest_completeness = dict(manifest_payload.get("completeness_metrics") or {})
        manifest_completeness["chunk_count"] = int(chunk_count or 0)
        manifest_payload["completeness_metrics"] = manifest_completeness
        manifest_payload["artifacts"] = artifacts
        quality_report_payload = dict(selection.quality_report_payload)
        quality_completeness = dict(quality_report_payload.get("completeness_metrics") or {})
        quality_completeness["chunk_count"] = int(chunk_count or 0)
        quality_report_payload["completeness_metrics"] = quality_completeness
        quality_report_payload["artifacts"] = artifacts
        return manifest_payload, quality_report_payload

    def _load_or_rebuild_stage1_selection(
        self,
        *,
        markdown_text: str,
        plain_text: str,
        page_index: List[Dict[str, Any]],
        artifact_paths: Dict[str, str],
        diagnostics: Dict[str, Any],
        manifest: Dict[str, Any],
        allow_rebuild: bool = True,
    ) -> tuple[str, str, str, List[str], List[Dict[str, Any]]]:
        stage1_paths = [
            artifact_paths["stage1_input_path"],
            artifact_paths["stage1_input_manifest_path"],
            artifact_paths["stage1_quality_report_path"],
        ]
        if all(os.path.exists(path) for path in stage1_paths):
            with open(artifact_paths["stage1_input_path"], "r", encoding="utf-8") as handle:
                stage1_input_text = handle.read()
            with open(artifact_paths["stage1_input_manifest_path"], "r", encoding="utf-8") as handle:
                stage1_manifest = json.load(handle)
            current_selection = self._select_stage1_input(
                markdown_text=markdown_text,
                plain_text=plain_text,
                page_index=page_index,
            )
            if self._stage1_cache_needs_refresh(stage1_manifest, stage1_input_text, current_selection):
                if not allow_rebuild:
                    raise RuntimeError("cached Stage 1 selection is inconsistent with its generation")
                chunks = self._write_stage1_selection_artifacts(
                    selection=current_selection,
                    artifact_paths=artifact_paths,
                    page_index=page_index,
                )
                self._update_stage1_artifact_metadata(
                    selection=current_selection,
                    chunks=chunks,
                    artifact_paths=artifact_paths,
                    diagnostics=diagnostics,
                    manifest=manifest,
                )
                return (
                    current_selection.selected_text,
                    current_selection.selected_source,
                    current_selection.quality_level,
                    current_selection.stage1_quality_reasons,
                    chunks,
                )
            try:
                with open(artifact_paths["chunks_path"], "r", encoding="utf-8") as handle:
                    chunks = json.load(handle)
            except Exception:
                if not allow_rebuild:
                    raise RuntimeError("cached chunks artifact cannot be read")
                chunks = self._build_chunks(stage1_input_text, page_index)
                with open(artifact_paths["chunks_path"], "w", encoding="utf-8") as handle:
                    json.dump(chunks, handle, ensure_ascii=False, indent=2)
            if (
                not self._chunks_use_selected_stage1(chunks)
                and not str(stage1_input_text or "").strip()
                and not str(current_selection.selected_text or "").strip()
            ):
                return (
                    stage1_input_text,
                    str(stage1_manifest.get("selected_text_source") or ""),
                    str(stage1_manifest.get("stage1_quality_level") or ""),
                    list(stage1_manifest.get("stage1_quality_reasons") or []),
                    chunks if isinstance(chunks, list) else [],
                )
            if not self._chunks_use_selected_stage1(chunks):
                if not allow_rebuild:
                    raise RuntimeError("cached chunks artifact is not bound to selected Stage 1 input")
                chunks = self._build_chunks(stage1_input_text, page_index)
                with open(artifact_paths["chunks_path"], "w", encoding="utf-8") as handle:
                    json.dump(chunks, handle, ensure_ascii=False, indent=2)
            return (
                stage1_input_text,
                str(stage1_manifest.get("selected_text_source") or ""),
                str(stage1_manifest.get("stage1_quality_level") or ""),
                list(stage1_manifest.get("stage1_quality_reasons") or []),
                chunks if isinstance(chunks, list) else [],
            )

        selection = self._select_stage1_input(
            markdown_text=markdown_text,
            plain_text=plain_text,
            page_index=page_index,
        )
        chunks = self._write_stage1_selection_artifacts(
            selection=selection,
            artifact_paths=artifact_paths,
            page_index=page_index,
        )
        self._update_stage1_artifact_metadata(
            selection=selection,
            chunks=chunks,
            artifact_paths=artifact_paths,
            diagnostics=diagnostics,
            manifest=manifest,
        )
        return (
            selection.selected_text,
            selection.selected_source,
            selection.quality_level,
            selection.stage1_quality_reasons,
            chunks,
        )

    def _stage1_cache_needs_refresh(
        self,
        stage1_manifest: Dict[str, Any],
        stage1_input_text: str,
        current_selection: Stage1InputSelection,
    ) -> bool:
        if not isinstance(stage1_manifest.get("completeness_metrics"), dict):
            return True
        cached_reasons = sorted(str(reason) for reason in (stage1_manifest.get("stage1_quality_reasons") or []))
        current_reasons = sorted(str(reason) for reason in current_selection.stage1_quality_reasons)
        return any(
            [
                str(stage1_manifest.get("selected_text_source") or "") != current_selection.selected_source,
                str(stage1_manifest.get("stage1_quality_level") or "") != current_selection.quality_level,
                cached_reasons != current_reasons,
                int(stage1_manifest.get("selected_text_length") or 0) != len(current_selection.selected_text),
                str(stage1_input_text or "") != current_selection.selected_text,
            ]
        )

    def _update_stage1_artifact_metadata(
        self,
        *,
        selection: Stage1InputSelection,
        chunks: List[Dict[str, Any]],
        artifact_paths: Dict[str, str],
        diagnostics: Dict[str, Any],
        manifest: Dict[str, Any],
    ) -> None:
        artifact_map = manifest.setdefault("artifacts", {})
        artifact_map.update(
            {
                "stage1_input": artifact_paths["stage1_input_path"],
                "stage1_input_manifest": artifact_paths["stage1_input_manifest_path"],
                "stage1_text_quality_report": artifact_paths["stage1_quality_report_path"],
                "chunks": artifact_paths["chunks_path"],
            }
        )
        manifest.update(
            {
                "chunk_count": len(chunks),
                "selected_text_source": selection.selected_source,
                "stage1_quality_level": selection.quality_level,
                "stage1_quality_reasons": selection.stage1_quality_reasons,
                "stage1_input_path": artifact_paths["stage1_input_path"],
                "stage1_input_manifest_path": artifact_paths["stage1_input_manifest_path"],
                "stage1_quality_report_path": artifact_paths["stage1_quality_report_path"],
            }
        )
        diagnostics.setdefault("artifact_paths", {}).update(artifact_map)
        diagnostics["stage1_input"] = {
            "selected_text_source": selection.selected_source,
            "stage1_quality_level": selection.quality_level,
            "fallback_reason": selection.fallback_reason,
            "stage1_quality_reasons": selection.stage1_quality_reasons,
            "stage1_input_path": artifact_paths["stage1_input_path"],
            "stage1_input_manifest_path": artifact_paths["stage1_input_manifest_path"],
            "stage1_quality_report_path": artifact_paths["stage1_quality_report_path"],
        }
        with open(artifact_paths["manifest_path"], "w", encoding="utf-8") as handle:
            json.dump(manifest, handle, ensure_ascii=False, indent=2)
        with open(artifact_paths["diagnostics_path"], "w", encoding="utf-8") as handle:
            json.dump(diagnostics, handle, ensure_ascii=False, indent=2)

    def _build_chunks(self, stage1_text: str, page_index: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        chunks: List[Dict[str, Any]] = []
        selected_text = str(stage1_text or "").strip()
        if not selected_text:
            return chunks

        page_lookup: Dict[int, str] = {}
        for page in page_index:
            raw_page_number = str(page.get("page_number") or "")
            if raw_page_number.isdigit():
                page_lookup[int(raw_page_number)] = str(page.get("text") or "").strip()
        page_sections = self._split_stage1_text_pages(selected_text)
        if page_sections:
            for index, (page_number, text) in enumerate(page_sections, start=1):
                chunk_text = text.strip() or (
                    page_lookup.get(page_number, "") if page_number is not None else ""
                )
                if not chunk_text:
                    continue
                chunks.append(
                    {
                        "chunk_id": f"stage1-page-{page_number or index}",
                        "page_number": page_number,
                        "page_range": [page_number] if page_number else [],
                        "text": chunk_text[:8000],
                        "source": "selected_stage1_input",
                        "chunk_source": "selected_stage1_input",
                    }
                )
            if chunks:
                return chunks

        chunk_size = 8000
        for index in range(0, len(selected_text), chunk_size):
            text = selected_text[index : index + chunk_size].strip()
            if not text:
                continue
            chunks.append(
                {
                    "chunk_id": f"stage1-{len(chunks) + 1}",
                    "page_number": None,
                    "page_range": [],
                    "text": text,
                    "source": "selected_stage1_input",
                    "chunk_source": "selected_stage1_input",
                }
            )
        return chunks

    def _chunks_use_selected_stage1(self, chunks: Any) -> bool:
        if not isinstance(chunks, list) or not chunks:
            return False
        return all(
            isinstance(chunk, dict)
            and chunk.get("chunk_source") == "selected_stage1_input"
            for chunk in chunks
        )

    def _split_stage1_text_pages(self, text: str) -> List[tuple[Optional[int], str]]:
        sections: List[tuple[Optional[int], str]] = []
        marker = "--- Page "
        if marker in text:
            for section in [block.strip() for block in text.split(marker) if block.strip()]:
                lines = section.splitlines()
                page_number = self._parse_page_number(lines[0] if lines else "")
                content = "\n".join(lines[1:]).strip()
                sections.append((page_number, content))
            return sections
        markdown_marker = "## Page "
        if markdown_marker in text:
            for section in [block.strip() for block in text.split(markdown_marker) if block.strip()]:
                lines = section.splitlines()
                page_number = self._parse_page_number(lines[0] if lines else "")
                content = "\n".join(lines[1:]).strip()
                sections.append((page_number, content))
        return sections

    def _parse_page_number(self, value: str) -> Optional[int]:
        digits = "".join(char for char in str(value or "") if char.isdigit())
        if not digits:
            return None
        try:
            return int(digits)
        except ValueError:
            return None

    def _load_cached_result(
        self,
        pdf_path: str,
        cache_dir: str,
        generation_dir: str,
        markdown_path: str,
        plain_text_path: str,
        page_index_path: str,
        chunks_path: str,
        diagnostics_path: str,
        ocr_diagnostics_path: str,
        ocr_artifact_path: str,
        structured_json_path: str,
        manifest_path: str,
    ) -> Optional[PreprocessResult]:
        try:
            artifact_paths = self._artifact_paths(generation_dir)
            with open(markdown_path, "r", encoding="utf-8") as handle:
                markdown_text = handle.read()
            with open(plain_text_path, "r", encoding="utf-8") as handle:
                plain_text = handle.read()
            with open(page_index_path, "r", encoding="utf-8") as handle:
                page_index = json.load(handle)
            with open(diagnostics_path, "r", encoding="utf-8") as handle:
                diagnostics = json.load(handle)
            with open(manifest_path, "r", encoding="utf-8") as handle:
                manifest = json.load(handle)

            page_diagnostics = [PageDiagnostics(**item) for item in diagnostics.get("page_diagnostics", [])]
            (
                stage1_input_text,
                selected_text_source,
                stage1_quality_level,
                stage1_quality_reasons,
                chunks,
            ) = self._load_or_rebuild_stage1_selection(
                markdown_text=markdown_text,
                plain_text=plain_text,
                page_index=page_index,
                artifact_paths=artifact_paths,
                diagnostics=diagnostics,
                manifest=manifest,
                allow_rebuild=False,
            )
            return PreprocessResult(
                pdf_path=pdf_path,
                cache_dir=cache_dir,
                markdown_path=markdown_path,
                plain_text_path=plain_text_path,
                page_index_path=page_index_path,
                chunks_path=chunks_path,
                diagnostics_path=diagnostics_path,
                ocr_diagnostics_path=ocr_diagnostics_path,
                ocr_artifact_path=ocr_artifact_path,
                structured_json_path=structured_json_path,
                manifest_path=manifest_path,
                stage1_input_path=artifact_paths["stage1_input_path"],
                stage1_input_manifest_path=artifact_paths["stage1_input_manifest_path"],
                stage1_quality_report_path=artifact_paths["stage1_quality_report_path"],
                markdown_text=markdown_text,
                plain_text=plain_text,
                stage1_input_text=stage1_input_text,
                page_index=page_index,
                page_diagnostics=page_diagnostics,
                low_quality=bool(diagnostics.get("low_quality")),
                scanned_like=bool(diagnostics.get("scanned_like")),
                used_ocr=bool(diagnostics.get("used_ocr")),
                extractor_used=str(manifest.get("extractor_used", "fitz")),
                chunk_count=int(manifest.get("chunk_count", len(chunks))),
                local_rag_enabled=bool(manifest.get("local_rag_enabled")),
                local_rag_built=bool(manifest.get("local_rag_built")),
                local_rag_persist_dir=str(manifest.get("local_rag_persist_dir", self.rag_persist_dir)),
                layout_fidelity=str(manifest.get("layout_fidelity", "page_text")),
                conversion_used=str(manifest.get("conversion_used", "native_pdf")),
                mineru_attempted=bool(manifest.get("mineru_attempted")),
                mineru_succeeded=bool(manifest.get("mineru_succeeded")),
                mineru_token_present=bool(manifest.get("mineru_token_present")),
                mineru_remote_requested=bool(manifest.get("mineru_remote_requested")),
                mineru_remote_enabled=bool(manifest.get("mineru_remote_enabled")),
                mineru_base_url=str(manifest.get("mineru_base_url", self.mineru_base_url)),
                selected_text_source=selected_text_source,
                stage1_quality_level=stage1_quality_level,
            )
        except Exception as exc:
            self._log(f"Failed to load preprocess cache for {pdf_path}: {exc}", level="warning")
            return None

    def _cache_is_fresh(self, pdf_path: str, manifest_path: str, *required_files: str) -> bool:
        if not os.path.isfile(manifest_path) or os.path.islink(manifest_path):
            return False
        if not all(os.path.isfile(path) and not os.path.islink(path) for path in required_files):
            return False
        try:
            with open(manifest_path, "r", encoding="utf-8") as handle:
                manifest = json.load(handle)
            if (
                manifest.get("schema_version") != PREPROCESS_MANIFEST_SCHEMA_VERSION
                or manifest.get("implementation_version") != PREPROCESS_IMPLEMENTATION_VERSION
            ):
                return False
            generation_id = str(manifest.get("generation_id") or "")
            if generation_id != os.path.basename(os.path.dirname(manifest_path)):
                return False
            source = self._source_identity(pdf_path)
            if (
                str(manifest.get("canonical_source_path") or "") != source["canonical_path"]
                or str(manifest.get("source_pdf_sha256") or "") != source["sha256"]
                or int(manifest.get("source_pdf_size") or -1) != source["size"]
                or int(manifest.get("source_pdf_mtime_ns") or -1) != source["mtime_ns"]
                or str(manifest.get("source_pdf_file_id") or "") != source["file_id"]
                or str(manifest.get("processing_fingerprint") or "") != self.processing_fingerprint
            ):
                return False
            artifact_hashes = manifest.get("artifact_hashes")
            if not isinstance(artifact_hashes, dict):
                return False
            for path in required_files:
                entry = artifact_hashes.get(os.path.basename(path))
                if not isinstance(entry, dict):
                    return False
                if str(entry.get("relative_path") or "") != os.path.basename(path):
                    return False
                if str(entry.get("schema_version") or "") != PREPROCESS_MANIFEST_SCHEMA_VERSION:
                    return False
                raw_size = entry.get("size")
                if raw_size is None or int(raw_size) != os.path.getsize(path):
                    return False
                if str(entry.get("sha256") or "") != self._file_sha256(path):
                    return False
            return True
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return False

    def _pdf_cache_key(
        self,
        pdf_path: str,
        source_identity: Dict[str, Any] | None = None,
    ) -> str:
        source = source_identity or self._source_identity(pdf_path)
        payload = (
            f"{source['canonical_path']}::{source['size']}::{source['sha256']}"
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def _ocr_available(self) -> bool:
        return shutil.which("tesseract") is not None

    def _should_try_ocr(self, scanned_candidate: bool) -> bool:
        if self.ocr_mode == "off":
            return False
        if self.ocr_mode == "always":
            return self._ocr_available()
        return scanned_candidate and self._ocr_available()

    def _ocr_page(self, page: Any) -> str:
        try:
            source_pdf = str(getattr(getattr(page, "parent", None), "name", "") or "")
            if not source_pdf:
                return ""
            source_pdf = os.path.abspath(source_pdf)
            with tempfile.TemporaryDirectory(prefix="auto-generate-ocr-") as temp_dir:
                output_path = os.path.join(temp_dir, "result.json")
                subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "preprocess.ocr_worker",
                        source_pdf,
                        str(int(page.number)),
                        self.ocr_languages,
                        output_path,
                    ],
                    cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=max(1.0, self.ocr_timeout_seconds),
                    check=False,
                )
                if not os.path.isfile(output_path):
                    return ""
                with open(output_path, "r", encoding="utf-8") as handle:
                    payload = json.load(handle)
            if not payload.get("ok"):
                self._log(
                    "OCR worker failed: "
                    f"{payload.get('error_type') or 'WorkerError'}: {payload.get('error') or 'unknown error'}",
                    level="warning",
                )
                return ""
            return str(payload.get("text") or "")
        except subprocess.TimeoutExpired:
            self._log(f"OCR timed out on page {page.number + 1}", level="warning")
            return ""
        except Exception as exc:  # pragma: no cover - depends on local OCR runtime.
            self._log(f"OCR failed on page {page.number + 1}: {exc}", level="warning")
            return ""

    def _make_json_safe(self, value: Any) -> Any:
        if isinstance(value, bytes):
            return f"<bytes:{len(value)}>"
        if isinstance(value, dict):
            return {str(key): self._make_json_safe(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [self._make_json_safe(item) for item in value]
        if isinstance(value, set):
            return [self._make_json_safe(item) for item in sorted(value, key=str)]
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        return str(value)

    def _maybe_build_local_rag(
        self,
        collection_name: str,
        chunks: List[Dict[str, Any]],
        *,
        source_pdf_sha256: str = "",
        processing_fingerprint: str = "",
    ) -> bool:
        if not self.enable_local_rag or not chunks:
            return False
        if self.rag_backend != "chroma":
            self._log(f"Unsupported local RAG backend: {self.rag_backend}", level="warning")
            return False
        try:
            from rag.local_rag import LocalRAGIndex

            index = LocalRAGIndex(persist_dir=self.rag_persist_dir, logger=self.logger)
            built = index.build_from_chunks(
                collection_name=collection_name,
                chunks=chunks,
                source_pdf_sha256=source_pdf_sha256,
                processing_fingerprint=processing_fingerprint,
                allow_model_download=self.local_rag_allow_model_download,
            )
            if not built:
                self._log("Local RAG skipped because dependencies are unavailable or chunks are empty.", level="info")
            return built
        except Exception as exc:  # pragma: no cover - optional dependency path.
            self._log(f"Local RAG build failed: {exc}", level="warning")
            return False

    def _log(self, message: str, level: str = "info") -> None:
        if not self.logger:
            return
        log_method = getattr(self.logger, level, None)
        if callable(log_method):
            log_method(message)
