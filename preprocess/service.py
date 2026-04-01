"""Structured PDF preprocessing with MinerU-first parsing and local fallbacks."""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import time
import zipfile
from dataclasses import asdict, dataclass
from typing import Any, Dict, Iterable, List, Optional

import fitz  # type: ignore
import requests  # type: ignore


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
    structured_json_path: str
    manifest_path: str
    markdown_text: str
    plain_text: str
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
    mineru_base_url: str


class PreprocessManager:
    """Create stable preprocess artifacts before stage-one AI analysis."""

    def __init__(self, config: Optional[Dict[str, Any]] = None, logger: Any = None):
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
        self.allow_local_parse_fallback = _as_bool(os.getenv("ALLOW_LOCAL_PARSE_FALLBACK", "true"), default=True)

    def prepare_pdf(self, pdf_path: str) -> Optional[PreprocessResult]:
        """Build or reuse cached preprocess artifacts for a PDF."""

        if not pdf_path or not os.path.exists(pdf_path):
            return None
        if not self.enabled:
            return None

        cache_dir = os.path.join(self.cache_root, self._pdf_cache_key(pdf_path))
        artifact_paths = self._artifact_paths(cache_dir)

        required_paths = [
            artifact_paths["manifest_path"],
            artifact_paths["markdown_path"],
            artifact_paths["plain_text_path"],
            artifact_paths["page_index_path"],
            artifact_paths["chunks_path"],
            artifact_paths["diagnostics_path"],
            artifact_paths["structured_json_path"],
        ]
        if (not self.force_rebuild) and self._cache_is_fresh(pdf_path, artifact_paths["manifest_path"], *required_paths[1:]):
            return self._load_cached_result(
                pdf_path=pdf_path,
                cache_dir=cache_dir,
                markdown_path=artifact_paths["markdown_path"],
                plain_text_path=artifact_paths["plain_text_path"],
                page_index_path=artifact_paths["page_index_path"],
                chunks_path=artifact_paths["chunks_path"],
                diagnostics_path=artifact_paths["diagnostics_path"],
                structured_json_path=artifact_paths["structured_json_path"],
                manifest_path=artifact_paths["manifest_path"],
            )

        os.makedirs(cache_dir, exist_ok=True)
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

        chunks = self._build_chunks(markdown_text, page_index)
        low_quality = any(item.low_quality for item in page_diagnostics)
        scanned_like = any(item.scanned_candidate for item in page_diagnostics)
        used_ocr = any(item.used_ocr for item in page_diagnostics) or bool(extraction.get("used_ocr"))
        local_rag_built = self._maybe_build_local_rag(
            collection_name=self._pdf_cache_key(pdf_path),
            chunks=chunks,
        )

        extractor_used = str(extraction.get("extractor_used", "fitz") or "fitz")
        layout_fidelity = str(extraction.get("layout_fidelity", "page_text") or "page_text")
        conversion_used = str(extraction.get("conversion_used", "native_pdf") or "native_pdf")
        mineru_attempted = bool(extraction.get("mineru_attempted"))
        mineru_succeeded = bool(extraction.get("mineru_succeeded"))
        mineru_token_present = bool(extraction.get("mineru_token_present"))
        structured_payload = extraction.get("structured_payload", {})

        diagnostics_payload = {
            "extractor_used": extractor_used,
            "layout_fidelity": layout_fidelity,
            "conversion_used": conversion_used,
            "low_quality": low_quality,
            "scanned_like": scanned_like,
            "used_ocr": used_ocr,
            "ocr_available": self._ocr_available(),
            "local_rag_enabled": self.enable_local_rag,
            "local_rag_built": local_rag_built,
            "mineru_attempted": mineru_attempted,
            "mineru_succeeded": mineru_succeeded,
            "mineru_token_present": mineru_token_present,
            "mineru_base_url": self.mineru_base_url,
            "page_diagnostics": [asdict(item) for item in page_diagnostics],
            "artifact_paths": {
                "normalized_md": artifact_paths["markdown_path"],
                "plain_text": artifact_paths["plain_text_path"],
                "page_index": artifact_paths["page_index_path"],
                "structured": artifact_paths["structured_json_path"],
                "chunks": artifact_paths["chunks_path"],
                "diagnostics": artifact_paths["diagnostics_path"],
                "prepare_manifest": artifact_paths["manifest_path"],
            },
        }
        manifest_payload = {
            "pdf_path": pdf_path,
            "file_size": os.path.getsize(pdf_path),
            "modified_time": os.path.getmtime(pdf_path),
            "extractor_used": extractor_used,
            "layout_fidelity": layout_fidelity,
            "conversion_used": conversion_used,
            "chunk_count": len(chunks),
            "low_quality": low_quality,
            "scanned_like": scanned_like,
            "used_ocr": used_ocr,
            "local_rag_enabled": self.enable_local_rag,
            "local_rag_built": local_rag_built,
            "local_rag_persist_dir": self.rag_persist_dir,
            "mineru_attempted": mineru_attempted,
            "mineru_succeeded": mineru_succeeded,
            "mineru_token_present": mineru_token_present,
            "mineru_base_url": self.mineru_base_url,
            "artifacts": diagnostics_payload["artifact_paths"],
        }

        with open(artifact_paths["markdown_path"], "w", encoding="utf-8") as handle:
            handle.write(markdown_text)
        with open(artifact_paths["plain_text_path"], "w", encoding="utf-8") as handle:
            handle.write(plain_text)
        with open(artifact_paths["page_index_path"], "w", encoding="utf-8") as handle:
            json.dump(page_index, handle, ensure_ascii=False, indent=2)
        with open(artifact_paths["chunks_path"], "w", encoding="utf-8") as handle:
            json.dump(chunks, handle, ensure_ascii=False, indent=2)
        with open(artifact_paths["diagnostics_path"], "w", encoding="utf-8") as handle:
            json.dump(diagnostics_payload, handle, ensure_ascii=False, indent=2)
        with open(artifact_paths["structured_json_path"], "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "pages": page_blocks,
                    "page_index": page_index,
                    "plain_text": plain_text,
                    "markdown_text": markdown_text,
                    "source_payload": self._make_json_safe(structured_payload),
                },
                handle,
                ensure_ascii=False,
                indent=2,
            )
        with open(artifact_paths["manifest_path"], "w", encoding="utf-8") as handle:
            json.dump(manifest_payload, handle, ensure_ascii=False, indent=2)

        return PreprocessResult(
            pdf_path=pdf_path,
            cache_dir=cache_dir,
            markdown_path=artifact_paths["markdown_path"],
            plain_text_path=artifact_paths["plain_text_path"],
            page_index_path=artifact_paths["page_index_path"],
            chunks_path=artifact_paths["chunks_path"],
            diagnostics_path=artifact_paths["diagnostics_path"],
            structured_json_path=artifact_paths["structured_json_path"],
            manifest_path=artifact_paths["manifest_path"],
            markdown_text=markdown_text,
            plain_text=plain_text,
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
            mineru_base_url=self.mineru_base_url,
        )

    def _artifact_paths(self, cache_dir: str) -> Dict[str, str]:
        return {
            "markdown_path": os.path.join(cache_dir, "normalized.md"),
            "plain_text_path": os.path.join(cache_dir, "plain_text.txt"),
            "page_index_path": os.path.join(cache_dir, "page_index.json"),
            "chunks_path": os.path.join(cache_dir, "chunks.json"),
            "diagnostics_path": os.path.join(cache_dir, "diagnostics.json"),
            "structured_json_path": os.path.join(cache_dir, "structured.json"),
            "manifest_path": os.path.join(cache_dir, "prepare_manifest.json"),
        }

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
        if self.parser_mode == "hybrid" and remote_requested:
            remote_enabled = self._should_try_remote_in_hybrid(
                baseline_plain_text=baseline_plain_text,
                baseline_page_diagnostics=baseline_page_diagnostics,
            )
            if not remote_enabled:
                self._log(
                    "Hybrid preprocess kept the local parser because the baseline extraction looked healthy.",
                    level="info",
                )

        effective_mineru_token_present = mineru_token_present if remote_enabled else False

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
                    remote_result["mineru_token_present"] = effective_mineru_token_present
                    return remote_result
            except Exception as exc:  # pragma: no cover - remote integration path.
                self._log(f"MinerU remote parsing failed, falling back to local parser: {exc}", level="warning")

        if remote_enabled and not mineru_token_present:
            self._log("MinerU remote parsing skipped because MINERU_API_TOKEN is not configured.", level="info")

        if not self.allow_local_parse_fallback and remote_enabled:
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
                "mineru_token_present": effective_mineru_token_present,
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
        local_result["mineru_token_present"] = effective_mineru_token_present
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
        docling_result = self._extract_with_docling(
            pdf_path=pdf_path,
            baseline_page_diagnostics=baseline_page_diagnostics,
            baseline_page_blocks=baseline_page_blocks,
            baseline_page_index=baseline_page_index,
        )
        if docling_result:
            return docling_result

        local_result = self._extract_with_existing_local_pipeline(pdf_path)
        if local_result:
            return local_result

        legacy_result = self._extract_with_legacy_pdf_extractor(
            pdf_path=pdf_path,
            baseline_plain_text=baseline_plain_text,
            baseline_page_diagnostics=baseline_page_diagnostics,
            baseline_page_blocks=baseline_page_blocks,
            baseline_page_index=baseline_page_index,
        )
        if legacy_result:
            return legacy_result

        return None

    def _extract_with_mineru_remote(
        self,
        pdf_path: str,
        baseline_page_diagnostics: List[PageDiagnostics],
        baseline_page_blocks: List[Dict[str, Any]],
        baseline_page_index: List[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
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

        with open(pdf_path, "rb") as handle:
            pdf_bytes = handle.read()

        for target in upload_targets:
            response = requests.put(target, data=pdf_bytes, timeout=120)
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
        return normalized

    def _request_json(self, method: str, url: str, **kwargs: Any) -> Optional[Dict[str, Any]]:
        last_exception: Optional[Exception] = None
        for attempt in range(self.mineru_request_max_retries + 1):
            try:
                response = requests.request(
                    method=method.upper(),
                    url=url,
                    headers=self._mineru_headers(),
                    timeout=60,
                    **kwargs,
                )
                response.raise_for_status()
                return response.json()
            except Exception as exc:  # pragma: no cover - transport path.
                last_exception = exc
                if attempt >= self.mineru_request_max_retries:
                    break
                time.sleep(self.mineru_retry_backoff_seconds * (attempt + 1))
        if last_exception:
            raise last_exception
        return None

    def _request_binary(self, url: str) -> bytes:
        last_exception: Optional[Exception] = None
        for attempt in range(self.mineru_request_max_retries + 1):
            try:
                response = requests.get(url, headers=self._mineru_headers(), timeout=120)
                response.raise_for_status()
                return response.content
            except Exception as exc:  # pragma: no cover - transport path.
                last_exception = exc
                if attempt >= self.mineru_request_max_retries:
                    break
                time.sleep(self.mineru_retry_backoff_seconds * (attempt + 1))
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
        if self._find_first_value(payload, {"result_zip_url", "zip_url", "artifact_zip_url"}):
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
            {"result_zip_url", "zip_url", "artifact_zip_url"},
        )
        zip_artifacts: Dict[str, Any] = {}
        if isinstance(zip_url, str) and zip_url.strip():
            try:
                zip_artifacts = self._artifacts_from_zip_bytes(self._request_binary(self._join_base_url(zip_url)))
            except Exception:  # pragma: no cover - transport path.
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
        if isinstance(content_list, list):
            page_index = self._build_page_index_from_content_list(content_list, baseline_page_index)
        if not page_index:
            page_index = self._coerce_page_index(zip_artifacts.get("page_index")) or baseline_page_index

        if not plain_text and page_index:
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
        with zipfile.ZipFile(io.BytesIO(raw_bytes)) as archive:
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
                artifacts["markdown_text"] = archive.read(markdown_candidate).decode("utf-8", errors="ignore")
            if plain_text_candidate:
                artifacts["plain_text"] = archive.read(plain_text_candidate).decode("utf-8", errors="ignore")
            if structured_candidate:
                artifacts["structured_payload"] = json.loads(
                    archive.read(structured_candidate).decode("utf-8", errors="ignore")
                )
                content_list = self._find_first_value(artifacts["structured_payload"], {"content_list", "contentList"})
                if isinstance(content_list, list):
                    artifacts["content_list"] = content_list
            if page_index_candidate:
                artifacts["page_index"] = json.loads(
                    archive.read(page_index_candidate).decode("utf-8", errors="ignore")
                )
        return artifacts

    def _extract_with_docling(
        self,
        pdf_path: str,
        baseline_page_diagnostics: List[PageDiagnostics],
        baseline_page_blocks: List[Dict[str, Any]],
        baseline_page_index: List[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        try:
            from docling.document_converter import DocumentConverter  # type: ignore
        except Exception:
            return None

        try:
            converter = DocumentConverter()
            result = converter.convert(pdf_path)
            document = getattr(result, "document", None)
            if document is None:
                return None

            markdown_text = ""
            if hasattr(document, "export_to_markdown"):
                markdown_text = str(document.export_to_markdown() or "")

            plain_text = ""
            if hasattr(document, "export_to_text"):
                plain_text = str(document.export_to_text() or "")
            if not plain_text:
                plain_text = markdown_text

            structured_payload: Dict[str, Any] = {}
            if hasattr(document, "export_to_dict"):
                exported = document.export_to_dict()
                if isinstance(exported, dict):
                    structured_payload = exported

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
        doc = fitz.open(pdf_path)
        plain_parts: List[str] = []
        page_diagnostics: List[PageDiagnostics] = []
        page_blocks: List[Dict[str, Any]] = []

        try:
            for page_number in range(doc.page_count):
                page = doc.load_page(page_number)
                raw_text_value = page.get_text("text")
                raw_text = raw_text_value if isinstance(raw_text_value, str) else ""
                image_count = len(page.get_images(full=True))
                scanned_candidate = len(raw_text.strip()) < 50 and image_count > 0
                used_ocr = False
                effective_text = raw_text

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
        finally:
            doc.close()

        return "".join(plain_parts).strip(), page_diagnostics, page_blocks

    def _extract_with_pymupdf4llm(self, pdf_path: str) -> str:
        import pymupdf4llm  # type: ignore

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

    def _extract_with_legacy_pdf_extractor(
        self,
        pdf_path: str,
        baseline_plain_text: str,
        baseline_page_diagnostics: List[PageDiagnostics],
        baseline_page_blocks: List[Dict[str, Any]],
        baseline_page_index: List[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        try:
            from pdf_extractor import extract_text_from_pdf  # type: ignore
        except Exception:
            return None

        try:
            plain_text = str(extract_text_from_pdf(pdf_path) or "").strip()
        except Exception as exc:
            self._log(f"Legacy pdf_extractor fallback failed: {exc}", level="warning")
            return None

        if not plain_text:
            plain_text = baseline_plain_text
        if not plain_text:
            return None

        page_index = baseline_page_index
        if not page_index:
            page_index = [
                {
                    "page_number": 1,
                    "text": plain_text,
                    "text_length": len(plain_text),
                    "image_count": 0,
                    "block_count": 0,
                    "scanned_candidate": False,
                    "used_ocr": False,
                    "low_quality": len(plain_text) < 80,
                }
            ]

        return {
            "markdown_text": self._fallback_markdown_from_text(plain_text),
            "plain_text": plain_text,
            "page_index": page_index,
            "page_diagnostics": baseline_page_diagnostics,
            "page_blocks": baseline_page_blocks,
            "structured_payload": {
                "pages": baseline_page_blocks,
                "page_index": page_index,
            },
            "extractor_used": "legacy_pdf_extractor",
            "layout_fidelity": "plain_text_only",
            "conversion_used": "native_pdf",
            "used_ocr": any(item.used_ocr for item in baseline_page_diagnostics),
        }

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
        if value.startswith("http://") or value.startswith("https://"):
            return value
        return f"{self.mineru_base_url}/{value.lstrip('/')}"

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

    def _build_chunks(self, markdown_text: str, page_index: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        chunks: List[Dict[str, Any]] = []
        for page in page_index:
            text = str(page.get("text", "") or "").strip()
            if not text:
                continue
            chunks.append(
                {
                    "chunk_id": f"page-{page.get('page_number')}",
                    "page_number": page.get("page_number"),
                    "text": text[:8000],
                    "source": "page_index",
                }
            )
        if not chunks and markdown_text.strip():
            chunks.append(
                {
                    "chunk_id": "markdown-1",
                    "page_number": None,
                    "text": markdown_text[:8000],
                    "source": "markdown",
                }
            )
        return chunks

    def _load_cached_result(
        self,
        pdf_path: str,
        cache_dir: str,
        markdown_path: str,
        plain_text_path: str,
        page_index_path: str,
        chunks_path: str,
        diagnostics_path: str,
        structured_json_path: str,
        manifest_path: str,
    ) -> Optional[PreprocessResult]:
        try:
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
            return PreprocessResult(
                pdf_path=pdf_path,
                cache_dir=cache_dir,
                markdown_path=markdown_path,
                plain_text_path=plain_text_path,
                page_index_path=page_index_path,
                chunks_path=chunks_path,
                diagnostics_path=diagnostics_path,
                structured_json_path=structured_json_path,
                manifest_path=manifest_path,
                markdown_text=markdown_text,
                plain_text=plain_text,
                page_index=page_index,
                page_diagnostics=page_diagnostics,
                low_quality=bool(diagnostics.get("low_quality")),
                scanned_like=bool(diagnostics.get("scanned_like")),
                used_ocr=bool(diagnostics.get("used_ocr")),
                extractor_used=str(manifest.get("extractor_used", "fitz")),
                chunk_count=int(manifest.get("chunk_count", 0)),
                local_rag_enabled=bool(manifest.get("local_rag_enabled")),
                local_rag_built=bool(manifest.get("local_rag_built")),
                local_rag_persist_dir=str(manifest.get("local_rag_persist_dir", self.rag_persist_dir)),
                layout_fidelity=str(manifest.get("layout_fidelity", "page_text")),
                conversion_used=str(manifest.get("conversion_used", "native_pdf")),
                mineru_attempted=bool(manifest.get("mineru_attempted")),
                mineru_succeeded=bool(manifest.get("mineru_succeeded")),
                mineru_token_present=bool(manifest.get("mineru_token_present")),
                mineru_base_url=str(manifest.get("mineru_base_url", self.mineru_base_url)),
            )
        except Exception as exc:
            self._log(f"Failed to load preprocess cache for {pdf_path}: {exc}", level="warning")
            return None

    def _cache_is_fresh(self, pdf_path: str, manifest_path: str, *required_files: str) -> bool:
        if not os.path.exists(manifest_path):
            return False
        if not all(os.path.exists(path) for path in required_files):
            return False
        try:
            with open(manifest_path, "r", encoding="utf-8") as handle:
                manifest = json.load(handle)
            return (
                manifest.get("file_size") == os.path.getsize(pdf_path)
                and abs(float(manifest.get("modified_time", 0.0)) - os.path.getmtime(pdf_path)) < 0.001
            )
        except Exception:
            return False

    def _pdf_cache_key(self, pdf_path: str) -> str:
        stat = os.stat(pdf_path)
        payload = f"{os.path.abspath(pdf_path)}::{stat.st_size}::{stat.st_mtime}".encode("utf-8")
        return hashlib.md5(payload).hexdigest()

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
            textpage = page.get_textpage_ocr(language=self.ocr_languages, dpi=300, full=True)
            ocr_text = page.get_text("text", textpage=textpage)
            return ocr_text if isinstance(ocr_text, str) else ""
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

    def _maybe_build_local_rag(self, collection_name: str, chunks: List[Dict[str, Any]]) -> bool:
        if not self.enable_local_rag or not chunks:
            return False
        if self.rag_backend != "chroma":
            self._log(f"Unsupported local RAG backend: {self.rag_backend}", level="warning")
            return False
        try:
            from rag.local_rag import LocalRAGIndex

            index = LocalRAGIndex(persist_dir=self.rag_persist_dir, logger=self.logger)
            built = index.build_from_chunks(collection_name=collection_name, chunks=chunks)
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
